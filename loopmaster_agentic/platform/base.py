from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from loopmaster_agentic.core.types import Observation


class RobotPlatform(ABC):
    """Small real-robot contract used by LoopMaster skills."""

    name: str = "robot"

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def observe(self) -> Observation: ...

    @abstractmethod
    def send_action(self, action: dict[str, float]) -> dict[str, float]: ...

    @abstractmethod
    def stop_motion(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @property
    def action_features(self) -> dict[str, type]:
        return {}

    @property
    def observation_features(self) -> dict[str, type | tuple[int, ...]]:
        return {}

    def __enter__(self) -> "RobotPlatform":
        self.connect()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
