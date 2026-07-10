---
name: grounded_sam2
category: base/perception
description: Segment objects in an RGB image with Grounded-SAM2 and save AnyGrasp-compatible masks.
args:
  check_only: bool
  repo_root: string
  grounding_model: string
  text_prompt: string
  img_path: string
  output_dir: string
  sam2_checkpoint: string
  sam2_model_config: string
  box_threshold: float
  text_threshold: float
  force_cpu: bool
---

# Grounded SAM2

Runs the Grounded-SAM2 image demo from `third_party/Grounded-SAM-2` as a base
perception skill. It detects objects named by `text_prompt`, predicts SAM2 masks,
and writes:

- `seg_mask.png`: a single-channel label image where label `1` is the first
  detected mask, label `2` is the second, and so on.
- `mask_###.png`: one binary mask per detection.
- annotated visualization images.
- `grounded_sam2_results.json`: labels, boxes, scores, and mask paths.

The returned `seg_mask_path` can be passed to `detect_grasps` with
`region_object_id=1` to restrict AnyGrasp to the first detected object. The skill
also stores the latest result in `context.memory["grounded_sam2"]`.
