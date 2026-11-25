"""EX001 V2 entry point and filesystem data layer. Run with python -m action.agent."""
import argparse
import json
import math
import os
from pathlib import Path
import signal
import sys
import time
from datetime import datetime, timezone
from uuid import uuid4

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from action.FSM.task_FSM import run_task
from action.SDK.x2sdk import SDK
from action.client.client import Client
from action.llm.judgeLLM import judge
from action.llm.optimizeLLM import optimize
from action.llm.taskLLM import plan


DATA = Path(__file__).resolve().parents[1] / "data"
LIMITS = {
    "max_steps": 64, "max_attempts": 2, "max_observations": 3,
    "action_timeout_s": 45, "settle_timeout_s": 5,
}
GARMENT_FIELDS = ("expected_count", "visible_count", "confirmed_count", "garments")


def load_local_env(path):
    if not path.is_file():
        return
    names = {"QWEN_API_KEY", "QWEN_BASE_URL", "QWEN_TEXT_MODEL",
             "QWEN_VISION_MODEL", "WALLX_MODEL_ADDRESS", "WALLX_API_KEY"}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if separator and key in names:
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            if value:
                os.environ.setdefault(key, value)


def read_json(path):
    with path.open(encoding="utf-8-sig") as source:
        obj = json.load(source)
    if not isinstance(obj, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return obj


def write_json(path, value):
    payload = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(payload + "\n", encoding="utf-8")
    temp.replace(path)


def append_event(path, value):
    payload = json.dumps(value, ensure_ascii=False, allow_nan=False)
    with path.open("a", encoding="utf-8") as target:
        target.write(payload + "\n")


def load_config(root):
    prompts = read_json(root / "config/prompts.json")
    for field in ("task", "judge", "optimize", "custom"):
        if not isinstance(prompts.get(field), str):
            raise ValueError(f"config/prompts.json: {field} must be a string")
    constraints = read_json(root / "config/constraints.json")
    for field, default in LIMITS.items():
        value = constraints.get(field)
        if value is None:
            value = default
        if type(value) is not int or value < 1:
            raise ValueError(f"{field} must be a positive integer or null")
        constraints[field] = value
    if constraints.get("allow_custom") is None:
        constraints["allow_custom"] = True
    if type(constraints["allow_custom"]) is not bool:
        raise ValueError("allow_custom must be boolean or null")
    rules = constraints.get("rules")
    if not isinstance(rules, list) or any(not isinstance(rule, str) or not rule.strip() for rule in rules):
        raise ValueError("rules must be a list of nonempty strings")
    actions = read_json(root / "config/actions.json").get("actions")
    if not isinstance(actions, list):
        raise ValueError("actions must be a list")
    codes = set()
    for entry in actions:
        if not isinstance(entry, dict) or any(
            not isinstance(entry.get(key), str) or not entry[key].strip()
            for key in ("code", "template", "physical_criterion")
        ):
            raise ValueError("Every action needs code, template and physical_criterion")
        if entry["code"] in codes:
            raise ValueError(f"Duplicate action code: {entry['code']}")
        codes.add(entry["code"])
    allowed = constraints.get("allowed_action_codes")
    if allowed is not None and (not isinstance(allowed, list) or
                                any(not isinstance(code, str) or code not in codes for code in allowed)):
        raise ValueError("allowed_action_codes must contain registered action codes")
    return {"prompts": prompts, "constraints": constraints, "actions": actions}


class RunStore:
    """A unique run owns its observations and checkpoints. Templates are read-only."""

    def __init__(self, root, config, task, expected_count):
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex
        self.directory = root / "runs" / run_id
        self.images_dir = self.directory / "scene/images"
        self.directory.mkdir(parents=True, exist_ok=False)
        for name in ("task", "logs", "scene/images"):
            (self.directory / name).mkdir(parents=True)
        self.config = config
        self.state = {
            "original": task, "remaining": task, "candidate_remainder": "", "attempt": {},
            "phase": "created", "garments": [], "expected_count": expected_count,
            "confirmed_count": 0, "visible_count": None,
        }
        self.sequence = 0
        self.last_prompt = None
        (self.directory / "task/prompt_revisions.jsonl").touch()
        write_json(self.directory / "config.json", {
            **config,
            "qwen_text_model": os.getenv("QWEN_TEXT_MODEL", "qwen3.8-flash"),
            "qwen_vision_model": os.getenv("QWEN_VISION_MODEL", "qwen3.8-flash"),
        })
        self.save("run_created")

    def save(self, event, **details):
        self.sequence += 1
        meta = {"sequence": self.sequence, "at": datetime.now(timezone.utc).isoformat(),
                "event": event}
        # The checkpoint is authoritative across views, including a remainder + garment commit.
        write_json(self.directory / "checkpoint.json", {"sequence": self.sequence, **self.state})
        write_json(self.directory / "task/progress.json",
                   {k: v for k, v in self.state.items() if k not in GARMENT_FIELDS})
        write_json(self.directory / "scene/garments.json",
                   {k: self.state[k] for k in GARMENT_FIELDS})
        append_event(self.directory / "logs/events.jsonl", {**meta, "details": details,
                     "phase": self.state["phase"], "remaining": self.state["remaining"]})
        attempt = self.state.get("attempt") or {}
        instruction = attempt.get("instruction")
        if instruction:
            revision = (attempt.get("step"), attempt.get("number"), instruction)
            if self.last_prompt != revision:
                append_event(self.directory / "task/prompt_revisions.jsonl",
                             {**meta, "attempt": attempt.copy(), "details": details})
                self.last_prompt = revision

    def capture(self, sdk, timeout):
        snap = sdk.capture(self.images_dir, timeout=timeout)
        stamp = {"observation_id": snap["id"], "captured_at": snap["captured_at"]}
        # Keep every measured pose with its frames, including observations taken before a fault.
        append_event(self.directory / "logs/observations.jsonl", snap)
        write_json(self.directory / "scene/arms.json", {**stamp, "arms": snap["arms"]})
        write_json(self.images_dir / "manifest.json", {**stamp, "images": snap["images"]})
        return snap


def main(argv=None):
    parser = argparse.ArgumentParser(description="EX001 V2 dynamic task agent")
    parser.add_argument("--data", type=Path, default=DATA)
    parser.add_argument("--task", help="Original command (otherwise use data/task/progress.json)")
    parser.add_argument("--real", action="store_true", help="Enable model calls and physical motion")
    parser.add_argument("--server", help="EX001 SDK host:port")
    parser.add_argument("--embodiment", choices=("quanta_x1_pro/gripper-g", "quanta_x1_pro/gripper-h"))
    parser.add_argument("--time-limit", type=float, default=480.0, help="Whole run in seconds, at most 480")
    args = parser.parse_args(argv)
    load_local_env(DATA.parent / ".env")
    root = args.data.resolve()
    config = load_config(root)
    task = read_json(root / "task/progress.json")["original"] if args.task is None else args.task
    expected = read_json(root / "scene/garments.json")["expected_count"]
    if not isinstance(task, str) or (expected is not None and (type(expected) is not int or expected < 0)):
        raise ValueError("original must be text; expected_count must be null or a nonnegative integer")
    if not args.real:
        print(f"V2 configuration OK: {len(config['actions'])} catalog actions. Offline configuration check.")
        return 0
    if not (task.strip() and args.embodiment and args.server):
        parser.error("--real requires a task, --server and the actual --embodiment")
    if not math.isfinite(args.time_limit) or not 0 < args.time_limit <= 480:
        parser.error("time limit must be positive and at most 480 seconds")
    if not all(os.getenv(key) for key in ("QWEN_API_KEY", "WALLX_API_KEY", "WALLX_MODEL_ADDRESS")):
        parser.error("--real requires QWEN_API_KEY, WALLX_API_KEY and WALLX_MODEL_ADDRESS")
    deadline = time.monotonic() + args.time_limit
    store = RunStore(root, config, task, expected)
    print(f"Run directory: {store.directory}", flush=True)
    try:
        client = Client(embodiment=args.embodiment, log_dir=store.directory / "logs")
        try:
            sdk = SDK(server=args.server)
            run_task(store, sdk, client, plan, judge, optimize, deadline=deadline)
        finally:
            client.stop()
    except (Exception, KeyboardInterrupt) as exc:
        store.state["phase"] = "stopped"
        store.save("run_interrupted", error_type=type(exc).__name__, reason=str(exc))
        raise
    return 0


if __name__ == "__main__":
    def request_stop(signum, _frame):
        raise KeyboardInterrupt(f"Received signal {signum}")

    signal.signal(signal.SIGTERM, request_stop)
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except (RuntimeError, ValueError, OSError, KeyError) as exc:
        print(f"EX001: {exc}", file=sys.stderr)
        sys.exit(1)
