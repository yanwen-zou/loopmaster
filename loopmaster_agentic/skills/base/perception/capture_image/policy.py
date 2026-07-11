from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


DEFAULT_D435_ROBOT_IP = "192.168.31.22"
DEFAULT_D435_PORT = 6560
DEFAULT_D435_TOPIC = "d435_rgbd"


def dispatch(context, args):
    camera = str(args.get("camera") or "front")
    if _wants_d435_rgbd(camera, args):
        return _capture_d435_rgbd(context, args, camera)
    return _capture_from_observation(context, args, camera)


def _wants_d435_rgbd(camera: str, args: dict[str, Any]) -> bool:
    source = str(args.get("source") or args.get("mode") or "").lower()
    if source in {"d435", "d435_rgbd", "rgbd", "zmq"}:
        return True
    if camera.lower() in {"d435", "d435_rgbd", "rgbd"}:
        return True
    return any(key in args for key in ("robot_ip", "endpoint", "port", "topic", "rgb_path", "depth_path", "output_dir"))


def _capture_d435_rgbd(context, args: dict[str, Any], camera: str) -> dict[str, Any]:
    required = bool(args.get("required", True))
    timeout_ms = int(args.get("timeout_ms") or 3000)
    topic = str(args.get("topic") or DEFAULT_D435_TOPIC)
    endpoint = _endpoint_from_args(args)

    try:
        cv2, np, zmq = _import_rgbd_deps()
        parts = _recv_latest_rgbd_multipart(zmq, endpoint, topic, timeout_ms)
        recv_s = time.time()
        metadata, color_bgr, depth_u16 = _decode_rgbd_parts(cv2, np, parts)
        paths = _save_rgbd_capture(context, args, cv2, metadata, color_bgr, depth_u16)
    except Exception as exc:
        return {
            "ok": not required,
            "captured": False,
            "camera": camera,
            "source": "d435_rgbd",
            "endpoint": endpoint,
            "topic": topic,
            "reason": str(exc),
        }

    timestamp_s = float(metadata.get("timestamp_s", recv_s))
    valid_depth_pct = float(np.count_nonzero(depth_u16)) / float(depth_u16.size) * 100.0 if depth_u16.size else 0.0
    return {
        "ok": True,
        "captured": True,
        "camera": camera,
        "source": "d435_rgbd",
        "endpoint": endpoint,
        "topic": topic,
        "metadata": _jsonable_metadata(metadata),
        "rgb": {**_image_summary(color_bgr), "path": str(paths["rgb_path"])},
        "depth": {
            **_image_summary(depth_u16),
            "path": str(paths["depth_path"]),
            "depth_scale_m": float(metadata.get("depth_scale_m", 0.001)),
            "valid_depth_pct": round(valid_depth_pct, 3),
        },
        "age_ms": round((recv_s - timestamp_s) * 1000.0, 3),
    }


def _endpoint_from_args(args: dict[str, Any]) -> str:
    endpoint = args.get("endpoint")
    if endpoint:
        return str(endpoint)
    robot_ip = str(args.get("robot_ip") or DEFAULT_D435_ROBOT_IP)
    port = int(args.get("port") or DEFAULT_D435_PORT)
    return f"tcp://{robot_ip}:{port}"


def _import_rgbd_deps():
    try:
        import cv2
        import numpy as np
        import zmq
    except ImportError as exc:
        raise RuntimeError("D435 RGB-D capture requires opencv-python-headless, numpy, and pyzmq") from exc
    return cv2, np, zmq


def _recv_latest_rgbd_multipart(zmq, endpoint: str, topic: str, timeout_ms: int):
    zmq_context = zmq.Context.instance()
    socket = zmq_context.socket(zmq.SUB)
    socket.setsockopt(zmq.RCVHWM, 2)
    socket.setsockopt(zmq.RCVTIMEO, max(100, int(timeout_ms)))
    socket.setsockopt_string(zmq.SUBSCRIBE, topic)
    socket.connect(endpoint)
    try:
        try:
            parts = socket.recv_multipart()
        except zmq.Again as exc:
            raise RuntimeError(
                f"timed out after {timeout_ms}ms waiting for D435 RGB-D frames from {endpoint}; "
                "check that robot_d435_rgbd_sender.py is running"
            ) from exc

        while True:
            try:
                parts = socket.recv_multipart(flags=zmq.NOBLOCK)
            except zmq.Again:
                break
        if len(parts) != 4:
            raise RuntimeError(f"expected 4 ZMQ message parts, got {len(parts)}")
        return parts
    finally:
        socket.close(0)


def _decode_rgbd_parts(cv2, np, parts):
    _topic_bytes, metadata_bytes, color_bytes, depth_bytes = parts
    metadata = json.loads(metadata_bytes.decode("utf-8"))

    color_arr = np.frombuffer(color_bytes, dtype=np.uint8)
    color_bgr = cv2.imdecode(color_arr, cv2.IMREAD_COLOR)
    if color_bgr is None:
        raise RuntimeError("failed to decode color JPEG")

    depth_arr = np.frombuffer(depth_bytes, dtype=np.uint8)
    depth_u16 = cv2.imdecode(depth_arr, cv2.IMREAD_UNCHANGED)
    if depth_u16 is None:
        raise RuntimeError("failed to decode depth PNG")
    if depth_u16.dtype != np.uint16:
        depth_u16 = depth_u16.astype(np.uint16)

    return metadata, color_bgr, depth_u16


def _save_rgbd_capture(context, args: dict[str, Any], cv2, metadata: dict[str, Any], color_bgr, depth_u16):
    workspace_root = _workspace_root(context)
    output_dir = _resolve_workspace_path(args.get("output_dir") or "captures", workspace_root=workspace_root)
    frame_id = int(metadata.get("frame_id", 0))
    rgb_path = _resolve_workspace_path(args.get("rgb_path") or output_dir / f"d435_frame_{frame_id:06d}_rgb.png", workspace_root=workspace_root)
    depth_path = _resolve_workspace_path(args.get("depth_path") or output_dir / f"d435_frame_{frame_id:06d}_depth.png", workspace_root=workspace_root)
    metadata_path = _resolve_workspace_path(args.get("metadata_path") or output_dir / f"d435_frame_{frame_id:06d}.json", workspace_root=workspace_root)

    for path in (rgb_path, depth_path, metadata_path):
        path.parent.mkdir(parents=True, exist_ok=True)

    if not cv2.imwrite(str(rgb_path), color_bgr):
        raise RuntimeError(f"failed to write RGB image to {rgb_path}")
    if not cv2.imwrite(str(depth_path), depth_u16):
        raise RuntimeError(f"failed to write depth image to {depth_path}")
    metadata_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")

    return {"rgb_path": rgb_path, "depth_path": depth_path, "metadata_path": metadata_path}


def _workspace_root(context) -> Path | None:
    workspace = getattr(context, "workspace", None)
    root = getattr(workspace, "root", None)
    if root is not None:
        return Path(root).expanduser().resolve()
    return None


def _resolve_workspace_path(value: Any, *, workspace_root: Path | None) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()
    if workspace_root is not None:
        return (workspace_root / path).resolve()
    return path.resolve()


def _capture_from_observation(context, args: dict[str, Any], camera: str) -> dict[str, Any]:
    required = bool(args.get("required", False))
    if hasattr(context.platform, "get_camera_image"):
        try:
            image = context.platform.get_camera_image(camera)
        except (KeyError, ValueError):
            observation = context.last_observation or context.platform.observe()
            context.last_observation = observation
            return {
                "ok": not required,
                "captured": False,
                "camera": camera,
                "available": sorted(observation.images),
                "reason": "camera frame not present in latest observation",
            }
        return {"ok": True, "captured": True, "camera": camera, "image": _image_summary(image)}

    observation = context.last_observation or context.platform.observe()
    context.last_observation = observation
    if camera not in observation.images:
        return {
            "ok": not required,
            "captured": False,
            "camera": camera,
            "available": sorted(observation.images),
            "reason": "camera frame not present in latest observation",
        }
    return {
        "ok": True,
        "captured": True,
        "camera": camera,
        "image": observation.summary()["images"].get(camera, {}),
    }


def _jsonable_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    return {str(key): _jsonable_value(value) for key, value in metadata.items()}


def _jsonable_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable_value(item) for item in value]
    return str(value)


def _image_summary(image):
    shape = getattr(image, "shape", None)
    dtype = getattr(image, "dtype", None)
    if shape is not None:
        return {"shape": tuple(int(v) for v in shape), "dtype": str(dtype)}
    return {"type": type(image).__name__}
