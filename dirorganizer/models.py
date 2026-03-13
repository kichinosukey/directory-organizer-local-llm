from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class FileRecord:
    relative_path: str
    parent_dir: str
    name: str
    extension: str
    size_bytes: int
    modified_at: str

    def prompt_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class PlanOperation:
    source: str
    destination_dir: str
    new_name: str
    target_path: str
    action: str
    confidence: float
    reason: str
    can_apply: bool
    issues: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "PlanOperation":
        return cls(
            source=str(payload["source"]),
            destination_dir=str(payload.get("destination_dir", "")),
            new_name=str(payload.get("new_name", "")),
            target_path=str(payload["target_path"]),
            action=str(payload.get("action", "noop")),
            confidence=float(payload.get("confidence", 0.0)),
            reason=str(payload.get("reason", "")),
            can_apply=bool(payload.get("can_apply", False)),
            issues=[str(item) for item in payload.get("issues", [])],
        )


@dataclass
class PlanResult:
    summary: str
    operations: list[PlanOperation]
    warnings: list[str]

    def as_dict(self) -> dict[str, object]:
        return {
            "summary": self.summary,
            "warnings": list(self.warnings),
            "operations": [operation.as_dict() for operation in self.operations],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "PlanResult":
        return cls(
            summary=str(payload.get("summary", "")),
            warnings=[str(item) for item in payload.get("warnings", [])],
            operations=[PlanOperation.from_dict(item) for item in payload.get("operations", [])],
        )
