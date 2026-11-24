import json
import unittest
from planner import heuristic_plan, parse_plan


class TestPlanner(unittest.TestCase):
    def test_two_steps(self):
        tasks = heuristic_plan("把红管放入左盒；把蓝管放入右盒")
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[1].success_condition, "把蓝管放入右盒")

    def test_plan_json(self):
        tasks = parse_plan({"steps": [{"instruction": "Put the cube in the box",
                                       "success_condition": "Cube inside box", "timeout_s": 20}]})
        self.assertEqual(tasks[0].timeout_s, 20)

    def test_reject_empty(self):
        with self.assertRaises(ValueError):
            parse_plan({"steps": []})

    def test_reject_many(self):
        with self.assertRaises(ValueError):
            parse_plan({"steps": ["task"] * 13})


if __name__ == "__main__":
    unittest.main()

class TestProbe(unittest.TestCase):
    def test_state_validation(self):
        from wallx_probe import validate_state
        state = {a: {"position": [0.1, 0.2, 0.3], "rotation_rpy": [0, 0, 0],
                     "gripper": 0.2} for a in ("left_ee", "right_ee")}
        validate_state(state, 1.526)
        state["left_ee"]["gripper"] = 10.0
        with self.assertRaises(ValueError):
            validate_state(state, 1.526)
