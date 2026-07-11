from __future__ import annotations

import time

from loopmaster_agentic.skills.control.arm_motion import (
    DEFAULT_ARM_VELOCITY_LIMIT_RAD_S,
    JOINTS,
    send_arm_motion,
)


def dispatch(context, args):
    side = str(args.get("side") or "").lower()
    if side not in {"left", "right", "both", "all"}:
        return {"ok": False, "error": "side must be left, right, or both"}
    positions = args.get("positions")

    if side in {"both", "all"}:
        try:
            right, left = _dual_arm_positions(positions)
        except (TypeError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        try:
            sent, trajectory = send_arm_motion(
                context,
                right=right,
                left=left,
                velocity_limit_rad_s=args.get("velocity_limit_rad_s", args.get("arm_velocity_limit_rad_s")),
            )
        except Exception as exc:
            return {"ok": False, "error": f"arm motion failed: {type(exc).__name__}: {exc}"}
        return _result(sent, trajectory, args)

    try:
        target = _normalize_positions(positions)
    except (TypeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
    try:
        sent, trajectory = send_arm_motion(
            context,
            right=target if side == "right" else None,
            left=target if side == "left" else None,
            velocity_limit_rad_s=args.get("velocity_limit_rad_s", args.get("arm_velocity_limit_rad_s")),
        )
    except Exception as exc:
        return {"ok": False, "error": f"arm motion failed: {type(exc).__name__}: {exc}"}
    return _result(sent, trajectory, args)


def _dual_arm_positions(positions):
    if isinstance(positions, dict) and ("right" in positions or "left" in positions):
        right = positions.get("right")
        left = positions.get("left")
        if right is None or left is None:
            raise ValueError("positions for side=both must include right and left, or a shared joint dict/list")
        return _normalize_positions(right), _normalize_positions(left)
    shared = _normalize_positions(positions)
    return dict(shared), dict(shared)


def _normalize_positions(positions):
    if isinstance(positions, list):
        if len(positions) != len(JOINTS):
            raise ValueError(f"positions list must contain {len(JOINTS)} values")
        return {joint: float(value) for joint, value in zip(JOINTS, positions)}
    if isinstance(positions, dict):
        out = {}
        for joint, value in positions.items():
            if joint not in JOINTS:
                raise ValueError(f"unknown joint: {joint}")
            out[joint] = float(value)
        return out
    raise TypeError("positions must be a list or dict")


def _result(sent, trajectory, args):
    settle_s = float(args.get("settle_s", 0.0) or 0.0)
    if settle_s > 0.0:
        time.sleep(settle_s)
    return {
        "ok": True,
        "action_sent": sent,
        "trajectory": trajectory,
        "settle_s": settle_s,
        "velocity_limit_rad_s": args.get(
            "velocity_limit_rad_s",
            args.get("arm_velocity_limit_rad_s", DEFAULT_ARM_VELOCITY_LIMIT_RAD_S),
        ),
    }
