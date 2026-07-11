from __future__ import annotations

import json
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any


ARM_JOINTS = ("joint_1", "joint_2", "joint_3", "joint_4", "joint_5", "joint_6", "gripper")
DEFAULT_ARM_QPOS = (0.0, -0.5, -0.5, 0.0, 0.0, 0.0, 0.0)
HEAD_CAMERA_EXTRINSICS_PATH = Path(__file__).resolve().parents[1] / "config" / "head_camera_extrinsics.json"
MIN_ARM_TARGET_Z = 0.06


@dataclass(frozen=True)
class MinkIkResult:
    side: str
    target_arm_pose: list[list[float]]
    target_camera_pose: list[list[float]] | None
    positions: dict[str, float]
    ik_success: bool
    ik_info: dict[str, Any]
    transform: dict[str, list[list[float]]] | None


def solve_arm_ee_mink(
    *,
    side: str,
    pose: Any,
    input_frame: str = "head_camera",
    current_positions: dict[str, float] | list[float] | tuple[float, ...] | None = None,
    gripper: float | None = None,
    orientation_cost: float = 0.1,
    preserve_current_orientation: bool = False,
) -> MinkIkResult:
    np, mujoco, mink = _deps()
    side = _normalize_side(side)
    target_input = _pose_to_matrix(pose)
    target_camera_pose = None
    transform_info = None
    if input_frame.lower() in {"head", "head_camera", "camera", "front", "front_camera"}:
        target_camera_pose = target_input
        t_arm_cam = _arm_to_head_camera_transform(side)
        t_cam_arm = _matrix_inverse_rigid(t_arm_cam)
        target_arm = _matrix_multiply(t_arm_cam, target_input)
        transform_info = {"head_camera_to_arm": t_cam_arm, "arm_to_head_camera": t_arm_cam}
    elif input_frame.lower() in {"arm", "arm_base", "base", f"{side}_arm"}:
        target_arm = target_input
    else:
        raise ValueError("input_frame must be head_camera, left_arm, or right_arm")
    target_arm_unclipped = [row[:] for row in target_arm]
    target_arm_clipped = _clip_arm_target_z(target_arm)

    sim = _sim()
    q = sim.model.qpos0.copy()
    side_q = _side_q_from_positions(current_positions)
    if gripper is not None:
        side_q[6] = float(gripper)
    for name, value in zip(_side_joint_names(side), side_q, strict=True):
        q[sim.joint_qposadr[name]] = float(value)
    configuration = mink.Configuration(sim.model, q=q)

    if preserve_current_orientation:
        current_arm_pose = _current_ee_pose_arm(sim, configuration, side)
        for row in range(3):
            for col in range(3):
                target_arm[row][col] = current_arm_pose[row][col]

    target_world = _arm_pose_to_world(side, target_arm)
    target = mink.SE3(
        wxyz_xyz=np.concatenate(
            [_matrix_to_wxyz(np.asarray(target_world, dtype=float)), np.asarray([target_world[0][3], target_world[1][3], target_world[2][3]], dtype=float)]
        )
    )
    frame_task = mink.FrameTask(
        frame_name="right_end_link" if side == "right" else "left_end_link",
        frame_type="body",
        position_cost=1.0,
        orientation_cost=max(float(orientation_cost), 0.0),
        gain=0.8,
        lm_damping=1e-3,
    )
    frame_task.set_target(target)
    posture_task = mink.PostureTask(sim.model, cost=1e-3)
    posture_task.set_target(q)
    tasks = [frame_task, posture_task]

    dt = 0.05
    solver = _select_solver()
    last_error = float("inf")
    iterations = 0
    for iterations in range(1, 81):
        velocity = mink.solve_ik(
            configuration,
            tasks,
            dt,
            solver=solver,
            damping=1e-4,
            safety_break=False,
        )
        configuration.integrate_inplace(velocity, dt)
        error = frame_task.compute_error(configuration)
        last_error = float(np.linalg.norm(error[:3]))
        if last_error < 0.01:
            break

    q_out = configuration.q
    positions = {
        joint: _clamp(float(q_out[sim.joint_qposadr[name]]), *sim.limits[name])
        for joint, name in zip(ARM_JOINTS[:6], _side_joint_names(side)[:6], strict=True)
    }
    positions["gripper"] = float(gripper) if gripper is not None else float(side_q[6])

    for name, joint in zip(_side_joint_names(side), ARM_JOINTS, strict=True):
        configuration.data.qpos[sim.joint_qposadr[name]] = positions[joint]
    configuration.update()
    body_id = mujoco.mj_name2id(sim.model, mujoco.mjtObj.mjOBJ_BODY, "right_end_link" if side == "right" else "left_end_link")
    reached = [float(v) for v in configuration.data.xpos[body_id]]
    target_pos = [float(target_world[0][3]), float(target_world[1][3]), float(target_world[2][3])]
    pos_error = math.sqrt(sum((a - b) ** 2 for a, b in zip(reached, target_pos, strict=True)))
    return MinkIkResult(
        side=side,
        target_arm_pose=target_arm,
        target_camera_pose=target_camera_pose,
        positions=positions,
        ik_success=pos_error < 0.03,
        ik_info={
            "backend": "mink",
            "solver": solver,
            "iterations": iterations,
            "position_error_m": pos_error,
            "task_position_error_m": last_error,
            "orientation_cost": max(float(orientation_cost), 0.0),
            "preserve_current_orientation": bool(preserve_current_orientation),
            "arm_target_z_min_m": MIN_ARM_TARGET_Z,
            "arm_target_z_clipped": target_arm_clipped,
            "unclipped_arm_position": [
                float(target_arm_unclipped[0][3]),
                float(target_arm_unclipped[1][3]),
                float(target_arm_unclipped[2][3]),
            ],
            "clipped_arm_position": [
                float(target_arm[0][3]),
                float(target_arm[1][3]),
                float(target_arm[2][3]),
            ],
            "target_world_position": target_pos,
            "reached_world_position": reached,
        },
        transform=transform_info,
    )


@dataclass(frozen=True)
class _MinkSim:
    model: Any
    joint_qposadr: dict[str, int]
    limits: dict[str, tuple[float, float]]


@lru_cache(maxsize=1)
def _sim() -> _MinkSim:
    _, mujoco, _ = _deps()
    model = mujoco.MjModel.from_xml_path(str(_model_xml_path()))
    joint_qposadr: dict[str, int] = {}
    limits: dict[str, tuple[float, float]] = {}
    for jid in range(model.njnt):
        name = mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_JOINT, jid)
        if not name:
            continue
        joint_qposadr[name] = int(model.jnt_qposadr[jid])
        limits[name] = (float(model.jnt_range[jid][0]), float(model.jnt_range[jid][1]))
    return _MinkSim(model=model, joint_qposadr=joint_qposadr, limits=limits)


def _deps():
    try:
        import numpy as np
        import mujoco
        import mink
    except ImportError as exc:
        raise ImportError("mink, mujoco, and numpy are required for move_arm_ee mink IK.") from exc
    return np, mujoco, mink


def _select_solver() -> str:
    try:
        import qpsolvers

        installed = set(qpsolvers.available_solvers)
    except Exception:
        installed = set()
    for candidate in ("daqp", "quadprog", "osqp", "clarabel", "scs"):
        if candidate in installed:
            return candidate
    return "daqp"


def _model_xml_path() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "hei-rebot-lift"
        / "software"
        / "lerobot-hei-rebot-lift"
        / "examples"
        / "hei_rebot_lift"
        / "VR_mujoco_ik"
        / "mujoco_ik"
        / "model"
        / "reBot_description"
        / "urdf"
        / "reBot_dual_with_gripper_scene.xml"
    )


def _side_joint_names(side: str) -> list[str]:
    prefix = "right_joint" if side == "right" else "left_joint"
    return [f"{prefix}{idx}" for idx in range(1, 7)] + [f"{side}_gripper_joint"]


def _arm_pose_to_world(side: str, pose_arm: list[list[float]]) -> list[list[float]]:
    offset_y = -0.23 if side == "right" else 0.23
    t_world_arm = _identity()
    t_world_arm[1][3] = offset_y
    return _matrix_multiply(t_world_arm, pose_arm)


def _clip_arm_target_z(pose_arm: list[list[float]]) -> bool:
    if float(pose_arm[2][3]) >= MIN_ARM_TARGET_Z:
        return False
    pose_arm[2][3] = MIN_ARM_TARGET_Z
    return True


def _current_ee_pose_arm(sim: _MinkSim, configuration: Any, side: str) -> list[list[float]]:
    np, mujoco, _ = _deps()
    configuration.update()
    body_id = mujoco.mj_name2id(sim.model, mujoco.mjtObj.mjOBJ_BODY, _end_link_name(side))
    t_world_ee = _identity()
    rot = np.asarray(configuration.data.xmat[body_id], dtype=float).reshape(3, 3)
    pos = np.asarray(configuration.data.xpos[body_id], dtype=float).reshape(3)
    for row in range(3):
        for col in range(3):
            t_world_ee[row][col] = float(rot[row, col])
        t_world_ee[row][3] = float(pos[row])
    return _matrix_multiply(_matrix_inverse_rigid(_arm_pose_to_world(side, _identity())), t_world_ee)


def _end_link_name(side: str) -> str:
    return "right_end_link" if side == "right" else "left_end_link"


def _side_q_from_positions(current_positions: Any) -> list[float]:
    if current_positions is None:
        return list(DEFAULT_ARM_QPOS)
    if isinstance(current_positions, dict):
        return [
            float(current_positions.get("joint_1", current_positions.get("joint_1.pos", DEFAULT_ARM_QPOS[0]))),
            float(current_positions.get("joint_2", current_positions.get("joint_2.pos", DEFAULT_ARM_QPOS[1]))),
            float(current_positions.get("joint_3", current_positions.get("joint_3.pos", DEFAULT_ARM_QPOS[2]))),
            float(current_positions.get("joint_4", current_positions.get("joint_4.pos", DEFAULT_ARM_QPOS[3]))),
            float(current_positions.get("joint_5", current_positions.get("joint_5.pos", DEFAULT_ARM_QPOS[4]))),
            float(current_positions.get("joint_6", current_positions.get("joint_6.pos", DEFAULT_ARM_QPOS[5]))),
            float(current_positions.get("gripper", current_positions.get("gripper.pos", DEFAULT_ARM_QPOS[6]))),
        ]
    values = [float(value) for value in current_positions]
    if len(values) != 7:
        raise ValueError("current_positions must contain 7 values")
    return values


def _pose_to_matrix(pose: Any) -> list[list[float]]:
    if isinstance(pose, dict):
        if "matrix" in pose:
            return [[float(v) for v in row] for row in pose["matrix"]]
        position = pose.get("position") or pose.get("translation") or pose.get("xyz")
        if position is None:
            position = [pose.get("x"), pose.get("y"), pose.get("z")]
        rot = _rotation_from_pose_dict(pose)
        mat = _identity()
        for row in range(3):
            for col in range(3):
                mat[row][col] = rot[row][col]
            mat[row][3] = float(position[row])
        return mat
    return [[float(v) for v in row] for row in pose]


def _rotation_from_pose_dict(pose: dict[str, Any]) -> list[list[float]]:
    if "rotation_matrix" in pose:
        return [[float(v) for v in row] for row in pose["rotation_matrix"]]
    rpy = pose.get("rpy") or pose.get("euler")
    if rpy is not None:
        return _euler_to_rotmat(float(rpy[0]), float(rpy[1]), float(rpy[2]))
    if all(key in pose for key in ("roll", "pitch", "yaw")):
        return _euler_to_rotmat(float(pose["roll"]), float(pose["pitch"]), float(pose["yaw"]))
    return [row[:3] for row in _identity()[:3]]


def _euler_to_rotmat(roll: float, pitch: float, yaw: float) -> list[list[float]]:
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    return [
        [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
        [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
        [-sp, cp * sr, cp * cr],
    ]


def _matrix_to_wxyz(mat: Any):
    np, _, _ = _deps()
    trace = float(mat[0, 0] + mat[1, 1] + mat[2, 2])
    if trace > 0.0:
        s = math.sqrt(trace + 1.0) * 2.0
        w = 0.25 * s
        x = (mat[2, 1] - mat[1, 2]) / s
        y = (mat[0, 2] - mat[2, 0]) / s
        z = (mat[1, 0] - mat[0, 1]) / s
    elif mat[0, 0] > mat[1, 1] and mat[0, 0] > mat[2, 2]:
        s = math.sqrt(1.0 + mat[0, 0] - mat[1, 1] - mat[2, 2]) * 2.0
        w = (mat[2, 1] - mat[1, 2]) / s
        x = 0.25 * s
        y = (mat[0, 1] + mat[1, 0]) / s
        z = (mat[0, 2] + mat[2, 0]) / s
    elif mat[1, 1] > mat[2, 2]:
        s = math.sqrt(1.0 + mat[1, 1] - mat[0, 0] - mat[2, 2]) * 2.0
        w = (mat[0, 2] - mat[2, 0]) / s
        x = (mat[0, 1] + mat[1, 0]) / s
        y = 0.25 * s
        z = (mat[1, 2] + mat[2, 1]) / s
    else:
        s = math.sqrt(1.0 + mat[2, 2] - mat[0, 0] - mat[1, 1]) * 2.0
        w = (mat[1, 0] - mat[0, 1]) / s
        x = (mat[0, 2] + mat[2, 0]) / s
        y = (mat[1, 2] + mat[2, 1]) / s
        z = 0.25 * s
    return np.asarray([w, x, y, z], dtype=float)


def _head_camera_to_arm_transform(side: str) -> list[list[float]]:
    return _matrix_inverse_rigid(_arm_to_head_camera_transform(side))


def _arm_to_head_camera_transform(side: str) -> list[list[float]]:
    data = json.loads(HEAD_CAMERA_EXTRINSICS_PATH.read_text(encoding="utf-8"))
    transforms = data["transforms"][side]
    raw = transforms.get("arm_to_camera", transforms.get("camera_to_arm"))
    if raw is None:
        raise KeyError(f"head-camera extrinsics missing arm_to_camera transform for side={side}")
    return [[float(v) for v in row] for row in raw]


def _matrix_inverse_rigid(mat: list[list[float]]) -> list[list[float]]:
    out = _identity()
    for r in range(3):
        for c in range(3):
            out[r][c] = mat[c][r]
    for r in range(3):
        out[r][3] = -sum(out[r][c] * mat[c][3] for c in range(3))
    return out


def _matrix_multiply(a: list[list[float]], b: list[list[float]]) -> list[list[float]]:
    return [[sum(a[r][k] * b[k][c] for k in range(4)) for c in range(4)] for r in range(4)]


def _identity() -> list[list[float]]:
    return [
        [1.0, 0.0, 0.0, 0.0],
        [0.0, 1.0, 0.0, 0.0],
        [0.0, 0.0, 1.0, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ]


def _clamp(value: float, lower: float, upper: float) -> float:
    return min(max(float(value), lower), upper)


def _normalize_side(side: str) -> str:
    side = str(side).lower()
    if side not in {"left", "right"}:
        raise ValueError("side must be left or right")
    return side
