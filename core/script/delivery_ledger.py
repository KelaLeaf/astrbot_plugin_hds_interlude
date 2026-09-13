"""M6.1 投递账本（上游 `src/script/delivery-ledger.ts` 的 Python 对应物）。

上游：Koishi / TypeScript，v1.0.1-beta6-rebuild。

语义：**只在权威剧本行的 metadata 内物化一份 outbox 形状的账本**——M6.1 刻意
只写元数据：没有第二个发送者，也没有 schema 迁移，因此不改变既有投递时序。

同一 ``commit_id`` 下的所有剧本行动（私聊气泡、群聊气泡、本地贴图、QQ 原生表情、
群消息表态）共享同一套 ``commitId`` / ``eventId`` 身份，逐段记录确认结果。

状态机（``aggregate_delivery_status``）
--------------------------------------
``pending | delivered | partial | failed | cancelled``。
* 已成功（``delivered``）的段是**终态**：更晚的记账失败绝不允许把已送达的发言
  降级回未发送。
* ``partial`` 只表示「已有部分成功」。
* ``failed`` / ``cancelled`` 混合时不得误报 ``partial``。
* 尚有 ``pending`` 且无任何成功时保持 ``pending``。

命名约定（docs/PORT_PLAN.md §2）
-------------------------------
上游 camelCase 字段一律转 snake_case（``commit_id`` / ``event_id`` /
``segment_index`` / ``attempted_at`` / ``completed_at`` …）；读取侧两种拼写都
接受（并行移植的兄弟模块或旧 JSON 可能仍是 camelCase），写出侧只写 snake_case。

时间约定
--------
``at`` 接受 timezone-aware ``datetime``（统一 UTC），也接受 ISO 字符串 / 毫秒数；
输出走 ``core/time.py`` 的 ``iso()``（与上游 ``toISOString()`` 同形：UTC、毫秒
三位、``Z`` 结尾）。
"""

from __future__ import annotations

from typing import Any, Literal, Optional, Required, TypedDict

from ..time import iso

try:  # 上游 `import { isOutgoingScriptEvent } from './contract'`
    from .contract import is_outgoing_script_event
except ImportError:  # pragma: no cover - contract.py 由并行 Agent 移植
    # 降级垫片：`contract.py` 尚未落地时，本模块退化为最小的等价判定，
    # 只覆盖「出站消息事件」这一处语义。contract.py 一旦可导入，本分支永不执行。
    def is_outgoing_script_event(event: Any) -> bool:
        """上游 `isOutgoingScriptEvent()` 的最小降级实现（仅判定用）。"""
        if not isinstance(event, dict):
            return False
        if event.get('kind') not in ('outgoing-message', 'group-message'):
            return False
        mode = _get(event, 'delivery_mode', 'deliveryMode')
        return (isinstance(event.get('content'), str)
                and isinstance(event.get('bubbles'), list)
                and mode in ('immediate', 'delayed'))


ScriptDeliveryStatus = Literal['pending', 'delivered', 'partial', 'failed', 'cancelled']
ScriptDeliverySegmentStatus = Literal['pending', 'delivered', 'failed', 'cancelled']
ScriptDeliverySegmentKind = Literal['message', 'local-media', 'native-face', 'message-reaction']

MAX_SAFE_INTEGER = 9_007_199_254_740_991  # 上游 `Number.isSafeInteger` 的边界


class ScriptDeliverySegment(TypedDict, total=False):
    """上游 `ScriptDeliverySegment`：一次投递里的一个可确认片段。"""

    index: Required[int]
    kind: Required[ScriptDeliverySegmentKind]
    content: Required[str]
    status: Required[ScriptDeliverySegmentStatus]
    attempted_at: str
    completed_at: str
    reason: str


class ScriptDeliveryAction(TypedDict, total=False):
    """上游 `ScriptDeliveryAction`：一个剧本行动（commit event）的投递账本。"""

    commit_id: Required[str]
    event_id: Required[str]
    event_kind: Required[str]
    participant_id: str
    status: Required[ScriptDeliveryStatus]
    segments: Required[list[ScriptDeliverySegment]]
    updated_at: Required[str]


class ScriptDeliveryReference(TypedDict, total=False):
    """上游 `ScriptDeliveryReference`：指向某个账号行里的确切片段。"""

    commit_id: Required[str]
    event_id: Required[str]
    script_entry_id: Required[int]
    segment_index: Required[int]


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


def _is_safe_integer(value: Any) -> bool:
    """上游 `Number.isSafeInteger()`（JS 数字是双精度，整值浮点也算）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if isinstance(value, float) and not value.is_integer():
        return False
    return abs(value) <= MAX_SAFE_INTEGER


def create_script_delivery_actions(commit: dict[str, Any]) -> list[ScriptDeliveryAction]:
    """上游 `createScriptDeliveryActions()`：由一次 commit 物化投递账本。

    没有可确认片段的行动（例如纯叙事事件）不产生账本行。
    """
    window = _get(commit, 'window', default={}) or {}
    commit_id = _get(commit, 'commit_id', 'commitId')
    actions: list[ScriptDeliveryAction] = []
    for event in _get(commit, 'events', default=[]) or []:
        segments = _delivery_segments(event)
        if not segments:
            continue
        action: dict[str, Any] = {
            'commit_id': commit_id,
            'event_id': _get(event, 'event_id', 'eventId'),
            'event_kind': _get(event, 'kind'),
        }
        participant_id = _get(event, 'participant_id', 'participantId')
        if participant_id:
            action['participant_id'] = participant_id
        action['status'] = 'pending'
        action['segments'] = segments
        action['updated_at'] = _get(window, 'to')
        actions.append(action)  # type: ignore[arg-type]
    return actions


def update_script_delivery_actions(
    current: Any,
    reference: dict[str, Any],
    status: ScriptDeliverySegmentStatus,
    at: Any,
    reason: Optional[str] = None,
) -> Optional[list[ScriptDeliveryAction]]:
    """上游 `updateScriptDeliveryActions()`：更新某一确切片段的投递结果。

    * ``current`` 不是数组时返回 ``None``。
    * 没有任何片段真正改变时返回 ``None``（调用方据此跳过写库）。
    * 已 ``delivered`` 的片段是终态；更晚的记账错误不得将其降级。
    """
    if not isinstance(current, list):
        return None
    changed = False
    timestamp = iso(at)
    ref_commit_id = _get(reference, 'commit_id', 'commitId')
    ref_event_id = _get(reference, 'event_id', 'eventId')
    ref_segment_index = _get(reference, 'segment_index', 'segmentIndex', default=0)
    actions: list[Any] = []
    for raw in current:
        if (not _is_delivery_action(raw)
                or _get(raw, 'commit_id', 'commitId') != ref_commit_id
                or _get(raw, 'event_id', 'eventId') != ref_event_id):
            actions.append(raw)
            continue
        action_changed = False
        segments: list[Any] = []
        for segment in _get(raw, 'segments', default=[]) or []:
            if not _is_record(segment) or _get(segment, 'index') != ref_segment_index:
                segments.append(segment)
                continue
            # 平台已接受的片段是终态：更晚的记账错误绝不能把已送达的发言变回未发送。
            if _get(segment, 'status') == 'delivered':
                segments.append(segment)
                continue
            if _get(segment, 'status') == status and _get(segment, 'reason') == reason:
                segments.append(segment)
                continue
            changed = True
            action_changed = True
            segments.append(_updated_segment(segment, status, timestamp, reason))
        if not action_changed:
            actions.append(raw)
            continue
        next_action = _normalize_action(raw)
        next_action['segments'] = segments
        next_action['status'] = aggregate_delivery_status(segments)
        next_action['updated_at'] = timestamp
        actions.append(next_action)
    return actions if changed else None  # type: ignore[return-value]


def delivery_reference(
    event: Optional[dict[str, Any]],
    script_entry_id: Optional[int],
    segment_index: int = 0,
) -> Optional[ScriptDeliveryReference]:
    """上游 `deliveryReference()`：出站事件 + 剧本行 → 账本定位符。"""
    if not event or not _is_safe_integer(script_entry_id):
        return None
    return {
        'commit_id': _get(event, 'commit_id', 'commitId'),
        'event_id': _get(event, 'event_id', 'eventId'),
        'script_entry_id': int(script_entry_id),  # type: ignore[arg-type]
        'segment_index': segment_index,
    }


def platform_action_reference(
    commit: Optional[dict[str, Any]],
    script_entry_id: Optional[int],
    kind: ScriptDeliverySegmentKind,
    content: Optional[str] = None,
) -> Optional[ScriptDeliveryReference]:
    """上游 `platformActionReference()`：定位平台行动（贴图/表情/表态）片段。"""
    event = None
    for item in _get(commit, 'events', default=[]) or []:
        if _get(item, 'kind') == 'platform-action':
            event = item
            break
    if not event or not _is_safe_integer(script_entry_id):
        return None
    for segment in _delivery_segments(event):
        if segment['kind'] == kind and (content is None or segment['content'] == content):
            return delivery_reference(event, script_entry_id, segment['index'])
    return None


def aggregate_delivery_status(segments: list[Any]) -> ScriptDeliveryStatus:
    """上游 `aggregateDeliveryStatus()`：由片段状态归并行动状态。"""
    if not segments or all(_get(item, 'status') == 'pending' for item in segments):
        return 'pending'
    if all(_get(item, 'status') == 'delivered' for item in segments):
        return 'delivered'
    if any(_get(item, 'status') == 'delivered' for item in segments):
        return 'partial'
    if any(_get(item, 'status') == 'pending' for item in segments):
        return 'pending'
    if any(_get(item, 'status') == 'failed' for item in segments):
        return 'failed'
    return 'cancelled'


def _delivery_segments(event: dict[str, Any]) -> list[ScriptDeliverySegment]:
    """上游 `deliverySegments()`：把一个事件拆成可确认片段。"""
    if is_outgoing_script_event(event):
        return [
            {'index': index, 'kind': 'message', 'content': content, 'status': 'pending'}
            for index, content in enumerate(_get(event, 'bubbles', default=[]) or [])
        ]
    if _get(event, 'kind') != 'platform-action':
        return []
    metadata = _get(event, 'metadata')
    if not _is_record(metadata):
        return []
    segments: list[ScriptDeliverySegment] = []
    local_media = _get(metadata, 'local_media', 'localMedia')
    if _is_record(local_media) and isinstance(_get(local_media, 'asset_id', 'assetId'), str):
        segments.append({'index': len(segments), 'kind': 'local-media',
                         'content': _get(local_media, 'asset_id', 'assetId'), 'status': 'pending'})
    native_face = _get(metadata, 'native_face', 'nativeFace')
    if _is_record(native_face) and isinstance(_get(native_face, 'semantic'), str):
        segments.append({'index': len(segments), 'kind': 'native-face',
                         'content': _get(native_face, 'semantic'), 'status': 'pending'})
    reactions = _get(metadata, 'message_reactions', 'messageReactions')
    if not isinstance(reactions, list):
        reactions = []
    for reaction in reactions:
        if not _is_record(reaction):
            continue
        message_ref = _get(reaction, 'message_ref', 'messageRef')
        reaction_name = _get(reaction, 'reaction')
        if not isinstance(message_ref, str) or not isinstance(reaction_name, str):
            continue
        segments.append({'index': len(segments), 'kind': 'message-reaction',
                         'content': f'{message_ref}:{reaction_name}', 'status': 'pending'})
    return segments


def _updated_segment(
    segment: dict[str, Any],
    status: ScriptDeliverySegmentStatus,
    timestamp: Optional[str],
    reason: Optional[str],
) -> dict[str, Any]:
    """上游 `updateScriptDeliveryActions()` 里的片段更新分支。"""
    next_segment = _normalize_segment(segment)
    next_segment['status'] = status
    attempted_at = _get(segment, 'attempted_at', 'attemptedAt')
    next_segment['attempted_at'] = timestamp if attempted_at is None else attempted_at
    # 上游：pending 时不写 completedAt，但也不删除既有的值（保持原样）。
    if status != 'pending':
        next_segment['completed_at'] = timestamp
    if reason:
        next_segment['reason'] = reason
    else:
        next_segment.pop('reason', None)
    return next_segment


def _normalize_segment(segment: dict[str, Any]) -> dict[str, Any]:
    """把片段里已知的 camelCase 键统一为 snake_case（未知键原样保留）。"""
    normalized = dict(segment)
    for camel, snake in (('attemptedAt', 'attempted_at'), ('completedAt', 'completed_at')):
        if camel in normalized:
            normalized.setdefault(snake, normalized.pop(camel))
    return normalized


def _normalize_action(action: dict[str, Any]) -> dict[str, Any]:
    """把账本行里已知的 camelCase 键统一为 snake_case（未知键原样保留）。"""
    normalized = dict(action)
    for camel, snake in (('commitId', 'commit_id'), ('eventId', 'event_id'),
                         ('eventKind', 'event_kind'), ('participantId', 'participant_id'),
                         ('updatedAt', 'updated_at')):
        if camel in normalized:
            normalized.setdefault(snake, normalized.pop(camel))
    segments = normalized.get('segments')
    if isinstance(segments, list):
        normalized['segments'] = [
            _normalize_segment(item) if _is_record(item) else item for item in segments
        ]
    return normalized


def _is_delivery_action(value: Any) -> bool:
    """上游 `isDeliveryAction()`（未知 / 未来形状的账本行按原样保留）。"""
    if (not _is_record(value)
            or not isinstance(_get(value, 'commit_id', 'commitId'), str)
            or not isinstance(_get(value, 'event_id', 'eventId'), str)
            or not isinstance(_get(value, 'segments'), list)):
        return False
    return all(
        _is_record(segment)
        and _is_safe_integer(_get(segment, 'index'))
        and isinstance(_get(segment, 'kind'), str)
        and isinstance(_get(segment, 'content'), str)
        and isinstance(_get(segment, 'status'), str)
        for segment in _get(value, 'segments')
    )
