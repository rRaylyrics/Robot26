#!/usr/bin/env python3
"""Task-level Agent for EX001 + official WALL-X client.

Offline demo: python agent.py --plan example_plan.json
Real: python agent.py --real --safety-ack --plan example_plan.json
"""
import argparse
import json
import select
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from bridge import EX001Bridge
from planner import Step, heuristic_plan, llm_plan, parse_plan


def event(log_file: Path, kind: str, **data):
    log_file.parent.mkdir(exist_ok=True, parents=True)
    obj = {"time": datetime.now(timezone.utc).isoformat(), "event": kind, **data}
    with log_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    print(f"[{kind}] " + " | ".join(f"{k}={v}" for k, v in data.items()), flush=True)


def wait_manual(step: Step, bridge: EX001Bridge, started: float):
    print("执行中：观察机器人。任务完成输入 y + Enter；失败输入 n；退出输入 q。", flush=True)
    while time.monotonic() - started < step.timeout_s:
        if not bridge.is_running():
            raise RuntimeError("Official EX001 client exited unexpectedly; check client log")
        readable, _, _ = select.select([sys.stdin], [], [], 0.25)
        if readable:
            answer = sys.stdin.readline().strip().lower()
            if answer in ("y", "yes"):
                return "success"
            if answer in ("n", "no"):
                return "retry"
            if answer in ("q", "quit"):
                return "abort"
            print("请输入 y / n / q，再按 Enter", flush=True)
    return "timeout"


def wait_vision(step: Step, bridge: EX001Bridge, checker, started: float):
    consecutive = 0
    while time.monotonic() - started < step.timeout_s:
        if not bridge.is_running():
            raise RuntimeError("Official EX001 client exited unexpectedly; check client log")
        ok, reason = checker.check(step.success_condition)
        print(f"[vision] confirmed={ok}  reason={reason}", flush=True)
        consecutive = consecutive + 1 if ok else 0
        if consecutive >= 2:
            return "success"
        time.sleep(3)
    return "timeout"


def run(args):
    if args.plan:
        with open(args.plan, encoding="utf-8") as f:
            steps = parse_plan(json.load(f))
    elif args.llm_plan:
        steps = llm_plan(args.task)
    else:
        steps = heuristic_plan(args.task)

    print("\n=== Agent task plan (review before any robot motion) ===")
    for i, step in enumerate(steps, start=1):
        print(f"{i}. {step.instruction}\n   verify: {step.success_condition} | limit: {step.timeout_s}s")
    print()
    log_dir = Path(args.log_dir)
    journal = log_dir / "events.jsonl"
    event(journal, "plan", steps=[vars(s) for s in steps], real=args.real)

    if not args.real:
        for i, step in enumerate(steps, 1):
            event(journal, "simulated_step", index=i, instruction=step.instruction,
                  outcome="simulated_only_no_robot_or_wallx_used")
        print("\nDry-run finished. No actual WALL-X request or robot motion was performed.")
        return 0

    if not args.safety_ack:
        raise RuntimeError("Real mode requires --safety-ack and an operator at the E-stop")
    if not sys.stdin.isatty() and args.checker == "manual":
        raise RuntimeError("Manual real mode requires an interactive terminal")
    bridge = EX001Bridge(args.embodiment, sync=(args.infer == "sync"), logs_dir=log_dir)
    checker = None
    if args.checker == "vision":
        from checker import CameraVisionChecker
        checker = CameraVisionChecker(args.sdk_server)
    print("SAFETY: Clear workspace; verify target pose, gripper, robot mode, E-stop. "
          "This program is not a certified safety controller.")
    if input("Type EXACTLY ARM to enable the run: ").strip() != "ARM":
        raise RuntimeError("Operator did not approve execution")
    completed = 0
    for i, step in enumerate(steps, start=1):
        for attempt in range(1, args.max_retries + 2):
            if attempt > 1:
                print("An attempt failed. Inspect the scene and clear any grasped objects.")
                if input("Type RETRY to repeat this step: ").strip() != "RETRY":
                    event(journal, "aborted", step=i, reason="retry_not_approved")
                    return 2
            label = f"step_{i:02d}_try_{attempt:02d}"
            event(journal, "started", step=i, attempt=attempt, instruction=step.instruction)
            bridge.start(step.instruction, label)
            started = time.monotonic()
            try:
                if checker is None:
                    outcome = wait_manual(step, bridge, started)
                else:
                    outcome = wait_vision(step, bridge, checker, started)
            finally:
                # Stop the client before any next instruction or planner decision.
                bridge.stop()
            event(journal, "finished", step=i, attempt=attempt, outcome=outcome,
                  elapsed_s=round(time.monotonic() - started, 2))
            if outcome == "success":
                completed += 1
                break
            if outcome == "abort":
                return 2
            if attempt == args.max_retries + 1:
                event(journal, "failed", step=i, reason=outcome)
                print("Step failed after allowed retries; subsequent steps will NOT run")
                return 2
    event(journal, "all_completed", count=completed)
    print("All steps verified. Inspect physical scene before leaving.")
    return 0


def main():
    p = argparse.ArgumentParser(description="EX001 + WALL-X task-level Agent")
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--plan", help="JSON file containing steps")
    source.add_argument("--task", help="High-level task or several tasks separated by 然后 / ;")
    p.add_argument("--llm-plan", action="store_true", help="Use optional planner LLM with --task")
    p.add_argument("--real", action="store_true", help="Enable real robot: OFF by default")
    p.add_argument("--safety-ack", action="store_true", help="Operator accepts safety preconditions")
    p.add_argument("--embodiment", default="quanta_x1_pro/gripper-g",
                   choices=["quanta_x1_pro/gripper-g", "quanta_x1_pro/gripper-h"])
    p.add_argument("--infer", choices=["sync", "rtc"], default="sync")
    p.add_argument("--checker", choices=["manual", "vision"], default="manual")
    p.add_argument("--sdk-server", default="192.168.10.1:50051")
    p.add_argument("--max-retries", type=int, choices=[0, 1, 2], default=1)
    p.add_argument("--log-dir", default="logs")
    args = p.parse_args()
    if args.llm_plan and not args.task:
        p.error("--llm-plan requires --task")
    try:
        return run(args)
    except KeyboardInterrupt:
        print("\nInterrupted. If robot is moving, use the hardware E-stop.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
