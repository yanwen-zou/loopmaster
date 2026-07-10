from __future__ import annotations

import contextlib
import io
import os
import shutil
import sys
import sysconfig
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any


REQUIRED_MODULES = ("numpy", "PIL", "torch", "open3d", "MinkowskiEngine", "graspnetAPI", "pointnet2")


def dispatch(context, args):
    args = _merge_grounded_sam2_memory(context, dict(args or {}))
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
    }

    dependency_ok = _check_dependencies(status)
    gsnet = _import_gsnet(detection_dir, status)
    if gsnet is not None:
        status["feature_id"] = _safe_feature_id(gsnet)
        status["license_ok"] = _safe_check_license(gsnet, license_dir)

    missing = _missing_requirements(status, dependency_ok)
    if args.get("check_only"):
        return {"ok": not missing, "status": status, "missing": missing}
    if missing:
        return {"ok": False, "error": "AnyGrasp is not ready", "status": status, "missing": missing}
    if gsnet is None:
        return {"ok": False, "error": "gsnet import failed", "status": status}

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
    color_path = Path(args.get("color_path") or data_dir / "color.png").expanduser()
    depth_path = Path(args.get("depth_path") or data_dir / "depth.png").expanduser()
    seg_path = Path(args.get("seg_mask_path") or data_dir / "seg_mask.png").expanduser()
    region_path = Path(args["region_mask_path"]).expanduser() if args.get("region_mask_path") else None

    colors = np.array(Image.open(color_path), dtype=np.float32) / 255.0
    depths = np.array(Image.open(depth_path))
    seg_raw = np.array(Image.open(seg_path)) if seg_path.is_file() else None
    region_raw = _load_binary_mask_image(region_path) if region_path and region_path.is_file() else None

    fx = float(args.get("fx", 927.17))
    fy = float(args.get("fy", 927.37))
    cx = float(args.get("cx", 651.32))
    cy = float(args.get("cy", 349.62))
    scale = float(args.get("depth_scale", 1000.0))
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
        "seg_mask_path": str(seg_path) if seg_path.is_file() else "",
        "region_mask_path": str(region_path) if region_path and region_path.is_file() else "",
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
    return Path(__file__).resolve().parents[5] / "third_party" / "anygrasp_sdk"
