#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from loopmaster_agentic.agents.workspace import new_workspace
from loopmaster_agentic.platform.dry_run import DryRunPlatform
from loopmaster_agentic.platform.hei_rebot_lift import HeiRebotLiftPlatform, HeiRebotLiftPlatformConfig
from loopmaster_agentic.skills.navigation.navigation.policy import (
    DEFAULT_COMMAND_PORT,
    DEFAULT_ROBOT_IP,
    DEFAULT_STATUS_PORT,
)
from loopmaster_agentic.skills.registry import SkillContext, SkillRegistry


TERMINAL_STATES = {"succeeded", "failed", "canceled", "cancelled", "aborted"}
KEYBOARD_MOTION = {
    "up": (1.0, 0.0, 0.0),
    "down": (-1.0, 0.0, 0.0),
    "a": (0.0, 1.0, 0.0),
    "d": (0.0, -1.0, 0.0),
    "left": (0.0, 0.0, 1.0),
    "right": (0.0, 0.0, -1.0),
}
REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_MAP_YAML = (
    REPO_ROOT
    / "hei-rebot-lift"
    / "software"
    / "lerobot-hei-rebot-lift"
    / "navigation"
    / "map"
    / "map.yaml"
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Send a navigation skill goal and display live map-frame pose/trajectory."
    )
    parser.add_argument("--x", type=float, default=None, help="Goal x in map frame, meters.")
    parser.add_argument("--y", type=float, default=None, help="Goal y in map frame, meters.")
    parser.add_argument("--yaw", type=float, default=0.0, help="Goal yaw in map frame, radians.")
    parser.add_argument("--robot-ip", default=DEFAULT_ROBOT_IP)
    parser.add_argument("--status-port", type=int, default=DEFAULT_STATUS_PORT)
    parser.add_argument("--command-port", type=int, default=DEFAULT_COMMAND_PORT)
    parser.add_argument("--status-timeout-s", type=float, default=0.5)
    parser.add_argument("--send-timeout-ms", type=int, default=1000)
    parser.add_argument("--poll-s", type=float, default=0.02)
    parser.add_argument("--status-print-s", type=float, default=0.5, help="Minimum interval between status prints.")
    parser.add_argument("--duration-s", type=float, default=0.0, help="0 means run until terminal state or Ctrl+C.")
    parser.add_argument("--goal-id", default="", help="Optional goal id; generated when omitted.")
    parser.add_argument("--status-only", action="store_true", help="Only monitor status; do not send a goal.")
    parser.add_argument("--no-wait-ack", action="store_true", help="Do not wait for goal ack after sending.")
    parser.add_argument("--cancel-on-exit", action="store_true", help="Cancel the sent goal when exiting via Ctrl+C/error.")
    parser.add_argument("--trail-limit", type=int, default=1000, help="Maximum trajectory points displayed.")
    parser.add_argument("--axis-padding", type=float, default=0.8)
    parser.add_argument("--map-yaml", type=Path, default=DEFAULT_MAP_YAML, help="Optional ROS map yaml for background.")
    parser.add_argument("--no-map-image", action="store_true", help="Do not draw the occupancy-grid map background.")
    parser.add_argument("--print-json", action="store_true", help="Print raw status JSON each poll.")
    parser.add_argument("--workspace-root", type=Path, default=Path("_runs"))
    parser.add_argument(
        "--keyboard-control",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable matplotlib keyboard teleop; key motion cancels active map-click navigation.",
    )
    parser.add_argument("--platform-remote-ip", default="", help="HEI platform remote IP for keyboard teleop; defaults to --robot-ip.")
    parser.add_argument("--robot-id", default="hei_rebot_lift")
    parser.add_argument("--linear-speed", type=float, default=0.18, help="Keyboard body-frame x/y speed in m/s.")
    parser.add_argument("--angular-speed", type=float, default=0.36, help="Keyboard yaw speed in rad/s.")
    parser.add_argument("--keyboard-refresh-hz", type=float, default=20.0, help="Refresh held keyboard velocity commands at this rate.")
    parser.add_argument(
        "--keyboard-x-sign",
        type=float,
        default=1.0,
        help="Sign applied to keyboard x velocity.",
    )
    parser.add_argument("--keyboard-y-sign", type=float, default=1.0, help="Sign applied to keyboard y velocity.")
    parser.add_argument("--keyboard-theta-sign", type=float, default=1.0, help="Sign applied to keyboard yaw velocity.")
    parser.add_argument("--keyboard-release-grace-s", type=float, default=0.25, help="Delay before stopping after key-release events.")
    parser.add_argument(
        "--goal-x-sign",
        type=float,
        default=1.0,
        help="Sign applied to map-click/CLI goal x relative to current pose. Use -1 only for explicit mirror compensation.",
    )
    parser.add_argument(
        "--goal-y-sign",
        type=float,
        default=1.0,
        help="Sign applied to map-click/CLI goal y relative to current pose. Use -1 only for explicit mirror compensation.",
    )
    args = parser.parse_args()

    plt = _load_matplotlib()
    registry = SkillRegistry(include_user=False)
    workspace = new_workspace("navigation_live_map", root=args.workspace_root)
    nav_context = SkillContext(platform=DryRunPlatform(), workspace=workspace)
    receiver = StatusReceiver(
        endpoint=f"tcp://{args.robot_ip}:{args.status_port}",
        timeout_s=args.status_timeout_s,
    )
    receiver.start()
    keyboard_platform = None
    keyboard_context = None
    if args.keyboard_control:
        keyboard_platform = HeiRebotLiftPlatform(
            HeiRebotLiftPlatformConfig(remote_ip=args.platform_remote_ip or args.robot_ip, robot_id=args.robot_id)
        )
        try:
            keyboard_platform.connect()
        except Exception:
            receiver.stop()
            raise
        keyboard_context = SkillContext(platform=keyboard_platform, workspace=workspace)

    goal_id = str(args.goal_id or f"nav-live-{uuid.uuid4()}")
    goal = (
        {"x": float(args.x), "y": float(args.y), "yaw": float(args.yaw), "goal_id": goal_id}
        if not args.status_only and args.x is not None and args.y is not None
        else None
    )
    sent_initial_goal = None

    if goal is not None:
        status_for_transform = receiver.wait_for_status(timeout_s=1.0)
        sent_initial_goal = _transform_goal_relative_to_pose(
            goal,
            status_for_transform,
            x_sign=float(args.goal_x_sign),
            y_sign=float(args.goal_y_sign),
        )
        result = _call_skill(
            registry,
            nav_context,
            "navigation",
            {
                "command": "goal",
                **sent_initial_goal,
                "robot_ip": args.robot_ip,
                "status_port": args.status_port,
                "command_port": args.command_port,
                "status_timeout_s": args.status_timeout_s,
                "send_timeout_ms": args.send_timeout_ms,
                "wait_for_ack": not args.no_wait_ack,
            },
        )
        print(
            json.dumps(
                {"requested_goal": goal, "sent_goal": sent_initial_goal, "goal_result": result, "workspace": str(workspace.root)},
                ensure_ascii=False,
                indent=2,
            )
        )
        if not result.get("ok"):
            receiver.stop()
            if keyboard_platform is not None:
                keyboard_platform.close()
            return 1

    map_image = None if args.no_map_image else _load_map_image(plt, args.map_yaml)
    viewer = LiveMapViewer(
        plt,
        goal=goal,
        sent_goal=sent_initial_goal,
        axis_padding=args.axis_padding,
        trail_limit=args.trail_limit,
        map_image=map_image,
    )
    controller = InteractiveController(
        registry=registry,
        nav_context=nav_context,
        keyboard_context=keyboard_context,
        robot_ip=args.robot_ip,
        status_port=args.status_port,
        command_port=args.command_port,
        status_timeout_s=args.status_timeout_s,
        send_timeout_ms=args.send_timeout_ms,
        wait_ack=not args.no_wait_ack,
        default_yaw=float(args.yaw),
        linear_speed=float(args.linear_speed),
        angular_speed=float(args.angular_speed),
        keyboard_refresh_hz=float(args.keyboard_refresh_hz),
        keyboard_release_grace_s=float(args.keyboard_release_grace_s),
        keyboard_x_sign=float(args.keyboard_x_sign),
        keyboard_y_sign=float(args.keyboard_y_sign),
        keyboard_theta_sign=float(args.keyboard_theta_sign),
        goal_x_sign=float(args.goal_x_sign),
        goal_y_sign=float(args.goal_y_sign),
        viewer=viewer,
        active_goal_id=goal_id if goal is not None else "",
    )
    started = time.monotonic()
    next_status_print = 0.0
    exit_code = 0
    try:
        while True:
            now = time.monotonic()
            controller.refresh_keyboard_motion(now=now)
            status, received_at = receiver.snapshot()
            status_age = now - received_at if status is not None else None
            status_result = (
                {"ok": True, "status": status, "summary": _status_text(status).replace("\n", " | ") + f" | age={status_age:.1f}s"}
                if status is not None
                else {
                    "ok": False,
                    "error": receiver.error or f"no navigation status received from tcp://{args.robot_ip}:{args.status_port}",
                }
            )
            if now >= next_status_print:
                if args.print_json:
                    print(json.dumps(status_result, ensure_ascii=False, default=str))
                elif status_result.get("ok"):
                    print(status_result.get("summary", ""), flush=True)
                else:
                    print(status_result.get("error", status_result), flush=True)
                next_status_print = now + max(float(args.status_print_s), 0.05)

            if status_result.get("ok"):
                status = status_result["status"]
                viewer.update(status)
                controller.update_status(status)
            else:
                viewer.update_error(str(status_result.get("error") or status_result))

            if args.duration_s > 0 and time.monotonic() - started >= args.duration_s:
                break
            if not viewer.is_open:
                break
            plt.pause(max(args.poll_s, 0.01))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        exit_code = 130
    finally:
        if args.cancel_on_exit and goal is not None:
            cancel = _call_skill(
                registry,
                nav_context,
                "navigation",
                {
                    "command": "cancel",
                    "goal_id": controller.active_goal_id or goal_id,
                    "robot_ip": args.robot_ip,
                    "status_port": args.status_port,
                    "command_port": args.command_port,
                    "status_timeout_s": args.status_timeout_s,
                    "send_timeout_ms": args.send_timeout_ms,
                },
            )
            print(json.dumps({"cancel_result": cancel}, ensure_ascii=False, indent=2))
        controller.close()
        print(f"workspace: {workspace.root}")
        receiver.stop()
        if keyboard_platform is not None:
            keyboard_platform.close()
        if viewer.is_open:
            plt.ioff()
            plt.show()
    return exit_code


def _call_skill(
    registry: SkillRegistry,
    context: SkillContext,
    name: str,
    args: dict[str, Any],
    *,
    trace: bool = True,
) -> dict[str, Any]:
    result = registry.dispatch(name, context, args)
    if trace:
        context.workspace.append_trace({"skill": name, "args": args, "result": result})
    if not result.get("ok"):
        print(f"{name} failed: {result.get('error') or result}")
    return result


def _load_matplotlib():
    try:
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise SystemExit(
            "matplotlib is required for the live map window. Install it in this env, for example: uv pip install matplotlib"
        ) from exc
    return plt


class StatusReceiver:
    def __init__(self, *, endpoint: str, timeout_s: float) -> None:
        self.endpoint = endpoint
        self.timeout_s = max(float(timeout_s), 0.05)
        self.latest: dict[str, Any] | None = None
        self.latest_received_at = 0.0
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.error: str | None = None

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=1.0)

    def snapshot(self) -> tuple[dict[str, Any] | None, float]:
        with self.lock:
            return self.latest, self.latest_received_at

    def wait_for_status(self, *, timeout_s: float) -> dict[str, Any] | None:
        deadline = time.monotonic() + max(timeout_s, 0.0)
        while time.monotonic() <= deadline:
            status, _received_at = self.snapshot()
            if status is not None:
                return status
            time.sleep(0.02)
        return None

    def _run(self) -> None:
        try:
            import zmq
        except ImportError as exc:
            self.error = f"pyzmq is required for navigation status: {exc}"
            return

        context = zmq.Context()
        socket = context.socket(zmq.SUB)
        socket.setsockopt_string(zmq.SUBSCRIBE, "")
        socket.setsockopt(zmq.CONFLATE, 1)
        socket.setsockopt(zmq.RCVTIMEO, max(1, int(self.timeout_s * 1000)))
        socket.setsockopt(zmq.LINGER, 0)
        socket.connect(self.endpoint)
        try:
            while not self.stop_event.is_set():
                try:
                    status = socket.recv_json()
                except zmq.Again:
                    continue
                except (ValueError, zmq.ZMQError) as exc:
                    self.error = str(exc)
                    continue
                with self.lock:
                    self.latest = status
                    self.latest_received_at = time.monotonic()
        finally:
            socket.close(linger=0)
            context.term()


class InteractiveController:
    def __init__(
        self,
        *,
        registry: SkillRegistry,
        nav_context: SkillContext,
        keyboard_context: SkillContext | None,
        robot_ip: str,
        status_port: int,
        command_port: int,
        status_timeout_s: float,
        send_timeout_ms: int,
        wait_ack: bool,
        default_yaw: float,
        linear_speed: float,
        angular_speed: float,
        keyboard_refresh_hz: float,
        keyboard_release_grace_s: float,
        keyboard_x_sign: float,
        keyboard_y_sign: float,
        keyboard_theta_sign: float,
        goal_x_sign: float,
        goal_y_sign: float,
        viewer: "LiveMapViewer",
        active_goal_id: str,
    ) -> None:
        self.registry = registry
        self.nav_context = nav_context
        self.keyboard_context = keyboard_context
        self.robot_ip = robot_ip
        self.status_port = status_port
        self.command_port = command_port
        self.status_timeout_s = status_timeout_s
        self.send_timeout_ms = send_timeout_ms
        self.wait_ack = wait_ack
        self.default_yaw = default_yaw
        self.linear_speed = abs(linear_speed)
        self.angular_speed = abs(angular_speed)
        self.keyboard_refresh_s = 1.0 / max(float(keyboard_refresh_hz), 1.0)
        self.keyboard_release_grace_s = max(float(keyboard_release_grace_s), 0.0)
        self.keyboard_x_sign = float(keyboard_x_sign)
        self.keyboard_y_sign = float(keyboard_y_sign)
        self.keyboard_theta_sign = float(keyboard_theta_sign)
        self.goal_x_sign = float(goal_x_sign)
        self.goal_y_sign = float(goal_y_sign)
        self.viewer = viewer
        self.active_goal_id = active_goal_id
        self.keyboard_active = False
        self.active_keyboard_command: dict[str, float] | None = None
        self.keyboard_stop_deadline = 0.0
        self.keyboard_lock = threading.Lock()
        self.keyboard_stop_event = threading.Event()
        self.keyboard_thread = threading.Thread(target=self._keyboard_refresh_loop, daemon=True)
        self.last_status: dict[str, Any] | None = None

        viewer.fig.canvas.mpl_connect("button_press_event", self.on_click)
        viewer.fig.canvas.mpl_connect("key_press_event", self.on_key_press)
        viewer.fig.canvas.mpl_connect("key_release_event", self.on_key_release)
        viewer.set_control_text(self._help_text())
        self.keyboard_thread.start()

    def update_status(self, status: dict[str, Any]) -> None:
        self.last_status = status
        if self.active_goal_id and _is_terminal(status, self.active_goal_id):
            self.active_goal_id = ""

    def refresh_keyboard_motion(self, *, now: float) -> None:
        with self.keyboard_lock:
            if self.keyboard_active and self.keyboard_stop_deadline and now >= self.keyboard_stop_deadline:
                self._stop_keyboard_motion_locked(send_stop=True)
                return

    def _keyboard_refresh_loop(self) -> None:
        while not self.keyboard_stop_event.is_set():
            command = None
            with self.keyboard_lock:
                if self.keyboard_active and self.active_keyboard_command is not None:
                    command = dict(self.active_keyboard_command)
            if command is not None and self.keyboard_context is not None:
                result = _call_skill(self.registry, self.keyboard_context, "set_base_velocity", command, trace=False)
                if not result.get("ok"):
                    with self.keyboard_lock:
                        self.keyboard_active = False
                        self.active_keyboard_command = None
            time.sleep(self.keyboard_refresh_s)

    def on_click(self, event) -> None:
        if event.inaxes is not self.viewer.ax or event.xdata is None or event.ydata is None:
            return
        if getattr(event, "button", None) != 1:
            return
        if self.keyboard_active:
            print("keyboard control active; map click ignored")
            return
        goal_id = f"nav-click-{uuid.uuid4()}"
        clicked_goal = {"x": float(event.xdata), "y": float(event.ydata), "yaw": self.default_yaw, "goal_id": goal_id}
        sent_goal = _transform_goal_relative_to_pose(
            clicked_goal,
            self.last_status,
            x_sign=self.goal_x_sign,
            y_sign=self.goal_y_sign,
        )
        result = _call_skill(
            self.registry,
            self.nav_context,
            "navigation",
            {
                "command": "goal",
                **sent_goal,
                "robot_ip": self.robot_ip,
                "status_port": self.status_port,
                "command_port": self.command_port,
                "status_timeout_s": self.status_timeout_s,
                "send_timeout_ms": self.send_timeout_ms,
                "wait_for_ack": self.wait_ack,
            },
        )
        print(json.dumps({"map_click_goal": clicked_goal, "sent_goal": sent_goal, "result": result}, ensure_ascii=False, indent=2))
        if result.get("ok"):
            self.active_goal_id = goal_id
            self.viewer.set_goal(clicked_goal, sent_goal=sent_goal)
            if _same_xy(clicked_goal, sent_goal):
                self.viewer.set_control_text(f"sent goal x={sent_goal['x']:.2f} y={sent_goal['y']:.2f}")
            else:
                self.viewer.set_control_text(
                    f"clicked x={clicked_goal['x']:.2f} y={clicked_goal['y']:.2f}; "
                    f"sent goal x={sent_goal['x']:.2f} y={sent_goal['y']:.2f}"
                )

    def on_key_press(self, event) -> None:
        key = str(getattr(event, "key", "") or "").lower()
        if key in {"escape", "x"}:
            self.stop_keyboard_motion()
            self.viewer.is_open = False
            self.viewer.plt.close(self.viewer.fig)
            return
        if key == " ":
            self.cancel_navigation_for_keyboard()
            self.stop_keyboard_motion()
            return
        if key not in KEYBOARD_MOTION:
            return
        if self.keyboard_context is None:
            self.viewer.set_control_text("keyboard disabled: start without --no-keyboard-control and ensure platform server is running")
            return
        self.cancel_navigation_for_keyboard()
        self.keyboard_active = True
        x_dir, y_dir, theta_dir = KEYBOARD_MOTION[key]
        command = {
            "x": x_dir * self.linear_speed * self.keyboard_x_sign,
            "y": y_dir * self.linear_speed * self.keyboard_y_sign,
            "theta": theta_dir * self.angular_speed * self.keyboard_theta_sign,
            "duration_s": 0.0,
        }
        with self.keyboard_lock:
            self.active_keyboard_command = command
            self.keyboard_active = True
            self.keyboard_stop_deadline = 0.0
        self.viewer.set_control_text(
            f"keyboard override: x={command['x']:+.2f} y={command['y']:+.2f} theta={command['theta']:+.2f}"
        )

    def on_key_release(self, event) -> None:
        key = str(getattr(event, "key", "") or "").lower()
        if key in KEYBOARD_MOTION:
            with self.keyboard_lock:
                if self.keyboard_active:
                    self.keyboard_stop_deadline = time.monotonic() + self.keyboard_release_grace_s

    def cancel_navigation_for_keyboard(self) -> None:
        if not self.active_goal_id:
            return
        result = _call_skill(
            self.registry,
            self.nav_context,
            "navigation",
            {
                "command": "cancel",
                "goal_id": self.active_goal_id,
                "robot_ip": self.robot_ip,
                "status_port": self.status_port,
                "command_port": self.command_port,
                "status_timeout_s": self.status_timeout_s,
                "send_timeout_ms": self.send_timeout_ms,
                "wait_for_ack": False,
            },
        )
        print(json.dumps({"keyboard_cancel_goal": self.active_goal_id, "result": result}, ensure_ascii=False, indent=2))
        self.active_goal_id = ""
        self.viewer.set_goal(None, sent_goal=None)

    def stop_keyboard_motion(self) -> None:
        if self.keyboard_context is None:
            with self.keyboard_lock:
                self.keyboard_active = False
                self.active_keyboard_command = None
            return
        with self.keyboard_lock:
            self._stop_keyboard_motion_locked(send_stop=True)
        self.viewer.set_control_text(self._help_text())

    def _stop_keyboard_motion_locked(self, *, send_stop: bool) -> None:
        was_active = self.keyboard_active
        self.keyboard_active = False
        self.active_keyboard_command = None
        self.keyboard_stop_deadline = 0.0
        if send_stop and was_active and self.keyboard_context is not None:
            _call_skill(
                self.registry,
                self.keyboard_context,
                "stop_motion",
                {"reason": "keyboard override stop"},
                trace=False,
            )

    def close(self) -> None:
        self.stop_keyboard_motion()
        self.keyboard_stop_event.set()
        self.keyboard_thread.join(timeout=1.0)

    def _help_text(self) -> str:
        if self.keyboard_context is None:
            return "left click: navigate | keyboard disabled"
        return (
            "left click: navigate | up/down: forward/back | a/d: strafe "
            "| left/right: rotate | space: stop | x/esc: quit"
        )


def _load_map_image(plt, map_yaml: Path | None) -> dict[str, Any] | None:
    if map_yaml is None or not map_yaml.is_file():
        return None
    try:
        meta = _read_simple_yaml(map_yaml)
        image_path = Path(str(meta["image"]))
        if not image_path.is_absolute():
            image_path = map_yaml.parent / image_path
        resolution = float(meta["resolution"])
        origin = meta["origin"]
        if not isinstance(origin, list) or len(origin) < 2:
            return None
        image = plt.imread(str(image_path))
        height, width = image.shape[:2]
        x0 = float(origin[0])
        y0 = float(origin[1])
        extent = [x0, x0 + width * resolution, y0, y0 + height * resolution]
        return {"image": image, "extent": extent, "path": str(image_path)}
    except Exception as exc:
        print(f"map background disabled: {type(exc).__name__}: {exc}")
        return None


def _transform_goal_relative_to_pose(
    goal: dict[str, float],
    status: dict[str, Any] | None,
    *,
    x_sign: float,
    y_sign: float,
) -> dict[str, float]:
    pose = _pose(status or {})
    if pose is None:
        return dict(goal)
    transformed = dict(goal)
    transformed["x"] = pose["x"] + float(x_sign) * (float(goal["x"]) - pose["x"])
    transformed["y"] = pose["y"] + float(y_sign) * (float(goal["y"]) - pose["y"])
    return transformed


def _same_xy(left: dict[str, float], right: dict[str, float], *, tol: float = 1e-6) -> bool:
    return abs(float(left["x"]) - float(right["x"])) <= tol and abs(float(left["y"]) - float(right["y"])) <= tol


def _read_simple_yaml(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        data[key.strip()] = _parse_yaml_value(value.strip())
    return data


def _parse_yaml_value(value: str) -> Any:
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_parse_yaml_value(item.strip()) for item in inner.split(",")]
    try:
        if any(char in value.lower() for char in (".", "e")):
            return float(value)
        return int(value)
    except ValueError:
        return value.strip("'\"")


def _is_terminal(status: dict[str, Any], goal_id: str) -> bool:
    nav = status.get("navigation") or {}
    state = str(nav.get("state") or "").lower()
    current_goal = str(nav.get("goal_id") or nav.get("queued_goal_id") or "")
    return state in TERMINAL_STATES and (not current_goal or current_goal == goal_id)


def _pose(status: dict[str, Any]) -> dict[str, float] | None:
    raw = status.get("pose") or status.get("last_valid_pose")
    if not isinstance(raw, dict):
        return None
    try:
        return {"x": float(raw["x"]), "y": float(raw["y"]), "yaw": float(raw.get("yaw", 0.0))}
    except (KeyError, TypeError, ValueError):
        return None


class LiveMapViewer:
    def __init__(
        self,
        plt,
        *,
        goal: dict[str, float] | None,
        sent_goal: dict[str, float] | None,
        axis_padding: float,
        trail_limit: int,
        map_image: dict[str, Any] | None,
    ) -> None:
        self.plt = plt
        self.goal = goal
        self.sent_goal = sent_goal
        self.axis_padding = max(float(axis_padding), 0.1)
        self.trail_limit = max(int(trail_limit), 2)
        self.points: list[tuple[float, float]] = []
        self.is_open = True

        plt.ion()
        self.fig, self.ax = plt.subplots(num="LoopMaster navigation live map")
        self.ax.set_title("LoopMaster navigation live map")
        self.ax.set_xlabel("map x (m)")
        self.ax.set_ylabel("map y (m)")
        self.ax.grid(True, alpha=0.35)
        self.ax.set_aspect("equal", adjustable="box")
        if map_image is not None:
            self.ax.imshow(
                map_image["image"],
                extent=map_image["extent"],
                origin="upper",
                cmap="gray",
                alpha=0.65,
                zorder=0,
            )

        (self.trail_line,) = self.ax.plot([], [], "-", linewidth=1.8, label="trajectory", zorder=3)
        (self.robot_dot,) = self.ax.plot([], [], "o", markersize=8, label="robot", zorder=4)
        (self.goal_marker,) = self.ax.plot([], [], "*", markersize=13, label="sent goal", zorder=4)
        (self.sent_goal_marker,) = self.ax.plot([], [], "o", markersize=8, fillstyle="none", label="clicked point", zorder=4)
        self.heading = None
        self.status_text = self.ax.text(0.02, 0.98, "", transform=self.ax.transAxes, va="top")
        self.control_text = self.ax.text(0.02, 0.02, "", transform=self.ax.transAxes, va="bottom")
        if goal is not None:
            self.set_goal(goal, sent_goal=sent_goal)
        self.ax.legend(loc="lower right")
        self.fig.canvas.mpl_connect("close_event", self._on_close)

    def update(self, status: dict[str, Any]) -> None:
        pose = _pose(status)
        if pose is not None:
            self.points.append((pose["x"], pose["y"]))
            self.points = self.points[-self.trail_limit :]
            xs = [point[0] for point in self.points]
            ys = [point[1] for point in self.points]
            self.trail_line.set_data(xs, ys)
            self.robot_dot.set_data([pose["x"]], [pose["y"]])
            self._draw_heading(pose)
            self._rescale(pose)
        self.status_text.set_text(_status_text(status, sent_goal=self.sent_goal))
        self.fig.canvas.draw_idle()

    def update_error(self, error: str) -> None:
        self.status_text.set_text(f"status error: {error}")
        self.fig.canvas.draw_idle()

    def set_goal(self, goal: dict[str, float] | None, *, sent_goal: dict[str, float] | None = None) -> None:
        self.goal = goal
        self.sent_goal = sent_goal
        plotted_goal = sent_goal or goal
        if plotted_goal is None:
            self.goal_marker.set_data([], [])
        else:
            self.goal_marker.set_data([plotted_goal["x"]], [plotted_goal["y"]])
        if goal is None or sent_goal is None or _same_xy(goal, sent_goal):
            self.sent_goal_marker.set_data([], [])
        else:
            self.sent_goal_marker.set_data([goal["x"]], [goal["y"]])
        self.fig.canvas.draw_idle()

    def set_control_text(self, text: str) -> None:
        self.control_text.set_text(text)
        self.fig.canvas.draw_idle()

    def _draw_heading(self, pose: dict[str, float]) -> None:
        if self.heading is not None:
            self.heading.remove()
        length = 0.35
        dx = math.cos(pose["yaw"]) * length
        dy = math.sin(pose["yaw"]) * length
        self.heading = self.ax.arrow(
            pose["x"],
            pose["y"],
            dx,
            dy,
            head_width=0.12,
            head_length=0.16,
            length_includes_head=True,
            zorder=5,
        )

    def _rescale(self, pose: dict[str, float]) -> None:
        xs = [point[0] for point in self.points]
        ys = [point[1] for point in self.points]
        if self.goal is not None:
            xs.append(self.goal["x"])
            ys.append(self.goal["y"])
        if self.sent_goal is not None:
            xs.append(self.sent_goal["x"])
            ys.append(self.sent_goal["y"])
        sent_x, sent_y = self.sent_goal_marker.get_data()
        if len(sent_x) and len(sent_y):
            xs.append(float(sent_x[0]))
            ys.append(float(sent_y[0]))
        xs.append(pose["x"])
        ys.append(pose["y"])
        self.ax.set_xlim(min(xs) - self.axis_padding, max(xs) + self.axis_padding)
        self.ax.set_ylim(min(ys) - self.axis_padding, max(ys) + self.axis_padding)

    def _on_close(self, _event) -> None:
        self.is_open = False


def _status_text(status: dict[str, Any], *, sent_goal: dict[str, float] | None = None) -> str:
    pose = _pose(status)
    nav = status.get("navigation") or {}
    pose_text = "pose unavailable"
    if pose is not None:
        pose_text = f"x={pose['x']:.3f} y={pose['y']:.3f} yaw={pose['yaw']:.3f}"
    parts = [
        pose_text,
        f"state={nav.get('state', 'unknown')}",
        f"goal={nav.get('goal_id') or '-'}",
    ]
    if pose is not None and sent_goal is not None:
        dx = float(sent_goal["x"]) - pose["x"]
        dy = float(sent_goal["y"]) - pose["y"]
        parts.append(f"sent_goal=({float(sent_goal['x']):.3f},{float(sent_goal['y']):.3f}) error={math.hypot(dx, dy):.2f}m")
    if nav.get("distance_remaining") is not None:
        parts.append(f"nav2_remaining={float(nav['distance_remaining']):.2f}m")
    if nav.get("error"):
        parts.append(f"error={nav['error']}")
    return "\n".join(parts)


if __name__ == "__main__":
    raise SystemExit(main())
