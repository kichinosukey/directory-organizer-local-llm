#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dirorganizer.env_loader import load_dotenv
from dirorganizer.llm_client import LocalLLMClient
from dirorganizer.planner import apply_plan, build_plan, render_plan_markdown
from dirorganizer.rules import load_rules
from dirorganizer.scanner import scan_directory

load_dotenv(REPO_ROOT / ".env")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plan or apply safe directory organization with a local LLM.")
    parser.add_argument(
        "--target-dir",
        default=os.getenv("DIRECTORY_ORGANIZER_TARGET_DIR"),
        help="Directory to organize",
    )
    parser.add_argument("--mode", choices=("plan", "apply"), default="plan")
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
    parser.add_argument("--max-files", type=int, default=50)
    parser.add_argument("--max-depth", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--min-confidence", type=float, default=0.60)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--max-output-tokens", type=int, default=1200)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--include-hidden", action="store_true")
    parser.add_argument("--mock", action="store_true", help="Use heuristic planner instead of LLM")
    args = parser.parse_args(argv)

    if not (args.target_dir or "").strip():
        parser.error("--target-dir is required unless DIRECTORY_ORGANIZER_TARGET_DIR is set")
    if not args.mock and not (args.model or "").strip():
        parser.error("--model is required unless --mock is used")

    return args


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


def resolve_output_root(args: argparse.Namespace, target_dir: Path) -> Path:
    if args.output_dir:
        return resolve_configured_path(args.output_dir)
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


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    target_dir = resolve_configured_path(args.target_dir)
    if not target_dir.is_dir():
        print(f"[ERROR] target directory does not exist: {target_dir}", file=sys.stderr)
        return 1

    output_root = resolve_output_root(args, target_dir)
    extra_ignores: set[str] = set()
    if output_root.is_relative_to(target_dir):
        relative_output_root = output_root.relative_to(target_dir)
        if relative_output_root.parts:
            extra_ignores.add(relative_output_root.parts[0])
    files, existing_dirs, scan_truncated = scan_directory(
        target_dir,
        max_files=args.max_files,
        max_depth=args.max_depth,
        include_hidden=args.include_hidden,
        extra_ignores=extra_ignores,
    )
    if not files:
        print("[ERROR] no files found to organize", file=sys.stderr)
        return 1

    rules = load_rules(args.rules)
    client = build_client(args)
    try:
        plan = build_plan(
            root=target_dir,
            files=files,
            existing_dirs=existing_dirs,
            rules=rules,
            client=client,
            batch_size=args.batch_size,
            min_confidence=args.min_confidence,
            mock=args.mock,
            scan_truncated=scan_truncated,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] failed to build plan: {exc}", file=sys.stderr)
        return 1

    run_dir = create_run_dir(output_root)
    manifest = {
        "mode": args.mode,
        "target_dir": str(target_dir),
        "output_dir": str(run_dir),
        "files_scanned": len(files),
        "existing_dirs": existing_dirs,
        "rules": rules,
        "summary": plan.summary,
        "warnings": plan.warnings,
    }
    (run_dir / "plan.json").write_text(
        json.dumps(plan.as_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (run_dir / "plan.md").write_text(render_plan_markdown(plan), encoding="utf-8")

    applied_moves = 0
    skipped = sum(1 for operation in plan.operations if not operation.can_apply or operation.action != "move")
    if args.mode == "apply":
        result = apply_plan(target_dir, plan)
        applied_moves = result.applied_moves
        skipped = result.skipped

    status = "success"
    print(
        f"[RESULT] mode={args.mode} status={status} run_dir={run_dir} "
        f"planned_moves={sum(1 for operation in plan.operations if operation.action == 'move')} "
        f"applied_moves={applied_moves} skipped={skipped}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
