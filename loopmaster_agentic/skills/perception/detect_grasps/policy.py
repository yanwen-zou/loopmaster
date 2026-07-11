from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import time
import zipfile
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any


REQUIRED_MODULES = ("numpy", "PIL", "torch", "open3d", "MinkowskiEngine", "graspnetAPI", "pointnet2")
DEFAULT_DETECT_DOWN_DEVICE = "enx00e04c360914"
DEFAULT_D435_INTRINSICS_PATH = (
    Path(__file__).resolve().parents[4]
    / "hei-rebot-lift"
    / "software"
    / "lerobot-hei-rebot-lift"
    / "src"
    / "lerobot"
    / "cameras"
    / "d435_intrinsics_640x480.json"
)


def dispatch(context, args):
    args = _merge_grounded_sam2_memory(context, dict(args or {}))
    args = _merge_capture_image_memory(context, args)
    sdk_root = Path(args.get("sdk_root") or _default_sdk_root()).expanduser().resolve()
    detection_dir = sdk_root / "grasp_detection"
    if not detection_dir.is_dir():
        return {"ok": False, "error": f"AnyGrasp detection directory not found: {detection_dir}"}

    try:
        gsnet_so = _prepare_gsnet_so(detection_dir)
        license_dir = _prepare_license(sdk_root, detection_dir, args)
    except Exception as exc:
        return {"ok": False, "error": f"failed to prepare AnyGrasp SDK layout: {type(exc).__name__}: {exc}"}

    checkpoint_path = _checkpoint_path(detection_dir, args)
    status = {
        "sdk_root": str(sdk_root),
        "detection_dir": str(detection_dir),
        "gsnet_so": str(gsnet_so),
        "license_dir": str(license_dir) if license_dir else "",
        "checkpoint_path": str(checkpoint_path),
        "python": sys.executable,
        "feature_id": "",
        "license_ok": False,
        "checkpoint_exists": checkpoint_path.is_file(),
        "dependencies": {},
        "license_network": {},
    }

    dependency_ok = _check_dependencies(status)
    gsnet = _import_gsnet(detection_dir, status)
    if gsnet is None:
        missing = _missing_requirements(status, dependency_ok)
        if args.get("check_only"):
            return {"ok": not missing, "status": status, "missing": missing}
        return {"ok": False, "error": "gsnet import failed", "status": status}

    with _license_network_context(args, status["license_network"]):
        status["feature_id"] = _safe_feature_id(gsnet)
        status["license_ok"] = _safe_check_license(gsnet, license_dir)

        missing = _missing_requirements(status, dependency_ok)
        if args.get("check_only"):
            return {"ok": not missing, "status": status, "missing": missing}
        if missing:
            return {"ok": False, "error": "AnyGrasp is not ready", "status": status, "missing": missing}

        try:
            points, _colors, seg_mask, source = _load_points(detection_dir, args)
        except Exception as exc:
            return {"ok": False, "error": f"failed to load point cloud: {type(exc).__name__}: {exc}"}

        try:
            detector = gsnet.create_detector(
                SimpleNamespace(
                    checkpoint_path=str(checkpoint_path),
                    max_gripper_width=float(args.get("max_gripper_width", 0.1)),
                    gripper_height=float(args.get("gripper_height", 0.03)),
                )
            )
            if detector is None:
                return {"ok": False, "error": "AnyGrasp create_detector returned None", "status": status}

            dense_grasp = bool(args.get("dense_grasp", False))
            optional_params = _optional_params(points, seg_mask, args, dense_grasp)
            region = optional_params.get("region_steering")
            if region is not None:
                source["region_point_count"] = int(region.sum())
                source["region_point_fraction"] = float(region.sum() / max(1, points.shape[0]))
                if _should_reject_empty_explicit_region(args, source["region_point_count"]):
                    return {
                        "ok": False,
                        "error": "segmentation mask selected zero valid depth points; refusing unconstrained AnyGrasp",
                        "status": status,
                        "source": source,
                        "point_count": int(points.shape[0]),
                    }
            grasps = detector.get_grasp(points, optional_params)
            if grasps is None:
                return {"ok": True, "status": status, "source": source, "grasp_count": 0, "grasps": []}
            if not dense_grasp:
                grasps = grasps.nms()
            grasps = grasps.sort_by_score()
        except Exception as exc:
            return {"ok": False, "error": f"AnyGrasp inference failed: {type(exc).__name__}: {exc}"}

    top_k = max(1, int(args.get("top_k", 5)))
    return {
        "ok": True,
        "status": status,
        "source": source,
        "point_count": int(points.shape[0]),
        "grasp_count": int(_safe_len(grasps)),
        "top_k": top_k,
        "grasps": _serialize_grasps(grasps[:top_k]),
    }


def _prepare_gsnet_so(detection_dir: Path) -> Path:
    target = detection_dir / "gsnet.so"
    ext_suffix = sysconfig.get_config_var("EXT_SUFFIX") or ""
    candidates = []
    if ext_suffix:
        candidates.append(detection_dir / "gsnet_versions" / f"gsnet{ext_suffix}")
    py_tag = f"cpython-{sys.version_info.major}{sys.version_info.minor}"
    candidates.extend(sorted((detection_dir / "gsnet_versions").glob(f"gsnet.{py_tag}*.so")))
    source = next((path for path in candidates if path.is_file()), None)
    if source is None:
        raise FileNotFoundError(f"no gsnet binary found for Python {sys.version_info.major}.{sys.version_info.minor}")
    if not target.exists() or target.stat().st_size != source.stat().st_size:
        shutil.copy2(source, target)
    return target


def _merge_grounded_sam2_memory(context: Any, args: dict[str, Any]) -> dict[str, Any]:
    if args.get("seg_mask_path") or args.get("region_mask_path"):
        return args
    latest = getattr(context, "memory", {}).get("grounded_sam2") if hasattr(context, "memory") else None
    if not isinstance(latest, dict):
        return args
    seg_mask_path = latest.get("seg_mask_path")
    if seg_mask_path:
        args["seg_mask_path"] = seg_mask_path
        args.setdefault("region_object_id", latest.get("anygrasp_hint", {}).get("region_object_id") or 1)
    return args


def _merge_capture_image_memory(context: Any, args: dict[str, Any]) -> dict[str, Any]:
    if args.get("color_path") and args.get("depth_path"):
        return args
    latest = getattr(context, "memory", {}).get("capture_image") if hasattr(context, "memory") else None
    if not isinstance(latest, dict):
        return args
    rgb = latest.get("rgb")
    depth = latest.get("depth")
    if not args.get("color_path") and isinstance(rgb, dict) and rgb.get("path"):
        args["color_path"] = rgb["path"]
    if not args.get("depth_path") and isinstance(depth, dict) and depth.get("path"):
        args["depth_path"] = depth["path"]
    return args


def _prepare_license(sdk_root: Path, detection_dir: Path, args: dict[str, Any]) -> Path | None:
    license_dir = Path(args.get("license_dir") or detection_dir / "license").expanduser()
    if not license_dir.is_absolute():
        license_dir = (detection_dir / license_dir).resolve()
    if _license_complete(license_dir):
        return license_dir

    license_zip = Path(args.get("license_zip") or sdk_root / "license_YanwenZou2.zip").expanduser()
    if not license_zip.is_absolute():
        license_zip = (sdk_root / license_zip).resolve()
    if not license_zip.is_file():
        return license_dir if license_dir.exists() else None

    license_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(license_zip) as archive:
        for member in archive.namelist():
            if member.endswith("/"):
                continue
            target_name = Path(member).name
            if not target_name:
                continue
            with archive.open(member) as src, open(license_dir / target_name, "wb") as dst:
                shutil.copyfileobj(src, dst)
    return license_dir


def _license_complete(license_dir: Path) -> bool:
    if not license_dir.is_dir() or not (license_dir / "licenseCfg.json").is_file():
        return False
    suffixes = {path.suffix for path in license_dir.iterdir() if path.is_file()}
    return {".lic", ".signature", ".public_key"} <= suffixes


def _checkpoint_path(detection_dir: Path, args: dict[str, Any]) -> Path:
    checkpoint = str(args.get("checkpoint_path") or os.environ.get("ANYGRASP_CHECKPOINT_PATH") or "")
    if checkpoint:
        path = Path(checkpoint).expanduser()
        return path if path.is_absolute() else (detection_dir / path).resolve()
    return detection_dir / "log" / "checkpoint_detection.tar"


def _check_dependencies(status: dict[str, Any]) -> bool:
    ok = True
    for module in REQUIRED_MODULES:
        try:
            imported = __import__(module)
        except Exception as exc:
            status["dependencies"][module] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            ok = False
        else:
            status["dependencies"][module] = {"ok": True, "version": str(getattr(imported, "__version__", ""))}
    return ok


def _import_gsnet(detection_dir: Path, status: dict[str, Any]) -> Any:
    sys.path.insert(0, str(detection_dir))
    try:
        with _third_party_stdout_to_stderr():
            gsnet = __import__("gsnet")
    except Exception as exc:
        status["dependencies"]["gsnet"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return None
    status["dependencies"]["gsnet"] = {"ok": True, "version": str(getattr(gsnet, "__version__", ""))}
    return gsnet


def _safe_feature_id(gsnet: Any) -> str:
    try:
        return str(gsnet.get_feature_id())
    except Exception as exc:
        return f"ERROR {type(exc).__name__}: {exc}"


def _safe_check_license(gsnet: Any, license_dir: Path | None) -> bool:
    if license_dir is None:
        return False
    try:
        with _third_party_stdout_to_stderr():
            return bool(gsnet.check_license(str(license_dir)))
    except Exception:
        return False


def _missing_requirements(status: dict[str, Any], dependency_ok: bool) -> list[str]:
    missing = []
    missing_packages = [
        module for module in (*REQUIRED_MODULES, "gsnet") if not status["dependencies"].get(module, {}).get("ok")
    ]
    if not dependency_ok or missing_packages:
        missing.append("python_dependencies")
        status["missing_packages"] = missing_packages
    if not status["license_ok"]:
        missing.append("valid_license")
    if not status["checkpoint_exists"]:
        missing.append("checkpoint")
    return missing


@contextmanager
def _license_network_context(args: dict[str, Any], status: dict[str, Any]):
    down_devices = _resolve_license_down_devices(args)
    disable_devices = _list_arg(args.get("license_disconnect_device") or args.get("detect_disable_device"))
    interactive_sudo = bool(args.get("license_interactive_sudo") or args.get("interactive_sudo"))
    if bool(args.get("wifi_only_license_check") or args.get("wifi_only_detect")):
        disable_devices.extend(_active_non_wifi_devices())
    disable_devices = list(dict.fromkeys(device for device in disable_devices if device))
    active_disable = [_active_connection_for_device(device) for device in disable_devices]
    active_down = [_active_connection_for_device(device) for device in down_devices]
    status.update(
        {
            "down_devices": list(down_devices),
            "disconnect_devices": list(disable_devices),
            "sudo_password_provided": bool(os.environ.get("LOOPMASTER_SUDO_PASSWORD")),
            "interactive_sudo": interactive_sudo,
            "events": [],
            "restore_warnings": [],
        }
    )
    try:
        for device in disable_devices:
            result = _run(["nmcli", "dev", "disconnect", device], check=False)
            _record_network_event(status, "disconnect", device, result)
        for device in down_devices:
            result = _sudo_ip_link(device, "down", interactive=interactive_sudo)
            _record_network_event(status, "link_down", device, result)
        if down_devices or disable_devices:
            time.sleep(float(args.get("license_network_settle_s", 2.0)))
        yield
    finally:
        for device, connection in zip(down_devices, active_down, strict=False):
            _restore_network_device(device, connection, status)
        for connection in active_disable:
            if connection:
                _restore_network_device(connection["device"], connection, status)
        if down_devices or disable_devices:
            time.sleep(float(args.get("license_network_settle_s", 2.0)))


def _resolve_license_down_devices(args: dict[str, Any]) -> list[str]:
    requested = _list_arg(args.get("license_down_device") or args.get("detect_down_device"))
    if not bool(args.get("no_default_license_down_device") or args.get("no_default_detect_down_device")):
        if DEFAULT_DETECT_DOWN_DEVICE not in requested and _network_device_exists(DEFAULT_DETECT_DOWN_DEVICE):
            requested.insert(0, DEFAULT_DETECT_DOWN_DEVICE)
    return list(dict.fromkeys(device for device in requested if device))


def _list_arg(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


def _network_device_exists(device: str) -> bool:
    return _run(["ip", "link", "show", device], check=False).returncode == 0


def _restore_network_device(device: str, connection: dict[str, str] | None, status: dict[str, Any]) -> None:
    interactive_sudo = bool(status.get("interactive_sudo"))
    _record_network_event(status, "link_up", device, _sudo_ip_link(device, "up", interactive=interactive_sudo))
    _record_network_event(status, "connect", device, _run(["nmcli", "dev", "connect", device], check=False))
    if connection:
        for attempt in range(1, 4):
            result = _run(["nmcli", "con", "up", "uuid", connection["uuid"]], check=False)
            _record_network_event(status, f"connection_up_attempt_{attempt}", device, result)
            if _wait_for_device_state(device, "connected", timeout_s=8.0):
                return
    if not _wait_for_link_up(device, timeout_s=5.0):
        status.setdefault("restore_warnings", []).append(f"{device} did not return to link UP")


def _active_non_wifi_devices() -> list[str]:
    out = _run_text(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "dev", "status"], check=False)
    devices = []
    for line in out.splitlines():
        parts = line.split(":")
        if len(parts) < 3:
            continue
        device, device_type, state = parts[:3]
        if state == "connected" and device_type not in {"wifi", "loopback"}:
            devices.append(device)
    return devices


def _active_connection_for_device(device: str) -> dict[str, str] | None:
    out = _run_text(["nmcli", "-t", "-f", "NAME,UUID,TYPE,DEVICE", "con", "show", "--active"], check=False)
    for line in out.splitlines():
        name, uuid, conn_type, conn_device = (line.split(":", 3) + ["", "", "", ""])[:4]
        if conn_device == device:
            return {"name": name, "uuid": uuid, "type": conn_type, "device": conn_device}
    return None


def _wait_for_device_state(device: str, desired: str, *, timeout_s: float) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if _nmcli_device_state(device) == desired:
            return True
        time.sleep(0.5)
    return False


def _wait_for_link_up(device: str, *, timeout_s: float) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        out = _run_text(["ip", "-brief", "link", "show", device], check=False)
        if " UP " in f" {out} " or ("<" in out and ",UP," in out):
            return True
        time.sleep(0.5)
    return False


def _nmcli_device_state(device: str) -> str:
    out = _run_text(["nmcli", "-t", "-f", "DEVICE,STATE", "dev", "status"], check=False)
    for line in out.splitlines():
        parts = line.split(":", 1)
        if len(parts) == 2 and parts[0] == device:
            return parts[1]
    return ""


def _sudo_ip_link(device: str, state: str, *, interactive: bool = False) -> subprocess.CompletedProcess[str]:
    password = os.environ.get("LOOPMASTER_SUDO_PASSWORD")
    if password:
        cmd = ["sudo", "-S", "ip", "link", "set", "dev", device, state]
    elif interactive:
        cmd = ["sudo", "ip", "link", "set", "dev", device, state]
    else:
        cmd = ["sudo", "-n", "ip", "link", "set", "dev", device, state]
    try:
        if interactive and not password:
            return subprocess.run(cmd, text=True, check=False)
        return subprocess.run(
            cmd,
            input=f"{password}\n" if password else None,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(cmd, 127, "", str(exc))


def _run_text(cmd: list[str], *, check: bool = True) -> str:
    return _run(cmd, check=check).stdout


def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, text=True, capture_output=True, check=check)
    except FileNotFoundError as exc:
        if check:
            raise
        return subprocess.CompletedProcess(cmd, 127, "", str(exc))


def _record_network_event(
    status: dict[str, Any],
    action: str,
    device: str,
    result: subprocess.CompletedProcess[str],
) -> None:
    status.setdefault("events", []).append(
        {
            "action": action,
            "device": device,
            "returncode": int(result.returncode),
            "stderr": _sanitize_command_output(result.stderr),
        }
    )


def _sanitize_command_output(value: str | None) -> str:
    text = (value or "").strip()
    return text if len(text) <= 240 else text[:237] + "..."


def _load_points(detection_dir: Path, args: dict[str, Any]):
    import numpy as np
    from PIL import Image

    if args.get("points_path"):
        points_path = Path(args["points_path"]).expanduser().resolve()
        points = np.asarray(np.load(points_path), dtype=np.float32)
        if points.ndim != 2 or points.shape[1] != 3:
            raise ValueError(f"points_path must contain an Nx3 array, got shape {points.shape}")
        region = _load_point_region_mask(args, points.shape[0])
        return points, None, region, {
            "type": "points_path",
            "points_path": str(points_path),
            "region_mask_path": str(Path(args["region_mask_path"]).expanduser().resolve())
            if args.get("region_mask_path")
            else "",
        }

    data_dir = Path(args.get("data_dir") or detection_dir / "example_data").expanduser()
    if not data_dir.is_absolute():
        data_dir = (detection_dir / data_dir).resolve()
    color_arg = args.get("color_path")
    depth_arg = args.get("depth_path")
    seg_arg = args.get("seg_mask_path")
    color_path = Path(color_arg or data_dir / "color.png").expanduser()
    depth_path = Path(depth_arg or data_dir / "depth.png").expanduser()
    seg_path = Path(seg_arg).expanduser() if seg_arg else None
    if seg_path is None and not color_arg and not depth_arg:
        seg_path = data_dir / "seg_mask.png"
    region_path = Path(args["region_mask_path"]).expanduser() if args.get("region_mask_path") else None

    colors = np.array(Image.open(color_path), dtype=np.float32) / 255.0
    depths = np.array(Image.open(depth_path))
    seg_raw = np.array(Image.open(seg_path)) if seg_path is not None and seg_path.is_file() else None
    region_raw = _load_binary_mask_image(region_path) if region_path and region_path.is_file() else None

    camera_params = _d435_camera_params()
    fx = float(args["fx"]) if args.get("fx") is not None else camera_params["fx"]
    fy = float(args["fy"]) if args.get("fy") is not None else camera_params["fy"]
    cx = float(args["cx"]) if args.get("cx") is not None else camera_params["cx"]
    cy = float(args["cy"]) if args.get("cy") is not None else camera_params["cy"]
    scale = float(args["depth_scale"]) if args.get("depth_scale") is not None else camera_params["depth_scale"]
    depth_trunc = float(args.get("depth_trunc", 1.0))

    xmap, ymap = np.meshgrid(np.arange(depths.shape[1]), np.arange(depths.shape[0]))
    points_z = depths / scale
    points_x = (xmap - cx) / fx * points_z
    points_y = (ymap - cy) / fy * points_z
    valid = (points_z > 0) & (points_z < depth_trunc)
    points = np.stack([points_x, points_y, points_z], axis=-1)[valid].astype(np.float32)
    colors = colors[valid].astype(np.float32)
    seg_mask = seg_raw[valid] if seg_raw is not None else None
    if region_raw is not None:
        if region_raw.shape[:2] != depths.shape[:2]:
            raise ValueError(
                f"region_mask_path shape {region_raw.shape[:2]} does not match depth image shape {depths.shape[:2]}"
            )
        seg_mask = region_raw[valid].astype(bool)
    return points, colors, seg_mask, {
        "type": "rgbd",
        "data_dir": str(data_dir),
        "color_path": str(color_path),
        "depth_path": str(depth_path),
        "seg_mask_path": str(seg_path) if seg_path is not None and seg_path.is_file() else "",
        "region_mask_path": str(region_path) if region_path and region_path.is_file() else "",
        "intrinsics_source": str(DEFAULT_D435_INTRINSICS_PATH),
        "fx": fx,
        "fy": fy,
        "cx": cx,
        "cy": cy,
        "depth_scale": scale,
    }


@lru_cache(maxsize=1)
def _d435_camera_params() -> dict[str, float]:
    data = json.loads(DEFAULT_D435_INTRINSICS_PATH.read_text(encoding="utf-8"))
    color = data.get("color_intrinsics", data)
    depth_scale_m = float(data.get("depth_scale_m", 0.001))
    return {
        "fx": float(color["fx"]),
        "fy": float(color["fy"]),
        "cx": float(color.get("cx", color.get("ppx"))),
        "cy": float(color.get("cy", color.get("ppy"))),
        "depth_scale": 1.0 / depth_scale_m,
    }


def _optional_params(points: Any, seg_mask: Any, args: dict[str, Any], dense_grasp: bool) -> dict[str, Any]:
    import numpy as np

    region = None
    if seg_mask is not None and seg_mask.dtype == bool:
        region = seg_mask
    elif seg_mask is not None and args.get("region_object_id") is not None:
        region = seg_mask == int(args["region_object_id"])
    if args.get("workspace"):
        limits = [float(value) for value in args["workspace"]]
        if len(limits) != 6:
            raise ValueError("workspace must contain [xmin, xmax, ymin, ymax, zmin, zmax]")
        workspace = (
            (points[:, 0] >= limits[0])
            & (points[:, 0] <= limits[1])
            & (points[:, 1] >= limits[2])
            & (points[:, 1] <= limits[3])
            & (points[:, 2] >= limits[4])
            & (points[:, 2] <= limits[5])
        )
        region = workspace if region is None else (region & workspace)
    approach = args.get("approach_steering")
    if approach is not None:
        approach = np.asarray(approach, dtype=np.float32)
    return {
        "dense_grasp": dense_grasp,
        "collision_detection": bool(args.get("collision_detection", True)),
        "region_steering": region,
        "approach_steering": approach,
        "approach_thresh": float(args.get("approach_thresh", np.pi)),
    }


def _has_explicit_region_mask(args: dict[str, Any]) -> bool:
    return bool(args.get("seg_mask_path") or args.get("region_mask_path"))


def _should_reject_empty_explicit_region(args: dict[str, Any], region_point_count: int) -> bool:
    return _has_explicit_region_mask(args) and region_point_count <= 0


def _load_point_region_mask(args: dict[str, Any], point_count: int):
    if not args.get("region_mask_path"):
        return None
    import numpy as np

    mask_path = Path(args["region_mask_path"]).expanduser().resolve()
    region = np.asarray(np.load(mask_path)).astype(bool)
    if region.ndim != 1 or region.shape[0] != point_count:
        raise ValueError(f"region_mask_path for points_path must contain shape ({point_count},), got {region.shape}")
    return region


def _load_binary_mask_image(path: Path):
    import numpy as np
    from PIL import Image

    mask = np.array(Image.open(path))
    if mask.ndim == 3:
        mask = np.any(mask > 0, axis=2)
    return mask.astype(bool)


def _serialize_grasps(group: Any) -> list[dict[str, Any]]:
    import numpy as np

    attrs = {
        "score": _array_or_none(group, "scores"),
        "width": _array_or_none(group, "widths"),
        "height": _array_or_none(group, "heights"),
        "depth": _array_or_none(group, "depths"),
        "translation": _array_or_none(group, "translations"),
        "rotation_matrix": _array_or_none(group, "rotation_matrices"),
        "object_id": _array_or_none(group, "object_ids"),
    }
    out = []
    for index in range(int(_safe_len(group))):
        item = {"index": index}
        for key, values in attrs.items():
            if values is not None:
                item[key] = np.asarray(values[index]).tolist()
        out.append(item)
    return out


def _array_or_none(group: Any, attr: str):
    import numpy as np

    value = getattr(group, attr, None)
    if value is None:
        return None
    return np.asarray(value)


def _safe_len(value: Any) -> int:
    try:
        return len(value)
    except Exception:
        return 0


@contextlib.contextmanager
def _third_party_stdout_to_stderr():
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        yield


def _default_sdk_root() -> Path:
    return Path(__file__).resolve().parents[4] / "third_party" / "anygrasp_sdk"
