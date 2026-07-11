---
name: set_gripper
category: control
description: Command one HEI ReBot Lift gripper with the robot's signed actuator convention.
args:
  side: string
  position: float
  settle_s: float
  verify: bool
  tolerance: float
  min_delta: float
---

# Set Gripper

Command one robot gripper and expose enough information for closed-loop verification.

Inputs:
- `side`: `left` or `right`.
- `position`: requested gripper command in the HEI ReBot convention:
  - `0.0` is fully closed.
  - `-5.0` is fully open.
  - Intermediate open widths are negative values between `-5.0` and `0.0`.
- `settle_s`: optional delay after sending the command before reading feedback.
- `verify`: optional boolean. When true, read gripper feedback before and after
  the command and return a `verified` result.
- `tolerance`: optional feedback tolerance for target-position verification.
- `min_delta`: optional minimum feedback movement for direction verification.

For grasp plans, open the gripper with `position=-5.0` before approach/contact,
then close with `position=0.0`. Use `settle_s>=1.0` and `verify=true` for both
open and close in real-robot grasp plans; immediate millisecond-scale observe
samples are not enough to prove gripper motion. Never use `position=1.0` for
open; it is outside the robot's gripper range and would collapse to closed if
blindly clamped.

Required behavior:
1. Map `position` to the correct actuator key for the selected side: `{side}_gripper.pos`.
2. Reject values outside `[-5.0, 0.0]` instead of silently collapsing open and close requests to the same actuator target.
3. Return `action_sent` with only the actual `{side}_gripper.pos` command value.
   Do not include arm joint keys in a gripper-only result.
4. Include enough metadata for verification, such as `requested_position`, `commanded_position`, and `side`.
5. After a gripper command in a grasp plan, run `observe(include_images=false, include_state=true)` and verify the selected `{side}_gripper.pos` changes in the expected direction or report that the gripper feedback did not move.

Do not declare a grasp successful from `action_sent` alone. For pick-up tasks, success requires observable gripper/contact/image feedback and, when requested, a lift observation.

Every gripper command is time-dependent. Use `settle_s` rather than issuing
open/close commands back-to-back with immediate observes; gripper feedback can
lag the command.
