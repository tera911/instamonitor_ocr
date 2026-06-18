"""CLI 引数解析と main エントリーポイント。"""

from __future__ import annotations

import argparse
import curses
import logging
import os
import sys
import threading
from pathlib import Path

from .config import Config, load_dotenv_file
from .dashboard import DashboardState, draw_dashboard
from .logging_setup import DashboardLogHandler, setup_file_logging
from .test_runner import run_test_mode
from .worker import OCRWorker


logger = logging.getLogger("ocr")


def _configure_root_logging() -> None:
    """初回起動時に root logger を 1 度だけ stdout に向ける。

    モジュール import 時の副作用を避けたいので、main() から呼ぶ。
    """
    if logging.getLogger().handlers:
        return
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def _is_console_handler(handler: logging.Handler) -> bool:
    """stdout / stderr に直結している StreamHandler を判定する。

    FileHandler は StreamHandler を継承しているため、単純な isinstance だと
    file handler まで除外されてしまう。stream 属性で sys.stdout / stderr を
    狙い撃ちすることでファイル系ハンドラを温存する。
    """
    if not isinstance(handler, logging.StreamHandler) or isinstance(handler, logging.FileHandler):
        return False
    return getattr(handler, "stream", None) in (sys.stdout, sys.stderr)


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
        help="Number of items to test in --test mode. Default: 3. Ignored when --pk is set.",
    )
    parser.add_argument(
        "--pk",
        type=str,
        default=None,
        help=(
            "Comma-separated pk(s) to focus on in --test mode (例: --pk 12345 / --pk 12345,67890). "
            "Only items from /media/latest whose pk matches are processed; --limit is ignored. "
            "pk が /media/latest の現ページに居ない (= 既に OCR_VERSION で処理済み等) ときは "
            "warning を出してスキップするので、再実行したいなら OCR_VERSION を bump してから試す。"
        ),
    )
    parser.add_argument(
        "--skip-no-text-detect",
        action="store_true",
        help=(
            "Test mode: skip items whose OCR returns no_text_detected=true and keep pulling "
            "from /media/latest until --limit usable items are collected (or the page runs out). "
            "テキスト無し画像を結果から外して、ちゃんと検証になるサンプルだけで report を作りたいとき用。"
        ),
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help=(
            "Test mode: HTML report (画像とOCR出力を並べた比較ビュー) を生成する。"
            "デフォルトは生成しない。出力先は --report-dir (デフォルト ./reports/test_<timestamp>/index.html)。"
        ),
    )
    parser.add_argument(
        "--report-dir",
        type=str,
        default="./reports",
        help=(
            "Test mode: directory under which test_<timestamp>/index.html is written. "
            "--report と組み合わせて使う。デフォルト ./reports。"
        ),
    )
    return parser.parse_args()


def _run_with_dashboard(worker: OCRWorker, state: DashboardState) -> int:
    thread = threading.Thread(target=worker.run_forever, daemon=True)
    thread.start()
    curses.wrapper(draw_dashboard, state)
    return 0


def main() -> int:
    args = parse_args()
    load_dotenv_file(Path(".env"))

    _configure_root_logging()
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
        target_pks: list[str] | None = None
        if args.pk:
            target_pks = [token.strip() for token in args.pk.split(",") if token.strip()]
            if not target_pks:
                logger.error("--pk was given but no valid pk parsed from value=%r", args.pk)
                return 2
        return run_test_mode(
            config,
            args.limit,
            target_pks=target_pks,
            skip_no_text_detect=args.skip_no_text_detect,
            report_dir=Path(args.report_dir) if args.report else None,
        )

    if args.no_dashboard:
        worker = OCRWorker(config)
        worker.run_forever()
        return 0

    dashboard_state = DashboardState()
    root_logger = logging.getLogger()
    for handler in list(root_logger.handlers):
        if _is_console_handler(handler):
            root_logger.removeHandler(handler)
    # `ocr` ロガーから stdout 直結 handler を削除して propagate=False に切り替えると
    # ダッシュボード描画中も画面が綺麗に保たれる。ファイル handler は logging_setup
    # 側で attach されており、ここでは触らないので継続して書き続ける。
    for handler in list(logger.handlers):
        if _is_console_handler(handler):
            logger.removeHandler(handler)
    logger.propagate = False
    dashboard_handler = DashboardLogHandler(dashboard_state)
    dashboard_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(dashboard_handler)

    worker = OCRWorker(config, dashboard_state=dashboard_state)
    return _run_with_dashboard(worker, dashboard_state)
