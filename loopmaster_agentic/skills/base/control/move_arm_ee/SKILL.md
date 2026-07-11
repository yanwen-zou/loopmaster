---
name: move_arm_ee
category: base/control
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
  max_joint_step: float
  step_dt: float
  hold_s: float
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
initialize both arms with `move_arm_joints` using
`loopmaster_agentic/config/arm_init_pose.json`. This should be the first arm
command in grasp/move-arm plans. After that initialization, pass the same
normalized init pose as `current_positions` for the moving arm and as
`other_arm_positions` for the non-moving arm so IK starts from the initialized
configuration and every executed trajectory holds the other arm in place. This
matches the initialization flow in `capture_anygrasp_grasp_pose.py`.

Pass `current_positions` to seed IK and joint-step limiting from a known pose.
Pass `other_arm_positions` to include the non-moving arm in each executed
command, keeping it at the provided joint targets while this arm moves.
Pass `preserve_current_orientation=true` to keep the current end-effector
rotation relative to the arm base while solving for a new translation.

For hardware safety, target poses are clipped after conversion into the target
arm base frame so the requested end-effector z is never below `0.06` meters.
The returned `ik_info` reports whether clipping happened.

Runtime speed control is implemented at the skill layer because the HEI ReBot
Lift arm command API does not expose a per-command velocity argument. The local
driver has a robot-config-level `arm_velocity_limit_rad_s`, but remote
`command_arm`/`send_action` only accepts joint position targets. Pass
`max_joint_step` in radians to split the IK target into intermediate joint
waypoints, with `step_dt` seconds between waypoints and optional final
`hold_s`.
