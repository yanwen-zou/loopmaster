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
