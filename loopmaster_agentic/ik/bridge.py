from __future__ import annotations

from dataclasses import asdict
from typing import Any


def solve_arm_ee_dict(
    *,
    side: str,
    pose: Any,
    input_frame: str = "head_camera",
    current_positions: dict[str, float] | list[float] | tuple[float, ...] | None = None,
    gripper: float | None = None,
    orientation_cost: float = 0.1,
    preserve_current_orientation: bool = False,
) -> dict[str, Any]:
    payload = {
        "side": side,
        "pose": pose,
        "input_frame": input_frame,
        "current_positions": current_positions,
        "gripper": gripper,
        "orientation_cost": orientation_cost,
        "preserve_current_orientation": preserve_current_orientation,
    }
    from loopmaster_agentic.ik.mink_ik import solve_arm_ee_mink

    return asdict(solve_arm_ee_mink(**payload))
