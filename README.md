# directory-organizer-local-llm

ローカル LLM を使ってディレクトリ整理の提案と適用を行う、安全寄りの CLI エージェントです。

目的は「雑多なファイル群を、小さく予測可能なフォルダ構造へ寄せる」ことです。  
LLM は分類提案だけを担当し、実際のファイル移動はローカル検証を通ったものだけが実行されます。

## 特徴

- OpenAI 互換のローカル API に対応
  - LM Studio
  - Ollama の OpenAI 互換エンドポイント
  - LocalAI など
- 既定は `plan` モード
  - まず提案 JSON と Markdown レポートを出す
- `apply` モードでも削除はしない
  - move のみ
- 危険な提案はローカルで拒否
  - 絶対パス禁止
  - `..` 禁止
  - 拡張子の変化を禁止
  - 既存ファイルとの衝突を拒否
  - 低信頼度の提案をスキップ可能
- LLM なしでも `--mock` で動作確認可能

## ディレクトリ構成

```text
directory-organizer-local-llm/
├── dirorganizer/
│   ├── __init__.py
│   ├── llm_client.py
│   ├── models.py
│   ├── planner.py
│   ├── rules.py
│   └── scanner.py
├── scripts/
│   └── run_directory_organizer.py
└── tests/
    └── test_directory_organizer.py
```

## 前提

- Python 3.10 以上
- `uv` を使う場合は `uv` がインストール済みであること
- ローカル LLM の OpenAI 互換 API が起動していること

例:

```bash
LM Studio: http://127.0.0.1:1234/v1
Ollama:    http://127.0.0.1:11434/v1
```

`.env` はリポジトリ直下から自動で読み込みます。必要なら [`.env.sample`](/Users/kichinosukey-mba/projects/directory-organizer-local-llm/.env.sample) をコピーして使ってください。

## セットアップ

### `uv` を使う場合

1. プロジェクトへ移動します。

```bash
cd /Users/kichinosukey-mba/projects/directory-organizer-local-llm
```

2. 仮想環境を作成します。

```bash
uv venv
```

3. 仮想環境を有効化します。

```bash
source .venv/bin/activate
```

4. 任意で開発用ツールを追加します。

```bash
uv pip install ruff
```

5. `.env.sample` をコピーして `.env` を作成します。

```bash
cp .env.sample .env
```

6. `.env` を編集します。

```dotenv
LOCAL_LLM_MODEL=openai/gpt-oss-20b
LOCAL_LLM_BASE_URL=http://127.0.0.1:1234/v1
LOCAL_LLM_API_KEY=not-needed
```

7. まずは mock モードで動作確認します。

```bash
uv run python scripts/run_directory_organizer.py \
  --target-dir ./sample-dir \
  --mode plan \
  --mock
```

8. 実 LLM で計画を出します。

```bash
uv run python scripts/run_directory_organizer.py \
  --target-dir ~/Downloads/messy-folder \
  --mode plan
```

9. 問題なければ適用します。

```bash
uv run python scripts/run_directory_organizer.py \
  --target-dir ~/Downloads/messy-folder \
  --mode apply
```

### 標準の `venv` を使う場合

1. プロジェクトへ移動します。

```bash
cd /Users/kichinosukey-mba/projects/directory-organizer-local-llm
```

2. 仮想環境を作成します。

```bash
python3 -m venv .venv
```

3. 仮想環境を有効化します。

```bash
source .venv/bin/activate
```

4. 任意で開発用ツールを追加します。

```bash
python -m pip install --upgrade pip
python -m pip install ruff
```

5. `.env.sample` をコピーして `.env` を作成します。

```bash
cp .env.sample .env
```

6. `.env` を編集して `LOCAL_LLM_MODEL` と `LOCAL_LLM_BASE_URL` を設定します。

## 実行例

### 1. dry-run 相当の計画作成

```bash
python scripts/run_directory_organizer.py \
  --target-dir ~/Downloads/messy-folder \
  --model openai/gpt-oss-20b \
  --base-url http://127.0.0.1:1234/v1 \
  --mode plan
```

### 2. 実際に move する

```bash
python scripts/run_directory_organizer.py \
  --target-dir ~/Downloads/messy-folder \
  --model openai/gpt-oss-20b \
  --base-url http://127.0.0.1:1234/v1 \
  --mode apply
```

### 3. LLM なしで挙動確認

```bash
python scripts/run_directory_organizer.py \
  --target-dir ./sample-dir \
  --mode plan \
  --mock
```

`uv` を使う場合は `python ...` を `uv run python ...` に置き換えて実行できます。

`--target-dir` を毎回省きたい場合は、`.env` に `DIRECTORY_ORGANIZER_TARGET_DIR` を設定できます。成果物の既定保存先を変えたい場合は `DIRECTORY_ORGANIZER_OUTPUT_DIR` も使えます。

## 主要オプション

| オプション | 説明 |
|---|---|
| `--target-dir` | 整理対象ディレクトリ |
| `--mode` | `plan` または `apply` |
| `--model` | 使用モデル名。`--mock` 以外では必須 |
| `--base-url` | OpenAI 互換 API ベース URL |
| `--api-key` | API キー。LM Studio では未使用でも可 |
| `--rules` | 任意のルール JSON |
| `--output-dir` | 実行成果物の保存先。既定は `<target>/.dirorganizer-runs` |
| `--max-files` | 一度に扱う最大ファイル数 |
| `--max-depth` | スキャン深さ |
| `--batch-size` | LLM へ渡す 1 バッチあたりの件数 |
| `--min-confidence` | 適用対象にする最小信頼度 |
| `--mock` | ヒューリスティックで代替 |
| `--include-hidden` | 隠しファイル・ディレクトリも対象にする |

## ルール JSON

`--rules` には次の形式を渡せます。

```json
{
  "goal": "研究メモと請求書を分けたい",
  "taxonomy": [
    {
      "path": "documents/research",
      "description": "調査メモ、pdf、技術資料"
    },
    {
      "path": "documents/finance",
      "description": "請求書、領収書、見積書"
    }
  ],
  "rename_style": "元のファイル名を維持し、必要時のみ日付を先頭に付ける",
  "additional_instructions": [
    "既存ディレクトリを優先する",
    "同名衝突を起こす rename は避ける"
  ]
}
```

## 成果物

各実行で `<output-dir>/<run_id>/` を作り、以下を保存します。

- `plan.json`
- `plan.md`
- `manifest.json`

CLI の最後には機械可読な結果行を 1 行出力します。

```text
[RESULT] mode=plan status=success run_dir=/.../20260313T160000Z planned_moves=8 applied_moves=0 skipped=3
```

## 安全設計

- 既定は `plan`
- `apply` でも `move` だけ
- すべて target root 内の相対パスに強制
- 提案の妥当性はローカルで再検証
- 衝突や低信頼度は自動スキップ

## テスト

```bash
python -m unittest discover -s tests -p 'test_*.py'
```

`uv` を使う場合:

```bash
uv run python -m unittest discover -s tests -p 'test_*.py'
```
