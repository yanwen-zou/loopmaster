---
name: detect_grasps
category: base/perception
description: Run AnyGrasp grasp detection on an RGB-D frame or point cloud.
args:
  check_only: bool
  sdk_root: string
  license_dir: string
  license_zip: string
  checkpoint_path: string
  data_dir: string
  points_path: string
  color_path: string
  depth_path: string
  seg_mask_path: string
  top_k: int
  dense_grasp: bool
  collision_detection: bool
  region_object_id: int
  approach_steering: list
  approach_thresh: float
---

# Detect Grasps

Runs AnyGrasp detection in the current Python environment through the SDK under
`third_party/anygrasp_sdk`. Use `check_only=true` to verify the SDK binary,
license, Python dependencies, and checkpoint path without running inference. If
dependencies are missing, the skill returns `status.missing_packages`.

The skill accepts either `points_path` containing an `Nx3` numpy point cloud, or
RGB-D inputs through `data_dir`/`color_path`/`depth_path`. If no data path is
provided it uses AnyGrasp's bundled `grasp_detection/example_data`.
