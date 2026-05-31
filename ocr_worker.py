from __future__ import annotations

import argparse
import base64
import curses
import hashlib
import json
import logging
import os
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests

try:
    from PIL import Image
except ImportError:
    Image = None


DEFAULT_PROMPT = """You are performing OCR on an Instagram image.
Extract all visible text as faithfully as possible.
Do not translate, rewrite, summarize, or normalize the text.
Return the original text exactly as it appears.
Keep line breaks when they are visually meaningful.
Also decide whether this should be treated as a PR post.
Set is_pr=true when the image strongly suggests PR content, especially when:
- the image includes #PR
- the image suggests a product or purchase link, including a clipboard/link-copy icon, "link in bio", URL-like notation, shop guidance, or similar cues
- the image prominently presents prices, discounts, sale amounts, campaign offers, coupon-style amounts, or other purchase-oriented money information
Also describe the visual background briefly in background.
- Focus on non-text visual context such as people, objects, layout, scenery, colors, product shots, screenshots, UI elements, and overall composition.
- Keep background concise, usually 1-3 short sentences.
- Write background in Japanese.
Also provide profile_estimate in Japanese as a free-form estimate of the poster/account style or likely profile.
- Keep it short, usually 1-3 short sentences.
- Treat it as a tentative estimate, not a fact.
- If there is not enough information, say so.
- Avoid highly sensitive traits or overconfident claims.
If the image truly contains no readable text, set no_text_detected=true.
Set no_text_detected=false whenever there is any readable text, even if partial.
Return only valid JSON in exactly this shape:
{"text":"<extracted text>","background":"<brief visual background>","profile_estimate":"<tentative profile estimate>","is_pr":true,"no_text_detected":false}"""

FALLBACK_PROMPT = """You are performing OCR on an Instagram image.
Extract visible text as faithfully as possible.
Do not translate, rewrite, summarize, or normalize the text.
Return the original text exactly as it appears.
If the image truly contains no readable text, set no_text_detected=true.
Set no_text_detected=false whenever there is any readable text, even if partial.
Also decide whether this should be treated as a PR post.
Set is_pr=true when the image strongly suggests PR content, especially when:
- the image includes #PR
- the image suggests a product or purchase link, including a clipboard/link-copy icon, "link in bio", URL-like notation, shop guidance, or similar cues
- the image prominently presents prices, discounts, sale amounts, campaign offers, coupon-style amounts, or other purchase-oriented money information
Return only valid JSON in exactly this shape:
{"text":"<extracted text>","is_pr":true,"no_text_detected":false}"""


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("ocr_worker")


class DashboardState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.log_lines: deque[tuple[str, int]] = deque(maxlen=200)
        self.processed_per_minute: dict[str, dict[str, int]] = {}
        self.last_cycle_stats: dict[str, Any] = {}
        self.current_item: dict[str, Any] = {}
        self.totals = {"processed": 0, "failed": 0, "skipped": 0}

    def add_log(self, message: str, levelno: int) -> None:
        with self._lock:
            self.log_lines.append((message, levelno))

    def _minute_bucket(self, minute_key: str) -> dict[str, int]:
        bucket = self.processed_per_minute.get(minute_key)
        if bucket is None:
            bucket = {"processed": 0, "failed": 0}
            self.processed_per_minute[minute_key] = bucket
        return bucket

    def increment_processed(self, count: int = 1) -> None:
        minute_key = datetime.now().strftime("%H:%M")
        with self._lock:
            self._minute_bucket(minute_key)["processed"] += count
            self.totals["processed"] += count
            if len(self.processed_per_minute) > 120:
                oldest_keys = sorted(self.processed_per_minute.keys())[:-60]
                for key in oldest_keys:
                    self.processed_per_minute.pop(key, None)

    def increment_failed(self, count: int = 1) -> None:
        minute_key = datetime.now().strftime("%H:%M")
        with self._lock:
            self._minute_bucket(minute_key)["failed"] += count
            self.totals["failed"] += count
            if len(self.processed_per_minute) > 120:
                oldest_keys = sorted(self.processed_per_minute.keys())[:-60]
                for key in oldest_keys:
                    self.processed_per_minute.pop(key, None)

    def increment_skipped(self, count: int = 1) -> None:
        with self._lock:
            self.totals["skipped"] += count

    def set_cycle_stats(self, stats: dict[str, Any]) -> None:
        with self._lock:
            self.last_cycle_stats = stats

    def set_current_item(self, pk: str, taken_at: int, taken_at_text: str) -> None:
        with self._lock:
            self.current_item = {
                "pk": pk,
                "taken_at": taken_at,
                "taken_at_text": taken_at_text,
            }

    def clear_current_item(self) -> None:
        with self._lock:
            self.current_item = {}

    def snapshot(
        self,
    ) -> tuple[list[tuple[str, int]], list[tuple[str, dict[str, int]]], dict[str, Any], dict[str, Any], dict[str, int]]:
        with self._lock:
            logs = list(self.log_lines)
            bars = sorted(self.processed_per_minute.items())[-20:]
            stats = dict(self.last_cycle_stats)
            current_item = dict(self.current_item)
            totals = dict(self.totals)
        return logs, bars, stats, current_item, totals


class DashboardLogHandler(logging.Handler):
    def __init__(self, state: DashboardState):
        super().__init__()
        self.state = state

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:
            message = record.getMessage()
        self.state.add_log(message, record.levelno)


def load_dotenv_file(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()

        if not key or key in os.environ:
            continue

        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]

        os.environ[key] = value


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
    return datetime.fromtimestamp(taken_at, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def wrap_log_entries(
    entries: list[tuple[str, int]],
    width: int,
    max_lines: int,
) -> list[tuple[str, int]]:
    if width <= 1 or max_lines <= 0:
        return []

    wrapped: list[tuple[str, int]] = []
    for message, levelno in reversed(entries):
        parts = message.splitlines() or [message]
        chunked_parts: list[tuple[str, int]] = []
        for part in parts:
            text = part if part else " "
            while text:
                chunked_parts.append((text[:width], levelno))
                text = text[width:]
        if not chunked_parts:
            chunked_parts.append((" ", levelno))
        if len(wrapped) + len(chunked_parts) > max_lines:
            remaining = max_lines - len(wrapped)
            if remaining > 0:
                wrapped.extend(reversed(chunked_parts[-remaining:]))
            break
        wrapped.extend(reversed(chunked_parts))

    return list(reversed(wrapped))


@dataclass
class Config:
    api_base_url: str
    image_host_prefix: str
    api_key: str
    include_done: bool
    ocr_version: str
    lm_studio_url: str
    lm_studio_model: str
    lm_studio_prompt: str
    lm_studio_api_key: str
    idle_sleep_sec: float
    write_interval_sec: float
    request_timeout_sec: int
    lm_studio_timeout_sec: int
    request_retry_count: int
    request_retry_backoff_sec: float
    ocr_retry_count: int
    ocr_retry_backoff_sec: float
    image_max_dim: int
    page_size: int
    max_items_per_cycle: int
    concurrency: int
    shard_count: int
    shard_index: int

    @classmethod
    def from_env(cls) -> "Config":
        api_base_url = os.getenv("OCR_API_BASE_URL", "").strip().rstrip("/")
        image_host_prefix = os.getenv("OCR_IMAGE_HOST_PREFIX", "").strip().rstrip("/")
        api_key = os.getenv("OCR_API_KEY", "").strip()
        shard_count = max(1, int(os.getenv("OCR_SHARD_COUNT", "1")))
        shard_index = int(os.getenv("OCR_SHARD_INDEX", "0"))

        if not api_base_url:
            raise ValueError("OCR_API_BASE_URL is required.")
        if not image_host_prefix:
            raise ValueError("OCR_IMAGE_HOST_PREFIX is required.")
        if not api_key:
            raise ValueError("OCR_API_KEY is required.")
        if shard_index < 0 or shard_index >= shard_count:
            raise ValueError("OCR_SHARD_INDEX must be greater than or equal to 0 and less than OCR_SHARD_COUNT.")

        return cls(
            api_base_url=api_base_url,
            image_host_prefix=image_host_prefix,
            api_key=api_key,
            include_done=os.getenv("OCR_INCLUDE_DONE", "0").strip() in {"1", "true", "yes", "on"},
            ocr_version=os.getenv("OCR_VERSION", "2026-04-11").strip(),
            lm_studio_url=os.getenv(
                "LM_STUDIO_API_URL",
                "http://127.0.0.1:1234/v1/chat/completions",
            ).strip(),
            lm_studio_model=os.getenv("LM_STUDIO_MODEL", "gemma-3-27b-it").strip(),
            lm_studio_prompt=os.getenv("LM_STUDIO_OCR_PROMPT", DEFAULT_PROMPT).strip(),
            lm_studio_api_key=os.getenv("LM_STUDIO_API_KEY", "lm-studio").strip(),
            idle_sleep_sec=float(os.getenv("IDLE_SLEEP_SEC", "60.0")),
            write_interval_sec=float(os.getenv("WRITE_INTERVAL_SEC", "1.0")),
            request_timeout_sec=int(os.getenv("REQUEST_TIMEOUT_SEC", "30")),
            lm_studio_timeout_sec=int(os.getenv("LM_STUDIO_TIMEOUT_SEC", "180")),
            request_retry_count=max(1, int(os.getenv("REQUEST_RETRY_COUNT", "3"))),
            request_retry_backoff_sec=float(os.getenv("REQUEST_RETRY_BACKOFF_SEC", "2.0")),
            ocr_retry_count=max(1, int(os.getenv("OCR_RETRY_COUNT", "3"))),
            ocr_retry_backoff_sec=float(os.getenv("OCR_RETRY_BACKOFF_SEC", "1.5")),
            image_max_dim=max(256, int(os.getenv("IMAGE_MAX_DIM", "1280"))),
            page_size=max(1, min(100, int(os.getenv("FETCH_PAGE_SIZE", "100")))),
            max_items_per_cycle=max(1, int(os.getenv("MAX_ITEMS_PER_CYCLE", "50"))),
            concurrency=max(1, int(os.getenv("OCR_CONCURRENCY", "1"))),
            shard_count=shard_count,
            shard_index=shard_index,
        )


class OCRWorker:
    def __init__(self, config: Config, dashboard_state: DashboardState | None = None):
        self.config = config
        self.dashboard_state = dashboard_state
        self._write_lock = threading.Lock()

    def _request_with_retry(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        last_exc: requests.RequestException | None = None
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault("X-API-Key", self.config.api_key)

        for attempt in range(1, self.config.request_retry_count + 1):
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
                    self.config.request_retry_count,
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
                    self.config.request_retry_count,
                    method,
                    url,
                    exc,
                )

            if attempt < self.config.request_retry_count:
                sleep_sec = self.config.request_retry_backoff_sec * attempt
                time.sleep(sleep_sec)

        if last_exc is None:
            raise RuntimeError(f"Request failed without exception: {method} {url}")
        raise last_exc

    def fetch_latest_media(self, before_taken_at: int | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": self.config.page_size}
        if before_taken_at is not None:
            params["before_taken_at"] = before_taken_at
        if self.config.include_done:
            params["include_done"] = 1

        response = self._request_with_retry(
            "GET",
            f"{self.config.api_base_url}/media/latest",
            params=params,
            timeout=self.config.request_timeout_sec,
        )
        return response.json()

    def post_ocr_result(
        self,
        pk: str,
        text: str,
        background: str,
        profile_estimate: str,
        is_pr: bool,
        no_text_detected: bool,
    ) -> None:
        payload = {
            "text": text if text.strip() else " ",
            "background": background if background.strip() else " ",
            "profile_estimate": profile_estimate if profile_estimate.strip() else " ",
            "is_pr": is_pr,
            "no_text_detected": no_text_detected,
            "version": self.config.ocr_version,
        }
        logger.info(
            "Posting OCR result pk=%s payload=%s",
            pk,
            json.dumps(payload, ensure_ascii=False),
        )
        response = self._request_with_retry(
            "POST",
            f"{self.config.api_base_url}/media/{pk}",
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=self.config.request_timeout_sec,
        )

    def _build_image_url(self, image_url: str) -> str:
        if image_url.startswith("http://") or image_url.startswith("https://"):
            return image_url
        return urljoin(f"{self.config.image_host_prefix}/", image_url.lstrip("/"))

    def _download_image_as_base64(self, image_url: str) -> str:
        response = requests.get(
            image_url,
            headers={"X-API-Key": self.config.api_key},
            timeout=self.config.request_timeout_sec,
        )
        response.raise_for_status()
        image_bytes = response.content

        if Image is not None:
            try:
                with Image.open(BytesIO(image_bytes)) as img:
                    width, height = img.size
                    max_dim = max(width, height)
                    if max_dim > self.config.image_max_dim:
                        scale = self.config.image_max_dim / max_dim
                        resized = img.resize(
                            (max(1, int(width * scale)), max(1, int(height * scale))),
                            Image.LANCZOS,
                        )
                        output = BytesIO()
                        image_format = (img.format or "JPEG").upper()
                        if image_format not in {"JPEG", "PNG", "WEBP"}:
                            image_format = "JPEG"
                        save_image = resized
                        if image_format == "JPEG" and resized.mode not in {"RGB", "L"}:
                            save_image = resized.convert("RGB")
                        save_image.save(output, format=image_format, quality=90, optimize=True)
                        image_bytes = output.getvalue()
                        logger.info(
                            "Resized image before OCR original=%sx%s resized=%sx%s max_dim=%s",
                            width,
                            height,
                            save_image.size[0],
                            save_image.size[1],
                            self.config.image_max_dim,
                        )
            except Exception as exc:
                logger.warning("Image resize failed, using original image error=%s", exc)

        return base64.b64encode(image_bytes).decode("ascii")

    def _parse_lm_studio_response(self, response_text: str) -> tuple[str, str, str, bool, bool]:
        raw = response_text.strip()

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            start = raw.find("{")
            end = raw.rfind("}")
            if start == -1 or end == -1 or end <= start:
                raise ValueError(f"LM Studio did not return JSON: {truncate_for_log(raw)}")
            parsed = json.loads(raw[start : end + 1])

        if not isinstance(parsed, dict):
            raise ValueError(f"LM Studio returned non-object JSON: {truncate_for_log(raw)}")

        text = str(parsed.get("text", "")).strip()
        background = str(parsed.get("background", "")).strip()
        profile_estimate = str(parsed.get("profile_estimate", "")).strip()
        is_pr = bool(parsed.get("is_pr", False))
        no_text_detected = bool(parsed.get("no_text_detected", False))
        return text, background, profile_estimate, is_pr, no_text_detected

    def _call_lm_studio(self, image_url: str, prompt: str) -> tuple[str, float]:
        started_at = time.perf_counter()
        image_b64 = self._download_image_as_base64(image_url)
        payload = {
            "model": self.config.lm_studio_model,
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
        }

        response = requests.post(
            self.config.lm_studio_url,
            headers={
                "Authorization": f"Bearer {self.config.lm_studio_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=self.config.lm_studio_timeout_sec,
        )
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            logger.error(
                "LM Studio request failed status=%s body=%s",
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

    def _run_lm_studio_ocr_once(self, image_url: str, prompt: str) -> tuple[str, str, str, bool, bool]:
        raw_text, elapsed_sec = self._call_lm_studio(image_url, prompt)
        text, background, profile_estimate, is_pr, no_text_detected = self._parse_lm_studio_response(raw_text)
        logger.info(
            "OCR complete elapsed=%.2fs result=%s",
            elapsed_sec,
            json.dumps(
                {
                    "text": truncate_for_log(text),
                    "background": truncate_for_log(background, limit=300),
                    "profile_estimate": truncate_for_log(profile_estimate, limit=300),
                    "is_pr": is_pr,
                    "no_text_detected": no_text_detected,
                },
                ensure_ascii=False,
            ),
        )
        return text, background, profile_estimate, is_pr, no_text_detected

    def run_test_mode(self, limit: int) -> int:
        payload = self.fetch_latest_media()
        items = payload.get("data", [])[:limit]
        if not items:
            logger.warning(
                "No items returned from /media/latest. "
                "Set OCR_INCLUDE_DONE=1 to also include already-OCR'd items.",
            )
            return 0

        logger.info(
            "Test mode: running OCR on %s item(s). Results will NOT be posted back to the API.",
            len(items),
        )
        logger.info("Prompt in use:\n%s", self.config.lm_studio_prompt)

        for index, item in enumerate(items, start=1):
            pk = str(item["pk"])
            taken_at = int(item["taken_at"])
            image_url = self._build_image_url(str(item["image_url"]))

            logger.info("=" * 80)
            logger.info(
                "[%s/%s] pk=%s taken_at=%s (%s) image=%s",
                index,
                len(items),
                pk,
                taken_at,
                format_taken_at(taken_at),
                image_url,
            )

            try:
                raw_text, elapsed_sec = self._call_lm_studio(image_url, self.config.lm_studio_prompt)
            except (requests.RequestException, ValueError):
                logger.exception("LM Studio call failed pk=%s", pk)
                continue

            logger.info("--- raw response (elapsed=%.2fs) ---\n%s", elapsed_sec, raw_text)

            try:
                text, background, profile_estimate, is_pr, no_text_detected = self._parse_lm_studio_response(raw_text)
            except ValueError as exc:
                logger.error("Failed to parse LM Studio response pk=%s error=%s", pk, exc)
                continue

            logger.info("--- parsed text ---\n%s", text)
            logger.info("--- parsed background ---\n%s", background)
            logger.info("--- parsed profile_estimate ---\n%s", profile_estimate)
            logger.info("is_pr=%s  no_text_detected=%s", is_pr, no_text_detected)

        logger.info("=" * 80)
        logger.info("Test mode complete.")
        return 0

    def run_lm_studio_ocr(self, image_url: str) -> tuple[str, str, str, bool, bool]:
        last_exc: Exception | None = None

        for attempt in range(1, self.config.ocr_retry_count + 1):
            prompt = self.config.lm_studio_prompt if attempt == 1 else FALLBACK_PROMPT
            try:
                text, background, profile_estimate, is_pr, no_text_detected = self._run_lm_studio_ocr_once(
                    image_url,
                    prompt,
                )
            except (requests.RequestException, ValueError) as exc:
                last_exc = exc
                logger.warning(
                    "OCR attempt failed attempt=%s/%s image=%s prompt_mode=%s error=%s",
                    attempt,
                    self.config.ocr_retry_count,
                    image_url,
                    "full" if attempt == 1 else "fallback",
                    exc,
                )
            else:
                if attempt > 1 and not background:
                    background = ""
                if attempt > 1 and not profile_estimate:
                    profile_estimate = ""
                if no_text_detected:
                    return text, background, profile_estimate, is_pr, True
                if text.strip():
                    return text, background, profile_estimate, is_pr, False

                last_exc = ValueError("LM Studio returned empty text without no_text_detected=true")
                logger.warning(
                    "OCR returned empty text without no_text_detected flag attempt=%s/%s image=%s prompt_mode=%s",
                    attempt,
                    self.config.ocr_retry_count,
                    image_url,
                    "full" if attempt == 1 else "fallback",
                )

            if attempt < self.config.ocr_retry_count:
                time.sleep(self.config.ocr_retry_backoff_sec * attempt)

        if last_exc is None:
            raise RuntimeError(f"OCR failed without exception: {image_url}")
        raise last_exc

    def _needs_processing(self, item: dict[str, Any]) -> bool:
        ocr = item.get("ocr")
        if ocr is None:
            return True
        if not isinstance(ocr, dict):
            return True
        return str(ocr.get("version", "")).strip() != self.config.ocr_version

    def _is_assigned_to_this_shard(self, pk: str) -> bool:
        if self.config.shard_count == 1:
            return True
        digest = hashlib.sha256(pk.encode("utf-8")).digest()
        shard = int.from_bytes(digest[:8], byteorder="big") % self.config.shard_count
        return shard == self.config.shard_index

    def _process_media_item(self, item: dict[str, Any]) -> bool:
        pk = str(item["pk"])
        taken_at = int(item["taken_at"])
        full_image_url = self._build_image_url(str(item["image_url"]))
        if self.dashboard_state is not None:
            self.dashboard_state.set_current_item(pk, taken_at, format_taken_at(taken_at))
        logger.info(
            "Processing pk=%s taken_at=%s (%s) image=%s",
            pk,
            taken_at,
            format_taken_at(taken_at),
            full_image_url,
        )

        try:
            text, background, profile_estimate, is_pr, no_text_detected = self.run_lm_studio_ocr(full_image_url)
        except (requests.RequestException, ValueError):
            if self.dashboard_state is not None:
                self.dashboard_state.increment_failed()
                self.dashboard_state.clear_current_item()
            logger.exception("OCR failed pk=%s image=%s", pk, full_image_url)
            return False

        if not text.strip():
            text = " "
        if not background.strip():
            background = " "
        if not profile_estimate.strip():
            profile_estimate = " "

        with self._write_lock:
            try:
                self.post_ocr_result(
                    pk,
                    text,
                    background,
                    profile_estimate,
                    is_pr,
                    no_text_detected,
                )
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
                "Posted OCR result pk=%s taken_at=%s (%s) is_pr=%s no_text_detected=%s",
                pk,
                taken_at,
                format_taken_at(taken_at),
                is_pr,
                no_text_detected,
            )
            time.sleep(self.config.write_interval_sec)
        return True

    def _process_media_batch(self, items: list[dict[str, Any]]) -> int:
        if not items:
            return 0
        if self.config.concurrency == 1 or len(items) == 1:
            return sum(1 for item in items if self._process_media_item(item))

        processed_count = 0
        worker_count = min(self.config.concurrency, len(items))
        logger.info(
            "Processing batch items=%s concurrency=%s",
            len(items),
            worker_count,
        )
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [executor.submit(self._process_media_item, item) for item in items]
            for future in as_completed(futures):
                try:
                    succeeded = future.result()
                except Exception:
                    if self.dashboard_state is not None:
                        self.dashboard_state.increment_failed()
                    logger.exception("Unexpected error while processing OCR batch item")
                    continue
                if succeeded:
                    processed_count += 1
        return processed_count

    def process_cycle(self) -> dict[str, int]:
        processed_count = 0
        skipped_count = 0
        page_count = 0
        before_taken_at: int | None = None
        should_stop = False

        while True:
            page_count += 1
            payload = self.fetch_latest_media(before_taken_at=before_taken_at)
            media_list = payload.get("data", [])
            next_before_taken_at = payload.get("next_before_taken_at")

            if not media_list:
                break

            batch: list[dict[str, Any]] = []
            for item in media_list:
                pk = str(item["pk"])

                if not self._is_assigned_to_this_shard(pk):
                    skipped_count += 1
                    if self.dashboard_state is not None:
                        self.dashboard_state.increment_skipped()
                    logger.info(
                        "Skipping post assigned to another shard pk=%s shard_index=%s shard_count=%s",
                        pk,
                        self.config.shard_index,
                        self.config.shard_count,
                    )
                    continue

                if not self._needs_processing(item):
                    skipped_count += 1
                    if self.dashboard_state is not None:
                        self.dashboard_state.increment_skipped()
                    logger.info(
                        "Skipping already OCR'd post pk=%s version=%s",
                        pk,
                        self.config.ocr_version,
                    )
                    continue

                if processed_count + len(batch) >= self.config.max_items_per_cycle:
                    logger.info(
                        "Reached cycle limit processed=%s queued=%s max_items_per_cycle=%s",
                        processed_count,
                        len(batch),
                        self.config.max_items_per_cycle,
                    )
                    should_stop = True
                    break

                batch.append(item)

            processed_count += self._process_media_batch(batch)

            if should_stop or next_before_taken_at is None:
                break

            before_taken_at = int(next_before_taken_at)

        return {
            "pages": page_count,
            "processed": processed_count,
            "skipped": skipped_count,
        }

    def run_forever(self) -> None:
        logger.info(
            "Starting OCR worker with model=%s concurrency=%s shard_index=%s shard_count=%s",
            self.config.lm_studio_model,
            self.config.concurrency,
            self.config.shard_index,
            self.config.shard_count,
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Continuously fetch Instagram media, run OCR with local LM Studio, and write results back. "
            "Default behavior is the terminal dashboard."
        )
    )
    parser.add_argument(
        "--no-dashboard",
        action="store_true",
        help="Run continuously without the terminal dashboard. Streams logs to stdout (for systemd/headless).",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help=(
            "Test mode: fetch a few items, run OCR with the configured prompt, "
            "print raw and parsed results to stdout, and exit. Does NOT post results to the API."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=3,
        help="Number of items to test in --test mode. Default: 3.",
    )
    return parser.parse_args()


def draw_dashboard(stdscr: Any, state: DashboardState) -> None:
    curses.curs_set(0)
    stdscr.nodelay(True)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN, -1)
    curses.init_pair(2, curses.COLOR_GREEN, -1)
    curses.init_pair(3, curses.COLOR_YELLOW, -1)
    curses.init_pair(4, curses.COLOR_RED, -1)
    curses.init_pair(5, curses.COLOR_WHITE, curses.COLOR_BLUE)

    while True:
        height, width = stdscr.getmaxyx()
        stdscr.erase()

        logs, bars, stats, current_item, totals = state.snapshot()
        header_lines = 4
        graph_height = min(14, max(8, height // 3))
        log_height = max(3, height - graph_height - header_lines - 3)

        stdscr.addnstr(0, 0, " OCR Worker Dashboard ", width - 1, curses.color_pair(5) | curses.A_BOLD)
        stdscr.addnstr(
            1,
            0,
            (
                f"Totals processed={totals.get('processed', 0)} "
                f"failed={totals.get('failed', 0)} skipped={totals.get('skipped', 0)} "
                f"last_cycle={stats}"
            ),
            width - 1,
            curses.color_pair(1),
        )
        if current_item:
            current_line = (
                f"Current pk={current_item.get('pk')} "
                f"taken_at={current_item.get('taken_at')} "
                f"({current_item.get('taken_at_text')})"
            )
        else:
            current_line = "Current idle"
        stdscr.addnstr(2, 0, current_line, width - 1, curses.color_pair(3))
        stdscr.addnstr(3, 0, "Recent Logs", width - 1, curses.A_BOLD)

        wrapped_logs = wrap_log_entries(logs, max(10, width - 1), log_height)
        for idx, entry in enumerate(wrapped_logs, start=4):
            if idx >= 4 + log_height:
                break
            line, levelno = entry
            color = 0
            if levelno >= logging.ERROR:
                color = curses.color_pair(4)
            elif levelno >= logging.WARNING:
                color = curses.color_pair(3)
            elif "Posted OCR result" in line or "Cycle complete" in line:
                color = curses.color_pair(2)
            stdscr.addnstr(idx, 0, line, width - 1, color)

        graph_top = 4 + log_height
        stdscr.addnstr(graph_top, 0, "Per-Minute Throughput", width - 1, curses.A_BOLD)

        chart_top = graph_top + 1
        chart_height = max(4, min(graph_height - 3, height - chart_top - 2))
        chart_width = max(10, width - 8)
        plot_left = 7
        plot_width = max(10, min(chart_width, width - plot_left - 1))
        bar_spacing = 4
        visible_bars = min(len(bars), max(1, plot_width // bar_spacing))
        bars_to_draw = bars[-visible_bars:]
        max_value = max(
            [max(bucket.get("processed", 0), bucket.get("failed", 0)) for _, bucket in bars_to_draw],
            default=1,
        )

        for row in range(chart_height):
            y_value = max_value - int((max_value * row) / max(1, chart_height - 1))
            label = f"{y_value:>4} |"
            stdscr.addnstr(chart_top + row, 0, label, width - 1, curses.color_pair(1))

        baseline_y = chart_top + chart_height
        stdscr.addnstr(baseline_y, 0, "     +" + "-" * max(1, plot_width), width - 1, curses.color_pair(1))

        for idx, (minute, bucket) in enumerate(bars_to_draw):
            x = plot_left + idx * bar_spacing
            if x >= width - 1:
                break
            processed = bucket.get("processed", 0)
            failed = bucket.get("failed", 0)
            processed_height = int((processed / max_value) * chart_height) if max_value else 0
            failed_height = int((failed / max_value) * chart_height) if max_value else 0

            for h in range(processed_height):
                y = baseline_y - 1 - h
                if chart_top <= y < height:
                    stdscr.addnstr(y, x, "|", 1, curses.color_pair(2) | curses.A_BOLD)

            for h in range(failed_height):
                y = baseline_y - 1 - h
                if chart_top <= y < height:
                    stdscr.addnstr(y, x + 1, "|", 1, curses.color_pair(4) | curses.A_BOLD)

            minute_label = minute[-2:]
            if baseline_y + 1 < height and x < width - 1:
                stdscr.addnstr(baseline_y + 1, x, minute_label, min(2, width - x - 1), curses.color_pair(1))

        legend_y = min(height - 1, baseline_y + 2)
        if legend_y < height:
            stdscr.addnstr(legend_y, 0, "green=processed  red=failed", width - 1, curses.A_DIM)

        stdscr.refresh()
        key = stdscr.getch()
        if key in {ord("q"), ord("Q")}:
            break
        time.sleep(1.0)


def run_with_dashboard(worker: OCRWorker, state: DashboardState) -> int:
    thread = threading.Thread(target=worker.run_forever, daemon=True)
    thread.start()
    curses.wrapper(draw_dashboard, state)
    return 0


def main() -> int:
    args = parse_args()
    load_dotenv_file(Path(".env"))

    try:
        config = Config.from_env()
    except ValueError as exc:
        logger.error("%s", exc)
        return 2

    if args.test:
        worker = OCRWorker(config)
        return worker.run_test_mode(args.limit)

    if args.no_dashboard:
        worker = OCRWorker(config)
        worker.run_forever()
        return 0

    dashboard_state = DashboardState()
    root_logger = logging.getLogger()
    for handler in list(root_logger.handlers):
        if isinstance(handler, logging.StreamHandler):
            root_logger.removeHandler(handler)
    for handler in list(logger.handlers):
        if isinstance(handler, logging.StreamHandler):
            logger.removeHandler(handler)
    logger.propagate = False
    handler = DashboardLogHandler(dashboard_state)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)

    worker = OCRWorker(config, dashboard_state=dashboard_state)
    return run_with_dashboard(worker, dashboard_state)


if __name__ == "__main__":
    raise SystemExit(main())
