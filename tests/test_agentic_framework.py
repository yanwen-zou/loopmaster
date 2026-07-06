from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from loopmaster_agentic.agents import Auditor, Handler, Strategist, Worker
from loopmaster_agentic.platform.dry_run import DryRunPlatform
from loopmaster_agentic.platform.hei_rebot_lift import split_hei_observation
from loopmaster_agentic.skills.registry import SkillRegistry


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


class _FakeImage:
    def __init__(self, shape):
        self.shape = shape
        self.dtype = "uint8"


if __name__ == "__main__":
    unittest.main()
