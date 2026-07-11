from __future__ import annotations

import math
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any


RIGHT_EE_FRAME = "a_right_end_link"
LEFT_EE_FRAME = "b_left_end_link"
RIGHT_QPOS = slice(0, 7)
LEFT_QPOS = slice(7, 14)
RIGHT_IK_QPOS = tuple(range(0, 6))
LEFT_IK_QPOS = tuple(range(7, 13))
RIGHT_GRIP_INDEX = 6
LEFT_GRIP_INDEX = 13
ROBOT_QPOS_COUNT = 14
GRIPPER_CLOSED_QPOS = 0.0
DEFAULT_ARM_QPOS = (0.0, -0.5, -0.5, 0.0, 0.0, 0.0, GRIPPER_CLOSED_QPOS)
HEAD_CAMERA_EXTRINSICS_PATH = Path(__file__).resolve().parents[1] / "config" / "head_camera_extrinsics.json"
DEFAULT_CONDA_ENV = "hei-rebot-vr"
IK_RESULT_PREFIX = "LOOPMASTER_IK_RESULT_JSON="
MIN_ARM_TARGET_Z = 0.06


@dataclass(frozen=True)
class IkResult:
    side: str
    target_arm_pose: list[list[float]]
    target_camera_pose: list[list[float]] | None
    positions: dict[str, float]
    ik_success: bool
    ik_info: dict[str, Any]
    transform: dict[str, list[list[float]]] | None


def solve_arm_ee(
    *,
    side: str,
    pose: Any,
    input_frame: str = "head_camera",
    current_positions: dict[str, float] | list[float] | tuple[float, ...] | None = None,
    gripper: float | None = None,
) -> IkResult:
    if os.environ.get("LOOPMASTER_IK_IN_PROCESS") == "1":
        return _solve_arm_ee_in_process(
            side=side,
            pose=pose,
            input_frame=input_frame,
            current_positions=current_positions,
            gripper=gripper,
        )
    return _solve_arm_ee_in_conda(
        side=side,
        pose=pose,
        input_frame=input_frame,
        current_positions=current_positions,
        gripper=gripper,
    )


def _solve_arm_ee_in_process(
    *,
    side: str,
    pose: Any,
    input_frame: str = "head_camera",
    current_positions: dict[str, float] | list[float] | tuple[float, ...] | None = None,
    gripper: float | None = None,
) -> IkResult:
    np = _numpy()
    side = _normalize_side(side)
    target_input = pose_to_matrix(pose)
    transform_info = None
    target_camera_pose = None

    if input_frame.lower() in {"head", "head_camera", "camera", "front", "front_camera"}:
        target_camera_pose = target_input
        t_arm_cam = arm_to_head_camera_transform(side)
        t_cam_arm = np.linalg.inv(t_arm_cam)
        target_arm = t_arm_cam @ target_input
        transform_info = {
            "arm_to_head_camera": t_arm_cam.tolist(),
            "head_camera_to_arm": t_cam_arm.tolist(),
        }
    elif input_frame.lower() in {"arm", "arm_base", "base", f"{side}_arm"}:
        target_arm = target_input
    else:
        raise ValueError("input_frame must be head_camera, left_arm, or right_arm")
    target_arm_unclipped = target_arm.copy()
    target_arm_clipped = _clip_arm_target_z(target_arm)

    solver = _solver(side)
    current_full_q = _current_full_q(side, current_positions)
    dof, info = solver.ik(target_arm, current_full_q)
    side_q = np.asarray(dof[RIGHT_QPOS if side == "right" else LEFT_QPOS], dtype=float).copy()
    if gripper is not None:
        side_q[6] = float(gripper)
    elif current_positions is not None:
        side_q[6] = _current_gripper(current_positions, default=side_q[6])

    positions = {
        "joint_1": float(side_q[0]),
        "joint_2": float(side_q[1]),
        "joint_3": float(side_q[2]),
        "joint_4": float(side_q[3]),
        "joint_5": float(side_q[4]),
        "joint_6": float(side_q[5]),
        "gripper": float(side_q[6]),
    }
    return IkResult(
        side=side,
        target_arm_pose=target_arm.tolist(),
        target_camera_pose=target_camera_pose.tolist() if target_camera_pose is not None else None,
        positions=positions,
        ik_success=bool(info.get("success", False)),
        ik_info={
            **_jsonable_info(info),
            "arm_target_z_min_m": MIN_ARM_TARGET_Z,
            "arm_target_z_clipped": target_arm_clipped,
            "unclipped_arm_position": target_arm_unclipped[:3, 3].astype(float).tolist(),
            "clipped_arm_position": target_arm[:3, 3].astype(float).tolist(),
        },
        transform=transform_info,
    )


def _solve_arm_ee_in_conda(
    *,
    side: str,
    pose: Any,
    input_frame: str,
    current_positions: dict[str, float] | list[float] | tuple[float, ...] | None,
    gripper: float | None,
) -> IkResult:
    env_name = (
        os.environ.get("LOOPMASTER_IK_CONDA_ENV")
        or os.environ.get("HEI_REBOT_IK_CONDA_ENV")
        or os.environ.get("HEI_REBOT_VR_CONDA_ENV")
        or DEFAULT_CONDA_ENV
    )
    payload = {
        "side": side,
        "pose": pose,
        "input_frame": input_frame,
        "current_positions": current_positions,
        "gripper": gripper,
    }
    env = dict(os.environ)
    env["LOOPMASTER_IK_IN_PROCESS"] = "1"
    env["PYTHONNOUSERSITE"] = "1"
    env.pop("LD_LIBRARY_PATH", None)
    env.pop("PYTHONPATH", None)
    env.pop("VIRTUAL_ENV", None)
    env.pop("PYTHONHOME", None)
    env.pop("UV_PROJECT_ENVIRONMENT", None)
    env.pop("UV", None)

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as handle:
        json.dump(payload, handle, ensure_ascii=False)
        request_path = Path(handle.name)
    try:
        cmd = _ik_subprocess_cmd(env_name=env_name, request_path=request_path)
        completed = subprocess.run(
            cmd,
            cwd=str(Path(__file__).resolve().parents[2]),
            env=env,
            text=True,
            capture_output=True,
            timeout=float(os.environ.get("LOOPMASTER_IK_TIMEOUT_S", "20")),
            check=False,
        )
    finally:
        try:
            request_path.unlink()
        except OSError:
            pass

    if completed.returncode != 0:
        raise RuntimeError(
            "conda IK subprocess failed "
            f"(env={env_name}, code={completed.returncode}): "
            f"stdout={completed.stdout[-1000:]!r} stderr={completed.stderr[-1000:]!r}"
        )
    for line in reversed(completed.stdout.splitlines()):
        if line.startswith(IK_RESULT_PREFIX):
            data = json.loads(line[len(IK_RESULT_PREFIX) :])
            return IkResult(**data)
    raise RuntimeError(f"conda IK subprocess did not emit result JSON: stdout={completed.stdout[-1000:]!r}")


def _ik_subprocess_cmd(*, env_name: str, request_path: Path) -> list[str]:
    script = str(Path(__file__).resolve())
    python_exe = os.environ.get("LOOPMASTER_IK_PYTHON")
    if python_exe:
        return [python_exe, script, "--solve-json", str(request_path)]

    conda_python = _conda_env_python(env_name)
    if conda_python is not None:
        return [str(conda_python), script, "--solve-json", str(request_path)]

    conda_exe = os.environ.get("CONDA_EXE") or shutil.which("conda")
    if not conda_exe:
        raise RuntimeError("conda executable not found; create the IK env with scripts/setup_ik_conda_env.sh")
    return [
        conda_exe,
        "run",
        "-n",
        env_name,
        "env",
        "-u",
        "LD_LIBRARY_PATH",
        "-u",
        "PYTHONPATH",
        "-u",
        "VIRTUAL_ENV",
        "-u",
        "PYTHONHOME",
        "-u",
        "UV_PROJECT_ENVIRONMENT",
        "PYTHONNOUSERSITE=1",
        "LOOPMASTER_IK_IN_PROCESS=1",
        "python",
        script,
        "--solve-json",
        str(request_path),
    ]


def _conda_env_python(env_name: str) -> Path | None:
    candidates: list[Path] = []
    conda_prefix = os.environ.get("CONDA_PREFIX")
    if conda_prefix:
        prefix = Path(conda_prefix)
        candidates.append(prefix.parent / env_name / "bin" / "python")
    conda_exe = os.environ.get("CONDA_EXE")
    if conda_exe:
        conda_path = Path(conda_exe).expanduser()
        if conda_path.name == "conda":
            candidates.append(conda_path.parent.parent / "envs" / env_name / "bin" / "python")
    home = Path.home()
    candidates.extend(
        [
            home / "miniconda3" / "envs" / env_name / "bin" / "python",
            home / "anaconda3" / "envs" / env_name / "bin" / "python",
            home / "mambaforge" / "envs" / env_name / "bin" / "python",
        ]
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def head_camera_to_arm_transform(side: str):
    """Return T_head_camera_arm: the arm base pose expressed in the head-camera frame."""

    np = _numpy()
    return np.linalg.inv(arm_to_head_camera_transform(side))


def arm_to_head_camera_transform(side: str):
    """Return T_arm_head_camera: the head-camera pose expressed in the arm-base frame."""

    np = _numpy()
    side = _normalize_side(side)
    data = load_head_camera_extrinsics()
    try:
        transforms = data["transforms"][side]
    except KeyError as exc:
        raise KeyError(f"head-camera extrinsics missing transform for side={side}") from exc
    raw = transforms.get("arm_to_camera", transforms.get("camera_to_arm"))
    if raw is None:
        raise KeyError(f"head-camera extrinsics missing arm_to_camera transform for side={side}")
    return _validate_matrix(np.asarray(raw, dtype=float))


def load_head_camera_extrinsics(path: Path | None = None) -> dict[str, Any]:
    extrinsics_path = path or HEAD_CAMERA_EXTRINSICS_PATH
    return json.loads(extrinsics_path.read_text(encoding="utf-8"))


def _clip_arm_target_z(pose_arm: Any) -> bool:
    if float(pose_arm[2, 3]) >= MIN_ARM_TARGET_Z:
        return False
    pose_arm[2, 3] = MIN_ARM_TARGET_Z
    return True


def rotation_y(angle_rad: float):
    np = _numpy()
    c = math.cos(float(angle_rad))
    s = math.sin(float(angle_rad))
    return np.array(
        [
            [c, 0.0, s],
            [0.0, 1.0, 0.0],
            [-s, 0.0, c],
        ],
        dtype=float,
    )


def pose_to_matrix(pose: Any):
    np = _numpy()
    if isinstance(pose, dict):
        if "matrix" in pose:
            return _validate_matrix(np.asarray(pose["matrix"], dtype=float))
        position = pose.get("position") or pose.get("translation") or pose.get("xyz")
        if position is None:
            position = [pose.get("x"), pose.get("y"), pose.get("z")]
        xyz = np.asarray(position, dtype=float).reshape(3)
        rot = _rotation_from_pose_dict(pose)
        mat = np.eye(4, dtype=float)
        mat[:3, :3] = rot
        mat[:3, 3] = xyz
        return mat
    return _validate_matrix(np.asarray(pose, dtype=float))


def _rotation_from_pose_dict(pose: dict[str, Any]):
    np = _numpy()
    if "rotation_matrix" in pose:
        return np.asarray(pose["rotation_matrix"], dtype=float).reshape(3, 3)
    if "quat" in pose or "quaternion" in pose:
        return quat_to_rotmat(pose.get("quat") or pose.get("quaternion"))
    rpy = pose.get("rpy") or pose.get("euler")
    if rpy is not None:
        roll, pitch, yaw = np.asarray(rpy, dtype=float).reshape(3)
        return euler_to_rotmat(float(roll), float(pitch), float(yaw))
    if all(key in pose for key in ("roll", "pitch", "yaw")):
        return euler_to_rotmat(float(pose["roll"]), float(pose["pitch"]), float(pose["yaw"]))
    return np.eye(3, dtype=float)


def quat_to_rotmat(quat: Any):
    np = _numpy()
    values = np.asarray(quat, dtype=float).reshape(4)
    w, x, y, z = values
    norm = math.sqrt(float(w * w + x * x + y * y + z * z))
    if norm < 1e-12:
        return np.eye(3, dtype=float)
    w, x, y, z = w / norm, x / norm, y / norm, z / norm
    return np.array(
        [
            [1 - 2 * y**2 - 2 * z**2, 2 * x * y - 2 * z * w, 2 * x * z + 2 * y * w],
            [2 * x * y + 2 * z * w, 1 - 2 * x**2 - 2 * z**2, 2 * y * z - 2 * x * w],
            [2 * x * z - 2 * y * w, 2 * y * z + 2 * x * w, 1 - 2 * x**2 - 2 * y**2],
        ],
        dtype=float,
    )


def euler_to_rotmat(roll: float, pitch: float, yaw: float):
    np = _numpy()
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    return np.array(
        [
            [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
            [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
            [-sp, cp * sr, cp * cr],
        ],
        dtype=float,
    )


def _validate_matrix(mat):
    if mat.shape != (4, 4):
        raise ValueError(f"pose matrix must be 4x4, got {mat.shape}")
    return mat


@lru_cache(maxsize=2)
def _solver(side: str):
    _sanitize_conda_sys_path()
    side = _normalize_side(side)
    ik_root = _mujoco_ik_root()
    src_dir = ik_root / "src"
    if str(ik_root) not in sys.path:
        sys.path.insert(0, str(ik_root))
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))
    from src.pinocchio_kinematic import Kinematics

    solver = Kinematics(RIGHT_EE_FRAME if side == "right" else LEFT_EE_FRAME)
    solver.buildFromMJCF(
        str(ik_root / "model" / "reBot_description" / "urdf" / "reBot_dual_with_gripper.urdf"),
        active_q_indices=RIGHT_IK_QPOS if side == "right" else LEFT_IK_QPOS,
        reference_q=_default_full_q(),
    )
    return solver


def _sanitize_conda_sys_path() -> None:
    if os.environ.get("LOOPMASTER_IK_IN_PROCESS") != "1":
        return
    repo_root = Path(__file__).resolve().parents[2]
    repo_venv = str(repo_root / ".venv")
    home_local = str(Path.home() / ".local")
    sys.path[:] = [
        path
        for path in sys.path
        if path and not path.startswith(repo_venv) and not path.startswith(home_local)
    ]


def _current_full_q(side: str, current_positions: Any):
    np = _numpy()
    full_q = _default_full_q()
    if current_positions is None:
        return full_q
    side_q = _side_q_from_positions(current_positions)
    full_q[RIGHT_QPOS if side == "right" else LEFT_QPOS] = side_q
    return full_q


def _side_q_from_positions(current_positions: Any):
    np = _numpy()
    if isinstance(current_positions, dict):
        return np.array(
            [
                current_positions.get("joint_1", current_positions.get("joint_1.pos", DEFAULT_ARM_QPOS[0])),
                current_positions.get("joint_2", current_positions.get("joint_2.pos", DEFAULT_ARM_QPOS[1])),
                current_positions.get("joint_3", current_positions.get("joint_3.pos", DEFAULT_ARM_QPOS[2])),
                current_positions.get("joint_4", current_positions.get("joint_4.pos", DEFAULT_ARM_QPOS[3])),
                current_positions.get("joint_5", current_positions.get("joint_5.pos", DEFAULT_ARM_QPOS[4])),
                current_positions.get("joint_6", current_positions.get("joint_6.pos", DEFAULT_ARM_QPOS[5])),
                current_positions.get("gripper", current_positions.get("gripper.pos", DEFAULT_ARM_QPOS[6])),
            ],
            dtype=float,
        )
    values = np.asarray(current_positions, dtype=float).reshape(-1)
    if values.shape[0] != 7:
        raise ValueError("current_positions must contain 7 values")
    return values


def _current_gripper(current_positions: Any, *, default: float) -> float:
    if isinstance(current_positions, dict):
        return float(current_positions.get("gripper", current_positions.get("gripper.pos", default)))
    values = _numpy().asarray(current_positions, dtype=float).reshape(-1)
    return float(values[6]) if values.shape[0] == 7 else float(default)


def _default_full_q():
    np = _numpy()
    q = np.zeros(ROBOT_QPOS_COUNT, dtype=float)
    q[RIGHT_QPOS] = np.asarray(DEFAULT_ARM_QPOS, dtype=float)
    q[LEFT_QPOS] = np.asarray(DEFAULT_ARM_QPOS, dtype=float)
    return q


def _mujoco_ik_root() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "hei-rebot-lift"
        / "software"
        / "lerobot-hei-rebot-lift"
        / "examples"
        / "hei_rebot_lift"
        / "VR_mujoco_ik"
        / "mujoco_ik"
    )


def _normalize_side(side: str) -> str:
    side = str(side).lower()
    if side not in {"left", "right"}:
        raise ValueError("side must be left or right")
    return side


def _jsonable_info(info: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in info.items():
        if key == "sol_tauff":
            continue
        if isinstance(value, (str, int, float, bool, type(None))):
            out[str(key)] = value
    return out


def _numpy():
    import numpy as np

    return np


def _main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="LoopMaster HEI ReBot Lift IK subprocess runner.")
    parser.add_argument("--solve-json", type=Path, help="Path to an IK request JSON file.")
    args = parser.parse_args(argv)
    if args.solve_json is None:
        parser.print_help()
        return 2
    payload = json.loads(args.solve_json.read_text(encoding="utf-8"))
    result = _solve_arm_ee_in_process(
        side=payload["side"],
        pose=payload["pose"],
        input_frame=payload.get("input_frame") or "head_camera",
        current_positions=payload.get("current_positions"),
        gripper=payload.get("gripper"),
    )
    print(IK_RESULT_PREFIX + json.dumps(asdict(result), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
