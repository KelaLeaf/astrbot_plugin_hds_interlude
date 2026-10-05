"""回合持久化（上游 `src/turn-persistence.ts` 的 Python 对应物）。

上游：Koishi / TypeScript，v1.0.1-beta6-rebuild。

把一次已校验的 ``ScriptCommitDraft`` 变成一个 ``ScriptEntryDraft``：正文永远等于
commit 的 prose，事件、场景增量、投递账本（M6.1）与可选的时间线计划都进 metadata。
**M6.1 只增加元数据**：不新增第二个发送者，也不改变既有投递时序。

``metadata.conversation_kind``（上游 ``conversationKind``，``turn-persistence.ts:14-18``）
是长线叙事评分的会话类型来源：群聊提交写 ``group``、私聊提交写 ``private``、跨两种会话
的提交写 ``unknown``；**拿不到就整个键不写**（缺省不是私聊，见 `_conversation_kind()`）。

命名约定（键名约定）：camelCase 字段转 snake_case；读取侧两种拼写都
接受（兄弟模块或旧 JSON 可能仍是 camelCase），写出侧只写 snake_case。
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from .script.delivery_ledger import create_script_delivery_actions

try:  # 上游 `import { normalizeLifeHandoff } from './script/life-handoff'`
    from .script.life_handoff import normalize_life_handoff
except ImportError:  # pragma: no cover - life_handoff.py 由并行 Agent 移植
    # 降级垫片：`life_handoff.py` 尚未落地时不做生活交接归一化（上游在缺少交接
    # 时同样返回 undefined）。life_handoff.py 一旦可导入，本分支永不执行。
    def normalize_life_handoff(raw: Any, prose: str) -> Optional[dict[str, Any]]:
        """上游 `normalizeLifeHandoff()` 的降级实现：不产出交接。"""
        return None


def _get(mapping: Any, *keys: str, default: Any = None) -> Any:
    """按顺序读取第一个存在的键（兼容 snake_case / camelCase 两种拼写）。"""
    if isinstance(mapping, dict):
        for key in keys:
            if key in mapping:
                return mapping[key]
    return default


def _conversation_kind(events: Any) -> Optional[str]:
    """上游 `scriptEntryDraftForCommit()` 的会话类型判定（`turn-persistence.ts:14-18`）。

    会话类型**只由这份提交自己的投递事件**决定：有群消息事件 = 群聊回合，有出站私聊
    事件 = 私聊回合；两种都有（一次提交跨了两种会话）时上游写 `'unknown'`，让长线叙事
    按"拿不准"保守处理；两种都没有时返回 `None` —— 调用方**不写这个键**，绝不缺省当成
    私聊，否则一个拿不到会话类型的群回合会被 `core/long_arc.py` 按私聊全额（1.0）计分。

    值域与读侧逐字对齐（`long_arc.resolve_conversation_kind()`：`private` / `group` /
    `unknown`），落库键名按本仓约定用 snake_case（`conversation_kind`）。
    """
    has_group = any(
        isinstance(event, Mapping) and event.get('kind') == 'group-message' for event in events
    )
    has_private = any(
        isinstance(event, Mapping) and event.get('kind') == 'outgoing-message' for event in events
    )
    if has_group and has_private:
        return 'unknown'
    if has_group:
        return 'group'
    if has_private:
        return 'private'
    return None


def script_entry_draft_for_commit(
    commit: dict[str, Any],
    interaction: Optional[dict[str, Any]],
    timeline_plan: Optional[dict[str, Any]] = None,
    life_handoff: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """上游 `scriptEntryDraftForCommit()`：commit → 剧本条目草稿。

    ``timeline_plan`` 用 ``is not None`` 判定（上游是 ``undefined`` 判定；Python
    里空 dict 为假值，不能直接照抄真值判定）。
    """
    prose = commit.get('prose')
    window = _get(commit, 'window', default={}) or {}
    scene_delta = _get(commit, 'scene_delta', 'sceneDelta', default={}) or {}
    events = _get(commit, 'events', default=[]) or []
    conversation_kind = _conversation_kind(events)
    metadata: dict[str, Any] = {
        # 上游把 `conversationKind` 放在 metadata 首位，且**只在拿得到时**展开这个键
        # （`turn-persistence.ts:28`：`...(conversationKind ? { conversationKind } : {})`）。
        **(({'conversation_kind': conversation_kind}) if conversation_kind else {}),
        'phase': _get(commit, 'phase'),
        'narrative_authority': 'original-v2',
        'life_handoff': normalize_life_handoff(life_handoff, prose),
        'commit_id': _get(commit, 'commit_id', 'commitId'),
        'frame_id': _get(scene_delta, 'frame_id', 'frameId'),
        'burst_id': _get(scene_delta, 'burst_id', 'burstId'),
        'interaction': interaction,
        'script_commit': {
            'commit_id': _get(commit, 'commit_id', 'commitId'),
            'source_format': _get(commit, 'source_format', 'sourceFormat'),
            'from': _get(window, 'from'),
            'to': _get(window, 'to'),
            'event_count': len(events),
        },
        # 上游同名条目原样存 `scriptEvents: commit.events`：显式端点（M4）就藏在这份
        # 事件列表里（`script_events[i].endpoint_id`，只在确实选了端点的事件上出现），
        # 本模块**刻意不另立顶层端点字段**——一次提交里可以有多条发往不同端点的发言，
        # 顶层标量会撒谎。落库字段名与值逐字来自 commit，不做改写。
        'script_events': events,
        'delivery_actions': create_script_delivery_actions(commit),
        'scene_delta': scene_delta,
    }
    if timeline_plan is not None:
        metadata['timeline_plan'] = timeline_plan
        metadata['timeline_window'] = {'from': _get(window, 'from'), 'to': _get(window, 'to')}
    return {
        'kind': 'script',
        'actor': 'narrator',
        'content': prose,
        'occurred_at': _get(window, 'to'),
        'metadata': metadata,
    }
