from __future__ import annotations

import time


MIN_GRIPPER_POSITION = -5.0
MAX_GRIPPER_POSITION = 0.0
DEFAULT_VERIFY_TOLERANCE = 0.05
DEFAULT_VERIFY_MIN_DELTA = 0.02


def dispatch(context, args):
    side = str(args.get("side") or "").lower()
    if side not in {"left", "right"}:
        return {"ok": False, "error": "side must be left or right"}
    try:
        position = float(args["position"])
    except (KeyError, TypeError, ValueError):
        return {"ok": False, "error": "position must be numeric"}
    if position < MIN_GRIPPER_POSITION or position > MAX_GRIPPER_POSITION:
        return {
            "ok": False,
            "error": "position must be in [-5.0, 0.0]; use -5.0 for open and 0.0 for closed",
            "side": side,
            "requested_position": position,
            "min_position": MIN_GRIPPER_POSITION,
            "max_position": MAX_GRIPPER_POSITION,
        }
    key = f"{side}_gripper.pos"
    verify = bool(args.get("verify", False))
    before = _read_gripper_position(context, key) if verify else None

    if hasattr(context.platform, "set_gripper"):
        sent = context.platform.set_gripper(side, position)
    else:
        sent = context.platform.send_action({key: position})
    if not isinstance(sent, dict):
        sent = {}
    commanded = float(sent.get(key, position))
    action_sent = {key: commanded}

    settle_s = float(args.get("settle_s", 0.0) or 0.0)
    if settle_s > 0.0:
        time.sleep(settle_s)
    after = _read_gripper_position(context, key) if verify or settle_s > 0.0 else None

    result = {
        "ok": True,
        "side": side,
        "requested_position": position,
        "commanded_position": commanded,
        "action_sent": action_sent,
    }
    if before is not None:
        result["observed_before"] = before
    if after is not None:
        result["observed_after"] = after
    if verify:
        result["verified"] = _verify_gripper_motion(
            target=position,
            before=before,
            after=after,
            tolerance=float(args.get("tolerance", DEFAULT_VERIFY_TOLERANCE)),
            min_delta=float(args.get("min_delta", DEFAULT_VERIFY_MIN_DELTA)),
        )
    return result


def _read_gripper_position(context, key: str) -> float | None:
    try:
        state = context.platform.observe().state
    except Exception:
        return None
    value = state.get(key) if isinstance(state, dict) else None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _verify_gripper_motion(
    *,
    target: float,
    before: float | None,
    after: float | None,
    tolerance: float,
    min_delta: float,
) -> dict:
    if after is None:
        return {"ok": False, "reason": "missing gripper feedback after command"}
    if abs(after - target) <= tolerance:
        return {"ok": True, "reason": "feedback reached commanded position"}
    if before is None:
        return {"ok": False, "reason": "feedback did not reach target and no before sample was available"}
    delta = after - before
    if target > before and delta >= min_delta:
        return {"ok": True, "reason": "feedback moved in closing direction"}
    if target < before and delta <= -min_delta:
        return {"ok": True, "reason": "feedback moved in opening direction"}
    return {
        "ok": False,
        "reason": "feedback did not move in the commanded direction",
        "delta": delta,
    }
