# OCR Worker

`LM Studio` のローカルモデルを使って Instagram 画像の OCR を回し続け、結果を API に書き戻すワーカーです。

最新一覧 API はデフォルトで未処理投稿のみ返す前提です。再OCRや確認用途で処理済みも含めたい場合だけ `OCR_INCLUDE_DONE=1` を設定してください。`OCR_VERSION` が現在のバージョンと異なる投稿は再処理対象にできます。

backlog が多い場合は `MAX_ITEMS_PER_CYCLE` で 1 サイクルあたりの処理件数を制限できます。上限に達したらその時点でサイクルを終え、次のポーリングで再び最新未処理から取り直します。

同一プロセス内で複数画像を並列に OCR したい場合は `OCR_CONCURRENCY` を設定します。デフォルトは従来どおり `1` です。ローカル LLM 側の GPU/VRAM 状況に依存するため、まず `OCR_CONCURRENCY=2` から試し、ダッシュボードの毎分処理件数と失敗率を見ながら上げてください。

複数台で動かす場合は `OCR_SHARD_COUNT` と `OCR_SHARD_INDEX` で担当範囲を分けられます。例えば 2 台なら 1 台目を `OCR_SHARD_COUNT=2` / `OCR_SHARD_INDEX=0`、2 台目を `OCR_SHARD_COUNT=2` / `OCR_SHARD_INDEX=1` にします。1 台運用に戻す場合は `OCR_SHARD_COUNT=1` / `OCR_SHARD_INDEX=0`、または未設定で全件処理します。

常駐モードでは、処理対象がある限り次サイクルへ即時進みます。処理対象がない時やエラー時だけ `IDLE_SLEEP_SEC` だけ待機します。デフォルトは `60` 秒です。

## 前提

- `python3`
- `requests`
- `LM Studio` の Local Server が起動済み
- 画像入力に対応した vision モデルを `LM Studio` でロード済み

## セットアップ

`.venv` を使う前提です。依存追加が必要な場合もグローバルの `pip` は使わず、`.venv/bin/pip` を使ってください。`.env` はスクリプトが自動で読み込みます。

```bash
virtualenv .venv
.venv/bin/pip install requests
export OCR_API_BASE_URL="https://your-host/api/ocr"
export OCR_IMAGE_HOST_PREFIX="https://your-host"
export OCR_API_KEY="your-api-key"
export OCR_VERSION="2026-05-31"
export OCR_CONCURRENCY="1"
export OCR_SHARD_COUNT="1"
export OCR_SHARD_INDEX="0"
export LM_STUDIO_API_URL="http://127.0.0.1:1234/v1/chat/completions"
export LM_STUDIO_MODEL="your-vision-model"
```

## 実行

ダッシュボード表示で常駐（デフォルト）:

```bash
.venv/bin/python ocr_worker.py
```

systemd や cron などダッシュボードを出せない環境向けにログ垂れ流し常駐:

```bash
.venv/bin/python ocr_worker.py --no-dashboard
```

プロンプト調整用のテストモード（API には書き戻さない。生応答とパース結果をフル出力する）:

```bash
.venv/bin/python ocr_worker.py --test            # 既定で3件
.venv/bin/python ocr_worker.py --test --limit 1  # 件数指定
```

`--test` は `LM_STUDIO_OCR_PROMPT`（未設定なら `DEFAULT_PROMPT`）をそのまま 1 回試行します。`OCR_INCLUDE_DONE=1` を設定すれば、すでに OCR 済みの投稿に対してもプロンプトを試せます。

## プロンプト

実際にモデルへ投げているプロンプトは `ocr_worker.py` 内に定数として置いてあります。プロンプトを変えたいときは下記を直接編集するか、`LM_STUDIO_OCR_PROMPT` 環境変数で `DEFAULT_PROMPT` を上書きしてください。

- [`DEFAULT_PROMPT`](./ocr_worker.py#L30-L60) — 唯一のプロンプト（通常運用 / `--test` モードどちらも同じものを使う）

旧 `FALLBACK_PROMPT` および OCR 自体の retry は廃止しています。LM Studio の返答が空 / 不正 JSON だった場合は即時失敗扱いとし、原因をログから追って prompt 側を直す方針です。

## 出力JSON

### モデルに要求している返却 JSON

```json
{
  "text": "<画像から抽出した本文。改行は意味があるときだけ保持>",
  "background": "<画像の視覚的背景の簡潔な説明 (日本語, 1-3文)>",
  "profile_estimate": "<投稿者プロフィールの推定 (日本語, 1-3文, 推定であることを明示)>",
  "is_pr": true,
  "is_ugc": true,
  "tags": ["タグ1", "タグ2"],
  "no_text_detected": false
}
```

パーサーは余分な前後文字列があっても最初の `{` から最後の `}` までを切り出して JSON として読みます。それでも JSON にならない / オブジェクトでない場合はエラー扱いで失敗させます。

### API に POST する JSON

`POST {OCR_API_BASE_URL}/media/{pk}` で書き戻すペイロードです。空文字は半角スペース `" "` に置換してから送ります（API 側で NOT NULL 要件を満たすための保険）。

```json
{
  "text": "<抽出本文 (空なら ' ')>",
  "background": "<背景説明 (空なら ' ')>",
  "profile_estimate": "<プロフィール推定 (空なら ' ')>",
  "is_pr": true,
  "is_ugc": true,
  "tags": ["タグ1", "タグ2"],
  "no_text_detected": false,
  "version": "<OCR_VERSION の値, 例: 2026-05-31>"
}
```

| フィールド | 型 | 説明 |
| --- | --- | --- |
| `text` | string | 抽出本文。`no_text_detected=true` の場合は空でよい。 |
| `background` | string | 画像の視覚的背景（日本語）。 |
| `profile_estimate` | string | 投稿者プロフィールの推定（日本語、推定であることを明示）。 |
| `is_pr` | boolean | 画像内に「PR」というリテラル文字（大文字小文字問わず）が読み取れる場合のみ `true`。 |
| `is_ugc` | boolean | 購入リンク（"link in bio"・URL・クリップボードアイコン等）や価格・割引・クーポンなどの購買性シグナルが目立つ場合に `true`。 |
| `tags` | string[] | LLM がルール無視で自由に付ける投稿タグ（日本語、`#` なし、目安 3-10 個）。空配列もあり得る。 |
| `no_text_detected` | boolean | 読み取れるテキストが本当に存在しないと判断した場合のみ `true`。部分的にでも読めれば `false`。 |
| `version` | string | このワーカーが書き込んだプロンプト/出力形式のバージョン (`OCR_VERSION`)。プロンプトや出力形式を更新したらここを変えて再 OCR を走らせる。 |

## 補足

`LM Studio` 側は OpenAI 互換 API を前提にしています。OCR には画像入力対応モデルが必要です。テキスト専用モデルでは動きません。

プロンプトや出力形式を更新して再OCRしたいときは `OCR_VERSION` を変更し、必要に応じて `OCR_INCLUDE_DONE=1` で既存投稿も取得してください。

画像は OCR 前にアスペクト比を維持したまま長辺 `IMAGE_MAX_DIM` まで縮小します。

デフォルトのダッシュボードでは上段に色付きログ、下段に毎分の成功/失敗件数グラフ、ヘッダに現在処理中の投稿と累計件数を表示します。`q` で抜けられます。
