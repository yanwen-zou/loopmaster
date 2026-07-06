---
name: set_gripper
category: base/control
description: Command one HEI ReBot Lift gripper position.
args:
  side: string
  position: float
---

# Set Gripper

Sets `left_gripper.pos` or `right_gripper.pos`. The numeric convention follows
the HEI ReBot Lift driver configuration.
