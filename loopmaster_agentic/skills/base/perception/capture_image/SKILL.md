---
name: capture_image
category: base/perception
description: Capture a named HEI ReBot Lift camera frame, including D435 RGB-D frames from the robot ZMQ stream.
args:
  camera: string
  required: bool
  source: string
  robot_ip: string
  port: int
  topic: string
  timeout_ms: int
  rgb_path: string
  depth_path: string
---

# Capture Image

Records camera evidence from `front`, `left_wrist`, or `right_wrist`.

For robot D435 RGB-D streaming, pass `source: d435_rgbd` or `camera: d435`. The
skill subscribes to the same four-part ZMQ message as `pc_d435_rgbd_receiver.py`:
topic, metadata JSON, color JPEG, and raw uint16 depth PNG. It saves the decoded
RGB image and raw depth image to `rgb_path` and `depth_path` when provided, or to
the current workspace captures directory otherwise.

The skill does not do task-specific perception; detectors such as AnyGrasp are
exposed as separate perception skills.
