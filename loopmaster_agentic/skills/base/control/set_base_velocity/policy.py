from __future__ import annotations


def dispatch(context, args):
    try:
        action = {
            "x.vel": float(args.get("x", 0.0)),
            "y.vel": float(args.get("y", 0.0)),
            "theta.vel": float(args.get("theta", 0.0)),
        }
    except (TypeError, ValueError):
        return {"ok": False, "error": "x, y, and theta must be numeric"}
    if hasattr(context.platform, "command_chassis"):
        sent = context.platform.command_chassis(action["x.vel"], action["y.vel"], action["theta.vel"])
    else:
        sent = context.platform.send_action(action)
    return {"ok": True, "action_sent": sent}
