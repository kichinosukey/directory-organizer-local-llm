from __future__ import annotations

import argparse
import io
import json
import os
from dataclasses import dataclass
from pathlib import Path

from dirorganizer import cli
from dirorganizer.cli import BLOCKED_ISSUE_MARKERS
from dirorganizer.models import PlanOperation
DEFAULT_REVIEW_PRESET = "finance-receipts"
DEFAULT_DOWNLOADS_DIR = Path.home() / "Downloads"


class ReviewSessionError(RuntimeError):
    """Raised when the review session cannot be built or applied safely."""


@dataclass(frozen=True)
class ReviewItem:
    source: str
    target_path: str
    destination_dir: str
    confidence: float
    reason: str
    can_apply: bool
    issues: tuple[str, ...]
    state: str
    state_detail: str

    @property
    def filename(self) -> str:
        return Path(self.source).name

    @property
    def destination_label(self) -> str:
        return self.destination_dir or "."


@dataclass(frozen=True)
class ReviewSession:
    target_dir: Path
    run_dir: Path
    manifest_path: Path
    created_at: str
    summary: str
    warnings: tuple[str, ...]
    items: tuple[ReviewItem, ...]
    safe_count: int
    blocked_count: int
    review_count: int
    moved_count: int
    missing_count: int
    skipped_count: int
    apply_summary: str | None

    @property
    def apply_button_text(self) -> str:
        return f"{self.safe_count}件を安全に整理"

    @property
    def footer_text(self) -> str:
        parts = [f"最終スキャン {self.created_at}"]
        parts.append(f"{self.safe_count}件 安全")
        parts.append(f"{self.blocked_count}件 要確認")
        if self.run_dir.joinpath("undo_manifest.json").exists():
            parts.append("Undo 利用可")
        return " • ".join(parts)

    @property
    def has_items(self) -> bool:
        return bool(self.items)


def build_review_session(
    *,
    target_dir: str | Path | None = None,
    output_dir: str | Path | None = None,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    api_mode: str | None = None,
    extra_body_json: str | None = None,
    timeout: int = 120,
    max_output_tokens: int = 1200,
    temperature: float = 0.0,
    include_hidden: bool = False,
    mock: bool = False,
) -> ReviewSession:
    resolved_target = _resolve_target_dir(target_dir)
    args = _build_review_namespace(
        target_dir=resolved_target,
        output_dir=output_dir,
        model=model,
        base_url=base_url,
        api_key=api_key,
        api_mode=api_mode,
        extra_body_json=extra_body_json,
        timeout=timeout,
        max_output_tokens=max_output_tokens,
        temperature=temperature,
        include_hidden=include_hidden,
        mock=mock,
    )
    stderr = io.StringIO()
    result = cli._build_plan_artifacts(args, command_name="review", stderr=stderr)
    if result is None:
        raise ReviewSessionError(_clean_error(stderr.getvalue()))

    _, run_dir, manifest = result
    return _build_session_from_manifest(run_dir=run_dir, manifest=manifest)


def apply_review_session(review_session: ReviewSession) -> ReviewSession:
    result = cli._apply_manifest(
        manifest_path=review_session.manifest_path,
        command_started_at=0.0,
    )
    manifest = result["manifest"]
    run_dir = review_session.run_dir
    apply_result = json.loads((run_dir / "apply_result.json").read_text(encoding="utf-8"))
    return _build_session_from_manifest(
        run_dir=run_dir,
        manifest=manifest,
        apply_result=apply_result,
    )


def _build_review_namespace(
    *,
    target_dir: Path,
    output_dir: str | Path | None,
    model: str | None,
    base_url: str | None,
    api_key: str | None,
    api_mode: str | None,
    extra_body_json: str | None,
    timeout: int,
    max_output_tokens: int,
    temperature: float,
    include_hidden: bool,
    mock: bool,
) -> argparse.Namespace:
    resolved_model = model or os.getenv("LOCAL_LLM_MODEL")
    if not mock and not resolved_model:
        raise ReviewSessionError("LOCAL_LLM_MODEL が未設定です。.env を設定するか、GUI を --mock 付きで起動してください。")
    return argparse.Namespace(
        command="review",
        target_dir=str(target_dir),
        model=resolved_model,
        base_url=base_url or os.getenv("LOCAL_LLM_BASE_URL", "http://127.0.0.1:1234/v1"),
        api_key=api_key or os.getenv("LOCAL_LLM_API_KEY", "not-needed"),
        api_mode=api_mode or os.getenv("LOCAL_LLM_API_MODE", "chat_completions"),
        rules=None,
        output_dir=str(output_dir) if output_dir is not None else None,
        max_files=None,
        max_depth=None,
        batch_size=None,
        min_confidence=None,
        timeout=timeout,
        max_output_tokens=max_output_tokens,
        temperature=temperature,
        extra_body_json=extra_body_json or os.getenv("LOCAL_LLM_EXTRA_BODY_JSON"),
        extra_body=_parse_extra_body(extra_body_json),
        include_hidden=include_hidden,
        mock=mock,
        fast_lane=True,
        preset=DEFAULT_REVIEW_PRESET,
        yes=False,
    )


def _parse_extra_body(explicit_value: str | None) -> dict[str, object]:
    value = explicit_value if explicit_value is not None else os.getenv("LOCAL_LLM_EXTRA_BODY_JSON")
    if value is None or not value.strip():
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ReviewSessionError(f"LOCAL_LLM_EXTRA_BODY_JSON is invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ReviewSessionError("LOCAL_LLM_EXTRA_BODY_JSON must decode to an object.")
    return payload


def _resolve_target_dir(target_dir: str | Path | None) -> Path:
    if target_dir is None:
        return DEFAULT_DOWNLOADS_DIR.expanduser().resolve()
    if isinstance(target_dir, Path):
        return target_dir.expanduser().resolve()
    return cli.resolve_configured_path(str(target_dir))


def _build_session_from_manifest(
    *,
    run_dir: Path,
    manifest: dict[str, object],
    apply_result: dict[str, object] | None = None,
) -> ReviewSession:
    operations = [PlanOperation.from_dict(item) for item in manifest.get("operations", [])]
    applied_by_source: set[str] = set()
    skipped_by_source: dict[str, str] = {}
    if apply_result is not None:
        applied_by_source = {
            str(item["source"])
            for item in apply_result.get("applied_operations", [])
            if isinstance(item, dict) and "source" in item
        }
        skipped_by_source = {
            str(item["source"]): str(item["reason"])
            for item in apply_result.get("skipped_operations", [])
            if isinstance(item, dict) and "source" in item
        }

    items = [
        _to_review_item(operation, applied_by_source=applied_by_source, skipped_by_source=skipped_by_source)
        for operation in operations
    ]
    items.sort(key=_sort_key)

    safe_count = sum(item.state == "safe" for item in items)
    blocked_count = sum(item.state == "blocked" for item in items)
    review_count = sum(item.state == "review" for item in items)
    moved_count = sum(item.state == "moved" for item in items)
    missing_count = sum(item.state == "missing" for item in items)
    skipped_count = sum(item.state == "skipped" for item in items)

    apply_summary = None
    if apply_result is not None:
        applied_moves = int(apply_result.get("applied_moves", 0))
        skipped_total = int(apply_result.get("skipped", 0))
        if skipped_total > 0:
            apply_summary = f"{applied_moves}件を安全に整理しました。{skipped_total}件は確認が必要です。"
        else:
            apply_summary = f"{applied_moves}件を安全に整理しました。"

    return ReviewSession(
        target_dir=Path(str(manifest["target_dir"])),
        run_dir=run_dir,
        manifest_path=run_dir / "manifest.json",
        created_at=str(manifest.get("created_at", "")),
        summary=str(manifest.get("summary", "")),
        warnings=tuple(str(item) for item in manifest.get("warnings", [])),
        items=tuple(items),
        safe_count=safe_count,
        blocked_count=blocked_count,
        review_count=review_count,
        moved_count=moved_count,
        missing_count=missing_count,
        skipped_count=skipped_count,
        apply_summary=apply_summary,
    )


def _to_review_item(
    operation: PlanOperation,
    *,
    applied_by_source: set[str],
    skipped_by_source: dict[str, str],
) -> ReviewItem:
    state, detail = _derive_state(operation, applied_by_source=applied_by_source, skipped_by_source=skipped_by_source)
    return ReviewItem(
        source=operation.source,
        target_path=operation.target_path,
        destination_dir=operation.destination_dir,
        confidence=operation.confidence,
        reason=operation.reason,
        can_apply=operation.can_apply,
        issues=tuple(operation.issues),
        state=state,
        state_detail=detail,
    )


def _derive_state(
    operation: PlanOperation,
    *,
    applied_by_source: set[str],
    skipped_by_source: dict[str, str],
) -> tuple[str, str]:
    if operation.source in applied_by_source:
        return "moved", "安全に整理済み"
    if operation.source in skipped_by_source:
        raw_skipped_reason = skipped_by_source[operation.source]
        skipped_reason = _present_issue(raw_skipped_reason)
        if "no longer exists" in raw_skipped_reason.lower():
            return "missing", "適用前に見つからなくなりました"
        if _is_blocked_operation(operation):
            return "blocked", skipped_reason
        return "skipped", skipped_reason
    if operation.can_apply and operation.action == "move":
        return "safe", "安全に整理できます"
    if _is_blocked_operation(operation):
        return "blocked", _present_issue(_primary_issue(operation) or operation.reason)
    return "review", _present_issue(_primary_issue(operation) or operation.reason)


def _is_blocked_operation(operation: PlanOperation) -> bool:
    if not operation.issues:
        return False
    issue_text = " ".join(operation.issues).lower()
    return any(marker in issue_text for marker in BLOCKED_ISSUE_MARKERS)


def _primary_issue(operation: PlanOperation) -> str | None:
    return operation.issues[0] if operation.issues else None


def _present_issue(value: str) -> str:
    lowered = value.lower()
    if "target collision" in lowered or "already exists" in lowered:
        return "整理先に同名ファイルがすでにあります"
    if "confidence below threshold" in lowered:
        return "信頼度がしきい値を下回っています"
    if "no longer exists" in lowered:
        return "適用前にファイルが見つからなくなりました"
    if "heuristic classification" in lowered:
        return "拡張子とファイル名から候補を推定しました"
    return value


def _sort_key(item: ReviewItem) -> tuple[int, float, str]:
    order = {
        "safe": 0,
        "review": 1,
        "blocked": 2,
        "moved": 3,
        "missing": 4,
        "skipped": 5,
    }
    return (order.get(item.state, 99), -item.confidence, item.source)


def _clean_error(raw_error: str) -> str:
    text = raw_error.strip()
    if not text:
        return "整理候補を作成できませんでした。"
    return text.splitlines()[-1].removeprefix("[ERROR] ").strip()
