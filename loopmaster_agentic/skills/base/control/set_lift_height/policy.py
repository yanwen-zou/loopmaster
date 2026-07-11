from __future__ import annotations

import time


def dispatch(context, args):
    try:
        height = float(args["height_mm"])
        settle_s = float(args.get("settle_s", 0.0) or 0.0)
    except (KeyError, TypeError, ValueError):
        return {"ok": False, "error": "height_mm and settle_s must be numeric"}
    sent = context.platform.send_action({"height.pos": height})
    if settle_s > 0.0:
        time.sleep(settle_s)
    return {"ok": True, "action_sent": sent, "settle_s": settle_s}
