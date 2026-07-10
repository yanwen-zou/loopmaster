from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from loopmaster_agentic.agents.codex_subagent import SubagentClient
from loopmaster_agentic.agents.workspace import Workspace
from loopmaster_agentic.core.types import Plan, SkillCall, TraceStep
from loopmaster_agentic.platform.base import RobotPlatform
from loopmaster_agentic.skills.registry import SkillContext, SkillRegistry


class Worker:
    """Executes planned skills and records closed-loop evidence."""

    role_name = "worker"

    def execute(
        self,
        *,
        plan: Plan,
        workspace: Workspace,
        platform: RobotPlatform,
        skills: SkillRegistry,
        agent_client: SubagentClient | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> list[TraceStep]:
        worker_agent: dict[str, Any] | None = None
        if agent_client is not None:
            worker_agent = agent_client.run_json(
                role=self.role_name,
                prompt=_worker_prompt(plan=plan, workspace=workspace),
                schema=_WORKER_SCHEMA,
            )
            (workspace.root / "worker_agent.json").write_text(
                json.dumps(worker_agent, indent=2, ensure_ascii=False, default=str) + "\n",
                encoding="utf-8",
            )
            if worker_agent.get("proceed") is False:
                workspace.write_summary(_summary_markdown(plan, [], worker_agent=worker_agent))
                return []

        context = SkillContext(platform=platform, workspace=workspace)
        trace: list[TraceStep] = []
        for call in plan.steps:
            if progress is not None:
                progress(f"skill `{call.name}` args={call.args}")
            step = _execute_call(
                call.name,
                call.args,
                call.why,
                context,
                skills,
                trace,
                workspace,
                role=self.role_name,
            )
            if progress is not None:
                if step.ok:
                    progress(f"skill `{call.name}` ok=True")
                else:
                    progress(f"skill `{call.name}` failed: {_short_error(step.result)}")
            if not step.ok:
                if call.name != "stop_motion" and skills.get("stop_motion") is not None:
                    if progress is not None:
                        progress("skill `stop_motion` args={'reason': safety abort}")
                    _execute_call(
                        "stop_motion",
                        {"reason": f"worker abort after failed {call.name}"},
                        "stop platform after failed skill",
                        context,
                        skills,
                        trace,
                        workspace,
                        role="worker.safety",
                    )
                break
            if _is_control_skill(call.name) and skills.get("observe") is not None:
                if progress is not None:
                    progress("skill `observe` args={'include_images': True, 'include_state': True}")
                post = _execute_call(
                    "observe",
                    {"include_images": True, "include_state": True},
                    f"observe live state after {call.name}",
                    context,
                    skills,
                    trace,
                    workspace,
                    role="worker.monitor",
                )
                if progress is not None:
                    if post.ok:
                        progress("skill `observe` ok=True")
                    else:
                        progress(f"skill `observe` failed: {_short_error(post.result)}")
                if not post.ok:
                    break
        workspace.write_summary(_summary_markdown(plan, trace, worker_agent=worker_agent))
        return trace


def _execute_call(
    name: str,
    args: dict,
    why: str,
    context: SkillContext,
    skills: SkillRegistry,
    trace: list[TraceStep],
    workspace: Workspace,
    *,
    role: str,
) -> TraceStep:
    try:
        result = skills.dispatch(name, context, args)
    except Exception as exc:
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    step = TraceStep(
        index=len(trace) + 1,
        skill=name,
        args=dict(args),
        result=result,
        ok=bool(result.get("ok", False)),
        why=why,
        role=role,
    )
    trace.append(step)
    workspace.append_trace(step.to_dict())
    return step


def _is_control_skill(name: str) -> bool:
    return name in {
        "send_action",
        "move_arm_joints",
        "set_gripper",
        "set_base_velocity",
        "set_lift_height",
    }


def _short_error(result: dict[str, Any]) -> str:
    text = str(result.get("error") or result)
    return text if len(text) <= 240 else text[:237] + "..."


_WORKER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "proceed": {"type": "boolean"},
        "execution_notes": {"type": "array", "items": {"type": "string"}},
        "concerns": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["proceed", "execution_notes", "concerns"],
    "additionalProperties": False,
}


def _worker_prompt(*, plan: Plan, workspace: Workspace) -> str:
    payload = {
        "role": "worker",
        "contract": (
            "You are the LoopMaster Worker subagent. Review the plan before local code executes "
            "registered platform skills. Do not execute tools yourself, do not edit files, and do "
            "not add unregistered skills. Return proceed=false only for a concrete safety or "
            "registry issue."
        ),
        "workspace": str(workspace.root),
        "plan": {
            "task": plan.task,
            "goal": plan.goal,
            "steps": [_call_to_dict(step) for step in plan.steps],
            "research_questions": list(plan.research_questions),
            "risks": list(plan.risks),
        },
    }
    return json.dumps(payload, indent=2, ensure_ascii=False, default=str)


def _call_to_dict(step: SkillCall) -> dict[str, Any]:
    return {"name": step.name, "args": step.args, "why": step.why}


def _summary_markdown(
    plan: Plan,
    trace: list[TraceStep],
    *,
    worker_agent: dict[str, Any] | None = None,
) -> str:
    lines = [
        f"# Worker Summary: {plan.task}",
        "",
        f"Executed {len(trace)} skill call(s).",
    ]
    if worker_agent is not None:
        lines += [
            "",
            "## Codex Worker",
            f"- Proceed: {worker_agent.get('proceed')}",
        ]
        for note in worker_agent.get("execution_notes") or []:
            lines.append(f"- Note: {note}")
        for concern in worker_agent.get("concerns") or []:
            lines.append(f"- Concern: {concern}")
    lines += [
        "",
        "## Trace",
    ]
    for step in trace:
        lines.append(
            f"- {step.index}. `{step.skill}` role={step.role} ok={step.ok} "
            f"why={step.why!r} result={step.result}"
        )
    if not trace:
        lines.append("- (no calls)")
    return "\n".join(lines).rstrip() + "\n"
