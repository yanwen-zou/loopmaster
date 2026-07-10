from __future__ import annotations

import argparse
import select
import sys
import termios
import tty
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loopmaster_agentic.platform.dry_run import DryRunPlatform
from loopmaster_agentic.platform.hei_rebot_lift import (
    ARM_JOINTS,
    ARM_SIDES,
    HeiRebotLiftPlatform,
    HeiRebotLiftPlatformConfig,
)
from loopmaster_agentic.skills.registry import SkillContext, SkillRegistry


KEY_HELP = """
LoopMaster keyboard control

Chassis:
  w/s: x +/-        a/d: y +/-        q/e: theta +/-
  space: stop       x or Ctrl-C: quit

Arm joints:
  l/r: select left/right arm
  1-7: select joint_1..joint_6/gripper
  +/-: increment/decrement selected joint
  [ ]: larger decrement/increment
  o: refresh selected arm positions from observation
"""

ARM_SIDE_KEYS = {
    "l": "left",
    "r": "right",
}


@dataclass
class TeleopState:
    side: str = "right"
    joint_index: int = 0
    arm_positions: dict[str, dict[str, float]] | None = None

    def __post_init__(self) -> None:
        if self.arm_positions is None:
            self.arm_positions = {
                side: {joint: 0.0 for joint in ARM_JOINTS} for side in ARM_SIDES
            }

    @property
    def joint(self) -> str:
        return ARM_JOINTS[self.joint_index]

    def selected_positions(self) -> dict[str, float]:
        assert self.arm_positions is not None
        return self.arm_positions[self.side]


class _WorkspaceStub:
    root = Path(".")

    def append_trace(self, record: dict[str, Any]) -> None:
        return None


class RawTerminal:
    def __enter__(self) -> "RawTerminal":
        self._fd = sys.stdin.fileno()
        self._old = termios.tcgetattr(self._fd)
        tty.setcbreak(self._fd)
        return self

    def __exit__(self, *exc: Any) -> None:
        termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)

    def read_key(self, timeout_s: float) -> str | None:
        readable, _, _ = select.select([sys.stdin], [], [], timeout_s)
        if not readable:
            return None
        key = sys.stdin.read(1)
        if key == "\x1b" and select.select([sys.stdin], [], [], 0.001)[0]:
            key += sys.stdin.read(2)
        return key


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Keyboard-control HEI ReBot Lift through LoopMaster control skills."
    )
    parser.add_argument("--dry-run", action="store_true", help="Use in-memory platform instead of hardware.")
    parser.add_argument("--remote-ip", default="172.16.22.19", help="Use HEI ReBot Lift host/client mode.")
    parser.add_argument("--robot-id", default="hei_rebot_lift")
    parser.add_argument("--lerobot-src", type=Path, default=None)
    parser.add_argument("--yes", action="store_true", help="Allow real hardware motion.")
    parser.add_argument("--linear-speed", type=float, default=0.05, help="Chassis x/y velocity command.")
    parser.add_argument("--angular-speed", type=float, default=0.15, help="Chassis theta velocity command.")
    parser.add_argument("--joint-step", type=float, default=0.02, help="Small arm joint increment.")
    parser.add_argument("--large-joint-step", type=float, default=0.10, help="Large arm joint increment.")
    parser.add_argument("--poll", type=float, default=0.05, help="Keyboard poll interval in seconds.")
    args = parser.parse_args(argv)

    if not args.dry_run and not args.yes:
        raise SystemExit("Real robot control can move hardware. Re-run with --yes after clearing the workspace.")

    platform = _make_platform(args)
    registry = SkillRegistry(include_user=False)
    context = SkillContext(platform=platform, workspace=_WorkspaceStub())
    state = TeleopState()

    print(KEY_HELP)
    print("Connecting platform...")
    platform.connect()

    try:
        _refresh_arm_positions(context, state, quiet=True)
        _print_status(state)
        with RawTerminal() as terminal:
            while True:
                key = terminal.read_key(max(args.poll, 0.001))
                if key is None:
                    continue
                if key in {"\x03", "x", "X"}:
                    break
                _handle_key(key, args, registry, context, state)
    finally:
        _dispatch(registry, context, "stop_motion", {"reason": "keyboard_control exit"})
        platform.close()
        print("\nStopped and disconnected.")

    return 0


def _make_platform(args: argparse.Namespace):
    if args.dry_run:
        return DryRunPlatform()
    return HeiRebotLiftPlatform(
        HeiRebotLiftPlatformConfig(
            remote_ip=args.remote_ip,
            robot_id=args.robot_id,
            lerobot_src=args.lerobot_src,
        )
    )


def _handle_key(
    key: str,
    args: argparse.Namespace,
    registry: SkillRegistry,
    context: SkillContext,
    state: TeleopState,
) -> None:
    chassis = {
        "w": (args.linear_speed, 0.0, 0.0),
        "s": (-args.linear_speed, 0.0, 0.0),
        "a": (0.0, args.linear_speed, 0.0),
        "d": (0.0, -args.linear_speed, 0.0),
        "q": (0.0, 0.0, args.angular_speed),
        "e": (0.0, 0.0, -args.angular_speed),
    }
    lowered = key.lower()
    if lowered in chassis:
        x, y, theta = chassis[lowered]
        _dispatch(registry, context, "set_base_velocity", {"x": x, "y": y, "theta": theta})
        print(f"\rbase x={x:+.3f} y={y:+.3f} theta={theta:+.3f}                 ", end="", flush=True)
        return
    if key == " ":
        _dispatch(registry, context, "stop_motion", {"reason": "keyboard stop"})
        print("\rbase stopped                                      ", end="", flush=True)
        return
    if lowered in ARM_SIDE_KEYS:
        state.side = ARM_SIDE_KEYS[lowered]
        _print_status(state)
        return
    if key in {"1", "2", "3", "4", "5", "6", "7"}:
        state.joint_index = int(key) - 1
        _print_status(state)
        return
    if key in {"+", "=", "-", "_", "[", "]"}:
        step = args.large_joint_step if key in {"[", "]"} else args.joint_step
        delta = step if key in {"+", "=", "]"} else -step
        _move_selected_joint(registry, context, state, delta)
        return
    if lowered == "o":
        _refresh_arm_positions(context, state, quiet=False)
        _print_status(state)


def _move_selected_joint(
    registry: SkillRegistry,
    context: SkillContext,
    state: TeleopState,
    delta: float,
) -> None:
    positions = state.selected_positions()
    positions[state.joint] += delta
    result = _dispatch(
        registry,
        context,
        "move_arm_joints",
        {"side": state.side, "positions": dict(positions)},
    )
    if not result.get("ok"):
        positions[state.joint] -= delta
        print(f"\nmove_arm_joints failed: {result.get('error')}")
        return
    print(
        f"\r{state.side} {state.joint}={positions[state.joint]:+.3f}                  ",
        end="",
        flush=True,
    )


def _refresh_arm_positions(context: SkillContext, state: TeleopState, *, quiet: bool) -> None:
    try:
        obs = context.platform.observe()
    except Exception as exc:
        if not quiet:
            print(f"\nobserve failed: {type(exc).__name__}: {exc}")
        return
    context.last_observation = obs
    assert state.arm_positions is not None
    for side in ARM_SIDES:
        for joint in ARM_JOINTS:
            key = f"{side}_{joint}.pos"
            if key in obs.state:
                state.arm_positions[side][joint] = float(obs.state[key])


def _dispatch(
    registry: SkillRegistry,
    context: SkillContext,
    name: str,
    args: dict[str, Any],
) -> dict[str, Any]:
    result = registry.dispatch(name, context, args)
    if not result.get("ok"):
        print(f"\n{name} failed: {result.get('error')}")
    return result


def _print_status(state: TeleopState) -> None:
    value = state.selected_positions()[state.joint]
    print(
        f"\rarm={state.side} joint={state.joint} value={value:+.3f}                  ",
        end="",
        flush=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
