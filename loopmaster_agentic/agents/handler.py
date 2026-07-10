from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from loopmaster_agentic.agents.auditor import Auditor
from loopmaster_agentic.agents.codex_subagent import SubagentClient
from loopmaster_agentic.agents.skill_updater import apply_review_skill_updates
from loopmaster_agentic.agents.strategist import Strategist
from loopmaster_agentic.agents.worker import Worker
from loopmaster_agentic.agents.workspace import new_workspace
from loopmaster_agentic.core.result import RunResult
from loopmaster_agentic.core.types import Plan
from loopmaster_agentic.platform.base import RobotPlatform
from loopmaster_agentic.skills.registry import SkillRegistry, user_skill_root


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
        progress: Callable[[str], None] | None = None,
    ) -> RunResult:
        workspace = new_workspace(task, self.workspace_root)
        notes: list[str] = []
        handler_agent: dict[str, Any] | None = None

        if self.agent_client is not None:
            if progress is not None:
                progress("handler agent routing request")
            handler_agent = self.agent_client.run_json(
                role="handler",
                prompt=_handler_prompt(task=task, user_request=user_request, skills=self.skills),
                schema=_HANDLER_SCHEMA,
            )
            _write_json(workspace.root / "handler_agent.json", handler_agent)
            notes.extend(_agent_notes("handler", handler_agent))
            if _handler_route(handler_agent) == "direct_response":
                response = str(handler_agent.get("direct_response") or "").strip()
                if not response:
                    response = _fallback_direct_response(user_request, skills=self.skills) or "我可以直接回答这个问题。"
                if progress is not None:
                    progress("handler agent answered directly; no platform connection or strategist handoff")
                return _direct_result(
                    task=task,
                    workspace=workspace,
                    response=response,
                    notes=[*notes, "handler agent answered directly"],
                )
        else:
            fallback_response = _fallback_direct_response(user_request, skills=self.skills)
            if fallback_response is not None:
                if progress is not None:
                    progress("local handler answered directly; no platform connection or strategist handoff")
                return _direct_result(
                    task=task,
                    workspace=workspace,
                    response=fallback_response,
                    notes=["local handler fallback answered directly"],
                )

        if progress is not None:
            progress("connecting platform")
        platform.connect()
        try:
            seen_skill_updates: set[str] = set()
            while True:
                if progress is not None:
                    progress("strategist planning skill calls")
                plan = self.strategist.plan(
                    task=task,
                    user_request=user_request,
                    workspace=workspace,
                    skills=self.skills,
                    agent_client=self.agent_client,
                )
                notes.extend(note for note in plan.subagent_notes if "Codex profile" in note)
                trace = []
                seen_failures: set[tuple[str, str, str]] = set()
                while True:
                    if progress is not None:
                        progress("worker executing plan")
                    trace = self.worker.execute(
                        plan=plan,
                        workspace=workspace,
                        platform=platform,
                        skills=self.skills,
                        agent_client=self.agent_client,
                        progress=progress,
                    )
                    if self.agent_client is not None and getattr(self.agent_client, "profile", None):
                        notes.append(f"worker ran through Codex profile {self.agent_client.profile}")
                    repairable_failure = _repairable_failure_signature(trace, agent_client=self.agent_client)
                    if repairable_failure is None:
                        break
                    if repairable_failure in seen_failures:
                        notes.append("worker failure repeated after strategist loop")
                        if progress is not None:
                            progress(f"loop stopped: repeated failure {_format_failure_signature(repairable_failure)}")
                        break
                    seen_failures.add(repairable_failure)
                    if progress is not None:
                        progress(f"worker failure is self-fixable: {_format_failure_signature(repairable_failure)}")
                        progress("returning failure trace to strategist")
                    notes.append("worker failure trace returned to strategist")
                    plan = self.strategist.replan_after_failure(
                        task=task,
                        user_request=user_request,
                        workspace=workspace,
                        skills=self.skills,
                        previous_plan=plan,
                        trace=trace,
                        agent_client=self.agent_client,
                    )
                    notes.extend(note for note in plan.subagent_notes if "Codex profile" in note)
                    if progress is not None:
                        progress(f"strategist revised plan: {_format_plan_steps(plan)}")
                if progress is not None:
                    progress("auditor reviewing trace")
                try:
                    review = self.auditor.review(
                        plan=plan,
                        trace=trace,
                        workspace=workspace,
                        agent_client=self.agent_client,
                    )
                except Exception as exc:
                    review = _auditor_failure_review(plan=plan, trace=trace, error=exc)
                    (workspace.root / "auditor_agent_error.txt").write_text(str(exc) + "\n", encoding="utf-8")
                    workspace.write_review(_auditor_failure_markdown(plan, review))
                    notes.append(f"auditor subagent failed: {type(exc).__name__}: {exc}")
                    if progress is not None:
                        progress(f"auditor failed: {type(exc).__name__}: {_short_error(exc)}")
                    return RunResult(
                        task=task,
                        workspace=str(workspace.root),
                        plan=plan,
                        trace=trace,
                        review=review,
                        success=False,
                        notes=notes,
                    )
                notes.extend(_agent_notes("auditor", review))
                if progress is not None:
                    progress(f"auditor verdict={review.get('verdict')} root_cause={review.get('root_cause') or 'none'}")
                if not review.get("success"):
                    update_signature = _skill_update_signature(review)
                    if update_signature and update_signature not in seen_skill_updates:
                        seen_skill_updates.add(update_signature)
                        if progress is not None:
                            progress("auditor proposed skill update; applying gated skill files")
                        update_results = apply_review_skill_updates(review, skills=self.skills, workspace=workspace)
                        notes.extend(f"skill update {item.skill_name}: {item.to_dict()}" for item in update_results)
                        if any(item.ok for item in update_results):
                            if progress is not None:
                                progress("skill update applied; reloading registry and rerunning")
                            roots = _roots_with_user_skill_root(self.skills)
                            self.skills = SkillRegistry(roots=roots, include_user=False)
                            continue
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
        "route": {"type": "string", "enum": ["direct_response", "strategist"]},
        "direct_response": {"type": "string"},
        "run_intent": {"type": "string"},
        "handoff_notes": {"type": "array", "items": {"type": "string"}},
        "safety_notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["route", "direct_response", "run_intent", "handoff_notes", "safety_notes"],
    "additionalProperties": False,
}


def _handler_prompt(*, task: str, user_request: str, skills: SkillRegistry) -> str:
    payload = {
        "role": "handler",
        "contract": (
            "You own the LoopMaster chat turn. Decide whether to answer directly or hand off "
            "to Strategist, Worker, and Auditor. Do not execute shell commands or edit files. "
            "Use route='direct_response' for greetings, capability questions, clarification, "
            "or other conversational turns that do not require robot state, perception, or motion. "
            "Use route='strategist' when the request asks for robot observation, perception, "
            "control, task execution, or any platform skill call. Return only the requested JSON."
        ),
        "task": task,
        "user_request": user_request,
        "available_skills": [skill.name for skill in skills.list()],
        "direct_response_guidance": (
            "When route is direct_response, write the final user-facing response in the user's "
            "language. Be concise. Do not mention internal route names, workspaces, or Codex."
        ),
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


def _handler_route(data: dict[str, Any]) -> str:
    route = str(data.get("route") or "strategist")
    if route not in {"direct_response", "strategist"}:
        return "strategist"
    return route


def _repairable_failure_signature(
    trace: list[Any],
    *,
    agent_client: SubagentClient | None,
) -> tuple[str, str, str] | None:
    if agent_client is None:
        return None
    failed = next((step for step in trace if not step.ok), None)
    if failed is None:
        return None
    error_text = str(failed.result.get("error") or failed.result).lower()
    retry_markers = (
        "worker preflight returned proceed=false",
        "proceed=false",
        "preflight",
        "stale",
        "schema",
        "argument",
        "args",
        "parameter",
        "side must be",
        "positions must",
        "unknown joint",
        "must contain",
        "must be numeric",
        "typeerror",
        "valueerror",
    )
    if not any(marker in error_text for marker in retry_markers):
        return None
    args_fingerprint = json.dumps(failed.args, sort_keys=True, ensure_ascii=False, default=str)
    return (str(failed.skill), args_fingerprint, error_text)


def _format_failure_signature(signature: tuple[str, str, str]) -> str:
    skill, args, error = signature
    if len(error) > 180:
        error = error[:177] + "..."
    return f"skill={skill} args={args} error={error}"


def _format_plan_steps(plan: Plan) -> str:
    parts = []
    for step in plan.steps:
        parts.append(f"{step.name}({json.dumps(step.args, ensure_ascii=False, sort_keys=True, default=str)})")
    text = " -> ".join(parts) if parts else "(no steps)"
    return text if len(text) <= 300 else text[:297] + "..."


def _skill_update_signature(review: dict[str, Any]) -> str:
    proposals = [*(review.get("skill_proposals") or []), *(review.get("skill_updates") or [])]
    if not proposals:
        return ""
    return json.dumps(proposals, sort_keys=True, ensure_ascii=False, default=str)


def _auditor_failure_review(*, plan: Plan, trace: list[Any], error: Exception) -> dict[str, Any]:
    used_skills = sorted({str(step.skill) for step in trace})
    control_skills = {
        "move_arm_ee",
        "move_arm_joints",
        "set_gripper",
        "set_base_velocity",
        "set_lift_height",
    }
    return {
        "verdict": "blocked",
        "root_cause": f"auditor subagent failed: {type(error).__name__}: {_short_error(error)}",
        "next_action": "Fix the auditor Codex/profile/model configuration and rerun audit; no local audit fallback was used.",
        "used_skills": used_skills,
        "used_control_skills": sorted(set(used_skills) & control_skills),
        "sim_leak": [],
        "research_questions": [],
        "success": False,
        "notes": ["worker trace exists but final auditor verification did not complete"],
    }


def _auditor_failure_markdown(plan: Plan, review: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"# Audit: {plan.task}",
            "",
            "**Verdict**: `blocked`",
            f"**Root cause**: {review['root_cause']}",
            f"**Next action**: {review['next_action']}",
            "",
            "Auditor subagent failed, so this run was not marked done.",
        ]
    ).rstrip() + "\n"


def _short_error(error: Exception, *, limit: int = 240) -> str:
    text = str(error).replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _roots_with_user_skill_root(skills: SkillRegistry) -> list[Path]:
    roots = list(skills.roots)
    user_root = user_skill_root()
    if user_root not in roots:
        roots.append(user_root)
    return roots


def _fallback_direct_response(user_request: str, *, skills: SkillRegistry) -> str | None:
    text = user_request.strip().lower()
    if not text:
        return ""
    capability_markers = (
        "你现在能干嘛",
        "你能干嘛",
        "你可以干嘛",
        "能做什么",
        "可以做什么",
        "what can you do",
        "help",
        "帮助",
    )
    greeting_markers = ("嗨", "你好", "hello", "hi", "hey")
    if not any(marker in text for marker in (*capability_markers, *greeting_markers)):
        return None
    if any(action in text for action in ("移动", "前进", "后退", "抓", "夹", "观察", "拍照", "检测", "设置", "控制")):
        return None

    skill_names = sorted(skill.name for skill in skills.list())
    skill_summary = "、".join(skill_names)
    return (
        "我可以帮你通过 LoopMaster 控制和观察 HEI ReBot Lift：读取机器人状态/相机图像，"
        "控制底盘速度、升降高度、左右机械臂关节和夹爪，并调用已注册的感知技能。"
        f"当前可用基础技能包括：{skill_summary}。\n\n"
        "给我一个具体任务就行，例如“观察当前状态”、“右夹爪张开一点”、"
        "或“底盘向前移动一小段”。涉及真实运动时我会通过平台 skill 执行。"
    )


def _direct_result(*, task: str, workspace: Any, response: str, notes: list[str]) -> RunResult:
    plan = Plan(
        task=task,
        goal="Answer a conversational turn directly.",
        steps=[],
        success_criteria=["User receives a direct response."],
    )
    workspace.write_plan(plan.to_markdown())
    workspace.write_summary("# Handler Summary\n\nAnswered directly without robot skill calls.\n")
    review = {
        "verdict": "done",
        "root_cause": "",
        "next_action": "Ask the user for a specific robot task when they want action.",
        "used_skills": [],
        "used_control_skills": [],
        "sim_leak": [],
        "research_questions": [],
        "success": True,
        "response": response,
    }
    workspace.write_review(_direct_review_markdown(task, review))
    return RunResult(
        task=task,
        workspace=str(workspace.root),
        plan=plan,
        trace=[],
        review=review,
        success=True,
        notes=notes,
    )


def _direct_review_markdown(task: str, review: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"# Audit: {task}",
            "",
            "**Verdict**: `done`",
            "**Root cause**: (none)",
            f"**Next action**: {review['next_action']}",
            "",
            "## Response",
            str(review["response"]),
        ]
    ).rstrip() + "\n"
