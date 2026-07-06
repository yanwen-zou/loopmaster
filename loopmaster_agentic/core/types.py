from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Observation:
    """Backend-neutral real-robot observation."""

    images: dict[str, Any] = field(default_factory=dict)
    state: dict[str, float] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    extras: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "images": {
                name: _image_summary(image) for name, image in self.images.items()
            },
            "state_keys": sorted(self.state),
            "timestamp": self.timestamp,
            "extras": self.extras,
        }


@dataclass(frozen=True)
class SkillCall:
    """One planned skill invocation."""

    name: str
    args: dict[str, Any] = field(default_factory=dict)
    why: str = ""


@dataclass
class Plan:
    """Strategist output consumed by Worker."""

    task: str
    goal: str
    steps: list[SkillCall] = field(default_factory=list)
    success_criteria: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    research_questions: list[str] = field(default_factory=list)
    subagent_notes: list[str] = field(default_factory=list)

    def to_markdown(self) -> str:
        lines = [
            f"# Plan: {self.task}",
            "",
            "## Goal",
            self.goal,
            "",
            "## Steps",
        ]
        for idx, step in enumerate(self.steps, start=1):
            suffix = f" - {step.why}" if step.why else ""
            lines.append(f"{idx}. `{step.name}` args={step.args}{suffix}")
        if not self.steps:
            lines.append("(no executable steps)")
        lines += ["", "## Success Criteria"]
        lines.extend(f"- {item}" for item in self.success_criteria)
        lines += ["", "## Risks"]
        lines.extend(f"- {item}" for item in self.risks)
        if self.assumptions:
            lines += ["", "## Assumptions"]
            lines.extend(f"- {item}" for item in self.assumptions)
        if self.research_questions:
            lines += ["", "## Research Questions"]
            lines.extend(f"- {item}" for item in self.research_questions)
        if self.subagent_notes:
            lines += ["", "## Subagent Notes"]
            lines.extend(f"- {item}" for item in self.subagent_notes)
        return "\n".join(lines).rstrip() + "\n"


@dataclass
class TraceStep:
    """Evidence from one Worker skill call."""

    index: int
    skill: str
    args: dict[str, Any]
    result: dict[str, Any]
    ok: bool
    why: str = ""
    role: str = "worker"
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "skill": self.skill,
            "args": self.args,
            "result": self.result,
            "ok": self.ok,
            "why": self.why,
            "role": self.role,
            "timestamp": self.timestamp,
        }


def _image_summary(image: Any) -> dict[str, Any]:
    shape = getattr(image, "shape", None)
    dtype = getattr(image, "dtype", None)
    if shape is not None:
        return {"shape": tuple(int(v) for v in shape), "dtype": str(dtype)}
    return {"type": type(image).__name__}
