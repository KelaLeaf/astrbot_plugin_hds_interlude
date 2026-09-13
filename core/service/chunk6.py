"""Chunk6 mixin：`upstream/src/service.ts:4814-5441` 的全部成员。

逐条对应上游顺序（**起始行**落在 `[4814, 5441)` 内；边界成员
`scheduleNextSplitWake`（`:4805`）归 Chunk5、`isAutomaticAdvancePaused`（`:5442`）
归 Chunk7）：

| 上游行 | 上游成员 | 本文件 |
| --- | --- | --- |
| 4814 | `deliverDueSplitSegments` | `deliver_due_split_segments` |
| 4893 | `pendingFollowUpCommitments` | `pending_follow_up_commitments` |
| 4899 | `appendFollowUpCommitment` | `append_follow_up_commitment` |
| 4940 | `applyFollowUpResolutions` | `apply_follow_up_resolutions` |
| 4978 | `deferUnresolvedDueFollowUps` | `defer_unresolved_due_follow_ups` |
| 5002 | `appendProactiveCheck` | `append_proactive_check` |
| 5046 | `cancelPendingOutgoingMessages` | `cancel_pending_outgoing_messages` |
| 5095 | `sendScheduledMessages` | `send_scheduled_messages` |
| 5107 | `sendOutgoingMessages` | `send_outgoing_messages` |
| 5193 | `confirmOutgoingDeliveries` | `confirm_outgoing_deliveries` |
| 5233 | `recordOutgoingDeliveryFailure` | `record_outgoing_delivery_failure` |
| 5253 | `updateScriptDeliveryOutcome` | `update_script_delivery_outcome` |
| 5300 | `recordPlatformDeliveryOutcome` | `record_platform_delivery_outcome` |
| 5313 | `resolveLiteralQuoteMessageId` | `resolve_literal_quote_message_id` |
| 5325 | `recordAutomaticDelivery` | `record_automatic_delivery` |
| 5372 | `splitOutgoingMessage` | `split_outgoing_message` |
| 5379 | `typingDelayMilliseconds` | `typing_delay_milliseconds` |
| 5389 | `findBotForParticipant` | `find_bot_for_participant` |
| 5395 | `get autoAdvanceConfig` | `auto_advance_config`（property） |
| 5411 | `get urgeConfig` | `urge_config`（property） |
| 5413 | `get effectiveUrgeRuntime` | `effective_urge_runtime`（property） |
| 5419 | `scheduleUrgeAdvance` | `schedule_urge_advance` |

本块主题：**拆分投递、承诺回访、取消已排期消息、真实平台出站发送与投递账本回执**。

## 受控偏离（均在方法注释里就地说明）

1. **数据库范围查询**：上游 `dbGet` 支持 `$lte` / `$in`；`ServiceBase.db_get` 刻意
   **拒绝算子**。本文件一律"取回后在 Python 侧过滤 / 按主键逐行写"，语义与上游一致：
   * `deliver_due_split_segments` 按 `notBefore` 升序取前 20 条再筛到期项（与
     `notBefore: {$lte: now}` + `limit: 20` 等价）；
   * `apply_follow_up_resolutions` 取回待处理承诺后按 id 集合过滤（等价 `id: {$in}`）；
   * `cancel_pending_outgoing_messages` 的批量 `$in` 更新拆成逐 id 更新。
2. **平台出站**：上游 `session.send(...)` / `bot.sendMessage(channelId, ...)` /
   `desktopDeliveryHandler({...})` 三条路径，本移植版分别落到
   `Transport.send_session` / `Transport.send_private` / `self.desktop_delivery_handler`
   （`docs/PORT_PLAN_SERVICE.md` §7）。`findBotForParticipant` 原样保留为**可用性判定**，
   因为 AstrBot 侧没有 Koishi 的 `ctx.bots` 出站对象；只有"既无匹配 bot、也没有
   Transport"时才走上游 `bot-not-found` 分支。
3. **引用消息**：上游把可见正文换成 `h('quote', {id}) + '\\u200b'`。本移植版把引用目标
   交给 `send_private` / `send_group` 的 `reply_to`；只有实时会话路径（`send_session`
   协议没有 `reply_to`）在**带引用**时退回按参与者投递，避免只剩一个零宽占位
   （§8：能力无法复现时必须写出可用降级分支，而不是静默丢消息）。
4. **键名法**（`docs/PORT_PLAN.md` §2）：
   * 数据库行列名逐字 camelCase（`storyId` / `notBefore` / `participantId`）；
   * **intent payload** 是跨模块 wire format：`helpers.automatic_delivery_from_payload`
     只认 `payload.automaticDelivery`，模型提示词投影读 `payload.expiresAt` /
     `sourceEntryIds`，故一律**保持上游键名**（写 camelCase）；
   * **script entry metadata**：投递账本由已落地的 `script/delivery_ledger.py` +
     `turn_persistence.py` 产出 `delivery_actions` / `script_events` / `commit_id`
     等 snake_case 键，本文件写 snake_case、并顺手清掉同名 camelCase 旧键；
     其余上游逐字键（`splitSegment` / `quoteMessageId` / `quoteTransport` /
     `participantId` / `reason` / `status`）保持上游拼写，与 Chunk4 的同一段逻辑一致；
   * 读取侧（模型产物、intent payload、旧数据、`story.state`）**一律双读**
     （`pick()`，优先 camelCase）。
5. **`autoAdvanceConfig`**：`base.py` 为 Chunk0 的 `background_tasks` 留了一个
   `_config_cache` 占位 property（`config.py` 并没有 `resolve_auto_advance_config`，
   占位会返回**未补默认值**的 runtime 段）。上游这个 getter 的**起始行是 5395**，
   属于本块，故本文件按上游逐条实现并覆盖占位（MRO：Chunk6 先于 ServiceBase）。
   输出键用 snake_case，因为 `helpers.automatic_interval_minutes` 用
   `config_get(config, 'rest_windows', 'restWindows')`（**snake 优先**）读它。
6. **`urge` 状态串**：上游 `JSON.stringify(config)` 写入 `state.extensions.urge.mode`
   并与 Chunk4 的 `advanceUnlocked` 比对。为与已落地的 Chunk4 保持一致
   （`json.dumps(urge_config, ensure_ascii=False)`），这里用同一形态，否则每次 sweep
   都会被判为"档位变化"而重置 Urge。
7. **`new Date()` / `Math.random()` 的落点**：上游测试会拿对象字面量
   （`Object.create(InterludeService.prototype)`）当 `this`，JS 里缺失的属性是
   `undefined`。因此当前时刻统一走 `_now_of(self)`（真实服务用可注入的 `self.now()`，
   桩对象回落 `utc_now()`），打字延迟与 Urge 采样走模块级 `random.random()`
   （`docs/PORT_PLAN.md` §2 的映射），而不是 `self.rng()`——后者需要 `self.ctx`，
   字面量桩没有。

## 与 helpers.py 的关系

上游模块级函数 `targetableMessageId`（`:7300`）按契约归 `helpers.py`，但该文件当前
没有它，故本文件给出逐字等价实现并沿用 `base.py` 的 `_prefer_helper` 模式：
`helpers.py` 一旦补上同名函数，自动改用它的版本。

本模块不 import astrbot（`core/` 铁律），也不 import 任何兄弟 chunk。
"""

from __future__ import annotations

import asyncio
import json
import math
import random
from typing import Any, Callable, Optional

from ..agency import active_agency_window, proactive_candidate_fingerprint
from ..delivery import delivery_entry_metadata, restore_message_event, script_event_payload
from ..script.delivery_ledger import update_script_delivery_actions
from ..story_state import decode_story_state, encode_story_state
from ..time import dt_ms, format_log_time, iso, parse_dt, utc_now
from ..urge import (
    acknowledge_urge,
    normalize_urge_state,
    plan_urge,
    resolve_urge_config,
    urge_user_event,
)
from .base import ServiceBase, is_one_bot_platform, pick
from .helpers import (
    active_rest_window,
    automatic_delivery_from_payload,
    automatic_interval_minutes,
    clip,
    follow_up_expires_at,
    is_literal_quote_only,
    is_record,
    literal_quote_text,
    merge_delivery_summary,
    normalize_follow_up_commitment,
    normalize_follow_up_minutes,
    normalize_follow_up_resolutions,
    normalize_follow_up_summary,
    to_date,
)

try:  # pragma: no cover - helpers.py 由并行的移植任务产出/补齐
    from . import helpers as _helpers_module
except ImportError:  # pragma: no cover
    _helpers_module = None  # type: ignore[assignment]

__all__ = ['ServiceChunk6']

#: 上游 `Time.second` / `Time.minute`（Koishi 毫秒常量）。
_SECOND_MS = 1_000
_MINUTE_MS = 60_000

#: 上游 `config.runtime.maxMessageCharacters` 的 schema 默认值（`upstream/src/index.ts`）。
_DEFAULT_MAX_MESSAGE_CHARACTERS = 3_000

#: 上游 `config.runtime` 的 schema 默认值（`:5398-5407` 的 `?? 默认值`）。
_DEFAULT_AUTO_ADVANCE_ENABLED = True
_DEFAULT_AUTO_ADVANCE_INTERVAL_MINUTES = 40
_DEFAULT_AUTO_ADVANCE_JITTER_MINUTES = 5
_DEFAULT_FOLLOW_UP_JITTER_MINUTES = 1
_DEFAULT_TYPING_BASE_DELAY_SECONDS = 1
_DEFAULT_TYPING_CHARACTERS_PER_SECOND = 8
_DEFAULT_TYPING_MAX_DELAY_SECONDS = 12
_DEFAULT_TYPING_JITTER_RATIO = 0.3

#: 上游 `autoAdvanceConfig.restWindows` 的默认值（`:5404-5407`）。
#: 内部结构用 snake_case；`helpers.active_rest_window` 读 `enabled/start/end`，
#: `helpers.automatic_interval_minutes` 双读 `min_interval_minutes|minIntervalMinutes`。
_DEFAULT_REST_WINDOW: dict[str, Any] = {
    'enabled': True,
    'label': 'night sleep',
    'start': '23:00',
    'end': '07:00',
    'min_interval_minutes': 120,
    'max_interval_minutes': 240,
}

#: 上游引用消息的可见正文（`'\u200b'` 零宽占位）。
_QUOTE_PLACEHOLDER = '\u200b'

#: 上游 `applyFollowUpResolutions` 的重排期上界（12 小时）。
_MAX_RESCHEDULE_MS = 12 * 60 * 60 * 1000

#: 上游 `deferUnresolvedDueFollowUps` 的重查延迟（20 分钟）。
_DEFER_RETRY_MS = 20 * _MINUTE_MS

#: 上游 `deliverDueSplitSegments` 未确认投递时的重试延迟（30 秒）。
_UNCONFIRMED_RETRY_MS = 30 * _SECOND_MS


# =========================================================================== #
# 模块级小工具（与 chunk4 / chunk5 的同名私有实现语义一致）
# =========================================================================== #

def _str(value: Any) -> str:
    """上游 `String(value ?? '')`。"""
    return '' if value is None else str(value)


def _now_of(service: Any) -> Any:
    """上游 `new Date()`：服务实例优先用注入时钟（`self.now()`），字面量桩回落 `utc_now()`。

    上游测试会拿对象字面量当 `this`（`upstream/test/delivery-ledger.test.ts`、
    `m10-cooperation.test.ts`）；JS 里缺失的属性是 `undefined`，那些路径拿到的是
    **全局时钟**。本助手保留同样的语义：真实服务仍走可注入的 `self.now()`，
    桩对象不再因缺字段而崩。
    """
    getter = getattr(service, 'now', None)
    if callable(getter):
        try:
            parsed = parse_dt(getter())
        except Exception:  # pragma: no cover - 注入时钟异常时按上游全局时钟回落
            parsed = None
        if parsed is not None:
            return parsed
    return utc_now()


def _prefer_helper(name: str, fallback: Any) -> Any:
    """优先用 `helpers.py` 的移植版（与 `base.py` 的 `_prefer_helper` 同一模式）。"""
    if _helpers_module is not None:
        candidate = getattr(_helpers_module, name, None)
        if callable(candidate):
            return candidate
    return fallback


def _snake(name: str) -> str:
    """`camelCase` → `snake_case`（`docs/PORT_PLAN.md` §2 命名映射）。"""
    import re
    return re.sub(r'(?<!^)(?=[A-Z])', '_', name).lower()


def _section(config: Any, name: str) -> dict[str, Any]:
    """读一个配置段：dict 双读，对象走属性（与 `base.py` 的 `_config_section` 同义）。"""
    if config is None:
        return {}
    if isinstance(config, dict):
        value = pick(config, name, _snake(name))
        return value if isinstance(value, dict) else {}
    value = getattr(config, _snake(name), None)
    if value is None:
        value = getattr(config, name, None)
    if isinstance(value, dict):
        return value
    if value is not None and hasattr(value, '__dict__'):
        return {key: item for key, item in vars(value).items() if not key.startswith('_')}
    return {}


def _cfg(section: Any, camel: str, default: Any = None) -> Any:
    """读配置项：camelCase 优先、snake_case 兜底，缺失（`None`）时回落 schema 默认值。"""
    if not isinstance(section, dict):
        return default
    value = pick(section, camel, _snake(camel))
    return default if value is None else value


def _record(value: Any) -> dict[str, Any]:
    """`isRecord(value) ? value : {}`。"""
    return value if isinstance(value, dict) else {}


def _num(value: Any, fallback: float) -> float:
    """`Number(value)`，非有限值回落 `fallback`（上游靠 `?? 默认值` 兜底）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return fallback
    number = float(value)
    return number if math.isfinite(number) else fallback


def _js_round(value: float) -> int:
    """JS `Math.round`：半数朝 +∞（Python `round()` 是银行家舍入，不可用）。"""
    return int(math.floor(value + 0.5))


def _is_safe_integer(value: Any) -> bool:
    """上游 `Number.isSafeInteger()`。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if isinstance(value, float) and not value.is_integer():
        return False
    return abs(value) <= 9007199254740991


def _field(value: Any, camel: str, snake: Optional[str] = None) -> Any:
    """读一个字段：dict 双读；对象走属性（`ctx.bots` 里的适配器对象）。"""
    if isinstance(value, dict):
        return pick(value, camel, snake)
    for name in ((camel, snake) if snake else (camel,)):
        if hasattr(value, name):
            return getattr(value, name)
    return None


def _ledger_actions(metadata: Any) -> Any:
    """读投递账本：优先本移植版生产者写的 snake_case，其次旧数据的 camelCase。"""
    if not isinstance(metadata, dict):
        return None
    if 'delivery_actions' in metadata:
        return metadata['delivery_actions']
    return metadata.get('deliveryActions')


def _script_events(metadata: Any) -> list[Any]:
    """读剧本事件数组：优先 `script_events`，其次旧数据的 `scriptEvents`。"""
    if not isinstance(metadata, dict):
        return []
    events = metadata.get('script_events')
    if events is None:
        events = metadata.get('scriptEvents')
    return events if isinstance(events, list) else []


def _event_metadata(event: Any) -> dict[str, Any]:
    """读一个剧本事件的 metadata（数组元素，可能是旧数据）。"""
    if not isinstance(event, dict):
        return {}
    metadata = event.get('metadata')
    if metadata is None:
        metadata = event.get('meta')
    return metadata if isinstance(metadata, dict) else {}


def _transport_failure(result: Any) -> Optional[str]:
    """`SendResult` 里的失败原因；非 dict 返回值按上游"resolve 即成功"处理。"""
    if not isinstance(result, dict):
        return None
    if result.get('ok') is False:
        return str(result.get('error') or 'transport-error')
    return None


def _cancel_wake_timer(wake: Any) -> None:
    """取消一个到期唤醒句柄（`TimerHandle` 或 Chunk5 的 `_DueIntentWake` 记录）。"""
    if wake is None:
        return
    cancel = getattr(wake, 'cancel', None)
    if not callable(cancel) and isinstance(wake, dict):
        cancel = wake.get('cancel')
    if callable(cancel):
        try:
            cancel()
        except Exception:  # pragma: no cover - 已触发的计时器取消是 no-op
            return


def _targetable_message_id(value: Any) -> Optional[str]:
    """上游 `targetableMessageId`（`:7300`）逐字移植。

    `String(value ?? '').trim()` 后必须是**十进制整数**且不等于 `'0'`。
    """
    text = '' if value is None else str(value)
    text = text.strip()
    if not text or text == '0':
        return None
    body = text[1:] if text[0] == '-' else text
    if not body or not body.isdigit():
        return None
    return text


#: `helpers.py` 一旦提供同名函数即自动改用它的版本（避免两份漂移的实现）。
targetable_message_id = _prefer_helper('targetable_message_id', _targetable_message_id)


# =========================================================================== #
# ServiceChunk6
# =========================================================================== #

class ServiceChunk6(ServiceBase):
    """对应 `upstream/src/service.ts` 第 4814–5441 行的成员。"""

    # ------------------------------------------------------------------ #
    # deliverDueSplitSegments（上游 :4814）
    # ------------------------------------------------------------------ #

    async def deliver_due_split_segments(self, story_id: str) -> None:
        """上游 `deliverDueSplitSegments(storyId)`（`:4814`）逐条移植。

        **不调用主叙事**地投递已经决定好的 `<sep/>` 分段。投递期间仍持着故事队列：
        此前到达的来信会置上 `interrupted_typing_participants` 并取消整条链；
        此后到达的来信无法撤回一条适配器已经开始发送的消息。
        """
        async def task() -> None:
            # 上游同样先 `getStory`：剧本不存在时立刻抛出，而不是静默跳过排期。
            story = await self.get_story(story_id)
            now = _now_of(self)
            due_rows = await self.db_get(
                'interlude_intent',
                {'storyId': story_id, 'status': 'pending', 'type': 'split-message'},
                {'sort': {'notBefore': 'asc'}, 'limit': 20},
            )
            # 上游 `notBefore: {$lte: now}`：按 notBefore 升序取前 20 条后，到期项必在头部，
            # 因此在 Python 侧过滤与上游的范围查询等价（见模块文档串第 1 条）。
            due = [
                intent for intent in due_rows
                if (parse_dt(pick(intent, 'notBefore', 'not_before')) or now) <= now
            ]
            max_characters = int(_cfg(
                _section(self.config, 'runtime'), 'maxMessageCharacters',
                _DEFAULT_MAX_MESSAGE_CHARACTERS,
            ))
            next_intent = due[0] if due else None
            if next_intent is not None:
                payload = _record(pick(next_intent, 'payload'))
                content = clip(payload.get('content'), max_characters)
                automatic_delivery = automatic_delivery_from_payload(payload)
                participant_id = pick(next_intent, 'participantId', 'participant_id')
                participant = await self.get_participant(participant_id) if participant_id else None
                if participant_id and participant_id in self.interrupted_typing_participants:
                    # 来信事务排在本事务之后：把每个分段意图都留在 pending，让它去取消，
                    # 并把它们的草稿原文原样带进那次替换写作请求。
                    return
                if not content or not participant or pick(participant, 'status') != 'active':
                    await self.db_set(
                        'interlude_intent', {'id': pick(next_intent, 'id')},
                        {'status': 'cancelled', 'updatedAt': now},
                    )
                    reference = restore_message_event(payload, content or '')
                    if reference:
                        await self.update_script_delivery_outcome(
                            story_id, reference, 'cancelled', now, 'delivery-target-unavailable',
                        )
                else:
                    message: dict[str, Any] = {
                        'participant_id': pick(participant, 'id'),
                        'content': content,
                        'automatic_delivery': automatic_delivery,
                        'script_event': restore_message_event(payload, content),
                    }
                    delivered = await self.send_outgoing_messages(
                        story,
                        [message],
                        None,
                        None,
                        lambda target: pick(target, 'id') in self.interrupted_typing_participants,
                        False,
                    )
                    if not delivered:
                        if pick(participant, 'id') in self.interrupted_typing_participants:
                            return
                        if message.get('script_event'):
                            await self.update_script_delivery_outcome(
                                story_id, message['script_event'], 'pending', now,
                                'delivery-unconfirmed-retry-scheduled',
                            )
                        retry_at = parse_dt(dt_ms(now) + _UNCONFIRMED_RETRY_MS)
                        await self.db_set(
                            'interlude_intent', {'id': pick(next_intent, 'id')},
                            {'notBefore': retry_at, 'updatedAt': now},
                        )
                        self.schedule_due_intent_wake(story_id, retry_at)
                        return
                    await self.append_entry(story_id, {
                        'kind': 'character-message',
                        'actor': 'character',
                        'content': content,
                        'occurred_at': iso(now),
                        'metadata': delivery_entry_metadata(message, {'splitSegment': True}),
                    }, now, pick(participant, 'id'))
                    if message.get('script_event'):
                        await self.update_script_delivery_outcome(
                            story_id, message['script_event'], 'delivered', now,
                        )
                    if automatic_delivery:
                        await self.record_automatic_delivery(
                            story_id, pick(participant, 'id'), automatic_delivery, now,
                        )
                    await self.record_character_message(participant, now)
                    await self.db_set(
                        'interlude_intent', {'id': pick(next_intent, 'id')},
                        {'status': 'completed', 'updatedAt': now},
                    )
            # 多段同时逾期时，恢复一个新的打字间隔，而不是立刻把积压全部倒出。
            remaining = due[1:]
            if remaining:
                following = remaining[0]
                following_not_before = parse_dt(pick(following, 'notBefore', 'not_before'))
                if following_not_before is not None and following_not_before <= now:
                    following_content = clip(
                        _record(pick(following, 'payload')).get('content'), max_characters,
                    )
                    if following_content:
                        await self.db_set(
                            'interlude_intent', {'id': pick(following, 'id')},
                            {
                                'notBefore': parse_dt(
                                    dt_ms(now) + self.typing_delay_milliseconds(following_content),
                                ),
                                'updatedAt': now,
                            },
                        )
            await self.schedule_next_split_wake(story_id)

        await self.serial(story_id, task)

    # ------------------------------------------------------------------ #
    # pendingFollowUpCommitments（上游 :4893）
    # ------------------------------------------------------------------ #

    async def pending_follow_up_commitments(
        self, story_id: str, participant_id: str,
    ) -> list[dict[str, Any]]:
        """上游 `pendingFollowUpCommitments(storyId, participantId)`（`:4893`）。

        待兑现的承诺刻意保持极小、且只属于当前关系分支：最多 2 条。
        """
        return await self.db_get('interlude_intent', {
            'storyId': story_id,
            'participantId': participant_id,
            'type': 'follow-up-commitment',
            'status': 'pending',
        }, {'limit': 2, 'sort': {'notBefore': 'asc'}})

    # ------------------------------------------------------------------ #
    # appendFollowUpCommitment（上游 :4899）
    # ------------------------------------------------------------------ #

    async def append_follow_up_commitment(
        self,
        story: Any,
        participant_id: str,
        draft: Any,
        fallback_source_entry_id: Any,
        now: Any,
        origin_delivery_event_id: Optional[str] = None,
    ) -> None:
        """上游 `appendFollowUpCommitment(...)`（`:4899`）逐条移植。

        只有**完整投递确认之后**才会走到这里（见 `updateScriptDeliveryOutcome`），
        因此登记的是"主角真的说出口的承诺"。同一 `draft.summary` 已有待处理项、
        或待处理项已达上限（2 条）时不重复创建。
        """
        story_id = pick(story, 'id')
        if origin_delivery_event_id:
            previous = await self.db_get('interlude_intent', {
                'storyId': story_id,
                'participantId': participant_id,
                'type': 'follow-up-commitment',
                'summary': pick(draft, 'summary'),
            })
            for intent in previous:
                payload = _record(pick(intent, 'payload'))
                if pick(payload, 'originDeliveryEventId', 'origin_delivery_event_id') == origin_delivery_event_id:
                    return
        pending = await self.db_get('interlude_intent', {
            'storyId': story_id,
            'participantId': participant_id,
            'type': 'follow-up-commitment',
            'status': 'pending',
        }, {'limit': 3, 'sort': {'notBefore': 'asc'}})
        key = normalize_follow_up_summary(pick(draft, 'summary'))
        duplicate = next(
            (intent for intent in pending
             if normalize_follow_up_summary(pick(intent, 'summary')) == key),
            None,
        )
        if duplicate is not None or len(pending) >= 2:
            self.report_operation(
                'diagnostic', 'debug', story, 'user-message',
                '承诺回访未重复创建 参与者=%s 原因=%s', participant_id,
                '同一事项待处理' if duplicate is not None else '待处理上限',
            )
            return
        raw_ids = pick(draft, 'sourceEntryIds', 'source_entry_ids')
        source_entry_ids = [
            int(item) for item in (raw_ids if isinstance(raw_ids, list) else [])
            if _is_safe_integer(item) and item > 0
        ]
        if fallback_source_entry_id:
            source_entry_ids.append(fallback_source_entry_id)
        source_entry_ids = source_entry_ids[-4:]
        expires_at = follow_up_expires_at(pick(draft, 'expiresAt', 'expires_at'), now)
        not_before = pick(draft, 'notBefore', 'not_before')
        await self.append_intent(story_id, {
            'type': 'follow-up-commitment',
            'summary': pick(draft, 'summary'),
            'notBefore': not_before,
            'payload': {
                'kind': pick(draft, 'kind'),
                'sourceEntryIds': source_entry_ids,
                'expiresAt': iso(expires_at),
                'requiresVisibleOutcome': True,
                'userInitiated': True,
                'originDeliveryEventId': origin_delivery_event_id,
            },
        }, now, participant_id)
        self.schedule_due_intent_wake(story_id, parse_dt(not_before))
        self.report_operation(
            'standard', 'info', story, 'user-message',
            '已登记承诺回访 参与者=%s 类型=%s 到期=%s',
            participant_id, pick(draft, 'kind'),
            format_log_time(parse_dt(not_before), _str(pick(pick(story, 'setting'), 'timezone'))),
        )

    # ------------------------------------------------------------------ #
    # applyFollowUpResolutions（上游 :4940）
    # ------------------------------------------------------------------ #

    async def apply_follow_up_resolutions(
        self,
        story_id: str,
        participant_id: str,
        resolutions: Any,
        interaction: Any,
        now: Any,
        delivery_event_id: Optional[str] = None,
    ) -> set[Any]:
        """上游 `applyFollowUpResolutions(...)`（`:4940`）逐条移植。

        只有**可见的立即回复**才能结清承诺：`fulfilled` / `cancelled` 直接结案，
        `rescheduled` 要求未来的、12 小时以内的新时间点。返回本次真正处置的 id 集合。
        """
        reply = _record(pick(_record(interaction), 'reply'))
        content = pick(reply, 'content')
        if (not resolutions or pick(reply, 'mode') != 'immediate'
                or not isinstance(content, str) or not content.strip()):
            return set()
        ids = [pick(item, 'id') for item in resolutions if is_record(item)]
        rows = await self.db_get('interlude_intent', {
            'storyId': story_id,
            'participantId': participant_id,
            'type': 'follow-up-commitment',
            'status': 'pending',
        })
        # 上游 `id: { $in: ids }`：本移植版无算子，取回后在 Python 侧过滤。
        rows = [row for row in rows if pick(row, 'id') in ids]
        resolved: set[Any] = set()
        for resolution in resolutions:
            intent = next(
                (row for row in rows if pick(row, 'id') == pick(resolution, 'id')), None,
            )
            if intent is None:
                continue
            payload = _record(pick(intent, 'payload'))
            if (delivery_event_id
                    and pick(payload, 'resolutionEventId', 'resolution_event_id') == delivery_event_id):
                # 同一次投递回执重复到达：接线已结清，绝不重复计数。
                resolved.add(pick(intent, 'id'))
                continue
            outcome = pick(resolution, 'outcome')
            if outcome == 'rescheduled':
                next_at = to_date(pick(resolution, 'notBefore', 'not_before'))
                if (next_at is None or next_at <= now
                        or dt_ms(next_at) - dt_ms(now) > _MAX_RESCHEDULE_MS):
                    continue
                await self.db_set('interlude_intent', {'id': pick(intent, 'id')}, {
                    'notBefore': next_at,
                    'payload': {
                        **payload,
                        'reschedules': _num(payload.get('reschedules'), 0) + 1,
                        'resolutionEventId': delivery_event_id,
                    },
                    'updatedAt': now,
                })
                self.schedule_due_intent_wake(story_id, next_at)
            else:
                await self.db_set('interlude_intent', {'id': pick(intent, 'id')}, {
                    'status': 'cancelled' if outcome == 'cancelled' else 'completed',
                    'updatedAt': now,
                })
            resolved.add(pick(intent, 'id'))
        return resolved

    # ------------------------------------------------------------------ #
    # deferUnresolvedDueFollowUps（上游 :4978）
    # ------------------------------------------------------------------ #

    async def defer_unresolved_due_follow_ups(
        self,
        story_id: str,
        participant_id: str,
        context_intents: Any,
        resolved_ids: Any,
        _interaction: Any,
        now: Any,
    ) -> None:
        """上游 `deferUnresolvedDueFollowUps(...)`（`:4978`）逐条移植。

        到期的承诺若没有被明确结算、也没有拿到完整投递确认，**绝不静默完成**：
        推迟 20 分钟并累计 `deferredChecks`，等下一次复核。
        """
        due = [
            intent for intent in (context_intents or [])
            if pick(intent, 'type') == 'follow-up-commitment'
            and pick(intent, 'participantId', 'participant_id') == participant_id
        ]
        if not due:
            return
        resolved = resolved_ids if isinstance(resolved_ids, (set, frozenset, list, tuple)) else set()
        for intent in due:
            if pick(intent, 'id') in resolved:
                continue
            retry_at = parse_dt(dt_ms(now) + _DEFER_RETRY_MS)
            payload = _record(pick(intent, 'payload'))
            await self.db_set('interlude_intent', {'id': pick(intent, 'id')}, {
                'notBefore': retry_at,
                'payload': {
                    **payload,
                    'deferredChecks': _num(payload.get('deferredChecks'), 0) + 1,
                },
                'updatedAt': now,
            })
            self.schedule_due_intent_wake(story_id, retry_at)
            self.report_operation(
                'diagnostic', 'debug', await self.get_story(story_id), 'intent-due',
                '承诺回访等待明确结算及完整投递确认，已保留重查 参与者=%s', participant_id,
            )

    # ------------------------------------------------------------------ #
    # appendProactiveCheck（上游 :5002）
    # ------------------------------------------------------------------ #

    async def append_proactive_check(
        self,
        story: Any,
        candidate: Any,
        not_before: Any,
        reason: str,
        now: Any,
    ) -> None:
        """上游 `appendProactiveCheck(story, candidate, notBefore, reason, now)`（`:5002`）。

        把 Agency 的主动联系候选落成一次未来的复核：必须落在候选有效期内，
        并按 `proactiveCandidateFingerprint`（目标 + 来源 + 证据 id）去重。
        """
        expires_at = to_date(pick(candidate, 'expiresAt', 'expires_at'))
        if expires_at is None or expires_at <= now or not_before >= expires_at:
            return
        participant_id = pick(candidate, 'participantId', 'participant_id')
        fingerprint = proactive_candidate_fingerprint(candidate)
        pending = await self.db_get('interlude_intent', {
            'storyId': pick(story, 'id'),
            'participantId': participant_id,
            'status': 'pending',
            'type': 'proactive-check',
        })
        for intent in pending:
            if pick(_record(pick(intent, 'payload')), 'fingerprint') == fingerprint:
                self.report_operation(
                    'diagnostic', 'debug', story, 'advance',
                    'Agency 主动联系候选去重 参与者=%s 指纹=%s', participant_id, fingerprint,
                )
                return
        await self.append_intent(pick(story, 'id'), {
            'type': 'proactive-check',
            'summary': 'Re-evaluate a life-grounded contact motive: %s' % pick(candidate, 'motive'),
            'notBefore': iso(not_before),
            'participantId': participant_id,
            'payload': {
                'origin': pick(candidate, 'origin'),
                'motive': pick(candidate, 'motive'),
                'disclosure': pick(candidate, 'disclosure'),
                'sourceEntryIds': list(pick(candidate, 'sourceEntryIds', 'source_entry_ids') or []),
                'willingness': pick(candidate, 'willingness'),
                'expiresAt': pick(candidate, 'expiresAt', 'expires_at'),
                'fingerprint': fingerprint,
                'agencyReason': reason,
                'userInitiated': False,
            },
        }, now, participant_id)
        self.schedule_due_intent_wake(pick(story, 'id'), not_before)
        self.report_operation(
            'standard', 'info', story, 'advance',
            'Agency 已安排主动联系重查 参与者=%s 时间=%s 原因=%s',
            participant_id,
            format_log_time(not_before, _str(pick(pick(story, 'setting'), 'timezone'))),
            reason,
        )

    # ------------------------------------------------------------------ #
    # cancelPendingOutgoingMessages（上游 :5046）
    # ------------------------------------------------------------------ #

    async def cancel_pending_outgoing_messages(
        self,
        story_id: str,
        participant_id: str,
        now: Any,
        cancel_planned: bool = True,
    ) -> list[dict[str, Any]]:
        """上游 `cancelPendingOutgoingMessages(...)`（`:5046`）逐条移植。

        新来信到达时取消该关系分支尚未发出的消息：拆分分段一定取消；`cancelPlanned`
        决定是否连"已排期的延迟回复/跨对话消息"一起取消。取消会写一条
        `intent-cancelled` 剧本条目，把被打断的草稿原文交给下一次写作。
        """
        completed = False
        try:
            intents = await self.db_get('interlude_intent', {
                'storyId': story_id, 'participantId': participant_id, 'status': 'pending',
            })
            matching = [
                intent for intent in intents
                if pick(intent, 'participantId', 'participant_id') == participant_id
                and (
                    pick(intent, 'type') == 'split-message'
                    or (cancel_planned and pick(intent, 'type') in (
                        'delayed-reply', 'cross-conversation-message',
                    ))
                )
            ]
            if not matching:
                completed = True
                return matching
            max_characters = int(_cfg(
                _section(self.config, 'runtime'), 'maxMessageCharacters',
                _DEFAULT_MAX_MESSAGE_CHARACTERS,
            ))
            # 上游是 `{ id: { $in: ids } }` 的一次批量更新；本移植版逐 id 更新
            # （`db_get` 刻意不支持算子，见模块文档串第 1 条）。
            for intent in matching:
                await self.db_set('interlude_intent', {'id': pick(intent, 'id')}, {
                    'status': 'cancelled', 'updatedAt': now,
                })
                payload = _record(pick(intent, 'payload'))
                content = clip(payload.get('content'), max_characters)
                reference = restore_message_event(payload, content)
                if reference:
                    await self.update_script_delivery_outcome(
                        story_id, reference, 'cancelled', now, 'superseded-by-new-message',
                    )
            _cancel_wake_timer(self.due_intent_wake_timers.get(story_id))
            self.due_intent_wake_timers.pop(story_id, None)
            await self.schedule_next_split_wake(story_id)
            interrupted_drafts = [
                clip(_record(pick(intent, 'payload')).get('content'), max_characters)
                for intent in matching if pick(intent, 'type') == 'split-message'
            ]
            interrupted_drafts = [draft for draft in interrupted_drafts if draft]
            if interrupted_drafts:
                content = ('The protagonist wanted to send %s, but had not finished typing '
                           "before the user's new message arrived."
                           % ' and '.join(json.dumps(draft, ensure_ascii=False)
                                          for draft in interrupted_drafts))
            else:
                content = ('A newer user message superseded a planned outgoing message '
                           'before it was sent.')
            await self.append_entry(story_id, {
                'kind': 'intent-cancelled',
                'actor': 'system',
                'content': content,
                'occurred_at': iso(now),
                'metadata': {
                    'intentIds': [pick(intent, 'id') for intent in matching],
                    'interruptedDrafts': interrupted_drafts,
                },
            }, now, participant_id)
            completed = True
            return matching
        finally:
            if completed:
                self.interrupted_typing_participants.discard(participant_id)

    # ------------------------------------------------------------------ #
    # sendScheduledMessages（上游 :5095）
    # ------------------------------------------------------------------ #

    async def send_scheduled_messages(
        self, story: Any, messages: list[Any],
    ) -> list[dict[str, Any]]:
        """上游 `sendScheduledMessages(story, messages)`（`:5095`）逐条移植。"""
        delivered = await self.send_outgoing_messages(story, messages)
        await self.confirm_outgoing_deliveries(story, delivered)
        return delivered

    # ------------------------------------------------------------------ #
    # sendOutgoingMessages（上游 :5107）
    # ------------------------------------------------------------------ #

    async def send_outgoing_messages(
        self,
        story: Any,
        messages: list[Any],
        current: Any = None,
        session: Any = None,
        should_cancel: Optional[Callable[[Any], bool]] = None,
        record_failures: bool = True,
    ) -> list[dict[str, Any]]:
        """上游 `sendOutgoingMessages(...)`（`:5107`）逐条移植。

        立即回复可以复用入站 `session`；跨账号与定时消息则通过**目标参与者自己的**
        通道投递。这条边界保证共享剧本不会把每条回复都发回恰好触发本回合的那个账号。
        """
        delivered: list[dict[str, Any]] = []
        if not messages:
            return delivered
        ids: list[str] = []
        for message in messages:
            participant_id = pick(message, 'participantId', 'participant_id')
            if participant_id and participant_id not in ids:
                ids.append(participant_id)
        by_id: dict[str, Any] = {}
        if current is not None and pick(current, 'id') in ids:
            by_id[pick(current, 'id')] = current
        missing = [participant_id for participant_id in ids if participant_id not in by_id]
        participants = await asyncio.gather(*[self.get_participant(item) for item in missing])
        for participant in participants:
            if participant:
                by_id[pick(participant, 'id')] = participant
        for message in messages:
            message_participant_id = pick(message, 'participantId', 'participant_id')
            target = by_id.get(message_participant_id)
            if target is None:
                self.report(
                    'warn', story, 'intent-due',
                    '无法投递消息：参与者不存在 %s', message_participant_id,
                )
                if record_failures:
                    await self.record_outgoing_delivery_failure(
                        story, message_participant_id, message, 'participant-not-found',
                    )
                continue
            target_id = pick(target, 'id')
            if not self.can_handle_participant(target):
                self.report(
                    'warn', story, 'intent-due',
                    '消息被当前账号白名单拦截 参与者=%s', target_id,
                )
                if record_failures:
                    await self.record_outgoing_delivery_failure(
                        story, target_id, message, 'participant-not-allowed',
                    )
                continue
            if should_cancel is not None and should_cancel(target):
                self.report_operation(
                    'standard', 'info', story, 'user-message',
                    '新消息打断主角输入，停止发送后续分段 参与者=%s', target_id,
                )
                continue
            try:
                self.report_operation(
                    'standard', 'info', story, 'intent-due', '消息投递开始 参与者=%s', target_id,
                )
                content = message.get('content') if isinstance(message.get('content'), str) else ''
                literal_quote_message_id = await self.resolve_literal_quote_message_id(
                    pick(story, 'id'), target_id, content,
                )
                literal_quote_only = is_literal_quote_only(content)
                if literal_quote_only and not literal_quote_message_id:
                    self.report(
                        'warn', story, 'intent-due',
                        '已阻止无法映射的伪引用文本 参与者=%s', target_id,
                    )
                    if record_failures:
                        await self.record_outgoing_delivery_failure(
                            story, target_id, message, 'literal-quote-target-not-found',
                        )
                    continue
                if literal_quote_message_id:
                    message['quote_message_id'] = literal_quote_message_id
                outgoing_content = _QUOTE_PLACEHOLDER if literal_quote_message_id else content
                logging_config = _section(self.config, 'logging')
                if _cfg(logging_config, 'logMessageContent', False):
                    preview_length = _cfg(logging_config, 'previewLength')
                    preview = (
                        content[:int(preview_length)]
                        if isinstance(preview_length, (int, float))
                        and not isinstance(preview_length, bool)
                        else content
                    )
                    self.report('info', story, 'intent-due', '主角消息内容：%s', preview)
                if session is not None and pick(current, 'id') == target_id:
                    if literal_quote_message_id:
                        # 上游把正文换成 `h('quote', {id}) + '\u200b'`；`send_session`
                        # 协议没有 reply_to，照原样发就只剩零宽占位，故这条降级路径
                        # 改走按参与者投递，把引用目标显式交给适配器（见模块文档串第 3 条）。
                        result = await self.transport.send_private(
                            target, _QUOTE_PLACEHOLDER, literal_quote_message_id,
                        )
                    else:
                        result = await self.transport.send_session(session, outgoing_content)
                    error = _transport_failure(result)
                    if error is not None:
                        raise RuntimeError(error)
                    delivered.append(message)
                    continue
                desktop_handler = getattr(self, 'desktop_delivery_handler', None)
                if desktop_handler is not None:
                    # typ-0 worker：没有 adapter bot，后台投递统一走宿主渠道。结果语义与
                    # `bot.sendMessage` 一致——成功 resolve 进 delivered 由账本确认，
                    # 失败 reject 进 catch 由 `recordOutgoingDeliveryFailure` 记录。
                    delivery: dict[str, Any] = {
                        'participantId': target_id,
                        'selfId': pick(target, 'selfId', 'self_id'),
                        'platform': pick(target, 'platform'),
                        'channelId': pick(target, 'channelId', 'channel_id'),
                        'kind': 'private',
                        'content': content,
                    }
                    if message.get('quote_message_id'):
                        delivery['quoteMessageId'] = message['quote_message_id']
                    outcome = await desktop_handler(delivery)
                    if not (isinstance(outcome, dict) and outcome.get('ok')):
                        failure = _record(outcome).get('error') or 'typ-0 宿主投递失败。'
                        raise RuntimeError(str(failure))
                    delivered.append(message)
                    continue
                bot = self.find_bot_for_participant(target)
                if self.transport is None and bot is None:
                    self.report(
                        'warn', story, 'intent-due',
                        '没有可用机器人账号投递消息 参与者=%s', target_id,
                    )
                    if record_failures:
                        await self.record_outgoing_delivery_failure(
                            story, target_id, message, 'bot-not-found',
                        )
                    continue
                if self.transport is None:
                    # 找到了 Koishi 式 bot 对象，但本移植版的出站协议只有 Transport：
                    # 记一次明确失败，绝不静默丢消息（见模块文档串第 2 条）。
                    raise RuntimeError('transport-unavailable')
                result = await self.transport.send_private(
                    target, outgoing_content, literal_quote_message_id,
                )
                error = _transport_failure(result)
                if error is not None:
                    raise RuntimeError(error)
                delivered.append(message)
            except Exception as error:
                self.report(
                    'warn', story, 'intent-due',
                    '消息投递失败 参与者=%s 错误=%s', target_id, error,
                )
                if record_failures:
                    await self.record_outgoing_delivery_failure(
                        story, target_id, message, 'transport-error: %s' % error,
                    )
        return delivered

    # ------------------------------------------------------------------ #
    # confirmOutgoingDeliveries（上游 :5193）
    # ------------------------------------------------------------------ #

    async def confirm_outgoing_deliveries(
        self, story: Any, delivered: list[Any],
    ) -> list[Any]:
        """上游 `confirmOutgoingDeliveries(story, delivered)`（`:5193`）逐条移植。

        只有平台**真正接受**了消息才确认可见投递。失败尝试变成显式的系统证据，
        而不是虚构的角色发言，并且刻意不自动重投——适配器在接受请求之后才失败时
        重投会产生重复消息。后续气泡在这里按模拟打字时长排期。
        """
        confirmed: list[Any] = []
        for message in delivered:
            async def task(message: Any = message) -> Any:
                participant = await self.get_participant(
                    pick(message, 'participantId', 'participant_id'),
                )
                if not participant:
                    return None
                now = _now_of(self)
                quote_message_id = message.get('quote_message_id')
                content = '[主角引用了此前的一条消息]' if quote_message_id else message.get('content')
                metadata = delivery_entry_metadata(message, {
                    'quoteMessageId': quote_message_id, 'quoteTransport': True,
                } if quote_message_id else {})
                persisted_entry = await self.append_entry(pick(story, 'id'), {
                    'kind': 'character-message',
                    'actor': 'character',
                    'content': content,
                    'occurred_at': iso(now),
                    'metadata': metadata,
                }, now, pick(participant, 'id'))
                if message.get('script_event'):
                    await self.update_script_delivery_outcome(
                        pick(story, 'id'), message['script_event'], 'delivered', now,
                    )
                await self.record_character_message(participant, now)
                automatic_delivery = pick(message, 'automaticDelivery', 'automatic_delivery')
                if automatic_delivery:
                    await self.record_automatic_delivery(
                        pick(story, 'id'), pick(participant, 'id'), automatic_delivery, now,
                    )
                delay = 0
                later_segments = pick(message, 'laterSegments', 'later_segments') or []
                for index, segment in enumerate(later_segments):
                    delay += self.typing_delay_milliseconds(segment)
                    send_at = parse_dt(dt_ms(now) + delay)
                    payload: dict[str, Any] = {
                        'content': segment,
                        'visibleMessage': True,
                        'userInitiated': pick(message, 'userInitiated', 'user_initiated') is True,
                        **script_event_payload(message, index + 1),
                    }
                    if automatic_delivery:
                        payload['automaticDelivery'] = automatic_delivery
                    await self.append_intent(pick(story, 'id'), {
                        'type': 'split-message',
                        'summary': 'The character is still typing the next message segment.',
                        'notBefore': iso(send_at),
                        'payload': payload,
                    }, now, pick(participant, 'id'))
                    self.schedule_due_intent_wake(pick(story, 'id'), send_at)
                return persisted_entry

            entry = await self.serial(pick(story, 'id'), task)
            if entry:
                confirmed.append(entry)
        return confirmed

    # ------------------------------------------------------------------ #
    # recordOutgoingDeliveryFailure（上游 :5233）
    # ------------------------------------------------------------------ #

    async def record_outgoing_delivery_failure(
        self, story: Any, participant_id: str, message: Any, reason: str,
    ) -> None:
        """上游 `recordOutgoingDeliveryFailure(...)`（`:5233`）逐条移植。

        未投递的主角消息必须留下显式的系统证据：它**仍未发送**，不能被视为
        用户已收到。metadata 沿用上游逐字键名（`status` / `participantId` /
        `reason`），并原样展开剧本事件身份与自动投递摘要。
        """
        async def task() -> None:
            now = _now_of(self)
            if message.get('script_event'):
                await self.update_script_delivery_outcome(
                    pick(story, 'id'), message['script_event'], 'failed', now, clip(reason, 500),
                )
            automatic_delivery = pick(message, 'automaticDelivery', 'automatic_delivery')
            metadata: dict[str, Any] = {
                'status': 'failed',
                'participantId': participant_id,
                'reason': clip(reason, 500),
                **_record(message.get('script_event')),
            }
            if automatic_delivery:
                metadata['automaticDelivery'] = automatic_delivery
            await self.append_entry(pick(story, 'id'), {
                'kind': 'outgoing-delivery-failed',
                'actor': 'system',
                'content': '未投递的主角消息（仍未发送，不能视为用户已收到）：%s' % clip(
                    message.get('content'),
                    int(_cfg(_section(self.config, 'runtime'), 'maxMessageCharacters',
                             _DEFAULT_MAX_MESSAGE_CHARACTERS)),
                ),
                'occurred_at': iso(now),
                'metadata': metadata,
            }, now, participant_id)

        await self.serial(pick(story, 'id'), task)

    # ------------------------------------------------------------------ #
    # updateScriptDeliveryOutcome（上游 :5253）
    # ------------------------------------------------------------------ #

    async def update_script_delivery_outcome(
        self,
        story_id: str,
        reference: Any,
        status: str,
        at: Any,
        reason: Optional[str] = None,
    ) -> None:
        """上游 `updateScriptDeliveryOutcome(...)`（`:5253`）逐条移植。

        更新权威剧本行旁边的 M6.1 投递账本。**调用方已经持有故事队列**，因此这个
        辅助函数绝不嵌套 `serial`，也不会打乱平台投递顺序。

        * 账本写入由 `script/delivery_ledger.update_script_delivery_actions` 完成：
          已 `delivered` 的片段是终态，晚到的 `failed` / `pending` **不能降级**它；
        * 只有该事件的**全部片段**都已送达（action 状态为 `delivered`）时，才结算
          承诺回访 / 登记新承诺，并用 `follow_up_resolution_event_id` 幂等标记；
        * M6.1 是观察性的：账本写失败只记一条 warn，绝不阻断平台结果或剩余气泡排期。
        """
        script_entry_id = pick(reference, 'scriptEntryId', 'script_entry_id')
        if not _is_safe_integer(script_entry_id):
            return
        event_id = pick(reference, 'eventId', 'event_id')
        try:
            rows = await self.db_get('interlude_script_entry', {'id': int(script_entry_id)})
            entry = rows[0] if rows else None
            if not entry or pick(entry, 'storyId', 'story_id') != story_id:
                return
            metadata = _record(pick(entry, 'metadata'))
            commit_id = pick(reference, 'commitId', 'commit_id')
            if pick(metadata, 'commitId', 'commit_id') != commit_id:
                return
            segment_index = pick(reference, 'segmentIndex', 'segment_index')
            if segment_index is None:
                segment_index = pick(reference, 'bubbleIndex', 'bubble_index')
            if segment_index is None:
                segment_index = 0
            normalized = {
                'commit_id': commit_id,
                'event_id': event_id,
                'script_entry_id': int(script_entry_id),
                'segment_index': segment_index,
            }
            actions = update_script_delivery_actions(
                _ledger_actions(metadata), normalized, status, at, reason,
            )
            next_metadata = dict(metadata)
            if actions is not None:
                next_metadata['delivery_actions'] = actions
                next_metadata.pop('deliveryActions', None)
                await self.db_set(
                    'interlude_script_entry', {'id': pick(entry, 'id')},
                    {'metadata': next_metadata},
                )
            delivered = None
            action_list = next_metadata.get('delivery_actions')
            if isinstance(action_list, list):
                for action in action_list:
                    if (is_record(action)
                            and pick(action, 'commitId', 'commit_id') == commit_id
                            and pick(action, 'eventId', 'event_id') == event_id
                            and pick(action, 'status') == 'delivered'):
                        delivered = action
                        break
            if delivered is None:
                return
            event = next(
                (item for item in _script_events(next_metadata)
                 if is_record(item)
                 and pick(item, 'commitId', 'commit_id') == commit_id
                 and pick(item, 'eventId', 'event_id') == event_id
                 and pick(item, 'kind') == 'outgoing-message'),
                None,
            )
            event_participant_id = pick(event, 'participantId', 'participant_id')
            if event is None or not event_participant_id:
                return
            settled_marker = pick(
                next_metadata, 'followUpResolutionEventId', 'follow_up_resolution_event_id',
            )
            if settled_marker == event_id:
                return
            event_metadata = _event_metadata(event)
            resolutions = normalize_follow_up_resolutions(
                event_metadata.get('follow_up_resolutions',
                                   event_metadata.get('followUpResolutions')),
            )
            commitment = normalize_follow_up_commitment(
                event_metadata.get('follow_up_commitment',
                                   event_metadata.get('followUpCommitment')),
                to_date(pick(entry, 'occurredAt', 'occurred_at')) or at,
            )
            if not resolutions and not commitment:
                return
            await self.apply_follow_up_resolutions(
                story_id,
                event_participant_id,
                resolutions,
                {'seen': False, 'reply': {'mode': 'immediate', 'content': pick(event, 'content')}},
                at,
                event_id,
            )
            if commitment:
                await self.append_follow_up_commitment(
                    await self.get_story(story_id), event_participant_id, commitment,
                    pick(entry, 'id'), at, event_id,
                )
            await self.db_set('interlude_script_entry', {'id': pick(entry, 'id')}, {
                'metadata': {**next_metadata, 'follow_up_resolution_event_id': event_id},
            })
        except Exception as error:
            # M6.1 是观察性的：账本写失败绝不能阻止调用方排期剩余气泡或执行平台动作。
            self.report_standalone(
                'warn',
                '投递账本记录失败 故事=%s 事件=%s；保留平台结果并继续原投递链 错误=%s',
                story_id, event_id, error,
            )

    # ------------------------------------------------------------------ #
    # recordPlatformDeliveryOutcome（上游 :5300）
    # ------------------------------------------------------------------ #

    async def record_platform_delivery_outcome(
        self,
        story_id: str,
        reference: Any,
        status: str,
        reason: Optional[str] = None,
    ) -> None:
        """上游 `recordPlatformDeliveryOutcome(...)`（`:5300`）逐条移植。"""
        try:
            async def task() -> None:
                await self.update_script_delivery_outcome(
                    story_id, reference, status, _now_of(self), reason,
                )

            await self.serial(story_id, task)
        except Exception as error:
            self.report_standalone(
                'warn', '平台行动结果记录失败 故事=%s 事件=%s segment=%s 错误=%s',
                story_id, pick(reference, 'eventId', 'event_id'),
                pick(reference, 'segmentIndex', 'segment_index'), error,
            )

    # ------------------------------------------------------------------ #
    # resolveLiteralQuoteMessageId（上游 :5313）
    # ------------------------------------------------------------------ #

    async def resolve_literal_quote_message_id(
        self, story_id: str, participant_id: str, content: str,
    ) -> Optional[str]:
        """上游 `resolveLiteralQuoteMessageId(...)`（`:5313`）逐条移植。

        模型有时会把「引用：…」当成字面文本写出来。只有在该关系分支最近 120 条
        剧本条目里能找到**内容完全一致**、且带可定位平台消息 id 的那一条时，
        才把它变成真正的平台引用；否则调用方会拦下这条伪引用。
        """
        quoted = literal_quote_text(content)
        if not quoted:
            return None
        entries = await self.db_get('interlude_script_entry', {
            'storyId': story_id, 'participantId': participant_id,
        }, {'limit': 120, 'sort': {'occurredAt': 'desc'}})
        for entry in entries:
            entry_content = pick(entry, 'content')
            if not isinstance(entry_content, str) or entry_content.strip() != quoted:
                continue
            matched = targetable_message_id(
                pick(_record(pick(entry, 'metadata')), 'messageId', 'message_id'),
            )
            if matched:
                return matched
        return None

    # ------------------------------------------------------------------ #
    # recordAutomaticDelivery（上游 :5325）
    # ------------------------------------------------------------------ #

    async def record_automatic_delivery(
        self,
        story_id: str,
        participant_id: str,
        delivery: Any,
        now: Any,
    ) -> None:
        """上游 `recordAutomaticDelivery(...)`（`:5325`）逐条移植。

        只记录**已经完成**的后台投递：它是一条有界的行动账本，而不是第二份对话记录。
        摘要是一次投影，永远不是剩余气泡的前置条件——任何失败只记 warn。
        """
        try:
            source_entry_id = pick(delivery, 'sourceEntryId', 'source_entry_id')
            urge_config = getattr(self, 'urge_config', None)
            if is_record(urge_config) and urge_config.get('enabled') and source_entry_id:
                current = await self.get_story(story_id)
                state = decode_story_state(pick(current, 'state'))
                extensions = _record(state.get('extensions'))
                prior = normalize_urge_state(extensions.get('urge'), dt_ms(now))
                urge = acknowledge_urge(prior, participant_id, source_entry_id, dt_ms(now))
                if urge != prior:
                    await self.db_set('interlude_story', {'id': story_id}, {
                        'state': encode_story_state({
                            **state,
                            'extensions': {**extensions, 'urge': urge},
                        }),
                        'updatedAt': now,
                    })
                    await self.schedule_urge_advance(await self.get_story(story_id), now)
            if source_entry_id:
                rows = await self.db_get(
                    'interlude_script_entry', {'storyId': story_id, 'id': source_entry_id},
                )
                entry = rows[0] if rows else None
                actions = _ledger_actions(_record(pick(entry, 'metadata')))
                if isinstance(actions, list):
                    speech = [
                        action for action in actions
                        if is_record(action)
                        and pick(action, 'participantId', 'participant_id') == participant_id
                        and pick(action, 'eventKind', 'event_kind') == 'outgoing-message'
                    ]
                    # 背景摘要必须等**完整**投递确认：还有未送达的发言就绝不落摘要。
                    if speech and any(pick(action, 'status') != 'delivered' for action in speech):
                        return
            story = await self.get_story(story_id)
            state = decode_story_state(pick(story, 'state'))
            summary = clip(pick(delivery, 'summary'), 240).strip()
            if not summary:
                return
            prior_summaries = state.get('automatic_delivery_summaries') or []
            same = next(
                (item for item in prior_summaries
                 if is_record(item)
                 and pick(item, 'participantId', 'participant_id') == participant_id
                 and pick(item, 'sourceEntryId', 'source_entry_id') == source_entry_id),
                None,
            )
            next_summary: dict[str, Any] = {
                'participant_id': participant_id,
                'summary': (
                    merge_delivery_summary(pick(same, 'summary'), summary) if same else summary
                ),
            }
            if source_entry_id:
                next_summary['source_entry_id'] = source_entry_id
            next_summary['delivered_at'] = iso(now)
            retained = [item for item in prior_summaries if item is not same]
            retained.append(next_summary)
            await self.db_set('interlude_story', {'id': pick(story, 'id')}, {
                'state': encode_story_state({
                    **state, 'automatic_delivery_summaries': retained[-6:],
                }),
                'updatedAt': now,
            })
        except Exception as error:
            # 摘要只是一次投影，绝不是剩余气泡的前置条件。
            self.report_standalone(
                'warn', '自动通信摘要记录失败，保留原文与投递主链 错误=%s', error,
            )

    # ------------------------------------------------------------------ #
    # splitOutgoingMessage（上游 :5372）
    # ------------------------------------------------------------------ #

    def split_outgoing_message(self, content: str) -> list[str]:
        """上游 `splitOutgoingMessage(content)`（`:5372`）逐字移植。"""
        runtime = _section(self.config, 'runtime')
        if _cfg(runtime, 'splitReplyMessages', True) is False:
            return [content]
        separator = _cfg(runtime, 'messageSeparator')
        separator = separator.strip() if isinstance(separator, str) else ''
        if not separator:
            separator = '<sep/>'
        if not separator or separator not in content:
            return [content]
        return [part.strip() for part in content.split(separator) if part.strip()]

    # ------------------------------------------------------------------ #
    # typingDelayMilliseconds（上游 :5379）
    # ------------------------------------------------------------------ #

    def typing_delay_milliseconds(self, next_segment: str) -> int:
        """上游 `typingDelayMilliseconds(nextSegment)`（`:5379`）逐字移植。

        `基础延迟 + ceil(字数 / 每秒字数)`，按抖动比例上下浮动，并夹在
        `[250ms, 最大延迟]` 之间。随机源走注入的 `ctx.random`（`self.rng()`）。
        """
        runtime = _section(self.config, 'runtime')
        base_seconds = max(0.0, _num(
            _cfg(runtime, 'typingBaseDelaySeconds', _DEFAULT_TYPING_BASE_DELAY_SECONDS),
            _DEFAULT_TYPING_BASE_DELAY_SECONDS,
        ))
        characters_per_second = max(1.0, _num(
            _cfg(runtime, 'typingCharactersPerSecond', _DEFAULT_TYPING_CHARACTERS_PER_SECOND),
            _DEFAULT_TYPING_CHARACTERS_PER_SECOND,
        ))
        maximum_seconds = max(base_seconds, _num(
            _cfg(runtime, 'typingMaxDelaySeconds', _DEFAULT_TYPING_MAX_DELAY_SECONDS),
            _DEFAULT_TYPING_MAX_DELAY_SECONDS,
        ))
        text = next_segment if isinstance(next_segment, str) else ''
        nominal = min(
            maximum_seconds, base_seconds + math.ceil(len(text) / characters_per_second),
        )
        jitter = _num(
            _cfg(runtime, 'typingJitterRatio', _DEFAULT_TYPING_JITTER_RATIO), 0.0,
        )
        jitter = max(0.0, min(0.5, jitter))
        # 上游 `Math.random()` → 模块级 `random.random()`（`docs/PORT_PLAN.md` §2）。
        factor = 1 + (random.random() * 2 - 1) * jitter if jitter else 1
        return int(max(250, min(maximum_seconds * _SECOND_MS,
                                _js_round(nominal * factor * _SECOND_MS))))

    # ------------------------------------------------------------------ #
    # findBotForParticipant（上游 :5389）
    # ------------------------------------------------------------------ #

    def find_bot_for_participant(self, participant: Any) -> Any:
        """上游 `findBotForParticipant(participant)`（`:5389`）逐字移植。

        `String(bot.selfId) === String(participant.selfId)` 且平台相同
        （OneBot 家族之间视为同一平台）。AstrBot 侧没有 Koishi 的 bot 对象，
        因此这里保留为**可用性判定**，真正的出站统一走 `self.transport`。
        """
        self_id = str(_field(participant, 'selfId', 'self_id') or '')
        platform = str(_field(participant, 'platform') or '')
        for bot in self.ctx.bots():
            bot_self_id = str(_field(bot, 'selfId', 'self_id') or '')
            bot_platform = str(_field(bot, 'platform') or '')
            if bot_self_id != self_id:
                continue
            if bot_platform == platform or (
                is_one_bot_platform(bot_platform) and is_one_bot_platform(platform)
            ):
                return bot
        return None

    # ------------------------------------------------------------------ #
    # autoAdvanceConfig（上游 :5395）
    # ------------------------------------------------------------------ #

    @property
    def auto_advance_config(self) -> dict[str, Any]:
        """上游 `get autoAdvanceConfig()`（`:5395`）逐字移植。

        覆盖 `base.py` 为 Chunk0 预留的 `_config_cache` 占位 property：上游这个
        getter 负责把 `config.runtime` 的 `?? 默认值` 补齐，而 `config.py` 并没有
        `resolve_auto_advance_config`（见模块文档串第 5 条）。输出键用 snake_case，
        因为 `helpers.automatic_interval_minutes` 以 `rest_windows` 为**首选**键读取。
        """
        cached = getattr(self, 'cached_auto_advance_config', None)
        if cached is not None:
            return cached
        runtime = _section(self.config, 'runtime')
        rest_windows = _cfg(runtime, 'restWindows')
        if rest_windows is None:
            rest_windows = [dict(_DEFAULT_REST_WINDOW)]
        resolved: dict[str, Any] = {
            'enabled': _cfg(runtime, 'autoAdvanceEnabled', _DEFAULT_AUTO_ADVANCE_ENABLED),
            'interval_minutes': max(1, _num(
                _cfg(runtime, 'autoAdvanceIntervalMinutes',
                     _DEFAULT_AUTO_ADVANCE_INTERVAL_MINUTES),
                _DEFAULT_AUTO_ADVANCE_INTERVAL_MINUTES,
            )),
            'jitter_minutes': max(0, _num(
                _cfg(runtime, 'autoAdvanceJitterMinutes', _DEFAULT_AUTO_ADVANCE_JITTER_MINUTES),
                _DEFAULT_AUTO_ADVANCE_JITTER_MINUTES,
            )),
            'follow_up_minutes': normalize_follow_up_minutes(
                _cfg(runtime, 'conversationFollowUpMinutes'),
            ),
            'follow_up_jitter_minutes': max(0, min(10, _num(
                _cfg(runtime, 'conversationFollowUpJitterMinutes',
                     _DEFAULT_FOLLOW_UP_JITTER_MINUTES),
                _DEFAULT_FOLLOW_UP_JITTER_MINUTES,
            ))),
            'rest_windows': rest_windows,
        }
        self.cached_auto_advance_config = resolved
        return resolved

    # ------------------------------------------------------------------ #
    # urgeConfig（上游 :5411）
    # ------------------------------------------------------------------ #

    @property
    def urge_config(self) -> dict[str, Any]:
        """上游 `get urgeConfig()`（`:5411`）：`resolveUrgeConfig(config.urge)`。

        上游刻意**不缓存**（Console 重载会换新的 service 实例），本移植版保持同样的
        每调用一次重新解析；`resolve_urge_config` 的键序固定，因此
        `json.dumps(urge_config)` 在 `scheduleUrgeAdvance` 与 Chunk4 之间稳定一致。
        """
        return resolve_urge_config(_section(self.config, 'urge'))

    # ------------------------------------------------------------------ #
    # effectiveUrgeRuntime（上游 :5413）
    # ------------------------------------------------------------------ #

    @property
    def effective_urge_runtime(self) -> dict[str, Any]:
        """上游 `get effectiveUrgeRuntime()`（`:5413`）逐字移植。

        Urge 开启时用 `urge.willingness` 顶替 `runtime.proactiveWillingnessThreshold`，
        让主动联系阈值只有**一个**配置来源。
        """
        urge = self.urge_config
        runtime = self.runtime_config
        if urge.get('enabled'):
            # 上游写 camelCase `proactiveWillingnessThreshold`；本移植版配置层
            # （`config.py` 的 runtime 默认值）与已落地的契约测试都用 snake_case，
            # 而 chunk4 的消费者一律 `_cfg()` 双读，故这里写 snake_case。
            return {**runtime, 'proactive_willingness_threshold': urge.get('willingness')}
        return runtime

    # ------------------------------------------------------------------ #
    # scheduleUrgeAdvance（上游 :5419）
    # ------------------------------------------------------------------ #

    async def schedule_urge_advance(
        self, story: Any, anchor: Any, incoming: bool = False,
    ) -> None:
        """上游 `scheduleUrgeAdvance(story, anchor, incoming = false)`（`:5419`）逐条移植。

        采样下一次自动推进时刻：休息窗口内用窗口自己的间隔区间，对话热度过低时按
        密度衰减排期，并把 `lastUserMessageAt` / `nextAdvanceAt` 一起写回
        `story.state.automation`（同时清空对话连续性补写闸门）。随机源不注入
        （上游 `planUrge` 内部直接用 `Math.random()`；本移植版的 `plan_urge`
        默认即模块级 `random.random()`，注入反而会让 `Object.create(prototype)`
        式桩对象因为缺 `ctx` 而崩）。
        """
        config = self.urge_config
        state = decode_story_state(pick(story, 'state'))
        extensions = _record(state.get('extensions'))
        anchor_ms = dt_ms(anchor)
        urge = normalize_urge_state(extensions.get('urge'), anchor_ms)
        if incoming:
            urge = urge_user_event(urge, anchor_ms)
        auto_advance = self.auto_advance_config
        timezone = _str(pick(pick(story, 'setting'), 'timezone'))
        rest = active_rest_window(_cfg(auto_advance, 'restWindows'), timezone, anchor)
        window = active_agency_window(state.get('agency_window'), anchor)
        planned = plan_urge(
            urge,
            anchor_ms,
            config,
            automatic_interval_minutes(story, anchor, auto_advance) if rest else 0,
            bool(window) and (
                pick(window, 'deviceAccess', 'device_access') != 'available'
                or pick(window, 'activityLoad', 'activity_load') == 'overloaded'
            ),
        )
        ordinary = parse_dt(planned.get('next_advance_at'))
        next_at = ordinary
        if planned.get('reason') == 'conversation-density-decay':
            anchored = await self.schedule_preplan_anchored_time(story, anchor, ordinary)
            next_at = parse_dt(anchored) or ordinary
        automation = _record(state.get('automation'))
        await self.db_set('interlude_story', {'id': pick(story, 'id')}, {
            'state': encode_story_state({
                **state,
                'extensions': {
                    **extensions,
                    # 上游 `{...planned.state, mode: JSON.stringify(config)}`。
                    'urge': {
                        **_record(planned.get('state')),
                        'mode': json.dumps(config, ensure_ascii=False),
                    },
                },
                'automation': {
                    **automation,
                    'quiet_until': None,
                    'conversation_follow_up_at': [],
                    'conversation_follow_up_participant_id': None,
                    **({'last_user_message_at': iso(anchor)} if incoming else {}),
                    'next_advance_at': iso(next_at),
                },
            }),
            'updatedAt': anchor,
        })
        burst_used = _record(_record(planned.get('state')).get('burst')).get('used') or 0
        self.report_operation(
            'standard', 'info', story, 'advance',
            'Urge 调度 档位=%s 原因=%s 下次=%s 加速已用=%d/%d',
            config.get('frequency'), planned.get('reason'), format_log_time(next_at, timezone),
            burst_used, config.get('budget'),
        )

