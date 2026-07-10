from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from typing import Any

from loopmaster_agentic import cli as cli_module
from loopmaster_agentic.agents import Auditor, Handler, Strategist, Worker
from loopmaster_agentic.agents.handler_chat import HandlerChatSession, format_handler_reply
from loopmaster_agentic.agents.skill_updater import apply_review_skill_updates
from loopmaster_agentic.cli import main
from loopmaster_agentic.core.result import RunResult
from loopmaster_agentic.core.types import Plan, TraceStep
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

    def test_base_skill_surface_excludes_task_specific_skills(self) -> None:
        skills = SkillRegistry(include_user=False).list()
        names = {skill.name for skill in skills}
        self.assertEqual(
            names,
            {
                "capture_image",
                "detect_grasps",
                "grounded_sam2",
                "move_arm_joints",
                "observe",
                "send_action",
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

    def test_keyboard_l_and_r_select_arm_side(self) -> None:
        state = TeleopState()
        args = argparse.Namespace(linear_speed=0.05, angular_speed=0.15)

        _handle_key("l", args, None, None, state)
        self.assertEqual(state.side, "left")

        _handle_key("r", args, None, None, state)
        self.assertEqual(state.side, "right")

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
        self.assertIn("本轮执行 trace", reply)
        self.assertIn("`observe` args=", reply)

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
        self.assertIn("`stop_motion` args=", reply)
        self.assertIn("role=worker.safety", reply)

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


if __name__ == "__main__":
    unittest.main()
