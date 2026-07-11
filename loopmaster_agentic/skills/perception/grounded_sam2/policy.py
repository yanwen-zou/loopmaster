from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


REQUIRED_MODULES = (
    "cv2",
    "numpy",
    "PIL",
    "torch",
    "supervision",
    "transformers",
    "pycocotools",
)


def dispatch(context, args):
    args = _merge_capture_image_memory(context, dict(args or {}))
    if "_workspace_root" not in args:
        workspace = getattr(context, "workspace", None)
        root = getattr(workspace, "root", None)
        if root is not None:
            args["_workspace_root"] = str(root)
    repo_root = Path(args.get("repo_root") or _default_repo_root()).expanduser().resolve()
    if not repo_root.is_dir():
        return {"ok": False, "error": f"Grounded-SAM2 repo not found: {repo_root}"}

    status = {
        "repo_root": str(repo_root),
        "python": sys.executable,
        "dependencies": {},
    }
    dependency_ok = _check_dependencies(status)
    if args.get("check_only"):
        missing = _missing_dependencies(status)
        return {"ok": dependency_ok and not missing, "status": status, "missing": missing}
    if not dependency_ok:
        return {"ok": False, "error": "Grounded-SAM2 dependencies are not ready", "status": status}

    try:
        result = _run_grounded_sam2(repo_root, args)
    except Exception as exc:
        return {"ok": False, "error": f"Grounded-SAM2 inference failed: {type(exc).__name__}: {exc}", "status": status}

    context.memory["grounded_sam2"] = result
    return {"ok": True, "status": status, **result}


def _merge_capture_image_memory(context: Any, args: dict[str, Any]) -> dict[str, Any]:
    if args.get("img_path"):
        return args
    latest = getattr(context, "memory", {}).get("capture_image") if hasattr(context, "memory") else None
    if not isinstance(latest, dict):
        return args
    rgb = latest.get("rgb")
    if isinstance(rgb, dict) and rgb.get("path"):
        args["img_path"] = rgb["path"]
    return args


def _run_grounded_sam2(repo_root: Path, args: dict[str, Any]) -> dict[str, Any]:
    import cv2
    import numpy as np
    import torch
    import supervision as sv
    from PIL import Image
    from supervision.draw.color import ColorPalette
    from transformers import AutoModelForZeroShotObjectDetection, AutoProcessor

    sys.path.insert(0, str(repo_root))
    from sam2.build_sam import build_sam2
    from sam2.sam2_image_predictor import SAM2ImagePredictor
    from utils.supervision_utils import CUSTOM_COLOR_MAP

    workspace_root = _workspace_root(args)
    img_path = _resolve_workspace_path(
        args.get("img_path") or repo_root / "notebooks" / "images" / "truck.jpg",
        workspace_root=workspace_root,
        repo_root=repo_root,
    )
    if not img_path.is_file():
        raise FileNotFoundError(f"image not found: {img_path}")

    output_dir = _resolve_workspace_path(
        args.get("output_dir") or "grounded_sam2",
        workspace_root=workspace_root,
        repo_root=repo_root,
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    text_prompt = _normalize_prompt(str(args.get("text_prompt") or "car. tire."))
    grounding_model_id = str(args.get("grounding_model") or "IDEA-Research/grounding-dino-tiny")
    local_files_only = bool(args.get("local_files_only", True))
    checkpoint = Path(args.get("sam2_checkpoint") or repo_root / "checkpoints" / "sam2.1_hiera_large.pt").expanduser()
    if not checkpoint.is_absolute():
        checkpoint = (repo_root / checkpoint).resolve()
    model_config = str(args.get("sam2_model_config") or "configs/sam2.1/sam2.1_hiera_l.yaml")
    device = "cuda" if torch.cuda.is_available() and not args.get("force_cpu") else "cpu"

    image = Image.open(img_path).convert("RGB")
    image_np = np.array(image)

    with torch.autocast(device_type=device, dtype=torch.bfloat16):
        if device == "cuda" and torch.cuda.get_device_properties(0).major >= 8:
            torch.backends.cuda.matmul.allow_tf32 = True
            torch.backends.cudnn.allow_tf32 = True

        sam2_model = build_sam2(model_config, str(checkpoint), device=device)
        predictor = SAM2ImagePredictor(sam2_model)
        predictor.set_image(image_np)

        processor = AutoProcessor.from_pretrained(grounding_model_id, local_files_only=local_files_only)
        grounding_model = AutoModelForZeroShotObjectDetection.from_pretrained(
            grounding_model_id,
            local_files_only=local_files_only,
        ).to(device)
        inputs = processor(images=image, text=text_prompt, return_tensors="pt").to(device)
        with torch.no_grad():
            outputs = grounding_model(**inputs)

        detections = processor.post_process_grounded_object_detection(
            outputs,
            inputs.input_ids,
            threshold=float(args.get("box_threshold", 0.4)),
            text_threshold=float(args.get("text_threshold", 0.3)),
            target_sizes=[image.size[::-1]],
        )[0]

        input_boxes = detections["boxes"].detach().cpu().numpy()
        if input_boxes.size == 0:
            masks = np.zeros((0, image.height, image.width), dtype=bool)
            sam_scores: list[float] = []
        else:
            masks, sam_scores, _logits = predictor.predict(
                point_coords=None,
                point_labels=None,
                box=input_boxes,
                multimask_output=False,
            )
            if masks.ndim == 4:
                masks = masks.squeeze(1)
            masks = masks.astype(bool)
            sam_scores = np.asarray(sam_scores).reshape(-1).tolist()

    class_names = [str(label) for label in detections["labels"]]
    confidences = detections["scores"].detach().cpu().numpy().tolist()
    count = min(len(class_names), len(confidences), int(input_boxes.shape[0]), int(masks.shape[0]))
    class_names = class_names[:count]
    confidences = confidences[:count]
    input_boxes = input_boxes[:count]
    masks = masks[:count]
    sam_scores = sam_scores[:count]
    seg_mask = _write_masks(output_dir, masks)
    annotations = _annotations(output_dir, class_names, confidences, input_boxes, sam_scores, masks)
    _write_visualizations(output_dir, img_path, input_boxes, masks, class_names, confidences)

    result = {
        "image_path": str(img_path),
        "text_prompt": text_prompt,
        "output_dir": str(output_dir),
        "seg_mask_path": str(output_dir / "seg_mask.png"),
        "annotation_count": len(annotations),
        "annotations": annotations,
        "anygrasp_hint": {
            "seg_mask_path": str(output_dir / "seg_mask.png"),
            "region_object_id": 1 if annotations else None,
        },
    }
    with open(output_dir / "grounded_sam2_results.json", "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    return result


def _workspace_root(args: dict[str, Any]) -> Path | None:
    value = args.get("_workspace_root")
    if not value:
        return None
    return Path(value).expanduser().resolve()


def _resolve_workspace_path(value: Any, *, workspace_root: Path | None, repo_root: Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()
    if workspace_root is not None:
        return (workspace_root / path).resolve()
    return (repo_root / path).resolve()


def _write_masks(output_dir: Path, masks: Any) -> Any:
    import cv2
    import numpy as np

    if masks.shape[0] == 0:
        seg_mask = np.zeros(masks.shape[1:], dtype=np.uint16)
    else:
        seg_mask = np.zeros(masks.shape[1:], dtype=np.uint16)
        for index, mask in enumerate(masks, start=1):
            binary = mask.astype(np.uint8) * 255
            cv2.imwrite(str(output_dir / f"mask_{index:03d}.png"), binary)
            seg_mask[mask] = index
    cv2.imwrite(str(output_dir / "seg_mask.png"), seg_mask)
    return seg_mask


def _annotations(
    output_dir: Path,
    class_names: list[str],
    confidences: list[float],
    boxes: Any,
    sam_scores: list[float],
    masks: Any,
) -> list[dict[str, Any]]:
    annotations = []
    for index, (class_name, confidence, box, mask) in enumerate(zip(class_names, confidences, boxes, masks), start=1):
        annotations.append(
            {
                "label_id": index,
                "class_name": class_name,
                "grounding_score": float(confidence),
                "sam_score": float(sam_scores[index - 1]) if index - 1 < len(sam_scores) else None,
                "bbox_xyxy": [float(v) for v in box.tolist()],
                "mask_path": str(output_dir / f"mask_{index:03d}.png"),
                "area_pixels": int(mask.sum()),
            }
        )
    return annotations


def _write_visualizations(
    output_dir: Path,
    img_path: Path,
    boxes: Any,
    masks: Any,
    class_names: list[str],
    confidences: list[float],
) -> None:
    import cv2
    import numpy as np
    import supervision as sv
    from supervision.draw.color import ColorPalette
    from utils.supervision_utils import CUSTOM_COLOR_MAP

    img = cv2.imread(str(img_path))
    count = min(len(class_names), len(confidences), int(boxes.shape[0]), int(masks.shape[0]))
    if count == 0:
        cv2.imwrite(str(output_dir / "groundingdino_annotated_image.jpg"), img)
        cv2.imwrite(str(output_dir / "grounded_sam2_annotated_image_with_mask.jpg"), img)
        return
    boxes = boxes[:count]
    masks = masks[:count]
    class_names = class_names[:count]
    confidences = confidences[:count]
    class_ids = np.arange(count, dtype=int)
    detections = sv.Detections(xyxy=boxes, mask=masks.astype(bool), class_id=class_ids)
    labels = [f"{name} {score:.2f}" for name, score in zip(class_names, confidences)]
    palette = ColorPalette.from_hex(CUSTOM_COLOR_MAP)

    box_annotator = sv.BoxAnnotator(color=palette)
    annotated = box_annotator.annotate(scene=img.copy(), detections=detections)
    label_annotator = sv.LabelAnnotator(color=palette)
    annotated = label_annotator.annotate(scene=annotated, detections=detections, labels=labels)
    cv2.imwrite(str(output_dir / "groundingdino_annotated_image.jpg"), annotated)

    mask_annotator = sv.MaskAnnotator(color=palette)
    annotated = mask_annotator.annotate(scene=annotated, detections=detections)
    cv2.imwrite(str(output_dir / "grounded_sam2_annotated_image_with_mask.jpg"), annotated)


def _normalize_prompt(text: str) -> str:
    parts = [part.strip().lower() for part in text.replace("\n", " ").split(".") if part.strip()]
    return " ".join(f"{part}." for part in parts)


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


def _missing_dependencies(status: dict[str, Any]) -> list[str]:
    return [name for name, data in status["dependencies"].items() if not data.get("ok")]


def _default_repo_root() -> Path:
    return Path(__file__).resolve().parents[4] / "third_party" / "Grounded-SAM-2"
