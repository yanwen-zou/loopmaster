---
name: set_base_velocity
category: base/control
description: Command HEI ReBot Lift omnidirectional chassis velocity.
args:
  x: float
  y: float
  theta: float
  duration_s: float
  refresh_hz: float
---

# Set Base Velocity

Maps body-frame velocity targets to `x.vel`, `y.vel`, and `theta.vel`.

Inputs:
- `x`, `y`, `theta`: body-frame chassis velocity command.
- `duration_s`: optional duration to keep refreshing the same velocity command.
- `refresh_hz`: optional refresh rate while `duration_s > 0`; default is 5 Hz.

For requests like "move forward for 5 seconds", call this skill once with
`duration_s=5.0`, then call `stop_motion`, then observe stopped state. Do not
approximate multi-second motion by issuing several immediate velocity commands
without sleeps; that produces only millisecond-scale motion windows.

This is the correct skill for explicit low-level operator commands such as
"move backward for 5 seconds" when the command is conservative and bounded. A
typical safe low-level command uses `abs(x)<=0.1`, `abs(y)<=0.1`,
`abs(theta)<=0.2`, `duration_s<=5.0`, followed by `stop_motion` and stopped-state
observation. Do not require `navigation(command="status")` as a prerequisite for
this low-level command; navigation status is not a path-clearance verdict.

Required behavior:
1. Command only `x.vel`, `y.vel`, and `theta.vel`.
2. Return `action_sent` containing only those three base velocity keys.
3. Never include arm joint, gripper, or lift keys in a base-only result.

Every base velocity command needs explicit time semantics. Use `duration_s` for
intentional motion and a zero-velocity command or `stop_motion(settle_s=...)`
for stopping. Do not issue several nonzero base velocity commands back-to-back
with no duration and treat that as multi-second motion.
