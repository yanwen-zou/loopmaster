from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from loopmaster_agentic.core.types import Plan, TraceStep


@dataclass
class RunResult:
    """Handler return value."""

    task: str
    workspace: str
    plan: Plan
    trace: list[TraceStep]
    review: dict[str, Any]
    success: bool
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "workspace": self.workspace,
            "trace": [step.to_dict() for step in self.trace],
            "review": self.review,
            "success": self.success,
            "notes": self.notes,
        }
