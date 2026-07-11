from __future__ import annotations

import argparse
import inspect
import json
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loopmaster_agentic.core.types import Observation
from loopmaster_agentic.platform.base import RobotPlatform


ARM_JOINTS = ("joint_1", "joint_2", "joint_3", "joint_4", "joint_5", "joint_6", "gripper")
ARM_SIDES = ("right", "left")
ARM_POSITION_LIMITS_RAD = {
    "right": {
        "joint_1": (-0.3, 1.5),
        "joint_2": (-3.14, 0.0),
        "joint_3": (-3.14, 0.0),
        "joint_4": (-1.4, 1.57),
        "joint_5": (-1.57, 1.57),
        "joint_6": (-3.14, 3.14),
        "gripper": (-5.0, 0.0),
    },
    "left": {
        "joint_1": (-1.5, 0.3),
        "joint_2": (-3.14, 0.0),
        "joint_3": (-3.14, 0.0),
        "joint_4": (-1.4, 1.57),
        "joint_5": (-1.57, 1.57),
        "joint_6": (-3.14, 3.14),
        "gripper": (-5.0, 0.0),
    },
}
CHASSIS_KEYS = {"x.vel", "y.vel", "theta.vel"}
INVERT_HARDWARE_CHASSIS_XY = True
LIFT_KEYS = {"height.pos"}
ARM_KEYS = {f"{side}_{joint}.pos" for side in ARM_SIDES for joint in ARM_JOINTS}
CONTROL_KEYS = {*CHASSIS_KEYS, *LIFT_KEYS, *ARM_KEYS}
HEAD_CAMERA_KEY = "front"
WRIST_CAMERA_KEYS = ("left_wrist", "right_wrist")
CAMERA_KEYS = {HEAD_CAMERA_KEY, *WRIST_CAMERA_KEYS}
CAMERA_ALIASES = {
    "head": HEAD_CAMERA_KEY,
    "head_camera": HEAD_CAMERA_KEY,
    "front": HEAD_CAMERA_KEY,
    "front_camera": HEAD_CAMERA_KEY,
    "left_wrist": "left_wrist",
    "left_wrist_camera": "left_wrist",
    "right_wrist": "right_wrist",
    "right_wrist_camera": "right_wrist",
}


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
        clean = _clamp_action({key: float(value) for key, value in action.items() if key in CONTROL_KEYS})
        if not clean:
            return {}
        sent = self.robot.send_action(_semantic_to_hardware_action(clean))
        numeric = {str(key): float(value) for key, value in dict(sent).items() if _is_number(value)}
        semantic_sent = _hardware_to_semantic_action(numeric)
        return {key: semantic_sent.get(key, value) for key, value in clean.items()}

    def command_chassis(self, x: float = 0.0, y: float = 0.0, theta: float = 0.0) -> dict[str, float]:
        requested = {"x.vel": float(x), "y.vel": float(y), "theta.vel": float(theta)}
        hardware = _semantic_to_hardware_action(requested)
        robot = self.robot
        if hasattr(robot, "base"):
            sent = robot.base.command_velocity(x=hardware["x.vel"], y=hardware["y.vel"], theta=hardware["theta.vel"])
            return _filter_action_sent(_hardware_to_semantic_action(_numeric_dict(sent)), requested)
        if hasattr(robot, "command_chassis"):
            sent = robot.command_chassis(x=hardware["x.vel"], y=hardware["y.vel"], theta=hardware["theta.vel"])
            return _filter_action_sent(_hardware_to_semantic_action(_numeric_dict(sent)), requested)
        return self.send_action(requested)

    def read_chassis_velocity(self) -> dict[str, float]:
        robot = self.robot
        if hasattr(robot, "base"):
            return _filter_action_sent(_hardware_to_semantic_action(_numeric_dict(robot.base.read_velocity())), {key: 0.0 for key in CHASSIS_KEYS})
        if hasattr(robot, "read_chassis_velocity"):
            return _filter_action_sent(_hardware_to_semantic_action(_numeric_dict(robot.read_chassis_velocity())), {key: 0.0 for key in CHASSIS_KEYS})
        return {key: float(self.observe().state.get(key, 0.0)) for key in sorted(CHASSIS_KEYS)}

    def command_arm(
        self,
        side: str,
        positions: Mapping[str, float] | Sequence[float],
        *,
        velocity_limit_rad_s: float | Sequence[float] | Mapping[str, float] | None = None,
    ) -> dict[str, float]:
        side = _normalize_side(side)
        positions = _clamp_arm_positions(side, positions)
        robot = self.robot
        if hasattr(robot, "arms"):
            sent = _call_with_optional_velocity(
                robot.arms.command_side,
                {"side": side, "positions": positions},
                velocity_limit_rad_s=velocity_limit_rad_s,
            )
            return _numeric_dict(sent)
        if hasattr(robot, "command_arm"):
            sent = _call_with_optional_velocity(
                robot.command_arm,
                {"side": side, "positions": positions},
                velocity_limit_rad_s=velocity_limit_rad_s,
            )
            return _numeric_dict(sent)
        return self.send_action(_make_arm_action(side, positions))

    def command_arms(
        self,
        *,
        right: Mapping[str, float] | Sequence[float] | None = None,
        left: Mapping[str, float] | Sequence[float] | None = None,
        velocity_limit_rad_s: float | Sequence[float] | Mapping[str, float] | None = None,
    ) -> dict[str, float]:
        if right is not None:
            right = _clamp_arm_positions("right", right)
        if left is not None:
            left = _clamp_arm_positions("left", left)
        robot = self.robot
        if hasattr(robot, "arms"):
            sent = _call_with_optional_velocity(
                robot.arms.command,
                {"right": right, "left": left},
                velocity_limit_rad_s=velocity_limit_rad_s,
            )
            return _numeric_dict(sent)
        if hasattr(robot, "command_arms"):
            sent = _call_with_optional_velocity(
                robot.command_arms,
                {"right": right, "left": left},
                velocity_limit_rad_s=velocity_limit_rad_s,
            )
            return _numeric_dict(sent)
        action: dict[str, float] = {}
        if right is not None:
            action.update(_make_arm_action("right", right))
        if left is not None:
            action.update(_make_arm_action("left", left))
        return self.send_action(action)

    def set_gripper(self, side: str, position: float) -> dict[str, float]:
        side = _normalize_side(side)
        position = _clamp_arm_value(side, "gripper", float(position))
        robot = self.robot
        key = f"{side}_gripper.pos"
        if hasattr(robot, "arms"):
            sent = robot.arms.set_gripper(side, position)
            numeric = _numeric_dict(sent)
            return {key: numeric.get(key, position)}
        if hasattr(robot, "set_gripper"):
            sent = robot.set_gripper(side, position)
            numeric = _numeric_dict(sent)
            return {key: numeric.get(key, position)}
        return self.send_action({key: position})

    def read_arm_positions(self, side: str | None = None) -> dict[str, float]:
        robot = self.robot
        if hasattr(robot, "arms"):
            return _numeric_dict(robot.arms.read(side))
        if hasattr(robot, "read_arm_positions"):
            return _numeric_dict(robot.read_arm_positions(side))
        state = self.observe().state
        sides = ARM_SIDES if side is None else (_normalize_side(side),)
        keys = [f"{current_side}_{joint}.pos" for current_side in sides for joint in ARM_JOINTS]
        return {key: float(state.get(key, 0.0)) for key in keys}

    def get_camera_image(self, camera: str = "head") -> Any:
        camera_key = _normalize_camera_key(camera)
        robot = self.robot
        if hasattr(robot, "vision"):
            return robot.vision.read(camera_key)
        if hasattr(robot, "get_camera_image"):
            return robot.get_camera_image(camera_key)
        return self.observe().images[camera_key]

    def get_head_image(self) -> Any:
        return self.get_camera_image(HEAD_CAMERA_KEY)

    def get_wrist_images(self) -> dict[str, Any]:
        return self.get_camera_images(WRIST_CAMERA_KEYS)

    def get_camera_images(self, cameras: Sequence[str] | None = None) -> dict[str, Any]:
        selected = CAMERA_KEYS if cameras is None else {_normalize_camera_key(camera) for camera in cameras}
        robot = self.robot
        if hasattr(robot, "vision"):
            return dict(robot.vision.read_all(tuple(selected)))
        if hasattr(robot, "get_camera_images"):
            return dict(robot.get_camera_images(tuple(selected)))
        images = self.observe().images
        return {camera: images[camera] for camera in selected}

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
            state[str(key)] = _hardware_to_semantic_value(str(key), float(value))
        else:
            extras[str(key)] = value
    return Observation(images=images, state=state, extras=extras)


def _make_arm_action(side: str, positions: Mapping[str, float] | Sequence[float]) -> dict[str, float]:
    side = _normalize_side(side)
    targets = _clamp_arm_positions(side, positions)
    return {f"{side}_{joint}.pos": value for joint, value in targets.items()}


def _clamp_action(action: Mapping[str, float]) -> dict[str, float]:
    clean = dict(action)
    for side in ARM_SIDES:
        prefix = f"{side}_"
        for key, value in list(clean.items()):
            if not key.startswith(prefix) or not key.endswith(".pos"):
                continue
            joint = _normalize_joint_key(key, side=side)
            clean[key] = _clamp_arm_value(side, joint, value)
    return clean


def _clamp_arm_positions(side: str, positions: Mapping[str, float] | Sequence[float]) -> dict[str, float]:
    side = _normalize_side(side)
    if isinstance(positions, Mapping):
        targets = {
            _normalize_joint_key(str(joint), side=side): _clamp_arm_value(side, str(joint), float(value))
            for joint, value in positions.items()
        }
    elif isinstance(positions, Sequence) and not isinstance(positions, (str, bytes, bytearray)):
        if len(positions) != len(ARM_JOINTS):
            raise ValueError(f"positions sequence must contain {len(ARM_JOINTS)} values")
        targets = {
            joint: _clamp_arm_value(side, joint, float(value))
            for joint, value in zip(ARM_JOINTS, positions, strict=True)
        }
    else:
        raise TypeError("positions must be a mapping or a sequence of joint targets")
    return targets


def _clamp_arm_value(side: str, joint: str, value: float) -> float:
    joint = _normalize_joint_key(joint, side=side)
    lower, upper = ARM_POSITION_LIMITS_RAD[side][joint]
    return min(max(float(value), lower), upper)


def _normalize_side(side: str) -> str:
    side = str(side).lower()
    if side not in ARM_SIDES:
        raise ValueError(f"side must be one of {ARM_SIDES}, got {side!r}")
    return side


def _normalize_joint_key(key: str, *, side: str) -> str:
    prefix = f"{side}_"
    if key.startswith(prefix):
        key = key[len(prefix) :]
    if key.endswith(".pos"):
        key = key[:-4]
    if key not in ARM_JOINTS:
        raise ValueError(f"joint must be one of {ARM_JOINTS}, got {key!r}")
    return key


def _normalize_camera_key(camera: str) -> str:
    key = str(camera).lower()
    try:
        return CAMERA_ALIASES[key]
    except KeyError as exc:
        raise ValueError(f"camera must be one of {tuple(CAMERA_ALIASES)}, got {camera!r}") from exc


def _ensure_lerobot_importable(explicit_src: Path | None) -> None:
    candidates: list[Path] = []
    if explicit_src:
        candidates.append(explicit_src)
    here = Path(__file__).resolve()
    candidates.append(here.parents[2] / "hei-rebot-lift" / "software" / "lerobot-hei-rebot-lift" / "src")
    for path in candidates:
        if path.exists() and str(path) not in sys.path:
            sys.path.insert(0, str(path))


def _numeric_dict(values: Any) -> dict[str, float]:
    return {str(key): float(value) for key, value in dict(values).items() if _is_number(value)}


def _filter_action_sent(sent: Mapping[str, float], requested: Mapping[str, float]) -> dict[str, float]:
    return {key: float(sent.get(key, value)) for key, value in requested.items()}


def _semantic_to_hardware_action(action: Mapping[str, float]) -> dict[str, float]:
    return {str(key): _semantic_to_hardware_value(str(key), float(value)) for key, value in action.items()}


def _hardware_to_semantic_action(action: Mapping[str, float]) -> dict[str, float]:
    return {str(key): _hardware_to_semantic_value(str(key), float(value)) for key, value in action.items()}


def _semantic_to_hardware_value(key: str, value: float) -> float:
    if INVERT_HARDWARE_CHASSIS_XY and key in {"x.vel", "y.vel"}:
        return -value
    return value


def _hardware_to_semantic_value(key: str, value: float) -> float:
    if INVERT_HARDWARE_CHASSIS_XY and key in {"x.vel", "y.vel"}:
        return -value
    return value


def _call_with_optional_velocity(method, kwargs: dict[str, Any], *, velocity_limit_rad_s: Any):
    signature = inspect.signature(method)
    accepts_kwargs = any(param.kind == inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values())
    if velocity_limit_rad_s is not None and (accepts_kwargs or "velocity_limit_rad_s" in signature.parameters):
        return method(**kwargs, velocity_limit_rad_s=velocity_limit_rad_s)
    return method(**kwargs)


def _is_number(value: Any) -> bool:
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke-test the HEI ReBot Lift platform adapter.")
    parser.add_argument(
        "test",
        choices=("info", "observe", "cameras", "chassis", "lift", "gripper"),
        help="Hardware check to run. Motion tests require --yes.",
    )
    parser.add_argument("--remote-ip", default=None, help="Use HEI ReBot Lift host/client mode.")
    parser.add_argument("--robot-id", default="hei_rebot_lift")
    parser.add_argument("--lerobot-src", type=Path, default=None)
    parser.add_argument("--yes", action="store_true", help="Allow commands that move hardware.")
    parser.add_argument("--duration", type=float, default=0.5, help="Motion test duration in seconds.")
    parser.add_argument("--x", type=float, default=0.03, help="Chassis x velocity for chassis test.")
    parser.add_argument("--y", type=float, default=0.0, help="Chassis y velocity for chassis test.")
    parser.add_argument("--theta", type=float, default=0.0, help="Chassis yaw velocity for chassis test.")
    parser.add_argument("--height", type=float, default=-20.0, help="Lift target height.pos for lift test.")
    parser.add_argument("--side", choices=ARM_SIDES, default="right", help="Arm side for gripper test.")
    parser.add_argument("--gripper", type=float, default=-0.5, help="Gripper target position.")
    args = parser.parse_args(list(argv) if argv is not None else None)

    platform = HeiRebotLiftPlatform(
        HeiRebotLiftPlatformConfig(
            remote_ip=args.remote_ip,
            robot_id=args.robot_id,
            lerobot_src=args.lerobot_src,
        )
    )

    try:
        if args.test == "info":
            print(
                json.dumps(
                    {
                        "mode": platform.config.mode,
                        "action_features": sorted(platform.action_features),
                        "observation_features": _feature_summary(platform.observation_features),
                    },
                    indent=2,
                    sort_keys=True,
                    default=str,
                )
            )
            return 0

        print(f"Connecting HEI ReBot Lift in {platform.config.mode} mode...")
        platform.connect()

        if args.test == "observe":
            obs = platform.observe()
            print(
                json.dumps(
                    {
                        "state": obs.state,
                        "images": {key: _shape_of(value) for key, value in obs.images.items()},
                        "extras": {key: str(value) for key, value in obs.extras.items()},
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0

        if args.test == "cameras":
            images = platform.get_camera_images()
            print(json.dumps({key: _shape_of(value) for key, value in images.items()}, indent=2, sort_keys=True))
            return 0

        _require_motion_confirmation(args.yes, args.test)

        if args.test == "chassis":
            print(f"Commanding chassis x={args.x} y={args.y} theta={args.theta} for {args.duration}s")
            sent = platform.command_chassis(x=args.x, y=args.y, theta=args.theta)
            time.sleep(max(args.duration, 0.0))
            platform.stop_motion()
            print(json.dumps({"sent": sent, "velocity": platform.read_chassis_velocity()}, indent=2, sort_keys=True))
            return 0

        if args.test == "lift":
            print(f"Commanding lift height.pos={args.height}")
            sent = platform.send_action({"height.pos": args.height})
            time.sleep(max(args.duration, 0.0))
            print(json.dumps({"sent": sent, "state": platform.observe().state}, indent=2, sort_keys=True))
            return 0

        if args.test == "gripper":
            print(f"Commanding {args.side} gripper={args.gripper}")
            sent = platform.set_gripper(args.side, args.gripper)
            time.sleep(max(args.duration, 0.0))
            print(json.dumps({"sent": sent, "arms": platform.read_arm_positions(args.side)}, indent=2, sort_keys=True))
            return 0

    finally:
        if args.test in {"chassis", "lift"} and args.yes:
            with _suppress_errors():
                platform.stop_motion()
        platform.close()

    return 1


def _feature_summary(features: Mapping[str, Any]) -> dict[str, str]:
    return {key: str(value) for key, value in sorted(features.items())}


def _shape_of(value: Any) -> str:
    shape = getattr(value, "shape", None)
    if shape is not None:
        return "x".join(str(part) for part in shape)
    return type(value).__name__


def _require_motion_confirmation(yes: bool, test: str) -> None:
    if not yes:
        raise SystemExit(f"{test} can move hardware. Re-run with --yes after clearing the robot workspace.")


class _suppress_errors:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: Any) -> bool:
        return True


if __name__ == "__main__":
    raise SystemExit(_main())
