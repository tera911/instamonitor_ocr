# OCR Worker

`LM Studio` のローカルモデルを使って Instagram 画像の OCR を回し続け、結果を API に書き戻すワーカーです。

配信順序とフィルタはサーバー側 (Laravel) が一括で決めます。本ワーカーは「サーバーから渡された pk を OCR して書き戻す」だけに専念します。

- **配信優先度**: サーバーは「当日 (JST 00:00 以降) の未処理を `taken_at` 昇順」→「過去の未処理を `taken_at` 降順」の順で返します。
- **再 OCR**: `OCR_VERSION` を毎リクエストにクエリ `current_version` として渡すので、`OCR_VERSION` を上げるだけで古いバージョンの投稿が再配信されます。`OCR_INCLUDE_DONE` のような明示フラグは不要になりました。
- **複数 LM Studio エンドポイント (並列度を個別調整)**: `endpoints.toml` で URL ごとに `concurrency` を指定します (`endpoints.toml.example` を参照)。Python プロセス 1 台が共有キューを持ち、各エンドポイント × `concurrency` 個のワーカースレッドが空き次第キューから取って自分の URL に投げる動的振り分け方式です。強いマシンに大きな `concurrency`、弱いマシンに小さい `concurrency` を割り当てれば、速い側が遅い側を待たずに先に消化していきます。
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

`endpoints.toml` は URL ごとに並列度と (任意で) モデル固有の推論パラメータを指定します。`endpoints.toml.example` をコピーして編集してください。

```toml
# 必須
[[endpoints]]
url = "http://machine-a:1234/v1/chat/completions"
concurrency = 4

# 任意項目を書くと、そのエンドポイントだけ .env のグローバル値を上書きする
[[endpoints]]
url = "http://machine-b:1234/v1/chat/completions"
concurrency = 64
model = "google/gemma-4-e4b"
max_tokens = 4096
frequency_penalty = 0.1
enable_thinking = false
```

エンドポイント別に指定できるパラメータは `model` / `max_tokens` / `frequency_penalty` / `repetition_penalty` / `enable_thinking` / `thinking_token_budget` / `max_soft_tokens`。書かなかった項目は `.env` (`LM_STUDIO_*`) のグローバル値、それも無ければコード内デフォルトへ順にフォールバックします。**4B エンドポイントと 12B エンドポイントを並べて、それぞれ最適な `frequency_penalty` で叩く**といった運用が可能です。

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

プロンプト調整用のテストモード（API には書き戻さない。生応答とパース結果をフル出力する）:

```bash
.venv/bin/python ocr_worker.py --test            # 既定で3件
.venv/bin/python ocr_worker.py --test --limit 1  # 件数指定
```

`--test` は `LM_STUDIO_OCR_PROMPT`（未設定なら `DEFAULT_PROMPT`）をそのまま 1 回試行します。テストモードでは `endpoints.toml` の先頭エンドポイントだけを使います。

## プロンプト

実際にモデルへ投げているプロンプトは `ocr_worker.py` 内に定数として置いてあります。プロンプトを変えたいときは下記を直接編集するか、`LM_STUDIO_OCR_PROMPT` 環境変数で `DEFAULT_PROMPT` を上書きしてください。

- [`DEFAULT_PROMPT`](./ocr_worker.py#L32-L71) — 唯一のプロンプト（通常運用 / `--test` モードどちらも同じものを使う）

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

### 推論暴走対策 (max_tokens / maxLength / penalty / thinking)

文字びっしりの画像や、モデルが緩いループに陥ったときに推論が止まらず `LM_STUDIO_TIMEOUT_SEC` を踏み抜くことがあります。これを抑えるため、複数のガードを重ねています。

- `LM_STUDIO_MAX_TOKENS` (最小 64) — 推論時間そのものを切るためのハード上限。デフォルトは 4096、E4B / 12B では後述の表参照
- schema 側の `maxLength` (text=4000, background=1000, profile_estimate=1000, tags items=64, tags maxItems=20) — 構造化出力 grammar に量化子として落ちて、内容が長くなりすぎることを構文レベルで防ぎます。vLLM では量化子上限制約はなく、llama.cpp 系の制約も text=4000 までなら問題ありません
- `LM_STUDIO_FREQUENCY_PENALTY` (OpenAI 互換) — 反復ループ (`\n\n\n…` や `しししし…` のような暴走) を抑止する第一の打ち手。0.1〜0.3 の範囲で効きます (0.5 以上は本文を歪めるので非推奨)
- `LM_STUDIO_REPETITION_PENALTY` (vLLM 拡張) — `frequency_penalty` の代替候補ですが Gemma 4 系では 1.05 でも強すぎて text フィールドが空になる副作用を観測。`frequency_penalty` を推奨します
- `LM_STUDIO_ENABLE_THINKING` (Gemma 4 系) — reasoning モードの有無。`true` だと reasoning が `max_tokens` を食い切って `content=null` になる事故が起きやすいので、現状は `false` 推奨。reasoning を活かしたい場合は `LM_STUDIO_THINKING_TOKEN_BUDGET` で reasoning の上限トークンを切ること
- `LM_STUDIO_MAX_SOFT_TOKENS` (Gemma 4 系) — vision token 数 (画像解像度)。サポート値 70 / 140 / 280 (デフォルト) / 560 / 1120。OCR 精度を上げたい場合に有効ですが、**vLLM 0.23 ではリクエストレベルで silently 無視されるケースを観測**。確実に効かせるには `vllm serve ... --mm-processor-kwargs '{"max_soft_tokens": 560}'` でサーバー側に渡す

#### モデル別の推奨パラメータ

セッション内の 100 件規模 A/B 評価で得られた、`google/gemma-4-*-it-qat-w4a16-ct` (vLLM 0.23 + xgrammar) 向けの最適値です。`endpoints.toml` の任意項目として書くか、`.env` のグローバル値として書きます。

| パラメータ | E4B (4B 系) | 12B | 備考 |
| --- | --- | --- | --- |
| `model` | `google/gemma-4-e4b` | `google/gemma-4-e4b` (alias) | 12B は vLLM 側で E4B 互換 alias を切ってる前提 |
| `max_tokens` | 8192 | 4096 | 12B は `max_model_len=8192` 制約あり |
| `frequency_penalty` | **0.3** | **0.1** | E4B は反復暴走しやすいので強め、12B は緩めで十分 |
| `enable_thinking` | false | false | thinking on は reasoning 暴走で content=null が出る・text が短縮される副作用、Speed も 3 倍遅 |
| `concurrency` | 64 | 64 | vLLM の `--max-num-seqs` を超えなければ 96 / 128 まで余地あり (proxy 無しの本番想定) |
| 期待 throughput | 〜7 req/s | 〜1.7 req/s | 100件×並列8の実測値ベース。本番並列ではスケール |
| corruption (HTMLタグ等) | ほぼゼロ | raw では `<br>` が約 25% に混入 → **`normalize_model_text` で `\n` に正規化済み** | 12B は改行を `<br>` で表現する癖を持つが、parse 後の `OCRResult.text` 等には残らない |

12B の `<br>` 癖は学習バイアスで、画像内の改行を表現する **正当な記号** です (情報損失なし)。ワーカー側で `normalize_model_text` が `<br>` を `\n` に変換し、それ以外の HTML/XML タグ (`<p>`, `<hr>` の反復暴走パターンを含む) は中身だけ残して strip します。連続改行は `\n\n` まで圧縮されます。

両ガードを重ねるのは、`max_tokens` だけだと壊れた途中で切れた JSON が返る可能性があるため。`maxLength` で構造的に閉じる、`frequency_penalty` で反復自体を止める、`max_tokens` で最終クリーンカット、という三段構えになっています。

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

画像は OCR 前にアスペクト比を維持したまま長辺 `IMAGE_MAX_DIM` まで縮小します。

デフォルトのダッシュボードでは上段に色付きログ、下段に毎分の成功/失敗件数グラフ、ヘッダに現在処理中の投稿と累計件数を表示します。`q` で抜けられます。
