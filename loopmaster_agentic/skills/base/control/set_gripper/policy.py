from __future__ import annotations


def dispatch(context, args):
    side = str(args.get("side") or "").lower()
    if side not in {"left", "right"}:
        return {"ok": False, "error": "side must be left or right"}
    try:
        position = float(args["position"])
    except (KeyError, TypeError, ValueError):
        return {"ok": False, "error": "position must be numeric"}
    if hasattr(context.platform, "set_gripper"):
        sent = context.platform.set_gripper(side, position)
    else:
        sent = context.platform.send_action({f"{side}_gripper.pos": position})
    return {"ok": True, "action_sent": sent}
