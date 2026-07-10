from __future__ import annotations


JOINTS = ("joint_1", "joint_2", "joint_3", "joint_4", "joint_5", "joint_6", "gripper")


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
        if hasattr(context.platform, "command_arms"):
            sent = context.platform.command_arms(right=right, left=left)
        else:
            action = {
                **{f"right_{joint}.pos": float(value) for joint, value in right.items()},
                **{f"left_{joint}.pos": float(value) for joint, value in left.items()},
            }
            sent = context.platform.send_action(action)
        return {"ok": True, "action_sent": sent}

    if isinstance(positions, list):
        if len(positions) != len(JOINTS):
            return {"ok": False, "error": f"positions list must contain {len(JOINTS)} values"}
        action = {f"{side}_{joint}.pos": float(value) for joint, value in zip(JOINTS, positions)}
    elif isinstance(positions, dict):
        action = {}
        for joint, value in positions.items():
            if joint not in JOINTS:
                return {"ok": False, "error": f"unknown joint: {joint}"}
            action[f"{side}_{joint}.pos"] = float(value)
    else:
        return {"ok": False, "error": "positions must be a list or dict"}
    if hasattr(context.platform, "command_arm"):
        sent = context.platform.command_arm(side, positions)
    else:
        sent = context.platform.send_action(action)
    return {"ok": True, "action_sent": sent}


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
