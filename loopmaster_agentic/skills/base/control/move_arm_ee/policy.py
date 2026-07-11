from __future__ import annotations

import time
from typing import Any

from loopmaster_agentic.ik.bridge import solve_arm_ee_dict
from loopmaster_agentic.skills.base.control.arm_motion import (
    DEFAULT_ARM_VELOCITY_LIMIT_RAD_S,
    JOINTS,
    send_arm_motion,
)


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
        other_side = "left" if side == "right" else "right"
        current_positions = _normalize_current_positions_arg(args.get("current_positions"), side=side) or _read_current_positions(context, side)
        other_arm_positions = _normalize_current_positions_arg(
            args.get("other_arm_positions"),
            side=other_side,
        ) or _read_current_positions(context, other_side)
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
        right_target = result["positions"] if side == "right" else other_arm_positions
        left_target = result["positions"] if side == "left" else other_arm_positions
        current_right = current_positions if side == "right" else other_arm_positions
        current_left = current_positions if side == "left" else other_arm_positions
        try:
            sent, trajectory = send_arm_motion(
                context,
                right=right_target,
                left=left_target,
                current_right=current_right,
                current_left=current_left,
                velocity_limit_rad_s=args.get("velocity_limit_rad_s", args.get("arm_velocity_limit_rad_s")),
            )
        except Exception as exc:
            return {"ok": False, "error": f"arm motion failed: {type(exc).__name__}: {exc}"}
        settle_s = float(args.get("settle_s", 0.0) or 0.0)
        if settle_s > 0.0:
            time.sleep(settle_s)
    else:
        settle_s = 0.0

    return {
        "ok": True,
        "side": side,
        "executed": execute,
        "ik_success": result["ik_success"],
        "positions": result["positions"],
        "action_sent": sent,
        "trajectory": trajectory,
        "settle_s": settle_s,
        "target_arm_pose": result["target_arm_pose"],
        "target_camera_pose": result["target_camera_pose"],
        "transform": result["transform"],
        "ik_info": result["ik_info"],
        "orientation_cost": orientation_cost,
        "velocity_limit_rad_s": args.get(
            "velocity_limit_rad_s",
            args.get("arm_velocity_limit_rad_s", DEFAULT_ARM_VELOCITY_LIMIT_RAD_S),
        ),
    }


def _normalize_current_positions_arg(raw: Any, *, side: str | None = None) -> dict[str, float] | None:
    if raw is None:
        return None
    if isinstance(raw, dict):
        out = {}
        for joint in JOINTS:
            keys = [joint, f"{joint}.pos"]
            if side is not None:
                keys = [f"{side}_{joint}.pos", f"{side}_{joint}", *keys]
            value = next((raw[key] for key in keys if key in raw), None)
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
    if hasattr(context.platform, "read_arm_positions"):
        raw = context.platform.read_arm_positions(side)
    elif hasattr(context.platform, "observe"):
        raw = getattr(context.platform.observe(), "state", {}) or {}
    else:
        return None
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
