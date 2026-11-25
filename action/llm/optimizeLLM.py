"""Rewrite a single EX001 instruction using its locked goal and feedback."""

from .taskLLM import QwenError, _context_json, _qwen_chat


_OPTIMIZE_PROMPT = """你是 EX001 的单动作提示词优化器。将 action 中已经锁定的衣物、动作和成功条件写成一条可发给 Wall-X 的操作指令。action 的模板和可选 preset 可补充措辞和语言；previous_instruction 是上次指令，feedback 是本轮观测反馈，constraints 是全过程约束。
保持衣物身份、动作范围、用户目标、目标区域、必须满足的验收条件以及必须保持的夹持或折形。抓取部位、方向、可见标记、放置与松手时机只能从现场已知事实细化。观测明确显示部分已完成时，从实测状态接续同一目标。保留已有有效部分，避免把推测原因写成观测事实。遇到遮挡或失败原因未知时，只使用可以确认的事实。不得新增步骤、恢复分支、动作编号、精确坐标或底层控制命令。
遵循操作指令语言设置，若未指定则沿用 previous_instruction 或 action.instruction 的语言。只输出一条纯文本指令，写明操作对象、动作和终态。"""


def optimize(context: dict, *, timeout: float) -> str:
    """Return a single ready-to-send instruction for the current action."""
    action = context.get("action")
    if not isinstance(action, dict) or not action:
        raise ValueError("Optimization requires a locked action")
    result = _qwen_chat(_OPTIMIZE_PROMPT, _context_json(context), timeout=timeout)
    if len(result.splitlines()) != 1 or result.startswith(("```", "{", "[")):
        raise QwenError("Optimizer must return one plain-text instruction")
    return result
