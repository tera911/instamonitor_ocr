"""LM Studio / vLLM への 1 タスク呼び出しと、endpoint.mode に応じた pipeline 実行。"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import requests

from .config import Config, Endpoint
from .image_loader import download_image_as_base64
from .postprocess import apply_placeholders
from .tasks import TASK_PIPELINES
from .utils import response_body_for_log, truncate_for_log


logger = logging.getLogger(__name__)


@dataclass
class OCRResult:
    text: str
    background: str
    profile_estimate: str
    is_pr: bool
    is_ugc: bool
    tags: list[str]
    no_text_detected: bool


def call_lm_studio(
    image_b64: str,
    prompt: str,
    schema: dict[str, Any],
    endpoint_url: str,
    config: Config,
    disable_thinking: bool = False,
) -> tuple[str, float]:
    """1 タスク分の LLM 呼び出し。画像はあらかじめ base64 化したものを受け取る。

    同じ画像に対してパイプラインを複数段回すケースで download を 1 回に抑えるため、
    画像取得は呼び出し側 (run_pipeline) に持ち、ここでは送信のみ行う。

    disable_thinking=True で payload に chat_template_kwargs.enable_thinking=False を
    入れる。vLLM の Gemma 系 reasoning モードを切る用途。未知フィールドはサーバー側で
    silently 無視されるので、LM Studio / 旧 vLLM でもそのまま動く。
    """
    started_at = time.perf_counter()
    payload: dict[str, Any] = {
        "model": config.lm_studio_model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{image_b64}",
                        },
                    },
                ],
            }
        ],
        "temperature": 0.0,
        "max_tokens": config.lm_studio_max_tokens,
        "response_format": {
            "type": "json_schema",
            "json_schema": schema,
        },
    }
    if disable_thinking:
        payload["chat_template_kwargs"] = {"enable_thinking": False}

    response = requests.post(
        endpoint_url,
        headers={
            "Authorization": f"Bearer {config.lm_studio_api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=config.lm_studio_timeout_sec,
    )
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        logger.error(
            "LM Studio request failed endpoint=%s status=%s body=%s",
            endpoint_url,
            response.status_code,
            response_body_for_log(response),
        )
        raise exc
    body = response.json()
    raw_text = (
        body.get("choices", [{}])[0]
        .get("message", {})
        .get("content", "")
    )
    elapsed_sec = time.perf_counter() - started_at
    return raw_text, elapsed_sec


# JSON で有効なエスケープシーケンスの直後だけ `\` を許す。それ以外 (例: `\夜`) は
# 不正なので `\\` に倒して救う。
_JSON_VALID_ESCAPE_RE = re.compile(r'\\(?!["\\/bfnrtu])')

# `{"text":"<key>":<value>}` の特殊破損パターン (= schema 構造を勘違いしたケース)。
# `<key>` は識別子っぽい文字列、`<value>` は bool / number / null / 文字列 / 配列 / オブジェクト。
_BROKEN_TEXT_KEY_RE = re.compile(
    r'^\s*\{\s*"text"\s*:\s*"(?P<key>[A-Za-z_][A-Za-z0-9_]*)"\s*:\s*(?P<value>true|false|null|-?\d+(?:\.\d+)?|"[^"]*"|\[[^\]]*\]|\{[^}]*\})\s*\}\s*$'
)

# 4 個以上連続する `\n` (生改行 or エスケープ) を 2 個に圧縮する正規表現。
# JSON 文字列内に裸の改行が入った C パターンと、`\n` エスケープ表記の連続両方に効かせる。
_RAW_RUN_OF_NEWLINES_RE = re.compile(r"(?:\\n){3,}")
_LITERAL_RUN_OF_NEWLINES_RE = re.compile(r"\n{3,}")


def _repair_compress_newlines(raw: str) -> str:
    """連続 `\\n` 暴走を 2 連まで圧縮し、まだ閉じていない文字列を雑に閉じる。

    末尾補完は「すでに `}` で閉じていればそのまま」「文字列値の中で切れていれば
    `"` + `}` を付け足す」だけのシンプルなルール。途中で切れた token を捨てると
    OCR テキストごと失うので、極力中身を残す方向で倒す。
    """
    fixed = _RAW_RUN_OF_NEWLINES_RE.sub(r"\\n\\n", raw)
    fixed = _LITERAL_RUN_OF_NEWLINES_RE.sub("\n\n", fixed)
    stripped = fixed.rstrip()
    if stripped.endswith("}"):
        return stripped
    # 末尾の `\` が奇数本だと「次の文字をエスケープする `\`」が宙ぶらりんで JSON 不正。
    # 1 本だけ落として「直前で escape 終わった」状態に揃える。
    trailing_backslashes = len(stripped) - len(stripped.rstrip("\\"))
    if trailing_backslashes % 2 == 1:
        stripped = stripped[:-1]
    # `"` で終わってるが直前が `\` (= escape された `"`) なら文字列はまだ閉じてない。
    string_already_closed = stripped.endswith('"') and not stripped.endswith('\\"')
    if not string_already_closed:
        stripped += '"'
    stripped += "}"
    return stripped


def _repair_invalid_backslash(raw: str) -> str:
    """`\\夜` のような不正エスケープを `\\\\夜` (= リテラル `\\` + `夜`) に倒す。"""
    return _JSON_VALID_ESCAPE_RE.sub(r"\\\\", raw)


def _repair_broken_text_key(raw: str) -> str | None:
    """`{"text":"key":val}` 形式を `{"text":"","key":val}` に組み直す。"""
    match = _BROKEN_TEXT_KEY_RE.match(raw)
    if match is None:
        return None
    return f'{{"text":"","{match["key"]}":{match["value"]}}}'


def _repair_close_object(raw: str) -> str | None:
    """末尾に `}` だけ足して閉じてみる。値はちゃんと出てるが } を出し忘れただけ用。"""
    stripped = raw.rstrip()
    if stripped.endswith("}"):
        return None
    return stripped + "}"


_REPAIRS: tuple[tuple[str, Any], ...] = (
    ("close_object", _repair_close_object),
    ("compress_newlines", _repair_compress_newlines),
    ("escape_invalid_backslash", _repair_invalid_backslash),
    ("broken_text_key", _repair_broken_text_key),
    (
        "compress_and_escape",
        lambda raw: _repair_invalid_backslash(_repair_compress_newlines(raw)),
    ),
)


def _try_repair_and_parse(raw: str) -> tuple[dict[str, Any] | None, str | None]:
    """壊れた raw に修復チェーンを当てて、最初に parse 成功したものを返す。

    Returns:
        (parsed_dict, applied_repair_name) もしくは (None, None) で全失敗を示す。
    """
    for name, repair in _REPAIRS:
        repaired = repair(raw)
        if repaired is None or repaired == raw:
            continue
        try:
            parsed = json.loads(repaired, strict=False)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed, name
    return None, None


def parse_lm_studio_response(response_text: str) -> dict[str, Any]:
    """LM Studio / vLLM の content 文字列を dict に parse する。

    Structured Outputs で型保証されていても、Gemma 4 12B 系で
    1. 文字列値内の生 `\\` (\\夜 など) が漏れる
    2. `\\n` 暴走で max_tokens 切れ → Unterminated string
    3. schema 構造を勘違いして `{"text":"key":value}` 形式が来る
    ケースが残るので、素朴な json.loads が失敗したら一定の修復を試みる。
    修復で救えなかった場合のみ ValueError に「失敗理由」と「raw response 全文」を
    含めて投げる (--test の report で raw を目視できるよう truncate しない)。
    """
    raw = response_text.strip()
    # Gemma 4 等が schema を無視して ```json ... ``` で包んでくるケースを剥がす。
    if raw.startswith("```"):
        first_newline = raw.find("\n")
        if first_newline != -1:
            raw = raw[first_newline + 1 :].rstrip()
        if raw.endswith("```"):
            raw = raw[:-3].rstrip()

    try:
        parsed = json.loads(raw, strict=False)
    except json.JSONDecodeError as exc:
        # ```json... フェンスを既に剥がしたあと、最終 fallback として最初の { 〜 最後の }
        # を切り出してパースし直す。これも失敗したら修復チェーンに進む。
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            candidate = raw[start : end + 1]
            try:
                parsed = json.loads(candidate, strict=False)
            except json.JSONDecodeError:
                parsed = None
        else:
            parsed = None

        if parsed is None:
            repaired, repair_name = _try_repair_and_parse(raw)
            if repaired is None:
                logger.error(
                    "LM Studio response JSON parse failed error=%s\n"
                    "--- raw response (truncated) ---\n%s\n--- end ---",
                    exc,
                    truncate_for_log(raw, limit=2000),
                )
                raise ValueError(
                    f"LM Studio JSON parse failed ({exc})\n"
                    f"--- raw response ---\n{raw}"
                ) from exc
            logger.warning(
                "LM Studio response was malformed but recovered via repair=%s",
                repair_name,
            )
            parsed = repaired

    if not isinstance(parsed, dict):
        raise ValueError(
            f"LM Studio returned non-object JSON\n--- raw response ---\n{raw}"
        )

    return apply_placeholders(parsed)


def build_ocr_result(accumulator: dict[str, Any]) -> OCRResult:
    """pipeline 全タスクの結果が積まれた accumulator を OCRResult に合成する。

    欠けているフィールド (split パイプラインで no_text_detected による skip 発生時など)
    は dataclass のデフォルト相当の空値で埋める。
    """
    raw_tags = accumulator.get("tags", [])
    if isinstance(raw_tags, list):
        tags = [str(t).strip() for t in raw_tags if str(t).strip()]
    else:
        tags = []
    return OCRResult(
        text=str(accumulator.get("text", "")).strip(),
        background=str(accumulator.get("background", "")).strip(),
        profile_estimate=str(accumulator.get("profile_estimate", "")).strip(),
        is_pr=bool(accumulator.get("is_pr", False)),
        is_ugc=bool(accumulator.get("is_ugc", False)),
        tags=tags,
        no_text_detected=bool(accumulator.get("no_text_detected", False)),
    )


def run_pipeline(image_url: str, endpoint: Endpoint, config: Config) -> OCRResult:
    """endpoint.mode に応じたタスクパイプラインを 1 枚の画像に対して回す。

    画像 download は 1 度だけ。各タスクの結果は accumulator にマージされ、
    次タスクの prompt に {ocr_text} で参照される。no_text_detected=True を立てた
    タスクの後は skip_if_no_text なタスクをスキップする。
    """
    pipeline = TASK_PIPELINES[endpoint.mode]
    image_b64 = download_image_as_base64(image_url, config)

    accumulator: dict[str, Any] = {}
    elapsed_total = 0.0
    for task in pipeline:
        if task.skip_if_no_text and accumulator.get("no_text_detected"):
            logger.info(
                "Skip task=%s endpoint=%s reason=no_text_detected",
                task.name,
                endpoint.url,
            )
            continue
        prompt = task.prompt
        if task.needs_ocr_text:
            ocr_text = str(accumulator.get("text", "")).strip()
            prompt = prompt.replace("{ocr_text}", ocr_text or "(no text)")
        try:
            raw_text, elapsed_sec = call_lm_studio(
                image_b64,
                prompt,
                task.schema,
                endpoint.url,
                config,
                disable_thinking=task.disable_thinking,
            )
        except requests.RequestException as exc:
            # どのタスク段で失敗したかを上層 (report 等) で識別できるよう task 名を含める。
            raise ValueError(f"task={task.name} HTTP/network error: {exc}") from exc
        elapsed_total += elapsed_sec
        try:
            parsed = parse_lm_studio_response(raw_text)
        except ValueError as exc:
            raise ValueError(f"task={task.name}: {exc}") from exc
        accumulator.update(parsed)
        if task.derive_no_text_detected:
            # schema を text 1 つに絞った OCR タスク用。LLM に書かせず Python 側で判定する。
            text_value = str(accumulator.get("text", "")).strip()
            is_empty = text_value == "" or text_value == "<empty>"
            if is_empty:
                accumulator["text"] = ""
            accumulator["no_text_detected"] = is_empty
        logger.info(
            "OCR task=%s endpoint=%s elapsed=%.2fs fields=%s",
            task.name,
            endpoint.url,
            elapsed_sec,
            json.dumps(
                {k: parsed.get(k) for k in task.fields if k in parsed},
                ensure_ascii=False,
                default=str,
            ),
        )

    result = build_ocr_result(accumulator)
    if not result.text.strip() and not result.no_text_detected:
        # 文字を含まない投稿 (写真のみ等) は普通にあり得る。text 空 + no_text_detected=false
        # で返ってきた場合は「読めなかった = テキストなし」として保存する。background /
        # tags / is_ugc などの他フィールドは Structured Outputs で型保証されているので
        # そのまま活かす。
        logger.info(
            "Empty text returned; recording as no_text_detected=true url=%s",
            image_url,
        )
        result = OCRResult(
            text=result.text,
            background=result.background,
            profile_estimate=result.profile_estimate,
            is_pr=result.is_pr,
            is_ugc=result.is_ugc,
            tags=result.tags,
            no_text_detected=True,
        )
    logger.info(
        "OCR complete endpoint=%s mode=%s elapsed=%.2fs result=%s",
        endpoint.url,
        endpoint.mode,
        elapsed_total,
        json.dumps(
            {
                "text": truncate_for_log(result.text),
                "background": truncate_for_log(result.background, limit=300),
                "profile_estimate": truncate_for_log(result.profile_estimate, limit=300),
                "is_pr": result.is_pr,
                "is_ugc": result.is_ugc,
                "tags": result.tags,
                "no_text_detected": result.no_text_detected,
            },
            ensure_ascii=False,
        ),
    )
    return result
