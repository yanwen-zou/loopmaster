import multiprocessing as mp
import time


def _float_arg(args, key, default):
    try:
        return float(args.get(key, default))
    except (TypeError, ValueError):
        return float(default)


def _int_arg(args, key, default):
    try:
        return int(args.get(key, default))
    except (TypeError, ValueError):
        return int(default)


def _wander_child(context, wander_args):
    context.call_skill('wander', wander_args)


def dispatch(context, args):
    duration_s = max(1.0, _float_arg(args, 'duration_s', 300.0))
    monitor_period_s = max(1.0, _float_arg(args, 'monitor_period_s', 5.0))
    robot_ip = str(args.get('robot_ip', '192.168.31.22'))
    status_port = _int_arg(args, 'status_port', 7210)
    command_port = _int_arg(args, 'command_port', 7211)
    status_timeout_s = max(0.5, _float_arg(args, 'status_timeout_s', 5.0))
    include_images = bool(args.get('include_images', True))

    wander_args = {
        'robot_ip': robot_ip,
        'status_port': status_port,
        'command_port': command_port,
        'status_timeout_s': status_timeout_s,
        'wait_for_ack': True,
        'radius_m': max(0.5, _float_arg(args, 'wander_radius_m', 2.0)),
        'min_radius_m': max(0.0, _float_arg(args, 'wander_min_radius_m', 0.5)),
        'clearance_m': max(0.0, _float_arg(args, 'clearance_m', 0.25)),
        'interval_s': max(1.0, _float_arg(args, 'wander_interval_s', 30.0)),
        'max_goals': 0,
        'duration_s': 0.0,
        'yaw_strategy': str(args.get('yaw_strategy', 'random')),
        'max_attempts': _int_arg(args, 'max_attempts', 50),
        'goal_id': str(args.get('goal_id', 'monitored_wander')),
    }

    samples = []
    start_wall = time.time()
    start_mono = time.monotonic()
    stopped_reason = 'timeout'

    samples.append({'phase': 'before_start', 'elapsed_s': 0.0, 'observe': context.call_skill('observe', {'include_images': include_images, 'include_state': True})})

    proc = mp.get_context('fork').Process(target=_wander_child, args=(context, wander_args))
    proc.start()

    try:
        while True:
            elapsed_s = time.monotonic() - start_mono
            if elapsed_s >= duration_s:
                stopped_reason = 'timeout'
                break
            if not proc.is_alive():
                stopped_reason = 'wander_exited'
                break
            sleep_s = min(monitor_period_s, max(0.0, duration_s - elapsed_s))
            time.sleep(sleep_s)
            elapsed_s = time.monotonic() - start_mono
            status = context.call_skill('navigation', {'command': 'status', 'robot_ip': robot_ip, 'status_port': status_port, 'command_port': command_port, 'status_timeout_s': status_timeout_s, 'wait_for_ack': False})
            obs = context.call_skill('observe', {'include_images': include_images, 'include_state': True})
            samples.append({'phase': 'monitor', 'elapsed_s': elapsed_s, 'navigation_status': status, 'observe': obs})
    except KeyboardInterrupt:
        stopped_reason = 'interrupted'
    finally:
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=5.0)
            if proc.is_alive():
                proc.kill()
                proc.join(timeout=2.0)
        cancel = context.call_skill('navigation', {'command': 'cancel', 'robot_ip': robot_ip, 'status_port': status_port, 'command_port': command_port, 'status_timeout_s': status_timeout_s, 'wait_for_ack': True})
        final_stop = context.call_skill('stop_motion', {'reason': 'monitor_wander_process ' + stopped_reason, 'settle_s': 1.0})
        stopped_observe = context.call_skill('observe', {'include_images': include_images, 'include_state': True})

    return {
        'ok': True,
        'stopped_reason': stopped_reason,
        'elapsed_s': time.monotonic() - start_mono,
        'started_epoch_s': start_wall,
        'ended_epoch_s': time.time(),
        'wander_args': wander_args,
        'child_exitcode': proc.exitcode,
        'samples': samples,
        'cancel': cancel,
        'final_stop': final_stop,
        'stopped_observe': stopped_observe,
    }
