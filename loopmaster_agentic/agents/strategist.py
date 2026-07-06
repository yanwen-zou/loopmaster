from __future__ import annotations

import ast
import re
from typing import Any

from loopmaster_agentic.agents.workspace import Workspace
from loopmaster_agentic.core.types import Plan, SkillCall
from loopmaster_agentic.skills.registry import SkillRegistry


class Strategist:
    """Plans a registry-grounded real-robot subagent run."""

    role_name = "strategist"

    def plan(
        self,
        *,
        task: str,
        user_request: str,
        workspace: Workspace,
        skills: SkillRegistry,
    ) -> Plan:
        available = {skill.name for skill in skills.list()}
        text = user_request.strip() or task
        lowered = text.lower()
        steps: list[SkillCall] = []
        assumptions: list[str] = []
        research_questions: list[str] = []
        subagent_notes = [
            f"Strategist inspected {len(available)} registered skill(s).",
            "Plan uses only discovered skills; no simulation-only predicate is assumed.",
        ]

        if "observe" in available:
            steps.append(
                SkillCall(
                    "observe",
                    {"include_images": True, "include_state": True},
                    "establish live robot state before choosing or executing control",
                )
            )

        if "capture_image" in available and _wants_visual_evidence(lowered):
            steps.append(
                SkillCall(
                    "capture_image",
                    {"camera": _requested_camera(lowered), "required": False},
                    "retain visual evidence for planning and audit",
                )
            )

        control_added = False
        control_added |= _maybe_add_base_velocity(lowered, available, steps, research_questions)
        control_added |= _maybe_add_lift_height(lowered, available, steps, research_questions)
        control_added |= _maybe_add_gripper(lowered, available, steps, research_questions, assumptions)
        control_added |= _maybe_add_arm_joints(lowered, available, steps, research_questions)
        control_added |= _maybe_add_raw_action(text, lowered, available, steps, research_questions)

        if not control_added and _looks_like_manipulation_goal(lowered):
            research_questions.append(
                "Goal appears to require a task-specific manipulation policy, "
                "but only base perception/control skills are registered."
            )
            subagent_notes.append(
                "This run should gather state evidence and surface the missing learned skill."
            )

        if "stop_motion" in available:
            steps.append(
                SkillCall(
                    "stop_motion",
                    {"reason": "handler end-of-run safety stop"},
                    "leave the real platform stationary before returning control",
                )
            )

        plan = Plan(
            task=task,
            goal=user_request.strip() or task,
            steps=[step for step in steps if step.name in available],
            success_criteria=[
                "Every planned tool call is backed by the LoopMaster skill registry.",
                "Worker records live observation or explicit platform feedback.",
                "Worker stops the platform after any control-oriented run.",
                "Auditor must report research_needed for goals that lack an executable task skill.",
            ],
            risks=[
                "Low-level motion is only planned when the request includes explicit numeric arguments.",
                "Task-specific manipulation policies are intentionally absent until learned under the user skill root.",
                "Real hardware execution requires operator safety review before enabling learned motion skills.",
            ],
            assumptions=assumptions,
            research_questions=research_questions,
            subagent_notes=subagent_notes,
        )
        workspace.write_plan(plan.to_markdown())
        return plan


FLOAT = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)"


def _wants_visual_evidence(text: str) -> bool:
    keywords = (
        "inspect",
        "look",
        "observe",
        "camera",
        "image",
        "photo",
        "visual",
        "scene",
        "state",
        "explore",
        "research",
        "看",
        "观察",
        "相机",
        "图像",
        "场景",
        "探索",
    )
    return any(item in text for item in keywords)


def _requested_camera(text: str) -> str:
    if "left_wrist" in text or "left wrist" in text or "左腕" in text:
        return "left_wrist"
    if "right_wrist" in text or "right wrist" in text or "右腕" in text:
        return "right_wrist"
    return "front"


def _maybe_add_base_velocity(
    text: str,
    available: set[str],
    steps: list[SkillCall],
    research_questions: list[str],
) -> bool:
    if "set_base_velocity" not in available:
        return False
    if not _mentions_any(text, ("base velocity", "set_base_velocity", "drive", "chassis", "底盘")):
        return False
    x = _extract_float(text, ("x", "x.vel"))
    y = _extract_float(text, ("y", "y.vel"))
    theta = _extract_float(text, ("theta", "theta.vel", "yaw"))
    if x is None or y is None or theta is None:
        research_questions.append(
            "Base motion requested but x, y, and theta numeric velocity arguments were not all provided."
        )
        return False
    steps.append(
        SkillCall(
            "set_base_velocity",
            {"x": x, "y": y, "theta": theta},
            "execute explicitly requested chassis velocity",
        )
    )
    return True


def _maybe_add_lift_height(
    text: str,
    available: set[str],
    steps: list[SkillCall],
    research_questions: list[str],
) -> bool:
    if "set_lift_height" not in available:
        return False
    if not _mentions_any(text, ("set_lift_height", "lift", "height", "升降", "高度")):
        return False
    height = _extract_float(text, ("height_mm", "height", "lift"))
    if height is None:
        research_questions.append(
            "Lift motion requested but no numeric height_mm value was provided."
        )
        return False
    steps.append(
        SkillCall(
            "set_lift_height",
            {"height_mm": height},
            "execute explicitly requested lift height target",
        )
    )
    return True


def _maybe_add_gripper(
    text: str,
    available: set[str],
    steps: list[SkillCall],
    research_questions: list[str],
    assumptions: list[str],
) -> bool:
    if "set_gripper" not in available:
        return False
    if not _mentions_any(text, ("set_gripper", "gripper", "夹爪", "爪")):
        return False
    side = _extract_side(text)
    position = _extract_float(text, ("position", "pos", "gripper"))
    if side is None:
        research_questions.append("Gripper command requested but side=left/right was not provided.")
        return False
    if position is None:
        research_questions.append("Gripper command requested but no numeric position was provided.")
        assumptions.append("Open/close words are not mapped to numbers because the driver convention is hardware-specific.")
        return False
    steps.append(
        SkillCall(
            "set_gripper",
            {"side": side, "position": position},
            "execute explicitly requested gripper position",
        )
    )
    return True


def _maybe_add_arm_joints(
    text: str,
    available: set[str],
    steps: list[SkillCall],
    research_questions: list[str],
) -> bool:
    if "move_arm_joints" not in available:
        return False
    if not _mentions_any(text, ("move_arm_joints", "arm joints", "joint", "关节")):
        return False
    side = _extract_side(text)
    positions = _extract_positions(text)
    if side is None:
        research_questions.append("Arm joint command requested but side=left/right was not provided.")
        return False
    if positions is None:
        research_questions.append("Arm joint command requested but positions=[...] with 7 values was not provided.")
        return False
    steps.append(
        SkillCall(
            "move_arm_joints",
            {"side": side, "positions": positions},
            "execute explicitly requested arm joint targets",
        )
    )
    return True


def _maybe_add_raw_action(
    original_text: str,
    text: str,
    available: set[str],
    steps: list[SkillCall],
    research_questions: list[str],
) -> bool:
    if "send_action" not in available:
        return False
    if "send_action" not in text and "action=" not in text and "action:" not in text:
        return False
    action = _extract_mapping(original_text, "action")
    if not isinstance(action, dict):
        research_questions.append("Raw action requested but action={...} was not parseable.")
        return False
    steps.append(
        SkillCall(
            "send_action",
            {"action": action},
            "execute explicitly supplied low-level action dictionary",
        )
    )
    return True


def _looks_like_manipulation_goal(text: str) -> bool:
    keywords = (
        "pick",
        "place",
        "grasp",
        "grab",
        "stack",
        "open",
        "close",
        "handover",
        "push",
        "press",
        "learn",
        "skill",
        "auto research",
        "抓",
        "拿",
        "放",
        "堆",
        "按",
        "打开",
        "关闭",
        "学习",
        "技能",
        "自主",
    )
    return any(item in text for item in keywords)


def _mentions_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle in text for needle in needles)


def _extract_float(text: str, keys: tuple[str, ...]) -> float | None:
    for key in keys:
        pattern = rf"(?:^|[\s,;{{(]){re.escape(key)}\s*[:=]\s*({FLOAT})(?:$|[\s,;)}}])"
        match = re.search(pattern, text)
        if match:
            return float(match.group(1))
    return None


def _extract_side(text: str) -> str | None:
    if re.search(r"(?:^|[\s,;{(])side\s*[:=]\s*left(?:$|[\s,;)}])", text) or " left " in f" {text} " or "左" in text:
        return "left"
    if re.search(r"(?:^|[\s,;{(])side\s*[:=]\s*right(?:$|[\s,;)}])", text) or " right " in f" {text} " or "右" in text:
        return "right"
    return None


def _extract_positions(text: str) -> list[float] | dict[str, float] | None:
    value = _extract_mapping_or_list(text, "positions")
    if isinstance(value, list) and len(value) == 7:
        return [float(item) for item in value]
    if isinstance(value, dict):
        return {str(key): float(val) for key, val in value.items()}
    return None


def _extract_mapping(text: str, key: str) -> dict[str, Any] | None:
    value = _extract_mapping_or_list(text, key)
    return value if isinstance(value, dict) else None


def _extract_mapping_or_list(text: str, key: str) -> Any:
    match = re.search(rf"{re.escape(key)}\s*[:=]\s*([\[{{])", text)
    if not match:
        return None
    start = match.start(1)
    opener = text[start]
    closer = "]" if opener == "[" else "}"
    depth = 0
    for idx in range(start, len(text)):
        char = text[idx]
        if char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                raw = text[start : idx + 1]
                try:
                    return ast.literal_eval(raw)
                except (SyntaxError, ValueError):
                    return None
    return None
