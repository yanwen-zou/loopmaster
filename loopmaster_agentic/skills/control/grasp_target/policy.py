from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np


def dispatch(context, args):
    target_prompt = _prompt(args.get("target_prompt") or args.get("prompt") or args.get("object"))
    if not target_prompt:
        return {"ok": False, "error": "target_prompt is required"}

    quantity = max(int(args.get("quantity") or 1), 1)
    execute = bool(args.get("execute", True))
    helper = _load_grasp_helper()

    delivered_count = 0
    attempts: list[dict[str, Any]] = []
    init_positions = None

    try:
        if execute:
            init_result = _call(
                context,
                "init_arms",
                {
                    "settle_s": float(args.get("init_settle_s", 2.0)),
                    "verify": bool(args.get("init_verify", True)),
                    "velocity_limit_rad_s": float(args.get("move_velocity_limit_rad_s", 0.8)),
                },
            )
            if not init_result.get("ok"):
                return _failed(
                    "init_arms failed",
                    target_prompt=target_prompt,
                    item_id=args.get("item_id"),
                    delivered_count=0,
                    attempts=[{"step": "init_arms", "result": init_result}],
                )
            init_positions = dict(init_result.get("positions") or {})

        for index in range(quantity):
            attempt = _run_one_grasp(
                context=context,
                helper=helper,
                args=args,
                target_prompt=target_prompt,
                execute=execute,
                init_positions=init_positions,
                attempt_index=index + 1,
            )
            attempts.append(attempt)
            if attempt.get("ok"):
                delivered_count += 1
                continue
            if not bool(args.get("continue_on_failure", False)):
                break
    except Exception as exc:
        return _failed(
            f"{type(exc).__name__}: {exc}",
            target_prompt=target_prompt,
            item_id=args.get("item_id"),
            delivered_count=delivered_count,
            attempts=attempts,
        )

    ok = delivered_count == quantity
    result = {
        "ok": ok,
        "target_prompt": target_prompt,
        "requested_quantity": quantity,
        "delivered_count": delivered_count,
        "delivered_items": _delivered_items(args.get("item_id"), delivered_count),
        "execute": execute,
        "attempts": attempts,
    }
    if not ok:
        result["error"] = f"delivered {delivered_count}/{quantity}"
    return result


def _run_one_grasp(
    *,
    context,
    helper,
    args: dict[str, Any],
    target_prompt: str,
    execute: bool,
    init_positions: dict[str, float] | None,
    attempt_index: int,
) -> dict[str, Any]:
    workspace = context.workspace.root
    capture = _call(
        context,
        "capture_image",
        {
            "source": "d435_rgbd",
            "camera": "d435",
            "robot_ip": str(args.get("robot_ip") or "192.168.31.22"),
            "port": int(args.get("port") or 6560),
            "topic": str(args.get("topic") or "d435_rgbd"),
            "timeout_ms": int(args.get("timeout_ms") or 3000),
            "output_dir": str(workspace / f"grasp_target_{attempt_index}_captures"),
        },
    )
    if not capture.get("ok"):
        return {"ok": False, "step": "capture_image", "result": capture}

    grounded = _call(
        context,
        "grounded_sam2",
        {
            "text_prompt": target_prompt,
            "img_path": capture["rgb"]["path"],
            "output_dir": str(workspace / f"grasp_target_{attempt_index}_grounded_sam2"),
            "box_threshold": float(args.get("grounded_sam_box_threshold", 0.4)),
            "text_threshold": float(args.get("grounded_sam_text_threshold", 0.3)),
            "force_cpu": bool(args.get("grounded_sam_force_cpu", False)),
            "local_files_only": not bool(args.get("grounded_sam_online", False)),
        },
    )
    if not grounded.get("ok") or int(grounded.get("annotation_count") or 0) <= 0:
        return {"ok": False, "step": "grounded_sam2", "result": grounded}

    helper_args = _helper_args(args, target_prompt)
    camera_params = helper._resolve_camera_params(helper_args, capture)
    detect_args = {
        "sdk_root": str(args.get("sdk_root") or _repo_root() / "third_party" / "anygrasp_sdk"),
        "color_path": capture["rgb"]["path"],
        "depth_path": capture["depth"]["path"],
        "seg_mask_path": grounded["seg_mask_path"],
        "region_object_id": int(grounded.get("anygrasp_hint", {}).get("region_object_id") or 1),
        "top_k": int(args.get("top_k") or 5),
        "depth_scale": camera_params["depth_scale"],
        "depth_trunc": float(args.get("depth_trunc", 1.0)),
        "fx": camera_params["fx"],
        "fy": camera_params["fy"],
        "cx": camera_params["cx"],
        "cy": camera_params["cy"],
        "license_interactive_sudo": bool(args.get("license_interactive_sudo", True)),
    }
    for key in ("checkpoint_path", "license_dir"):
        if args.get(key):
            detect_args[key] = str(args[key])
    if args.get("detect_down_device") is not None:
        detect_args["detect_down_device"] = list(args.get("detect_down_device") or [])
    if args.get("detect_disable_device"):
        detect_args["detect_disable_device"] = list(args.get("detect_disable_device") or [])
    if args.get("wifi_only_detect"):
        detect_args["wifi_only_detect"] = True
    if args.get("no_default_detect_down_device"):
        detect_args["no_default_detect_down_device"] = True

    detected = _call(context, "detect_grasps", detect_args)
    if not detected.get("ok") or not detected.get("grasps"):
        return {"ok": False, "step": "detect_grasps", "result": detected}

    top = detected["grasps"][0]
    t_cam_obj = helper._grasp_to_matrix(top)
    t_right_grasp = helper._camera_grasp_to_right_arm(Path(args.get("extrinsics") or helper.DEFAULT_EXTRINSICS), t_cam_obj)
    t_right_target = np.array(t_right_grasp, copy=True)
    t_right_target[0, 3] += float(args.get("pregrasp_x_offset", 0.05))
    t_right_target[2, 3] += float(args.get("pregrasp_z_offset", 0.12))
    target_rotation = str(args.get("target_rotation") or "fixed")
    if target_rotation == "fixed":
        t_right_target[:3, :3] = np.eye(3, dtype=float)

    move_args = {
        "side": "right",
        "input_frame": "arm",
        "pose": helper._move_arm_ee_pose_from_target(t_right_target, target_rotation),
        "execute": execute,
        "velocity_limit_rad_s": float(args.get("move_velocity_limit_rad_s", 0.8)),
        "orientation_cost": float(args.get("fixed_orientation_cost", 0.1)) if target_rotation == "fixed" else 0.1,
        "preserve_current_orientation": target_rotation == "fixed",
        "settle_s": float(args.get("move_settle_s", 1.0)) if execute else 0.0,
    }
    if init_positions is not None:
        move_args["current_positions"] = dict(init_positions)
        move_args["other_arm_positions"] = dict(init_positions)

    move_result = _call(context, "move_arm_ee", move_args)
    if not move_result.get("ok"):
        return {"ok": False, "step": "move_arm_ee", "result": move_result}

    sequence = []
    if execute:
        helper_args.execute_move_arm_ee = True
        helper_args.skip_grasp_sequence = False
        sequence = helper._run_grasp_sequence(
            _RegistryProxy(context),
            context,
            helper_args,
            t_right_target,
            move_result,
            init_positions,
        )

    observation = _call(context, "observe", {"include_images": True, "include_state": True})
    return {
        "ok": True,
        "step": "grasp",
        "top_grasp": top,
        "capture": _paths_only(capture),
        "grounded_sam2": _paths_only(grounded),
        "detect_grasps": {"ok": detected.get("ok"), "grasp_count": len(detected.get("grasps") or [])},
        "right_arm_target_pose": t_right_target.tolist(),
        "move_arm_ee": move_result,
        "grasp_sequence": sequence,
        "post_observe": observation,
    }


class _RegistryProxy:
    def __init__(self, context) -> None:
        self.context = context

    def dispatch(self, name: str, context, args: dict[str, Any]) -> dict[str, Any]:
        return _call(self.context, name, args)


def _call(context, name: str, args: dict[str, Any]) -> dict[str, Any]:
    caller = getattr(context, "call_skill", None) or getattr(context, "call", None)
    if caller is None:
        return {"ok": False, "error": "grasp_target requires Worker context.call_skill support"}
    return dict(caller(name, args))


def _helper_args(args: dict[str, Any], target_prompt: str) -> SimpleNamespace:
    repo = _repo_root()
    helper = _load_grasp_helper()
    return SimpleNamespace(
        grounded_sam_prompt=target_prompt,
        intrinsics_json=Path(args.get("intrinsics_json") or helper.DEFAULT_INTRINSICS),
        depth_scale=args.get("depth_scale"),
        fx=args.get("fx"),
        fy=args.get("fy"),
        cx=args.get("cx"),
        cy=args.get("cy"),
        target_rotation=str(args.get("target_rotation") or "fixed"),
        move_velocity_limit_rad_s=float(args.get("move_velocity_limit_rad_s", 0.8)),
        fixed_orientation_cost=float(args.get("fixed_orientation_cost", 0.1)),
        grasp_sequence_sleep_s=float(args.get("grasp_sequence_sleep_s", 2.0)),
        grasp_z_delta=float(args.get("grasp_z_delta", 0.05)),
        grasp_gripper_open=float(args.get("grasp_gripper_open", -5.0)),
        grasp_gripper_close=float(args.get("grasp_gripper_close", 0.0)),
    )


def _load_grasp_helper():
    path = _repo_root() / "capture_anygrasp_grasp_pose.py"
    module_name = "_loopmaster_grasp_target_helper"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load grasp helper: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _repo_root() -> Path:
    configured = os.environ.get("LOOPMASTER_REPO_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    cwd = Path.cwd().resolve()
    if (cwd / "capture_anygrasp_grasp_pose.py").exists():
        return cwd
    for parent in cwd.parents:
        if (parent / "capture_anygrasp_grasp_pose.py").exists():
            return parent
    return cwd


def _prompt(value: Any) -> str:
    text = str(value or "").strip()
    if text and not text.endswith("."):
        text += "."
    return text


def _delivered_items(item_id: Any, delivered_count: int) -> list[dict[str, Any]]:
    if item_id is None or item_id == "":
        return []
    return [{"id": item_id, "delivered": int(delivered_count)}]


def _failed(
    error: str,
    *,
    target_prompt: str,
    item_id: Any,
    delivered_count: int,
    attempts: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "ok": False,
        "error": error,
        "target_prompt": target_prompt,
        "delivered_count": delivered_count,
        "delivered_items": _delivered_items(item_id, delivered_count),
        "attempts": attempts,
    }


def _paths_only(value: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": value.get("ok")}
    for key in ("rgb", "depth"):
        item = value.get(key)
        if isinstance(item, dict) and item.get("path"):
            out[key] = {"path": item.get("path")}
    for key in ("output_dir", "seg_mask_path", "annotated_path"):
        if value.get(key):
            out[key] = value.get(key)
    return out
