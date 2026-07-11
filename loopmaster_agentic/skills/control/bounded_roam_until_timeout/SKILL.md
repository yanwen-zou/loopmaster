# Bounded Roam Until Timeout

Conservatively roam with low-level body-frame base velocity commands until interrupted or a bounded timeout elapses.

## Inputs

- `duration_s`: maximum roam duration in seconds. Defaults to `300.0` and is capped at `300.0`.
- `segment_s`: duration of each nonzero base command. Defaults to `5.0` and is capped at `5.0`.
- `settle_s`: zero-velocity settle duration after each segment. Defaults to `0.5`.
- `refresh_hz`: refresh rate for base velocity commands. Defaults to `5.0`.
- `x`: forward body-frame velocity in m/s. Defaults to `0.05` and is clipped to `[-0.1, 0.1]`.
- `theta`: turn rate magnitude in rad/s. Defaults to `0.15` and is clipped to `[0.0, 0.2]`.
- `include_images`: whether periodic observes include images. Defaults to `true`.
- `max_segments`: optional cap on segment count. When omitted, duration controls the loop.

## Behavior

1. Observe before motion.
2. Repeat bounded low-speed base arcs with explicit `duration_s` on every base command.
3. Keep every nonzero base command at or below 5 seconds.
4. Alternate turn direction to keep motion varied.
5. Send a zero-velocity command and observe after every segment for closed-loop feedback.
6. Stop when interrupted, when timeout is reached, or when `max_segments` is reached.
7. Before returning, send a zero-velocity settle command, observe stopped-state feedback, then call `stop_motion` as the final safety command.

This skill is for supervised casual bounded roaming. It is not autonomous map navigation and does not provide semantic obstacle clearance.
