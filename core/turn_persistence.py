"""回合持久化（上游 `src/turn-persistence.ts` 的 Python 对应物）。

上游：Koishi / TypeScript，v1.0.1-beta6-rebuild。

把一次已校验的 ``ScriptCommitDraft`` 变成一个 ``ScriptEntryDraft``：正文永远等于
commit 的 prose，事件、场景增量、投递账本（M6.1）与可选的时间线计划都进 metadata。
**M6.1 只增加元数据**：不新增第二个发送者，也不改变既有投递时序。

命名约定（键名约定）：camelCase 字段转 snake_case；读取侧两种拼写都
接受（兄弟模块或旧 JSON 可能仍是 camelCase），写出侧只写 snake_case。
"""

from __future__ import annotations

from typing import Any, Optional

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
    metadata: dict[str, Any] = {
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
