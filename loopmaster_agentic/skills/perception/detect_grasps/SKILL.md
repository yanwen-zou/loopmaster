# detect_grasps

Run local AnyGrasp grasp detection on RGB-D inputs and optionally constrain detection to a segmentation mask.

Inputs:
- `color_path`: RGB image path. Prefer the absolute `rgb.path` returned by `capture_image`. Relative paths must be resolved against the current LoopMaster run/workspace, not the third-party detector directory or process cwd.
- `depth_path`: depth image path. Prefer the absolute `depth.path` returned by `capture_image`. Relative paths must be resolved against the current LoopMaster run/workspace.
- `seg_mask_path`: optional segmentation mask path. Prefer the absolute `seg_mask_path` returned by `grounded_sam2`.
- `region_object_id`: optional integer label id in the segmentation mask.
- `top_k`: maximum grasp candidates to return.
- `dense_grasp`: optional boolean.
- `collision_detection`: optional boolean.
- `wifi_only_license_check`: optional boolean for AnyGrasp license/network handling.
- `license_network_settle_s`: optional settle time after license network handling.

Behavior:
1. Resolve `color_path`, `depth_path`, and `seg_mask_path` to existing absolute paths before loading images or changing directories.
2. Use the RGB-D image dimensions and depth scale returned by capture metadata when available.
3. If a segmentation mask is provided, constrain grasp detection to `region_object_id`.
4. If a segmentation mask is provided but selects zero valid depth points, return
   `ok=false` instead of falling back to unconstrained grasps.
5. Return `ok=true` with a ranked `grasps` list and the exact fields available for downstream motion, including each grasp's `translation` and `rotation_matrix` when present.
6. Return `ok=false` with a clear path/status error if any input file is missing.

For plans, pass absolute paths from prior skill results whenever available. Do not pass stale or workspace-relative artifact strings when capture_image has returned absolute artifact paths.
