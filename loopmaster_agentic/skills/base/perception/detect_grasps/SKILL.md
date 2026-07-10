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
  region_mask_path: string
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

To restrict grasps to a segmented object, pass a label image with
`seg_mask_path` and select a label with `region_object_id`. This is the direct
output contract of the `grounded_sam2` skill: use
`grounded_sam2.seg_mask_path` with `region_object_id=1` for the first detected
object. A binary `region_mask_path` is also accepted; it is interpreted as a 2D
image mask and filtered with the same valid-depth mask as the point cloud.

If `color_path` and `depth_path` are omitted and `capture_image` already ran,
the skill uses `context.memory["capture_image"]["rgb"]["path"]` and
`context.memory["capture_image"]["depth"]["path"]`.
