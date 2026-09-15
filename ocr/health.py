"""LLM エンドポイントの死活監視。"""

from __future__ import annotations

import logging
import signal
import threading
import time

import requests

from .config import Config, Endpoint

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SEC = 30.0
MAX_CONSECUTIVE_FAILURES = 3
CHECK_TIMEOUT_SEC = 10


def find_dead_endpoint(config: Config) -> tuple[Endpoint, requests.RequestException] | None:
    """応答しない最初のエンドポイントと例外を返す。"""
    for endpoint in config.endpoints:
        try:
            requests.get(
                endpoint.url.removesuffix("/chat/completions") + "/models",
                headers={"Authorization": f"Bearer {config.lm_studio_api_key}"},
                timeout=CHECK_TIMEOUT_SEC,
            ).raise_for_status()
        except requests.RequestException as exc:
            return endpoint, exc
    return None


def start_llm_health_monitor(config: Config) -> None:
    """監視スレッドを起動し、連続失敗時にメインスレッドへ SIGINT を送る。"""
    def monitor() -> None:
        failures = 0
        while True:
            time.sleep(CHECK_INTERVAL_SEC)
            dead = find_dead_endpoint(config)
            if dead is None:
                failures = 0
                continue
            endpoint, error = dead
            failures += 1
            logger.warning(
                "LLM health check failed endpoint=%s failures=%s/%s error=%s",
                endpoint.url,
                failures,
                MAX_CONSECUTIVE_FAILURES,
                error,
            )
            if failures >= MAX_CONSECUTIVE_FAILURES:
                logger.error("LLM endpoint unavailable endpoint=%s error=%s", endpoint.url, error)
                signal.pthread_kill(threading.main_thread().ident, signal.SIGINT)
                return

    threading.Thread(target=monitor, name="llm-health-monitor", daemon=True).start()
