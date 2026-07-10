from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from loopmaster_agentic.agents import Auditor, Handler, Strategist, Worker
from loopmaster_agentic.agents.handler_chat import HandlerChatSession
from loopmaster_agentic.cli import main
from loopmaster_agentic.platform.dry_run import DryRunPlatform
from loopmaster_agentic.platform.hei_rebot_lift import HeiRebotLiftPlatform, split_hei_observation
from loopmaster_agentic.skills.registry import SkillContext, SkillRegistry


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

            self.assertIn("Handler completed this turn.", reply)
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
            return {
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
                "_codex": codex,
            }
        raise AssertionError(f"unexpected role {role}")


if __name__ == "__main__":
    unittest.main()
