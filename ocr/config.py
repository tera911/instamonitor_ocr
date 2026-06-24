"""ランタイム設定 (Config / Endpoint) と起動時の env / endpoints.toml ロード。"""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .tasks import DEFAULT_PROMPT, TASK_PIPELINES


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Endpoint:
    """1 つの LM Studio / vLLM エンドポイントの設定。

    mode は TASK_PIPELINES のキーに対応し、このエンドポイント宛のリクエストを
    分割するか one-shot で 1 リクエストにまとめるかを切り替える。
    Q8 等で精度に余裕がある endpoint は "oneshot"、Q4 など分割した方が安定する
    endpoint は "split" を指定する。
    """

    url: str
    concurrency: int
    mode: str = "oneshot"


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
            image_max_dim=max(256, int(os.getenv("IMAGE_MAX_DIM", "1920"))),
            page_size=max(1, min(200, int(os.getenv("FETCH_PAGE_SIZE", "50")))),
        )


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
        mode = str(row.get("mode", "oneshot")).strip()
        if mode not in TASK_PIPELINES:
            raise ValueError(
                f"{path}: endpoints[{index}].mode={mode!r} is unknown. "
                f"Expected one of {sorted(TASK_PIPELINES.keys())}."
            )
        endpoints.append(Endpoint(url=url, concurrency=concurrency, mode=mode))
    return endpoints
