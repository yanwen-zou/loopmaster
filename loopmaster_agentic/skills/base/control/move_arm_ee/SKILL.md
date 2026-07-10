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
`rotation_matrix`. `input_frame` defaults to `head_camera`; use `arm` when the
pose is already expressed in that arm's IK base frame.

Head-camera extrinsics are loaded from
`loopmaster_agentic/config/head_camera_extrinsics.json`. The initial checked-in
arm-to-camera values are:

- left arm to camera: `[0.03, -0.23, 0.34]`
- right arm to camera: `[0.03, 0.23, 0.34]`
- camera rotation: +60 degrees around the arm-frame Y axis

Pass `execute=false` to compute and return the IK joint targets without moving
hardware.

Pass `current_positions` to seed IK and joint-step limiting from a known pose.
Pass `other_arm_positions` to include the non-moving arm in each executed
command, keeping it at the provided joint targets while this arm moves.
Pass `preserve_current_orientation=true` to keep the current end-effector
rotation relative to the arm base while solving for a new translation.

Runtime speed control is implemented at the skill layer because the HEI ReBot
Lift arm command API does not expose a per-command velocity argument. The local
driver has a robot-config-level `arm_velocity_limit_rad_s`, but remote
`command_arm`/`send_action` only accepts joint position targets. Pass
`max_joint_step` in radians to split the IK target into intermediate joint
waypoints, with `step_dt` seconds between waypoints and optional final
`hold_s`.
