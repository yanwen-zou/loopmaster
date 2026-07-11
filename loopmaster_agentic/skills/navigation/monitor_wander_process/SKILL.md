# Monitor Wander Process

Run `wander` as a supervised child process and monitor robot/navigation state until a timeout, child exit, or interruption.

## Inputs

- `duration_s`: maximum monitoring duration in seconds. Defaults to `300.0`.
- `monitor_period_s`: seconds between monitor samples. Defaults to `5.0`.
- `robot_ip`: robot IP for navigation. Defaults to `192.168.31.22`.
- `status_port`: navigation status port. Defaults to `7210`.
- `command_port`: navigation command port. Defaults to `7211`.
- `status_timeout_s`: navigation status timeout. Defaults to `5.0`.
- `wander_radius_m`: wander sampling radius. Defaults to `2.0`.
- `wander_min_radius_m`: minimum wander sampling radius. Defaults to `0.5`.
- `clearance_m`: map free-space clearance for wander. Defaults to `0.25`.
- `wander_interval_s`: interval between child wander goals. Defaults to `30.0`.
- `yaw_strategy`: wander yaw strategy. Defaults to `random`.
- `include_images`: whether monitor observations include images. Defaults to `true`.

## Behavior

1. Observe current robot state before launching the child.
2. Start `wander` in a separate process with `max_goals=0` so it keeps producing goals until interrupted.
3. Monitor wall-clock elapsed time and periodically sample `navigation(status)` plus `observe`.
4. When `duration_s` elapses, or if the monitor is interrupted, terminate the child process.
5. Cancel navigation, command a settled stop through `stop_motion`, and collect stopped-state feedback before returning.

This skill supervises autonomous wandering. It does not provide a semantic obstacle clearance verdict beyond the map-free-space sampling already implemented by `wander`.
