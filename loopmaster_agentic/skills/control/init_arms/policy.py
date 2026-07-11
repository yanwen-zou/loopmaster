from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from loopmaster_agentic.platform.hei_rebot_lift import ARM_JOINTS, ARM_POSITION_LIMITS_RAD
from loopmaster_agentic.skills.control.arm_motion import (
    DEFAULT_ARM_VELOCITY_LIMIT_RAD_S,
    send_arm_motion,
)


PACKAGE_ROOT = Path(__file__).resolve().parents[3]
REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_CONFIG = PACKAGE_ROOT / "config" / "arm_init_pose.json"


def dispatch(context, args):
    try:
        config_path = _resolve_config_path(args.get("config_path"))
        positions = _load_positions(config_path)
        _validate_limits(positions)
        sent, trajectory = send_arm_motion(
            context,
            right=positions,
            left=positions,
            velocity_limit_rad_s=args.get("velocity_limit_rad_s", args.get("arm_velocity_limit_rad_s")),
        )
        settle_s = float(args.get("settle_s", 1.0))
        if settle_s > 0:
            time.sleep(settle_s)
        verify = bool(args.get("verify", True))
        verification = (
            _verify(
                context,
                positions,
                float(args.get("tolerance_rad", 0.08)),
                timeout_s=float(args.get("verify_timeout_s", 3.0)),
                poll_s=float(args.get("verify_poll_s", 0.1)),
            )
            if verify
            else None
        )
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    ok = verification is None or bool(verification.get("ok"))
    result = {
        "ok": ok,
        "config_path": str(config_path),
        "positions": positions,
        "action_sent": sent,
        "trajectory": trajectory,
        "verified": verification,
        "velocity_limit_rad_s": args.get(
            "velocity_limit_rad_s",
            args.get("arm_velocity_limit_rad_s", DEFAULT_ARM_VELOCITY_LIMIT_RAD_S),
        ),
    }
    if not ok:
        result["error"] = "init arm verification failed"
    return result


def _resolve_config_path(value: Any) -> Path:
    if value:
        path = Path(str(value)).expanduser()
        if path.is_absolute():
            return path
        return REPO_ROOT / path
    return DEFAULT_CONFIG


def _load_positions(path: Path) -> dict[str, float]:
    data = json.loads(path.read_text(encoding="utf-8"))
    raw = data.get("positions")
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a positions object")
    positions: dict[str, float] = {}
    for key, value in raw.items():
        joint = _normalize_joint_key(str(key))
        positions[joint] = float(value)
    missing = [joint for joint in ARM_JOINTS if joint not in positions]
    if missing:
        raise ValueError(f"init config missing joints: {missing}")
    extra = [joint for joint in positions if joint not in ARM_JOINTS]
    if extra:
        raise ValueError(f"init config has unknown joints: {extra}")
    return {joint: positions[joint] for joint in ARM_JOINTS}


def _normalize_joint_key(key: str) -> str:
    key = key.removesuffix(".pos")
    for prefix in ("left_", "right_"):
        if key.startswith(prefix):
            key = key[len(prefix) :]
    return key


def _validate_limits(positions: dict[str, float]) -> None:
    for side, limits in ARM_POSITION_LIMITS_RAD.items():
        for joint, value in positions.items():
            lower, upper = limits[joint]
            if value < lower or value > upper:
                raise ValueError(f"{side} {joint} init target {value} outside limit [{lower}, {upper}]")


def _verify(context, positions: dict[str, float], tolerance_rad: float, *, timeout_s: float, poll_s: float) -> dict[str, Any]:
    deadline = time.monotonic() + max(timeout_s, 0.0)
    last: dict[str, Any] | None = None
    samples = 0
    while True:
        samples += 1
        state, source = _read_verify_state(context)
        last = _compare_state(state, positions, tolerance_rad)
        last["source"] = source
        last["samples"] = samples
        if last["ok"] or time.monotonic() >= deadline:
            return last
        time.sleep(max(poll_s, 0.0))


def _read_verify_state(context) -> tuple[dict[str, float], str]:
    if hasattr(context.platform, "read_arm_positions"):
        state: dict[str, float] = {}
        try:
            for side in ("right", "left"):
                state.update(_normalize_arm_state(context.platform.read_arm_positions(side), side))
            if state:
                return state, "read_arm_positions"
        except Exception:
            pass

    observation = context.platform.observe()
    context.last_observation = observation
    return dict(getattr(observation, "state", {}) or {}), "observe"


def _normalize_arm_state(raw: Any, side: str) -> dict[str, float]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    prefix = f"{side}_"
    for key, value in raw.items():
        joint = str(key)
        if joint.startswith(prefix):
            joint = joint[len(prefix) :]
        if joint.endswith(".pos"):
            joint = joint[:-4]
        if joint in ARM_JOINTS:
            out[f"{side}_{joint}.pos"] = float(value)
    return out


def _compare_state(state: dict[str, float], positions: dict[str, float], tolerance_rad: float) -> dict[str, Any]:
    errors: dict[str, float | None] = {}
    missing: list[str] = []
    for side in ("right", "left"):
        for joint, target in positions.items():
            key = f"{side}_{joint}.pos"
            if key not in state:
                missing.append(key)
                errors[key] = None
                continue
            errors[key] = abs(float(state[key]) - float(target))
    max_error = max((value for value in errors.values() if value is not None), default=None)
    ok = not missing and (max_error is None or max_error <= tolerance_rad)
    return {
        "ok": ok,
        "tolerance_rad": tolerance_rad,
        "max_error_rad": max_error,
        "missing_state_keys": missing,
        "errors_rad": errors,
    }
