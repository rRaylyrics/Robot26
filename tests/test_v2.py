"""Offline integration of V2 scheduling, observations, retries and persisted state."""
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
import unittest

from action.agent import DATA, RunStore, load_config, read_json
from action.FSM.task_FSM import run_task


class RobotFixture:
    def __init__(self):
        self.active = False
        self.sent = []
        self.frame = 0

    def capture(self, directory, *, timeout):
        self.frame += 1
        pose = dict(position_m=[0, 0, 0], orientation_xyzw=[0, 0, 0, 1],
                    gripper_rad=0, pose_timestamp=self.frame, gripper_timestamp=self.frame,
                    frame_id="base")
        return dict(id=str(self.frame), arms={"left": pose, "right": pose},
                    captured_at=self.frame, images={})

    def start(self, instruction, label, *, timeout):
        if self.active:
            raise AssertionError("overlapping Client sessions")
        self.active = True
        self.sent.append(instruction)

    def stop(self):
        self.active = False

    def is_running(self):
        return self.active


class TestV2(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.robot = RobotFixture()
        config = load_config(DATA)
        config["prompts"]["custom"] = "Perform the indicated operation"
        self.store = RunStore(Path(temp.name), config, "整理红衣服", 1)

    def judge(self, context, snapshots, *, timeout):
        phase = context["phase"]
        latest = snapshots[-1]["id"]
        done = phase == "final"
        failed = phase == "precheck" or (phase == "verify" and len(self.robot.sent) == 1)
        if phase == "verify":
            self.assertFalse(self.robot.active)
        features = dict(in_scope=True, folding="complete" if done else "failed",
                        classification="not_required", folding_evidence=[latest])
        garment = dict(id="red", type="背心", visible=True, features=features,
                       processing_status="已操作完" if done else "未操作")
        return dict(status="failed" if failed else "complete", reason="下摆角仍在原位" if failed else "目标可见",
                    evidence=[latest], garments=[garment], preconditions_met=True)

    def test_failure_revises_only_current_action(self):
        pending = []
        def plan(ctx, *, timeout):
            current = ctx["current"]
            pending.append(current)
            direct = len(pending) == 2
            return dict(kind="direct" if direct else "custom", action_code="", garment_ids=["red"],
                        instruction=current if direct else "展开下摆", success_condition="布边可见",
                        remainder="" if direct else "把红衣服放进框")

        def optimize(ctx, *, timeout):
            if ctx["previous_instruction"]:
                self.assertIn("下摆角仍在原位", str(ctx["feedback"]))
                self.assertEqual(self.store.state["remaining"], "整理红衣服")
                return "夹住露出的下摆角，向外展开"
            return "展开红色下摆"

        result = run_task(self.store, self.robot, self.robot, plan, self.judge, optimize,
                          deadline=monotonic() + 60)
        self.assertEqual(pending, ["整理红衣服", "把红衣服放进框"])
        self.assertEqual(self.robot.sent, ["展开红色下摆", "夹住露出的下摆角，向外展开", pending[-1]])
        self.assertEqual(result["phase"], "complete")
        self.assertEqual(read_json(self.store.directory / "checkpoint.json")["remaining"], "")
        self.assertEqual(len((self.store.directory / "task/prompt_revisions.jsonl").read_text(
            encoding="utf-8").splitlines()), 3)

    def test_uncertain_precheck_prevents_motion(self):
        def uncertain(ctx, snapshots, *, timeout):
            result = self.judge(ctx, snapshots, timeout=timeout)
            if ctx["phase"] == "precheck":
                result.update(status="uncertain", reason="目标被遮挡")
            return result
        proposal = dict(kind="direct", action_code="", garment_ids=["red"],
                        instruction="整理红衣服", success_condition="可观察终态", remainder="")
        with self.assertRaises(RuntimeError):
            run_task(self.store, self.robot, self.robot, lambda *a, **kw: proposal,
                     uncertain, None, deadline=monotonic() + 30)
        self.assertEqual(self.robot.sent, [])
        self.assertEqual(self.store.state["remaining"], "整理红衣服")

    def test_registered_action_preserves_catalog_criterion(self):
        self.store.config["actions"] = [
            {"code": "D3", "template": "将{衣物}两角对齐", "physical_criterion": "双角对折"}
        ]
        def propose(ctx, *, timeout):
            return dict(kind="atomic", action_code="D3", garment_ids=["red"],
                        instruction="对折背心", success_condition="角标对齐", remainder="")
        def optimize(ctx, *, timeout):
            self.assertEqual(ctx["action"]["template"], "将{衣物}两角对齐")
            self.assertEqual(ctx["action"]["physical_criterion"], "双角对折")
            return "把红背心两角对齐" if not ctx["previous_instruction"] else "对齐右下摆角"
        result = run_task(self.store, self.robot, self.robot, propose, self.judge, optimize,
                          deadline=monotonic() + 60)
        self.assertEqual(result["phase"], "complete")
        self.assertEqual(self.robot.sent, ["把红背心两角对齐", "对齐右下摆角"])
