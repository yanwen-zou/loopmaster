from __future__ import annotations

import inspect
from typing import Any


JOINTS = ("joint_1", "joint_2", "joint_3", "joint_4", "joint_5", "joint_6", "gripper")
DEFAULT_ARM_VELOCITY_LIMIT_RAD_S = 0.8


def send_arm_motion(
    context,
    *,
    right: dict[str, float] | None = None,
    left: dict[str, float] | None = None,
    current_right: dict[str, float] | None = None,
    current_left: dict[str, float] | None = None,
    velocity_limit_rad_s: Any = None,
) -> tuple[dict[str, float], list[dict[str, Any]]]:
    targets = {
        side: _target_positions(positions)
        for side, positions in (("right", right), ("left", left))
        if positions is not None
    }
    if not targets:
        return {}, []

    currents = {
        "right": _joint_positions_or_none(current_right) if current_right is not None else _read_current_positions(context, "right"),
        "left": _joint_positions_or_none(current_left) if current_left is not None else _read_current_positions(context, "left"),
    }
    targets = {
        side: _fill_unspecified_joints(side, target, currents[side])
        for side, target in targets.items()
    }
    velocity_limit = (
        DEFAULT_ARM_VELOCITY_LIMIT_RAD_S
        if velocity_limit_rad_s is None
        else velocity_limit_rad_s
    )
    return _send_arm_targets(context, targets, velocity_limit_rad_s=velocity_limit), []


def _target_positions(positions: dict[str, float]) -> dict[str, float]:
    return {joint: float(value) for joint, value in positions.items() if joint in JOINTS}


def _fill_unspecified_joints(
    side: str,
    target: dict[str, float],
    current: dict[str, float] | None,
) -> dict[str, float]:
    missing = [joint for joint in JOINTS if joint not in target]
    if not missing:
        return dict(target)
    if current is None:
        raise ValueError(
            f"partial {side} arm target is missing {missing}; current arm state is required to preserve unspecified joints"
        )
    filled = dict(current)
    filled.update(target)
    return {joint: float(filled[joint]) for joint in JOINTS}


def _send_arm_targets(context, targets: dict[str, dict[str, float]], *, velocity_limit_rad_s: Any) -> dict[str, float]:
    if set(targets) == {"right", "left"} and hasattr(context.platform, "command_arms"):
        return _call_with_optional_velocity(
            context.platform.command_arms,
            {"right": targets["right"], "left": targets["left"]},
            velocity_limit_rad_s=velocity_limit_rad_s,
        )
    if set(targets) == {"right"} and hasattr(context.platform, "command_arm"):
        return _call_with_optional_velocity(
            context.platform.command_arm,
            {"side": "right", "positions": targets["right"]},
            velocity_limit_rad_s=velocity_limit_rad_s,
        )
    if set(targets) == {"left"} and hasattr(context.platform, "command_arm"):
        return _call_with_optional_velocity(
            context.platform.command_arm,
            {"side": "left", "positions": targets["left"]},
            velocity_limit_rad_s=velocity_limit_rad_s,
        )

    action: dict[str, float] = {}
    for side, positions in targets.items():
        action.update({f"{side}_{joint}.pos": float(value) for joint, value in positions.items()})
    return context.platform.send_action(action)


def _call_with_optional_velocity(method, kwargs: dict[str, Any], *, velocity_limit_rad_s: Any) -> dict[str, float]:
    signature = inspect.signature(method)
    accepts_kwargs = any(param.kind == inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values())
    if accepts_kwargs or "velocity_limit_rad_s" in signature.parameters:
        return method(**kwargs, velocity_limit_rad_s=velocity_limit_rad_s)
    return method(**kwargs)


def _joint_positions_or_none(raw: dict[str, float] | None) -> dict[str, float] | None:
    if raw is None:
        return None
    out = {}
    for joint in JOINTS:
        value = raw.get(joint, raw.get(f"{joint}.pos"))
        if value is None:
            return None
        out[joint] = float(value)
    return out


def _read_current_positions(context, side: str) -> dict[str, float] | None:
    if hasattr(context.platform, "read_arm_positions"):
        raw = context.platform.read_arm_positions(side)
        return _normalize_side_positions(raw, side)
    if hasattr(context.platform, "observe"):
        observation = context.platform.observe()
        return _normalize_side_positions(getattr(observation, "state", {}) or {}, side)
    return None


def _normalize_side_positions(raw: Any, side: str) -> dict[str, float] | None:
    if not isinstance(raw, dict):
        return None
    out: dict[str, float] = {}
    prefix = f"{side}_"
    for key, value in raw.items():
        joint = str(key)
        if joint.startswith(prefix):
            joint = joint[len(prefix) :]
        if joint.endswith(".pos"):
            joint = joint[:-4]
        if joint in JOINTS:
            out[joint] = float(value)
    return _joint_positions_or_none(out)
