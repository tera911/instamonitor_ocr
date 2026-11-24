"""過去分をカーソルで進める処理 (OCRWorker.process_cycle) の検証。

/media/latest は、処理済みの行を結果から消すサーバの振る舞いを再現したものに差し替える。

    .venv/bin/python -m unittest discover -s tests
"""

from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from ocr import worker as worker_module
from ocr.config import Endpoint
from ocr.worker import OCRWorker


BELOW_LOWER_BOUND = "0:0"


class FakeServer:
    """過去分だけを持つ /media/latest。新しい順に、カーソルより前の未処理を返す。

    過去分を読み切った (枠より少なく返した) ときは、空の応答でも下限 (pk=1) より前のカーソルを返す。
    """

    def __init__(self, pks: list[int], page_size: int):
        # taken_at は pk と同じ値にして、新しい順 = pk の大きい順にする
        self.rows = sorted(pks, reverse=True)
        self.page_size = page_size
        self.processed: set[str] = set()
        self.calls: list[str | None] = []
        self.lock = threading.Lock()

    def fetch(self, config, past_before=None):
        with self.lock:
            self.calls.append(past_before)
            before = int(past_before.split(":")[1]) if past_before else None
            page = [
                pk for pk in self.rows
                if str(pk) not in self.processed and (before is None or pk < before)
            ][: self.page_size]
        items = [{"pk": str(pk), "taken_at": pk, "image_url": f"/{pk}.jpg"} for pk in page]
        cursor = f"{page[-1]}:{page[-1]}" if len(page) == self.page_size else BELOW_LOWER_BOUND
        return items, cursor

    def add(self, pk: int) -> None:
        with self.lock:
            self.rows = sorted(self.rows + [pk], reverse=True)


def make_worker(server: FakeServer, failing: set[str], reset_sec: float) -> OCRWorker:
    config = SimpleNamespace(
        endpoints=[Endpoint(url="http://lm", concurrency=1, mode="oneshot")],
        total_concurrency=1,
        page_size=server.page_size,
        past_cursor_reset_sec=reset_sec,
    )
    worker = OCRWorker(config)  # type: ignore[arg-type]

    def process(item, endpoint):
        pk = str(item["pk"])
        if pk in failing:
            with worker._failed_pks_lock:
                worker._failed_pks.add(pk)
            return False
        with server.lock:
            server.processed.add(pk)
        return True

    worker._process_media_item = process  # type: ignore[method-assign]
    return worker


class PastCursorTest(unittest.TestCase):
    # 処理済みが結果から消えても、失敗して残った行があっても、未処理を飛ばさずに全件処理する
    def test_processes_every_row_without_skipping(self):
        server = FakeServer(list(range(1, 12)), page_size=2)
        worker = make_worker(server, failing={"9"}, reset_sec=600)

        with mock.patch.object(worker_module, "fetch_latest_media", server.fetch):
            stats = worker.process_cycle()

        expected = {str(pk) for pk in range(1, 12)} - {"9"}
        self.assertEqual(server.processed, expected)
        self.assertEqual(stats["processed"], 10)
        # 2 回目以降はサーバが返したカーソルを渡している
        self.assertIsNone(server.calls[0])
        self.assertTrue(any(call is not None for call in server.calls[1:]))

    # リセット間隔を過ぎるとカーソルを捨て、いちばん新しい過去分から取り直す
    def test_cursor_is_reset_after_interval(self):
        server = FakeServer(list(range(1, 6)), page_size=2)
        worker = make_worker(server, failing=set(), reset_sec=0)

        with mock.patch.object(worker_module, "fetch_latest_media", server.fetch):
            worker.process_cycle()

        self.assertEqual(server.processed, {str(pk) for pk in range(1, 6)})
        self.assertTrue(all(call is None for call in server.calls), server.calls)


class CarriedCursorTest(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer([], page_size=2)
        self.worker = make_worker(self.server, failing=set(), reset_sec=600)
        patcher = mock.patch.object(worker_module, "fetch_latest_media", self.server.fetch)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cycle(self) -> list[str | None]:
        """1 サイクル回し、そのサイクルで送った past_before を返す。"""
        self.server.calls.clear()
        self.worker.process_cycle()
        return list(self.server.calls)

    # 過去分を読み切った後の次のサイクルは、先頭からではなく下限より前のカーソルから読む
    def test_cursor_is_carried_over_to_next_cycle(self):
        for pk in range(1, 4):
            self.server.add(pk)

        self.assertEqual(self.run_cycle(), [None, "2:2", BELOW_LOWER_BOUND])
        self.assertEqual(self.run_cycle(), [BELOW_LOWER_BOUND])

    # 何も返らない応答でも、下限より前のカーソルを受け取って次のサイクルに持ち越す
    def test_cursor_is_kept_from_empty_response(self):
        self.assertEqual(self.run_cycle(), [None])
        self.assertEqual(self.run_cycle(), [BELOW_LOWER_BOUND])

    # カーソルより新しい側に増えた未処理 (日付の切り替わりで過去分に移った前日分、
    # 新しい OCR の版でのやり直し) は、リセット間隔を過ぎてカーソルを戻した後に処理する
    def test_rows_above_cursor_are_processed_after_reset(self):
        self.run_cycle()
        self.server.add(9)

        self.run_cycle()
        self.assertEqual(self.server.processed, set())

        self.worker._past_cursor_reset_at -= self.worker.config.past_cursor_reset_sec
        self.assertEqual(self.run_cycle()[0], None)
        self.assertEqual(self.server.processed, {"9"})

    # 再起動 (OCR の版を変えたときを含む) の後は、いちばん新しい過去分から読む
    def test_restarted_worker_starts_from_newest_past(self):
        self.run_cycle()
        self.server.add(4)

        self.worker = make_worker(self.server, failing=set(), reset_sec=600)
        self.assertEqual(self.run_cycle()[0], None)
        self.assertEqual(self.server.processed, {"4"})


if __name__ == "__main__":
    unittest.main()
