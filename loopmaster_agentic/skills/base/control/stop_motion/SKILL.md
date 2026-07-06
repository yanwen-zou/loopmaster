---
name: stop_motion
category: base/control
description: Stop HEI ReBot Lift chassis and lift motion through the platform safety hook.
args:
  reason: string
---

# Stop Motion

Calls the platform stop hook. Use this at the end of inspection runs or after a
failed skill before handing control back to the operator.
