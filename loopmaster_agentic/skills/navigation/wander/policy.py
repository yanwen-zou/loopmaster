import math
import os
import random
import time


def _float(args, key, default):
    try:
        return float(args.get(key, default))
    except (TypeError, ValueError):
        return float(default)


def _int(args, key, default):
    try:
        return int(args.get(key, default))
    except (TypeError, ValueError):
        return int(default)


def _pose_from_status(obj):
    if not isinstance(obj, dict):
        return None
    for key in ("pose", "map_pose", "robot_pose"):
        pose = obj.get(key)
        if isinstance(pose, dict) and "x" in pose and "y" in pose:
            return pose
    for key in ("result", "status", "data", "observation"):
        pose = _pose_from_status(obj.get(key))
        if pose:
            return pose
    return None


def _read_yaml(path):
    data = {}
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, value = line.split(":", 1)
            value = value.strip().strip("\"").strip("'")
            if value.startswith("[") and value.endswith("]"):
                data[key.strip()] = [float(x.strip()) for x in value[1:-1].split(",") if x.strip()]
            else:
                try:
                    data[key.strip()] = float(value)
                except ValueError:
                    data[key.strip()] = value
    return data


def _read_pgm(path):
    with open(path, "rb") as f:
        magic = f.readline().strip()
        if magic not in (b"P5", b"P2"):
            raise ValueError("unsupported PGM format")
        toks = []
        while len(toks) < 3:
            line = f.readline()
            if line.startswith(b"#"):
                continue
            toks.extend(line.split())
        w, h, maxv = int(toks[0]), int(toks[1]), int(toks[2])
        if magic == b"P5":
            pix = list(f.read(w * h)[:w * h])
        else:
            pix = [int(x) for x in f.read().split()[:w * h]]
        return w, h, maxv, pix


def _world_to_grid(x, y, origin, res, height):
    gx = int((x - origin[0]) / res)
    gy_map = int((y - origin[1]) / res)
    return gx, height - 1 - gy_map


def _clear(gx, gy, w, h, pix, free_min, cells):
    if gx < cells or gy < cells or gx >= w - cells or gy >= h - cells:
        return False
    for yy in range(gy - cells, gy + cells + 1):
        row = yy * w
        for xx in range(gx - cells, gx + cells + 1):
            if pix[row + xx] < free_min:
                return False
    return True


def dispatch(context, args):
    robot_ip = str(args.get("robot_ip", "192.168.31.22"))
    status_port = _int(args, "status_port", 7210)
    command_port = _int(args, "command_port", 7211)
    status_timeout_s = max(0.5, _float(args, "status_timeout_s", 5.0))
    wait_for_ack = bool(args.get("wait_for_ack", True))
    radius_m = max(0.5, _float(args, "radius_m", 6.0))
    min_radius_m = min(radius_m, max(0.0, _float(args, "min_radius_m", 0.5)))
    clearance_m = max(0.0, _float(args, "clearance_m", 0.25))
    interval_s = max(1.0, _float(args, "interval_s", 30.0))
    max_goals = _int(args, "max_goals", 1)
    duration_s = min(300.0, max(0.0, _float(args, "duration_s", 300.0)))
    yaw_strategy = str(args.get("yaw_strategy", "random"))
    max_attempts = max(1, _int(args, "max_attempts", 100))
    free_min = max(0, min(255, _int(args, "free_min_value", 250)))
    goal_id = str(args.get("goal_id", "start_bounded_wander"))
    rng = random.Random(args.get("seed"))

    map_yaml = str(args.get("map_yaml", "hei-rebot-lift/software/lerobot-hei-rebot-lift/navigation/map/map.yaml"))
    if not os.path.isabs(map_yaml):
        map_yaml = os.path.abspath(map_yaml)

    nav = {"robot_ip": robot_ip, "status_port": status_port, "command_port": command_port, "status_timeout_s": status_timeout_s}
    samples = []
    goals = []
    start_mono = time.monotonic()
    stopped_reason = "completed"

    start_status = context.call_skill("navigation", dict(nav, command="status", wait_for_ack=False))
    start_obs = context.call_skill("observe", {"include_images": True, "include_state": True})
    samples.append({"phase": "start", "elapsed_s": 0.0, "navigation_status": start_status, "observe": start_obs})
    start_pose = _pose_from_status(start_status)
    if not start_pose:
        final_stop = context.call_skill("stop_motion", {"reason": "wander no_start_pose", "settle_s": 1.0})
        stopped_observe = context.call_skill("observe", {"include_images": True, "include_state": True})
        return {"ok": False, "error": "could not determine start pose from navigation status", "samples": samples, "final_stop": final_stop, "stopped_observe": stopped_observe}

    meta = _read_yaml(map_yaml)
    image_path = str(meta.get("image", ""))
    if not os.path.isabs(image_path):
        image_path = os.path.join(os.path.dirname(map_yaml), image_path)
    res = float(meta.get("resolution", 0.05))
    origin = meta.get("origin", [0.0, 0.0, 0.0])
    w, h, _maxv, pix = _read_pgm(image_path)
    clearance_cells = int(math.ceil(clearance_m / res))
    sx, sy = float(start_pose["x"]), float(start_pose["y"])

    try:
        while True:
            elapsed = time.monotonic() - start_mono
            if duration_s > 0.0 and elapsed >= duration_s:
                stopped_reason = "timeout"
                break
            if max_goals > 0 and len(goals) >= max_goals:
                stopped_reason = "max_goals"
                break
            selected = None
            for _ in range(max_attempts):
                r = rng.uniform(min_radius_m, radius_m)
                a = rng.uniform(-math.pi, math.pi)
                x = sx + r * math.cos(a)
                y = sy + r * math.sin(a)
                gx, gy = _world_to_grid(x, y, origin, res, h)
                if _clear(gx, gy, w, h, pix, free_min, clearance_cells):
                    selected = (x, y, r)
                    break
            if selected is None:
                stopped_reason = "no_valid_goal"
                break
            x, y, dist = selected
            yaw = rng.uniform(-math.pi, math.pi) if yaw_strategy == "random" else float(start_pose.get("yaw", 0.0))
            goal_args = dict(nav, command="goal", x=x, y=y, yaw=yaw, wait_for_ack=wait_for_ack, goal_id=goal_id)
            result = context.call_skill("navigation", goal_args)
            goals.append({"elapsed_s": elapsed, "args": goal_args, "distance_from_start_m": dist, "result": result})
            remaining = duration_s - (time.monotonic() - start_mono) if duration_s > 0.0 else interval_s
            time.sleep(max(0.0, min(interval_s, remaining)))
            status = context.call_skill("navigation", dict(nav, command="status", wait_for_ack=False))
            obs = context.call_skill("observe", {"include_images": True, "include_state": True})
            samples.append({"phase": "monitor", "elapsed_s": time.monotonic() - start_mono, "navigation_status": status, "observe": obs})
    except KeyboardInterrupt:
        stopped_reason = "interrupted"

    cancel = context.call_skill("navigation", dict(nav, command="cancel", wait_for_ack=True))
    final_stop = context.call_skill("stop_motion", {"reason": "wander " + stopped_reason, "settle_s": 1.0})
    stopped_observe = context.call_skill("observe", {"include_images": True, "include_state": True})
    ok = stopped_reason in ("timeout", "max_goals", "completed")
    return {"ok": ok, "stopped_reason": stopped_reason, "elapsed_s": time.monotonic() - start_mono, "start_pose": start_pose, "radius_m": radius_m, "min_radius_m": min_radius_m, "goals": goals, "samples": samples, "cancel": cancel, "final_stop": final_stop, "stopped_observe": stopped_observe}
