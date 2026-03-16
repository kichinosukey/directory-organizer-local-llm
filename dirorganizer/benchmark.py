from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from dirorganizer.presets import DEFAULT_PRESET_NAME, list_presets


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "run_directory_organizer.py"
ProgressCallback = Callable[[str], None]

FIXTURE_CORPUS_FILES: tuple[tuple[str, bytes], ...] = (
    ("receipt-2026-01.pdf", b"fixture pdf 01\n"),
    ("receipt-2026-02.pdf", b"fixture pdf 02\n"),
    ("receipt-2026-03.pdf", b"fixture pdf 03\n"),
    ("meeting-notes-01.txt", b"fixture txt 01\n"),
    ("meeting-notes-02.txt", b"fixture txt 02\n"),
    ("meeting-notes-03.txt", b"fixture txt 03\n"),
    ("roadmap-01.md", b"# fixture md 01\n"),
    ("roadmap-02.md", b"# fixture md 02\n"),
    ("roadmap-03.md", b"# fixture md 03\n"),
    ("contract-draft-01.docx", b"fixture docx 01\n"),
    ("contract-draft-02.docx", b"fixture docx 02\n"),
    ("contract-draft-03.docx", b"fixture docx 03\n"),
    ("metrics-01.csv", b"col1,col2\n1,2\n"),
    ("metrics-02.csv", b"col1,col2\n3,4\n"),
    ("metrics-03.csv", b"col1,col2\n5,6\n"),
    ("budget-01.xlsx", b"fixture xlsx 01\n"),
    ("budget-02.xlsx", b"fixture xlsx 02\n"),
    ("budget-03.xlsx", b"fixture xlsx 03\n"),
    ("screenshot-01.png", b"fixture png 01\n"),
    ("screenshot-02.png", b"fixture png 02\n"),
    ("screenshot-03.png", b"fixture png 03\n"),
    ("photo-01.jpg", b"fixture jpg 01\n"),
    ("photo-02.jpg", b"fixture jpg 02\n"),
    ("photo-03.jpg", b"fixture jpg 03\n"),
    ("scan-01.jpeg", b"fixture jpeg 01\n"),
    ("scan-02.jpeg", b"fixture jpeg 02\n"),
    ("scan-03.jpeg", b"fixture jpeg 03\n"),
    ("workspace-summary.md", b"# workspace summary\n"),
    ("research-notes.txt", b"research notes\n"),
    ("travel-receipt.pdf", b"travel receipt\n"),
)


@dataclass(frozen=True)
class PlannerProfile:
    name: str
    model: str
    base_url: str
    api_key: str
    api_mode: str = "chat_completions"
    extra_body_json: str | None = None


@dataclass(frozen=True)
class BenchmarkRun:
    plan_seconds: float
    llm_seconds: float
    processing_seconds: float
    llm_request_count: int


@dataclass(frozen=True)
class MetricSummary:
    min: float
    median: float
    max: float


def materialize_fixture_corpus(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    base_time = 1_710_000_000
    for index, (name, content) in enumerate(FIXTURE_CORPUS_FILES):
        path = root / name
        path.write_bytes(content)
        timestamp = base_time + index
        os.utime(path, (timestamp, timestamp))
    return root


def benchmark_profiles(
    *,
    baseline_profile: PlannerProfile,
    candidate_profile: PlannerProfile,
    real_target_dir: Path | None,
    warmup_runs: int,
    measured_runs: int,
    preset: str,
    use_fast_lane: bool = True,
    include_fixture: bool = True,
    max_files: int | None = None,
    max_depth: int | None = None,
    batch_size: int | None = None,
    timeout: int | None = None,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, object]:
    report: dict[str, object] = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "warmup_runs": warmup_runs,
        "measured_runs": measured_runs,
        "datasets": [],
    }
    dataset_count = int(include_fixture) + int(real_target_dir is not None)
    total_runs = dataset_count * 2 * (warmup_runs + measured_runs)
    if progress_callback is not None:
        progress_callback(
            f"benchmarking {total_runs} local planner runs across {dataset_count} dataset(s)"
        )

    if include_fixture:
        with tempfile.TemporaryDirectory() as tmp:
            fixture_dir = materialize_fixture_corpus(Path(tmp) / "fixture-corpus")
            report["datasets"].append(
                _benchmark_dataset(
                    dataset_name=_dataset_name("fixture", use_fast_lane=use_fast_lane),
                    target_dir=fixture_dir,
                    baseline_profile=baseline_profile,
                    candidate_profile=candidate_profile,
                    warmup_runs=warmup_runs,
                    measured_runs=measured_runs,
                    preset=preset,
                    use_fast_lane=use_fast_lane,
                    max_files=max_files,
                    max_depth=max_depth,
                    batch_size=batch_size,
                    timeout=timeout,
                    progress_callback=progress_callback,
                )
            )

    if real_target_dir is not None:
        report["datasets"].append(
            _benchmark_dataset(
                dataset_name=_dataset_name("real-target", use_fast_lane=use_fast_lane),
                target_dir=real_target_dir,
                baseline_profile=baseline_profile,
                candidate_profile=candidate_profile,
                warmup_runs=warmup_runs,
                measured_runs=measured_runs,
                preset=preset,
                use_fast_lane=use_fast_lane,
                max_files=max_files,
                max_depth=max_depth,
                batch_size=batch_size,
                timeout=timeout,
                progress_callback=progress_callback,
            )
        )

    return report


def render_report(report: dict[str, object], *, output_format: str) -> str:
    if output_format == "json":
        return json.dumps(report, ensure_ascii=False, indent=2)
    return _render_markdown_report(report)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark planner latency across two local planner profiles.")
    parser.add_argument("--real-target-dir", help="Optional real directory to benchmark in addition to the fixture.")
    parser.add_argument("--warmup-runs", type=int, default=1, help="Warmup runs before measured runs.")
    parser.add_argument("--runs", type=int, default=5, help="Measured runs per dataset/profile.")
    parser.add_argument(
        "--full-scan",
        action="store_true",
        help="Disable fast-lane and benchmark full plan mode instead.",
    )
    parser.add_argument(
        "--skip-fixture",
        action="store_true",
        help="Skip the synthetic fixture dataset and benchmark only --real-target-dir.",
    )
    parser.add_argument("--max-files", type=int, default=None, help="Optional max files to pass through to plan.")
    parser.add_argument("--max-depth", type=int, default=None, help="Optional max depth to pass through to plan.")
    parser.add_argument("--batch-size", type=int, default=None, help="Optional batch size to pass through to plan.")
    parser.add_argument("--timeout", type=int, default=None, help="Optional request timeout seconds to pass through to plan.")
    parser.add_argument(
        "--preset",
        default=DEFAULT_PRESET_NAME,
        choices=list_presets(),
        help="Preset to use for the benchmark plan runs.",
    )
    parser.add_argument(
        "--format",
        choices=("markdown", "json"),
        default="markdown",
        help="Report format.",
    )
    parser.add_argument("--output", help="Optional output file path.")

    _add_profile_arguments(
        parser,
        prefix="baseline",
        model_env=None,
        base_url_env=None,
        api_key_env=None,
        api_mode_env=None,
        extra_body_env=None,
    )
    _add_profile_arguments(
        parser,
        prefix="candidate",
        model_env=None,
        base_url_env=None,
        api_key_env=None,
        api_mode_env=None,
        extra_body_env=None,
    )

    args = parser.parse_args(argv)
    for prefix in ("baseline", "candidate"):
        if not getattr(args, f"{prefix}_model"):
            parser.error(f"--{prefix}-model is required")
        if not getattr(args, f"{prefix}_base_url"):
            parser.error(f"--{prefix}-base-url is required")
    if args.warmup_runs < 0:
        parser.error("--warmup-runs must be >= 0")
    if args.runs <= 0:
        parser.error("--runs must be > 0")
    if args.skip_fixture and not args.real_target_dir:
        parser.error("--skip-fixture requires --real-target-dir")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    baseline_profile = PlannerProfile(
        name="baseline",
        model=args.baseline_model,
        base_url=args.baseline_base_url,
        api_key=args.baseline_api_key,
        api_mode=args.baseline_api_mode,
        extra_body_json=args.baseline_extra_body_json,
    )
    candidate_profile = PlannerProfile(
        name="candidate",
        model=args.candidate_model,
        base_url=args.candidate_base_url,
        api_key=args.candidate_api_key,
        api_mode=args.candidate_api_mode,
        extra_body_json=args.candidate_extra_body_json,
    )
    real_target_dir = Path(args.real_target_dir).expanduser().resolve() if args.real_target_dir else None
    report = benchmark_profiles(
        baseline_profile=baseline_profile,
        candidate_profile=candidate_profile,
        real_target_dir=real_target_dir,
        warmup_runs=args.warmup_runs,
        measured_runs=args.runs,
        preset=args.preset,
        use_fast_lane=not args.full_scan,
        include_fixture=not args.skip_fixture,
        max_files=args.max_files,
        max_depth=args.max_depth,
        batch_size=args.batch_size,
        timeout=args.timeout,
        progress_callback=_write_benchmark_progress,
    )
    rendered = render_report(report, output_format=args.format)
    if args.output:
        Path(args.output).expanduser().resolve().write_text(rendered, encoding="utf-8")
    print(rendered)
    return 0


def _add_profile_arguments(
    parser: argparse.ArgumentParser,
    *,
    prefix: str,
    model_env: str | None,
    base_url_env: str | None,
    api_key_env: str | None,
    api_mode_env: str | None,
    extra_body_env: str | None,
) -> None:
    parser.add_argument(f"--{prefix}-model", default=os.getenv(model_env) if model_env else None)
    parser.add_argument(f"--{prefix}-base-url", default=os.getenv(base_url_env) if base_url_env else None)
    parser.add_argument(f"--{prefix}-api-key", default=os.getenv(api_key_env, "not-needed") if api_key_env else "not-needed")
    parser.add_argument(
        f"--{prefix}-api-mode",
        choices=("chat_completions", "responses"),
        default=os.getenv(api_mode_env, "chat_completions") if api_mode_env else "chat_completions",
    )
    parser.add_argument(f"--{prefix}-extra-body-json", default=os.getenv(extra_body_env) if extra_body_env else None)


def _benchmark_dataset(
    *,
    dataset_name: str,
    target_dir: Path,
    baseline_profile: PlannerProfile,
    candidate_profile: PlannerProfile,
    warmup_runs: int,
    measured_runs: int,
    preset: str,
    use_fast_lane: bool,
    max_files: int | None,
    max_depth: int | None,
    batch_size: int | None,
    timeout: int | None,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, object]:
    dataset_payload: dict[str, object] = {
        "name": dataset_name,
        "target_dir": str(target_dir),
        "profiles": {},
    }
    for profile in (baseline_profile, candidate_profile):
        runs = _benchmark_profile_runs(
            dataset_name=dataset_name,
            target_dir=target_dir,
            profile=profile,
            warmup_runs=warmup_runs,
            measured_runs=measured_runs,
            preset=preset,
            use_fast_lane=use_fast_lane,
            max_files=max_files,
            max_depth=max_depth,
            batch_size=batch_size,
            timeout=timeout,
            progress_callback=progress_callback,
        )
        dataset_payload["profiles"][profile.name] = {
            "summary": _summarize_runs(runs),
            "runs": [asdict(run) for run in runs],
        }
    return dataset_payload


def _benchmark_profile_runs(
    *,
    dataset_name: str,
    target_dir: Path,
    profile: PlannerProfile,
    warmup_runs: int,
    measured_runs: int,
    preset: str,
    use_fast_lane: bool,
    max_files: int | None,
    max_depth: int | None,
    batch_size: int | None,
    timeout: int | None,
    progress_callback: ProgressCallback | None = None,
) -> list[BenchmarkRun]:
    for run_index in range(1, warmup_runs + 1):
        warmup_label = f"{dataset_name} / {profile.name} / warmup {run_index}/{warmup_runs}"
        if progress_callback is not None:
            progress_callback(f"starting {warmup_label}")
        started_at = time.perf_counter()
        manifest = _run_plan_once(
            target_dir=target_dir,
            profile=profile,
            preset=preset,
            use_fast_lane=use_fast_lane,
            max_files=max_files,
            max_depth=max_depth,
            batch_size=batch_size,
            timeout=timeout,
        )
        if progress_callback is not None:
            progress_callback(
                f"completed {warmup_label} in {time.perf_counter() - started_at:.1f}s "
                f"(plan={_metric_from_manifest(manifest, 'plan_seconds'):.1f}s, "
                f"llm={_metric_from_manifest(manifest, 'llm_seconds'):.1f}s, "
                f"req={_request_count_from_manifest(manifest)})"
            )

    results: list[BenchmarkRun] = []
    for run_index in range(1, measured_runs + 1):
        measured_label = f"{dataset_name} / {profile.name} / run {run_index}/{measured_runs}"
        if progress_callback is not None:
            progress_callback(f"starting {measured_label}")
        started_at = time.perf_counter()
        manifest = _run_plan_once(
            target_dir=target_dir,
            profile=profile,
            preset=preset,
            use_fast_lane=use_fast_lane,
            max_files=max_files,
            max_depth=max_depth,
            batch_size=batch_size,
            timeout=timeout,
        )
        if progress_callback is not None:
            progress_callback(
                f"completed {measured_label} in {time.perf_counter() - started_at:.1f}s "
                f"(plan={_metric_from_manifest(manifest, 'plan_seconds'):.1f}s, "
                f"llm={_metric_from_manifest(manifest, 'llm_seconds'):.1f}s, "
                f"req={_request_count_from_manifest(manifest)})"
            )
        timings = manifest.get("timings", {})
        planner = manifest.get("planner", {})
        results.append(
            BenchmarkRun(
                plan_seconds=float(timings.get("plan_seconds", 0.0)),
                llm_seconds=float(timings.get("llm_seconds", 0.0)),
                processing_seconds=float(timings.get("processing_seconds", 0.0)),
                llm_request_count=int(planner.get("llm_request_count", 0)),
            )
        )
    return results


def _run_plan_once(
    *,
    target_dir: Path,
    profile: PlannerProfile,
    preset: str,
    use_fast_lane: bool,
    max_files: int | None,
    max_depth: int | None,
    batch_size: int | None,
    timeout: int | None,
) -> dict[str, object]:
    with tempfile.TemporaryDirectory() as tmp:
        output_dir = Path(tmp) / "artifacts"
        command = [
            sys.executable,
            str(SCRIPT_PATH),
            "plan",
            "--target-dir",
            str(target_dir),
            "--output-dir",
            str(output_dir),
            "--preset",
            preset,
            "--model",
            profile.model,
            "--base-url",
            profile.base_url,
            "--api-key",
            profile.api_key,
            "--api-mode",
            profile.api_mode,
        ]
        if use_fast_lane:
            command.append("--fast-lane")
        if max_files is not None:
            command.extend(["--max-files", str(max_files)])
        if max_depth is not None:
            command.extend(["--max-depth", str(max_depth)])
        if batch_size is not None:
            command.extend(["--batch-size", str(batch_size)])
        if timeout is not None:
            command.extend(["--timeout", str(timeout)])
        if profile.extra_body_json:
            command.extend(["--extra-body-json", profile.extra_body_json])
        result = subprocess.run(
            command,
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            stderr_tail = result.stderr.strip()[-400:]
            stdout_tail = result.stdout.strip()[-400:]
            raise RuntimeError(
                f"benchmark run failed for {profile.name}: stderr={stderr_tail!r} stdout={stdout_tail!r}"
            )
        run_dir = _extract_run_dir(result.stdout)
        manifest_path = run_dir / "manifest.json"
        return json.loads(manifest_path.read_text(encoding="utf-8"))


def _extract_run_dir(stdout: str) -> Path:
    for line in stdout.splitlines():
        if not line.startswith("[RESULT] "):
            continue
        for token in line.split():
            if token.startswith("run_dir="):
                return Path(token.split("=", 1)[1])
    raise RuntimeError(f"run_dir not found in planner output: {stdout}")


def _summarize_runs(runs: list[BenchmarkRun]) -> dict[str, dict[str, float]]:
    return {
        "plan_seconds": asdict(_metric_summary([run.plan_seconds for run in runs])),
        "llm_seconds": asdict(_metric_summary([run.llm_seconds for run in runs])),
        "processing_seconds": asdict(_metric_summary([run.processing_seconds for run in runs])),
        "llm_request_count": asdict(_metric_summary([float(run.llm_request_count) for run in runs])),
    }


def _metric_summary(values: list[float]) -> MetricSummary:
    return MetricSummary(
        min=min(values),
        median=statistics.median(values),
        max=max(values),
    )


def _metric_from_manifest(manifest: dict[str, object], key: str) -> float:
    timings = manifest.get("timings", {})
    if not isinstance(timings, dict):
        return 0.0
    return float(timings.get(key, 0.0))


def _request_count_from_manifest(manifest: dict[str, object]) -> int:
    planner = manifest.get("planner", {})
    if not isinstance(planner, dict):
        return 0
    return int(planner.get("llm_request_count", 0))


def _write_benchmark_progress(message: str) -> None:
    sys.stderr.write(f"[benchmark] {message}\n")
    sys.stderr.flush()


def _render_markdown_report(report: dict[str, object]) -> str:
    lines = [
        "# Local Planner Benchmark Report",
        "",
        f"- generated_at: {report['generated_at']}",
        f"- warmup_runs: {report['warmup_runs']}",
        f"- measured_runs: {report['measured_runs']}",
        "",
    ]
    for dataset in report.get("datasets", []):
        if not isinstance(dataset, dict):
            continue
        lines.extend(
            [
                f"## {dataset['name']}",
                "",
                f"- target_dir: {dataset['target_dir']}",
                "",
                "| profile | plan median | plan min | plan max | llm median | processing median | req median |",
                "|---|---:|---:|---:|---:|---:|---:|",
            ]
        )
        profiles = dataset.get("profiles", {})
        if not isinstance(profiles, dict):
            continue
        for profile_name, payload in profiles.items():
            if not isinstance(payload, dict):
                continue
            summary = payload.get("summary", {})
            if not isinstance(summary, dict):
                continue
            plan = _summary_values(summary, "plan_seconds")
            llm = _summary_values(summary, "llm_seconds")
            processing = _summary_values(summary, "processing_seconds")
            request_count = _summary_values(summary, "llm_request_count")
            lines.append(
                "| {profile} | {plan_median:.4f} | {plan_min:.4f} | {plan_max:.4f} | "
                "{llm_median:.4f} | {processing_median:.4f} | {req_median:.1f} |".format(
                    profile=profile_name,
                    plan_median=plan["median"],
                    plan_min=plan["min"],
                    plan_max=plan["max"],
                    llm_median=llm["median"],
                    processing_median=processing["median"],
                    req_median=request_count["median"],
                )
            )
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def _dataset_name(prefix: str, *, use_fast_lane: bool) -> str:
    suffix = "fast-lane" if use_fast_lane else "full-scan"
    return f"{prefix}-{suffix}"


def _summary_values(summary: dict[str, Any], name: str) -> dict[str, float]:
    values = summary.get(name, {})
    if not isinstance(values, dict):
        return {"min": 0.0, "median": 0.0, "max": 0.0}
    return {
        "min": float(values.get("min", 0.0)),
        "median": float(values.get("median", 0.0)),
        "max": float(values.get("max", 0.0)),
    }
