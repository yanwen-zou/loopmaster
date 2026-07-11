from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def default_workspace_root() -> Path:
    return Path(
        os.environ.get(
            "LOOPMASTER_WORKSPACE_ROOT",
            Path(__file__).resolve().parents[2] / "_runs",
        )
    ).expanduser()


@dataclass(frozen=True)
class Workspace:
    task: str
    run_id: str
    root: Path

    @property
    def plan_path(self) -> Path:
        return self.root / "plan.md"

    @property
    def summary_path(self) -> Path:
        return self.root / "summary.md"

    @property
    def review_path(self) -> Path:
        return self.root / "review.md"

    @property
    def trace_path(self) -> Path:
        return self.root / "trace.jsonl"

    def write_plan(self, text: str) -> None:
        self.plan_path.write_text(text, encoding="utf-8")

    def read_plan(self) -> str:
        if not self.plan_path.exists():
            return ""
        return self.plan_path.read_text(encoding="utf-8")

    def write_summary(self, text: str) -> None:
        self.summary_path.write_text(text, encoding="utf-8")

    def read_summary(self) -> str:
        if not self.summary_path.exists():
            return ""
        return self.summary_path.read_text(encoding="utf-8")

    def write_review(self, text: str) -> None:
        self.review_path.write_text(text, encoding="utf-8")

    def append_trace(self, record: dict[str, Any]) -> None:
        with self.trace_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def new_workspace(task: str, root: Path | None = None) -> Workspace:
    run_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    safe_task = "".join(c if c.isalnum() or c in "._-" else "_" for c in task)[:80]
    workspace_root = root or default_workspace_root()
    path = workspace_root / f"{safe_task}-{run_id}"
    path.mkdir(parents=True, exist_ok=True)
    return Workspace(task=task, run_id=run_id, root=path)
