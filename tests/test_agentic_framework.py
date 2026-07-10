from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace
from pathlib import Path
from typing import Any

from loopmaster_agentic import cli as cli_module
from loopmaster_agentic.agents import Auditor, Handler, Strategist, Worker
from loopmaster_agentic.agents.handler_chat import HandlerChatSession, format_handler_reply
from loopmaster_agentic.agents.skill_updater import apply_review_skill_updates
from loopmaster_agentic.agents.workspace import new_workspace
from loopmaster_agentic.cli import main
from loopmaster_agentic.core.result import RunResult
from loopmaster_agentic.core.types import Plan, SkillCall, TraceStep
from loopmaster_agentic.platform.dry_run import DryRunPlatform
from loopmaster_agentic.platform.hei_rebot_lift import HeiRebotLiftPlatform, split_hei_observation
from loopmaster_agentic.skills.registry import SkillContext, SkillRegistry
from keyboard_control import TeleopState, _handle_key


class LoopMasterAgenticTests(unittest.TestCase):
    def test_roles_define_four_role_architecture(self) -> None:
        self.assertEqual(Handler.role_name, "handler")
        self.assertEqual(Strategist.role_name, "strategist")
        self.assertEqual(Worker.role_name, "worker")
        self.assertEqual(Auditor.role_name, "auditor")

    def test_default_workspace_root_lives_under_repo(self) -> None:
        from loopmaster_agentic.agents.workspace import default_workspace_root

        self.assertEqual(default_workspace_root(), Path.cwd() / "_runs")

    def test_base_skill_surface_excludes_task_specific_skills(self) -> None:
        skills = SkillRegistry(include_user=False).list()
        names = {skill.name for skill in skills}
        self.assertEqual(
            names,
            {
                "capture_image",
                "create_skill",
                "detect_grasps",
                "grounded_sam2",
                "move_arm_ee",
                "move_arm_joints",
                "observe",
                "set_base_velocity",
                "set_gripper",
                "set_lift_height",
                "stop_motion",
            },
        )
        forbidden_terms = ("atomic", "zeroshot", "robotwin", "sim")
        searchable = "\n".join(f"{skill.category}/{skill.name}" for skill in skills).lower()
        for term in forbidden_terms:
            self.assertNotIn(term, searchable)

    def test_hei_platform_clamps_arm_targets_to_original_limits(self) -> None:
        platform = HeiRebotLiftPlatform()
        fake = _FakeHeiRobot()
        platform._robot = fake

        sent = platform.send_action(
            {
                "right_joint_1.pos": -99.0,
                "right_joint_2.pos": 99.0,
                "left_joint_1.pos": 99.0,
                "right_gripper.pos": 99.0,
                "x.vel": 0.2,
            }
        )

        self.assertEqual(sent["right_joint_1.pos"], -0.3)
        self.assertEqual(sent["right_joint_2.pos"], 0.0)
        self.assertEqual(sent["left_joint_1.pos"], 0.3)
        self.assertEqual(sent["right_gripper.pos"], 0.0)
        self.assertEqual(sent["x.vel"], 0.2)
        self.assertEqual(fake.actions[-1], sent)

        arm_sent = platform.command_arm("left", {"joint_1": -99.0, "joint_4": 99.0})
        self.assertEqual(arm_sent["left_joint_1.pos"], -1.5)
        self.assertEqual(arm_sent["left_joint_4.pos"], 1.57)

        gripper_sent = platform.set_gripper("right", 99.0)
        self.assertEqual(gripper_sent["right_gripper.pos"], 0.0)

    def test_move_arm_ee_head_camera_extrinsics(self) -> None:
        import numpy as np

        from loopmaster_agentic.ik.hei_rebot_lift_ik import (
            arm_to_head_camera_transform,
            head_camera_to_arm_transform,
            load_head_camera_extrinsics,
        )

        extrinsics = load_head_camera_extrinsics()
        left_cam = arm_to_head_camera_transform("left")
        right_cam = arm_to_head_camera_transform("right")
        cam_left = head_camera_to_arm_transform("left")

        np.testing.assert_allclose(left_cam, extrinsics["transforms"]["left"]["arm_to_camera"])
        np.testing.assert_allclose(right_cam, extrinsics["transforms"]["right"]["arm_to_camera"])
        np.testing.assert_allclose(left_cam[:3, 3], [0.03, -0.2, 0.34])
        np.testing.assert_allclose(right_cam[:3, 3], [0.03, 0.26, 0.34])
        self.assertAlmostEqual(left_cam[0, 0], np.cos(np.deg2rad(150.0)), places=6)
        self.assertAlmostEqual(left_cam[0, 2], np.sin(np.deg2rad(150.0)), places=6)
        self.assertAlmostEqual(left_cam[2, 0], -np.sin(np.deg2rad(150.0)), places=6)
        self.assertAlmostEqual(left_cam[2, 2], np.cos(np.deg2rad(150.0)), places=6)
        np.testing.assert_allclose(left_cam[:3, :3], right_cam[:3, :3])
        np.testing.assert_allclose(cam_left, np.linalg.inv(left_cam))
        self.assertAlmostEqual((left_cam @ np.linalg.inv(right_cam))[1, 3], -0.46)

    def test_move_arm_ee_skill_returns_structured_ik_errors(self) -> None:
        from loopmaster_agentic.skills.base.control.move_arm_ee import policy

        with mock.patch.object(policy, "solve_arm_ee_dict", side_effect=RuntimeError("missing ik deps")):
            result = policy.dispatch(
                SkillContext(platform=DryRunPlatform(), workspace=new_workspace("move_ee", root=Path("/tmp"))),
                {
                    "side": "right",
                    "pose": {"position": [0.2, 0.0, 0.3], "rpy": [0.0, 0.0, 0.0]},
                    "execute": False,
                },
            )

        self.assertFalse(result["ok"])
        self.assertIn("IK failed", result["error"])

    def test_move_arm_ee_skill_limits_joint_step(self) -> None:
        from loopmaster_agentic.skills.base.control.move_arm_ee import policy

        class Platform(DryRunPlatform):
            def read_arm_positions(self, side: str) -> dict[str, float]:
                return {f"{side}_{joint}.pos": 0.0 for joint in policy.JOINTS}

            def command_arm(self, side: str, positions: dict[str, float]) -> dict[str, float]:
                self.actions.append(dict(positions))
                return {f"{side}_{joint}.pos": float(value) for joint, value in positions.items()}

        fake_ik = {
            "ik_success": True,
            "positions": {joint: 0.0 for joint in policy.JOINTS},
            "target_arm_pose": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
            "target_camera_pose": None,
            "transform": None,
            "ik_info": {},
        }
        fake_ik["positions"]["joint_1"] = 0.12

        platform = Platform()
        with mock.patch.object(policy, "solve_arm_ee_dict", return_value=fake_ik):
            result = policy.dispatch(
                SkillContext(platform=platform, workspace=new_workspace("move_ee_limit", root=Path("/tmp"))),
                {
                    "side": "right",
                    "pose": {"matrix": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]},
                    "input_frame": "arm",
                    "max_joint_step": 0.05,
                    "step_dt": 0.0,
                },
            )

        self.assertTrue(result["ok"])
        self.assertEqual(len(result["trajectory"]), 3)
        self.assertAlmostEqual(platform.actions[0]["joint_1"], 0.04)
        self.assertAlmostEqual(platform.actions[-1]["joint_1"], 0.12)

    def test_move_arm_ee_skill_uses_explicit_current_positions(self) -> None:
        from loopmaster_agentic.skills.base.control.move_arm_ee import policy

        class Platform(DryRunPlatform):
            def read_arm_positions(self, side: str) -> dict[str, float]:
                return {f"{side}_{joint}.pos": 0.0 for joint in policy.JOINTS}

            def command_arm(self, side: str, positions: dict[str, float]) -> dict[str, float]:
                self.actions.append(dict(positions))
                return {f"{side}_{joint}.pos": float(value) for joint, value in positions.items()}

        fake_ik = {
            "ik_success": True,
            "positions": {joint: 0.0 for joint in policy.JOINTS},
            "target_arm_pose": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
            "target_camera_pose": None,
            "transform": None,
            "ik_info": {},
        }
        fake_ik["positions"]["joint_1"] = 0.2
        current = {joint: 0.1 for joint in policy.JOINTS}
        current["joint_1"] = 0.1

        platform = Platform()
        with mock.patch.object(policy, "solve_arm_ee_dict", return_value=fake_ik) as solve:
            result = policy.dispatch(
                SkillContext(platform=platform, workspace=new_workspace("move_ee_current", root=Path("/tmp"))),
                {
                    "side": "right",
                    "pose": {"position": [0.2, 0.0, 0.3]},
                    "current_positions": current,
                    "max_joint_step": 0.05,
                    "step_dt": 0.0,
                },
            )

        self.assertTrue(result["ok"])
        self.assertEqual(solve.call_args.kwargs["current_positions"], current)
        self.assertEqual(len(result["trajectory"]), 2)
        self.assertAlmostEqual(platform.actions[0]["joint_1"], 0.15)

    def test_move_arm_ee_skill_holds_other_arm_positions(self) -> None:
        from loopmaster_agentic.skills.base.control.move_arm_ee import policy

        class Platform(DryRunPlatform):
            def command_arms(self, *, right=None, left=None) -> dict[str, float]:
                self.actions.append({"right": dict(right or {}), "left": dict(left or {})})
                sent = {}
                if right:
                    sent.update({f"right_{joint}.pos": float(value) for joint, value in right.items()})
                if left:
                    sent.update({f"left_{joint}.pos": float(value) for joint, value in left.items()})
                return sent

        fake_ik = {
            "ik_success": True,
            "positions": {joint: 0.0 for joint in policy.JOINTS},
            "target_arm_pose": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
            "target_camera_pose": None,
            "transform": None,
            "ik_info": {},
        }
        fake_ik["positions"]["joint_1"] = 0.2
        current = {joint: 0.1 for joint in policy.JOINTS}
        other = {joint: -0.2 for joint in policy.JOINTS}

        platform = Platform()
        with mock.patch.object(policy, "solve_arm_ee_dict", return_value=fake_ik):
            result = policy.dispatch(
                SkillContext(platform=platform, workspace=new_workspace("move_ee_hold_other", root=Path("/tmp"))),
                {
                    "side": "right",
                    "pose": {"position": [0.2, 0.0, 0.3]},
                    "current_positions": current,
                    "other_arm_positions": other,
                    "max_joint_step": 0.05,
                    "step_dt": 0.0,
                },
            )

        self.assertTrue(result["ok"])
        self.assertEqual(len(result["trajectory"]), 2)
        self.assertEqual(platform.actions[0]["left"], other)
        self.assertAlmostEqual(platform.actions[-1]["right"]["joint_1"], 0.2)

    def test_move_arm_joints_skill_can_command_both_arms(self) -> None:
        from loopmaster_agentic.skills.base.control.move_arm_joints import policy

        class Platform(DryRunPlatform):
            def command_arms(self, *, right=None, left=None) -> dict[str, float]:
                self.actions.append({"right": dict(right or {}), "left": dict(left or {})})
                return {
                    **{f"right_{joint}.pos": float(value) for joint, value in (right or {}).items()},
                    **{f"left_{joint}.pos": float(value) for joint, value in (left or {}).items()},
                }

        platform = Platform()
        positions = {joint: 0.1 for joint in policy.JOINTS}
        result = policy.dispatch(
            SkillContext(platform=platform, workspace=new_workspace("move_joints_both", root=Path("/tmp"))),
            {"side": "both", "positions": positions},
        )

        self.assertTrue(result["ok"])
        self.assertEqual(platform.actions[-1]["right"], positions)
        self.assertEqual(platform.actions[-1]["left"], positions)

    def test_move_arm_ee_position_only_target_ignores_orientation(self) -> None:
        from loopmaster_agentic.skills.base.control.move_arm_ee import policy

        fake_ik = {
            "ik_success": True,
            "positions": {joint: 0.0 for joint in policy.JOINTS},
            "target_arm_pose": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
            "target_camera_pose": None,
            "transform": None,
            "ik_info": {},
        }

        with mock.patch.object(policy, "solve_arm_ee_dict", return_value=fake_ik) as solve:
            result = policy.dispatch(
                SkillContext(platform=DryRunPlatform(), workspace=new_workspace("move_ee_position", root=Path("/tmp"))),
                {
                    "side": "right",
                    "pose": {"position": [0.2, 0.0, 0.3]},
                    "execute": False,
                },
            )

        self.assertTrue(result["ok"])
        self.assertEqual(solve.call_args.kwargs["orientation_cost"], 0.0)

    def test_ik_bridge_uses_mink_solver_without_conda_subprocess(self) -> None:
        from loopmaster_agentic.ik import bridge
        from loopmaster_agentic.ik.mink_ik import MinkIkResult

        fake_result = MinkIkResult(
            side="left",
            target_arm_pose=[[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
            target_camera_pose=None,
            positions={
                "joint_1": 0.0,
                "joint_2": 0.0,
                "joint_3": 0.0,
                "joint_4": 0.0,
                "joint_5": 0.0,
                "joint_6": 0.0,
                "gripper": 0.0,
            },
            ik_success=True,
            ik_info={},
            transform=None,
        )

        with mock.patch("loopmaster_agentic.ik.mink_ik.solve_arm_ee_mink", return_value=fake_result) as solve:
            result = bridge.solve_arm_ee_dict(
                side="left",
                pose={"matrix": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]},
            )

        self.assertTrue(result["ik_success"])
        self.assertEqual(solve.call_count, 1)

    def test_keyboard_l_and_r_select_arm_side(self) -> None:
        state = TeleopState()
        args = argparse.Namespace(linear_speed=0.05, angular_speed=0.15)

        _handle_key("l", args, None, None, state)
        self.assertEqual(state.side, "left")

        _handle_key("r", args, None, None, state)
        self.assertEqual(state.side, "right")

    def test_keyboard_ee_mode_dispatches_move_arm_ee(self) -> None:
        class Registry:
            def __init__(self) -> None:
                self.calls = []

            def dispatch(self, name, context, args):
                self.calls.append((name, args))
                return {"ok": True}

        registry = Registry()
        state = TeleopState()
        args = argparse.Namespace(
            ee=True,
            ee_step=0.05,
            ee_frame="arm",
            linear_speed=0.05,
            angular_speed=0.15,
        )

        _handle_key("w", args, registry, SimpleNamespace(), state)

        self.assertEqual(registry.calls[0][0], "move_arm_ee")
        self.assertEqual(registry.calls[0][1]["side"], "right")
        self.assertEqual(registry.calls[0][1]["input_frame"], "arm")
        self.assertEqual(registry.calls[0][1]["pose"]["position"], [0.25, 0.0, 0.3])
        self.assertEqual(state.selected_ee_target()["position"], [0.25, 0.0, 0.3])

    def test_dry_run_handler_executes_four_role_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = Handler(workspace_root=Path(tmp)).run(
                task="inspect robot",
                user_request="inspect robot",
                platform=DryRunPlatform(),
            )
            self.assertTrue(result.success)
            self.assertEqual(result.review["verdict"], "done")
            self.assertTrue((Path(result.workspace) / "plan.md").is_file())
            self.assertTrue((Path(result.workspace) / "summary.md").is_file())
            self.assertTrue((Path(result.workspace) / "review.md").is_file())
            self.assertGreaterEqual(len(result.trace), 2)

    def test_handler_plans_explicit_low_level_control(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = Handler(workspace_root=Path(tmp)).run(
                task="set right gripper position=0.25",
                user_request="set right gripper position=0.25",
                platform=DryRunPlatform(),
            )
            self.assertTrue(result.success)
            skills = [step.skill for step in result.trace]
            roles = [step.role for step in result.trace]
            self.assertIn("set_gripper", skills)
            self.assertIn("worker.monitor", roles)
            self.assertIn("stop_motion", skills)

    def test_handler_reports_research_needed_for_missing_task_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = Handler(workspace_root=Path(tmp)).run(
                task="pick up the red block",
                user_request="pick up the red block",
                platform=DryRunPlatform(),
            )
            self.assertFalse(result.success)
            self.assertEqual(result.review["verdict"], "research_needed")
            self.assertTrue(result.review["research_questions"])

    def test_worker_resolves_dynamic_context_refs_between_skill_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "skills"
            producer = root / "base" / "test" / "produce"
            consumer = root / "base" / "test" / "consume"
            producer.mkdir(parents=True)
            consumer.mkdir(parents=True)
            (producer / "SKILL.md").write_text(
                "---\nname: produce\ncategory: test\n---\n# Produce\n",
                encoding="utf-8",
            )
            (producer / "policy.py").write_text(
                "def dispatch(context, args):\n"
                "    return {'ok': True, 'artifact': {'path': 'rgb.png'}, 'items': [{'score': 0.9}]}\n",
                encoding="utf-8",
            )
            (consumer / "SKILL.md").write_text(
                "---\nname: consume\ncategory: test\n---\n# Consume\n",
                encoding="utf-8",
            )
            (consumer / "policy.py").write_text(
                "def dispatch(context, args):\n"
                "    return {'ok': True, 'received': args}\n",
                encoding="utf-8",
            )

            workspace = new_workspace("dynamic_refs", root=Path(tmp) / "workspaces")
            trace = Worker().execute(
                plan=Plan(
                    task="dynamic refs",
                    goal="dynamic refs",
                    steps=[
                        SkillCall("produce", {}, "make an artifact"),
                        SkillCall(
                            "consume",
                            {
                                "img_path": {"$ref": "produce.artifact.path"},
                                "score": "$produce.items.0.score",
                                "label": "path=${produce.artifact.path}",
                            },
                            "consume prior result",
                        ),
                    ],
                ),
                workspace=workspace,
                platform=DryRunPlatform(),
                skills=SkillRegistry(roots=[root], include_user=False),
            )

            self.assertEqual(len(trace), 2)
            self.assertTrue(trace[1].ok)
            self.assertEqual(trace[1].args["img_path"], "rgb.png")
            self.assertEqual(trace[1].args["score"], 0.9)
            self.assertEqual(trace[1].args["label"], "path=rgb.png")

    def test_worker_preflight_block_writes_trace(self) -> None:
        class BlockingWorkerAgent:
            def run_json(self, *, role: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
                self.prompt = prompt
                return {
                    "proceed": False,
                    "execution_notes": ["plan uses stale joint constants"],
                    "concerns": ["derive targets from fresh observe first"],
                }

        with tempfile.TemporaryDirectory() as tmp:
            workspace = new_workspace("blocked_worker", root=Path(tmp) / "workspaces")
            trace = Worker().execute(
                plan=Plan(
                    task="blocked worker",
                    goal="blocked worker",
                    steps=[SkillCall("observe", {"include_state": True}, "read state")],
                ),
                workspace=workspace,
                platform=DryRunPlatform(),
                skills=SkillRegistry(include_user=False),
                agent_client=BlockingWorkerAgent(),
            )

            self.assertEqual(len(trace), 1)
            self.assertEqual(trace[0].skill, "worker_gate")
            self.assertFalse(trace[0].ok)
            self.assertIn("proceed=false", trace[0].result["error"])
            self.assertTrue((workspace.root / "trace.jsonl").is_file())

    def test_worker_learned_skill_can_call_registered_skills_with_trace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_root = os.environ.get("LOOPMASTER_SKILL_ROOT")
            os.environ["LOOPMASTER_SKILL_ROOT"] = str(Path(tmp) / "skills")
            try:
                skill_dir = Path(os.environ["LOOPMASTER_SKILL_ROOT"]) / "learned" / "control" / "combo"
                skill_dir.mkdir(parents=True)
                (skill_dir / "SKILL.md").write_text(
                    "---\nname: combo\ncategory: learned/control\n---\n# Combo\n",
                    encoding="utf-8",
                )
                (skill_dir / "policy.py").write_text(
                    "def dispatch(context, args):\n"
                    "    observe = context.call_skill('observe', {'include_images': False, 'include_state': True})\n"
                    "    stop = context.call_skill('stop_motion', {'reason': 'combo done'})\n"
                    "    return {'ok': observe.get('ok') and stop.get('ok'), 'observe': observe, 'stop': stop}\n",
                    encoding="utf-8",
                )

                workspace = new_workspace("combo", root=Path(tmp) / "workspaces")
                trace = Worker().execute(
                    plan=Plan(
                        task="combo",
                        goal="combo",
                        steps=[SkillCall("combo", {}, "run learned combo")],
                    ),
                    workspace=workspace,
                    platform=DryRunPlatform(),
                    skills=SkillRegistry(include_user=True),
                )
            finally:
                if old_root is None:
                    os.environ.pop("LOOPMASTER_SKILL_ROOT", None)
                else:
                    os.environ["LOOPMASTER_SKILL_ROOT"] = old_root

        self.assertEqual([step.skill for step in trace], ["observe", "stop_motion", "combo"])
        self.assertEqual(trace[0].role, "worker.subskill")
        self.assertEqual(trace[1].role, "worker.subskill")
        self.assertTrue(trace[2].ok)

    def test_create_skill_creates_validated_user_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_root = os.environ.get("LOOPMASTER_SKILL_ROOT")
            os.environ["LOOPMASTER_SKILL_ROOT"] = str(Path(tmp) / "user_skills")
            try:
                registry = SkillRegistry(include_user=True)
                workspace = _FakeWorkspace(Path(tmp))
                result = registry.dispatch(
                    "create_skill",
                    SkillContext(platform=DryRunPlatform(), workspace=workspace),
                    {
                        "skill_name": "created_by_skill",
                        "category": "learned/control",
                        "files": [
                            {
                                "path": "SKILL.md",
                                "content": (
                                    "---\n"
                                    "name: created_by_skill\n"
                                    "description: created by create_skill\n"
                                    "category: learned/control\n"
                                    "---\n\n"
                                    "# Created\n"
                                ),
                            },
                            {
                                "path": "policy.py",
                                "content": "def dispatch(context, args):\n    return {'ok': True, 'created': True}\n",
                            },
                        ],
                    },
                )
                refreshed = SkillRegistry(include_user=True)
                created = refreshed.dispatch(
                    "created_by_skill",
                    SkillContext(platform=DryRunPlatform(), workspace=workspace),
                    {},
                )
            finally:
                if old_root is None:
                    os.environ.pop("LOOPMASTER_SKILL_ROOT", None)
                else:
                    os.environ["LOOPMASTER_SKILL_ROOT"] = old_root

        self.assertTrue(result["ok"])
        self.assertTrue(created["created"])

    def test_create_skill_rejects_policy_without_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_root = os.environ.get("LOOPMASTER_SKILL_ROOT")
            os.environ["LOOPMASTER_SKILL_ROOT"] = str(Path(tmp) / "user_skills")
            try:
                registry = SkillRegistry(include_user=True)
                workspace = _FakeWorkspace(Path(tmp))
                result = registry.dispatch(
                    "create_skill",
                    SkillContext(platform=DryRunPlatform(), workspace=workspace),
                    {
                        "skill_name": "bad_created_by_skill",
                        "category": "learned/control",
                        "files": [
                            {
                                "path": "SKILL.md",
                                "content": "---\nname: bad_created_by_skill\ncategory: learned/control\n---\n# Bad\n",
                            },
                            {"path": "policy.py", "content": "def run(context, args):\n    return {'ok': True}\n"},
                        ],
                    },
                )
                refreshed = SkillRegistry(include_user=True)
            finally:
                if old_root is None:
                    os.environ.pop("LOOPMASTER_SKILL_ROOT", None)
                else:
                    os.environ["LOOPMASTER_SKILL_ROOT"] = old_root

        self.assertFalse(result["ok"])
        self.assertIn("policy.py must define callable dispatch", result["rejected"][0])
        self.assertIsNone(refreshed.get("bad_created_by_skill"))

    def test_perception_skills_merge_capture_image_memory_paths(self) -> None:
        from loopmaster_agentic.skills.base.perception.detect_grasps.policy import (
            _merge_capture_image_memory as merge_anygrasp_capture,
        )
        from loopmaster_agentic.skills.base.perception.grounded_sam2.policy import (
            _merge_capture_image_memory as merge_sam_capture,
        )

        context = SimpleNamespace(
            memory={
                "capture_image": {
                    "rgb": {"path": "/tmp/rgb.png"},
                    "depth": {"path": "/tmp/depth.png"},
                }
            }
        )

        self.assertEqual(merge_sam_capture(context, {})["img_path"], "/tmp/rgb.png")
        grasp_args = merge_anygrasp_capture(context, {})
        self.assertEqual(grasp_args["color_path"], "/tmp/rgb.png")
        self.assertEqual(grasp_args["depth_path"], "/tmp/depth.png")

    def test_detect_grasps_does_not_mix_example_mask_with_custom_rgbd(self) -> None:
        import numpy as np
        from PIL import Image

        from loopmaster_agentic.skills.base.perception.detect_grasps.policy import _load_points

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            detection_dir = root / "grasp_detection"
            example = detection_dir / "example_data"
            example.mkdir(parents=True)
            rgb = root / "rgb.png"
            depth = root / "depth.png"
            Image.fromarray(np.zeros((2, 2, 3), dtype=np.uint8)).save(rgb)
            Image.fromarray(np.ones((2, 2), dtype=np.uint16) * 500).save(depth)
            Image.fromarray(np.ones((3, 2), dtype=np.uint8)).save(example / "seg_mask.png")

            points, colors, seg_mask, source = _load_points(
                detection_dir,
                {
                    "color_path": str(rgb),
                    "depth_path": str(depth),
                    "fx": 1.0,
                    "fy": 1.0,
                    "cx": 0.0,
                    "cy": 0.0,
                    "depth_scale": 1000.0,
                    "depth_trunc": 1.0,
                },
            )

        self.assertEqual(points.shape, (4, 3))
        self.assertEqual(colors.shape, (4, 3))
        self.assertIsNone(seg_mask)
        self.assertEqual(source["seg_mask_path"], "")

    def test_strategist_links_object_grasp_perception_with_dynamic_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = Strategist().plan(
                task="detect target grasp",
                user_request="detect grasp pose for object=red cup with mask",
                workspace=new_workspace("plan_refs", root=Path(tmp)),
                skills=SkillRegistry(include_user=False),
            )

            steps = [step.name for step in plan.steps]
            self.assertEqual(steps[:4], ["observe", "capture_image", "grounded_sam2", "detect_grasps"])
            self.assertEqual(plan.steps[1].args["source"], "d435_rgbd")
            self.assertEqual(plan.steps[2].args["img_path"], {"$ref": "capture_image.rgb.path"})
            self.assertEqual(plan.steps[3].args["color_path"], {"$ref": "capture_image.rgb.path"})
            self.assertEqual(plan.steps[3].args["depth_path"], {"$ref": "capture_image.depth.path"})
            self.assertEqual(plan.steps[3].args["seg_mask_path"], {"$ref": "grounded_sam2.seg_mask_path"})

    def test_handler_connects_all_four_roles_to_subagent_client(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = _FakeSubagentClient()
            result = Handler(workspace_root=Path(tmp), agent_client=fake).run(
                task="inspect robot",
                user_request="inspect robot",
                platform=DryRunPlatform(),
            )

            self.assertTrue(result.success)
            self.assertEqual(fake.roles, ["handler", "strategist", "worker", "auditor"])
            workspace = Path(result.workspace)
            self.assertTrue((workspace / "handler_agent.json").is_file())
            self.assertTrue((workspace / "strategist_agent.json").is_file())
            self.assertTrue((workspace / "worker_agent.json").is_file())
            self.assertTrue((workspace / "auditor_agent.json").is_file())

    def test_strategist_agent_prompt_includes_skill_args(self) -> None:
        class CapturingStrategistClient:
            profile = "fake"

            def run_json(self, *, role: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
                payload = json.loads(prompt)
                self.available_skills = payload["available_skills"]
                candidate = payload["candidate_plan"]
                return {
                    "goal": candidate["goal"],
                    "steps": [],
                    "success_criteria": candidate["success_criteria"],
                    "risks": candidate["risks"],
                    "assumptions": candidate["assumptions"],
                    "research_questions": candidate["research_questions"],
                    "subagent_notes": [],
                }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "skills"
            skill_dir = root / "learned" / "control" / "arg_skill"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\n"
                "name: arg_skill\n"
                "category: learned/control\n"
                "args:\n"
                "  side: string\n"
                "  cycles: integer\n"
                "---\n"
                "# Arg Skill\n",
                encoding="utf-8",
            )
            (skill_dir / "policy.py").write_text(
                "def dispatch(context, args):\n    return {'ok': True}\n",
                encoding="utf-8",
            )
            fake = CapturingStrategistClient()
            Strategist().plan(
                task="use arg skill",
                user_request="use arg skill",
                workspace=new_workspace("args", root=Path(tmp) / "workspaces"),
                skills=SkillRegistry(roots=[root], include_user=False),
                agent_client=fake,
            )

        arg_skill = next(item for item in fake.available_skills if item["name"] == "arg_skill")
        self.assertEqual(arg_skill["args"], {"side": "string", "cycles": "integer"})

    def test_handler_chat_session_persists_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = root / "handler-chat.jsonl"
            session = HandlerChatSession(
                handler=Handler(workspace_root=root / "workspaces"),
                platform=DryRunPlatform(),
                state_path=state_path,
            )
            reply = session.reply("inspect robot state")

            self.assertIn("机器人连接看起来是通的", reply)
            self.assertTrue(state_path.is_file())
            self.assertEqual(len(session.messages), 2)
            self.assertTrue(Path(session.last_result.workspace).is_dir())

            resumed = HandlerChatSession(
                handler=Handler(workspace_root=root / "workspaces"),
                platform=DryRunPlatform(),
                state_path=state_path,
            )
            self.assertEqual(len(resumed.messages), 2)
            self.assertIn("inspect robot state", resumed.reply("/history"))
            self.assertIn("set_gripper", resumed.reply("set right gripper position=0.25"))
            self.assertEqual(len(resumed.messages), 4)

    def test_chat_cli_once_uses_persistent_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("sys.stdout") as stdout:
                code = main(
                    [
                        "chat",
                        "--dry-run",
                        "--local-agents",
                        "--state-dir",
                        str(root / "state"),
                        "--workspace-root",
                        str(root / "workspaces"),
                        "--session-id",
                        "test-session",
                        "--once",
                        "inspect robot state",
                    ]
                )

            self.assertEqual(code, 0)
            self.assertTrue((root / "state" / "test-session.jsonl").is_file())
            printed = "\n".join(str(call.args[0]) for call in stdout.write.call_args_list if call.args)
            self.assertIn("worker executing plan", printed)
            self.assertIn("skill `observe` args=", printed)

    def test_chat_cli_defaults_to_remote_robot_client(self) -> None:
        captured = {}

        class _FakeChatSession:
            def __init__(self, *, platform, **kwargs):
                captured["remote_ip"] = platform.config.remote_ip

            def reply(self, text, *, progress=None):
                return "ok"

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(cli_module, "HandlerChatSession", _FakeChatSession):
                code = main(
                    [
                        "chat",
                        "--local-agents",
                        "--state-dir",
                        str(Path(tmp) / "state"),
                        "--workspace-root",
                        str(Path(tmp) / "workspaces"),
                        "--once",
                        "inspect robot state",
                    ]
                )

        self.assertEqual(code, 0)
        self.assertEqual(captured["remote_ip"], cli_module.DEFAULT_REMOTE_IP)

    def test_handler_chat_agent_can_answer_capability_question_directly(self) -> None:
        class _ExplodingPlatform(DryRunPlatform):
            def connect(self) -> None:
                raise AssertionError("direct chat answer should not connect platform")

        with tempfile.TemporaryDirectory() as tmp:
            events = []
            fake = _FakeSubagentClient()
            session = HandlerChatSession(
                handler=Handler(workspace_root=Path(tmp) / "workspaces", agent_client=fake),
                platform=_ExplodingPlatform(),
                state_path=Path(tmp) / "handler-chat.jsonl",
            )
            reply = session.reply("嗨 你现在能干嘛", progress=events.append)

        self.assertEqual(reply, "fake direct capability answer")
        self.assertEqual(fake.roles, ["handler"])
        self.assertIn("handler agent answered directly", events[-1])
        self.assertEqual(session.last_result.trace, [])

    def test_successful_handler_reply_hides_internal_auditor_fields(self) -> None:
        result = RunResult(
            task="check connection",
            workspace="/tmp/workspace",
            plan=Plan(task="check connection", goal="check robot"),
            trace=[
                TraceStep(
                    index=1,
                    skill="observe",
                    args={"include_images": False, "include_state": True},
                    result={"ok": True, "observation": {"state_keys": ["x.vel"], "images": {}, "extras": {}}},
                    ok=True,
                ),
                TraceStep(
                    index=2,
                    skill="capture_image",
                    args={"camera": "front", "required": False},
                    result={
                        "ok": True,
                        "captured": True,
                        "camera": "front",
                        "image": {"shape": (480, 640, 3), "dtype": "uint8"},
                    },
                    ok=True,
                ),
            ],
            review={
                "verdict": "done",
                "next_action": "Tell the user in Chinese that the robot connection appears live.",
                "research_questions": ["If observe fails, what should be inspected next?"],
                "used_skills": ["observe", "capture_image"],
                "used_control_skills": [],
                "sim_leak": [],
                "success": True,
            },
            success=True,
        )

        reply = format_handler_reply(result)

        self.assertIn("机器人连接看起来是通的", reply)
        self.assertNotIn("Tell the user", reply)
        self.assertNotIn("If observe fails", reply)
        self.assertNotIn("还需要你补充", reply)
        self.assertNotIn("本轮执行 trace", reply)
        self.assertNotIn("`observe` args=", reply)
        self.assertIn("本轮调用了：observe, capture_image", reply)

    def test_handler_reply_includes_failed_skill_args_and_error(self) -> None:
        result = RunResult(
            task="oscillate left joint5",
            workspace="/tmp/workspace",
            plan=Plan(task="oscillate left joint5", goal="move joint"),
            trace=[
                TraceStep(
                    index=1,
                    skill="move_arm_joints",
                    args={"arm": "left", "positions": {"joint_5": 0.5}},
                    result={"ok": False, "error": "side must be left or right"},
                    ok=False,
                    why="test bad schema",
                ),
                TraceStep(
                    index=2,
                    skill="stop_motion",
                    args={"reason": "worker abort after failed move_arm_joints"},
                    result={"ok": True},
                    ok=True,
                    role="worker.safety",
                ),
            ],
            review={
                "verdict": "blocked",
                "root_cause": "skill `move_arm_joints` failed",
                "next_action": "repair args",
                "research_questions": [],
                "used_skills": ["move_arm_joints", "stop_motion"],
                "success": False,
            },
            success=False,
        )

        reply = format_handler_reply(result)

        self.assertIn('`move_arm_joints` args={"arm": "left"', reply)
        self.assertIn("error=side must be left or right", reply)
        self.assertNotIn("`stop_motion` args=", reply)
        self.assertNotIn("role=worker.safety", reply)

    def test_handler_reply_summarizes_control_targets(self) -> None:
        result = RunResult(
            task="oscillate left joint5",
            workspace="/tmp/workspace",
            plan=Plan(task="oscillate left joint5", goal="move joint"),
            trace=[
                TraceStep(
                    index=1,
                    skill="move_arm_joints",
                    args={"side": "left", "positions": [0, 0, 0, 0, 0.5, 0, 0]},
                    result={"ok": True, "action_sent": {"left_joint_5.pos": 0.5}},
                    ok=True,
                ),
                TraceStep(
                    index=2,
                    skill="move_arm_joints",
                    args={"side": "left", "positions": [0, 0, 0, 0, -0.5, 0, 0]},
                    result={"ok": True, "action_sent": {"left_joint_5.pos": -0.5}},
                    ok=True,
                ),
                TraceStep(
                    index=3,
                    skill="stop_motion",
                    args={"reason": "done"},
                    result={"ok": True, "stopped": True},
                    ok=True,
                ),
            ],
            review={
                "verdict": "done",
                "root_cause": "",
                "next_action": "",
                "research_questions": [],
                "used_skills": ["move_arm_joints", "stop_motion"],
                "used_control_skills": ["move_arm_joints", "stop_motion"],
                "success": True,
            },
            success=True,
        )

        reply = format_handler_reply(result)

        self.assertIn("运动日志：发出了 2 次 `move_arm_joints`", reply)
        self.assertIn("left joint_5 -0.500..+0.500 rad", reply)

    def test_handler_marks_auditor_subagent_failure_blocked_without_local_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = _FailingAuditorSubagentClient()
            result = Handler(workspace_root=Path(tmp), agent_client=fake).run(
                task="inspect robot",
                user_request="inspect robot",
                platform=DryRunPlatform(),
            )
            self.assertTrue((Path(result.workspace) / "auditor_agent_error.txt").is_file())

        self.assertFalse(result.success)
        self.assertEqual(result.review["verdict"], "blocked")
        self.assertIn("auditor subagent failed", result.review["root_cause"])
        self.assertIn("no local audit fallback", result.review["next_action"])

    def test_handler_returns_fixable_worker_failure_to_strategist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = _RetrySubagentClient()
            result = Handler(workspace_root=Path(tmp), agent_client=fake).run(
                task="oscillate left joint5",
                user_request="oscillate left joint5",
                platform=DryRunPlatform(),
            )

        self.assertTrue(result.success)
        self.assertGreaterEqual(fake.roles.count("strategist"), 2)
        self.assertTrue(any("failure trace returned to strategist" in note for note in result.notes))
        move_steps = [step for step in result.trace if step.skill == "move_arm_joints"]
        self.assertEqual(len(move_steps), 1)
        self.assertEqual(move_steps[0].args["side"], "left")

    def test_handler_returns_worker_preflight_block_to_strategist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = _GateRetrySubagentClient()
            result = Handler(workspace_root=Path(tmp), agent_client=fake).run(
                task="oscillate left joint5",
                user_request="oscillate left joint5",
                platform=DryRunPlatform(),
            )

        self.assertTrue(result.success)
        self.assertGreaterEqual(fake.roles.count("strategist"), 2)
        self.assertTrue(any("failure trace returned to strategist" in note for note in result.notes))
        self.assertTrue(result.trace)
        self.assertNotEqual(result.trace[0].skill, "worker_gate")

    def test_hei_observation_split(self) -> None:
        raw = {
            "front": _FakeImage((480, 640, 3)),
            "left_joint_1.pos": 0.25,
            "height.pos": -10,
            "status": "ok",
        }
        obs = split_hei_observation(raw)
        self.assertIn("front", obs.images)
        self.assertEqual(obs.state["left_joint_1.pos"], 0.25)
        self.assertEqual(obs.state["height.pos"], -10.0)
        self.assertEqual(obs.extras["status"], "ok")

    def test_observe_skill_returns_numeric_state_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            platform = DryRunPlatform()
            platform.state["left_joint_5.pos"] = 0.25
            context = SkillContext(platform=platform, workspace=_FakeWorkspace(Path(tmp)))
            result = SkillRegistry(include_user=False).dispatch(
                "observe",
                context,
                {"include_images": False, "include_state": True},
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["observation"]["state"]["left_joint_5.pos"], 0.25)
        self.assertIn("left_joint_5.pos", result["observation"]["state_keys"])

    def test_reviewer_skill_update_applies_only_skill_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "skills"
            skill_dir = root / "base" / "perception" / "toy"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: toy\ndescription: toy\ncategory: base/perception\n---\n\n# Toy\n",
                encoding="utf-8",
            )
            (skill_dir / "policy.py").write_text(
                "def dispatch(context, args):\n    return {'ok': True, 'value': 1}\n",
                encoding="utf-8",
            )
            registry = SkillRegistry(roots=[root], include_user=False)
            workspace = _FakeWorkspace(Path(tmp))
            review = {
                "skill_updates": [
                    {
                        "skill_name": "toy",
                        "rationale": "test update",
                        "files": [
                            {
                                "path": "policy.py",
                                "content": "def dispatch(context, args):\n    return {'ok': True, 'value': 2}\n",
                            },
                            {"path": "../escape.py", "content": "x = 1\n"},
                        ],
                    }
                ]
            }

            results = apply_review_skill_updates(review, skills=registry, workspace=workspace)

        self.assertFalse(results[0].ok)
        self.assertIn("unsupported skill file path", results[0].rejected[0])

    def test_reviewer_can_propose_and_register_new_user_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_root = os.environ.get("LOOPMASTER_SKILL_ROOT")
            os.environ["LOOPMASTER_SKILL_ROOT"] = str(Path(tmp) / "user_skills")
            try:
                registry = SkillRegistry(roots=[Path(tmp) / "base"], include_user=True)
                workspace = _FakeWorkspace(Path(tmp))
                review = {
                    "skill_proposals": [
                        {
                            "kind": "new_skill",
                            "skill_name": "toy_new",
                            "category": "learned/control",
                            "rationale": "test new skill",
                            "files": [
                                {
                                    "path": "SKILL.md",
                                    "content": (
                                        "---\n"
                                        "name: toy_new\n"
                                        "description: toy new\n"
                                        "category: learned/control\n"
                                        "---\n\n"
                                        "# Toy New\n"
                                    ),
                                },
                                {
                                    "path": "policy.py",
                                    "content": "def dispatch(context, args):\n    return {'ok': True, 'value': 3}\n",
                                },
                            ],
                        }
                    ]
                }

                results = apply_review_skill_updates(review, skills=registry, workspace=workspace)
                refreshed = SkillRegistry(roots=list(registry.roots), include_user=False)
                result = refreshed.dispatch("toy_new", SkillContext(platform=DryRunPlatform(), workspace=workspace), {})
            finally:
                if old_root is None:
                    os.environ.pop("LOOPMASTER_SKILL_ROOT", None)
                else:
                    os.environ["LOOPMASTER_SKILL_ROOT"] = old_root

        self.assertTrue(results[0].ok)
        self.assertEqual(result["value"], 3)

    def test_reviewer_rejects_skill_policy_without_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_root = os.environ.get("LOOPMASTER_SKILL_ROOT")
            os.environ["LOOPMASTER_SKILL_ROOT"] = str(Path(tmp) / "user_skills")
            try:
                registry = SkillRegistry(roots=[Path(tmp) / "base"], include_user=True)
                workspace = _FakeWorkspace(Path(tmp))
                review = {
                    "skill_proposals": [
                        {
                            "kind": "new_skill",
                            "skill_name": "bad_new",
                            "category": "learned/control",
                            "files": [
                                {
                                    "path": "SKILL.md",
                                    "content": "---\nname: bad_new\ncategory: learned/control\n---\n# Bad\n",
                                },
                                {"path": "policy.py", "content": "def run(context, args):\n    return {'ok': True}\n"},
                            ],
                        }
                    ]
                }

                results = apply_review_skill_updates(review, skills=registry, workspace=workspace)
                refreshed = SkillRegistry(roots=list(registry.roots), include_user=False)
            finally:
                if old_root is None:
                    os.environ.pop("LOOPMASTER_SKILL_ROOT", None)
                else:
                    os.environ["LOOPMASTER_SKILL_ROOT"] = old_root

        self.assertFalse(results[0].ok)
        self.assertIn("policy.py must define callable dispatch", results[0].rejected[0])
        self.assertIsNone(refreshed.get("bad_new"))

    def test_hei_platform_component_interfaces_use_local_actions(self) -> None:
        robot = _FakeHeiRobot()
        platform = HeiRebotLiftPlatform()
        platform._robot = robot

        self.assertEqual(
            platform.command_chassis(x=0.1, y=0.0, theta=0.2),
            {"x.vel": 0.1, "y.vel": 0.0, "theta.vel": 0.2},
        )
        self.assertEqual(robot.actions[-1], {"x.vel": 0.1, "y.vel": 0.0, "theta.vel": 0.2})

        self.assertEqual(platform.set_gripper("right", -0.5), {"right_gripper.pos": -0.5})
        self.assertEqual(robot.actions[-1], {"right_gripper.pos": -0.5})

        self.assertIs(platform.get_head_image(), robot.observation["front"])
        self.assertEqual(set(platform.get_wrist_images()), {"left_wrist", "right_wrist"})

    def test_anygrasp_skill_reports_missing_sdk_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing_sdk = Path(tmp) / "missing_anygrasp_sdk"
            context = SkillContext(platform=DryRunPlatform(), workspace=_FakeWorkspace(Path(tmp)))
            result = SkillRegistry(include_user=False).dispatch(
                "detect_grasps",
                context,
                {
                    "check_only": True,
                    "sdk_root": str(missing_sdk),
                    "python_executable": sys.executable,
                },
            )

        self.assertFalse(result["ok"])
        self.assertIn("AnyGrasp detection directory not found", result["error"])

    def test_grounded_sam2_skill_reports_missing_repo_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing_repo = Path(tmp) / "missing_grounded_sam2"
            context = SkillContext(platform=DryRunPlatform(), workspace=_FakeWorkspace(Path(tmp)))
            result = SkillRegistry(include_user=False).dispatch(
                "grounded_sam2",
                context,
                {
                    "check_only": True,
                    "repo_root": str(missing_repo),
                },
            )

        self.assertFalse(result["ok"])
        self.assertIn("Grounded-SAM2 repo not found", result["error"])


class _FakeImage:
    def __init__(self, shape):
        self.shape = shape
        self.dtype = "uint8"


class _FakeHeiRobot:
    is_connected = True

    def __init__(self) -> None:
        self.actions = []
        self.observation = {
            "front": _FakeImage((480, 640, 3)),
            "left_wrist": _FakeImage((480, 640, 3)),
            "right_wrist": _FakeImage((480, 640, 3)),
            "x.vel": 0.0,
            "y.vel": 0.0,
            "theta.vel": 0.0,
        }

    def send_action(self, action):
        self.actions.append(dict(action))
        return dict(action)

    def get_observation(self):
        return dict(self.observation)

    def disconnect(self):
        pass


class _FakeWorkspace:
    def __init__(self, root: Path) -> None:
        self.root = root


class _FakeSubagentClient:
    profile = "fnyweg"

    def __init__(self) -> None:
        self.roles: list[str] = []

    def run_json(self, *, role: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        self.roles.append(role)
        payload = json.loads(prompt)
        codex = {"profile": self.profile, "session_id": f"fake-{role}", "role": role}
        if role == "handler":
            direct = "你现在能干嘛" in str(payload["user_request"])
            return {
                "route": "direct_response" if direct else "strategist",
                "direct_response": "fake direct capability answer" if direct else "",
                "run_intent": payload["task"],
                "handoff_notes": ["fake handler handoff"],
                "safety_notes": ["fake safety"],
                "_codex": codex,
            }
        if role == "strategist":
            candidate = payload["candidate_plan"]
            steps = [
                {"name": step["name"], "args_json": json.dumps(step["args"]), "why": step["why"]}
                for step in candidate["steps"]
            ]
            return {
                "goal": candidate["goal"],
                "steps": steps,
                "success_criteria": candidate["success_criteria"],
                "risks": candidate["risks"],
                "assumptions": candidate["assumptions"],
                "research_questions": candidate["research_questions"],
                "subagent_notes": ["fake strategist plan"],
                "_codex": codex,
            }
        if role == "worker":
            return {
                "proceed": True,
                "execution_notes": ["fake worker preflight"],
                "concerns": [],
                "_codex": codex,
            }
        if role == "auditor":
            review = payload["candidate_review"]
            return {
                **review,
                "notes": ["fake auditor review"],
                "skill_updates": [],
                "skill_proposals": [],
                "_codex": codex,
            }
        raise AssertionError(f"unexpected role {role}")


class _RetrySubagentClient(_FakeSubagentClient):
    def __init__(self) -> None:
        super().__init__()
        self.strategist_calls = 0

    def run_json(self, *, role: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        if role != "strategist":
            return super().run_json(role=role, prompt=prompt, schema=schema)

        self.roles.append(role)
        self.strategist_calls += 1
        payload = json.loads(prompt)
        codex = {"profile": self.profile, "session_id": f"fake-{role}", "role": role}
        bad_args = {"arm": "left", "positions": {"joint_5": 0.5}}
        good_args = {"side": "left", "positions": {"joint_5": 0.5}}
        args = good_args if "failed_trace" in payload else bad_args
        return {
            "goal": payload["user_request"],
            "steps": [
                {"name": "move_arm_joints", "args_json": json.dumps(args), "why": "test retry"},
                {
                    "name": "stop_motion",
                    "args_json": json.dumps({"reason": "test end"}),
                    "why": "safety stop",
                },
            ],
            "success_criteria": ["test retry succeeds"],
            "risks": [],
            "assumptions": [],
            "research_questions": [],
            "subagent_notes": ["fake strategist retry plan" if "failed_trace" in payload else "fake bad plan"],
            "_codex": codex,
        }


class _GateRetrySubagentClient(_RetrySubagentClient):
    def __init__(self) -> None:
        super().__init__()
        self.worker_calls = 0

    def run_json(self, *, role: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        if role != "worker":
            return super().run_json(role=role, prompt=prompt, schema=schema)

        self.roles.append(role)
        self.worker_calls += 1
        codex = {"profile": self.profile, "session_id": f"fake-{role}", "role": role}
        if self.worker_calls == 1:
            return {
                "proceed": False,
                "execution_notes": ["plan uses stale joint constants"],
                "concerns": ["derive targets from fresh observe first"],
                "_codex": codex,
            }
        return {
            "proceed": True,
            "execution_notes": ["retry plan is executable"],
            "concerns": [],
            "_codex": codex,
        }


class _FailingAuditorSubagentClient(_FakeSubagentClient):
    def run_json(self, *, role: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        if role == "auditor":
            self.roles.append(role)
            raise RuntimeError("model list response missing field models")
        return super().run_json(role=role, prompt=prompt, schema=schema)


if __name__ == "__main__":
    unittest.main()
