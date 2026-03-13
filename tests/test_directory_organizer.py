from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dirorganizer.cli import main
from dirorganizer.env_loader import load_dotenv
from dirorganizer.llm_client import LocalLLMClient
from dirorganizer.models import FileRecord
from dirorganizer.planner import build_plan


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
        for index, token in enumerate(args):
            if token in {"--target-dir", "--source"} and index + 1 < len(args):
                raw_target = os.path.expandvars(args[index + 1])
                if "$" in raw_target:
                    return None
                return Path(raw_target).expanduser().resolve() / ".dirorganizer-test-runs"
            if token.startswith("--target-dir=") or token.startswith("--source="):
                raw_target = os.path.expandvars(token.split("=", 1)[1])
                if "$" in raw_target:
                    return None
                return Path(raw_target).expanduser().resolve() / ".dirorganizer-test-runs"
        return None


if __name__ == "__main__":
    unittest.main()
