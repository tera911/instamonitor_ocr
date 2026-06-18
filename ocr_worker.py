"""旧来の起動スクリプト互換 entrypoint。実装は ocr/ パッケージにある。

systemd や cron 等の運用スクリプトが ``python ocr_worker.py`` を直接叩いている
ので、リポジトリ直下にこの 1 行ものを残しつつ、中身は ``ocr.cli.main`` に
委譲する。
"""

from __future__ import annotations

from ocr.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
