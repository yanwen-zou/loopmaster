---
name: stop_motion
category: base/control
description: Stop HEI ReBot Lift chassis and lift motion through the platform safety hook.
args:
  reason: string
  settle_s: float
---

# Stop Motion

Calls the platform stop hook. Use this at the end of inspection runs or after a
failed skill before handing control back to the operator.

Stopping is also time-dependent. For real robot runs, pass `settle_s` when the
next step or final audit needs evidence that base/lift velocity has settled to
zero, then run `observe(include_state=true)`. Do not call `stop_motion` and
immediately declare the robot stopped without a settling window or state
feedback when stopped-state verification matters.
