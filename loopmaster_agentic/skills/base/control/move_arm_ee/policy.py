from __future__ import annotations

import math
import time
from typing import Any

from loopmaster_agentic.ik.bridge import solve_arm_ee_dict


JOINTS = ("joint_1", "joint_2", "joint_3", "joint_4", "joint_5", "joint_6", "gripper")


def dispatch(context, args):
    side = str(args.get("side") or args.get("arm") or "").lower()
    if side not in {"left", "right"}:
        return {"ok": False, "error": "side must be left or right"}

    pose = args.get("pose")
    if pose is None:
        pose = _pose_from_args(args)
    if pose is None:
        return {
            "ok": False,
            "error": "pose is required; pass pose.matrix or pose.position plus rpy/quat/rotation_matrix",
        }

    try:
        current_positions = _normalize_current_positions_arg(args.get("current_positions")) or _read_current_positions(context, side)
        other_arm_positions = _normalize_current_positions_arg(args.get("other_arm_positions"))
        orientation_cost = _orientation_cost(args, pose)
        result = solve_arm_ee_dict(
            side=side,
            pose=pose,
            input_frame=str(args.get("input_frame") or "head_camera"),
            current_positions=current_positions,
            gripper=float(args["gripper"]) if args.get("gripper") is not None else None,
            orientation_cost=orientation_cost,
            preserve_current_orientation=bool(args.get("preserve_current_orientation", False)),
        )
    except Exception as exc:
        return {"ok": False, "error": f"IK failed: {type(exc).__name__}: {exc}"}

    execute = bool(args.get("execute", True))
    require_success = bool(args.get("require_ik_success", True))
    if require_success and not result["ik_success"]:
        return {
            "ok": False,
            "side": side,
            "ik_success": False,
            "positions": result["positions"],
            "target_arm_pose": result["target_arm_pose"],
            "target_camera_pose": result["target_camera_pose"],
            "transform": result["transform"],
            "error": "IK did not converge",
            "ik_info": result["ik_info"],
        }

    sent = {}
    trajectory = []
    if execute:
        sent, trajectory = _send_limited_arm_motion(
            context,
            side,
            result["positions"],
            current_positions,
            other_arm_positions=other_arm_positions,
            max_joint_step=args.get("max_joint_step"),
            step_dt=float(args.get("step_dt", 0.08)),
            hold_s=float(args.get("hold_s", 0.0)),
        )

    return {
        "ok": True,
        "side": side,
        "executed": execute,
        "ik_success": result["ik_success"],
        "positions": result["positions"],
        "action_sent": sent,
        "trajectory": trajectory,
        "target_arm_pose": result["target_arm_pose"],
        "target_camera_pose": result["target_camera_pose"],
        "transform": result["transform"],
        "ik_info": result["ik_info"],
        "orientation_cost": orientation_cost,
    }


def _send_limited_arm_motion(
    context,
    side: str,
    target_positions: dict[str, float],
    current_positions: dict[str, float] | None,
    *,
    other_arm_positions: dict[str, float] | None,
    max_joint_step: Any,
    step_dt: float,
    hold_s: float,
) -> tuple[dict[str, float], list[dict[str, Any]]]:
    target = {joint: float(target_positions[joint]) for joint in JOINTS if joint in target_positions}
    current = _joint_positions_or_none(current_positions)
    max_step = float(max_joint_step) if max_joint_step is not None else 0.0
    if current is None or max_step <= 0.0:
        return _send_arm_positions(context, side, target, other_arm_positions), []

    max_delta = max(abs(target[joint] - current[joint]) for joint in target if joint in current)
    steps = max(1, int(math.ceil(max_delta / max(max_step, 1e-9))))
    trajectory = []
    sent = {}
    step_dt = max(float(step_dt), 0.0)
    hold_s = max(float(hold_s), 0.0)
    for index in range(1, steps + 1):
        alpha = index / steps
        waypoint = {
            joint: current[joint] + (target[joint] - current[joint]) * alpha
            for joint in target
            if joint in current
        }
        sent = _send_arm_positions(context, side, waypoint, other_arm_positions)
        trajectory.append({"index": index, "steps": steps, "positions": waypoint, "action_sent": sent})
        if index < steps and step_dt > 0.0:
            time.sleep(step_dt)
    if hold_s > 0.0:
        time.sleep(hold_s)
    return sent, trajectory


def _send_arm_positions(
    context,
    side: str,
    positions: dict[str, float],
    other_arm_positions: dict[str, float] | None = None,
) -> dict[str, float]:
    if other_arm_positions is not None:
        other_side = "left" if side == "right" else "right"
        if hasattr(context.platform, "command_arms"):
            kwargs = {
                side: positions,
                other_side: other_arm_positions,
            }
            return context.platform.command_arms(**kwargs)
        action = {
            **{f"{side}_{joint}.pos": value for joint, value in positions.items()},
            **{f"{other_side}_{joint}.pos": value for joint, value in other_arm_positions.items()},
        }
        return context.platform.send_action(action)
    if hasattr(context.platform, "command_arm"):
        return context.platform.command_arm(side, positions)
    return context.platform.send_action({f"{side}_{joint}.pos": value for joint, value in positions.items()})


def _joint_positions_or_none(raw: dict[str, float] | None) -> dict[str, float] | None:
    if raw is None:
        return None
    out = {}
    for joint in JOINTS:
        if joint not in raw:
            return None
        out[joint] = float(raw[joint])
    return out


def _normalize_current_positions_arg(raw: Any) -> dict[str, float] | None:
    if raw is None:
        return None
    if isinstance(raw, dict):
        out = {}
        for joint in JOINTS:
            value = raw.get(joint, raw.get(f"{joint}.pos"))
            if value is None:
                return None
            out[joint] = float(value)
        return out
    if isinstance(raw, (list, tuple)):
        if len(raw) != len(JOINTS):
            return None
        return {joint: float(value) for joint, value in zip(JOINTS, raw, strict=True)}
    return None


def _orientation_cost(args: dict[str, Any], pose: Any) -> float:
    if args.get("orientation_cost") is not None:
        return max(float(args["orientation_cost"]), 0.0)
    if isinstance(pose, dict):
        has_explicit_rotation = any(
            key in pose
            for key in (
                "matrix",
                "rotation_matrix",
                "rpy",
                "euler",
                "roll",
                "pitch",
                "yaw",
                "quat",
                "quaternion",
            )
        )
        return 0.1 if has_explicit_rotation else 0.0
    return 0.1


def _pose_from_args(args: dict[str, Any]) -> dict[str, Any] | None:
    if args.get("position") is not None or all(key in args for key in ("x", "y", "z")):
        pose: dict[str, Any] = {
            "position": args.get("position") or [args.get("x"), args.get("y"), args.get("z")],
        }
        for key in ("rpy", "euler", "roll", "pitch", "yaw", "quat", "quaternion", "rotation_matrix"):
            if key in args:
                pose[key] = args[key]
        return pose
    if args.get("matrix") is not None:
        return {"matrix": args["matrix"]}
    return None


def _read_current_positions(context, side: str) -> dict[str, float] | None:
    if not hasattr(context.platform, "read_arm_positions"):
        return None
    raw = context.platform.read_arm_positions(side)
    out: dict[str, float] = {}
    prefix = f"{side}_"
    for key, value in dict(raw).items():
        joint = str(key)
        if joint.startswith(prefix):
            joint = joint[len(prefix) :]
        if joint.endswith(".pos"):
            joint = joint[:-4]
        out[joint] = float(value)
    return out
