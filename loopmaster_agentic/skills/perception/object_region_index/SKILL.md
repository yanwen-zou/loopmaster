---
name: object_region_index
category: perception
description: Map a Grounded-SAM object mask to a cached trajectory episode index clipped to [0, 4].
args:
  mask_path: string
  seg_mask_path: string
  region_object_id: int
  annotation: dict
  detections: list
  fallback_index: int
  tray_x_min: float
  tray_x_max: float
  segment_count: int
---

# object_region_index

Map a segmented target mask to a cached trajectory region index on the tray.

## Purpose

Use this skill after a perception skill has produced a segmentation mask for the specific target object. The returned `index`/`episode` is intended to be passed to `play_cache_traj.episode`, and is always clipped to the cached trajectory range `[0, 4]`.

## Arguments

- `seg_mask_path` (string, required): Path to a segmentation mask image. This may be a label-encoded mask when paired with `region_object_id`, or a single-object binary mask when supported by the runtime.
- `region_object_id` (integer, required for label-encoded masks): Object label id to extract from `seg_mask_path`.
- `tray_x_min` (number, required): Left image x boundary of the tray region.
- `tray_x_max` (number, required): Right image x boundary of the tray region.
- `segment_count` (integer, required): Number of tray segments. For cached trajectories this is normally 5.
- `fallback_index` (integer, optional): Episode to use when the mask cannot determine a region. When omitted, a random episode in `[0, 4]` is selected. Explicit values are clipped to `[0, 4]`.

## Result Contract

The result always includes a motion-ready cached trajectory index:

- `ok == true`
- `index` is an integer from `0` to `4`
- `episode` equals `index`
- `fallback_used` is `false` when mask evidence selected the region
- `fallback_used` is `true` when the skill could not determine a region and used `fallback_index`

When mask evidence is available, the result also includes `top_overlap`, all per-region `overlaps`, `mask_area_pixels`, and `tray_mask_area_pixels`.

If the mask is empty, the selected `region_object_id` is absent, the target has no pixels inside the tray bounds, no mask/x input is provided, or the skill cannot determine a unique segment, it returns `fallback_used: true` and still returns a clipped `index`/`episode`. Without an explicit `fallback_index`, that fallback is random in `[0, 4]`. This lets the web bridge "grab any one" instead of blocking the order.

## Required Planner Behavior

- After calling this skill, pass `episode` or `index` directly to `play_cache_traj.episode`.
- If `fallback_used` is true, treat the selected episode as arbitrary rather than perception-confirmed.
- Prefer a per-annotation `mask_path` for the intended object when the combined `seg_mask_path` does not contain the expected `region_object_id` label.

## Region Convention

Tray regions are numbered from right to left in image coordinates when using the standard cached trajectory setup:

- `0`: rightmost tray segment
- `segment_count - 1`: leftmost tray segment

The returned `index`/`episode` corresponds directly to `play_cache_traj.episode` for the cached pick/delivery trajectories.
