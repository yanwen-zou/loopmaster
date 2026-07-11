from __future__ import annotations

import time

BASE_KEYS = ("x.vel", "y.vel", "theta.vel")


def dispatch(context, args):
    try:
        action = {
            "x.vel": float(args.get("x", 0.0)),
            "y.vel": float(args.get("y", 0.0)),
            "theta.vel": float(args.get("theta", 0.0)),
        }
        duration_s = float(args.get("duration_s", 0.0) or 0.0)
        refresh_hz = float(args.get("refresh_hz", 5.0) or 5.0)
    except (TypeError, ValueError):
        return {"ok": False, "error": "x, y, theta, duration_s, and refresh_hz must be numeric"}
    if duration_s < 0.0:
        return {"ok": False, "error": "duration_s must be non-negative"}
    if refresh_hz <= 0.0:
        return {"ok": False, "error": "refresh_hz must be positive"}

    started = time.monotonic()
    sent = _send_base_velocity(context, action)
    refresh_count = 1
    samples = [_read_base_velocity(context)]

    if duration_s > 0.0:
        period_s = 1.0 / refresh_hz
        deadline = started + duration_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                break
            time.sleep(min(period_s, remaining))
            if time.monotonic() < deadline:
                sent = _send_base_velocity(context, action)
                refresh_count += 1
                samples.append(_read_base_velocity(context))

    elapsed_s = time.monotonic() - started
    return {
        "ok": True,
        "action_sent": _base_only(sent, action),
        "duration_s": duration_s,
        "elapsed_s": elapsed_s,
        "refresh_count": refresh_count,
        "velocity_samples": [sample for sample in samples if sample],
    }


def _send_base_velocity(context, action: dict[str, float]) -> dict[str, float]:
    if hasattr(context.platform, "command_chassis"):
        sent = context.platform.command_chassis(action["x.vel"], action["y.vel"], action["theta.vel"])
    else:
        sent = context.platform.send_action(action)
    return sent if isinstance(sent, dict) else {}


def _base_only(sent: dict[str, float], fallback: dict[str, float]) -> dict[str, float]:
    return {key: float(sent.get(key, fallback[key])) for key in BASE_KEYS}


def _read_base_velocity(context) -> dict[str, float]:
    try:
        if hasattr(context.platform, "read_chassis_velocity"):
            state = context.platform.read_chassis_velocity()
        else:
            state = context.platform.observe().state
    except Exception:
        return {}
    if not isinstance(state, dict):
        return {}
    sample = {}
    for key in BASE_KEYS:
        try:
            sample[key] = float(state.get(key, 0.0))
        except (TypeError, ValueError):
            sample[key] = 0.0
    return sample
