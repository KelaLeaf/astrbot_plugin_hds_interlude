"""上游 `src/script/development.ts` 的 Python 对应物：人物成长与倾向的证据链。

指向所提供原文的**引用**，把「观察 / 解释 / 接收」三者绑在一起，而不另写一份
心理摘要。

键名（键名约定）
------------------------------------------
- `interaction_evidence()` 是**模型可见的 wire 投影**：它的结果由
  `narrator_prompts.to_compaction_payload` 放进 `interactionEvidence` 发给模型，
  上游压缩提示词按 `feedbackEntryIds` / `responseEntryIds` 要求模型回填
  `interactionReview`。因此字段**逐字保持上游 camelCase**
  （`participantId` / `feedbackEntryId` / `priorCommunicationEntryId` /
  `interpretationEntryIds` / `responseEntryIds`）。
- 入参 `entries` 是内部领域对象（`types.ScriptEntry`，snake_case），读取按既有拼写。
- `ScriptEntry.metadata` 的键同样按 `snake_case` 存放，本模块读 `scene_checkpoint`
  （内含 `scene_id` / `first_entry_id` / `last_entry_id`）与 `frame_id`。
- JS 里 `undefined` / `NaN` 参与比较恒为 false，这里用显式的数值守卫复现
  （`_safe_integer`），避免 Python 抛 `TypeError`。
"""

from __future__ import annotations

import re
from typing import Any, Optional, Required, TypedDict

from ..types import ScriptEntry, StatePatchDraft, StatePatchProposal

# 上游 `const dimensions`。
_DIMENSIONS: dict[str, tuple[str, ...]] = {
    'character': ('traits', 'preferences', 'coping'),
    'perspective': ('values', 'interpretation'),
    'relationship': ('trust', 'closeness', 'boundaries'),
    'world': ('established',),
}
# 上游 `path.trim().replace(/^(development|character|perspective|relationship|world)\./, '')`
_DIMENSION_PREFIX = re.compile(r'^(development|character|perspective|relationship|world)\.')
# `Number.isSafeInteger` 的上界。
_MAX_SAFE_INTEGER = 9007199254740991


class InteractionEvidence(TypedDict, total=False):
    """上游 `interactionEvidence` 的元素形状（上游为内联对象字面量）。

    这是**模型可见的 wire 结构**，故字段一律上游 camelCase。
    """

    participantId: Required[str]
    feedbackEntryId: Required[int]
    # 没有更早的角色消息时上游是 `undefined`；Python 用 None。
    priorCommunicationEntryId: Optional[int]
    interpretationEntryIds: Required[list[int]]
    responseEntryIds: Required[list[int]]


def _safe_integer(value: Any) -> bool:
    """等价 JS `Number.isSafeInteger(value)`。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if isinstance(value, float) and not value.is_integer():
        return False
    return abs(value) <= _MAX_SAFE_INTEGER


def _value_of(value: Any) -> Any:
    """安全读取可能为 None / 非数值的比较值。"""
    return value if _safe_integer(value) else None


def interaction_evidence(entries: list[ScriptEntry]) -> list[InteractionEvidence]:
    """把每条用户反馈和它的「此前沟通 → 实际回应 → 她的解释」串成一条证据链。

    只取最后 16 条用户反馈；每条链只在**同一个关系分支**内寻找前后文。
    """
    ordered = sorted(entries, key=lambda entry: entry.get('id') if _safe_integer(entry.get('id')) else 0)
    feedbacks = [entry for entry in ordered if entry.get('kind') == 'user-message'][-16:]
    evidence: list[InteractionEvidence] = []
    for feedback in feedbacks:
        feedback_id = feedback.get('id')
        branch = [entry for entry in ordered if entry.get('participant_id') == feedback.get('participant_id')]
        # 上游 `branch.find(...)?.id ?? Infinity`：同一分支里下一条用户消息，没有则正无穷。
        next_feedback = next(
            (entry['id'] for entry in branch
             if _safe_integer(entry.get('id')) and entry['id'] > feedback_id
             and entry.get('kind') == 'user-message'),
            float('inf'),
        )
        prior = [entry for entry in branch
                 if _safe_integer(entry.get('id')) and entry['id'] < feedback_id
                 and entry.get('kind') == 'character-message']
        middle = [entry for entry in branch
                  if _safe_integer(entry.get('id')) and feedback_id < entry['id'] < next_feedback]
        evidence.append({
            'participantId': feedback.get('participant_id'),
            'feedbackEntryId': feedback_id,
            'priorCommunicationEntryId': prior[-1]['id'] if prior else None,
            'interpretationEntryIds': [entry['id'] for entry in middle if entry.get('kind') == 'script'],
            'responseEntryIds': [entry['id'] for entry in middle if entry.get('kind') == 'character-message'],
        })
    return evidence


def reviewed_development_support(draft: StatePatchDraft, entries: list[ScriptEntry], participant_id: str) -> bool:
    """只有被 `interactionReview` 明确评为 supported、且引用与源条目逐条对得上的
    关系类成长，才允许落库。"""
    review = draft.get('interaction_review')
    if (not isinstance(review, dict) or review.get('outcome') != 'supported'
            or not isinstance(review.get('feedback_entry_ids'), list)
            or not isinstance(review.get('response_entry_ids'), list)):
        return False
    cited = list(draft.get('source_entry_ids') or [])

    def valid(entry_id: Any, kind: str) -> bool:
        return entry_id in cited and any(
            entry.get('id') == entry_id and entry.get('kind') == kind
            and entry.get('participant_id') == participant_id for entry in entries)

    feedback_ids = review['feedback_entry_ids']
    response_ids = review['response_entry_ids']
    if not feedback_ids or not response_ids:
        return False
    if not all(valid(entry_id, 'user-message') for entry_id in feedback_ids):
        return False
    if not all(valid(entry_id, 'character-message') for entry_id in response_ids):
        return False
    # 回应必须发生在全部反馈之后（Math.max 在列表非空时才有定义）。
    newest_feedback = max(feedback_ids)
    return any(_safe_integer(entry_id) and entry_id > newest_feedback for entry_id in response_ids)


def development_context_query(
    user_message: Optional[str],
    due_summaries: list[str],
    visible_entries: list[ScriptEntry],
) -> str:
    """安静的一轮也有生活上下文：把可见原文只当作**相关性查询**，
    绝不当成新的学习观察或证据权重。"""
    if isinstance(user_message, str) and user_message.strip():
        return user_message.strip()
    scripts = [entry for entry in visible_entries if entry.get('kind') == 'script']
    last_script = scripts[-1] if scripts else None
    content = last_script.get('content') if isinstance(last_script, dict) else None
    parts: list[Any] = [*due_summaries, (content or '')[-800:]]
    texts = [part for part in parts if part]
    return '\n'.join(part if isinstance(part, str) else str(part) for part in texts)[:1200]


def development_dimension(target: str, path: str) -> Optional[str]:
    """canon 保持原样；新的成长提案只使用一小组词汇。"""
    normalized = _DIMENSION_PREFIX.sub('', path.strip())
    allowed = _DIMENSIONS.get(target)
    return normalized if allowed and normalized in allowed else None


def _scene_bounds_cover(checkpoint: Any, entry_id: Any) -> bool:
    """上游 `Number.isSafeInteger(item.sceneId) && entry.id >= item.firstEntryId && entry.id <= item.lastEntryId`。

    任何一侧不是安全整数时，JS 的比较结果都是 false；这里返回 False 保持一致。
    """
    if not isinstance(checkpoint, dict) or not _safe_integer(checkpoint.get('scene_id')):
        return False
    first = _value_of(checkpoint.get('first_entry_id'))
    last = _value_of(checkpoint.get('last_entry_id'))
    current = _value_of(entry_id)
    if first is None or last is None or current is None:
        return False
    return first <= current <= last


def development_scenes(entries: list[ScriptEntry]) -> int:
    """一个已完成的场景只贡献一次，与散文长度或回合数无关。

    没有场景出处的旧数据行仍然可读，但不能靠时间戳把置信度乘出多份。
    """
    checkpoints: list[dict[str, Any]] = []
    for entry in entries:
        metadata = entry.get('metadata')
        checkpoint = metadata.get('scene_checkpoint') if isinstance(metadata, dict) else None
        if isinstance(checkpoint, dict):
            checkpoints.append(checkpoint)

    scenes: set[str] = set()
    frame_scenes: dict[str, str] = {}
    for entry in entries:
        checkpoint = next((item for item in checkpoints if _scene_bounds_cover(item, entry.get('id'))), None)
        metadata = entry.get('metadata')
        frame = metadata.get('frame_id') if isinstance(metadata, dict) else None
        if checkpoint is not None and isinstance(frame, str):
            frame_scenes[frame] = f"scene:{checkpoint['scene_id']}"
    for entry in entries:
        if entry.get('kind') != 'script':
            continue
        checkpoint = next((item for item in checkpoints if _scene_bounds_cover(item, entry.get('id'))), None)
        metadata = entry.get('metadata')
        frame = metadata.get('frame_id') if isinstance(metadata, dict) else None
        if checkpoint is not None:
            scenes.add(f"scene:{checkpoint['scene_id']}")
        elif isinstance(frame, str) and frame:
            scenes.add(frame_scenes.get(frame) or frame)
    return len(scenes)


def prompt_ready_development(candidate: StatePatchProposal, entries: list[ScriptEntry]) -> bool:
    """暂定的成长仍可审计地留在存储里，但在跨过场景边界之前不会变成
    prompt 侧的预期。已应用的倾向本来就通过了宿主更严格的证据闸门。"""
    if candidate.get('status') == 'applied':
        return True
    sources = list(candidate.get('source_entry_ids') or [])
    return development_scenes([entry for entry in entries if entry.get('id') in sources]) >= 2
