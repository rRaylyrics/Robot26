"""Qwen task planning and shared DashScope chat transport."""

import json
import math
import os
import re
from http.client import HTTPException
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


_DEFAULT_BASE_URL = "https://maas.qianwenaiapi.com/compatible-mode/v1"
_MAX_TIMEOUT = 60.0
_MAX_REQUEST_BYTES = 16 * 1024 * 1024
_MAX_RESPONSE_BYTES = 1024 * 1024

_PLAN_PROMPT = """你是 EX001 衣物整理 Agent 的任务规划器。结合任务、当前余令、已观测的衣物台账、动作库和比赛约束，只安排一个当前可执行且终态可观测的动作。
返回一个 JSON 对象，字段如下，全部必填：
{"kind":"direct|atomic|custom|unclear","instruction":"","remainder":"","action_code":"","success_condition":"","garment_ids":[]}
direct：仅当衣物可唯一定位、终态明确、前置条件已满足且无需中间决策时，逐字保留当前余令，remainder 和 action_code 为空。
atomic：拆出库中一个动作，action_code 必须原样取自 catalog，遵守其物理判据和使用范围。
custom：无对应库项且约束允许时，给出一个简单动作，action_code 为空。动作终态须可独立观测。
unclear：目标指代、前置状态或允许范围无法确认时，instruction 和 action_code 填空，remainder 保留当前余令，在 success_condition 写明应补充的证据。
拆解时，instruction 只描述本轮动作；remainder 保留未完成的用户义务和持续约束，已经单列的动作不再写进余令。按已验证的场景事实跳过已完成目标。garment_ids 是台账中与本轮相关的稳定 ID 字符串列表。
success_condition 写具体可观察终态，包括任务要求的衣物、目标区域、折形、释放或保持夹持。不得编造衣物 ID、官方动作编号、现场状态或精确坐标。约束和可选 preset 用来细化任务，固定接口和输出格式始终适用。
只输出 JSON。"""


class QwenError(RuntimeError):
    """The Qwen request or returned model output could not be used."""


def _context_json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _qwen_chat(system_prompt: str, content: str | list[dict], *,
               timeout: float, vision: bool = False) -> str:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ValueError("Qwen timeout must be a finite positive number of seconds")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Qwen timeout must be a finite positive number of seconds")
    key = os.environ.get("QWEN_API_KEY", "").strip()
    if not key:
        raise QwenError("Set QWEN_API_KEY before calling Qwen")
    base_url = os.environ.get("QWEN_BASE_URL", _DEFAULT_BASE_URL).strip().rstrip("/")
    parsed = urlsplit(base_url)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.query or parsed.fragment or parsed.username or parsed.password):
        raise QwenError("QWEN_BASE_URL must be a valid OpenAI-compatible base URL")
    url = base_url if parsed.path.endswith("/chat/completions") else base_url + "/chat/completions"
    model_variable = "QWEN_VISION_MODEL" if vision else "QWEN_TEXT_MODEL"
    model = os.environ.get(model_variable, "qwen3.8-flash").strip()
    if not model:
        raise QwenError(f"Set {model_variable} to a model name")

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ],
        "temperature": 0,
        "stream": False,
    }
    encoded = _context_json(body).encode("utf-8")
    if len(encoded) > _MAX_REQUEST_BYTES:
        raise QwenError("Qwen request body exceeds 16 MiB")
    request = Request(url, data=encoded, method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_unredirected_header("Authorization", f"Bearer {key}")
    try:
        with urlopen(request, timeout=min(timeout, _MAX_TIMEOUT)) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        exc.close()
        raise QwenError(f"Qwen returned HTTP {exc.code}") from None
    except (OSError, HTTPException) as exc:
        raise QwenError(f"Qwen request failed ({type(exc).__name__})") from None
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise QwenError("Qwen response is too large")
    try:
        choice = json.loads(raw)["choices"][0]
        result = choice["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise QwenError("Qwen returned an invalid chat completion") from exc
    if choice.get("finish_reason") not in (None, "stop"):
        raise QwenError("Qwen did not finish writing a usable response")
    if not isinstance(result, str) or not result.strip():
        raise QwenError("Qwen returned empty message content")
    return result.strip()


def _read_object(content: str) -> dict:
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", content, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        content = fenced.group(1)
    try:
        result = json.loads(content)
        _context_json(result)  # JSON constants such as NaN cannot be written to the event log.
    except ValueError as exc:
        raise QwenError("Qwen must return valid JSON") from exc
    if not isinstance(result, dict):
        raise QwenError("Qwen must return a JSON object")
    return result


def _string_ids(value: object) -> bool:
    return (isinstance(value, list)
            and all(isinstance(item, str) and item.strip() for item in value)
            and len(value) == len(set(value)))


def plan(context: dict, *, timeout: float) -> dict:
    """Return one proposed action and its uncommitted remainder."""
    current = context.get("current")
    if not isinstance(current, str) or not current.strip():
        raise ValueError("Planning requires a non-empty current instruction")
    catalog = context.get("catalog", [])
    if not isinstance(catalog, (list, tuple)) or any(
        not isinstance(item, dict) or not isinstance(item.get("code"), str) or not item["code"].strip()
        for item in catalog
    ):
        raise ValueError("Planning catalog requires action entries with non-empty codes")
    codes = {item["code"] for item in catalog}

    result = _read_object(_qwen_chat(_PLAN_PROMPT, _context_json(context), timeout=timeout))
    fields = ("kind", "instruction", "remainder", "action_code", "success_condition")
    if any(not isinstance(result.get(field), str) for field in fields):
        raise QwenError("Plan requires string fields for kind, instruction, remainder, action_code and success_condition")
    if not _string_ids(result.get("garment_ids")):
        raise QwenError("Plan garment_ids must be a list of unique non-empty strings")
    kind = result["kind"]
    if kind not in {"direct", "atomic", "custom", "unclear"}:
        raise QwenError("Unknown plan kind")
    code = result["action_code"]
    if kind == "atomic" and code not in codes:
        raise QwenError("Plan action_code must reference the provided catalog")
    if kind != "atomic" and code:
        raise QwenError(f"A {kind} plan requires an empty action_code")
    result = {field: result[field] for field in (*fields, "garment_ids")}
    if kind == "direct":
        if result["remainder"].strip():
            raise QwenError("A direct plan must finish the full current instruction")
        result.update(instruction=current, remainder="")
    elif kind == "unclear":
        result.update(instruction="", remainder=current)
    elif not result["instruction"].strip():
        raise QwenError("A split plan requires one action instruction")
    if kind != "unclear" and not result["success_condition"].strip():
        raise QwenError("An executable plan requires an observable success condition")
    return result
