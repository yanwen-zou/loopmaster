from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loopmaster_agentic.agents.auditor import Auditor
from loopmaster_agentic.agents.codex_subagent import SubagentClient
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
        agent_client: SubagentClient | None = None,
    ) -> None:
        self.strategist = strategist or Strategist()
        self.worker = worker or Worker()
        self.auditor = auditor or Auditor()
        self.skills = skills or SkillRegistry()
        self.workspace_root = workspace_root
        self.agent_client = agent_client

    def run(
        self,
        *,
        task: str,
        user_request: str,
        platform: RobotPlatform,
    ) -> RunResult:
        workspace = new_workspace(task, self.workspace_root)
        notes: list[str] = []
        if self.agent_client is not None:
            handler_agent = self.agent_client.run_json(
                role="handler",
                prompt=_handler_prompt(task=task, user_request=user_request),
                schema=_HANDLER_SCHEMA,
            )
            _write_json(workspace.root / "handler_agent.json", handler_agent)
            notes.extend(_agent_notes("handler", handler_agent))
        platform.connect()
        try:
            plan = self.strategist.plan(
                task=task,
                user_request=user_request,
                workspace=workspace,
                skills=self.skills,
                agent_client=self.agent_client,
            )
            notes.extend(note for note in plan.subagent_notes if "Codex profile" in note)
            trace = self.worker.execute(
                plan=plan,
                workspace=workspace,
                platform=platform,
                skills=self.skills,
                agent_client=self.agent_client,
            )
            if self.agent_client is not None and getattr(self.agent_client, "profile", None):
                notes.append(f"worker ran through Codex profile {self.agent_client.profile}")
            review = self.auditor.review(
                plan=plan,
                trace=trace,
                workspace=workspace,
                agent_client=self.agent_client,
            )
            notes.extend(_agent_notes("auditor", review))
            return RunResult(
                task=task,
                workspace=str(workspace.root),
                plan=plan,
                trace=trace,
                review=review,
                success=bool(review.get("success")),
                notes=notes,
            )
        finally:
            platform.close()

    def clear_agent_sessions(self) -> None:
        if self.agent_client is not None and hasattr(self.agent_client, "clear"):
            self.agent_client.clear()


_HANDLER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "run_intent": {"type": "string"},
        "handoff_notes": {"type": "array", "items": {"type": "string"}},
        "safety_notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["run_intent", "handoff_notes", "safety_notes"],
    "additionalProperties": False,
}


def _handler_prompt(*, task: str, user_request: str) -> str:
    payload = {
        "role": "handler",
        "contract": (
            "You own the LoopMaster run and hand off to Strategist, Worker, and Auditor. "
            "Do not execute shell commands or edit files. Return only the requested JSON."
        ),
        "task": task,
        "user_request": user_request,
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")


def _agent_notes(role: str, data: dict[str, Any]) -> list[str]:
    codex = data.get("_codex")
    if not isinstance(codex, dict):
        return []
    profile = codex.get("profile")
    session_id = codex.get("session_id")
    if not profile:
        return []
    suffix = f" session={session_id}" if session_id else ""
    return [f"{role} ran through Codex profile {profile}{suffix}"]
