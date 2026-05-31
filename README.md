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
export OCR_VERSION="2026-04-11"
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

`--test` は `LM_STUDIO_OCR_PROMPT`（未設定なら `DEFAULT_PROMPT`）をそのまま 1 回試行し、retry も fallback prompt も使いません。`OCR_INCLUDE_DONE=1` を設定すれば、すでに OCR 済みの投稿に対してもプロンプトを試せます。

## プロンプト

実際にモデルへ投げているプロンプトは `ocr_worker.py` 内に定数として置いてあります。プロンプトを変えたいときは下記を直接編集するか、`LM_STUDIO_OCR_PROMPT` 環境変数で `DEFAULT_PROMPT` を上書きしてください。`FALLBACK_PROMPT` は再試行専用でコード固定です。

- [`DEFAULT_PROMPT`](./ocr_worker.py#L30-L52) — 初回試行 / `--test` モードで使われるもの
- [`FALLBACK_PROMPT`](./ocr_worker.py#L54-L66) — 再試行時に切り替わる軽量版

## 出力JSON

### モデルに要求している返却 JSON

`DEFAULT_PROMPT` 版:

```json
{
  "text": "<画像から抽出した本文。改行は意味があるときだけ保持>",
  "background": "<画像の視覚的背景の簡潔な説明 (日本語, 1-3文)>",
  "profile_estimate": "<投稿者プロフィールの推定 (日本語, 1-3文, 推定であることを明示)>",
  "is_pr": true,
  "no_text_detected": false
}
```

`FALLBACK_PROMPT` 版（`background` / `profile_estimate` を要求しない）:

```json
{
  "text": "<抽出した本文>",
  "is_pr": true,
  "no_text_detected": false
}
```

パーサーは余分な前後文字列があっても最初の `{` から最後の `}` までを切り出して JSON として読みます。それでも JSON にならない / オブジェクトでない場合はエラー扱いで再試行します。

### API に POST する JSON

`POST {OCR_API_BASE_URL}/media/{pk}` で書き戻すペイロードです。空文字は半角スペース `" "` に置換してから送ります（API 側で NOT NULL 要件を満たすための保険）。

```json
{
  "text": "<抽出本文 (空なら ' ')>",
  "background": "<背景説明 (空なら ' ')>",
  "profile_estimate": "<プロフィール推定 (空なら ' ')>",
  "is_pr": true,
  "no_text_detected": false,
  "version": "<OCR_VERSION の値, 例: 2026-04-11>"
}
```

| フィールド | 型 | 説明 |
| --- | --- | --- |
| `text` | string | 抽出本文。`no_text_detected=true` の場合は空でよい。 |
| `background` | string | 画像の視覚的背景（日本語）。`FALLBACK_PROMPT` 採用時は空で送信される。 |
| `profile_estimate` | string | 投稿者プロフィールの推定（日本語、推定であることを明示）。`FALLBACK_PROMPT` 採用時は空で送信される。 |
| `is_pr` | boolean | PR 投稿らしい兆候 (#PR・購入リンク・価格表示など) があれば `true`。 |
| `no_text_detected` | boolean | 読み取れるテキストが本当に存在しないと判断した場合のみ `true`。部分的にでも読めれば `false`。 |
| `version` | string | このワーカーが書き込んだプロンプト/出力形式のバージョン (`OCR_VERSION`)。プロンプトや出力形式を更新したらここを変えて再 OCR を走らせる。 |

## 補足

`LM Studio` 側は OpenAI 互換 API を前提にしています。OCR には画像入力対応モデルが必要です。テキスト専用モデルでは動きません。

OCR 結果は `text` に加えて `background`、`profile_estimate`、`version` を送信します。プロンプトや出力形式を更新して再OCRしたいときは `OCR_VERSION` を変更し、必要に応じて `OCR_INCLUDE_DONE=1` で既存投稿も取得してください。

OCR が空だった場合は `no_text_detected` も送信します。モデルが `no_text_detected=true` と明示した場合はそのまま採用し、それ以外の失敗や空結果は `OCR_RETRY_COUNT` 回まで再試行します。

画像は OCR 前にアスペクト比を維持したまま長辺 `IMAGE_MAX_DIM` まで縮小します。初回は詳細 prompt、再試行時は token 消費を抑えた軽量 OCR prompt に切り替えます。

デフォルトのダッシュボードでは上段に色付きログ、下段に毎分の成功/失敗件数グラフ、ヘッダに現在処理中の投稿と累計件数を表示します。`q` で抜けられます。
