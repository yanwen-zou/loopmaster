---
name: move_arm_joints
category: base/control
description: Command one or both HEI ReBot Lift arms using joint positions.
args:
  side: string
  positions: object
---

# Move Arm Joints

Commands `left`, `right`, or `both` arm joint targets. Positions may be a list
of seven values or a dictionary keyed by `joint_1` through `joint_6` and
`gripper`. For `side=both`, a shared list/dict is applied to both arms; pass
`{"right": {...}, "left": {...}}` to use different targets per arm.

After connecting to the physical arms and before any grasp or end-effector
motion, initialize both arms from
`loopmaster_agentic/config/arm_init_pose.json`. Load the JSON, read its
`positions` object, normalize optional `left_`/`right_` prefixes and `.pos`
suffixes, then call this skill as:

```json
{
  "side": "both",
  "positions": {
    "joint_1": -0.1356145590543747,
    "joint_2": -1.3578622341156006,
    "joint_3": -0.91,
    "joint_4": 1.2438009977340698,
    "joint_5": 0.010490577667951584,
    "joint_6": 0.026131074875593185,
    "gripper": -0.21229113638401031
  }
}
```

This mirrors `capture_anygrasp_grasp_pose.py`: initialize `side=both`, wait for
the arms to settle, then continue with right-arm IK or grasp sequence commands.
