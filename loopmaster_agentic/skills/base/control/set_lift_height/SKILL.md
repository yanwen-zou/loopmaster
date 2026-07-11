---
name: set_lift_height
category: base/control
description: Command HEI ReBot Lift vertical platform target height in millimeters.
args:
  height_mm: float
  settle_s: float
---

# Set Lift Height

Maps a millimeter height target to the HEI ReBot Lift `height.pos` action key.

Every lift command needs timing semantics. Pass `settle_s` when the next step
depends on the lift having physically moved or settled; then run `observe` to
verify the height trend/state if the task depends on it. Do not issue several
lift targets back-to-back with no settle window and treat them as completed
motion.
