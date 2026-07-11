from __future__ import annotations

import json
from typing import Any

from loopmaster_agentic.agents.codex_subagent import SubagentClient
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
        agent_client: SubagentClient | None = None,
    ) -> dict[str, Any]:
        failed = [step for step in trace if not step.ok]
        used_skills = {step.skill for step in trace}
        control_skills = {
            "move_arm_ee",
            "move_arm_joints",
            "play_cache_traj",
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
            next_action = "Inspect the failed skill result and rerun after platform issue is corrected."
        elif sim_leak:
            verdict = "blocked"
            root_cause = "simulation-only evidence leaked into real-robot run"
            next_action = "Remove sim-only tool/evidence from the plan."
        elif not trace:
            verdict = "blocked"
            root_cause = "worker produced no trace"
            next_action = "Generate an executable plan with at least one repository skill."
        elif plan.research_questions:
            verdict = "research_needed"
            root_cause = "planner found missing task-level capability"
            next_action = (
                "Use the trace as evidence to author or approve a task-level skill under "
                "LOOPMASTER_SKILL_ROOT, then rerun the handler."
            )
        elif used_control and "stop_motion" not in used_skills and not _has_internal_safety_stop(trace):
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
        if agent_client is not None:
            agent_review = agent_client.run_json(
                role=self.role_name,
                prompt=_auditor_prompt(plan=plan, trace=trace, candidate_review=review),
                schema=_AUDITOR_SCHEMA,
            )
            (workspace.root / "auditor_agent.json").write_text(
                json.dumps(agent_review, indent=2, ensure_ascii=False, default=str) + "\n",
                encoding="utf-8",
            )
            review = _review_from_agent(agent_review, fallback=review)
            review = _relax_play_cache_traj_review(review, trace=trace)
        workspace.write_review(_review_markdown(plan, review))
        return review


_SKILL_FILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "content": {"type": "string"},
    },
    "required": ["path", "content"],
    "additionalProperties": False,
}


_SKILL_PROPOSAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {"type": "string"},
        "skill_name": {"type": "string"},
        "category": {"type": "string"},
        "rationale": {"type": "string"},
        "files": {"type": "array", "items": _SKILL_FILE_SCHEMA},
    },
    "required": ["kind", "skill_name", "category", "rationale", "files"],
    "additionalProperties": False,
}


_AUDITOR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["done", "retry", "blocked", "research_needed"]},
        "root_cause": {"type": "string"},
        "next_action": {"type": "string"},
        "used_skills": {"type": "array", "items": {"type": "string"}},
        "used_control_skills": {"type": "array", "items": {"type": "string"}},
        "sim_leak": {"type": "array", "items": {"type": "string"}},
        "research_questions": {"type": "array", "items": {"type": "string"}},
        "success": {"type": "boolean"},
        "notes": {"type": "array", "items": {"type": "string"}},
        "skill_updates": {"type": "array", "items": _SKILL_PROPOSAL_SCHEMA},
        "skill_proposals": {"type": "array", "items": _SKILL_PROPOSAL_SCHEMA},
    },
    "required": [
        "verdict",
        "root_cause",
        "next_action",
        "used_skills",
        "used_control_skills",
        "sim_leak",
        "research_questions",
        "success",
        "notes",
        "skill_updates",
        "skill_proposals",
    ],
    "additionalProperties": False,
}


def _auditor_prompt(*, plan: Plan, trace: list[TraceStep], candidate_review: dict[str, Any]) -> str:
    payload = {
        "role": "auditor",
        "contract": (
            "You are the LoopMaster Auditor subagent. Independently review the plan and trace. "
            "Do not execute tools or edit files. Classify the run as done, retry, blocked, or "
            "research_needed. Do not ask the user for information that is available in the "
            "repository or in skill implementations; classify repository-local skill/schema/"
            "trace-output defects as retry or blocked with a concrete skill repair next_action. "
            "For real robot motion, never mark done from action_sent alone. Check whether the "
            "trace includes periodic or post-action observe feedback showing the actual state "
            "changed as expected. Feedback is asynchronous: prefer observed range, direction, "
            "multi-sample trends, and settling-window evidence over a single exact equality check. "
            "A final sample between the prior command and return target may indicate settling or "
            "sensor lag, not necessarily failure. If commands were acknowledged but feedback is "
            "missing, unchanged, too fast to observe, clamped, or consistently inconsistent with "
            "the target, classify as retry or blocked and diagnose the likely cause. "
            "For low-level motion requests such as a timed base velocity command, mark the run "
            "done when the requested motion duration/velocity and final stopped state are supported "
            "by feedback, even if optional visual evidence is incomplete. Do not convert generic "
            "object detections from capture_image or grounded_sam2 into a path_clearance=false "
            "verdict unless a registered clearance/safety skill explicitly returned unsafe, "
            "path_clearance=false, or an abort decision before motion. Treat ambiguous perception "
            "annotations as notes or risks, not automatic failure of an otherwise completed "
            "low-level control command. "
            "For play_cache_traj, use the skill's top-level ok/sent_frames/episode result as the "
            "contracted completion evidence. This skill replays a fixed cached trajectory and cannot "
            "produce in-trajectory grasp/contact/retention feedback. Do not classify it as retry only "
            "because visual/contact feedback is absent, gripper feedback is stale, or its nested "
            "return-to-init verification is missing/disabled/stale, as long as play_cache_traj itself "
            "returned ok=true and sent at least one frame. "
            "When a repository-local skill defect can be repaired from the trace and known skill "
            "contract, include a skill_proposals entry. Use kind='update_skill' for existing "
            "registered skills and kind='new_skill' for a new user skill; new skills are applied "
            "through the registered create_skill skill under LOOPMASTER_SKILL_ROOT. "
            "Each proposal must include complete replacement content for SKILL.md and/or policy.py; "
            "new_skill proposals must include both files. A SKILL.md-only update is appropriate "
            "when clearer usage guidance can make the next Strategist call the existing runtime "
            "correctly, including argument shape, frame convention, path convention, or sequencing. "
            "If the same skill fails again with the same root cause after a documentation-only "
            "update, explicitly consider whether policy.py must change because the runtime behavior "
            "cannot be fixed by different planning arguments alone. "
            "Reserve research_needed for missing task intent, external runtime state, hardware "
            "safety approval, or genuinely absent learned capabilities. Return only JSON "
            "matching the schema."
        ),
        "plan": {
            "task": plan.task,
            "goal": plan.goal,
            "steps": [{"name": step.name, "args": step.args, "why": step.why} for step in plan.steps],
            "research_questions": list(plan.research_questions),
        },
        "trace": [step.to_dict() for step in trace],
        "candidate_review": candidate_review,
        "skill_proposal_schema": {
            "kind": "update_skill or new_skill",
            "skill_name": "registered skill name",
            "category": "required for new_skill; e.g. control",
            "rationale": "why this skill should change",
            "files": [
                {
                    "path": "SKILL.md or policy.py only",
                    "content": "complete replacement file content",
                }
            ],
        },
    }
    return json.dumps(payload, indent=2, ensure_ascii=False, default=str)


def _review_from_agent(data: dict[str, Any], *, fallback: dict[str, Any]) -> dict[str, Any]:
    verdict = str(data.get("verdict") or fallback["verdict"])
    if verdict not in {"done", "retry", "blocked", "research_needed"}:
        verdict = str(fallback["verdict"])
    review = {
        "verdict": verdict,
        "root_cause": str(data.get("root_cause") or ""),
        "next_action": str(data.get("next_action") or ""),
        "used_skills": _strings(data.get("used_skills")) or list(fallback.get("used_skills") or []),
        "used_control_skills": _strings(data.get("used_control_skills"))
        or list(fallback.get("used_control_skills") or []),
        "sim_leak": _strings(data.get("sim_leak")) or list(fallback.get("sim_leak") or []),
        "research_questions": _strings(data.get("research_questions"))
        or list(fallback.get("research_questions") or []),
        "success": bool(data.get("success")) and verdict == "done",
        "skill_updates": _skill_updates(data.get("skill_updates")),
        "skill_proposals": _skill_updates(data.get("skill_proposals")),
    }
    codex = data.get("_codex")
    if isinstance(codex, dict):
        review["_codex"] = dict(codex)
    notes = _strings(data.get("notes"))
    if notes:
        review["notes"] = notes
    return review


def _relax_play_cache_traj_review(review: dict[str, Any], *, trace: list[TraceStep]) -> dict[str, Any]:
    replay_step = next((step for step in trace if step.skill == "play_cache_traj"), None)
    if replay_step is None or not replay_step.ok:
        return review
    if any(not step.ok for step in trace if step.skill != "play_cache_traj"):
        return review
    result = replay_step.result if isinstance(replay_step.result, dict) else {}
    if not result.get("ok"):
        return review
    try:
        sent_frames = int(result.get("sent_frames") or 0)
    except (TypeError, ValueError):
        sent_frames = 0
    if sent_frames <= 0:
        return review

    verdict = str(review.get("verdict") or "")
    root_cause = str(review.get("root_cause") or "").lower()
    retry_markers = (
        "feedback",
        "grasp",
        "contact",
        "retention",
        "init_arm",
        "init_arms",
        "return-to-init",
        "return to init",
        "verification",
    )
    if verdict == "done" or not any(marker in root_cause for marker in retry_markers):
        return review

    relaxed = dict(review)
    relaxed["verdict"] = "done"
    relaxed["root_cause"] = ""
    relaxed["next_action"] = ""
    relaxed["success"] = True
    notes = list(relaxed.get("notes") or [])
    notes.append("relaxed play_cache_traj success: fixed trajectory replay cannot provide in-trajectory grasp feedback")
    relaxed["notes"] = notes
    return relaxed


def _has_internal_safety_stop(trace: list[TraceStep]) -> bool:
    for step in trace:
        if step.skill != "play_cache_traj" or not isinstance(step.result, dict):
            continue
        stop = ((step.result.get("return_to_init") or {}).get("stop_motion") or {})
        if isinstance(stop, dict) and stop.get("ok"):
            return True
    return False


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _skill_updates(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


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
