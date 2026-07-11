import time


def _float_arg(args, key, default):
    try:
        return float(args.get(key, default))
    except (TypeError, ValueError):
        return float(default)


def dispatch(context, args):
    duration_s = min(300.0, max(0.0, _float_arg(args, 'duration_s', 300.0)))
    segment_s = min(5.0, max(0.5, _float_arg(args, 'segment_s', 5.0)))
    settle_s = max(0.5, _float_arg(args, 'settle_s', 0.5))
    refresh_hz = max(1.0, _float_arg(args, 'refresh_hz', 5.0))
    x = max(-0.1, min(0.1, _float_arg(args, 'x', 0.05)))
    theta_mag = max(0.0, min(0.2, abs(_float_arg(args, 'theta', 0.15))))
    include_images = bool(args.get('include_images', True))
    max_segments_arg = args.get('max_segments')
    max_segments = None if max_segments_arg is None else max(0, int(max_segments_arg))

    observations = []
    commands = []
    start = time.monotonic()
    stopped_reason = 'timeout'
    segment_index = 0

    observations.append({'phase': 'start', 'result': context.call_skill('observe', {'include_images': include_images, 'include_state': True})})

    try:
        while time.monotonic() - start < duration_s:
            if max_segments is not None and segment_index >= max_segments:
                stopped_reason = 'max_segments'
                break
            remaining = duration_s - (time.monotonic() - start)
            if remaining <= 0.0:
                stopped_reason = 'timeout'
                break
            command_s = min(segment_s, remaining, 5.0)
            turn = theta_mag if segment_index % 2 == 0 else -theta_mag
            move_args = {'x': x, 'y': 0.0, 'theta': turn, 'duration_s': command_s, 'refresh_hz': refresh_hz}
            commands.append({'phase': 'move', 'segment': segment_index, 'args': move_args, 'result': context.call_skill('set_base_velocity', move_args)})

            settle_args = {'x': 0.0, 'y': 0.0, 'theta': 0.0, 'duration_s': settle_s, 'refresh_hz': refresh_hz}
            commands.append({'phase': 'settle', 'segment': segment_index, 'args': settle_args, 'result': context.call_skill('set_base_velocity', settle_args)})
            observations.append({'phase': 'after_segment', 'segment': segment_index, 'result': context.call_skill('observe', {'include_images': include_images, 'include_state': True})})
            segment_index += 1
    except KeyboardInterrupt:
        stopped_reason = 'interrupted'

    final_settle_args = {'x': 0.0, 'y': 0.0, 'theta': 0.0, 'duration_s': settle_s, 'refresh_hz': refresh_hz}
    commands.append({'phase': 'final_settle', 'args': final_settle_args, 'result': context.call_skill('set_base_velocity', final_settle_args)})
    observations.append({'phase': 'stopped_state_before_final_stop', 'result': context.call_skill('observe', {'include_images': include_images, 'include_state': True})})
    final_stop = context.call_skill('stop_motion', {'reason': 'bounded_roam_until_timeout ' + stopped_reason, 'settle_s': max(1.0, settle_s)})

    elapsed_s = time.monotonic() - start
    return {'ok': True, 'elapsed_s': elapsed_s, 'segments': segment_index, 'stopped_reason': stopped_reason, 'commands': commands, 'observations': observations, 'final_stop': final_stop}
