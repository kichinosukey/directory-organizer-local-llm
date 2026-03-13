from __future__ import annotations

import json
from pathlib import Path

DEFAULT_RULES = {
    "goal": "散らかったファイルを少数の安定したフォルダに整理する。",
    "taxonomy": [
        {"path": "documents/notes", "description": "Markdown, text, PDF のメモや資料"},
        {"path": "documents/finance", "description": "請求書、領収書、見積書、契約関連"},
        {"path": "documents/spreadsheets", "description": "CSV, XLSX, Numbers, TSV"},
        {"path": "media/images", "description": "画像, スクリーンショット, 写真"},
        {"path": "media/audio", "description": "音声ファイル"},
        {"path": "media/video", "description": "動画ファイル"},
        {"path": "projects/code", "description": "スクリプト、設定ファイル、ソースコード"},
        {"path": "archives", "description": "zip, tar.gz, 7z などの圧縮ファイル"},
        {"path": "installers", "description": "dmg, pkg, app のインストーラ類"},
        {"path": "misc", "description": "上記に当てはまらないファイル"},
    ],
    "rename_style": "元のファイル名を維持し、曖昧さが大きい場合のみ最小限の rename を行う。",
    "additional_instructions": [
        "削除提案はしない",
        "既存ディレクトリが適切なら優先する",
        "相対パスのみを使う",
        "ファイル拡張子は変えない",
    ],
}


def load_rules(path: str | None) -> dict[str, object]:
    if path is None:
        return json.loads(json.dumps(DEFAULT_RULES))

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    merged = json.loads(json.dumps(DEFAULT_RULES))
    for key, value in payload.items():
        merged[key] = value
    return merged
