# Wander

Start-anchored autonomous map wandering. Samples random valid free-space map goals within a fixed radius of the robot pose captured at the start of the run.

## Inputs
- `robot_ip`: default `192.168.31.22`.
- `status_port`: default `7210`.
- `command_port`: default `7211`.
- `status_timeout_s`: default `5.0`.
- `wait_for_ack`: default `true`.
- `map_yaml`: default `hei-rebot-lift/software/lerobot-hei-rebot-lift/navigation/map/map.yaml`.
- `radius_m`: maximum distance from the starting map pose. Default `6.0`.
- `min_radius_m`: minimum distance from the starting map pose. Default `0.5`.
- `clearance_m`: required occupancy-grid clearance. Default `0.25`.
- `interval_s`: seconds between goals and monitor samples. Default `30.0`.
- `max_goals`: goal count cap. Default `1`; use `0` to run until `duration_s` or interruption.
- `duration_s`: runtime cap in seconds. Default `300.0`, capped at `300.0`.
- `yaw_strategy`: `random` or `current`. Default `random`.
- `max_attempts`: random samples per goal. Default `100`.
- `free_min_value`: PGM value treated as free. Default `250`.
- `goal_id`: optional navigation goal id.
- `seed`: optional random seed.

## Behavior
1. Read `navigation(status)` and `observe` before motion.
2. Load map YAML and PGM.
3. Sample only high-confidence free cells with clearance.
4. Keep sampled goals within `radius_m` of the starting pose, not the latest pose.
5. Send goals using `navigation(command="goal")`.
6. Periodically sample `navigation(status)` and `observe`.
7. Stop after `max_goals`, capped `duration_s`, no valid goal, or interruption.
8. Cancel navigation, call `stop_motion(settle_s=1.0)`, then observe stopped-state feedback before returning.

This is autonomous map wandering. It uses map/Nav2 free-space checks, not semantic obstacle clearance.
