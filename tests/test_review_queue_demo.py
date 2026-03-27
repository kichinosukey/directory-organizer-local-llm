from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from dirorganizer.cli import main
from dirorganizer.demo_data import prepare_demo_dataset
from dirorganizer.gui.app import ReviewDemoConfig, ReviewQueueService
from dirorganizer.review_api import apply_review_session, build_review_session

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6 import QtWidgets
except ModuleNotFoundError:  # pragma: no cover
    QtWidgets = None  # type: ignore[assignment]
else:
    from dirorganizer.gui.app import ReviewQueueWindow


class ReviewQueueApiTests(unittest.TestCase):
    def test_build_review_session_matches_cli_fast_lane_manifest_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "messy"
            output_a = Path(tmp) / "shared"
            output_b = Path(tmp) / "cli"
            root.mkdir()
            (root / "receipt-mar.pdf").write_text("finance", encoding="utf-8")
            (root / "scan_001.jpg").write_bytes(b"jpg")

            shared = build_review_session(target_dir=root, output_dir=output_a, mock=True)

            stdout = []
            stderr = []
            exit_code = main(
                [
                    "plan",
                    "--target-dir",
                    str(root),
                    "--output-dir",
                    str(output_b),
                    "--fast-lane",
                    "--preset",
                    "finance-receipts",
                    "--mock",
                ],
                stdout=_Collector(stdout),
                stderr=_Collector(stderr),
            )

            self.assertEqual(exit_code, 0, "".join(stderr))
            run_dir = _extract_run_dir("".join(stdout))
            shared_manifest = json.loads(shared.manifest_path.read_text(encoding="utf-8"))
            cli_manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(shared_manifest["counts"], cli_manifest["counts"])
            self.assertEqual(shared_manifest["preset"], "finance-receipts")
            self.assertTrue(shared_manifest["fast_lane"])

    def test_apply_review_session_preserves_undo_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "messy"
            output_root = Path(tmp) / "shared"
            root.mkdir()
            (root / "receipt-mar.pdf").write_text("finance", encoding="utf-8")

            session = build_review_session(target_dir=root, output_dir=output_root, mock=True)
            applied = apply_review_session(session)

            undo_manifest = json.loads((applied.run_dir / "undo_manifest.json").read_text(encoding="utf-8"))
            apply_result = json.loads((applied.run_dir / "apply_result.json").read_text(encoding="utf-8"))
            self.assertEqual(apply_result["applied_moves"], 1)
            self.assertEqual(undo_manifest["operations"][0]["target_path"], "receipt-mar.pdf")
            self.assertTrue((root / "documents" / "finance" / "receipt-mar.pdf").exists())
            self.assertEqual(applied.moved_count, 1)

    def test_apply_review_session_marks_missing_files_as_partial_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "messy"
            output_root = Path(tmp) / "shared"
            root.mkdir()
            missing = root / "receipt-mar.pdf"
            missing.write_text("finance", encoding="utf-8")

            session = build_review_session(target_dir=root, output_dir=output_root, mock=True)
            missing.unlink()
            applied = apply_review_session(session)

            self.assertEqual(applied.missing_count, 1)
            self.assertIn("確認が必要", applied.apply_summary or "")
            self.assertEqual(applied.items[0].state, "missing")

    def test_review_queue_service_reuses_short_lived_cache(self) -> None:
        calls = 0

        def fake_build_session(**_: object):
            nonlocal calls
            calls += 1
            return _fake_session()

        service = ReviewQueueService(
            ReviewDemoConfig(
                target_dir=Path("/tmp/example"),
                output_dir=None,
                model=None,
                base_url=None,
                api_key=None,
                api_mode=None,
                extra_body_json=None,
                mock=True,
            ),
            build_session=fake_build_session,
        )

        first = service.scan()
        second = service.scan()
        self.assertEqual(calls, 1)
        self.assertIs(first, second)

    def test_prepare_demo_dataset_builds_mixed_review_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dataset = prepare_demo_dataset(output_root=Path(tmp))
            session = build_review_session(target_dir=dataset.source_dir, mock=True)

            states = {item.state for item in session.items}
            self.assertIn("safe", states)
            self.assertIn("blocked", states)
            self.assertIn("review", states)

    def test_prepare_demo_dataset_apply_preserves_blocked_and_moves_safe_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dataset = prepare_demo_dataset(output_root=Path(tmp))
            session = build_review_session(target_dir=dataset.source_dir, mock=True)

            blocked_before = [item for item in session.items if item.state == "blocked"]
            self.assertTrue(blocked_before)

            applied = apply_review_session(session)
            moved = [item for item in applied.items if item.state == "moved"]
            blocked_after = [item for item in applied.items if item.state == "blocked"]

            self.assertTrue(moved)
            self.assertTrue(blocked_after)
            self.assertTrue((dataset.source_dir / "documents" / "finance").exists())


@unittest.skipIf(QtWidgets is None, "PySide6 is not installed")
class ReviewQueueGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_scan_success_renders_queue_and_safe_apply_label(self) -> None:
        service = ReviewQueueService(
            ReviewDemoConfig(Path("/tmp/example"), None, None, None, None, None, None, True),
            build_session=lambda **_: _fake_session(),
        )
        window = ReviewQueueWindow(service)
        window.show()
        self.app.processEvents()
        window._handle_scan_success(_fake_session())
        self.app.processEvents()

        self.assertEqual(window.list_widget.count(), 2)
        self.assertEqual(window.apply_button.text(), "1件を安全に整理")
        self.assertIn("経理候補", window._last_announcement)

    def test_empty_state_is_reassuring(self) -> None:
        service = ReviewQueueService(
            ReviewDemoConfig(Path("/tmp/example"), None, None, None, None, None, None, True),
            build_session=lambda **_: _fake_empty_session(),
        )
        window = ReviewQueueWindow(service)
        window.show()
        self.app.processEvents()
        window._handle_scan_success(_fake_empty_session())
        self.app.processEvents()

        self.assertTrue(window.empty_label.isVisible())
        self.assertIn("急ぎの経理書類", window.empty_label.text())
        self.assertEqual(window.list_widget.count(), 0)

    def test_apply_partial_failure_shows_summary_and_row_status(self) -> None:
        service = ReviewQueueService(
            ReviewDemoConfig(Path("/tmp/example"), None, None, None, None, None, None, True),
            build_session=lambda **_: _fake_session(),
            apply_session=lambda _: _fake_partial_session(),
        )
        window = ReviewQueueWindow(service)
        window.show()
        self.app.processEvents()
        window._current_session = _fake_session()
        window._handle_apply_success(_fake_partial_session())
        self.app.processEvents()

        self.assertTrue(window.summary_band.isVisible())
        self.assertIn("確認が必要", window.summary_band.text())
        first_widget = window.list_widget.itemWidget(window.list_widget.item(0))
        self.assertIsNotNone(first_widget)
        self.assertIn(window._last_announcement, window.summary_band.text())

    def test_scan_downloads_ignores_duplicate_scan_requests(self) -> None:
        service = ReviewQueueService(
            ReviewDemoConfig(Path("/tmp/example"), None, None, None, None, None, None, True),
            build_session=lambda **_: _fake_session(),
        )
        window = ReviewQueueWindow(service)
        window.show()
        self.app.processEvents()
        window._scan_thread = object()  # type: ignore[assignment]

        window.scan_downloads()
        self.app.processEvents()

        self.assertEqual(window._last_announcement, "スキャンはすでに実行中です。")

    def test_error_state_is_clear_when_scan_fails(self) -> None:
        service = ReviewQueueService(
            ReviewDemoConfig(Path("/tmp/example"), None, None, None, None, None, None, True),
            build_session=lambda **_: _fake_session(),
        )
        window = ReviewQueueWindow(service)
        window.show()
        self.app.processEvents()

        window._handle_task_error("候補を確認できませんでした。")
        self.app.processEvents()

        self.assertTrue(window.summary_band.isVisible())
        self.assertIn("候補を確認できませんでした。", window.summary_band.text())
        self.assertIn("候補を確認できませんでした。", window._last_announcement)

    def test_idle_state_mentions_target_directory(self) -> None:
        target_dir = Path("/tmp/demo-source")
        service = ReviewQueueService(
            ReviewDemoConfig(target_dir, None, None, None, None, None, None, True),
            build_session=lambda **_: _fake_session(target_dir=target_dir),
        )
        window = ReviewQueueWindow(service)
        window.show()
        self.app.processEvents()

        self.assertIn("demo-source", window.subtitle_label.text())
        self.assertIn("まだスキャンしていません", window.empty_label.text())

    def test_target_dir_aware_copy_updates_after_scan(self) -> None:
        target_dir = Path("/tmp/demo-source")
        service = ReviewQueueService(
            ReviewDemoConfig(target_dir, None, None, None, None, None, None, True),
            build_session=lambda **_: _fake_session(target_dir=target_dir),
        )
        window = ReviewQueueWindow(service)
        window.show()
        self.app.processEvents()
        window._handle_scan_success(_fake_session(target_dir=target_dir))
        self.app.processEvents()

        self.assertIn("demo-source", window.subtitle_label.text())
        self.assertIn("整理レビュー", window.windowTitle())


class _Collector:
    def __init__(self, chunks: list[str]) -> None:
        self._chunks = chunks

    def write(self, value: str) -> int:
        self._chunks.append(value)
        return len(value)

    def flush(self) -> None:  # pragma: no cover
        return None


def _extract_run_dir(stdout: str) -> Path:
    for line in stdout.splitlines():
        if line.startswith("[RESULT] "):
            for token in line.split():
                if token.startswith("run_dir="):
                    return Path(token.split("=", 1)[1])
    raise AssertionError(f"run_dir not found in stdout: {stdout}")


def _fake_session(*, target_dir: Path | None = None):
    from dirorganizer.review_api import ReviewItem, ReviewSession

    resolved_target_dir = target_dir or Path("/tmp/example")
    return ReviewSession(
        target_dir=resolved_target_dir,
        run_dir=Path("/tmp/example/run"),
        manifest_path=Path("/tmp/example/run/manifest.json"),
        created_at="2026-03-27T18:00:00+09:00",
        summary="demo",
        warnings=(),
        items=(
            ReviewItem(
                source="receipt-mar.pdf",
                target_path="documents/finance/invoices/receipt-mar.pdf",
                destination_dir="documents/finance/invoices",
                confidence=0.92,
                reason="finance",
                can_apply=True,
                issues=(),
                state="safe",
                state_detail="安全に整理できます",
            ),
            ReviewItem(
                source="scan_8841.jpg",
                target_path="scan_8841.jpg",
                destination_dir="",
                confidence=0.0,
                reason="collision",
                can_apply=False,
                issues=("target already exists on disk",),
                state="blocked",
                state_detail="整理先に同名ファイルがすでにあります",
            ),
        ),
        safe_count=1,
        blocked_count=1,
        review_count=0,
        moved_count=0,
        missing_count=0,
        skipped_count=0,
        apply_summary=None,
    )


def _fake_empty_session():
    from dirorganizer.review_api import ReviewSession

    return ReviewSession(
        target_dir=Path("/tmp/example"),
        run_dir=Path("/tmp/example/run"),
        manifest_path=Path("/tmp/example/run/manifest.json"),
        created_at="2026-03-27T18:00:00+09:00",
        summary="empty",
        warnings=(),
        items=(),
        safe_count=0,
        blocked_count=0,
        review_count=0,
        moved_count=0,
        missing_count=0,
        skipped_count=0,
        apply_summary=None,
    )


def _fake_partial_session():
    from dirorganizer.review_api import ReviewItem, ReviewSession

    return ReviewSession(
        target_dir=Path("/tmp/example"),
        run_dir=Path("/tmp/example/run"),
        manifest_path=Path("/tmp/example/run/manifest.json"),
        created_at="2026-03-27T18:00:00+09:00",
        summary="partial",
        warnings=(),
        items=(
            ReviewItem(
                source="receipt-mar.pdf",
                target_path="documents/finance/invoices/receipt-mar.pdf",
                destination_dir="documents/finance/invoices",
                confidence=0.92,
                reason="finance",
                can_apply=True,
                issues=(),
                state="moved",
                state_detail="安全に整理済み",
            ),
            ReviewItem(
                source="scan_8841.jpg",
                target_path="scan_8841.jpg",
                destination_dir="",
                confidence=0.0,
                reason="missing",
                can_apply=False,
                issues=("source file no longer exists",),
                state="missing",
                state_detail="適用前に見つからなくなりました",
            ),
        ),
        safe_count=0,
        blocked_count=0,
        review_count=0,
        moved_count=1,
        missing_count=1,
        skipped_count=0,
        apply_summary="1件を安全に整理しました。1件は確認が必要です。",
    )
