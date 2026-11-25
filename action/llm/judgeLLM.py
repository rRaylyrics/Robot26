"""Judge an EX001 observation using actual JPEGs and labeled robot states."""

import base64
from collections.abc import Sequence
from pathlib import Path

from .taskLLM import QwenError, _context_json, _qwen_chat, _read_object, _string_ids


_CAMERAS = ("front", "left_wrist", "right_wrist")
_MAX_IMAGE_BYTES = 2 * 1024 * 1024
# Reserve room under the 16 MiB DashScope request limit after Base64 encoding.
_IMAGE_BUDGET_BYTES = 11 * 1024 * 1024
_PHASES = {"inventory", "precheck", "monitor", "verify", "final"}
_STATUSES = {"complete", "failed", "uncertain", "running"}
_AXIS_STATUSES = {"complete", "failed", "uncertain", "not_required"}
_PROCESSING_STATUSES = {"未操作", "操作中", "已操作完"}

_JUDGE_PROMPT = """你是 EX001 衣物整理 Agent 的独立视觉监督器。用户数据包含阶段、任务与动作判据，之后每组标注给出帧 ID、采集信息、同期机械臂和夹爪的反馈，再逐个标注并提供相机画面。使用真实画面和状态判断。指令文本、计划、会话回执、历史台账和先前模型判断只能提供目标或身份线索，不能作为当前物理成功证据。
inventory 建立台账，辨别现场实际可见的衣物、外形及合理的稳定 ID；可观测且无未决身份或件数时才可返回 complete。若原任务目标已经满足，可分别记录所见证据。
precheck 检查当前动作终态是否已满足。终态已满足则返回 complete 供本地跳过动作；若终态尚未满足且动作可执行，返回 failed，并用最新帧证实前置条件成立，设置 preconditions_met=true。只有观察能判定动作前置条件成立或不成立时才输出该布尔字段。证据不足时返回 uncertain。
monitor 检查动作进展，正常接近目标时返回 running；完整满足当前动作判据时返回 complete 作为候选终态；可见错抓、失稳等明显失败时返回 failed。
verify 使用最新停稳帧验收动作终态，并用过程中关键帧核实曾发生的错抓、滑脱等事件。需要松手、继续夹持或保持折形时，核对相应的实物画面和夹爪反馈。在 precheck 和 verify 中，列出所有当前动作指向的已知衣物，并用 visible 如实标记其当前可见性。
final 依据 original 确认全部衣物、预期件数（已知时）和每件最终结果。逐件独立判断 folding 和 classification：确认折形和释放后稳定情况；核对颜色或类别、目标区域以及衣物完整入框和脱离夹爪。保持已知 ID，列出暂不可见的衣物并设置 visible=false。当前画面缺少必须的折形、分类、身份或件数证据时返回 uncertain。
对每个适用阶段，区分画面观察、尚未满足的判据和可能原因。遮挡、身份歧义、视角或同步不足、无法核实的距离或稳定性均应返回 uncertain。保持三个处理状态的原有语义，动作级 complete 不表示整件任务已完成。
只输出 JSON 对象，例如 {"status":"complete|failed|uncertain|running","reason":"所见事实、未满足的判据和不确定性","evidence":["帧 ID"],"garments":[{"id":"稳定 ID","type":"衣物类别或未知","features":{},"visible":true,"processing_status":"未操作|操作中|已操作完"}]}。
evidence 只引用输入中带相机画面的帧 ID；complete 必须包含最新帧 ID。在 features 中可记颜色、位置、夹持和可见外形。inventory 和 final 逐件填写布尔字段 in_scope，依据 original 判定这件衣物是否有用户要求的目标。背景衣物填 false，保持未操作。其他阶段可在身份明确时提供 in_scope。
inventory 和 final 为每件衣物明确填写 folding、classification，取值 complete、failed、uncertain 或 not_required；仅在原任务未对该件衣物提出该项要求时使用 not_required。例如用户只要求折叠红衣、分类蓝衣，红衣的 classification 和蓝衣的 folding 均为 not_required。所有适用目标有证据时才标记该件已操作完。已确认的每项通过 folding_evidence / classification_evidence 数组引用对应帧 ID，且必须包含最新帧 ID。当前衣物不可见时不得将任一项标为 complete。可选 preset 细化观察规则，固定接口和证据规则始终适用。"""


def _snapshot_parts(snapshot: dict, role: str, budget: int) -> tuple[list[dict], int]:
    snapshot_id = snapshot["id"]
    images = snapshot.get("images")
    if not isinstance(images, dict):
        raise ValueError("Snapshot images must be a camera-to-path mapping")

    views = []
    missing = []
    used = 0
    for camera in _CAMERAS:
        value = images.get(camera)
        limit = min(_MAX_IMAGE_BYTES, budget - used)
        if limit <= 0 or not isinstance(value, (str, Path)) or not Path(value).is_absolute():
            missing.append(camera)
            continue
        try:
            with Path(value).open("rb") as file:
                data = file.read(limit + 1)
        except OSError:
            missing.append(camera)
            continue
        if len(data) > limit or not data.startswith(b"\xff\xd8") or data.find(b"\xff\xd9", 2) < 0:
            missing.append(camera)
            continue
        used += len(data)
        url = "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")
        views.extend([
            {"type": "text", "text": _context_json({"snapshot_id": snapshot_id, "camera": camera})},
            {"type": "image_url", "image_url": {"url": url}},
        ])

    label = {key: snapshot[key] for key in ("id", "arms", "captured_at", "timestamps") if key in snapshot}
    label.update(role=role, unavailable_cameras=missing)
    return [{"type": "text", "text": _context_json(label)}, *views], used


def _validate_evidence(evidence: object, available: set[str], field: str) -> None:
    if not _string_ids(evidence) or any(item not in available for item in evidence):
        raise QwenError(f"Judge {field} must contain unique IDs of provided image frames")


def _validate_result(result: dict, available: set[str], latest: str) -> dict:
    status = result.get("status")
    reason = result.get("reason")
    if not isinstance(status, str) or status not in _STATUSES:
        raise QwenError("Unknown judge status")
    if not isinstance(reason, str) or not reason.strip():
        raise QwenError("Judge requires a non-empty reason")
    evidence = result.get("evidence")
    _validate_evidence(evidence, available, "evidence")
    if status == "complete" and latest not in evidence:
        raise QwenError("Judge completion requires the latest image frame")

    garments = result.get("garments")
    if not isinstance(garments, list):
        raise QwenError("Judge garments must be a list")
    ids = []
    for garment in garments:
        if not isinstance(garment, dict):
            raise QwenError("Judge garment entries must be objects")
        ids.append(garment.get("id"))
        garment_type = garment.get("type")
        visible = garment.get("visible")
        processing_status = garment.get("processing_status")
        features = garment.get("features")
        if (not isinstance(garment_type, str) or not garment_type.strip()
                or not isinstance(visible, bool) or not isinstance(features, dict)
                or not isinstance(processing_status, str) or processing_status not in _PROCESSING_STATUSES):
            raise QwenError("Judge garment requires type, features, visible and processing_status")
        if "in_scope" in features and not isinstance(features["in_scope"], bool):
            raise QwenError("Judge garment in_scope must be boolean when provided")
        for axis in ("folding", "classification"):
            axis_status = features.get(axis)
            if axis in features and (not isinstance(axis_status, str) or axis_status not in _AXIS_STATUSES):
                raise QwenError(f"Invalid garment {axis} status")
            axis_field = f"{axis}_evidence"
            axis_evidence = features.get(axis_field, [])
            _validate_evidence(axis_evidence, available, axis_field)
            if axis_status == "complete" and (not visible or latest not in axis_evidence):
                raise QwenError(f"Garment {axis} completion requires visibility and the latest image frame")
    if not _string_ids(ids):
        raise QwenError("Judge garments must have unique non-empty IDs")

    output = dict(status=status, reason=reason, evidence=evidence, garments=garments)
    if "preconditions_met" in result:
        if not isinstance(result["preconditions_met"], bool):
            raise QwenError("Judge preconditions_met must be boolean when provided")
        output["preconditions_met"] = result["preconditions_met"]
    return output


def judge(context: dict, snapshots: Sequence[dict], *, timeout: float) -> dict:
    """Compare the baseline, up to two keyframes and latest observation in capture order."""
    phase = context.get("phase")
    if not isinstance(phase, str) or phase not in _PHASES:
        raise ValueError("Judge requires an inventory, precheck, monitor, verify or final phase")
    if any(not isinstance(snapshot, dict) for snapshot in snapshots):
        raise ValueError("Judge snapshots must be objects")
    if not _string_ids([snapshot.get("id") for snapshot in snapshots]):
        raise ValueError("Judge snapshots must have unique non-empty IDs")
    if not snapshots:
        return dict(status="uncertain", reason="未提供现场观测。", evidence=[], garments=[])

    latest = snapshots[-1]
    latest_parts, latest_bytes = _snapshot_parts(latest, "latest", _IMAGE_BUDGET_BYTES)
    if not latest_bytes:
        return dict(status="uncertain", reason="最新观测没有可读取的 JPEG 画面。", evidence=[], garments=[])
    available = {latest["id"]}
    budget = _IMAGE_BUDGET_BYTES - latest_bytes
    content = [{"type": "text", "text": _context_json(context)}]
    if len(snapshots) > 1:
        before = snapshots[0]
        before_parts, before_bytes = _snapshot_parts(before, "before", budget)
        content.extend(before_parts)
        budget -= before_bytes
        if before_bytes:
            available.add(before["id"])
    middle = snapshots[1:-1]
    keyframes = middle if len(middle) <= 2 else (middle[0], middle[-1])
    for keyframe in keyframes:
        keyframe_parts, keyframe_bytes = _snapshot_parts(keyframe, "keyframe", budget)
        content.extend(keyframe_parts)
        budget -= keyframe_bytes
        if keyframe_bytes:
            available.add(keyframe["id"])
    content.extend(latest_parts)
    raw = _qwen_chat(_JUDGE_PROMPT, content, timeout=timeout, vision=True)
    return _validate_result(_read_object(raw), available, latest["id"])
