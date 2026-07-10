#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import rerun as rr


REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_EXTRINSICS = REPO_ROOT / "loopmaster_agentic" / "config" / "head_camera_extrinsics.json"


def main() -> int:
    _require_rerun_sdk()
    parser = argparse.ArgumentParser(
        description="Visualize HEI ReBot Lift camera, arm bases, and optional grasp pose in the left-arm frame."
    )
    parser.add_argument("--extrinsics", type=Path, default=DEFAULT_EXTRINSICS)
    parser.add_argument("--axis-length", type=float, default=0.12, help="Frame axis length in meters.")
    parser.add_argument("--save", type=Path, default=None, help="Save an .rrd file instead of only spawning viewer.")
    parser.add_argument("--spawn", action="store_true", help="Open the Rerun viewer after logging.")
    args = parser.parse_args()

    extrinsics = _load_extrinsics(args.extrinsics)
    rr.init("loopmaster_arm_camera_frames")
    rr.log("/", rr.ViewCoordinates.RIGHT_HAND_Z_UP, static=True)

    t_left_cam = _arm_to_camera_transform(extrinsics, "left")

    _log_frame(
        "world/left_arm_base",
        np.eye(4),
        axis_length=args.axis_length,
        label="left_arm_base / world",
    )

    _log_frame(
        "world/head_camera",
        t_left_cam,
        axis_length=args.axis_length,
        label="head_camera",
    )

    t_right_cam = _arm_to_camera_transform(extrinsics, "right")
    t_left_right = t_left_cam @ np.linalg.inv(t_right_cam)
    _log_frame(
        "world/right_arm_base",
        t_left_right,
        axis_length=args.axis_length,
        label="right_arm_base",
    )

    grasp = extrinsics.get("objects", {}).get("grasp_pose")
    if isinstance(grasp, dict) and grasp.get("camera_to_object") is not None:
        t_cam_obj = _matrix(grasp["camera_to_object"])
        t_left_obj = t_left_cam @ t_cam_obj
        _log_frame(
            "world/grasp_pose",
            t_left_obj,
            axis_length=args.axis_length,
            label="grasp_pose",
        )

    rr.log(
            "world/metadata",
        rr.TextDocument(
            _format_matrix_report(t_left_cam, t_right_cam, t_left_right, grasp),
            media_type="text/markdown",
        ),
        static=True,
    )

    if args.save is not None:
        rr.save(args.save)
        print(f"saved {args.save}")
    if args.spawn or args.save is None:
        rr.spawn()
        print("spawned Rerun viewer")
    return 0


def _require_rerun_sdk() -> None:
    if hasattr(rr, "init") and hasattr(rr, "Transform3D"):
        return
    module_path = getattr(rr, "__file__", "<unknown>")
    raise RuntimeError(
        "This script requires the Rerun robotics SDK package `rerun-sdk`, "
        f"but Python imported a different `rerun` module from {module_path}. "
        "Run `uv pip uninstall rerun && uv pip install rerun-sdk>=0.26.0,<0.27.0`."
    )


def _load_extrinsics(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    for side in ("left", "right"):
        _arm_to_camera_transform(data, side)
    grasp = data.get("objects", {}).get("grasp_pose")
    if isinstance(grasp, dict) and grasp.get("camera_to_object") is not None:
        _matrix(grasp["camera_to_object"])
    return data


def _arm_to_camera_transform(extrinsics: dict[str, Any], side: str) -> np.ndarray:
    transforms = extrinsics["transforms"][side]
    if "arm_to_camera" in transforms:
        return _matrix(transforms["arm_to_camera"])
    if "camera_to_arm" in transforms:
        return _matrix(transforms["camera_to_arm"])
    raise KeyError(f"missing arm_to_camera transform for side={side}")


def _matrix(raw: Any) -> np.ndarray:
    mat = np.asarray(raw, dtype=float)
    if mat.shape != (4, 4):
        raise ValueError(f"expected 4x4 matrix, got shape={mat.shape}")
    if not np.allclose(mat[3], [0.0, 0.0, 0.0, 1.0]):
        raise ValueError(f"expected homogeneous matrix last row [0, 0, 0, 1], got {mat[3].tolist()}")
    return mat


def _log_frame(entity: str, t_world_frame: np.ndarray, *, axis_length: float, label: str) -> None:
    origin = t_world_frame[:3, 3]
    rotation = t_world_frame[:3, :3]
    local_origin = np.zeros(3)
    local_axes = np.eye(3) * axis_length
    rr.log(
        entity,
        rr.Transform3D(
            translation=origin,
            mat3x3=rotation,
            relation=rr.TransformRelation.ParentFromChild,
            axis_length=axis_length,
        ),
        static=True,
    )
    rr.log(
        f"{entity}/origin",
        rr.Points3D([local_origin], radii=[0.012], colors=[[255, 255, 255]], show_labels=False),
        static=True,
    )
    rr.log(
        f"{entity}/axes",
        rr.Arrows3D(
            origins=[local_origin, local_origin, local_origin],
            vectors=local_axes,
            colors=[[255, 60, 60], [60, 220, 80], [80, 140, 255]],
            show_labels=False,
        ),
        static=True,
    )


def _format_matrix_report(
    t_left_cam: np.ndarray,
    t_right_cam: np.ndarray,
    t_left_right: np.ndarray,
    grasp: Any,
) -> str:
    lines = [
        "# left-arm-base visualization",
        "",
        "`T_left_cam` from config, visualized as head_camera:",
        "",
        "```text",
        _format_matrix(t_left_cam),
        "```",
        "",
        "`T_right_cam` from config:",
        "",
        "```text",
        _format_matrix(t_right_cam),
        "```",
        "",
        "`T_left_right = T_left_cam @ inv(T_right_cam)` visualized as right_arm_base:",
        "",
        "```text",
        _format_matrix(t_left_right),
        "```",
    ]
    if isinstance(grasp, dict) and grasp.get("camera_to_object") is not None:
        t_cam_obj = _matrix(grasp["camera_to_object"])
        lines.extend(
            [
                "",
                "`T_cam_obj` from config:",
                "",
                "```text",
                _format_matrix(t_cam_obj),
                "```",
                "",
                "`T_left_obj = T_left_cam @ T_cam_obj` visualized as grasp_pose:",
                "",
                "```text",
                _format_matrix(t_left_cam @ t_cam_obj),
                "```",
            ]
        )
    return "\n".join(lines)


def _format_matrix(mat: np.ndarray) -> str:
    return "\n".join("[" + ", ".join(f"{value: .6f}" for value in row) + "]" for row in mat)


if __name__ == "__main__":
    raise SystemExit(main())
