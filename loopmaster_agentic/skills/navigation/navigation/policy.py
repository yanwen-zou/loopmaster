from __future__ import annotations

import math
import time
import uuid
from typing import Any


PROTOCOL_VERSION = 1
DEFAULT_ROBOT_IP = "192.168.31.22"
DEFAULT_STATUS_PORT = 7210
DEFAULT_COMMAND_PORT = 7211
DEFAULT_STATUS_TIMEOUT_S = 1.5
DEFAULT_SEND_TIMEOUT_MS = 1000
MEMORY_LAST_GOAL_ID = "navigation.last_goal_id"


def dispatch(context, args):
    command = str(args.get("command") or args.get("action") or "goal").strip().lower()
    robot_ip = str(args.get("robot_ip") or DEFAULT_ROBOT_IP).strip()
    status_port = _int_arg(args, "status_port", DEFAULT_STATUS_PORT)
    command_port = _int_arg(args, "command_port", DEFAULT_COMMAND_PORT)
    status_timeout_s = _float_arg(args, "status_timeout_s", DEFAULT_STATUS_TIMEOUT_S)
    send_timeout_ms = _int_arg(args, "send_timeout_ms", DEFAULT_SEND_TIMEOUT_MS)
    if not robot_ip:
        return {"ok": False, "error": "robot_ip must not be empty"}
    if status_port <= 0 or command_port <= 0:
        return {"ok": False, "error": "status_port and command_port must be positive"}
    if status_timeout_s < 0.0:
        return {"ok": False, "error": "status_timeout_s must be non-negative"}
    if send_timeout_ms <= 0:
        return {"ok": False, "error": "send_timeout_ms must be positive"}

    if command in {"goal", "navigate", "navigate_to_pose"}:
        return _dispatch_goal(context, args, robot_ip, status_port, command_port, status_timeout_s, send_timeout_ms)
    if command == "status":
        return _dispatch_status(robot_ip, status_port, status_timeout_s)
    if command == "cancel":
        return _dispatch_cancel(context, args, robot_ip, status_port, command_port, status_timeout_s, send_timeout_ms)
    if command == "ping":
        return _dispatch_ping(robot_ip, status_port, command_port, status_timeout_s, send_timeout_ms)
    return {"ok": False, "error": f"unsupported navigation command: {command}"}


def _dispatch_goal(context, args, robot_ip, status_port, command_port, status_timeout_s, send_timeout_ms):
    try:
        payload = _make_goal_command(
            _finite_float(args.get("x"), "x"),
            _finite_float(args.get("y"), "y"),
            _finite_float(args.get("yaw", 0.0), "yaw"),
            goal_id=args.get("goal_id"),
            frame_id=str(args.get("frame_id") or "map"),
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    sent = _send_json(robot_ip, command_port, payload, send_timeout_ms)
    if not sent["ok"]:
        return sent
    context.memory[MEMORY_LAST_GOAL_ID] = payload["goal_id"]

    result = {
        "ok": True,
        "command": "navigate_to_pose",
        "goal_id": payload["goal_id"],
        "frame_id": payload["frame_id"],
        "target": {"x": payload["x"], "y": payload["y"], "yaw": payload["yaw"]},
        "command_endpoint": sent["endpoint"],
    }
    if bool(args.get("wait_for_ack", True)):
        result.update(_wait_for_ack(robot_ip, status_port, status_timeout_s, "navigate_to_pose", payload["goal_id"]))
    return result


def _dispatch_status(robot_ip, status_port, status_timeout_s):
    status = _receive_status(robot_ip, status_port, status_timeout_s)
    if status is None:
        return {
            "ok": False,
            "error": "no navigation status received",
            "status_endpoint": _status_endpoint(robot_ip, status_port),
        }
    return {"ok": True, "status": status, "summary": _status_summary(status)}


def _dispatch_cancel(context, args, robot_ip, status_port, command_port, status_timeout_s, send_timeout_ms):
    goal_id = str(args.get("goal_id") or context.memory.get(MEMORY_LAST_GOAL_ID, ""))
    payload = _make_cancel_command(goal_id)
    sent = _send_json(robot_ip, command_port, payload, send_timeout_ms)
    if not sent["ok"]:
        return sent
    result = {"ok": True, "command": "cancel", "goal_id": goal_id, "command_endpoint": sent["endpoint"]}
    if bool(args.get("wait_for_ack", True)):
        result.update(_wait_for_ack(robot_ip, status_port, status_timeout_s, "cancel", goal_id))
    return result


def _dispatch_ping(robot_ip, status_port, command_port, status_timeout_s, send_timeout_ms):
    request_id = str(uuid.uuid4())
    payload = {"protocol_version": PROTOCOL_VERSION, "type": "ping", "request_id": request_id, "timestamp": time.time()}
    sent = _send_json(robot_ip, command_port, payload, send_timeout_ms)
    if not sent["ok"]:
        return sent
    result = {"ok": True, "command": "ping", "request_id": request_id, "command_endpoint": sent["endpoint"]}
    result.update(_wait_for_ack(robot_ip, status_port, status_timeout_s, "ping", request_id))
    return result


def _send_json(robot_ip: str, command_port: int, payload: dict[str, Any], send_timeout_ms: int) -> dict[str, Any]:
    try:
        import zmq
    except ImportError:
        return {"ok": False, "error": "pyzmq is required for navigation; install the hei-rebot-lift extra"}

    endpoint = _command_endpoint(robot_ip, command_port)
    context = zmq.Context()
    socket = context.socket(zmq.PUSH)
    socket.setsockopt(zmq.LINGER, 0)
    socket.setsockopt(zmq.IMMEDIATE, 1)
    socket.setsockopt(zmq.SNDHWM, 10)
    socket.setsockopt(zmq.SNDTIMEO, send_timeout_ms)
    try:
        socket.connect(endpoint)
        socket.send_json(payload)
        return {"ok": True, "endpoint": endpoint}
    except zmq.ZMQError as exc:
        return {"ok": False, "error": f"navigation command send failed: {exc}", "endpoint": endpoint}
    finally:
        socket.close(linger=0)
        context.term()


def _receive_status(robot_ip: str, status_port: int, timeout_s: float) -> dict[str, Any] | None:
    try:
        import zmq
    except ImportError:
        return None

    endpoint = _status_endpoint(robot_ip, status_port)
    context = zmq.Context()
    socket = context.socket(zmq.SUB)
    socket.setsockopt_string(zmq.SUBSCRIBE, "")
    socket.setsockopt(zmq.CONFLATE, 1)
    socket.setsockopt(zmq.RCVTIMEO, max(1, int(timeout_s * 1000)))
    socket.setsockopt(zmq.LINGER, 0)
    deadline = time.monotonic() + timeout_s
    try:
        socket.connect(endpoint)
        latest = None
        while time.monotonic() <= deadline:
            try:
                latest = socket.recv_json()
            except zmq.Again:
                break
            except (ValueError, zmq.ZMQError):
                break
        return latest
    finally:
        socket.close(linger=0)
        context.term()


def _wait_for_ack(robot_ip: str, status_port: int, timeout_s: float, command_type: str, identifier: str) -> dict[str, Any]:
    status = _receive_status(robot_ip, status_port, timeout_s)
    if status is None:
        return {"ack_received": False, "status": None, "summary": "", "warning": "no status ack received before timeout"}
    ack = status.get("last_command_ack") or {}
    ack_matches = ack.get("type") == command_type
    if identifier:
        ack_matches = ack_matches and identifier in {str(ack.get("goal_id", "")), str(ack.get("message", ""))}
    return {
        "ack_received": bool(ack_matches),
        "ack": ack,
        "status": status,
        "summary": _status_summary(status),
        "ok": bool(ack.get("accepted", True)) if ack_matches else True,
    }


def _make_goal_command(x: float, y: float, yaw: float, goal_id: Any = None, frame_id: str = "map") -> dict[str, Any]:
    frame_id = str(frame_id).strip()
    if not frame_id:
        raise ValueError("frame_id must not be empty")
    return {
        "protocol_version": PROTOCOL_VERSION,
        "type": "navigate_to_pose",
        "goal_id": str(goal_id or uuid.uuid4()),
        "frame_id": frame_id,
        "x": x,
        "y": y,
        "yaw": yaw,
        "timestamp": time.time(),
    }


def _make_cancel_command(goal_id: str = "") -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "type": "cancel",
        "goal_id": str(goal_id),
        "timestamp": time.time(),
    }


def _status_summary(status: dict[str, Any]) -> str:
    pose = status.get("pose") or status.get("last_valid_pose")
    nav = status.get("navigation") or {}
    if pose:
        pose_text = f"x={float(pose.get('x', 0.0)):.3f} y={float(pose.get('y', 0.0)):.3f} yaw={float(pose.get('yaw', 0.0)):.3f}"
        if status.get("pose") is None:
            pose_text += " (stale pose)"
    else:
        pose_text = "pose unavailable"
    nav_text = f"nav={nav.get('state', 'unknown')} goal={nav.get('goal_id') or '-'}"
    distance = nav.get("distance_remaining")
    if distance is not None:
        nav_text += f" remaining={float(distance):.2f}m"
    if nav.get("error"):
        nav_text += f" error={nav['error']}"
    return f"{pose_text} | {nav_text}"


def _finite_float(value: Any, field_name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field_name} must be finite")
    return number


def _float_arg(args: dict[str, Any], key: str, default: float) -> float:
    try:
        return float(args.get(key, default))
    except (TypeError, ValueError):
        return default


def _int_arg(args: dict[str, Any], key: str, default: int) -> int:
    try:
        return int(args.get(key, default))
    except (TypeError, ValueError):
        return default


def _command_endpoint(robot_ip: str, command_port: int) -> str:
    return f"tcp://{robot_ip}:{command_port}"


def _status_endpoint(robot_ip: str, status_port: int) -> str:
    return f"tcp://{robot_ip}:{status_port}"
