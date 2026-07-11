from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_TRAJ_ROOT = REPO_ROOT / "loopmaster_agentic" / "config" / "record_traj"


def dispatch(context, args):
    args = dict(args or {})
    try:
        episode = int(args["episode"])
    except (KeyError, TypeError, ValueError):
        return {"ok": False, "error": "episode is required and must be an integer from 0 to 4"}

    try:
        traj_root = _resolve_traj_root(args.get("traj_root"))
        info = _load_info(traj_root)
        names = _action_names(info)
        total_episodes = int(info.get("total_episodes") or 0)
        fps = float(args.get("fps") or info.get("fps") or 30.0)
        speed = float(args.get("speed") or 1.0)
        stride = max(int(args.get("stride") or 1), 1)
        max_frames = _optional_positive_int(args.get("max_frames"))
        dry_run_limit = _optional_positive_int(args.get("dry_run_limit"), default=30)
        return_to_init = bool(args.get("return_to_init", True))
        settle_s = max(float(args.get("settle_s", 1.0) or 0.0), 0.0)
        velocity_limit_rad_s = args.get("velocity_limit_rad_s")
    except Exception as exc:
        return {"ok": False, "error": f"invalid play_cache_traj args: {type(exc).__name__}: {exc}"}

    if fps <= 0.0:
        return {"ok": False, "error": "fps must be positive"}
    if speed <= 0.0:
        return {"ok": False, "error": "speed must be positive"}
    if total_episodes > 0 and not 0 <= episode < total_episodes:
        return {"ok": False, "error": f"episode must be in [0, {total_episodes - 1}], got {episode}"}

    sent_count = 0
    selected_count = 0
    started = time.monotonic()
    finalization: dict[str, Any] = {}
    error = ""
    try:
        rows = _load_episode_rows(traj_root, episode)
        if not rows:
            return {"ok": False, "error": f"episode {episode} not found under {traj_root}"}

        selected = rows[::stride]
        selected_count = len(selected)
        if max_frames is not None:
            selected = selected[:max_frames]
        elif _is_dry_run(context) and dry_run_limit is not None:
            selected = selected[:dry_run_limit]

        prev_timestamp: float | None = None
        last_action: dict[str, float] | None = None
        for row in selected:
            timestamp = float(row.get("timestamp") or 0.0)
            if prev_timestamp is not None:
                delay = max(timestamp - prev_timestamp, 0.0) / speed
                if delay > 0.0:
                    time.sleep(delay)
            else:
                delay = 0.0
            action = _action_dict(names, row["action"])
            if velocity_limit_rad_s is not None:
                action["arm_velocity_limit_rad_s"] = float(velocity_limit_rad_s)
            sent = context.platform.send_action(action)
            sent_count += 1
            prev_timestamp = timestamp
            last_action = sent if isinstance(sent, dict) else action
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        if return_to_init:
            finalization = _return_to_init(context, settle_s=settle_s, velocity_limit_rad_s=velocity_limit_rad_s)

    elapsed_s = time.monotonic() - started
    result = {
        "ok": not error,
        "episode": episode,
        "traj_root": str(traj_root),
        "fps": fps,
        "total_episodes": total_episodes,
        "speed": speed,
        "stride": stride,
        "selected_frames": selected_count,
        "sent_frames": sent_count,
        "elapsed_s": elapsed_s,
        "return_to_init": finalization,
    }
    if error:
        result["error"] = error
    return result


def _resolve_traj_root(value: Any) -> Path:
    if value:
        path = Path(str(value)).expanduser()
        if not path.is_absolute():
            path = REPO_ROOT / path
        return path.resolve()
    return DEFAULT_TRAJ_ROOT


def _load_info(root: Path) -> dict[str, Any]:
    path = root / "meta" / "info.json"
    if not path.is_file():
        raise FileNotFoundError(f"missing trajectory metadata: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _action_names(info: dict[str, Any]) -> list[str]:
    names = (((info.get("features") or {}).get("action") or {}).get("names") or [])
    if not isinstance(names, list) or not names:
        raise ValueError("meta/info.json does not define features.action.names")
    return [str(name) for name in names]


def _load_episode_rows(root: Path, episode: int) -> list[dict[str, Any]]:
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise RuntimeError("pyarrow is required to read record_traj parquet files") from exc

    rows: list[dict[str, Any]] = []
    for path in sorted((root / "data").glob("chunk-*/file-*.parquet")):
        table = pq.read_table(path, columns=["action", "timestamp", "frame_index", "episode_index", "index"])
        data = table.to_pydict()
        for idx, ep in enumerate(data["episode_index"]):
            if int(ep) != episode:
                continue
            rows.append(
                {
                    "action": data["action"][idx],
                    "timestamp": data["timestamp"][idx],
                    "frame_index": data["frame_index"][idx],
                    "index": data["index"][idx],
                }
            )
    rows.sort(key=lambda item: (int(item["frame_index"]), int(item["index"])))
    return rows


def _action_dict(names: list[str], values: Any) -> dict[str, float]:
    if len(values) != len(names):
        raise ValueError(f"action length {len(values)} does not match action names length {len(names)}")
    return {name: float(value) for name, value in zip(names, values, strict=True)}


def _return_to_init(context, *, settle_s: float, velocity_limit_rad_s: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    caller = getattr(context, "call_skill", None) or getattr(context, "call", None)
    if caller is not None:
        out["stop_motion"] = caller("stop_motion", {"reason": "play_cache_traj finished; returning to init", "settle_s": 0.0})
        init_args: dict[str, Any] = {"settle_s": settle_s, "verify": False}
        if velocity_limit_rad_s is not None:
            init_args["velocity_limit_rad_s"] = float(velocity_limit_rad_s)
        out["init_arms"] = caller("init_arms", init_args)
        return out

    try:
        context.platform.stop_motion()
        out["stop_motion"] = {"ok": True}
    except Exception as exc:
        out["stop_motion"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return out


def _optional_positive_int(value: Any, *, default: int | None = None) -> int | None:
    if value is None or value == "":
        return default
    parsed = int(value)
    if parsed <= 0:
        return None
    return parsed


def _is_dry_run(context) -> bool:
    return str(getattr(context.platform, "name", "")) == "dry_run"
