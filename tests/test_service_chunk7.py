"""`plugin/core/service/chunk7.py`（`upstream/src/service.ts:5442-6082`）的单元测试。

运行：

    cd /home/kela/文档/harness/hds-interlude && python3 -m unittest plugin.tests.test_service_chunk7 -v

包含两类用例：

1. **上游用例逐条移植**（`@unittest.skipUnless` 守门）
   - `upstream/test/urge.test.ts` 末尾 5 条依赖 service 调度 API 的集成用例
     → `UrgeServiceSchedulingTests`（其中 `recordAutomaticDelivery` 那条断言的是
     chunk6 的成员，按约定做归属标注）；
   - `upstream/test/beta6-handoff.test.ts` 的
     `new private life handoff still feeds Preplan without forwarding private prose`
     → `SchedulePreplanEvidenceTests`（断言 `schedulePreplanEvidence`，本范围成员）；
   - `upstream/test/continuity-guards.test.ts`、`memory-continuity.test.ts`、
     `database-upgrade.test.ts` 里**没有**断言本范围成员的用例（分别是
     `normalizeScenePresenceDrafts` / `toPromptPayload`、`facts` / `persistFact` /
     `shouldRefreshContinuity`、`registerTables`），故只在本文件里记录归属，不重复移植。
2. **本移植版新增的可独立验证用例** → `AutomaticAdvanceGateTests` /
   `ParticipantStateTests` / `ContinuityAndMigrationTests` / `SchedulePreplanStoreTests`
   （用真 sqlite 内存库，不 mock 数据库）。

上游 `urge.test.ts` 的宿主对象是 `Object.create(InterludeService.prototype)`；本移植版
用 `UrgeHost`（继承 `ServiceChunk7` + 已落地的 `ServiceChunk6`）复刻同一形状，并按
上游测试的做法桩掉 `getStory` / `dbSet` / `reportOperation` 三个跨 mixin 接缝。
"""

from __future__ import annotations

import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from plugin.core import logging as interlude_logging
from plugin.core.database import Database
from plugin.core.schedule_preplan import (
    DEFAULT_SCHEDULE_PREPLAN_CONFIG,
    apply_schedule_preplan_proposal,
    materialize_schedule_preplan,
    resolve_schedule_preplan_config,
)
from plugin.core.service import (
    InterludeContext,
    InterludeService,
    NullTransport,
    ServiceBase,
    SessionView,
)
from plugin.core.service.chunk7 import ServiceChunk7
from plugin.core.service.helpers import normalize_participant_state
from plugin.core.story_state import decode_story_state, encode_story_state
from plugin.core.time import iso, parse_dt
from plugin.core.types import empty_participant_state, empty_story_setting, empty_story_state
from plugin.core.urge import commit_urge, normalize_urge_state, resolve_urge_config

try:  # chunk6（`:4814-5441`）由并行任务产出；缺失时相关用例自动 skip。
    from plugin.core.service.chunk6 import ServiceChunk6
except ImportError:  # pragma: no cover - 取决于同批任务的落地顺序
    ServiceChunk6 = None  # type: ignore[assignment]

#: `src/service.ts:5411` / `:5413` / `:5333` 属 chunk6；这些断言只在 chunk6 落地后跑。
_CHUNK6_READY = ServiceChunk6 is not None and hasattr(
    InterludeService, 'effective_urge_runtime',
)
_CHUNK6_RECORD_DELIVERY_READY = ServiceChunk6 is not None and hasattr(
    InterludeService, 'record_automatic_delivery',
)

#: 上游 `const now = Date.parse('2026-09-07T04:00:00Z'), minute = 60_000`。
NOW = datetime(2026, 9, 7, 4, 0, 0, tzinfo=timezone.utc)
MINUTE = timedelta(minutes=1)

#: 上游 `resolveUrgeConfig({ enabled: true, advanced: { jitter: 0, extremeChance: 0 } })`。
URGE_CONFIG = resolve_urge_config({'enabled': True, 'advanced': {'jitter': 0, 'extremeChance': 0}})
#: 上游 `const high = { value: .9, pace: 'normal', basisQuote: '她想再问一句。' }`。
HIGH = {'value': .9, 'pace': 'normal', 'basisQuote': '她想再问一句。'}

#: 上游 `const date = new Date('2026-08-30T11:22:00.000Z')`（memory-continuity 的口径）。
SCHEDULE_DATE = datetime(2026, 8, 30, 0, 0, 0, tzinfo=timezone.utc)


def _scheduled_time(value: Any) -> Optional[datetime]:
    """等价上游 `Date.parse(x)` 后与 `Date` 比较：这里统一解析成 datetime。"""
    return parse_dt(value)


def _urge_willingness(runtime: dict[str, Any]) -> Any:
    """读 `effectiveUrgeRuntime` 的主动意愿阈值。

    上游 `src/service.ts:5413` 用 camelCase 键 `proactiveWillingnessThreshold`；
    本移植版的 chunk6 按 `config.py` 的约定输出 snake_case。同一字段只会出现一种
    拼写，两种都认，避免测试被键名口径绑死。
    """
    if 'proactive_willingness_threshold' in runtime:
        return runtime['proactive_willingness_threshold']
    return runtime['proactiveWillingnessThreshold']


# =========================================================================== #
# 夹具
# =========================================================================== #

def make_config(**overrides: Any) -> dict[str, Any]:
    """一份最小可用配置（只含本范围读到的分组）。"""
    config: dict[str, Any] = {
        'model': {},
        # `runtime` 用本移植版 `config.py` 的 snake_case 键（chunk6 的
        # `autoAdvanceConfig` / `effectiveUrgeRuntime` 都按 snake_case 输出，
        # 读取侧一律双拼写；这里只写一种拼写，避免同一字段出现两个值）。
        'runtime': {
            'auto_advance_enabled': True,
            'auto_advance_interval_minutes': 40,
            'auto_advance_jitter_minutes': 0,
            'conversation_follow_up_minutes': [10, 20],
            'conversation_follow_up_jitter_minutes': 0,
            'rest_windows': [],
            'proactive_willingness_threshold': .65,
            'context_entry_limit': 50,
            'memory_limit': 20,
        },
        'storyDefaults': {
            'characterName': '测试角色', 'characterProfile': '安静的女高中生',
            'userProfile': '用户', 'relationship': '朋友', 'world': '现实',
            'perspective': '克制', 'supportingCast': '', 'location': '东京',
            'style': '', 'timezone': 'Asia/Shanghai',
        },
        'sharedStory': {},
        'onebot': {'enabled': False},
        'schedulePreplan': {'enabled': False},
        'logging': {'level': 'debug', 'verbosity': 'diagnostic', 'format': 'layered'},
    }
    config.update(overrides)
    return config


class _Sink:
    """把分层日志收进内存，避免测试输出噪音。"""

    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def __call__(self, level: str, text: str) -> None:
        self.records.append((level, text))


class ServiceFixtureMixin:
    """共享夹具：真 sqlite 内存库 + 内存日志 sink + `NullTransport`。"""

    def setUp(self) -> None:
        self.sink = _Sink()
        interlude_logging.set_log_sink(self.sink)
        self.addCleanup(interlude_logging.set_log_sink, interlude_logging._default_sink)
        self.db = Database(':memory:')
        self.addCleanup(self.db.close)
        self.db.register_tables()
        self.config = make_config()
        self.service = self.make_service(self.config)

    def make_service(self, config: Optional[dict[str, Any]] = None) -> Any:
        """构造真实组装后的 `InterludeService`，但阻断构造期排定的后台调度。"""
        service = InterludeService(
            # 固定时钟：`now()` / `now_ms()` 必须与用例里的 NOW 一致，
            # 退避 / 冷却这类相对时间的断言才有意义。
            InterludeContext(logger=None, database=self.db, clock=lambda: NOW),
            config if config is not None else self.config,
            self.db,
            NullTransport(),
        )
        # 上游把后台调度推迟一个事件循环；测试里立刻置位，避免定时器真的跑起来。
        service.background_started = True
        return service

    async def insert_story(self, story_id: str = 'story', **overrides: Any) -> dict[str, Any]:
        row: dict[str, Any] = {
            'id': story_id, 'platform': 'onebot', 'selfId': '1', 'userId': '', 'channelId': '',
            'status': 'active', 'setting': empty_story_setting(),
            'state': empty_story_state(), 'cursorAt': NOW, 'createdAt': NOW, 'updatedAt': NOW,
        }
        row.update(overrides)
        return await self.service.db_create('interlude_story', row)

    async def story_row(self, story_id: str = 'story') -> dict[str, Any]:
        rows = await self.service.db_get('interlude_story', {'id': story_id})
        self.assertTrue(rows, '剧本行应已写入')
        return rows[0]


class SyncServiceTestCase(ServiceFixtureMixin, unittest.TestCase):
    """同步测试基类。"""


class AsyncServiceTestCase(unittest.IsolatedAsyncioTestCase, ServiceFixtureMixin):
    """异步测试基类（`IsolatedAsyncioTestCase` 必须排在 MRO 前面）。"""

    def setUp(self) -> None:
        ServiceFixtureMixin.setUp(self)


# --------------------------------------------------------------------------- #
# 上游 `urge.test.ts` 的宿主
# --------------------------------------------------------------------------- #

_HOST_BASES: tuple[type, ...] = (
    (ServiceChunk7, ServiceChunk6) if ServiceChunk6 is not None else (ServiceChunk7,)
)


class UrgeHost(*_HOST_BASES):  # type: ignore[misc]
    """上游 `function host(enabled = true)`（`urge.test.ts:106`）。

    `Object.create(InterludeService.prototype)` → 这里只装配 chunk7（以及已落地的
    chunk6）并桩掉上游测试同样桩掉的三个接缝（`getStory` / `dbSet` /
    `reportOperation`）；`scheduleUrgeAdvance` 在 chunk6 未落地时退化成一个
    只完成上游那段写入的等价桩，保证 chunk7 的 Urge 分支仍被真实执行。
    """

    def __init__(self, urge_enabled: bool = True) -> None:
        ServiceBase.__init__(
            self, InterludeContext(), {'urge': {'enabled': urge_enabled}},
        )
        self.config['runtime'] = dict(make_config()['runtime'])
        self.config['schedulePreplan'] = {'enabled': False}
        self._story: dict[str, Any] = {
            'id': 's',
            'setting': {'timezone': 'Asia/Shanghai'},
            'cursorAt': NOW,
            'state': decode_story_state({
                'automation': {'conversationFollowUpAt': [iso(NOW)]},
            }),
        }
        self.operations: list[Any] = []
        self.urge_calls: list[tuple[Any, Any, bool]] = []

    # ---- 跨 mixin 接缝（上游测试同款桩） ----

    async def get_story(self, story_id: str) -> Any:  # type: ignore[override]
        return self._story

    async def db_set(self, table: str, query: Any, patch: Any) -> None:  # type: ignore[override]
        self._story = {**self._story, **patch}

    def report_operation(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        self.operations.append(args)

    if ServiceChunk6 is None:  # pragma: no cover - chunk6 落地后走真实实现

        async def schedule_urge_advance(  # type: ignore[override]
            self, story: Any, anchor: Any, incoming: bool = False,
        ) -> None:
            """chunk6 `scheduleUrgeAdvance` 的等价桩（`:5419`）。

            只复刻上游那段对断言可见的写入：清空固定补写、落一个 Urge 锚定的
            下次推进时刻，`incoming` 时补 `lastUserMessageAt`。
            """
            self.urge_calls.append((story.get('id'), anchor, incoming))
            state = decode_story_state(story.get('state'))
            automation = dict(state.get('automation') or {})
            automation.update({
                'quiet_until': None,
                'conversation_follow_up_at': [],
                'conversation_follow_up_participant_id': None,
                'next_advance_at': iso(anchor + 5 * MINUTE),
            })
            if incoming:
                automation['last_user_message_at'] = iso(anchor)
            state['automation'] = automation
            await self.db_set(
                'interlude_story', {'id': story.get('id')},
                {'state': encode_story_state(state), 'updatedAt': anchor},
            )

    # ---- 断言辅助 ----

    def story(self) -> dict[str, Any]:
        return self._story

    def automation(self) -> dict[str, Any]:
        return (self._story.get('state') or {}).get('automation') or {}


# =========================================================================== #
# 上游用例移植：`urge.test.ts` 末尾 5 条 service 集成用例
# =========================================================================== #

class UrgeServiceSchedulingTests(unittest.IsolatedAsyncioTestCase):
    """上游 `urge.test.ts:115-165` 的 service 调度用例。"""

    async def test_real_service_scheduling_replaces_fixed_followups_persists_one_deadline_and_shares_threshold(self):
        """上游 `real service scheduling replaces fixed followups, persists one deadline...`（`:115`）。"""
        host = UrgeHost()
        await host.pause_automatic_advance_after_user_message('s', NOW)
        self.assertEqual(host.automation()['conversation_follow_up_at'], [])
        # 本移植版新增的一致性断言：入站消息锚点也要落库。
        self.assertEqual(host.automation()['last_user_message_at'], iso(NOW))
        if ServiceChunk6 is None:
            self.assertEqual(host.urge_calls, [('s', NOW, True)])
        before = host.automation()['next_advance_at']
        host.is_automatic_advance_due(host.story(), NOW + MINUTE)
        self.assertEqual(host.automation()['next_advance_at'], before)

        if not _CHUNK6_READY:
            self.skipTest('effectiveUrgeRuntime（:5413）归 chunk6；chunk6 尚未落地')
        self.assertEqual(_urge_willingness(host.effective_urge_runtime), .4)
        host.config['urge']['enabled'] = False
        self.assertEqual(_urge_willingness(host.effective_urge_runtime), .65)
        await host.schedule_next_automatic_advance('s', NOW)
        self.assertEqual(host.automation()['next_advance_at'], iso(NOW + 40 * MINUTE))

    async def test_delayed_reply_anchors_urge_at_actual_planned_endpoint_without_adding_density(self):
        """上游 `delayed reply anchors Urge at actual/planned endpoint...`（`:128`）。"""
        host = UrgeHost()
        await host.pause_automatic_advance_after_delayed_reply('s', NOW + 15 * MINUTE, 'alice')
        anchored = normalize_urge_state(
            (host.story()['state'].get('extensions') or {}).get('urge'), NOW + 15 * MINUTE,
        )
        self.assertEqual(len(anchored['buckets']), 0)
        self.assertTrue(
            _scheduled_time(host.automation()['next_advance_at']) > NOW + 15 * MINUTE,
        )
        if ServiceChunk6 is None:
            # 本移植版新增：锚点必须是这条延迟回复的 `now`，不是上一个回合的时刻。
            self.assertEqual(host.urge_calls[-1][1], NOW + 15 * MINUTE)

    async def test_disabled_urge_preserves_old_followup_scheduling(self):
        """上游 `disabled Urge preserves old followup scheduling`（`:134`）。"""
        host = UrgeHost(False)
        await host.schedule_conversation_follow_ups_after_turn('s', NOW, None, 'alice')
        planned = host.automation()['conversation_follow_up_at']
        self.assertEqual(len(planned), 2)
        self.assertIsNone((host.story()['state'].get('extensions') or {}).get('urge'))
        # 本移植版新增：10/20 分钟档、末次补写即常规推进、参与者指针就位。
        self.assertEqual([parse_dt(value) for value in planned], [NOW + 10 * MINUTE, NOW + 20 * MINUTE])
        self.assertEqual(host.automation()['next_advance_at'], iso(NOW + 20 * MINUTE))
        self.assertEqual(host.automation()['conversation_follow_up_participant_id'], 'alice')

    async def test_service_slow_excludes_soft_preplan_and_due_planning_never_overwrites_unrelated_state(self):
        """上游 `service slow excludes soft Preplan and due planning...`（`:155`）。"""
        host = UrgeHost()
        slow = commit_urge(
            normalize_urge_state({}, NOW),
            {**HIGH, 'pace': 'slow', 'suggestedDelayMinutes': 120},
            HIGH['basisQuote'], 9, None, NOW, URGE_CONFIG,
        )
        host.story()['state']['extensions'] = {'urge': slow, 'untouched': {'x': 1}}
        host.automation()['timeline_retry_at'] = iso(NOW + 2 * MINUTE)

        def fail(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError('soft Preplan must not shorten slow')

        host.schedule_preplan_anchored_time = fail
        await host.schedule_next_automatic_advance('s', NOW)
        # 本移植版新增：slow 走 Urge 分支，软 Preplan 一次都没被问到。
        if ServiceChunk6 is None:
            self.assertEqual(len(host.urge_calls), 1)
        self.assertEqual(host.story()['state']['extensions']['untouched'], {'x': 1})
        self.assertEqual(host.automation()['timeline_retry_at'], iso(NOW + 2 * MINUTE))
        if not _CHUNK6_READY:
            self.skipTest('slow 的 110 分钟下界由 chunk6 的 planUrge 决定；chunk6 尚未落地')
        self.assertTrue(
            _scheduled_time(host.automation()['next_advance_at']) >= NOW + 110 * MINUTE,
        )

    @unittest.skipUnless(
        _CHUNK6_RECORD_DELIVERY_READY,
        '断言的是 chunk6 的 recordAutomaticDelivery（:5333）；已在 test_urge.py 的 '
        'ServiceIntegrationTests 中同样移植，这里保留以便 chunk7 的宿主一起跑',
    )
    async def test_service_first_bubble_receipt_activates_urge_but_still_waits_to_summarize_all_bubbles(self):
        """上游 `service first-bubble receipt activates Urge...`（`urge.test.ts:141`）。"""
        host = UrgeHost()
        host.story()['state']['extensions'] = {
            'urge': commit_urge(
                normalize_urge_state({}, NOW), HIGH, HIGH['basisQuote'], 9, 'alice',
                NOW, URGE_CONFIG, lambda: .5,
            ),
        }

        async def db_get(*args: Any, **kwargs: Any) -> list[Any]:
            return [{
                'metadata': {
                    'delivery_actions': [
                        {'participantId': 'alice', 'eventKind': 'outgoing-message', 'status': 'partial'},
                    ],
                },
            }]

        host.db_get = db_get  # type: ignore[assignment]
        host.report_standalone = (  # type: ignore[assignment]
            lambda *args, **kwargs: self.fail('unexpected projection error')
        )
        await host.record_automatic_delivery(  # type: ignore[attr-defined]
            's', 'alice', {'source_entry_id': 9, 'summary': 'contact'}, NOW,
        )
        urge_state = host.story()['state']['extensions']['urge']
        self.assertEqual(urge_state['burst']['participant_id'], 'alice')
        self.assertEqual(urge_state['burst']['used'], 1)
        self.assertEqual(len(host.story()['state'].get('automatic_delivery_summaries') or []), 0)
        deadline = host.automation()['next_advance_at']
        await host.record_automatic_delivery(  # type: ignore[attr-defined]
            's', 'alice', {'source_entry_id': 9, 'summary': 'contact'}, NOW + MINUTE,
        )
        self.assertEqual(host.automation()['next_advance_at'], deadline)


# =========================================================================== #
# 上游用例移植：`beta6-handoff.test.ts` 的 schedulePreplanEvidence
# =========================================================================== #

class SchedulePreplanEvidenceHost:
    """上游 `const host = { sharedStoryConfig: {...}, dbGet: async () => [row] }`。"""

    def __init__(self, rows: list[Any], share_participant_details: bool = False) -> None:
        self.shared_story_config = {'shareParticipantDetails': share_participant_details}
        self.service = self
        self.config: dict[str, Any] = {}
        self._rows = rows
        self.queries: list[Any] = []

    async def db_get(self, table: str, query: Any, options: Any = None) -> list[Any]:
        self.queries.append((table, query, options))
        return list(self._rows)


class SchedulePreplanEvidenceTests(unittest.IsolatedAsyncioTestCase):
    """上游 `beta6-handoff.test.ts` 的 `schedulePreplanEvidence` 用例。"""

    @staticmethod
    def _entry(entry_id: int, content: str) -> dict[str, Any]:
        return {
            'id': entry_id, 'storyId': 's', 'participantId': 'alice', 'kind': 'script',
            'actor': 'narrator', 'content': content, 'occurredAt': NOW,
            'createdAt': NOW, 'metadata': {},
        }

    async def test_new_private_life_handoff_still_feeds_preplan_without_forwarding_private_prose(self):
        """上游 `new private life handoff still feeds Preplan...`（`beta6-handoff.test.ts:247`）。"""
        row = self._entry(30, '她整理书包，想到小桃私下说的秘密。')
        row['metadata'] = {
            'narrativeAuthority': 'original-v2',
            'lifeHandoff': {'activity': {'value': '整理书包', 'quote': '整理书包'}},
            'timelinePlan': {'beats': [{'at': 1, 'kind': 'state', 'summary': '猜测用户在玩游戏'}]},
        }
        host = SchedulePreplanEvidenceHost([row])
        projected = await ServiceChunk7.schedule_preplan_evidence(host, 's', 0)  # type: ignore[arg-type]
        self.assertEqual(len(projected), 1)
        self.assertIn('整理书包', projected[0]['content'])
        # 上游同样断言：私密散文 / 导演猜测 / 两个 metadata 键都不得外泄。
        serialized = json.dumps(projected, ensure_ascii=False, default=iso)
        self.assertNotIn('秘密', serialized)
        self.assertNotIn('猜测用户', serialized)
        self.assertNotIn('lifeHandoff', serialized)
        self.assertNotIn('life_handoff', serialized)
        self.assertNotIn('timelinePlan', serialized)
        self.assertNotIn('timeline_plan', serialized)
        self.assertEqual(projected[0]['participantId'], '')
        self.assertIn('秘密', row['content'], '已提交的原文保持不动')

    async def test_share_participant_details_returns_the_raw_rows(self):
        """上游 `if (this.sharedStoryConfig.shareParticipantDetails) return entries`（`:6012`）。"""
        rows = [self._entry(1, '原始散文'), self._entry(2, '另一条')]
        host = SchedulePreplanEvidenceHost(rows, share_participant_details=True)
        projected = await ServiceChunk7.schedule_preplan_evidence(host, 's', 0)  # type: ignore[arg-type]
        self.assertEqual(projected, rows)

    async def test_cursor_query_filters_rows_not_yet_seen(self):
        """上游 `if (afterEntryId > 0) filter.id = { $gt: afterEntryId }`（`:6010`）。"""
        rows = [self._entry(1, '旧'), self._entry(2, '新'), self._entry(3, '更新')]
        host = SchedulePreplanEvidenceHost(rows, share_participant_details=True)
        projected = await ServiceChunk7.schedule_preplan_evidence(host, 's', 1)  # type: ignore[arg-type]
        self.assertEqual([item['id'] for item in projected], [2, 3])


# =========================================================================== #
# 本移植版新增：可独立验证的用例
# =========================================================================== #

class AutomaticAdvanceGateTests(SyncServiceTestCase):
    """自动推进闸门：`isAutomaticAdvancePaused` / `isAutomaticAdvanceDue` / `dueConversationFollowUps`。"""

    def _story(self, automation: dict[str, Any]) -> dict[str, Any]:
        return {
            'id': 'story', 'setting': {'timezone': 'Asia/Shanghai'}, 'cursorAt': NOW,
            'state': decode_story_state({'automation': automation}),
        }

    def test_quiet_until_pauses_only_while_it_is_in_the_future(self):
        """`story.state.automation.quietUntil > now`（`src/service.ts:5442`）。"""
        story = self._story({'quietUntil': iso(NOW + MINUTE)})
        self.assertTrue(self.service.is_automatic_advance_paused(story, NOW))
        self.assertFalse(self.service.is_automatic_advance_paused(story, NOW + 2 * MINUTE))
        # 边界：正好等于 now 不算暂停（上游用严格 `>`）。
        self.assertFalse(self.service.is_automatic_advance_paused(story, NOW + MINUTE))
        self.assertFalse(self.service.is_automatic_advance_paused(self._story({}), NOW))

    def test_persisted_deadline_beats_the_cursor_cadence(self):
        """`isAutomaticAdvanceDue`：有 `nextAdvanceAt` 时只看它（`src/service.ts:5475`）。"""
        story = self._story({'nextAdvanceAt': iso(NOW + 10 * MINUTE)})
        self.assertFalse(self.service.is_automatic_advance_due(story, NOW))
        self.assertTrue(self.service.is_automatic_advance_due(story, NOW + 10 * MINUTE))

    def test_old_stories_without_a_deadline_fall_back_to_the_cursor(self):
        """早于调度器存在的剧本按 `cursorAt + intervalMinutes` 判定（`:5478`）。"""
        story = self._story({})
        # 配置里的 `autoAdvanceIntervalMinutes = 40`，`autoAdvanceJitterMinutes = 0`。
        self.assertFalse(self.service.is_automatic_advance_due(story, NOW + 39 * MINUTE))
        self.assertTrue(self.service.is_automatic_advance_due(story, NOW + 40 * MINUTE))

    def test_disabled_auto_advance_is_never_due(self):
        """`config.enabled === false` 时直接 false（`:5477`）。"""
        service = self.make_service(make_config(runtime={
            'autoAdvanceEnabled': False, 'autoAdvanceIntervalMinutes': 1,
        }))
        story = self._story({'nextAdvanceAt': iso(NOW - timedelta(days=1))})
        self.assertFalse(service.is_automatic_advance_due(story, NOW))


class ConversationFollowUpTests(SyncServiceTestCase):
    """`dueConversationFollowUps` / `completeConversationFollowUps`（`:5447` / `:5459`）。"""

    def _story(self, follow_ups: list[Any], **extra: Any) -> dict[str, Any]:
        return {
            'id': 'story', 'setting': {'timezone': 'Asia/Shanghai'},
            'state': decode_story_state({
                'automation': {'conversationFollowUpAt': follow_ups, **extra},
            }),
        }

    def test_due_follow_ups_are_sorted_and_limited_to_the_past(self):
        story = self._story([
            iso(NOW + 20 * MINUTE), iso(NOW - 5 * MINUTE), iso(NOW + 10 * MINUTE),
        ])
        due = self.service.due_conversation_follow_ups(story, NOW)
        self.assertEqual(due, [NOW - 5 * MINUTE])
        self.assertEqual(
            self.service.due_conversation_follow_ups(story, NOW + 11 * MINUTE),
            [NOW - 5 * MINUTE, NOW + 10 * MINUTE],
        )

    def test_urge_owns_the_follow_up_lane_when_enabled(self):
        """上游 `if (this.urgeConfig.enabled) return []`（`:5448`）。"""
        service = self.make_service(make_config(urge={'enabled': True}))
        story = self._story([iso(NOW - MINUTE)])
        self.assertEqual(service.due_conversation_follow_ups(story, NOW), [])
        self.assertEqual(self.service.due_conversation_follow_ups(story, NOW), [NOW - MINUTE])

    def test_completing_keeps_the_future_pass_and_clears_the_pointer_when_empty(self):
        """上游 `completeConversationFollowUps`：一次写作回合后只留未来那一档（`:5459`）。"""
        async def run() -> tuple[bool, dict[str, Any]]:
            await self.insert_story('story', state=decode_story_state({
                'automation': {
                    'conversationFollowUpAt': [iso(NOW - MINUTE), iso(NOW + 10 * MINUTE)],
                    'conversationFollowUpParticipantId': 'alice',
                    'nextAdvanceAt': iso(NOW - MINUTE),
                },
            }))
            kept = await self.service.complete_conversation_follow_ups('story', NOW)
            row = await self.story_row('story')
            return kept, decode_story_state(row['state'])['automation']

        kept, automation = asyncio.run(run())
        self.assertTrue(kept)
        self.assertEqual(automation['conversation_follow_up_at'], [iso(NOW + 10 * MINUTE)])
        self.assertEqual(automation['next_advance_at'], iso(NOW + 10 * MINUTE))
        self.assertEqual(automation['conversation_follow_up_participant_id'], 'alice')

        async def run_empty() -> tuple[bool, dict[str, Any]]:
            kept_again = await self.service.complete_conversation_follow_ups('story', NOW + 20 * MINUTE)
            row = await self.story_row('story')
            return kept_again, decode_story_state(row['state'])['automation']

        kept_again, automation_after = asyncio.run(run_empty())
        self.assertFalse(kept_again)
        self.assertEqual(automation_after['conversation_follow_up_at'], [])
        self.assertIsNone(automation_after['conversation_follow_up_participant_id'])
        self.assertIsNone(automation_after['next_advance_at'])


class CompactionGuardTests(SyncServiceTestCase):
    """`compactionFingerprint` / `compactionIsBackedOff` / `noteCompactionFailure`（`:5866-5888`）。"""

    def test_fingerprint_is_a_stable_scene_entry_signature(self):
        scene = {'id': 7, 'lastEntryId': 3}
        entries = [{'id': 4}, {'id': 5}, {'id': 6}]
        self.assertEqual(self.service.compaction_fingerprint(scene, entries, 1234), '7:3:4-6:3:1234')
        # 空条目：两端都按 0（上游 `entries[0]?.id ?? 0`）。
        self.assertEqual(self.service.compaction_fingerprint(scene, [], 0), '7:3:0-0:0:0')
        # 缺少 lastEntryId 时按 0。
        self.assertEqual(
            self.service.compaction_fingerprint({'id': 7}, [{'id': 1}], 10), '7:0:1-1:1:10',
        )

    def test_backoff_only_matches_the_same_fingerprint_and_expires(self):
        story_id, fingerprint = 'story', '7:3:4-6:3:100'
        self.assertFalse(self.service.compaction_is_backed_off(story_id, fingerprint, NOW))
        self.service.note_compaction_failure(story_id, fingerprint, 'boom')
        self.assertTrue(self.service.compaction_is_backed_off(story_id, fingerprint, NOW))
        # 指纹变了（场景推进过）就不再退避。
        self.assertFalse(self.service.compaction_is_backed_off(story_id, 'other', NOW))
        # 2 小时后冷却结束，并顺手删掉记录。
        self.assertFalse(
            self.service.compaction_is_backed_off(story_id, fingerprint, NOW + timedelta(hours=3)),
        )
        self.assertNotIn(story_id, self.service.compaction_backoff)

    def test_backoff_accepts_both_datetime_and_millisecond_clock(self):
        self.service.note_compaction_failure('s', 'fp', 'boom')
        until = self.service.compaction_backoff['s']['until']
        self.assertTrue(self.service.compaction_is_backed_off('s', 'fp', until - 1))
        self.assertFalse(self.service.compaction_is_backed_off('s', 'fp', until))

    def test_checkpoint_advance_requires_the_persisted_scene_to_move(self):
        """上游 `compactionCheckpointAdvanced`（`:5891`）。"""
        async def run() -> list[bool]:
            await self.insert_story('story')
            scene = await self.service.db_create('interlude_scene', {
                'storyId': 'story', 'status': 'active', 'startedAt': NOW, 'endedAt': None,
                'hook': '', 'summary': '', 'entryCount': 0, 'lastEntryId': 2,
                'createdAt': NOW, 'updatedAt': NOW,
            })
            moved_context = {'scene': {'id': scene['id']}, 'scene_entries': [{'id': 1}, {'id': 2}]}
            moved = await self.service.compaction_checkpoint_advanced(moved_context)
            # 场景检查点仍是 2，但本次要处理的最后一条是 3：写入丢了 → 必须拦下。
            stale_context = {'scene': {'id': scene['id']}, 'scene_entries': [{'id': 1}, {'id': 3}]}
            stale = await self.service.compaction_checkpoint_advanced(stale_context)
            await self.service.db_set(
                'interlude_scene', {'id': scene['id']}, {'status': 'closed', 'lastEntryId': 1},
            )
            closed = await self.service.compaction_checkpoint_advanced(stale_context)
            empty = await self.service.compaction_checkpoint_advanced(
                {'scene': {'id': scene['id']}, 'scene_entries': []},
            )
            missing = await self.service.compaction_checkpoint_advanced(
                {'scene': {'id': 999}, 'scene_entries': [{'id': 1}]},
            )
            return [stale, moved, closed, empty, missing]

        stale, moved, closed, empty, missing = asyncio.run(run())
        self.assertFalse(stale, '未推进的场景必须让守卫拦下来')
        self.assertTrue(moved)
        self.assertTrue(closed, '场景已关闭同样算检查点前进')
        self.assertTrue(empty, '没有期望的条目 id 时直接放行')
        self.assertFalse(missing, '场景行不存在视为没有推进')

    def test_schedule_compaction_needs_a_switch_and_is_idempotent(self):
        """上游 `scheduleCompaction`（`:5898`）：两个开关都关就完全不排队。"""
        paused = self.make_service(make_config(
            memory={'enabled': False}, schedulePreplan={'enabled': False},
        ))
        paused.schedule_compaction('story')
        self.assertEqual(paused.scheduled_compactions, set())

        # 运行期暂停 / 清库中：排队后必须立刻撤销，否则重载后再也排不进来。
        service = self.make_service(make_config(memory={'enabled': True}))
        service.desktop_runtime_phase = 'paused'
        service.schedule_compaction('story')
        self.assertEqual(service.scheduled_compactions, set())

        service.desktop_runtime_phase = 'running'
        service.database_resetting = True
        service.schedule_compaction('story')
        self.assertEqual(service.scheduled_compactions, set())

    def test_compact_stories_is_gated_by_phase_sweep_flag_and_story_availability(self):
        """上游 `compactStories`（`:5986`）的三个提前返回分支。"""
        service = self.make_service(make_config(memory={'enabled': True}))
        service.desktop_runtime_phase = 'paused'
        asyncio.run(service.compact_stories())
        self.assertFalse(service.compaction_sweep_running)

        service.desktop_runtime_phase = 'running'
        service.compaction_sweep_running = True
        asyncio.run(service.compact_stories())
        self.assertTrue(service.compaction_sweep_running, '重入时原值不得被改写')

        service.compaction_sweep_running = False
        # 库里没有 canonical 剧本：扫一遍后干净返回，什么也不排队。
        asyncio.run(service.compact_stories())
        self.assertFalse(service.compaction_sweep_running)
        self.assertEqual(service.scheduled_compactions, set())


class SchedulePreplanAnchorTests(SyncServiceTestCase):
    """`schedulePreplanAnchoredTime`（`:5563`）：只提前、不推后。"""

    def test_disabled_or_unanchored_returns_the_ordinary_time(self):
        for section in ({'enabled': False}, {'enabled': True, 'anchorAutoAdvance': False}):
            config = make_config()
            service = self.make_service(config)
            service.cached_schedule_preplan_config = resolve_schedule_preplan_config(section)
            ordinary = NOW + 40 * MINUTE
            story = {'id': 'story', 'setting': {'timezone': 'Asia/Shanghai'}}
            self.assertEqual(
                asyncio.run(service.schedule_preplan_anchored_time(story, NOW, ordinary)), ordinary,
            )

    def test_fixed_block_boundary_pulls_the_next_advance_forward(self):
        """固定块（`kind: 'fixed'`）可以锚定；`13:30 Asia/Shanghai` → `14:00`（上游 `:5563`）。"""
        config = make_config()
        service = self.make_service(config)
        service.cached_schedule_preplan_config = resolve_schedule_preplan_config({
            'enabled': True, 'anchorAutoAdvance': True,
        })
        now = datetime(2026, 8, 31, 5, 30, tzinfo=timezone.utc)  # 13:30 Asia/Shanghai
        record = {
            'story_id': 'story', 'revision': 2, 'timezone': 'Asia/Shanghai',
            'valid_from': '2026-08-30', 'valid_through': '2026-09-12',
            'last_reviewed_local_date': '2026-08-30', 'last_evidence_entry_id': 10,
            'review_reason': 'stable',
            'regimes': [{
                'id': 'summer', 'label': '暑假', 'from': '2026-08-01', 'to': '2026-09-02',
                'weekly': {'monday': [
                    {'id': 'class', 'start': '14:00', 'end': '17:00', 'label': '补课', 'kind': 'fixed'},
                    {'id': 'drawing', 'start': '20:00', 'end': '21:30', 'label': '画画', 'kind': 'flexible'},
                ]},
            }],
            'exceptions': [],
            'materialized_days': materialize_schedule_preplan(
                [{'id': 'summer', 'label': '暑假', 'from': '2026-08-01', 'to': '2026-09-02',
                  'weekly': {'monday': [
                      {'id': 'class', 'start': '14:00', 'end': '17:00', 'label': '补课', 'kind': 'fixed'},
                  ]}}], [], '2026-08-30', 14,
            ),
            'created_at': SCHEDULE_DATE, 'updated_at': SCHEDULE_DATE,
        }

        async def run() -> tuple[datetime, datetime]:
            await self.insert_story('story')
            await service.save_schedule_preplan(record)
            story = {'id': 'story', 'setting': {'timezone': 'Asia/Shanghai'}}
            anchored = await service.schedule_preplan_anchored_time(
                story, now, now + 40 * MINUTE,
            )
            # 一个紧贴 now 的常规时刻：锚点只能提前、不得推后。
            later = await service.schedule_preplan_anchored_time(
                story, now, now + 1 * MINUTE,
            )
            return anchored, later

        anchored, later = asyncio.run(run())
        self.assertEqual(anchored, datetime(2026, 8, 31, 6, 0, tzinfo=timezone.utc))
        self.assertEqual(later, now + 1 * MINUTE)


class StorySettingAndPresetTests(SyncServiceTestCase):
    """`initialStorySetting` / `participantPreset` / `userAccountRule`（`:5603-5640`）。"""

    def test_initial_setting_uses_story_defaults_and_keeps_the_canon_shape(self):
        setting = self.service.initial_story_setting('  自定义角色  ')
        self.assertEqual(setting['character']['name'], '自定义角色')
        self.assertEqual(setting['character']['profile'], '安静的女高中生')
        self.assertEqual(setting['user']['display_name'], 'Multiple participants')
        self.assertEqual(setting['user']['profile'], '用户')
        self.assertEqual(setting['relationship'], '朋友')
        self.assertEqual(setting['world'], '现实')
        self.assertEqual(setting['perspective'], '克制')
        self.assertEqual(setting['location'], '东京')
        self.assertEqual(setting['timezone'], 'Asia/Shanghai')
        # 空名字回落配置里的角色名；style 为空时保留 `emptyStorySetting` 的默认。
        self.assertEqual(self.service.initial_story_setting()[ 'character']['name'], '测试角色')
        self.assertTrue(setting['style'])

    def test_initial_setting_reads_snake_case_config_too(self):
        service = self.make_service(make_config(storyDefaults={
            'character_name': '片假名角色', 'character_profile': 'P',
            'user_profile': 'U', 'relationship': 'R', 'world': 'W',
            'perspective': 'X', 'supporting_cast': 'S', 'location': 'L',
            'style': 'ST', 'timezone': 'UTC',
        }))
        setting = service.initial_story_setting()
        self.assertEqual(setting['character']['name'], '片假名角色')
        self.assertEqual(setting['supporting_cast'], 'S')
        self.assertEqual(setting['style'], 'ST')
        self.assertEqual(setting['timezone'], 'UTC')

    def test_participant_preset_treats_a_missing_enabled_flag_as_enabled(self):
        """上游 `preset.enabled !== false`（`:5598`）。"""
        service = self.make_service(make_config(sharedStory={
            'participantPresets': [
                {'qq': '1', 'label': '停用的', 'enabled': False},
                {'qq': 'private:2', 'label': '启用的'},
            ],
        }))
        self.assertIsNone(service.participant_preset('1'))
        self.assertEqual(service.participant_preset('2')['label'], '启用的')
        self.assertEqual(service.participant_preset('QQ:2')['label'], '启用的')
        self.assertIsNone(service.participant_preset('3'))

    def test_user_account_rule_matches_normalized_qq(self):
        service = self.make_service(make_config(onebot={
            'enabled': True,
            'userAccounts': [
                {'qq': '9', 'enabled': False, 'label': '停用'},
                {'qq': 'private:10', 'label': '主人'},
            ],
        }))
        self.assertIsNone(service.user_account_rule('9'))
        self.assertEqual(service.user_account_rule('10')['label'], '主人')
        self.assertEqual(service.user_account_rule('onebot:10')['label'], '主人')
        self.assertIsNone(service.user_account_rule('11'))


class ParticipantStateTests(AsyncServiceTestCase):
    """参与者状态读写：`recordIncomingMessage` / `markParticipantSeen` /
    `recordCharacterMessage` / `updateParticipantState` / `getParticipant`（`:5643-5681`）。"""

    async def _participant(self) -> dict[str, Any]:
        return await self.service.db_create('interlude_participant', {
            'id': 'p1', 'storyId': 'story', 'platform': 'onebot', 'selfId': '1', 'userId': '2',
            'channelId': '', 'personId': '2', 'displayName': '主人', 'profile': '',
            'relationship': '', 'state': empty_participant_state(),
            'status': 'active', 'createdAt': NOW, 'updatedAt': NOW,
        })

    async def test_get_participant_returns_none_when_missing(self):
        self.assertIsNone(await self.service.get_participant('nope'))
        participant = await self._participant()
        found = await self.service.get_participant('p1')
        self.assertEqual(found['id'], participant['id'])

    async def test_incoming_message_increments_both_counters(self):
        participant = await self._participant()
        updated = await self.service.record_incoming_message(participant, NOW)
        state = updated['state']
        # `ParticipantState` 是持久化 wire format：写出侧键名保持上游 camelCase。
        self.assertEqual(state['unreadMessageCount'], 1)
        self.assertEqual(state['pendingReplyCount'], 1)
        self.assertEqual(state['lastUserMessageAt'], iso(NOW))
        stored = await self.service.get_participant('p1')
        # 读取侧统一过权威归一化器：`ParticipantState` 的两种拼写都应能读回。
        self.assertEqual(normalize_participant_state(stored['state'])['unreadMessageCount'], 1)
        # 再来一条：累加而不是覆盖。
        again = await self.service.record_incoming_message(updated, NOW + MINUTE)
        self.assertEqual(again['state']['unreadMessageCount'], 2)

    async def test_character_message_clears_both_counters_and_stamps_the_time(self):
        participant = await self.service.record_incoming_message(await self._participant(), NOW)
        seen = await self.service.mark_participant_seen(participant, NOW + MINUTE)
        self.assertEqual(seen['state']['unreadMessageCount'], 0)
        self.assertEqual(seen['state']['pendingReplyCount'], 1, 'markParticipantSeen 只清未读')
        replied = await self.service.record_character_message(participant, NOW + 2 * MINUTE)
        self.assertEqual(replied['state']['unreadMessageCount'], 0)
        self.assertEqual(replied['state']['pendingReplyCount'], 0)
        self.assertEqual(replied['state']['lastCharacterMessageAt'], iso(NOW + 2 * MINUTE))
        # 上一次的 `lastUserMessageAt` 不能被抹掉（上游是展开合并）。
        self.assertEqual(replied['state']['lastUserMessageAt'], iso(NOW))

    async def test_update_participant_state_merges_patch_and_keeps_arrays(self):
        participant = await self._participant()
        patched = await self.service.update_participant_state(
            participant, {'openThreads': ['奶茶']}, NOW,
        )
        self.assertEqual(patched['state']['openThreads'], ['奶茶'])
        stored = await self.service.get_participant('p1')
        self.assertEqual(normalize_participant_state(stored['state'])['openThreads'], ['奶茶'])
        # 上游 `mergeParticipantState`：数组字段不会被 undefined 覆盖。
        kept = await self.service.update_participant_state(
            patched, {'relationshipNotes': []}, NOW + MINUTE,
        )
        self.assertEqual(kept['state']['openThreads'], ['奶茶'])
        self.assertEqual(kept['state']['relationshipNotes'], [])


class ContinuityAndMigrationTests(AsyncServiceTestCase):
    """`ensureContinuity`（`:5840`）与旧故事迁移（`:5683` / `:5736`）。"""

    async def test_ensure_continuity_creates_one_arc_and_one_scene_and_is_idempotent(self):
        story = await self.insert_story('story')
        await self.service.ensure_continuity(story, NOW)
        arcs = await self.service.db_get('interlude_arc', {'storyId': 'story'})
        scenes = await self.service.db_get('interlude_scene', {'storyId': 'story'})
        self.assertEqual(len(arcs), 1)
        self.assertEqual(len(scenes), 1)
        self.assertEqual(arcs[0]['title'], 'Beginning')
        self.assertEqual(scenes[0]['status'], 'active')
        row = await self.story_row('story')
        state = decode_story_state(row['state'])
        self.assertEqual(state['active_arc_id'], arcs[0]['id'])
        self.assertEqual(state['active_scene_id'], scenes[0]['id'])
        # 第二个场景建立时弧线的 sceneCount 才 +1。
        self.assertEqual(arcs[0]['sceneCount'], 1)

        await self.service.ensure_continuity(row, NOW + MINUTE)
        self.assertEqual(len(await self.service.db_get('interlude_arc', {'storyId': 'story'})), 1)
        self.assertEqual(len(await self.service.db_get('interlude_scene', {'storyId': 'story'})), 1)

    async def test_migrate_legacy_story_repoints_rows_and_archives_the_old_one(self):
        """上游 `migrateLegacyStory`（`:5683`）：旧账号剧本 → 机器人绑定共享剧本。"""
        legacy = await self.insert_story('onebot:1:2', userId='2')
        entry = await self.service.db_create('interlude_script_entry', {
            'storyId': 'onebot:1:2', 'participantId': '', 'kind': 'script', 'actor': 'narrator',
            'content': '旧世界的独白。', 'occurredAt': NOW, 'metadata': {}, 'createdAt': NOW,
        })
        session = SessionView(platform='onebot', self_id='1', user_id='2')
        migrated = await self.service.migrate_legacy_story(legacy, session)
        self.assertEqual(migrated['id'], 'character:onebot:1')
        self.assertEqual(migrated['platform'], 'onebot')
        self.assertEqual(migrated['selfId'], '1')
        self.assertEqual(migrated['userId'], '')
        self.assertEqual(migrated['channelId'], '')
        # 旧行归档。
        old = await self.story_row('onebot:1:2')
        self.assertEqual(old['status'], 'archived')
        # 条目改挂到共享剧本 + 首个关系分支（`ensureParticipant` 还会追加一条
        # `participant-joined` 系统条目，因此按 id 定位而不是断言整表）。
        rows = await self.service.db_get('interlude_script_entry', {'storyId': 'character:onebot:1'})
        by_id = {row['id']: row for row in rows}
        self.assertIn(entry['id'], by_id)
        self.assertTrue(by_id[entry['id']]['participantId'])
        self.assertIn('participant-joined', [row['kind'] for row in rows])
        # 迁移后立即补齐连续性。
        self.assertEqual(len(await self.service.db_get('interlude_arc', {'storyId': 'character:onebot:1'})), 1)
        self.assertEqual(len(await self.service.db_get('interlude_scene', {'storyId': 'character:onebot:1'})), 1)

    async def test_migrate_legacy_story_joins_the_row_that_won_the_primary_key_race(self):
        """并发首访：两个旧账号都判定"共享行不存在"，输的一方加入赢家（`:5696`）。

        用 TOCTOU 复现上游注释里的竞争：共享行的可见性在第一次查询之后才变化，
        于是 `dbCreate` 撞主键，迁移必须改用"加入赢家 + 合并本分支"的路径。
        """
        legacy = await self.insert_story('onebot:1:2', userId='2')
        winner = await self.insert_story('character:onebot:1')
        session = SessionView(platform='onebot', self_id='1', user_id='2')

        original_get = self.service.db_get
        original_create = self.service.db_create
        hidden = {'pending': True}

        async def racing_get(table: str, query: Any, options: Any = None) -> list[Any]:
            if (table == 'interlude_story' and isinstance(query, dict)
                    and query.get('id') == 'character:onebot:1' and hidden['pending']):
                hidden['pending'] = False  # 第一次查不到：判定"共享行不存在"
                return []
            return await original_get(table, query, options)

        async def racing_create(table: str, data: Any) -> Any:
            if table == 'interlude_story' and data.get('id') == 'character:onebot:1':
                raise RuntimeError('UNIQUE constraint failed: interlude_story.id')
            return await original_create(table, data)

        self.service.db_get = racing_get  # type: ignore[assignment]
        self.service.db_create = racing_create  # type: ignore[assignment]
        migrated = await self.service.migrate_legacy_story(legacy, session)
        self.assertFalse(hidden['pending'], '必须真的走到主键冲突分支')
        self.assertEqual(migrated['id'], winner['id'])
        self.assertEqual(migrated['platform'], 'onebot')
        # 输的一方把旧分支并进赢家，而不是留下一个仍然 active 的旧副本。
        old = await self.story_row('onebot:1:2')
        self.assertEqual(old['status'], 'archived')
        rows = await self.service.db_get('interlude_script_entry', {'storyId': 'character:onebot:1'})
        self.assertIn('legacy-branch-merged', [row['kind'] for row in rows])


class SchedulePreplanStoreTests(AsyncServiceTestCase):
    """`saveSchedulePreplan` / `getSchedulePreplan`（`:6003` / `:6034`）与复核准备（`:6047`）。"""

    @staticmethod
    def _record(story_id: str = 'story', revision: int = 2) -> dict[str, Any]:
        config = resolve_schedule_preplan_config(None)
        record = apply_schedule_preplan_proposal(
            None,
            {
                'outcome': 'replace', 'reason': '有稳定日程',
                'regimes': [{
                    'id': 'summer', 'label': '暑假', 'from': '2026-08-01', 'to': '2026-09-02',
                    'weekly': {'monday': [
                        {'id': 'class', 'start': '14:00', 'end': '17:00', 'label': '补课', 'kind': 'fixed'},
                    ]},
                }],
                'exceptions': [],
            },
            [], '2026-08-30', 'Asia/Shanghai', config, SCHEDULE_DATE,
        )
        assert record is not None
        record['story_id'] = story_id
        record['revision'] = revision
        return record

    async def test_round_trip_writes_camel_case_columns_and_patches_without_the_primary_key(self):
        record = self._record()
        await self.service.save_schedule_preplan(record)
        raw = await self.service.db_get('interlude_schedule_preplan', {'storyId': 'story'})
        self.assertEqual(len(raw), 1)
        # 列名是上游 camelCase wire format。
        self.assertEqual(raw[0]['validFrom'], '2026-08-30')
        self.assertEqual(raw[0]['lastReviewedLocalDate'], '2026-08-30')
        self.assertEqual(raw[0]['regimes'][0]['id'], 'summer')

        loaded = await self.service.get_schedule_preplan('story')
        self.assertEqual(loaded['revision'], 2)
        self.assertEqual(loaded['valid_from'], '2026-08-30')
        self.assertEqual(loaded['timezone'], 'Asia/Shanghai')

        # 第二次保存必须走 update 且不带主键列（否则检查点永远不前进）。
        await self.service.save_schedule_preplan(self._record(revision=3))
        rows = await self.service.db_get('interlude_schedule_preplan', {'storyId': 'story'})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['revision'], 3)

    async def test_missing_row_yields_none_and_bad_dates_are_rejected(self):
        self.assertIsNone(await self.service.get_schedule_preplan('story'))
        await self.service.db_create('interlude_schedule_preplan', {
            'storyId': 'story', 'revision': 1, 'timezone': 'UTC',
            'validFrom': 'not-a-date', 'validThrough': '2026-09-12',
            'lastReviewedLocalDate': '', 'lastEvidenceEntryId': 0, 'reviewReason': '',
            'regimes': [], 'exceptions': [], 'materializedDays': [],
            'createdAt': NOW, 'updatedAt': NOW,
        })
        # 上游 `normalizeSchedulePreplanRecord`：缺 `from` 键整条记录作废。
        self.assertIsNone(await self.service.get_schedule_preplan('story'))

    async def test_evidence_free_first_review_persists_an_explicit_empty_record(self):
        """上游 `if (!current && !evidenceEntries.length)` 分支（`:6056`）。"""
        config = make_config(schedulePreplan={
            'enabled': True, 'horizonDays': 14, 'reviewAfterLocalHour': 0,
            'anchorAutoAdvance': True, 'variationLevel': 'stable',
            'candidateActivationProbability': .25, 'candidateRevealMinutes': 120,
        })
        service = self.make_service(config)
        service.config['schedulePreplan'] = dict(config['schedulePreplan'])
        service.cached_schedule_preplan_config = None
        await self.insert_story('story')
        story = await self.story_row('story')
        review = await service.prepare_schedule_preplan_review(
            story, datetime(2026, 8, 31, 4, 0, tzinfo=timezone.utc),
        )
        self.assertIsNotNone(review)
        self.assertFalse(review['needs_model'])
        self.assertIsNone(review['request'])
        self.assertEqual(review['local_date'], '2026-08-31')
        self.assertEqual(review['evidence_entries'], [])
        stored = await service.get_schedule_preplan('story')
        self.assertIsNotNone(stored, '空记录必须落库，避免每天都重问模型')
        self.assertEqual(stored['regimes'], [])
        self.assertEqual(stored['last_reviewed_local_date'], '2026-08-31')

    async def test_review_is_once_per_local_day(self):
        """上游 `schedulePreplanReviewDue` 的日切判定在 service 侧同样成立（`:6053`）。"""
        config = make_config(schedulePreplan={
            'enabled': True, 'horizonDays': 14, 'reviewAfterLocalHour': 3,
            'anchorAutoAdvance': True, 'variationLevel': 'stable',
        })
        service = self.make_service(config)
        service.cached_schedule_preplan_config = None
        await self.insert_story('story')
        story = await self.story_row('story')
        first = await service.prepare_schedule_preplan_review(
            story, datetime(2026, 8, 31, 4, 0, tzinfo=timezone.utc),
        )
        self.assertIsNotNone(first)
        same_day = await service.prepare_schedule_preplan_review(
            story, datetime(2026, 8, 31, 6, 0, tzinfo=timezone.utc),
        )
        self.assertIsNone(same_day, '同一个本地日不再复核')
        self.assertEqual(DEFAULT_SCHEDULE_PREPLAN_CONFIG['horizon_days'], 14)


if __name__ == '__main__':
    unittest.main(verbosity=2)
