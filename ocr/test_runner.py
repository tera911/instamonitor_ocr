"""`--test` モード本体 (API には書き戻さずに OCR 結果をログ + HTML レポート出力)。

並列化と --skip-no-text-detect 補充ロジックは ThreadPoolExecutor + イテレータ
パターンで実装する。queue や Event を直接さわるよりプリミティブの組み合わせが
少なく、--limit 件「テキスト入り」を集めきった時点で submit を止められる。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

import requests

from .api_client import fetch_latest_media
from .config import Config, Endpoint
from .image_loader import build_image_url
from .llm_client import OCRResult, run_pipeline
from .report import TestItemReport, write_report
from .utils import format_taken_at


logger = logging.getLogger(__name__)


def _process_one(
    item: dict[str, Any],
    endpoint: Endpoint,
    config: Config,
) -> TestItemReport:
    """1 件分の pipeline を走らせて TestItemReport を返す。失敗時も例外を握って報告する。"""
    pk = str(item["pk"])
    taken_at = int(item["taken_at"])
    image_url = build_image_url(str(item["image_url"]), config)

    started = time.perf_counter()
    try:
        result: OCRResult | None = run_pipeline(image_url, endpoint, config)
        error: str | None = None
    except (requests.RequestException, ValueError) as exc:
        logger.exception("OCR pipeline failed pk=%s", pk)
        result = None
        # str(exc) には run_pipeline / parse_lm_studio_response が組み立てた
        # `task=<name>: ...\n--- raw response ---\n<...>` がそのまま入っている。
        # report 側で <pre> 表示するのでここでは整形しない。
        error = str(exc)
    elapsed = time.perf_counter() - started

    return TestItemReport(
        pk=pk,
        taken_at=taken_at,
        taken_at_text=format_taken_at(taken_at),
        image_url=image_url,
        result=result,
        elapsed_sec=elapsed,
        error=error,
    )


def _log_item(item: TestItemReport, index: int, total: int | None, endpoint: Endpoint) -> None:
    """1 件分の OCR 結果を整形してログに出す。

    total が None のとき (= skip-no-text-detect モードで全体件数を確定できない場合) は
    分母なしで連番だけを出す。[N/limit] にしてしまうと「[4/3]」のような違和感が出るため。
    """
    header = f"[{index}/{total}]" if total is not None else f"[{index}]"
    logger.info("=" * 80)
    logger.info(
        "%s pk=%s taken_at=%s (%s) endpoint=%s image=%s elapsed=%.2fs",
        header,
        item.pk,
        item.taken_at,
        item.taken_at_text,
        endpoint.url,
        item.image_url,
        item.elapsed_sec,
    )
    if item.result is None:
        logger.error("pk=%s pipeline failed: %s", item.pk, item.error)
        return
    r = item.result
    logger.info("--- parsed text ---\n%s", r.text)
    logger.info("--- parsed background ---\n%s", r.background)
    logger.info("--- parsed profile_estimate ---\n%s", r.profile_estimate)
    logger.info("--- parsed tags ---\n%s", json.dumps(r.tags, ensure_ascii=False))
    logger.info(
        "is_pr=%s  is_ugc=%s  no_text_detected=%s",
        r.is_pr,
        r.is_ugc,
        r.no_text_detected,
    )


def _collect_target_items(
    page: list[dict[str, Any]],
    target_pks: list[str] | None,
    limit: int,
    skip_no_text_detect: bool,
) -> list[dict[str, Any]]:
    """target_pks / limit / skip_no_text_detect を踏まえて処理候補を返す。

    --skip-no-text-detect 指定時の補充は呼び出し側 (run_test_mode) で行うので、
    ここでは候補プールを返すだけ。--pk 指定時はその pk のみ、それ以外は
    skip_no_text 指定でも一旦ページ全件 (limit 補充の余白を残す) を返す。
    """
    if target_pks is not None:
        target_set = set(target_pks)
        items = [item for item in page if str(item["pk"]) in target_set]
        missing = sorted(target_set - {str(i["pk"]) for i in items})
        if missing:
            logger.warning(
                "--pk specified but not present in /media/latest page (size=%s): %s. "
                "OCR_VERSION を bump すると再配信される可能性あり。",
                len(page),
                missing,
            )
        return items

    if skip_no_text_detect:
        # 補充の余地のためページ全件を渡す。実際の上限制御は executor 側で行う。
        return list(page)
    return page[:limit]


def run_test_mode(
    config: Config,
    limit: int,
    target_pks: list[str] | None = None,
    skip_no_text_detect: bool = False,
    report_dir: Path | None = None,
) -> int:
    """テスト 1 サイクル分の OCR を回す。

    挙動:
    - target_pks 指定: その pk のみ処理 (--limit / --skip-no-text-detect は無視)
    - --skip-no-text-detect: text 入り (= no_text_detected != True) が limit 件
      集まるまで /media/latest 1 ページから順次 submit。ページが尽きたら集まった
      分だけで終了
    - それ以外: ページ先頭 limit 件をそのまま処理 (従来動作)

    並列度は config.endpoints[0].concurrency (= test mode が叩く先頭エンドポイントの
    最大同時接続数)。pipeline 1 件あたり画像 download + LLM call が含まれるので、
    LLM が GPU bound でも HTTP/画像 download の遅延を隠して endpoint を使い切る。
    """
    page, _past_count = fetch_latest_media(config)
    candidates = _collect_target_items(page, target_pks, limit, skip_no_text_detect)
    if not candidates:
        if target_pks is None:
            logger.warning(
                "No items returned from /media/latest. "
                "Bump OCR_VERSION to also re-OCR previously processed posts."
            )
        return 0

    endpoint = config.endpoints[0]
    workers = max(1, endpoint.concurrency)
    logger.info(
        "Test mode: candidates=%s workers=%s endpoint=%s mode=%s "
        "skip_no_text_detect=%s target_pks=%s. Results will NOT be posted back to the API.",
        len(candidates),
        workers,
        endpoint.url,
        endpoint.mode,
        skip_no_text_detect,
        target_pks,
    )

    collected: list[TestItemReport] = []
    started_at = time.perf_counter()

    if target_pks is not None or not skip_no_text_detect:
        # 単純並列: 候補全部を投げて完了順に拾う。順序は pk 並びを保つため
        # submit 順 (= candidates 順) と1対1で対応する future リストで読む。
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ocr-test") as pool:
            futures = [pool.submit(_process_one, item, endpoint, config) for item in candidates]
            for index, future in enumerate(futures, start=1):
                item_report = future.result()
                collected.append(item_report)
                _log_item(item_report, index, len(candidates), endpoint)
    else:
        # skip-no-text-detect モード: text 入りが limit 件集まったら停止。
        # 候補プールを iterator で前から消費し、空きが出るたびに 1 件追加 submit する。
        # こうすると「先頭 limit 件を投げて結果を見て…」より無駄な submit が少なく、
        # かつ「全部投げて完了次第捨てる」より過剰実行を抑えられる。
        collected = _run_skip_no_text(candidates, limit, endpoint, config, workers)

    elapsed_total = time.perf_counter() - started_at

    logger.info("=" * 80)
    logger.info(
        "Test mode complete. items=%s elapsed=%.2fs no_text_detected=%s failed=%s",
        len(collected),
        elapsed_total,
        sum(1 for c in collected if c.result is not None and c.result.no_text_detected),
        sum(1 for c in collected if c.result is None),
    )

    if report_dir is not None and collected:
        meta = {
            "endpoint": endpoint.url,
            "mode": endpoint.mode,
            "workers": workers,
            "skip_no_text_detect": skip_no_text_detect,
            "target_pks": target_pks or [],
            "limit": limit,
            "items_collected": len(collected),
            "elapsed_total_sec": round(elapsed_total, 2),
        }
        try:
            write_report(collected, report_root=report_dir, meta=meta)
        except OSError:
            logger.exception("Failed to write HTML report under %s", report_dir)

    return 0


def _run_skip_no_text(
    candidates: list[dict[str, Any]],
    limit: int,
    endpoint: Endpoint,
    config: Config,
    workers: int,
) -> list[TestItemReport]:
    """text 入りが limit 件集まるまで submit を続けるループ。

    実装上の罠を避けるためのポイント:
    - 候補プールが先に尽きると ``inflight`` が空になり、`while inflight` 抜けに任せる
    - 採用件数の判定は「result が None でなく、かつ no_text_detected が False」
    - キャンセル判断はメインスレッドで lock 越しに見る (collected の中身は ordered で
      ないが、report 時に pk 順 / submit 順は重視しないので OK)
    """
    collected: list[TestItemReport] = []
    accepted = 0
    accepted_lock = threading.Lock()
    pool_iter = iter(candidates)
    log_index = 0

    def take_next() -> dict[str, Any] | None:
        try:
            return next(pool_iter)
        except StopIteration:
            return None

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ocr-test") as pool:
        inflight: dict[Future[TestItemReport], dict[str, Any]] = {}

        # 起動時に workers 分まで submit して GPU 並列を立ち上げる。
        for _ in range(workers):
            nxt = take_next()
            if nxt is None:
                break
            inflight[pool.submit(_process_one, nxt, endpoint, config)] = nxt

        while inflight:
            # 1 つずつ完了を見る。as_completed を使わない理由は、完了したら
            # その場で次を 1 件 submit して inflight に登録したいから (as_completed の
            # snapshot だと「実行中に追加した future」を拾わない)。
            done_futures = [f for f in list(inflight.keys()) if f.done()]
            if not done_futures:
                time.sleep(0.05)
                continue

            for future in done_futures:
                inflight.pop(future, None)
                item_report = future.result()
                log_index += 1
                collected.append(item_report)
                _log_item(item_report, log_index, None, endpoint)

                is_acceptable = (
                    item_report.result is not None
                    and not item_report.result.no_text_detected
                )
                if is_acceptable:
                    with accepted_lock:
                        accepted += 1
                        accepted_now = accepted
                else:
                    accepted_now = accepted

                if accepted_now >= limit:
                    # 既に投入済みの inflight は走り切らせる (途中 cancel しても
                    # GPU 側まで止まらないので益が薄い)。これ以上 submit はしない。
                    continue

                # 補充: 採用がまだ届かない and 候補が残っていれば 1 件追加。
                nxt = take_next()
                if nxt is not None:
                    inflight[pool.submit(_process_one, nxt, endpoint, config)] = nxt

    if accepted < limit:
        remaining_in_pool = sum(1 for _ in pool_iter)
        logger.warning(
            "--skip-no-text-detect collected %s/%s text-bearing items "
            "(page exhausted, pool_remaining=%s). Bump OCR_VERSION or "
            "increase FETCH_PAGE_SIZE if you need more.",
            accepted,
            limit,
            remaining_in_pool,
        )

    return collected
