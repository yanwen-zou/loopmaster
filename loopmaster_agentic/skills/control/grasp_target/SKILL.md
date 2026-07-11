---
name: grasp_target
category: control
description: Grasp one natural-language target by chaining D435 capture, Grounded-SAM2, AnyGrasp, and right-arm grasp motion.
args:
  target_prompt: string
  item_id: integer
  quantity: integer
  execute: bool
  robot_ip: string
  top_k: integer
---

# Grasp Target

Task-level skill for the vending/web bridge path.

Inputs:
- `target_prompt`: natural-language object prompt for Grounded-SAM2, for example
  `cola can.` or `bread.`.
- `item_id`: optional web product id. When present, the result includes
  `delivered_items: [{"id": item_id, "delivered": delivered_count}]` for
  `/api/tasks/<id>/report`.
- `quantity`: number of grasp attempts/items requested. Defaults to `1`.
- `execute`: whether to physically move the right arm and gripper. Defaults to
  `true`. Set `false` to run perception and IK planning without motion.

Behavior:
1. Capture D435 RGB-D with `capture_image`.
2. Segment `target_prompt` with `grounded_sam2`.
3. Run `detect_grasps` constrained to the first segmentation mask.
4. Initialize both arms once with `init_arms` when `execute=true`.
5. Move the right arm to the pregrasp pose, open, descend, close, and lift.
6. Return `delivered_count` and `delivered_items` for the web bridge.

This skill intentionally keeps the web order bridge simple: Strategist chooses a
single task-level skill, Worker records all subskill traces, and
`server_bridge.py` reports the returned delivery count to the webpage.
