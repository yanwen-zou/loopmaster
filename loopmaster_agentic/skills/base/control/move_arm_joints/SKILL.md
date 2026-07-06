---
name: move_arm_joints
category: base/control
description: Command one HEI ReBot Lift arm using joint positions.
args:
  side: string
  positions: object
---

# Move Arm Joints

Commands `left` or `right` arm joint targets. Positions may be a list of seven
values or a dictionary keyed by `joint_1` through `joint_6` and `gripper`.
