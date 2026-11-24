"""High-level task planner. Never generates motor trajectories."""
import json
import os
import re
from dataclasses import dataclass
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class Step:
    instruction: str
    success_condition: str
    timeout_s: int = 60

    def __post_init__(self):
        if not (1 <= len(self.instruction.strip()) <= 300):
            raise ValueError("Each instruction must contain 1-300 characters")
        if not (1 <= len(self.success_condition.strip()) <= 300):
            raise ValueError("Each success condition must contain 1-300 characters")
        if not (5 <= self.timeout_s <= 300):
            raise ValueError("timeout_s must be between 5 and 300 seconds")


def parse_plan(raw) -> list[Step]:
    if isinstance(raw, dict):
        raw = raw.get("steps", [])
    if not isinstance(raw, list) or not 1 <= len(raw) <= 12:
        raise ValueError("Plan requires 1-12 steps")
    steps = []
    for item in raw:
        if isinstance(item, str):
            steps.append(Step(item, item))
        elif isinstance(item, dict):
            instruction = item["instruction"]
            steps.append(Step(instruction, item.get("success_condition", instruction),
                              int(item.get("timeout_s", 60))))
        else:
            raise ValueError("Invalid plan step")
    return steps


def heuristic_plan(task: str) -> list[Step]:
    """Rule-based fallback: segment high-level, self-contained tasks."""
    chunks = re.split(r"(?:然后|接着|随后|；|;|\n)+", task.strip())
    chunks = [s.strip(" ，。,. ") for s in chunks if s.strip(" ，。,. ")]
    return parse_plan(chunks)


def chat_api(url: str, key: str, model: str, messages: list[dict]) -> str:
    """Optional OpenAI-compatible chat/completions endpoint; NOT Wall-X."""
    request = Request(
        url,
        data=json.dumps({"model": model, "messages": messages, "temperature": 0}).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
        method="POST",
    )
    with urlopen(request, timeout=45) as response:
        output = json.load(response)
    return output["choices"][0]["message"]["content"]


def llm_plan(task: str) -> list[Step]:
    """Model generates only a high-level step plan, never coordinates/commands."""
    required = ["PLANNER_API_URL", "PLANNER_API_KEY", "PLANNER_MODEL"]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        raise RuntimeError(f"Set the planner environment variables: {', '.join(missing)}")
    rules = (
        "You are a robot high-level task planner. Return ONLY JSON object with key steps. "
        "steps is a list of 1-12 objects: instruction, success_condition, timeout_s (5..300). "
        "Plan only self-contained pick-and-place/arrangement goals that can be observed "
        "after completion. Avoid splitting pick and place into separate steps: the robot "
        "client is restarted between steps. Do not output joint commands, coordinates, "
        "dangerous or destructive tasks. Do not invent object locations. "
        "Instructions should be short and explicit. "
    )
    result = chat_api(os.environ["PLANNER_API_URL"], os.environ["PLANNER_API_KEY"],
                      os.environ["PLANNER_MODEL"], [
                          {"role": "system", "content": rules},
                          {"role": "user", "content": task},
                      ])
    result = result.strip()
    if result.startswith("```"):
        result = re.sub(r"^```(?:json)?\s*|\s*```$", "", result)
    return parse_plan(json.loads(result))
