# OCR Worker

`LM Studio` のローカルモデルを使って Instagram 画像の OCR を回し続け、結果を API に書き戻すワーカーです。

最新一覧 API はデフォルトで未処理投稿のみ返す前提です。再OCRや確認用途で処理済みも含めたい場合だけ `OCR_INCLUDE_DONE=1` を設定してください。`OCR_VERSION` が現在のバージョンと異なる投稿は再処理対象にできます。

backlog が多い場合は `MAX_ITEMS_PER_CYCLE` で 1 サイクルあたりの処理件数を制限できます。上限に達したらその時点でサイクルを終え、次のポーリングで再び最新未処理から取り直します。

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
export OCR_SHARD_COUNT="1"
export OCR_SHARD_INDEX="0"
export LM_STUDIO_API_URL="http://127.0.0.1:1234/v1/chat/completions"
export LM_STUDIO_MODEL="your-vision-model"
```

## 実行

1回だけ流す:

```bash
.venv/bin/python ocr_worker.py --once
```

常駐:

```bash
.venv/bin/python ocr_worker.py
```

ダッシュボード表示:

```bash
.venv/bin/python ocr_worker.py --dashboard
```

## 補足

`LM Studio` 側は OpenAI 互換 API を前提にしています。OCR には画像入力対応モデルが必要です。テキスト専用モデルでは動きません。

OCR 結果は `text` に加えて `background`、`profile_estimate`、`version` を送信します。プロンプトや出力形式を更新して再OCRしたいときは `OCR_VERSION` を変更し、必要に応じて `OCR_INCLUDE_DONE=1` で既存投稿も取得してください。

OCR が空だった場合は `no_text_detected` も送信します。モデルが `no_text_detected=true` と明示した場合はそのまま採用し、それ以外の失敗や空結果は `OCR_RETRY_COUNT` 回まで再試行します。

画像は OCR 前にアスペクト比を維持したまま長辺 `IMAGE_MAX_DIM` まで縮小します。初回は詳細 prompt、再試行時は token 消費を抑えた軽量 OCR prompt に切り替えます。

`--dashboard` では上段に色付きログ、下段に毎分の成功/失敗件数グラフ、ヘッダに現在処理中の投稿と累計件数を表示します。
