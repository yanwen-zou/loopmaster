---
name: object_region_index
category: perception
description: Map a Grounded-SAM object mask to one of five tray regions, indexed from right to left.
args:
  mask_path: string
  seg_mask_path: string
  region_object_id: int
  annotation: dict
  detections: list
  tray_x_min: float
  tray_x_max: float
  segment_count: int
---

# Object Region Index

Use this when the robot needs a coarse left/right ordering for a Grounded-SAM
object mask on the white tray in front of it.

The default calibration is based on
`_runs/拍一张你当前看到的图片-20260711-140306-1f82c5/artifacts/current_view_rgb.png`:
the white tray's useful width spans approximately `x=106` to `x=604` in a
640-pixel-wide RGB image. The skill divides that tray width into five equal
horizontal regions.

Index order is right-to-left:

- `0`: rightmost object region
- `1`: second from right
- `2`: center object region
- `3`: second from left
- `4`: leftmost object region

Anything to the left of `tray_x_min` is ignored because it is outside the white
tray. The skill counts the mask pixels overlapping each of the five width
regions, computes `ratio = region_mask_pixels / tray_mask_pixels`, and returns
the top-overlap region as `index`.

Preferred inputs:

- `mask_path`: a single binary Grounded-SAM mask such as `mask_001.png`
- `annotation`: one Grounded-SAM annotation dict containing `mask_path`
- `seg_mask_path` + `region_object_id`: a combined label mask plus object id
- `detections`: a batch of dicts using any of the above forms

The result includes `index`, `top_overlap`, all per-region `overlaps`,
`mask_area_pixels`, and `tray_mask_area_pixels`. `x` and `bbox` are still
accepted as a fallback, but mask overlap is the intended behavior.
