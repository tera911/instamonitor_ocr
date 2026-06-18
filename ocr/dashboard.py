"""curses ベースの常駐ダッシュボード (描画 + 状態 + ログ折り返し)。"""

from __future__ import annotations

import curses
import logging
import threading
import time
from collections import deque
from datetime import datetime
from typing import Any


class DashboardState:
    """worker -> 描画ループへスレッドセーフに状態を渡すバッファ。"""

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
    ) -> tuple[
        list[tuple[str, int]],
        list[tuple[str, dict[str, int]]],
        dict[str, Any],
        dict[str, Any],
        dict[str, int],
    ]:
        with self._lock:
            logs = list(self.log_lines)
            bars = sorted(self.processed_per_minute.items())[-20:]
            stats = dict(self.last_cycle_stats)
            current_item = dict(self.current_item)
            totals = dict(self.totals)
        return logs, bars, stats, current_item, totals


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
