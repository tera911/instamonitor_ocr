"""VLM 出力に対する後処理。`<bs>` placeholder と `<br>` タグの救済が主。"""

from __future__ import annotations

import re
from typing import Any


# Gemma 4 12B 等が改行を `<br>` で表現する癖がプロンプト指示でも残るので、
# 受信側で問答無用に `\n` へ正規化する。`<br>` `<br/>` `<br />` を全部拾う。
_BR_TAG_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)

# 3 連以上の改行は OCR テキストとして意味を持たない (画像上の段落区切りでも 2 行で十分)。
# 12B 系が `\n` を吐き続ける暴走の名残を最終出力から消す。
_EXCESS_NEWLINE_RE = re.compile(r"\n{3,}")


def restore_placeholders(text: str) -> str:
    """VLM に書かせた `<bs>` をバックスラッシュに戻し、`<br>` 群を `\\n` に正規化する。

    llama.cpp の json_schema grammar は `"` や 制御文字 は弾くが、文字列値内の
    生 `\\` だけは漏れて不正 JSON を生む (Gemma 4 e4b で再現)。回避策としてプロンプト
    側で `\\` を出力させず `<bs>` プレースホルダで書かせ、Python 側で戻す。
    `<br>` 系はプロンプトで禁じても 12B 系が出してくるので、ここで一律改行に倒す。
    parse 段でリカバリーをすり抜けた連続改行も、見た目用に 2 つに丸める。
    """
    if not text:
        return text
    text = text.replace("<bs>", "\\")
    text = _BR_TAG_RE.sub("\n", text)
    text = _EXCESS_NEWLINE_RE.sub("\n\n", text)
    return text


def apply_placeholders(obj: Any) -> Any:
    """parse 済み JSON ツリー内のすべての文字列に restore_placeholders を再帰適用する。"""
    if isinstance(obj, str):
        return restore_placeholders(obj)
    if isinstance(obj, list):
        return [apply_placeholders(v) for v in obj]
    if isinstance(obj, dict):
        return {k: apply_placeholders(v) for k, v in obj.items()}
    return obj
