from __future__ import annotations

import ast
import json
import re
from typing import Any

from loopmaster_agentic.agents.codex_subagent import SubagentClient
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
        agent_client: SubagentClient | None = None,
    ) -> Plan:
        discovered_skills = skills.list()
        available = {skill.name for skill in discovered_skills}
        text = user_request.strip() or task
        lowered = text.lower()
        steps: list[SkillCall] = []
        assumptions: list[str] = []
        research_questions: list[str] = []
        subagent_notes = [
            f"Strategist inspected {len(available)} registered skill(s).",
            "Plan uses only discovered skills; no simulation-only predicate is assumed.",
        ]
        object_conditioned_grasp = _wants_object_conditioned_grasp(lowered)

        if "observe" in available:
            steps.append(
                SkillCall(
                    "observe",
                    {"include_images": True, "include_state": True},
                    "establish live robot state before choosing or executing control",
                )
            )

        if "capture_image" in available and (_wants_visual_evidence(lowered) or object_conditioned_grasp):
            capture_args: dict[str, Any]
            if object_conditioned_grasp:
                capture_args = {"source": "d435_rgbd", "camera": "d435", "required": True}
            else:
                capture_args = {"camera": _requested_camera(lowered), "required": False}
            steps.append(
                SkillCall(
                    "capture_image",
                    capture_args,
                    "retain visual evidence for planning and audit",
                )
            )

        if "grounded_sam2" in available and object_conditioned_grasp:
            steps.append(
                SkillCall(
                    "grounded_sam2",
                    {
                        "text_prompt": _requested_object_prompt(text),
                        "img_path": {"$ref": "capture_image.rgb.path"},
                    },
                    "segment the requested object before grasp detection",
                )
            )

        if "detect_grasps" in available and _wants_grasp_detection(lowered):
            grasp_args: dict[str, Any] = {"check_only": _wants_anygrasp_check_only(lowered), "top_k": 5}
            if object_conditioned_grasp:
                grasp_args["region_object_id"] = 1
                grasp_args["color_path"] = {"$ref": "capture_image.rgb.path"}
                grasp_args["depth_path"] = {"$ref": "capture_image.depth.path"}
                grasp_args["seg_mask_path"] = {"$ref": "grounded_sam2.seg_mask_path"}
            steps.append(
                SkillCall(
                    "detect_grasps",
                    grasp_args,
                    "run AnyGrasp grasp perception or readiness check",
                )
            )

        control_added = False
        control_added |= _maybe_add_base_velocity(lowered, available, steps, research_questions)
        control_added |= _maybe_add_lift_height(lowered, available, steps, research_questions)
        control_added |= _maybe_add_gripper(lowered, available, steps, research_questions, assumptions)
        control_added |= _maybe_add_arm_ee(text, lowered, available, steps, research_questions)
        control_added |= _maybe_add_arm_joints(lowered, available, steps, research_questions)

        if "init_arms" in available and _plan_needs_arm_init(steps):
            insert_at = 1 if steps and steps[0].name == "observe" else 0
            steps.insert(
                insert_at,
                SkillCall(
                    "init_arms",
                    {},
                    "initialize both arms through the registered validated init skill before grasp or arm motion",
                ),
            )

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
        if agent_client is not None:
            agent_plan = agent_client.run_json(
                role=self.role_name,
                prompt=_strategist_prompt(
                    task=task,
                    user_request=user_request,
                    skills=discovered_skills,
                    candidate_plan=_plan_to_dict(plan),
                ),
                schema=_PLAN_SCHEMA,
            )
            (workspace.root / "strategist_agent.json").write_text(
                json.dumps(agent_plan, indent=2, ensure_ascii=False, default=str) + "\n",
                encoding="utf-8",
            )
            plan = _plan_from_agent(agent_plan, fallback=plan, available=available)
        workspace.write_plan(plan.to_markdown())
        return plan

    def replan_after_failure(
        self,
        *,
        task: str,
        user_request: str,
        workspace: Workspace,
        skills: SkillRegistry,
        previous_plan: Plan,
        trace: list[Any],
        agent_client: SubagentClient | None = None,
    ) -> Plan:
        if agent_client is None:
            return previous_plan

        discovered_skills = skills.list()
        available = {skill.name for skill in discovered_skills}
        agent_plan = agent_client.run_json(
            role=self.role_name,
            prompt=_strategist_retry_prompt(
                task=task,
                user_request=user_request,
                skills=discovered_skills,
                previous_plan=_plan_to_dict(previous_plan),
                trace=[step.to_dict() for step in trace],
            ),
            schema=_PLAN_SCHEMA,
        )
        (workspace.root / "strategist_retry_agent.json").write_text(
            json.dumps(agent_plan, indent=2, ensure_ascii=False, default=str) + "\n",
            encoding="utf-8",
        )
        plan = _plan_from_agent(agent_plan, fallback=previous_plan, available=available)
        workspace.write_plan(plan.to_markdown())
        return plan


FLOAT = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)"
_CONTROL_SKILLS = {
    "move_arm_ee",
    "move_arm_joints",
    "set_gripper",
    "set_base_velocity",
    "set_lift_height",
}

_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "goal": {"type": "string"},
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "args_json": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["name", "args_json", "why"],
                "additionalProperties": False,
            },
        },
        "success_criteria": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "research_questions": {"type": "array", "items": {"type": "string"}},
        "subagent_notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "goal",
        "steps",
        "success_criteria",
        "risks",
        "assumptions",
        "research_questions",
        "subagent_notes",
    ],
    "additionalProperties": False,
}


def _strategist_prompt(*, task: str, user_request: str, skills: list[Any], candidate_plan: dict[str, Any]) -> str:
    payload = {
        "role": "strategist",
        "contract": (
            "You are the LoopMaster Strategist subagent. Produce a registry-grounded plan for a "
            "real robot or dry-run platform. Use only the provided skill names. Do not invent "
            "simulation-only tools. Keep stop_motion at the end when any control skill appears. "
            "For any real robot motion, include or rely on closed-loop state feedback: control "
            "success requires observe evidence that the actual robot state changed as expected, "
            "not just action_sent acknowledgements. Add dwell/settling time through skill args "
            "when repeated targets would otherwise be issued too quickly to observe. Feedback is "
            "asynchronous, so plan for ranges/trends or multiple samples rather than one exact "
            "post-command equality check. For explicit low-level body-frame base requests such "
            "as moving forward/backward for N seconds, use set_base_velocity with duration_s, "
            "then stop_motion and stopped-state observe. Do not insert navigation status or "
            "semantic path-clearance steps as prerequisites for that low-level command unless "
            "the user asked for autonomous map navigation or a registered clearance/safety skill "
            "exists and returns an explicit unsafe/abort verdict. Every control command needs "
            "explicit timing semantics: base velocity uses duration_s, gripper/lift/stop use "
            "settle_s when feedback matters, and arm motion uses velocity_limit_rad_s plus "
            "post-motion observe/settling. Do not satisfy motion requests by issuing several "
            "control commands back-to-back with no duration or settle window. Use the timer "
            "meta skill when the plan needs wall-clock/monotonic time evidence or a non-actuator "
            "wait between skills; prefer actuator-specific duration/settle args for actuator timing. "
            "For each step, encode skill arguments as a compact JSON object string in args_json. "
            "Later step args may reference prior skill results with {\"$ref\":\"skill.path.to.value\"} "
            "or string templates like ${skill.path.to.value}; the Worker resolves these from context.memory. "
            "Return only JSON matching the schema."
        ),
        "task": task,
        "user_request": user_request,
        "available_skills": [
            {
                "name": skill.name,
                "category": skill.category,
                "description": skill.description,
                "args": skill.frontmatter.get("args", {}),
                "usage_markdown": _skill_usage_markdown(skill),
                "is_user": skill.is_user,
            }
            for skill in skills
        ],
        "candidate_plan": candidate_plan,
    }
    return json.dumps(payload, indent=2, ensure_ascii=False, default=str)


def _skill_usage_markdown(skill: Any, *, limit: int = 1600) -> str:
    body = str(getattr(skill, "body", "") or "").strip()
    if len(body) <= limit:
        return body
    return body[: limit - 3].rstrip() + "..."


def _strategist_retry_prompt(
    *,
    task: str,
    user_request: str,
    skills: list[Any],
    previous_plan: dict[str, Any],
    trace: list[dict[str, Any]],
) -> str:
    payload = {
        "role": "strategist",
        "contract": (
            "You are the LoopMaster Strategist retry pass. The previous worker execution failed "
            "or the Auditor requested more closed-loop evidence. "
            "Inspect the trace, correct fixable skill argument/schema mistakes, and return a revised "
            "registry-grounded plan. Use only the provided skill names and each skill's documented args. "
            "Do not ask the user to fix schema mismatches that you can correct. Keep stop_motion at the "
            "end when any control skill appears. If a skill's SKILL.md was updated after the previous "
            "failure, use its current usage_markdown to change call arguments, path forms, frame names, "
            "or step sequencing as documented. If the same skill still fails with the same root cause "
            "after following updated documentation, call out that the skill runtime may need a policy.py "
            "repair instead of repeating identical arguments. If the failure is caused by a "
            "repository-local skill output or documentation defect, identify that skill repair in risks or "
            "subagent_notes instead of turning it into a user research question. For motion retries, "
            "do not treat action_sent alone as success; require observe feedback that the robot "
            "state reached or moved toward the target, and add dwell/settling time when commands "
            "were sent too quickly for physical motion. Feedback can lag commands, so prefer "
            "multi-sample trends/ranges over single-sample exact equality. For low-level timed "
            "base motion, do not escalate a successful duration/velocity/stopped-state retry into "
            "navigation status or a semantic path-clearance task unless a registered clearance/safety "
            "skill exists and returns an explicit unsafe or abort result; generic grounded_sam2 "
            "detections and navigation status are not by themselves clearance verdicts. Every revised "
            "control plan must include explicit timing semantics: duration_s for base velocity, "
            "settle_s for gripper/lift/stop when feedback matters, and velocity_limit_rad_s plus "
            "settling/observe for arms. Do not retry by rapidly emitting multiple control commands "
            "without a duration or settle window. Use timer for wall-clock/monotonic time evidence "
            "or non-actuator waits; use actuator-specific timing args for actuator commands. "
            "Later step args may reference prior skill results with {\"$ref\":\"skill.path.to.value\"} "
            "or string templates like ${skill.path.to.value}. "
            "Return only JSON matching the schema."
        ),
        "task": task,
        "user_request": user_request,
        "available_skills": [
            {
                "name": skill.name,
                "category": skill.category,
                "description": skill.description,
                "args": skill.frontmatter.get("args", {}),
                "usage_markdown": _skill_usage_markdown(skill),
                "is_user": skill.is_user,
            }
            for skill in skills
        ],
        "previous_plan": previous_plan,
        "failed_trace": trace,
        "retry_guidance": (
            "For move_arm_joints use args {\"side\": \"left\"|\"right\"|\"both\", \"positions\": {...}} "
            "or positions as a 7-value numeric array. For move_arm_ee use args "
            "{\"side\":\"left\"|\"right\", \"pose\": {\"position\": [x,y,z], \"rpy\": [r,p,y]}, "
            "\"input_frame\":\"head_camera\"|\"left_arm\"|\"right_arm\"}. Do not use arm=... for move_arm_joints."
        ),
    }
    return json.dumps(payload, indent=2, ensure_ascii=False, default=str)


def _plan_to_dict(plan: Plan) -> dict[str, Any]:
    return {
        "task": plan.task,
        "goal": plan.goal,
        "steps": [{"name": step.name, "args": step.args, "why": step.why} for step in plan.steps],
        "success_criteria": list(plan.success_criteria),
        "risks": list(plan.risks),
        "assumptions": list(plan.assumptions),
        "research_questions": list(plan.research_questions),
        "subagent_notes": list(plan.subagent_notes),
    }


def _plan_from_agent(data: dict[str, Any], *, fallback: Plan, available: set[str]) -> Plan:
    steps: list[SkillCall] = []
    for item in data.get("steps") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        if name not in available:
            continue
        args = _decode_args(item)
        steps.append(
            SkillCall(
                name=name,
                args=args,
                why=str(item.get("why") or ""),
            )
        )
    if not steps:
        steps = list(fallback.steps)
    used_control = any(step.name in _CONTROL_SKILLS for step in steps)
    if used_control and "stop_motion" in available and all(step.name != "stop_motion" for step in steps):
        steps.append(
            SkillCall(
                "stop_motion",
                {"reason": "strategist safety guardrail after Codex plan"},
                "leave the real platform stationary before returning control",
            )
        )
    notes = _strings(data.get("subagent_notes")) or list(fallback.subagent_notes)
    codex = data.get("_codex")
    if isinstance(codex, dict) and codex.get("profile"):
        notes.append(f"Strategist ran through Codex profile {codex['profile']}.")
    return Plan(
        task=fallback.task,
        goal=str(data.get("goal") or fallback.goal),
        steps=steps,
        success_criteria=_strings(data.get("success_criteria")) or list(fallback.success_criteria),
        risks=_strings(data.get("risks")) or list(fallback.risks),
        assumptions=_strings(data.get("assumptions")),
        research_questions=_strings(data.get("research_questions")),
        subagent_notes=notes,
    )


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _decode_args(item: dict[str, Any]) -> dict[str, Any]:
    args_json = item.get("args_json")
    if isinstance(args_json, str):
        try:
            decoded = json.loads(args_json)
        except json.JSONDecodeError:
            decoded = {}
        return dict(decoded) if isinstance(decoded, dict) else {}
    args = item.get("args")
    return dict(args) if isinstance(args, dict) else {}


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


def _wants_grasp_detection(text: str) -> bool:
    keywords = (
        "anygrasp",
        "detect_grasps",
        "grasp detection",
        "grasp pose",
        "grasp poses",
        "find grasps",
        "抓取检测",
        "抓取位姿",
        "抓取姿态",
    )
    return any(item in text for item in keywords)


def _wants_object_conditioned_grasp(text: str) -> bool:
    if not _wants_grasp_detection(text):
        return False
    return _mentions_any(
        text,
        (
            "mask",
            "segment",
            "segmentation",
            "grounded sam",
            "grounded-sam",
            "sam2",
            "object",
            "target",
            "物体",
            "目标",
            "分割",
            "掩码",
            "mask指定",
        ),
    )


def _wants_anygrasp_check_only(text: str) -> bool:
    keywords = ("check", "test", "dry run", "readiness", "测试", "检查", "自检")
    return any(item in text for item in keywords)


def _requested_camera(text: str) -> str:
    if "left_wrist" in text or "left wrist" in text or "左腕" in text:
        return "left_wrist"
    if "right_wrist" in text or "right wrist" in text or "右腕" in text:
        return "right_wrist"
    return "front"


def _requested_object_prompt(text: str) -> str:
    prompt = _extract_quoted(text)
    if prompt:
        return prompt
    for key in ("object", "target", "prompt", "text_prompt", "物体", "目标"):
        match = re.search(rf"{re.escape(key)}\s*[:=]\s*([^,;，。]+)", text, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return "object."


def _extract_quoted(text: str) -> str | None:
    match = re.search(r"['\"]([^'\"]+)['\"]", text)
    if match:
        return match.group(1).strip()
    return None


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
        assumptions.append(
            "Open/close words are not mapped to numbers because the driver convention is hardware-specific."
        )
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


def _maybe_add_arm_ee(
    original_text: str,
    text: str,
    available: set[str],
    steps: list[SkillCall],
    research_questions: list[str],
) -> bool:
    if "move_arm_ee" not in available:
        return False
    if not _mentions_any(text, ("move_arm_ee", "end effector", "ee pose", "末端", "位姿")):
        return False
    side = _extract_side(text)
    pose = _extract_mapping(original_text, "pose")
    if pose is None:
        matrix = _extract_mapping_or_list(original_text, "matrix")
        if matrix is not None:
            pose = {"matrix": matrix}
    if side is None:
        research_questions.append("End-effector motion requested but side=left/right was not provided.")
        return False
    if pose is None:
        research_questions.append("End-effector motion requested but pose={...} or matrix=[...] was not provided.")
        return False
    steps.append(
        SkillCall(
            "move_arm_ee",
            {
                "side": side,
                "pose": pose,
                "input_frame": "head_camera" if "camera" in text or "相机" in text else f"{side}_arm",
            },
            "execute explicitly requested end-effector pose target through IK",
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


def _plan_needs_arm_init(steps: list[SkillCall]) -> bool:
    return any(step.name in {"detect_grasps", "move_arm_ee", "move_arm_joints"} for step in steps)


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
