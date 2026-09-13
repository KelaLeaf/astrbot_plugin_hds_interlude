"""投递准备（上游 `src/delivery.ts` 的 Python 对应物）。

上游：Koishi / TypeScript，v1.0.1-beta6-rebuild。

职责：把一条 ``OutgoingMessageDraft`` 与它来源的剧本事件绑定起来，把多气泡
回复拆成「首条 + 后续段」，并在投递成功后把剧本事件身份写进剧本条目的 metadata。
**所有气泡共享同一个 ``commitId`` / ``eventId`` 身份**，只是 ``bubbleIndex``
不同——投递意图与回执因此可以追溯到同一条权威剧本行。

命名约定（docs/PORT_PLAN.md §2）
-------------------------------
上游 camelCase 字段转 snake_case（``script_event`` / ``later_segments`` /
``bubble_index`` / ``bubble_count`` / ``full_content`` / ``caused_by_event_ids``
/ ``script_entry_id`` …）。读取侧两种拼写都接受（``contract.py`` 等兄弟模块或
旧 JSON 可能仍是 camelCase），写出侧只写 snake_case。
"""

from __future__ import annotations

from typing import Any, Optional

MAX_SAFE_INTEGER = 9_007_199_254_740_991


def _get(mapping: Any, *keys: str, default: Any = None) -> Any:
    """按顺序读取第一个存在的键（兼容 snake_case / camelCase 两种拼写）。"""
    if isinstance(mapping, dict):
        for key in keys:
            if key in mapping:
                return mapping[key]
    return default


def _is_safe_integer(value: Any) -> bool:
    """上游 `Number.isSafeInteger()`。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if isinstance(value, float) and not value.is_integer():
        return False
    return abs(value) <= MAX_SAFE_INTEGER


try:  # 上游 `import { messageEventReference } from './script/contract'`
    from .script.contract import message_event_reference
except ImportError:  # pragma: no cover - contract.py 由并行 Agent 移植
    # 降级垫片：`contract.py` 尚未落地时，本模块退化为上游 `messageEventReference()`
    # 的等价实现（仅覆盖消息事件引用的构造）。contract.py 一旦可导入，本分支永不执行。
    def _fallback_is_outgoing(event: Any) -> bool:
        if not isinstance(event, dict):
            return False
        mode = _get(event, 'delivery_mode', 'deliveryMode')
        return (event.get('kind') in ('outgoing-message', 'group-message')
                and isinstance(event.get('content'), str)
                and isinstance(event.get('bubbles'), list)
                and mode in ('immediate', 'delayed'))

    def message_event_reference(event: Any, bubble_index: int = 0,
                                script_entry_id: Optional[int] = None) -> Optional[dict[str, Any]]:
        """上游 `messageEventReference()` 的最小降级实现。"""
        if not _fallback_is_outgoing(event):
            return None
        result: dict[str, Any] = {
            'commit_id': _get(event, 'commit_id', 'commitId'),
            'event_id': _get(event, 'event_id', 'eventId'),
        }
        if _is_safe_integer(script_entry_id):
            result['script_entry_id'] = script_entry_id
        result['event_kind'] = _get(event, 'kind')
        result['caused_by_event_ids'] = list(_get(event, 'caused_by_event_ids',
                                                  'causedByEventIds', default=[]) or [])
        result['full_content'] = event.get('content')
        result['bubble_index'] = bubble_index
        result['bubble_count'] = len(event.get('bubbles') or [])
        return result


def attach_message_event(
    message: dict[str, Any],
    event: Optional[dict[str, Any]],
    script_entry_id: Optional[int] = None,
) -> dict[str, Any]:
    """上游 `attachMessageEvent()`：把剧本事件身份挂到待投递消息上。"""
    script_event = message_event_reference(event, 0, script_entry_id) if event is not None else None
    if not script_event:
        return message
    return {**message, 'script_event': script_event}


def prepare_outgoing_delivery(message: dict[str, Any], bubbles: list[str]) -> Optional[dict[str, Any]]:
    """上游 `prepareOutgoingDelivery()`：拆成首条 + 后续段（首条为空则放弃投递）。"""
    first = bubbles[0] if bubbles else None
    later = bubbles[1:]
    if not first:
        return None
    script_event = None
    existing = _get(message, 'script_event', 'scriptEvent')
    if existing:
        script_event = {
            **existing,
            'bubble_index': 0,
            'bubble_count': len(bubbles),
            'full_content': _get(existing, 'full_content', 'fullContent') or message.get('content'),
        }
        script_event.pop('scriptEvent', None)
    prepared: dict[str, Any] = {**message, 'content': first}
    prepared.pop('scriptEvent', None)
    if later:
        prepared['later_segments'] = later
    if script_event:
        prepared['script_event'] = script_event
    return prepared


def script_event_payload(message: dict[str, Any], bubble_index: int = 0) -> dict[str, Any]:
    """上游 `scriptEventPayload()`：后续气泡 intent 里携带的剧本事件身份。"""
    script_event = _get(message, 'script_event', 'scriptEvent')
    if not script_event:
        return {}
    payload = {**script_event, 'bubble_index': bubble_index}
    payload.pop('scriptEvent', None)
    return {'script_event': payload}


def restore_message_event(value: Any, content: str) -> Optional[dict[str, Any]]:
    """上游 `restoreMessageEvent()`：从 intent payload 还原剧本事件引用。"""
    if not isinstance(value, dict):
        return None
    event = _get(value, 'script_event', 'scriptEvent')
    if not isinstance(event, dict):
        return None
    commit_id = _get(event, 'commit_id', 'commitId')
    event_id = _get(event, 'event_id', 'eventId')
    if not isinstance(commit_id, str) or not isinstance(event_id, str):
        return None
    event_kind = _get(event, 'event_kind', 'eventKind')
    if event_kind not in ('outgoing-message', 'group-message'):
        return None
    raw_causes = _get(event, 'caused_by_event_ids', 'causedByEventIds')
    caused_by_event_ids = [item for item in raw_causes if isinstance(item, str)] \
        if isinstance(raw_causes, list) else []
    script_entry_id = _get(event, 'script_entry_id', 'scriptEntryId')
    full_content = _get(event, 'full_content', 'fullContent')
    bubble_index = _get(event, 'bubble_index', 'bubbleIndex')
    bubble_count = _get(event, 'bubble_count', 'bubbleCount')
    restored: dict[str, Any] = {'commit_id': commit_id, 'event_id': event_id}
    if _is_safe_integer(script_entry_id):
        restored['script_entry_id'] = script_entry_id
    restored['event_kind'] = event_kind
    restored['caused_by_event_ids'] = caused_by_event_ids
    restored['full_content'] = full_content if isinstance(full_content, str) else content
    restored['bubble_index'] = bubble_index if _is_safe_integer(bubble_index) else 0
    restored['bubble_count'] = bubble_count if _is_safe_integer(bubble_count) else 1
    return restored


def delivery_entry_metadata(message: dict[str, Any],
                            extra: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """上游 `deliveryEntryMetadata()`：剧本条目的 metadata（含投递身份）。"""
    metadata: dict[str, Any] = {
        'visible': True,
        'interaction': message.get('interaction'),  # 上游 `?? null`
    }
    script_event = _get(message, 'script_event', 'scriptEvent')
    if script_event:
        metadata.update(script_event)
        metadata.pop('scriptEvent', None)
        if metadata.get('bubble_index') is None and metadata.get('bubbleIndex') is None:
            metadata['bubble_index'] = 0
        elif 'bubbleIndex' in metadata:
            metadata['bubble_index'] = metadata.pop('bubbleIndex')
    if extra:
        metadata.update(extra)
    return metadata
