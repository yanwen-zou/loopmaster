from __future__ import annotations

from pathlib import Path

from loopmaster_agentic.agents.auditor import Auditor
from loopmaster_agentic.agents.strategist import Strategist
from loopmaster_agentic.agents.worker import Worker
from loopmaster_agentic.agents.workspace import new_workspace
from loopmaster_agentic.core.result import RunResult
from loopmaster_agentic.platform.base import RobotPlatform
from loopmaster_agentic.skills.registry import SkillRegistry


class Handler:
    """Owns the real-robot subagent handoff."""

    role_name = "handler"

    def __init__(
        self,
        *,
        strategist: Strategist | None = None,
        worker: Worker | None = None,
        auditor: Auditor | None = None,
        skills: SkillRegistry | None = None,
        workspace_root: Path | None = None,
    ) -> None:
        self.strategist = strategist or Strategist()
        self.worker = worker or Worker()
        self.auditor = auditor or Auditor()
        self.skills = skills or SkillRegistry()
        self.workspace_root = workspace_root

    def run(
        self,
        *,
        task: str,
        user_request: str,
        platform: RobotPlatform,
    ) -> RunResult:
        workspace = new_workspace(task, self.workspace_root)
        platform.connect()
        try:
            plan = self.strategist.plan(
                task=task,
                user_request=user_request,
                workspace=workspace,
                skills=self.skills,
            )
            trace = self.worker.execute(
                plan=plan,
                workspace=workspace,
                platform=platform,
                skills=self.skills,
            )
            review = self.auditor.review(plan=plan, trace=trace, workspace=workspace)
            return RunResult(
                task=task,
                workspace=str(workspace.root),
                plan=plan,
                trace=trace,
                review=review,
                success=bool(review.get("success")),
            )
        finally:
            platform.close()
