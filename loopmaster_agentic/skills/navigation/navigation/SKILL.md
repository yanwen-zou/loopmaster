---
name: navigation
category: navigation
description: Send and monitor HEI ReBot Lift Nav2 map-frame navigation commands through the hei_nav_zmq_bridge ZMQ interface. Use for autonomous base navigation to a map pose, checking robot map pose or Nav2 state, canceling an active or queued navigation goal, or pinging the navigation command channel.
args:
  command: string
  x: float
  y: float
  yaw: float
  goal_id: string
  robot_ip: string
  status_port: int
  command_port: int
  status_timeout_s: float
  wait_for_ack: bool
---

# Navigation

Use this skill for high-level Nav2 navigation through `hei_nav_zmq_bridge`.
The bridge sends commands over ZMQ and reports robot status from
`map -> base_footprint`.

Robot-side prerequisites:
1. Start hardware, chassis bridge, odometry, lidar, IMU, robot state publisher,
   and EKF with `hei_nav_bringup robot_full_bringup.launch.py`.
2. Start Nav2 with `turtlebot3_navigation2 hei_navigation2.launch.py` and the
   map file, then set the initial pose in RViz with `2D Pose Estimate`.
3. Start `hei_nav_zmq_bridge robot_nav_server.launch.py`.

Default network settings:
- Robot IP: `192.168.31.22`
- Status PUB: TCP port `7210`
- Command PULL: TCP port `7211`
- Command frame: `map`

Commands:
- `goal` or `navigate_to_pose`: send a `map` goal with `x`, `y`, and `yaw` in
  meters/radians. A new goal cancels the active goal before executing.
- `status`: return the latest ZMQ status, including pose, Nav2 state, remaining
  distance, navigation time, estimated remaining time, recoveries, and last
  command ack.
- `cancel`: cancel the active or queued goal. Pass `goal_id` to cancel a
  specific goal; omit it to cancel the last goal sent by this skill or the
  current goal.
- `ping`: send a command-channel ping and read the next status ack.

Prefer explicit, map-frame goals. Do not use body-frame velocity commands for
multi-meter route navigation when this skill is available.

Do not use `navigation(command="status")` as a path-clearance or rear-obstacle
check for a low-level body-frame velocity command. Navigation status reports
localization/Nav2 state; it does not prove that a short backward/forward
operator velocity command is clear. For explicit low-level timed base motion,
use `set_base_velocity` with bounded `duration_s`, then stop and observe.
