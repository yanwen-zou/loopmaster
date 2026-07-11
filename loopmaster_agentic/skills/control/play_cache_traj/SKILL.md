---
name: play_cache_traj
category: control
description: Replay one recorded trajectory episode from loopmaster_agentic/config/record_traj and return the arms to init pose.
args:
  episode: integer
  traj_root: string
  fps: number
  speed: number
  stride: integer
  max_frames: integer
  dry_run_limit: integer
  settle_s: number
  return_to_init: bool
  velocity_limit_rad_s: number
---

# Play Cache Trajectory

Replay one episode from `loopmaster_agentic/config/record_traj`. The checked-in
dataset contains five episodes, indexed `0` through `4`, corresponding to the
five recorded points.

Inputs:
- `episode`: required integer episode id, `0` to `4`.
- `traj_root`: optional dataset root. Defaults to
  `loopmaster_agentic/config/record_traj`.
- `fps`: optional playback FPS. Defaults to dataset `meta/info.json` FPS.
- `speed`: playback speed multiplier. `2.0` plays twice as fast.
- `stride`: send every Nth frame. Defaults to `1`.
- `max_frames`: optional cap for debugging.
- `dry_run_limit`: optional cap applied only when the platform name is
  `dry_run`; default `30` so local smoke tests do not wait for a full episode.
- `return_to_init`: defaults true. Calls `stop_motion` and `init_arms` after
  replay, even when replay fails partway through.

Behavior:
1. Load `meta/info.json` and `data/chunk-*/file-*.parquet`.
2. Filter rows by `episode_index`.
3. Send each row's `action` vector as a named action dict using the feature
   names in `meta/info.json`.
4. Sleep according to dataset timestamps/FPS, adjusted by `speed`.
5. On exit, stop base motion and return both arms to
   `loopmaster_agentic/config/arm_init_pose.json` through `init_arms`.

This is a direct trajectory replay skill. It does not perform perception,
collision checking, or semantic task verification.
