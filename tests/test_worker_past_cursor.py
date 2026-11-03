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


class FakeServer:
    """過去分だけを持つ /media/latest。新しい順に、カーソルより前の未処理を返す。"""

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
        cursor = f"{page[-1]}:{page[-1]}" if page else None
        return items, cursor


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


if __name__ == "__main__":
    unittest.main()
