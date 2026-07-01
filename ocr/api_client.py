"""OCR バックエンド API (/media/latest, /media/{pk}) との通信。"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import requests

from .config import Config
from .llm_client import OCRResult
from .utils import response_body_for_log


logger = logging.getLogger(__name__)


def request_with_retry(
    method: str,
    url: str,
    config: Config,
    **kwargs: Any,
) -> requests.Response:
    """指数バックオフ風の手動リトライ付き HTTP 呼び出し。

    4xx のうち 408 / 429 を除いた永久エラーは即時で raise する (= リトライしても
    成功しないため)。それ以外の例外と 5xx は retry_count 回まで再試行する。
    """
    last_exc: requests.RequestException | None = None
    headers = dict(kwargs.pop("headers", {}) or {})
    headers.setdefault("X-API-Key", config.api_key)

    for attempt in range(1, config.request_retry_count + 1):
        try:
            response = requests.request(method, url, headers=headers, **kwargs)
            response.raise_for_status()
            return response
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status is not None and 400 <= status < 500 and status not in {408, 429}:
                raise
            last_exc = exc
            logger.warning(
                "HTTP request failed attempt=%s/%s method=%s url=%s status=%s body=%s",
                attempt,
                config.request_retry_count,
                method,
                url,
                status,
                response_body_for_log(exc.response),
            )
        except requests.RequestException as exc:
            last_exc = exc
            logger.warning(
                "Network request failed attempt=%s/%s method=%s url=%s error=%s",
                attempt,
                config.request_retry_count,
                method,
                url,
                exc,
            )

        if attempt < config.request_retry_count:
            sleep_sec = config.request_retry_backoff_sec * attempt
            time.sleep(sleep_sec)

    if last_exc is None:
        raise RuntimeError(f"Request failed without exception: {method} {url}")
    raise last_exc


def fetch_latest_media(
    config: Config, past_offset: int = 0
) -> tuple[list[dict[str, Any]], int]:
    """当日分 (offset 不要・日次で自然にリセットされるカーソル) + 過去分 (past_offset で
    永続失敗 pk を飛ばして前進) を返す。past_offset は呼び出し側が今回のレスポンスに
    含まれていた過去分の件数 (戻り値の2番目) を積算して次回に渡す。
    """
    params: dict[str, Any] = {
        "limit": config.page_size,
        "current_version": config.ocr_version,
    }
    if past_offset > 0:
        params["past_offset"] = past_offset
    response = request_with_retry(
        "GET",
        f"{config.api_base_url}/media/latest",
        config,
        params=params,
        timeout=config.request_timeout_sec,
    )
    payload = response.json()
    return list(payload.get("data", [])), int(payload.get("past_count", 0))


def post_ocr_result(pk: str, result: OCRResult, config: Config) -> None:
    """API へ OCR 結果を 1 件 POST する。空文字列は半角スペースで NOT NULL 保険。"""
    payload = {
        "text": result.text if result.text.strip() else " ",
        "background": result.background if result.background.strip() else " ",
        "profile_estimate": result.profile_estimate if result.profile_estimate.strip() else " ",
        "is_pr": result.is_pr,
        "is_ugc": result.is_ugc,
        "tags": result.tags,
        "no_text_detected": result.no_text_detected,
        "version": config.ocr_version,
    }
    logger.info(
        "Posting OCR result pk=%s payload=%s",
        pk,
        json.dumps(payload, ensure_ascii=False),
    )
    request_with_retry(
        "POST",
        f"{config.api_base_url}/media/{pk}",
        config,
        headers={"Content-Type": "application/json"},
        json=payload,
        timeout=config.request_timeout_sec,
    )
