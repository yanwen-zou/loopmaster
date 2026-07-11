---
name: move_arm_ee
category: control
description: Command one HEI ReBot Lift arm by end-effector pose using the mink IK model.
args:
  side: string
  pose: object
  input_frame: string
  execute: bool
  gripper: float
  require_ik_success: bool
  current_positions: object
  other_arm_positions: object
  preserve_current_orientation: bool
  velocity_limit_rad_s: float
  settle_s: float
---

# Move Arm End Effector

Commands the `left` or `right` arm from an end-effector pose. The skill runs
mink differential IK against the checked-in HEI ReBot Lift MuJoCo XML model,
converts the pose to joint targets, then calls the platform arm command. This
backend is intended for LoopMaster's uv runtime and does not use the
Pinocchio/CasADi subprocess path.

Install `mink` and `mujoco` in the LoopMaster environment, for example through
the `vr` optional dependency or `uv pip install mink mujoco`.

`pose` may be a 4x4 `matrix`, or `position` plus one of `rpy`, `quat`, or
`rotation_matrix`. `input_frame` names the coordinate frame of the target pose.
It defaults to `head_camera`; use `left_arm` or `right_arm` when the pose is
already expressed in that arm's IK base frame.

Head-camera extrinsics are loaded from
`loopmaster_agentic/config/head_camera_extrinsics.json`. The initial checked-in
arm-to-camera values are:

- left arm to camera: `[0.03, -0.23, 0.34]`
- right arm to camera: `[0.03, 0.23, 0.34]`
- camera rotation: +60 degrees around the arm-frame Y axis

Pass `execute=false` to compute and return the IK joint targets without moving
hardware.

Before executing physical end-effector motion after connecting the arms,
initialize both arms with the registered `init_arms` skill. This should be the
first arm command in grasp/move-arm plans. After that initialization, pass the
same normalized init pose as `current_positions` for the moving arm and as
`other_arm_positions` for the non-moving arm so IK starts from the initialized
configuration and every executed trajectory holds the other arm in place. This
matches the initialization flow in `capture_anygrasp_grasp_pose.py` while
keeping the fixed initialization behind a registered, limit-checked skill.

Pass `current_positions` to seed IK and joint-step limiting from a known pose.
The value may be either an unprefixed arm joint dict or a full observed state
with keys like `right_joint_1.pos`. Pass `other_arm_positions` to include the
non-moving arm in each executed command, keeping it at the provided joint
targets while this arm moves. If `other_arm_positions` is omitted, the skill
reads the current non-moving arm state and holds it; it must not send zeros for
unspecified joints.
Pass `preserve_current_orientation=true` to keep the current end-effector
rotation relative to the arm base while solving for a new translation.

For hardware safety, target poses are clipped after conversion into the target
arm base frame so the requested end-effector z is never below `0.06` meters.
The returned `ik_info` reports whether clipping happened.

Runtime speed control uses the HEI ReBot arm velocity interface. Pass
`velocity_limit_rad_s` to set the per-command arm joint velocity limit. The
default is `0.8` rad/s. The skill sends one target command with that velocity
limit; it does not insert intermediate waypoints.

Every physical end-effector command needs timing semantics. Use
`velocity_limit_rad_s` for movement speed and pass `settle_s` when downstream
steps rely on the reached pose. Do not emit several end-effector commands
back-to-back with no duration or settling window; send one target, wait/observe,
then continue.
