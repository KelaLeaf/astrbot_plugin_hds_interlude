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

## 键名约定（键名约定）

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
import time
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
    # 上游 1.0.1-rc25：主动联系温度三模式与每日上限。
    resolve_proactive_daily_cap,
    resolve_proactive_interval_minutes,
    resolve_proactive_threshold,
)
from ..bubbles import strip_voice_marker
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
from ..story_state import (
    append_proactive_contact,
    count_proactive_contacts_in_window,
    decode_story_state,
    encode_story_state,
    normalize_continuity_snapshot,
)
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
    backfilled_quote_content,
    clip,
    describe_timeline_plan_rejection,
    detect_live_script_time_overflow,
    detect_message_repetition,
    extract_user_reported_times,
    follow_up_resolution_problems,
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

def _quote_text(value: Any, *keys: str) -> str:
    """按 camelCase 优先从引文里读字符串（缺值给空串）。"""
    if not isinstance(value, dict):
        return ''
    for key in keys:
        found = value.get(key)
        if found not in (None, ''):
            return str(found)
    return ''


def _quote_entry_id(value: Any) -> Optional[int]:
    """`msg-<条目id>` → 条目 id；不是这个形状就返回 None。"""
    for key in ('messageId', 'message_id', 'messageRef', 'message_ref', 'id'):
        raw = _quote_text(value, key).strip()
        match = re.match(r'^msg-(\d+)$', raw)
        if match:
            return int(match.group(1))
    return None


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
    # 共同作品：模型提出的修改稿 / 异步写手请求（上游 works.ts 的两个入口字段）。
    'workProposal', 'workRequest',
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


def _resync_dual(raw: Any) -> Any:
    """把顶层已知键的两种拼写重新指回**同一个**对象。

    `resolve_authored_actions` 只重写 snake_case 那份列表（`cross_conversation_actions`、
    `interaction`…），于是 `_dual` 早先补出来的 camelCase 别名会留在**解析前**的旧值上。
    而本块按键名法优先读 camelCase —— 模型用 `actionId` 引用原话时，
    跨对话动作就会因为 camel 那份没有 content 被 `_normalize_conversation_action` 丢掉。
    解析之后调用它，两边继续指向同一份数据。
    """
    if not is_record(raw):
        return raw
    updated = dict(raw)
    for key in (*_DUAL_ELEMENT_KEYS, *_DUAL_SINGLE_KEYS):
        snake = _snake(key)
        if snake in updated:
            updated[key] = updated[snake]
    interaction = _record(updated.get('interaction'))
    reply = _record(interaction.get('reply'))
    if reply and 'content' in reply:
        # `resolve_authored_actions` 换的是 `interaction` 这个新 dict，回复内容同样要同步。
        updated['interaction'] = interaction
    return updated


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


def _without_voice_marker(value: Any) -> Any:
    """删掉正文语音标记（v1.7.7）：只处理字符串，其它形状原样返回。"""
    return strip_voice_marker(value)[0] if isinstance(value, str) else value


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


async def _report_completion(
    service: Any,
    story_id: str,
    reference: Any,
    status: str,
    reason: Any,
    now: Any,
    kind: str = 'due-intent',
) -> str:
    """把一次到期投递的结局接进**统一回报通道**（v1.9.9，见 §87）。

    受控偏离：上游在这一跳之后只等下一次 sweep。回报本身炸了绝不影响已经算好的
    投递结果（只留一条 warn），宿主缺 `report_ledger_completion`（只装了 Chunk4 的
    测试宿主）时安静跳过——生产服务永远具备这个成员。
    """
    reporter = getattr(service, 'report_ledger_completion', None)
    if not callable(reporter):
        return 'unavailable'
    try:
        return await reporter(story_id, reference, status, reason, now, kind=kind)
    except Exception as error:  # noqa: BLE001 - 回报是观察性的第二跳
        service.report_standalone(
            'warn', '异步动作完成回报失败 故事=%s 错误=%s', story_id, error,
        )
        return 'failed'


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


# --------------------------------------------------------------------------- #
# 受控偏离（见移植说明 §24）：后台回合里「回答另一条对话的来信」
# --------------------------------------------------------------------------- #

def _participant_waiting_for_reply(participant: Any) -> bool:
    """这条关系分支是否还有等待她的来信（未读 / 待回）。

    只读参与者状态里的两个计数。它们是「账本上的到达记录」，不是她的注意力——
    但作为**投递归属**的证据足够：没有等待来信的分支，这一回合不可能凭空产生
    一条「回复」。
    """
    state = normalize_participant_state(_record(participant).get('state'))
    return bool(state.get('unreadMessageCount') or state.get('pendingReplyCount'))


def _route_background_reply(
    raw: Any,
    phase: str,
    participant: Any,
    all_participants: list[Any],
    permitted_participant_ids: set[str],
    permit_messages: bool,
    shared: dict[str, Any],
) -> tuple[Any, dict[str, Any]]:
    """给后台回合里的即时回复定收件人（返回改过的 raw 与一条路由结论）。

    上游语义是 `interaction.reply` 永远投给本回合的 participant。共享主剧本下，
    后台回合的 participant 来自**到期的计划**（可能只是一条关于某人的待办），
    而这一回合写出来的散文完全可能是「她翻到另一个人的未读、顺手回了话」——
    上游那条规则会把这句回话投进错的聊天窗（用户 2026-09-27 06:19 的实测：
    她回的是主人那两条未读，三条消息却落进陌生账号「汐雨.」的对话框）。

    判定顺序（全部只在「本回合没有来信」的后台回合里生效）：

    1. **模型自己指名了**（写了跨对话动作）→ 什么都不做。它已经表达清楚要发给谁，
       同内容的那条即时回复会被去掉，免得同一句话发两次。
    2. **恰好另一条分支在等她** → 把即时回复改挂到那条分支的跨对话通道（上游已有的
       「发给另一条对话」）。投递坐标、剧本事件与投递账本因此全部落在正确分支上。
    3. **两条以上分支都在等她** → 不猜。把候选报回去由调用方拦下并留一条可见记录：
       改投错人比不投更糟。
    4. 谁都没在等她 → 保持上游（这条即时回复就是一次主动联系）。

    返回的 `route` 形如 `{'redirect_to': <参与者 id>, 'ambiguous': [<参与者 id>…]}`。
    """
    route: dict[str, Any] = {'redirect_to': '', 'ambiguous': []}
    if phase != 'intent-due' or not permit_messages or not is_record(raw):
        return raw, route
    if not _cfg(shared, 'allowCrossConversationMessages', False):
        return raw, route
    if int(_cfg(shared, 'maxCrossConversationActions', 0) or 0) <= 0:
        return raw, route
    interaction = _raw_decision(raw, 'interaction')
    reply = _record(_record(interaction).get('reply'))
    content = pick(reply, 'content')
    if pick(reply, 'mode') != 'immediate' or not isinstance(content, str) or not content.strip():
        return raw, route
    current_id = _record(participant).get('id') or ''
    if not current_id or _participant_waiting_for_reply(participant):
        return raw, route
    waiting = [
        item for item in all_participants
        if _record(item).get('id') in permitted_participant_ids
        and _record(item).get('id') != current_id
        and _participant_waiting_for_reply(item)
    ]
    if not waiting:
        return raw, route
    text = content.strip()
    existing = [
        action for action in (_raw_decision(raw, 'crossConversationActions') or [])
        if is_record(action)
    ]
    named = {
        pick(action, 'participantId', 'participant_id') for action in existing
    }
    # 同一句话已经由跨对话动作发过一次：去掉即时回复，别发两遍。
    if any(
        pick(action, 'mode') == 'immediate' and pick(action, 'content') == text
        for action in existing
    ):
        updated = {**raw, 'interaction': {**_record(interaction), 'reply': {**reply, 'mode': 'none'}}}
        return updated, route
    if len(waiting) > 1:
        if not named & {_record(item).get('id') for item in waiting}:
            route['ambiguous'] = [_record(item).get('id') for item in waiting]
        return raw, route
    target_id = _record(waiting[0]).get('id')
    if not target_id or target_id in named:
        return raw, route
    updated = {**raw, 'crossConversationActions': [
        {'participantId': target_id, 'mode': 'immediate', 'content': text}, *existing,
    ]}
    updated['interaction'] = {**_record(interaction), 'reply': {**reply, 'mode': 'none'}}
    route['redirect_to'] = target_id
    return updated, route


def _browser_intent_drafts(raw: Any) -> list[Any]:
    """读模型给的浏览意图草稿：`browserIntents`（数组）与 `browserIntent`（单个对象）都认。

    **契约真相**（受控偏离，见 `docs/PORTING_NOTES.md` §79）：wire 上唯一被解析、落库、
    注入下一回合 `webContext` 的名字是 **`browserIntents`（数组，最多一项）**。
    提示词从上游 `narrator.ts:1750` 起一直写的是单数 `browserIntent`，模型照提示词吐
    单个对象时 `_raw_decision(raw, 'browserIntents')` 读不到 → 意图被**静默丢弃** →
    她永远"还在转"、结果永远回不到她手上。

    两种拼写、两种形状都收（数组 / 单对象），统一成列表交给调用方；其余形状返回空列表
    （不猜、不把脏值当意图）。调用方是本文件的三处：`_normalize_decision`、
    `try_decide` 的即时浏览分支、`persist_decision` 的落库分支。
    """
    if not is_record(raw):
        return []
    value = pick(raw, 'browserIntents', 'browser_intents')
    if value is None:
        value = pick(raw, 'browserIntent', 'browser_intent')
    if isinstance(value, list):
        return value
    if is_record(value):
        return [value]
    return []


def _browser_intent_draft_read(value: Any) -> tuple[Optional[dict[str, Any]], str]:
    """读一条浏览草稿：``(归一化草稿, 丢弃原因)``；可用时原因是空串。

    **判据只有这一处**：`_normalize_browser_intent_draft_loose`（收下可用的）与
    `browser_intent_draft_problem`（说出为什么丢）都是它的视图。

    宽容两处**模型实际写过的形状**（真机 2026-10-05：`{url, timing:"deferred",
    reason:"…"}`，mode/purpose 都没写）：
    - `mode` 缺失时按给了什么推断 —— 有公开 url 就是 `visit`，只有 query 就是 `search`；
    - `purpose` 缺失时接受 `reason` / `summary` 作为同一件事的别的写法。
    真的什么都缺才判脏值（由调用方打可见 warn，绝不静默）。
    """
    if not is_record(value):
        return None, '不是对象'
    mode = str(pick(value, 'mode') or '').strip().lower()
    query = pick(value, 'query')
    url = pick(value, 'url')
    has_query = isinstance(query, str) and bool(query.strip())
    has_url = isinstance(url, str) and bool(url.strip())
    if mode not in ('search', 'visit'):
        if has_url:
            mode = 'visit'
        elif has_query:
            mode = 'search'
        else:
            return None, '需要 mode=search|visit，且给出公开 url 或 query'
    if mode == 'search' and not has_query:
        return None, 'mode=search 需要 query'
    if mode == 'visit' and not has_url:
        return None, 'mode=visit 需要公开 url'
    purpose = ''
    for key in ('purpose', 'reason', 'summary'):
        candidate = pick(value, key)
        if isinstance(candidate, str) and candidate.strip():
            purpose = candidate
            break
    if not purpose:
        return None, '需要一个 purpose（或同义的 reason）字符串'
    draft: dict[str, Any] = {'mode': mode, 'purpose': clip(purpose, 500)}
    if has_query:
        draft['query'] = clip(query, 500)
    if has_url:
        draft['url'] = clip(url, 2_000)
    draft['timing'] = 'immediate' if pick(value, 'timing') == 'immediate' else 'deferred'
    raw_participant = pick(value, 'participantId', 'participant_id')
    if isinstance(raw_participant, str):
        draft['participantId'] = raw_participant.strip()
    return draft, ''


def _normalize_browser_intent_draft_loose(value: Any) -> Optional[dict[str, Any]]:
    """上游 `normalizeBrowserIntentDraftLoose`（`:7881`）：判据见 `_browser_intent_draft_read`。"""
    draft, _problem = _browser_intent_draft_read(value)
    return draft


def browser_intent_draft_problem(value: Any) -> str:
    """这条浏览草稿为什么不可用（空串 = 可用）；判据与归一化同一处。"""
    _draft, problem = _browser_intent_draft_read(value)
    return problem


def dropped_draft_action_kinds(raw: Any) -> list[str]:
    """被丢弃的草稿里**属于她的行动意图**都有哪些（判据只有这一处）。

    旁白被丢弃是对的（那段话从没说过），但 `browserIntents` / `followUpCommitment`
    / `intents` 这类"她打算去做什么"不该跟着无声消失：否则就会出现"她说过要去查、
    结果没人查，最后模型自己把页面内容编出来"（真机 2026-10-05 的现场）。
    """
    decision = _record(raw)
    checks = (
        ('浏览意图', len(_browser_intent_drafts(decision))),
        ('承诺回访', 1 if is_record(pick(decision, 'followUpCommitment', 'follow_up_commitment')) else 0),
        ('到期计划', _list_length(pick(decision, 'intents'))),
        ('计划更新', _list_length(pick(decision, 'intentUpdates', 'intent_updates'))),
        ('跨对话动作', _list_length(pick(decision, 'crossConversationActions', 'cross_conversation_actions'))),
    )
    return [name for name, count in checks if count > 0]


def report_dropped_draft_actions(service: Any, story: Any, phase: str, raw: Any, reason: str) -> bool:
    """草稿被丢弃时，把它带着的行动意图打成一条**可见 warn**（绝不静默）。

    规则（2026-10-05 定）：被丢弃草稿里的意图**要么落库、要么留痕**。这里选留痕 ——
    被丢的那一版旁白从没对用户说过，把里面的意图单独落库会凭空给她排一件她从没
    承诺过的事；而"说一声丢了什么"既让运维能定位，也不制造幽灵动作。
    返回是否真的打了 warn（调用方/测试用它区分"没有意图"与"有意图但被丢"）。
    """
    kinds = dropped_draft_action_kinds(raw)
    if not kinds:
        return False
    service.report_operation(
        'standard', 'warn', story, phase,
        '被丢弃的草稿里带着她的行动意图，已一并丢弃（未落库、未执行）原因=%s 项目=%s',
        reason, '、'.join(kinds),
    )
    return True


def _list_length(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


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


def _structured_group_reply_ok(value: Any) -> bool:
    """上游 `hasStructuredGroupReplyField`：顶层 groupReply 是否已构成合法群回复。"""
    if not isinstance(value, dict):
        return False
    mode = value.get('mode')
    if mode == 'none':
        return True
    if mode != 'immediate':
        return False
    content = value.get('content')
    return isinstance(content, str) and bool(content.strip())


def hoist_participantless_interaction(decision: Any, phase: str) -> tuple[Any, str]:
    """上游 1.0.1-rc22 `hoistParticipantlessInteraction`（移植版同名）。

    无参与者的回合（群聊 user-message、advance 等）如果模型把回复写进了
    `interaction.reply`，commit-builder 会为这个没有 participant 的回复生成
    `outgoing-message` 事件，结构校验直接拒掉整个提交 —— 症状是「群聊整回合静默」
    （用户 2026-09-26 的日志）。这里统一容错：

    - 私聊形态的 immediate 回复落在 `user-message` 相位且顶层还没有合法 `groupReply`
      → **提升**为 `groupReply`（`replyTo` 一并带过），清空 `interaction`；
    - 其余情形（delayed、advance 等本无回复通道的相位、已有 groupReply）
      → 只清空 `interaction`，不提升。

    返回 `(decision, 处理标签)`；标签为空串表示没动过。
    """
    if not isinstance(decision, dict):
        return decision, ''
    interaction = decision.get('interaction')
    reply = interaction.get('reply') if isinstance(interaction, dict) else None
    if not isinstance(reply, dict):
        return decision, ''
    mode = reply.get('mode')
    content = reply.get('content')
    if mode == 'none' or not isinstance(content, str) or not content.strip():
        return decision, ''
    if mode == 'immediate' and phase == 'user-message' and not _structured_group_reply_ok(decision.get('groupReply')):
        hoisted = dict(decision)
        forwarded: dict[str, Any] = {'mode': 'immediate', 'content': content}
        reply_to = reply.get('replyTo', reply.get('reply_to'))
        if reply_to:
            forwarded['replyTo'] = reply_to
        hoisted['groupReply'] = forwarded
        hoisted.pop('interaction', None)
        return hoisted, '提升为群发'
    stripped = dict(decision)
    stripped.pop('interaction', None)
    return stripped, '剥离（无回复通道）'


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
    raw = _resync_dual(resolve_authored_actions(raw, False, separator))

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
    browser_raw = _browser_intent_drafts(raw)
    browser_intents = [
        item for item in (_normalize_browser_intent_draft_loose(value) for value in browser_raw) if item
    ][:1]
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

    def _maybe_schedule_alter_analysis(
        self, alter_turn: Any, story_id: str, phase: str,
    ) -> bool:
        """**触发判定**：`advance_alter_system()` 说到了阈值就排一次侧端氛围分析。

        ⚠️ 双拼写读取（坑 41/46/53/65）：`advance_alter_system()` 返回的是 **snake_case**
        （`threshold_reached` / `source_participant_id` / `offset_expired`）。这里原先只读
        camelCase → 恒为 `None` → **触发后的侧端氛围分析从来没被排过**：真机现场是桶值攒到
        -43、阈值只有 ~9.7，页面却写「上次分析：从未」、内心天气永远空着（2026-10-05 由用户
        贴出的控制台数据暴露）。返回是否排了一次（给用例断言，不用 mock 内部状态）。
        """
        if not pick(alter_turn, 'threshold_reached', 'thresholdReached'):
            return False
        self.schedule_alter_analysis(
            story_id, phase,
            pick(alter_turn, 'source_participant_id', 'sourceParticipantId') or '',
        )
        return True
    """对应 `upstream/src/service.ts` 第 3217–4185 行的成员。"""

    # ------------------------------------------------------------------ #
    # Agency 端点门控（M3，上游 `:5013-5030`）
    # ------------------------------------------------------------------ #

    async def agency_endpoint_gate(
        self, decision: Any, agency_candidate: Any, story: Any, phase: str,
    ) -> Optional[str]:
        """主动联系前的端点门控：返回阻止原因（`None` = 放行）。

        上游在 `:5013-5030` 内联这段；本移植版抽成一个方法，好让"闸门真的会拦"
        有独立的反向用例（这一段跑的仍然是 chunk11 的真实门控实现）。

        模型能显式指定端点的唯一入口是 `crossConversationActions[*].endpointId`
        （M4 的字段，本批只**双读**、不新造）；没指定时退回历史的"最活跃端点"，
        再让最终发送侧走一遍硬门。注册表整个不可用时**保守阻止**——宁可这次不主动
        联系，也不拿一条来历不明的通道发出去。
        """
        participant_id = _record(agency_candidate).get('participant_id')
        agency_action = next((
            action for action in (_record(decision).get('crossConversationActions') or [])
            if _record(action).get('participantId') == participant_id
            and _record(action).get('mode') == 'immediate'
        ), None)
        chosen_endpoint_id = (
            _record(agency_action).get('endpointId')
            or _record(agency_action).get('endpoint_id')
            or _record(agency_candidate).get('endpointId')
            or _record(agency_candidate).get('endpoint_id')
        )
        reason: Optional[str] = None
        try:
            ensure_registry = getattr(self, 'ensure_endpoint_registry', None)
            if callable(ensure_registry):
                await ensure_registry()
            if not chosen_endpoint_id:
                resolve_endpoint = getattr(self, 'resolve_most_active_endpoint_id', None)
                if callable(resolve_endpoint):
                    chosen_endpoint_id = resolve_endpoint(participant_id)
            if chosen_endpoint_id:
                gate = getattr(self, 'endpoint_for_delivery', None)
                gated = (
                    gate(chosen_endpoint_id, 'participant-user', participant_id)
                    if callable(gate) else {}
                )
                reason = _record(gated).get('reason')
                if not reason:
                    initiate_gate = getattr(self, 'endpoint_initiate_gate_reason', None)
                    if callable(initiate_gate):
                        reason = initiate_gate(chosen_endpoint_id)
        except Exception as error:  # noqa: BLE001 - 注册表不可用即阻止（保守）
            reason = 'endpoint-registry-unavailable'
            self.report_operation(
                'diagnostic', 'warn', story, phase,
                'Agency 端点注册表不可用，阻止主动联系 参与者=%s 端点=%s 错误=%s',
                participant_id, chosen_endpoint_id or '(legacy)', error,
            )
        if reason:
            # 这是**运营层的路由失败**，不是角色决定：理由必须显式分类且可见，
            # 免得在日志或重查推理里被误当成意愿/容量拒绝（上游注释逐字）。
            self.report_operation(
                'standard', 'warn', story, phase,
                'Agency 端点门控阻止主动联系 参与者=%s 端点=%s 原因=%s',
                participant_id, chosen_endpoint_id or '(legacy)', reason,
            )
        return reason

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
                    # v1.9.9（§87）：定时的分段气泡被取消 = 一条"没发出去"的事实，
                    # 立刻回报（否则她会一直以为那条已经发出去了）。
                    await _report_completion(
                        self, story['id'], reference, 'cancelled', 'delivery-target-unavailable',
                        now, kind='due-intent',
                    )
                continue
            message: dict[str, Any] = {
                'participant_id': participant['id'],
                'content': content,
                'automatic_delivery': automatic_delivery,
                'script_event': restore_message_event(intent.get('payload'), content),
            }
            if payload.get('voice') is True:
                # 正文 `<tts/>` 指定的语音分段（意图排期时写进 payload）。
                message['voice'] = True
            delivered = await self.send_outgoing_messages(
                story, [message], None, None,
                lambda target: target.get('id') in self.interrupted_typing_participants,
                False,
            )
            if not delivered:
                if participant['id'] in self.interrupted_typing_participants:
                    continue
                if message.get('delivery_ambiguous') is True:
                    # v1.9.9（与 `chunk6.deliver_due_split_segments` 同一条判据）：
                    # 结果不可知**绝不排 30 秒重试**（请求已经写出去了，重投就是真重复）。
                    # 意图结清成终态，投递现实留在 `pending`（三态里的"结果不确定"）。
                    if message.get('script_event'):
                        await self.update_script_delivery_outcome(
                            story['id'], message['script_event'], 'pending', now,
                            'delivery-ambiguous-no-retry',
                        )
                    await self.db_set('interlude_intent', {'id': intent['id']}, {'status': 'completed', 'updatedAt': now})
                    if message.get('script_event'):
                        await _report_completion(
                            self, story['id'], message['script_event'], 'pending',
                            'delivery-ambiguous-no-retry', now, kind='due-intent',
                        )
                    continue
                if message.get('script_event'):
                    await self.update_script_delivery_outcome(
                        story['id'], message['script_event'], 'pending', now, 'delivery-unconfirmed-retry-scheduled',
                    )
                retry_at = parse_dt(dt_ms(now) + 30 * SECOND_MS)
                await self.db_set('interlude_intent', {'id': intent['id']}, {'notBefore': retry_at, 'updatedAt': now})
                self.schedule_due_intent_wake(story['id'], retry_at)
                # v1.9.9（§87）：这条定时的分段气泡**没有回执**（重试已排期）——
                # 不可知也要当场说一声"不确定"，不能让她停在"还在转"。
                if message.get('script_event'):
                    await _report_completion(
                        self, story['id'], message['script_event'], 'pending',
                        'delivery-unconfirmed-retry-scheduled', now, kind='due-intent',
                    )
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
            # 本移植版：这条分段气泡已经投出去了，熄灭"正在输入"（下一条有自己的窗口，
            # 由 `confirm_outgoing_deliveries` 按 `notBefore` 到点才点亮）。
            ender = getattr(self, 'end_typing', None)
            if callable(ender):
                await ender(participant)
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
        attachments: Optional[list[dict[str, Any]]] = None,
        sticker_groups: Optional[list[dict[str, Any]]] = None,
        historical_group_images: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        """上游 `decide(story, participant, phase, from, now, ...)`（`:3437`）。

        `attachments` 是**本移植版追加的末位可选参数**（受控偏离，见
        `docs/PORTING_NOTES.md` §29）：本轮附带的媒体种类（照片 / 表情包 / 动画表情 /
        QQ 商城表情 / 小程序卡片）。上游没有这个概念——它的适配器把种类信息丢在
        解析层，模型只能一律看到 `[图片]`。追加在末尾，位置参数调用不受影响。

        `sticker_groups` 同样追加在末尾（受控偏离 §48 甲）：**分组目录**
        （`[{groupId, name, description, count}]`，不列条目）——两级选择的第一段。
        与 `sticker_catalog` 互斥：给了分组目录就不平铺条目（那正是省 token 的地方）。

        `historical_group_images` 同样追加在末尾（上游 1.0.1-rc31 的末位参数
        `historicalGroupImages`，`service.ts:4217`）：群聊历史图片**证据**，与
        `images`（当前回合的图）分开走 —— multipart 里它是低细节块，`currentEvent`
        里单独计数（`historicalImageCount`）并带上来源元数据。

        主模型上下文的**唯一入口**。返回的 `NarrativeRequest` 是**发给模型的 wire
        format**：顶层与嵌套键全部保持上游 camelCase（见模块 docstring 第 3 条）。
        """
        started = time.perf_counter()
        # 长线指导（v1.9.9，上游 `decide` 的第一句）：每个 story 每进程只从库里加载
        # 一次 active 指导，后面 `long_horizon_prompt_projection` 才是同步的。
        # 关着时 `long_horizon_enabled()` 直接早退（零查库、零成本）。
        # `getattr` 探（与同一块里 `dispatcher` / `work_saver` 同一套写法）：只装了
        # Chunk4 的替身宿主没有这个口（生产里 chunk15 一定混在 `InterludeService` 里）。
        _horizon_enabled = getattr(self, 'long_horizon_enabled', None)
        _horizon_load = getattr(self, 'ensure_long_horizon_guidance_loaded', None)
        if callable(_horizon_enabled) and callable(_horizon_load) and _horizon_enabled():
            await _horizon_load(story['id'])
        superseded_intents = superseded_intents or []
        images = images or []
        audio = audio or []
        extra_web_context = extra_web_context or []
        quoted_messages = quoted_messages or []
        sticker_catalog = sticker_catalog or []
        sticker_groups = sticker_groups or []
        historical_group_images = historical_group_images or []
        visual_observations = visual_observations or []
        # 上游 1.0.1-rc23/rc26：到点世界事件在**本回合开始前**排水注入，于是它们本回合
        # 就出现在 recentScript 里（先进账、后写作）。`low` 只在非 user-message 相位
        # 排水，免得一条无关紧要的背景事件劫持对话回合。
        if getattr(self, 'world_seeder_available', None) and self.world_seeder_available():
            await self.drain_due_seeded_events(story, now, phase != 'user-message')
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
        # 上游 1.0.1-rc18：条数锚定只对私聊对话回合生效（群聊与推进回合不注入守卫）。
        repetition = (
            detect_message_repetition(prompt_entries)
            if (phase in ('user-message', 'conversation-follow-up') and not group_context)
            else None
        )
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
        # 本移植版：共同作品（works）的当前投影与工作模式；没启用/没这部作品时为 None。
        shared_work = None
        works_mode = None
        works_state = getattr(self, 'shared_work_state', None)
        if callable(works_state):
            try:
                shared_work = await works_state(story, participant)
                # 工作模式从 `works` 配置组读（chunk14 的 `works_config()`），缺省 main。
                config_reader = getattr(self, 'works_config', None)
                config = config_reader() if callable(config_reader) else {}
                works_mode = pick(config, 'generationMode', 'generation_mode') or 'main'
            except Exception as error:  # noqa: BLE001 - 作品投影失败不该挡住叙事
                self.report('warn', story, phase, '共同作品投影失败 错误=%s', error)
                shared_work = None
        # 本移植版：把"这一回合她实际能对平台做什么"算好随请求带下去（提示词只列启用项）。
        # 权限表、配置开关、会话身份都在 chunk12 判完；这里只负责取一份结果。
        action_scopes = ('private', 'group') if group_context else ('private',)
        platform_actions = self.available_platform_actions(
            self.resolve_action_session_role(participant), action_scopes,
        )
        # 长线指导（v1.9.9，上游 `service.ts:4357`）：软许可，不是剧本——关着或没有
        # active 时是 None，`**{…}` 展开让这个键**完全消失**（不注入空块、零成本）。
        # `getattr` 探：只装了 Chunk4 的替身宿主没有这个口（生产里 chunk15 一定在）。
        _projection = getattr(self, 'long_horizon_prompt_projection', None)
        long_horizon_guidance = _projection(story['id']) if callable(_projection) else None
        # 发给模型的请求：键名逐字保持上游 camelCase。
        request: dict[str, Any] = {
            'urgeEnabled': _cfg(self.urge_config, 'enabled', False) and not any(
                intent.get('type') == 'narrative-retry' for intent in due_intents
            ),
            'phase': phase,
            **({'longHorizonGuidance': long_horizon_guidance} if long_horizon_guidance else {}),
            'refreshContinuity': refresh_continuity,
            'outputRecovery': output_recovery,
            'story': story,
            'from': from_,
            'now': now,
            'userMessage': user_message,
            'userReportedTimes': user_reported_times,
            'images': images,
            'audio': audio,
            # 1.0.1-rc31：群聊历史图片证据（短生命周期，只活在本回合的请求里；
            # `dataUri` 只在发往视觉模型的 multipart 里出现，不进数据库 / 日志 / JSON payload）。
            'historicalGroupImages': historical_group_images,
            'visualObservations': visual_observations,
            'attachments': attachments or [],
            'timelinePlan': timeline_plan,
            'developmentTendencies': development_tendencies,
            # M4 §十：通道路由事实（注册表命中才给；单平台时为 None → 不标注）。
            'channelData': await self.channel_data_for(
                story, participant, group_context, prompt_entries,
            ) if phase != 'advance' else None,
            'writingOptions': {
                'messageSeparator': (str(_cfg(runtime, 'messageSeparator', '')).strip() or '<sep/>'),
                'splitReplyMessages': _cfg(runtime, 'splitReplyMessages', True) is not False,
                # v1.7.7：正文语音标记 `<tts/>`。关掉时这段提示词改成"不要写语音标记"
                # （模型压根不知道这个 token 存在，别教它用一个不会生效的东西）。
                'ttsEnabled': bool(self.voice_reply_enabled),
                # 上游 1.0.1-rc18：只在**私聊对话回合**注入条数守卫（推进回合与群聊不注入）。
                **({
                    'messageRepetition': repetition,
                } if repetition else {}),
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
            # 共同作品：把当前共享文本的投影与工作模式带下去（启用时才给；内容不受信）。
            **({} if shared_work is None else {'sharedWork': shared_work}),
            **({} if works_mode is None else {'worksMode': works_mode}),
            # 双拼写：提示词渲染侧两种写法都认（跨 chunk 传参的既有约定，见坑 41）。
            'platformActions': platform_actions,
            'platform_actions': platform_actions,
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
        quoted_messages = await self._backfill_quoted_messages(story, quoted_messages, prompt_entries)
        if quoted_messages:
            request['quotedMessages'] = quoted_messages
        if sticker_catalog and phase == 'user-message':
            request['stickerCatalog'] = sticker_catalog
        # 两级选择的第一段（§48 甲）：只给分组目录、不列条目。
        if sticker_groups and phase == 'user-message':
            request['stickerGroupCatalog'] = sticker_groups
        # 上轮上下文构成（v1.4.0，`docs/MEMORY_MAINTENANCE.md` §5.4）：只记装配侧的量，
        # 真正的 token 账单在模型中心的用量账里。诊断记账失败不影响本回合。
        await self.record_context_metrics(
            story, request, (time.perf_counter() - started) * 1000.0, phase,
            participant_id or '', now,
        )
        return _resync_dual(resolve_authored_actions(
            await self.narrator.decide(request), False, separator,
        ))

    async def _backfill_quoted_messages(
        self, story: Any, quoted_messages: Any, entries: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """补出被回复消息的正文（v1.4.0，`docs/MEMORY_MAINTENANCE.md` §5.5）。

        平台只给一个 id 时，模型看到的是"引用了某条看不见的消息"——比不引用更糟。
        先在本回合的条目里找，找不到再按 `msg-<条目id>` 去库里取那一条（我们自己发出去的
        消息用的就是这种 id）。补上的条目带 `backfilled: True`，日志里能分清
        "平台给的引文"和"我们补出来的引文"。
        """
        rows = [item for item in (quoted_messages or []) if isinstance(item, dict)]
        if not rows:
            return []
        resolved = list(entries or [])
        result: list[dict[str, Any]] = []
        for quote in rows:
            if _quote_text(quote, 'content').strip():
                result.append(quote)
                continue
            content = backfilled_quote_content(quote, resolved)
            if not content:
                # 合成 id 指向的条目可能不在本回合窗口里：补一次按 id 的精确查询。
                wanted = _quote_entry_id(quote)
                if wanted is not None:
                    found = await self.db_get('interlude_script_entry', {
                        'storyId': pick(story, 'id'), 'id': wanted,
                    })
                    if found:
                        resolved = [*resolved, *found]
                        content = backfilled_quote_content(quote, resolved)
            if not content:
                result.append(quote)
                continue
            result.append({**quote, 'content': content, 'backfilled': True})
        return result

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
        attachments: Optional[list[dict[str, Any]]] = None,
        sticker_groups: Optional[list[dict[str, Any]]] = None,
        historical_group_images: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        """上游 `tryDecide(...)`（`:3720`）。

        参数顺序与上游**逐字对齐**（位置参数），因为 `chunk3.flush_buffered_narrative`
        等兄弟成员按位置调用它。返回 dict 同时提供 `timelinePlan` / `timeline_plan`
        与 `effectiveNow` / `effective_now`（跨 chunk 双读）。

        `sticker_groups`（末位追加，受控偏离 §48 甲）是两级选择第一段的分组目录，
        原样透传给 `decide()`（三次重写调用都要带上，否则重写那一遍会退回平铺目录）。

        `historical_group_images`（1.0.1-rc31，上游末位参数）是群聊历史图片证据，
        同样**三次重写调用都要带上**：重写那一遍丢了它，模型就会在第一遍看见旧图、
        重写时突然看不见 —— 那是同一回合里的两份现实。
        """
        superseded_intents = superseded_intents or []
        images = images or []
        audio = audio or []
        quoted_messages = quoted_messages or []
        sticker_catalog = sticker_catalog or []
        sticker_groups = sticker_groups or []
        historical_group_images = historical_group_images or []
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
                timeline_plan, early_reply if can_early_reply else None, attachments,
                sticker_groups, historical_group_images,
            )
            immediate = None
            if (
                phase == 'user-message' and participant and not group_context
                and _cfg(browser, 'enabled', False) and _cfg(browser, 'mode') == 'allow-immediate'
            ):
                for raw_intent in _browser_intent_drafts(decision):
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
                    attachments, sticker_groups, historical_group_images,
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
                    # 上游 rc28 健康指标：结构化可见回复缺失（触发重写的那一类）。
                    health = getattr(self, 'health', None)
                    if health is not None:
                        health.record_structure_missing(pick(story, 'id'))
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
                # 与 chunk3 的"过期作废"同一条规则：被抛弃的草稿里若带着她的行动意图，
                # 也必须留一条可见 warn（判据与文案只有 report_dropped_draft_actions 一处）。
                report_dropped_draft_actions(
                    self, story, phase, decision,
                    '剧本越过当前时间终点' if initial_time_overflow else '结构化可见回复缺失',
                )
                decision = await self.decide(
                    story, participant, phase, from_, effective_now, user_message, due_intents,
                    superseded_intents, group_context, images, audio, immediate_observations, True,
                    chat_capabilities, quoted_messages, sticker_catalog, turn_query_embedding,
                    visual_observations, timeline_plan, early_reply if can_early_reply else None,
                    attachments, sticker_groups, historical_group_images,
                )
                recovered_time_overflow = detect_live_script_time_overflow(
                    _raw_decision(decision, 'script'), phase, from_, effective_now, timezone, endorsed_clocks,
                )
                if recovered_time_overflow:
                    raise ValueError(
                        'Narrative provider crossed the live time boundary after one recovery attempt: %s'
                        % recovered_time_overflow,
                    )
                if not requires_visible_reply_recovery(phase, group_context, decision):
                    # 重写把结构化回复救回来了（上游 rc28 的「挽回」计数）。
                    health = getattr(self, 'health', None)
                    if health is not None and initial_visible_recovery:
                        health.record_recovery_saved(pick(story, 'id'))
                if (
                    main_available and not early_reply_committed
                    and requires_visible_reply_recovery(phase, group_context, decision)
                ):
                    # 上游 1.0.1-rc24/rc28：两稿都缺结构化回复时**降级为无可见回复提交**，
                    # 不再抛错。旧写法会进 60 秒重试队列，弱模型下变成失败循环，而剧本
                    # 本身是好的 —— 推进不该因为一个传输字段缺失而停摆。
                    preview = safe_json_preview(_record(decision).get('interaction'))
                    if has_required_narrative_script(decision):
                        self.report_operation(
                            'standard', 'warn', story, phase,
                            '结构化回复两稿均缺失，降级为无可见回复提交（剧本推进不受影响）interaction=%s',
                            preview,
                        )
                        decision = dict(_record(decision))
                        decision.pop('interaction', None)
                        decision.pop('groupReply', None)
                    else:
                        self.report_operation(
                            'standard', 'warn', story, phase,
                            '恢复尝试仍缺失结构化回复且没有可用剧本 interaction=%s', preview,
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
        raw = _resync_dual(resolve_authored_actions(
            _dual(raw) if isinstance(raw, dict) else {},
            immediate_reply_already_delivered,
            separator,
        ))
        all_participants = await self.participants(story['id'])
        permitted_participant_ids = {
            item['id'] for item in all_participants if self.can_handle_participant(item)
        }
        # 受控偏离（见移植说明 §24）：后台回合里「回答另一条对话的来信」改走跨对话通道，
        # 免得上游那条「回复永远投给本回合 participant」把回话投进错的聊天窗。
        raw, reply_route = _route_background_reply(
            raw, phase, participant, all_participants, permitted_participant_ids,
            permit_messages, shared,
        )
        if reply_route['redirect_to']:
            self.report_operation(
                'standard', 'warn', story, phase,
                '即时回复的对话与本回合不一致，已改投等待来信的那条：本回合=%s 改投=%s',
                participant_id or '(无)', reply_route['redirect_to'],
            )
        if not participant_id:
            # 上游 1.0.1-rc22：无参与者回合的 interaction 形态回复统一容错。
            raw, hoist_label = hoist_participantless_interaction(raw, phase)
            if hoist_label:
                self.report_operation(
                    'standard', 'info', story, phase,
                    '无参与者回合已重排 interaction 回复 原mode=%s 处理=%s',
                    _record(_raw_decision(raw, 'interaction')).get('reply', {}).get('mode') if isinstance(
                        _record(_raw_decision(raw, 'interaction')).get('reply'), dict,
                    ) else '(缺失)',
                    hoist_label,
                )
        refresh_continuity = self.should_refresh_continuity(story, phase)
        decision = _normalize_decision(
            raw, from_, now, permit_messages, self.effective_urge_runtime, shared,
            participant_id or '', permitted_participant_ids, phase, self.memory_config, refresh_continuity,
        )
        # 丢一条"她申请去查"必须留痕（真机 2026-10-05：草稿写成 `{url, timing, reason}`，
        # 形状在 `_normalize_decision` 里被判脏值丢掉——旧实现在这里**一个字都不打**，
        # 于是既没有意图落库、也没有任何 warn，模型只好把页面内容编出来）。
        for browser_draft in _browser_intent_drafts(raw):
            problem = browser_intent_draft_problem(browser_draft)
            if not problem:
                continue
            self.report_operation(
                'standard', 'warn', story, phase,
                '网页浏览请求被忽略：草稿不可用（原因=%s）。需要 mode=search|visit 与 purpose'
                '（或同义的 reason）；search 还需 query，visit 还需公开 url。原样=%s',
                problem, safe_json_preview(browser_draft),
            )
        # 同一条纪律用在承诺结算上：形状不对就点名缺什么，绝不让它静默地"再等一次"。
        resolution_problems = follow_up_resolution_problems(
            _raw_decision(raw, 'followUpResolutions'),
        )
        if resolution_problems:
            self.report_operation(
                'standard', 'warn', story, phase,
                '承诺结算被忽略：followUpResolutions 形状不可用（%s）。'
                '需要 [{"id": <正整数>, "outcome": "fulfilled|rescheduled|cancelled"}]；'
                '同义的 "status" 也认。',
                '；'.join(resolution_problems),
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
        for browser_intent in _browser_intent_drafts(decision):
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
                    burst_interval = None
                    if _cfg(urge_config, 'enabled', False) and urge_burst_active(
                        normalize_urge_state(
                            _record(_record(state.get('extensions')).get('urge')), dt_ms(now),
                        ),
                        dt_ms(now), urge_config, agency_candidate['participant_id'],
                    ):
                        # 上游 1.0.1-rc25：Urge 爆发期把安全间隔压到 contactMin，**优先于**
                        # 自然/平衡模式的 30 分钟。容量硬门（设备/隐私/负荷）三模式都不变。
                        burst_interval = urge_config.get('contact_min')
                    capacity_config = {
                        **self.agency_config,
                        'minimumProactiveIntervalMinutes': resolve_proactive_interval_minutes(
                            self.agency_config, burst_interval,
                        ),
                    }
                    capacity = evaluate_agency_capacity(
                        agency_window, agency_candidate, now, capacity_config,
                        _record(_record(target).get('state')).get('lastCharacterMessageAt'),
                    )
                    willingness = agency_candidate.get('willingness') or 0
                    willingness_passes = willingness >= resolve_proactive_threshold(
                        self.agency_config,
                        _cfg(self.effective_urge_runtime, 'proactiveWillingnessThreshold', 0.65),
                    )
                    # 上游 1.0.1-rc25：每参与者每 24 小时的主动联系上限（0 = 不限）。
                    daily_cap = resolve_proactive_daily_cap(self.agency_config)
                    daily_count = count_proactive_contacts_in_window(
                        _record(state).get('proactive_contact_log'),
                        agency_candidate['participant_id'], now,
                    )
                    cap_passes = daily_cap <= 0 or daily_count < daily_cap
                    # M3：端点策略同时属于 **Agency 决策边界**与最终投递边界（上游
                    # `:5000-5030`）。动作是模型唯一能显式指定参与者端点的地方；缺省时
                    # 保留历史的"最活跃端点"解析，并让最终发送侧再走一遍硬门。
                    agency_endpoint_reason = await self.agency_endpoint_gate(
                        decision, agency_candidate, story, phase,
                    )
                    agency_policy_allows = not agency_endpoint_reason
                    agency_allows_send = (
                        agency_candidate.get('outcome') == 'send-now'
                        and bool(capacity.get('allowed')) and willingness_passes and cap_passes
                        and agency_policy_allows
                    )
                    health = getattr(self, 'health', None)
                    if health is not None:
                        # 上游 rc28：主动联系候选一次（`sent` = 真的决定发出）。
                        health.record_proactive(pick(story, 'id'), agency_allows_send)
                    if agency_allows_send:
                        # 上游：只有允许发送时才写审计窗（入列不等于发送成功）；
                        # 它是每日上限的计数来源，且**不进模型上下文**。
                        next_state['proactive_contact_log'] = append_proactive_contact(
                            _record(next_state).get('proactive_contact_log'),
                            agency_candidate['participant_id'], iso(now),
                            # 上游 M3：审计行带上实际使用的端点（多通道归因的依据）。
                            self.resolve_most_active_endpoint_id(agency_candidate['participant_id']) or '',
                        )
                    if (
                        not agency_allows_send
                        and agency_candidate.get('outcome') != 'let-go'
                        and willingness_passes
                        and cap_passes
                    ):
                        agency_recheck = {
                            'candidate': agency_candidate,
                            'window': agency_window,
                            'reason': 'model-requested-recheck' if capacity.get('allowed') else capacity.get('reason'),
                            'at': proactive_recheck_at(agency_candidate, capacity, agency_window, now),
                        }
                    self.report_operation(
                        'standard', 'info', story, phase,
                        'Agency 主动联系判断 参与者=%s 结果=%s 原因=%s 意愿=%s 模式=%s 日上限=%s/%s',
                        agency_candidate['participant_id'],
                        '立即联系' if agency_allows_send else ('稍后重查' if agency_recheck else '自然放下'),
                        capacity.get('reason') if capacity.get('allowed') or cap_passes else 'proactive-daily-cap',
                        '%.2f' % willingness,
                        self.agency_config.get('contact_mode') or 'strict',
                        daily_count, '不限' if daily_cap <= 0 else daily_cap,
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
            self._maybe_schedule_alter_analysis(alter_turn, story['id'], phase)

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
            outgoing = attach_message_event({
                'participant_id': participant['id'],
                'content': reply['content'],
                'automatic_delivery': automatic_delivery,
                'interaction': interaction or None,
                'user_initiated': phase == 'user-message',
            }, find_outgoing_script_event(
                commit, participant['id'], 'immediate', reply['content'], separator,
            ) if commit is not None else None, (script_entry or {}).get('id'))
            if reply_route['ambiguous']:
                # 受控偏离（见移植说明 §24）：本回合分支没有等待来信，而两条以上分支都在
                # 等她 —— 不猜。宁可留一条"写了没发出"的可见证据，也不把话投进错的聊天窗。
                await self.record_outgoing_delivery_failure(
                    story, participant['id'], outgoing, 'reply-target-ambiguous',
                )
                self.report_operation(
                    'standard', 'warn', story, phase,
                    '即时回复的收件人不明确（本回合=%s 没有等待来信，另有 %d 条分支在等她），'
                    '已拦下不投；要她回哪一条，请让模型用 crossConversationActions 指明',
                    participant['id'], len(reply_route['ambiguous']),
                )
            else:
                messages.append(outgoing)
        if (
            participant and permit_messages and reply.get('mode') == 'delayed'
            and reply.get('content') and reply.get('sendAt')
        ):
            send_at = parse_dt(reply['sendAt'])
            # v1.7.7：延迟回复的 payload 是**给模型看的草稿**（到期回合会重新决策），
            # 标记是投递意图、不是她要说的字——按同一口径删掉，别让 `<tts/>` 混进
            # 提示词里的 "The protagonist wanted to send …"。
            await self.append_intent(story['id'], {
                'type': 'delayed-reply',
                'summary': 'The character decided to send a delayed reply.',
                'not_before': reply['sendAt'],
                'payload': {
                    'content': _without_voice_marker(reply['content']),
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
        elif phase == 'intent-due':
            # 受控偏离（见移植说明 §24）：到期回合里「回一条还在等她的对话」是**回信**，
            # 不是主动联系——它不该被主动开关和 Agency 容量挡住，也不该被丢进错的聊天窗。
            # 判据与改投同源：目标分支确实有未读 / 待回的来信。
            waiting_ids = {
                _record(item).get('id') for item in all_participants
                if _participant_waiting_for_reply(item)
            }
            cross_actions = [
                action for action in cross_all
                if action.get('mode') == 'immediate' and action.get('participantId') in waiting_ids
            ]
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
                        'content': _without_voice_marker(action.get('content')),
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
                message, self.split_outgoing_segments(message['content']),
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
        # 长线指导（v1.9.9，上游 `service.ts:5218` 的 `void this.longHorizonSweep(story, now).catch(...)`）：
        # 剧本提交成功后**异步**扫描，不阻塞主回合。异常在
        # `schedule_long_horizon_sweep` 里就地吸收并打可见 warn（关着时它一步都不走）。
        self.schedule_long_horizon_sweep(story, now)
        return {'messages': prepared, 'commit': commit, 'scriptEntry': script_entry, 'script_entry': script_entry}

