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
from loopmaster_agentic.core.types import Plan, TraceStep
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
            seen_auditor_retries: set[str] = set()
            auditor_retry_count = 0
            plan_override: Plan | None = None
            while True:
                if plan_override is None:
                    if progress is not None:
                        progress("strategist planning skill calls")
                    plan = self.strategist.plan(
                        task=task,
                        user_request=user_request,
                        workspace=workspace,
                        skills=self.skills,
                        agent_client=self.agent_client,
                    )
                else:
                    plan = plan_override
                    plan_override = None
                notes.extend(note for note in plan.subagent_notes if "Codex profile" in note)
                if progress is not None:
                    progress(f"strategist plan: {_format_plan_steps(plan)}")
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
                    retry_signature = _auditor_retry_signature(review, agent_client=self.agent_client)
                    if retry_signature:
                        if retry_signature in seen_auditor_retries:
                            notes.append("auditor retry repeated after strategist loop")
                            if progress is not None:
                                progress("loop stopped: repeated auditor retry")
                            escalated = _escalate_auditor_retry(
                                agent_client=self.agent_client,
                                task=task,
                                user_request=user_request,
                                workspace=str(workspace.root),
                                skills=self.skills,
                                plan=plan,
                                trace=trace,
                                review=review,
                                notes=notes,
                                reason="repeated auditor retry after strategist loop",
                                escalation_index=auditor_retry_count + 1,
                            )
                            if escalated is not None:
                                review = _merge_escalation_review(review, escalated)
                                notes.extend(_agent_notes("auditor_escalation", escalated))
                                if _escalation_wants_skill_retry(escalated):
                                    if progress is not None:
                                        progress("auditor escalation proposed skill update; applying gated skill files")
                                    update_results = apply_review_skill_updates(
                                        escalated, skills=self.skills, workspace=workspace
                                    )
                                    notes.extend(
                                        f"escalation skill update {item.skill_name}: {item.to_dict()}"
                                        for item in update_results
                                    )
                                    if any(item.ok for item in update_results):
                                        if progress is not None:
                                            progress("escalation skill update applied; reloading registry and rerunning")
                                        roots = _roots_with_user_skill_root(self.skills)
                                        self.skills = SkillRegistry(roots=roots, include_user=False)
                                        seen_auditor_retries.clear()
                                        auditor_retry_count = 0
                                        continue
                        elif auditor_retry_count >= 2:
                            notes.append("auditor retry limit reached")
                            if progress is not None:
                                progress("loop stopped: auditor retry limit reached")
                            escalated = _escalate_auditor_retry(
                                agent_client=self.agent_client,
                                task=task,
                                user_request=user_request,
                                workspace=str(workspace.root),
                                skills=self.skills,
                                plan=plan,
                                trace=trace,
                                review=review,
                                notes=notes,
                                reason="auditor retry limit reached",
                                escalation_index=auditor_retry_count + 1,
                            )
                            if escalated is not None:
                                review = _merge_escalation_review(review, escalated)
                                notes.extend(_agent_notes("auditor_escalation", escalated))
                                if _escalation_wants_skill_retry(escalated):
                                    if progress is not None:
                                        progress("auditor escalation proposed skill update; applying gated skill files")
                                    update_results = apply_review_skill_updates(
                                        escalated, skills=self.skills, workspace=workspace
                                    )
                                    notes.extend(
                                        f"escalation skill update {item.skill_name}: {item.to_dict()}"
                                        for item in update_results
                                    )
                                    if any(item.ok for item in update_results):
                                        if progress is not None:
                                            progress("escalation skill update applied; reloading registry and rerunning")
                                        roots = _roots_with_user_skill_root(self.skills)
                                        self.skills = SkillRegistry(roots=roots, include_user=False)
                                        seen_auditor_retries.clear()
                                        auditor_retry_count = 0
                                        continue
                        else:
                            seen_auditor_retries.add(retry_signature)
                            auditor_retry_count += 1
                            if progress is not None:
                                progress("returning auditor retry review to strategist")
                            notes.append("auditor retry review returned to strategist")
                            plan_override = self.strategist.replan_after_failure(
                                task=task,
                                user_request=user_request,
                                workspace=workspace,
                                skills=self.skills,
                                previous_plan=plan,
                                trace=_trace_with_auditor_review(trace, review),
                                agent_client=self.agent_client,
                            )
                            notes.extend(note for note in plan_override.subagent_notes if "Codex profile" in note)
                            if progress is not None:
                                progress(f"strategist revised plan: {_format_plan_steps(plan_override)}")
                            continue
                if self.agent_client is not None:
                    review = self._summarize_for_user(
                        task=task,
                        user_request=user_request,
                        workspace=workspace,
                        plan=plan,
                        trace=trace,
                        review=review,
                        success=bool(review.get("success")),
                        notes=notes,
                        progress=progress,
                    )
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

    def _summarize_for_user(
        self,
        *,
        task: str,
        user_request: str,
        workspace: Any,
        plan: Plan,
        trace: list[Any],
        review: dict[str, Any],
        success: bool,
        notes: list[str],
        progress: Callable[[str], None] | None,
    ) -> dict[str, Any]:
        if self.agent_client is None:
            return review
        if progress is not None:
            progress("handler agent summarizing run for user")
        try:
            summary = self.agent_client.run_json(
                role="handler_summary",
                prompt=_handler_summary_prompt(
                    task=task,
                    user_request=user_request,
                    workspace=str(workspace.root),
                    plan=plan,
                    trace=trace,
                    review=review,
                    success=success,
                    notes=notes,
                ),
                schema=_HANDLER_SUMMARY_SCHEMA,
            )
        except Exception as exc:
            notes.append(f"handler summary agent failed: {type(exc).__name__}: {_short_error(exc)}")
            if progress is not None:
                progress(f"handler summary failed: {type(exc).__name__}: {_short_error(exc)}")
            return review
        _write_json(workspace.root / "handler_summary_agent.json", summary)
        notes.extend(_agent_notes("handler_summary", summary))
        response = str(summary.get("response") or "").strip()
        if not response:
            return review
        merged = dict(review)
        merged["response"] = response
        return merged


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


_HANDLER_SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "response": {"type": "string"},
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["response", "notes"],
    "additionalProperties": False,
}


def _skill_proposal_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "kind": {"type": "string"},
            "skill_name": {"type": "string"},
            "category": {"type": "string"},
            "rationale": {"type": "string"},
            "files": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["kind", "skill_name", "category", "rationale", "files"],
        "additionalProperties": False,
    }


_ESCALATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["return_to_user", "apply_skill_updates_and_retry"]},
        "root_cause": {"type": "string"},
        "next_action": {"type": "string"},
        "user_summary": {"type": "string"},
        "notes": {"type": "array", "items": {"type": "string"}},
        "skill_updates": {"type": "array", "items": _skill_proposal_schema()},
        "skill_proposals": {"type": "array", "items": _skill_proposal_schema()},
    },
    "required": [
        "decision",
        "root_cause",
        "next_action",
        "user_summary",
        "notes",
        "skill_updates",
        "skill_proposals",
    ],
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


def _handler_summary_prompt(
    *,
    task: str,
    user_request: str,
    workspace: str,
    plan: Plan,
    trace: list[Any],
    review: dict[str, Any],
    success: bool,
    notes: list[str],
) -> str:
    payload = {
        "role": "handler_summary",
        "contract": (
            "You are the LoopMaster Handler writing the final user-facing reply after a robot run. "
            "Summarize what happened in the user's language. Do not expose internal prompts, raw JSON, "
            "stack traces, hidden auditor fields, or full low-level args. Be concise and concrete. "
            "For success, state what was completed and mention important skills/results. For failure, "
            "state what blocked completion and the most useful next action. Include the workspace path "
            "only if it helps the user inspect the run artifact. Return only JSON matching the schema."
        ),
        "task": task,
        "user_request": user_request,
        "workspace": workspace,
        "success": success,
        "plan": {
            "goal": plan.goal,
            "steps": [{"name": step.name, "args": step.args, "why": step.why} for step in plan.steps],
            "success_criteria": list(plan.success_criteria),
            "risks": list(plan.risks),
            "assumptions": list(plan.assumptions),
        },
        "trace": [
            {
                "index": step.index,
                "skill": step.skill,
                "ok": step.ok,
                "why": step.why,
                "result_summary": _trace_result_for_summary(step.result, ok=step.ok),
            }
            for step in trace
        ],
        "auditor_review": {
            "verdict": review.get("verdict"),
            "success": review.get("success"),
            "root_cause": review.get("root_cause"),
            "next_action": review.get("next_action"),
            "used_skills": review.get("used_skills") or [],
            "used_control_skills": review.get("used_control_skills") or [],
            "research_questions": review.get("research_questions") or [],
            "notes": review.get("notes") or [],
        },
        "handler_notes": list(notes),
    }
    return json.dumps(payload, indent=2, ensure_ascii=False, default=str)


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


def _escalate_auditor_retry(
    *,
    agent_client: SubagentClient | None,
    task: str,
    user_request: str,
    workspace: str,
    skills: SkillRegistry,
    plan: Plan,
    trace: list[Any],
    review: dict[str, Any],
    notes: list[str],
    reason: str,
    escalation_index: int,
) -> dict[str, Any] | None:
    if agent_client is None:
        return None
    role = f"auditor_escalation_{escalation_index}"
    payload = {
        "role": "auditor_escalation",
        "contract": (
            "You are a fresh Codex escalation session for a LoopMaster real-robot run. "
            "The normal Auditor retry loop has repeated or hit its limit. Decide whether "
            "the handler should return a concise failure summary to the user, or whether a "
            "repository-local skill defect can be repaired through gated skill updates and "
            "then retried. Do not ask the user for information that is already available in "
            "the trace, skill docs, or skill implementations. Prefer apply_skill_updates_and_retry "
            "only when you can provide complete replacement SKILL.md and/or policy.py content for "
            "registered skills. The handler will validate and compile those files before rerunning. "
            "If the defect is in core framework/platform code, external hardware state, licensing, "
            "physical clearance, or safety approval, choose return_to_user with a concrete summary "
            "and next_action. Return only JSON matching the schema."
        ),
        "escalation_reason": reason,
        "task": task,
        "user_request": user_request,
        "workspace": workspace,
        "plan": {
            "task": plan.task,
            "goal": plan.goal,
            "steps": [{"name": step.name, "args": step.args, "why": step.why} for step in plan.steps],
            "success_criteria": list(plan.success_criteria),
            "risks": list(plan.risks),
            "assumptions": list(plan.assumptions),
            "research_questions": list(plan.research_questions),
            "subagent_notes": list(plan.subagent_notes),
        },
        "trace": [step.to_dict() for step in trace],
        "auditor_review": review,
        "handler_notes": list(notes),
        "available_skills": _skills_for_escalation(skills),
        "decision_guidance": {
            "return_to_user": (
                "Use when no safe gated skill update can fix the issue, when the next step is "
                "operator/hardware intervention, or when repeated execution would be unsafe."
            ),
            "apply_skill_updates_and_retry": (
                "Use only with complete skill_proposals/skill_updates that can plausibly fix the "
                "observed root cause without arbitrary repository edits."
            ),
        },
    }
    result = agent_client.run_json(role=role, prompt=json.dumps(payload, indent=2, ensure_ascii=False), schema=_ESCALATION_SCHEMA)
    Path(workspace).joinpath(f"{role}.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )
    return result


def _skills_for_escalation(skills: SkillRegistry) -> list[dict[str, Any]]:
    out = []
    for skill in skills.list():
        body = str(getattr(skill, "body", "") or "")
        if len(body) > 2400:
            body = body[:2397].rstrip() + "..."
        out.append(
            {
                "name": skill.name,
                "category": skill.category,
                "description": skill.description,
                "args": skill.frontmatter.get("args", {}),
                "usage_markdown": body,
                "path": str(skill.path),
                "is_user": skill.is_user,
            }
        )
    return out


def _escalation_wants_skill_retry(escalated: dict[str, Any]) -> bool:
    if str(escalated.get("decision") or "") != "apply_skill_updates_and_retry":
        return False
    return bool(escalated.get("skill_proposals") or escalated.get("skill_updates"))


def _merge_escalation_review(review: dict[str, Any], escalated: dict[str, Any]) -> dict[str, Any]:
    merged = dict(review)
    root_cause = str(escalated.get("root_cause") or "").strip()
    next_action = str(escalated.get("next_action") or "").strip()
    if root_cause:
        merged["root_cause"] = root_cause
    if next_action:
        merged["next_action"] = next_action
    merged["escalation_decision"] = str(escalated.get("decision") or "")
    merged["escalation_notes"] = [str(item) for item in escalated.get("notes") or []]
    merged["skill_updates"] = escalated.get("skill_updates") or []
    merged["skill_proposals"] = escalated.get("skill_proposals") or []
    merged["success"] = False
    return merged


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


def _auditor_retry_signature(
    review: dict[str, Any],
    *,
    agent_client: SubagentClient | None,
) -> str:
    if agent_client is None:
        return ""
    if str(review.get("verdict") or "") != "retry" or review.get("success"):
        return ""
    payload = {
        "root_cause": str(review.get("root_cause") or ""),
        "next_action": str(review.get("next_action") or ""),
        "used_skills": review.get("used_skills") or [],
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)


def _trace_with_auditor_review(trace: list[Any], review: dict[str, Any]) -> list[Any]:
    return [
        *trace,
        TraceStep(
            index=len(trace) + 1,
            skill="auditor_review",
            args={},
            result={
                "verdict": review.get("verdict"),
                "root_cause": review.get("root_cause"),
                "next_action": review.get("next_action"),
                "notes": review.get("notes") or [],
            },
            ok=False,
            why="Auditor requested retry after reviewing closed-loop evidence.",
            role="auditor",
        ),
    ]


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


def _trace_result_for_summary(result: Any, *, ok: bool) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {"ok": ok, "value": _short_text(str(result), limit=240)}
    out: dict[str, Any] = {"ok": ok}
    if not ok:
        out["error"] = _short_text(str(result.get("error") or result), limit=240)
        return out
    for key in ("summary", "diagnosis", "side", "joint", "cycles", "sent_frames", "episode"):
        if key in result:
            out[key] = result[key]
    if "action_sent" in result:
        action = result["action_sent"]
        if isinstance(action, dict):
            out["action_sent_keys"] = sorted(str(key) for key in action)[:20]
        else:
            out["action_sent"] = _short_text(str(action), limit=240)
    observation = result.get("observation")
    if isinstance(observation, dict):
        out["state_keys"] = observation.get("state_keys") or sorted((observation.get("state") or {}).keys())[:20]
        if observation.get("images"):
            out["image_keys"] = sorted(str(key) for key in observation.get("images", {}))
    image = result.get("image")
    if isinstance(image, dict):
        out["image"] = {key: image.get(key) for key in ("shape", "dtype", "path") if key in image}
    return out


def _short_text(text: str, *, limit: int = 240) -> str:
    text = text.replace("\n", " ").strip()
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
