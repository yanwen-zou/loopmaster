from __future__ import annotations

import time
from typing import Any

JOINTS = ("joint_1", "joint_2", "joint_3", "joint_4", "joint_5", "joint_6", "gripper")


def dispatch(context, args: dict[str, Any] | None = None) -> dict[str, Any]:
    args = args or {}
    side = str(args.get("side") or "left").lower()
    if side not in {"left", "right"}:
        return {"ok": False, "error": "side must be left or right"}

    try:
        joint = int(args.get("joint", 5))
        amplitude_rad = abs(_number(args.get("amplitude_rad", 0.5), "amplitude_rad"))
        cycles = int(args.get("cycles", 5))
        dwell_s = _clamp(_number(args.get("dwell_s", 0.75), "dwell_s"), 0.0, 3.0)
        feedback_polls = max(1, min(int(args.get("feedback_polls", 2)), 8))
        feedback_poll_s = _clamp(_number(args.get("feedback_poll_s", 0.15), "feedback_poll_s"), 0.0, 1.0)
        tolerance_rad = abs(_number(args.get("tolerance_rad", 0.12), "tolerance_rad"))
        min_motion_rad = abs(_number(args.get("min_motion_rad", 0.15), "min_motion_rad"))
        strict_verify = bool(args.get("strict_verify", False))
    except (TypeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
    if joint < 1 or joint > 6:
        return {"ok": False, "error": "joint must be between 1 and 6"}
    if cycles < 1:
        return {"ok": False, "error": "cycles must be positive"}

    observe_result = _call_skill(context, "observe", {"include_images": False, "include_state": True})
    if not observe_result.get("ok"):
        _safe_stop(context, "abort oscillate_arm_joint after observe failure")
        return {"ok": False, "error": "observe failed", "observe": observe_result}

    state = observe_result.get("observation", {}).get("state")
    if not isinstance(state, dict):
        _safe_stop(context, "abort oscillate_arm_joint because observe returned no numeric state")
        return {"ok": False, "error": "observe returned no numeric state"}

    try:
        start = _start_vector(state, side)
    except Exception as exc:
        _safe_stop(context, "abort oscillate_arm_joint because required state values are missing or non-numeric")
        return {"ok": False, "error": str(exc)}

    joint_index = joint - 1
    positive = list(start)
    negative = list(start)
    positive[joint_index] = start[joint_index] + amplitude_rad
    negative[joint_index] = start[joint_index] - amplitude_rad

    feedback: list[dict[str, Any]] = []
    command_count = 0
    for cycle in range(1, cycles + 1):
        result = _command_target(context, side, positive)
        command_count += 1
        if not result.get("ok"):
            _safe_stop(context, "abort oscillate_arm_joint after positive target command failure")
            return {"ok": False, "error": "positive target command failed", "cycle": cycle, "result": result, "feedback": feedback}
        feedback.append(_sample_feedback(context, side, joint, positive[joint_index], f"cycle_{cycle}_positive", dwell_s, feedback_polls, feedback_poll_s, tolerance_rad))
        if strict_verify and not feedback[-1].get("hit_tolerance"):
            _safe_stop(context, "abort oscillate_arm_joint because positive target strict feedback failed")
            return {"ok": False, "error": "positive target feedback mismatch", "cycle": cycle, "feedback": feedback}

        result = _command_target(context, side, negative)
        command_count += 1
        if not result.get("ok"):
            _safe_stop(context, "abort oscillate_arm_joint after negative target command failure")
            return {"ok": False, "error": "negative target command failed", "cycle": cycle, "result": result, "feedback": feedback}
        feedback.append(_sample_feedback(context, side, joint, negative[joint_index], f"cycle_{cycle}_negative", dwell_s, feedback_polls, feedback_poll_s, tolerance_rad))
        if strict_verify and not feedback[-1].get("hit_tolerance"):
            _safe_stop(context, "abort oscillate_arm_joint because negative target strict feedback failed")
            return {"ok": False, "error": "negative target feedback mismatch", "cycle": cycle, "feedback": feedback}

    result = _command_target(context, side, start)
    command_count += 1
    if not result.get("ok"):
        _safe_stop(context, "abort oscillate_arm_joint after return-to-start command failure")
        return {"ok": False, "error": "return-to-start command failed", "result": result, "feedback": feedback}
    feedback.append(_sample_feedback(context, side, joint, start[joint_index], "return_to_start", min(dwell_s, 0.75), feedback_polls, feedback_poll_s, tolerance_rad))

    summary = _feedback_summary(feedback, min_motion_rad=min_motion_rad)
    stop_result = _safe_stop(context, "completed diagnostic arm joint oscillation; final safety stop")
    ok = bool(summary.get("movement_observed")) or not feedback
    if strict_verify:
        ok = ok and all(item.get("hit_tolerance") for item in feedback)
    return {
        "ok": ok,
        "side": side,
        "joint": joint,
        "amplitude_rad": amplitude_rad,
        "cycles": cycles,
        "dwell_s": dwell_s,
        "feedback_polls": feedback_polls,
        "feedback_poll_s": feedback_poll_s,
        "tolerance_rad": tolerance_rad,
        "min_motion_rad": min_motion_rad,
        "strict_verify": strict_verify,
        "start": start,
        "targets": {"positive": positive, "negative": negative},
        "executed_steps": command_count,
        "feedback": feedback,
        "feedback_summary": summary,
        "diagnosis": _diagnosis(summary, strict_verify=strict_verify),
        "stop": stop_result,
    }


def _command_target(context, side: str, positions: list[float]) -> dict[str, Any]:
    return _call_skill(context, "move_arm_joints", {"side": side, "positions": positions})


def _sample_feedback(
    context,
    side: str,
    joint: int,
    target: float,
    label: str,
    dwell_s: float,
    polls: int,
    poll_s: float,
    tolerance_rad: float,
) -> dict[str, Any]:
    if dwell_s > 0.0:
        time.sleep(dwell_s)
    key = f"{side}_joint_{joint}.pos"
    samples: list[dict[str, Any]] = []
    for index in range(polls):
        observe = _call_skill(context, "observe", {"include_images": False, "include_state": True})
        state = observe.get("observation", {}).get("state") if observe.get("ok") else None
        actual = None
        error = None
        if isinstance(state, dict) and key in state:
            try:
                actual = _number(state[key], key)
                error = actual - target
            except Exception:
                actual = None
        samples.append({
            "index": index,
            "ok": bool(observe.get("ok")) and actual is not None,
            "actual": actual,
            "target": float(target),
            "error": error,
            "timestamp": observe.get("observation", {}).get("timestamp") if isinstance(observe.get("observation"), dict) else None,
        })
        if index < polls - 1 and poll_s > 0.0:
            time.sleep(poll_s)
    valid = [sample for sample in samples if sample.get("actual") is not None]
    best = min(valid, key=lambda sample: abs(float(sample["error"]))) if valid else None
    return {
        "label": label,
        "target": float(target),
        "samples": samples,
        "best_actual": best.get("actual") if best else None,
        "best_error": best.get("error") if best else None,
        "latest_actual": valid[-1].get("actual") if valid else None,
        "hit_tolerance": bool(best is not None and abs(float(best["error"])) <= tolerance_rad),
        "tolerance_rad": tolerance_rad,
    }


def _feedback_summary(feedback: list[dict[str, Any]], *, min_motion_rad: float) -> dict[str, Any]:
    values: list[float] = []
    hits = 0
    for item in feedback:
        if item.get("hit_tolerance"):
            hits += 1
        for sample in item.get("samples") or []:
            actual = sample.get("actual")
            if actual is not None:
                values.append(float(actual))
    if not values:
        return {"samples": 0, "hits": hits, "movement_observed": False, "reason": "no numeric feedback samples"}
    observed_min = min(values)
    observed_max = max(values)
    observed_range = observed_max - observed_min
    return {
        "samples": len(values),
        "hits": hits,
        "observed_min": observed_min,
        "observed_max": observed_max,
        "observed_range": observed_range,
        "movement_observed": observed_range >= min_motion_rad,
        "min_motion_rad": min_motion_rad,
        "latest_actual": values[-1],
    }


def _diagnosis(summary: dict[str, Any], *, strict_verify: bool) -> str:
    if not summary.get("samples"):
        return "commands were acknowledged, but no numeric feedback samples were available"
    if summary.get("movement_observed"):
        if strict_verify and summary.get("hits", 0) == 0:
            return "motion was observed, but strict target tolerance was not met; feedback may lag or controller may not settle at extrema"
        return "motion was observed from feedback range; individual samples may lag commanded targets"
    return "feedback range was small; possible no motion, delayed observation, clamping, or insufficient dwell/settling time"


def _call_skill(context, name: str, args: dict[str, Any]) -> dict[str, Any]:
    caller = getattr(context, "call_skill", None) or getattr(context, "call", None)
    if caller is None:
        return {"ok": False, "error": "context does not expose call_skill"}
    result = caller(name, args)
    return result if isinstance(result, dict) else {"ok": False, "error": f"{name} returned non-dict result"}


def _safe_stop(context, reason: str) -> dict[str, Any]:
    try:
        return _call_skill(context, "stop_motion", {"reason": reason})
    except Exception as exc:
        return {"ok": False, "error": f"stop_motion failed: {type(exc).__name__}: {exc}"}


def _number(value: Any, key: str) -> float:
    if not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be numeric")
    return float(value)


def _start_vector(state: dict[str, Any], side: str) -> list[float]:
    values = []
    for joint in JOINTS:
        key = f"{side}_{joint}.pos"
        if key not in state:
            raise KeyError(f"missing observed state value: {key}")
        values.append(_number(state[key], key))
    return values


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))
