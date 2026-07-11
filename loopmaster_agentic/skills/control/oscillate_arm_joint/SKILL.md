---
name: oscillate_arm_joint
description: Oscillate one arm joint around the latest observed robot state with diagnostic feedback.
category: control
args:
  side: string
  joint: integer
  amplitude_rad: number
  cycles: integer
  dwell_s: number
  feedback_polls: integer
  feedback_poll_s: number
  tolerance_rad: number
  min_motion_rad: number
  strict_verify: boolean
---

# oscillate_arm_joint

Oscillate one arm joint around the latest observed starting pose while holding the rest of that arm command vector fixed.

Inputs:
- `side`: `left` or `right`, default `left`.
- `joint`: integer from 1 through 6, default `5`.
- `amplitude_rad`: positive joint offset in radians, default `0.5`.
- `cycles`: positive number of back-and-forth cycles, default `5`.
- `dwell_s`: seconds to hold each positive/negative target before feedback polling, default `0.75`.
- `feedback_polls`: number of state samples after each target, default `2`.
- `feedback_poll_s`: seconds between feedback samples, default `0.15`.
- `tolerance_rad`: target-hit tolerance used for diagnostics, default `0.12`.
- `min_motion_rad`: minimum observed range that proves motion happened, default `0.15`.
- `strict_verify`: if true, fail when target samples miss tolerance; default false.

Behavior:
1. Calls `observe` immediately before motion.
2. Builds the 7-value `move_arm_joints` array in order: joint_1, joint_2, joint_3, joint_4, joint_5, joint_6, gripper.
3. Changes only the selected joint for positive and negative targets.
4. Holds each target for `dwell_s`, then samples state feedback one or more times.
5. Uses feedback as a diagnostic time series: report target error, observed min/max, and whether motion range was observed.
6. Does not require one instantaneous sample to equal the target unless `strict_verify=true`, because real feedback can be delayed or sampled while the joint is still moving.
7. Returns to the fresh start vector, samples final feedback, and calls `stop_motion`.
