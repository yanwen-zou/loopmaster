---
name: init_arms
category: base/control
description: Initialize both HEI ReBot Lift arms from the repository-validated joint pose.
args:
  config_path: string
  settle_s: number
  tolerance_rad: number
  verify: bool
  velocity_limit_rad_s: float
---

# Init Arms

Moves both arms to the validated repository initialization pose in
`loopmaster_agentic/config/arm_init_pose.json`. Use this registered skill after
connecting to the physical robot and before any grasp, end-effector motion, or
single-arm joint command.

The skill loads the config, verifies every configured joint is present and
within the HEI ReBot Lift per-side joint limits, commands both arms together,
waits for settling, then observes the robot state and compares reported arm
positions against the targets. This is the preferred initialization path; do
not inline the fixed joint dictionary in a plan unless this skill is unavailable.

Optional args:

- `config_path`: override the init config path. Relative paths are resolved
  against the LoopMaster repository root.
- `settle_s`: seconds to wait before verification. Defaults to `1.0`.
- `tolerance_rad`: max allowed observed joint error. Defaults to `0.08`.
- `verify`: set false only for dry infrastructure checks where arm state
  feedback is intentionally unavailable.
- `velocity_limit_rad_s`: per-command arm joint velocity limit. Defaults to
  `0.8` rad/s. The skill sends one target command through the velocity
  interface rather than inserting waypoints.

Initialization is physical motion and needs timing semantics. Keep
`velocity_limit_rad_s` conservative and use `settle_s` before verification; do
not follow initialization immediately with dependent arm motion unless the
settling/verification result is acceptable.
