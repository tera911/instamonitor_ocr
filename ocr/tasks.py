"""OCR ワーカーが LLM に投げる「ひとかたまりの仕事 (= OcrTask)」と、その組み合わせ。

Q4 量子化モデルで OCR と分類タスクを 1 プロンプトに混ぜると attention が崩れて両方とも
劣化する事象が出たため、タスクを宣言的に分割できるよう OcrTask を導入。endpoint.mode で
「分割 (split) するか one-shot で全部やらせるか」を切り替える (Q8 系は one-shot のままで
問題ないため)。

新しいタスクを足したいときは OcrTask を 1 つ書いて TASK_PIPELINES["split"] (もしくは
独自 mode) のタプルに追加するだけで済む。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# プロンプト末尾に必ず付けて出力フォーマットを統一する。`<bs>` placeholder ルール込み。
_JSON_OUTPUT_RULES = """Output format rules (MANDATORY):
- Return ONLY the raw JSON object. Output MUST start with `{` and end with `}`.
- Do NOT wrap the JSON in markdown code fences. No triple backticks, no `json` language tag.
- Do NOT add any prose, explanation, label, or commentary before or after the JSON.
- NEVER write the backslash character `\\` anywhere in any JSON string value. Whenever you would write `\\` — whether it is a decorative slash in the image like `\\テキスト/`, a stray `\\` in the middle of a sentence, or anywhere else — write the literal token `<bs>` instead. Example: `\\待ってました/` MUST be `<bs>待ってました/`; `あ\\り` MUST be `あ<bs>り`.
- For everything else, use standard JSON escapes: `\\n` for line breaks, `\\"` for a literal double quote."""


# あらゆる文字列値に共通で守らせる「テキスト品質」のルール。Gemma 4 12B 系で
# `<br>` `<hr>` `<p>` 等の HTML/XML タグが text フィールドに混入する事象が観測
# されているので明示的に禁じる。改行は本物の `\n` で表現させる。
_TEXT_FIDELITY_RULES = """Text fidelity rules (MANDATORY):
- Output plain Unicode characters only. NEVER insert HTML or XML tags such as <br>, <br/>, <p>, <span>, <hr>, <div>, <b>, <i>, or any other markup unless that exact tag is actually drawn as visible text inside the image.
- For a visual line break, use a real newline (the JSON escape `\\n`) inside the string value. Do NOT write `<br>` or `<br/>` to represent a line break.
- NEVER emit more than two consecutive newlines. `\\n\\n` is the maximum; never produce `\\n\\n\\n` or longer runs. Long runs of newlines are a sign the model is stuck in a loop and waste output tokens.
- Do not invent placeholder tokens. If a character is unreadable, omit it; do NOT write `<unk>`, `???`, `[redacted]`, `[unreadable]`, or similar fillers."""


_DEFAULT_PROMPT_HEAD = """You are performing OCR on an Instagram image.
Extract all visible text as faithfully as possible.
Do not translate, rewrite, summarize, or normalize the text.
Return the original text exactly as it appears.
Keep line breaks when they are visually meaningful.

Set is_pr=true ONLY when the image visually contains the literal characters "PR" (case-insensitive) as readable text. Otherwise set is_pr=false.

Set is_ugc=true when the image strongly suggests promotional/commerce content, especially when:
- the image suggests a product or purchase link, including a clipboard/link-copy icon, "link in bio", URL-like notation, shop guidance, or similar cues
- the image prominently presents prices, discounts, sale amounts, campaign offers, coupon-style amounts, or other purchase-oriented money information
Otherwise set is_ugc=false.

Also generate tags as a list of short Japanese strings that describe or categorize this post. There are no strict rules — pick whatever tags you think fit (topic, mood, product category, audience, vibe, etc.). Typically 3-10 tags. Each tag is a short noun phrase, no leading "#".

Also describe the visual background briefly in background.
- Focus on non-text visual context such as people, objects, layout, scenery, colors, product shots, screenshots, UI elements, and overall composition.
- Keep background concise, usually 1-3 short sentences.
- Write background in Japanese.

Also provide profile_estimate in Japanese as a free-form estimate of the poster/account style or likely profile.
- Keep it short, usually 1-3 short sentences.
- Treat it as a tentative estimate, not a fact.
- If there is not enough information, say so.
- Avoid highly sensitive traits or overconfident claims.

If the image truly contains no readable text, set no_text_detected=true.
Set no_text_detected=false whenever there is any readable text, even if partial."""


_DEFAULT_PROMPT_TAIL = """JSON shape:
{"text":"<extracted text>","background":"<brief visual background>","profile_estimate":"<tentative profile estimate>","is_pr":true,"is_ugc":true,"tags":["tag1","tag2"],"no_text_detected":false}"""


DEFAULT_PROMPT = "\n\n".join(
    [_DEFAULT_PROMPT_HEAD, _TEXT_FIDELITY_RULES, _JSON_OUTPUT_RULES, _DEFAULT_PROMPT_TAIL]
)


# LM Studio Structured Outputs (0.3.0+) で出力 JSON を文法的に強制する。
# constrained sampling により \ の未エスケープ等の不正 JSON が物理的に出なくなる。
# maxLength / maxItems は GBNF grammar に量化子として落ちる。
# 注意: llama.cpp の grammar parser は大きな量化子 (1024 超?) を "exceeds sane defaults"
# として弾き、schema 全体が silently 無効化される。maxLength は控えめに保つ。
OCR_RESPONSE_SCHEMA: dict[str, Any] = {
    "name": "ocr_result",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "maxLength": 4000},
            "background": {"type": "string", "maxLength": 1000},
            "profile_estimate": {"type": "string", "maxLength": 1000},
            "is_pr": {"type": "boolean"},
            "is_ugc": {"type": "boolean"},
            "tags": {
                "type": "array",
                "maxItems": 20,
                "items": {"type": "string", "maxLength": 64},
            },
            "no_text_detected": {"type": "boolean"},
        },
        "required": [
            "text",
            "background",
            "profile_estimate",
            "is_pr",
            "is_ugc",
            "tags",
            "no_text_detected",
        ],
        "additionalProperties": False,
    },
}


@dataclass(frozen=True)
class OcrTask:
    """OCR ワーカが 1 リクエストで LLM に投げる「ひとかたまりの仕事」の定義。

    skip_if_no_text: True のとき、これより前のタスクで no_text_detected=True が立ったら
        このタスクは丸ごとスキップする。OCR で「テキストなし」と判定された画像に対して、
        is_pr / tags 等の重い判定を回さないための短絡。
    needs_ocr_text: True のとき、prompt 内の `{ocr_text}` を accumulator に積まれた
        text フィールドで置換してから LLM に送る。Q4 でも分類が安定するように画像 +
        既知の OCR テキストの 2 系統で判定させる目的。
    disable_thinking: True のとき、リクエスト payload に
        `chat_template_kwargs.enable_thinking=False` を埋めて vLLM/Gemma 系の
        reasoning モードを切る。OCR は思考よりも視覚 fidelity が大事なので、
        TASK_OCR では True にしている。サーバーがこのフィールドを知らない場合は
        サイレントに無視される (LM Studio / 旧 vLLM)。
    derive_no_text_detected: True のとき、task 完了後に accumulator["text"] を見て
        `<empty>` / 空白だけ を「テキスト無し」と解釈し、text を "" にリセットして
        no_text_detected=True を accumulator に詰める。OCR タスクの schema を text 1 つ
        に絞り、判断責務を Python 側に持つための仕組み。
    max_soft_tokens: Gemma 4 系の vision token 数をリクエストレベルで上書きする。
        None なら指定なし (= サーバ起動時設定 or モデルデフォルト)。
        サポート値: 70 / 140 / 280 / 560 / 1120。OCR は 1120 (高解像度) で、
        背景・分類タスクは 140 (低解像度) を割り当てるなど用途別の精度/速度
        トレードオフを宣言する。注意: vLLM 0.23 ではリクエストレベルの
        mm_processor_kwargs が silently 無視される事例が観測されているため、
        確実に効かせたい解像度はサーバー起動引数
        `--mm-processor-kwargs '{"max_soft_tokens": <n>}'` で張った上で、
        下げたいタスクだけ payload 上書きするのが安全。
    fields: このタスクが結果として埋めるキー (ロギング / バリデーション用)。
    """

    name: str
    prompt: str
    schema: dict[str, Any]
    fields: tuple[str, ...]
    skip_if_no_text: bool = False
    needs_ocr_text: bool = False
    disable_thinking: bool = False
    derive_no_text_detected: bool = False
    max_soft_tokens: int | None = None


OCR_TEXT_SCHEMA: dict[str, Any] = {
    "name": "ocr_text",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "maxLength": 4000},
        },
        "required": ["text"],
        "additionalProperties": False,
    },
}


# OCR タスクは「text を出すか、`<empty>` を返すか」の 2 択にして判断項目をゼロに絞る。
# Q4 12B モデルが no_text_detected の真偽判定で本文に JSON を混ぜる事故が出ていたので、
# schema を 1 フィールドに減らし、テキスト無し判定は run_pipeline 側で行う。
OCR_TEXT_PROMPT = f"""You are an OCR engine for Japanese/English Instagram images.
Extract every readable character drawn in the image and put it into `text`.
- Preserve line breaks with `\\n` (max two consecutive).
- Do not translate, rewrite, summarize, or normalize the text.
- If the image contains NO readable text at all, write the literal token `<empty>` (and nothing else) as the value of `text`.
- Do NOT add any other field. Do NOT write `no_text_detected`. The schema only allows `text`.

{_TEXT_FIDELITY_RULES}

{_JSON_OUTPUT_RULES}

JSON shape:
{{"text":"<extracted text or <empty>>"}}"""


CONTEXT_SCHEMA: dict[str, Any] = {
    "name": "ocr_context",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "background": {"type": "string", "maxLength": 1000},
            "profile_estimate": {"type": "string", "maxLength": 1000},
        },
        "required": ["background", "profile_estimate"],
        "additionalProperties": False,
    },
}


CONTEXT_PROMPT = f"""You are describing the visual context of an Instagram image.
The OCR engine already extracted the visible text below (may be `(no text)` when the image had no readable text at all):
\"\"\"
{{ocr_text}}
\"\"\"

Look at the IMAGE itself (this is the primary source — the OCR text is just a hint and can be empty) and fill in:

- background: brief Japanese description of the non-text visual context.
  Focus on people, objects, layout, scenery, colors, product shots, screenshots,
  UI elements, and overall composition. Usually 1-3 short sentences. Do not just
  repeat the OCR text. Even when OCR returned `(no text)`, you MUST describe what
  the image visually shows.

- profile_estimate: Japanese free-form estimate of the poster/account style or
  likely profile. Usually 1-3 short sentences. Tentative, not a fact. If there is
  not enough information, say so. Avoid highly sensitive traits or overconfident
  claims. When OCR is `(no text)`, base your estimate purely on the visual.

{_TEXT_FIDELITY_RULES}

{_JSON_OUTPUT_RULES}

JSON shape:
{{"background":"<brief visual background>","profile_estimate":"<tentative profile estimate>"}}"""


CLASSIFICATION_SCHEMA: dict[str, Any] = {
    "name": "ocr_classification",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "is_pr": {"type": "boolean"},
            "is_ugc": {"type": "boolean"},
            "tags": {
                "type": "array",
                "maxItems": 20,
                "items": {"type": "string", "maxLength": 64},
            },
        },
        "required": ["is_pr", "is_ugc", "tags"],
        "additionalProperties": False,
    },
}


CLASSIFICATION_PROMPT = f"""You are classifying an Instagram post.
The OCR engine already extracted the visible text below (may be `(no text)` when the image had no readable text at all):
\"\"\"
{{ocr_text}}
\"\"\"

Use BOTH the image and the OCR text. When the OCR text is `(no text)`, judge
purely from the image visual.

- is_pr: true ONLY when the image visually contains the literal characters "PR"
  (case-insensitive) as readable text. When OCR is `(no text)`, this should be
  false unless you can actually see the letters "PR" drawn in the image.

- is_ugc: true when the image strongly suggests promotional/commerce content,
  especially when:
    - the image suggests a product or purchase link, including a clipboard /
      link-copy icon, "link in bio", URL-like notation, shop guidance, or
      similar cues
    - the image prominently presents prices, discounts, sale amounts, campaign
      offers, coupon-style amounts, or other purchase-oriented money information
  Otherwise false.

- tags: list of short Japanese strings that describe or categorize this post.
  No strict rules — pick whatever fits (topic, mood, product category, audience,
  vibe, etc.). Typically 3-10 tags. Each tag is a short noun phrase, no leading
  "#".

{_TEXT_FIDELITY_RULES}

{_JSON_OUTPUT_RULES}

JSON shape:
{{"is_pr":false,"is_ugc":true,"tags":["tag1","tag2"]}}"""


TASK_OCR = OcrTask(
    name="ocr",
    prompt=OCR_TEXT_PROMPT,
    schema=OCR_TEXT_SCHEMA,
    fields=("text",),
    # OCR は視覚を読み取るだけのタスクで思考連鎖は不要なため、reasoning を切って
    # 出力 token を OCR テキスト本体に振り向ける。
    disable_thinking=True,
    # text 1 フィールドだけ書かせ、no_text_detected は run_pipeline 側で text の
    # 空 / `<empty>` 判定から導出する。
    derive_no_text_detected=True,
    # 文字を粒子レベルで読み取る必要があるので vision token 上限の 1120 を割り当てる。
    max_soft_tokens=1120,
)

TASK_CONTEXT = OcrTask(
    name="context",
    prompt=CONTEXT_PROMPT,
    schema=CONTEXT_SCHEMA,
    fields=("background", "profile_estimate"),
    # 画像から直接読める情報 (背景・プロフィール推定) なので、OCR テキスト無しでも実行する。
    # text 空時は prompt の {ocr_text} が `(no text)` で埋まり、画像のみを根拠に判定させる。
    needs_ocr_text=True,
    # 背景・プロフィール推定は構図と全体の雰囲気が分かれば足りるので、低解像度で速度優先。
    max_soft_tokens=140,
)

TASK_CLASSIFICATION = OcrTask(
    name="classification",
    prompt=CLASSIFICATION_PROMPT,
    schema=CLASSIFICATION_SCHEMA,
    fields=("is_pr", "is_ugc", "tags"),
    # is_ugc / tags は画像視覚で判定可能、is_pr は text 必須だが OCR `(no text)` 時は
    # 画像内に "PR" 文字が無いので自動的に false に倒れる想定。CONTEXT と同様に実行する。
    needs_ocr_text=True,
    # 分類タスクも荒い視覚で十分なので低解像度に倒して速度を稼ぐ。
    max_soft_tokens=140,
)

TASK_ALL_IN_ONE = OcrTask(
    name="all",
    prompt=DEFAULT_PROMPT,
    schema=OCR_RESPONSE_SCHEMA,
    fields=(
        "text",
        "background",
        "profile_estimate",
        "is_pr",
        "is_ugc",
        "tags",
        "no_text_detected",
    ),
)


# endpoint.mode に対応する実行パイプライン。新しいモードを足したい場合はここに
# (name, tuple[OcrTask, ...]) を追加する。
TASK_PIPELINES: dict[str, tuple[OcrTask, ...]] = {
    "oneshot": (TASK_ALL_IN_ONE,),
    "split": (TASK_OCR, TASK_CONTEXT, TASK_CLASSIFICATION),
}
