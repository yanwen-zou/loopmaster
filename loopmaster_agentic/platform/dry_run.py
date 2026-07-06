from __future__ import annotations

from loopmaster_agentic.core.types import Observation
from loopmaster_agentic.platform.base import RobotPlatform
from loopmaster_agentic.platform.hei_rebot_lift import CONTROL_KEYS


class DryRunPlatform(RobotPlatform):
    """In-memory platform for framework smoke tests, not a simulator."""

    name = "dry_run"

    def __init__(self) -> None:
        self.connected = False
        self.state = {key: 0.0 for key in CONTROL_KEYS}
        self.actions: list[dict[str, float]] = []

    @property
    def action_features(self) -> dict[str, type]:
        return {key: float for key in sorted(CONTROL_KEYS)}

    @property
    def observation_features(self) -> dict[str, type | tuple[int, ...]]:
        return {**self.action_features, "front": (1, 1, 3)}

    def connect(self) -> None:
        self.connected = True

    def observe(self) -> Observation:
        return Observation(images={}, state=dict(self.state), extras={"platform": self.name})

    def send_action(self, action: dict[str, float]) -> dict[str, float]:
        clean = {key: float(value) for key, value in action.items() if key in self.state}
        self.state.update(clean)
        self.actions.append(clean)
        return clean

    def stop_motion(self) -> None:
        self.send_action({"x.vel": 0.0, "y.vel": 0.0, "theta.vel": 0.0})

    def close(self) -> None:
        self.connected = False
