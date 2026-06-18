# OCR Worker

`LM Studio` のローカルモデルを使って Instagram 画像の OCR を回し続け、結果を API に書き戻すワーカーです。

配信順序とフィルタはサーバー側 (Laravel) が一括で決めます。本ワーカーは「サーバーから渡された pk を OCR して書き戻す」だけに専念します。

- **配信優先度**: サーバーは「当日 (JST 00:00 以降) の未処理を `taken_at` 昇順」→「過去の未処理を `taken_at` 降順」の順で返します。
- **再 OCR**: `OCR_VERSION` を毎リクエストにクエリ `current_version` として渡すので、`OCR_VERSION` を上げるだけで古いバージョンの投稿が再配信されます。`OCR_INCLUDE_DONE` のような明示フラグは不要になりました。
- **複数 LM Studio エンドポイント (並列度を個別調整)**: `endpoints.toml` で URL ごとに `concurrency` と `mode` を指定します (`endpoints.toml.example` を参照)。Python プロセス 1 台が共有キューを持ち、各エンドポイント × `concurrency` 個のワーカースレッドが空き次第キューから取って自分の URL に投げる動的振り分け方式です。強いマシンに大きな `concurrency`、弱いマシンに小さい `concurrency` を割り当てれば、速い側が遅い側を待たずに先に消化していきます。
- **タスク分割 (Q4 等での精度低下対策)**: `mode = "split"` を指定したエンドポイントは、1 画像を「OCR」「文脈推定 (background / profile_estimate)」「分類 (is_pr / is_ugc / tags)」の 3 リクエストに分割して投げます。Q4 量子化モデルで OCR と分類タスクを 1 プロンプトに混ぜると attention が崩れて両方とも劣化する事象に対する回避策です。後段の 2 タスクには 1 つ前で得た OCR テキストを `{ocr_text}` 経由で渡します。OCR で `no_text_detected=true` が立った場合は残り 2 タスクをスキップします。Q8 等で精度に余裕があるエンドポイントは `mode = "oneshot"` (デフォルト) のままで OK です。
- **複数台運用は非推奨**: 旧 `OCR_SHARD_COUNT` / `OCR_SHARD_INDEX` は廃止しました。サーバー側に重複排除の仕組みは無いので、本ワーカーは **必ず 1 プロセスのみ** 起動してください。スループットは LM Studio エンドポイントの台数と各 `concurrency` で稼ぎます。

1 サイクルでサーバーから最大 `FETCH_PAGE_SIZE` 件を取得 → 全件処理 → 0 件なら `IDLE_SLEEP_SEC` (デフォルト 60 秒) 待機します。

画像を CDN 経由ではなく S3 マウント等のローカルパスから直接読みたい場合は `OCR_IMAGE_LOCAL_ROOT` を設定します。例えば `OCR_IMAGE_LOCAL_ROOT=~/s3` にすると、`https://your-host/stories/xxx.jpg` のような URL は `~/s3/stories/xxx.jpg` から読みます。ファイルが存在しない場合は WARNING を出した上で従来通り HTTP にフォールバックします（マウント外れに気付きつつ処理は止めない）。未設定なら従来通り常に HTTP 経由です。

## ログファイル出力

エラー調査用にログをファイルへ書き出したい場合は `OCR_LOG_FILE` を設定します。`RotatingFileHandler` で自動ローテーションされ、ダッシュボードモードでも `--no-dashboard` モードでもファイルへの記録は止まりません。

```bash
export OCR_LOG_FILE="$HOME/logs/ocr.log"
# 任意。デフォルトは 10MB ローテ、5 世代保持
export OCR_LOG_MAX_BYTES="10485760"
export OCR_LOG_BACKUP_COUNT="5"
# 任意。ファイルに書く最低レベル (DEBUG / INFO / WARNING / ERROR / CRITICAL)。
# 例: ERROR にすると ERROR と CRITICAL だけファイルに残る。ターミナル側の
# 出力レベルは LOG_LEVEL のままなので、画面ログを汚さずに本番ファイルだけ静かにできる。
export OCR_LOG_FILE_LEVEL="ERROR"
```

`logger.exception(...)` 経由のトレースバックも含めて記録されるので、`Invalid \escape` などの過去のパース失敗もファイルから事後追跡できます。未設定なら従来通り stdout / ダッシュボードのみで、ファイル出力は行われません。

## 前提

- `python3` (TOML パースに `tomllib` を使うため **3.11 以上**)
- `requests`
- `LM Studio` の Local Server が 1 つ以上起動済み (**Structured Outputs 対応 = 0.3.0 以上**)
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
# 任意: S3 マウントなどローカルから読みたい場合に設定 (未設定なら CDN 経由)
# export OCR_IMAGE_LOCAL_ROOT="$HOME/s3"
# エンドポイント定義ファイル (未設定時は ./endpoints.toml を見る)
export OCR_ENDPOINTS_FILE="$PWD/endpoints.toml"
export LM_STUDIO_MODEL="your-vision-model"
```

`endpoints.toml` は次のように URL ごとに並列度と `mode` を指定します。`endpoints.toml.example` をコピーして編集してください。

```toml
[[endpoints]]
url = "http://machine-a:1234/v1/chat/completions"
concurrency = 4
mode = "oneshot"  # 省略時のデフォルト。1 リクエストで全項目を生成する

[[endpoints]]
url = "http://machine-b:1234/v1/chat/completions"
concurrency = 2
mode = "split"    # OCR / 文脈 / 分類 を 3 リクエストに分割する (Q4 等の精度低下対策)
```

旧設定 (`LM_STUDIO_API_URL` / `LM_STUDIO_API_URLS` / `OCR_CONCURRENCY` / `OCR_SHARD_*`) は廃止しています。並列度は `endpoints.toml` の `concurrency` で URL ごとに調整してください。

## 実行

ダッシュボード表示で常駐（デフォルト）:

```bash
.venv/bin/python ocr_worker.py
```

systemd や cron などダッシュボードを出せない環境向けにログ垂れ流し常駐:

```bash
.venv/bin/python ocr_worker.py --no-dashboard
```

プロンプト調整用のテストモード（API には書き戻さない。デフォルトはログのみ）:

```bash
.venv/bin/python ocr_worker.py --test                                # 既定で3件
.venv/bin/python ocr_worker.py --test --limit 10                     # 件数指定
.venv/bin/python ocr_worker.py --test --pk 12345                     # pk 単体を指定
.venv/bin/python ocr_worker.py --test --pk 12345,67890               # 複数 pk をカンマ区切りで指定
.venv/bin/python ocr_worker.py --test --limit 10 --skip-no-text-detect   # text 入りだけ 10 件集める
.venv/bin/python ocr_worker.py --test --report                       # HTML レポートも出す
.venv/bin/python ocr_worker.py --test --report --report-dir ~/ocr_reports  # 出力先を変える
```

テストモードでは `endpoints.toml` の先頭エンドポイントだけを使い、そのエンドポイントの `concurrency` 分のスレッドで並列に投げます (HTTP・画像 download と LLM 呼び出しの I/O 待ちを埋めて GPU を遊ばせないため)。先頭の `mode` が `"split"` なら 3 タスクを順に走らせ、`"oneshot"` なら従来通り 1 リクエストで動きます。

`--pk` を付けると `/media/latest` から返ってきたページのうち、指定 pk と一致するものだけを処理対象にします（`--limit` / `--skip-no-text-detect` は無視されます）。pk が現ページに居ない（=その `OCR_VERSION` ではサーバー側で既に処理済みと判定されている）場合は warning を出してスキップするので、強制的に再テストしたいときは `OCR_VERSION` を bump してから実行してください。

`--skip-no-text-detect` を付けると、OCR 結果が `no_text_detected=true` だった投稿は採用カウントから外し、text 入り (= 検証になる) 投稿が `--limit` 件集まるまで `/media/latest` の続きを引き出して投げ続けます。1 ページ (`FETCH_PAGE_SIZE` 件) スキャンしきっても件数が足りない場合は集まった分だけで終了し、warning が出ます。

`--report` を付けたときだけ、`<report-dir>/test_<timestamp>/index.html` が生成されます (デフォルト出力先 `./reports/`、`.gitignore` 済み)。HTML は CDN URL を `<img src>` で埋め込むので、サムネ表示には API key 不要の公開 URL であることが前提です。生成後はブラウザで開けば画像と OCR テキスト / background / profile_estimate / tags / is_pr / is_ugc を 1 列で並べて目視比較できます。普段の高速確認はログだけで足りるので opt-in にしています。

## プロンプトとタスクパイプライン

モデルに投げているプロンプトと JSON schema は `ocr_worker.py` 内に定数として置いてあります。

- **one-shot mode** (`mode = "oneshot"`)
  - `DEFAULT_PROMPT` + `OCR_RESPONSE_SCHEMA` を 1 リクエストで投げる
  - 全項目を 1 度で生成する従来動作
- **split mode** (`mode = "split"`)
  - `OCR_TEXT_PROMPT` + `OCR_TEXT_SCHEMA` (text / no_text_detected)
  - `CONTEXT_PROMPT` + `CONTEXT_SCHEMA` (background / profile_estimate, 画像 + OCR テキストを入力)
  - `CLASSIFICATION_PROMPT` + `CLASSIFICATION_SCHEMA` (is_pr / is_ugc / tags, 画像 + OCR テキストを入力)
  - を順に投げ、結果をマージして 1 件の出力 JSON にする

### 新しいタスクを追加したいとき

`ocr_worker.py` の `OcrTask` を 1 つ書いて `TASK_PIPELINES["split"]` のタプル末尾に追加するだけで、split mode のパイプライン後段に組み込まれます。`OcrTask` のフィールド:

| field | 説明 |
| --- | --- |
| `name` | ログ表示用の識別子 (例: `"ocr"`, `"context"`, `"safety_check"`) |
| `prompt` | このタスクで送るプロンプト。`needs_ocr_text=True` なら `{ocr_text}` プレースホルダで OCR 結果が埋め込まれる |
| `schema` | このタスクの Structured Outputs 用 JSON schema |
| `fields` | このタスクが埋めるキー (ログ / バリデーション用) |
| `skip_if_no_text` | 直前までに `no_text_detected=true` が立っていたらスキップする |
| `needs_ocr_text` | prompt に OCR 結果テキストを差し込む |

新しい mode (例: `"split_with_safety"`) を作りたい場合は `TASK_PIPELINES` に `(tuple[OcrTask, ...])` を 1 行追加します。`endpoints.toml` の `mode` 値に未知の文字列を書くと起動時に弾かれます。

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

出力 JSON は LM Studio の Structured Outputs (`response_format: json_schema`) で文法的に強制しているため、バックスラッシュ未エスケープ等の不正 JSON が物理的に発生しません。万一に備えてパーサーは最初の `{` から最後の `}` までを切り出すフォールバックも持っています。

### 推論暴走対策 (max_tokens / maxLength)

文字びっしりの画像や、モデルが緩いループに陥ったときに推論が止まらず `LM_STUDIO_TIMEOUT_SEC` を踏み抜くことがあります。これを抑えるため、payload に `max_tokens` を、schema の各フィールドに `maxLength` / `maxItems` を入れています。

- `LM_STUDIO_MAX_TOKENS` (デフォルト 4096, 最小 64) — 推論時間そのものを切るためのハード上限。ループ系のタイムアウトに直接効きます
- schema 側の `maxLength` (text=8000, background=1000, profile_estimate=1000, tags items=64, tags maxItems=20) — LM Studio (llama.cpp) の GBNF grammar に量化子として落ちて、内容が長くなりすぎることを構文レベルで防ぎます

両方積むのは、`max_tokens` だけだと壊れた途中で切れた JSON が返る可能性があるためで、`maxLength` と組み合わせると上限内なら閉じた JSON、超えても max_tokens でクリーンに切断、という棲み分けになります。

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

プロンプトや出力形式を更新して再OCRしたいときは `OCR_VERSION` を変更してください。サーバー側が `current_version` を見て古いバージョンの投稿を自動的に再配信します。

画像は OCR 前にアスペクト比を維持したまま長辺 `IMAGE_MAX_DIM` (デフォルト 1920) まで縮小します。Instagram の Stories / Reels がネイティブ 1080×1920 なので、これで素通りさせ OCR 精度を稼ぐ前提です。フィード正方形 / ポートレートは元々それ以下なので無処理、長辺 2000 超の高解像度広告だけ 1920 に丸めます。VRAM / 速度に余裕があれば 2048 や 2560 に上げても良いです。

デフォルトのダッシュボードでは上段に色付きログ、下段に毎分の成功/失敗件数グラフ、ヘッダに現在処理中の投稿と累計件数を表示します。`q` で抜けられます。
