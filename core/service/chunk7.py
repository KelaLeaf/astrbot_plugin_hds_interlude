"""`ServiceChunk7`：`upstream/src/service.ts` 第 5442–6082 行的全部成员。

主题：自动推进调度（quiet / follow-up / Preplan 锚点 / Urge 交接）、主模型标签、
参与者预置与状态读写、旧故事迁移（legacy per-account → bot-bound shared）、
连续性补齐、压缩退避与后台压缩调度、日程预排读取与持久化。

对应关系（成员起始行与上游一致，顺序照抄）：

| 上游行 | 成员 |
| --- | --- |
| 5442 | `isAutomaticAdvancePaused` |
| 5447 | `dueConversationFollowUps` |
| 5459 | `completeConversationFollowUps` |
| 5475 | `isAutomaticAdvanceDue` |
| 5485 | `pauseAutomaticAdvanceAfterUserMessage` |
| 5505 | `pauseAutomaticAdvanceAfterDelayedReply` |
| 5511 | `scheduleConversationFollowUpsAfterTurn` |
| 5543 | `scheduleNextAutomaticAdvance` |
| 5563 | `schedulePreplanAnchoredTime` |
| 5589 | `mainModelLabel` |
| 5597 | `participantPreset` |
| 5603 | `initialStorySetting` |
| 5621 | `resetParticipantCanon` |
| 5637 | `userAccountRule` |
| 5643 | `getParticipant` |
| 5647 | `recordIncomingMessage` |
| 5659 | `markParticipantSeen` |
| 5666 | `recordCharacterMessage` |
| 5676 | `updateParticipantState` |
| 5683 | `migrateLegacyStory` |
| 5736 | `migrateLegacyBranchIntoShared` |
| 5840 | `ensureContinuity` |
| 5866 | `compactionFingerprint` |
| 5872 | `compactionIsBackedOff` |
| 5881 | `noteCompactionFailure` |
| 5891 | `compactionCheckpointAdvanced` |
| 5898 | `scheduleCompaction` |
| 5986 | `compactStories` |
| 6003 | `getSchedulePreplan` |
| 6008 | `schedulePreplanEvidence` |
| 6034 | `saveSchedulePreplan` |
| 6047 | `prepareSchedulePreplanReview` |

**范围内但属于 base.py 的三个配置 getter**（`get sharedStoryConfig` 5570、
`get memoryConfig` 5752、`get browserConfig` 5802）：`base.py` 已用
`_config_cache(...)` 逐字实现了同名属性（`shared_story_config` / `memory_config` /
`browser_config`），本文件**不重复声明**，只在读取时用双拼写（camelCase / snake_case）
取值，从而与 base.py 的原始段返回、`plugin/core/service/config.py` 的
`CONFIG_DEFAULTS` 两种来源都兼容。

键名法（`docs/PORT_PLAN.md` §2）：

- 从模型 / 旧数据 / 配置读入的值一律 `pick(value, 'camelCase', 'snake_case')` 双读；
- `story.state` 经 `story_state.decode_story_state` / `encode_story_state` 归一，
  其 `automation` 子字典在本移植版内部是 snake_case（`next_advance_at` /
  `conversation_follow_up_at` …），写出前必须过 codec；
- 参与者状态（`interlude_participant.state`）是 wire format，键名保持上游
  camelCase（`unreadMessageCount` / `lastUserMessageAt` …），与
  `helpers.normalize_participant_state` 的输出一致；
- 数据库列名保持上游 camelCase。
"""

from __future__ import annotations

import asyncio
import json
import math
from datetime import datetime
from typing import Any, Callable, Optional

from ..script.life_handoff import entry_life_handoff
from ..schedule_preplan import (
    apply_schedule_preplan_proposal,
    next_schedule_preplan_transition,
    normalize_schedule_preplan_record,
    refresh_schedule_preplan,
    schedule_preplan_needs_model,
    schedule_preplan_review_due,
)
from ..story_state import decode_story_state, encode_story_state
from ..time import calendar_day_key, dt_ms, format_log_time, iso, parse_dt
from ..types import empty_participant_state, empty_story_setting
from ..urge import resolve_urge_config
from .base import ServiceBase, legacy_story_id_for, normalize_account_id, pick, story_id_for_character
from .config import COMPACTION_RETRY_BACKOFF, SCHEDULE_PREPLAN_RETRY_BACKOFF
from .session import SessionView
from .helpers import (
    _random_integer,
    active_rest_window,
    automatic_interval_minutes,
    merge_participant_state,
    normalize_follow_up_minutes,
    normalize_interaction,
    normalize_participant_state,
    timeline_entry_prompt_projection,
    to_date,
)

__all__ = ['ServiceChunk7']


def _text_value(value: Any) -> str:
    """`str` 化并去空白（`None` → 空串）。"""
    return ('' if value is None else str(value)).strip()

#: 上游 `Time.minute`。
MINUTE_MS = 60_000

#: 上游 `automaticIntervalMinutes` 读不到配置时的兜底间隔（`src/service.ts:5400`）。
DEFAULT_INTERVAL_MINUTES = 40

#: 上游 `autoAdvanceConfig.restWindows` 的默认夜间窗口（`src/service.ts:5405`）。
DEFAULT_REST_WINDOWS: list[dict[str, Any]] = [{
    'enabled': True, 'label': 'night sleep', 'start': '23:00', 'end': '07:00',
    'minIntervalMinutes': 120, 'maxIntervalMinutes': 240,
}]

#: `migrateLegacyStory` 迁移的整表清单（`src/service.ts:5710`）。
_LEGACY_MIGRATION_TABLES = (
    'interlude_script_entry', 'interlude_memory', 'interlude_intent',
    'interlude_scene', 'interlude_arc', 'interlude_fact', 'interlude_state_patch',
    'interlude_overlay_snapshot', 'interlude_web_observation', 'interlude_schedule_preplan',
)

#: 旧剧本只有单一用户，这些表的记录可以安全地挂到首个关系分支上（`src/service.ts:5717`）。
_LEGACY_ACCOUNT_TABLES = (
    'interlude_script_entry', 'interlude_memory', 'interlude_intent', 'interlude_fact',
    'interlude_state_patch', 'interlude_overlay_snapshot', 'interlude_web_observation',
)

#: `interlude_schedule_preplan` 的记录键（本移植版 snake_case）→ 数据库列名（上游 camelCase）。
_SCHEDULE_PREPLAN_COLUMNS: dict[str, str] = {
    'story_id': 'storyId',
    'revision': 'revision',
    'timezone': 'timezone',
    'valid_from': 'validFrom',
    'valid_through': 'validThrough',
    'last_reviewed_local_date': 'lastReviewedLocalDate',
    'last_evidence_entry_id': 'lastEvidenceEntryId',
    'review_reason': 'reviewReason',
    'regimes': 'regimes',
    'exceptions': 'exceptions',
    'materialized_days': 'materializedDays',
    'created_at': 'createdAt',
    'updated_at': 'updatedAt',
}


# =========================================================================== #
# 模块级小工具
# =========================================================================== #

def _camel_to_snake(name: str) -> str:
    """`lastUserMessageAt` → `last_user_message_at`。"""
    out: list[str] = []
    for index, char in enumerate(name):
        if char.isupper() and index:
            out.append('_')
        out.append(char.lower())
    return ''.join(out)


def _cfg(config: Any, camel: str, snake: Optional[str] = None, default: Any = None) -> Any:
    """读一个配置项：dict 双拼写（camel 优先），对象走属性，最后回落 `default`。

    `plugin/core/service/base.py` 的 `cachedXxxConfig` 返回的是**原始配置段**
    （上游 Koishi Console 的 YAML 是 camelCase，`config.CONFIG_DEFAULTS` 是
    snake_case），两条来源都要能读，所以读取侧一律双拼写。
    """
    if config is None:
        return default
    key = snake or _camel_to_snake(camel)
    if isinstance(config, dict):
        value = pick(config, camel, key)
        return default if value is None else value
    value = getattr(config, key, None)
    if value is None:
        value = getattr(config, camel, None)
    return default if value is None else value


def _number(value: Any, fallback: float = 0.0) -> float:
    """`Number(value) || fallback` 的等价物：非数字 / NaN / 布尔一律回落。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return float(fallback)
    number = float(value)
    return float(fallback) if number != number else number


def _js_round(value: float) -> int:
    """`Math.round`：向正无穷方向的 .5 取整（Python `round` 是银行家取整）。"""
    return int(math.floor(value + 0.5))


def _js_number_text(value: Any) -> str:
    """JS 模板字符串里的数字：整数值不写成 `10000.0`（`${chars}` 的形状）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return str(number)


def _section(config: Any, camel: str, snake: Optional[str] = None) -> dict[str, Any]:
    """读一个配置段：dict 双读段名，对象走属性，非 dict 结果一律当空。"""
    if config is None:
        return {}
    key = snake or _camel_to_snake(camel)
    if isinstance(config, dict):
        raw = pick(config, camel, key)
        return raw if isinstance(raw, dict) else {}
    raw = getattr(config, key, None)
    if raw is None:
        raw = getattr(config, camel, None)
    return raw if isinstance(raw, dict) else {}


def _urge_config(service: Any) -> dict[str, Any]:
    """上游 `get urgeConfig()`（`src/service.ts:5411`，属 chunk6）。

    chunk6 落地后 `self.urge_config` 属性（MRO 上优先于本模块）直接生效；本函数只在
    该属性缺失时（并行移植期 / chunk7 独立单测）按同一表达式
    `resolveUrgeConfig(config.urge)` 现场解析，保证 chunk7 可独立构造与验证。
    """
    resolved = getattr(service, 'urge_config', None)
    if isinstance(resolved, dict):
        return resolved
    return resolve_urge_config(_section(getattr(service, 'config', None), 'urge'))


def _memory_enabled(service: Any) -> bool:
    """上游 `this.memoryConfig.enabled`：默认 true。"""
    return _cfg(getattr(service, 'memory_config', None), 'enabled', 'enabled', True) is not False


def _schedule_preplan_enabled(service: Any) -> bool:
    """上游 `this.schedulePreplanConfig.enabled`：默认 true。"""
    return _cfg(
        getattr(service, 'schedule_preplan_config', None), 'enabled', 'enabled', True,
    ) is not False


def _auto_advance_config(service: Any) -> dict[str, Any]:
    """上游 `autoAdvanceConfig`（`src/service.ts:5395`）的读取口。

    该 getter 归 chunk6；本函数只保证 chunk7 在任意落地顺序下都能读到**同一批语义**
    的字段（缺失时用上游源码里的字面默认值），不做任何推导或夹取复制。
    """
    config = getattr(service, 'auto_advance_config', None)
    return config if isinstance(config, dict) else {}


def _encoded_state_with_automation(story: Any, patch: dict[str, Any]) -> dict[str, Any]:
    """上游 `encodeStoryState({ ...decodeStoryState(story.state), automation })`。

    `patch` 用本移植版内部的 snake_case 键；codec 负责归一成持久化的 `state` 信封。
    """
    state = decode_story_state(pick(story, 'state'))
    automation = dict(pick(state, 'automation') or {})
    automation.update(patch)
    state['automation'] = automation
    return encode_story_state(state)


def _encoded_state_with_ids(state_value: Any, arc_id: Any, scene_id: Any) -> dict[str, Any]:
    """上游 `encodeStoryState({ ...decodeStoryState(story.state), activeArcId, activeSceneId })`。"""
    state = decode_story_state(state_value)
    state['active_arc_id'] = arc_id
    state['active_scene_id'] = scene_id
    return encode_story_state(state)


def _schedule_conversation_follow_ups(anchor: datetime, config: dict[str, Any]) -> list[datetime]:
    """上游 `scheduleConversationFollowUps(anchor, config)`（`src/service.ts:8339`）。

    两次短期补写在 `followUpMinutes` 之后离散排布，抖动不会让后一次落在前一次之前。
    `helpers.py` 没有这个模块级函数，故按上游逐字移植在本模块。
    """
    minutes = normalize_follow_up_minutes(
        _cfg(config, 'followUpMinutes', 'follow_up_minutes', None)
        if _cfg(config, 'followUpMinutes', 'follow_up_minutes', None) is not None
        else _cfg(config, 'conversationFollowUpMinutes', 'conversation_follow_up_minutes', None),
    )
    jitter_range = _cfg(
        config, 'followUpJitterMinutes', 'follow_up_jitter_minutes',
        _cfg(config, 'conversationFollowUpJitterMinutes', 'conversation_follow_up_jitter_minutes', 0),
    )
    jitter_range = max(0, int(_number(jitter_range, 0)))
    previous = dt_ms(anchor)
    planned: list[datetime] = []
    for value in minutes:
        jitter = _random_integer(-jitter_range, jitter_range) if jitter_range else 0
        at = max(previous + 1_000, dt_ms(anchor) + max(1, int(value) + jitter) * MINUTE_MS)
        previous = at
        parsed = parse_dt(at)
        if parsed is not None:
            planned.append(parsed)
    return planned


def _ms_from_now(now: datetime, minutes: float) -> datetime:
    """`new Date(now.getTime() + minutes * Time.minute)`。"""
    parsed = parse_dt(dt_ms(now) + int(round(minutes * MINUTE_MS)))
    return parsed if parsed is not None else now


def _automation_of(story: Any) -> dict[str, Any]:
    """按双拼写读 `story.state.automation`。"""
    state = pick(story, 'state') or {}
    automation = pick(state, 'automation')
    return automation if isinstance(automation, dict) else {}


def _setting_timezone(story: Any) -> str:
    """`story.setting.timezone`。"""
    return str(pick(pick(story, 'setting'), 'timezone') or 'UTC')


def _trim(value: Any) -> str:
    """上游 `value?.trim() || ...` 链里的 `trim()`。"""
    return value.strip() if isinstance(value, str) else ''


def _readable_metadata(entry: Any) -> Any:
    """补全 `metadata` 里两种拼写的 `lifeHandoff` / `timelinePlan`（只补，不改原行）。

    `docs/PORT_PLAN.md` §2「键名法」要求从旧数据 / 模型输出读入时同时接受
    camelCase 与 snake_case。本移植版的底层读取器口径恰好不一致：
    `script/life_handoff.py` 读 snake_case（`life_handoff`），而
    `helpers.timeline_entry_prompt_projection` 读上游 camelCase（`timelinePlan`）。
    这里在调用前把两种拼写都补进一份**浅拷贝**，两个读取器都能命中，
    旧 Koishi 数据与模型新输出都不会被静默丢掉；原始行本身保持不变。
    """
    metadata = pick(entry, 'metadata')
    if not isinstance(metadata, dict):
        return entry
    normalized = dict(metadata)
    changed = False
    for camel, snake in (('lifeHandoff', 'life_handoff'), ('timelinePlan', 'timeline_plan')):
        value = metadata.get(camel, metadata.get(snake))
        if value is None:
            continue
        for key in (camel, snake):
            if key not in normalized:
                normalized[key] = value
                changed = True
    if not changed:
        return entry
    return {**entry, 'metadata': normalized}


# =========================================================================== #
# ServiceChunk7
# =========================================================================== #

class ServiceChunk7(ServiceBase):
    """`src/service.ts:5442-6082` 的成员（顺序与上游一致）。"""

    # ------------------------------------------------------------------ #
    # 自动推进调度（`src/service.ts:5442-5568`）
    # ------------------------------------------------------------------ #

    def is_automatic_advance_paused(self, story: Any, now: datetime) -> bool:
        """上游 `isAutomaticAdvancePaused(story, now)`（`src/service.ts:5442`）。"""
        quiet_until = to_date(pick(_automation_of(story), 'quietUntil', 'quiet_until'))
        return bool(quiet_until) and quiet_until > now

    def due_conversation_follow_ups(self, story: Any, now: datetime) -> list[datetime]:
        """上游 `dueConversationFollowUps(story, now)`（`src/service.ts:5447`）。

        Urge 启用时短期补写整条链路交给 Urge，固定 10/20 分钟档不再生效。
        """
        if _cfg(_urge_config(self), 'enabled', 'enabled', False) is True:
            return []
        raw = pick(
            _automation_of(story), 'conversationFollowUpAt', 'conversation_follow_up_at',
        )
        planned = [to_date(value) for value in raw] if isinstance(raw, list) else []
        dated = [value for value in planned if value is not None]
        dated.sort(key=dt_ms)
        return [value for value in dated if value <= now]

    async def complete_conversation_follow_ups(self, story_id: str, now: datetime) -> bool:
        """上游 `completeConversationFollowUps(storyId, now)`（`src/service.ts:5459`）。

        一次写作回合之后清掉已到期的短期补写；剩下的那条继续持久化，重载不会重跑
        10/20 分钟序列，也不会让两次补写同时触发。
        """
        story = await self.get_story(story_id)  # type: ignore[attr-defined]
        raw = pick(
            _automation_of(story), 'conversationFollowUpAt', 'conversation_follow_up_at',
        )
        candidates = [to_date(value) for value in raw] if isinstance(raw, list) else []
        remaining = [value for value in candidates if value is not None and value > now]
        remaining.sort(key=dt_ms)
        patch: dict[str, Any] = {
            'conversation_follow_up_at': [iso(value) for value in remaining],
        }
        # 上游：剩余为空时才把参与者指针抹掉（`...(remaining.length ? {} : {...})`）。
        if not remaining:
            patch['conversation_follow_up_participant_id'] = None
        patch['next_advance_at'] = iso(remaining[0]) if remaining else None
        await self.db_set(  # type: ignore[attr-defined]
            'interlude_story', {'id': pick(story, 'id')},
            {'state': _encoded_state_with_automation(story, patch), 'updatedAt': now},
        )
        return len(remaining) > 0

    def is_automatic_advance_due(self, story: Any, now: datetime) -> bool:
        """上游 `isAutomaticAdvanceDue(story, now)`（`src/service.ts:5475`）。"""
        config = _auto_advance_config(self)
        if _cfg(config, 'enabled', 'enabled', True) is not True:
            return False
        scheduled = to_date(pick(_automation_of(story), 'nextAdvanceAt', 'next_advance_at'))
        if scheduled is not None:
            return scheduled <= now
        # 早于本调度器创建的剧本没有持久化时刻：这一次按常规节奏判断，
        # 之后就会写回一个带抖动的新时刻。
        cursor_at = parse_dt(pick(story, 'cursorAt', 'cursor_at'))
        if cursor_at is None:
            return False
        interval = max(1.0, _number(
            _cfg(config, 'intervalMinutes', 'interval_minutes', DEFAULT_INTERVAL_MINUTES),
            DEFAULT_INTERVAL_MINUTES,
        ))
        return dt_ms(now) - dt_ms(cursor_at) >= interval * MINUTE_MS

    async def pause_automatic_advance_after_user_message(self, story_id: str, now: datetime) -> None:
        """上游 `pauseAutomaticAdvanceAfterUserMessage(storyId, now)`（`src/service.ts:5485`）。

        新消息一到就取消旧的"对话结束后节奏"；新节奏要等这一回合真正决定
        「现在回 / 稍后回 / 不回」之后才设定。
        """
        story = await self.get_story(story_id)  # type: ignore[attr-defined]
        if _cfg(_urge_config(self), 'enabled', 'enabled', False) is True:
            return await self.schedule_urge_advance(story, now, True)  # type: ignore[attr-defined]
        config = _auto_advance_config(self)
        interval = automatic_interval_minutes(story, now, config)
        fallback_next = await self.schedule_preplan_anchored_time(  # type: ignore[attr-defined]
            story, now, _ms_from_now(now, interval),
        )
        patch = {
            'conversation_follow_up_at': [],
            'conversation_follow_up_participant_id': None,
            'quiet_until': None,
            'last_user_message_at': iso(now),
            # 覆盖群聊闸门静默与提供者失败：这条新对话事件还没落定之前，
            # 任何旧的短期定时器都不许触发。
            'next_advance_at': iso(fallback_next),
        }
        await self.db_set(  # type: ignore[attr-defined]
            'interlude_story', {'id': pick(story, 'id')},
            {'state': _encoded_state_with_automation(story, patch), 'updatedAt': now},
        )

    async def pause_automatic_advance_after_delayed_reply(
        self, story_id: str, now: datetime, participant_id: str = '',
    ) -> None:
        """上游 `pauseAutomaticAdvanceAfterDelayedReply(storyId, now, participantId)`（`:5505`）。"""
        await self.schedule_conversation_follow_ups_after_turn(
            story_id, now, None, participant_id,
        )

    async def schedule_conversation_follow_ups_after_turn(
        self,
        story_id: str,
        now: datetime,
        raw_interaction: Any = None,
        participant_id: str = '',
    ) -> None:
        """上游 `scheduleConversationFollowUpsAfterTurn(...)`（`src/service.ts:5511`）。

        10/20 分钟的连续性补写从对话的**真实终点**起算：延迟回复以它计划发出的时刻为锚。
        """
        config = _auto_advance_config(self)
        if _cfg(config, 'enabled', 'enabled', True) is not True:
            return
        story = await self.get_story(story_id)  # type: ignore[attr-defined]
        interaction = (
            normalize_interaction(raw_interaction, now, self.runtime_config)
            if raw_interaction else None
        )
        reply = pick(interaction, 'reply') or {}
        delayed_until = (
            to_date(pick(reply, 'sendAt', 'send_at'))
            if pick(reply, 'mode') == 'delayed' else None
        )
        anchor = delayed_until if (delayed_until is not None and delayed_until > now) else now
        if _cfg(_urge_config(self), 'enabled', 'enabled', False) is True:
            return await self.schedule_urge_advance(story, anchor)  # type: ignore[attr-defined]
        # 睡眠 / 休息窗口保持低频节奏：不因为对话正好在睡前结束就在二十分钟内醒两次。
        rest_windows = _cfg(config, 'restWindows', 'rest_windows', None)
        if not isinstance(rest_windows, list):
            rest_windows = DEFAULT_REST_WINDOWS
        in_rest = active_rest_window(rest_windows, _setting_timezone(story), anchor) is not None
        follow_ups = [] if in_rest else _schedule_conversation_follow_ups(anchor, config)
        ordinary_next = (
            follow_ups[-1] if follow_ups
            else _ms_from_now(anchor, automatic_interval_minutes(story, anchor, config))
        )
        normal_next = (
            ordinary_next if follow_ups
            else await self.schedule_preplan_anchored_time(story, anchor, ordinary_next)  # type: ignore[attr-defined]
        )
        patch = {
            # 短期补写是唯一的"对话结束后"专用日程；常规 40 分钟节奏在最后一次
            # 短期补写结束后恢复，而不是从每条入站消息重新计时。
            'quiet_until': None,
            'conversation_follow_up_at': [iso(value) for value in follow_ups],
            'conversation_follow_up_participant_id': (
                (participant_id or None) if follow_ups else None
            ),
            'next_advance_at': iso(normal_next),
        }
        await self.db_set(  # type: ignore[attr-defined]
            'interlude_story', {'id': pick(story, 'id')},
            {'state': _encoded_state_with_automation(story, patch), 'updatedAt': now},
        )
        timezone = _setting_timezone(story)
        self.report_operation(  # type: ignore[attr-defined]
            'standard', 'info', story, 'conversation-follow-up',
            '已更新对话后续计划 短期补写=%s 常规推进=%s',
            '、'.join(format_log_time(value, timezone) for value in follow_ups) if follow_ups else '无',
            format_log_time(normal_next, timezone),
        )

    async def schedule_next_automatic_advance(self, story_id: str, now: datetime) -> None:
        """上游 `scheduleNextAutomaticAdvance(storyId, now)`（`src/service.ts:5543`）。"""
        config = _auto_advance_config(self)
        if _cfg(config, 'enabled', 'enabled', True) is not True:
            return
        story = await self.get_story(story_id)  # type: ignore[attr-defined]
        if _cfg(_urge_config(self), 'enabled', 'enabled', False) is True:
            return await self.schedule_urge_advance(story, now)  # type: ignore[attr-defined]
        interval_minutes = automatic_interval_minutes(story, now, config)
        ordinary_next = _ms_from_now(now, interval_minutes)
        next_advance_at = await self.schedule_preplan_anchored_time(  # type: ignore[attr-defined]
            story, now, ordinary_next,
        )
        patch = {
            'quiet_until': None,
            'conversation_follow_up_at': [],
            'conversation_follow_up_participant_id': None,
            'last_auto_advance_at': iso(now),
            'next_advance_at': iso(next_advance_at),
        }
        await self.db_set(  # type: ignore[attr-defined]
            'interlude_story', {'id': pick(story, 'id')},
            {'state': _encoded_state_with_automation(story, patch), 'updatedAt': now},
        )
        self.report_operation(  # type: ignore[attr-defined]
            'standard', 'info', story, 'advance',
            '已设置下次自动推进 时间=%s 间隔=%d分钟%s',
            format_log_time(next_advance_at, _setting_timezone(story)),
            max(1, _js_round((dt_ms(next_advance_at) - dt_ms(now)) / MINUTE_MS)),
            '（Schedule Preplan 锚点）' if next_advance_at < ordinary_next else '',
        )

    async def schedule_preplan_anchored_time(
        self, story: Any, now: datetime, ordinary_next: datetime,
    ) -> datetime:
        """上游 `schedulePreplanAnchoredTime(story, now, ordinaryNext)`（`:5563`）。

        日程预排只把自动推进**提前**到下一个固定块边界，永不推后。
        """
        config = self.schedule_preplan_config  # type: ignore[attr-defined]
        if _cfg(config, 'enabled', 'enabled', True) is not True:
            return ordinary_next
        if _cfg(config, 'anchorAutoAdvance', 'anchor_auto_advance', True) is not True:
            return ordinary_next
        schedule = await self.get_schedule_preplan(pick(story, 'id'))  # type: ignore[attr-defined]
        transition = next_schedule_preplan_transition(
            schedule, now, _setting_timezone(story), 12,
        )
        if transition is not None and transition > now and transition < ordinary_next:
            return transition
        return ordinary_next

    # ------------------------------------------------------------------ #
    # 主模型标签与参与者预置（`src/service.ts:5589-5601`）
    # ------------------------------------------------------------------ #

    def main_model_label(self) -> str:
        """上游 `mainModelLabel()`（`src/service.ts:5589`）。"""
        route = pick(self.model_routing, 'main') or {}
        providers = pick(route, 'providers')
        provider = providers[0] if isinstance(providers, list) and providers else None
        provider_label = _trim(pick(provider, 'label')) or str(pick(provider, 'id') or '')
        if pick(route, 'assigned'):
            model = pick(provider, 'model')
        else:
            model = pick(pick(route, 'target'), 'model') or pick(provider, 'model') or '未配置'
        # 上游此处可能返回 `undefined`（未分配且没有 provider）；字符串语境一律按空串。
        return '%s/%s' % (provider_label, model) if provider_label else str(model or '')

    def participant_preset(self, user_id: Any) -> Optional[dict[str, Any]]:
        """上游 `participantPreset(userId)`（`src/service.ts:5597`）。

        `find(preset => preset.enabled !== false && normalizeAccountId(preset.qq) === ...)`
        —— 注意 `enabled !== false` 意味着**缺省视为启用**。
        """
        presets = pick(self.shared_story_config, 'participantPresets', 'participant_presets')  # type: ignore[attr-defined]
        if not isinstance(presets, list):
            return None
        normalized = normalize_account_id(user_id)
        for preset in presets:
            if pick(preset, 'enabled') is False:
                continue
            if normalize_account_id(pick(preset, 'qq')) == normalized:
                return preset if isinstance(preset, dict) else None
        return None

    def initial_story_setting(self, name: Optional[str] = None) -> Any:
        """上游 `initialStorySetting(name?)`（`src/service.ts:5603`）。

        剧本创建与完全管理重置共用这份干净 Canon。
        """
        setting = empty_story_setting()
        defaults = self.story_defaults  # type: ignore[attr-defined]
        character = setting['character']
        character['name'] = (
            _trim(name)
            or _cfg(defaults, 'characterName', 'character_name', '')
            or character.get('name', '')
        )
        character['profile'] = _cfg(defaults, 'characterProfile', 'character_profile', '')
        setting['user']['display_name'] = 'Multiple participants'
        setting['user']['profile'] = _cfg(defaults, 'userProfile', 'user_profile', '')
        setting['relationship'] = _cfg(defaults, 'relationship', 'relationship', '')
        setting['world'] = _cfg(defaults, 'world', 'world', '')
        setting['perspective'] = _trim(_cfg(defaults, 'perspective', 'perspective', ''))[:1_200]
        setting['supporting_cast'] = _cfg(defaults, 'supportingCast', 'supporting_cast', '')
        setting['location'] = _cfg(defaults, 'location', 'location', '')
        setting['style'] = _cfg(defaults, 'style', 'style', '') or setting['style']
        setting['timezone'] = _cfg(defaults, 'timezone', 'timezone', '') or setting['timezone']
        return setting

    # ------------------------------------------------------------------ #
    # 参与者状态（`src/service.ts:5621-5681`）
    # ------------------------------------------------------------------ #

    async def reset_participant_canon(self, story_id: str, now: datetime) -> None:
        """上游 `resetParticipantCanon(storyId, now)`（`src/service.ts:5621`）。

        重建每个账号的关系基线并丢弃演化状态。
        """
        participants = await self.db_get('interlude_participant', {'storyId': story_id})  # type: ignore[attr-defined]
        defaults = self.story_defaults  # type: ignore[attr-defined]
        for participant in participants:
            user_id = pick(participant, 'userId', 'user_id')
            account = self.user_account_rule(user_id)
            preset = self.participant_preset(user_id)
            await self.db_set(  # type: ignore[attr-defined]
                'interlude_participant', {'id': pick(participant, 'id')}, {
                    'personId': (
                        _trim(pick(account, 'personId', 'person_id'))
                        or _trim(pick(preset, 'personId', 'person_id'))
                        or pick(participant, 'personId', 'person_id')
                        or user_id
                    ),
                    'displayName': (
                        _trim(pick(account, 'label'))
                        or _trim(pick(preset, 'label'))
                        or pick(participant, 'displayName', 'display_name')
                        or user_id
                    ),
                    'profile': (
                        _trim(pick(account, 'profile'))
                        or _trim(pick(preset, 'profile'))
                        or _cfg(defaults, 'userProfile', 'user_profile', '')
                    ),
                    'relationship': (
                        _trim(pick(account, 'relationship'))
                        or _trim(pick(preset, 'relationship'))
                        or _cfg(defaults, 'relationship', 'relationship', '')
                    ),
                    'state': empty_participant_state(),
                    'updatedAt': now,
                },
            )

    def user_account_rule(self, user_id: Any) -> Optional[dict[str, Any]]:
        """上游 `userAccountRule(userId)`（`src/service.ts:5637`）。"""
        accounts = _cfg(
            _section(self.config, 'onebot'), 'userAccounts', 'user_accounts', [],
        )
        if not isinstance(accounts, list):
            return None
        normalized = normalize_account_id(user_id)
        for account in accounts:
            if pick(account, 'enabled') is False:
                continue
            if normalize_account_id(pick(account, 'qq')) == normalized:
                return account if isinstance(account, dict) else None
        return None

    async def get_participant(self, participant_id: str) -> Optional[dict[str, Any]]:
        """上游 `getParticipant(id)`（`src/service.ts:5643`）。"""
        rows = await self.db_get('interlude_participant', {'id': participant_id})  # type: ignore[attr-defined]
        return rows[0] if rows else None

    async def record_incoming_message(self, participant: Any, now: datetime) -> dict[str, Any]:
        """上游 `recordIncomingMessage(participant, now)`（`src/service.ts:5647`）。

        `ParticipantState` 是持久化 wire format，键名保持上游 camelCase。
        """
        current = normalize_participant_state(pick(participant, 'state'))
        state = {
            **current,
            'unreadMessageCount': int(current.get('unreadMessageCount') or 0) + 1,
            'pendingReplyCount': int(current.get('pendingReplyCount') or 0) + 1,
            'lastUserMessageAt': iso(now),
        }
        await self.db_set(  # type: ignore[attr-defined]
            'interlude_participant', {'id': pick(participant, 'id')},
            {'state': state, 'updatedAt': now},
        )
        return {**participant, 'state': state, 'updatedAt': now}

    async def mark_participant_seen(self, participant: Any, now: datetime) -> dict[str, Any]:
        """上游 `markParticipantSeen(participant, now)`（`src/service.ts:5659`）。"""
        current = normalize_participant_state(pick(participant, 'state'))
        state = {**current, 'unreadMessageCount': 0}
        await self.db_set(  # type: ignore[attr-defined]
            'interlude_participant', {'id': pick(participant, 'id')},
            {'state': state, 'updatedAt': now},
        )
        return {**participant, 'state': state, 'updatedAt': now}

    async def record_character_message(self, participant: Any, now: datetime) -> dict[str, Any]:
        """上游 `recordCharacterMessage(participant, now)`（`src/service.ts:5666`）。"""
        current = normalize_participant_state(pick(participant, 'state'))
        state = {
            **current, 'unreadMessageCount': 0, 'pendingReplyCount': 0,
            'lastCharacterMessageAt': iso(now),
        }
        await self.db_set(  # type: ignore[attr-defined]
            'interlude_participant', {'id': pick(participant, 'id')},
            {'state': state, 'updatedAt': now},
        )
        return {**participant, 'state': state, 'updatedAt': now}

    async def update_participant_state(
        self, participant: Any, patch: dict[str, Any], now: datetime,
    ) -> dict[str, Any]:
        """上游 `updateParticipantState(participant, patch, now)`（`src/service.ts:5676`）。"""
        state = merge_participant_state(normalize_participant_state(pick(participant, 'state')), patch)
        await self.db_set(  # type: ignore[attr-defined]
            'interlude_participant', {'id': pick(participant, 'id')},
            {'state': state, 'updatedAt': now},
        )
        return {**participant, 'state': state, 'updatedAt': now}

    # ------------------------------------------------------------------ #
    # 旧故事迁移（`src/service.ts:5683-5750`）
    # ------------------------------------------------------------------ #

    async def migrate_legacy_story(self, legacy: Any, session: Any) -> Any:
        """上游 `migrateLegacyStory(legacy, session)`（`src/service.ts:5683`）。

        把一部旧的"账号绑定"剧本一次性转换成"机器人绑定"的共享剧本。
        """
        now = self.now()  # type: ignore[attr-defined]
        story_id = story_id_for_character(pick(session, 'platform'), pick(session, 'selfId', 'self_id'))
        rows = await self.db_get('interlude_story', {'id': story_id})  # type: ignore[attr-defined]
        existing = rows[0] if rows else None
        if existing:
            await self.migrate_legacy_branch_into_shared(existing, session)
            await self.ensure_continuity(existing, now)
            return existing
        legacy_id = pick(legacy, 'id')
        story = {
            **legacy,
            'id': story_id,
            'platform': pick(session, 'platform'),
            'selfId': pick(session, 'selfId', 'self_id'),
            'userId': '',
            'channelId': '',
            'state': decode_story_state(pick(legacy, 'state')),
            'updatedAt': now,
        }
        try:
            await self.db_create('interlude_story', story)  # type: ignore[attr-defined]
        except Exception as error:
            # 两个旧账号同时首次到访时都会判定"共享行不存在"。加入赢得主键竞争的那一行，
            # 把本条分支并进去，而不是留下一个仍然 active 的旧副本。
            raced_rows = await self.db_get('interlude_story', {'id': story_id})  # type: ignore[attr-defined]
            raced = raced_rows[0] if raced_rows else None
            if not raced:
                raise error
            await self.migrate_legacy_branch_into_shared(raced, session)
            await self.ensure_continuity(raced, now)
            return raced
        participant = await self.ensure_participant(story, session, now)  # type: ignore[attr-defined]
        for table in _LEGACY_MIGRATION_TABLES:
            await self.db_set(table, {'storyId': legacy_id}, {'storyId': story_id})  # type: ignore[attr-defined]
        # 旧剧本只有一个用户，账号绑定的记录可以在迁移时安全地挂到那条初始关系分支上。
        for table in _LEGACY_ACCOUNT_TABLES:
            await self.db_set(  # type: ignore[attr-defined]
                table, {'storyId': story_id}, {'participantId': pick(participant, 'id')},
            )
        await self.db_set(  # type: ignore[attr-defined]
            'interlude_story', {'id': legacy_id}, {'status': 'archived', 'updatedAt': now},
        )
        await self.ensure_continuity(story, now)
        return story

    async def migrate_legacy_branch_into_shared(self, story: Any, session: Any) -> None:
        """上游 `migrateLegacyBranchIntoShared(story, session)`（`src/service.ts:5736`）。

        一个部署里可能有多部旧的按账号切割的剧本。第一部创建了共享剧本之后，其余旧分支
        在各自用户回来时并进来；否则它们会继续被后台扫描并行推进，让同一个角色活出第二条命。
        """
        legacy_id = legacy_story_id_for(
            pick(session, 'platform'), pick(session, 'selfId', 'self_id'), pick(session, 'userId', 'user_id'),
        )
        if legacy_id == pick(story, 'id'):
            return
        rows = await self.db_get('interlude_story', {'id': legacy_id})  # type: ignore[attr-defined]
        legacy = rows[0] if rows else None
        if not legacy or pick(legacy, 'status') == 'archived':
            return
        now = self.now()  # type: ignore[attr-defined]
        participant = await self.ensure_participant(story, session, now)  # type: ignore[attr-defined]
        for table in _LEGACY_ACCOUNT_TABLES:
            await self.db_set(  # type: ignore[attr-defined]
                table, {'storyId': pick(legacy, 'id')},
                {'storyId': pick(story, 'id'), 'participantId': pick(participant, 'id')},
            )
        await self.db_set(  # type: ignore[attr-defined]
            'interlude_story', {'id': pick(legacy, 'id')}, {'status': 'archived', 'updatedAt': now},
        )
        await self.append_entry(  # type: ignore[attr-defined]
            pick(story, 'id'), {
                'kind': 'legacy-branch-merged', 'actor': 'system',
                'content': 'Earlier account-specific history for %s was merged into the shared story.'
                           % pick(participant, 'displayName', 'display_name'),
                'occurredAt': iso(now), 'metadata': {'legacyStoryId': pick(legacy, 'id')},
            }, now, pick(participant, 'id'),
        )
        await self.ensure_continuity(story, now)

    async def merge_story_into_canonical(
        self, source_story_id: Any, target_story_id: Any = '',
    ) -> dict[str, Any]:
        """把一部剧本并入共享主剧本（**本移植版扩展**，控制台「并入主剧本」用）。

        为什么需要它：上游的惰性合并（`migrateLegacyBranchIntoShared`）只处理
        "这个账号回来时**还 active** 的那条旧分支"，而单剧本守卫 `getCanonicalStory`
        在**任何人**发消息时就会把其余 active 剧本归档，归档后那条分支再也不会被合并
        ——于是从"每个 QQ 一部剧本"升级到共享主剧本时，先被归档的那部剧本虽然
        内容还在库里（条目、记忆、事实一条不少），却不会自己走进主剧本。
        控制台用这个入口把用户选中的剧本显式并进去。

        语义与 `migrate_legacy_branch_into_shared` 一致：搬 `storyId`（分支表同时改挂
        到迁移出来的那条关系分支上）、源剧本转 `archived`、追加一条
        `legacy-branch-merged` 系统条目。返回搬运统计。上游没有这个入口，属于
        「控制台专属能力」，因此不改动 `findStory` 的任何行为。
        """
        source_id = _text_value(source_story_id)
        target_id = _text_value(target_story_id)
        source_rows = await self.db_get('interlude_story', {'id': source_id})  # type: ignore[attr-defined]
        source = source_rows[0] if source_rows else None
        if not source:
            raise LookupError('source-story-not-found')
        if not target_id:
            target_id = await self.canonical_story_id()
        if not target_id:
            raise LookupError('target-story-not-found')
        if target_id == source_id:
            raise ValueError('same-story')
        target_rows = await self.db_get('interlude_story', {'id': target_id})  # type: ignore[attr-defined]
        target = target_rows[0] if target_rows else None
        if not target:
            raise LookupError('target-story-not-found')
        # 目标必须是**活动**剧本：往归档剧本里搬内容等于把内容搬进死档案。
        # 不要求目标已经是 `character:…`——升级后还没人说过话时，canonical 仍是旧的
        # 按账号剧本，它会在这部剧本的下一条消息里被 `migrateLegacyStory` 迁移成共享
        # 剧本，那时并进来的内容跟着一起走，最终结果一致。
        if _text_value(pick(target, 'status')) != 'active':
            raise ValueError('target-not-active')

        now = self.now()  # type: ignore[attr-defined]
        # ⚠️ 必须是真 `SessionView`（或任何实现 `__getitem__` 双读的视图）：
        # service 层的 `pick()` **只认 dict 与 `__getitem__`**，不读对象属性——
        # 自造一个只有属性的小对象会让 `pick(session, 'userId', 'user_id')` 全返回
        # None，参与者 id 直接变成 `None:None:None`（实测踩过）。
        session = SessionView(
            platform=pick(source, 'platform'),
            self_id=pick(source, 'selfId', 'self_id') or '',
            user_id=pick(source, 'userId', 'user_id') or '',
            channel_id=pick(source, 'channelId', 'channel_id') or '',
        )
        participant = await self.ensure_participant(target, session, now)  # type: ignore[attr-defined]
        participant_id = pick(participant, 'id')
        moved = 0
        for table in _LEGACY_ACCOUNT_TABLES:
            rows = await self.db_get(table, {'storyId': source_id}, {'limit': 100_000})  # type: ignore[attr-defined]
            moved += len(rows)
            await self.db_set(  # type: ignore[attr-defined]
                table, {'storyId': source_id},
                {'storyId': target_id, 'participantId': participant_id},
            )
        for table in _LEGACY_MIGRATION_TABLES:
            if table in _LEGACY_ACCOUNT_TABLES:
                continue
            rows = await self.db_get(table, {'storyId': source_id}, {'limit': 100_000})  # type: ignore[attr-defined]
            moved += len(rows)
            await self.db_set(table, {'storyId': source_id}, {'storyId': target_id})  # type: ignore[attr-defined]
        await self.db_set(  # type: ignore[attr-defined]
            'interlude_story', {'id': source_id}, {'status': 'archived', 'updatedAt': now},
        )
        await self.append_entry(  # type: ignore[attr-defined]
            target_id, {
                'kind': 'legacy-branch-merged', 'actor': 'system',
                'content': 'Earlier account-specific history for %s was merged into the shared story.'
                           % pick(participant, 'displayName', 'display_name'),
                'occurredAt': iso(now), 'metadata': {'legacyStoryId': source_id},
            }, now, participant_id,
        )
        await self.ensure_continuity(target, now)
        return {
            'source': source_id,
            'target': target_id,
            'participant_id': participant_id,
            'moved': moved,
        }

    async def canonical_story_id(self) -> str:
        """当前共享主剧本的 id（`character:…`），没有就返回空串。

        单剧本守卫保证"同时只有一部 active 剧本"，但升级后第一次有人说话之前也可能
        出现多部 active（旧库遗留），此时按 `updatedAt` 取最新的一部。
        """
        rows = await self.db_get(  # type: ignore[attr-defined]
            'interlude_story', {'status': 'active'}, {'sort': {'updatedAt': 'DESC'}, 'limit': 50},
        )
        for row in rows:
            if str(pick(row, 'id')).startswith('character:'):
                return str(pick(row, 'id'))
        return str(pick(rows[0], 'id')) if rows else ''

    # ------------------------------------------------------------------ #
    # 连续性（`src/service.ts:5840-5864`）
    # ------------------------------------------------------------------ #

    async def ensure_continuity(self, story: Any, now: datetime) -> None:
        """上游 `ensureContinuity(story, now)`（`src/service.ts:5840`）。

        每部剧本始终应有一个活动场景与一条活动弧线。旧数据升级或手动关闭场景之后，
        这里负责补齐，并把 id 缓存进 `story.state` 供 Console / 外部工具查看。
        """
        story_id = pick(story, 'id')
        arc = await self.active_arc(story_id)  # type: ignore[attr-defined]
        if not arc:
            await self.db_create('interlude_arc', {  # type: ignore[attr-defined]
                'storyId': story_id, 'status': 'active', 'title': 'Beginning', 'summary': '',
                'sceneCount': 0, 'createdAt': now, 'updatedAt': now,
            })
            arc = await self.active_arc(story_id)  # type: ignore[attr-defined]
        scene = await self.active_scene(story_id)  # type: ignore[attr-defined]
        if not scene:
            await self.db_create('interlude_scene', {  # type: ignore[attr-defined]
                'storyId': story_id, 'status': 'active', 'startedAt': now, 'endedAt': None,
                'hook': '', 'summary': '', 'entryCount': 0, 'lastEntryId': None,
                'createdAt': now, 'updatedAt': now,
            })
            scene = await self.active_scene(story_id)  # type: ignore[attr-defined]
            if arc:
                await self.db_set(  # type: ignore[attr-defined]
                    'interlude_arc', {'id': pick(arc, 'id')},
                    {
                        'sceneCount': int(_number(pick(arc, 'sceneCount', 'scene_count'), 0)) + 1,
                        'updatedAt': now,
                    },
                )
        if not arc or not scene:
            return
        state = decode_story_state(pick(story, 'state'))
        if (
            pick(state, 'activeArcId', 'active_arc_id') != pick(arc, 'id')
            or pick(state, 'activeSceneId', 'active_scene_id') != pick(scene, 'id')
        ):
            await self.db_set(  # type: ignore[attr-defined]
                'interlude_story', {'id': story_id},
                {'state': _encoded_state_with_ids(state, pick(arc, 'id'), pick(scene, 'id')), 'updatedAt': now},
            )

    # ------------------------------------------------------------------ #
    # 压缩退避与调度（`src/service.ts:5866-6001`）
    # ------------------------------------------------------------------ #

    def compaction_fingerprint(self, scene: Any, entries: list[Any], chars: float) -> str:
        """上游 `compactionFingerprint(scene, entries, chars)`（`src/service.ts:5866`）。"""
        first = pick(entries[0], 'id') if entries else None
        last = pick(entries[-1], 'id') if entries else None
        return '%s:%s:%s-%s:%s:%s' % (
            pick(scene, 'id'), pick(scene, 'lastEntryId', 'last_entry_id') or 0,
            first if first is not None else 0, last if last is not None else 0,
            len(entries), _js_number_text(chars),
        )

    def compaction_is_backed_off(
        self, story_id: str, fingerprint: str, now: Any = None,
    ) -> bool:
        """上游 `compactionIsBackedOff(storyId, fingerprint, now = Date.now())`（`:5872`）。"""
        now_ms = self.now_ms() if now is None else (  # type: ignore[attr-defined]
            dt_ms(now) if isinstance(now, datetime) else int(_number(now, 0))
        )
        backoff = self.compaction_backoff.get(story_id)
        until = _number(pick(backoff, 'until'), 0) if backoff else 0
        if not backoff or pick(backoff, 'fingerprint') != fingerprint or now_ms >= until:
            if backoff and now_ms >= until:
                self.compaction_backoff.pop(story_id, None)
            return False
        return True

    def note_compaction_failure(self, story_id: str, fingerprint: str, error: Any) -> None:
        """上游 `noteCompactionFailure(storyId, fingerprint, error)`（`src/service.ts:5881`）。"""
        until = self.now_ms() + COMPACTION_RETRY_BACKOFF  # type: ignore[attr-defined]
        self.compaction_backoff[story_id] = {'fingerprint': fingerprint, 'until': until}
        self.report_standalone_operation(  # type: ignore[attr-defined]
            'diagnostic', 'debug', '记忆整理进入冷却 故事=%s 冷却至=%s 错误=%s',
            story_id, iso(parse_dt(until)), error,
        )

    async def compaction_checkpoint_advanced(self, context: Any) -> bool:
        """上游 `compactionCheckpointAdvanced(context)`（`src/service.ts:5891`）。

        只有提供者返回还不够：如果写入丢了或被中断，每回合重试同一段范围会重新制造
        这个守卫本来要阻止的烧 token 循环。
        """
        scene_entries = pick(context, 'sceneEntries', 'scene_entries') or []
        expected_last_entry_id = pick(scene_entries[-1], 'id') if scene_entries else None
        if not expected_last_entry_id:
            return True
        scene = pick(context, 'scene') or {}
        rows = await self.db_get('interlude_scene', {'id': pick(scene, 'id')})  # type: ignore[attr-defined]
        persisted = rows[0] if rows else None
        if not persisted:
            return False
        return (
            _number(pick(persisted, 'lastEntryId', 'last_entry_id'), 0) >= expected_last_entry_id
            or pick(persisted, 'status') == 'closed'
        )

    def schedule_compaction(self, story_id: str) -> None:
        """上游 `scheduleCompaction(storyId)`（`src/service.ts:5898`）逐条移植。

        排队一次后台整理：先排队列上做便宜的读取与到期判定，模型调用**刻意在队列之外**
        进行（Promise 链式串行不能从自己正在跑的任务里重入，否则整条故事队列死锁），
        之后再排队列写库。
        """
        if (
            (not _memory_enabled(self) and not _schedule_preplan_enabled(self))
            or story_id in self.scheduled_compactions
        ):
            return
        self.scheduled_compactions.add(story_id)
        self.report_standalone_operation(  # type: ignore[attr-defined]
            'diagnostic', 'debug', '记忆整理已排队 故事=%s', story_id,
        )

        def run() -> None:
            if self.desktop_runtime_phase == 'paused' or self.database_resetting:  # type: ignore[attr-defined]
                self.scheduled_compactions.discard(story_id)
                return
            # 让正在跑或处于防抖窗口里的用户回合先走：即使对话很密，
            # 压缩也完全不在延迟敏感的路径上。
            if self.has_pending_narrative(story_id):  # type: ignore[attr-defined]
                self.report_standalone_operation(  # type: ignore[attr-defined]
                    'diagnostic', 'debug', '记忆整理等待前台回合结束 故事=%s', story_id,
                )
                self.ctx.set_timeout(run, 500)  # type: ignore[attr-defined]
                return

            async def execute() -> None:
                try:
                    # 阶段 1（串行）：便宜的读取与到期判定。
                    async def phase_one() -> Optional[dict[str, Any]]:
                        if self.has_pending_narrative(story_id):  # type: ignore[attr-defined]
                            return None
                        story = await self.get_story(story_id)  # type: ignore[attr-defined]
                        review = await self.prepare_schedule_preplan_review(story, self.now())  # type: ignore[attr-defined]
                        context = await self.prepare_compaction(story, self.now(), False)  # type: ignore[attr-defined]
                        return {'story': story, 'review': review, 'context': context}

                    prepared = await self.serial(story_id, phase_one)  # type: ignore[attr-defined]
                    if not prepared:
                        return
                    story = prepared['story']
                    review = prepared['review']
                    context = prepared['context']
                    needs_model = bool(pick(review, 'needsModel', 'needs_model')) if review else False
                    if not needs_model and (
                        not context or pick(context, 'phase') == 'skip'
                    ):
                        return
                    # 阶段 2：昂贵的模型调用，刻意在队列之外。
                    started_at = self.now_ms()  # type: ignore[attr-defined]
                    run_context = context if (context and pick(context, 'phase') == 'run') else None
                    scene_entries = (pick(run_context, 'sceneEntries', 'scene_entries') or []) if run_context else []
                    self.report_operation(  # type: ignore[attr-defined]
                        'standard', 'info', story, 'advance',
                        '后台整理开始 条目=%d 字符=%d 场景压缩=%s SchedulePreplan=%s',
                        len(scene_entries),
                        int(_number(pick(run_context, 'chars'), 0)) if run_context else 0,
                        bool(pick(run_context, 'sceneCompactionDue', 'scene_compaction_due')) if run_context else False,
                        needs_model,
                    )
                    schedule_proposal: Any = None
                    if needs_model and pick(review, 'request'):
                        try:
                            schedule_proposal = await self.request_schedule_preplan(  # type: ignore[attr-defined]
                                story, pick(review, 'request'),
                            )
                        except Exception as error:
                            # 即使提供者在返回提案前就抛错，也要让审查检查点继续前进：
                            # 持久化阶段会保留现有计划（或建立空的首份审查），
                            # 避免同一天的请求在每次维护扫描里重复触发。
                            self.report(  # type: ignore[attr-defined]
                                'warn', story, 'advance',
                                'Schedule Preplan 调用失败，将保存本日审查状态：%s', error,
                            )
                    decision: dict[str, Any] = {}
                    compaction_error: Optional[BaseException] = None
                    if run_context:
                        try:
                            decision = await self.compactor.compact(  # type: ignore[attr-defined]
                                pick(run_context, 'compactRequest', 'compact_request'),
                            )
                        except Exception as error:
                            compaction_error = error
                            self.note_compaction_failure(
                                story_id, str(pick(run_context, 'fingerprint') or ''), error,
                            )
                            self.report(  # type: ignore[attr-defined]
                                'warn', pick(run_context, 'current'), 'advance', '记忆压缩失败：%s', error,
                            )
                    # 阶段 3（串行）：便宜的写库，模型调用之后重新排队。
                    async def phase_three() -> None:
                        if self.database_resetting:  # type: ignore[attr-defined]
                            return
                        if needs_model:
                            persisted = await self.persist_schedule_preplan_review(  # type: ignore[attr-defined]
                                story, review, schedule_proposal, self.now(),
                            )
                            if persisted:
                                self.schedule_preplan_backoff.pop(story_id, None)
                            else:
                                self.schedule_preplan_backoff[story_id] = (  # type: ignore[attr-defined]
                                    self.now_ms() + SCHEDULE_PREPLAN_RETRY_BACKOFF  # type: ignore[attr-defined]
                                )
                        # Schedule Preplan 是独立的后台工作：一次场景压缩失败不能丢掉
                        # 已经完成的审查，把场景留给它自己的冷却即可。
                        if run_context and compaction_error is None:
                            await self.apply_compaction(  # type: ignore[attr-defined]
                                pick(run_context, 'current'), run_context, decision, self.now(), started_at,
                            )
                            if not await self.compaction_checkpoint_advanced(run_context):
                                raise RuntimeError(
                                    'Compaction checkpoint did not advance (scene=%s, expected=%s)' % (
                                        pick(pick(run_context, 'scene') or {}, 'id'),
                                        pick((pick(run_context, 'sceneEntries', 'scene_entries') or [None])[-1], 'id') or 0,
                                    ),
                                )

                    try:
                        await self.serial(story_id, phase_three)  # type: ignore[attr-defined]
                        if run_context and compaction_error is None:
                            self.compaction_backoff.pop(story_id, None)
                    except Exception as error:
                        if run_context and compaction_error is None:
                            self.note_compaction_failure(
                                story_id, str(pick(run_context, 'fingerprint') or ''), error,
                            )
                        raise
                except Exception as error:
                    self.report_standalone_operation(  # type: ignore[attr-defined]
                        'diagnostic', 'debug', '记忆压缩跳过 错误=%s', error,
                    )
                finally:
                    self.scheduled_compactions.discard(story_id)

            asyncio.ensure_future(execute())

        run()

    async def compact_stories(self) -> None:
        """上游 `compactStories()`（`src/service.ts:5986`）。"""
        memory_enabled = _memory_enabled(self)
        if (
            self.desktop_runtime_phase == 'paused'  # type: ignore[attr-defined]
            or (not memory_enabled and not _schedule_preplan_enabled(self))
            or self.compaction_sweep_running  # type: ignore[attr-defined]
        ):
            return
        self.compaction_sweep_running = True  # type: ignore[attr-defined]
        try:
            story = await self.get_canonical_story()  # type: ignore[attr-defined]
            if not story or not self.can_handle_story(story):  # type: ignore[attr-defined]
                return
            story_id = pick(story, 'id')
            if memory_enabled:
                self.schedule_fact_embedding_backfill(story_id)  # type: ignore[attr-defined]
            embedding = _section(self.config, 'model')
            embedding = _section(embedding, 'embedding')
            if _cfg(embedding, 'semanticHistory', 'semantic_history', False) is True:
                async def backfill() -> None:
                    try:
                        await self.backfill_history_embeddings(story_id)  # type: ignore[attr-defined]
                    except Exception as error:
                        self.report_standalone_operation(  # type: ignore[attr-defined]
                            'diagnostic', 'debug', '历史向量补齐跳过 错误=%s', error,
                        )

                asyncio.ensure_future(backfill())
            self.schedule_compaction(story_id)
        finally:
            self.compaction_sweep_running = False  # type: ignore[attr-defined]

    # ------------------------------------------------------------------ #
    # 日程预排（`src/service.ts:6003-6081`）
    # ------------------------------------------------------------------ #

    async def get_schedule_preplan(self, story_id: str) -> Any:
        """上游 `getSchedulePreplan(storyId)`（`src/service.ts:6003`）。"""
        rows = await self.db_get('interlude_schedule_preplan', {'storyId': story_id})  # type: ignore[attr-defined]
        return normalize_schedule_preplan_record(rows[0] if rows else None)

    async def schedule_preplan_evidence(self, story_id: str, after_entry_id: float) -> list[Any]:
        """上游 `schedulePreplanEvidence(storyId, afterEntryId)`（`src/service.ts:6008`）。

        重复日程属于主角，但原始私密散文不得送进后台日程模型。自动生成的剧本已经带着
        宿主校验过的 timelinePlan，所以投影那份安全账本，而不是把所有参与者生活事件都丢掉。

        上游的 `id: { $gt: afterEntryId }` 是范围查询，本移植版的 `Database.all` 只支持等值
        `where`（见 `base.ServiceBase.db_get`），因此在 Python 侧过滤：条目按 `occurredAt`
        升序取回后剔除 `id <= afterEntryId` 再截断 60 条。没有游标时可以直接用 SQL 的
        `LIMIT 60`，行为与上游逐字一致。
        """
        after = int(_number(after_entry_id, 0))
        options: dict[str, Any] = {'sort': {'occurredAt': 'ASC'}}
        rows = await self.db_get(  # type: ignore[attr-defined]
            'interlude_script_entry', {'storyId': story_id, 'kind': 'script'},
            options if after > 0 else {**options, 'limit': 60},
        )
        if after > 0:
            entries = [row for row in rows if _number(pick(row, 'id'), 0) > after][:60]
        else:
            entries = list(rows)
        if _cfg(self.shared_story_config, 'shareParticipantDetails', 'share_participant_details', False) is True:  # type: ignore[attr-defined]
            return entries
        projected_entries: list[Any] = []
        for entry in entries:
            if not pick(entry, 'participantId', 'participant_id'):
                projected_entries.append(entry)
                continue
            metadata = pick(entry, 'metadata')
            metadata = metadata if isinstance(metadata, dict) else {}
            readable = _readable_metadata(entry)
            if pick(metadata, 'narrativeAuthority', 'narrative_authority') == 'original-v2':
                handoff = entry_life_handoff(readable)
                activity = pick(handoff, 'activity')
                place = pick(handoff, 'place')
                if not activity and not place:
                    continue
                # 只有主角那点具体的本地字段会跨进日程读取器，
                # 私密散文 / 对白 / 在场 / 引用一律不过去。
                payload: dict[str, Any] = {'sourceEntryId': pick(entry, 'id')}
                observed_at = parse_dt(pick(entry, 'occurredAt', 'occurred_at'))
                if observed_at is not None:
                    payload['observedAt'] = iso(observed_at)
                # `JSON.stringify` 会丢掉值为 undefined 的键，这里必须同样省略。
                if place:
                    payload['place'] = pick(place, 'value')
                if activity:
                    payload['activity'] = pick(activity, 'value')
                projected_entries.append({
                    **entry,
                    'participantId': '',
                    'content': json.dumps(payload, ensure_ascii=False),
                    'metadata': {'narrativeAuthority': 'original-v2'},
                })
                continue
            projected = timeline_entry_prompt_projection(readable)
            if projected is readable or projected == entry:
                continue
            # 上游 `{ ...projected, participantId: '' }` 保留的是**原始** metadata，
            # 不是我们为读取器补过拼写的那份浅拷贝。
            emitted = {**projected, 'participantId': ''}
            emitted['metadata'] = pick(entry, 'metadata')
            projected_entries.append(emitted)
        return projected_entries

    async def save_schedule_preplan(self, record: Any) -> None:
        """上游 `saveSchedulePreplan(record)`（`src/service.ts:6034`）。

        `storyId` 是表主键。上游 Minato 会拒绝带主键字段的 update（哪怕值没变），
        所以主键只留在查询条件里，补丁本身不带主键——这样一次成功/空的审查之后
        检查点才能真正前进。本移植版的 `Database.update` 同样会剥掉主键列，
        因此这里额外把本移植版 snake_case 的记录键翻译成上游 camelCase 列名。
        """
        story_id = pick(record, 'storyId', 'story_id')
        rows = await self.db_get('interlude_schedule_preplan', {'storyId': story_id})  # type: ignore[attr-defined]
        if rows:
            update: dict[str, Any] = {}
            for key, value in record.items():
                column = _SCHEDULE_PREPLAN_COLUMNS.get(key)
                if column is None and key in _SCHEDULE_PREPLAN_COLUMNS.values():
                    column = key
                if column is None or column == 'storyId':
                    continue
                update[column] = value
            await self.db_set('interlude_schedule_preplan', {'storyId': story_id}, update)  # type: ignore[attr-defined]
            return
        payload: dict[str, Any] = {}
        for key, value in record.items():
            column = _SCHEDULE_PREPLAN_COLUMNS.get(key, key)
            payload[column] = value
        await self.db_create('interlude_schedule_preplan', payload)  # type: ignore[attr-defined]

    async def prepare_schedule_preplan_review(self, story: Any, now: datetime) -> Optional[dict[str, Any]]:
        """上游 `prepareSchedulePreplanReview(story, now)`（`src/service.ts:6047`）。

        返回本移植版内部的复核上下文（键名 snake_case，与 `types.py` 的
        `SchedulePreplanReviewRequest` 一致）：`current` / `evidence_entries` /
        `local_date` / `needs_model` / `request`。
        """
        config = self.schedule_preplan_config  # type: ignore[attr-defined]
        if _cfg(config, 'enabled', 'enabled', True) is not True:
            return None
        story_id = pick(story, 'id')
        backoff_until = self.schedule_preplan_backoff.get(story_id)  # type: ignore[attr-defined]
        if backoff_until and dt_ms(now) < _number(backoff_until, 0):
            return None
        current = await self.get_schedule_preplan(story_id)
        timezone = _setting_timezone(story)
        if not schedule_preplan_review_due(current, now, timezone, config):
            return None
        local_date = calendar_day_key(now, timezone)
        evidence_entries = await self.schedule_preplan_evidence(
            story_id, _number(pick(current, 'lastEvidenceEntryId', 'last_evidence_entry_id'), 0),
        )
        if not current and not evidence_entries:
            empty = apply_schedule_preplan_proposal(
                None,
                {
                    'outcome': 'replace', 'reason': 'No concrete recurring schedule evidence yet.',
                    'regimes': [], 'exceptions': [],
                },
                [], local_date, timezone, config, now,
                str(_cfg(config, 'variationLevel', 'variation_level', 'stable')),
            )
            if empty:
                empty['story_id'] = story_id
                await self.save_schedule_preplan(empty)
                self.report_operation(  # type: ignore[attr-defined]
                    'diagnostic', 'debug', story, 'advance',
                    'Schedule Preplan 已建立空记录：等待可验证的生活日程证据',
                )
                return {
                    'current': empty, 'evidence_entries': evidence_entries,
                    'local_date': local_date, 'needs_model': False, 'request': None,
                }
        needs_model = schedule_preplan_needs_model(current, evidence_entries, local_date, timezone, config)
        if not needs_model and current:
            await self.save_schedule_preplan(
                refresh_schedule_preplan(current, local_date, timezone, config, now),
            )
            self.report_operation(  # type: ignore[attr-defined]
                'diagnostic', 'debug', story, 'advance',
                'Schedule Preplan 今日检查完成：没有新证据，日程保持不变',
            )
        request = {
            'local_date': local_date,
            'horizon_days': int(_number(_cfg(config, 'horizonDays', 'horizon_days', 14), 14)),
            'variation_level': str(_cfg(config, 'variationLevel', 'variation_level', 'stable')),
            'current': current if current else None,
            'evidence_entries': evidence_entries,
        } if needs_model else None
        return {
            'current': current, 'evidence_entries': evidence_entries,
            'local_date': local_date, 'needs_model': needs_model, 'request': request,
        }
