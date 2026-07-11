from __future__ import annotations

import json
import re
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
                prompt=_worker_prompt(plan=plan, workspace=workspace, skills=skills.list()),
                schema=_WORKER_SCHEMA,
            )
            (workspace.root / "worker_agent.json").write_text(
                json.dumps(worker_agent, indent=2, ensure_ascii=False, default=str) + "\n",
                encoding="utf-8",
            )
            if worker_agent.get("proceed") is False:
                trace: list[TraceStep] = []
                step = _record_call_result(
                    "worker_gate",
                    {},
                    "worker agent preflight blocked the plan before skill execution",
                    {
                        "ok": False,
                        "error": "worker preflight returned proceed=false",
                        "execution_notes": worker_agent.get("execution_notes") or [],
                        "concerns": worker_agent.get("concerns") or [],
                    },
                    trace,
                    workspace,
                    role="worker.preflight",
                )
                if progress is not None:
                    progress(f"worker preflight blocked execution: {_short_error(step.result)}")
                    for note in (worker_agent.get("execution_notes") or [])[:3]:
                        progress(f"worker note: {note}")
                    for concern in (worker_agent.get("concerns") or [])[:3]:
                        progress(f"worker concern: {concern}")
                workspace.write_summary(_summary_markdown(plan, trace, worker_agent=worker_agent))
                return trace

        context = SkillContext(platform=platform, workspace=workspace)
        trace: list[TraceStep] = []
        _attach_skill_caller(context, skills, trace, workspace)
        for call in plan.steps:
            try:
                resolved_args = _resolve_dynamic_args(call.args, context.memory)
            except Exception as exc:
                step = _record_call_result(
                    call.name,
                    dict(call.args),
                    call.why,
                    {"ok": False, "error": f"failed to resolve dynamic args: {type(exc).__name__}: {exc}"},
                    trace,
                    workspace,
                    role=self.role_name,
                )
                if progress is not None:
                    progress(f"skill `{call.name}` failed: {_short_error(step.result)}")
                break
            if progress is not None:
                progress(f"skill `{call.name}` args={resolved_args}")
            step = _execute_call(
                call.name,
                resolved_args,
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
    _store_result(context, name, result)
    return _record_call_result(name, args, why, result, trace, workspace, role=role)


def _attach_skill_caller(
    context: SkillContext,
    skills: SkillRegistry,
    trace: list[TraceStep],
    workspace: Workspace,
) -> None:
    def call_skill(name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        step = _execute_call(
            name,
            args or {},
            f"called by composite skill through context.call_skill",
            context,
            skills,
            trace,
            workspace,
            role="worker.subskill",
        )
        return step.result

    setattr(context, "call_skill", call_skill)
    setattr(context, "call", call_skill)


def _record_call_result(
    name: str,
    args: dict,
    why: str,
    result: dict[str, Any],
    trace: list[TraceStep],
    workspace: Workspace,
    *,
    role: str,
) -> TraceStep:
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


def _store_result(context: SkillContext, name: str, result: dict[str, Any]) -> None:
    context.memory[name] = result
    context.memory.setdefault("skills", {})[name] = result
    context.memory.setdefault("trace", []).append({"skill": name, "result": result})
    context.memory["last_result"] = result


def _resolve_dynamic_args(value: Any, memory: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            return _lookup_ref(memory, str(value["$ref"]))
        return {key: _resolve_dynamic_args(item, memory) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_dynamic_args(item, memory) for item in value]
    if isinstance(value, str):
        return _resolve_string_refs(value, memory)
    return value


def _resolve_string_refs(value: str, memory: dict[str, Any]) -> Any:
    if value.startswith("$") and _REF_PATTERN.fullmatch(value[1:]):
        return _lookup_ref(memory, value[1:])
    full = _TEMPLATE_PATTERN.fullmatch(value)
    if full:
        return _lookup_ref(memory, full.group(1).strip())

    def replace(match: re.Match[str]) -> str:
        resolved = _lookup_ref(memory, match.group(1).strip())
        return str(resolved)

    return _TEMPLATE_PATTERN.sub(replace, value)


def _lookup_ref(memory: dict[str, Any], path: str) -> Any:
    current: Any = memory
    for part in path.split("."):
        if not part:
            continue
        if isinstance(current, dict):
            if part not in current:
                raise KeyError(f"unknown context ref: {path}")
            current = current[part]
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError) as exc:
                raise KeyError(f"unknown context ref: {path}") from exc
        else:
            raise KeyError(f"unknown context ref: {path}")
    return current


_REF_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*")
_TEMPLATE_PATTERN = re.compile(r"\$\{([^}]+)\}")


def _is_control_skill(name: str) -> bool:
    return name in {
        "init_arms",
        "move_arm_ee",
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


def _worker_prompt(*, plan: Plan, workspace: Workspace, skills: list[Any] | None = None) -> str:
    skills = skills or []
    payload = {
        "role": "worker",
        "contract": (
            "You are the LoopMaster Worker subagent. Review the plan before local code executes "
            "registered platform skills. Do not execute tools yourself, do not edit files, and do "
            "not add unregistered skills. A plan step named create_skill is a registered meta skill "
            "when it appears in the provided plan; do not reject it merely because it authors a "
            "new skill, but do reject malformed create_skill args or unsafe immediate motion. "
            "A plan step named init_arms is the registered arm initialization skill: it loads the "
            "repository init config, validates joint limits, commands both arms, and verifies state "
            "feedback. Do not reject init_arms merely because it moves both arms to a fixed pose; "
            "do reject hand-inlined fixed arm initialization that bypasses registered skills. "
            "For real robot control, action_sent only means the command was accepted. Require "
            "periodic or post-action observe feedback and compare actual state with expected "
            "motion; if targets are issued too quickly to move visibly or feedback does not "
            "change as expected, return proceed=false or let the trace show the mismatch rather "
            "than treating the run as physically complete. For an explicit low-level operator "
            "request to move the base for a bounded short duration, allow a conservative "
            "set_base_velocity step when it has duration_s<=5.0, abs(x)<=0.1, abs(y)<=0.1, "
            "abs(theta)<=0.2, plus stop_motion and post-motion observe/settling evidence. "
            "Do not block such a plan solely because there is no rear camera, navigation status, "
            "or registered path-clearance skill; missing clearance is a risk/note unless a "
            "registered safety or clearance skill has explicitly returned unsafe/path_clearance=false/abort. "
            "Do block unbounded, high-speed, no-stop, or explicitly unsafe base motion. Every control "
            "step should have timing semantics appropriate to the actuator: duration_s for base velocity, "
            "settle_s for gripper/lift/stop feedback, and velocity_limit_rad_s plus observe/settling for "
            "arm motion. Treat repeated control commands with no duration or settle window as a plan "
            "defect unless they are explicit zero-duration dry checks. Allow the timer meta skill for "
            "wall-clock/monotonic time evidence or non-actuator waits; do not require timer when an "
            "actuator skill already has a suitable duration_s or settle_s argument. "
            "Return proceed=false only for a concrete safety or registry issue."
        ),
        "workspace": str(workspace.root),
        "plan": {
            "task": plan.task,
            "goal": plan.goal,
            "steps": [_call_to_dict(step) for step in plan.steps],
            "research_questions": list(plan.research_questions),
            "risks": list(plan.risks),
        },
        "available_skills": [
            {
                "name": skill.name,
                "category": skill.category,
                "description": skill.description,
                "args": skill.frontmatter.get("args", {}),
                "usage_markdown": _skill_usage_markdown(skill),
            }
            for skill in skills
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False, default=str)


def _call_to_dict(step: SkillCall) -> dict[str, Any]:
    return {"name": step.name, "args": step.args, "why": step.why}


def _skill_usage_markdown(skill: Any, *, limit: int = 1200) -> str:
    body = str(getattr(skill, "body", "") or "").strip()
    if len(body) <= limit:
        return body
    return body[: limit - 3].rstrip() + "..."


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
