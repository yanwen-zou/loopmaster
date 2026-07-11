from __future__ import annotations

from pathlib import Path
from typing import Any


DEFAULT_TRAY_X_MIN = 106.0
DEFAULT_TRAY_X_MAX = 604.0
DEFAULT_SEGMENT_COUNT = 5


def dispatch(context, args):
    args = dict(args or {})
    tray_x_min = float(args.get("tray_x_min", DEFAULT_TRAY_X_MIN))
    tray_x_max = float(args.get("tray_x_max", DEFAULT_TRAY_X_MAX))
    segment_count = int(args.get("segment_count", DEFAULT_SEGMENT_COUNT))
    if segment_count <= 0:
        return {"ok": False, "error": "segment_count must be positive"}
    if tray_x_max <= tray_x_min:
        return {"ok": False, "error": "tray_x_max must be greater than tray_x_min"}

    ranges = _build_ranges(tray_x_min, tray_x_max, segment_count)

    if _has_mask_input(args):
        try:
            mask = _mask_from_args(context, args)
        except Exception as exc:
            return {"ok": False, "error": f"failed to load mask: {type(exc).__name__}: {exc}", "ranges": ranges}
        result = {
            "ok": True,
            "ranges": ranges,
            **_classify_mask(mask, tray_x_min, tray_x_max, segment_count),
        }
        _remember(context, result)
        return result

    detections = args.get("detections")
    if detections is not None:
        if not isinstance(detections, list):
            return {"ok": False, "error": "detections must be a list"}
        results = []
        for detection in detections:
            if _has_mask_input(detection):
                try:
                    mask = _mask_from_args(context, detection)
                    results.append({"ok": True, **_classify_mask(mask, tray_x_min, tray_x_max, segment_count)})
                except Exception as exc:
                    results.append({"ok": False, "error": f"failed to load mask: {type(exc).__name__}: {exc}", "input": detection})
                continue
            try:
                x = _x_from_value(detection)
            except ValueError as exc:
                results.append({"ok": False, "error": str(exc), "input": detection})
                continue
            results.append({"ok": True, **_classify_x(x, tray_x_min, tray_x_max, segment_count)})
        output = {"ok": True, "ranges": ranges, "results": results}
        _remember(context, output)
        return output

    try:
        x = _x_from_args(args)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "ranges": ranges}

    result = {"ok": True, "ranges": ranges, **_classify_x(x, tray_x_min, tray_x_max, segment_count)}
    _remember(context, result)
    return result


def _has_mask_input(args: Any) -> bool:
    if not isinstance(args, dict):
        return False
    if args.get("mask_path") or args.get("seg_mask_path") or args.get("region_mask_path"):
        return True
    annotation = args.get("annotation")
    return isinstance(annotation, dict) and bool(annotation.get("mask_path"))


def _mask_from_args(context: Any, args: dict[str, Any]) -> Any:
    import numpy as np
    from PIL import Image

    annotation = args.get("annotation")
    if isinstance(annotation, dict) and annotation.get("mask_path") and not args.get("mask_path"):
        args = {**args, "mask_path": annotation["mask_path"]}

    mask_path = args.get("mask_path") or args.get("region_mask_path")
    if mask_path:
        image = np.array(Image.open(_resolve_path(context, mask_path)))
        return image > 0

    seg_mask_path = args.get("seg_mask_path")
    if not seg_mask_path:
        latest = getattr(context, "memory", {}).get("grounded_sam2") if hasattr(context, "memory") else None
        if isinstance(latest, dict):
            seg_mask_path = latest.get("seg_mask_path")
            args.setdefault("region_object_id", latest.get("anygrasp_hint", {}).get("region_object_id"))
    if not seg_mask_path:
        raise ValueError("provide mask_path, annotation.mask_path, or seg_mask_path")

    region_object_id = args.get("region_object_id", args.get("label_id"))
    if region_object_id is None:
        raise ValueError("seg_mask_path requires region_object_id")
    image = np.array(Image.open(_resolve_path(context, seg_mask_path)))
    return image == int(region_object_id)


def _resolve_path(context: Any, value: Any) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()
    workspace = getattr(context, "workspace", None)
    root = getattr(workspace, "root", None)
    if root is not None:
        return (Path(root).expanduser().resolve() / path).resolve()
    return path.resolve()


def _x_from_args(args: dict[str, Any]) -> float:
    if "x" in args:
        return float(args["x"])
    for key in ("bbox", "box", "input_box"):
        if key in args:
            return _bbox_center_x(args[key])
    annotation = args.get("annotation")
    if isinstance(annotation, dict):
        return _x_from_value(annotation)
    raise ValueError("provide x or bbox")


def _x_from_value(value: Any) -> float:
    if isinstance(value, dict):
        if "x" in value:
            return float(value["x"])
        for key in ("bbox", "box", "input_box"):
            if key in value:
                return _bbox_center_x(value[key])
    if isinstance(value, (list, tuple)) and len(value) >= 4:
        return _bbox_center_x(value)
    raise ValueError("detection must contain x or bbox")


def _bbox_center_x(value: Any) -> float:
    if not isinstance(value, (list, tuple)) or len(value) < 4:
        raise ValueError("bbox must be [x1, y1, x2, y2]")
    return (float(value[0]) + float(value[2])) / 2.0


def _classify_mask(mask: Any, tray_x_min: float, tray_x_max: float, segment_count: int) -> dict[str, Any]:
    import numpy as np

    mask = np.asarray(mask).astype(bool)
    if mask.ndim == 3:
        mask = mask.any(axis=2)
    if mask.ndim != 2:
        raise ValueError("mask must be a 2D image")

    mask_area = int(mask.sum())
    if mask_area == 0:
        return {
            "ignored": True,
            "index": None,
            "reason": "empty mask",
            "mask_area_pixels": 0,
            "tray_mask_area_pixels": 0,
            "overlaps": _empty_overlaps(segment_count),
        }

    _ys, xs = np.nonzero(mask)
    in_tray = (xs >= tray_x_min) & (xs <= tray_x_max)
    tray_xs = xs[in_tray]
    tray_mask_area = int(tray_xs.size)
    if tray_mask_area == 0:
        return {
            "ignored": True,
            "index": None,
            "reason": "mask has no pixels in white tray range",
            "mask_area_pixels": mask_area,
            "tray_mask_area_pixels": 0,
            "overlaps": _empty_overlaps(segment_count),
        }

    segment_width = (tray_x_max - tray_x_min) / float(segment_count)
    left_to_right_slots = ((tray_xs - tray_x_min) / segment_width).astype(int)
    left_to_right_slots = np.clip(left_to_right_slots, 0, segment_count - 1)
    counts_left_to_right = np.bincount(left_to_right_slots, minlength=segment_count)

    overlaps = []
    for slot, count in enumerate(counts_left_to_right.tolist()):
        overlaps.append(
            {
                "index": segment_count - 1 - slot,
                "pixels": int(count),
                "ratio": float(count / tray_mask_area),
            }
        )
    overlaps.sort(key=lambda item: int(item["index"]))
    top = max(overlaps, key=lambda item: (float(item["ratio"]), int(item["pixels"])))
    return {
        "ignored": False,
        "index": int(top["index"]),
        "top_overlap": top,
        "overlaps": overlaps,
        "mask_area_pixels": mask_area,
        "tray_mask_area_pixels": tray_mask_area,
        "ignored_left_pixels": int((xs < tray_x_min).sum()),
        "ignored_right_pixels": int((xs > tray_x_max).sum()),
    }


def _empty_overlaps(segment_count: int) -> list[dict[str, float | int]]:
    return [{"index": index, "pixels": 0, "ratio": 0.0} for index in range(segment_count)]


def _classify_x(x: float, tray_x_min: float, tray_x_max: float, segment_count: int) -> dict[str, Any]:
    if x < tray_x_min:
        return {
            "ignored": True,
            "index": None,
            "x": x,
            "reason": "left of white tray",
        }
    if x > tray_x_max:
        return {
            "ignored": True,
            "index": None,
            "x": x,
            "reason": "right of white tray",
        }

    segment_width = (tray_x_max - tray_x_min) / float(segment_count)
    left_to_right_slot = min(segment_count - 1, int((x - tray_x_min) / segment_width))
    index = segment_count - 1 - left_to_right_slot
    return {
        "ignored": False,
        "index": index,
        "x": x,
        "slot_left_to_right": left_to_right_slot,
    }


def _build_ranges(tray_x_min: float, tray_x_max: float, segment_count: int) -> list[dict[str, float | int]]:
    segment_width = (tray_x_max - tray_x_min) / float(segment_count)
    ranges = []
    for slot in range(segment_count):
        left = tray_x_min + slot * segment_width
        right = tray_x_min + (slot + 1) * segment_width
        ranges.append(
            {
                "index": segment_count - 1 - slot,
                "x_min": round(left, 3),
                "x_max": round(right, 3),
            }
        )
    return sorted(ranges, key=lambda item: int(item["index"]))


def _remember(context, result: dict[str, Any]) -> None:
    memory = getattr(context, "memory", None)
    if isinstance(memory, dict):
        memory["object_region_index"] = result
