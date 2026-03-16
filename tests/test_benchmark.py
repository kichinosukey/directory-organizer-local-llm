from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dirorganizer.benchmark import (
    PlannerProfile,
    benchmark_profiles,
    materialize_fixture_corpus,
    parse_args as benchmark_parse_args,
    render_report,
)


class BenchmarkTests(unittest.TestCase):
    def test_materialize_fixture_corpus_creates_expected_file_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = materialize_fixture_corpus(Path(tmp) / "fixture")
            self.assertEqual(len(list(root.iterdir())), 30)
            self.assertTrue((root / "travel-receipt.pdf").exists())

    def test_benchmark_profiles_returns_fixture_and_real_dataset_summaries(self) -> None:
        baseline_profile = PlannerProfile(
            name="baseline",
            model="baseline-model",
            base_url="http://127.0.0.1:1234/v1",
            api_key="not-needed",
        )
        candidate_profile = PlannerProfile(
            name="candidate",
            model="candidate-model",
            base_url="http://127.0.0.1:11434/v1",
            api_key="not-needed",
        )
        call_count = {"baseline": 0, "candidate": 0}

        def fake_run_plan_once(
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
            call_count[profile.name] += 1
            self.assertTrue(use_fast_lane)
            self.assertIsNone(max_files)
            self.assertIsNone(max_depth)
            self.assertIsNone(batch_size)
            self.assertIsNone(timeout)
            return {
                "timings": {
                    "plan_seconds": 1.0 if profile.name == "baseline" else 0.5,
                    "llm_seconds": 0.8 if profile.name == "baseline" else 0.3,
                    "processing_seconds": 1.2 if profile.name == "baseline" else 0.7,
                },
                "planner": {
                    "llm_request_count": 2 if profile.name == "baseline" else 1,
                },
            }

        with tempfile.TemporaryDirectory() as tmp:
            real_target_dir = Path(tmp) / "real"
            real_target_dir.mkdir()
            (real_target_dir / "notes.txt").write_text("notes", encoding="utf-8")

            with mock.patch("dirorganizer.benchmark._run_plan_once", side_effect=fake_run_plan_once):
                report = benchmark_profiles(
                    baseline_profile=baseline_profile,
                    candidate_profile=candidate_profile,
                    real_target_dir=real_target_dir,
                    warmup_runs=1,
                    measured_runs=2,
                    preset="downloads-default",
                )

        self.assertEqual(len(report["datasets"]), 2)
        self.assertEqual(call_count["baseline"], 6)
        self.assertEqual(call_count["candidate"], 6)
        fixture_dataset = report["datasets"][0]
        self.assertEqual(fixture_dataset["name"], "fixture-fast-lane")
        self.assertEqual(
            fixture_dataset["profiles"]["candidate"]["summary"]["plan_seconds"]["median"],
            0.5,
        )

    def test_benchmark_profiles_can_skip_fixture_and_use_full_scan_real_target_only(self) -> None:
        baseline_profile = PlannerProfile(
            name="baseline",
            model="baseline-model",
            base_url="http://127.0.0.1:1234/v1",
            api_key="not-needed",
        )
        candidate_profile = PlannerProfile(
            name="candidate",
            model="candidate-model",
            base_url="http://127.0.0.1:11434/v1",
            api_key="not-needed",
        )
        observed_calls: list[tuple[str, bool, int | None, int | None, int | None, int | None]] = []

        def fake_run_plan_once(
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
            observed_calls.append((profile.name, use_fast_lane, max_files, max_depth, batch_size, timeout))
            return {
                "timings": {
                    "plan_seconds": 1.0,
                    "llm_seconds": 0.8,
                    "processing_seconds": 1.2,
                },
                "planner": {
                    "llm_request_count": 2,
                },
            }

        with tempfile.TemporaryDirectory() as tmp:
            real_target_dir = Path(tmp) / "real"
            real_target_dir.mkdir()
            (real_target_dir / "notes.txt").write_text("notes", encoding="utf-8")

            with mock.patch("dirorganizer.benchmark._run_plan_once", side_effect=fake_run_plan_once):
                report = benchmark_profiles(
                    baseline_profile=baseline_profile,
                    candidate_profile=candidate_profile,
                    real_target_dir=real_target_dir,
                    warmup_runs=0,
                    measured_runs=1,
                    preset="downloads-default",
                    use_fast_lane=False,
                    include_fixture=False,
                    max_files=200,
                    max_depth=3,
                    batch_size=10,
                    timeout=180,
                )

        self.assertEqual(len(report["datasets"]), 1)
        self.assertEqual(report["datasets"][0]["name"], "real-target-full-scan")
        self.assertEqual(
            observed_calls,
            [
                ("baseline", False, 200, 3, 10, 180),
                ("candidate", False, 200, 3, 10, 180),
            ],
        )

    def test_benchmark_parse_args_requires_real_target_when_skipping_fixture(self) -> None:
        with self.assertRaises(SystemExit):
            benchmark_parse_args(
                [
                    "--skip-fixture",
                    "--baseline-model",
                    "baseline-model",
                    "--baseline-base-url",
                    "http://127.0.0.1:1234/v1",
                    "--candidate-model",
                    "candidate-model",
                    "--candidate-base-url",
                    "http://127.0.0.1:11434/v1",
                ]
            )

    def test_render_report_outputs_markdown_table(self) -> None:
        report = {
            "generated_at": "2026-03-15T00:00:00+00:00",
            "warmup_runs": 1,
            "measured_runs": 5,
            "datasets": [
                {
                    "name": "fixture-fast-lane",
                    "target_dir": "/tmp/example",
                    "profiles": {
                        "baseline": {
                            "summary": {
                                "plan_seconds": {"min": 1.0, "median": 1.1, "max": 1.2},
                                "llm_seconds": {"min": 0.8, "median": 0.9, "max": 1.0},
                                "processing_seconds": {"min": 1.3, "median": 1.4, "max": 1.5},
                                "llm_request_count": {"min": 2.0, "median": 2.0, "max": 2.0},
                            }
                        }
                    },
                }
            ],
        }

        rendered = render_report(report, output_format="markdown")

        self.assertIn("# Local Planner Benchmark Report", rendered)
        self.assertIn("| profile | plan median |", rendered)
        self.assertIn("| baseline | 1.1000 |", rendered)

    def test_benchmark_profile_allows_candidate_extra_body_json(self) -> None:
        profile = PlannerProfile(
            name="candidate",
            model="Qwen/Qwen3-8B-AWQ",
            base_url="http://127.0.0.1:11434/v1",
            api_key="not-needed",
            extra_body_json='{"chat_template_kwargs":{"enable_thinking":false}}',
        )

        self.assertEqual(
            profile.extra_body_json,
            '{"chat_template_kwargs":{"enable_thinking":false}}',
        )


if __name__ == "__main__":
    unittest.main()
