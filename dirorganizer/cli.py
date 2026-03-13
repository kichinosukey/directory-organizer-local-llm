from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from dirorganizer.env_loader import load_dotenv
from dirorganizer.llm_client import LocalLLMClient
from dirorganizer.models import PlanOperation, PlanResult
from dirorganizer.planner import apply_plan, build_plan, build_skipped_operation, render_plan_markdown
from dirorganizer.presets import DEFAULT_PRESET_NAME, PresetConfig, get_preset, list_presets
from dirorganizer.rules import load_rules
from dirorganizer.scanner import filter_fast_lane_files, scan_directory


REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(REPO_ROOT / ".env")

MANIFEST_VERSION = 2
DEFAULT_BATCH_SIZE = 20
DEFAULT_MIN_CONFIDENCE = 0.60
LEGACY_COMMANDS = {"plan", "apply", "run"}
BLOCKED_ISSUE_MARKERS = (
    "invalid",
    "collision",
    "already exists",
    "unsafe",
    "extension change",
    "escapes",
)


@dataclass(frozen=True)
class ExecutionSettings:
    fast_lane: bool
    scan_max_files: int | None
    scan_max_depth: int | None
    candidate_max_files: int | None
    batch_size: int
    min_confidence: float
    allowed_extensions: frozenset[str] | None
    max_size_bytes: int | None
    heuristic_directory_map: dict[str, str]


def main(
    argv: list[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    normalized_argv = _normalize_legacy_argv(list(sys.argv[1:] if argv is None else argv))
    args = parse_args(normalized_argv)

    if args.command == "apply":
        return _run_apply_command(args, stdout=stdout, stderr=stderr)
    if args.command == "plan":
        return _run_plan_command(args, stdout=stdout, stderr=stderr)
    return _run_run_command(args, stdin=stdin, stdout=stdout, stderr=stderr)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plan, run, or apply safe directory organization.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_parser = subparsers.add_parser("plan", help="Build a plan and save artifacts")
    _add_source_arguments(plan_parser, include_yes=False)

    run_parser = subparsers.add_parser("run", help="Build a plan and optionally apply it")
    _add_source_arguments(run_parser, include_yes=True)

    apply_parser = subparsers.add_parser("apply", help="Apply a saved manifest without re-planning")
    apply_parser.add_argument("--manifest", required=True, help="Path to a manifest.json file")

    args = parser.parse_args(argv)
    if args.command in {"plan", "run"}:
        if not (args.target_dir or "").strip():
            parser.error("--target-dir/--source is required unless DIRECTORY_ORGANIZER_TARGET_DIR is set")
        if not args.mock and not (args.model or "").strip():
            parser.error("--model is required unless --mock is used")
    return args


def _add_source_arguments(parser: argparse.ArgumentParser, *, include_yes: bool) -> None:
    parser.add_argument(
        "--target-dir",
        "--source",
        dest="target_dir",
        default=os.getenv("DIRECTORY_ORGANIZER_TARGET_DIR"),
        help="Directory to organize",
    )
    parser.add_argument(
        "--model",
        default=os.getenv("LOCAL_LLM_MODEL"),
        help="OpenAI-compatible local model name",
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("LOCAL_LLM_BASE_URL", "http://127.0.0.1:1234/v1"),
        help="OpenAI-compatible base URL",
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("LOCAL_LLM_API_KEY", "not-needed"),
        help="API key if your local endpoint requires one",
    )
    parser.add_argument("--rules", help="Optional JSON file that customizes taxonomy and instructions")
    parser.add_argument("--output-dir", help="Artifact directory root")
    parser.add_argument("--max-files", type=int, default=None)
    parser.add_argument("--max-depth", type=int, default=None)
    parser.add_argument("--batch-size", type=int, default=None)
    parser.add_argument("--min-confidence", type=float, default=None)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--max-output-tokens", type=int, default=1200)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--include-hidden", action="store_true")
    parser.add_argument("--mock", action="store_true", help="Use heuristic planner instead of LLM")
    parser.add_argument("--fast-lane", action="store_true", help="Use the fast-lane preset constraints")
    parser.add_argument(
        "--preset",
        default=DEFAULT_PRESET_NAME,
        choices=list_presets(),
        help="Preset that defines taxonomy and fast-lane defaults",
    )
    if include_yes:
        parser.add_argument("--yes", action="store_true", help="Skip interactive confirmation and apply immediately")


def _normalize_legacy_argv(argv: list[str]) -> list[str]:
    if not argv:
        return ["plan"]
    if argv[0] in LEGACY_COMMANDS:
        return argv
    if any(flag in {"-h", "--help"} for flag in argv):
        return argv

    mode = "plan"
    normalized: list[str] = []
    skip_next = False
    for index, token in enumerate(argv):
        if skip_next:
            skip_next = False
            continue
        if token == "--mode" and index + 1 < len(argv):
            mode = argv[index + 1]
            skip_next = True
            continue
        if token.startswith("--mode="):
            mode = token.split("=", 1)[1]
            continue
        normalized.append(token)

    command = "run" if mode == "apply" else "plan"
    normalized.insert(0, command)
    if command == "run" and "--yes" not in normalized:
        normalized.append("--yes")
    return normalized


def _run_plan_command(args: argparse.Namespace, *, stdout: TextIO, stderr: TextIO) -> int:
    plan_result = _build_plan_artifacts(args, command_name="plan", stderr=stderr)
    if plan_result is None:
        return 1

    plan, run_dir, manifest = plan_result
    if args.fast_lane:
        _write_fast_lane_summary(plan=plan, manifest=manifest, stdout=stdout)
    _write_result_line(
        stdout,
        mode="plan",
        status="success",
        run_dir=run_dir,
        manifest=manifest,
        applied_moves=0,
    )
    return 0


def _run_run_command(args: argparse.Namespace, *, stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    command_started_at = time.perf_counter()
    plan_result = _build_plan_artifacts(args, command_name="run", stderr=stderr)
    if plan_result is None:
        return 1

    plan, run_dir, manifest = plan_result
    if args.fast_lane:
        _write_fast_lane_summary(plan=plan, manifest=manifest, stdout=stdout)

    should_apply = args.yes
    if not should_apply:
        if _is_tty(stdin) and _is_tty(stdout):
            should_apply = _prompt_for_apply(run_dir=run_dir, stdin=stdin, stdout=stdout)
        else:
            stdout.write(f"Plan ready; rerun with `apply --manifest {run_dir / 'manifest.json'}` or `run --yes`.\n")
            _write_result_line(
                stdout,
                mode="run",
                status="pending_apply",
                run_dir=run_dir,
                manifest=manifest,
                applied_moves=0,
            )
            return 0

    if not should_apply:
        _write_result_line(
            stdout,
            mode="run",
            status="cancelled",
            run_dir=run_dir,
            manifest=manifest,
            applied_moves=0,
        )
        return 0

    apply_result = _apply_manifest(
        manifest_path=run_dir / "manifest.json",
        command_started_at=command_started_at,
    )
    _write_result_line(
        stdout,
        mode="run",
        status="success",
        run_dir=run_dir,
        manifest=apply_result["manifest"],
        applied_moves=apply_result["applied_moves"],
        skipped_total=apply_result["skipped"],
    )
    return 0


def _run_apply_command(args: argparse.Namespace, *, stdout: TextIO, stderr: TextIO) -> int:
    manifest_path = resolve_configured_path(args.manifest)
    if not manifest_path.is_file():
        stderr.write(f"[ERROR] manifest does not exist: {manifest_path}\n")
        return 1

    apply_result = _apply_manifest(manifest_path=manifest_path, command_started_at=time.perf_counter())
    _write_result_line(
        stdout,
        mode="apply",
        status="success",
        run_dir=manifest_path.parent,
        manifest=apply_result["manifest"],
        applied_moves=apply_result["applied_moves"],
        skipped_total=apply_result["skipped"],
    )
    return 0


def _build_plan_artifacts(
    args: argparse.Namespace,
    *,
    command_name: str,
    stderr: TextIO,
) -> tuple[PlanResult, Path, dict[str, object]] | None:
    target_dir = resolve_configured_path(args.target_dir)
    if not target_dir.is_dir():
        stderr.write(f"[ERROR] target directory does not exist: {target_dir}\n")
        return None

    preset = get_preset(args.preset)
    settings = _resolve_execution_settings(args, preset)
    output_root = resolve_output_root(args.output_dir, target_dir)
    extra_ignores = _build_extra_ignores(output_root=output_root, target_dir=target_dir)

    scan_started_at = time.perf_counter()
    files, existing_dirs, scan_truncated = scan_directory(
        target_dir,
        max_files=settings.scan_max_files,
        max_depth=settings.scan_max_depth,
        include_hidden=args.include_hidden,
        extra_ignores=extra_ignores,
    )
    scan_seconds = time.perf_counter() - scan_started_at

    if not files:
        stderr.write("[ERROR] no files found to organize\n")
        return None

    filtered_operations: list[PlanOperation] = []
    plan_warnings: list[str] = []
    selected_files = files
    if settings.fast_lane and settings.allowed_extensions is not None and settings.max_size_bytes is not None:
        filter_result = filter_fast_lane_files(
            files,
            allowed_extensions=settings.allowed_extensions,
            max_files=settings.candidate_max_files or preset.max_files,
            max_size_bytes=settings.max_size_bytes,
        )
        selected_files = filter_result.selected_files
        filtered_operations = [
            build_skipped_operation(record, reason, issue=reason) for record, reason in filter_result.skipped
        ]
        if filter_result.skipped:
            plan_warnings.append(f"fast lane skipped {len(filter_result.skipped)} files before planning")

    rules = load_rules(args.rules, preset_name=args.preset)
    build_started_at = time.perf_counter()
    if selected_files:
        plan = build_plan(
            root=target_dir,
            files=selected_files,
            existing_dirs=existing_dirs,
            rules=rules,
            client=build_client(args),
            batch_size=settings.batch_size,
            min_confidence=settings.min_confidence,
            mock=args.mock,
            scan_truncated=scan_truncated,
            heuristic_directory_map=settings.heuristic_directory_map,
        )
    else:
        empty_summary = "no eligible files selected for fast lane"
        plan = PlanResult(summary=empty_summary, operations=[], warnings=["no fast-lane eligible files found"])
    plan_seconds = time.perf_counter() - build_started_at

    combined_plan = PlanResult(
        summary=plan.summary,
        warnings=plan.warnings + plan_warnings,
        operations=sorted(plan.operations + filtered_operations, key=lambda operation: operation.source),
    )

    counts = _compute_counts(
        plan=combined_plan,
        existing_dirs=existing_dirs,
        files_scanned=len(files),
        files_considered=len(selected_files),
    )
    created_at = datetime.now(tz=timezone.utc).isoformat()
    run_dir = create_run_dir(output_root)

    timings = {
        "scan_seconds": round(scan_seconds, 4),
        "plan_seconds": round(plan_seconds, 4),
        "save_seconds": 0.0,
        "apply_seconds": 0.0,
        "processing_seconds": 0.0,
        "total_seconds": 0.0,
    }
    manifest = {
        "version": MANIFEST_VERSION,
        "created_at": created_at,
        "target_dir": str(target_dir),
        "mode": command_name,
        "fast_lane": settings.fast_lane,
        "preset": args.preset,
        "rules": rules,
        "summary": combined_plan.summary,
        "counts": counts,
        "warnings": combined_plan.warnings,
        "timings": timings,
        "operations": [operation.as_dict() for operation in combined_plan.operations],
    }

    save_started_at = time.perf_counter()
    _write_plan_artifacts(run_dir=run_dir, plan=combined_plan, manifest=manifest)
    save_seconds = time.perf_counter() - save_started_at
    manifest["timings"]["save_seconds"] = round(save_seconds, 4)
    manifest["timings"]["processing_seconds"] = round(scan_seconds + plan_seconds + save_seconds, 4)
    manifest["timings"]["total_seconds"] = manifest["timings"]["processing_seconds"]
    (run_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return combined_plan, run_dir, manifest


def _apply_manifest(manifest_path: Path, *, command_started_at: float) -> dict[str, object]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    target_dir = resolve_configured_path(str(manifest["target_dir"]))
    plan = PlanResult.from_dict(
        {
            "summary": manifest.get("summary", ""),
            "warnings": manifest.get("warnings", []),
            "operations": manifest.get("operations", []),
        }
    )
    apply_started_at = time.perf_counter()
    result = apply_plan(target_dir, plan)
    apply_seconds = time.perf_counter() - apply_started_at

    run_dir = manifest_path.parent
    manifest.setdefault("counts", {})
    manifest.setdefault("timings", {})
    manifest["counts"]["applied_moves"] = result.applied_moves
    manifest["timings"]["apply_seconds"] = round(apply_seconds, 4)
    processing_seconds = float(manifest["timings"].get("processing_seconds", 0.0))
    manifest["timings"]["total_seconds"] = round(processing_seconds + apply_seconds, 4)
    (run_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    apply_result_payload = {
        "version": MANIFEST_VERSION,
        "manifest_path": str(manifest_path),
        "target_dir": str(target_dir),
        "applied_moves": result.applied_moves,
        "skipped": result.skipped,
        "applied_operations": [operation.as_dict() for operation in result.applied_operations],
        "skipped_operations": result.skipped_operations,
        "timings": {
            "apply_seconds": round(apply_seconds, 4),
            "total_seconds": round(time.perf_counter() - command_started_at, 4),
        },
    }
    (run_dir / "apply_result.json").write_text(
        json.dumps(apply_result_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    undo_manifest = {
        "version": MANIFEST_VERSION,
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
        "target_dir": str(target_dir),
        "operations": [
            {
                "source": operation.target_path,
                "destination_dir": ""
                if Path(operation.source).parent == Path(".")
                else Path(operation.source).parent.as_posix(),
                "new_name": Path(operation.source).name,
                "target_path": operation.source,
                "action": "move",
                "confidence": 1.0,
                "reason": f"undo move for {operation.source}",
                "can_apply": True,
                "issues": [],
            }
            for operation in reversed(result.applied_operations)
        ],
    }
    (run_dir / "undo_manifest.json").write_text(
        json.dumps(undo_manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return {
        "manifest": manifest,
        "applied_moves": result.applied_moves,
        "skipped": result.skipped,
    }


def build_client(args: argparse.Namespace) -> LocalLLMClient | None:
    if args.mock:
        return None
    return LocalLLMClient(
        base_url=args.base_url,
        model=args.model,
        api_key=args.api_key,
        timeout=args.timeout,
        temperature=args.temperature,
        max_output_tokens=args.max_output_tokens,
    )


def resolve_configured_path(value: str) -> Path:
    return Path(os.path.expandvars(value)).expanduser().resolve()


def resolve_output_root(configured_output_dir: str | None, target_dir: Path) -> Path:
    if configured_output_dir:
        return resolve_configured_path(configured_output_dir)
    env_output_dir = os.getenv("DIRECTORY_ORGANIZER_OUTPUT_DIR")
    if env_output_dir:
        return resolve_configured_path(env_output_dir)
    return target_dir / ".dirorganizer-runs"


def create_run_dir(output_root: Path) -> Path:
    output_root.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = output_root / run_id
    suffix = 0
    while run_dir.exists():
        suffix += 1
        run_dir = output_root / f"{run_id}_{suffix:02d}"
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def _build_extra_ignores(*, output_root: Path, target_dir: Path) -> set[str]:
    extra_ignores: set[str] = set()
    if output_root.is_relative_to(target_dir):
        relative_output_root = output_root.relative_to(target_dir)
        if relative_output_root.parts:
            extra_ignores.add(relative_output_root.parts[0])
    return extra_ignores


def _resolve_execution_settings(args: argparse.Namespace, preset: PresetConfig) -> ExecutionSettings:
    if args.fast_lane:
        return ExecutionSettings(
            fast_lane=True,
            scan_max_files=None,
            scan_max_depth=args.max_depth if args.max_depth is not None else preset.max_depth,
            candidate_max_files=args.max_files if args.max_files is not None else preset.max_files,
            batch_size=args.batch_size if args.batch_size is not None else preset.batch_size,
            min_confidence=args.min_confidence if args.min_confidence is not None else preset.min_confidence,
            allowed_extensions=preset.allowed_extensions,
            max_size_bytes=preset.max_size_bytes,
            heuristic_directory_map=preset.destination_mapping,
        )
    return ExecutionSettings(
        fast_lane=False,
        scan_max_files=args.max_files,
        scan_max_depth=args.max_depth,
        candidate_max_files=None,
        batch_size=args.batch_size if args.batch_size is not None else DEFAULT_BATCH_SIZE,
        min_confidence=args.min_confidence if args.min_confidence is not None else DEFAULT_MIN_CONFIDENCE,
        allowed_extensions=None,
        max_size_bytes=None,
        heuristic_directory_map=preset.destination_mapping,
    )


def _compute_counts(
    *,
    plan: PlanResult,
    existing_dirs: list[str],
    files_scanned: int,
    files_considered: int,
) -> dict[str, int]:
    planned_moves = 0
    skipped = 0
    blocked = 0
    new_folders: set[str] = set()

    for operation in plan.operations:
        if operation.action == "move" and operation.can_apply:
            planned_moves += 1
            if operation.destination_dir and operation.destination_dir not in existing_dirs:
                new_folders.add(operation.destination_dir)
            continue
        if _is_blocked_operation(operation):
            blocked += 1
        else:
            skipped += 1

    return {
        "files_scanned": files_scanned,
        "files_considered": files_considered,
        "planned_moves": planned_moves,
        "skipped": skipped,
        "blocked": blocked,
        "new_folders": len(new_folders),
        "applied_moves": 0,
    }


def _is_blocked_operation(operation: PlanOperation) -> bool:
    if not operation.issues:
        return False
    issue_text = " ".join(operation.issues).lower()
    return any(marker in issue_text for marker in BLOCKED_ISSUE_MARKERS)


def _write_plan_artifacts(*, run_dir: Path, plan: PlanResult, manifest: dict[str, object]) -> None:
    (run_dir / "plan.json").write_text(
        json.dumps(plan.as_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (run_dir / "plan.md").write_text(render_plan_markdown(plan), encoding="utf-8")
    (run_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_fast_lane_summary(*, plan: PlanResult, manifest: dict[str, object], stdout: TextIO) -> None:
    counts = manifest["counts"]
    stdout.write("Fast lane plan ready\n")
    stdout.write(f"- preset: {manifest['preset']}\n")
    stdout.write(f"- planned: {counts['planned_moves']}\n")
    stdout.write(f"- skipped: {counts['skipped']}\n")
    stdout.write(f"- blocked: {counts['blocked']}\n")
    stdout.write(f"- new folders: {counts['new_folders']}\n")
    stdout.write("\nTop moves:\n")
    top_moves = [operation for operation in plan.operations if operation.action == "move" and operation.can_apply][:5]
    if not top_moves:
        stdout.write("0. none\n")
    else:
        for index, operation in enumerate(top_moves, start=1):
            stdout.write(f"{index}. {operation.source} -> {operation.target_path}\n")


def _prompt_for_apply(*, run_dir: Path, stdin: TextIO, stdout: TextIO) -> bool:
    while True:
        stdout.write("\n[a] apply   [v] view details   [q] quit\n")
        stdout.write("> ")
        stdout.flush()
        answer = stdin.readline()
        if answer == "":
            return False
        choice = answer.strip().lower()
        if choice == "a":
            return True
        if choice == "v":
            stdout.write(f"Plan details: {run_dir / 'plan.md'}\n")
            continue
        if choice == "q":
            return False
        stdout.write("Enter a, v, or q.\n")


def _write_result_line(
    stdout: TextIO,
    *,
    mode: str,
    status: str,
    run_dir: Path,
    manifest: dict[str, object],
    applied_moves: int,
    skipped_total: int | None = None,
) -> None:
    counts = manifest["counts"]
    skipped_value = counts["skipped"] + counts["blocked"] if skipped_total is None else skipped_total
    stdout.write(
        f"[RESULT] mode={mode} status={status} run_dir={run_dir} "
        f"planned_moves={counts['planned_moves']} applied_moves={applied_moves} skipped={skipped_value}\n"
    )


def _is_tty(stream: TextIO) -> bool:
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())
