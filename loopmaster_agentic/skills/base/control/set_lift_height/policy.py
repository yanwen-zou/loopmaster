from __future__ import annotations


def dispatch(context, args):
    try:
        height = float(args["height_mm"])
    except (KeyError, TypeError, ValueError):
        return {"ok": False, "error": "height_mm must be numeric"}
    sent = context.platform.send_action({"height.pos": height})
    return {"ok": True, "action_sent": sent}
