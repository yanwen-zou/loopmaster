---
name: capture_image
category: base/perception
description: Capture metadata for a named HEI ReBot Lift camera frame from the latest observation.
args:
  camera: string
  required: bool
---

# Capture Image

Records camera evidence from `front`, `left_wrist`, or `right_wrist`. The skill
does not do task-specific perception; learned detectors should be added later as
separate learned skills.
