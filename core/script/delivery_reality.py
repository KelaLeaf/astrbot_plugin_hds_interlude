"""投递现实（上游 `src/script/delivery-reality.ts` 的 Python 对应物）。

上游：Koishi / TypeScript，v1.0.1-beta6-rebuild。

**只应传入已按当前关系过滤过的条目**。这是同一份剧本的执行注记，永远不是替换
正文的第二份散文：它把「已确认送达 / 未确认 / 出错后仍未确认 / 已取消」如实报告
给叙事器，绝不改写原文。

键名（键名约定）
------------------------------------------
- **返回值是 wire format**：它由 `narrator_prompts.to_prompt_payload` 放进
  `ongoingThreads.deliveryReality` 直接发给模型，上游 `systemPrompt` 也按
  `deliveryReality` / `communicationOutcome` 指路。因此输出键名**逐字保持上游
  camelCase**：`sourceEntryId` / `eventId` / `segments` / `communicationOutcome`。
- **入参条目是内部领域对象**（`types.ScriptEntry`，键名 snake_case）：`metadata`
  里存 `delivery_actions` / `commit_id` / `participant_id`。读取一律走
  `_get(..., camel, snake)`，旧 Koishi camelCase 数据与模型输出同样认。
"""

from __future__ import annotations

from typing import Any, Optional

_SEGMENT_STATUSES = ('pending', 'delivered', 'failed', 'cancelled')


def _get(mapping: Any, *keys: str, default: Any = None) -> Any:
    """按顺序读取第一个存在的键（兼容 snake_case / camelCase 两种拼写）。"""
    if isinstance(mapping, dict):
        for key in keys:
            if key in mapping:
                return mapping[key]
    return default


def _is_record(value: Any) -> bool:
    """上游 `isRecord()`。"""
    return isinstance(value, dict)


def delivery_reality(
    entries: list[dict[str, Any]],
    participant_id: Optional[str] = None,
    share_participant_details: bool = False,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """上游 `deliveryReality()`：把账本压缩成有界的执行注记。

    只报告**仍有未确认片段**的行动（全部已送达的行动不进上下文）。
    与上游一致保留 ``slice(-limit)`` 的语义：``limit == 0`` 时返回全部条目。
    """
    collected: list[dict[str, Any]] = []
    for entry in entries:
        if _get(entry, 'kind') != 'script':
            continue
        metadata = _get(entry, 'metadata')
        if not _is_record(metadata):
            continue
        actions = _get(metadata, 'deliveryActions', 'delivery_actions')
        if not isinstance(actions, list):
            continue
        commit_id = _get(metadata, 'commitId', 'commit_id')
        if not actions and commit_id:
            collected.append({
                'sourceEntryId': entry.get('id'),
                'eventId': '',
                'segments': [],
                'communicationOutcome': 'no-outgoing-action-recorded',
            })
            continue
        for action in actions:
            if not _is_record(action) or _get(action, 'commitId', 'commit_id') != commit_id:
                continue
            raw_segments = _get(action, 'segments')
            if not isinstance(raw_segments, list):
                continue
            action_participant_id = _get(action, 'participantId', 'participant_id')
            if (action_participant_id and action_participant_id != participant_id
                    and not share_participant_details):
                continue
            segments = [
                item for item in raw_segments
                if _is_record(item)
                and isinstance(_get(item, 'content'), str)
                and _get(item, 'status') in _SEGMENT_STATUSES
            ]
            if not any(_get(item, 'status') != 'delivered' for item in segments):
                continue
            collected.append({
                'sourceEntryId': entry.get('id'),
                'eventId': _get(action, 'eventId', 'event_id'),
                'segments': [
                    {
                        'kind': _get(item, 'kind'),
                        'content': _get(item, 'content'),
                        'outcome': _segment_outcome(_get(item, 'status')),
                    }
                    for item in segments
                ],
            })
    # 上游结尾是 `.slice(-limit)`：`limit` 为 `Infinity` 时（压缩载荷那条路径
    # 传的就是 `Infinity`，见 `narrator.ts` 的 `toCompactionPayload`）
    # `slice(-Infinity)` 返回整个数组，Python 的负下标不能直接表达它。
    if limit == float('inf'):
        return collected
    return collected[-limit:]


def _segment_outcome(status: Any) -> str:
    """上游内联三元：pending → not-confirmed；failed → delivery-not-confirmed-after-error。"""
    if status == 'pending':
        return 'not-confirmed'
    if status == 'failed':
        return 'delivery-not-confirmed-after-error'
    return status
