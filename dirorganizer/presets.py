from __future__ import annotations

import copy
from dataclasses import dataclass


@dataclass(frozen=True)
class PresetConfig:
    name: str
    rules: dict[str, object]
    destination_mapping: dict[str, str]
    allowed_extensions: frozenset[str]
    min_confidence: float
    batch_size: int
    max_files: int
    max_depth: int
    max_size_bytes: int
    skip_unknown_extensions: bool = True

    def copy_rules(self) -> dict[str, object]:
        return copy.deepcopy(self.rules)


DEFAULT_PRESET_NAME = "downloads-default"
FAST_LANE_ALLOWED_EXTENSIONS = frozenset(
    {
        ".pdf",
        ".docx",
        ".txt",
        ".md",
        ".xlsx",
        ".csv",
        ".jpg",
        ".jpeg",
        ".png",
    }
)
FAST_LANE_MAX_SIZE_BYTES = 100 * 1024 * 1024


PRESETS: dict[str, PresetConfig] = {
    "downloads-default": PresetConfig(
        name="downloads-default",
        rules={
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
        },
        destination_mapping={
            ".pdf": "documents/notes",
            ".docx": "documents/notes",
            ".txt": "documents/notes",
            ".md": "documents/notes",
            ".xlsx": "documents/spreadsheets",
            ".csv": "documents/spreadsheets",
            ".jpg": "media/images",
            ".jpeg": "media/images",
            ".png": "media/images",
            ".zip": "archives",
        },
        allowed_extensions=FAST_LANE_ALLOWED_EXTENSIONS,
        min_confidence=0.80,
        batch_size=15,
        max_files=30,
        max_depth=1,
        max_size_bytes=FAST_LANE_MAX_SIZE_BYTES,
    ),
    "finance-receipts": PresetConfig(
        name="finance-receipts",
        rules={
            "goal": "請求書、領収書、見積書、契約書を見つけやすい場所へ整理する。",
            "taxonomy": [
                {"path": "documents/finance/receipts", "description": "領収書や支払証憑"},
                {"path": "documents/finance/invoices", "description": "請求書や見積書"},
                {"path": "documents/finance/contracts", "description": "契約書や合意書"},
                {"path": "documents/notes", "description": "上記以外の文書"},
                {"path": "media/images", "description": "撮影した証憑や画像"},
                {"path": "misc", "description": "未分類ファイル"},
            ],
            "rename_style": "元のファイル名を維持し、日付がある場合のみ先頭へ寄せる。",
            "additional_instructions": [
                "財務関連の文書を優先的に分類する",
                "削除提案はしない",
                "相対パスのみを使う",
                "ファイル拡張子は変えない",
            ],
        },
        destination_mapping={
            ".pdf": "documents/finance/invoices",
            ".docx": "documents/finance/contracts",
            ".txt": "documents/notes",
            ".md": "documents/notes",
            ".xlsx": "documents/finance/invoices",
            ".csv": "documents/finance/invoices",
            ".jpg": "media/images",
            ".jpeg": "media/images",
            ".png": "media/images",
        },
        allowed_extensions=FAST_LANE_ALLOWED_EXTENSIONS,
        min_confidence=0.82,
        batch_size=15,
        max_files=30,
        max_depth=1,
        max_size_bytes=FAST_LANE_MAX_SIZE_BYTES,
    ),
    "research-papers": PresetConfig(
        name="research-papers",
        rules={
            "goal": "論文、調査メモ、補助資料を研究用フォルダへ整理する。",
            "taxonomy": [
                {"path": "documents/research/papers", "description": "論文本文や PDF 資料"},
                {"path": "documents/research/notes", "description": "メモ、Markdown、テキスト"},
                {"path": "documents/research/data", "description": "CSV や表計算の補助データ"},
                {"path": "media/images", "description": "図表やスクリーンショット"},
                {"path": "misc", "description": "未分類ファイル"},
            ],
            "rename_style": "原名維持を優先し、必要なら最小限の整形のみ行う。",
            "additional_instructions": [
                "研究資料は papers / notes / data に寄せる",
                "削除提案はしない",
                "相対パスのみを使う",
                "ファイル拡張子は変えない",
            ],
        },
        destination_mapping={
            ".pdf": "documents/research/papers",
            ".docx": "documents/research/papers",
            ".txt": "documents/research/notes",
            ".md": "documents/research/notes",
            ".xlsx": "documents/research/data",
            ".csv": "documents/research/data",
            ".jpg": "media/images",
            ".jpeg": "media/images",
            ".png": "media/images",
        },
        allowed_extensions=FAST_LANE_ALLOWED_EXTENSIONS,
        min_confidence=0.80,
        batch_size=15,
        max_files=30,
        max_depth=1,
        max_size_bytes=FAST_LANE_MAX_SIZE_BYTES,
    ),
}


def get_preset(name: str) -> PresetConfig:
    try:
        return PRESETS[name]
    except KeyError as exc:
        supported = ", ".join(sorted(PRESETS))
        raise ValueError(f"unknown preset: {name}. Supported presets: {supported}") from exc


def list_presets() -> list[str]:
    return sorted(PRESETS)
