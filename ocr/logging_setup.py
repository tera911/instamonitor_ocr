"""ログハンドラ周りの初期化 (ファイル出力 + ダッシュボード転送)。"""

from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

from .dashboard import DashboardState


logger = logging.getLogger("ocr")


class DashboardLogHandler(logging.Handler):
    """ロガーの出力を DashboardState のリングバッファに横流しする handler。"""

    def __init__(self, state: DashboardState):
        super().__init__()
        self.state = state

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:
            message = record.getMessage()
        self.state.add_log(message, record.levelno)


def setup_file_logging() -> Path | None:
    """OCR_LOG_FILE が設定されていれば RotatingFileHandler を ocr ロガーに追加する。"""
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

    # `ocr` パッケージ全体のロガーに付ける。dashboard モードで propagate=False にしても
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
