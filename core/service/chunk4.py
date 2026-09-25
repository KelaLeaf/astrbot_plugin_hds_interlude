"""Chunk4：上游 `upstream/src/service.ts:3217-4185` 的全部 8 个成员。

| 上游成员 | 本文件 | 行 |
| --- | --- | --- |
| `advanceUnlocked` | `advance_unlocked` | 3217 |
| `decide` | `decide` | 3437 |
| `shouldRefreshContinuity` | `should_refresh_continuity` | 3604 |
| `planAutomaticTimeline` | `plan_automatic_timeline` | 3613 |
| `isTimelineDirectorFused` | `is_timeline_director_fused` | 3689 |
| `persistTimelineRetry` | `persist_timeline_retry` | 3698 |
| `tryDecide` | `try_decide` | 3720 |
| `persistDecision` | `persist_decision` | 3833 |

本块主题：**自动推进主循环 + 主模型回合（含时间导演/网页观察/恢复重写）+ 落库**。
`persistDecision` 是权重最大的一个成员：正文、投递草稿、剧本条目、记忆、意图、
状态补丁、Alter、Agency 与 Urge 的交接都在这里一次性落库。

## 键名约定（`docs/PORT_PLAN.md` §2「键名法」）

1. **数据库行**：列名逐字 camelCase（`normalize_database_row` 的产物），因此
   `story['setting']['timezone']` / `participant['state']` / `intent['notBefore']`
   这类读取一律 camelCase。
2. **`decode_story_state()` 的产物**：snake_case（`working_details` /
   `narrative_update_count` / `scene_presence` / `scene_frame` / `timeline_carry`
   / `agency_window` / `alter_system` / `working_detail_resolutions`）。
3. **发给模型的 `NarrativeRequest`**：**逐字保持上游 camelCase**
   （`recentEntries` / `sceneContext` / `dueIntents` / `chatCapabilities` /
   `writingOptions` / `userReportedTimes` …）：`narrator_prompts.system_prompt`
   明文告诉模型输出 `actionId` / `sendAt` / `replyTo` / `localMedia`，改键名即断协议。
4. **模型输出（不可信输入）**：一律 `pick()` 双读（优先 camelCase）。
5. **`normalize_decision()` 的产物**：以上游形状（camelCase）为主，并对
   **已知会被兄弟模块用 snake_case 读取**的键补上 snake_case 别名
   （`_dual()`），因为并行移植期已落地的读取方约定不一致：
   `script/commit_builder.py` 读 `cross_conversation_actions` / `browser_intents` /
   `follow_up_commitment` / `message_reactions` / `local_media` / `native_face`，
   而 `service/helpers.py` 的 `visible_reply_mode` / `requires_visible_reply_recovery`
   读 `crossConversationActions` / `groupReply`。两套读取方都在运行期生效，
   因此 `decision` 同时提供两种拼写（写多读一，不改变任何语义）。
   同理 `try_decide()` / `persist_decision()` 的返回值同时给出
   `timelinePlan`/`timeline_plan`、`scriptEntry`/`script_entry`。
6. **跨成员调用**一律 `self.other_method()`（MRO 解析）；本范围之外的成员
   （`get_story` / `append_entry` / `due_intents` / `send_outgoing_messages` …）
   都定义在别的 chunk 里，本文件只调用、不重复实现。

## 与 helpers.py 的关系

上游 `normalizeDecision`（`:7835`）、`createFactQuery`（`:8306`）、
`normalizeBrowserIntentDraft`（`:7896`）等是 `service.ts` 的**模块级**函数，
按分解契约归 `helpers.py`。它们尚未在该文件落地，本文件因此内置了逐字等价实现，
并**优先采用 `helpers.py` 的版本**（`_prefer_helper`）——helpers 一旦补齐，
这里自动让位，不会出现两份漂移的实现。

本模块不 import astrbot（`core/` 铁律），也不 import 任何兄弟 chunk。
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from datetime import datetime
from typing import Any, Optional

from ..alter import normalize_alter_value
from ..agency import (
    active_agency_window,
    evaluate_agency_capacity,
    normalize_agency_window_draft,
    normalize_proactive_contact,
    proactive_recheck_at,
)
from ..delivery import (
    attach_message_event,
    delivery_entry_metadata,
    prepare_outgoing_delivery,
    restore_message_event,
    script_event_payload,
)
from ..logging import phase_label
from ..narrator_prompts import compact_prompt_entries
from ..schedule_preplan import schedule_preplan_window
from ..script.authored_actions import inspect_say_markup, resolve_authored_actions
from ..script.commit_builder import (
    decision_to_script_commit,
    find_outgoing_script_event,
    unbound_immediate_message_events,
)
from ..script.development import development_context_query
from ..script.life_handoff import normalize_life_handoff
from ..script.recall_navigation import recall_focus
from ..script.scene_frame import advance_scene_frame, project_scene_frame, resolve_dialogue_burst
from ..script.timeline_routing import needs_timeline_director
from ..script.validator import validate_script_commit
from ..story_state import decode_story_state, encode_story_state, normalize_continuity_snapshot
from ..time import dt_ms, format_log_time, iso, parse_dt, utc_now
from ..turn_persistence import script_entry_draft_for_commit
from ..urge import commit_urge, normalize_urge_state, urge_burst_active
from .base import ServiceBase, pick
from .config import (
    TIMELINE_DIRECTOR_FUSE,
    TIMELINE_DIRECTOR_FUSE_COOLDOWN,
    TIMELINE_RETRY_BACKOFF_BASE,
)

try:  # pragma: no cover - helpers.py 由并行的移植任务产出/补齐
    from . import helpers as _helpers_module
except ImportError:  # pragma: no cover
    _helpers_module = None  # type: ignore[assignment]

from .helpers import (
    automatic_delivery_from_payload,
    clip,
    describe_timeline_plan_rejection,
    detect_live_script_time_overflow,
    extract_user_reported_times,
    group_due_intents,
    has_required_narrative_script,
    inferred_follow_up_commitment,
    interaction_promises_follow_up,
    is_automatic_narrative_phase,
    is_record,
    narrative_cursor,
    normalize_automatic_delivery_summary,
    normalize_follow_up_commitment,
    normalize_follow_up_resolutions,
    normalize_group_visible_reply,
    normalize_interaction,
    normalize_participant_state,
    normalize_timeline_plan,
    participant_relevance,
    pick_participant_state_patch,
    requires_visible_reply_recovery,
    safe_json_preview,
    timeline_entry_prompt_projection,
    to_date,
    visible_reply_mode,
)
# 上游 `normalizeVisibleMessageContent`（`service.ts` 模块级函数）在 helpers.py 里是
# 私有名；它决定跨账号主动联系的可见文本契约（去掉括号标签等），必须与群回复同源。
from .helpers import _normalize_visible_message_content as normalize_visible_message_content

__all__ = ['ServiceChunk4']

#: JS `Time.second` / `Time.minute` / `Time.day`（Koishi `Time` 工具）。
SECOND_MS = 1_000
MINUTE_MS = 60 * 1_000
DAY_MS = 24 * 60 * MINUTE_MS

#: 上游 `this.config.runtime.maxScriptCharacters` 等运行时默认值。
_DEFAULT_MAX_SCRIPT_CHARACTERS = 4_000
_DEFAULT_MAX_MESSAGE_CHARACTERS = 3_000


# =========================================================================== #
# 键名小工具（本块内部使用）
# =========================================================================== #

def _camel(name: str) -> str:
    parts = name.split('_')
    return parts[0] + ''.join(part[:1].upper() + part[1:] for part in parts[1:])


def _snake(name: str) -> str:
    return re.sub(r'(?<!^)(?=[A-Z])', '_', name).lower()


#: `decision` 上会被兄弟模块以 snake_case 读取的键（见模块 docstring 第 5 条）。
_DUAL_ELEMENT_KEYS = (
    'intents', 'memories', 'browserIntents', 'crossConversationActions',
    'followUpResolutions', 'messageReactions',
)

_DUAL_SINGLE_KEYS = (
    'followUpCommitment', 'localMedia', 'nativeFace', 'groupReply',
    'automaticDeliverySummary', 'agencyWindow', 'proactiveContact', 'statePatch',
    'authoredActions', 'lifeHandoff', 'intentUpdates',
)


def _dual(value: Any) -> Any:
    """给 dict 补上另一种拼写（camelCase ⇄ snake_case）；已是 list 时逐元素处理。

    只是让「读 camelCase 的模块」和「读 snake_case 的模块」都能拿到值，
    不改变任何取值语义。调用方只对本块自己构造的小对象使用，绝不递归进
    metadata / payload（那些是持久化 wire format，多出来的键会污染后续 prompt）。
    """
    if isinstance(value, list):
        return [_dual(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = dict(value)
    for key, item in list(result.items()):
        snake = _snake(key)
        camel = _camel(key)
        if snake != key:
            result.setdefault(snake, item)
        elif camel != key:
            result.setdefault(camel, item)
    return result


def _dual_decision(decision: dict[str, Any]) -> dict[str, Any]:
    """`normalize_decision()` 的收尾：顶层 + 已知跨模块字段补双拼写。"""
    result = dict(decision)
    for key in _DUAL_SINGLE_KEYS:
        if key in result and result[key] is not None:
            result[_snake(key)] = result[key]
    for key in _DUAL_ELEMENT_KEYS:
        value = result.get(key)
        if isinstance(value, list) and value:
            aliased = [_dual(item) for item in value]
            result[key] = aliased
            result[_snake(key)] = aliased
    # `interaction.reply.sendAt`：commit_builder 按 snake_case 读 `send_at`。
    interaction = result.get('interaction')
    if isinstance(interaction, dict):
        reply = interaction.get('reply')
        if isinstance(reply, dict) and 'sendAt' in reply:
            result['interaction'] = {
                **interaction,
                'reply': {**reply, 'send_at': reply.get('send_at', reply['sendAt'])},
            }
    return result


def _prefer_helper(name: str, fallback: Any) -> Any:
    """优先用 `helpers.py` 的同名实现；缺失时用本文件的逐字等价实现。"""
    if _helpers_module is not None:
        candidate = getattr(_helpers_module, name, None)
        if callable(candidate):
            return candidate
    return fallback


def _section(config: Any, name: str) -> dict[str, Any]:
    """读配置段（dict 双读 / 对象走属性），永远返回 dict。"""
    value: Any
    if isinstance(config, dict):
        value = pick(config, name, _snake(name))
    else:
        value = getattr(config, name, None)
        if value is None:
            value = getattr(config, _snake(name), None)
    return value if isinstance(value, dict) else {}


def _cfg(section: Any, camel: str, default: Any = None) -> Any:
    """读配置项：双读 + `?? default` 语义（`None` 即上游 `undefined`）。"""
    value = pick(section, camel, _snake(camel)) if isinstance(section, dict) else None
    return default if value is None else value


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _automation(story: Any) -> dict[str, Any]:
    """`story.state.automation`（行是 camelCase 桩，内部键可能是 snake_case）。"""
    return _record(_record(_record(story).get('state')).get('automation'))


def _timezone(story: Any) -> str:
    return str(_record(_record(story).get('setting')).get('timezone') or '')


def _raw_decision(decision: Any, camel: str) -> Any:
    """从模型原始输出里读字段（双读，优先 camelCase）。"""
    return pick(decision, camel, _snake(camel)) if isinstance(decision, dict) else None


def _is_finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


async def _db_set_by_ids(service: Any, table: str, ids: list[int], patch: dict[str, Any]) -> None:
    """上游 `dbSet(table, { id: { $in: ids } }, patch)` 的等价物。

    `plugin/core/database.py` 的 `update()` 只支持等值 where，因此在 Python 侧
    逐条更新（写队列仍由 `db_set()` 串行化，语义与一次批量更新一致）。
    """
    for identifier in ids:
        await service.db_set(table, {'id': identifier}, dict(patch))


def _resolved(value: Any) -> Any:
    """`asyncio.gather` 里的常量值（等价上游 `Promise.resolve(x)`）。"""
    async def _value() -> Any:
        return value

    return _value()


# =========================================================================== #
# helpers.py 尚未落地的模块级函数（本文件内置等价实现）
# =========================================================================== #

def _valid_memory(value: Any) -> bool:
    """上游 `validMemory`（`:7986`）。"""
    return (
        is_record(value)
        and isinstance(value.get('category'), str)
        and isinstance(value.get('content'), str)
        and bool(value['content'].strip())
    )


def _is_active_consequence_draft(intent: Any) -> bool:
    """上游 `isActiveConsequenceDraft`（`:8028`）。"""
    if not is_record(intent):
        return False
    return intent.get('type') == 'active-consequence' and _record(intent.get('payload')).get('lifecycle') == 'active'


def _consequence_expires_at(payload: Any) -> Optional[datetime]:
    """上游 `consequenceExpiresAt`（`:8033`）。"""
    return to_date(_record(payload).get('expiresAt')) if is_record(payload) else None


def _valid_intent(value: Any, from_dt: datetime, now: datetime, memory: Any = None) -> bool:
    """上游 `validIntent`（`:7990`）：意图必须是**未来**的事，后果型另有窄契约。"""
    if not is_record(value) or not isinstance(value.get('type'), str) or not isinstance(value.get('summary'), str):
        return False
    not_before = to_date(value.get('notBefore'))
    if not_before is None:
        return False
    if not _is_active_consequence_draft(value):
        return dt_ms(not_before) > dt_ms(now)
    payload = _record(value.get('payload'))
    effect = payload.get('effect').strip() if isinstance(payload.get('effect'), str) else ''
    strength = payload.get('strength')
    maximum_lifetime = max(1, int(_cfg(memory, 'activeConsequenceMaxDays', 7))) * DAY_MS
    expires_at = _consequence_expires_at(payload)
    return bool(
        _cfg(memory, 'activeConsequencesEnabled', False)
        and effect
        and (strength is None or (_is_finite_number(strength) and 0 <= float(strength) <= 1))
        and dt_ms(not_before) <= dt_ms(now)
        and dt_ms(not_before) >= dt_ms(from_dt)
        and expires_at is not None
        and dt_ms(expires_at) > dt_ms(now)
        and dt_ms(expires_at) - dt_ms(now) <= maximum_lifetime
    )


def _permitted_or_global(value: Any, fallback: str, permitted_participant_ids: set[str]) -> str:
    """上游 `permittedOrGlobal`（`:8065`）。"""
    candidate = value.strip() if isinstance(value, str) else ''
    if candidate and candidate in permitted_participant_ids:
        return candidate
    return fallback if fallback and fallback in permitted_participant_ids else ''


def _normalize_intent_updates(value: Any) -> list[dict[str, Any]]:
    """上游 `normalizeIntentUpdates`（`:8011`）：最多 8 条，id 必须为正整数。"""
    if not isinstance(value, list):
        return []
    updates: list[dict[str, Any]] = []
    for item in value:
        if not is_record(item):
            continue
        identifier = item.get('id')
        if isinstance(identifier, bool) or not isinstance(identifier, int) or identifier <= 0:
            continue
        if item.get('status') not in ('completed', 'cancelled'):
            continue
        entry: dict[str, Any] = {'id': identifier, 'status': item['status']}
        resolution = item.get('resolution')
        if isinstance(resolution, str) and resolution.strip():
            entry['resolution'] = clip(resolution, 1_000)
        updates.append(entry)
    return updates[:8]


def _normalize_conversation_action(
    value: Any,
    runtime: dict[str, Any],
    permitted_participant_ids: set[str],
    current_participant_id: str,
    now: Optional[datetime] = None,
    proactive: bool = False,
) -> Optional[dict[str, Any]]:
    """上游 `normalizeConversationAction`（`:8045`）。"""
    at = now if now is not None else utc_now()
    participant_id = pick(value, 'participantId', 'participant_id') if is_record(value) else None
    if not isinstance(participant_id, str) or not participant_id or participant_id == current_participant_id:
        return None
    mode = pick(value, 'mode')
    if participant_id not in permitted_participant_ids or mode not in ('immediate', 'delayed'):
        return None
    raw_content = pick(value, 'content')
    content = (
        normalize_visible_message_content(
            raw_content,
            int(_cfg(runtime, 'maxMessageCharacters', _DEFAULT_MAX_MESSAGE_CHARACTERS)),
            str(_cfg(runtime, 'messageSeparator', '<sep/>')),
        )
        if isinstance(raw_content, str) else ''
    )
    if not content:
        return None
    willingness_raw = pick(value, 'willingness')
    willingness = _clamp(float(willingness_raw), 0.0, 1.0) if _is_finite_number(willingness_raw) else None
    threshold = _cfg(runtime, 'proactiveWillingnessThreshold', 0.65)
    if proactive and (willingness is None or willingness < float(threshold)):
        return None
    reason_raw = pick(value, 'reason')
    reason = clip(reason_raw, 300) if isinstance(reason_raw, str) else None
    if mode == 'immediate':
        action: dict[str, Any] = {'participantId': participant_id, 'mode': mode, 'content': content}
        if willingness is not None:
            action['willingness'] = willingness
        if reason:
            action['reason'] = reason
        return action
    send_at = to_date(pick(value, 'sendAt', 'send_at'))
    delay = dt_ms(send_at) - dt_ms(at) if send_at is not None else None
    if (
        send_at is None or delay is None
        or delay < float(_cfg(runtime, 'minimumDelayedReplySeconds', 0)) * 1_000
        or delay > float(_cfg(runtime, 'maximumDelayedReplyMinutes', 0)) * MINUTE_MS
    ):
        return None
    action = {'participantId': participant_id, 'mode': mode, 'content': content, 'sendAt': iso(send_at)}
    if willingness is not None:
        action['willingness'] = willingness
    if reason:
        action['reason'] = reason
    return action


def _normalize_browser_intent_draft_loose(value: Any) -> Optional[dict[str, Any]]:
    """上游 `normalizeBrowserIntentDraftLoose`（`:7881`）。"""
    if not is_record(value) or value.get('mode') not in ('search', 'visit'):
        return None
    if not isinstance(value.get('purpose'), str):
        return None
    query = clip(value.get('query'), 500) if isinstance(value.get('query'), str) else ''
    url = clip(value.get('url'), 2_000) if isinstance(value.get('url'), str) else ''
    if value['mode'] == 'search' and not query:
        return None
    if value['mode'] == 'visit' and not url:
        return None
    draft: dict[str, Any] = {'mode': value['mode'], 'purpose': clip(value['purpose'], 500)}
    if query:
        draft['query'] = query
    if url:
        draft['url'] = url
    draft['timing'] = 'immediate' if pick(value, 'timing') == 'immediate' else 'deferred'
    raw_participant = pick(value, 'participantId', 'participant_id')
    if isinstance(raw_participant, str):
        draft['participantId'] = raw_participant.strip()
    return draft


def _normalize_browser_intent_draft(draft: Any, config: Any) -> Optional[dict[str, Any]]:
    """上游 `normalizeBrowserIntentDraft`（`:7896`）：再按配置闸门过滤。"""
    normalized = _normalize_browser_intent_draft_loose(draft)
    if normalized is None:
        return None
    if normalized['mode'] == 'search' and not _cfg(config, 'allowSearch', False):
        return None
    if normalized['mode'] == 'visit' and not _cfg(config, 'allowVisit', False):
        return None
    return normalized


def _create_fact_query(
    participant: Any,
    user_message: Any,
    due_intents: list[dict[str, Any]],
    superseded_intents: list[dict[str, Any]],
) -> str:
    """上游 `createFactQuery`（`:8306`）：语义召回的事实查询串。"""
    state = normalize_participant_state(_record(participant).get('state')) if participant else None
    parts: list[str] = []
    if user_message:
        parts.append('Current user message: %s' % user_message)
    if state:
        parts.extend('Open thread: %s' % thread for thread in state.get('openThreads') or [])
        parts.extend('Relationship note: %s' % note for note in state.get('relationshipNotes') or [])
    parts.extend('Due intent: %s' % pick(intent, 'summary') for intent in due_intents)
    parts.extend('Superseded plan: %s' % pick(intent, 'summary') for intent in superseded_intents)
    return '\n'.join(part for part in parts if part)


def _normalize_decision(
    raw: Any,
    from_dt: datetime,
    now: datetime,
    permit_messages: bool,
    runtime: dict[str, Any],
    shared: dict[str, Any],
    current_participant_id: str,
    permitted_participant_ids: set[str],
    phase: str = 'advance',
    memory: Any = None,
    refresh_continuity: bool = False,
) -> dict[str, Any]:
    """上游 `normalizeDecision`（`:7835`）：把模型输出裁成可信的决策对象。"""
    separator = str(_cfg(runtime, 'messageSeparator', '<sep/>'))
    raw = _dual(raw) if isinstance(raw, dict) else {}
    raw = resolve_authored_actions(raw, False, separator)

    script_raw = raw.get('script') if isinstance(raw, dict) else None
    script = (
        clip(script_raw.strip(), int(_cfg(runtime, 'maxScriptCharacters', _DEFAULT_MAX_SCRIPT_CHARACTERS)))
        if isinstance(script_raw, str) else ''
    )
    # 私聊可见回复的唯一通道是 interaction；自动生活回合没有实时事件，不能产生它。
    interaction = None if phase == 'advance' else normalize_interaction(
        _raw_decision(raw, 'interaction'), now, runtime,
    )
    # 可选记忆是模型建议，不是事实来源：绝不允许模型把「编造的线上联系」变成长期检索数据。
    memories_raw = _raw_decision(raw, 'memories')
    memories = (
        [
            {**memory_item, 'participantId': _permitted_or_global(
                pick(memory_item, 'participantId', 'participant_id'),
                current_participant_id, permitted_participant_ids,
            )}
            for memory_item in memories_raw if _valid_memory(memory_item)
        ]
        if isinstance(memories_raw, list) else []
    )
    intents_raw = _raw_decision(raw, 'intents')
    intents: list[dict[str, Any]] = []
    if isinstance(intents_raw, list):
        for intent in intents_raw:
            if is_record(intent) and intent.get('type') == 'follow-up-commitment':
                continue
            if not _valid_intent(intent, from_dt, now, memory):
                continue
            intents.append({**intent, 'participantId': _permitted_or_global(
                pick(intent, 'participantId', 'participant_id'),
                current_participant_id, permitted_participant_ids,
            )})
    intents = intents[:8]
    intent_updates = _normalize_intent_updates(_raw_decision(raw, 'intentUpdates'))
    browser_raw = _raw_decision(raw, 'browserIntents')
    browser_intents = (
        [item for item in (_normalize_browser_intent_draft_loose(value) for value in browser_raw) if item][:1]
        if isinstance(browser_raw, list) else []
    )
    proactive = phase == 'advance'
    agency_gated_proactive = proactive and not is_record(_raw_decision(raw, 'proactiveContact'))
    cross_raw = _raw_decision(raw, 'crossConversationActions')
    cross_conversation_actions: list[dict[str, Any]] = []
    if permit_messages and _cfg(shared, 'allowCrossConversationMessages', False) and isinstance(cross_raw, list):
        for action in cross_raw:
            normalized = _normalize_conversation_action(
                action, runtime, permitted_participant_ids, current_participant_id, now, agency_gated_proactive,
            )
            if normalized is not None:
                cross_conversation_actions.append(normalized)
        cross_conversation_actions = cross_conversation_actions[:max(0, int(_cfg(shared, 'maxCrossConversationActions', 0)))]
    state_patch_raw = _raw_decision(raw, 'statePatch')
    state_patch = pick_participant_state_patch(state_patch_raw) if is_record(state_patch_raw) else None
    continuity = normalize_continuity_snapshot(_raw_decision(raw, 'continuity')) if refresh_continuity else None
    alter = normalize_alter_value(_raw_decision(raw, 'alter'))
    automatic_delivery_summary = (
        normalize_automatic_delivery_summary(_raw_decision(raw, 'automaticDeliverySummary')) or None
        if is_automatic_narrative_phase(phase) else None
    )
    follow_up_commitment = (
        normalize_follow_up_commitment(_raw_decision(raw, 'followUpCommitment'), now)
        if phase == 'user-message' else None
    )
    follow_up_resolutions = (
        normalize_follow_up_resolutions(_raw_decision(raw, 'followUpResolutions'))
        if phase in ('user-message', 'intent-due') else []
    )
    proactive_raw = _raw_decision(raw, 'proactiveContact')
    agency_window_raw = _raw_decision(raw, 'agencyWindow')
    decision = {
        'script': script,
        'authoredActions': _raw_decision(raw, 'authoredActions'),
        'lifeHandoff': normalize_life_handoff(_raw_decision(raw, 'lifeHandoff'), script),
        'alter': alter,
        'agencyWindow': agency_window_raw if is_record(agency_window_raw) else None,
        'proactiveContact': proactive_raw if is_record(proactive_raw) else None,
        'interaction': interaction,
        'automaticDeliverySummary': automatic_delivery_summary,
        'followUpCommitment': follow_up_commitment,
        'followUpResolutions': follow_up_resolutions,
        'continuity': continuity,
        'memories': memories,
        'intents': intents,
        'intentUpdates': intent_updates,
        'browserIntents': browser_intents,
        'statePatch': state_patch,
        'crossConversationActions': cross_conversation_actions,
    }
    # 逐字保留模型原文里本成员读到的传输字段（上游 `raw.groupReply` 等）。
    for key in ('groupReply', 'messageReactions', 'localMedia', 'nativeFace', 'urge'):
        value = _raw_decision(raw, key)
        decision[key] = value if value is not None else ([] if key == 'messageReactions' else None)
    return _dual_decision(decision)


# helpers.py 一旦落地同名实现，这里自动让位（保持单一真相来源）。
_normalize_decision = _prefer_helper('normalize_decision', _normalize_decision)
_create_fact_query = _prefer_helper('create_fact_query', _create_fact_query)
_normalize_browser_intent_draft = _prefer_helper('normalize_browser_intent_draft', _normalize_browser_intent_draft)
_normalize_browser_intent_draft_loose = _prefer_helper(
    'normalize_browser_intent_draft_loose', _normalize_browser_intent_draft_loose,
)


# =========================================================================== #
# ServiceChunk4
# =========================================================================== #

class ServiceChunk4(ServiceBase):
    """对应 `upstream/src/service.ts` 第 3217–4185 行的成员。"""

    # ------------------------------------------------------------------ #
    # advanceUnlocked（上游 :3217）
    # ------------------------------------------------------------------ #

    async def advance_unlocked(self, story: Any, now: datetime, force: bool) -> list[dict[str, Any]]:
        """上游 `advanceUnlocked(story, now, force)`（`:3217`）。

        自动推进主循环：先补投递已决定的分段消息与网页观察，再处理时间导演重试
        闸门，最后按「到期计划 / 自动生活」二选一开启一次写作回合。
        """
        runtime = self.runtime_config
        urge_config = self.urge_config
        urge_mode = json.dumps(urge_config, ensure_ascii=False) if _cfg(urge_config, 'enabled', False) else 'off'
        previous_urge = normalize_urge_state(
            _record(_record(decode_story_state(story.get('state')).get('extensions')).get('urge')),
            dt_ms(now),
        )
        if previous_urge.get('mode') != urge_mode and (
            _cfg(urge_config, 'enabled', False)
            or (previous_urge.get('mode') and previous_urge.get('mode') != 'off')
        ):
            state = decode_story_state(story.get('state'))
            extensions = dict(state.get('extensions') or {})
            next_urge = dict(previous_urge)
            next_urge['mode'] = urge_mode
            next_urge.pop('armed', None)
            next_urge.pop('burst', None)
            next_urge['pace'] = 'normal'
            extensions['urge'] = next_urge
            await self.db_set(
                'interlude_story', {'id': story['id']},
                {'state': encode_story_state({**state, 'extensions': extensions})},
            )
            await self.schedule_next_automatic_advance(story['id'], now)
            story = await self.get_story(story['id'])

        cursor_from = narrative_cursor(story, now)
        elapsed = max(0, dt_ms(now) - dt_ms(cursor_from))
        due = await self.due_intents(story['id'], now)
        messages: list[dict[str, Any]] = []

        # 后续 `<sep/>` 气泡是投递事件而不是新的写作回合：只在真正发出时才落库，
        # 这样更新的来信仍能取消「还在打字」的那一条。每次唤醒最多投递一段；
        # 调度被阻塞很久时一次性补发全部过期分段会跳过配置的打字时长模拟。
        max_message_characters = int(_cfg(runtime, 'maxMessageCharacters', _DEFAULT_MAX_MESSAGE_CHARACTERS))
        split_segments = sorted(
            [intent for intent in due if intent.get('type') == 'split-message'],
            key=lambda intent: dt_ms(to_date(intent.get('notBefore')) or now),
        )[:1]
        split_handled = False
        for intent in split_segments:
            payload = _record(intent.get('payload'))
            content = clip(payload.get('content'), max_message_characters)
            automatic_delivery = automatic_delivery_from_payload(intent.get('payload'))
            participant = await self.get_participant(intent['participantId']) if intent.get('participantId') else None
            if intent.get('participantId') and intent['participantId'] in self.interrupted_typing_participants:
                continue
            split_handled = True
            if not content or not participant or participant.get('status') != 'active':
                await self.db_set('interlude_intent', {'id': intent['id']}, {'status': 'cancelled', 'updatedAt': now})
                reference = restore_message_event(intent.get('payload'), content or '')
                if reference:
                    await self.update_script_delivery_outcome(
                        story['id'], reference, 'cancelled', now, 'delivery-target-unavailable',
                    )
                continue
            message: dict[str, Any] = {
                'participant_id': participant['id'],
                'content': content,
                'automatic_delivery': automatic_delivery,
                'script_event': restore_message_event(intent.get('payload'), content),
            }
            delivered = await self.send_outgoing_messages(
                story, [message], None, None,
                lambda target: target.get('id') in self.interrupted_typing_participants,
                False,
            )
            if not delivered:
                if participant['id'] in self.interrupted_typing_participants:
                    continue
                if message.get('script_event'):
                    await self.update_script_delivery_outcome(
                        story['id'], message['script_event'], 'pending', now, 'delivery-unconfirmed-retry-scheduled',
                    )
                retry_at = parse_dt(dt_ms(now) + 30 * SECOND_MS)
                await self.db_set('interlude_intent', {'id': intent['id']}, {'notBefore': retry_at, 'updatedAt': now})
                self.schedule_due_intent_wake(story['id'], retry_at)
                continue
            await self.append_entry(story['id'], {
                'kind': 'character-message',
                'actor': 'character',
                'content': content,
                'occurred_at': iso(now),
                'metadata': delivery_entry_metadata(message, {'splitSegment': True}),
            }, now, participant['id'])
            if message.get('script_event'):
                await self.update_script_delivery_outcome(story['id'], message['script_event'], 'delivered', now)
            if automatic_delivery:
                await self.record_automatic_delivery(story['id'], participant['id'], automatic_delivery, now)
            await self.record_character_message(participant, now)
            await self.db_set('interlude_intent', {'id': intent['id']}, {'status': 'completed', 'updatedAt': now})
        if split_handled:
            await self.schedule_next_split_wake(story['id'])
        due = [intent for intent in due if intent.get('type') != 'split-message']

        # 浏览器研究一旦完成就是「已经发生过的外部观察」，绝不能当作未来的普通计划
        # 交给叙事器（否则模型会在 Puppeteer 真正读过之前就写「我看过那页」）。
        # 同时限制每轮扫描的页数，避免积压的可选研究把一次后台扫描拖成多次串行加载。
        max_research = max(1, int(_cfg(self.browser_config, 'maxResearchPerSweep', 1)))
        browser_intents = [intent for intent in due if intent.get('type') == 'browser-research'][:max_research]
        for intent in browser_intents:
            await self.execute_deferred_browser_intent(story, intent, now)
        # executeDeferredBrowserIntent() 总会结清（成功或记录失败），
        # 所以这里重读整个待办列表只会给每次后台扫描白加一次 SQLite 往返。
        due = [intent for intent in due if intent.get('type') != 'browser-research']

        # 只为「时间导演重试」限速：持久化的重试闸门若时间窗口起点未变，
        # 说明上一次扫描没有消费游标（例如又一次失败），此时应等冷却而不是
        # 每轮都重进 tryDecide。成功的无账本降级会推进游标并自然清掉闸门；
        # 手动推进（force）仍是运维的显式逃生口。
        automation = _automation(story)
        timeline_retry_at = to_date(pick(automation, 'timelineRetryAt', 'timeline_retry_at'))
        timeline_retry_from = pick(automation, 'timelineRetryFrom', 'timeline_retry_from')
        timezone = _timezone(story)
        if (
            not force and timeline_retry_at is not None
            and timeline_retry_from == iso(cursor_from) and dt_ms(timeline_retry_at) > dt_ms(now)
        ):
            self.report_operation(
                'diagnostic', 'debug', story, 'advance',
                '自动推进等待时间导演重试 冷却至=%s 时间窗口起点=%s',
                format_log_time(timeline_retry_at, timezone), format_log_time(cursor_from, timezone),
            )
            self.schedule_due_intent_wake(story['id'], timeline_retry_at)
            return messages
        if timeline_retry_at is not None and (
            dt_ms(timeline_retry_at) <= dt_ms(now) or timeline_retry_from != iso(cursor_from)
        ):
            cleared = {
                key: value for key, value in automation.items()
                if key not in ('timelineRetryAt', 'timeline_retry_at', 'timelineRetryFrom', 'timeline_retry_from')
            }
            story = {**story, 'state': {**_record(story.get('state')), 'automation': cleared}}

        # 关闭自动推进必须压制**所有**后台写作路径，包括关闭前就已持久化的短程计划。
        # 手动 `interlude.advance` 仍会传 `force`，保持可用。
        auto_advance_enabled = bool(_cfg(self.auto_advance_config, 'enabled', False))
        due_follow_ups = self.due_conversation_follow_ups(story, now) if auto_advance_enabled else []
        automatic_due = auto_advance_enabled and (
            len(due_follow_ups) > 0 or self.is_automatic_advance_due(story, now)
        )
        paused_for_conversation = self.is_automatic_advance_paused(story, now)
        self.report_operation(
            'diagnostic', 'debug', story, 'advance',
            '后台状态 到期计划=%d 分段消息=%d 网页任务=%d 短期跟进=%d 自动推进到期=%s 对话暂停=%s',
            len(due), len(split_segments), len(browser_intents), len(due_follow_ups),
            automatic_due, paused_for_conversation,
        )
        # 到期的打字分段可以在对话暂停期投递：它已经是承诺过的消息，不是自动生活更新。
        if not force and not due and (not automatic_due or paused_for_conversation):
            return messages

        # 手动推进可能排在一次后台扫描之后：不要为几秒空白时间再开一次叙事回合。
        minimum_manual_advance_ms = max(1, int(_cfg(runtime, 'minimumAdvanceMinutes', 1))) * MINUTE_MS
        manual_advance_too_soon = (
            force and not due and not due_follow_ups and elapsed < minimum_manual_advance_ms
        )
        if manual_advance_too_soon:
            self.report_operation(
                'standard', 'info', story, 'advance',
                '手动推进跳过：游标距离现在不足 %d 分钟，且没有到期计划或对话后续任务',
                int(_cfg(runtime, 'minimumAdvanceMinutes', 1)),
            )
            return messages

        advanced = False
        delayed_reply_processed = False
        # 一条到期计划本身就是一次完整写作回合：它补齐旧游标→现在的时间缺口，
        # 然后再决定那条计划。避免先做一次普通推进，否则下一次请求会把
        # 同一个 now→now 时刻再写一遍。
        has_narrative_due = len(due) > 0
        if elapsed > 0 and not has_narrative_due and (force or (automatic_due and not paused_for_conversation)):
            follow_up_participant_id = (
                pick(_automation(story), 'conversationFollowUpParticipantId', 'conversation_follow_up_participant_id') or ''
            ) if due_follow_ups else ''
            follow_up_participant = await self.get_participant(follow_up_participant_id) if follow_up_participant_id else None
            phase = 'conversation-follow-up' if _record(follow_up_participant).get('status') == 'active' else 'advance'
            self.report_operation(
                'standard', 'info', story, phase,
                '即将执行自动写作 类型=%s 时间段=%s→%s',
                phase_label(phase), format_log_time(cursor_from, timezone), format_log_time(now, timezone),
            )
            result = await self.try_decide(
                story, follow_up_participant, phase, cursor_from, now, None, [],
            )
            if result['succeeded']:
                permit_messages = phase == 'conversation-follow-up' or bool(
                    _cfg(runtime, 'allowProactiveMessages', False)
                )
                persisted = await self.persist_decision(
                    story, follow_up_participant, result['decision'], cursor_from, now,
                    permit_messages, phase, [], False, result.get('timelinePlan'),
                )
                messages.extend(persisted['messages'])
                await self.db_set('interlude_story', {'id': story['id']}, {'cursorAt': now, 'updatedAt': now})
                advanced = True

        due_batches = group_due_intents(due)
        # 共享剧本只有一个时钟。每轮只处理一条关系分支，避免另一条分支触发重复的
        # now→now 场景。
        due_batch = due_batches[0] if due_batches else None
        if due_batch:
            current = await self.get_story(story['id'])
            # 如果本轮没有先做 automatic advance，到期意图也必须从故事游标补写到现在；
            # 否则「延迟回复到点」会漏掉中间这段角色生活。
            due_from = narrative_cursor(current, now)
            # 每一组就是一条关系分支：既保持 prompt 私密，又能排空本轮已到期的全部计划。
            due_participant_id = (due_batch[0].get('participantId') if due_batch else '') or ''
            due_participant = await self.get_participant(due_participant_id) if due_participant_id else None
            self.report_operation(
                'standard', 'info', current, 'intent-due',
                '即将处理到期计划 数量=%d 类型=%s 参与者=%s',
                len(due_batch),
                ','.join(sorted({str(intent.get('type')) for intent in due_batch})),
                _record(due_participant).get('id') or '全局',
            )
            result = await self.try_decide(
                current, due_participant, 'intent-due', due_from, now, None, due_batch,
            )
            decision = result['decision']
            succeeded = result['succeeded']
            stream_recovery = all(
                intent.get('type') == 'narrative-retry'
                and pick(_record(intent.get('payload')), 'streamRecovery', 'stream_recovery') is True
                for intent in due_batch
            )
            recovered = (
                await self.persist_stream_script_recovery(current, due_participant, decision, now)
                if stream_recovery and succeeded else False
            )
            turn_succeeded = recovered if stream_recovery else succeeded
            if not stream_recovery:
                permit_messages = bool(_cfg(runtime, 'allowProactiveMessages', False)) or any(
                    pick(_record(intent.get('payload')), 'userInitiated', 'user_initiated') is True
                    for intent in due_batch
                )
                persisted = await self.persist_decision(
                    current, due_participant, decision, due_from, now,
                    permit_messages, 'intent-due', due_batch, False, result.get('timelinePlan'),
                )
                messages.extend(persisted['messages'])
            if turn_succeeded:
                await self.db_set('interlude_story', {'id': current['id']}, {'cursorAt': now, 'updatedAt': now})
                ordinary_due_ids = [
                    intent['id'] for intent in due_batch if intent.get('type') != 'follow-up-commitment'
                ]
                if ordinary_due_ids:
                    await _db_set_by_ids(
                        self, 'interlude_intent', ordinary_due_ids, {'status': 'completed', 'updatedAt': now},
                    )
                if any(intent.get('type') == 'delayed-reply' for intent in due_batch):
                    delayed_reply_processed = True
                    await self.pause_automatic_advance_after_delayed_reply(
                        story['id'], now, _record(due_participant).get('id') or '',
                    )
                elif not advanced and not delayed_reply_processed:
                    await self.schedule_next_automatic_advance(story['id'], now)
            else:
                # 失败的用户回合要留下持久化重试，否则一次瞬时 403/5xx 会让已经记下的
                # 来信永远等着「有人再发一条私聊」。
                retries = [intent for intent in due_batch if intent.get('type') == 'narrative-retry']
                if retries:
                    attempts = max(
                        int(pick(_record(intent.get('payload')), 'attempt') or 0) for intent in retries
                    )
                    await _db_set_by_ids(
                        self, 'interlude_intent', [intent['id'] for intent in retries],
                        {'status': 'cancelled', 'updatedAt': now},
                    )
                    if stream_recovery:
                        await self.schedule_stream_script_recovery(
                            current['id'], _record(due_participant).get('id') or '', now, attempts,
                        )
                    else:
                        await self.schedule_narrative_retry(
                            current['id'], _record(due_participant).get('id') or '', now, attempts,
                        )
                # 普通延迟计划继续保持 pending，等 provider 恢复。
        if len(due_batches) > 1:
            current = await self.get_story(story['id'])
            self.report_operation(
                'standard', 'info', current, 'intent-due',
                '其余 %d 组到期计划已保留，下一次扫描将按新的时间段继续处理', len(due_batches) - 1,
            )
            self.schedule_due_intent_wake(
                story['id'],
                parse_dt(dt_ms(now) + max(SECOND_MS, int(_cfg(runtime, 'sweepIntervalMinutes', 1)) * MINUTE_MS)),
            )
        if advanced and not delayed_reply_processed:
            has_more_follow_ups = len(due_follow_ups) > 0 and await self.complete_conversation_follow_ups(
                story['id'], now,
            )
            if not has_more_follow_ups:
                await self.schedule_next_automatic_advance(story['id'], now)
        return messages

    # ------------------------------------------------------------------ #
    # decide（上游 :3437）
    # ------------------------------------------------------------------ #

    async def decide(
        self,
        story: Any,
        participant: Any,
        phase: str,
        from_: datetime,
        now: datetime,
        user_message: Any,
        due_intents: list[dict[str, Any]],
        superseded_intents: Optional[list[dict[str, Any]]] = None,
        group_context: Any = None,
        images: Optional[list[dict[str, Any]]] = None,
        audio: Optional[list[dict[str, Any]]] = None,
        extra_web_context: Optional[list[dict[str, Any]]] = None,
        output_recovery: bool = False,
        chat_capabilities: Any = None,
        quoted_messages: Optional[list[dict[str, Any]]] = None,
        sticker_catalog: Optional[list[dict[str, Any]]] = None,
        turn_query_embedding: Optional[list[float]] = None,
        visual_observations: Optional[list[str]] = None,
        timeline_plan: Any = None,
        on_early_reply: Any = None,
    ) -> dict[str, Any]:
        """上游 `decide(story, participant, phase, from, now, ...)`（`:3437`）。

        主模型上下文的**唯一入口**。返回的 `NarrativeRequest` 是**发给模型的 wire
        format**：顶层与嵌套键全部保持上游 camelCase（见模块 docstring 第 3 条）。
        """
        superseded_intents = superseded_intents or []
        images = images or []
        audio = audio or []
        extra_web_context = extra_web_context or []
        quoted_messages = quoted_messages or []
        sticker_catalog = sticker_catalog or []
        visual_observations = visual_observations or []
        runtime = self.runtime_config
        shared = self.shared_story_config
        memory = self.memory_config
        participant_id = _record(participant).get('id')
        separator = _cfg(runtime, 'messageSeparator', '<sep/>')

        # 用户回合与到期回合可能先于下一次后台扫描到来：顺手在这里淘汰过期后果，
        # 这仍是一次廉价的本地数据库操作，而不是额外的模型请求。
        fact_query = _create_fact_query(participant, user_message, due_intents, superseded_intents)
        memory_enabled = bool(_cfg(memory, 'enabled', False))
        embedding_config = _record(_cfg(_section(self.config, 'model'), 'embedding'))
        # 一次回合级查询向量服务全部语义消费者（表情过滤、事实排序、历史召回）；
        # 没有消费者需要时就是 undefined。
        resolved_turn_embedding = turn_query_embedding
        if (
            resolved_turn_embedding is None and participant and user_message and user_message.strip()
            and self.semantic_turn_embedding_enabled()
        ):
            resolved_turn_embedding = await self.embed_text(
                user_message.strip()[:int(_cfg(embedding_config, 'maxInputCharacters', 4_000))],
            )
        live_fact_embedding = resolved_turn_embedding if _cfg(embedding_config, 'liveQuery', False) else None
        participant_id_for_agency = participant_id or ''
        (
            recent_entries, memories, scene, arc, previous_scenes, facts, all_participants,
            web_context, active_consequences, overlay_snapshots, follow_up_commitments,
            schedule_record, upcoming_intents,
        ) = await asyncio.gather(
            # 实况路径必须用 runtime 上限：它们就是给测试者看的「上下文条目/长期事实」。
            self.recent_entries_for_prompt(story['id'], now),
            self.memories(story['id'], int(_cfg(runtime, 'memoryLimit', 0)), participant_id)
            if memory_enabled else _resolved([]),
            self.active_scene(story['id']),
            self.active_arc(story['id']),
            self.previous_scene_summaries(story['id']),
            self.facts(
                story['id'], int(_cfg(runtime, 'memoryLimit', 0)), fact_query, participant_id,
                live_fact_embedding,
            ) if memory_enabled else _resolved([]),
            self.participants(story['id']),
            self.web_observations(story['id'], participant_id),
            self.active_consequences_and_expire(
                story['id'], now,
                None if (phase == 'advance' or _cfg(shared, 'shareParticipantDetails', False)) else participant_id,
            ),
            self.overlay_snapshots_for_prompt(story['id'], participant_id, phase == 'advance')
            if memory_enabled else _resolved([]),
            self.pending_follow_up_commitments(story['id'], participant_id)
            if (participant and phase in ('user-message', 'intent-due')) else _resolved([]),
            self.get_schedule_preplan(story['id'])
            if _cfg(self.schedule_preplan_config, 'enabled', False) else _resolved(None),
            self.upcoming_narrative_intents(story['id'], now),
        )
        visible_entries = (
            recent_entries if _cfg(shared, 'shareParticipantDetails', False)
            else [
                entry for entry in recent_entries
                # 群记录是共享生活的一部分，但除非主人显式开启，不把原文暴露给单条私聊关系。
                if not (
                    (not group_context and entry.get('kind') in ('group-message', 'character-group-message'))
                    or (entry.get('participantId') and entry.get('participantId') != participant_id)
                )
            ]
        )
        # 后台推进不是聊天回合：它拿到持续的生活剧本、场景与事实，但不拿原始私聊/群
        # 记录，免得模型把它误当成「此刻刚到的消息」。
        turn_entries = (
            [entry for entry in visible_entries if entry.get('kind') not in (
                'user-message', 'character-message', 'group-message', 'character-group-message',
            )] if phase == 'advance' else visible_entries
        )
        # 本回合的显式事件决定「现在在发生什么」；历史行只是上下文，绝不被重新解读为来信。
        prompt_entries = [entry for entry in turn_entries if (entry.get('content') or '').strip()]
        if (
            phase == 'user-message'
            and any((entry.get('content') or '').strip() for entry in visible_entries)
            and not prompt_entries
        ):
            raise ValueError('Narrative context integrity failure: visible raw history did not reach recentScript.')
        decoded_state = decode_story_state(story.get('state'))
        scene_frame = project_scene_frame({
            'story_id': story['id'],
            'now': now,
            'scene': scene,
            'state': decoded_state,
            'recent_entries': prompt_entries,
            'working_details': self.prune_working_details(decoded_state.get('working_details'), now),
            'scene_presence': decoded_state.get('scene_presence'),
            'agency_window': active_agency_window(decoded_state.get('agency_window'), now),
        })
        dialogue_burst = resolve_dialogue_burst(
            scene_frame,
            decoded_state.get('dialogue_burst'),
            now,
            {
                'scope': participant_id or 'protagonist-life',
                'topic_text': (
                    user_message if user_message is not None
                    else '\n'.join(
                        str(pick(message, 'content') or '')
                        for message in (_record(group_context).get('messages') or [])
                    ) if group_context else ''
                ),
                'boundary': phase == 'advance',
            },
        )
        participants = sorted(
            [
                item for item in all_participants
                if item.get('id') != participant_id and self.can_handle_participant(item)
            ],
            key=lambda item: -participant_relevance(item),
        )[:int(_cfg(shared, 'participantContextLimit', 0))]
        agency_enabled = bool(
            _cfg(self.agency_config, 'enabled', False)
            and _cfg(runtime, 'allowProactiveMessages', False)
            and (phase == 'advance' or (phase == 'intent-due' and any(
                intent.get('type') == 'proactive-check' for intent in due_intents
            )))
        )
        advance_can_contact = phase == 'advance' and bool(_cfg(runtime, 'allowProactiveMessages', False))
        share_details = bool(_cfg(shared, 'shareParticipantDetails', False))
        visible_due_intents = (
            due_intents if share_details
            else [intent for intent in due_intents if not intent.get('participantId') or intent.get('participantId') == participant_id]
        )
        visible_upcoming_intents = (
            upcoming_intents if (phase == 'advance' or share_details)
            else [
                intent for intent in upcoming_intents
                if not intent.get('participantId') or intent.get('participantId') == participant_id
            ]
        )
        # 关系后果属于主角真实的生活：后台写作因此在原始跨参与者聊天记录保持私密时
        # 也能看到它的紧凑效果。实况回合仍只看自己（与全局）的后果，除非开启了共享。
        visible_consequences = (
            active_consequences if (phase == 'advance' or share_details)
            else [
                intent for intent in active_consequences
                if not intent.get('participantId') or intent.get('participantId') == participant_id
            ]
        )
        merged_web_context = sorted(
            [
                observation for observation in [*web_context, *extra_web_context]
                if observation.get('status') != 'deleted'
            ],
            key=lambda observation: dt_ms(to_date(observation.get('accessedAt')) or now),
        )[-max(1, int(_cfg(self.browser_config, 'maxObservationsInPrompt', 1))):]
        refresh_continuity = self.should_refresh_continuity(story, phase)
        user_reported_times = (
            extract_user_reported_times(user_message, now, _timezone(story))
            if (phase == 'user-message' and user_message and user_message.strip()) else None
        )
        development_tendencies = (
            await self.development_for_prompt(
                story['id'], participant_id,
                development_context_query(
                    user_message,
                    [intent.get('summary') for intent in visible_due_intents],
                    prompt_entries,
                ),
            ) if memory_enabled else []
        )
        context_window_minutes = int(_cfg(runtime, 'contextTimeWindowMinutes', 60))
        recent_protection_since = (
            parse_dt(dt_ms(now) - min(context_window_minutes, 1_440) * MINUTE_MS)
            if context_window_minutes > 0 else None
        )
        recall_topics = [
            _record(fact.get('knowledge')).get('topic')
            for fact in facts if fact.get('unresolved')
        ]
        recall_topics = [topic for topic in recall_topics if topic]
        focus = recall_focus(user_message, recall_topics, [intent.get('summary') for intent in visible_due_intents])
        recalled_history = None
        if memory_enabled and focus.strip():
            recalled_history = await self.recall_history(
                story['id'],
                participant_id or '',
                focus,
                resolved_turn_embedding or [],
                {
                    entry.get('id') for entry in compact_prompt_entries(
                        prompt_entries, 24_000, recent_protection_since,
                    )
                },
                {
                    entry_id
                    for fact in facts
                    if (user_message and user_message.strip()) or (fact.get('unresolved') and _record(fact.get('knowledge')).get('topic'))
                    for entry_id in (fact.get('sourceEntryIds') or [])
                },
                3 if (user_message and user_message.strip()) else 1,
            )
        # 发给模型的请求：键名逐字保持上游 camelCase。
        request: dict[str, Any] = {
            'urgeEnabled': _cfg(self.urge_config, 'enabled', False) and not any(
                intent.get('type') == 'narrative-retry' for intent in due_intents
            ),
            'phase': phase,
            'refreshContinuity': refresh_continuity,
            'outputRecovery': output_recovery,
            'story': story,
            'from': from_,
            'now': now,
            'userMessage': user_message,
            'userReportedTimes': user_reported_times,
            'images': images,
            'audio': audio,
            'visualObservations': visual_observations,
            'timelinePlan': timeline_plan,
            'developmentTendencies': development_tendencies,
            'writingOptions': {
                'messageSeparator': (str(_cfg(runtime, 'messageSeparator', '')).strip() or '<sep/>'),
                'splitReplyMessages': _cfg(runtime, 'splitReplyMessages', True) is not False,
                'browserMode': (
                    'disabled'
                    if (not _cfg(self.browser_config, 'enabled', False)
                        or (group_context and not _cfg(self.browser_config, 'allowGroupTriggeredResearch', False)))
                    else (_cfg(self.browser_config, 'mode')
                          if (phase == 'user-message' and participant and not group_context)
                          else 'deferred-only')
                ),
            },
            'participant': None if phase == 'advance' else participant,
            # 后台回合可以通过这些不透明的参与者摘要看到关系状态，但只有在主人显式
            # 打开主动消息时才允许主动联系其中一个账号。
            'participants': [] if (phase == 'advance' and not advance_can_contact) else participants,
            'dueIntents': visible_due_intents,
            'upcomingIntents': visible_upcoming_intents,
            'activeConsequences': visible_consequences,
            'supersededIntents': superseded_intents,
            'shareParticipantDetails': share_details,
            'recentEntries': prompt_entries,
            'memories': memories,
            'sceneContext': {
                'scene': scene,
                'arc': arc,
                **({'previousScenes': previous_scenes} if previous_scenes else {}),
            },
            'facts': facts,
            'groupContext': group_context,
            'chatCapabilities': chat_capabilities,
            'contactThreads': await self.contact_threads(story['id'], facts, participant_id) if memory_enabled else [],
            'sceneFrame': scene_frame,
            'dialogueBurst': dialogue_burst,
            'workingDetails': self.prune_working_details(decoded_state.get('working_details'), now),
            'timelineCarry': decoded_state.get('timeline_carry'),
            'recalledHistory': recalled_history,
            'recentProtectionSince': recent_protection_since,
            'webContext': merged_web_context,
            'overlaySnapshots': overlay_snapshots,
            'alterEnabled': _cfg(self.alter_system_config, 'enabled', False),
            'emotionalOffset': self.emotional_offset_for_prompt(story),
            'agencyEnabled': agency_enabled,
            'agencyWindow': (
                active_agency_window(decoded_state.get('agency_window'), now) or None if agency_enabled else None
            ),
            'automaticDeliverySummaries': (
                decoded_state.get('automatic_delivery_summaries') if is_automatic_narrative_phase(phase) else []
            ),
            'followUpCommitments': follow_up_commitments,
            'schedulePreplan': schedule_preplan_window(
                schedule_record, now, _timezone(story), 12, self.schedule_preplan_config,
            ),
            'onEarlyReply': on_early_reply,
        }
        if quoted_messages:
            request['quotedMessages'] = quoted_messages
        if sticker_catalog and phase == 'user-message':
            request['stickerCatalog'] = sticker_catalog
        return resolve_authored_actions(
            await self.narrator.decide(request), False, separator,
        )

    # ------------------------------------------------------------------ #
    # shouldRefreshContinuity（上游 :3604）
    # ------------------------------------------------------------------ #

    def should_refresh_continuity(self, _story: Any, _phase: str) -> bool:
        """上游 `shouldRefreshContinuity`（`:3604`）。

        只为「首次自动补写」或「每 15 次成功叙事写入」刷新连续性；普通回合复用
        上一份快照。后台场景/弧压缩仍在工作，第二份实时摘要既不提供原文证据
        也不提供执行确认，因此当前恒为 `False`。
        """
        return False

    # ------------------------------------------------------------------ #
    # planAutomaticTimeline（上游 :3613）
    # ------------------------------------------------------------------ #

    async def plan_automatic_timeline(
        self,
        story: Any,
        participant: Any,
        phase: str,
        from_: datetime,
        now: datetime,
        due_intents: list[dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        """上游 `planAutomaticTimeline(story, participant, phase, from, now, dueIntents)`（`:3613`）。

        自动散文不再自己发明世界时间线：压缩路由先返回一份极小的相对时间账本；
        拿不到时保留当前游标，比写出一段没有根据的未来更安全。
        """
        if _cfg(_section(self.config, 'timelineDirector'), 'enabled', True) is False:
            return None
        if not callable(getattr(self.compactor, 'plan_timeline', None)):
            return None
        timezone = _timezone(story)
        backoff = self.timeline_backoff.get(story['id'])
        if backoff and backoff.get('from') == dt_ms(from_) and dt_ms(now) < int(backoff.get('until') or 0):
            self.report_operation(
                'diagnostic', 'debug', story, phase,
                '时间导演调用冷却中，保留当前时间窗口至 %s',
                format_log_time(parse_dt(backoff.get('until')), timezone),
            )
            return None
        if backoff and (backoff.get('from') != dt_ms(from_) or dt_ms(now) >= int(backoff.get('until') or 0)):
            self.timeline_backoff.pop(story['id'], None)
        # 熔断冷却：连续失败达阈值后，冷却期内不再调用时间导演（降级路径接管），
        # 到期重试一次完整路径。
        failures = self.timeline_director_failures.get(story['id']) or 0
        if failures >= TIMELINE_DIRECTOR_FUSE and backoff and dt_ms(now) < int(backoff.get('until') or 0):
            return None
        participant_id = _record(participant).get('id')
        scene, recent_entries, facts, schedule_record = await asyncio.gather(
            self.active_scene(story['id']),
            self.recent_entries_for_prompt(story['id'], now),
            self.facts(
                story['id'], min(16, int(_cfg(self.runtime_config, 'memoryLimit', 0))), '', participant_id,
            ) if _cfg(self.memory_config, 'enabled', False) else _resolved([]),
            self.get_schedule_preplan(story['id'])
            if _cfg(self.schedule_preplan_config, 'enabled', False) else _resolved(None),
        )
        share_details = bool(_cfg(self.shared_story_config, 'shareParticipantDetails', False))
        visible_entries = (
            recent_entries if share_details
            else [entry for entry in recent_entries
                  if not entry.get('participantId') or entry.get('participantId') == participant_id]
        )
        continuation = next(
            (entry for entry in reversed(visible_entries) if entry.get('kind') == 'script' and (entry.get('content') or '').strip()),
            None,
        )
        continuation_ledger = timeline_entry_prompt_projection(continuation) if continuation else None
        visible_due = [
            intent for intent in due_intents
            if not intent.get('participantId') or intent.get('participantId') == participant_id
        ]
        topics = [_record(fact.get('knowledge')).get('topic') for fact in facts if fact.get('unresolved')]
        topics = [topic for topic in topics if topic]
        focus = recall_focus(None, topics, [intent.get('summary') for intent in visible_due])
        memory_enabled = bool(_cfg(self.memory_config, 'enabled', False))
        request: dict[str, Any] = {
            'story': story,
            'participant': participant,
            'phase': phase,
            'from': from_,
            'now': now,
            'scene': scene,
            'facts': facts,
            'recentEntries': [timeline_entry_prompt_projection(entry) for entry in visible_entries],
            'contactThreads': await self.contact_threads(story['id'], facts, participant_id) if memory_enabled else [],
            'recalledHistory': await self.recall_history(
                story['id'], participant_id or '', focus, [],
                {entry.get('id') for entry in visible_entries},
                {
                    entry_id
                    for fact in facts
                    if fact.get('unresolved') and _record(fact.get('knowledge')).get('topic')
                    for entry_id in (fact.get('sourceEntryIds') or [])
                },
                1,
            ) if (memory_enabled and focus.strip()) else None,
            'recentScriptContinuation': ({
                'content': continuation.get('content'),
                'occurredAt': continuation.get('occurredAt'),
                **({'hostTimelineLedger': continuation_ledger.get('content')}
                   if continuation_ledger and continuation_ledger.get('content') != continuation.get('content') else {}),
            } if continuation else None),
            'dueIntents': due_intents,
            'schedulePreplan': schedule_preplan_window(
                schedule_record, now, timezone, 12, self.schedule_preplan_config,
            ),
        }
        try:
            raw_plan = await self.compactor.plan_timeline(request)
            plan = normalize_timeline_plan(raw_plan)
            if plan:
                self.timeline_director_failures.pop(story['id'], None)
                self.timeline_backoff.pop(story['id'], None)
                automation = {
                    key: value for key, value in _automation(story).items()
                    if key not in ('timelineRetryAt', 'timeline_retry_at', 'timelineRetryFrom', 'timeline_retry_from')
                }
                story = {**story, 'state': {**_record(story.get('state')), 'automation': automation}}
                self.report_operation(
                    'diagnostic', 'debug', story, phase,
                    '时间导演已生成事件账本 节点=%d', len(plan.get('beats') or []),
                )
            else:
                # 可诊断性：把模型原始返回暴露出来，避免「永远失效但不知道为什么」。
                raw_preview = (
                    raw_plan[:400] if isinstance(raw_plan, str)
                    else json.dumps(raw_plan if raw_plan is not None else None, ensure_ascii=False)[:400]
                )
                failures = (self.timeline_director_failures.get(story['id']) or 0) + 1
                self.timeline_director_failures[story['id']] = failures
                self.report_operation(
                    'standard', 'warn', story, phase,
                    '时间导演返回被拒绝（连续第 %d 次）原始返回=%s 拒绝原因=%s',
                    failures, raw_preview, describe_timeline_plan_rejection(raw_plan),
                )
                await self.persist_timeline_retry(story, from_, phase, failures)
            return plan
        except Exception as error:
            failures = (self.timeline_director_failures.get(story['id']) or 0) + 1
            self.timeline_director_failures[story['id']] = failures
            self.report_operation('diagnostic', 'warn', story, phase, '时间导演调用失败 错误=%s', error)
            await self.persist_timeline_retry(story, from_, phase, failures)
            return None

    # ------------------------------------------------------------------ #
    # isTimelineDirectorFused（上游 :3689）
    # ------------------------------------------------------------------ #

    def is_timeline_director_fused(self, story_id: str) -> Optional[int]:
        """上游 `isTimelineDirectorFused`（`:3689`）：连续失败达阈值即熔断。"""
        failures = self.timeline_director_failures.get(story_id)
        if not failures or failures < TIMELINE_DIRECTOR_FUSE:
            return None
        return failures

    # ------------------------------------------------------------------ #
    # persistTimelineRetry（上游 :3698）
    # ------------------------------------------------------------------ #

    async def persist_timeline_retry(
        self,
        story: Any,
        from_: datetime,
        phase: str,
        failures: int = 1,
    ) -> None:
        """上游 `persistTimelineRetry(story, from, phase, failures = 1)`（`:3698`）。

        指数退避：10min → 20min → 40min → 80min → 160min → 封顶 2h。退避只决定重试
        节奏；真正终止循环的是熔断降级（`plan_automatic_timeline`）。
        """
        backoff_ms = min(
            TIMELINE_RETRY_BACKOFF_BASE * (2 ** max(0, failures - 1)),
            TIMELINE_DIRECTOR_FUSE_COOLDOWN,
        )
        until = parse_dt(dt_ms(self.now()) + backoff_ms)
        self.timeline_backoff[story['id']] = {'from': dt_ms(from_), 'until': dt_ms(until)}
        automation = {
            **_automation(story),
            'timelineRetryAt': iso(until),
            'timelineRetryFrom': iso(from_),
            # 把自动唤醒挪出失败窗口。这只是给旧调度器的提示，上面的入口守卫才是权威。
            'nextAdvanceAt': iso(until),
        }
        story['state'] = encode_story_state({
            **decode_story_state(story.get('state')), 'automation': automation,
        })
        try:
            await self.db_set(
                'interlude_story', {'id': story['id']}, {'state': story['state'], 'updatedAt': self.now()},
            )
        except Exception as error:
            self.report_operation(
                'diagnostic', 'debug', story, phase,
                '时间导演冷却状态持久化失败，将继续使用内存冷却 错误=%s', error,
            )

    # ------------------------------------------------------------------ #
    # tryDecide（上游 :3720）
    # ------------------------------------------------------------------ #

    async def try_decide(
        self,
        story: Any,
        participant: Any,
        phase: str,
        from_: datetime,
        now: datetime,
        user_message: Any,
        due_intents: list[dict[str, Any]],
        superseded_intents: Optional[list[dict[str, Any]]] = None,
        group_context: Any = None,
        images: Optional[list[dict[str, Any]]] = None,
        audio: Optional[list[dict[str, Any]]] = None,
        chat_capabilities: Any = None,
        quoted_messages: Optional[list[dict[str, Any]]] = None,
        sticker_catalog: Optional[list[dict[str, Any]]] = None,
        turn_query_embedding: Optional[list[float]] = None,
        visual_observations: Optional[list[str]] = None,
        on_early_reply: Any = None,
    ) -> dict[str, Any]:
        """上游 `tryDecide(...)`（`:3720`）。

        参数顺序与上游**逐字对齐**（位置参数），因为 `chunk3.flush_buffered_narrative`
        等兄弟成员按位置调用它。返回 dict 同时提供 `timelinePlan` / `timeline_plan`
        与 `effectiveNow` / `effective_now`（跨 chunk 双读）。
        """
        superseded_intents = superseded_intents or []
        images = images or []
        audio = audio or []
        quoted_messages = quoted_messages or []
        sticker_catalog = sticker_catalog or []
        visual_observations = visual_observations or []
        immediate_observations: list[dict[str, Any]] = []
        effective_now = now
        automatic_phase = phase in ('advance', 'conversation-follow-up', 'intent-due')
        short_schedule = (
            schedule_preplan_window(
                await self.get_schedule_preplan(story['id']),
                from_, _timezone(story), 12, self.schedule_preplan_config,
            )
            if (automatic_phase and phase != 'advance' and _cfg(self.schedule_preplan_config, 'enabled', False))
            else None
        )
        director_required = (
            automatic_phase
            and _cfg(_section(self.config, 'timelineDirector'), 'enabled', True) is not False
            and needs_timeline_director(phase, from_, now, _timezone(story), short_schedule)
        )
        timeline_plan = (
            await self.plan_automatic_timeline(story, participant, phase, from_, now, due_intents)
            if director_required else None
        )
        if director_required and not timeline_plan:
            # 降级而非冻结：账本缺失只损失时间结构辅助，自动推进照常进行
            # （熔断计数仍抑制导演调用本身，冷却后自动重试完整路径）。
            # 旧实现在这里丢弃整个回合，0 命中率曾让自动生活流实质瘫痪。
            fused = self.is_timeline_director_fused(story['id'])
            self.report_operation(
                'standard', 'warn', story, phase,
                '时间导演已熔断（连续失败 %d 次），本次自动回合降级为无账本推进' if fused
                else '时间导演未生成有效事件账本，本次自动回合降级为无账本推进',
                *([fused] if fused else []),
            )
        started_at = dt_ms(self.now())
        timezone = _timezone(story)
        self.report_operation(
            'standard', 'info', story, phase,
            '模型调用开始 任务=主叙事 模型=%s 参与者=%s 时间段=%s→%s 到期计划=%d',
            self.main_model_label(), _record(participant).get('id') or '全局',
            format_log_time(from_, timezone), format_log_time(now, timezone), len(due_intents),
        )
        try:
            early_reply_committed = False
            browser = self.browser_config
            can_early_reply = bool(on_early_reply) and not (
                phase == 'user-message' and participant and not group_context
                and _cfg(browser, 'enabled', False) and _cfg(browser, 'mode') == 'allow-immediate'
            )

            async def early_reply(reply: Any) -> bool:
                nonlocal early_reply_committed
                committed = await on_early_reply(reply)
                if committed:
                    early_reply_committed = True
                return bool(committed)

            decision = await self.decide(
                story, participant, phase, from_, effective_now, user_message, due_intents,
                superseded_intents, group_context, images, audio, [], False, chat_capabilities,
                quoted_messages, sticker_catalog, turn_query_embedding, visual_observations,
                timeline_plan, early_reply if can_early_reply else None,
            )
            immediate = None
            if (
                phase == 'user-message' and participant and not group_context
                and _cfg(browser, 'enabled', False) and _cfg(browser, 'mode') == 'allow-immediate'
            ):
                for raw_intent in (_raw_decision(decision, 'browserIntents') or []):
                    normalized = _normalize_browser_intent_draft(raw_intent, browser)
                    if normalized and normalized.get('timing') == 'immediate':
                        immediate = normalized
                        break
            if immediate:
                # 第一遍只是提出动作：不要落库它的散文或聊天决策。真正读过页面之后
                # 再问叙事器一次，最终剧本才是一件连贯的作品而不是两次工具调用的拼接。
                self.report_operation('standard', 'info', story, phase, '即时网页观察开始 模式=%s', immediate.get('mode'))
                observation = await self.collect_web_observation(
                    story, immediate, participant['id'], None, utc_now(), False,
                )
                immediate_observations = [observation]
                effective_now = utc_now()
                decision = await self.decide(
                    story, participant, phase, from_, effective_now, user_message, due_intents,
                    superseded_intents, group_context, images, audio, immediate_observations, False,
                    chat_capabilities, quoted_messages, sticker_catalog, turn_query_embedding,
                    visual_observations, timeline_plan, early_reply if can_early_reply else None,
                )
            # 用户自报的钟点（「八点赶到」）对守卫背书：模型复述它们不是时间越界。
            # 提取是 O(消息长度) 的本地正则，只在实况用户回合发生一次。
            endorsed_clocks = None
            if phase == 'user-message' and user_message and user_message.strip():
                clocks: set[int] = set()
                for fact in extract_user_reported_times(user_message, effective_now, timezone):
                    local_time = pick(fact, 'localTime', 'local_time')
                    if isinstance(local_time, str):
                        try:
                            clocks.add(int(local_time[-5:-3]) * 60 + int(local_time[-2:]))
                        except ValueError:
                            continue
                endorsed_clocks = clocks
            main_available = bool(_record(self.model_routing.get('main')).get('available'))
            initial_time_overflow = detect_live_script_time_overflow(
                _raw_decision(decision, 'script'), phase, from_, effective_now, timezone, endorsed_clocks,
            )
            initial_visible_recovery = (
                main_available and not early_reply_committed
                and requires_visible_reply_recovery(phase, group_context, decision)
            )
            if initial_time_overflow or initial_visible_recovery:
                # 诊断：记录被抛弃草稿里模型实际返回的 interaction（缺失/为空/形状错误），
                # 让下一次「结构化可见回复缺失」可以直接从日志定位是模型行为还是解析问题。
                if initial_visible_recovery:
                    # ⚠️ 这条必须是**可见**的：core 的 `diagnostic` 频道在默认
                    # `logging.verbosity` 下一个字都不打（见 AGENTS.md 坑 25），而
                    # 「一个已经写好的回合被白重写一次」正是运维最需要看见的事。
                    # 带上「残留 say 标记」与预览：解析成功时标签会被整个解包、不会留在
                    # 散文里，所以 `残留>0` 就是"模型写了行动但一个都没解析出来"的铁证
                    # （2026-09-25 23:56 那次重写就是靠这个才定位得了的）。
                    markup = inspect_say_markup(_raw_decision(decision, 'script'))
                    actions = _raw_decision(decision, 'authoredActions') or []
                    self.report_operation(
                        'standard', 'warn', story, phase,
                        '被抛弃草稿的结构化回复字段 interaction=%s 已解析动作=%d 残留say标记=%d 残留预览=%s',
                        safe_json_preview(_record(decision).get('interaction')),
                        len(actions) if isinstance(actions, list) else 0,
                        markup['leftover'], markup['preview'] or '(无)',
                    )
                self.report_operation(
                    'standard', 'warn', story, phase,
                    '剧本越过当前时间终点，已抛弃本次未落库剧本并重新写作 原因=%s' if initial_time_overflow
                    else '结构化可见回复缺失，已抛弃本次未落库剧本并重新写作',
                    *([initial_time_overflow] if initial_time_overflow else []),
                )
                decision = await self.decide(
                    story, participant, phase, from_, effective_now, user_message, due_intents,
                    superseded_intents, group_context, images, audio, immediate_observations, True,
                    chat_capabilities, quoted_messages, sticker_catalog, turn_query_embedding,
                    visual_observations, timeline_plan, early_reply if can_early_reply else None,
                )
                recovered_time_overflow = detect_live_script_time_overflow(
                    _raw_decision(decision, 'script'), phase, from_, effective_now, timezone, endorsed_clocks,
                )
                if recovered_time_overflow:
                    raise ValueError(
                        'Narrative provider crossed the live time boundary after one recovery attempt: %s'
                        % recovered_time_overflow,
                    )
                if (
                    main_available and not early_reply_committed
                    and requires_visible_reply_recovery(phase, group_context, decision)
                ):
                    self.report_operation(
                        'diagnostic', 'warn', story, phase,
                        '恢复尝试仍缺失结构化回复 interaction=%s',
                        safe_json_preview(_record(decision).get('interaction')),
                    )
                    raise ValueError(
                        'Narrative provider omitted the required visible-reply structure after one recovery attempt.',
                    )
            # 固定的叙事契约要求每个真实模型回合都有散文。一个语法合法但省略/留空
            # script 的对象过去会被当成成功，推进游标却在生活记录里留下空洞。
            # 把它当成 provider 失败，实况用户回合走既有的持久化重试路径，
            # 后台回合则为下一次尝试保留时间。兜底刻意是无网络的冒烟模式。
            if main_available and not has_required_narrative_script(decision):
                raise ValueError('Narrative provider returned no usable script.')
            result = {
                'decision': decision,
                'succeeded': True,
                'effectiveNow': effective_now,
                'effective_now': effective_now,
                'immediateObservations': immediate_observations,
                'immediate_observations': immediate_observations,
                'timelinePlan': timeline_plan,
                'timeline_plan': timeline_plan,
            }
            logging_config = _section(self.config, 'logging')
            script_text = _raw_decision(decision, 'script')
            if _cfg(logging_config, 'logScriptPreview', False) and script_text:
                self.report(
                    'info', story, phase, '当前剧本内容：\n%s',
                    script_text[:int(_cfg(logging_config, 'previewLength', 0))],
                )
            self.report_operation(
                'standard', 'info', story, phase,
                '模型调用完成 任务=主叙事 耗时=%dms 剧本文字=%d 回复模式=%s',
                dt_ms(self.now()) - started_at, len(script_text or ''), visible_reply_mode(decision, phase, group_context),
            )
            # 无可见回复的私聊回合打出最终 interaction，用于区分：模型主动 none、
            # 未读沉默（seen=false）、引用失配被兜底前丢弃（actionId 残留）三种链路。
            if (
                phase == 'user-message' and not group_context
                and _record(_record(_record(decision).get('interaction')).get('reply')).get('mode') == 'none'
            ):
                self.report_operation(
                    'diagnostic', 'info', story, phase, '本回合无可见回复 interaction=%s',
                    safe_json_preview(_record(decision).get('interaction')),
                )
            return result
        except Exception as error:
            self.report(
                'warn', story, phase, '模型调用失败 任务=主叙事 耗时=%dms 错误=%s',
                dt_ms(self.now()) - started_at, error,
            )
            return {
                'decision': {},
                'succeeded': False,
                'effectiveNow': effective_now,
                'effective_now': effective_now,
                'immediateObservations': immediate_observations,
                'immediate_observations': immediate_observations,
                'timelinePlan': timeline_plan,
                'timeline_plan': timeline_plan,
            }

    # ------------------------------------------------------------------ #
    # persistDecision（上游 :3833）
    # ------------------------------------------------------------------ #

    async def persist_decision(
        self,
        story: Any,
        participant: Any,
        raw: Any,
        from_: datetime,
        now: datetime,
        permit_messages: bool,
        phase: str,
        context_intents: Optional[list[dict[str, Any]]] = None,
        immediate_reply_already_delivered: bool = False,
        timeline_plan: Any = None,
    ) -> dict[str, Any]:
        """上游 `persistDecision(...)`（`:3833`）：一次叙事决策的完整落库。

        先规范化再写库：不信任模型给出的时间、长度与结构，尤其不能让未来剧情落库。
        返回 `{'messages', 'commit', 'scriptEntry', 'script_entry'}`。
        """
        context_intents = context_intents or []
        runtime = self.runtime_config
        shared = self.shared_story_config
        separator = _cfg(runtime, 'messageSeparator', '<sep/>')
        max_message_characters = int(_cfg(runtime, 'maxMessageCharacters', _DEFAULT_MAX_MESSAGE_CHARACTERS))
        participant_id = _record(participant).get('id')
        phase_zone = _timezone(story)

        # 先规范化，再写库。
        raw = resolve_authored_actions(
            _dual(raw) if isinstance(raw, dict) else {},
            immediate_reply_already_delivered,
            separator,
        )
        all_participants = await self.participants(story['id'])
        permitted_participant_ids = {
            item['id'] for item in all_participants if self.can_handle_participant(item)
        }
        refresh_continuity = self.should_refresh_continuity(story, phase)
        decision = _normalize_decision(
            raw, from_, now, permit_messages, self.effective_urge_runtime, shared,
            participant_id or '', permitted_participant_ids, phase, self.memory_config, refresh_continuity,
        )
        script = decision.get('script') or ''
        state_before = decode_story_state(story.get('state'))
        active_scene = await self.active_scene(story['id']) if script else None
        scene_frame = None
        dialogue_burst = None
        if script:
            scene_frame = project_scene_frame({
                'story_id': story['id'],
                'now': now,
                'scene': active_scene,
                'state': state_before,
                'working_details': self.prune_working_details(state_before.get('working_details'), now),
                'scene_presence': state_before.get('scene_presence'),
                'agency_window': active_agency_window(state_before.get('agency_window'), now),
            })
            dialogue_burst = resolve_dialogue_burst(
                scene_frame,
                state_before.get('dialogue_burst'),
                now,
                {'scope': participant_id or 'protagonist-life', 'boundary': phase == 'advance'},
            )
        group_reply_content = normalize_group_visible_reply(
            _record(raw).get('groupReply'), None, max_message_characters, separator,
        )
        commit = None
        if script:
            commit_decision = {
                **decision,
                'groupReply': _record(raw).get('groupReply'),
                'messageReactions': _record(raw).get('messageReactions'),
                'localMedia': _record(raw).get('localMedia'),
                'nativeFace': _record(raw).get('nativeFace'),
            }
            commit = decision_to_script_commit({
                'story_id': story['id'],
                'participant_id': participant_id,
                'phase': phase,
                'from': from_,
                'now': now,
                'decision': commit_decision,
                'message_separator': separator,
                'split_reply_messages': _cfg(runtime, 'splitReplyMessages', True),
                'group_reply_content': group_reply_content,
                'frame_id': (scene_frame or {}).get('id'),
                'burst_id': (dialogue_burst or {}).get('id'),
                'starts_after_event_id': (dialogue_burst or {}).get('last_event_id'),
            })
        if commit is not None:
            validation = validate_script_commit(commit, separator)
            if not validation.get('valid'):
                raise ValueError(
                    'ScriptCommit structural validation failed: %s' % '; '.join(validation.get('errors') or []),
                )
            unbound = unbound_immediate_message_events(commit)
            if unbound:
                self.report_operation(
                    'diagnostic', 'debug', story, phase,
                    'M5 剧本行动绑定未确认 即时消息=%d；保留兼容投递，不重写已生成剧本', len(unbound),
                )
        script_entry: Optional[dict[str, Any]] = None
        if commit is not None:
            interaction_reply = _record(_record(decision.get('interaction')).get('reply'))
            # 一次承诺处置属于「这条消息真的到达用户」这件事。
            if participant and (permit_messages or immediate_reply_already_delivered) and decision.get('followUpResolutions'):
                event = find_outgoing_script_event(
                    commit, participant['id'], 'immediate', interaction_reply.get('content'), separator,
                )
                if event is not None:
                    event['metadata'] = {
                        **_record(event.get('metadata')),
                        'followUpResolutions': decision['followUpResolutions'],
                    }
            if participant and phase == 'user-message' and (permit_messages or immediate_reply_already_delivered):
                event = find_outgoing_script_event(
                    commit, participant['id'], 'immediate', interaction_reply.get('content'), separator,
                )
                commitment = decision.get('followUpCommitment') or (
                    inferred_follow_up_commitment(str(interaction_reply.get('content')), now)
                    if interaction_promises_follow_up(interaction_reply.get('content')) else None
                )
                if event is not None and commitment:
                    event['metadata'] = {**_record(event.get('metadata')), 'followUpCommitment': commitment}
            script_entry = await self.append_entry(
                story['id'],
                script_entry_draft_for_commit(
                    commit, decision.get('interaction') or None, timeline_plan, decision.get('lifeHandoff'),
                ),
                now,
                participant_id or '',
            )
        resolved_consequences = await self.apply_intent_updates(
            story['id'], decision.get('intentUpdates'), now, participant_id,
        )
        for memory in decision.get('memories') or []:
            await self.append_memory(
                story['id'], memory, now,
                memory.get('participantId') or participant_id or '',
                (script_entry or {}).get('id'),
            )
        for intent in decision.get('intents') or []:
            # 处理用户来信时创建的提醒/承诺是对这段关系的回应，即使它很久以后才到期。
            # 把这份来源信息带进共享意图账本，它到期的那一回合才被允许发出消息，
            # 而不必因此打开宽泛的后台主动打扰。
            intent_payload = _record(intent.get('payload'))
            await self.append_intent(story['id'], {
                **intent,
                'payload': (
                    {**intent_payload, 'userInitiated': intent_payload.get('userInitiated') is not False}
                    if (phase == 'user-message' and participant) else intent_payload
                ),
            }, now, intent.get('participantId') or participant_id or '')
        resolved_follow_ups: set[int] = set()  # 只有确认投递才能结清。
        for browser_intent in decision.get('browserIntents') or []:
            # 即时意图在启用时已由最终叙事回合之前处理。若它走到这里（模式被关、群回合、
            # 或连续第二次请求），安全地降级为延迟任务。
            if participant or phase != 'user-message' or _cfg(self.browser_config, 'allowGroupTriggeredResearch', False):
                await self.append_browser_intent(story['id'], browser_intent, now, participant_id or '')
        if participant and decision.get('statePatch'):
            await self.update_participant_state(participant, decision['statePatch'], now)

        is_agency_check = len(context_intents) > 0 and all(
            intent.get('type') == 'proactive-check' for intent in context_intents
        )
        agency_candidate: Optional[dict[str, Any]] = None
        agency_allows_send = False
        agency_recheck: Optional[dict[str, Any]] = None

        alter_turn: Any = None
        if script:
            state = state_before
            next_count = max(0, int(math.floor(state.get('narrative_update_count') or 0))) + 1
            next_state: dict[str, Any] = {**state, 'narrative_update_count': next_count}
            # 原文与执行标注现在就提供连续性：第二份实时散文摘要不得为一次
            # 没有执行过的发送动作背书。
            if resolved_consequences or resolved_follow_ups:
                next_state['continuity_dirty'] = True
            alter_turn = self.update_alter_system(
                story, state.get('alter_system'), decision.get('alter'), phase, now, participant_id or '',
            )
            next_state['alter_system'] = (
                alter_turn.get('state') if _record(alter_turn).get('state') is not None else state.get('alter_system')
            )
            next_state['timeline_carry'] = []  # 计划不会变成持久事实。
            handoff = decision.get('lifeHandoff')
            if handoff and script_entry:
                presence = {item['name']: item for item in (next_state.get('scene_presence') or [])}
                handoff_place = _record(handoff.get('place'))
                previous_place = _record(state_before.get('scene_frame')).get('place')
                if (
                    handoff.get('transition')
                    or (handoff.get('place') and handoff_place.get('value') != previous_place)
                    or handoff.get('presence')
                ):
                    for name, item in list(presence.items()):
                        presence[name] = {
                            **item,
                            'status': 'off-scene',
                            'source_entry_ids': [script_entry['id']],
                            'updated_at': iso(now),
                            'basis': 'Local occupancy superseded by a new original-script handoff; not an invented departure.',
                        }
                for name in _record(handoff.get('presence')).get('names') or []:
                    presence[name] = {
                        'name': name,
                        'status': 'present',
                        'basis': _record(handoff.get('presence')).get('quote'),
                        'source_entry_ids': [script_entry['id']],
                        'updated_at': iso(now),
                    }
                next_state['scene_presence'] = list(presence.values())[-8:]
                resolved = {item.get('label') for item in (handoff.get('resolved_details') or [])}
                next_state['working_details'] = [
                    item for item in (next_state.get('working_details') or []) if item.get('label') not in resolved
                ]
                resolutions = dict(next_state.get('working_detail_resolutions') or {})
                for label in resolved:
                    resolutions[label] = script_entry['id']
                next_state['working_detail_resolutions'] = resolutions
            if script_entry and decision.get('lifeHandoff'):
                await self.persist_timeline_scene_anchor(
                    story['id'], decision['lifeHandoff'], script_entry['id'], now,
                )
            if scene_frame and dialogue_burst and script_entry and commit is not None:
                advanced_frame = advance_scene_frame(
                    scene_frame, dialogue_burst, commit, script_entry['id'], now, decision.get('lifeHandoff'),
                )
                next_state['scene_frame'] = advanced_frame['frame']
                next_state['dialogue_burst'] = advanced_frame['burst']
            if _cfg(self.agency_config, 'enabled', False) and (phase == 'advance' or is_agency_check):
                source_entries = (
                    await self.recent_entries(
                        story['id'], max(40, int(_cfg(runtime, 'contextEntryLimit', 20)) * 2),
                    )
                    if (decision.get('agencyWindow') or decision.get('proactiveContact')) else []
                )
                valid_source_entry_ids = {entry['id'] for entry in source_entries}
                if script_entry and script_entry.get('id'):
                    valid_source_entry_ids.add(script_entry['id'])
                agency_window = normalize_agency_window_draft(
                    decision.get('agencyWindow'), now, self.agency_config, valid_source_entry_ids,
                    script_entry['id'] if script_entry else None,
                ) or active_agency_window(state.get('agency_window'), now)
                next_state['agency_window'] = agency_window
                agency_candidate = normalize_proactive_contact(
                    decision.get('proactiveContact'), now, self.agency_config, permitted_participant_ids,
                    valid_source_entry_ids, script_entry['id'] if script_entry else None,
                )
                if is_agency_check and _record(agency_candidate).get('participant_id') != participant_id:
                    agency_candidate = None
                if agency_candidate and agency_window:
                    target = next(
                        (item for item in all_participants if item.get('id') == agency_candidate['participant_id']),
                        None,
                    )
                    urge_config = self.urge_config
                    capacity_config = self.agency_config
                    if _cfg(urge_config, 'enabled', False) and urge_burst_active(
                        normalize_urge_state(
                            _record(_record(state.get('extensions')).get('urge')), dt_ms(now),
                        ),
                        dt_ms(now), urge_config, agency_candidate['participant_id'],
                    ):
                        capacity_config = {
                            **self.agency_config, 'minimumProactiveIntervalMinutes': urge_config.get('contact_min'),
                        }
                    capacity = evaluate_agency_capacity(
                        agency_window, agency_candidate, now, capacity_config,
                        _record(_record(target).get('state')).get('lastCharacterMessageAt'),
                    )
                    willingness = agency_candidate.get('willingness') or 0
                    willingness_passes = willingness >= float(
                        _cfg(self.effective_urge_runtime, 'proactiveWillingnessThreshold', 0.65)
                    )
                    agency_allows_send = (
                        agency_candidate.get('outcome') == 'send-now'
                        and bool(capacity.get('allowed')) and willingness_passes
                    )
                    if not agency_allows_send and agency_candidate.get('outcome') != 'let-go' and willingness_passes:
                        agency_recheck = {
                            'candidate': agency_candidate,
                            'window': agency_window,
                            'reason': 'model-requested-recheck' if capacity.get('allowed') else capacity.get('reason'),
                            'at': proactive_recheck_at(agency_candidate, capacity, agency_window, now),
                        }
                    self.report_operation(
                        'standard', 'info', story, phase,
                        'Agency 主动联系判断 参与者=%s 结果=%s 原因=%s 意愿=%s',
                        agency_candidate['participant_id'],
                        '立即联系' if agency_allows_send else ('稍后重查' if agency_recheck else '自然放下'),
                        capacity.get('reason'), '%.2f' % willingness,
                    )
                if agency_window:
                    self.report_operation(
                        'diagnostic', 'debug', story, phase,
                        'Agency Window 更新 负荷=%s 隐私=%s 设备=%s 有效至=%s',
                        agency_window.get('activity_load'), agency_window.get('privacy'),
                        agency_window.get('device_access'),
                        format_log_time(to_date(agency_window.get('valid_until')), phase_zone),
                    )
            await self.db_set(
                'interlude_story', {'id': story['id']},
                {'state': encode_story_state(next_state), 'updatedAt': now},
            )
            if _record(alter_turn).get('thresholdReached'):
                self.schedule_alter_analysis(
                    story['id'], phase, _record(alter_turn).get('sourceParticipantId') or '',
                )

        if agency_recheck:
            await self.append_proactive_check(
                story, agency_recheck['candidate'], agency_recheck['at'], agency_recheck['reason'], now,
            )

        messages: list[dict[str, Any]] = []
        if is_agency_check:
            agency_reply_mode = _record(_record(decision.get('interaction')).get('reply')).get('mode')
            interaction = decision.get('interaction') if (agency_allows_send and agency_reply_mode == 'immediate') else None
        else:
            interaction = decision.get('interaction')
        if phase in ('intent-due', 'user-message') and participant:
            await self.defer_unresolved_due_follow_ups(
                story['id'], participant['id'], context_intents, resolved_follow_ups, interaction, now,
            )
        automatic_delivery = None
        if (
            is_automatic_narrative_phase(phase)
            or (_cfg(self.urge_config, 'enabled', False) and is_agency_check)
        ) and script_entry:
            automatic_delivery = {
                'summary': decision.get('automaticDeliverySummary')
                or ('Background delivery based on script #%d.' % script_entry['id']),
                'sourceEntryId': script_entry['id'],
                'source_entry_id': script_entry['id'],
            }
        reply = _record(_record(interaction).get('reply'))
        if participant and phase == 'user-message' and not is_agency_check and _record(interaction).get('seen'):
            await self.mark_participant_seen(participant, now)
        if (
            participant and permit_messages and not immediate_reply_already_delivered
            and reply.get('mode') == 'immediate' and reply.get('content')
        ):
            messages.append(attach_message_event({
                'participant_id': participant['id'],
                'content': reply['content'],
                'automatic_delivery': automatic_delivery,
                'interaction': interaction or None,
                'user_initiated': phase == 'user-message',
            }, find_outgoing_script_event(
                commit, participant['id'], 'immediate', reply['content'], separator,
            ) if commit is not None else None, (script_entry or {}).get('id')))
        if (
            participant and permit_messages and reply.get('mode') == 'delayed'
            and reply.get('content') and reply.get('sendAt')
        ):
            send_at = parse_dt(reply['sendAt'])
            await self.append_intent(story['id'], {
                'type': 'delayed-reply',
                'summary': 'The character decided to send a delayed reply.',
                'not_before': reply['sendAt'],
                'payload': {
                    'content': reply['content'],
                    'userInitiated': phase == 'user-message',
                    'interaction': True,
                    **({
                        **script_event_payload(attach_message_event(
                            {'participant_id': participant['id'], 'content': reply['content']},
                            find_outgoing_script_event(
                                commit, participant['id'], 'delayed', reply['content'], separator,
                            ),
                            (script_entry or {}).get('id'),
                        )),
                    } if commit is not None else {}),
                },
            }, now, participant['id'])
            self.schedule_due_intent_wake(story['id'], send_at)

        # 跨账号消息从目标视角看就是主动联系：实况用户事件里允许；后台只在
        # 全局主动消息开关打开时允许。
        cross_all = decision.get('crossConversationActions') or []
        if phase == 'user-message':
            cross_actions = cross_all
        elif phase == 'advance' and not _cfg(self.agency_config, 'enabled', False) and _cfg(runtime, 'allowProactiveMessages', False):
            cross_actions = cross_all
        elif phase == 'advance' and agency_allows_send and agency_candidate:
            cross_actions = [
                action for action in cross_all
                if action.get('participantId') == agency_candidate['participant_id']
                and action.get('mode') == 'immediate'
            ][:1]
        else:
            cross_actions = []
        if phase == 'advance' and cross_all and not cross_actions:
            self.report_operation(
                'diagnostic', 'debug', story, phase,
                'Agency 拒绝未通过容量或来源验证的 crossConversationAction 数量=%d', len(cross_all),
            )
        approved_automatic_outgoing_actions = (
            [
                {'participantId': action.get('participantId'), 'mode': action.get('mode')}
                for action in cross_actions if action.get('mode') == 'immediate'
            ] if phase == 'advance' else []
        )
        # 保留一份通过本地策略闸门的动作的紧凑记录；真正的可见回执只在传输成功后才写。
        if script_entry and approved_automatic_outgoing_actions:
            await self.db_set('interlude_script_entry', {'id': script_entry['id']}, {
                'metadata': {
                    **_record(script_entry.get('metadata')),
                    'approvedAutomaticOutgoingActions': approved_automatic_outgoing_actions,
                },
            })
        for action in cross_actions:
            if action.get('mode') == 'immediate':
                messages.append(attach_message_event({
                    'participant_id': action.get('participantId'),
                    'content': action.get('content'),
                    'automatic_delivery': automatic_delivery,
                    'interaction': interaction or None,
                    'user_initiated': phase == 'user-message',
                }, find_outgoing_script_event(
                    commit, action.get('participantId'), 'immediate', action.get('content'), separator,
                ) if commit is not None else None, (script_entry or {}).get('id')))
            else:
                send_at_value = action.get('sendAt')
                if action.get('mode') != 'delayed' or not send_at_value:
                    continue
                send_at = parse_dt(send_at_value)
                await self.append_intent(story['id'], {
                    'type': 'cross-conversation-message',
                    'summary': 'The character planned a message to another relationship branch.',
                    'not_before': send_at_value,
                    'payload': {
                        'content': action.get('content'),
                        'userInitiated': False,
                        'crossConversation': True,
                        'willingness': action.get('willingness'),
                        'reason': action.get('reason'),
                        **({
                            **script_event_payload(attach_message_event(
                                {'participant_id': action.get('participantId'), 'content': action.get('content')},
                                find_outgoing_script_event(
                                    commit, action.get('participantId'), 'delayed', action.get('content'), separator,
                                ),
                                (script_entry or {}).get('id'),
                            )),
                        } if commit is not None else {}),
                    },
                }, now, action.get('participantId'))
                self.schedule_due_intent_wake(story['id'], send_at)

        # 可见消息只在传输成功后才被确认。后面的气泡留在内存里直到第一条到达；
        # 每个气泡都保留它来自的那条剧本事件的身份。
        prepared: list[dict[str, Any]] = []
        for message in messages:
            prepared_message = prepare_outgoing_delivery(
                message, self.split_outgoing_message(message['content']),
            )
            if prepared_message:
                prepared.append(prepared_message)
        if _cfg(self.urge_config, 'enabled', False) and script_entry and (
            is_automatic_narrative_phase(phase)
            or (phase == 'intent-due' and not any(intent.get('type') == 'narrative-retry' for intent in context_intents))
        ):
            try:
                current = await self.get_story(story['id'])
                state = decode_story_state(current.get('state'))
                target = (
                    agency_candidate['participant_id']
                    if (agency_allows_send and agency_candidate
                        and any(message.get('participant_id') == agency_candidate['participant_id'] for message in prepared))
                    else None
                )
                urge = commit_urge(
                    normalize_urge_state(_record(state.get('extensions')).get('urge'), dt_ms(now)),
                    _record(raw).get('urge'), script or '', script_entry['id'], target,
                    dt_ms(now), self.urge_config, random=self.rng,
                )
                await self.db_set('interlude_story', {'id': story['id']}, {
                    'state': encode_story_state({
                        **state, 'extensions': {**_record(state.get('extensions')), 'urge': urge},
                    }),
                    'updatedAt': now,
                })
            except Exception as error:
                # 可选的调度投影绝不能吞掉已经落库的发言。
                self.report_standalone('warn', 'Urge 调度交接保存失败，保留既有剧本与投递 错误=%s', error)
        return {'messages': prepared, 'commit': commit, 'scriptEntry': script_entry, 'script_entry': script_entry}

