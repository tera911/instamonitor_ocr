"""OCRWorker: 常駐モード (streaming 連続パイプライン + run_forever) の本体。

LM Studio / API / 画像周りの細かい仕事は api_client / image_loader / llm_client に
分かれており、ここではキュー・スレッド・カウンタ・dashboard 連携など
orchestration だけを持つ。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any

import requests

from .api_client import fetch_latest_media, post_ocr_result
from .config import Config, Endpoint
from .dashboard import DashboardState
from .image_loader import build_image_url
from .llm_client import run_pipeline
from .utils import format_taken_at, response_body_for_log


logger = logging.getLogger(__name__)


class OCRWorker:
    def __init__(self, config: Config, dashboard_state: DashboardState | None = None):
        self.config = config
        self.dashboard_state = dashboard_state
        self._failed_pks_lock = threading.Lock()
        self._failed_pks: set[str] = set()

    def _process_media_item(self, item: dict[str, Any], endpoint: Endpoint) -> bool:
        pk = str(item["pk"])

        with self._failed_pks_lock:
            if pk in self._failed_pks:
                logger.debug("Skipping previously failed pk=%s", pk)
                return False

        taken_at = int(item["taken_at"])
        full_image_url = build_image_url(str(item["image_url"]), self.config)
        if self.dashboard_state is not None:
            self.dashboard_state.set_current_item(pk, taken_at, format_taken_at(taken_at))
        logger.info(
            "Processing pk=%s taken_at=%s (%s) endpoint=%s mode=%s image=%s",
            pk,
            taken_at,
            format_taken_at(taken_at),
            endpoint.url,
            endpoint.mode,
            full_image_url,
        )

        try:
            result = run_pipeline(full_image_url, endpoint, self.config)
        except (requests.RequestException, ValueError):
            with self._failed_pks_lock:
                self._failed_pks.add(pk)
            if self.dashboard_state is not None:
                self.dashboard_state.increment_failed()
                self.dashboard_state.clear_current_item()
            logger.exception(
                "OCR failed pk=%s endpoint=%s image=%s",
                pk,
                endpoint.url,
                full_image_url,
            )
            return False

        try:
            post_ocr_result(pk, result, self.config)
        except requests.RequestException as exc:
            if self.dashboard_state is not None:
                self.dashboard_state.increment_failed()
                self.dashboard_state.clear_current_item()
            status = exc.response.status_code if exc.response is not None else "unknown"
            logger.error(
                "POST failed pk=%s status=%s body=%s",
                pk,
                status,
                response_body_for_log(exc.response),
            )
            return False

        if self.dashboard_state is not None:
            self.dashboard_state.increment_processed()
            self.dashboard_state.clear_current_item()
        logger.info(
            "Posted OCR result pk=%s taken_at=%s (%s) is_pr=%s is_ugc=%s no_text_detected=%s",
            pk,
            taken_at,
            format_taken_at(taken_at),
            result.is_pr,
            result.is_ugc,
            result.no_text_detected,
        )
        return True

    def process_cycle(self) -> dict[str, int]:
        """連続パイプラインで /media/latest を空になるまで処理しきる。

        従来のバッチ barrier (page を処理しきってから次の page を fetch) を撤廃。
        prefetcher thread が queue 残量を監視し、低水位を切ったタイミングで次の page を
        先読みする。worker はバッチ境界を意識せず queue から item を pull し続ける。
        重複 fetch は in_flight_pks セットで弾く (API 側が処理中の pk を再度返してくる
        ことを想定)。
        """
        endpoints = self.config.endpoints
        total_workers = self.config.total_concurrency
        # queue 残量がこの値を下回ったら prefetcher が次の page を取りに行く。
        # 1 ページ分の fetch 中も worker を遊ばせないため worker 数より高めに張る。
        low_watermark = max(1, total_workers * 2)

        item_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        in_flight_pks: set[str] = set()
        in_flight_lock = threading.Lock()
        fetch_done = threading.Event()
        workers_done = threading.Event()
        counter_lock = threading.Lock()
        counters = {"pages": 0, "processed": 0}
        # cycle ローカルの past_offset。当日分はサーバ側で `ocr IS NULL` 系フィルタが
        # 日次で自然にリセットされるカーソルなので offset 不要 (毎回先頭 = 当日最古の
        # 未処理から)。past_offset は過去分だけを対象に、サーバが返した過去分件数
        # (past_count) ずつ前進させる。当日/過去を1本の offset で共有していた旧実装は、
        # 失敗 pk の蓄積で offset が当日分の件数を恒久的に超えてしまい、当日分が
        # 二度と返らなくなる (= 過去分モードから戻れない) 不具合があったため分離した。
        cycle_past_offset = 0
        cycle_offset_lock = threading.Lock()

        def fetch_into_queue() -> tuple[bool, int]:
            nonlocal cycle_past_offset
            with cycle_offset_lock:
                past_offset = cycle_past_offset
            try:
                items, past_count = fetch_latest_media(self.config, past_offset=past_offset)
            except Exception:
                logger.exception(
                    "Prefetch fetch_latest_media failed past_offset=%s", past_offset
                )
                return False, 0
            if not items:
                return True, 0
            with cycle_offset_lock:
                cycle_past_offset += past_count
            added = 0
            in_flight_skipped = 0
            failed_skipped = 0
            with in_flight_lock:
                for item in items:
                    pk = str(item["pk"])
                    if pk in in_flight_pks:
                        in_flight_skipped += 1
                        continue
                    with self._failed_pks_lock:
                        if pk in self._failed_pks:
                            failed_skipped += 1
                            continue
                    in_flight_pks.add(pk)
                    item_queue.put(item)
                    added += 1
            with counter_lock:
                counters["pages"] += 1
            logger.info(
                "Fetched page past_offset=%s items=%s past_count=%s added=%s "
                "in_flight_skipped=%s failed_skipped=%s queue_size=%s",
                past_offset,
                len(items),
                past_count,
                added,
                in_flight_skipped,
                failed_skipped,
                item_queue.qsize(),
            )
            return False, added

        def prefetcher() -> None:
            while not workers_done.is_set():
                if item_queue.qsize() >= low_watermark:
                    time.sleep(0.2)
                    continue
                exhausted, _added = fetch_into_queue()
                if exhausted:
                    fetch_done.set()
                    return
                # added==0 でも cycle_past_offset は前進済み。
                # 全件 failed_skipped の page を歩き抜けて fresh item に辿り着くため、
                # ここでは sleep せず即次の page を取りに行く。

        exhausted, _added = fetch_into_queue()
        if exhausted:
            return {"pages": counters["pages"], "processed": 0, "skipped": 0}

        logger.info(
            "Starting pipeline endpoints=%s workers=%s breakdown=%s",
            len(endpoints),
            total_workers,
            ", ".join(f"{ep.url}={ep.concurrency}" for ep in endpoints),
        )

        prefetch_thread = threading.Thread(
            target=prefetcher, name="ocr-prefetcher", daemon=True
        )
        prefetch_thread.start()

        def run_worker(endpoint: Endpoint) -> None:
            while True:
                try:
                    item = item_queue.get(timeout=0.5)
                except queue.Empty:
                    if fetch_done.is_set() and item_queue.empty():
                        return
                    continue
                pk = str(item["pk"])
                try:
                    succeeded = self._process_media_item(item, endpoint)
                except Exception:
                    if self.dashboard_state is not None:
                        self.dashboard_state.increment_failed()
                    logger.exception("Unexpected error while processing OCR item")
                    succeeded = False
                finally:
                    with in_flight_lock:
                        in_flight_pks.discard(pk)
                    item_queue.task_done()
                if succeeded:
                    with counter_lock:
                        counters["processed"] += 1

        threads: list[threading.Thread] = []
        for ep in endpoints:
            for slot in range(ep.concurrency):
                thread = threading.Thread(
                    target=run_worker,
                    args=(ep,),
                    name=f"ocr-worker[{ep.url}#{slot}]",
                    daemon=True,
                )
                thread.start()
                threads.append(thread)

        for thread in threads:
            thread.join()

        workers_done.set()
        prefetch_thread.join(timeout=5)

        return {"pages": counters["pages"], "processed": counters["processed"], "skipped": 0}

    def run_forever(self) -> None:
        breakdown = ", ".join(
            f"{ep.url}={ep.concurrency}" for ep in self.config.endpoints
        )
        logger.info(
            "Starting OCR worker model=%s ocr_version=%s endpoints=%s total_workers=%s breakdown=%s",
            self.config.lm_studio_model,
            self.config.ocr_version,
            len(self.config.endpoints),
            self.config.total_concurrency,
            breakdown,
        )

        while True:
            try:
                stats = self.process_cycle()
                if self.dashboard_state is not None:
                    self.dashboard_state.set_cycle_stats(stats)
                if stats["processed"] == 0:
                    logger.info(
                        "Cycle complete pages=%s processed=%s skipped=%s idle_sleep=%ss",
                        stats["pages"],
                        stats["processed"],
                        stats["skipped"],
                        self.config.idle_sleep_sec,
                    )
                    time.sleep(self.config.idle_sleep_sec)
                else:
                    logger.info(
                        "Cycle complete pages=%s processed=%s skipped=%s continuing_immediately=true",
                        stats["pages"],
                        stats["processed"],
                        stats["skipped"],
                    )
            except requests.HTTPError as exc:
                body = exc.response.text[:1000] if exc.response is not None else ""
                logger.exception("HTTP error during cycle: %s body=%s", exc, body)
                time.sleep(self.config.idle_sleep_sec)
            except requests.RequestException:
                logger.exception("Network error during cycle")
                time.sleep(self.config.idle_sleep_sec)
            except Exception:
                logger.exception("Unexpected error during cycle")
                time.sleep(self.config.idle_sleep_sec)
