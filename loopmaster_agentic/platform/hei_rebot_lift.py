from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loopmaster_agentic.core.types import Observation
from loopmaster_agentic.platform.base import RobotPlatform


ARM_JOINTS = ("joint_1", "joint_2", "joint_3", "joint_4", "joint_5", "joint_6", "gripper")
CONTROL_KEYS = {
    "x.vel",
    "y.vel",
    "theta.vel",
    "height.pos",
    *(f"{side}_{joint}.pos" for side in ("right", "left") for joint in ARM_JOINTS),
}
CAMERA_KEYS = {"front", "left_wrist", "right_wrist"}


@dataclass
class HeiRebotLiftPlatformConfig:
    """Configuration for direct or remote HEI ReBot Lift control."""

    remote_ip: str | None = None
    robot_id: str = "hei_rebot_lift"
    lerobot_src: Path | None = None
    connect_on_init: bool = False

    @property
    def mode(self) -> str:
        return "client" if self.remote_ip else "local"


class HeiRebotLiftPlatform(RobotPlatform):
    """Real HEI ReBot Lift adapter backed by the LeRobot driver/client."""

    name = "hei_rebot_lift"

    def __init__(self, config: HeiRebotLiftPlatformConfig | None = None):
        self.config = config or HeiRebotLiftPlatformConfig()
        self._robot: Any = None
        if self.config.connect_on_init:
            self.connect()

    @property
    def robot(self) -> Any:
        if self._robot is None:
            self._robot = self._build_robot()
        return self._robot

    @property
    def action_features(self) -> dict[str, type]:
        robot = self._robot
        if robot is not None and hasattr(robot, "action_features"):
            return dict(robot.action_features)
        return {key: float for key in sorted(CONTROL_KEYS)}

    @property
    def observation_features(self) -> dict[str, type | tuple[int, ...]]:
        robot = self._robot
        if robot is not None and hasattr(robot, "observation_features"):
            return dict(robot.observation_features)
        features: dict[str, type | tuple[int, ...]] = {key: float for key in sorted(CONTROL_KEYS)}
        features.update({key: (480, 640, 3) for key in CAMERA_KEYS})
        return features

    def connect(self) -> None:
        robot = self.robot
        if not getattr(robot, "is_connected", False):
            robot.connect()

    def observe(self) -> Observation:
        raw = self.robot.get_observation()
        return split_hei_observation(raw)

    def send_action(self, action: dict[str, float]) -> dict[str, float]:
        clean = {key: float(value) for key, value in action.items() if key in CONTROL_KEYS}
        if not clean:
            return {}
        sent = self.robot.send_action(clean)
        return {str(key): float(value) for key, value in dict(sent).items() if _is_number(value)}

    def stop_motion(self) -> None:
        robot = self.robot
        if hasattr(robot, "stop_motion"):
            robot.stop_motion()
        else:
            self.send_action({"x.vel": 0.0, "y.vel": 0.0, "theta.vel": 0.0})

    def close(self) -> None:
        if self._robot is None:
            return
        if getattr(self._robot, "is_connected", False):
            self._robot.disconnect()

    def _build_robot(self) -> Any:
        _ensure_lerobot_importable(self.config.lerobot_src)
        if self.config.remote_ip:
            from lerobot.robots.hei_rebot_lift import (
                HeiRebotLiftClient,
                HeiRebotLiftClientConfig,
            )

            cfg = HeiRebotLiftClientConfig(
                remote_ip=self.config.remote_ip,
                id=self.config.robot_id,
            )
            return HeiRebotLiftClient(cfg)

        from lerobot.robots.hei_rebot_lift import HeiRebotLift, HeiRebotLiftConfig

        return HeiRebotLift(HeiRebotLiftConfig(id=self.config.robot_id))


def split_hei_observation(raw: dict[str, Any]) -> Observation:
    """Convert LeRobot's flat observation dict into image/state buckets."""

    images: dict[str, Any] = {}
    state: dict[str, float] = {}
    extras: dict[str, Any] = {}
    for key, value in dict(raw).items():
        if key in CAMERA_KEYS or hasattr(value, "shape"):
            images[str(key)] = value
        elif _is_number(value):
            state[str(key)] = float(value)
        else:
            extras[str(key)] = value
    return Observation(images=images, state=state, extras=extras)


def _ensure_lerobot_importable(explicit_src: Path | None) -> None:
    candidates: list[Path] = []
    if explicit_src:
        candidates.append(explicit_src)
    here = Path(__file__).resolve()
    candidates.append(here.parents[2] / "hei-rebot-lift" / "software" / "lerobot-hei-rebot-lift" / "src")
    for path in candidates:
        if path.exists() and str(path) not in sys.path:
            sys.path.insert(0, str(path))


def _is_number(value: Any) -> bool:
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True
