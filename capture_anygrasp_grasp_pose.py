#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import json
import subprocess
import time
from pathlib import Path
from typing import Any

import numpy as np

from loopmaster_agentic.agents.workspace import new_workspace
from loopmaster_agentic.platform.dry_run import DryRunPlatform
from loopmaster_agentic.platform.hei_rebot_lift import HeiRebotLiftPlatform, HeiRebotLiftPlatformConfig
from loopmaster_agentic.skills.registry import SkillContext, SkillRegistry


REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_EXTRINSICS = REPO_ROOT / "loopmaster_agentic" / "config" / "head_camera_extrinsics.json"
DEFAULT_ARM_INIT_POSE = REPO_ROOT / "loopmaster_agentic" / "config" / "arm_init_pose.json"
DEFAULT_RUN_ROOT = REPO_ROOT / "_grasp_runs"
DEFAULT_INTRINSICS = (
    REPO_ROOT
    / "hei-rebot-lift"
    / "software"
    / "lerobot-hei-rebot-lift"
    / "src"
    / "lerobot"
    / "cameras"
    / "d435_intrinsics_640x480.json"
)
DEFAULT_DETECT_DOWN_DEVICE = "enx00e04c360914"
HARDCODE_CAMERA_POSITION = (-0.4, -0.35, 0.23)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Capture D435 RGB-D, run AnyGrasp, save top grasp as T_cam_obj, and emit a debug image."
    )
    parser.add_argument("--extrinsics", type=Path, default=DEFAULT_EXTRINSICS)
    parser.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    parser.add_argument("--robot-ip", default="192.168.31.22")
    parser.add_argument("--port", type=int, default=6560)
    parser.add_argument("--topic", default="d435_rgbd")
    parser.add_argument("--timeout-ms", type=int, default=3000)
    parser.add_argument("--rgb-path", type=Path, default=None, help="Use an existing RGB image and skip capture_image.")
    parser.add_argument("--depth-path", type=Path, default=None, help="Use an existing depth image and skip capture_image.")
    parser.add_argument("--capture-metadata-path", type=Path, default=None, help="Metadata JSON for existing RGB-D inputs.")
    parser.add_argument("--pause-before-detect", action="store_true", help="Pause after capture so network interfaces can be changed before AnyGrasp license check.")
    parser.add_argument("--wifi-only-detect", action="store_true", help="Temporarily disconnect active non-Wi-Fi devices while running AnyGrasp.")
    parser.add_argument("--detect-disable-device", action="append", default=[], help="Temporarily disconnect this NetworkManager device while running AnyGrasp.")
    parser.add_argument("--detect-down-device", action="append", default=None, help="Temporarily set this network link DOWN with sudo while running AnyGrasp.")
    parser.add_argument("--no-default-detect-down-device", action="store_true", help=f"Do not automatically down {DEFAULT_DETECT_DOWN_DEVICE} before AnyGrasp.")
    parser.add_argument("--sdk-root", type=Path, default=REPO_ROOT / "third_party" / "anygrasp_sdk")
    parser.add_argument("--checkpoint-path", type=Path, default=None)
    parser.add_argument("--license-dir", type=Path, default=None)
    parser.add_argument("--intrinsics-json", type=Path, default=DEFAULT_INTRINSICS)
    parser.add_argument("--region-mask-path", type=Path, default=None)
    parser.add_argument("--seg-mask-path", type=Path, default=None)
    parser.add_argument("--region-object-id", type=int, default=1)
    parser.add_argument(
        "--grounded-sam-prompt",
        "--text-prompt",
        dest="grounded_sam_prompt",
        default=None,
        help="Required in AnyGrasp mode. Text prompt passed to Grounded-SAM2, e.g. 'cola can.'.",
    )
    parser.add_argument("--grounded-sam-repo-root", type=Path, default=None, help="Grounded-SAM2 repo root.")
    parser.add_argument("--grounded-sam-grounding-model", default=None, help="GroundingDINO Hugging Face model id.")
    parser.add_argument("--grounded-sam-box-threshold", type=float, default=0.4)
    parser.add_argument("--grounded-sam-text-threshold", type=float, default=0.3)
    parser.add_argument("--grounded-sam-force-cpu", action="store_true")
    parser.add_argument("--grounded-sam-online", action="store_true", help="Allow Grounded-SAM2 to download/load Hugging Face files online instead of local cache only.")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--depth-scale", type=float, default=None, help="AnyGrasp depth divisor; defaults to 1/depth_scale_m.")
    parser.add_argument("--depth-trunc", type=float, default=1.0)
    parser.add_argument("--fx", type=float, default=None)
    parser.add_argument("--fy", type=float, default=None)
    parser.add_argument("--cx", type=float, default=None)
    parser.add_argument("--cy", type=float, default=None)
    parser.add_argument("--axis-length", type=float, default=0.08, help="Debug projected grasp axis length in meters.")
    parser.add_argument(
        "--hardcode",
        "--hardcode-mode",
        dest="hardcode_mode",
        action="store_true",
        help="Skip RGB-D capture and AnyGrasp; use camera-frame position [0.03, 0, 0.3] with identity rotation.",
    )
    parser.add_argument("--skip-move-arm-ee", action="store_true", help="Skip converting the grasp to a right-arm target and calling move_arm_ee.")
    parser.add_argument("--pregrasp-x-offset", type=float, default=0.05, help="Offset added to the target pose x in the right-arm base frame, in meters.")
    parser.add_argument("--pregrasp-z-offset", type=float, default=0.12, help="Offset added to the target pose z in the right-arm base frame, in meters.")
    parser.add_argument(
        "--target-rotation",
        choices=("fixed", "anygrasp"),
        default="fixed",
        help="Use fixed right-arm rotation while keeping AnyGrasp translation, or use AnyGrasp rotation.",
    )
    parser.add_argument("--execute-move-arm-ee", action="store_true", help="Actually command the right arm after IK. Default only computes IK/dry-run action.")
    parser.add_argument("--post-move-observe-s", type=float, default=1.0, help="Seconds to wait before reading right-arm positions after an executed move.")
    parser.add_argument("--move-max-joint-step", type=float, default=0.05, help="Maximum joint delta per move_arm_ee waypoint, in radians. Set <=0 to send one command.")
    parser.add_argument("--move-step-dt", type=float, default=0.08, help="Seconds between move_arm_ee joint waypoints.")
    parser.add_argument("--move-hold-s", type=float, default=0.0, help="Seconds to hold after the final move_arm_ee waypoint.")
    parser.add_argument("--fixed-orientation-cost", type=float, default=0.1, help="IK orientation cost used to preserve current EE rotation when --target-rotation fixed.")
    parser.add_argument("--skip-grasp-sequence", action="store_true", help="After reaching target, skip open/down/close/up grasp sequence.")
    parser.add_argument("--grasp-sequence-sleep-s", type=float, default=2.0, help="Seconds to sleep between grasp sequence actions.")
    parser.add_argument("--grasp-z-delta", type=float, default=0.05, help="Meters to move down/up along right-arm frame z during grasp sequence.")
    parser.add_argument("--grasp-gripper-open", type=float, default=-5, help="Right gripper open position for grasp sequence.")
    parser.add_argument("--grasp-gripper-close", type=float, default=0.0, help="Right gripper closed position for grasp sequence.")
    parser.add_argument("--arm-init-config", type=Path, default=DEFAULT_ARM_INIT_POSE, help="Joint init pose config applied to both arms before move_arm_ee.")
    parser.add_argument("--init-arm-hold-s", type=float, default=2, help="Seconds to wait after initializing both arms from arm_init_config.")
    parser.add_argument("--move-robot-id", default="hei_rebot_lift", help="Robot id used only with --execute-move-arm-ee.")
    parser.add_argument("--move-lerobot-src", type=Path, default=None, help="LeRobot source path used only with --execute-move-arm-ee.")
    args = parser.parse_args()
    if not args.hardcode_mode and not _has_text(args.grounded_sam_prompt):
        parser.error("--grounded-sam-prompt/--text-prompt is required in AnyGrasp mode")
    args.detect_down_device = _resolve_detect_down_devices(args)

    workspace = new_workspace("capture_anygrasp_grasp_pose", root=args.run_root)
    registry = SkillRegistry(include_user=False)
    context = SkillContext(platform=DryRunPlatform(), workspace=workspace)
    grounded_sam2_result = None

    if args.hardcode_mode:
        capture = None
        camera_params = None
        t_cam_obj = _hardcoded_camera_pose()
        top = _hardcoded_grasp_record(t_cam_obj)
        detected = {
            "ok": True,
            "source": {"type": "hardcode", "description": "camera-frame pose with identity rotation"},
            "grasps": [top],
        }
        debug_path = None
    else:
        if args.rgb_path is not None or args.depth_path is not None:
            if args.rgb_path is None or args.depth_path is None:
                raise ValueError("--rgb-path and --depth-path must be provided together")
            capture = _capture_from_existing_paths(args)
        else:
            capture_args = {
                "source": "d435_rgbd",
                "camera": "d435",
                "robot_ip": args.robot_ip,
                "port": args.port,
                "topic": args.topic,
                "timeout_ms": args.timeout_ms,
                "output_dir": str(workspace.root / "captures"),
            }
            capture = _dispatch_or_raise(registry, context, "capture_image", capture_args)
        context.memory["capture_image"] = capture
        camera_params = _resolve_camera_params(args, capture)
        grounded_sam2_result = _run_grounded_sam2_for_anygrasp(registry, context, args, workspace, capture)
        if args.pause_before_detect:
            print("Captured RGB-D. You may now change network interfaces before AnyGrasp runs.")
            print(f"rgb_path={capture['rgb']['path']}")
            print(f"depth_path={capture['depth']['path']}")
            input("Press Enter to run detect_grasps...")

        grasp_args: dict[str, Any] = {
            "sdk_root": str(args.sdk_root),
            "color_path": capture["rgb"]["path"],
            "depth_path": capture["depth"]["path"],
            "top_k": args.top_k,
            "depth_scale": camera_params["depth_scale"],
            "depth_trunc": args.depth_trunc,
            "fx": camera_params["fx"],
            "fy": camera_params["fy"],
            "cx": camera_params["cx"],
            "cy": camera_params["cy"],
        }
        if args.checkpoint_path is not None:
            grasp_args["checkpoint_path"] = str(args.checkpoint_path)
        if args.license_dir is not None:
            grasp_args["license_dir"] = str(args.license_dir)
        if args.region_mask_path is not None:
            grasp_args["region_mask_path"] = str(args.region_mask_path)
        grasp_args["seg_mask_path"] = grounded_sam2_result["seg_mask_path"]
        grasp_args["region_object_id"] = grounded_sam2_result["anygrasp_hint"]["region_object_id"]

        with _detect_network_context(args):
            detected = _dispatch_or_raise(registry, context, "detect_grasps", grasp_args)
        if not detected.get("grasps"):
            raise RuntimeError(f"AnyGrasp returned no grasps; full result written under {workspace.root}")

        top = detected["grasps"][0]
        t_cam_obj = _grasp_to_matrix(top)
        debug_path = workspace.root / "top_grasp_debug.png"
        _write_debug_image(
            Path(capture["rgb"]["path"]),
            debug_path,
            t_cam_obj,
            fx=camera_params["fx"],
            fy=camera_params["fy"],
            cx=camera_params["cx"],
            cy=camera_params["cy"],
            axis_length=args.axis_length,
        )
        _write_extrinsics_grasp(args.extrinsics, t_cam_obj, top, capture, detected, debug_path, camera_params)

    move_arm_ee_result = None
    post_move_arm_positions = None
    init_arm_positions = None
    init_arm_results = []
    move_context = context
    move_platform = None
    move_arm_ee_args: dict[str, Any] = {}
    move_arm_ee_error = None
    grasp_sequence_results = []
    try:
        if args.execute_move_arm_ee:
            move_platform = HeiRebotLiftPlatform(
                HeiRebotLiftPlatformConfig(
                    remote_ip=args.robot_ip,
                    robot_id=args.move_robot_id,
                    lerobot_src=args.move_lerobot_src,
                )
            )
            move_platform.connect()
            move_context = SkillContext(platform=move_platform, workspace=workspace)
            init_arm_positions = _load_arm_init_pose(args.arm_init_config)
            init_arm_results = _initialize_arms_from_init_pose(
                registry,
                move_context,
                init_arm_positions,
            )
            print(f"Initialized both arms from {args.arm_init_config}; waiting {args.init_arm_hold_s}s before moving right arm...")
            if args.init_arm_hold_s > 0.0:
                time.sleep(float(args.init_arm_hold_s))

        t_right_grasp = _camera_grasp_to_right_arm(args.extrinsics, t_cam_obj)
        t_right_target = np.array(t_right_grasp, copy=True)
        if not args.hardcode_mode:
            t_right_target[0, 3] += float(args.pregrasp_x_offset)
            t_right_target[2, 3] += float(args.pregrasp_z_offset)
        if args.target_rotation == "fixed":
            t_right_target[:3, :3] = np.eye(3, dtype=float)
        target_pose = _move_arm_ee_pose_from_target(t_right_target, args.target_rotation)
        move_arm_ee_args = {
            "side": "right",
            "input_frame": "arm",
            "pose": target_pose,
            "execute": bool(args.execute_move_arm_ee),
            "max_joint_step": float(args.move_max_joint_step),
            "step_dt": float(args.move_step_dt),
            "hold_s": float(args.move_hold_s),
            "orientation_cost": float(args.fixed_orientation_cost) if args.target_rotation == "fixed" else 0.1,
            "preserve_current_orientation": args.target_rotation == "fixed",
        }
        if init_arm_positions is not None:
            move_arm_ee_args["current_positions"] = dict(init_arm_positions)
            move_arm_ee_args["other_arm_positions"] = dict(init_arm_positions)

        if not args.skip_move_arm_ee:
            move_arm_ee_result = _dispatch_with_trace(registry, move_context, "move_arm_ee", move_arm_ee_args)
            if not move_arm_ee_result.get("ok"):
                move_arm_ee_error = _format_skill_error("move_arm_ee", move_arm_ee_result, workspace.root)
            elif args.execute_move_arm_ee:
                if not args.skip_grasp_sequence:
                    try:
                        grasp_sequence_results = _run_grasp_sequence(
                            registry,
                            move_context,
                            args,
                            t_right_target,
                            move_arm_ee_result,
                            init_arm_positions,
                        )
                    except RuntimeError as exc:
                        move_arm_ee_error = str(exc)
                if hasattr(move_context.platform, "read_arm_positions"):
                    time.sleep(max(float(args.post_move_observe_s), 0.0))
                    post_move_arm_positions = dict(move_context.platform.read_arm_positions("right"))
    finally:
        if move_platform is not None:
            move_platform.close()

    summary = {
        "workspace": str(workspace.root),
        "mode": "hardcode" if args.hardcode_mode else "anygrasp",
        "rgb_path": capture["rgb"]["path"] if capture is not None else None,
        "depth_path": capture["depth"]["path"] if capture is not None else None,
        "debug_image_path": str(debug_path) if debug_path is not None else None,
        "extrinsics_path": str(args.extrinsics),
        "camera_to_object": t_cam_obj.tolist(),
        "right_arm_grasp_pose": t_right_grasp.tolist(),
        "right_arm_target_pose": t_right_target.tolist(),
        "right_arm_target_x_offset_m": 0.0 if args.hardcode_mode else float(args.pregrasp_x_offset),
        "right_arm_target_z_offset_m": 0.0 if args.hardcode_mode else float(args.pregrasp_z_offset),
        "target_rotation": args.target_rotation,
        "move_arm_ee_args": move_arm_ee_args,
        "arm_init_config": str(args.arm_init_config),
        "init_arm_positions": init_arm_positions,
        "init_arm_results": init_arm_results,
        "move_arm_ee_result": move_arm_ee_result,
        "move_arm_ee_error": move_arm_ee_error,
        "grasp_sequence_results": grasp_sequence_results,
        "post_move_arm_positions": post_move_arm_positions,
        "camera_params": camera_params,
        "top_grasp": top,
        "grounded_sam2_result": grounded_sam2_result,
    }
    summary_path = workspace.root / "grasp_pose_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    if move_arm_ee_error:
        raise RuntimeError(move_arm_ee_error)
    return 0


def _dispatch_or_raise(
    registry: SkillRegistry,
    context: SkillContext,
    name: str,
    args: dict[str, Any],
) -> dict[str, Any]:
    result = _dispatch_with_trace(registry, context, name, args)
    if not result.get("ok"):
        raise RuntimeError(_format_skill_error(name, result, context.workspace.root))
    return result


def _dispatch_with_trace(
    registry: SkillRegistry,
    context: SkillContext,
    name: str,
    args: dict[str, Any],
) -> dict[str, Any]:
    result = registry.dispatch(name, context, args)
    trace = {"skill": name, "args": args, "result": result, "time_s": time.time()}
    context.workspace.append_trace(trace)
    return result


def _load_arm_init_pose(path: Path) -> dict[str, float]:
    data = json.loads(path.read_text(encoding="utf-8"))
    raw = data.get("positions", data)
    out: dict[str, float] = {}
    for key, value in dict(raw).items():
        joint = str(key)
        for prefix in ("right_", "left_"):
            if joint.startswith(prefix):
                joint = joint[len(prefix) :]
        if joint.endswith(".pos"):
            joint = joint[:-4]
        if joint in {"joint_1", "joint_2", "joint_3", "joint_4", "joint_5", "joint_6", "gripper"}:
            out[joint] = float(value)
    missing = [joint for joint in ("joint_1", "joint_2", "joint_3", "joint_4", "joint_5", "joint_6", "gripper") if joint not in out]
    if missing:
        raise RuntimeError(f"failed to read complete arm init pose from {path}; missing={missing}, raw={raw}")
    return out


def _initialize_arms_from_init_pose(
    registry: SkillRegistry,
    context: SkillContext,
    init_positions: dict[str, float],
) -> list[dict[str, Any]]:
    results = []
    args = {"side": "both", "positions": dict(init_positions)}
    result = _dispatch_with_trace(registry, context, "move_arm_joints", args)
    results.append({"side": "both", "args": args, "result": result})
    if not result.get("ok"):
        raise RuntimeError(_format_skill_error("move_arm_joints", result, context.workspace.root))
    return results


def _run_grounded_sam2_for_anygrasp(
    registry: SkillRegistry,
    context: SkillContext,
    args: argparse.Namespace,
    workspace: Any,
    capture: dict[str, Any],
) -> dict[str, Any]:
    grounded_args: dict[str, Any] = {
        "text_prompt": str(args.grounded_sam_prompt),
        "img_path": capture["rgb"]["path"],
        "output_dir": str(workspace.root / "grounded_sam2"),
        "box_threshold": float(args.grounded_sam_box_threshold),
        "text_threshold": float(args.grounded_sam_text_threshold),
        "force_cpu": bool(args.grounded_sam_force_cpu),
        "local_files_only": not bool(args.grounded_sam_online),
    }
    if args.grounded_sam_repo_root is not None:
        grounded_args["repo_root"] = str(args.grounded_sam_repo_root)
    if args.grounded_sam_grounding_model:
        grounded_args["grounding_model"] = str(args.grounded_sam_grounding_model)

    result = _dispatch_or_raise(registry, context, "grounded_sam2", grounded_args)
    if int(result.get("annotation_count") or 0) <= 0:
        raise RuntimeError(
            f"Grounded-SAM2 found no objects for prompt {args.grounded_sam_prompt!r}; "
            f"outputs written under {result.get('output_dir')}"
        )
    seg_mask_path = result.get("seg_mask_path")
    region_object_id = result.get("anygrasp_hint", {}).get("region_object_id")
    if not seg_mask_path or region_object_id is None:
        raise RuntimeError(f"Grounded-SAM2 did not produce an AnyGrasp mask hint; result={result}")
    return result


def _run_grasp_sequence(
    registry: SkillRegistry,
    context: SkillContext,
    args: argparse.Namespace,
    t_right_target: np.ndarray,
    move_arm_ee_result: dict[str, Any],
    init_arm_positions: dict[str, float] | None,
) -> list[dict[str, Any]]:
    sleep_s = max(float(args.grasp_sequence_sleep_s), 0.0)
    right_positions = dict(move_arm_ee_result.get("positions") or {})
    if not right_positions:
        raise RuntimeError("cannot run grasp sequence: move_arm_ee_result has no positions")
    left_positions = dict(init_arm_positions or {})
    if not left_positions:
        left_positions = dict(right_positions)

    results: list[dict[str, Any]] = []

    open_positions = dict(right_positions)
    open_positions["gripper"] = float(args.grasp_gripper_open)
    open_result = _command_both_arms(
        registry,
        context,
        right=open_positions,
        left=left_positions,
        label="open_gripper",
    )
    results.append(open_result)
    if sleep_s > 0.0:
        time.sleep(sleep_s)

    down_target = np.array(t_right_target, copy=True)
    down_target[2, 3] -= float(args.grasp_z_delta)
    down_result = _move_right_to_target(
        registry,
        context,
        args,
        down_target,
        current_positions=open_positions,
        other_arm_positions=left_positions,
        label="move_down_z",
    )
    results.append(down_result)
    right_positions = dict(down_result["result"].get("positions") or open_positions)
    if sleep_s > 0.0:
        time.sleep(sleep_s)

    close_positions = dict(right_positions)
    close_positions["gripper"] = float(args.grasp_gripper_close)
    close_result = _command_both_arms(
        registry,
        context,
        right=close_positions,
        left=left_positions,
        label="close_gripper",
    )
    results.append(close_result)
    if sleep_s > 0.0:
        time.sleep(sleep_s)

    up_target = np.array(t_right_target, copy=True)
    up_result = _move_right_to_target(
        registry,
        context,
        args,
        up_target,
        current_positions=close_positions,
        other_arm_positions=left_positions,
        label="move_up_z",
    )
    results.append(up_result)
    return results


def _command_both_arms(
    registry: SkillRegistry,
    context: SkillContext,
    *,
    right: dict[str, float],
    left: dict[str, float],
    label: str,
) -> dict[str, Any]:
    call_args = {"side": "both", "positions": {"right": dict(right), "left": dict(left)}}
    result = _dispatch_with_trace(registry, context, "move_arm_joints", call_args)
    item = {"step": label, "skill": "move_arm_joints", "args": call_args, "result": result}
    if not result.get("ok"):
        raise RuntimeError(_format_skill_error("move_arm_joints", result, context.workspace.root))
    return item


def _move_right_to_target(
    registry: SkillRegistry,
    context: SkillContext,
    args: argparse.Namespace,
    target: np.ndarray,
    *,
    current_positions: dict[str, float],
    other_arm_positions: dict[str, float],
    label: str,
) -> dict[str, Any]:
    call_args = {
        "side": "right",
        "input_frame": "arm",
        "pose": _move_arm_ee_pose_from_target(target, args.target_rotation),
        "execute": True,
        "max_joint_step": float(args.move_max_joint_step),
        "step_dt": float(args.move_step_dt),
        "hold_s": float(args.move_hold_s),
        "orientation_cost": float(args.fixed_orientation_cost) if args.target_rotation == "fixed" else 0.1,
        "preserve_current_orientation": args.target_rotation == "fixed",
        "current_positions": dict(current_positions),
        "other_arm_positions": dict(other_arm_positions),
    }
    result = _dispatch_with_trace(registry, context, "move_arm_ee", call_args)
    item = {"step": label, "skill": "move_arm_ee", "args": call_args, "result": result}
    if not result.get("ok"):
        raise RuntimeError(_format_skill_error("move_arm_ee", result, context.workspace.root))
    return item


def _has_text(value: Any) -> bool:
    return bool(str(value or "").strip())


def _capture_from_existing_paths(args: argparse.Namespace) -> dict[str, Any]:
    metadata = {}
    if args.capture_metadata_path is not None:
        metadata = json.loads(args.capture_metadata_path.read_text(encoding="utf-8"))
    return {
        "ok": True,
        "captured": True,
        "camera": "d435",
        "source": "existing_rgbd",
        "metadata": metadata,
        "rgb": {"path": str(args.rgb_path)},
        "depth": {
            "path": str(args.depth_path),
            "depth_scale_m": float(metadata.get("depth_scale_m", 0.001)),
        },
    }


def _resolve_detect_down_devices(args: argparse.Namespace) -> list[str]:
    requested = list(args.detect_down_device or [])
    if args.no_default_detect_down_device:
        return requested
    if DEFAULT_DETECT_DOWN_DEVICE not in requested and _network_device_exists(DEFAULT_DETECT_DOWN_DEVICE):
        requested.insert(0, DEFAULT_DETECT_DOWN_DEVICE)
    return requested


def _network_device_exists(device: str) -> bool:
    return _run(["ip", "link", "show", device], check=False).returncode == 0


@contextlib.contextmanager
def _detect_network_context(args: argparse.Namespace):
    down_devices = list(dict.fromkeys(device for device in (args.detect_down_device or []) if device))
    devices = list(args.detect_disable_device or [])
    if args.wifi_only_detect:
        devices.extend(_active_non_wifi_devices())
    devices = list(dict.fromkeys(device for device in devices if device))
    active = [_active_connection_for_device(device) for device in devices]
    down_active = [_active_connection_for_device(device) for device in down_devices]
    try:
        for device in devices:
            print(f"Temporarily disconnecting {device} for AnyGrasp feature-id check...")
            _run(["nmcli", "dev", "disconnect", device], check=False)
        for device in down_devices:
            print(f"Temporarily setting {device} DOWN for AnyGrasp feature-id check...")
            _run_interactive(["sudo", "ip", "link", "set", "dev", device, "down"], check=True)
        if devices or down_devices:
            time.sleep(2.0)
        yield
    finally:
        for device, item in zip(down_devices, down_active, strict=False):
            _restore_network_device(device, item)
        for item in active:
            if not item:
                continue
            _restore_network_device(item["device"], item)
        if active or down_active:
            time.sleep(2.0)


def _restore_network_device(device: str, connection: dict[str, str] | None) -> None:
    print(f"Restoring network device {device}...")
    _run_and_report(["sudo", "ip", "link", "set", "dev", device, "up"], interactive=True)
    _run_and_report(["nmcli", "dev", "connect", device])
    if connection:
        for attempt in range(1, 4):
            print(f"Restoring {device} via {connection['name']} attempt {attempt}/3...")
            _run_and_report(["nmcli", "con", "up", "uuid", connection["uuid"]])
            if _wait_for_device_state(device, "connected", timeout_s=8.0):
                return
    if not _wait_for_link_up(device, timeout_s=5.0):
        print(f"WARNING: {device} did not return to link UP; run `nmcli con up uuid {connection['uuid']}` manually" if connection else f"WARNING: {device} did not return to link UP")


def _wait_for_device_state(device: str, desired: str, *, timeout_s: float) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        state = _nmcli_device_state(device)
        if state == desired:
            return True
        time.sleep(0.5)
    print(f"WARNING: {device} state is {_nmcli_device_state(device)!r}, expected {desired!r}")
    return False


def _wait_for_link_up(device: str, *, timeout_s: float) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        out = _run_text(["ip", "-brief", "link", "show", device], check=False)
        if " UP " in f" {out} " or "<" in out and ",UP," in out:
            return True
        time.sleep(0.5)
    return False


def _nmcli_device_state(device: str) -> str:
    out = _run_text(["nmcli", "-t", "-f", "DEVICE,STATE", "dev", "status"], check=False)
    for line in out.splitlines():
        parts = line.split(":", 1)
        if len(parts) == 2 and parts[0] == device:
            return parts[1]
    return ""


def _active_non_wifi_devices() -> list[str]:
    out = _run_text(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "dev", "status"], check=False)
    devices = []
    for line in out.splitlines():
        parts = line.split(":")
        if len(parts) < 3:
            continue
        device, device_type, state = parts[:3]
        if state == "connected" and device_type not in {"wifi", "loopback"}:
            devices.append(device)
    return devices


def _active_connection_for_device(device: str) -> dict[str, str] | None:
    out = _run_text(["nmcli", "-t", "-f", "NAME,UUID,TYPE,DEVICE", "con", "show", "--active"], check=False)
    for line in out.splitlines():
        name, uuid, conn_type, conn_device = (line.split(":", 3) + ["", "", "", ""])[:4]
        if conn_device == device:
            return {"name": name, "uuid": uuid, "type": conn_type, "device": conn_device}
    return None


def _run_text(cmd: list[str], *, check: bool = True) -> str:
    return _run(cmd, check=check).stdout


def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, check=check)


def _run_interactive(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, check=check)


def _run_and_report(cmd: list[str], *, interactive: bool = False) -> subprocess.CompletedProcess[str]:
    if interactive:
        completed = subprocess.run(cmd, text=True, check=False)
    else:
        completed = subprocess.run(cmd, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        print(f"WARNING: command failed ({completed.returncode}): {' '.join(cmd)}")
        stderr = getattr(completed, "stderr", None)
        stdout = getattr(completed, "stdout", None)
        if stdout:
            print(stdout.strip())
        if stderr:
            print(stderr.strip())
    return completed


def _format_skill_error(name: str, result: dict[str, Any], workspace_root: Path) -> str:
    lines = [
        f"{name} failed: {result.get('error') or result.get('reason') or 'unknown error'}",
        f"trace: {workspace_root / 'trace.jsonl'}",
    ]
    missing = result.get("missing")
    if missing:
        lines.append(f"missing: {missing}")
    status = result.get("status")
    if isinstance(status, dict):
        for key in ("python", "feature_id", "license_dir", "license_ok", "checkpoint_path", "checkpoint_exists"):
            if key in status:
                lines.append(f"{key}: {status[key]}")
        deps = status.get("dependencies")
        if isinstance(deps, dict):
            bad = {
                module: info.get("error", "not ok")
                for module, info in deps.items()
                if isinstance(info, dict) and not info.get("ok")
            }
            if bad:
                lines.append(f"bad_dependencies: {bad}")
    return "\n".join(lines)


def _resolve_camera_params(args: argparse.Namespace, capture: dict[str, Any]) -> dict[str, Any]:
    metadata = capture.get("metadata") if isinstance(capture.get("metadata"), dict) else {}
    intrinsics = metadata.get("intrinsics") if isinstance(metadata.get("intrinsics"), dict) else None
    source = "capture_metadata"
    if intrinsics is None:
        intrinsics = _load_intrinsics_json(args.intrinsics_json)
        source = str(args.intrinsics_json)

    color = intrinsics.get("color_intrinsics") if isinstance(intrinsics.get("color_intrinsics"), dict) else intrinsics
    depth_scale_m = (
        metadata.get("depth_scale_m")
        or capture.get("depth", {}).get("depth_scale_m")
        or intrinsics.get("depth_scale_m")
        or 0.001
    )
    depth_scale = float(args.depth_scale) if args.depth_scale is not None else 1.0 / float(depth_scale_m)
    return {
        "source": source,
        "fx": float(args.fx) if args.fx is not None else float(color["fx"]),
        "fy": float(args.fy) if args.fy is not None else float(color["fy"]),
        "cx": float(args.cx) if args.cx is not None else float(color.get("cx", color.get("ppx"))),
        "cy": float(args.cy) if args.cy is not None else float(color.get("cy", color.get("ppy"))),
        "depth_scale": depth_scale,
        "depth_scale_m": float(depth_scale_m),
        "width": int(color.get("width", metadata.get("width", 0)) or 0),
        "height": int(color.get("height", metadata.get("height", 0)) or 0),
    }


def _load_intrinsics_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"intrinsics JSON not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _grasp_to_matrix(grasp: dict[str, Any]) -> np.ndarray:
    translation = np.asarray(grasp.get("translation"), dtype=float).reshape(3)
    rotation = np.asarray(grasp.get("rotation_matrix"), dtype=float).reshape(3, 3)
    t_cam_obj = np.eye(4, dtype=float)
    t_cam_obj[:3, :3] = rotation
    t_cam_obj[:3, 3] = translation
    return t_cam_obj


def _hardcoded_camera_pose() -> np.ndarray:
    t_cam_obj = np.eye(4, dtype=float)
    t_cam_obj[:3, 3] = np.asarray(HARDCODE_CAMERA_POSITION, dtype=float)
    return t_cam_obj


def _hardcoded_grasp_record(t_cam_obj: np.ndarray) -> dict[str, Any]:
    return {
        "index": 0,
        "score": None,
        "width": None,
        "height": None,
        "depth": None,
        "translation": t_cam_obj[:3, 3].tolist(),
        "rotation_matrix": t_cam_obj[:3, :3].tolist(),
        "object_id": None,
        "source": "hardcode",
    }


def _camera_grasp_to_right_arm(extrinsics_path: Path, t_cam_obj: np.ndarray) -> np.ndarray:
    extrinsics = json.loads(extrinsics_path.read_text(encoding="utf-8"))
    t_right_cam = _arm_to_camera_transform(extrinsics, "right")
    return t_right_cam @ t_cam_obj


def _move_arm_ee_pose_from_target(t_right_target: np.ndarray, target_rotation: str) -> dict[str, Any]:
    position = t_right_target[:3, 3].tolist()
    if target_rotation == "fixed":
        return {"position": position}
    return {"matrix": t_right_target.tolist()}


def _arm_to_camera_transform(extrinsics: dict[str, Any], side: str) -> np.ndarray:
    transforms = extrinsics["transforms"][side]
    raw = transforms.get("arm_to_camera", transforms.get("camera_to_arm"))
    if raw is None:
        raise KeyError(f"head-camera extrinsics missing arm_to_camera transform for side={side}")
    mat = np.asarray(raw, dtype=float)
    if mat.shape != (4, 4):
        raise ValueError(f"expected {side} arm_to_camera to be 4x4, got shape={mat.shape}")
    if not np.allclose(mat[3], [0.0, 0.0, 0.0, 1.0]):
        raise ValueError(f"expected homogeneous matrix last row [0, 0, 0, 1], got {mat[3].tolist()}")
    return mat


def _write_debug_image(
    rgb_path: Path,
    output_path: Path,
    t_cam_obj: np.ndarray,
    *,
    fx: float,
    fy: float,
    cx: float,
    cy: float,
    axis_length: float,
) -> None:
    import cv2

    image = cv2.imread(str(rgb_path), cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError(f"failed to read RGB image for debug overlay: {rgb_path}")
    origin = t_cam_obj[:3, 3]
    axes = [
        (t_cam_obj[:3, 0] * axis_length, (0, 0, 255), "+x"),
        (t_cam_obj[:3, 1] * axis_length, (0, 255, 0), "+y"),
        (t_cam_obj[:3, 2] * axis_length, (255, 0, 0), "+z"),
    ]
    origin_px = _project(origin, fx=fx, fy=fy, cx=cx, cy=cy)
    if origin_px is not None:
        cv2.circle(image, origin_px, 5, (255, 255, 255), -1, lineType=cv2.LINE_AA)
        cv2.putText(
            image,
            "top grasp",
            (origin_px[0] + 8, origin_px[1] - 8),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )
    for vector, color, label in axes:
        endpoint_px = _project(origin + vector, fx=fx, fy=fy, cx=cx, cy=cy)
        if origin_px is None or endpoint_px is None:
            continue
        cv2.arrowedLine(image, origin_px, endpoint_px, color, 3, line_type=cv2.LINE_AA, tipLength=0.18)
        cv2.putText(
            image,
            label,
            (endpoint_px[0] + 4, endpoint_px[1] + 4),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.45,
            color,
            2,
            cv2.LINE_AA,
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(output_path), image):
        raise RuntimeError(f"failed to write debug image: {output_path}")


def _project(point: np.ndarray, *, fx: float, fy: float, cx: float, cy: float) -> tuple[int, int] | None:
    z = float(point[2])
    if z <= 1e-6:
        return None
    u = int(round(float(point[0]) / z * fx + cx))
    v = int(round(float(point[1]) / z * fy + cy))
    return (u, v)


def _write_extrinsics_grasp(
    path: Path,
    t_cam_obj: np.ndarray,
    top_grasp: dict[str, Any],
    capture: dict[str, Any],
    detected: dict[str, Any],
    debug_path: Path,
    camera_params: dict[str, Any],
) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("objects", {})
    data["objects"]["grasp_pose"] = {
        "camera_to_object": t_cam_obj.tolist(),
        "source": "capture_anygrasp_grasp_pose.py",
        "updated_at_s": time.time(),
        "debug_image_path": str(debug_path),
        "rgb_path": capture["rgb"]["path"],
        "depth_path": capture["depth"]["path"],
        "score": top_grasp.get("score"),
        "width": top_grasp.get("width"),
        "height": top_grasp.get("height"),
        "depth": top_grasp.get("depth"),
        "camera_params": camera_params,
        "anygrasp_source": detected.get("source", {}),
    }
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
