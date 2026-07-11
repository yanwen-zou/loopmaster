from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
import threading
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
from loopmaster_agentic.skills.registry import SHIPPED_ROOT, SkillContext, SkillRegistry
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

    def test_repo_skill_surface_lists_registered_skills(self) -> None:
        skills = SkillRegistry(include_user=False).list()
        names = {skill.name for skill in skills}
        self.assertEqual(
            names,
            {
                "capture_image",
                "create_skill",
                "detect_grasps",
                "grasp_target",
                "grounded_sam2",
                "init_arms",
                "move_arm_ee",
                "move_arm_joints",
                "navigation",
                "object_region_index",
                "observe",
                "oscillate_arm_joint",
                "play_cache_traj",
                "set_base_velocity",
                "set_gripper",
                "set_lift_height",
                "stop_motion",
                "timer",
                "wander",
            },
        )
        forbidden_terms = ("atomic", "zeroshot", "robotwin", "sim")
        searchable = "\n".join(f"{skill.category}/{skill.name}" for skill in skills).lower()
        for term in forbidden_terms:
            self.assertNotIn(term, searchable)

    def test_navigation_skill_sends_map_goal_and_records_goal_id(self) -> None:
        from loopmaster_agentic.skills.navigation.navigation import policy

        sent_payloads = []
        status = {
            "pose": {"x": 1.0, "y": 2.0, "yaw": 0.5},
            "navigation": {"state": "navigating", "goal_id": "g1", "distance_remaining": 0.7},
            "last_command_ack": {
                "type": "navigate_to_pose",
                "accepted": True,
                "message": "Nav2 accepted the goal",
                "goal_id": "g1",
            },
        }

        def fake_send(robot_ip, command_port, payload, send_timeout_ms):
            sent_payloads.append(payload)
            return {"ok": True, "endpoint": f"tcp://{robot_ip}:{command_port}"}

        with mock.patch.object(policy, "_send_json", side_effect=fake_send), mock.patch.object(
            policy, "_receive_status", return_value=status
        ):
            context = SkillContext(platform=DryRunPlatform(), workspace=new_workspace("nav", root=Path("/tmp")))
            result = policy.dispatch(context, {"x": 1.5, "y": -0.8, "yaw": 1.57, "goal_id": "g1"})

        self.assertTrue(result["ok"])
        self.assertTrue(result["ack_received"])
        self.assertEqual(result["goal_id"], "g1")
        self.assertEqual(context.memory[policy.MEMORY_LAST_GOAL_ID], "g1")
        self.assertEqual(sent_payloads[0]["type"], "navigate_to_pose")
        self.assertEqual(sent_payloads[0]["frame_id"], "map")
        self.assertEqual(sent_payloads[0]["x"], 1.5)

    def test_navigation_skill_rejects_invalid_goal_number(self) -> None:
        from loopmaster_agentic.skills.navigation.navigation import policy

        context = SkillContext(platform=DryRunPlatform(), workspace=new_workspace("nav_bad", root=Path("/tmp")))
        result = policy.dispatch(context, {"x": "bad", "y": 0, "yaw": 0})

        self.assertFalse(result["ok"])
        self.assertIn("x must be a number", result["error"])

    def test_wander_skill_samples_valid_map_goal_and_uses_navigation(self) -> None:
        from loopmaster_agentic.skills.navigation.wander import policy

        calls = []

        def fake_navigation_dispatch(context, args):
            calls.append(dict(args))
            if args["command"] == "status":
                return {
                    "ok": True,
                    "status": {
                        "pose": {"x": -3.0, "y": -4.5, "yaw": 0.25},
                        "navigation": {"state": "succeeded", "goal_id": "previous"},
                    },
                }
            if args["command"] == "goal":
                return {
                    "ok": True,
                    "command": "navigate_to_pose",
                    "goal_id": args["goal_id"],
                    "target": {"x": args["x"], "y": args["y"], "yaw": args["yaw"]},
                }
            return {"ok": False, "error": "unexpected command"}

        context = SkillContext(platform=DryRunPlatform(), workspace=new_workspace("wander", root=Path("/tmp")))
        with mock.patch.object(policy.navigation_policy, "dispatch", side_effect=fake_navigation_dispatch):
            result = policy.dispatch(
                context,
                {
                    "radius_m": 5.0,
                    "min_radius_m": 0.5,
                    "clearance_m": 0.25,
                    "interval_s": 0.0,
                    "max_goals": 1,
                    "seed": 7,
                    "yaw_strategy": "toward_goal",
                    "goal_id": "wander-test",
                },
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual([call["command"] for call in calls], ["status", "goal"])
        sent = calls[-1]
        self.assertEqual(sent["goal_id"], "wander-test")
        self.assertLessEqual(math.hypot(sent["x"] + 3.0, sent["y"] + 4.5), 5.0)
        occ_map = policy._load_map(policy.DEFAULT_MAP_YAML, free_min_value=250)
        self.assertTrue(occ_map.is_valid_world(sent["x"], sent["y"], clearance_m=0.25))

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
                "y.vel": 0.05,
            }
        )

        self.assertEqual(sent["right_joint_1.pos"], -0.3)
        self.assertEqual(sent["right_joint_2.pos"], 0.0)
        self.assertEqual(sent["left_joint_1.pos"], 0.3)
        self.assertEqual(sent["right_gripper.pos"], 0.0)
        self.assertEqual(sent["x.vel"], 0.2)
        self.assertEqual(sent["y.vel"], 0.05)
        self.assertEqual(fake.actions[-1]["x.vel"], -0.2)
        self.assertEqual(fake.actions[-1]["y.vel"], -0.05)

        arm_sent = platform.command_arm("left", {"joint_1": -99.0, "joint_4": 99.0})
        self.assertEqual(arm_sent["left_joint_1.pos"], -1.5)
        self.assertEqual(arm_sent["left_joint_4.pos"], 1.57)

        gripper_sent = platform.set_gripper("right", 99.0)
        self.assertEqual(gripper_sent["right_gripper.pos"], 0.0)
        gripper_open = platform.set_gripper("right", -99.0)
        self.assertEqual(gripper_open["right_gripper.pos"], -5.0)

    def test_move_arm_ee_skill_returns_structured_ik_errors(self) -> None:
        from loopmaster_agentic.skills.control.move_arm_ee import policy

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

    def test_move_arm_ee_skill_uses_velocity_limit_without_waypoints(self) -> None:
        from loopmaster_agentic.skills.control.move_arm_ee import policy

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
                    "velocity_limit_rad_s": 0.4,
                },
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["trajectory"], [])
        self.assertAlmostEqual(platform.actions[-1]["right_joint_1.pos"], 0.12)
        self.assertIn("left_joint_1.pos", platform.actions[-1])
        self.assertEqual(result["velocity_limit_rad_s"], 0.4)

    def test_move_arm_ee_skill_uses_explicit_current_positions(self) -> None:
        from loopmaster_agentic.skills.control.move_arm_ee import policy

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
                    "velocity_limit_rad_s": 0.4,
                },
            )

        self.assertTrue(result["ok"])
        self.assertEqual(solve.call_args.kwargs["current_positions"], current)
        self.assertEqual(result["trajectory"], [])
        self.assertAlmostEqual(platform.actions[0]["right_joint_1.pos"], 0.2)
        self.assertIn("left_joint_1.pos", platform.actions[0])

    def test_move_arm_ee_skill_holds_other_arm_positions(self) -> None:
        from loopmaster_agentic.skills.control.move_arm_ee import policy

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
                    "velocity_limit_rad_s": 0.4,
                },
            )

        self.assertTrue(result["ok"])
        self.assertEqual(platform.actions[0]["left"], other)
        self.assertAlmostEqual(platform.actions[-1]["right"]["joint_1"], 0.2)

    def test_move_arm_ee_parses_prefixed_state_and_holds_other_arm(self) -> None:
        from loopmaster_agentic.skills.control.move_arm_ee import policy

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
        fake_ik["positions"]["joint_5"] = 0.4
        state = {}
        for side in ("right", "left"):
            for joint in policy.JOINTS:
                state[f"{side}_{joint}.pos"] = 0.1 if side == "right" else -0.2

        platform = Platform()
        with mock.patch.object(policy, "solve_arm_ee_dict", return_value=fake_ik) as solve:
            result = policy.dispatch(
                SkillContext(platform=platform, workspace=new_workspace("move_ee_prefixed_state", root=Path("/tmp"))),
                {
                    "side": "right",
                    "pose": {"position": [0.2, 0.0, 0.3]},
                    "current_positions": state,
                    "other_arm_positions": state,
                    "velocity_limit_rad_s": 0.4,
                },
            )

        self.assertTrue(result["ok"])
        self.assertEqual(solve.call_args.kwargs["current_positions"]["joint_1"], 0.1)
        self.assertEqual(platform.actions[-1]["left"], {joint: -0.2 for joint in policy.JOINTS})
        self.assertEqual(set(platform.actions[-1]["right"]), set(policy.JOINTS))

    def test_move_arm_joints_skill_can_command_both_arms(self) -> None:
        from loopmaster_agentic.skills.control.move_arm_joints import policy

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
            {"side": "both", "positions": positions, "velocity_limit_rad_s": 0.4},
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["trajectory"], [])
        self.assertEqual(platform.actions[-1]["right"], positions)
        self.assertEqual(platform.actions[-1]["left"], positions)

    def test_move_arm_joints_preserves_unspecified_joints_from_current_state(self) -> None:
        from loopmaster_agentic.skills.control.move_arm_joints import policy

        platform = DryRunPlatform()
        for joint in policy.JOINTS:
            platform.state[f"left_{joint}.pos"] = -0.3
        result = policy.dispatch(
            SkillContext(platform=platform, workspace=new_workspace("move_joints_preserve", root=Path("/tmp"))),
            {"side": "left", "positions": {"joint_5": 0.5}, "velocity_limit_rad_s": 0.4},
        )

        self.assertTrue(result["ok"])
        sent = result["action_sent"]
        self.assertEqual(set(sent), {f"left_{joint}.pos" for joint in policy.JOINTS})
        self.assertEqual(sent["left_joint_5.pos"], 0.5)
        self.assertEqual(sent["left_joint_1.pos"], -0.3)

    def test_init_arms_skill_commands_and_verifies_registered_pose(self) -> None:
        platform = DryRunPlatform()
        workspace = new_workspace("init_arms", root=Path("/tmp"))
        registry = SkillRegistry(include_user=False)

        result = registry.dispatch(
            "init_arms",
            SkillContext(platform=platform, workspace=workspace),
            {"settle_s": 0.0, "tolerance_rad": 0.001, "velocity_limit_rad_s": 0.4},
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["verified"]["ok"])
        self.assertEqual(result["trajectory"], [])
        self.assertAlmostEqual(platform.state["right_joint_3.pos"], result["positions"]["joint_3"])
        self.assertAlmostEqual(platform.state["left_joint_3.pos"], result["positions"]["joint_3"])

    def test_init_arms_verification_prefers_direct_arm_feedback_over_stale_observation(self) -> None:
        class Platform(DryRunPlatform):
            def __init__(self) -> None:
                super().__init__()
                self.arm_targets: dict[str, dict[str, float]] = {"right": {}, "left": {}}

            def command_arms(self, *, right=None, left=None, velocity_limit_rad_s=None) -> dict[str, float]:
                if right is not None:
                    self.arm_targets["right"] = dict(right)
                if left is not None:
                    self.arm_targets["left"] = dict(left)
                return {
                    f"{side}_{joint}.pos": float(value)
                    for side, positions in self.arm_targets.items()
                    for joint, value in positions.items()
                }

            def read_arm_positions(self, side: str | None = None) -> dict[str, float]:
                if side is None:
                    return {
                        f"{current_side}_{joint}.pos": float(value)
                        for current_side, positions in self.arm_targets.items()
                        for joint, value in positions.items()
                    }
                return dict(self.arm_targets[side])

        platform = Platform()
        result = SkillRegistry(include_user=False).dispatch(
            "init_arms",
            SkillContext(platform=platform, workspace=new_workspace("init_arms_direct_feedback", root=Path("/tmp"))),
            {"settle_s": 0.0, "verify_timeout_s": 0.0},
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["verified"]["source"], "read_arm_positions")
        self.assertTrue(result["verified"]["ok"])

    def test_play_cache_traj_skill_replays_episode_and_returns_to_init(self) -> None:
        platform = DryRunPlatform()
        workspace = new_workspace("play_cache_traj", root=Path("/tmp"))
        registry = SkillRegistry(include_user=False)
        context = SkillContext(platform=platform, workspace=workspace)

        def call_skill(name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
            return registry.dispatch(name, context, args or {})

        setattr(context, "call_skill", call_skill)
        result = registry.dispatch(
            "play_cache_traj",
            context,
            {"episode": 0, "max_frames": 2, "settle_s": 0.0},
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["episode"], 0)
        self.assertEqual(result["total_episodes"], 5)
        self.assertEqual(result["sent_frames"], 2)
        self.assertEqual(result["return_to_init"]["stop_motion"]["stopped"], True)
        self.assertIsNone(result["return_to_init"]["init_arms"]["verified"])
        init_positions = result["return_to_init"]["init_arms"]["positions"]
        self.assertAlmostEqual(platform.state["right_joint_3.pos"], init_positions["joint_3"])
        self.assertGreaterEqual(len(platform.actions), 4)

    def test_play_cache_traj_skill_rejects_unknown_episode_before_motion(self) -> None:
        platform = DryRunPlatform()
        result = SkillRegistry(include_user=False).dispatch(
            "play_cache_traj",
            SkillContext(platform=platform, workspace=new_workspace("play_cache_traj_bad", root=Path("/tmp"))),
            {"episode": 5, "max_frames": 1},
        )

        self.assertFalse(result["ok"])
        self.assertIn("episode must be in [0, 4]", result["error"])
        self.assertEqual(platform.actions, [])

    def test_set_gripper_uses_signed_open_close_convention(self) -> None:
        from loopmaster_agentic.skills.control.set_gripper import policy

        context = SkillContext(platform=DryRunPlatform(), workspace=new_workspace("gripper", root=Path("/tmp")))

        bad = policy.dispatch(context, {"side": "right", "position": 1.0})
        self.assertFalse(bad["ok"])
        self.assertIn("-5.0", bad["error"])

        opened = policy.dispatch(context, {"side": "right", "position": -5.0})
        self.assertTrue(opened["ok"])
        self.assertEqual(opened["commanded_position"], -5.0)
        self.assertEqual(opened["action_sent"]["right_gripper.pos"], -5.0)

        closed = policy.dispatch(context, {"side": "right", "position": 0.0})
        self.assertTrue(closed["ok"])
        self.assertEqual(closed["commanded_position"], 0.0)

    def test_set_gripper_filters_full_action_vector_and_can_verify_feedback(self) -> None:
        from loopmaster_agentic.skills.control.set_gripper import policy

        class Platform(DryRunPlatform):
            def set_gripper(self, side: str, position: float) -> dict[str, float]:
                key = f"{side}_gripper.pos"
                self.state[key] = float(position)
                sent = {control_key: 0.0 for control_key in self.state}
                sent[key] = float(position)
                return sent

        platform = Platform()
        platform.state["right_gripper.pos"] = -5.0
        context = SkillContext(platform=platform, workspace=new_workspace("gripper_filter", root=Path("/tmp")))

        result = policy.dispatch(context, {"side": "right", "position": 0.0, "verify": True})

        self.assertTrue(result["ok"])
        self.assertEqual(result["action_sent"], {"right_gripper.pos": 0.0})
        self.assertTrue(result["verified"]["ok"])
        self.assertNotIn("right_joint_1.pos", result["action_sent"])
        self.assertNotIn("left_joint_1.pos", result["action_sent"])

    def test_set_base_velocity_filters_full_action_vector(self) -> None:
        from loopmaster_agentic.skills.control.set_base_velocity import policy

        class Platform(DryRunPlatform):
            def command_chassis(self, x=0.0, y=0.0, theta=0.0) -> dict[str, float]:
                self.send_action({"x.vel": x, "y.vel": y, "theta.vel": theta})
                sent = {control_key: 0.0 for control_key in self.state}
                sent.update({"x.vel": x, "y.vel": y, "theta.vel": theta})
                return sent

        context = SkillContext(platform=Platform(), workspace=new_workspace("base_filter", root=Path("/tmp")))

        result = policy.dispatch(context, {"x": 0.1, "y": 0.0, "theta": 0.0})

        self.assertTrue(result["ok"])
        self.assertEqual(result["action_sent"], {"x.vel": 0.1, "y.vel": 0.0, "theta.vel": 0.0})
        self.assertNotIn("right_joint_1.pos", result["action_sent"])
        self.assertNotIn("height.pos", result["action_sent"])

    def test_lift_and_stop_support_settle_windows(self) -> None:
        from loopmaster_agentic.skills.control.set_lift_height import policy as lift_policy
        from loopmaster_agentic.skills.control.stop_motion import policy as stop_policy

        platform = DryRunPlatform()
        context = SkillContext(platform=platform, workspace=new_workspace("settle_controls", root=Path("/tmp")))
        with mock.patch.object(lift_policy.time, "sleep", return_value=None) as lift_sleep:
            lift = lift_policy.dispatch(context, {"height_mm": 12.0, "settle_s": 0.25})
        with mock.patch.object(stop_policy.time, "sleep", return_value=None) as stop_sleep:
            stop = stop_policy.dispatch(context, {"reason": "done", "settle_s": 0.5})

        self.assertTrue(lift["ok"])
        self.assertEqual(lift["settle_s"], 0.25)
        lift_sleep.assert_called_once_with(0.25)
        self.assertTrue(stop["ok"])
        self.assertEqual(stop["settle_s"], 0.5)
        stop_sleep.assert_called_once_with(0.5)

    def test_timer_skill_records_elapsed_and_wall_time(self) -> None:
        from loopmaster_agentic.skills.meta.timer import policy

        context = SkillContext(platform=DryRunPlatform(), workspace=new_workspace("timer", root=Path("/tmp")))
        with mock.patch.object(policy.time, "sleep", return_value=None) as sleep:
            result = policy.dispatch(context, {"duration_s": 0.25, "label": "between actions"})

        self.assertTrue(result["ok"])
        self.assertEqual(result["label"], "between actions")
        self.assertEqual(result["slept_s"], 0.25)
        self.assertGreaterEqual(result["elapsed_s"], 0.0)
        self.assertIn("T", result["started_wall_time"])
        self.assertIn("T", result["ended_wall_time"])
        sleep.assert_called_once_with(0.25)

    def test_move_arm_ee_position_only_target_ignores_orientation(self) -> None:
        from loopmaster_agentic.skills.control.move_arm_ee import policy

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

    def test_arm_target_z_safety_clip(self) -> None:
        from loopmaster_agentic.ik.mink_ik import MIN_ARM_TARGET_Z, _clip_arm_target_z

        pose = [[1, 0, 0, 0.2], [0, 1, 0, 0.0], [0, 0, 1, -0.3], [0, 0, 0, 1]]

        self.assertTrue(_clip_arm_target_z(pose))
        self.assertEqual(pose[2][3], MIN_ARM_TARGET_Z)

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
                task="set right gripper position=-0.25",
                user_request="set right gripper position=-0.25",
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
            skill_root = Path(tmp) / "skills"
            for name in ("observe", "stop_motion"):
                skill_dir = skill_root / "perception" / name if name == "observe" else skill_root / "control" / name
                skill_dir.mkdir(parents=True)
                (skill_dir / "SKILL.md").write_text(
                    f"---\nname: {name}\ncategory: {'perception' if name == 'observe' else 'control'}\n---\n# {name}\n",
                    encoding="utf-8",
                )
                (skill_dir / "policy.py").write_text(
                    "def dispatch(context, args):\n    return {'ok': True}\n",
                    encoding="utf-8",
                )
            result = Handler(
                workspace_root=Path(tmp),
                skills=SkillRegistry(roots=[skill_root], include_user=False),
            ).run(
                task="pick up the red block",
                user_request="pick up the red block",
                platform=DryRunPlatform(),
            )
            self.assertFalse(result.success)
            self.assertEqual(result.review["verdict"], "research_needed")
            self.assertTrue(result.review["research_questions"])

    def test_strategist_uses_cache_traj_perception_chain_for_web_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = SkillRegistry(roots=[SHIPPED_ROOT], include_user=False)
            workspace = new_workspace("web_order_plan", root=Path(tmp) / "workspaces")

            plan = Strategist().plan(
                task="抓取「可口可乐 330ml」x1 交付顾客",
                user_request=(
                    "抓取「可口可乐 330ml」x1 交付顾客\n\n"
                    'Order payload: [{"id": 1, "name": "可口可乐 330ml", "qty": 1}]'
                ),
                workspace=workspace,
                skills=registry,
            )

        names = [step.name for step in plan.steps]
        self.assertIn("capture_image", names)
        self.assertIn("grounded_sam2", names)
        self.assertIn("object_region_index", names)
        self.assertIn("play_cache_traj", names)
        self.assertNotIn("grasp_target", names)
        chain = [name for name in names if name in {"capture_image", "grounded_sam2", "object_region_index", "play_cache_traj"}]
        self.assertEqual(chain, ["capture_image", "grounded_sam2", "object_region_index", "play_cache_traj"])
        grounded = next(step for step in plan.steps if step.name == "grounded_sam2")
        region = next(step for step in plan.steps if step.name == "object_region_index")
        replay = next(step for step in plan.steps if step.name == "play_cache_traj")
        self.assertEqual(grounded.args["text_prompt"], "可口可乐 330ml.")
        self.assertEqual(region.args["annotation"], {"$ref": "grounded_sam2.annotations.0"})
        self.assertEqual(replay.args["episode"], {"$ref": "object_region_index.index"})
        self.assertFalse(plan.research_questions)

    def test_server_bridge_prefers_skill_delivered_items(self) -> None:
        from loopmaster_agentic.server_bridge import _result_delivered_items

        result = RunResult(
            task="order",
            workspace="/tmp/run",
            plan=Plan(task="order", goal="order"),
            trace=[
                TraceStep(
                    index=1,
                    skill="grasp_target",
                    args={},
                    result={"ok": True, "delivered_items": [{"id": 1, "delivered": 1}]},
                    ok=True,
                )
            ],
            review={"success": True},
            success=True,
        )

        delivered = _result_delivered_items(result, [{"id": 1, "qty": 3}])

        self.assertEqual(delivered, [{"id": 1, "delivered": 1}])

    def test_object_region_index_falls_back_to_random_episode(self) -> None:
        from loopmaster_agentic.skills.perception.object_region_index import policy

        context = SimpleNamespace(memory={})
        with mock.patch.object(policy.random, "randint", return_value=3):
            result = policy.dispatch(context, {})

        self.assertTrue(result["ok"])
        self.assertEqual(result["index"], 3)
        self.assertEqual(result["episode"], 3)
        self.assertTrue(result["fallback_used"])

    def test_server_bridge_web_order_bypasses_handler_for_direct_skill_chain(self) -> None:
        from loopmaster_agentic.server_bridge import ServerBridge, ServerBridgeConfig

        class Client:
            config = ServerBridgeConfig(base_url="http://test", task_timeout_s=5)

            def __init__(self) -> None:
                self.report = None

            def claim_task(self, task_id: int) -> dict[str, Any]:
                return {"ok": True}

            def post_exec_log(self, **kwargs) -> dict[str, Any]:
                return {"ok": True}

            def push_run_dir(self, run_dir: Path) -> dict[str, Any]:
                return {"ok": True}

            def report_task(self, **kwargs) -> dict[str, Any]:
                self.report = kwargs
                return {"ok": True}

        class Skills:
            def __init__(self) -> None:
                self.play_args = None
                self.registry = SkillRegistry(include_user=False)

            def dispatch(self, name: str, context: Any, args: dict[str, Any]) -> dict[str, Any]:
                if name == "capture_image":
                    return {"ok": True, "rgb": {"path": "/tmp/rgb.png"}}
                if name == "grounded_sam2":
                    return {"ok": True, "annotation_count": 0, "annotations": []}
                if name == "object_region_index":
                    return self.registry.dispatch(name, context, args)
                if name == "play_cache_traj":
                    self.play_args = dict(args)
                    return {"ok": True, "episode": args["episode"], "sent_frames": 1}
                return {"ok": False, "error": f"unexpected skill {name}"}

        class HandlerStub:
            workspace_root = None

            def __init__(self) -> None:
                self.skills = Skills()

            def run(self, **kwargs):
                raise AssertionError("web order should bypass Handler.run")

        class Platform:
            name = "test"

            def connect(self) -> None:
                pass

            def close(self) -> None:
                pass

        client = Client()
        handler = HandlerStub()
        bridge = ServerBridge(client=client, handler=handler, platform=Platform())

        with mock.patch("loopmaster_agentic.skills.perception.object_region_index.policy.random.randint", return_value=2):
            result = bridge.process_task(
                {
                    "id": 12,
                    "order_id": 20,
                    "instruction": "抓取「德芙巧克力」x1 交付顾客",
                    "payload": '[{"id": 4, "name": "德芙巧克力", "qty": 1}]',
                }
            )

        self.assertIsNotNone(result)
        self.assertTrue(result.success)
        self.assertEqual(handler.skills.play_args["episode"], 2)
        self.assertEqual(client.report["status"], "done")

    def test_server_bridge_continues_when_claim_endpoint_is_unsupported(self) -> None:
        from loopmaster_agentic.server_bridge import ServerBridge, ServerBridgeConfig

        class Client:
            config = ServerBridgeConfig(base_url="http://test", task_timeout_s=5)

            def __init__(self) -> None:
                self.report = None
                self.exec_codes = []
                self.db_rows = []

            def claim_task(self, task_id: int) -> dict[str, Any]:
                raise RuntimeError("POST /api/tasks/57/claim failed with HTTP 405: Method Not Allowed")

            def upsert_db_row(self, table: str, row: dict[str, Any]) -> dict[str, Any]:
                self.db_rows.append((table, row))
                return {"ok": True}

            def post_exec_log(self, **kwargs) -> dict[str, Any]:
                self.exec_codes.append(kwargs.get("code"))
                return {"ok": True}

            def push_run_dir(self, run_dir: Path) -> dict[str, Any]:
                return {"ok": True}

            def report_task(self, **kwargs) -> dict[str, Any]:
                self.report = kwargs
                return {"ok": True}

        class Skills:
            def dispatch(self, name: str, context: Any, args: dict[str, Any]) -> dict[str, Any]:
                if name == "capture_image":
                    return {"ok": True, "rgb": {"path": "/tmp/rgb.png"}}
                if name == "grounded_sam2":
                    return {"ok": True, "annotation_count": 0, "annotations": []}
                if name == "object_region_index":
                    return {"ok": True, "index": 1, "episode": 1, "fallback_used": True}
                if name == "play_cache_traj":
                    return {"ok": True, "episode": args["episode"], "sent_frames": 1}
                return {"ok": False, "error": f"unexpected skill {name}"}

        class HandlerStub:
            workspace_root = None
            skills = Skills()

        class Platform:
            def connect(self) -> None:
                pass

            def close(self) -> None:
                pass

        client = Client()
        bridge = ServerBridge(client=client, handler=HandlerStub(), platform=Platform())
        result = bridge.process_task(
            {
                "id": 57,
                "order_id": 65,
                "instruction": "pick cake x1 deliver_to_customer",
                "payload": '[{"id": 9, "name": "cake", "qty": 1}]',
            }
        )

        self.assertIsNotNone(result)
        self.assertTrue(result.success)
        self.assertIn("CLAIM_UNSUPPORTED_CONTINUING", client.exec_codes)
        self.assertEqual(client.db_rows[0][0], "tasks")
        self.assertEqual(client.db_rows[0][1]["id"], 57)
        self.assertEqual(client.db_rows[0][1]["status"], "running")
        self.assertEqual(client.report["status"], "done")

    def test_server_bridge_treats_already_settled_report_as_idempotent(self) -> None:
        from loopmaster_agentic.server_bridge import ServerBridge, ServerBridgeConfig

        class Client:
            config = ServerBridgeConfig(base_url="http://test", task_timeout_s=5)

            def __init__(self) -> None:
                self.db_rows = []

            def claim_task(self, task_id: int) -> dict[str, Any]:
                return {"ok": True}

            def upsert_db_row(self, table: str, row: dict[str, Any]) -> dict[str, Any]:
                self.db_rows.append((table, row))
                return {"ok": True}

            def post_exec_log(self, **kwargs) -> dict[str, Any]:
                return {"ok": True}

            def push_run_dir(self, run_dir: Path) -> dict[str, Any]:
                return {"ok": True}

            def report_task(self, **kwargs) -> dict[str, Any]:
                raise RuntimeError(
                    'POST /api/tasks/35/report failed with HTTP 409: {"msg":"\\u4efb\\u52a1\\u5df2\\u7ed3\\u7b97","ok":false}'
                )

        class Skills:
            def dispatch(self, name: str, context: Any, args: dict[str, Any]) -> dict[str, Any]:
                if name == "capture_image":
                    return {"ok": True, "rgb": {"path": "/tmp/rgb.png"}}
                if name == "grounded_sam2":
                    return {"ok": True, "annotation_count": 0, "annotations": []}
                if name == "object_region_index":
                    return {"ok": True, "index": 0, "episode": 0, "fallback_used": True}
                if name == "play_cache_traj":
                    return {"ok": True, "episode": args["episode"], "sent_frames": 1}
                return {"ok": False, "error": f"unexpected skill {name}"}

        class HandlerStub:
            workspace_root = None
            skills = Skills()

        class Platform:
            def connect(self) -> None:
                pass

            def close(self) -> None:
                pass

        bridge = ServerBridge(client=Client(), handler=HandlerStub(), platform=Platform())
        result = bridge.process_task(
            {
                "id": 35,
                "order_id": 40,
                "instruction": "pick bottled water x1 deliver to customer",
                "payload": '[{"id": 6, "name": "农夫山泉 550ml", "qty": 1}]',
            }
        )

        self.assertIsNotNone(result)
        self.assertTrue(result.success)

    def test_server_bridge_marks_report_405_finished_locally_to_avoid_repeat(self) -> None:
        from loopmaster_agentic.server_bridge import ServerBridge, ServerBridgeConfig

        class Client:
            config = ServerBridgeConfig(base_url="http://test", task_timeout_s=5)

            def __init__(self) -> None:
                self.db_rows = []

            def claim_task(self, task_id: int) -> dict[str, Any]:
                return {"ok": True}

            def upsert_db_row(self, table: str, row: dict[str, Any]) -> dict[str, Any]:
                self.db_rows.append((table, row))
                return {"ok": True}

            def post_exec_log(self, **kwargs) -> dict[str, Any]:
                return {"ok": True}

            def push_run_dir(self, run_dir: Path) -> dict[str, Any]:
                return {"ok": True}

            def report_task(self, **kwargs) -> dict[str, Any]:
                raise RuntimeError("POST /api/tasks/65/report failed with HTTP 405: Method Not Allowed")

        class Skills:
            def __init__(self) -> None:
                self.replay_count = 0

            def dispatch(self, name: str, context: Any, args: dict[str, Any]) -> dict[str, Any]:
                if name == "capture_image":
                    return {"ok": True, "rgb": {"path": "/tmp/rgb.png"}}
                if name == "grounded_sam2":
                    return {"ok": True, "annotation_count": 0, "annotations": []}
                if name == "object_region_index":
                    return {"ok": True, "index": 2, "episode": 2, "fallback_used": True}
                if name == "play_cache_traj":
                    self.replay_count += 1
                    return {"ok": True, "episode": args["episode"], "sent_frames": 1}
                return {"ok": False, "error": f"unexpected skill {name}"}

        class HandlerStub:
            workspace_root = None

            def __init__(self) -> None:
                self.skills = Skills()

        class Platform:
            def connect(self) -> None:
                pass

            def close(self) -> None:
                pass

        handler = HandlerStub()
        client = Client()
        bridge = ServerBridge(client=client, handler=handler, platform=Platform())
        task = {
            "id": 65,
            "order_id": 70,
            "instruction": "pick custom x1 deliver_to_customer",
            "payload": '[{"id": 10, "name": "custom", "qty": 1}]',
        }

        first = bridge.process_task(task)
        second = bridge.process_task(task)

        self.assertIsNotNone(first)
        self.assertTrue(first.success)
        self.assertIsNone(second)
        self.assertEqual(handler.skills.replay_count, 1)
        self.assertEqual(client.db_rows[0][0], "tasks")
        self.assertEqual(client.db_rows[0][1]["id"], 65)
        self.assertEqual(client.db_rows[0][1]["status"], "done")

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

    def test_worker_prompt_exposes_registered_init_arms_skill(self) -> None:
        class CapturingWorkerAgent:
            def run_json(self, *, role: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
                self.payload = json.loads(prompt)
                return {"proceed": True, "execution_notes": [], "concerns": []}

        with tempfile.TemporaryDirectory() as tmp:
            agent = CapturingWorkerAgent()
            Worker().execute(
                plan=Plan(
                    task="init arms",
                    goal="init arms",
                    steps=[SkillCall("init_arms", {"settle_s": 0.0, "velocity_limit_rad_s": 0.4}, "registered init")],
                ),
                workspace=new_workspace("worker_init_prompt", root=Path(tmp) / "workspaces"),
                platform=DryRunPlatform(),
                skills=SkillRegistry(include_user=False),
                agent_client=agent,
            )

        names = {skill["name"] for skill in agent.payload["available_skills"]}
        self.assertIn("init_arms", names)
        self.assertIn("registered arm initialization skill", agent.payload["contract"])

    def test_worker_prompt_allows_bounded_low_level_backward_base_motion(self) -> None:
        from loopmaster_agentic.agents.worker import _worker_prompt

        prompt = _worker_prompt(
            plan=Plan(
                task="move backward",
                goal="move backward for 5 seconds",
                steps=[
                    SkillCall(
                        "set_base_velocity",
                        {"x": -0.1, "y": 0.0, "theta": 0.0, "duration_s": 5.0, "refresh_hz": 5.0},
                        "bounded low-level operator command",
                    ),
                    SkillCall("observe", {"include_state": True}, "check motion feedback"),
                    SkillCall("stop_motion", {"reason": "done"}, "safety stop"),
                    SkillCall("observe", {"include_state": True}, "verify stopped"),
                ],
            ),
            workspace=new_workspace("worker_backward_prompt", root=Path("/tmp")),
            skills=SkillRegistry(include_user=False).list(),
        )

        self.assertIn("duration_s<=5.0", prompt)
        self.assertIn("Do not block such a plan solely because there is no rear camera", prompt)
        self.assertIn("registered safety or clearance skill has explicitly returned unsafe", prompt)

    def test_control_skill_docs_require_timing_semantics(self) -> None:
        registry = SkillRegistry(include_user=False)
        control_names = {
            "init_arms",
            "move_arm_ee",
            "move_arm_joints",
            "set_base_velocity",
            "set_gripper",
            "set_lift_height",
            "stop_motion",
        }

        for name in control_names:
            skill = registry.get(name)
            self.assertIsNotNone(skill, name)
            body = skill.body.lower()
            self.assertTrue(
                any(term in body for term in ("duration_s", "settle_s", "velocity_limit_rad_s")),
                name,
            )
            self.assertTrue(any(term in body for term in ("time", "timing")), name)

    def test_worker_learned_skill_can_call_registered_skills_with_trace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_root = os.environ.get("LOOPMASTER_SKILL_ROOT")
            os.environ["LOOPMASTER_SKILL_ROOT"] = str(Path(tmp) / "skills")
            try:
                skill_dir = Path(os.environ["LOOPMASTER_SKILL_ROOT"]) / "control" / "combo"
                skill_dir.mkdir(parents=True)
                (skill_dir / "SKILL.md").write_text(
                    "---\nname: combo\ncategory: control\n---\n# Combo\n",
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
                        "category": "control",
                        "files": [
                            {
                                "path": "SKILL.md",
                                "content": (
                                    "---\n"
                                    "name: created_by_skill\n"
                                    "description: created by create_skill\n"
                                    "category: control\n"
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
                        "category": "control",
                        "files": [
                            {
                                "path": "SKILL.md",
                                "content": "---\nname: bad_created_by_skill\ncategory: control\n---\n# Bad\n",
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
        from loopmaster_agentic.skills.perception.detect_grasps.policy import (
            _merge_capture_image_memory as merge_anygrasp_capture,
        )
        from loopmaster_agentic.skills.perception.grounded_sam2.policy import (
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

    def test_grounded_sam2_resolves_relative_paths_against_workspace(self) -> None:
        from loopmaster_agentic.skills.perception.grounded_sam2.policy import _resolve_workspace_path

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "run"
            repo = Path(tmp) / "third_party" / "Grounded-SAM-2"

            resolved = _resolve_workspace_path("artifacts/rgb.png", workspace_root=workspace, repo_root=repo)

        self.assertEqual(resolved, workspace / "artifacts" / "rgb.png")

    def test_detect_grasps_does_not_mix_example_mask_with_custom_rgbd(self) -> None:
        import numpy as np
        from PIL import Image

        from loopmaster_agentic.skills.perception.detect_grasps.policy import _load_points

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

    def test_detect_grasps_defaults_to_d435_intrinsics_config(self) -> None:
        from loopmaster_agentic.skills.perception.detect_grasps.policy import _d435_camera_params

        params = _d435_camera_params()

        self.assertAlmostEqual(params["fx"], 607.03662109375)
        self.assertAlmostEqual(params["fy"], 606.8038940429688)
        self.assertAlmostEqual(params["cx"], 316.76116943359375)
        self.assertAlmostEqual(params["cy"], 242.75991821289062)

    def test_detect_grasps_rejects_empty_explicit_mask_region(self) -> None:
        from loopmaster_agentic.skills.perception.detect_grasps.policy import _should_reject_empty_explicit_region

        self.assertTrue(_should_reject_empty_explicit_region({"seg_mask_path": "/tmp/seg.png"}, 0))
        self.assertFalse(_should_reject_empty_explicit_region({"seg_mask_path": "/tmp/seg.png"}, 1))
        self.assertFalse(_should_reject_empty_explicit_region({}, 0))

    def test_auditor_prompt_does_not_treat_grounded_sam_as_clearance_verdict(self) -> None:
        from loopmaster_agentic.agents.auditor import _auditor_prompt

        prompt = _auditor_prompt(
            plan=Plan(task="move forward", goal="move forward"),
            trace=[],
            candidate_review={"verdict": "done"},
        )

        self.assertIn("grounded_sam2", prompt)
        self.assertIn("Do not convert generic object detections", prompt)
        self.assertIn("Treat ambiguous perception annotations as notes or risks", prompt)
        self.assertIn("duration/velocity and final stopped state", prompt)
        self.assertIn("For play_cache_traj", prompt)
        self.assertIn("cannot produce in-trajectory grasp/contact/retention feedback", prompt)

    def test_auditor_relaxes_play_cache_traj_feedback_requirements(self) -> None:
        class StrictReplayAuditor:
            def run_json(self, *, role: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
                self.payload = json.loads(prompt)
                return {
                    "verdict": "retry",
                    "root_cause": (
                        "play_cache_traj reported replaying frames, but the trace has no "
                        "in-trajectory feedback and no credible grasp success evidence. "
                        "The return-to-init verification is stale."
                    ),
                    "next_action": "collect more feedback",
                    "used_skills": ["play_cache_traj"],
                    "used_control_skills": ["play_cache_traj"],
                    "sim_leak": [],
                    "research_questions": [],
                    "success": False,
                    "notes": [],
                    "skill_updates": [],
                    "skill_proposals": [],
                }

        with tempfile.TemporaryDirectory() as tmp:
            workspace = new_workspace("cache_replay_audit", root=Path(tmp))
            trace = [
                TraceStep(
                    index=1,
                    skill="play_cache_traj",
                    args={"episode": 1},
                    result={
                        "ok": True,
                        "episode": 1,
                        "sent_frames": 550,
                        "return_to_init": {"stop_motion": {"ok": True}, "init_arms": {"ok": False}},
                    },
                    ok=True,
                )
            ]
            review = Auditor().review(
                plan=Plan(task="play cached trajectory", goal="replay point 1"),
                trace=trace,
                workspace=workspace,
                agent_client=StrictReplayAuditor(),
            )

        self.assertEqual(review["verdict"], "done")
        self.assertTrue(review["success"])
        self.assertEqual(review["root_cause"], "")
        self.assertIn("play_cache_traj", review["used_control_skills"])

    def test_navigation_docs_do_not_present_status_as_clearance_gate(self) -> None:
        skill = SkillRegistry(include_user=False).get("navigation")

        self.assertIsNotNone(skill)
        self.assertIn("does not prove", skill.body)
        self.assertIn("path-clearance or rear-obstacle", skill.body)

    def test_detect_grasps_wraps_license_check_with_network_context(self) -> None:
        import subprocess

        from loopmaster_agentic.skills.perception.detect_grasps import policy

        events = []

        def fake_sudo_ip_link(device: str, state: str, **_kwargs):
            events.append((device, state))
            return subprocess.CompletedProcess(["sudo"], 0, "", "")

        with (
            mock.patch.object(policy, "_network_device_exists", return_value=True),
            mock.patch.object(policy, "_active_connection_for_device", return_value=None),
            mock.patch.object(policy, "_sudo_ip_link", side_effect=fake_sudo_ip_link),
            mock.patch.object(policy, "_run", return_value=subprocess.CompletedProcess(["nmcli"], 0, "", "")),
            mock.patch.object(policy.time, "sleep", return_value=None),
        ):
            status = {}
            with policy._license_network_context({}, status):
                events.append(("license", "check"))

        self.assertEqual(events, [("enx00e04c360914", "down"), ("license", "check"), ("enx00e04c360914", "up")])
        self.assertEqual(status["down_devices"], ["enx00e04c360914"])
        self.assertFalse(status["sudo_password_provided"])

    def test_strategist_links_object_grasp_perception_with_dynamic_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = Strategist().plan(
                task="detect target grasp",
                user_request="detect grasp pose for object=red cup with mask",
                workspace=new_workspace("plan_refs", root=Path(tmp)),
                skills=SkillRegistry(include_user=False),
            )

            steps = [step.name for step in plan.steps]
            self.assertEqual(steps[:5], ["observe", "init_arms", "capture_image", "grounded_sam2", "detect_grasps"])
            self.assertEqual(plan.steps[2].args["source"], "d435_rgbd")
            self.assertEqual(plan.steps[3].args["img_path"], {"$ref": "capture_image.rgb.path"})
            self.assertEqual(plan.steps[4].args["color_path"], {"$ref": "capture_image.rgb.path"})
            self.assertEqual(plan.steps[4].args["depth_path"], {"$ref": "capture_image.depth.path"})
            self.assertEqual(plan.steps[4].args["seg_mask_path"], {"$ref": "grounded_sam2.seg_mask_path"})

    def test_handler_connects_all_four_roles_to_subagent_client(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = _FakeSubagentClient()
            result = Handler(workspace_root=Path(tmp), agent_client=fake).run(
                task="inspect robot",
                user_request="inspect robot",
                platform=DryRunPlatform(),
            )

            self.assertTrue(result.success)
            self.assertEqual(fake.roles, ["handler", "strategist", "worker", "auditor", "handler_summary"])
            workspace = Path(result.workspace)
            self.assertTrue((workspace / "handler_agent.json").is_file())
            self.assertTrue((workspace / "strategist_agent.json").is_file())
            self.assertTrue((workspace / "worker_agent.json").is_file())
            self.assertTrue((workspace / "auditor_agent.json").is_file())
            self.assertTrue((workspace / "handler_summary_agent.json").is_file())
            self.assertEqual(result.review["response"], "fake handler summary for inspect robot")

    def test_handler_reruns_after_documentation_only_skill_update(self) -> None:
        class UpdatingAuditor(Auditor):
            def __init__(self) -> None:
                self.calls = 0

            def review(self, **kwargs):
                self.calls += 1
                if self.calls > 1:
                    return {
                        "verdict": "done",
                        "root_cause": "",
                        "next_action": "",
                        "used_skills": ["observe"],
                        "used_control_skills": [],
                        "sim_leak": [],
                        "research_questions": [],
                        "success": True,
                    }
                return {
                    "verdict": "retry",
                    "root_cause": "documentation-only update",
                    "next_action": "reload skill docs and replan",
                    "used_skills": ["observe"],
                    "used_control_skills": [],
                    "sim_leak": [],
                    "research_questions": [],
                    "success": False,
                    "skill_updates": [
                        {
                            "skill_name": "observe",
                            "rationale": "doc only",
                            "files": [
                                {
                                    "path": "SKILL.md",
                                    "content": "---\nname: observe\ncategory: perception\n---\n# Observe\nDoc only.\n",
                                }
                            ],
                        }
                    ],
                }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "skills"
            observe_dir = root / "perception" / "observe"
            observe_dir.mkdir(parents=True)
            (observe_dir / "SKILL.md").write_text(
                "---\nname: observe\ncategory: perception\n---\n# Observe\n",
                encoding="utf-8",
            )
            (observe_dir / "policy.py").write_text(
                "def dispatch(context, args):\n"
                "    return {'ok': True, 'observation': {'state_keys': []}}\n",
                encoding="utf-8",
            )
            auditor = UpdatingAuditor()
            result = Handler(
                workspace_root=Path(tmp) / "workspaces",
                auditor=auditor,
                skills=SkillRegistry(roots=[root], include_user=False),
            ).run(
                task="inspect robot",
                user_request="inspect robot",
                platform=DryRunPlatform(),
            )

        self.assertTrue(result.success)
        self.assertEqual(auditor.calls, 2)
        self.assertIn("skill update observe", "\n".join(result.notes))

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
                "category: control\n"
                "args:\n"
                "  side: string\n"
                "  cycles: integer\n"
                "---\n"
                "# Arg Skill\n"
                "\n"
                "Use this markdown body as planner-facing skill usage guidance.\n",
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
        self.assertIn("planner-facing skill usage guidance", arg_skill["usage_markdown"])

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
            self.assertIn("set_gripper", resumed.reply("set right gripper position=-0.25"))
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

    def test_chat_cli_tui_starts_handoff_server_without_web_poll(self) -> None:
        class _FakeChatSession:
            def __init__(self, **kwargs):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            stop = mock.Mock()
            with mock.patch.object(cli_module, "HandlerChatSession", _FakeChatSession), mock.patch.object(
                cli_module, "_run_handler_chat_tui", return_value=None
            ), mock.patch.object(cli_module, "_start_chat_handoff_server", return_value=stop) as handoff, mock.patch.object(
                cli_module, "_start_chat_web_bridge"
            ) as poll:
                code = main(
                    [
                        "chat",
                        "--dry-run",
                        "--local-agents",
                        "--state-dir",
                        str(Path(tmp) / "state"),
                        "--workspace-root",
                        str(Path(tmp) / "workspaces"),
                    ]
                )

        self.assertEqual(code, 0)
        self.assertTrue(handoff.called)
        self.assertFalse(poll.called)
        self.assertTrue(stop.called)

    def test_chat_cli_web_poll_is_explicit(self) -> None:
        class _FakeChatSession:
            def __init__(self, **kwargs):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            stop_event = threading.Event()
            stop_handoff = mock.Mock()
            with mock.patch.object(cli_module, "HandlerChatSession", _FakeChatSession), mock.patch.object(
                cli_module, "_run_handler_chat_tui", return_value=None
            ), mock.patch.object(cli_module, "_start_chat_handoff_server", return_value=stop_handoff), mock.patch.object(
                cli_module, "_start_chat_web_bridge", return_value=stop_event
            ) as start:
                code = main(
                    [
                        "chat",
                        "--dry-run",
                        "--local-agents",
                        "--web-poll",
                        "--state-dir",
                        str(Path(tmp) / "state"),
                        "--workspace-root",
                        str(Path(tmp) / "workspaces"),
                    ]
                )

        self.assertEqual(code, 0)
        self.assertTrue(start.called)
        self.assertTrue(stop_event.is_set())
        self.assertTrue(stop_handoff.called)

    def test_chat_cli_once_does_not_start_background_services(self) -> None:
        class _FakeChatSession:
            def __init__(self, **kwargs):
                pass

            def reply(self, text, *, progress=None):
                return "ok"

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(cli_module, "HandlerChatSession", _FakeChatSession), mock.patch.object(
                cli_module, "_start_chat_web_bridge"
            ) as poll, mock.patch.object(cli_module, "_start_chat_handoff_server") as handoff:
                code = main(
                    [
                        "chat",
                        "--dry-run",
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
        self.assertFalse(poll.called)
        self.assertFalse(handoff.called)

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

    def test_handler_replans_after_auditor_retry_without_skill_update(self) -> None:
        class AuditorRetryClient(_FakeSubagentClient):
            def __init__(self) -> None:
                super().__init__()
                self.auditor_calls = 0
                self.retry_prompts = 0

            def run_json(self, *, role: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
                if role == "strategist":
                    self.roles.append(role)
                    payload = json.loads(prompt)
                    if "failed_trace" in payload:
                        self.retry_prompts += 1
                    codex = {"profile": self.profile, "session_id": f"fake-{role}", "role": role}
                    return {
                        "goal": payload["user_request"],
                        "steps": [
                            {
                                "name": "set_gripper",
                                "args_json": json.dumps({"side": "right", "position": -5.0}),
                                "why": "open gripper",
                            },
                            {
                                "name": "stop_motion",
                                "args_json": json.dumps({"reason": "test end"}),
                                "why": "safety stop",
                            },
                        ],
                        "success_criteria": ["auditor retry path is exercised"],
                        "risks": [],
                        "assumptions": [],
                        "research_questions": [],
                        "subagent_notes": ["retry plan" if "failed_trace" in payload else "initial plan"],
                        "_codex": codex,
                    }
                if role == "auditor":
                    self.roles.append(role)
                    self.auditor_calls += 1
                    payload = json.loads(prompt)
                    review = payload["candidate_review"]
                    codex = {"profile": self.profile, "session_id": f"fake-{role}", "role": role}
                    if self.auditor_calls == 1:
                        return {
                            **review,
                            "verdict": "retry",
                            "root_cause": "need a second closed-loop verification pass",
                            "next_action": "Replan with additional verification evidence.",
                            "success": False,
                            "notes": ["fake retry"],
                            "skill_updates": [],
                            "skill_proposals": [],
                            "_codex": codex,
                        }
                    return {
                        **review,
                        "verdict": "done",
                        "root_cause": "",
                        "next_action": "",
                        "success": True,
                        "notes": ["fake done"],
                        "skill_updates": [],
                        "skill_proposals": [],
                        "_codex": codex,
                    }
                return super().run_json(role=role, prompt=prompt, schema=schema)

        with tempfile.TemporaryDirectory() as tmp:
            events = []
            fake = AuditorRetryClient()
            result = Handler(workspace_root=Path(tmp) / "workspaces", agent_client=fake).run(
                task="open gripper",
                user_request="open gripper",
                platform=DryRunPlatform(),
                progress=events.append,
            )

        self.assertTrue(result.success)
        self.assertEqual(fake.auditor_calls, 2)
        self.assertEqual(fake.retry_prompts, 1)
        self.assertIn("returning auditor retry review to strategist", events)

    def test_handler_escalates_repeated_auditor_retry_to_fresh_codex_role(self) -> None:
        class RepeatedAuditorRetryClient(_FakeSubagentClient):
            def __init__(self) -> None:
                super().__init__()
                self.escalation_payload = None

            def run_json(self, *, role: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
                if role == "strategist":
                    self.roles.append(role)
                    payload = json.loads(prompt)
                    codex = {"profile": self.profile, "session_id": f"fake-{role}", "role": role}
                    return {
                        "goal": payload["user_request"],
                        "steps": [
                            {
                                "name": "set_gripper",
                                "args_json": json.dumps({"side": "right", "position": -5.0}),
                                "why": "open gripper",
                            },
                            {
                                "name": "stop_motion",
                                "args_json": json.dumps({"reason": "test end"}),
                                "why": "safety stop",
                            },
                        ],
                        "success_criteria": ["retry escalation path is exercised"],
                        "risks": [],
                        "assumptions": [],
                        "research_questions": [],
                        "subagent_notes": ["fake plan"],
                        "_codex": codex,
                    }
                if role == "auditor":
                    self.roles.append(role)
                    payload = json.loads(prompt)
                    review = payload["candidate_review"]
                    codex = {"profile": self.profile, "session_id": f"fake-{role}", "role": role}
                    return {
                        **review,
                        "verdict": "retry",
                        "root_cause": "same closed-loop evidence gap",
                        "next_action": "Escalate after repeated retry.",
                        "success": False,
                        "notes": ["same retry"],
                        "skill_updates": [],
                        "skill_proposals": [],
                        "_codex": codex,
                    }
                if role.startswith("auditor_escalation_"):
                    self.roles.append(role)
                    self.escalation_payload = json.loads(prompt)
                    codex = {"profile": self.profile, "session_id": f"fake-{role}", "role": role}
                    return {
                        "decision": "return_to_user",
                        "root_cause": "escalation decided this needs operator review",
                        "next_action": "Inspect hardware state before retrying.",
                        "user_summary": "operator review needed",
                        "notes": ["fresh escalation session reviewed full trace"],
                        "skill_updates": [],
                        "skill_proposals": [],
                        "_codex": codex,
                    }
                return super().run_json(role=role, prompt=prompt, schema=schema)

        with tempfile.TemporaryDirectory() as tmp:
            events = []
            fake = RepeatedAuditorRetryClient()
            result = Handler(workspace_root=Path(tmp) / "workspaces", agent_client=fake).run(
                task="open gripper",
                user_request="open gripper",
                platform=DryRunPlatform(),
                progress=events.append,
            )

        self.assertFalse(result.success)
        self.assertIn("auditor_escalation_2", fake.roles)
        self.assertIsNotNone(fake.escalation_payload)
        self.assertEqual(fake.escalation_payload["auditor_review"]["root_cause"], "same closed-loop evidence gap")
        self.assertEqual(result.review["root_cause"], "escalation decided this needs operator review")
        self.assertEqual(result.review["escalation_decision"], "return_to_user")

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

    def test_handler_reply_summarizes_failed_skill_naturally(self) -> None:
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

        self.assertIn("这轮没有完成，卡在 `move_arm_joints`", reply)
        self.assertIn("机械臂侧别不对", reply)
        self.assertNotIn('args={"arm": "left"', reply)
        self.assertNotIn("失败点", reply)
        self.assertNotIn("`stop_motion` args=", reply)
        self.assertNotIn("role=worker.safety", reply)

    def test_handler_reply_summarizes_navigation_status_timeout_naturally(self) -> None:
        result = RunResult(
            task="你能看到现在自己在map中的位置吗",
            workspace="/tmp/workspace",
            plan=Plan(task="map pose", goal="query map pose"),
            trace=[
                TraceStep(
                    index=1,
                    skill="observe",
                    args={"include_state": True},
                    result={"ok": True, "observation": {"state_keys": ["x.vel"]}},
                    ok=True,
                ),
                TraceStep(
                    index=2,
                    skill="navigation",
                    args={
                        "command": "status",
                        "robot_ip": "192.168.31.22",
                        "status_port": 7210,
                        "status_timeout_s": 5.0,
                    },
                    result={"ok": False, "error": "no navigation status received"},
                    ok=False,
                    why="query map pose",
                ),
            ],
            review={
                "verdict": "retry",
                "root_cause": "The robot interface responded to observe, but observe only exposed proprioceptive state.",
                "next_action": "Return a concise failure summary to the user.",
                "research_questions": [],
                "used_skills": ["observe", "navigation"],
                "success": False,
            },
            success=False,
        )

        reply = format_handler_reply(result)

        self.assertIn("这轮没有完成，卡在 `navigation`", reply)
        self.assertIn("没有收到导航状态", reply)
        self.assertIn("请先确认机器人端导航栈", reply)
        self.assertNotIn("失败点", reply)
        self.assertNotIn("The robot interface responded", reply)
        self.assertNotIn("tcp://192.168.31.22:7210", reply)

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
            "x.vel": -0.2,
            "y.vel": -0.05,
            "status": "ok",
        }
        obs = split_hei_observation(raw)
        self.assertIn("front", obs.images)
        self.assertEqual(obs.state["left_joint_1.pos"], 0.25)
        self.assertEqual(obs.state["height.pos"], -10.0)
        self.assertEqual(obs.state["x.vel"], 0.2)
        self.assertEqual(obs.state["y.vel"], 0.05)
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
                "---\nname: toy\ndescription: toy\ncategory: perception\n---\n\n# Toy\n",
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
                registry = SkillRegistry(roots=[Path(tmp) / "skills"], include_user=True)
                workspace = _FakeWorkspace(Path(tmp))
                review = {
                    "skill_proposals": [
                        {
                            "kind": "new_skill",
                            "skill_name": "toy_new",
                            "category": "control",
                            "rationale": "test new skill",
                            "files": [
                                {
                                    "path": "SKILL.md",
                                    "content": (
                                        "---\n"
                                        "name: toy_new\n"
                                        "description: toy new\n"
                                        "category: control\n"
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
                registry = SkillRegistry(roots=[Path(tmp) / "skills"], include_user=True)
                workspace = _FakeWorkspace(Path(tmp))
                review = {
                    "skill_proposals": [
                        {
                            "kind": "new_skill",
                            "skill_name": "bad_new",
                            "category": "control",
                            "files": [
                                {
                                    "path": "SKILL.md",
                                    "content": "---\nname: bad_new\ncategory: control\n---\n# Bad\n",
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
            platform.command_chassis(x=0.1, y=0.05, theta=0.2),
            {"x.vel": 0.1, "y.vel": 0.05, "theta.vel": 0.2},
        )
        self.assertEqual(robot.actions[-1], {"x.vel": -0.1, "y.vel": -0.05, "theta.vel": 0.2})

        self.assertEqual(platform.set_gripper("right", -0.5), {"right_gripper.pos": -0.5})
        self.assertEqual(robot.actions[-1], {"right_gripper.pos": -0.5})

        self.assertIs(platform.get_head_image(), robot.observation["front"])
        self.assertEqual(set(platform.get_wrist_images()), {"left_wrist", "right_wrist"})

    def test_hei_platform_filters_full_client_action_vector_to_requested_keys(self) -> None:
        class FullVectorRobot(_FakeHeiRobot):
            def send_action(self, action):
                self.actions.append(dict(action))
                sent = {key: 0.0 for key in self.observation}
                sent.update(action)
                sent["right_joint_1.pos"] = 0.0
                sent["left_joint_1.pos"] = 0.0
                return sent

        robot = FullVectorRobot()
        platform = HeiRebotLiftPlatform()
        platform._robot = robot

        self.assertEqual(platform.set_gripper("right", -5.0), {"right_gripper.pos": -5.0})
        self.assertEqual(robot.actions[-1], {"right_gripper.pos": -5.0})

    def test_hei_platform_filters_full_chassis_action_vector_to_base_keys(self) -> None:
        class FullVectorRobot(_FakeHeiRobot):
            def command_chassis(self, *, x=0.0, y=0.0, theta=0.0):
                return {
                    "x.vel": x,
                    "y.vel": y,
                    "theta.vel": theta,
                    "right_joint_1.pos": 0.0,
                    "left_joint_1.pos": 0.0,
                    "height.pos": 0.0,
                }

        robot = FullVectorRobot()
        platform = HeiRebotLiftPlatform()
        platform._robot = robot

        self.assertEqual(platform.command_chassis(x=0.1), {"x.vel": 0.1, "y.vel": 0.0, "theta.vel": 0.0})

    def test_hei_platform_passes_arm_velocity_limit_to_arm_interface(self) -> None:
        class Arms:
            def __init__(self) -> None:
                self.calls = []

            def command_side(self, side, positions, *, velocity_limit_rad_s=None):
                self.calls.append((side, dict(positions), velocity_limit_rad_s))
                return {f"{side}_{joint}.pos": value for joint, value in positions.items()}

        robot = SimpleNamespace(arms=Arms())
        platform = HeiRebotLiftPlatform()
        platform._robot = robot

        sent = platform.command_arm("right", {"joint_1": 0.2}, velocity_limit_rad_s=0.4)

        self.assertEqual(sent["right_joint_1.pos"], 0.2)
        self.assertEqual(robot.arms.calls[-1][2], 0.4)

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
        if role == "handler_summary":
            return {
                "response": f"fake handler summary for {payload['user_request']}",
                "notes": ["fake handler summary"],
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
        good_args = {"side": "left", "positions": {"joint_5": 0.5}, "velocity_limit_rad_s": 0.4}
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
