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
