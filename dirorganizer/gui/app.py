from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from dirorganizer.review_api import (
    DEFAULT_DOWNLOADS_DIR,
    ReviewItem,
    ReviewSession,
    ReviewSessionError,
    apply_review_session,
    build_review_session,
)

try:
    from PySide6 import QtCore, QtWidgets
except ModuleNotFoundError as exc:  # pragma: no cover
    QtCore = QtWidgets = None  # type: ignore[assignment]
    _PYSIDE_IMPORT_ERROR = exc
else:
    _PYSIDE_IMPORT_ERROR = None


COPY = {
    "window_title": "整理レビュー",
    "title": "整理レビュー",
    "subtitle": "「{target}」から経理書類らしい候補を見つけます。",
    "scan_button": "スキャン",
    "apply_button": "{count}件を安全に整理",
    "summary_scanning": "候補を確認しています...",
    "summary_applying": "安全な候補を整理しています...",
    "why_band": "Why this matters\n埋もれた領収書や請求書で月次の締めが遅れるのを防ぎます。",
    "idle_state": "まだスキャンしていません。下のボタンから整理候補を確認できます。",
    "empty_state": "いまは急ぎの経理書類は見つかりませんでした。",
    "empty_error_hint": "ローカルモデルかフォルダ権限を確認して、もう一度試してください。",
    "announcement_scan_found": "{count}件の経理候補が見つかりました。",
    "announcement_scan_empty": "いまは急ぎの経理書類は見つかりませんでした。",
    "announcement_scan_busy": "スキャンはすでに実行中です。",
    "announcement_apply_busy": "整理の適用はすでに実行中です。",
    "error_scan": "候補を確認できませんでした。",
    "footer_idle": "対象フォルダ: {target}",
    "footer_last_scan": "最終スキャン {created_at}",
    "footer_safe": "{count}件 安全",
    "footer_blocked": "{count}件 要確認",
    "footer_undo": "Undo 利用可",
    "footer_moved": "{count}件 整理済み",
    "footer_missing": "{count}件 消失",
    "footer_skipped": "{count}件 保留",
    "state_safe": "安全",
    "state_review": "確認",
    "state_blocked": "保留",
    "state_moved": "整理済み",
    "state_missing": "消失",
    "state_skipped": "保留",
    "pyside_error": "PySide6 が入っていません。`uv pip install --python .venv/bin/python PySide6` を実行してから起動してください。",
    "argparse_description": "整理レビューのデモ画面を起動します。",
    "argparse_target_dir": "スキャン対象ディレクトリ",
    "argparse_output_dir": "成果物の保存先ルート",
    "argparse_model": "LOCAL_LLM_MODEL を上書きします",
    "argparse_base_url": "LOCAL_LLM_BASE_URL を上書きします",
    "argparse_api_key": "LOCAL_LLM_API_KEY を上書きします",
    "argparse_extra_body_json": "追加の planner payload JSON",
    "argparse_mock": "LLM の代わりにヒューリスティックを使います",
}


def _copy(key: str, **values: object) -> str:
    return COPY[key].format(**values)


def _display_target_label(target_dir: Path) -> str:
    return target_dir.name or str(target_dir)


@dataclass(frozen=True)
class ReviewDemoConfig:
    target_dir: Path
    output_dir: Path | None
    model: str | None
    base_url: str | None
    api_key: str | None
    api_mode: str | None
    extra_body_json: str | None
    mock: bool


class ReviewQueueService:
    def __init__(
        self,
        config: ReviewDemoConfig,
        *,
        cache_ttl_seconds: float = 8.0,
        build_session: Callable[..., ReviewSession] = build_review_session,
        apply_session: Callable[[ReviewSession], ReviewSession] = apply_review_session,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.cache_ttl_seconds = cache_ttl_seconds
        self._build_session = build_session
        self._apply_session = apply_session
        self._clock = clock
        self._cached_session: ReviewSession | None = None
        self._cached_at = 0.0

    def scan(self) -> ReviewSession:
        if self._cached_session is not None and (self._clock() - self._cached_at) < self.cache_ttl_seconds:
            return self._cached_session
        session = self._build_session(
            target_dir=self.config.target_dir,
            output_dir=self.config.output_dir,
            model=self.config.model,
            base_url=self.config.base_url,
            api_key=self.config.api_key,
            api_mode=self.config.api_mode,
            extra_body_json=self.config.extra_body_json,
            mock=self.config.mock,
        )
        self._cached_session = session
        self._cached_at = self._clock()
        return session

    def apply_safe_moves(self, session: ReviewSession) -> ReviewSession:
        updated = self._apply_session(session)
        self._cached_session = updated
        self._cached_at = self._clock()
        return updated

    def invalidate_cache(self) -> None:
        self._cached_session = None
        self._cached_at = 0.0


if QtCore is not None:
    class ReviewWorker(QtCore.QObject):
        finished = QtCore.Signal(object)
        failed = QtCore.Signal(str)

        def __init__(self, task: Callable[[], ReviewSession]) -> None:
            super().__init__()
            self._task = task

        @QtCore.Slot()
        def run(self) -> None:
            try:
                session = self._task()
            except ReviewSessionError as exc:
                self.failed.emit(str(exc))
            except Exception as exc:  # noqa: BLE001
                self.failed.emit(str(exc) or exc.__class__.__name__)
            else:
                self.finished.emit(session)


    class ReviewItemWidget(QtWidgets.QFrame):
        def __init__(self, item: ReviewItem) -> None:
            super().__init__()
            self.setObjectName("reviewRow")
            self._title = QtWidgets.QLabel(item.filename)
            self._title.setObjectName("rowTitle")
            self._title.setWordWrap(True)
            self._title.setAccessibleName(item.filename)

            self._status = QtWidgets.QLabel(_display_state(item.state))
            self._status.setObjectName(f"status-{item.state}")
            self._status.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            self._status.setMinimumWidth(70)

            confidence_text = f"{item.confidence:.2f}" if item.confidence > 0 else "—"
            self._meta = QtWidgets.QLabel(f"{item.destination_label} • {item.state_detail} • {confidence_text}")
            self._meta.setObjectName("rowMeta")
            self._meta.setWordWrap(True)

            top = QtWidgets.QHBoxLayout()
            top.setContentsMargins(0, 0, 0, 0)
            top.setSpacing(12)
            top.addWidget(self._title, 1)
            top.addWidget(self._status, 0)

            layout = QtWidgets.QVBoxLayout(self)
            layout.setContentsMargins(16, 12, 16, 12)
            layout.setSpacing(6)
            layout.addLayout(top)
            layout.addWidget(self._meta)


    class ReviewQueueWindow(QtWidgets.QMainWindow):
        def __init__(self, service: ReviewQueueService) -> None:
            super().__init__()
            self._service = service
            self._scan_thread: QtCore.QThread | None = None
            self._apply_thread: QtCore.QThread | None = None
            self._scan_worker: ReviewWorker | None = None
            self._apply_worker: ReviewWorker | None = None
            self._current_session: ReviewSession | None = None
            self._last_announcement = ""
            self.setWindowTitle(_copy("window_title"))
            self.resize(920, 640)
            self._build_ui()
            self._apply_styles()
            self._render_idle()

        def _build_ui(self) -> None:
            central = QtWidgets.QWidget()
            self.setCentralWidget(central)
            root = QtWidgets.QVBoxLayout(central)
            root.setContentsMargins(24, 24, 24, 24)
            root.setSpacing(16)

            self.title_label = QtWidgets.QLabel(_copy("title"))
            self.title_label.setObjectName("title")
            self.subtitle_label = QtWidgets.QLabel("")
            self.subtitle_label.setObjectName("subtitle")
            self.subtitle_label.setWordWrap(True)

            self.scan_button = QtWidgets.QPushButton(_copy("scan_button"))
            self.scan_button.clicked.connect(self.scan_downloads)
            self.scan_button.setAutoDefault(True)

            self.apply_button = QtWidgets.QPushButton(_copy("apply_button", count=0))
            self.apply_button.clicked.connect(self.apply_safe_moves)
            self.apply_button.setEnabled(False)

            button_row = QtWidgets.QHBoxLayout()
            button_row.addWidget(self.scan_button, 0)
            button_row.addStretch(1)
            button_row.addWidget(self.apply_button, 0)

            self.summary_band = QtWidgets.QLabel("")
            self.summary_band.setObjectName("summaryBand")
            self.summary_band.setWordWrap(True)
            self.summary_band.hide()

            self.why_band = QtWidgets.QLabel(_copy("why_band"))
            self.why_band.setObjectName("whyBand")
            self.why_band.setWordWrap(True)

            self.list_widget = QtWidgets.QListWidget()
            self.list_widget.setObjectName("reviewList")
            self.list_widget.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)

            self.empty_label = QtWidgets.QLabel("")
            self.empty_label.setObjectName("emptyState")
            self.empty_label.setWordWrap(True)
            self.empty_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            self.empty_label.hide()

            self.footer_label = QtWidgets.QLabel("")
            self.footer_label.setObjectName("footer")
            self.footer_label.setWordWrap(True)

            self.sr_status_label = QtWidgets.QLabel("")
            self.sr_status_label.setObjectName("srStatus")
            self.sr_status_label.setAccessibleName("Status updates")
            self.sr_status_label.hide()

            root.addWidget(self.title_label)
            root.addWidget(self.subtitle_label)
            root.addLayout(button_row)
            root.addWidget(self.summary_band)
            root.addWidget(self.why_band)
            root.addWidget(self.list_widget, 1)
            root.addWidget(self.empty_label)
            root.addWidget(self.footer_label)
            root.addWidget(self.sr_status_label)

        def _apply_styles(self) -> None:
            self.setStyleSheet(
                """
                QMainWindow, QWidget {
                    background: #f6f4ef;
                    color: #1f2328;
                    font-family: "Hiragino Sans", "Helvetica Neue", "Aptos", sans-serif;
                    font-size: 14px;
                }
                QLabel#title { font-size: 28px; font-weight: 650; }
                QLabel#subtitle { color: #4c5560; margin-bottom: 4px; }
                QPushButton {
                    min-height: 44px;
                    padding: 10px 16px;
                    border-radius: 10px;
                    border: 1px solid #c8c4bb;
                    background: #fffdf8;
                }
                QPushButton:disabled {
                    color: #8b949e;
                    background: #efebe2;
                }
                QLabel#summaryBand, QLabel#whyBand, QListWidget#reviewList {
                    border: 1px solid #d8d2c7;
                    border-radius: 10px;
                    background: #fffdf8;
                }
                QLabel#summaryBand {
                    padding: 12px 14px;
                    background: #f0f5ef;
                    border-color: #c3d2c0;
                }
                QLabel#whyBand {
                    padding: 12px 14px;
                    color: #38424c;
                }
                QListWidget#reviewList {
                    padding: 4px;
                }
                QFrame#reviewRow {
                    border-bottom: 1px solid #ebe5da;
                }
                QLabel#rowTitle { font-size: 15px; font-weight: 600; }
                QLabel#rowMeta { color: #5b6672; }
                QLabel#status-safe, QLabel#status-moved {
                    color: #245b32;
                    background: #dfeee1;
                    padding: 4px 8px;
                    border-radius: 8px;
                }
                QLabel#status-review {
                    color: #6b4d16;
                    background: #f5ead1;
                    padding: 4px 8px;
                    border-radius: 8px;
                }
                QLabel#status-blocked, QLabel#status-missing, QLabel#status-skipped {
                    color: #7a2d2d;
                    background: #f5dddd;
                    padding: 4px 8px;
                    border-radius: 8px;
                }
                QLabel#emptyState, QLabel#footer {
                    color: #4c5560;
                }
                """
            )

        def scan_downloads(self) -> None:
            if self._scan_thread is not None:
                self._announce(_copy("announcement_scan_busy"))
                return
            self._set_busy_state(scanning=True)
            self.summary_band.hide()
            self.empty_label.hide()
            self._run_task(
                task=self._service.scan,
                kind="scan",
                on_success=self._handle_scan_success,
            )

        def apply_safe_moves(self) -> None:
            if self._current_session is None or self._current_session.safe_count == 0:
                return
            if self._apply_thread is not None:
                self._announce(_copy("announcement_apply_busy"))
                return
            self._set_busy_state(applying=True)
            self._run_task(
                task=lambda: self._service.apply_safe_moves(self._current_session),
                kind="apply",
                on_success=self._handle_apply_success,
            )

        def _run_task(
            self,
            *,
            task: Callable[[], ReviewSession],
            kind: str,
            on_success: Callable[[ReviewSession], None],
        ) -> None:
            thread = QtCore.QThread(self)
            worker = ReviewWorker(task)
            worker.moveToThread(thread)
            thread.started.connect(worker.run)
            worker.finished.connect(on_success)
            worker.finished.connect(thread.quit)
            worker.finished.connect(worker.deleteLater)
            worker.failed.connect(self._handle_task_error)
            worker.failed.connect(thread.quit)
            worker.failed.connect(worker.deleteLater)
            thread.finished.connect(thread.deleteLater)
            if kind == "scan":
                self._scan_thread = thread
                self._scan_worker = worker
                thread.finished.connect(self._clear_scan_thread)
            else:
                self._apply_thread = thread
                self._apply_worker = worker
                thread.finished.connect(self._clear_apply_thread)
            thread.start()

        def _handle_scan_success(self, session: ReviewSession) -> None:
            self._current_session = session
            self._render_session(session)
            if session.has_items:
                self._announce(_copy("announcement_scan_found", count=len(session.items)))
            else:
                self._announce(_copy("announcement_scan_empty"))

        def _handle_apply_success(self, session: ReviewSession) -> None:
            self._current_session = session
            self._render_session(session)
            self.summary_band.setText(session.apply_summary or _copy("apply_button", count=0))
            self.summary_band.show()
            self._announce(session.apply_summary or _copy("apply_button", count=0))

        def _handle_task_error(self, message: str) -> None:
            self._set_busy_state()
            self.summary_band.setText(message)
            self.summary_band.show()
            if self.list_widget.count() == 0:
                self.empty_label.setText(f"{_copy('error_scan')} {_copy('empty_error_hint')}")
                self.empty_label.show()
            self._announce(message)

        def _render_idle(self) -> None:
            self.list_widget.clear()
            self.subtitle_label.setText(_copy("subtitle", target=_display_target_label(self._service.config.target_dir)))
            self.empty_label.setText(_copy("idle_state"))
            self.empty_label.show()
            self.footer_label.setText(_copy("footer_idle", target=self._service.config.target_dir))

        def _render_session(self, session: ReviewSession) -> None:
            self._set_busy_state()
            self.apply_button.setText(session.apply_button_text)
            self.apply_button.setEnabled(session.safe_count > 0)
            self.list_widget.clear()
            self.empty_label.hide()
            self.subtitle_label.setText(_copy("subtitle", target=_display_target_label(session.target_dir)))
            if not session.items:
                self.empty_label.setText(_copy("empty_state"))
                self.empty_label.show()
            for item in session.items:
                entry = QtWidgets.QListWidgetItem(self.list_widget)
                widget = ReviewItemWidget(item)
                entry.setSizeHint(widget.sizeHint())
                self.list_widget.addItem(entry)
                self.list_widget.setItemWidget(entry, widget)
            footer_parts = [
                session.footer_text,
                _copy("footer_moved", count=session.moved_count) if session.moved_count else None,
                _copy("footer_missing", count=session.missing_count) if session.missing_count else None,
                _copy("footer_skipped", count=session.skipped_count) if session.skipped_count else None,
            ]
            self.footer_label.setText(" • ".join(part for part in footer_parts if part))

        def _set_busy_state(self, *, scanning: bool = False, applying: bool = False) -> None:
            self.scan_button.setEnabled(not scanning and self._apply_thread is None)
            self.apply_button.setEnabled(
                not scanning and not applying and self._current_session is not None and self._current_session.safe_count > 0
            )
            if scanning:
                self.summary_band.setText(_copy("summary_scanning"))
                self.summary_band.show()
            elif applying:
                self.summary_band.setText(_copy("summary_applying"))
                self.summary_band.show()

        def _announce(self, message: str) -> None:
            self._last_announcement = message
            self.sr_status_label.setText(message)
            self.sr_status_label.setAccessibleDescription(message)

        def _clear_scan_thread(self) -> None:
            self._scan_thread = None
            self._scan_worker = None
            self._set_busy_state()

        def _clear_apply_thread(self) -> None:
            self._apply_thread = None
            self._apply_worker = None
            self._set_busy_state()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=_copy("argparse_description"))
    parser.add_argument("--target-dir", default=str(DEFAULT_DOWNLOADS_DIR), help=_copy("argparse_target_dir"))
    parser.add_argument("--output-dir", default=None, help=_copy("argparse_output_dir"))
    parser.add_argument("--model", default=None, help=_copy("argparse_model"))
    parser.add_argument("--base-url", default=None, help=_copy("argparse_base_url"))
    parser.add_argument("--api-key", default=None, help=_copy("argparse_api_key"))
    parser.add_argument("--api-mode", choices=("chat_completions", "responses"), default=None)
    parser.add_argument("--extra-body-json", default=None, help=_copy("argparse_extra_body_json"))
    parser.add_argument("--mock", action="store_true", help=_copy("argparse_mock"))
    return parser.parse_args(argv)


def build_config(args: argparse.Namespace) -> ReviewDemoConfig:
    return ReviewDemoConfig(
        target_dir=Path(os.path.expandvars(args.target_dir)).expanduser().resolve(),
        output_dir=None if args.output_dir is None else Path(os.path.expandvars(args.output_dir)).expanduser().resolve(),
        model=args.model,
        base_url=args.base_url,
        api_key=args.api_key,
        api_mode=args.api_mode,
        extra_body_json=args.extra_body_json,
        mock=bool(args.mock),
    )


def main(argv: list[str] | None = None) -> int:
    if _PYSIDE_IMPORT_ERROR is not None:  # pragma: no cover
        raise SystemExit(_copy("pyside_error"))
    args = parse_args(list(sys.argv[1:] if argv is None else argv))
    config = build_config(args)
    app = QtWidgets.QApplication(sys.argv[:1])
    window = ReviewQueueWindow(ReviewQueueService(config))
    window.show()
    return app.exec()


def _display_state(state: str) -> str:
    labels = {
        "safe": _copy("state_safe"),
        "review": _copy("state_review"),
        "blocked": _copy("state_blocked"),
        "moved": _copy("state_moved"),
        "missing": _copy("state_missing"),
        "skipped": _copy("state_skipped"),
    }
    return labels.get(state, state.title())


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
