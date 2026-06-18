"""OCR ワーカーのパッケージ。

エントリーポイントは ocr_worker.py (リポジトリ直下) から `from ocr.cli import main`
で呼ばれる。systemd / cron から起動するときも `python ocr_worker.py` のままで動く。
"""
