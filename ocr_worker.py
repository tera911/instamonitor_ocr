from __future__ import annotations

import argparse
import base64
import curses
import json
import logging
import logging.handlers
import os
import queue
import sys
import threading
import time
import tomllib
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

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

Set is_pr=true ONLY when the image visually contains the literal characters "PR" (case-insensitive) as readable text. Otherwise set is_pr=false.

Set is_ugc=true when the image strongly suggests promotional/commerce content, especially when:
- the image suggests a product or purchase link, including a clipboard/link-copy icon, "link in bio", URL-like notation, shop guidance, or similar cues
- the image prominently presents prices, discounts, sale amounts, campaign offers, coupon-style amounts, or other purchase-oriented money information
Otherwise set is_ugc=false.

Also generate tags as a list of short Japanese strings that describe or categorize this post. There are no strict rules — pick whatever tags you think fit (topic, mood, product category, audience, vibe, etc.). Typically 3-10 tags. Each tag is a short noun phrase, no leading "#".

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
{"text":"<extracted text>","background":"<brief visual background>","profile_estimate":"<tentative profile estimate>","is_pr":true,"is_ugc":true,"tags":["tag1","tag2"],"no_text_detected":false}"""


# LM Studio Structured Outputs (0.3.0+) で出力 JSON を文法的に強制する。
# constrained sampling により \ の未エスケープ等の不正 JSON が物理的に出なくなる。
# maxLength / maxItems は GBNF grammar に量化子として落ちて、ループ系の暴走で
# 出力が肥大化するのを構文レベルで止める (推論時間そのものは max_tokens で切る)。
OCR_RESPONSE_SCHEMA: dict[str, Any] = {
    "name": "ocr_result",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "maxLength": 8000},
            "background": {"type": "string", "maxLength": 1000},
            "profile_estimate": {"type": "string", "maxLength": 1000},
            "is_pr": {"type": "boolean"},
            "is_ugc": {"type": "boolean"},
            "tags": {
                "type": "array",
                "maxItems": 20,
                "items": {"type": "string", "maxLength": 64},
            },
            "no_text_detected": {"type": "boolean"},
        },
        "required": [
            "text",
            "background",
            "profile_estimate",
            "is_pr",
            "is_ugc",
            "tags",
            "no_text_detected",
        ],
        "additionalProperties": False,
    },
}


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


def _is_console_handler(handler: logging.Handler) -> bool:
    """stdout / stderr に直結している StreamHandler を判定する。

    FileHandler は StreamHandler を継承しているため、単純な isinstance だと
    file handler まで除外されてしまう。stream 属性で sys.stdout / stderr を
    狙い撃ちすることでファイル系ハンドラを温存する。"""
    if not isinstance(handler, logging.StreamHandler) or isinstance(handler, logging.FileHandler):
        return False
    return getattr(handler, "stream", None) in (sys.stdout, sys.stderr)


def setup_file_logging() -> Path | None:
    raw_path = os.getenv("OCR_LOG_FILE", "").strip()
    if not raw_path:
        return None

    log_path = Path(raw_path).expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    max_bytes = max(1024, int(os.getenv("OCR_LOG_MAX_BYTES", str(10 * 1024 * 1024))))
    backup_count = max(1, int(os.getenv("OCR_LOG_BACKUP_COUNT", "5")))

    file_handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

    level_name = os.getenv("OCR_LOG_FILE_LEVEL", "").strip().upper()
    if level_name:
        level_value = logging.getLevelName(level_name)
        if isinstance(level_value, int):
            file_handler.setLevel(level_value)
        else:
            logger.warning(
                "Unknown OCR_LOG_FILE_LEVEL=%s; falling back to logger level.",
                level_name,
            )

    # `ocr_worker` logger 自身に付ける。dashboard モードで propagate=False にしても
    # ファイル出力が止まらないようにするため。
    logger.addHandler(file_handler)
    logger.info(
        "File logging enabled path=%s maxBytes=%s backupCount=%s level=%s",
        log_path,
        max_bytes,
        backup_count,
        logging.getLevelName(file_handler.level) if file_handler.level else "inherit",
    )
    return log_path


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
class OCRResult:
    text: str
    background: str
    profile_estimate: str
    is_pr: bool
    is_ugc: bool
    tags: list[str]
    no_text_detected: bool


@dataclass(frozen=True)
class Endpoint:
    url: str
    concurrency: int


@dataclass
class Config:
    api_base_url: str
    image_host_prefix: str
    image_local_root: Path | None
    api_key: str
    ocr_version: str
    endpoints: list[Endpoint]
    lm_studio_model: str
    lm_studio_prompt: str
    lm_studio_api_key: str
    idle_sleep_sec: float
    request_timeout_sec: int
    lm_studio_timeout_sec: int
    lm_studio_max_tokens: int
    request_retry_count: int
    request_retry_backoff_sec: float
    image_max_dim: int
    page_size: int

    @property
    def total_concurrency(self) -> int:
        return sum(ep.concurrency for ep in self.endpoints)

    @classmethod
    def from_env(cls) -> "Config":
        api_base_url = os.getenv("OCR_API_BASE_URL", "").strip().rstrip("/")
        image_host_prefix = os.getenv("OCR_IMAGE_HOST_PREFIX", "").strip().rstrip("/")
        api_key = os.getenv("OCR_API_KEY", "").strip()

        if not api_base_url:
            raise ValueError("OCR_API_BASE_URL is required.")
        if not image_host_prefix:
            raise ValueError("OCR_IMAGE_HOST_PREFIX is required.")
        if not api_key:
            raise ValueError("OCR_API_KEY is required.")

        endpoints_path = Path(os.getenv("OCR_ENDPOINTS_FILE", "endpoints.toml")).expanduser()
        endpoints = load_endpoints(endpoints_path)

        local_root_raw = os.getenv("OCR_IMAGE_LOCAL_ROOT", "").strip()
        image_local_root: Path | None = None
        if local_root_raw:
            resolved = Path(local_root_raw).expanduser().resolve()
            if resolved.is_dir():
                image_local_root = resolved
            else:
                logger.warning(
                    "OCR_IMAGE_LOCAL_ROOT=%s does not exist or is not a directory; "
                    "falling back to HTTP-only image loading.",
                    resolved,
                )

        return cls(
            api_base_url=api_base_url,
            image_host_prefix=image_host_prefix,
            image_local_root=image_local_root,
            api_key=api_key,
            ocr_version=os.getenv("OCR_VERSION", "2026-05-31").strip(),
            endpoints=endpoints,
            lm_studio_model=os.getenv("LM_STUDIO_MODEL", "gemma-3-27b-it").strip(),
            lm_studio_prompt=os.getenv("LM_STUDIO_OCR_PROMPT", DEFAULT_PROMPT).strip(),
            lm_studio_api_key=os.getenv("LM_STUDIO_API_KEY", "lm-studio").strip(),
            idle_sleep_sec=float(os.getenv("IDLE_SLEEP_SEC", "60.0")),
            request_timeout_sec=int(os.getenv("REQUEST_TIMEOUT_SEC", "30")),
            lm_studio_timeout_sec=int(os.getenv("LM_STUDIO_TIMEOUT_SEC", "180")),
            lm_studio_max_tokens=max(64, int(os.getenv("LM_STUDIO_MAX_TOKENS", "4096"))),
            request_retry_count=max(1, int(os.getenv("REQUEST_RETRY_COUNT", "3"))),
            request_retry_backoff_sec=float(os.getenv("REQUEST_RETRY_BACKOFF_SEC", "2.0")),
            image_max_dim=max(256, int(os.getenv("IMAGE_MAX_DIM", "1280"))),
            page_size=max(1, min(100, int(os.getenv("FETCH_PAGE_SIZE", "50")))),
        )


def load_endpoints(path: Path) -> list[Endpoint]:
    if not path.is_file():
        raise ValueError(
            f"Endpoints file not found: {path}. "
            "Set OCR_ENDPOINTS_FILE or place endpoints.toml in the working directory."
        )

    with path.open("rb") as f:
        raw = tomllib.load(f)

    rows = raw.get("endpoints", [])
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"{path}: [[endpoints]] must contain at least one entry.")

    endpoints: list[Endpoint] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"{path}: endpoints[{index}] must be a table.")
        url = str(row.get("url", "")).strip()
        if not url:
            raise ValueError(f"{path}: endpoints[{index}].url is required.")
        concurrency_raw = row.get("concurrency", 1)
        try:
            concurrency = int(concurrency_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{path}: endpoints[{index}].concurrency must be a positive integer."
            ) from exc
        if concurrency < 1:
            raise ValueError(
                f"{path}: endpoints[{index}].concurrency must be a positive integer."
            )
        endpoints.append(Endpoint(url=url, concurrency=concurrency))
    return endpoints


class OCRWorker:
    def __init__(self, config: Config, dashboard_state: DashboardState | None = None):
        self.config = config
        self.dashboard_state = dashboard_state
        self._failed_pks_lock = threading.Lock()
        self._failed_pks: set[str] = set()

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

    def fetch_latest_media(self) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "limit": self.config.page_size,
            "current_version": self.config.ocr_version,
        }
        response = self._request_with_retry(
            "GET",
            f"{self.config.api_base_url}/media/latest",
            params=params,
            timeout=self.config.request_timeout_sec,
        )
        payload = response.json()
        return list(payload.get("data", []))

    def post_ocr_result(self, pk: str, result: OCRResult) -> None:
        payload = {
            "text": result.text if result.text.strip() else " ",
            "background": result.background if result.background.strip() else " ",
            "profile_estimate": result.profile_estimate if result.profile_estimate.strip() else " ",
            "is_pr": result.is_pr,
            "is_ugc": result.is_ugc,
            "tags": result.tags,
            "no_text_detected": result.no_text_detected,
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

    def _resolve_local_image_path(self, image_url: str) -> Path | None:
        root = self.config.image_local_root
        if root is None:
            return None

        parsed = urlparse(image_url)
        relative = parsed.path.lstrip("/") if parsed.scheme else image_url.lstrip("/")
        if not relative:
            return None

        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            logger.warning(
                "Local image path escapes OCR_IMAGE_LOCAL_ROOT, falling back to HTTP: url=%s candidate=%s",
                image_url,
                candidate,
            )
            return None
        return candidate

    def _load_image_bytes(self, image_url: str) -> bytes:
        local_path = self._resolve_local_image_path(image_url)
        if local_path is not None:
            if local_path.is_file():
                logger.info("Loaded image from local mount path=%s", local_path)
                return local_path.read_bytes()
            logger.warning(
                "Local image not found, falling back to HTTP: path=%s url=%s",
                local_path,
                image_url,
            )

        response = requests.get(
            image_url,
            headers={"X-API-Key": self.config.api_key},
            timeout=self.config.request_timeout_sec,
        )
        response.raise_for_status()
        return response.content

    def _download_image_as_base64(self, image_url: str) -> str:
        image_bytes = self._load_image_bytes(image_url)

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

    def _parse_lm_studio_response(self, response_text: str) -> OCRResult:
        raw = response_text.strip()

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            start = raw.find("{")
            end = raw.rfind("}")
            if start == -1 or end == -1 or end <= start:
                logger.error(
                    "LM Studio response has no JSON object error=%s\n--- raw response ---\n%s\n--- end ---",
                    exc,
                    raw,
                )
                raise ValueError(f"LM Studio did not return JSON: {truncate_for_log(raw)}")
            candidate = raw[start : end + 1]
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError as inner_exc:
                logger.error(
                    "LM Studio response JSON parse failed error=%s\n--- raw response ---\n%s\n--- extracted candidate ---\n%s\n--- end ---",
                    inner_exc,
                    raw,
                    candidate,
                )
                raise

        if not isinstance(parsed, dict):
            raise ValueError(f"LM Studio returned non-object JSON: {truncate_for_log(raw)}")

        raw_tags = parsed.get("tags", [])
        if isinstance(raw_tags, list):
            tags = [str(t).strip() for t in raw_tags if str(t).strip()]
        else:
            tags = []

        return OCRResult(
            text=str(parsed.get("text", "")).strip(),
            background=str(parsed.get("background", "")).strip(),
            profile_estimate=str(parsed.get("profile_estimate", "")).strip(),
            is_pr=bool(parsed.get("is_pr", False)),
            is_ugc=bool(parsed.get("is_ugc", False)),
            tags=tags,
            no_text_detected=bool(parsed.get("no_text_detected", False)),
        )

    def _call_lm_studio(self, image_url: str, prompt: str, endpoint_url: str) -> tuple[str, float]:
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
            "max_tokens": self.config.lm_studio_max_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": OCR_RESPONSE_SCHEMA,
            },
        }

        response = requests.post(
            endpoint_url,
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

    def _run_lm_studio_ocr_once(self, image_url: str, prompt: str, endpoint_url: str) -> OCRResult:
        raw_text, elapsed_sec = self._call_lm_studio(image_url, prompt, endpoint_url)
        result = self._parse_lm_studio_response(raw_text)
        logger.info(
            "OCR complete endpoint=%s elapsed=%.2fs result=%s",
            endpoint_url,
            elapsed_sec,
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

    def run_test_mode(self, limit: int) -> int:
        items = self.fetch_latest_media()[:limit]
        if not items:
            logger.warning(
                "No items returned from /media/latest. "
                "Bump OCR_VERSION to also re-OCR previously processed posts."
            )
            return 0

        logger.info(
            "Test mode: running OCR on %s item(s). Results will NOT be posted back to the API.",
            len(items),
        )
        logger.info("Prompt in use:\n%s", self.config.lm_studio_prompt)

        endpoint_url = self.config.endpoints[0].url
        for index, item in enumerate(items, start=1):
            pk = str(item["pk"])
            taken_at = int(item["taken_at"])
            image_url = self._build_image_url(str(item["image_url"]))

            logger.info("=" * 80)
            logger.info(
                "[%s/%s] pk=%s taken_at=%s (%s) endpoint=%s image=%s",
                index,
                len(items),
                pk,
                taken_at,
                format_taken_at(taken_at),
                endpoint_url,
                image_url,
            )

            try:
                raw_text, elapsed_sec = self._call_lm_studio(
                    image_url, self.config.lm_studio_prompt, endpoint_url
                )
            except (requests.RequestException, ValueError):
                logger.exception("LM Studio call failed pk=%s", pk)
                continue

            logger.info("--- raw response (elapsed=%.2fs) ---\n%s", elapsed_sec, raw_text)

            try:
                result = self._parse_lm_studio_response(raw_text)
            except ValueError as exc:
                logger.error("Failed to parse LM Studio response pk=%s error=%s", pk, exc)
                continue

            logger.info("--- parsed text ---\n%s", result.text)
            logger.info("--- parsed background ---\n%s", result.background)
            logger.info("--- parsed profile_estimate ---\n%s", result.profile_estimate)
            logger.info("--- parsed tags ---\n%s", json.dumps(result.tags, ensure_ascii=False))
            logger.info(
                "is_pr=%s  is_ugc=%s  no_text_detected=%s",
                result.is_pr,
                result.is_ugc,
                result.no_text_detected,
            )

        logger.info("=" * 80)
        logger.info("Test mode complete.")
        return 0

    def run_lm_studio_ocr(self, image_url: str, endpoint_url: str) -> OCRResult:
        result = self._run_lm_studio_ocr_once(image_url, self.config.lm_studio_prompt, endpoint_url)
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
        return result

    def _process_media_item(self, item: dict[str, Any], endpoint_url: str) -> bool:
        pk = str(item["pk"])

        with self._failed_pks_lock:
            if pk in self._failed_pks:
                logger.debug("Skipping previously failed pk=%s", pk)
                return False

        taken_at = int(item["taken_at"])
        full_image_url = self._build_image_url(str(item["image_url"]))
        if self.dashboard_state is not None:
            self.dashboard_state.set_current_item(pk, taken_at, format_taken_at(taken_at))
        logger.info(
            "Processing pk=%s taken_at=%s (%s) endpoint=%s image=%s",
            pk,
            taken_at,
            format_taken_at(taken_at),
            endpoint_url,
            full_image_url,
        )

        try:
            result = self.run_lm_studio_ocr(full_image_url, endpoint_url)
        except (requests.RequestException, ValueError):
            with self._failed_pks_lock:
                self._failed_pks.add(pk)
            if self.dashboard_state is not None:
                self.dashboard_state.increment_failed()
                self.dashboard_state.clear_current_item()
            logger.exception(
                "OCR failed pk=%s endpoint=%s image=%s",
                pk,
                endpoint_url,
                full_image_url,
            )
            return False

        try:
            self.post_ocr_result(pk, result)
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

    def _process_media_batch(self, items: list[dict[str, Any]]) -> int:
        if not items:
            return 0

        item_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        for item in items:
            item_queue.put(item)

        counter_lock = threading.Lock()
        processed_total = 0

        endpoints = self.config.endpoints
        total_workers = self.config.total_concurrency
        logger.info(
            "Processing batch items=%s endpoints=%s workers=%s breakdown=%s",
            len(items),
            len(endpoints),
            total_workers,
            ", ".join(f"{ep.url}={ep.concurrency}" for ep in endpoints),
        )

        def run_worker(endpoint_url: str) -> None:
            nonlocal processed_total
            while True:
                try:
                    item = item_queue.get_nowait()
                except queue.Empty:
                    return
                try:
                    succeeded = self._process_media_item(item, endpoint_url)
                except Exception:
                    if self.dashboard_state is not None:
                        self.dashboard_state.increment_failed()
                    logger.exception("Unexpected error while processing OCR batch item")
                    succeeded = False
                finally:
                    item_queue.task_done()
                if succeeded:
                    with counter_lock:
                        processed_total += 1

        threads: list[threading.Thread] = []
        for ep in endpoints:
            for slot in range(ep.concurrency):
                thread = threading.Thread(
                    target=run_worker,
                    args=(ep.url,),
                    name=f"ocr-worker[{ep.url}#{slot}]",
                    daemon=True,
                )
                thread.start()
                threads.append(thread)

        for thread in threads:
            thread.join()

        return processed_total

    def process_cycle(self) -> dict[str, int]:
        items = self.fetch_latest_media()
        processed_count = self._process_media_batch(items) if items else 0
        return {
            "pages": 1,
            "processed": processed_count,
            "skipped": 0,
        }

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

    setup_file_logging()

    try:
        config = Config.from_env()
    except ValueError as exc:
        logger.error("%s", exc)
        return 2

    if config.image_local_root is not None:
        logger.info(
            "Image loading mode: local root=%s (HTTP fallback enabled)",
            config.image_local_root,
        )
    else:
        logger.info("Image loading mode: HTTP only (OCR_IMAGE_LOCAL_ROOT not set)")

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
        if _is_console_handler(handler):
            root_logger.removeHandler(handler)
    for handler in list(logger.handlers):
        if _is_console_handler(handler):
            logger.removeHandler(handler)
    logger.propagate = False
    dashboard_handler = DashboardLogHandler(dashboard_state)
    dashboard_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(dashboard_handler)

    worker = OCRWorker(config, dashboard_state=dashboard_state)
    return run_with_dashboard(worker, dashboard_state)


if __name__ == "__main__":
    raise SystemExit(main())
