"""Chunk8：`upstream/src/service.ts:6083-6713` 的逐条移植（压缩与记忆整理核心）。

成员顺序与上游**逐字对齐**（起始行落在 `[6083, 6713)` 内）：

| 上游行 | 成员 | 本文件 |
| --- | --- | --- |
| 6083 | `requestSchedulePreplan` | `request_schedule_preplan` |
| 6092 | `persistSchedulePreplanReview` | `persist_schedule_preplan_review` |
| 6131 | `scheduleStreamScriptRecovery` | `schedule_stream_script_recovery` |
| 6148 | `persistStreamScriptRecovery` | `persist_stream_script_recovery` |
| 6163 | `compactUnlocked` | `compact_unlocked` |
| 6199 | `prepareCompaction` | `prepare_compaction` |
| 6263 | `applyCompaction` | `apply_compaction` |
| 6273 | `compactOverlayUnlocked` | `compact_overlay_unlocked` |
| 6329 | `overlaySnapshotsForPrompt` | `overlay_snapshots_for_prompt` |
| 6348 | `rebuildLiveOverlayState` | `rebuild_live_overlay_state` |
| 6394 | `persistCompaction` | `persist_compaction` |
| 6535 | `persistFact` | `persist_fact` |
| 6582 | `embedText` | `embed_text` |
| 6592 | `scheduleFactEmbeddingBackfill` | `schedule_fact_embedding_backfill` |
| 6605 | `backfillFactEmbeddings` | `backfill_fact_embeddings` |
| 6617 | `persistStatePatch` | `persist_state_patch` |

边界说明
--------
* `getSchedulePreplan` / `schedulePreplanEvidence` / `saveSchedulePreplan` /
  `prepareSchedulePreplanReview`（6003–6081）属于 **chunk7**，本文件只 `self.…()` 调用。
* `developmentForPrompt`（6714）起属于 **chunk9**。
* `resolveCompactionFacts` / `markContinuityDirty`（6792 / 6804）属于 **chunk9**，
  `compactionFingerprint` / `noteCompactionFailure` / `compactionIsBackedOff` /
  `compactionCheckpointAdvanced`（5866–5896）属于 **chunk7**；一律 `self.…()`。

键名法（`docs/PORT_PLAN.md` §2）
-------------------------------
* **数据库行**（`db_get`/`db_create`/`db_set` 的列名）：保持上游 camelCase ——
  列名是持久化 wire format（`plugin/core/database.py` 的 `TABLES`）。
* **内部结构**（`PreparedCompactionRun`、`compact_request`、日程记录、story state）：
  snake_case（`plugin/core/service/config.py` / `core/story_state.py` / `types.py`）。
* **跨 mixin 的字典**（`review` / `context`）用 `pick` 双读，容忍任一拼写。
* **送进 `core/script/*` 的领域对象**：那些模块按 `types.py` 的 snake_case 口径读
  （`participant_id` / `source_entry_ids` / `metadata.scene_checkpoint` / `frame_id`），
  而库行是 camelCase。本文件用 `_dual_entry` / `_dual_draft` / `_dual_patch` /
  `_dual_knowledge` 在**边界处**补齐 snake_case 别名（值一字不改），
  camelCase 原键保留 —— 两边都能读，不产生第二份真相。

数据库访问
----------
统一走 `base.py` 的 `db_get` / `db_create` / `db_set`（上游 `dbGet` / `dbCreate` /
`dbSet` 的等价物，内部已经是 `self.db.all/insert/update` + `await self.write(fn)`，
并带瞬时错误重试与行归一化）。`plugin/core/database.py` 的 `all()` 只支持等值
`where`，上游的 `$gt` / `$gte` / `$lte` / `$in` 查询由本文件的 `_scan_entries` /
`_entries_by_ids` **先按上游形状问库、遇到 `NotImplementedError` 再降级为取回后
在 Python 侧过滤**（与 `docs/PORT_PLAN_SERVICE.md` §2 的约定一致）。
"""

from __future__ import annotations

import asyncio
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from ..script.continuity_checkpoint import assert_continuity_review, compaction_prefix
from ..script.development import (
    development_dimension,
    development_scenes,
    prompt_ready_development,
    reviewed_development_support,
)
from ..script.episode_index import grounded_episode_tags
from ..script.knowledge_evidence import normalize_knowledge_evidence, supports_recorded_outcome
from ..schedule_preplan import apply_schedule_preplan_proposal
from ..story_state import decode_story_state, encode_story_state
from ..time import calendar_day_key, dt_ms, iso, parse_dt
from .base import ServiceBase, _config_section, pick
from .config import SCHEDULE_PREPLAN_RETRY_BACKOFF
from .helpers import (
    clamp_number,
    clip,
    limit_entries_by_characters,
    merge_note,
    normalize_fact,
    normalize_major_events,
    normalize_participant_state,
    normalize_scene_presence_drafts,
    patch_claims_match,
    resolve_participant_id,
)

try:  # pragma: no cover - helpers.py 由并行任务产出，可能还没有这几个函数
    from . import helpers as _helpers_module
except ImportError:  # pragma: no cover
    _helpers_module = None  # type: ignore[assignment]

__all__ = [
    'ServiceChunk8',
    'explicit_evidence_ids',
    'group_overlay_patches',
    'group_overlay_snapshots',
    'has_compaction_evidence',
    'start_of_utc_window',
    'state_patch_evidence',
]

#: 上游 `Time.day` / `Time.hour`（`src/time.ts`）。
_DAY_MS = 86_400_000
_HOUR_MS = 3_600_000

#: 上游 `memoryConfig` getter（`src/service.ts:5758-5798`）的默认值。
#: base.py 的 `memory_config` 只回原始配置段（`plugin/core/config.py` 尚未落地时），
#: 所以这里逐字保留上游默认值表；chunk7 若提供了带默认值的 getter，本表自动让位。
_MEMORY_DEFAULTS: dict[str, Any] = {
    'enabled': True,
    'backgroundIntervalMinutes': 10,
    'sceneEntryThreshold': 16,
    'sceneCharacterThreshold': 10_000,
    'compactionEntryLimit': 80,
    'compactionCharacterLimit': 32_000,
    'sceneHookCharacters': 2_000,
    'sceneSummaryCharacters': 8_000,
    'arcSummaryCharacters': 12_000,
    'previousSceneSummaries': 2,
    'factContentCharacters': 4_000,
    'statePatchConfidenceThreshold': 0.82,
    'majorStatePatchConfidenceThreshold': 0.95,
    'statePatchMinEvidence': 3,
    'statePatchMinTurns': 3,
    'statePatchMinDays': 2,
    'statePatchCooldownHours': 72,
    'autoApplyStatePatches': True,
    'allowMajorStateChanges': True,
    'maxFactsPerStory': 200,
    'overlayCompressionEnabled': True,
    'overlayRecentDays': 2,
    'overlayMonthlyAfterDays': 10,
    'overlayWeeklyWindowDays': 5,
    'overlayMonthlyWindowDays': 10,
    'overlayWeeklySummaryCharacters': 1_600,
    'overlayMonthlySummaryCharacters': 2_400,
}


# =========================================================================== #
# 键名法：跨边界补齐 snake_case 别名（值不动，camelCase 原键保留）
# =========================================================================== #

_ENTRY_ALIASES = {
    'storyId': 'story_id',
    'participantId': 'participant_id',
    'occurredAt': 'occurred_at',
    'createdAt': 'created_at',
}
#: 上游剧本条目的 `metadata` 子对象（chunk4/chunk5 写库时是 camelCase）。
_METADATA_ALIASES = {
    'sceneCheckpoint': 'scene_checkpoint',
    'frameId': 'frame_id',
    'episodeTags': 'episode_tags',
    'timelinePlan': 'timeline_plan',
    'lifeHandoff': 'life_handoff',
    'narrativeAuthority': 'narrative_authority',
}
_PATCH_ALIASES = {
    'storyId': 'story_id',
    'participantId': 'participant_id',
    'proposedValue': 'proposed_value',
    'sourceEntryIds': 'source_entry_ids',
    'createdAt': 'created_at',
    'appliedAt': 'applied_at',
}
_DRAFT_ALIASES = {
    'participantId': 'participant_id',
    'proposedValue': 'proposed_value',
    'sourceEntryIds': 'source_entry_ids',
    'contradictsProposalIds': 'contradicts_proposal_ids',
    'interactionReview': 'interaction_review',
    'resolvesFactIds': 'resolves_fact_ids',
}
_INTERACTION_REVIEW_ALIASES = {
    'feedbackEntryIds': 'feedback_entry_ids',
    'responseEntryIds': 'response_entry_ids',
}
_KNOWLEDGE_ALIASES = {'relatedFactIds': 'related_fact_ids'}
_CLAUSE_ALIASES = {'sourceEntryId': 'source_entry_id'}


def _with_snake(value: Any, aliases: dict[str, str]) -> Any:
    """给 dict 补 snake_case 别名；已有 snake 键时不覆盖（不产生歧义）。"""
    if not isinstance(value, dict):
        return value
    result = dict(value)
    for camel, snake in aliases.items():
        if camel in value and snake not in value:
            result[snake] = value[camel]
    return result


def _dual_entry(entry: Any) -> Any:
    """剧本条目 → 同时带 camelCase / snake_case 键的副本（值一字不改）。"""
    row = _with_snake(entry, _ENTRY_ALIASES)
    if isinstance(row, dict):
        metadata = row.get('metadata')
        if isinstance(metadata, dict):
            row['metadata'] = _with_snake(metadata, _METADATA_ALIASES)
    return row


def _dual_entries(entries: Any) -> list[dict[str, Any]]:
    return [_dual_entry(entry) for entry in (entries or []) if isinstance(entry, dict)]


def _dual_draft(draft: Any) -> Any:
    """模型草稿 → 补 snake 别名（`core/script/development.py` 按 snake 读）。"""
    result = _with_snake(draft, _DRAFT_ALIASES)
    if isinstance(result, dict):
        review = result.get('interaction_review')
        if isinstance(review, dict):
            result['interaction_review'] = _with_snake(review, _INTERACTION_REVIEW_ALIASES)
    return result


def _dual_patch(patch: Any) -> Any:
    return _with_snake(patch, _PATCH_ALIASES)


def _dual_knowledge(value: Any) -> Any:
    """知识证据 → 补 snake 别名（`normalize_knowledge_evidence` 按 snake 读 clause）。"""
    result = _with_snake(value, _KNOWLEDGE_ALIASES)
    if isinstance(result, dict):
        clauses = result.get('clauses')
        if isinstance(clauses, list):
            result['clauses'] = [
                _with_snake(clause, _CLAUSE_ALIASES) if isinstance(clause, dict) else clause
                for clause in clauses
            ]
    return result


# =========================================================================== #
# 小工具（JS 语义等价物）
# =========================================================================== #

def _is_int(value: Any) -> bool:
    """JS `Number.isSafeInteger` 的宽松版（id 比较用）。"""
    return isinstance(value, int) and not isinstance(value, bool)


def _nullish(*values: Any) -> Any:
    """JS `a ?? b`：第一个非 None 的值。"""
    for value in values:
        if value is not None:
            return value
    return None


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _unique(values: Any) -> list[Any]:
    """`Array.from(new Set(values))`：去重保序。"""
    result: list[Any] = []
    for value in _as_list(values):
        if value not in result:
            result.append(value)
    return result


def _js_max(values: Any, fallback: float = float('-inf')) -> float:
    """`Math.max(...values)`：空列表返回 `-Infinity`（不是抛异常）。"""
    items = _as_list(values)
    return max(items) if items else fallback


def _datetime(value: Any) -> Optional[datetime]:
    return parse_dt(value)


def _lte(left: Any, right: Any) -> bool:
    """时间比较；任一侧解析不了按 JS 的 `NaN` 语义返回 False。"""
    a = parse_dt(left)
    b = parse_dt(right)
    if a is None or b is None:
        return False
    return a <= b


def _where_dt(value: Any) -> Any:
    """等值 where 里比较时间列时用 ISO 字符串（库里存的就是 `iso()` 形状）。"""
    parsed = parse_dt(value)
    return iso(parsed) if parsed is not None else value


def _config_int(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return int(value)


def _prefer_helper(name: str, fallback: Callable[..., Any]) -> Callable[..., Any]:
    """`helpers.py` 里有同名移植版就用它，否则用本文件的等价实现。

    与 `base.py` 的 `_prefer_helper` 同一模式：helpers 由并行任务产出，
    这几个函数属于它的行范围（7084–8524），先到先得，避免两份漂移的实现。
    """
    if _helpers_module is not None:
        candidate = getattr(_helpers_module, name, None)
        if callable(candidate):
            return candidate
    return fallback


def start_of_utc_window(value: Any, window_days: float) -> datetime:
    """上游 `startOfUtcWindow(value, windowDays)`（`src/service.ts:8487`）。

    `Math.floor(value.getTime() / Time.day)` 的 JS 语义：浮点除法后向下取整
    （负数也向下），本移植版用 `math.floor` 而非 `//`，逐位对齐。
    """
    size = max(1, math.floor(window_days))
    moment = parse_dt(value) or datetime(1970, 1, 1, tzinfo=timezone.utc)
    epoch_day = math.floor(dt_ms(moment) / _DAY_MS)
    return datetime.fromtimestamp(math.floor(epoch_day / size) * size * 86_400, tz=timezone.utc)


def group_overlay_patches(patches: list[dict[str, Any]], window_days: int = 5) -> list[dict[str, Any]]:
    """上游 `groupOverlayPatches(patches, windowDays = 5)`（`src/service.ts:8493`）。"""
    groups: dict[str, dict[str, Any]] = {}
    for patch in patches:
        participant_id = patch.get('participantId') or ''
        target = patch.get('target')
        start = start_of_utc_window(_nullish(patch.get('appliedAt'), patch.get('createdAt')), window_days)
        key = '%s|%s|%s' % (participant_id, target, iso(start))
        group = groups.get(key)
        if group is None:
            group = {
                'participant_id': participant_id, 'target': target, 'from': start,
                'to': datetime.fromtimestamp(dt_ms(start) / 1000 + window_days * 86_400, tz=timezone.utc),
                'patches': [],
            }
            groups[key] = group
        group['patches'].append(patch)
    return list(groups.values())


def group_overlay_snapshots(snapshots: list[dict[str, Any]], window_days: int = 10) -> list[dict[str, Any]]:
    """上游 `groupOverlaySnapshots(snapshots, windowDays = 10)`（`src/service.ts:8505`）。"""
    groups: dict[str, dict[str, Any]] = {}
    for snapshot in snapshots:
        participant_id = snapshot.get('participantId') or ''
        target = snapshot.get('target')
        start = start_of_utc_window(snapshot.get('periodEnd'), window_days)
        key = '%s|%s|%s' % (participant_id, target, iso(start))
        group = groups.get(key)
        if group is None:
            group = {
                'participant_id': participant_id, 'target': target, 'from': start,
                'to': datetime.fromtimestamp(dt_ms(start) / 1000 + window_days * 86_400, tz=timezone.utc),
                'snapshots': [],
            }
            groups[key] = group
        group['snapshots'].append(snapshot)
    return list(groups.values())


def has_compaction_evidence(source_entry_ids: Any, entries: list[dict[str, Any]]) -> bool:
    """上游 `hasCompactionEvidence(sourceEntryIds, entries)`（`src/service.ts:8039`）。"""
    if not isinstance(source_entry_ids, list) or not source_entry_ids:
        return False
    ids = {entry.get('id') for entry in entries if isinstance(entry, dict)}
    return any(item in ids for item in source_entry_ids)


def explicit_evidence_ids(source_entry_ids: Any, entries: list[dict[str, Any]]) -> list[int]:
    """把 `sourceEntryIds` 收敛成"确实出现在本批条目里"的 id（上游各处 inline 写法）。"""
    return [
        item for item in _as_list(source_entry_ids)
        if _is_int(item) and any(entry.get('id') == item for entry in entries)
    ]


def state_patch_evidence(entries: list[dict[str, Any]], timezone_name: str) -> dict[str, int]:
    """上游 `statePatchEvidence(entries, timezone)`（`src/service.ts:8478`）。"""
    narrative = [
        entry for entry in entries
        if entry.get('kind') == 'script' or entry.get('actor') == 'narrator'
    ]
    turns: list[int] = []
    days: list[str] = []
    for entry in narrative:
        occurred = parse_dt(entry.get('occurredAt'))
        if occurred is None:
            continue
        turns.append(dt_ms(occurred))
        days.append(calendar_day_key(occurred, timezone_name))
    return {
        'turns': len(set(turns)),
        'days': len(set(days)),
        'scenes': development_scenes(_dual_entries(narrative)),
    }


#: 这些 module-level 纯函数属于 helpers.py 的行范围（7084–8524）：优先用它的移植版。
group_overlay_patches = _prefer_helper('group_overlay_patches', group_overlay_patches)
group_overlay_snapshots = _prefer_helper('group_overlay_snapshots', group_overlay_snapshots)
has_compaction_evidence = _prefer_helper('has_compaction_evidence', has_compaction_evidence)
state_patch_evidence = _prefer_helper('state_patch_evidence', state_patch_evidence)


# =========================================================================== #
# ServiceChunk8：src/service.ts:6083-6713
# =========================================================================== #

class ServiceChunk8(ServiceBase):
    """Chunk8 mixin：压缩调度与执行、场景/弧落库、事实与状态补丁审核。

    逐条对应 `src/service.ts:6083-6713`，成员顺序与上游一致。
    """

    # ------------------------------------------------------------------ #
    # 配置读取（上游 `memoryConfig` getter 的默认值展开）
    # ------------------------------------------------------------------ #

    def _memory_section(self) -> dict[str, Any]:
        """当前生效的 memory 配置段（dict）。"""
        try:
            raw: Any = self.memory_config
        except Exception:  # pragma: no cover - 缺 config 的最小测试替身
            raw = None
        if isinstance(raw, dict):
            return raw
        try:
            section = _config_section(getattr(self, 'config', None), 'memory')
        except Exception:  # pragma: no cover
            return {}
        return section if isinstance(section, dict) else {}

    def _memory_raw(self, camel: str, snake: Optional[str] = None) -> Any:
        """只读配置段（无默认值）——用于 `?? 另一个字段` 这类上游语义。"""
        return pick(self._memory_section(), camel, snake)

    def _memory(self, camel: str, snake: Optional[str] = None, default: Any = None) -> Any:
        """读 memory 配置项：配置缺省时回落上游默认值（`:5758-5798`）。"""
        value = self._memory_raw(camel, snake)
        if value is None:
            return _MEMORY_DEFAULTS.get(camel, default)
        return value

    def _memory_int(self, camel: str, snake: Optional[str] = None, default: int = 0) -> int:
        return _config_int(self._memory(camel, snake), default)

    def _memory_bool(self, camel: str, snake: Optional[str] = None) -> bool:
        return bool(self._memory(camel, snake))

    def _share_participant_details(self) -> bool:
        """上游 `this.sharedStoryConfig.shareParticipantDetails`。"""
        try:
            shared: Any = self.shared_story_config
        except Exception:  # pragma: no cover
            shared = None
        if not isinstance(shared, dict):
            shared = _config_section(getattr(self, 'config', None), 'sharedStory')
        return pick(shared or {}, 'shareParticipantDetails', 'share_participant_details') is True

    def _schedule_preplan_settings(self) -> dict[str, Any]:
        """上游 `get schedulePreplanConfig()`：`resolveSchedulePreplanConfig(...)`。

        `base.py` 的 `schedule_preplan_config` 回的是**原始配置段**；上游 getter 会把
        缺省字段展开成默认值。`resolve_schedule_preplan_config()` 幂等，这里补上这一步，
        保证 `apply_schedule_preplan_proposal` 拿到的是完整配置（缺 `horizonDays`
        时它会算出 `NaN` 时间范围）。
        """
        try:
            raw: Any = self.schedule_preplan_config
        except Exception:  # pragma: no cover
            raw = None
        if not isinstance(raw, dict):
            raw = _config_section(getattr(self, 'config', None), 'schedulePreplan')
        try:
            from ..schedule_preplan import resolve_schedule_preplan_config
        except ImportError:  # pragma: no cover
            return raw
        try:
            return resolve_schedule_preplan_config(raw)
        except Exception:  # pragma: no cover
            return raw

    # ------------------------------------------------------------------ #
    # 上游 $gt / $gte / $lte / $in 查询的等价读取
    # ------------------------------------------------------------------ #

    async def _scan_entries(
        self,
        story_id: str,
        where: dict[str, Any],
        options: Optional[dict[str, Any]],
        predicate: Callable[[dict[str, Any]], bool],
        descending: bool = False,
    ) -> list[dict[str, Any]]:
        """按上游的查询形状读剧本条目；本移植版不支持算子时降级为 Python 侧过滤。"""
        try:
            return await self.db_get('interlude_script_entry', where, options)
        except NotImplementedError:
            pass
        rows = await self.db_get(
            'interlude_script_entry', {'storyId': story_id}, {'sort': {'id': 'ASC'}},
        )
        selected = [row for row in rows if predicate(row)]
        limit = pick(options or {}, 'limit')
        if isinstance(limit, int) and not isinstance(limit, bool) and limit >= 0:
            if descending:
                selected = selected[len(selected) - limit:] if limit else []
            else:
                selected = selected[:limit]
        if descending:
            selected = list(reversed(selected))
        return selected

    async def _entries_by_ids(self, story_id: str, ids: Any) -> list[dict[str, Any]]:
        """上游 `{ id: { $in: ids } }` 查询的等价物。"""
        unique = _unique([item for item in _as_list(ids) if _is_int(item)])
        if not unique:
            return []
        try:
            return await self.db_get(
                'interlude_script_entry', {'storyId': story_id, 'id': {'$in': unique}},
            )
        except NotImplementedError:
            pass
        rows = await self.db_get(
            'interlude_script_entry', {'storyId': story_id}, {'sort': {'id': 'ASC'}},
        )
        wanted = set(unique)
        return [row for row in rows if row.get('id') in wanted]

    async def _scan_state_patches(
        self, story_id: str, statuses: list[str], options: Optional[dict[str, Any]] = None,
    ) -> list[dict[str, Any]]:
        """上游 `{ status: { $in: [...] } }` 查询的等价物（按 createdAt DESC 截断）。"""
        try:
            return await self.db_get(
                'interlude_state_patch',
                {'storyId': story_id, 'status': {'$in': list(statuses)}}, options,
            )
        except NotImplementedError:
            pass
        rows = await self.db_get('interlude_state_patch', {'storyId': story_id})
        selected = [row for row in rows if row.get('status') in statuses]
        selected.sort(
            key=lambda row: dt_ms(parse_dt(row.get('createdAt')) or moment_zero()),
            reverse=True,
        )
        limit = pick(options or {}, 'limit')
        if isinstance(limit, int) and not isinstance(limit, bool) and limit >= 0:
            selected = selected[:limit]
        return selected

    # ------------------------------------------------------------------ #
    # 日程预排复核落库（上游 6083–6127）
    # ------------------------------------------------------------------ #

    async def request_schedule_preplan(self, story: Any, request: Any) -> Any:
        """上游 `requestSchedulePreplan(story, request)`（`src/service.ts:6083`）。

        预排有一个独立的轻量请求，不与场景/事实压缩争抢一次模型调用；
        返回空时只做**一次**恢复重试，覆盖偶尔漏掉 JSON 对象的 provider。
        """
        planner = getattr(self.compactor, 'plan_schedule_preplan', None)
        if not callable(planner):
            return None
        proposal = await planner(request)
        if proposal:
            return proposal
        self.report_operation(
            'diagnostic', 'warn', story, 'advance',
            'Schedule Preplan 返回为空，正在进行一次轻量恢复重试',
        )
        return await planner(request)

    async def persist_schedule_preplan_review(
        self, story: Any, review: Any, proposal: Any, now: Any,
    ) -> bool:
        """上游 `persistSchedulePreplanReview(story, review, proposal, now)`（`:6092`）。"""
        config = self._schedule_preplan_settings()
        current = pick(review, 'current')
        evidence_entries = _as_list(pick(review, 'evidenceEntries', 'evidence_entries'))
        local_date = pick(review, 'localDate', 'local_date')
        timezone_name = pick(pick(story, 'setting') or {}, 'timezone')
        variation = pick(config, 'variationLevel', 'variation_level')
        next_record = apply_schedule_preplan_proposal(
            current, proposal, evidence_entries, local_date, timezone_name, config, now, variation,
        )
        if not next_record:
            # 窄范围复核在 provider 不稳时仍可能失败。首次使用时写一份**显式的空复核**
            # 并带上已检视的证据游标：它诚实、能阻止整轮重试，新证据自然会让下次复核到期。
            if not current:
                empty = apply_schedule_preplan_proposal(
                    None,
                    {
                        'outcome': 'replace',
                        'reason': 'Schedule review returned no valid structure; waiting for new concrete evidence.',
                        'regimes': [], 'exceptions': [],
                    },
                    evidence_entries, local_date, timezone_name, config, now, variation,
                )
                if empty:
                    empty['story_id'] = pick(story, 'id')
                    await self.save_schedule_preplan(empty)
                    self.report_operation(
                        'standard', 'warn', story, 'advance',
                        'Schedule Preplan 未形成有效日程，已保存空审查记录并等待新证据',
                    )
                    return True
            self.report_operation(
                'standard', 'warn', story, 'advance',
                'Schedule Preplan 未更新：模型没有返回可用日程，保留现有版本',
            )
            return False
        next_record['story_id'] = pick(story, 'id')
        await self.save_schedule_preplan(next_record)
        self.report_operation(
            'standard', 'info', story, 'advance',
            'Schedule Preplan 已审查 版本=%s 覆盖=%s→%s 原因=%s',
            next_record.get('revision'), next_record.get('valid_from'),
            next_record.get('valid_through'), next_record.get('review_reason'),
        )
        return True

    # ------------------------------------------------------------------ #
    # 流式回复后的剧本补写（上游 6131–6161）
    # ------------------------------------------------------------------ #

    async def schedule_stream_script_recovery(
        self, story_id: str, participant_id: str, now: Any, previous_attempts: int = 0,
    ) -> bool:
        """上游 `scheduleStreamScriptRecovery(storyId, participantId, now, previousAttempts = 0)`
        （`src/service.ts:6131`）。可见回复已经到达用户，因此这次重试**只许**补写缺失的
        生活剧本，绝不能制造第二条出站消息。
        """
        runtime = self.runtime_config
        delay_seconds = max(
            5,
            _config_int(
                _nullish(
                    pick(runtime, 'narrativeRetryDelaySeconds', 'narrative_retry_delay_seconds'), 60,
                ),
                60,
            ),
        )
        max_attempts = min(
            2,
            max(0, _config_int(
                _nullish(
                    pick(runtime, 'narrativeRetryMaxAttempts', 'narrative_retry_max_attempts'), 6,
                ),
                6,
            )),
        )
        if not participant_id or previous_attempts >= max_attempts:
            return False
        pending = await self.db_get('interlude_intent', {
            'storyId': story_id, 'participantId': participant_id,
            'status': 'pending', 'type': 'narrative-retry',
        })
        existing = [
            intent for intent in pending
            if pick(pick(intent, 'payload') or {}, 'streamRecovery', 'stream_recovery') is True
        ]
        # 上游用 `{id: {$in: [...]}}` 一次批量取消；本移植版的等值 where 逐条更新，
        # 语义一致（同一事务语义不需要，写入仍走 base.write 串行队列）。
        for intent in existing:
            await self.db_set(
                'interlude_intent', {'id': pick(intent, 'id')},
                {'status': 'cancelled', 'updatedAt': now},
            )
        attempt = previous_attempts + 1
        not_before = parse_dt(now) + timedelta(seconds=delay_seconds)
        await self.append_intent(story_id, {
            'type': 'narrative-retry',
            'summary': 'Recover only the missing script after a streamed reply (attempt %d/%d).'
                       % (attempt, max_attempts),
            # IntentDraft 在本移植版 types.py 里是 snake_case；两种拼写都给，
            # 让 chunk5 的实现读哪一份都成立（值相同，不产生第二份真相）。
            'notBefore': iso(not_before), 'not_before': iso(not_before),
            'payload': {
                'narrativeRetry': True, 'streamRecovery': True,
                'userInitiated': True, 'attempt': attempt,
            },
        }, now, participant_id)
        self.schedule_due_intent_wake(story_id, not_before)
        return True

    async def persist_stream_script_recovery(
        self, story: Any, participant: Any, decision: Any, now: Any,
    ) -> bool:
        """上游 `persistStreamScriptRecovery(story, participant, decision, now)`（`:6148`）。

        流式补写只恢复缺失的原文，绝不生成第二个通信决策或独立的连续性摘要。
        """
        raw_script = pick(decision, 'script')
        script = raw_script.strip() if isinstance(raw_script, str) else ''
        if not script:
            return False
        await self.append_entry(story.get('id'), {
            'kind': 'script', 'actor': 'narrator', 'content': script,
            'occurredAt': iso(now), 'metadata': {'phase': 'stream-script-recovery', 'interaction': None},
        }, now, pick(participant, 'id') or '')
        state = decode_story_state(pick(story, 'state'))
        next_state = dict(state)
        next_state['narrative_update_count'] = _config_int(state.get('narrative_update_count'), 0) + 1
        await self.db_set('interlude_story', {'id': story.get('id')}, {
            'state': encode_story_state(next_state), 'updatedAt': now,
        })
        return True

    # ------------------------------------------------------------------ #
    # 压缩主流程（上游 6163–6269）
    # ------------------------------------------------------------------ #

    async def compact_unlocked(self, story: Any, now: Any, force: bool) -> Any:
        """上游 `compactUnlocked(story, now, force)`（`src/service.ts:6163`）。"""
        review = await self.prepare_schedule_preplan_review(story, now)
        context = await self.prepare_compaction(story, now, force)
        needs_model = bool(pick(review, 'needsModel', 'needs_model')) if review else False
        if needs_model and pick(review, 'request'):
            proposal = await self.request_schedule_preplan(story, pick(review, 'request'))
            persisted = await self.persist_schedule_preplan_review(story, review, proposal, self.now())
            if persisted:
                self.schedule_preplan_backoff.pop(story.get('id'), None)
            else:
                self.schedule_preplan_backoff[story.get('id')] = (
                    self.now_ms() + SCHEDULE_PREPLAN_RETRY_BACKOFF
                )
        if not context or pick(context, 'phase') == 'skip':
            return bool(pick(context, 'overlayCompacted', 'overlay_compacted')) if context else False
        started_at = self.now_ms()
        self.report_operation(
            'standard', 'info', story, 'advance',
            '后台整理开始 条目=%d 字符=%d 场景压缩=%s SchedulePreplan=%s',
            len(_as_list(pick(context, 'sceneEntries', 'scene_entries'))),
            pick(context, 'chars') or 0,
            bool(pick(context, 'sceneCompactionDue', 'scene_compaction_due')),
            needs_model,
        )
        decision: dict[str, Any] = {}
        try:
            decision = await self.compactor.compact(pick(context, 'compactRequest', 'compact_request'))
        except Exception as error:
            self.note_compaction_failure(story.get('id'), pick(context, 'fingerprint'), error)
            self.report('warn', story, 'advance', '记忆压缩失败：%s', error)
            return False
        try:
            result = await self.apply_compaction(story, context, decision, now, started_at)
            if not await self.compaction_checkpoint_advanced(context):
                scene_entries = _as_list(pick(context, 'sceneEntries', 'scene_entries'))
                expected = scene_entries[-1].get('id', 0) if scene_entries else 0
                raise RuntimeError(
                    'Compaction checkpoint did not advance (scene=%s, expected=%s)'
                    % (pick(pick(context, 'scene') or {}, 'id'), expected)
                )
            self.compaction_backoff.pop(story.get('id'), None)
            return result
        except Exception as error:
            self.note_compaction_failure(story.get('id'), pick(context, 'fingerprint'), error)
            raise

    async def prepare_compaction(self, story: Any, now: Any, force: bool) -> Optional[dict[str, Any]]:
        """上游 `prepareCompaction(story, now, force)`（`src/service.ts:6199`）。

        到昂贵压缩调用之前的全部廉价读取与到期判定。它在故事串行队列里跑，
        但模型调用本身不能 —— 排队中的压缩请求会拖慢下一个实时回合。
        """
        await self.ensure_continuity(story, now)
        memory_enabled = self._memory_bool('enabled')
        overlay_compacted = await self.compact_overlay_unlocked(story, now) if memory_enabled else False
        story_id = pick(story, 'id')
        scene = await self.active_scene(story_id)
        if not scene:
            return {'phase': 'skip', 'overlay_compacted': overlay_compacted}
        # lastEntryId 把场景摘要变成增量检查点：已压缩过的原文不再重复传给模型。
        last_entry_id = scene.get('lastEntryId')
        entry_limit = max(
            self._memory_int('compactionEntryLimit', default=80) * 2,
            self._memory_int('compactionEntryLimit', default=80),
        )
        started_at = scene.get('startedAt')
        if last_entry_id is not None:
            entries = await self._scan_entries(
                story_id,
                {'storyId': story_id, 'id': {'$gt': last_entry_id}},
                {'limit': entry_limit, 'sort': {'id': 'ASC'}},
                lambda row: _is_int(row.get('id')) and row['id'] > last_entry_id,
            )
        else:
            entries = await self._scan_entries(
                story_id,
                {'storyId': story_id, 'occurredAt': {'$gte': started_at}},
                {'limit': entry_limit, 'sort': {'id': 'ASC'}},
                lambda row: _lte(started_at, row.get('occurredAt')),
            )
        scene_entries = compaction_prefix(entries, self._memory_int('compactionCharacterLimit', default=32_000))
        chars = sum(len(entry.get('content') or '') for entry in scene_entries)
        threshold_entries = self._memory_int('sceneEntryThreshold', default=16)
        threshold_chars = self._memory_int('sceneCharacterThreshold', default=10_000)
        total_chars = sum(len(entry.get('content') or '') for entry in entries)
        scene_compaction_due = (
            memory_enabled and len(scene_entries) > 0
            and (force or len(entries) >= threshold_entries or total_chars >= threshold_chars)
        )
        if not scene_compaction_due:
            self.report_operation(
                'diagnostic', 'debug', story, 'advance',
                '记忆整理跳过：未达到阈值 条目=%d/%d 字符=%d/%d',
                len(scene_entries), threshold_entries, chars, threshold_chars,
            )
            return {'phase': 'skip', 'overlay_compacted': overlay_compacted}
        fingerprint = self.compaction_fingerprint(scene, scene_entries, chars)
        if not force and self.compaction_is_backed_off(story_id, fingerprint):
            return {'phase': 'skip', 'overlay_compacted': overlay_compacted}
        current = await self.get_story(story_id)
        participants = await self.participants(story_id)
        shared = self._share_participant_details()
        if last_entry_id is None:
            preceding: list[dict[str, Any]] = []
        else:
            preceding = await self._scan_entries(
                story_id,
                {'storyId': story_id, 'id': {'$lte': last_entry_id}},
                {'limit': 12, 'sort': {'id': 'DESC'}},
                lambda row: _is_int(row.get('id')) and row['id'] <= last_entry_id,
                descending=True,
            )
        # 上游 `preceding.reverse()` 后按字数预算保留 8_000 字符（从最新往前）。
        preceding_entries = limit_entries_by_characters(
            [entry for entry in reversed(preceding) if shared or not entry.get('participantId')],
            8_000,
        )
        if shared:
            visible_rows = list(scene_entries)
        else:
            visible_rows = [
                dict(entry, participantId='', content='[participant-specific conversation omitted by privacy setting]', metadata={})
                if entry.get('participantId') else entry
                for entry in scene_entries
            ]
            masked = sum(1 for entry in scene_entries if entry.get('participantId'))
            if masked:
                # 共享主剧本是硬开启的，于是**每句私聊都挂在某个参与者上**；而
                # `share_participant_details` 默认关闭时，上面这段会把它们的正文全换成占位串
                # ——压缩模型看到的就是「内容因隐私设置被省略」，产出「场景摘要全是省略」
                # +「长期事实 0」。这是上游的隐私语义（刻意如此），但默认组合下的效果很像故障，
                # 所以在日志里说清楚，并给出唯一的补救开关。
                self.report_operation(
                    'standard', 'warn', story, 'advance',
                    '记忆整理：共享主剧本下的对话内容被隐私开关隐藏 条数=%d ——'
                    'share_participant_details 关闭时场景摘要与长期事实只能看到系统条目；'
                    '单人使用（不在乎各账号互相可见）建议在「共享主剧本」里打开它',
                    masked,
                )
        visible_compaction_entries = [
            entry for entry in visible_rows if (entry.get('content') or '').strip()
        ]
        if not memory_enabled:
            visible_compaction_facts: list[dict[str, Any]] = []
        else:
            facts = await self.facts(story_id, self._memory_int('maxFactsPerStory', default=200))
            visible_compaction_facts = facts if shared else [
                fact for fact in facts if not fact.get('participantId')
            ]
        raw_candidates = await self._scan_state_patches(
            story_id, ['proposed', 'applied'],
            {'limit': 50, 'sort': {'createdAt': 'DESC'}},
        )
        raw_development_candidates = [
            item for item in raw_candidates
            if str(item.get('path') or '').startswith('development.')
            and (shared or not item.get('participantId'))
        ]
        candidate_source_ids: list[int] = []
        for item in raw_development_candidates:
            for source_id in _as_list(item.get('sourceEntryIds')):
                if _is_int(source_id) and source_id not in candidate_source_ids:
                    candidate_source_ids.append(source_id)
        candidate_source_ids = candidate_source_ids[:1_000]
        candidate_sources = (
            await self._entries_by_ids(story_id, candidate_source_ids) if candidate_source_ids else []
        )
        development_candidates = [
            item for item in raw_development_candidates
            if prompt_ready_development(_dual_patch(item), _dual_entries(candidate_sources))
        ]
        return {
            'phase': 'run',
            'overlay_compacted': overlay_compacted,
            'scene': scene,
            'scene_entries': scene_entries,
            'chars': chars,
            'scene_compaction_due': True,
            'current': current,
            'participants': participants,
            'visible_compaction_entries': visible_compaction_entries,
            'visible_compaction_facts': visible_compaction_facts,
            'fingerprint': fingerprint,
            'compact_request': {
                'story': current,
                'from': parse_dt(started_at),
                'now': now,
                'entries': visible_compaction_entries,
                'scene': scene,
                'arc': await self.active_arc(story_id),
                'participants': participants,
                'preceding_entries': preceding_entries,
                'development_candidates': development_candidates,
                'facts': visible_compaction_facts,
            },
        }

    async def apply_compaction(
        self, story: Any, context: Any, decision: Any, now: Any, started_at: int,
    ) -> bool:
        """上游 `applyCompaction(story, context, decision, now, startedAt)`（`src/service.ts:6263`）。

        一次压缩决策的廉价落库。调用方重新取得故事串行队列，保证写入与叙事回合有序。
        """
        scene_compaction_due = bool(pick(context, 'sceneCompactionDue', 'scene_compaction_due'))
        if scene_compaction_due:
            await self.persist_compaction(
                pick(context, 'current'),
                pick(context, 'scene'),
                decision,
                _as_list(pick(context, 'sceneEntries', 'scene_entries')),
                now,
                {pick(fact, 'id') for fact in _as_list(pick(context, 'visibleCompactionFacts', 'visible_compaction_facts'))},
            )
        self.report_operation(
            'standard', 'info', story, 'advance',
            '后台整理完成 耗时=%dms 剧本条目=%d 长期事实=%d 状态变更=%d',
            self.now_ms() - started_at,
            len(_as_list(pick(context, 'sceneEntries', 'scene_entries'))) if scene_compaction_due else 0,
            len(_as_list(pick(decision, 'facts'))) if scene_compaction_due else 0,
            len(_as_list(pick(decision, 'statePatches', 'state_patches'))) if scene_compaction_due else 0,
        )
        return True

    # ------------------------------------------------------------------ #
    # Overlay 分层归档（上游 6273–6343）
    # ------------------------------------------------------------------ #

    async def compact_overlay_unlocked(self, story: Any, now: Any) -> bool:
        """上游 `compactOverlayUnlocked(story, now)`（`src/service.ts:6273`）。

        更早的 state patch 只由后台维护通道压缩；实时回合始终保留最近几天的原文。
        """
        if not self._memory_bool('overlayCompressionEnabled'):
            return False
        try:
            recent_days = self._memory_int('overlayRecentDays', default=2)
            monthly_after_days = self._memory_int('overlayMonthlyAfterDays', default=10)
            weekly_window = self._memory_int('overlayWeeklyWindowDays', default=5)
            monthly_window = self._memory_int('overlayMonthlyWindowDays', default=10)
            weekly_chars = self._memory_int('overlayWeeklySummaryCharacters', default=1_600)
            monthly_chars = self._memory_int('overlayMonthlySummaryCharacters', default=2_400)
            moment = parse_dt(now)
            recent_cutoff = moment - timedelta(days=recent_days)
            monthly_cutoff = moment - timedelta(days=monthly_after_days)
            story_id = pick(story, 'id')
            applied = await self.db_get(
                'interlude_state_patch', {'storyId': story_id, 'status': 'applied'},
                {'sort': {'appliedAt': 'ASC'}},
            )
            weekly = [
                patch for patch in applied
                if not str(patch.get('path') or '').startswith('development.')
                and _lte(_nullish(patch.get('appliedAt'), patch.get('createdAt')), recent_cutoff)
            ]
            changed = False
            for group in group_overlay_patches(weekly, weekly_window):
                group_participant = pick(group, 'participantId', 'participant_id') or ''
                group_target = pick(group, 'target')
                group_from = pick(group, 'from')
                group_to = pick(group, 'to')
                group_patches = _as_list(pick(group, 'patches'))
                existing = await self.db_get('interlude_overlay_snapshot', {
                    'storyId': story_id, 'participantId': group_participant,
                    'target': group_target, 'tier': 'weekly',
                    'periodStart': _where_dt(group_from),
                })
                if existing:
                    continue
                participant = (
                    await self.get_participant(group_participant) if group_participant else None
                )
                decision = await self.compactor.compact_overlay({
                    'story': story, 'participant': participant, 'target': group_target,
                    'tier': 'weekly', 'from': group_from, 'to': group_to,
                    'patches': group_patches,
                })
                summary = clip(pick(decision, 'summary'), weekly_chars)
                if not summary:
                    continue
                await self.db_create('interlude_overlay_snapshot', {
                    'storyId': story_id, 'participantId': group_participant,
                    'target': group_target, 'tier': 'weekly',
                    'periodStart': group_from, 'periodEnd': group_to,
                    'summary': summary,
                    'majorEvents': normalize_major_events(
                        pick(decision, 'majorEvents', 'major_events'), group_patches,
                    ),
                    'sourcePatchIds': [patch.get('id') for patch in group_patches],
                    'status': 'active', 'createdAt': now, 'updatedAt': now,
                })
                for patch in group_patches:
                    await self.db_set('interlude_state_patch', {'id': patch.get('id')}, {'status': 'compacted'})
                changed = True

            snapshots = await self.db_get(
                'interlude_overlay_snapshot',
                {'storyId': story_id, 'tier': 'weekly', 'status': 'active'},
                {'sort': {'periodEnd': 'ASC'}},
            )
            for group in group_overlay_snapshots(
                [item for item in snapshots if _lte(item.get('periodEnd'), monthly_cutoff)], monthly_window,
            ):
                group_participant = pick(group, 'participantId', 'participant_id') or ''
                group_target = pick(group, 'target')
                group_from = pick(group, 'from')
                group_to = pick(group, 'to')
                group_snapshots = _as_list(pick(group, 'snapshots'))
                existing = await self.db_get('interlude_overlay_snapshot', {
                    'storyId': story_id, 'participantId': group_participant,
                    'target': group_target, 'tier': 'monthly',
                    'periodStart': _where_dt(group_from),
                })
                if existing:
                    continue
                participant = (
                    await self.get_participant(group_participant) if group_participant else None
                )
                decision = await self.compactor.compact_overlay({
                    'story': story, 'participant': participant, 'target': group_target,
                    'tier': 'monthly', 'from': group_from, 'to': group_to,
                    'patches': [], 'snapshots': group_snapshots,
                })
                summary = clip(pick(decision, 'summary'), monthly_chars)
                if not summary:
                    continue
                source_patch_ids: list[Any] = []
                for snapshot in group_snapshots:
                    for patch_id in _as_list(snapshot.get('sourcePatchIds')):
                        if patch_id not in source_patch_ids:
                            source_patch_ids.append(patch_id)
                await self.db_create('interlude_overlay_snapshot', {
                    'storyId': story_id, 'participantId': group_participant,
                    'target': group_target, 'tier': 'monthly',
                    'periodStart': group_from, 'periodEnd': group_to,
                    'summary': summary,
                    'majorEvents': normalize_major_events(
                        pick(decision, 'majorEvents', 'major_events'), [], group_snapshots,
                    ),
                    'sourcePatchIds': source_patch_ids,
                    'status': 'active', 'createdAt': now, 'updatedAt': now,
                })
                for snapshot in group_snapshots:
                    await self.db_set(
                        'interlude_overlay_snapshot', {'id': snapshot.get('id')},
                        {'status': 'superseded', 'updatedAt': now},
                    )
                changed = True
            if changed:
                await self.rebuild_live_overlay_state(story, now)
                self.report_operation(
                    'standard', 'info', story, 'advance',
                    'Overlay 分层归档完成：最近 %d 天保留原始补丁，短期窗口=%d天，长期窗口=%d天',
                    recent_days, weekly_window, monthly_window,
                )
            return changed
        except Exception as error:
            # Overlay 维护是可选的后台工作。一次坏的压缩响应必须让原始补丁毫发无损，
            # 并且绝不阻塞叙事。
            self.report_operation('standard', 'warn', story, 'advance', 'Overlay 分层归档跳过：%s', error)
            return False

    async def overlay_snapshots_for_prompt(
        self, story_id: str, participant_id: Optional[str] = None, background: bool = False,
    ) -> list[dict[str, Any]]:
        """上游 `overlaySnapshotsForPrompt(storyId, participantId?, background = false)`（`:6329`）。"""
        if not self._memory_bool('overlayCompressionEnabled'):
            return []
        rows = await self.db_get(
            'interlude_overlay_snapshot', {'storyId': story_id, 'status': 'active'},
            {'sort': {'periodEnd': 'DESC'}},
        )
        shared = self._share_participant_details()
        visible = [
            snapshot for snapshot in rows
            if not snapshot.get('participantId')
            or (shared if background else snapshot.get('participantId') == participant_id)
        ]
        # 当前长期状态 + 最近短期增量已经足够；更旧的快照仍可检索/审计，
        # 不必永久占用 prompt 预算。
        result: list[dict[str, Any]] = []
        for target in ('character', 'perspective', 'world', 'relationship'):
            matches = [snapshot for snapshot in visible if snapshot.get('target') == target]
            monthly = next((item for item in matches if item.get('tier') == 'monthly'), None)
            if monthly is not None:
                result.append(monthly)
            result.extend([item for item in matches if item.get('tier') == 'weekly'][:4])
        return result

    async def rebuild_live_overlay_state(self, story: Any, now: Any) -> None:
        """上游 `rebuildLiveOverlayState(story, now)`（`src/service.ts:6348`）。

        一旦快照安全地代表了更早的变化，`state.overlay` 就只保留**实时（未压缩）增量**。
        这才是真正缩小 prompt 的地方；更早的演化由快照单独承载。
        """
        story_id = pick(story, 'id')
        # 上游是 `Promise.all([...])`（两读并发）。本移植版的 `db_get` 把查询丢进
        # 线程池，而 `plugin/core/database.py` 只有一条 sqlite3 连接：并发读会在
        # 驱动层偶发 `sqlite3.InterfaceError: bad parameter or other API misuse`
        # （实测 300 行规模下 20 次并发读失败 5 次）。两次读的顺序不影响结果，
        # 因此这里改成串行 await。
        applied_rows = await self.db_get(
            'interlude_state_patch', {'storyId': story_id, 'status': 'applied'},
        )
        snapshots = await self.db_get(
            'interlude_overlay_snapshot', {'storyId': story_id, 'status': 'active'},
        )
        applied = [
            item for item in applied_rows if not str(item.get('path') or '').startswith('development.')
        ]
        state = decode_story_state(pick(story, 'state'))
        overlay = dict(state.get('setting_overlay') or {})

        def has_global_history(target: str) -> bool:
            return any(
                snapshot.get('target') == target and not snapshot.get('participantId')
                for snapshot in snapshots
            )

        def global_patches(target: str) -> list[dict[str, Any]]:
            return [
                patch for patch in applied
                if not patch.get('participantId') and patch.get('target') == target
            ]

        if has_global_history('character'):
            overlay.pop('character_profile', None)
            overlay['character_traits'] = []
            for patch in global_patches('character'):
                if 'trait' in str(patch.get('path') or ''):
                    overlay['character_traits'].append(clip(patch.get('proposedValue'), 500))
                else:
                    overlay['character_profile'] = merge_note(
                        overlay.get('character_profile'), patch.get('proposedValue'),
                    )
            overlay['character_traits'] = _unique(overlay['character_traits'])[-30:]
        if has_global_history('perspective'):
            overlay.pop('perspective', None)
            for patch in global_patches('perspective'):
                overlay['perspective'] = merge_note(overlay.get('perspective'), patch.get('proposedValue'))
        if has_global_history('world'):
            overlay.pop('world', None)
            for patch in global_patches('world'):
                overlay['world'] = merge_note(overlay.get('world'), patch.get('proposedValue'))
        if has_global_history('relationship'):
            overlay.pop('relationship', None)
            for patch in global_patches('relationship'):
                overlay['relationship'] = merge_note(overlay.get('relationship'), patch.get('proposedValue'))
        next_state = dict(state)
        next_state['setting_overlay'] = overlay
        await self.db_set('interlude_story', {'id': story_id}, {
            'state': encode_story_state(next_state), 'updatedAt': now,
        })

        participant_ids = _unique([
            snapshot.get('participantId') for snapshot in snapshots
            if snapshot.get('target') == 'relationship' and snapshot.get('participantId')
        ])
        for participant_id in participant_ids:
            participant = await self.get_participant(participant_id)
            if not participant:
                continue
            participant_state = normalize_participant_state(participant.get('state'))
            participant_state.pop('relationshipOverlay', None)
            for patch in applied:
                if patch.get('target') == 'relationship' and patch.get('participantId') == participant_id:
                    participant_state['relationshipOverlay'] = merge_note(
                        participant_state.get('relationshipOverlay'), patch.get('proposedValue'),
                    )
            await self.db_set('interlude_participant', {'id': participant.get('id')}, {
                'state': participant_state, 'updatedAt': now,
            })

    # ------------------------------------------------------------------ #
    # 压缩落库（上游 6394–6615）
    # ------------------------------------------------------------------ #

    async def persist_compaction(
        self,
        story: Any,
        scene: Any,
        decision: Any,
        entries: list[dict[str, Any]],
        now: Any,
        visible_fact_ids: Optional[set[Any]] = None,
    ) -> None:
        """上游 `persistCompaction(story, scene, decision, entries, now, visibleFactIds)`（`:6394`）。"""
        visible_fact_ids = visible_fact_ids if visible_fact_ids is not None else set()
        # 空/沉默的 provider 响应必须让证据留在原地等待重试。
        assert_continuity_review(decision)
        if not entries:
            return
        story_id = pick(story, 'id')
        for draft in _as_list(pick(decision, 'episodeTags', 'episode_tags')):
            source_id = pick(draft, 'sourceEntryId', 'source_entry_id')
            source = next((entry for entry in entries if entry.get('id') == source_id), None)
            if source is None:
                continue
            tags = grounded_episode_tags(source.get('content') or '', draft)
            if not tags:
                continue
            current_rows = await self.db_get(
                'interlude_script_entry', {'storyId': story_id, 'id': source.get('id')},
            )
            current = current_rows[0] if current_rows else None
            if current is None:
                continue
            await self.db_set('interlude_script_entry', {'id': source.get('id')}, {
                'metadata': {**(current.get('metadata') or {}), 'episodeTags': tags},
            })
            self.invalidate_history_vectors(story_id)
        # 先提交剧情弧，再确认增量证据。弧写入失败不能悄悄丢掉它本该学习的材料。
        arc = await self.active_arc(story_id)
        if not arc:
            raise RuntimeError('Continuity review has no active arc')
        arc_decision = pick(decision, 'arc') or {}
        arc_title = clip((pick(arc_decision, 'title') or '').strip() or arc.get('title'), 255)
        await self.db_set('interlude_arc', {'id': arc.get('id')}, {
            'title': arc_title,
            'summary': clip(pick(arc_decision, 'summary'), self._memory_int('arcSummaryCharacters', default=12_000)),
            'updatedAt': now,
        })
        # 摘要更新成功后才移动 lastEntryId，确保失败时原始条目仍会在下次被重新处理。
        scene_patch = pick(decision, 'scene') or {}
        boundary = pick(scene_patch, 'boundary') or {}
        explicit_boundary = (
            pick(scene_patch, 'close') is True
            and bool((pick(boundary, 'reason') or '').strip())
            and has_compaction_evidence(
                pick(boundary, 'sourceEntryIds', 'source_entry_ids'), entries,
            )
        )
        last_entry = entries[-1] if entries else None
        last_entry_id = _nullish(
            last_entry.get('id') if last_entry else None, scene.get('lastEntryId'),
        )
        await self.db_set('interlude_scene', {'id': scene.get('id')}, {
            'hook': clip(
                _nullish(pick(scene_patch, 'hook'), scene.get('hook')),
                self._memory_int('sceneHookCharacters', default=2_000),
            ),
            'summary': clip(
                _nullish(pick(scene_patch, 'summary'), scene.get('summary')),
                self._memory_int('sceneSummaryCharacters', default=8_000),
            ),
            'entryCount': max(0, _config_int(scene.get('entryCount'), 0)) + len(entries),
            'lastEntryId': last_entry_id,
            'updatedAt': now,
        })
        if explicit_boundary:
            # 边界出处与它的原始证据放在一起。这个检查点只做导航，
            # 没有生成的摘要或散文。
            source_ids = explicit_evidence_ids(
                pick(boundary, 'sourceEntryIds', 'source_entry_ids'), entries,
            )
            boundary_entry = None
            if source_ids:
                boundary_entry = next(
                    (entry for entry in entries if entry.get('id') == max(source_ids)), None,
                )
            if boundary_entry is not None:
                source_rows = await self.db_get('interlude_script_entry', {'id': boundary_entry.get('id')})
                source = source_rows[0] if source_rows else None
                if source is not None and source.get('storyId') == story_id:
                    first_rows = await self._scan_entries(
                        story_id,
                        {
                            'storyId': story_id,
                            'occurredAt': {'$gte': scene.get('startedAt')},
                            'id': {'$lte': boundary_entry.get('id')},
                        },
                        {'sort': {'id': 'ASC'}, 'limit': 1},
                        lambda row: (
                            _lte(scene.get('startedAt'), row.get('occurredAt'))
                            and _is_int(row.get('id')) and row['id'] <= boundary_entry.get('id')
                        ),
                    )
                    first = first_rows[0] if first_rows else None
                    await self.db_set('interlude_script_entry', {'id': source.get('id')}, {
                        'metadata': {
                            **(source.get('metadata') or {}),
                            'sceneCheckpoint': {
                                'sceneId': scene.get('id'),
                                'startedAt': iso(scene.get('startedAt')),
                                'endedAt': iso(last_entry.get('occurredAt')) if last_entry else None,
                                'boundarySourceEntryIds': source_ids,
                                'reason': pick(boundary, 'reason'),
                                'firstEntryId': (first.get('id') if first else None) or boundary_entry.get('id'),
                                'lastEntryId': last_entry_id,
                            },
                        },
                    })
                    self.invalidate_history_vectors(story_id)
            # 在处理到的前沿关闭，而不是在模型返回时关闭。后台请求期间到达的条目
            # 属于下一次复核。
            frontier = last_entry.get('occurredAt') if last_entry else None
            await self.db_set('interlude_scene', {'id': scene.get('id')}, {
                'status': 'closed', 'endedAt': frontier, 'updatedAt': now,
            })
            await self.ensure_continuity(story, frontier)
            next_scene = await self.active_scene(story_id)
            if next_scene:
                await self.db_set(
                    'interlude_scene', {'id': next_scene.get('id')},
                    {'lastEntryId': last_entry.get('id') if last_entry else None},
                )
        presence_updates = normalize_scene_presence_drafts(
            pick(scene_patch, 'presence'), entries, now,
        )
        if presence_updates:
            current = await self.get_story(story_id)
            state = decode_story_state(pick(current, 'state'))
            by_name: dict[Any, dict[str, Any]] = {
                item.get('name'): item for item in _as_list(state.get('scene_presence'))
            }
            boundary_entry_id = pick(state.get('scene_frame') or {}, 'localBoundaryEntryId', 'local_boundary_entry_id') or 0
            for update in presence_updates:
                if _js_max(update.get('source_entry_ids'), 0) < boundary_entry_id:
                    continue
                previous = by_name.get(update.get('name'))
                if previous and _js_max(previous.get('source_entry_ids'), 0) > _js_max(update.get('source_entry_ids'), 0):
                    continue
                by_name[update.get('name')] = update
            next_state = dict(state)
            next_state['scene_presence'] = list(by_name.values())[-8:]
            await self.db_set('interlude_story', {'id': current.get('id')}, {
                'state': encode_story_state(next_state), 'updatedAt': now,
            })
        working_details = _as_list(pick(decision, 'workingDetails', 'working_details'))
        if working_details:
            current = await self.get_story(story_id)
            state = decode_story_state(pick(current, 'state'))
            merged: dict[str, dict[str, Any]] = {}
            resolutions = dict(state.get('working_detail_resolutions') or {})
            for item in _as_list(state.get('working_details')):
                merged[item.get('label')] = item
            for draft in working_details:
                draft_source_ids = pick(draft, 'sourceEntryIds', 'source_entry_ids') or []
                if not has_compaction_evidence(draft_source_ids, entries):
                    continue
                raw_label = pick(draft, 'label')
                label = clip(raw_label, 80).strip() if isinstance(raw_label, str) else ''
                # JS `Math.max(...[])` 是 -Infinity（不是抛错）：没有一条 id 落在本批
                # 条目里时，revision 永远不可能推进任何已有记录。
                revision = _js_max(explicit_evidence_ids(draft_source_ids, entries))
                if revision <= (resolutions.get(label) or 0):
                    continue
                previous = merged.get(label)
                if previous and revision <= max(0.0, _js_max(previous.get('source_entry_ids') or previous.get('sourceEntryIds'))):
                    continue
                sources = [entry for entry in entries if entry.get('id') in draft_source_ids]
                owners = {entry.get('participantId') for entry in sources}
                participant_id = sources[0].get('participantId') if len(owners) == 1 and sources else None
                previous_participant = previous.get('participant_id', previous.get('participantId')) if previous else None
                if previous is not None and previous_participant is not None and previous_participant != participant_id:
                    continue
                source_entry_ids = explicit_evidence_ids(draft_source_ids, entries)[:8]
                knowledge = normalize_knowledge_evidence(
                    _dual_knowledge(pick(draft, 'knowledge')), _dual_entries(entries), source_entry_ids,
                )
                previous_knowledge = previous.get('knowledge') if previous else None
                if previous_knowledge and supports_recorded_outcome(previous_knowledge) \
                        and not supports_recorded_outcome(knowledge):
                    continue
                if label and pick(draft, 'resolved') is True:
                    merged.pop(label, None)
                    resolutions[label] = revision
                    continue
                value = clip(pick(draft, 'value'), 300).strip()
                if not label or not value:
                    continue
                raw_expires = pick(draft, 'expiresAt', 'expires_at')
                expires_at = raw_expires if isinstance(raw_expires, str) and parse_dt(raw_expires) is not None else None
                raw_replaces = pick(draft, 'replacesLabel', 'replaces_label')
                replaces_label = clip(raw_replaces, 80).strip() if isinstance(raw_replaces, str) else ''
                replaced = merged.get(replaces_label) if replaces_label and replaces_label != label else None
                if replaces_label and replaces_label != label and (
                    replaced is None or previous is not None
                    or revision <= (resolutions.get(replaces_label) or 0)
                ):
                    continue
                if replaced is not None:
                    replaced_participant = replaced.get('participant_id', replaced.get('participantId'))
                    if replaced_participant is not None:
                        same_owner = replaced_participant == participant_id
                    else:
                        replaced_knowledge = replaced.get('knowledge') or {}
                        same_owner = bool(replaced_knowledge.get('holder')) \
                            and replaced_knowledge.get('holder') == knowledge.get('holder')
                    if not same_owner or not supports_recorded_outcome(knowledge) \
                            or revision <= max(
                                resolutions.get(replaces_label) or 0,
                                _js_max(replaced.get('source_entry_ids') or replaced.get('sourceEntryIds'), 0),
                            ):
                        continue
                    merged.pop(replaces_label, None)
                    resolutions[replaces_label] = revision
                detail: dict[str, Any] = {
                    'label': label, 'value': value, 'knowledge': knowledge,
                    'created_at': (
                        previous.get('created_at', previous.get('createdAt')) if previous
                        else (replaced.get('created_at', replaced.get('createdAt')) if replaced else iso(now))
                    ),
                }
                if participant_id is not None:
                    detail['participant_id'] = participant_id
                if expires_at:
                    detail['expires_at'] = expires_at
                if source_entry_ids:
                    detail['source_entry_ids'] = source_entry_ids
                merged[label] = detail
            live = [
                item for item in merged.values()
                if not _nullish(item.get('expires_at'), item.get('expiresAt'))
                or (_datetime(_nullish(item.get('expires_at'), item.get('expiresAt'))) or moment_zero()) > parse_dt(now)
            ][-10:]
            next_state = dict(state)
            next_state['working_details'] = live
            next_state['working_detail_resolutions'] = resolutions
            await self.db_set('interlude_story', {'id': current.get('id')}, {
                'state': encode_story_state(next_state), 'updatedAt': now,
            })
        resolved_facts = False
        for fact in _as_list(pick(decision, 'facts')):
            fact_source_ids = pick(fact, 'sourceEntryIds', 'source_entry_ids')
            if not has_compaction_evidence(fact_source_ids, entries):
                continue
            # 上游直接把 `fact.sourceEntryIds ?? []` 交给证据归一化（**不过滤**）。
            knowledge = normalize_knowledge_evidence(
                _dual_knowledge(pick(fact, 'knowledge')),
                _dual_entries(entries),
                _as_list(fact_source_ids),
            )
            if supports_recorded_outcome(knowledge):
                resolved = await self.resolve_compaction_facts(
                    story_id, pick(fact, 'resolvesFactIds', 'resolves_fact_ids'),
                    visible_fact_ids, now,
                )
            else:
                resolved = False
            resolved_facts = resolved_facts or bool(resolved)
            merged_resolution = await self.persist_fact(story_id, fact, entries, now)
            resolved_facts = resolved_facts or bool(merged_resolution)
        for patch in _as_list(pick(decision, 'statePatches', 'state_patches')):
            if not has_compaction_evidence(pick(patch, 'sourceEntryIds', 'source_entry_ids'), entries):
                continue
            await self.persist_state_patch(story, patch, entries, now)
        if resolved_facts:
            await self.mark_continuity_dirty(story_id, now)

    async def persist_fact(
        self, story_id: str, draft: Any, entries: list[dict[str, Any]], now: Any,
    ) -> bool:
        """上游 `persistFact(storyId, draft, entries, now)`（`src/service.ts:6535`）。"""
        content = clip(pick(draft, 'content'), self._memory_int('factContentCharacters', default=4_000))
        if not content:
            return False
        draft_source_ids = pick(draft, 'sourceEntryIds', 'source_entry_ids')
        participant_id = resolve_participant_id(
            pick(draft, 'participantId', 'participant_id'), draft_source_ids, _dual_entries(entries),
        )
        existing = await self.db_get('interlude_fact', {'storyId': story_id, 'status': 'active'})
        # 当前先做完全规范化匹配的去重；更复杂的语义去重可在检索层升级时替换。
        matching = [
            fact for fact in existing
            if normalize_fact(fact.get('content') or '') == normalize_fact(content)
            and (fact.get('participantId') or '') == (participant_id or '')
        ]
        source_entry_ids = explicit_evidence_ids(draft_source_ids, entries)[:20]
        raw_related = pick(pick(draft, 'knowledge') or {}, 'relatedFactIds', 'related_fact_ids')
        related_fact_ids = [
            item for item in _as_list(raw_related)
            if any(fact.get('id') == item and (fact.get('participantId') or '') == (participant_id or '') for fact in existing)
        ] if isinstance(raw_related, list) else []
        knowledge = normalize_knowledge_evidence(
            _dual_knowledge(pick(draft, 'knowledge')), _dual_entries(entries),
            source_entry_ids, related_fact_ids,
        )
        # 承诺类事实默认未兑现，除非压缩器明确说这条承诺已经履行或关闭。
        can_close = supports_recorded_outcome(knowledge)
        same = next(
            (fact for fact in matching
             if (pick(fact, 'knowledge') or {}).get('mode', 'unclassified') == knowledge.get('mode')),
            None,
        )
        if same is None:
            same = next((fact for fact in matching if not fact.get('knowledge') and can_close), None)
        draft_unresolved = pick(draft, 'unresolved')
        unresolved = draft_unresolved is True or (
            pick(draft, 'scope') == 'promise'
            and (draft_unresolved is not False or not can_close)
        )
        if same is not None and (
            (pick(same, 'knowledge') or {}).get('mode', 'unclassified') == knowledge.get('mode')
            or (not same.get('knowledge') and can_close)
        ):
            resolved = bool(same.get('unresolved')) and draft_unresolved is False and can_close
            same_embedding = same.get('embedding')
            embedding = same_embedding if same_embedding else await self.embed_text(content)
            same_source_ids = _as_list(same.get('sourceEntryIds'))
            confidence = same.get('confidence')
            gained = any(
                clause.get('source_entry_id') not in same_source_ids
                and clause.get('role') in ('observation', 'confirmation')
                and any(
                    entry.get('id') == clause.get('source_entry_id')
                    and entry.get('kind') in ('user-message', 'character-message')
                    for entry in entries
                )
                for clause in _as_list(knowledge.get('clauses'))
            )
            if gained:
                confidence = max(
                    same.get('confidence') or 0,
                    clamp_number(pick(draft, 'confidence'), same.get('confidence') or 0, 0, 1),
                )
            patch: dict[str, Any] = {
                'importance': max(
                    same.get('importance') or 0,
                    clamp_number(pick(draft, 'importance'), same.get('importance') or 0, 0, 1),
                ),
                'confidence': confidence,
                'unresolved': False if resolved else (bool(same.get('unresolved')) or unresolved),
                'sourceEntryIds': list(dict.fromkeys([*same_source_ids, *source_entry_ids])),
                'lastSeenAt': now,
                'updatedAt': now,
                'knowledge': {
                    **knowledge,
                    'clauses': [
                        *_as_list((pick(same, 'knowledge') or {}).get('clauses')),
                        *_as_list(knowledge.get('clauses')),
                    ][-12:],
                    'related_fact_ids': list(dict.fromkeys([
                        *_as_list((pick(same, 'knowledge') or {}).get('related_fact_ids')),
                        *_as_list(knowledge.get('related_fact_ids')),
                    ]))[-12:],
                },
            }
            if embedding:
                patch['embedding'] = embedding
            await self.db_set('interlude_fact', {'id': same.get('id')}, patch)
            return resolved
        if len(existing) >= self._memory_int('maxFactsPerStory', default=200):
            oldest = sorted(
                existing,
                key=lambda fact: (fact.get('importance') or 0) * (fact.get('confidence') or 0),
            )[0]
            if oldest:
                await self.db_set(
                    'interlude_fact', {'id': oldest.get('id')},
                    {'status': 'superseded', 'updatedAt': now},
                )
        await self.db_create('interlude_fact', {
            'storyId': story_id, 'participantId': participant_id,
            'scope': pick(draft, 'scope'), 'content': content, 'knowledge': knowledge,
            'importance': clamp_number(pick(draft, 'importance'), 0.5, 0, 1),
            'confidence': clamp_number(pick(draft, 'confidence'), 0.5, 0, 1),
            'unresolved': unresolved,
            'embedding': await self.embed_text(content),
            'status': 'active', 'sourceEntryIds': source_entry_ids,
            'lastSeenAt': now, 'createdAt': now, 'updatedAt': now,
        })
        return False

    async def embed_text(self, value: str) -> list[float]:
        """上游 `embedText(value)`（`src/service.ts:6582`）。

        向量能提升召回，但绝不能让一个私聊回合失败。
        """
        try:
            return await self.embedder.embed(value)
        except Exception as error:
            self.report_standalone_operation('diagnostic', 'debug', 'Embedding 请求跳过 错误=%s', error)
            return []

    def schedule_fact_embedding_backfill(self, story_id: str) -> None:
        """上游 `scheduleFactEmbeddingBackfill(storyId)`（`src/service.ts:6592`）。

        这个维护任务刻意留在叙事串行队列之外：它只填一个可选的索引列，
        绝不能拖慢新的用户事件。
        """
        try:
            embedding = _config_section(getattr(self, 'config', None), 'model').get('embedding')
        except Exception:  # pragma: no cover
            embedding = None
        if not isinstance(embedding, dict):
            return
        batch_size = pick(embedding, 'backfillBatchSize', 'backfill_batch_size')
        batch_size = 5 if batch_size is None else _config_int(batch_size, 5)
        model = pick(embedding, 'model')
        if not pick(embedding, 'enabled') or not (model or '').strip() or batch_size <= 0:
            return
        if story_id in self.fact_backfills:
            return
        self.fact_backfills.add(story_id)

        async def run() -> None:
            try:
                await self.backfill_fact_embeddings(story_id, batch_size)
            except Exception as error:
                self.report_standalone_operation(
                    'diagnostic', 'debug', '长期事实向量补齐跳过 错误=%s', error,
                )
            finally:
                self.fact_backfills.discard(story_id)

        asyncio.ensure_future(run())

    async def backfill_fact_embeddings(self, story_id: str, batch_size: int) -> None:
        """上游 `backfillFactEmbeddings(storyId, batchSize)`（`src/service.ts:6605`）。"""
        facts = await self.db_get('interlude_fact', {'storyId': story_id, 'status': 'active'})
        missing = [fact for fact in facts if not fact.get('embedding')]
        missing.sort(
            key=lambda fact: dt_ms(parse_dt(fact.get('updatedAt')) or moment_zero()),
            reverse=True,
        )
        for fact in missing[:max(0, batch_size)]:
            embedding = await self.embed_text(fact.get('content') or '')
            if embedding:
                await self.db_set(
                    'interlude_fact', {'id': fact.get('id')},
                    {'embedding': embedding, 'updatedAt': self.now()},
                )

    async def persist_state_patch(
        self, story: Any, draft: Any, entries: list[dict[str, Any]], now: Any,
    ) -> None:
        """上游 `persistStatePatch(story, draft, entries, now)`（`src/service.ts:6617`）。"""
        confidence = clamp_number(pick(draft, 'confidence'), 0, 0, 1)
        target = pick(draft, 'target')
        draft_source_ids = pick(draft, 'sourceEntryIds', 'source_entry_ids')
        if target == 'perspective':
            participant_id = ''
        else:
            participant_id = resolve_participant_id(
                pick(draft, 'participantId', 'participant_id'), draft_source_ids, _dual_entries(entries),
            )
        dimension = development_dimension(target, pick(draft, 'path') or '')
        path = 'development.%s' % dimension if dimension else ''
        source_entry_ids = explicit_evidence_ids(draft_source_ids, entries)[:20]
        proposed_value = clip(pick(draft, 'proposedValue', 'proposed_value'), 4_000)
        impact = 'major' if pick(draft, 'impact') == 'major' else 'minor'
        if not path or not proposed_value or not source_entry_ids:
            return
        # 关系观察必须留在它自己的具名分支里；私密材料不可能变成全局人格或世界变化。
        if target == 'relationship' and not participant_id:
            return
        if target == 'relationship' and any(
            entry.get('participantId') and entry.get('participantId') != participant_id
            for entry in entries if entry.get('id') in source_entry_ids
        ):
            return
        if target != 'relationship' and any(
            entry.get('participantId')
            for entry in entries if entry.get('id') in source_entry_ids
        ):
            return
        # 一条关系倾向只有在**真实的用户反馈**与主角随后已投递的回应支持同一种读法时
        # 才能投入使用。这防止某一场戏的散文节奏在对方尚未参与前就变成关于关系的断言。
        reception_supported = target != 'relationship' or reviewed_development_support(
            _dual_draft(draft), _dual_entries(entries), participant_id,
        )
        if not reception_supported:
            self.report_operation(
                'diagnostic', 'debug', story, 'advance',
                '关系发展提案暂不保留：缺少已接收反馈的交互证据 目标=%s/%s', target, path,
            )
            return
        story_id = pick(story, 'id')
        # 评估前先合并同一设定路径上的重复提案。
        candidates = await self.db_get('interlude_state_patch', {
            'storyId': story_id, 'participantId': participant_id, 'target': target, 'path': path,
        })
        matching = [
            candidate for candidate in candidates
            if patch_claims_match(candidate.get('proposedValue') or '', proposed_value)
        ]
        contradicts = _as_list(pick(draft, 'contradictsProposalIds', 'contradicts_proposal_ids'))
        for contradicted in candidates:
            if contradicted.get('status') not in ('proposed', 'applied'):
                continue
            if contradicted.get('id') not in contradicts:
                continue
            fresh = [
                item for item in source_entry_ids
                if item not in _as_list(contradicted.get('sourceEntryIds'))
            ]
            if not fresh:
                continue
            await self.db_set('interlude_state_patch', {'id': contradicted.get('id')}, {
                'confidence': max(0.0, (contradicted.get('confidence') or 0) - 0.2),
                'status': 'rejected',
                'sourceEntryIds': list(dict.fromkeys([
                    *_as_list(contradicted.get('sourceEntryIds')), *fresh,
                ])),
                'evidence': clip(merge_note(
                    contradicted.get('evidence'),
                    'Counter-evidence (%s): %s' % (
                        ','.join(str(item) for item in fresh), pick(draft, 'evidence'),
                    ),
                ), 4_000),
            })
        if contradicts:
            return
        if any(candidate.get('status') in ('applied', 'compacted') for candidate in matching):
            return
        candidate = next(
            (item for item in matching if item.get('status') == 'proposed'), None,
        )
        merged_source_entry_ids = list(dict.fromkeys([
            *_as_list(pick(candidate, 'sourceEntryIds') if candidate else []), *source_entry_ids,
        ]))[:80]
        source_rows = await self._entries_by_ids(story_id, merged_source_entry_ids)
        evidence = state_patch_evidence(
            _dual_entries(source_rows),
            pick(pick(story, 'setting') or {}, 'timezone'),
        )
        min_turns_raw = self._memory_raw('statePatchMinTurns', 'state_patch_min_turns')
        if min_turns_raw is None:
            min_turns_raw = self._memory_raw('statePatchMinEvidence', 'state_patch_min_evidence')
        minimum_turns = max(3, _config_int(min_turns_raw, 3))
        minimum_days = max(1, self._memory_int('statePatchMinDays', default=2))
        minimum = (
            self._memory('majorStatePatchConfidenceThreshold', default=0.95) if impact == 'major'
            else self._memory('statePatchConfidenceThreshold', default=0.82)
        )
        candidate_source_ids = _as_list(pick(candidate, 'sourceEntryIds') if candidate else [])
        old_scenes = development_scenes(_dual_entries([
            entry for entry in source_rows if entry.get('id') in candidate_source_ids
        ]))
        if candidate:
            merged_confidence = min(
                confidence,
                (candidate.get('confidence') or 0)
                + (0.05 if reception_supported and evidence['scenes'] > old_scenes else 0),
            )
        else:
            merged_confidence = confidence
        merged_evidence_text = merge_note(
            pick(candidate, 'evidence') if candidate else None, pick(draft, 'evidence'),
        )
        if candidate is None:
            proposal = await self.db_create('interlude_state_patch', {
                'storyId': story_id, 'participantId': participant_id, 'target': target,
                'path': path, 'proposedValue': proposed_value,
                'evidence': clip(merged_evidence_text, 4_000),
                'confidence': merged_confidence, 'impact': impact, 'status': 'proposed',
                'sourceEntryIds': merged_source_entry_ids, 'createdAt': now, 'appliedAt': None,
            })
        else:
            proposal = candidate
            await self.db_set('interlude_state_patch', {'id': candidate.get('id')}, {
                'evidence': clip(merged_evidence_text, 4_000),
                'confidence': merged_confidence,
                'impact': 'major' if (candidate.get('impact') == 'major' or impact == 'major') else 'minor',
                'sourceEntryIds': merged_source_entry_ids,
            })
        # 普通变化要求不同日期上的独立叙事回合。
        if not reception_supported:
            return
        if (
            candidate is None
            or evidence['scenes'] <= old_scenes
            or not self._memory_bool('autoApplyStatePatches')
            or (impact == 'major' and not self._memory_bool('allowMajorStateChanges'))
        ):
            return
        if impact == 'major':
            stable_evidence = merged_confidence >= minimum and evidence['scenes'] >= 2
        else:
            stable_evidence = (
                merged_confidence >= minimum
                and evidence['turns'] >= minimum_turns
                and evidence['days'] >= minimum_days
            )
        if not stable_evidence:
            self.report_operation(
                'diagnostic', 'debug', story, 'advance',
                'Overlay 候选继续累计 目标=%s/%s 回合=%d/%d 日期=%d/%d',
                target, path, evidence['turns'], minimum_turns, evidence['days'], minimum_days,
            )
            return
        cooldown_hours = max(1, self._memory_int('statePatchCooldownHours', default=72))
        recent_applied = None
        for item in candidates:
            if item.get('status') not in ('applied', 'compacted'):
                continue
            moment = _nullish(item.get('appliedAt'), item.get('createdAt'))
            if moment is None:
                continue
            if recent_applied is None or (_datetime(moment) or moment_zero()) > (_datetime(recent_applied) or moment_zero()):
                recent_applied = moment
        if recent_applied is not None and (
            dt_ms(parse_dt(now)) - dt_ms(parse_dt(recent_applied)) < cooldown_hours * _HOUR_MS
        ):
            self.report_operation(
                'diagnostic', 'debug', story, 'advance',
                'Overlay 冷却中，候选保留 目标=%s/%s 冷却=%d小时', target, path, cooldown_hours,
            )
            return
        # 新的倾向仍以来源记录存在，而不是在永久 Canon overlay 里累积。
        # 实时读取侧只挑选相关的那些。
        if pick(proposal, 'id') is not None:
            await self.db_set(
                'interlude_state_patch', {'id': pick(proposal, 'id')},
                {'status': 'applied', 'appliedAt': now},
            )


def moment_zero() -> datetime:
    """上游 `new Date(0)`。"""
    return datetime(1970, 1, 1, tzinfo=timezone.utc)
