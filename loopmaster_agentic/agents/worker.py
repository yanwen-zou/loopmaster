from __future__ import annotations

from loopmaster_agentic.agents.workspace import Workspace
from loopmaster_agentic.core.types import Plan, TraceStep
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
    ) -> list[TraceStep]:
        context = SkillContext(platform=platform, workspace=workspace)
        trace: list[TraceStep] = []
        for call in plan.steps:
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
            if not step.ok:
                if call.name != "stop_motion" and skills.get("stop_motion") is not None:
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
                if not post.ok:
                    break
        workspace.write_summary(_summary_markdown(plan, trace))
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


def _summary_markdown(plan: Plan, trace: list[TraceStep]) -> str:
    lines = [
        f"# Worker Summary: {plan.task}",
        "",
        f"Executed {len(trace)} skill call(s).",
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
