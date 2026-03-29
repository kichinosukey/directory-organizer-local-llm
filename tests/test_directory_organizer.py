from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from dirorganizer.cli import _build_planner_manifest_section, build_client, main, parse_args, resolve_output_root
from dirorganizer.env_loader import load_dotenv
from dirorganizer.llm_client import LocalLLMClient
from dirorganizer.models import FileRecord
from dirorganizer.planner import build_plan
from dirorganizer.scanner import scan_directory


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "run_directory_organizer.py"


class FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


class DirectoryOrganizerTests(unittest.TestCase):
    def test_load_dotenv_reads_values_without_overwriting_existing_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dotenv_path = Path(tmp) / ".env"
            dotenv_path.write_text(
                'LOCAL_LLM_MODEL="from-dotenv"\nLOCAL_LLM_API_KEY=test-key\n',
                encoding="utf-8",
            )

            previous_model = os.environ.get("LOCAL_LLM_MODEL")
            previous_api_key = os.environ.get("LOCAL_LLM_API_KEY")
            try:
                os.environ["LOCAL_LLM_MODEL"] = "from-env"
                os.environ.pop("LOCAL_LLM_API_KEY", None)
                loaded = load_dotenv(dotenv_path)
                self.assertTrue(loaded)
                self.assertEqual(os.environ["LOCAL_LLM_MODEL"], "from-env")
                self.assertEqual(os.environ["LOCAL_LLM_API_KEY"], "test-key")
            finally:
                if previous_model is None:
                    os.environ.pop("LOCAL_LLM_MODEL", None)
                else:
                    os.environ["LOCAL_LLM_MODEL"] = previous_model
                if previous_api_key is None:
                    os.environ.pop("LOCAL_LLM_API_KEY", None)
                else:
                    os.environ["LOCAL_LLM_API_KEY"] = previous_api_key

    def test_build_client_prefers_local_env_then_cli_overrides(self) -> None:
        original_env = {
            "LOCAL_LLM_MODEL": os.environ.get("LOCAL_LLM_MODEL"),
            "LOCAL_LLM_BASE_URL": os.environ.get("LOCAL_LLM_BASE_URL"),
            "LOCAL_LLM_API_KEY": os.environ.get("LOCAL_LLM_API_KEY"),
            "LOCAL_LLM_API_MODE": os.environ.get("LOCAL_LLM_API_MODE"),
            "LOCAL_LLM_EXTRA_BODY_JSON": os.environ.get("LOCAL_LLM_EXTRA_BODY_JSON"),
        }
        try:
            os.environ["LOCAL_LLM_MODEL"] = "local-model"
            os.environ["LOCAL_LLM_BASE_URL"] = "http://127.0.0.1:1234/v1"
            os.environ["LOCAL_LLM_API_KEY"] = "local-key"
            os.environ["LOCAL_LLM_API_MODE"] = "responses"
            os.environ["LOCAL_LLM_EXTRA_BODY_JSON"] = '{"chat_template_kwargs":{"enable_thinking":false}}'

            args_from_env = parse_args(["plan", "--target-dir", "."])
            client_from_env = build_client(args_from_env)
            assert client_from_env is not None
            self.assertEqual(client_from_env.model, "local-model")
            self.assertEqual(client_from_env.base_url, "http://127.0.0.1:1234/v1")
            self.assertEqual(client_from_env.api_key, "local-key")
            self.assertEqual(client_from_env.api_mode, "responses")
            self.assertEqual(client_from_env.extra_body, {"chat_template_kwargs": {"enable_thinking": False}})

            args_from_cli = parse_args(
                [
                    "plan",
                    "--target-dir",
                    ".",
                    "--model",
                    "cli-model",
                    "--base-url",
                    "https://cli.example/v1",
                    "--api-key",
                    "cli-key",
                    "--api-mode",
                    "chat_completions",
                    "--extra-body-json",
                    '{"chat_template_kwargs":{"enable_thinking":false}}',
                ]
            )
            client_from_cli = build_client(args_from_cli)
            assert client_from_cli is not None
            self.assertEqual(client_from_cli.model, "cli-model")
            self.assertEqual(client_from_cli.base_url, "https://cli.example/v1")
            self.assertEqual(client_from_cli.api_key, "cli-key")
            self.assertEqual(client_from_cli.api_mode, "chat_completions")
            self.assertEqual(client_from_cli.extra_body, {"chat_template_kwargs": {"enable_thinking": False}})
        finally:
            for key, value in original_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_parse_args_rejects_invalid_extra_body_json(self) -> None:
        with self.assertRaises(SystemExit):
            parse_args(
                [
                    "plan",
                    "--target-dir",
                    ".",
                    "--extra-body-json",
                    "not-json",
                ]
            )

    def test_plan_mode_writes_manifest_v2_and_keeps_files_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "messy"
            root.mkdir()
            (root / "receipt-2026.pdf").write_text("finance", encoding="utf-8")
            (root / "notes.txt").write_text("notes", encoding="utf-8")

            result = self.run_cli("plan", "--target-dir", str(root), "--mock")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("planned_moves=2", result.stdout)
            self.assertTrue((root / "receipt-2026.pdf").exists())
            self.assertTrue((root / "notes.txt").exists())

            run_dir = self.extract_run_dir(result.stdout)
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["version"], 2)
            self.assertEqual(manifest["mode"], "plan")
            self.assertFalse(manifest["fast_lane"])
            self.assertEqual(manifest["counts"]["planned_moves"], 2)

            plan = json.loads((run_dir / "plan.json").read_text(encoding="utf-8"))
            operations = {item["source"]: item for item in plan["operations"]}
            self.assertEqual(operations["receipt-2026.pdf"]["target_path"], "documents/finance/receipt-2026.pdf")
            self.assertEqual(operations["notes.txt"]["target_path"], "documents/notes/notes.txt")

    def test_plan_manifest_includes_planner_section_and_extended_timings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "messy"
            root.mkdir()
            (root / "notes.txt").write_text("notes", encoding="utf-8")

            result = self.run_cli("plan", "--target-dir", str(root), "--mock")

            self.assertEqual(result.returncode, 0, result.stderr)
            run_dir = self.extract_run_dir(result.stdout)
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["planner"]["provider"], "mock")
            self.assertEqual(manifest["planner"]["transport"], "heuristic")
            self.assertEqual(manifest["planner"]["llm_request_count"], 0)
            self.assertIn("llm_seconds", manifest["timings"])
            self.assertIn("validation_seconds", manifest["timings"])

    def test_legacy_mode_apply_maps_to_run_yes_and_moves_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "messy"
            root.mkdir()
            (root / "screenshot.png").write_bytes(b"png")
            (root / "archive.zip").write_bytes(b"zip")

            result = self.run_cli("--target-dir", str(root), "--mode", "apply", "--mock")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((root / "screenshot.png").exists())
            self.assertFalse((root / "archive.zip").exists())
            self.assertTrue((root / "media" / "images" / "screenshot.png").exists())
            self.assertTrue((root / "archives" / "archive.zip").exists())

            run_dir = self.extract_run_dir(result.stdout)
            self.assertTrue((run_dir / "apply_result.json").exists())
            self.assertTrue((run_dir / "undo_manifest.json").exists())

    def test_hidden_files_are_ignored_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "messy"
            root.mkdir()
            (root / ".secret.txt").write_text("secret", encoding="utf-8")
            (root / "visible.txt").write_text("visible", encoding="utf-8")

            result = self.run_cli("plan", "--target-dir", str(root), "--mock")

            self.assertEqual(result.returncode, 0, result.stderr)
            run_dir = self.extract_run_dir(result.stdout)
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["counts"]["files_scanned"], 1)

    def test_scan_directory_skips_files_that_disappear_mid_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "messy"
            root.mkdir()
            stable_file = root / "stable.txt"
            disappearing_file = root / "gone.txt"
            stable_file.write_text("stable", encoding="utf-8")
            disappearing_file.write_text("gone", encoding="utf-8")

            original_stat = Path.stat

            def flaky_stat(path: Path, *args: object, **kwargs: object):
                if Path(path).name == disappearing_file.name:
                    raise FileNotFoundError(path)
                return original_stat(path, *args, **kwargs)

            with mock.patch("pathlib.Path.stat", autospec=True, side_effect=flaky_stat):
                files, existing_dirs, truncated = scan_directory(
                    root,
                    max_files=None,
                    max_depth=None,
                    include_hidden=False,
                )

            self.assertFalse(truncated)
            self.assertEqual(existing_dirs, [])
            self.assertEqual([record.relative_path for record in files], ["stable.txt"])

    def test_plan_mode_handles_output_root_equal_to_target_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "messy"
            root.mkdir()
            (root / "notes.txt").write_text("notes", encoding="utf-8")

            result = self.run_cli(
                "plan",
                "--target-dir",
                str(root),
                "--mock",
                extra_env={"DIRECTORY_ORGANIZER_OUTPUT_DIR": str(root)},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            run_dir = self.extract_run_dir(result.stdout)
            self.assertEqual(run_dir.parent.resolve(), root.resolve())

    def test_default_output_root_is_repo_local_runs_dir(self) -> None:
        original_output_dir = os.environ.get("DIRECTORY_ORGANIZER_OUTPUT_DIR")
        try:
            os.environ.pop("DIRECTORY_ORGANIZER_OUTPUT_DIR", None)
            resolved = resolve_output_root(None, REPO_ROOT)
            self.assertEqual(resolved, REPO_ROOT / ".dirorganizer-runs")
        finally:
            if original_output_dir is None:
                os.environ.pop("DIRECTORY_ORGANIZER_OUTPUT_DIR", None)
            else:
                os.environ["DIRECTORY_ORGANIZER_OUTPUT_DIR"] = original_output_dir

    def test_manifest_created_at_and_run_dir_use_local_timezone(self) -> None:
        fixed_now = datetime(2026, 3, 16, 20, 45, 30, tzinfo=timezone(timedelta(hours=9)))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "messy"
            output_root = Path(tmp) / "artifacts"
            root.mkdir()
            (root / "notes.txt").write_text("notes", encoding="utf-8")

            stdout = io.StringIO()
            stderr = io.StringIO()
            with mock.patch("dirorganizer.cli._now_local", return_value=fixed_now):
                exit_code = main(
                    [
                        "plan",
                        "--target-dir",
                        str(root),
                        "--output-dir",
                        str(output_root),
                        "--mock",
                    ],
                    stdout=stdout,
                    stderr=stderr,
                )

            self.assertEqual(exit_code, 0, stderr.getvalue())
            run_dir = self.extract_run_dir(stdout.getvalue())
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(run_dir.name, "20260316T204530+0900")
            self.assertEqual(manifest["created_at"], "2026-03-16T20:45:30+09:00")

    def test_cli_expands_environment_variables_in_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "messy"
            output_root = Path(tmp) / "artifacts"
            root.mkdir()
            (root / "notes.txt").write_text("notes", encoding="utf-8")

            result = self.run_cli(
                "plan",
                "--target-dir",
                "$TEST_ORG_TARGET",
                "--mock",
                extra_env={
                    "TEST_ORG_TARGET": str(root),
                    "DIRECTORY_ORGANIZER_OUTPUT_DIR": "$TEST_ORG_OUTPUT",
                    "TEST_ORG_OUTPUT": str(output_root),
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            run_dir = self.extract_run_dir(result.stdout)
            self.assertEqual(run_dir.parent.resolve(), output_root.resolve())

    def test_default_plan_scans_recursively_without_depth_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "messy"
            nested = root / "a" / "b" / "c" / "d" / "e"
            nested.mkdir(parents=True)
            (nested / "deep.txt").write_text("deep", encoding="utf-8")

            result = self.run_cli("plan", "--target-dir", str(root), "--mock")

            self.assertEqual(result.returncode, 0, result.stderr)
            run_dir = self.extract_run_dir(result.stdout)
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["counts"]["files_scanned"], 1)
            plan = json.loads((run_dir / "plan.json").read_text(encoding="utf-8"))
            self.assertEqual(plan["operations"][0]["source"], "a/b/c/d/e/deep.txt")

    def test_fast_lane_filters_extensions_size_and_candidate_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "messy"
            root.mkdir()
            base_time = 1_710_000_000
            for index in range(32):
                path = root / f"note-{index:02d}.txt"
                path.write_text("note", encoding="utf-8")
                os.utime(path, (base_time + index, base_time + index))

            unknown = root / "unknown.bin"
            unknown.write_bytes(b"bin")
            os.utime(unknown, (base_time + 100, base_time + 100))

            huge = root / "huge.pdf"
            with huge.open("wb") as handle:
                handle.truncate(100 * 1024 * 1024 + 1)
            os.utime(huge, (base_time + 101, base_time + 101))

            result = self.run_cli("plan", "--target-dir", str(root), "--fast-lane", "--mock")

            self.assertEqual(result.returncode, 0, result.stderr)
            run_dir = self.extract_run_dir(result.stdout)
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(manifest["fast_lane"])
            self.assertEqual(manifest["counts"]["files_scanned"], 34)
            self.assertEqual(manifest["counts"]["files_considered"], 30)
            self.assertEqual(manifest["counts"]["planned_moves"], 30)
            self.assertEqual(manifest["counts"]["skipped"], 4)
            self.assertEqual(manifest["counts"]["blocked"], 0)

    def test_run_fast_lane_yes_uses_same_manifest_for_apply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "messy"
            root.mkdir()
            (root / "receipt.pdf").write_text("finance", encoding="utf-8")

            result = self.run_cli("run", "--target-dir", str(root), "--fast-lane", "--yes", "--mock")

            self.assertEqual(result.returncode, 0, result.stderr)
            run_dir = self.extract_run_dir(result.stdout)
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            apply_result = json.loads((run_dir / "apply_result.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["counts"]["planned_moves"], 1)
            self.assertEqual(manifest["counts"]["applied_moves"], 1)
            self.assertEqual(apply_result["applied_moves"], 1)
            self.assertTrue((root / "documents" / "finance" / "receipt.pdf").exists())

    def test_run_fast_lane_interactive_view_then_apply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "messy"
            output_root = Path(tmp) / "artifacts"
            root.mkdir()
            (root / "meeting.txt").write_text("notes", encoding="utf-8")

            stdin = FakeTTY("v\na\n")
            stdout = FakeTTY()
            stderr = io.StringIO()

            exit_code = main(
                [
                    "run",
                    "--target-dir",
                    str(root),
                    "--output-dir",
                    str(output_root),
                    "--fast-lane",
                    "--mock",
                ],
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
            )

            self.assertEqual(exit_code, 0, stderr.getvalue())
            output = stdout.getvalue()
            self.assertIn("Fast lane plan ready", output)
            self.assertIn("Plan details:", output)
            self.assertIn("status=success", output)
            self.assertTrue((root / "documents" / "notes" / "meeting.txt").exists())

    def test_apply_manifest_does_not_scan_or_initialize_llm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "messy"
            output_root = Path(tmp) / "artifacts"
            root.mkdir()
            (root / "notes.txt").write_text("notes", encoding="utf-8")

            plan_result = self.run_cli(
                "plan",
                "--target-dir",
                str(root),
                "--output-dir",
                str(output_root),
                "--mock",
            )
            self.assertEqual(plan_result.returncode, 0, plan_result.stderr)
            manifest_path = self.extract_run_dir(plan_result.stdout) / "manifest.json"

            with mock.patch("dirorganizer.cli.scan_directory", side_effect=AssertionError("scan should not run")), mock.patch(
                "dirorganizer.cli.build_client", side_effect=AssertionError("LLM client should not init")
            ):
                stdout = io.StringIO()
                stderr = io.StringIO()
                exit_code = main(
                    ["apply", "--manifest", str(manifest_path)],
                    stdout=stdout,
                    stderr=stderr,
                )

            self.assertEqual(exit_code, 0, stderr.getvalue())
            self.assertTrue((root / "documents" / "notes" / "notes.txt").exists())

    def test_module_entrypoint_matches_console_script_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "messy"
            root.mkdir()
            (root / "notes.txt").write_text("notes", encoding="utf-8")

            result = self.run_module("dirorganizer", "plan", "--target-dir", str(root), "--mock")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("mode=plan", result.stdout)

    def test_llm_client_retries_without_response_format_after_model_crash(self) -> None:
        client = LocalLLMClient(base_url="http://example.invalid", model="test", api_key="x")
        calls: list[dict[str, object]] = []

        def fake_request(payload: dict[str, object]) -> dict[str, object]:
            calls.append(payload)
            if "response_format" in payload:
                raise RuntimeError(
                    'HTTP 400: {"error":"The model has crashed without additional information. (Exit code: null)"}'
                )
            return {"summary": "ok", "operations": []}

        client._request = fake_request  # type: ignore[method-assign]
        result = client.chat_json("system", "user")

        self.assertEqual(result["summary"], "ok")
        self.assertEqual(len(calls), 2)
        self.assertIn("response_format", calls[0])
        self.assertNotIn("response_format", calls[1])

    def test_llm_client_builds_responses_endpoint_and_parses_output(self) -> None:
        client = LocalLLMClient(
            base_url="http://127.0.0.1:1234/v1",
            model="test",
            api_key="x",
            api_mode="responses",
        )

        self.assertEqual(client._build_endpoint(), "http://127.0.0.1:1234/v1/responses")
        self.assertEqual(
            client._extract_content(
                {
                    "output": [
                        {
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": '{"summary":"ok","operations":[]}',
                                }
                            ]
                        }
                    ]
                }
            ),
            '{"summary":"ok","operations":[]}',
        )

    def test_llm_client_keeps_full_endpoint_url_without_double_append(self) -> None:
        client = LocalLLMClient(
            base_url="http://127.0.0.1:1234/v1/chat/completions",
            model="test",
            api_key="x",
        )

        self.assertEqual(client._build_endpoint(), "http://127.0.0.1:1234/v1/chat/completions")

    def test_llm_client_merges_extra_body_into_chat_payload(self) -> None:
        client = LocalLLMClient(
            base_url="http://127.0.0.1:1234/v1",
            model="test",
            api_key="x",
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )

        payload = client._build_payload_attempts(system_prompt="system", user_prompt="user")[0]

        self.assertEqual(payload["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(payload["response_format"], {"type": "json_object"})

    def test_planner_manifest_section_redacts_url_and_api_key(self) -> None:
        client = LocalLLMClient(
            base_url="http://127.0.0.1:1234/v1/chat/completions",
            model="local-model",
            api_key="super-secret",
            api_mode="chat_completions",
            request_count=7,
        )

        planner = _build_planner_manifest_section(client=client, batch_size=15, mock=False)

        self.assertEqual(planner["provider"], "local")
        self.assertEqual(planner["host"], "127.0.0.1")
        self.assertEqual(planner["llm_request_count"], 7)
        self.assertNotIn("api_key", planner)
        self.assertNotIn("base_url", planner)

    def test_build_plan_splits_failed_batches_and_falls_back_per_file(self) -> None:
        files = [
            FileRecord(
                relative_path="receipt.pdf",
                parent_dir=".",
                name="receipt.pdf",
                extension=".pdf",
                size_bytes=10,
                modified_at="2026-03-13T00:00:00+00:00",
            ),
            FileRecord(
                relative_path="notes.txt",
                parent_dir=".",
                name="notes.txt",
                extension=".txt",
                size_bytes=10,
                modified_at="2026-03-13T00:00:00+00:00",
            ),
        ]

        class StubClient:
            def chat_json(self, system_prompt: str, user_prompt: str) -> dict[str, object]:
                payload = json.loads(user_prompt)
                batch_files = payload["files"]
                if len(batch_files) > 1:
                    raise RuntimeError("HTTP 400: model crashed")
                return {
                    "summary": f"single {batch_files[0]['name']}",
                    "operations": [
                        {
                            "source": batch_files[0]["relative_path"],
                            "destination_dir": "documents/notes",
                            "new_name": batch_files[0]["name"],
                            "reason": "single file ok",
                            "confidence": 0.9,
                        }
                    ],
                }

        plan = build_plan(
            root=Path("/tmp/example"),
            files=files,
            existing_dirs=[],
            rules={},
            client=StubClient(),  # type: ignore[arg-type]
            batch_size=20,
            min_confidence=0.6,
            mock=False,
            scan_truncated=False,
        )

        self.assertEqual(len(plan.operations), 2)
        self.assertTrue(any("split 2 files" in warning for warning in plan.warnings))
        operations = {operation.source: operation for operation in plan.operations}
        self.assertEqual(operations["receipt.pdf"].target_path, "documents/notes/receipt.pdf")
        self.assertEqual(operations["notes.txt"].target_path, "documents/notes/notes.txt")

    def test_build_plan_reports_live_progress_messages(self) -> None:
        files = [
            FileRecord(
                relative_path="receipt.pdf",
                parent_dir=".",
                name="receipt.pdf",
                extension=".pdf",
                size_bytes=10,
                modified_at="2026-03-13T00:00:00+00:00",
            ),
            FileRecord(
                relative_path="notes.txt",
                parent_dir=".",
                name="notes.txt",
                extension=".txt",
                size_bytes=10,
                modified_at="2026-03-13T00:00:00+00:00",
            ),
        ]

        class StubClient:
            def chat_json(self, system_prompt: str, user_prompt: str) -> dict[str, object]:
                payload = json.loads(user_prompt)
                batch_files = payload["files"]
                if len(batch_files) > 1:
                    raise RuntimeError("HTTP 400: model crashed")
                return {
                    "summary": f"single {batch_files[0]['name']}",
                    "operations": [
                        {
                            "source": batch_files[0]["relative_path"],
                            "destination_dir": "documents/notes",
                            "new_name": batch_files[0]["name"],
                            "reason": "single file ok",
                            "confidence": 0.9,
                        }
                    ],
                }

        messages: list[str] = []
        build_plan(
            root=Path("/tmp/example"),
            files=files,
            existing_dirs=[],
            rules={},
            client=StubClient(),  # type: ignore[arg-type]
            batch_size=20,
            min_confidence=0.6,
            mock=False,
            scan_truncated=False,
            progress_callback=messages.append,
        )

        self.assertIn("planning 2 files in 1 batches (batch_size=20)", messages)
        self.assertTrue(any("batch 1/1 (files 1-2 of 2)" in message for message in messages))
        self.assertTrue(any("request failed for 2 files" in message for message in messages))
        self.assertTrue(any("split into 1 and 1 files" in message for message in messages))
        self.assertTrue(any("completed batch 1/1" in message and "processed 2/2 files" in message for message in messages))

    def test_plan_command_writes_progress_to_stderr_for_live_planner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "messy"
            output_root = Path(tmp) / "artifacts"
            root.mkdir()
            (root / "notes.txt").write_text("notes", encoding="utf-8")

            class StubClient:
                request_count = 0
                request_seconds = 0.0
                api_mode = "chat_completions"
                model = "local-model"
                base_url = "http://127.0.0.1:1234/v1"

                def host(self) -> str:
                    return "127.0.0.1"

                def chat_json(self, system_prompt: str, user_prompt: str) -> dict[str, object]:
                    self.request_count += 1
                    payload = json.loads(user_prompt)
                    batch_file = payload["files"][0]
                    return {
                        "summary": "ok",
                        "operations": [
                            {
                                "source": batch_file["relative_path"],
                                "destination_dir": "documents/notes",
                                "new_name": batch_file["name"],
                                "reason": "ok",
                                "confidence": 0.95,
                            }
                        ],
                    }

            stdout = io.StringIO()
            stderr = io.StringIO()
            with mock.patch("dirorganizer.cli.build_client", return_value=StubClient()):
                exit_code = main(
                    [
                        "plan",
                        "--target-dir",
                        str(root),
                        "--output-dir",
                        str(output_root),
                        "--model",
                        "local-model",
                        "--base-url",
                        "http://127.0.0.1:1234/v1",
                        "--api-key",
                        "token",
                    ],
                    stdout=stdout,
                    stderr=stderr,
                )

            self.assertEqual(exit_code, 0, stderr.getvalue())
            self.assertIn("[planner] planning 1 files in 1 batches (batch_size=20)", stderr.getvalue())
            self.assertIn("[planner] batch 1/1 (files 1-1 of 1)", stderr.getvalue())
            self.assertIn("[planner] completed batch 1/1 (files 1-1 of 1)", stderr.getvalue())
            self.assertIn("processed 1/1 files", stderr.getvalue())
            self.assertIn("run_dir=", stdout.getvalue())

    def extract_run_dir(self, stdout: str) -> Path:
        for line in stdout.splitlines():
            if line.startswith("[RESULT] "):
                for token in line.split():
                    if token.startswith("run_dir="):
                        return Path(token.split("=", 1)[1])
        raise AssertionError(f"run_dir not found in stdout: {stdout}")

    def run_cli(self, *args: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.pop("DIRECTORY_ORGANIZER_TARGET_DIR", None)
        env.pop("DIRECTORY_ORGANIZER_OUTPUT_DIR", None)
        if extra_env:
            env.update(extra_env)
        derived_output_dir = self._derive_output_dir(args, env)
        if derived_output_dir is not None and "DIRECTORY_ORGANIZER_OUTPUT_DIR" not in env:
            env["DIRECTORY_ORGANIZER_OUTPUT_DIR"] = str(derived_output_dir)
        return subprocess.run(
            [sys.executable, str(SCRIPT_PATH), *args],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )

    def run_module(self, module_name: str, *args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.pop("DIRECTORY_ORGANIZER_TARGET_DIR", None)
        env.pop("DIRECTORY_ORGANIZER_OUTPUT_DIR", None)
        derived_output_dir = self._derive_output_dir(args, env)
        if derived_output_dir is not None and "DIRECTORY_ORGANIZER_OUTPUT_DIR" not in env:
            env["DIRECTORY_ORGANIZER_OUTPUT_DIR"] = str(derived_output_dir)
        return subprocess.run(
            [sys.executable, "-m", module_name, *args],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )

    def _derive_output_dir(self, args: tuple[str, ...], env: dict[str, str]) -> Path | None:
        return REPO_ROOT / ".dirorganizer-test-runs"


if __name__ == "__main__":
    unittest.main()
