"""剧本提交校验（上游 `src/script/validator.ts` 的 Python 对应物）。

上游：Koishi / TypeScript，v1.0.1-beta6-rebuild。

**只做结构校验**：文学风格永远不会成为拒绝规则。校验失败只意味着这次 commit
在结构上自相矛盾（空正文、身份缺失、事件顺序不一致、消息无法重建、因果悬空），
而不是「写得不好」。

命名约定（docs/PORT_PLAN.md §2）：camelCase 字段转 snake_case；读取侧两种拼写都
接受（兄弟模块或旧 JSON 可能仍是 camelCase）。错误文案逐字照抄上游（英文原文），
因为它们是可被断言与日志检索的契约。
"""

from __future__ import annotations

from typing import Any, Optional, Required, TypedDict

from ..time import parse_dt

try:  # 上游 `import { isOutgoingScriptEvent } from './contract'`
    from .contract import is_outgoing_script_event
except ImportError:  # pragma: no cover - contract.py 由并行 Agent 移植
    # 降级垫片：`contract.py` 尚未落地时，本模块退化为最小的等价判定。
    # contract.py 一旦可导入，本分支永不执行。
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


class ScriptCommitValidation(TypedDict, total=False):
    """上游 `ScriptCommitValidation`。"""

    valid: Required[bool]
    errors: Required[list[str]]


def _get(mapping: Any, *keys: str, default: Any = None) -> Any:
    """按顺序读取第一个存在的键（兼容 snake_case / camelCase 两种拼写）。"""
    if isinstance(mapping, dict):
        for key in keys:
            if key in mapping:
                return mapping[key]
    return default


def validate_script_commit(commit: dict[str, Any], separator: str = '<sep/>') -> ScriptCommitValidation:
    """上游 `validateScriptCommit()`：结构性校验一次已提交剧本。"""
    errors: list[str] = []
    prose = _get(commit, 'prose')
    if not isinstance(prose, str):
        prose = ''
    if not prose.strip():
        errors.append('script prose is empty')
    scene_delta = _get(commit, 'scene_delta', 'sceneDelta', default={}) or {}
    events = _get(commit, 'events', default=[]) or []
    if not _get(scene_delta, 'frame_id', 'frameId') or not _get(scene_delta, 'burst_id', 'burstId'):
        errors.append('scene delta identity is missing')
    if _get(scene_delta, 'prose_append', 'proseAppend') != prose:
        errors.append('scene delta prose differs from commit prose')
    delta_event_ids = _get(scene_delta, 'event_ids', 'eventIds', default=[]) or []
    if '\u001f'.join(str(item) for item in delta_event_ids) != \
            '\u001f'.join(str(_get(event, 'event_id', 'eventId')) for event in events):
        errors.append('scene delta event order differs from commit events')
    window = _get(commit, 'window', default={}) or {}
    from_at = parse_dt(_get(window, 'from'))
    to_at = parse_dt(_get(window, 'to'))
    if from_at is None or to_at is None or from_at > to_at:
        errors.append('commit window is invalid')
    commit_id = _get(commit, 'commit_id', 'commitId')
    ids: set[str] = set()
    for event in events:
        event_id = _get(event, 'event_id', 'eventId')
        if _get(event, 'commit_id', 'commitId') != commit_id:
            errors.append(f'event {event_id} belongs to another commit')
        if not event_id or event_id in ids:
            errors.append(f'event id is missing or duplicated: {event_id}')
        ids.add(event_id)
        if is_outgoing_script_event(event):
            _validate_message_event(event, event_id, separator, errors)
    for event in events:
        event_id = _get(event, 'event_id', 'eventId')
        for parent in _get(event, 'caused_by_event_ids', 'causedByEventIds', default=[]) or []:
            if parent not in ids:
                errors.append(f'event {event_id} has unknown cause {parent}')
    return {'valid': len(errors) == 0, 'errors': errors}


def _validate_message_event(event: dict[str, Any], event_id: Any, separator: str,
                            errors: list[str]) -> None:
    """上游出站消息事件分支。"""
    if not _get(event, 'participant_id', 'participantId') and _get(event, 'kind') != 'group-message':
        errors.append(f'message event {event_id} has no participant')
    bubbles: Optional[list[Any]] = _get(event, 'bubbles')
    if not isinstance(bubbles, list):
        bubbles = []
    if not bubbles or any(not isinstance(item, str) or not item.strip() for item in bubbles):
        errors.append(f'message event {event_id} has empty bubbles')
    if separator.join(str(item) for item in bubbles) != _get(event, 'content'):
        errors.append(f'message event {event_id} cannot reconstruct its content')
