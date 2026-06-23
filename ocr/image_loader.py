"""画像 URL の解決・ローカルマウントからの直読み・リサイズ・base64 化。"""

from __future__ import annotations

import base64
import logging
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

from .config import Config


try:
    from PIL import Image, UnidentifiedImageError
except ImportError:  # Pillow 未インストールでもリサイズだけスキップして動かす
    Image = None  # type: ignore[assignment]
    UnidentifiedImageError = None  # type: ignore[assignment,misc]


logger = logging.getLogger(__name__)


def build_image_url(image_url: str, config: Config) -> str:
    if image_url.startswith("http://") or image_url.startswith("https://"):
        return image_url
    return urljoin(f"{config.image_host_prefix}/", image_url.lstrip("/"))


def resolve_local_image_path(image_url: str, config: Config) -> Path | None:
    """OCR_IMAGE_LOCAL_ROOT 配下に画像があれば、そのローカルパスを返す。"""
    root = config.image_local_root
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


def load_image_bytes(image_url: str, config: Config) -> bytes:
    """ローカルマウントを優先しつつ画像 bytes を取得する。"""
    local_path = resolve_local_image_path(image_url, config)
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
        headers={"X-API-Key": config.api_key},
        timeout=config.request_timeout_sec,
    )
    response.raise_for_status()
    return response.content


def download_image_as_base64(image_url: str, config: Config) -> str:
    """画像を取得し、必要なら長辺 IMAGE_MAX_DIM にリサイズして base64 化する。"""
    image_bytes = load_image_bytes(image_url, config)

    if not image_bytes:
        # 0 byte。LM Studio 側で "cannot identify image file" 400 になり再試行不能。
        raise OSError(f"Image bytes empty url={image_url}")

    if Image is not None:
        try:
            with Image.open(BytesIO(image_bytes)) as img:
                width, height = img.size
                max_dim = max(width, height)
                if max_dim > config.image_max_dim:
                    scale = config.image_max_dim / max_dim
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
                        config.image_max_dim,
                    )
        except UnidentifiedImageError as exc:
            # PIL すら識別できない bytes は LM Studio も同じく弾く。再試行で
            # 直らないので OSError として上層に投げ run_pipeline 側で empty 扱いに倒す。
            raise OSError(f"Image data unrecognizable url={image_url}: {exc}") from exc
        except Exception as exc:
            logger.warning("Image resize failed, using original image error=%s", exc)

    return base64.b64encode(image_bytes).decode("ascii")
