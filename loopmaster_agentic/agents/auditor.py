from __future__ import annotations

from typing import Any

from loopmaster_agentic.agents.workspace import Workspace
from loopmaster_agentic.core.types import Plan, TraceStep


class Auditor:
    """Reviews execution evidence and proposes the next subagent move."""

    role_name = "auditor"

    def review(
        self,
        *,
        plan: Plan,
        trace: list[TraceStep],
        workspace: Workspace,
    ) -> dict[str, Any]:
        failed = [step for step in trace if not step.ok]
        used_skills = {step.skill for step in trace}
        control_skills = {
            "send_action",
            "move_arm_joints",
            "set_gripper",
            "set_base_velocity",
            "set_lift_height",
        }
        used_control = sorted(used_skills & control_skills)
        simulation_terms = {"robotwin", "sim", "simulation", "check_task_success"}
        sim_leak = sorted(term for term in simulation_terms if any(term in str(step.result).lower() for step in trace))
        if failed:
            verdict = "retry"
            root_cause = f"skill `{failed[0].skill}` failed"
            next_action = "Inspect the failed base skill result and rerun after platform issue is corrected."
        elif sim_leak:
            verdict = "blocked"
            root_cause = "simulation-only evidence leaked into real-robot run"
            next_action = "Remove sim-only tool/evidence from the plan."
        elif not trace:
            verdict = "blocked"
            root_cause = "worker produced no trace"
            next_action = "Generate an executable plan with at least one base skill."
        elif plan.research_questions:
            verdict = "research_needed"
            root_cause = "planner found missing task-level capability"
            next_action = (
                "Use the trace as evidence to author or approve a learned skill under "
                "LOOPMASTER_SKILL_ROOT, then rerun the handler."
            )
        elif used_control and "stop_motion" not in used_skills:
            verdict = "blocked"
            root_cause = "control skill ran without a final stop_motion safety call"
            next_action = "Append stop_motion to the plan before any further real-robot run."
        else:
            verdict = "done"
            root_cause = ""
            next_action = ""
        review = {
            "verdict": verdict,
            "root_cause": root_cause,
            "next_action": next_action,
            "used_skills": sorted(used_skills),
            "used_control_skills": used_control,
            "sim_leak": sim_leak,
            "research_questions": list(plan.research_questions),
            "success": verdict == "done",
        }
        workspace.write_review(_review_markdown(plan, review))
        return review


def _review_markdown(plan: Plan, review: dict[str, Any]) -> str:
    lines = [
        f"# Audit: {plan.task}",
        "",
        f"**Verdict**: `{review['verdict']}`",
        f"**Root cause**: {review['root_cause'] or '(none)'}",
        f"**Next action**: {review['next_action'] or '(none)'}",
        "",
        "## Evidence",
        f"- Used skills: {', '.join(review['used_skills']) or '(none)'}",
        f"- Used control skills: {', '.join(review['used_control_skills']) or '(none)'}",
        f"- Simulation leak terms: {', '.join(review['sim_leak']) or '(none)'}",
    ]
    if review["research_questions"]:
        lines += ["", "## Research Needed"]
        lines.extend(f"- {item}" for item in review["research_questions"])
    return "\n".join(lines).rstrip() + "\n"
