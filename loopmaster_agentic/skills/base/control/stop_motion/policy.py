from __future__ import annotations

import time


def dispatch(context, args):
    context.platform.stop_motion()
    try:
        settle_s = float(args.get("settle_s", 0.0) or 0.0)
    except (TypeError, ValueError):
        return {"ok": False, "error": "settle_s must be numeric"}
    if settle_s > 0.0:
        time.sleep(settle_s)
    return {"ok": True, "stopped": True, "reason": args.get("reason", ""), "settle_s": settle_s}
