"""Chunk6（`upstream/src/service.ts:4814-5441`）的单元测试。

覆盖三部分：

1. **上游用例逐条移植**（断言原样保留，仅按 `docs/PORT_PLAN.md` §2 的键名法把
   camelCase 数据转成内部 snake_case；上游 stub 对象改写成 Python 桩类，
   行为契约完全一致）：
   * `upstream/test/delivery-ledger.test.ts` —— 第 2 条（账本读/写失败不得中断
     确认与剩余气泡排期；这条在 `test_delivery_ledger.py` 里曾被 skip，
     现在 `chunk6` 落地后在这里真实执行）；
   * `upstream/test/m10-cooperation.test.ts` —— 承诺结算的 7 条（`settlementHarness`
     逐条对照：全气泡确认才结算、错故事/中断不结算、reschedule 幂等、
     无关立即回复不得静默结清、新承诺只在完整投递后且旧承诺已结清时登记、
     已结清承诺不得被原始投递回执重建、背景摘要等待完整投递且投影失败隔离）；
   * `upstream/test/delivery-boundary.test.ts` / `follow-up-commitment.test.ts` /
     `dialogue-burst.test.ts` 里**没有**断言本范围成员的用例（它们只打
     `delivery.py` / `narrator` / `scene_frame`），已在各自的测试文件里覆盖，
     这里不重复移植。
2. **可独立验证的纯逻辑用例**：`splitOutgoingMessage` / `typingDelayMilliseconds` /
   `autoAdvanceConfig`・`urgeConfig`・`effectiveUrgeRuntime` / `findBotForParticipant` /
   `resolveLiteralQuoteMessageId` / 投递状态聚合（已 delivered 是终态）。
3. **真实平台出站与拆分投递**：`sendOutgoingMessages` 的出站路由与失败记账、
   `deliverDueSplitSegments` 在**真实 sqlite 数据库 + 真实兄弟 mixin** 上的端到端行为。

运行：`python3 -m unittest plugin.tests.test_service_chunk6 -v`
"""

from __future__ import annotations

import asyncio
import copy
import random
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock as unittest_mock
from typing import Any, Optional

from plugin.core import logging as interlude_logging
from plugin.core.database import Database
from plugin.core.delivery import attach_message_event, prepare_outgoing_delivery
from plugin.core.script.commit_builder import (
    decision_to_script_commit,
    find_outgoing_script_event,
)
from plugin.core.script.delivery_ledger import (
    aggregate_delivery_status,
    create_script_delivery_actions,
)
from plugin.core.service import InterludeContext, NullTransport
from plugin.core.service.base import ServiceBase, ServiceChunk0
from plugin.core.service.chunk5 import ServiceChunk5
from plugin.core.service.chunk6 import ServiceChunk6
from plugin.core.service.chunk7 import ServiceChunk7
from plugin.core.service.chunk9 import ServiceChunk9
from plugin.core.time import iso, parse_dt
from plugin.core.turn_persistence import script_entry_draft_for_commit
from plugin.core.types import empty_participant_state, empty_story_setting, empty_story_state

_UTC = timezone.utc

#: 上游 `const now = new Date('2026-09-05T08:00:00Z')`。
NOW = datetime(2026, 9, 5, 8, 0, tzinfo=_UTC)

PROSE = '她发出“想好了”，接着说“选第一个”。'
REPLY_CONTENT = '想好了<sep/>选第一个'


def _commit_fixture() -> dict[str, Any]:
    """上游 `m10-cooperation.test.ts` 的 `settlementHarness` 里的 commit。"""
    return decision_to_script_commit({
        'story_id': 's',
        'participant_id': 'alice',
        'phase': 'intent-due',
        'from': NOW,
        'now': NOW,
        'decision': {
            'script': PROSE,
            'interaction': {'seen': False, 'reply': {'mode': 'immediate', 'content': REPLY_CONTENT}},
        },
    })


def _intent(intent_id: int, type_: str, participant_id: str = 'alice') -> dict[str, Any]:
    """上游 `const intent = (id, type, participantId = 'alice')`。"""
    return {
        'id': intent_id, 'type': type_, 'participantId': participant_id, 'storyId': 's',
        'summary': 'task %d' % intent_id, 'status': 'pending', 'notBefore': NOW,
        'createdAt': NOW, 'updatedAt': NOW, 'payload': {},
    }


def _make_config(**overrides: Any) -> dict[str, Any]:
    """一份最小可用配置（其余走 schema 默认值）。"""
    config: dict[str, Any] = {
        'model': {},
        'runtime': {},
        'storyDefaults': {},
        'logging': {'level': 'debug', 'verbosity': 'diagnostic', 'format': 'layered'},
    }
    config.update(overrides)
    return config


# =========================================================================== #
# 上游用例的门卫：chunk6 未落地 / 未组装时整体 skip（绝不假绿）
# =========================================================================== #

def _chunk6_hooks() -> Optional[type]:
    """取 `plugin.core.service.InterludeService` 上的 chunk6 入口。"""
    try:
        from plugin.core.service import InterludeService
    except Exception:  # pragma: no cover - 组装入口尚未落地
        return None
    required = (
        'confirm_outgoing_deliveries', 'update_script_delivery_outcome',
        'apply_follow_up_resolutions', 'append_follow_up_commitment',
        'defer_unresolved_due_follow_ups', 'record_automatic_delivery',
    )
    if any(not callable(getattr(InterludeService, name, None)) for name in required):
        return None
    return InterludeService


_SERVICE = _chunk6_hooks()
_needs_service = unittest.skipUnless(
    _SERVICE is not None,
    'plugin/core/service 尚未提供 chunk6 的投递回执入口（并行移植期）',
)


class _Sink:
    """把分层日志收进内存，避免测试输出噪音。"""

    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def __call__(self, level: str, text: str) -> None:
        self.records.append((level, text))


class ServiceFixtureMixin:
    """共享夹具：内存日志 sink + 内存数据库 + 记录型 Transport。"""

    def setUp(self) -> None:  # noqa: D102 - 由各个 TestCase 显式转发
        self.sink = _Sink()
        interlude_logging.set_log_sink(self.sink)
        self.addCleanup(interlude_logging.set_log_sink, interlude_logging._default_sink)

    def make_transport(self) -> '_RecordingTransport':
        return _RecordingTransport()

    def make_service(self, config: Any = None, **kwargs: Any) -> ServiceChunk6:
        """构造一个不触碰真实平台的 `ServiceChunk6`（`ctx.bots` / 随机源可注入）。"""
        db = kwargs.pop('db', None)
        bots = kwargs.pop('bots', None)
        transport = kwargs.pop('transport', None)
        ctx_random = kwargs.pop('ctx_random', None)
        if kwargs:
            raise TypeError('未预期的夹具参数：%s' % sorted(kwargs))
        ctx = InterludeContext(
            logger=None, database=db, bots=bots, random=ctx_random,
        )
        return ServiceChunk6(
            ctx,
            config if config is not None else _make_config(),
            db,
            transport if transport is not None else NullTransport(),
        )


class _RecordingTransport(NullTransport):
    """记录出站调用的 `Transport`（默认全部成功）。"""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.ok = True
        self.error = 'adapter exploded'

    async def send_private(self, participant: Any, content: str, reply_to: Any = None) -> dict[str, Any]:
        self.sent.append({'kind': 'private', 'participant': participant, 'content': content,
                          'reply_to': reply_to})
        return {'ok': self.ok, 'error': None if self.ok else self.error}

    async def send_session(self, session: Any, content: str) -> dict[str, Any]:
        self.sent.append({'kind': 'session', 'session': session, 'content': content})
        return {'ok': self.ok, 'error': None if self.ok else self.error}


# =========================================================================== #
# 1. 上游 `delivery-ledger.test.ts` 第 2 条
# =========================================================================== #

class _ConfirmOutgoingStub(ServiceChunk6):
    """上游测试里那个 `service` 字面量对象的等价物（只注入 IO 失败）。

    上游把 `updateScriptDeliveryOutcome` 从原型上取下来挂到字面量对象上；
    Python 里等价的做法是继承 `ServiceChunk6` 并覆盖 IO 方法，让**真实**的
    `update_script_delivery_outcome` / `confirm_outgoing_deliveries` 跑起来。
    """

    def __init__(self, failure: str, commit: dict[str, Any]) -> None:
        self.failure = failure
        self.commit = commit
        self.intents: list[Any] = []
        self.warnings: list[Any] = []
        self.ledger_patches: list[Any] = []
        self.entries: list[Any] = []

    # ---- 时间（上游是全局 `new Date()`） ----
    def now(self) -> datetime:
        return NOW

    # ---- IO ----
    async def serial(self, _story_id: str, task: Any) -> Any:
        result = task()
        return await result if asyncio.iscoroutine(result) else result

    async def get_participant(self, _participant_id: str) -> dict[str, Any]:
        return {'id': 'alice'}

    async def append_entry(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        entry = {'id': 99}
        self.entries.append(entry)
        return entry

    async def db_get(self, table: str, _query: Any, _options: Any = None) -> list[Any]:
        if self.failure == 'read':
            raise RuntimeError('ledger read failed')
        if table != 'interlude_script_entry':
            return []
        return [{
            'id': 42, 'storyId': 'story:1',
            'metadata': {
                'commitId': self.commit['commit_id'],
                'deliveryActions': create_script_delivery_actions(self.commit),
            },
        }]

    async def db_set(self, table: str, _query: Any, data: Any) -> None:
        if table == 'interlude_script_entry':
            self.ledger_patches.append(copy.deepcopy(data))
            if self.failure == 'write':
                raise RuntimeError('ledger write failed')
            return
        raise AssertionError('本用例不应写入其它表：%s' % table)

    def report_standalone(self, *args: Any) -> None:
        self.warnings.append(args)

    async def record_character_message(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def typing_delay_milliseconds(self, _segment: str) -> int:
        return 100

    async def append_intent(self, _story_id: str, intent: Any, *_args: Any, **_kwargs: Any) -> None:
        self.intents.append(intent)

    def schedule_due_intent_wake(self, *_args: Any, **_kwargs: Any) -> None:
        return None


@_needs_service
class DeliveryLedgerChunk6Tests(unittest.TestCase):
    """`upstream/test/delivery-ledger.test.ts` 里断言本范围成员的那一条。"""

    def test_ledger_read_and_write_failures_do_not_interrupt_confirmation_or_remaining_bubble_scheduling(self) -> None:
        # 上游 fixture：同一份 commit → decisionToScriptCommit（含 <sep/> 两个气泡）。
        for failure in ('read', 'write'):
            commit = decision_to_script_commit({
                'story_id': 'story:1',
                'participant_id': 'alice',
                'phase': 'user-message',
                'from': datetime(2026, 9, 5, 10, 0, tzinfo=_UTC),
                'now': datetime(2026, 9, 5, 10, 1, tzinfo=_UTC),
                'message_separator': '<sep/>',
                'split_reply_messages': True,
                'decision': {
                    'script': '她放下杯子，发来“第一句”，紧接着又发来“第二句”。',
                    'interaction': {'seen': True, 'reply': {'mode': 'immediate', 'content': '第一句<sep/>第二句'}},
                    'local_media': {'asset_id': 'sticker:cat', 'willingness': 0.9},
                    'native_face': {'semantic': 'smile', 'willingness': 0.8},
                    'message_reactions': [{'message_ref': 'group-message:7', 'reaction': 'heart'}],
                },
            })
            event = find_outgoing_script_event(commit, 'alice')
            self.assertIsNotNone(event)
            stub = _ConfirmOutgoingStub(failure, commit)
            message = prepare_outgoing_delivery(
                attach_message_event({'participant_id': 'alice', 'content': event['content']},
                                     event, 42),
                event['bubbles'],
            )
            confirmed = asyncio.run(
                ServiceChunk6.confirm_outgoing_deliveries(stub, {'id': 'story:1'}, [message]),
            )
            # 上游：`assert.equal(intents.length, 1)` / `intents[0].payload.content === '第二句'`
            self.assertEqual(len(stub.intents), 1, failure)
            self.assertEqual(stub.intents[0]['payload']['content'], '第二句', failure)
            # 上游：`assert.equal(warnings.length, 1)`
            self.assertEqual(len(stub.warnings), 1, failure)
            # 补充断言（同一条契约的其余部分，全部来自上游源码）：
            self.assertEqual(confirmed, [{'id': 99}], failure)          # 剧本条目照常落库
            self.assertEqual(stub.intents[0]['type'], 'split-message', failure)
            self.assertIs(stub.intents[0]['payload']['visibleMessage'], True, failure)
            self.assertIs(stub.intents[0]['payload']['userInitiated'], False, failure)
            self.assertEqual(
                stub.intents[0]['payload']['script_event']['script_entry_id'], 42, failure,
            )
            if failure == 'write':
                # 账本回写确实被尝试过（且只写到 snake_case 的 delivery_actions 上）。
                self.assertEqual(len(stub.ledger_patches), 1, failure)
                actions = stub.ledger_patches[0]['metadata']['delivery_actions']
                speech = next(item for item in actions if item['event_kind'] == 'outgoing-message')
                self.assertEqual(
                    [segment['status'] for segment in speech['segments']],
                    ['delivered', 'pending'], failure,
                )
            else:
                # 读失败时根本走不到写：M6.1 是观察性的，不影响确认与后续排期。
                self.assertEqual(stub.ledger_patches, [], failure)


# =========================================================================== #
# 2. 上游 `m10-cooperation.test.ts`：承诺结算
# =========================================================================== #

class _SettlementStub(ServiceChunk6):
    """上游 `settlementHarness` 里的 `service` 对象（脚本条目 + 承诺两条数据源）。"""

    def __init__(self, task: dict[str, Any], entry: dict[str, Any]) -> None:
        self.task = task
        self.entry = entry
        self.writes = 0
        self.warnings = 0
        self.registrations: list[dict[str, Any]] = []

    async def db_get(self, table: str, query: Any, _options: Any = None) -> list[Any]:
        if table == 'interlude_script_entry':
            return [copy.deepcopy(self.entry)]
        if (self.task.get('status') == 'pending'
                and query.get('participantId') == self.task.get('participantId')
                and query.get('type') == 'follow-up-commitment'):
            return [copy.deepcopy(self.task)]
        return []

    async def db_set(self, table: str, _query: Any, data: Any) -> None:
        if table == 'interlude_script_entry':
            self.entry.update(copy.deepcopy(data))
            return
        self.task.update(copy.deepcopy(data))
        self.writes += 1

    def schedule_due_intent_wake(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def report_standalone(self, *_args: Any, **_kwargs: Any) -> None:
        self.warnings += 1

    async def get_story(self, _story_id: str) -> dict[str, Any]:
        return {'id': 's'}

    # 上游把 `appendFollowUpCommitment` 换成一个只登记调用的 spy。
    async def append_follow_up_commitment(
        self, _story: Any, _participant_id: str, draft: Any, *_args: Any, **_kwargs: Any,
    ) -> None:
        self.registrations.append({'draft': draft, 'previous_status': self.task.get('status')})


class _SettlementHarness:
    """上游 `settlementHarness(outcome)` 的等价物。"""

    def __init__(self, outcome: str = 'fulfilled') -> None:
        self.task = _intent(11, 'follow-up-commitment')
        commit = _commit_fixture()
        event = find_outgoing_script_event(commit, 'alice')
        if event is None:  # pragma: no cover - fixture 不自洽
            raise AssertionError('fixture 没有 alice 的 outgoing-message 事件')
        event['metadata'] = {'follow_up_resolutions': [{
            'id': 11,
            'outcome': outcome,
            'notBefore': iso(NOW + timedelta(minutes=1)),
        }]}
        entry: dict[str, Any] = {'id': 3, 'storyId': 's', **script_entry_draft_for_commit(commit, None)}
        self.commit = commit
        self.event = event
        self.entry = entry
        self.stub = _SettlementStub(self.task, entry)

    def update(self, segment_index: int, status: str, story_id: str = 's') -> Any:
        return ServiceChunk6.update_script_delivery_outcome(
            self.stub,
            story_id,
            {'commitId': self.commit['commit_id'], 'eventId': self.event['event_id'],
             'scriptEntryId': self.entry['id'], 'segmentIndex': segment_index},
            status,
            NOW,
        )

    def stored_action(self) -> dict[str, Any]:
        actions = self.entry['metadata']['delivery_actions']
        return next(item for item in actions if item['event_id'] == self.event['event_id'])


@_needs_service
class FollowUpSettlementTests(unittest.IsolatedAsyncioTestCase):
    """`upstream/test/m10-cooperation.test.ts` 里断言本范围成员的 6 条。"""

    async def test_promise_settles_after_all_bubbles_are_confirmed_never_after_first_or_failed_delivery(self) -> None:
        h = _SettlementHarness()
        await h.update(0, 'delivered')
        self.assertEqual(h.task['status'], 'pending')
        await h.update(1, 'failed')
        self.assertEqual(h.task['status'], 'pending')
        await h.update(1, 'pending')
        self.assertEqual(h.task['status'], 'pending')
        await h.update(1, 'delivered')
        self.assertEqual(h.task['status'], 'completed')
        self.assertEqual(h.stub.writes, 1)
        await h.update(1, 'delivered')
        self.assertEqual(h.stub.writes, 1)
        self.assertEqual(h.stub.warnings, 0)
        # 全部片段送达后账本行动状态是终态 delivered。
        self.assertEqual(h.stored_action()['status'], 'delivered')
        self.assertEqual(
            aggregate_delivery_status(h.stored_action()['segments']), 'delivered',
        )

    async def test_interrupted_last_bubble_and_wrong_story_callbacks_leave_the_promise_pending(self) -> None:
        h = _SettlementHarness()
        await h.update(0, 'delivered', 'other-story')
        await h.update(1, 'delivered', 'other-story')
        self.assertEqual(h.stub.writes, 0)
        await h.update(0, 'delivered')
        await h.update(1, 'cancelled')
        self.assertEqual(h.task['status'], 'pending')
        self.assertEqual(h.stub.writes, 0)

    async def test_reschedule_is_idempotent_even_if_the_script_settlement_marker_was_lost(self) -> None:
        h = _SettlementHarness('rescheduled')
        await h.update(0, 'delivered')
        await h.update(1, 'delivered')
        self.assertEqual(h.task['payload']['reschedules'], 1)
        del h.entry['metadata']['follow_up_resolution_event_id']
        await h.update(1, 'delivered')
        self.assertEqual(h.task['payload']['reschedules'], 1)
        self.assertEqual(h.stub.writes, 1)

    async def test_new_spoken_promise_is_registered_only_after_full_delivery_and_after_old_resolution(self) -> None:
        h = _SettlementHarness()
        # 上游：`event.metadata.followUpCommitment = {...}`（本移植版内部键为 snake_case，
        # 读取侧双读；这里同时验证 camelCase 旧数据也能被认出来）。
        h.event['metadata']['followUpCommitment'] = {
            'kind': 'checking',
            'summary': '明白了，稍后确认库存',
            'notBefore': iso(NOW + timedelta(minutes=10)),
        }
        await h.update(0, 'delivered')
        self.assertEqual(len(h.stub.registrations), 0)
        await h.update(1, 'failed')
        self.assertEqual(len(h.stub.registrations), 0)
        await h.update(1, 'delivered')
        self.assertEqual(len(h.stub.registrations), 1)
        self.assertEqual(h.stub.registrations[0]['previous_status'], 'completed')
        self.assertEqual(h.stub.registrations[0]['draft']['summary'], '明白了，稍后确认库存')
        await h.update(1, 'delivered')
        self.assertEqual(len(h.stub.registrations), 1)

    async def test_an_unrelated_immediate_reply_cannot_silently_fulfil_an_unresolved_due_promise(self) -> None:
        patches: list[Any] = []
        wakes: list[Any] = []

        class _DeferStub(ServiceChunk6):
            def __init__(self) -> None:
                self.config = _make_config()

            async def db_set(self, _table: str, _query: Any, data: Any) -> None:
                patches.append(copy.deepcopy(data))

            def schedule_due_intent_wake(self, *args: Any) -> None:
                wakes.append(args)

            def report_operation(self, *_args: Any, **_kwargs: Any) -> None:
                return None

            async def get_story(self, _story_id: str) -> dict[str, Any]:
                return {'id': 's'}

        await ServiceChunk6.defer_unresolved_due_follow_ups(
            _DeferStub(), 's', 'alice', [_intent(11, 'follow-up-commitment')], set(),
            {'seen': False, 'reply': {'mode': 'immediate', 'content': '我还在书店'}}, NOW,
        )
        self.assertEqual(len(patches), 1)
        self.assertNotIn('status', patches[0])
        self.assertGreater(patches[0]['notBefore'], NOW)
        # 上游同一契约的其余部分：累计 deferredChecks 并重排一次唤醒。
        self.assertEqual(patches[0]['payload']['deferredChecks'], 1)
        self.assertEqual(len(wakes), 1)

    async def test_a_completed_promise_cannot_be_recreated_by_replaying_its_original_delivery_callback(self) -> None:
        class _ReplayStub(ServiceChunk6):
            def __init__(self) -> None:
                self.writes = 0

            async def db_get(self, *_args: Any, **_kwargs: Any) -> list[Any]:
                completed = {**_intent(1, 'follow-up-commitment'), 'status': 'completed',
                             'payload': {'originDeliveryEventId': 'e1'}}
                return [completed]

            async def append_intent(self, *_args: Any, **_kwargs: Any) -> None:
                self.writes += 1

        stub = _ReplayStub()
        await ServiceChunk6.append_follow_up_commitment(
            stub, {'id': 's'}, 'alice', {'summary': '旧承诺'}, 3, NOW, 'e1',
        )
        self.assertEqual(stub.writes, 0)

    async def test_background_summary_waits_for_the_complete_delivery_and_projection_failure_is_isolated(self) -> None:
        speech = {'participantId': 'alice', 'eventKind': 'outgoing-message', 'status': 'partial'}

        class _AutomaticDeliveryStub(ServiceChunk6):
            def __init__(self) -> None:
                self.writes = 0
                self.warnings = 0
                self.fail_reads = False

            async def db_get(self, *_args: Any, **_kwargs: Any) -> list[Any]:
                if self.fail_reads:
                    raise RuntimeError('read unavailable')
                return [{'metadata': {'deliveryActions': [speech]}}]

            async def get_story(self, _story_id: str) -> dict[str, Any]:
                return {'id': 's', 'state': empty_story_state()}

            async def db_set(self, *_args: Any, **_kwargs: Any) -> None:
                self.writes += 1

            def report_standalone(self, *_args: Any, **_kwargs: Any) -> None:
                self.warnings += 1

        stub = _AutomaticDeliveryStub()

        async def call() -> None:
            await ServiceChunk6.record_automatic_delivery(
                stub, 's', 'alice', {'sourceEntryId': 3, 'summary': '已经问过对方'}, NOW,
            )

        await call()
        self.assertEqual(stub.writes, 0)
        speech['status'] = 'delivered'
        await call()
        self.assertEqual(stub.writes, 1)
        stub.fail_reads = True
        await call()  # 绝不抛出：摘要只是投影
        self.assertEqual(stub.warnings, 1)


# =========================================================================== #
# 3. 可独立验证的纯逻辑用例
# =========================================================================== #

class PureLogicFixture(ServiceFixtureMixin, unittest.TestCase):
    """同步纯逻辑用例的基类。"""


class SplitOutgoingMessageTests(PureLogicFixture):
    """上游 `splitOutgoingMessage`（`:5372`）。"""

    def test_disabled_splitting_returns_the_whole_content(self) -> None:
        service = self.make_service(_make_config(runtime={'splitReplyMessages': False}))
        self.assertEqual(service.split_outgoing_message('第一句<sep/>第二句'), ['第一句<sep/>第二句'])

    def test_default_separator_splits_and_trims_and_drops_empties(self) -> None:
        service = self.make_service(_make_config(runtime={}))
        self.assertEqual(
            service.split_outgoing_message('第一句<sep/> 第二句 <sep/><sep/>第三句'),
            ['第一句', '第二句', '第三句'],
        )

    def test_custom_separator_and_absence(self) -> None:
        service = self.make_service(_make_config(runtime={'messageSeparator': '||'}))
        self.assertEqual(service.split_outgoing_message('a||b'), ['a', 'b'])
        self.assertEqual(service.split_outgoing_message('a<sep/>b'), ['a<sep/>b'])

    def test_blank_separator_falls_back_to_the_default(self) -> None:
        service = self.make_service(_make_config(runtime={'messageSeparator': '   '}))
        self.assertEqual(service.split_outgoing_message('a<sep/>b'), ['a', 'b'])

    def test_all_separators_yields_fallback_single_part(self) -> None:
        # 上游会把空段过滤掉；全部是分隔符时得到空列表（调用方按"无内容"处理）。
        service = self.make_service(_make_config(runtime={}))
        self.assertEqual(service.split_outgoing_message('<sep/>'), [])


class TypingDelayMillisecondsTests(PureLogicFixture):
    """上游 `typingDelayMilliseconds`（`:5379`）：`[250, 最大延迟]` 且带抖动。"""

    def test_nominal_value_without_jitter(self) -> None:
        service = self.make_service(_make_config(runtime={
            'typingBaseDelaySeconds': 1, 'typingCharactersPerSecond': 8,
            'typingMaxDelaySeconds': 12, 'typingJitterRatio': 0,
        }))
        # 1s + ceil(8 / 8) = 2s
        self.assertEqual(service.typing_delay_milliseconds('一二三四五六七八'), 2000)
        # 1s + ceil(0 / 8) = 1s
        self.assertEqual(service.typing_delay_milliseconds(''), 1000)

    def test_clamped_between_floor_and_maximum(self) -> None:
        service = self.make_service(_make_config(runtime={
            'typingBaseDelaySeconds': 0, 'typingCharactersPerSecond': 8,
            'typingMaxDelaySeconds': 12, 'typingJitterRatio': 0,
        }))
        self.assertEqual(service.typing_delay_milliseconds(''), 250)
        self.assertEqual(service.typing_delay_milliseconds('字' * 5000), 12000)

    def test_maximum_never_below_base(self) -> None:
        # `maximumSeconds = max(baseSeconds, typingMaxDelaySeconds)`：base 更大时以 base 为准，
        # 且 `nominal = min(maximumSeconds, ...)` 保证不会被 base 顶破上限。
        service = self.make_service(_make_config(runtime={
            'typingBaseDelaySeconds': 20, 'typingCharactersPerSecond': 8,
            'typingMaxDelaySeconds': 12, 'typingJitterRatio': 0,
        }))
        self.assertEqual(service.typing_delay_milliseconds('一二三四五六七八'), 20000)
        self.assertEqual(service.typing_delay_milliseconds('字' * 5000), 20000)

    def test_jitter_stays_inside_the_configured_ratio(self) -> None:
        # 上游 `Math.random()` → 模块级 `random.random()`（`docs/PORT_PLAN.md` §2），
        # 因此照上游测试的做法 mock 全局随机源。
        for ratio in (0.1, 0.3, 0.5):
            for value in (0.0, 0.25, 0.5, 0.75, 0.999):
                service = self.make_service(_make_config(runtime={
                    'typingBaseDelaySeconds': 2, 'typingCharactersPerSecond': 8,
                    'typingMaxDelaySeconds': 20, 'typingJitterRatio': ratio,
                }))
                with unittest_mock.patch.object(random, 'random', return_value=value):
                    delay = service.typing_delay_milliseconds('一二三四五六七八')
                # nominal = 2 + 1 = 3s；factor ∈ [1-ratio, 1+ratio]
                self.assertGreaterEqual(delay, int(3000 * (1 - ratio)) - 1)
                self.assertLessEqual(delay, int(3000 * (1 + ratio)) + 1)

    def test_garbage_ratio_behaves_like_no_jitter(self) -> None:
        service = self.make_service(_make_config(runtime={
            'typingBaseDelaySeconds': 1, 'typingCharactersPerSecond': 8,
            'typingMaxDelaySeconds': 12, 'typingJitterRatio': 'nope',
        }))
        self.assertEqual(service.typing_delay_milliseconds('一二三四五六七八'), 2000)


class ConfigGetterTests(PureLogicFixture):
    """`autoAdvanceConfig` / `urgeConfig` / `effectiveUrgeRuntime`（`:5395`/`:5411`/`:5413`）。"""

    def test_auto_advance_defaults(self) -> None:
        service = self.make_service(_make_config(runtime={}))
        config = service.auto_advance_config
        self.assertIs(config['enabled'], True)
        self.assertEqual(config['interval_minutes'], 40)
        self.assertEqual(config['jitter_minutes'], 5)
        self.assertEqual(config['follow_up_minutes'], [10, 20])
        self.assertEqual(config['follow_up_jitter_minutes'], 1)
        self.assertEqual(config['rest_windows'], [{
            'enabled': True, 'label': 'night sleep', 'start': '23:00', 'end': '07:00',
            'min_interval_minutes': 120, 'max_interval_minutes': 240,
        }])

    def test_auto_advance_overrides_and_clamps(self) -> None:
        service = self.make_service(_make_config(runtime={
            'autoAdvanceEnabled': False,
            'autoAdvanceIntervalMinutes': 0,
            'autoAdvanceJitterMinutes': -3,
            'conversationFollowUpMinutes': [5, 300, 5, 20],
            'conversationFollowUpJitterMinutes': 99,
            'restWindows': [],
        }))
        config = service.auto_advance_config
        self.assertIs(config['enabled'], False)
        self.assertEqual(config['interval_minutes'], 1)
        self.assertEqual(config['jitter_minutes'], 0)
        self.assertEqual(config['follow_up_minutes'], [5, 20])
        self.assertEqual(config['follow_up_jitter_minutes'], 10)
        # 上游 `runtime.restWindows ?? [默认]`：显式空数组**不会**回落默认窗口。
        self.assertEqual(config['rest_windows'], [])

    def test_auto_advance_is_cached_on_first_read(self) -> None:
        service = self.make_service(_make_config(runtime={'autoAdvanceIntervalMinutes': 7}))
        self.assertIsNone(service.cached_auto_advance_config)
        self.assertEqual(service.auto_advance_config['interval_minutes'], 7)
        self.assertIsNotNone(service.cached_auto_advance_config)
        service.config['runtime']['autoAdvanceIntervalMinutes'] = 99
        self.assertEqual(service.auto_advance_config['interval_minutes'], 7)

    def test_urge_config_resolves_frequency_and_is_not_cached(self) -> None:
        service = self.make_service(_make_config(urge={'enabled': True, 'frequency': 'high'}))
        config = service.urge_config
        self.assertIs(config['enabled'], True)
        self.assertEqual(config['frequency'], 'high')
        self.assertEqual(config['budget'], 3)
        service.config['urge']['enabled'] = False
        self.assertIs(service.urge_config['enabled'], False)

    def test_effective_urge_runtime_only_overrides_when_enabled(self) -> None:
        service = self.make_service(_make_config(
            runtime={'proactiveWillingnessThreshold': 0.9}, urge={'enabled': False},
        ))
        self.assertEqual(service.effective_urge_runtime['proactiveWillingnessThreshold'], 0.9)
        service = self.make_service(_make_config(
            runtime={}, urge={'enabled': True, 'proactiveWillingnessThreshold': 0.25},
        ))
        runtime = service.effective_urge_runtime
        # 上游 `{...this.config.runtime, proactiveWillingnessThreshold}`；本移植版配置层
        # （`config.py` 的 runtime 默认值 + 已落地的契约测试）用 snake_case，消费者一律
        # `_cfg()` 双读，故这里写 snake_case。
        self.assertEqual(runtime['proactive_willingness_threshold'], 0.25)
        self.assertEqual(runtime, {**service.runtime_config,
                                   'proactive_willingness_threshold': 0.25})


class FindBotForParticipantTests(PureLogicFixture):
    """上游 `findBotForParticipant`（`:5389`）。"""

    def test_matches_self_id_and_platform(self) -> None:
        bots = [{'selfId': 'bot-1', 'platform': 'telegram'}]
        service = self.make_service(bots=lambda: bots)
        self.assertIs(
            service.find_bot_for_participant({'selfId': 'bot-1', 'platform': 'telegram'}),
            bots[0],
        )
        self.assertIsNone(
            service.find_bot_for_participant({'selfId': 'bot-2', 'platform': 'telegram'}),
        )
        self.assertIsNone(
            service.find_bot_for_participant({'selfId': 'bot-1', 'platform': 'discord'}),
        )

    def test_onebot_family_counts_as_the_same_platform(self) -> None:
        class _Bot:
            selfId = 'bot-1'
            platform = 'onebot:123'

        bots = [_Bot()]
        service = self.make_service(bots=lambda: bots)
        self.assertIs(service.find_bot_for_participant({'selfId': 'bot-1', 'platform': 'napcat'}), bots[0])
        self.assertIsNone(service.find_bot_for_participant({'selfId': 'bot-1', 'platform': 'telegram'}))

    def test_no_bots_returns_none(self) -> None:
        service = self.make_service(bots=lambda: [])
        self.assertIsNone(service.find_bot_for_participant({'selfId': 'bot-1', 'platform': 'telegram'}))


class _QuoteStub(ServiceChunk6):
    """`resolveLiteralQuoteMessageId` 的数据源桩：只提供剧本条目读取。"""

    def __init__(self, entries: list[Any]) -> None:
        self.entries = entries
        self.reads = 0

    async def db_get(self, table: str, _query: Any, _options: Any = None) -> list[Any]:
        self.reads += 1
        if table != 'interlude_script_entry':
            return []
        return self.entries


class ResolveLiteralQuoteMessageIdTests(unittest.IsolatedAsyncioTestCase):
    """上游 `resolveLiteralQuoteMessageId`（`:5313`）与 `targetableMessageId`（`:7300`）。"""

    def _entry(self, content: str, message_id: Any) -> dict[str, Any]:
        return {'id': 7, 'content': content, 'metadata': {'messageId': message_id}}

    async def test_resolves_the_exact_quoted_entry(self) -> None:
        stub = _QuoteStub([
            self._entry('别的话', '111'),
            self._entry('早上好', '222'),
            self._entry('早上好', '333'),
        ])
        self.assertEqual(
            await ServiceChunk6.resolve_literal_quote_message_id(stub, 's', 'alice', '「引用：早上好」'),
            '222',
        )
        self.assertEqual(
            await ServiceChunk6.resolve_literal_quote_message_id(stub, 's', 'alice', '[引用：早上好]'),
            '222',
        )

    async def test_requires_a_targetable_platform_message_id(self) -> None:
        for message_id in ('0', 'abc', '', None, 0):
            stub = _QuoteStub([self._entry('早上好', message_id)])
            self.assertIsNone(
                await ServiceChunk6.resolve_literal_quote_message_id(stub, 's', 'alice', '「引用：早上好」'),
            )

    async def test_no_match_or_not_a_literal_quote(self) -> None:
        stub = _QuoteStub([self._entry('晚上好', '222')])
        self.assertIsNone(
            await ServiceChunk6.resolve_literal_quote_message_id(stub, 's', 'alice', '「引用：早上好」'),
        )
        self.assertIsNone(
            await ServiceChunk6.resolve_literal_quote_message_id(stub, 's', 'alice', '普通消息'),
        )
        # 非引用文本绝不读库。
        self.assertEqual(stub.reads, 1)

    async def test_snake_case_platform_message_id_is_accepted(self) -> None:
        stub = _QuoteStub([{'id': 7, 'content': '早上好', 'metadata': {'message_id': '444'}}])
        self.assertEqual(
            await ServiceChunk6.resolve_literal_quote_message_id(stub, 's', 'alice', '「引用：早上好」'),
            '444',
        )


class LedgerTerminalStateTests(unittest.IsolatedAsyncioTestCase):
    """投递状态聚合：已 `delivered` 是终态，晚到的 `failed`/`pending` 不得降级。"""

    def setUp(self) -> None:
        self.commit = _commit_fixture()
        self.event = find_outgoing_script_event(self.commit, 'alice')
        self.entry: dict[str, Any] = {
            'id': 42, 'storyId': 's', **script_entry_draft_for_commit(self.commit, None),
        }
        # 去掉剧本事件，让本用例只断言账本（承诺结算另有专测）。
        self.entry['metadata']['script_events'] = []

    def _stub(self) -> ServiceChunk6:
        entry = self.entry

        class _Stub(ServiceChunk6):
            writes: list[Any] = []

            def __init__(self) -> None:
                self.config = _make_config()

            async def db_get(self, table: str, _query: Any, _options: Any = None) -> list[Any]:
                return [copy.deepcopy(entry)] if table == 'interlude_script_entry' else []

            async def db_set(self, table: str, _query: Any, data: Any) -> None:
                self.writes.append(copy.deepcopy(data))
                if table == 'interlude_script_entry':
                    entry.update(copy.deepcopy(data))

            def report_standalone(self, *_args: Any, **_kwargs: Any) -> None:
                return None

        _Stub.writes = []
        return _Stub()

    def _update(self, stub: ServiceChunk6, index: int, status: str) -> Any:
        return ServiceChunk6.update_script_delivery_outcome(
            stub, 's',
            {'commit_id': self.commit['commit_id'], 'event_id': self.event['event_id'],
             'script_entry_id': 42, 'bubble_index': index},
            status, NOW,
        )

    async def test_delivered_is_terminal_and_late_failures_do_not_downgrade(self) -> None:
        stub = self._stub()
        await self._update(stub, 0, 'delivered')
        await self._update(stub, 1, 'delivered')
        self.assertEqual(len(stub.writes), 2)
        action = next(item for item in self.entry['metadata']['delivery_actions']
                      if item['event_id'] == self.event['event_id'])
        self.assertEqual(action['status'], 'delivered')

        # 晚到的失败/回退记账：`update_script_delivery_actions` 返回 None → 不写库。
        await self._update(stub, 0, 'failed')
        await self._update(stub, 0, 'pending')
        self.assertEqual(len(stub.writes), 2)
        action = next(item for item in self.entry['metadata']['delivery_actions']
                      if item['event_id'] == self.event['event_id'])
        self.assertEqual(action['status'], 'delivered')
        self.assertEqual([segment['status'] for segment in action['segments']],
                         ['delivered', 'delivered'])

    async def test_unknown_entry_or_foreign_commit_is_ignored(self) -> None:
        stub = self._stub()
        await ServiceChunk6.update_script_delivery_outcome(
            stub, 's',
            {'commit_id': 'other', 'event_id': 'x', 'script_entry_id': 42, 'segment_index': 0},
            'delivered', NOW,
        )
        self.assertEqual(stub.writes, [])
        # 非安全整数 / 缺 scriptEntryId 一律早退（上游 `isSafeInteger` 判定）。
        await ServiceChunk6.update_script_delivery_outcome(
            stub, 's', {'commit_id': 'x', 'event_id': 'y'}, 'delivered', NOW,
        )
        self.assertEqual(stub.writes, [])


# =========================================================================== #
# 4. 真实平台出站：sendOutgoingMessages
# =========================================================================== #

class _SendStub(ServiceChunk6, ServiceChunk0):
    """只注入参与者表与失败记账，其余（引用解析、白名单判定）跑真实实现。"""

    def __init__(self, participants: list[Any], entries: Optional[list[Any]] = None) -> None:
        self.participants = {item['id']: item for item in participants}
        self.entries = entries or []
        self.failures: list[tuple[str, str]] = []
        self.reports: list[tuple[str, str]] = []
        self.config = _make_config()
        self.ctx = InterludeContext(logger=None)
        self.transport = NullTransport()
        self.desktop_delivery_handler = None
        self.interrupted_typing_participants: set[str] = set()

    def now(self) -> datetime:
        return NOW

    async def get_participant(self, participant_id: str) -> Optional[dict[str, Any]]:
        return self.participants.get(participant_id)

    async def db_get(self, table: str, _query: Any, _options: Any = None) -> list[Any]:
        return self.entries if table == 'interlude_script_entry' else []

    async def record_outgoing_delivery_failure(
        self, _story: Any, participant_id: str, _message: Any, reason: str,
    ) -> None:
        self.failures.append((participant_id, reason))

    def report(self, level: str, _story: Any, phase: str, message: str, *args: Any) -> None:
        self.reports.append((level, message % args if args else message))

    def report_operation(
        self, _verbosity: str, level: str, _story: Any, phase: str, message: str, *args: Any,
    ) -> None:
        self.reports.append((level, message % args if args else message))


class SendOutgoingMessagesTests(unittest.IsolatedAsyncioTestCase):
    """上游 `sendOutgoingMessages`（`:5107`）：出站路由 + 失败记账 + 打断闸门。"""

    STORY = {'id': 'story:1'}
    PARTICIPANT = {
        'id': 'p1', 'storyId': 'story:1', 'platform': 'telegram', 'selfId': 'bot',
        'userId': 'u1', 'channelId': 'c1', 'personId': 'u1', 'displayName': 'Alice',
        'profile': '', 'relationship': '', 'state': empty_participant_state(),
        'status': 'active',
    }

    def _message(self, content: str = '你好', **extra: Any) -> dict[str, Any]:
        return {'participant_id': 'p1', 'content': content, **extra}

    async def test_delivers_through_transport_and_returns_the_message(self) -> None:
        transport = _RecordingTransport()
        stub = _SendStub([dict(self.PARTICIPANT)])
        stub.transport = transport
        message = self._message()
        delivered = await ServiceChunk6.send_outgoing_messages(stub, self.STORY, [message])
        self.assertEqual(delivered, [message])
        self.assertEqual(len(transport.sent), 1)
        self.assertEqual(transport.sent[0]['kind'], 'private')
        self.assertEqual(transport.sent[0]['content'], '你好')
        self.assertIsNone(transport.sent[0]['reply_to'])
        self.assertEqual(stub.failures, [])

    async def test_empty_message_list_short_circuits(self) -> None:
        transport = _RecordingTransport()
        stub = _SendStub([dict(self.PARTICIPANT)])
        stub.transport = transport
        self.assertEqual(await ServiceChunk6.send_outgoing_messages(stub, self.STORY, []), [])
        self.assertEqual(transport.sent, [])

    async def test_transport_failure_is_recorded_and_not_delivered(self) -> None:
        transport = _RecordingTransport()
        transport.ok = False
        stub = _SendStub([dict(self.PARTICIPANT)])
        stub.transport = transport
        delivered = await ServiceChunk6.send_outgoing_messages(stub, self.STORY, [self._message()])
        self.assertEqual(delivered, [])
        self.assertEqual(stub.failures, [('p1', 'transport-error: adapter exploded')])

    async def test_record_failures_false_leaves_no_ledger_trace(self) -> None:
        transport = _RecordingTransport()
        transport.ok = False
        stub = _SendStub([dict(self.PARTICIPANT)])
        stub.transport = transport
        delivered = await ServiceChunk6.send_outgoing_messages(
            stub, self.STORY, [self._message()], None, None, None, False,
        )
        self.assertEqual(delivered, [])
        self.assertEqual(stub.failures, [])

    async def test_missing_placeholder_and_blocked_participant(self) -> None:
        stub = _SendStub([])  # 参与者不存在
        stub.transport = _RecordingTransport()
        await ServiceChunk6.send_outgoing_messages(stub, self.STORY, [self._message()])
        self.assertEqual(stub.failures, [('p1', 'participant-not-found')])

        blocked = _SendStub([{**self.PARTICIPANT, 'platform': 'onebot', 'selfId': 'bot'}])
        blocked.transport = _RecordingTransport()
        blocked.config = _make_config(onebot={
            'enabled': True,
            'botAccounts': [{'qq': 'bot'}],
            'userAccounts': [{'qq': 'someone-else'}],
        })
        await ServiceChunk6.send_outgoing_messages(blocked, self.STORY, [self._message()])
        self.assertEqual(blocked.failures, [('p1', 'participant-not-allowed')])
        self.assertEqual(blocked.transport.sent, [])

    async def test_should_cancel_stops_remaining_segments_without_recording_failures(self) -> None:
        transport = _RecordingTransport()
        stub = _SendStub([dict(self.PARTICIPANT)])
        stub.transport = transport
        delivered = await ServiceChunk6.send_outgoing_messages(
            stub, self.STORY, [self._message()], None, None, lambda _target: True,
        )
        self.assertEqual(delivered, [])
        self.assertEqual(transport.sent, [])
        self.assertEqual(stub.failures, [])

    async def test_literal_quote_only_text_is_blocked_without_a_target(self) -> None:
        transport = _RecordingTransport()
        stub = _SendStub([dict(self.PARTICIPANT)])
        stub.transport = transport
        delivered = await ServiceChunk6.send_outgoing_messages(
            stub, self.STORY, [self._message('「引用：不存在的一句话」')],
        )
        self.assertEqual(delivered, [])
        self.assertEqual(transport.sent, [])
        self.assertEqual(stub.failures, [('p1', 'literal-quote-target-not-found')])

    async def test_resolved_literal_quote_sends_zero_width_placeholder_with_reply_to(self) -> None:
        transport = _RecordingTransport()
        stub = _SendStub(
            [dict(self.PARTICIPANT)],
            entries=[{'id': 7, 'content': '别的话', 'metadata': {'messageId': '222'}}],
        )
        stub.transport = transport
        delivered = await ServiceChunk6.send_outgoing_messages(
            stub, self.STORY, [self._message('「引用：别的话」')],
        )
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0]['quote_message_id'], '222')
        # 上游把可见正文换成 `h('quote') + '\u200b'`：正文为零宽占位、引用走 reply_to。
        self.assertEqual(transport.sent[0]['content'], '\u200b')
        self.assertEqual(transport.sent[0]['reply_to'], '222')

    async def test_session_path_reuses_the_incoming_session(self) -> None:
        transport = _RecordingTransport()
        stub = _SendStub([dict(self.PARTICIPANT)])
        stub.transport = transport
        session = object()
        delivered = await ServiceChunk6.send_outgoing_messages(
            stub, self.STORY, [self._message('原路回复')], dict(self.PARTICIPANT), session,
        )
        self.assertEqual(len(delivered), 1)
        self.assertEqual(transport.sent[0]['kind'], 'session')
        self.assertIs(transport.sent[0]['session'], session)
        self.assertEqual(transport.sent[0]['content'], '原路回复')

    async def test_desktop_delivery_handler_takes_precedence_over_transport(self) -> None:
        transport = _RecordingTransport()
        stub = _SendStub([dict(self.PARTICIPANT)])
        stub.transport = transport
        seen: list[Any] = []

        async def handler(delivery: Any) -> dict[str, Any]:
            seen.append(delivery)
            return {'ok': True}

        stub.desktop_delivery_handler = handler
        delivered = await ServiceChunk6.send_outgoing_messages(stub, self.STORY, [self._message('后台')])
        self.assertEqual(len(delivered), 1)
        self.assertEqual(transport.sent, [])
        self.assertEqual(seen[0]['participantId'], 'p1')
        self.assertEqual(seen[0]['channelId'], 'c1')
        self.assertEqual(seen[0]['kind'], 'private')
        self.assertEqual(seen[0]['content'], '后台')

        async def failing_handler(_delivery: Any) -> dict[str, Any]:
            return {'ok': False, 'error': '宿主拒绝'}

        stub.desktop_delivery_handler = failing_handler
        delivered = await ServiceChunk6.send_outgoing_messages(stub, self.STORY, [self._message('后台')])
        self.assertEqual(delivered, [])
        self.assertEqual(stub.failures[-1], ('p1', 'transport-error: 宿主拒绝'))

    async def test_bot_not_found_when_neither_bot_nor_transport_exists(self) -> None:
        stub = _SendStub([dict(self.PARTICIPANT)])
        stub.transport = None
        await ServiceChunk6.send_outgoing_messages(stub, self.STORY, [self._message()])
        self.assertEqual(stub.failures, [('p1', 'bot-not-found')])


# =========================================================================== #
# 5. 真实数据库上的 deliverDueSplitSegments
# =========================================================================== #

class _DbHarness(ServiceChunk6, ServiceChunk5, ServiceChunk7, ServiceChunk9, ServiceChunk0):
    """把本范围之外的兄弟 chunk 一起混入，让集成用例跑**真实实现**（无测试替身）。

    显式调用 `ServiceBase.__init__`：`ServiceChunk0.__init__` 会注册后台计时器
    （心跳/sweep/表情扫描），集成用例不需要它们。
    """

    def __init__(self, ctx: Any, config: Any, db: Any = None, transport: Any = None) -> None:
        ServiceBase.__init__(self, ctx, config, db, transport)


class DeliverDueSplitSegmentsTests(ServiceFixtureMixin, unittest.IsolatedAsyncioTestCase):
    """上游 `deliverDueSplitSegments`（`:4814`）：不经过主叙事的拆分投递。"""

    def setUp(self) -> None:
        ServiceFixtureMixin.setUp(self)
        self.db = Database(':memory:')
        self.addCleanup(self.db.close)
        self.db.register_tables()
        self.transport = self.make_transport()
        self.service = self._make_service()

    def _make_service(self, participant_status: str = 'active',
                      automatic_delivery: Optional[dict[str, Any]] = None) -> _DbHarness:
        ctx = InterludeContext(logger=None, database=self.db, clock=lambda: NOW)
        service = _DbHarness(
            ctx,
            _make_config(runtime={'maxMessageCharacters': 3000}),
            self.db,
            self.transport,
        )
        # 上游测试用 `new Date()`；这里注入固定时钟，使"到期/未到期"的判定可复现。
        self.db.remove('interlude_story', {'id': 'story:1'})
        self.db.remove('interlude_participant', {'storyId': 'story:1'})
        self.db.remove('interlude_intent', {'storyId': 'story:1'})
        self.db.remove('interlude_script_entry', {'storyId': 'story:1'})
        self.db.insert('interlude_story', {
            'id': 'story:1', 'platform': 'telegram', 'selfId': 'bot', 'userId': '',
            'channelId': '', 'status': 'active', 'setting': empty_story_setting(),
            'state': empty_story_state(), 'cursorAt': NOW, 'createdAt': NOW, 'updatedAt': NOW,
        })
        self.db.insert('interlude_participant', {
            'id': 'p1', 'storyId': 'story:1', 'platform': 'telegram', 'selfId': 'bot',
            'userId': 'u1', 'channelId': 'c1', 'personId': 'u1', 'displayName': 'Alice',
            'profile': '', 'relationship': '', 'state': empty_participant_state(),
            'status': participant_status, 'createdAt': NOW, 'updatedAt': NOW,
        })
        payload: dict[str, Any] = {'content': '第二句', 'visibleMessage': True}
        if automatic_delivery:
            payload['automaticDelivery'] = automatic_delivery
        self.db.insert('interlude_intent', {
            'storyId': 'story:1', 'participantId': 'p1', 'type': 'split-message',
            'summary': '分段', 'notBefore': NOW - timedelta(seconds=1), 'status': 'pending',
            'payload': payload, 'createdAt': NOW, 'updatedAt': NOW,
        })
        return service

    async def test_due_segment_is_delivered_recorded_and_completed(self) -> None:
        await self.service.deliver_due_split_segments('story:1')
        # 平台侧真的发出去了（正文按上限裁剪后的原文）。
        self.assertEqual(len(self.transport.sent), 1)
        self.assertEqual(self.transport.sent[0]['content'], '第二句')
        # 意图结清。
        intents = self.db.all('interlude_intent', {'storyId': 'story:1'})
        self.assertEqual([item['status'] for item in intents], ['completed'])
        # 剧本条目带 splitSegment 标记与参与者归属。
        entries = self.db.all('interlude_script_entry', {'storyId': 'story:1'})
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['kind'], 'character-message')
        self.assertEqual(entries[0]['actor'], 'character')
        self.assertEqual(entries[0]['content'], '第二句')
        self.assertEqual(entries[0]['participantId'], 'p1')
        self.assertIs(entries[0]['metadata']['splitSegment'], True)
        # 参与者的角色发言计数被刷新（chunk7 的真实实现）。
        participant = self.db.get('interlude_participant', {'id': 'p1'})
        self.assertIsNotNone(participant['state'].get('lastCharacterMessageAt'))

    async def test_unavailable_target_cancels_the_intent_and_writes_no_entry(self) -> None:
        self.service = self._make_service(participant_status='paused')
        await self.service.deliver_due_split_segments('story:1')
        self.assertEqual(self.transport.sent, [])
        intents = self.db.all('interlude_intent', {'storyId': 'story:1'})
        self.assertEqual([item['status'] for item in intents], ['cancelled'])
        self.assertEqual(self.db.all('interlude_script_entry', {'storyId': 'story:1'}), [])

    async def test_automatic_delivery_summary_is_projected_into_story_state(self) -> None:
        self.service = self._make_service(
            automatic_delivery={'summary': '已经问过对方', 'sourceEntryId': 3},
        )
        await self.service.deliver_due_split_segments('story:1')
        story = self.db.get('interlude_story', {'id': 'story:1'})
        summaries = story['state']['automatic_delivery_summaries']
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]['participant_id'], 'p1')
        self.assertEqual(summaries[0]['summary'], '已经问过对方')
        self.assertEqual(summaries[0]['source_entry_id'], 3)

    async def test_interrupted_typing_participant_leaves_every_segment_pending(self) -> None:
        self.service.interrupted_typing_participants.add('p1')
        await self.service.deliver_due_split_segments('story:1')
        self.assertEqual(self.transport.sent, [])
        intents = self.db.all('interlude_intent', {'storyId': 'story:1'})
        self.assertEqual([item['status'] for item in intents], ['pending'])

    async def test_future_segment_is_left_alone(self) -> None:
        self.db.update('interlude_intent', {'storyId': 'story:1'},
                       {'notBefore': NOW + timedelta(hours=1)})
        await self.service.deliver_due_split_segments('story:1')
        self.assertEqual(self.transport.sent, [])
        intents = self.db.all('interlude_intent', {'storyId': 'story:1'})
        self.assertEqual([item['status'] for item in intents], ['pending'])

    async def test_overdue_backlog_restores_a_fresh_typing_interval(self) -> None:
        self.db.insert('interlude_intent', {
            'storyId': 'story:1', 'participantId': 'p1', 'type': 'split-message',
            'summary': '分段 2', 'notBefore': NOW - timedelta(seconds=1), 'status': 'pending',
            'payload': {'content': '第三句', 'visibleMessage': True},
            'createdAt': NOW, 'updatedAt': NOW,
        })
        await self.service.deliver_due_split_segments('story:1')
        intents = sorted(self.db.all('interlude_intent', {'storyId': 'story:1'}),
                         key=lambda item: item['id'])
        self.assertEqual(intents[0]['status'], 'completed')
        self.assertEqual(intents[1]['status'], 'pending')
        # 第二段被推到"现在 + 模拟打字时长"，而不是立刻倒出（上游注释的语义）。
        self.assertGreater(parse_dt(intents[1]['notBefore']), NOW)


# =========================================================================== #
# 6. cancelPendingOutgoingMessages / sendScheduledMessages
# =========================================================================== #

class _CancelStub(ServiceChunk6):
    """只提供意图查询与剧本追加的桩，验证取消语义与打断集合的清理。"""

    def __init__(self, intents: list[Any]) -> None:
        self.intents = intents
        self.sets: list[Any] = []
        self.entries: list[Any] = []
        self.ledger: list[Any] = []
        self.interrupted_typing_participants = {'p1'}
        self.due_intent_wake_timers: dict[str, Any] = {}
        self.split_wakes = 0
        self.config = _make_config()

    def now(self) -> datetime:
        return NOW

    async def db_get(self, table: str, _query: Any, _options: Any = None) -> list[Any]:
        return list(self.intents) if table == 'interlude_intent' else []

    async def db_set(self, table: str, query: Any, data: Any) -> None:
        self.sets.append((table, query, data))
        for intent in self.intents:
            if intent['id'] == query.get('id'):
                intent.update(data)

    async def append_entry(self, story_id: str, entry: Any, _now: Any, participant_id: str = '') -> Any:
        self.entries.append((story_id, entry, participant_id))
        return {'id': 1}

    async def update_script_delivery_outcome(
        self, story_id: str, reference: Any, status: str, at: Any, reason: Any = None,
    ) -> None:
        self.ledger.append((story_id, reference, status, reason))

    async def schedule_next_split_wake(self, story_id: str) -> None:
        self.split_wakes += 1


class CancelPendingOutgoingMessagesTests(unittest.IsolatedAsyncioTestCase):
    """上游 `cancelPendingOutgoingMessages`（`:5046`）。"""

    def _intents(self) -> list[dict[str, Any]]:
        return [
            {'id': 1, 'storyId': 's', 'participantId': 'p1', 'type': 'split-message',
             'status': 'pending', 'payload': {'content': '第一段'}},
            {'id': 2, 'storyId': 's', 'participantId': 'p1', 'type': 'delayed-reply',
             'status': 'pending', 'payload': {'content': '旧计划'}},
            {'id': 3, 'storyId': 's', 'participantId': 'p1', 'type': 'follow-up-commitment',
             'status': 'pending', 'payload': {'content': '承诺'}},
            {'id': 4, 'storyId': 's', 'participantId': 'p2', 'type': 'split-message',
             'status': 'pending', 'payload': {'content': '别人的'}},
        ]

    async def test_cancels_split_and_planned_messages_and_clears_the_interrupt_flag(self) -> None:
        stub = _CancelStub(self._intents())
        stub.due_intent_wake_timers['s'] = NullTransport()  # 无 cancel 的哑对象
        matching = await ServiceChunk6.cancel_pending_outgoing_messages(stub, 's', 'p1', NOW)
        self.assertEqual([item['id'] for item in matching], [1, 2])
        self.assertEqual([item['status'] for item in stub.intents], ['cancelled', 'cancelled', 'pending', 'pending'])
        self.assertNotIn('p1', stub.interrupted_typing_participants)
        self.assertEqual(stub.split_wakes, 1)
        self.assertEqual(stub.due_intent_wake_timers, {})
        # 只有携带剧本事件身份的意图才会走账本（这里是空 payload → 没有 reference）。
        self.assertEqual(stub.ledger, [])
        # 被打断的草稿原文交给下一次写作。
        story_id, entry, participant_id = stub.entries[0]
        self.assertEqual((story_id, participant_id), ('s', 'p1'))
        self.assertEqual(entry['kind'], 'intent-cancelled')
        self.assertEqual(entry['metadata']['interruptedDrafts'], ['第一段'])
        self.assertIn('第一段', entry['content'])

    async def test_cancel_planned_false_keeps_delayed_replies(self) -> None:
        stub = _CancelStub(self._intents())
        matching = await ServiceChunk6.cancel_pending_outgoing_messages(
            stub, 's', 'p1', NOW, cancel_planned=False,
        )
        self.assertEqual([item['id'] for item in matching], [1])
        self.assertEqual(stub.intents[1]['status'], 'pending')
        self.assertEqual(stub.entries[0][1]['metadata']['interruptedDrafts'], ['第一段'])

    async def test_no_matching_intent_clears_the_interrupt_flag(self) -> None:
        stub = _CancelStub([])
        matching = await ServiceChunk6.cancel_pending_outgoing_messages(stub, 's', 'p1', NOW)
        self.assertEqual(matching, [])
        self.assertNotIn('p1', stub.interrupted_typing_participants)
        self.assertEqual(stub.entries, [])
        self.assertEqual(stub.split_wakes, 0)

    async def test_cancelled_intents_with_script_identity_update_the_ledger(self) -> None:
        commit = _commit_fixture()
        event = find_outgoing_script_event(commit, 'alice')
        from plugin.core.delivery import attach_message_event, script_event_payload
        payload = {'content': '想好了', **script_event_payload(
            attach_message_event({'participant_id': 'p1', 'content': '想好了'}, event, 42))}
        stub = _CancelStub([{'id': 1, 'storyId': 's', 'participantId': 'p1',
                             'type': 'split-message', 'status': 'pending', 'payload': payload}])
        await ServiceChunk6.cancel_pending_outgoing_messages(stub, 's', 'p1', NOW)
        self.assertEqual(len(stub.ledger), 1)
        self.assertEqual(stub.ledger[0][2], 'cancelled')
        self.assertEqual(stub.ledger[0][3], 'superseded-by-new-message')

    async def test_send_scheduled_messages_confirms_after_sending(self) -> None:
        calls: list[Any] = []

        class _Stub(ServiceChunk6):
            def __init__(self) -> None:
                self.config = _make_config()

            async def send_outgoing_messages(self, story: Any, messages: Any, *args: Any) -> list[Any]:
                calls.append(('send', messages))
                return messages

            async def confirm_outgoing_deliveries(self, story: Any, delivered: Any) -> list[Any]:
                calls.append(('confirm', delivered))
                return delivered

        messages = [{'participant_id': 'p1', 'content': 'hi'}]
        delivered = await ServiceChunk6.send_scheduled_messages(_Stub(), {'id': 's'}, messages)
        self.assertEqual(delivered, messages)
        self.assertEqual([name for name, _ in calls], ['send', 'confirm'])


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
