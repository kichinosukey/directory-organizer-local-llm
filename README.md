# directory-organizer-local-llm

ローカル LLM を使ってディレクトリ整理の提案と適用を行う CLI です。  
通常モードは指定ディレクトリ配下を再帰的に全件走査し、Fast Lane は 30 秒以内の整理体験を狙って高信頼な一部だけを高速処理します。

## 特徴

- `plan` `run` `apply` の 3 コマンド
- `run` で `plan -> apply` を 1 プロセスで完了
- Fast Lane は保存済み `manifest.json` をそのまま apply
- `apply --manifest` は LLM 再推論なし
- 既存の `--mode plan|apply` も後方互換で利用可能
- 削除なし、move のみ
- 危険パス、拡張子変更、衝突はローカルでブロック
- `undo_manifest.json` と `apply_result.json` を保存

## 前提

- Python 3.10 以上
- ローカル LLM の OpenAI 互換 API
  - LM Studio
  - Ollama の OpenAI 互換エンドポイント
  - LocalAI など

`.env` はリポジトリ直下から自動で読み込みます。最低限、以下を設定してください。

```dotenv
LOCAL_LLM_MODEL=openai/gpt-oss-20b
LOCAL_LLM_BASE_URL=http://127.0.0.1:1234/v1
LOCAL_LLM_API_KEY=not-needed
```

## セットアップ

### `uv` を使う場合

```bash
cd /Users/kichinosukey-mba/projects/directory-organizer-local-llm
uv venv
source .venv/bin/activate
```

### `venv` を使う場合

```bash
cd /Users/kichinosukey-mba/projects/directory-organizer-local-llm
python3 -m venv .venv
source .venv/bin/activate
```

## 実行方法

### 1. plan のみ作る

```bash
python scripts/run_directory_organizer.py \
  plan \
  --target-dir ~/Downloads/messy-folder \
  --model openai/gpt-oss-20b
```

### 2. plan -> apply を一気通貫で実行する

```bash
python scripts/run_directory_organizer.py \
  run \
  --target-dir ~/Downloads/messy-folder \
  --model openai/gpt-oss-20b
```

TTY 上では plan 要約を出した後に `[a] apply / [v] view details / [q] quit` を受け付けます。  
非対話実行では `--yes` を付けると即 apply します。

```bash
python scripts/run_directory_organizer.py \
  run \
  --target-dir ~/Downloads/messy-folder \
  --model openai/gpt-oss-20b \
  --yes
```

### 3. 保存済み manifest を apply する

```bash
python scripts/run_directory_organizer.py \
  apply \
  --manifest ~/Downloads/messy-folder/.dirorganizer-runs/20260313T160000Z/manifest.json
```

### 4. Fast Lane を使う

```bash
python scripts/run_directory_organizer.py \
  run \
  --target-dir ~/Downloads \
  --fast-lane \
  --preset downloads-default \
  --model openai/gpt-oss-20b
```

Fast Lane は以下の既定制限で動きます。

- `max-depth=1`
- `max-files=30`
- `batch-size=15`
- `min-confidence=0.80`
- 対象拡張子: `.pdf` `.docx` `.txt` `.md` `.xlsx` `.csv` `.jpg` `.jpeg` `.png`
- `100 MiB` 超のファイルはスキップ
- 更新日時の新しい順で候補を絞り込み

### 5. mock で挙動確認する

```bash
python scripts/run_directory_organizer.py \
  run \
  --target-dir ./sample-dir \
  --fast-lane \
  --mock \
  --yes
```

## CLI オプション

### source ベースコマンド

`plan` と `run` で使えます。

| オプション | 説明 |
|---|---|
| `--target-dir`, `--source` | 整理対象ディレクトリ |
| `--model` | 使用モデル名 |
| `--base-url` | OpenAI 互換 API ベース URL |
| `--api-key` | API キー |
| `--rules` | 追加ルール JSON |
| `--output-dir` | 成果物保存先 |
| `--max-files` | 通常モードでは走査上限、Fast Lane では候補上限 |
| `--max-depth` | 走査深さの上限 |
| `--batch-size` | LLM へ渡す件数 |
| `--min-confidence` | apply 対象にする最小信頼度 |
| `--include-hidden` | 隠しファイルも対象にする |
| `--mock` | LLM の代わりにヒューリスティックを使う |
| `--fast-lane` | 高速整理モードを有効化 |
| `--preset` | `downloads-default`, `finance-receipts`, `research-papers` |
| `--yes` | `run` の確認プロンプトを省略する |

### manifest ベースコマンド

| コマンド | 説明 |
|---|---|
| `apply --manifest <path>` | 保存済み manifest を再利用して apply |

## プリセット

Fast Lane では毎回ルールを組み立てず、以下の preset を使います。

- `downloads-default`
- `finance-receipts`
- `research-papers`

preset には taxonomy, rename_style, allowed_extensions, confidence threshold, skip policy, destination mapping が含まれます。

## 成果物

各実行で `<output-dir>/<run_id>/` を作成し、以下を保存します。

- `plan.json`
- `plan.md`
- `manifest.json`
- `apply_result.json`
- `undo_manifest.json`

`manifest.json` は v2 形式です。

```json
{
  "version": 2,
  "created_at": "2026-03-13T12:00:00+00:00",
  "target_dir": "/path/to/target",
  "mode": "run",
  "fast_lane": true,
  "preset": "downloads-default",
  "rules": {},
  "summary": "short summary",
  "counts": {
    "files_scanned": 34,
    "files_considered": 30,
    "planned_moves": 18,
    "skipped": 10,
    "blocked": 2,
    "new_folders": 4,
    "applied_moves": 18
  },
  "warnings": [],
  "timings": {
    "scan_seconds": 1.5,
    "plan_seconds": 9.1,
    "save_seconds": 0.2,
    "apply_seconds": 1.1,
    "processing_seconds": 10.8,
    "total_seconds": 11.9
  },
  "operations": []
}
```

CLI の最後には結果行を 1 行出力します。

```text
[RESULT] mode=run status=success run_dir=/.../20260313T160000Z planned_moves=18 applied_moves=18 skipped=12
```

## 通常モードと Fast Lane の違い

### 通常モード

- 既定で全件走査
- 深いサブディレクトリも対象
- `apply` は `run` または legacy `--mode apply` を使う

### Fast Lane

- 深さ 1 のみ
- 高信頼な候補だけを短時間で処理
- `apply --manifest` で再推論しない
- 定期実行向き

## 定期実行

v1 では CLI と `launchd` 手順を提供します。  
Fast Lane を無人実行する場合は `run --fast-lane --yes` を使ってください。

### `launchd` 例

`~/Library/LaunchAgents/com.example.directory-organizer-fastlane.plist`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>com.example.directory-organizer-fastlane</string>
    <key>ProgramArguments</key>
    <array>
      <string>/Users/yourname/projects/directory-organizer-local-llm/.venv/bin/python</string>
      <string>/Users/yourname/projects/directory-organizer-local-llm/scripts/run_directory_organizer.py</string>
      <string>run</string>
      <string>--target-dir</string>
      <string>/Users/yourname/Downloads</string>
      <string>--fast-lane</string>
      <string>--preset</string>
      <string>downloads-default</string>
      <string>--yes</string>
    </array>
    <key>StartInterval</key>
    <integer>3600</integer>
    <key>WorkingDirectory</key>
    <string>/Users/yourname/projects/directory-organizer-local-llm</string>
    <key>StandardOutPath</key>
    <string>/tmp/directory-organizer-fastlane.out</string>
    <key>StandardErrorPath</key>
    <string>/tmp/directory-organizer-fastlane.err</string>
  </dict>
</plist>
```

読み込み例:

```bash
launchctl load ~/Library/LaunchAgents/com.example.directory-organizer-fastlane.plist
```

## テスト

```bash
python -m unittest tests.test_directory_organizer
```

## 安全設計

- delete しない
- move のみ
- 相対パスのみ許可
- 拡張子変更を拒否
- 衝突時はスキップ
- apply 前にローカル再検証
- undo 用 manifest を保存
