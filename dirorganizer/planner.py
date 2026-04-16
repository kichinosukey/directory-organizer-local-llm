from __future__ import annotations

import hashlib
import json
import shutil
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable

from dirorganizer.llm_client import LocalLLMClient
from dirorganizer.models import FileRecord, PlanOperation, PlanResult

HEURISTIC_DIRECTORY_MAP = {
    ".png": "media/images",
    ".jpg": "media/images",
    ".jpeg": "media/images",
    ".gif": "media/images",
    ".webp": "media/images",
    ".svg": "media/images",
    ".mp3": "media/audio",
    ".wav": "media/audio",
    ".m4a": "media/audio",
    ".aac": "media/audio",
    ".mp4": "media/video",
    ".mov": "media/video",
    ".mkv": "media/video",
    ".pdf": "documents/notes",
    ".md": "documents/notes",
    ".txt": "documents/notes",
    ".docx": "documents/notes",
    ".csv": "documents/spreadsheets",
    ".tsv": "documents/spreadsheets",
    ".xlsx": "documents/spreadsheets",
    ".numbers": "documents/spreadsheets",
    ".py": "projects/code",
    ".js": "projects/code",
    ".ts": "projects/code",
    ".tsx": "projects/code",
    ".jsx": "projects/code",
    ".json": "projects/code",
    ".yaml": "projects/code",
    ".yml": "projects/code",
    ".toml": "projects/code",
    ".sh": "projects/code",
    ".zip": "archives",
    ".tar": "archives",
    ".gz": "archives",
    ".7z": "archives",
    ".dmg": "installers",
    ".pkg": "installers",
}

FINANCE_KEYWORDS = ("invoice", "receipt", "estimate", "quote", "請求", "領収", "見積")
CONTRACT_KEYWORDS = ("contract", "agreement", "nda", "契約")
ProgressCallback = Callable[[str], None]


@dataclass
class ApplyResult:
    applied_moves: int
    skipped: int
    applied_operations: list[PlanOperation] = field(default_factory=list)
    skipped_operations: list[dict[str, str]] = field(default_factory=list)


def build_plan(
    *,
    root: Path,
    files: list[FileRecord],
    existing_dirs: list[str],
    rules: dict[str, object],
    client: LocalLLMClient | None,
    batch_size: int,
    min_confidence: float,
    mock: bool,
    scan_truncated: bool,
    heuristic_directory_map: dict[str, str] | None = None,
    progress_callback: ProgressCallback | None = None,
) -> PlanResult:
    warnings: list[str] = []
    if scan_truncated:
        warnings.append("scan truncated because max_files limit was reached")

    raw_operations: list[dict[str, object]] = []
    batch_summaries: list[str] = []
    if mock:
        raw_operations = _build_mock_operations(files, heuristic_directory_map=heuristic_directory_map)
        batch_summaries.append("heuristic planner grouped files by extension and finance keywords")
    else:
        if client is None:
            raise ValueError("client is required when mock is false")
        total_started_at = time.perf_counter()
        total_batches = max(1, (len(files) + batch_size - 1) // batch_size)
        if progress_callback is not None:
            progress_callback(
                f"planning {len(files)} files in {total_batches} batches (batch_size={batch_size})"
            )
        for batch_index, index in enumerate(range(0, len(files), batch_size), start=1):
            batch = files[index : index + batch_size]
            batch_label = (
                f"batch {batch_index}/{total_batches} "
                f"(files {index + 1}-{index + len(batch)} of {len(files)})"
            )
            if progress_callback is not None:
                progress_callback(batch_label)
            batch_started_at = time.perf_counter()
            payload, batch_warnings = _request_batch_with_fallback(
                client=client,
                root=root,
                files=batch,
                existing_dirs=existing_dirs,
                rules=rules,
                batch_size=batch_size,
                progress_callback=progress_callback,
                batch_label=batch_label,
            )
            batch_finished_at = time.perf_counter()
            warnings.extend(batch_warnings)
            operations = payload.get("operations", [])
            if not isinstance(operations, list):
                raise RuntimeError("LLM payload did not contain operations list")
            raw_operations.extend(operations)
            summary = payload.get("summary")
            if isinstance(summary, str) and summary.strip():
                batch_summaries.append(summary.strip())
            if progress_callback is not None:
                processed_files = min(index + len(batch), len(files))
                progress_callback(
                    f"completed {batch_label} in {batch_finished_at - batch_started_at:.1f}s "
                    f"(processed {processed_files}/{len(files)} files, "
                    f"elapsed {batch_finished_at - total_started_at:.1f}s)"
                )

    operations, validation_warnings = _validate_operations(
        root=root,
        files=files,
        raw_operations=raw_operations,
        min_confidence=min_confidence,
    )
    warnings.extend(validation_warnings)
    summary = " / ".join(batch_summaries) if batch_summaries else "no summary"
    return PlanResult(summary=summary, operations=operations, warnings=warnings)


def apply_plan(root: Path, plan: PlanResult) -> ApplyResult:
    applied_moves = 0
    skipped = 0
    applied_operations: list[PlanOperation] = []
    skipped_operations: list[dict[str, str]] = []

    for operation in plan.operations:
        source_path = root / operation.source
        target_path = root / operation.target_path
        apply_issue = _validate_apply_operation(root=root, operation=operation, source_path=source_path, target_path=target_path)
        if apply_issue is not None:
            skipped += 1
            skipped_operations.append(
                {
                    "source": operation.source,
                    "target_path": operation.target_path,
                    "reason": apply_issue,
                }
            )
            continue
        if operation.action == "dedup":
            source_path.unlink()
        else:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source_path), str(target_path))
        applied_moves += 1
        applied_operations.append(operation)

    return ApplyResult(
        applied_moves=applied_moves,
        skipped=skipped,
        applied_operations=applied_operations,
        skipped_operations=skipped_operations,
    )


def render_plan_markdown(plan: PlanResult) -> str:
    lines = [
        "# Directory Organizer Plan",
        "",
        f"Summary: {plan.summary}",
        "",
        "## Warnings",
    ]
    if plan.warnings:
        lines.extend([f"- {warning}" for warning in plan.warnings])
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Operations",
            "",
            "| source | target | action | confidence | apply | reason |",
            "|---|---|---|---:|---|---|",
        ]
    )
    for operation in plan.operations:
        reason = operation.reason.replace("|", "/")
        apply_flag = "yes" if operation.can_apply else "no"
        lines.append(
            f"| `{operation.source}` | `{operation.target_path}` | {operation.action} "
            f"| {operation.confidence:.2f} | {apply_flag} | {reason} |"
        )
        if operation.issues:
            issue_text = "; ".join(operation.issues).replace("|", "/")
            lines.append(f"|  |  |  |  |  | issue: {issue_text} |")
    lines.append("")
    return "\n".join(lines)


def _request_batch(
    *,
    client: LocalLLMClient,
    root: Path,
    files: list[FileRecord],
    existing_dirs: list[str],
    rules: dict[str, object],
) -> dict[str, object]:
    system_prompt = """
You are a careful filesystem organizer.
Return JSON only.
You must never suggest deleting files.
You must keep every destination_dir relative to the provided target root.
You must keep file extensions unchanged.
Prefer existing directories when they match the taxonomy.
Output shape:
{
  "summary": "short summary",
  "operations": [
    {
      "source": "relative/path.ext",
      "destination_dir": "documents/notes",
      "new_name": "path.ext",
      "reason": "short reason",
      "confidence": 0.84
    }
  ]
}
""".strip()
    user_prompt = json.dumps(
        {
            "target_root": str(root),
            "rules": rules,
            "existing_dirs": existing_dirs,
            "files": [record.prompt_dict() for record in files],
        },
        ensure_ascii=False,
        indent=2,
    )
    return client.chat_json(system_prompt, user_prompt)


def _request_batch_with_fallback(
    *,
    client: LocalLLMClient,
    root: Path,
    files: list[FileRecord],
    existing_dirs: list[str],
    rules: dict[str, object],
    batch_size: int,
    progress_callback: ProgressCallback | None = None,
    batch_label: str | None = None,
) -> tuple[dict[str, object], list[str]]:
    try:
        return (
            _request_batch(
                client=client,
                root=root,
                files=files,
                existing_dirs=existing_dirs,
                rules=rules,
            ),
            [],
        )
    except Exception as exc:  # noqa: BLE001
        if progress_callback is not None:
            context = batch_label or "batch"
            progress_callback(
                f"{context} request failed for {len(files)} files: {_summarize_progress_error(exc)}"
            )
        if len(files) == 1:
            record = files[0]
            if progress_callback is not None:
                progress_callback(f"{batch_label or 'batch'} heuristic fallback for {record.relative_path}")
            return (
                {
                    "summary": f"heuristic fallback for {record.relative_path}",
                    "operations": _build_mock_operations(files),
                },
                [f"LLM failed for {record.relative_path}; used heuristic fallback: {exc}"],
            )

        midpoint = max(1, min(len(files) - 1, batch_size // 2, len(files) // 2))
        if progress_callback is not None:
            progress_callback(
                f"{batch_label or 'batch'} split into {midpoint} and {len(files) - midpoint} files"
            )
        left_payload, left_warnings = _request_batch_with_fallback(
            client=client,
            root=root,
            files=files[:midpoint],
            existing_dirs=existing_dirs,
            rules=rules,
            batch_size=max(1, midpoint),
            progress_callback=progress_callback,
            batch_label=f"{batch_label or 'batch'} L",
        )
        right_payload, right_warnings = _request_batch_with_fallback(
            client=client,
            root=root,
            files=files[midpoint:],
            existing_dirs=existing_dirs,
            rules=rules,
            batch_size=max(1, len(files) - midpoint),
            progress_callback=progress_callback,
            batch_label=f"{batch_label or 'batch'} R",
        )
        merged_operations = []
        left_operations = left_payload.get("operations", [])
        if isinstance(left_operations, list):
            merged_operations.extend(left_operations)
        right_operations = right_payload.get("operations", [])
        if isinstance(right_operations, list):
            merged_operations.extend(right_operations)
        summaries = []
        for payload in (left_payload, right_payload):
            summary = payload.get("summary")
            if isinstance(summary, str) and summary.strip():
                summaries.append(summary.strip())
        warnings = [f"LLM batch retry split {len(files)} files after error: {exc}"]
        warnings.extend(left_warnings)
        warnings.extend(right_warnings)
        return (
            {
                "summary": " / ".join(summaries) if summaries else f"split fallback for {len(files)} files",
                "operations": merged_operations,
            },
            warnings,
        )


def _summarize_progress_error(exc: Exception, *, max_length: int = 160) -> str:
    message = str(exc).strip().replace("\n", " ")
    if len(message) <= max_length:
        return message
    return f"{message[: max_length - 3]}..."


def build_skipped_operation(record: FileRecord, reason: str, *, issue: str | None = None) -> PlanOperation:
    cleaned_parent = record.parent_dir if record.parent_dir != "." else ""
    issues = [issue or reason]
    return PlanOperation(
        source=record.relative_path,
        destination_dir=cleaned_parent,
        new_name=record.name,
        target_path=record.relative_path,
        action="noop",
        confidence=0.0,
        reason=reason,
        can_apply=False,
        issues=issues,
    )


def _build_mock_operations(
    files: list[FileRecord], *, heuristic_directory_map: dict[str, str] | None = None
) -> list[dict[str, object]]:
    operations = []
    for record in files:
        destination_dir = _heuristic_destination(record, heuristic_directory_map=heuristic_directory_map)
        operations.append(
            {
                "source": record.relative_path,
                "destination_dir": destination_dir,
                "new_name": record.name,
                "reason": "heuristic classification based on extension and filename",
                "confidence": 0.85,
            }
        )
    return operations


def _heuristic_destination(record: FileRecord, *, heuristic_directory_map: dict[str, str] | None = None) -> str:
    lowered_name = record.name.lower()
    if any(keyword in lowered_name for keyword in FINANCE_KEYWORDS):
        return "documents/finance"
    if any(keyword in lowered_name for keyword in CONTRACT_KEYWORDS):
        return "documents/finance"
    directory_map = heuristic_directory_map or HEURISTIC_DIRECTORY_MAP
    return directory_map.get(record.extension, "misc")


def _validate_operations(
    *,
    root: Path,
    files: list[FileRecord],
    raw_operations: list[dict[str, object]],
    min_confidence: float,
) -> tuple[list[PlanOperation], list[str]]:
    files_by_source = {record.relative_path: record for record in files}
    nfc_to_source = {unicodedata.normalize("NFC", k): k for k in files_by_source}
    operations_by_source: dict[str, dict[str, object]] = {}
    warnings: list[str] = []

    for item in raw_operations:
        source = item.get("source")
        if isinstance(source, str):
            matched = files_by_source.get(source) or files_by_source.get(
                nfc_to_source.get(unicodedata.normalize("NFC", source), "")
            )
            if matched is not None:
                operations_by_source[matched.relative_path] = item

    for source in files_by_source:
        if source not in operations_by_source:
            warnings.append(f"missing LLM operation for {source}; keeping file in place")

    operations: list[PlanOperation] = []
    target_to_source: dict[str, str] = {}
    for source, record in files_by_source.items():
        raw = operations_by_source.get(source)
        if raw is None:
            operation = _make_operation(
                record=record,
                destination_dir=record.parent_dir if record.parent_dir != "." else "",
                new_name=record.name,
                reason="no LLM proposal received",
                confidence=0.0,
                min_confidence=min_confidence,
            )
        else:
            operation = _make_operation(
                record=record,
                destination_dir=str(raw.get("destination_dir", "")),
                new_name=str(raw.get("new_name", record.name)),
                reason=str(raw.get("reason", "no reason provided")),
                confidence=_coerce_confidence(raw.get("confidence")),
                min_confidence=min_confidence,
            )

        existing_source = target_to_source.get(operation.target_path)
        if existing_source and existing_source != operation.source:
            operation.can_apply = False
            operation.issues.append(f"target collision with {existing_source}")
        else:
            target_to_source[operation.target_path] = operation.source

        absolute_target = root / operation.target_path
        absolute_source = root / operation.source
        if absolute_target.exists() and absolute_target != absolute_source and operation.action == "move":
            if _files_are_identical(absolute_source, absolute_target):
                operation.action = "dedup"
                operation.reason = f"identical to existing {operation.target_path}; remove source"
            else:
                unique = _find_unique_target(root, operation.target_path)
                operation.target_path = unique
                operation.new_name = PurePosixPath(unique).name

        operations.append(operation)

    return operations, warnings


def _make_operation(
    *,
    record: FileRecord,
    destination_dir: str,
    new_name: str,
    reason: str,
    confidence: float,
    min_confidence: float,
) -> PlanOperation:
    issues: list[str] = []
    cleaned_destination = _sanitize_relative_directory(destination_dir)
    if cleaned_destination is None:
        cleaned_destination = record.parent_dir if record.parent_dir != "." else ""
        issues.append("invalid destination_dir; kept original directory")

    cleaned_name = _sanitize_filename(new_name)
    if cleaned_name is None:
        cleaned_name = record.name
        issues.append("invalid new_name; kept original filename")

    if record.extension:
        candidate_suffix = Path(cleaned_name).suffix.lower()
        if candidate_suffix != record.extension.lower():
            cleaned_name = f"{Path(cleaned_name).stem}{record.extension}"
            issues.append("extension change was blocked")

    target_path = str(PurePosixPath(cleaned_destination) / cleaned_name) if cleaned_destination else cleaned_name
    action = "noop" if target_path == record.relative_path else "move"
    can_apply = action == "move" and confidence >= min_confidence and not issues

    if confidence < min_confidence and action == "move":
        issues.append(f"confidence below threshold {min_confidence:.2f}")
        can_apply = False

    if action == "noop":
        can_apply = False

    return PlanOperation(
        source=record.relative_path,
        destination_dir=cleaned_destination,
        new_name=cleaned_name,
        target_path=target_path,
        action=action,
        confidence=confidence,
        reason=reason,
        can_apply=can_apply,
        issues=issues,
    )


def _validate_apply_operation(
    *,
    root: Path,
    operation: PlanOperation,
    source_path: Path,
    target_path: Path,
) -> str | None:
    if operation.action == "dedup":
        if not operation.can_apply:
            return "operation is marked as not applicable"
        if not source_path.exists():
            return "source file no longer exists"
        if not target_path.exists():
            return "dedup target no longer exists"
        if not _files_are_identical(source_path, target_path):
            return "files are no longer identical; skipping dedup"
        return None

    if operation.action != "move":
        return "operation is not a move"
    if not operation.can_apply:
        return "operation is marked as not applicable"
    if not source_path.exists():
        return "source file no longer exists"

    cleaned_destination = _sanitize_relative_directory(operation.destination_dir)
    if cleaned_destination is None:
        return "destination_dir is no longer safe"

    cleaned_name = _sanitize_filename(operation.new_name)
    if cleaned_name is None:
        return "new_name is no longer safe"

    expected_target = str(PurePosixPath(cleaned_destination) / cleaned_name) if cleaned_destination else cleaned_name
    if expected_target != operation.target_path:
        return "target_path does not match destination_dir/new_name"

    if not target_path.is_relative_to(root):
        return "target path escapes target directory"
    if target_path.exists() and target_path != source_path:
        return "target already exists on disk"

    return None


def _sanitize_relative_directory(value: str) -> str | None:
    candidate = value.strip().strip("/")
    if candidate in {"", "."}:
        return ""
    path = PurePosixPath(candidate)
    if path.is_absolute():
        return None
    if any(part in {"..", "."} for part in path.parts):
        return None
    return path.as_posix()


def _sanitize_filename(value: str) -> str | None:
    candidate = value.strip()
    if not candidate:
        return None
    path = PurePosixPath(candidate)
    if path.is_absolute():
        return None
    if len(path.parts) != 1:
        return None
    if path.name in {".", ".."}:
        return None
    return path.name


def _coerce_confidence(value: object) -> float:
    if isinstance(value, (float, int)):
        return max(0.0, min(1.0, float(value)))
    return 0.0


def _file_hash(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _files_are_identical(a: Path, b: Path) -> bool:
    try:
        if a.stat().st_size != b.stat().st_size:
            return False
        return _file_hash(a) == _file_hash(b)
    except OSError:
        return False


def _find_unique_target(root: Path, target_path: str) -> str:
    p = PurePosixPath(target_path)
    stem = p.stem
    suffix = p.suffix
    parent = p.parent.as_posix()
    for i in range(1, 100):
        candidate_name = f"{stem}_{i}{suffix}"
        candidate = str(PurePosixPath(parent) / candidate_name) if parent != "." else candidate_name
        if not (root / candidate).exists():
            return candidate
    return target_path
