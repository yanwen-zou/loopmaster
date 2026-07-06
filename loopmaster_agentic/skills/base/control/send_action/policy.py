from __future__ import annotations


def dispatch(context, args):
    action = args.get("action") or {}
    if not isinstance(action, dict):
        return {"ok": False, "error": "action must be a dictionary"}
    sent = context.platform.send_action(action)
    return {"ok": True, "action_sent": sent}
