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
  license_down_device: string or list
  no_default_license_down_device: bool
  license_disconnect_device: string or list
  wifi_only_license_check: bool
  license_network_settle_s: float
---

# Detect Grasps

Runs AnyGrasp detection in the current Python environment through the SDK under
`third_party/anygrasp_sdk`. Use `check_only=true` to verify the SDK binary,
license, Python dependencies, and checkpoint path without running inference. If
dependencies are missing, the skill returns `status.missing_packages`.

Before reading AnyGrasp's feature id and checking the license, the skill
temporarily disconnects the wired interface `enx00e04c360914` when it exists,
then restores it immediately after the license check. Override this with
`license_down_device`, disable the default with
`no_default_license_down_device=true`, or request additional NetworkManager
disconnects with `license_disconnect_device` / `wifi_only_license_check`.
If sudo needs a password for `ip link set`, provide it only through the
temporary environment variable `LOOPMASTER_SUDO_PASSWORD`; the value is not
returned in skill status.

The skill accepts either `points_path` containing an `Nx3` numpy point cloud, or
RGB-D inputs through `data_dir`/`color_path`/`depth_path`. If no data path is
provided it uses AnyGrasp's bundled `grasp_detection/example_data`.

RGB-D point lifting defaults to the checked-in D435 640x480 intrinsics config:
`hei-rebot-lift/software/lerobot-hei-rebot-lift/src/lerobot/cameras/d435_intrinsics_640x480.json`.
Explicit `fx`, `fy`, `cx`, `cy`, or `depth_scale` args override those defaults.

To restrict grasps to a segmented object, pass a label image with
`seg_mask_path` and select a label with `region_object_id`. This is the direct
output contract of the `grounded_sam2` skill: use
`grounded_sam2.seg_mask_path` with `region_object_id=1` for the first detected
object. A binary `region_mask_path` is also accepted; it is interpreted as a 2D
image mask and filtered with the same valid-depth mask as the point cloud.

For object grasp attempts, prefer context-first perception instead of bare
AnyGrasp over the full scene. A typical plan is:

1. `capture_image` with `source=d435_rgbd`.
2. `grounded_sam2` on `capture_image.rgb.path` using the requested object text,
   or a broad prompt such as `object.` when the user only refers to the item in
   front of the robot.
3. `detect_grasps` with `color_path=$ref:capture_image.rgb.path`,
   `depth_path=$ref:capture_image.depth.path`,
   `seg_mask_path=$ref:grounded_sam2.seg_mask_path`, and `region_object_id=1`.

Use bare `detect_grasps` without a segmentation mask mainly for AnyGrasp
readiness checks, debugging, or when the user explicitly asks for full-scene
grasp proposals.

If `color_path` and `depth_path` are omitted and `capture_image` already ran,
the skill uses `context.memory["capture_image"]["rgb"]["path"]` and
`context.memory["capture_image"]["depth"]["path"]`.
