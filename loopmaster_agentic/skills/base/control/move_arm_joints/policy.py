from __future__ import annotations


JOINTS = ("joint_1", "joint_2", "joint_3", "joint_4", "joint_5", "joint_6", "gripper")


def dispatch(context, args):
    side = str(args.get("side") or "").lower()
    if side not in {"left", "right"}:
        return {"ok": False, "error": "side must be left or right"}
    positions = args.get("positions")
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
    sent = context.platform.send_action(action)
    return {"ok": True, "action_sent": sent}
