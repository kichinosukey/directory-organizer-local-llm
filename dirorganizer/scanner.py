from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from dirorganizer.models import FileRecord

DEFAULT_IGNORES = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "__pycache__",
    "node_modules",
    ".dirorganizer-runs",
}


def _is_hidden(name: str) -> bool:
    return name.startswith(".")


@dataclass(frozen=True)
class FastLaneFilterResult:
    selected_files: list[FileRecord]
    skipped: list[tuple[FileRecord, str]]


def scan_directory(
    root: Path,
    *,
    max_files: int | None,
    max_depth: int | None,
    include_hidden: bool,
    extra_ignores: set[str] | None = None,
) -> tuple[list[FileRecord], list[str], bool]:
    root = root.resolve()
    ignore_names = set(DEFAULT_IGNORES)
    if extra_ignores:
        ignore_names.update(extra_ignores)

    files: list[FileRecord] = []
    existing_dirs: list[str] = []
    truncated = False

    for current_dir, dirnames, filenames in os.walk(root):
        current_path = Path(current_dir)
        relative_dir = current_path.relative_to(root)
        depth = len(relative_dir.parts)

        kept_dirs: list[str] = []
        for dirname in sorted(dirnames):
            if dirname in ignore_names:
                continue
            if not include_hidden and _is_hidden(dirname):
                continue
            kept_dirs.append(dirname)
            existing_dirs.append((relative_dir / dirname).as_posix())
        dirnames[:] = kept_dirs

        if max_depth is not None and depth >= max_depth:
            dirnames[:] = []

        for filename in sorted(filenames):
            if filename in ignore_names:
                continue
            if not include_hidden and _is_hidden(filename):
                continue
            absolute_path = current_path / filename
            relative_path = absolute_path.relative_to(root).as_posix()
            stat = absolute_path.stat()
            files.append(
                FileRecord(
                    relative_path=relative_path,
                    parent_dir=absolute_path.parent.relative_to(root).as_posix(),
                    name=filename,
                    extension=absolute_path.suffix.lower(),
                    size_bytes=stat.st_size,
                    modified_at=datetime.fromtimestamp(
                        stat.st_mtime,
                        tz=timezone.utc,
                    ).isoformat(),
                )
            )
            if max_files is not None and len(files) >= max_files:
                truncated = True
                return files, _normalize_dirs(existing_dirs), truncated

    return files, _normalize_dirs(existing_dirs), truncated


def filter_fast_lane_files(
    files: list[FileRecord],
    *,
    allowed_extensions: frozenset[str],
    max_files: int,
    max_size_bytes: int,
) -> FastLaneFilterResult:
    selected: list[FileRecord] = []
    skipped: list[tuple[FileRecord, str]] = []

    for record in files:
        if record.extension.lower() not in allowed_extensions:
            skipped.append((record, f"fast lane skipped unsupported extension: {record.extension or '<none>'}"))
            continue
        if record.size_bytes > max_size_bytes:
            skipped.append((record, f"fast lane skipped large file > {max_size_bytes} bytes"))
            continue
        selected.append(record)

    selected.sort(key=lambda item: item.modified_at, reverse=True)
    if len(selected) > max_files:
        overflow = selected[max_files:]
        selected = selected[:max_files]
        for record in overflow:
            skipped.append((record, f"fast lane skipped older file outside newest {max_files} candidates"))

    return FastLaneFilterResult(selected_files=selected, skipped=skipped)


def _normalize_dirs(paths: list[str]) -> list[str]:
    normalized = []
    for path in paths:
        cleaned = path.strip().strip("/")
        if not cleaned:
            continue
        normalized.append(cleaned)
    return sorted(set(normalized))
