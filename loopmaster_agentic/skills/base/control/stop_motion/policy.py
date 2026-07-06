from __future__ import annotations


def dispatch(context, args):
    context.platform.stop_motion()
    return {"ok": True, "stopped": True, "reason": args.get("reason", "")}
