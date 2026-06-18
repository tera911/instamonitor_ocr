"""--test の結果を HTML レポートに書き出す。

画像と OCR 出力を 1 列に並べ、目視で比較できるようにする。pk 1 件あたり
1 ブロック (画像 + 各フィールド) で、デバッグ時に「この画像でこう間違えた」
を一発で指摘できる粒度。
"""

from __future__ import annotations

import html
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .llm_client import OCRResult


logger = logging.getLogger(__name__)


@dataclass
class TestItemReport:
    """1 件分のテスト結果。test_runner が collect して report に渡す。"""

    pk: str
    taken_at: int
    taken_at_text: str
    image_url: str
    result: OCRResult | None
    elapsed_sec: float
    error: str | None = None


_CSS = """
* { box-sizing: border-box; }
body {
    margin: 0;
    padding: 24px;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Hiragino Kaku Gothic ProN", sans-serif;
    background: #fafafa;
    color: #222;
    line-height: 1.55;
}
header {
    max-width: 1200px;
    margin: 0 auto 24px;
    padding-bottom: 12px;
    border-bottom: 1px solid #ddd;
}
header h1 { margin: 0 0 8px; font-size: 20px; }
header .meta { font-size: 13px; color: #555; display: flex; flex-wrap: wrap; gap: 12px 24px; }
header .meta span code { background: #eee; padding: 2px 6px; border-radius: 4px; font-size: 12px; }
main { max-width: 1200px; margin: 0 auto; display: grid; gap: 24px; }
.card {
    background: #fff;
    border: 1px solid #e2e2e2;
    border-radius: 8px;
    padding: 16px;
    display: grid;
    grid-template-columns: 280px 1fr;
    gap: 20px;
}
.card.error { border-color: #e57373; background: #fff5f5; }
.card .image { display: flex; align-items: flex-start; justify-content: center; }
.card .image img {
    max-width: 100%;
    max-height: 360px;
    border-radius: 6px;
    border: 1px solid #ddd;
    background: #f0f0f0;
}
.card .body { display: grid; gap: 12px; min-width: 0; }
.card h2 {
    margin: 0;
    font-size: 15px;
    color: #444;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px 12px;
}
.card h2 small { font-weight: normal; color: #888; font-size: 12px; }
.field { display: grid; gap: 4px; }
.field .label { font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; color: #888; }
.field .value {
    background: #f6f6f6;
    border: 1px solid #ececec;
    border-radius: 6px;
    padding: 8px 10px;
    font-size: 13px;
    white-space: pre-wrap;
    word-break: break-word;
    max-height: 320px;
    overflow: auto;
    font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
}
.field .value.tags { font-family: inherit; }
.tag {
    display: inline-block;
    background: #eef4ff;
    border: 1px solid #c9d8f5;
    color: #2b4d8c;
    padding: 2px 8px;
    margin: 2px 4px 2px 0;
    border-radius: 999px;
    font-size: 12px;
}
.flags { display: flex; flex-wrap: wrap; gap: 8px; font-size: 12px; }
.flag {
    padding: 2px 8px;
    border-radius: 999px;
    border: 1px solid #ccc;
    background: #f0f0f0;
    color: #555;
}
.flag.on { background: #e9f7ed; border-color: #b7dfc1; color: #1f6b3a; }
.flag.warn { background: #fff7e0; border-color: #ebd596; color: #875b00; }
.error-msg {
    background: #fdecea;
    border: 1px solid #f1b0b0;
    color: #8b1a1a;
    padding: 10px 12px;
    border-radius: 6px;
    font-size: 12px;
    font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
    white-space: pre-wrap;
    word-break: break-word;
    line-height: 1.5;
    max-height: 480px;
    overflow: auto;
}
@media (max-width: 720px) {
    .card { grid-template-columns: 1fr; }
}
"""


def _flag(name: str, value: bool, *, true_class: str = "on") -> str:
    cls = true_class if value else ""
    return f'<span class="flag {cls}">{html.escape(name)}: {str(value).lower()}</span>'


def _render_tags(tags: list[str]) -> str:
    if not tags:
        return '<div class="value tags"><em>(empty)</em></div>'
    spans = "".join(f'<span class="tag">{html.escape(t)}</span>' for t in tags)
    return f'<div class="value tags">{spans}</div>'


def _render_card(item: TestItemReport, index: int) -> str:
    img_attr = html.escape(item.image_url, quote=True)
    header_meta = (
        f'<small>taken_at={item.taken_at} ({html.escape(item.taken_at_text)})</small>'
        f'<small>elapsed={item.elapsed_sec:.2f}s</small>'
    )
    title = f'<h2>#{index} pk={html.escape(item.pk)} {header_meta}</h2>'

    if item.result is None:
        body = (
            f'{title}'
            f'<div class="error-msg">{html.escape(item.error or "unknown error")}</div>'
        )
        return f'<section class="card error"><div class="image"><img src="{img_attr}" alt="pk {html.escape(item.pk)}"></div><div class="body">{body}</div></section>'

    r = item.result
    flags_html = (
        f'<div class="flags">'
        f'{_flag("is_pr", r.is_pr, true_class="warn")}'
        f'{_flag("is_ugc", r.is_ugc)}'
        f'{_flag("no_text_detected", r.no_text_detected, true_class="warn")}'
        f'</div>'
    )

    def field(label: str, value: str) -> str:
        if not value:
            value_html = '<em>(empty)</em>'
        else:
            value_html = html.escape(value)
        return (
            f'<div class="field"><span class="label">{html.escape(label)}</span>'
            f'<div class="value">{value_html}</div></div>'
        )

    body = (
        f'{title}'
        f'{flags_html}'
        f'{field("text", r.text)}'
        f'{field("background", r.background)}'
        f'{field("profile_estimate", r.profile_estimate)}'
        f'<div class="field"><span class="label">tags</span>{_render_tags(r.tags)}</div>'
    )
    return f'<section class="card"><div class="image"><img src="{img_attr}" alt="pk {html.escape(item.pk)}"></div><div class="body">{body}</div></section>'


def _meta_html(meta: dict[str, Any]) -> str:
    pieces: list[str] = []
    for key, value in meta.items():
        if isinstance(value, (list, dict)):
            value_str = json.dumps(value, ensure_ascii=False)
        else:
            value_str = str(value)
        pieces.append(f'<span>{html.escape(key)}: <code>{html.escape(value_str)}</code></span>')
    return "".join(pieces)


def write_report(
    items: list[TestItemReport],
    *,
    report_root: Path,
    meta: dict[str, Any],
    timestamp: datetime | None = None,
) -> Path:
    """test_<timestamp>/index.html を書き出してそのパスを返す。"""
    timestamp = timestamp or datetime.now()
    stamp = timestamp.strftime("%Y%m%d_%H%M%S")
    out_dir = report_root / f"test_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    index_path = out_dir / "index.html"

    cards_html = "".join(_render_card(item, idx) for idx, item in enumerate(items, start=1))
    meta_with_timestamp = {"generated_at": timestamp.isoformat(timespec="seconds"), **meta}

    html_doc = f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>OCR test report {stamp}</title>
<style>{_CSS}</style>
</head>
<body>
<header>
<h1>OCR test report — {html.escape(stamp)}</h1>
<div class="meta">{_meta_html(meta_with_timestamp)}</div>
</header>
<main>
{cards_html or '<p>(no items processed)</p>'}
</main>
</body>
</html>"""

    index_path.write_text(html_doc, encoding="utf-8")
    logger.info("Wrote HTML report path=%s items=%s", index_path, len(items))
    return index_path
