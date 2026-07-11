---
name: timer
category: meta
description: Record wall-clock/monotonic time and optionally wait for a duration.
args:
  duration_s: float
  label: string
---

# Timer

Use this meta skill when a plan needs explicit time evidence that is not itself
an actuator command.

Inputs:
- `duration_s`: optional non-negative wait duration in seconds. Defaults to
  `0.0`, which records time without sleeping.
- `label`: optional short label describing why the timer exists.

Returns:
- `started_wall_time` and `ended_wall_time`: local timezone ISO timestamps.
- `started_epoch_s` and `ended_epoch_s`: Unix epoch seconds.
- `started_monotonic_s` and `ended_monotonic_s`: monotonic clock values.
- `elapsed_s`: measured elapsed monotonic time.
- `slept_s`: requested sleep duration.

Use `timer` for non-actuator waits, explicit dwell periods between independent
skills, and audit evidence about absolute or elapsed time. Do not use repeated
instantaneous control commands as a substitute for time. For actuator commands,
prefer the actuator skill's own timing arguments first, such as
`set_base_velocity(duration_s=...)`, `set_gripper(settle_s=...)`, or arm
`velocity_limit_rad_s` plus observe/settling.
