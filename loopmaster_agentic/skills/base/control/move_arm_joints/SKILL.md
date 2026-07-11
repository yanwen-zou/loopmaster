---
name: move_arm_joints
category: base/control
description: Command one or both HEI ReBot Lift arms using joint positions.
args:
  side: string
  positions: object
  velocity_limit_rad_s: float
  settle_s: float
---

# Move Arm Joints

Commands `left`, `right`, or `both` arm joint targets. Positions may be a list
of seven values or a dictionary keyed by `joint_1` through `joint_6` and
`gripper`. For `side=both`, a shared list/dict is applied to both arms; pass
`{"right": {...}, "left": {...}}` to use different targets per arm.

When a dictionary specifies only some joints, the skill must preserve every
unspecified joint by reading the current arm state and filling a complete
seven-joint target before sending any command. If current state cannot be read,
the skill fails instead of sending zeros or relying on implicit defaults.

After connecting to the physical arms and before any grasp or end-effector
motion, prefer the registered `init_arms` skill. It loads
`loopmaster_agentic/config/arm_init_pose.json`, checks joint limits, commands
both arms, waits for settling, and verifies observed state. Use
`move_arm_joints side=both` directly only when issuing an explicit operator
target or when `init_arms` is unavailable.

Arm motion is speed-limited through the platform velocity interface, not by
inserting intermediate waypoints. Pass `velocity_limit_rad_s` to set the arm
joint velocity limit for this command. The default is `0.8` rad/s.

Every physical arm command needs timing semantics. Use `velocity_limit_rad_s` to
control how fast the arm is allowed to move, and pass `settle_s` when the next
step depends on observed arm state. Do not approximate a duration by sending
several joint commands back-to-back with no wait; issue one velocity-limited
target, wait/observe, then decide the next command.
