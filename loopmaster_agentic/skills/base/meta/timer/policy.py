from __future__ import annotations

import time
from datetime import datetime


def dispatch(context, args):
    try:
        duration_s = float(args.get("duration_s", 0.0) or 0.0)
    except (TypeError, ValueError):
        return {"ok": False, "error": "duration_s must be numeric"}
    if duration_s < 0.0:
        return {"ok": False, "error": "duration_s must be non-negative"}

    label = str(args.get("label") or "")
    started_epoch_s = time.time()
    started_monotonic_s = time.monotonic()
    started_wall_time = datetime.now().astimezone().isoformat()

    if duration_s > 0.0:
        time.sleep(duration_s)

    ended_epoch_s = time.time()
    ended_monotonic_s = time.monotonic()
    ended_wall_time = datetime.now().astimezone().isoformat()

    return {
        "ok": True,
        "label": label,
        "slept_s": duration_s,
        "elapsed_s": ended_monotonic_s - started_monotonic_s,
        "started_epoch_s": started_epoch_s,
        "ended_epoch_s": ended_epoch_s,
        "started_monotonic_s": started_monotonic_s,
        "ended_monotonic_s": ended_monotonic_s,
        "started_wall_time": started_wall_time,
        "ended_wall_time": ended_wall_time,
    }
