"""ログ整形 / 時刻フォーマット系の小道具。"""

from __future__ import annotations

from datetime import datetime, timezone

import requests


def truncate_for_log(text: str, limit: int = 500) -> str:
    normalized = text.replace("\r\n", "\n").strip()
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[:limit]}... (truncated, total={len(normalized)} chars)"


def response_body_for_log(response: requests.Response | None, limit: int = 1000) -> str:
    if response is None:
        return ""
    try:
        body = response.text
    except Exception:
        return "<failed to read response body>"
    if len(body) <= limit:
        return body
    return f"{body[:limit]}... (truncated, total={len(body)} chars)"


def format_taken_at(taken_at: int) -> str:
    return (
        datetime.fromtimestamp(taken_at, tz=timezone.utc)
        .astimezone()
        .strftime("%Y-%m-%d %H:%M:%S %Z")
    )
