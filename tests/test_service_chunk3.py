"""`plugin/core/service/chunk3.py` 的单元测试（stdlib `unittest`，零依赖）。

对应上游 `upstream/src/service.ts` 第 2573–3216 行的 20 个成员。

## 上游测试的逐条移植

| 上游测试 | 断言的成员 | 本文件 |
| --- | --- | --- |
| `test/p1-memory-navigation.test.ts`「P1 archive cursor reaches records outside latest 4000 with a strict shared attempt budget」 | `backfillHistoryEmbeddings` | `TestBackfillHistoryEmbeddings.test_archive_cursor_*` |
| 同上「P1 batch one, restart cursor and model change progress without clearing old vectors」 | `backfillHistoryEmbeddings` | `TestBackfillHistoryEmbeddings.test_batch_one_*` |
| 同上「P1 backfill is single-flight, preserves concurrent ledger updates, backs off on failure」 | `backfillHistoryEmbeddings` | `TestBackfillHistoryEmbeddings.test_backfill_is_single_flight_*` |
| `test/m10-cooperation.test.ts`「mixed due work does not starve committed typing or start background narration during a live turn」 | `sweep` | `TestSweep.test_mixed_due_work_*` |

这四条按上游的 `backfillHost` / fake service 形状重建，断言逐条对齐（含
`cursor === 4` → `8`、`rows[0].embeddingIdentity` 的 `model-a` → `model-b`
而 `rows[1]` 保持 `model-a` 的「无破坏性重建」、单飞与 60s 退避）。

上游 `timeline-director.test.ts` / `continuity-guards.test.ts` /
`conversation-interruption.test.ts` / `memory-continuity.test.ts` 里的断言对象是
`toPromptPayload` / `systemPrompt` / `normalizeScenePresenceDrafts` /
`shouldSupersedeNarrativeRequest` / `decide` / `shouldRefreshContinuity`，
**全部落在 Chunk4 及更后面的行范围**（3437/3604/…），不属于本文件的 20 个成员，
故不在本文件重复断言（它们归 chunk4 的测试）。

## 一处刻意的断言改写（不是假绿）

上游断言 `queries.every(q => !q.options.limit || q.options.limit <= 128)` 保护的是
「归档窗口查询有界」。本移植版的 `Database` 只支持等值 `where`，`id > cursor` 改为
「升序取回后在 Python 侧裁剪」（见 `chunk3.py` 模块文档串「互操作」第 2 条），
因此那条**查询形状**断言在本移植版不成立也不能成立。替代断言是它真正保护的行为
不变量：每轮最多 `batchSize` 次向量化、每轮游标前进不超过 128。这里如实写出来，
避免用一个空洞通过的 limit 判断冒充覆盖。
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import random
import time
import unittest
from datetime import datetime, timezone
from typing import Any, Optional

from plugin.core.service import chunk3
from plugin.core.service.chunk3 import ServiceChunk3
from plugin.core.service.transport import NullTransport

try:  # 组装后的服务（并行移植期间某些 chunk 可能尚未落地）
    from plugin.core.service import InterludeService
except Exception:  # pragma: no cover - 只影响组装相关的两条用例
    InterludeService = None  # type: ignore[assignment]

try:  # chunk4（`advanceUnlocked` / `decide` / `tryDecide` 的归属块）
    from plugin.core.service.chunk4 import ServiceChunk4
except Exception:  # pragma: no cover
    ServiceChunk4 = None  # type: ignore[assignment]

from plugin.core.types import empty_story_state

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)

#: 本文件负责的 20 个成员（顺序 = 上游声明顺序）。
CHUNK3_MEMBERS = [
    'invalidate_history_vectors',
    'backfill_history_embeddings',
    'load_native_audio',
    'fetch_native_audio',
    'describe_vision_event',
    'load_native_images',
    'describe_current_images',
    'fetch_native_image',
    'image_bytes_to_native',
    'downscale_image_for_vision',
    'render_animated_image_frame',
    'invalidate_buffered_narratives',
    'has_pending_narrative',
    'flush_buffered_narrative',
    'advance_story',
    'deliver_messages',
    'compact_story',
    'compact_overlay',
    'admin_overlay_status',
    'sweep',
]

#: Chunk4 的成员（起始行 ≥ 3217）：本文件**不得**定义，否则会静默覆盖 MRO。
CHUNK4_MEMBERS = [
    'advance_unlocked',
    'decide',
    'should_refresh_continuity',
    'plan_automatic_timeline',
    'is_timeline_director_fused',
    'persist_timeline_retry',
    'try_decide',
]

# ---- 守门用事实（并行移植期间依赖可能缺位） ----

_HELPERS_LANDED = all(
    callable(getattr(chunk3, name, None))
    for name in ('narrative_cursor', 'guess_audio_format', 'guess_image_mime')
) and isinstance(getattr(chunk3, 'RECALLABLE_ENTRY_KINDS', None), list)

_SERVICE_ASSEMBLED = InterludeService is not None and ServiceChunk3 in getattr(
    InterludeService, '__mro__', (),
)

_CHUNK4_LANDED = ServiceChunk4 is not None


# =========================================================================== #
# 测试替身
# =========================================================================== #

class FakeCtx:
    """`InterludeContext` 的最小替身：固定时钟 + 记录计时器注册。"""

    def __init__(self) -> None:
        self.timeouts: list[tuple[Any, float]] = []
        self.clock = lambda: NOW

    def set_timeout(self, callback: Any, delay_ms: float) -> Any:
        self.timeouts.append((callback, delay_ms))
        return lambda: None


class FakeService(ServiceChunk3):
    """`ServiceChunk3` 的真实实例 + 外部依赖替身。

    刻意**继承** `ServiceChunk3`：本文件里的成员大量互相调用
    （`fetch_native_image` → `image_bytes_to_native` → `downscale_image_for_vision`、
    `flush_buffered_narrative` → `load_native_images` / `load_native_audio` …），
    用纯鸭子对象会让这些内部调用直接 `AttributeError`，测试就成了假覆盖。
    只有真正外部的依赖（数据库、模型提供者、平台传输、日志 sink、兄弟 chunk）
    被替换掉。
    """

    def __init__(self, config: Any = None, ctx: Any = None, **fields: Any) -> None:
        super().__init__(ctx if ctx is not None else FakeCtx(), config or {}, None, NullTransport())
        self.logs: list[Any] = []
        self.writes: list[tuple[str, Any, Any]] = []
        self.__dict__.update(fields)

    # ---- base 基础设施 ----
    def now(self) -> datetime:
        return NOW

    def now_ms(self) -> int:
        return int(time.time() * 1000)

    async def serial(self, key: str, task: Any) -> Any:
        return await task()

    async def db_get(self, table: str, query: Any, options: Any = None) -> list[Any]:
        raise AssertionError('本替身未预期的 db_get 表=%s 查询=%s' % (table, query))

    async def db_set(self, table: str, query: Any, data: Any) -> None:
        self.writes.append((table, query, data))

    # ---- 日志通道 ----
    def report(self, level: str, story: Any, phase: str, message: str, *args: Any) -> None:
        self.logs.append(('report', level, phase, message % args if args else message))

    def report_operation(self, *args: Any) -> None:
        self.logs.append(('operation',) + args)

    def report_standalone(self, level: str, message: str, *args: Any) -> None:
        self.logs.append(('standalone', level, message % args if args else message))

    def report_standalone_operation(self, *args: Any) -> None:
        self.logs.append(('standalone-operation',) + args)


class FakeEmbedder:
    """`Embedder.identity()` 的可控替身（测试里可换模型身份）。"""

    def __init__(self, identity: Any) -> None:
        self._identity = identity

    def identity(self) -> Any:
        return self._identity


class FakeTransport:
    """`Transport` 的局部替身：只实现本文件用到的可选钩子。"""

    def __init__(self, **hooks: Any) -> None:
        self.__dict__.update(hooks)


# =========================================================================== #
# 上游 test/p1-memory-navigation.test.ts 的 `backfillHost` 等价物
# =========================================================================== #

def _entry(entry_id: int, content: Optional[str] = None, participant_id: str = 'alice') -> dict[str, Any]:
    return {
        'id': entry_id, 'storyId': 's', 'content': content if content is not None else '原始记录 %d' % entry_id,
        'participantId': participant_id, 'kind': 'user-message', 'actor': 'user',
        'occurredAt': NOW, 'createdAt': NOW, 'metadata': {},
    }


class _BackfillHost(FakeService):
    """`backfillHost(rows, batchSize)`：数据库语义与真实 `Database.all` 对齐。

    真实 `Database` 只支持等值 `where` + `order` + `limit`（无算子、无 OFFSET），
    所以这个替身也**不**实现 `$gt`：`chunk3._archive_window_after` 必须靠
    Python 侧裁剪工作，而不是靠替身替它完成「id > cursor」。
    """

    def __init__(self, rows: list[dict[str, Any]], batch_size: int, box: dict[str, Any]) -> None:
        super().__init__(
            config={'model': {'embedding': {
                'enabled': True, 'semantic_history': True, 'backfill_batch_size': batch_size,
            }}},
            embedder=FakeEmbedder('model-a'),
        )
        self.rows = rows
        self.box = box

    async def get_story(self, story_id: str) -> Any:
        return {'id': 's', 'state': self.box['state']}

    async def embed_text(self, text: str) -> list[float]:
        self.box['calls'].append(text)
        return [1.0, 0.0]

    async def db_get(self, table: str, query: Any, options: Any = None) -> list[Any]:
        options = options or {}
        self.box['queries'].append({'query': query, 'options': options})
        result = [
            row for row in self.rows
            if row.get('storyId') == query.get('storyId')
            and ('id' not in query or row.get('id') == query.get('id'))
        ]
        directions = options.get('sort') or {}
        direction = directions.get('id')
        result = sorted(result, key=lambda row: row['id'], reverse=(str(direction).upper() == 'DESC'))
        limit = options.get('limit')
        return result[:limit] if isinstance(limit, int) else result

    async def db_set(self, table: str, query: Any, data: Any) -> None:
        if table == 'interlude_story':
            self.box['state'] = data['state']
        else:
            row = next(item for item in self.rows if item['id'] == query['id'])
            row.update(data)
        self.writes.append((table, query, data))


def _backfill_host(rows: list[dict[str, Any]], batch_size: int = 5) -> tuple[Any, dict[str, Any]]:
    box: dict[str, Any] = {
        'calls': [], 'queries': [],
        'state': {**empty_story_state(), 'extensions': {'unrelated': 'preserved'}},
    }
    return _BackfillHost(rows, batch_size, box), box


# =========================================================================== #
# 1. 范围与组装（成员清单即契约）
# =========================================================================== #

class TestMemberSurface(unittest.TestCase):
    """范围铁律：只移植声明起始行落在 [2573, 3216) 内的成员。"""

    def test_member_surface_is_exactly_the_upstream_range_in_order(self) -> None:
        names = [name for name in vars(ServiceChunk3) if not name.startswith('__')]
        self.assertEqual(names, CHUNK3_MEMBERS)

    def test_chunk4_members_are_not_redefined_here(self) -> None:
        for name in CHUNK4_MEMBERS:
            self.assertNotIn(name, vars(ServiceChunk3), 'chunk3 不得定义 %s（Chunk4 的成员）' % name)

    def test_chunk3_is_a_service_base_mixin(self) -> None:
        from plugin.core.service.base import ServiceBase

        self.assertTrue(issubclass(ServiceChunk3, ServiceBase))

    @unittest.skipUnless(_SERVICE_ASSEMBLED, '并行任务尚未组装出 InterludeService')
    def test_assembled_service_has_no_duplicate_definitions_against_chunk4(self) -> None:
        for name in CHUNK3_MEMBERS:
            owner = next(
                (cls.__name__ for cls in InterludeService.__mro__ if name in vars(cls)),
                None,
            )
            self.assertEqual(owner, 'ServiceChunk3', '%s 的定义落在 %s' % (name, owner))


# =========================================================================== #
# 2. invalidateHistoryVectors / invalidateBufferedNarratives
# =========================================================================== #

class TestHistoryVectorInvalidation(unittest.TestCase):

    def _service(self) -> FakeService:
        service = FakeService()
        service.history_vectors = {'s1': {1: {'vector': [1.0]}}, 's2': {}}
        service.history_vectors_ready = {'s1', 's2'}
        service.history_vector_loads = {'s1': object(), 's2': object()}
        service.automatic_recall_cache = {'s1': {}, 's2': {}}
        return service

    def test_story_scoped_invalidation_drops_only_that_story(self) -> None:
        service = self._service()
        ServiceChunk3.invalidate_history_vectors(service, 's1')
        self.assertEqual(list(service.history_vectors), ['s2'])
        self.assertEqual(service.history_vectors_ready, {'s2'})
        self.assertEqual(list(service.history_vector_loads), ['s2'])
        self.assertEqual(list(service.automatic_recall_cache), ['s2'])

    def test_global_invalidation_clears_everything(self) -> None:
        service = self._service()
        ServiceChunk3.invalidate_history_vectors(service)
        self.assertEqual(service.history_vectors, {})
        self.assertEqual(service.history_vectors_ready, set())
        self.assertEqual(service.history_vector_loads, {})
        self.assertEqual(service.automatic_recall_cache, {})


class TestInvalidateBufferedNarratives(unittest.TestCase):

    def test_story_scoped_reset_cancels_only_that_story(self) -> None:
        cancelled: list[str] = []
        wakes: list[str] = []
        service = FakeService()
        service.buffered_narrative_turns = {
            'a': {'storyId': 's1', 'messages': [{'content': 'x'}], 'timer': lambda: cancelled.append('a-timer'),
                  'inFlightRequestId': 7},
            'b': {'storyId': 's2', 'messages': [], 'timer': None},
        }
        service.buffered_group_turns = {
            'g1': {'storyId': 's1', 'messages': [1], 'timer': lambda: cancelled.append('g1-timer')},
            'g2': {'storyId': 's2', 'messages': [1], 'timer': None},
        }
        service.group_willingness = {'s1:group': {}, 's2:group': {}}
        service.due_intent_wake_timers = {
            's1': {'cancel': lambda: wakes.append('s1')},
            's2': {'cancel': lambda: wakes.append('s2')},
        }
        service.compaction_backoff = {'s1': {'until': 1}, 's2': {'until': 1}}

        ServiceChunk3.invalidate_buffered_narratives(service, 's1')

        self.assertEqual(cancelled, ['a-timer', 'g1-timer'])
        self.assertEqual(list(service.buffered_narrative_turns), ['b'])
        self.assertEqual(list(service.buffered_group_turns), ['g2'])
        self.assertEqual(list(service.group_willingness), ['s2:group'])
        self.assertEqual(list(service.compaction_backoff), ['s2'])
        self.assertEqual(wakes, ['s1'])
        self.assertEqual(list(service.due_intent_wake_timers), ['s2'])

    def test_in_flight_request_is_marked_obsolete_before_the_turn_is_dropped(self) -> None:
        marked: list[Any] = []
        turn = {'storyId': 's1', 'messages': [1], 'timer': None, 'inFlightRequestId': 42,
                'obsoleteRequestIds': set()}
        service = FakeService()
        service.buffered_narrative_turns = {'k': turn}
        # 删除后仍能从闭包持有的 turn 上看到标记结果。
        ServiceChunk3.invalidate_buffered_narratives(service, 's1')
        marked = turn['obsoleteRequestIds']
        self.assertEqual(marked, {42})
        self.assertEqual(service.buffered_narrative_turns, {})


# =========================================================================== #
# 3. hasPendingNarrative / flushBufferedNarrative 的守卫
# =========================================================================== #

class TestHasPendingNarrative(unittest.TestCase):

    def test_detects_narrating_buffered_and_group_turns(self) -> None:
        service = FakeService()
        self.assertFalse(ServiceChunk3.has_pending_narrative(service, 's'))

        service.narrating_stories = {'s'}
        self.assertTrue(ServiceChunk3.has_pending_narrative(service, 's'))
        service.narrating_stories = set()

        service.buffered_narrative_turns = {'k': {'storyId': 's', 'messages': [1], 'timer': None}}
        self.assertTrue(ServiceChunk3.has_pending_narrative(service, 's'))
        service.buffered_narrative_turns = {'k': {'storyId': 's', 'messages': [], 'timer': object()}}
        self.assertTrue(ServiceChunk3.has_pending_narrative(service, 's'))
        service.buffered_narrative_turns = {'k': {'storyId': 's', 'messages': [], 'timer': None,
                                                 'inFlightRequestId': 3}}
        self.assertTrue(ServiceChunk3.has_pending_narrative(service, 's'))
        service.buffered_narrative_turns = {}

        service.buffered_group_turns = {'g': {'storyId': 's', 'messages': [], 'timer': object()}}
        self.assertTrue(ServiceChunk3.has_pending_narrative(service, 's'))

    def test_other_stories_do_not_block_this_story(self) -> None:
        service = FakeService()
        service.buffered_narrative_turns = {'k': {'storyId': 'other', 'messages': [1], 'timer': None}}
        service.buffered_group_turns = {'g': {'storyId': 'other', 'messages': [1], 'timer': None}}
        self.assertFalse(ServiceChunk3.has_pending_narrative(service, 's'))

    def test_snake_case_turn_dicts_are_recognised_too(self) -> None:
        """`config.py` 的 `BufferedNarrativeTurn` 用 snake_case，base.py 用 camelCase。

        两种拼写都必须被认出来，否则「前台回合进行中」会漏判、后台扫描会插队。
        """
        service = FakeService()
        service.buffered_narrative_turns = {
            'k': {'story_id': 's', 'messages': [], 'timer': None, 'in_flight_request_id': 9},
        }
        self.assertTrue(ServiceChunk3.has_pending_narrative(service, 's'))


class TestFlushBufferedNarrativeGuards(unittest.IsolatedAsyncioTestCase):

    def _turn(self, **overrides: Any) -> dict[str, Any]:
        turn = {
            'storyId': 's', 'participantId': 'p', 'messages': [{'content': '在吗'}],
            'latestSession': object(), 'timer': None, 'nextRevision': 1,
            'inFlightRequestId': None, 'obsoleteRequestIds': set(),
        }
        turn.update(overrides)
        return turn

    async def test_paused_or_resetting_service_ignores_the_flush(self) -> None:
        for field in ('desktop_runtime_phase', 'database_resetting'):
            service = FakeService()
            turn = self._turn()
            service.buffered_narrative_turns = {'k': turn}
            if field == 'desktop_runtime_phase':
                service.desktop_runtime_phase = 'paused'
            else:
                service.database_resetting = True
            await ServiceChunk3.flush_buffered_narrative(service, 'k', 1)
            self.assertEqual(len(turn['messages']), 1, field)

    async def test_stale_revision_is_dropped(self) -> None:
        service = FakeService()
        turn = self._turn(nextRevision=4)
        service.buffered_narrative_turns = {'k': turn}
        await ServiceChunk3.flush_buffered_narrative(service, 'k', 3)
        self.assertEqual(len(turn['messages']), 1)
        self.assertNotIn('s', service.narrating_stories)

    async def test_unknown_key_is_a_noop(self) -> None:
        service = FakeService()
        await ServiceChunk3.flush_buffered_narrative(service, 'missing', 1)
        self.assertEqual(service.buffered_narrative_turns, {})

    async def test_busy_story_rearms_a_250ms_timer_and_keeps_the_batch(self) -> None:
        service = FakeService()
        turn = self._turn()
        service.buffered_narrative_turns = {'k': turn}
        service.narrating_stories = {'s'}
        await ServiceChunk3.flush_buffered_narrative(service, 'k', 1)
        self.assertEqual(len(turn['messages']), 1)
        self.assertEqual([delay for _callback, delay in service.ctx.timeouts], [250])
        self.assertTrue(callable(service.ctx.timeouts[0][0]))
        self.assertTrue(callable(turn['timer']))

    async def test_empty_batch_releases_the_story(self) -> None:
        service = FakeService()
        turn = self._turn(messages=[])
        service.buffered_narrative_turns = {'k': turn}
        await ServiceChunk3.flush_buffered_narrative(service, 'k', 1)
        self.assertNotIn('s', service.narrating_stories)
        self.assertEqual(turn['messages'], [])


# =========================================================================== #
# 4. 缓冲叙事 flush 主流程（组装 NarrativeRequest → 落库 → 投递）
# =========================================================================== #

class _FlushHost(FakeService):
    """把 `flushBufferedNarrative` 的全部跨 mixin 依赖记下来的替身。"""

    def __init__(self) -> None:
        super().__init__(
            config={'runtime': {'message_separator': '<sep/>'}},
        )
        self.calls: dict[str, Any] = {}
        self.participant = {
            'id': 'p', 'storyId': 's', 'status': 'active', 'channelId': 'private:p',
            'displayName': '希绘', 'selfId': 'bot', 'platform': 'onebot', 'userId': 'u',
        }
        self.story = {'id': 's', 'status': 'active', 'state': empty_story_state(), 'setting': {'timezone': 'Asia/Shanghai'},
                      'cursorAt': NOW, 'platform': 'onebot', 'selfId': 'bot'}

    async def get_story(self, story_id: str) -> Any:
        return dict(self.story)

    async def get_participant(self, participant_id: str) -> Any:
        return dict(self.participant)

    async def due_intents(self, story_id: str, now: Any) -> list[Any]:
        return [
            {'id': 1, 'type': 'delayed-reply', 'participantId': 'p', 'summary': '待回'},
            {'id': 2, 'type': 'split-message', 'participantId': 'p', 'summary': '分段'},
            {'id': 3, 'type': 'delayed-reply', 'participantId': 'other', 'summary': '别人的'},
        ]

    def semantic_turn_embedding_enabled(self) -> bool:
        return False

    async def sticker_catalog_for_session(self, session: Any, turn_query_embedding: Any = None) -> list[Any]:
        return []

    def private_chat_capabilities(self, session: Any) -> Any:
        return None

    async def try_decide(self, *args: Any) -> Any:
        self.calls['try_decide'] = args
        return {
            'decision': {
                'interaction': {'seen': True, 'reply': {'mode': 'immediate', 'content': '在的。'}},
                'localMedia': None, 'nativeFace': None,
            },
            'succeeded': True,
            'effectiveNow': NOW,
            'immediateObservations': [],
        }

    def resolve_sticker(self, draft: Any, catalog: Any) -> Any:
        return None

    def resolve_native_face(self, decision: Any, capabilities: Any) -> Any:
        return None

    async def persist_decision(self, *args: Any) -> Any:
        self.calls['persist_decision'] = args
        return {'messages': [{'content': '在的。'}], 'commit': None, 'scriptEntry': None}

    def can_handle_participant(self, participant: Any) -> bool:
        return True

    async def send_outgoing_messages(self, story: Any, messages: Any, participant: Any, session: Any) -> list[Any]:
        self.calls['send_outgoing_messages'] = (messages, participant, session)
        return list(messages)

    async def confirm_outgoing_deliveries(self, story: Any, delivered: Any) -> None:
        self.calls['confirm_outgoing_deliveries'] = delivered

    def schedule_compaction(self, story_id: str) -> None:
        self.calls['schedule_compaction'] = story_id

    async def schedule_conversation_follow_ups_after_turn(self, *args: Any) -> None:
        self.calls['follow_ups'] = args


class TestFlushBufferedNarrativePipeline(unittest.IsolatedAsyncioTestCase):

    async def test_full_turn_assembles_request_persists_decision_and_delivers(self) -> None:
        host = _FlushHost()
        turn = {
            'storyId': 's', 'participantId': 'p',
            'messages': [{'content': '在吗', 'occurredAt': NOW, 'imageSources': [], 'audioSources': []}],
            'latestSession': 'session', 'timer': None, 'nextRevision': 3,
            'inFlightRequestId': None, 'obsoleteRequestIds': set(),
        }
        host.buffered_narrative_turns = {'k': turn}

        await ServiceChunk3.flush_buffered_narrative(host, 'k', 3)

        # 1) tryDecide 的位置参数与上游 `tryDecide(...)` 调用逐位对齐。
        args = host.calls['try_decide']
        self.assertEqual(args[0]['id'], 's')                     # story
        self.assertEqual(args[1]['id'], 'p')                     # participant
        self.assertEqual(args[2], 'user-message')                # phase
        self.assertEqual(args[3], NOW)                           # from（游标）
        self.assertEqual(args[4], NOW)                           # now
        self.assertEqual(args[5], '在吗')                        # userMessage
        self.assertEqual([intent['id'] for intent in args[6]], [1])  # due：过滤掉自执行/他人
        self.assertEqual(args[7], [])                            # supersededIntents
        self.assertIsNone(args[8])                               # groupContext
        self.assertEqual(args[9], [])                            # images（vision 未启用）
        self.assertEqual(args[10], [])                           # audio（audio 未启用）
        self.assertIsNone(args[11])                              # chatCapabilities
        self.assertEqual(args[12], [])                           # quotedMessages
        self.assertEqual(args[13], [])                           # stickerCatalog
        self.assertIsNone(args[14])                              # turnQueryEmbedding
        self.assertIsNone(args[15])                              # visualObservations
        self.assertTrue(callable(args[16]))                      # onEarlyReply

        # 2) persistDecision 用 camelCase 的决策键名（模型 wire format）。
        persist_args = host.calls['persist_decision']
        raw = persist_args[2]
        self.assertIn('localMedia', raw)
        self.assertIsNone(raw['localMedia'])
        self.assertIn('nativeFace', raw)
        self.assertEqual(raw['interaction']['reply']['content'], '在的。')
        self.assertEqual(persist_args[3], NOW)                   # from
        self.assertEqual(persist_args[4], NOW)                   # effectiveNow
        self.assertTrue(persist_args[5])                         # permitMessages
        self.assertEqual(persist_args[6], 'user-message')        # phase
        self.assertFalse(persist_args[8])                        # 首条回复未提前投递

        # 3) 游标推进 + 已消费意图逐条结清（上游的 $in 被拆成逐 id 更新）。
        self.assertIn(('interlude_story', {'id': 's'}, {'cursorAt': NOW, 'updatedAt': NOW}), host.writes)
        intent_writes = [write for write in host.writes if write[0] == 'interlude_intent']
        self.assertEqual([write[1]['id'] for write in intent_writes], [1])
        self.assertEqual(intent_writes[0][2]['status'], 'completed')

        # 4) 投递 + 后续调度。
        self.assertEqual(host.calls['send_outgoing_messages'][0], [{'content': '在的。'}])
        self.assertEqual(host.calls['confirm_outgoing_deliveries'], [{'content': '在的。'}])
        self.assertEqual(host.calls['schedule_compaction'], 's')
        self.assertEqual(host.calls['follow_ups'][0], 's')

        # 5) 回合收尾：锁释放、缓冲清空。
        self.assertEqual(host.narrating_stories, set())
        self.assertEqual(host.buffered_narrative_turns, {})

    async def test_inactive_participant_marks_the_result_obsolete(self) -> None:
        host = _FlushHost()
        host.participant = {**host.participant, 'status': 'paused'}
        turn = {
            'storyId': 's', 'participantId': 'p', 'messages': [{'content': '在吗', 'occurredAt': NOW}],
            'latestSession': 'session', 'timer': None, 'nextRevision': 1,
            'inFlightRequestId': None, 'obsoleteRequestIds': set(),
        }
        host.buffered_narrative_turns = {'k': turn}
        await ServiceChunk3.flush_buffered_narrative(host, 'k', 1)
        self.assertNotIn('send_outgoing_messages', host.calls)
        self.assertNotIn('persist_decision', host.calls)
        self.assertEqual(host.buffered_narrative_turns, {})


# =========================================================================== #
# 5. backfillHistoryEmbeddings（上游 p1-memory-navigation 逐条移植）
# =========================================================================== #

@unittest.skipUnless(_HELPERS_LANDED, 'helpers.py / config.py 的依赖尚未落地')
class TestBackfillHistoryEmbeddings(unittest.IsolatedAsyncioTestCase):

    async def test_archive_cursor_reaches_records_outside_the_latest_window(self) -> None:
        """上游「P1 archive cursor reaches records outside latest 4000 ...」逐条移植。"""
        rows = [_entry(index + 1) for index in range(5002)]
        host, box = _backfill_host(rows)

        await ServiceChunk3.backfill_history_embeddings(host, 's')

        calls = box['calls']
        self.assertEqual(len(calls), 5)
        self.assertEqual([row['id'] for row in rows if row.get('embedding')], [1, 2, 3, 4, 5002])
        cursor_after_first = box['state']['extensions']['historyBackfill']['cursor']
        self.assertEqual(cursor_after_first, 4)
        self.assertEqual(box['state']['extensions']['unrelated'], 'preserved')

        await ServiceChunk3.backfill_history_embeddings(host, 's')
        self.assertEqual(len(calls), 10)
        cursor_after_second = box['state']['extensions']['historyBackfill']['cursor']
        self.assertEqual(cursor_after_second, 8)

        # 上游此处断言 queries.every(q => !q.options.limit || q.options.limit <= 128)。
        # 本移植版把「id > cursor」改成 Python 侧裁剪（见文件头说明），归档窗口查询
        # 因此不带 limit；替代断言是它真正保护的行为不变量：
        #   1) 最新窗口查询仍然有界（上游 limit 64 ≤ 128）；
        #   2) 单轮向量化条数 ≤ batchSize（上面 calls 的 5 / 10）；
        #   3) 单轮游标前进不超过一个归档窗口（128）。
        archive_queries = [
            item for item in box['queries']
            if item['query'].get('storyId') == 's'
            and (item['options'].get('sort') or {}).get('id') == 'ASC'
        ]
        recent_queries = [
            item for item in box['queries']
            if (item['options'].get('sort') or {}).get('id') == 'DESC'
        ]
        self.assertTrue(archive_queries)
        self.assertTrue(recent_queries)
        self.assertTrue(all(int(item['options'].get('limit', 0) or 0) == 0 for item in archive_queries))
        self.assertTrue(all(
            int(item['options'].get('limit') or 0) <= chunk3.ARCHIVE_WINDOW_ROWS
            for item in recent_queries
        ))
        self.assertLessEqual(cursor_after_second - cursor_after_first, chunk3.ARCHIVE_WINDOW_ROWS)
        self.assertLessEqual(cursor_after_first, chunk3.ARCHIVE_WINDOW_ROWS)

    async def test_batch_one_restart_cursor_and_model_change_progress_without_clearing_old_vectors(self) -> None:
        """上游「P1 batch one, restart cursor and model change ...」逐条移植。"""
        rows = [_entry(1), _entry(2), _entry(3)]
        rows[0]['embedding'] = [0.0, 1.0]
        rows[0]['metadata']['embeddingIdentity'] = 'older-model'
        host, box = _backfill_host(rows, 1)

        await ServiceChunk3.backfill_history_embeddings(host, 's')
        self.assertEqual(len(box['calls']), 1)
        self.assertEqual(rows[0]['metadata']['embeddingIdentity'], 'model-a')

        host.history_backfills = set()  # 进程内状态可能丢失
        await ServiceChunk3.backfill_history_embeddings(host, 's')
        self.assertEqual(box['state']['extensions']['historyBackfill']['cursor'], 2)

        host.embedder._identity = 'model-b'
        await ServiceChunk3.backfill_history_embeddings(host, 's')
        self.assertEqual(rows[0]['metadata']['embeddingIdentity'], 'model-b')
        self.assertEqual(
            rows[1]['metadata'].get('embeddingIdentity'), 'model-a',
            '模型更换不得就地破坏性重建全部向量',
        )

    async def test_backfill_is_single_flight_preserves_ledger_updates_and_backs_off(self) -> None:
        """上游「P1 backfill is single-flight, preserves concurrent ledger updates ...」逐条移植。"""
        rows = [_entry(1)]
        host, box = _backfill_host(rows, 1)
        release = asyncio.Event()
        started = asyncio.Event()

        async def blocking_embed(text: str) -> list[float]:
            box['calls'].append(text)
            started.set()
            await release.wait()
            return [1.0, 0.0]

        host.embed_text = blocking_embed  # type: ignore[assignment]
        pending = asyncio.ensure_future(ServiceChunk3.backfill_history_embeddings(host, 's'))
        await started.wait()
        await ServiceChunk3.backfill_history_embeddings(host, 's')  # 单飞：直接返回
        rows[0]['metadata']['delivery'] = {'status': 'delivered'}
        release.set()
        await pending

        self.assertEqual(len(box['calls']), 1)
        self.assertEqual(rows[0]['metadata']['delivery']['status'], 'delivered')
        self.assertEqual(box['state']['extensions']['historyBackfill']['cursor'], 1)

        rows.append(_entry(2))

        async def failing_embed(text: str) -> list[float]:
            box['calls'].append(text)
            return []

        host.embed_text = failing_embed  # type: ignore[assignment]
        await ServiceChunk3.backfill_history_embeddings(host, 's')
        await ServiceChunk3.backfill_history_embeddings(host, 's')  # 退避窗口内
        self.assertEqual(len(box['calls']), 2)
        self.assertEqual(box['state']['extensions']['historyBackfill']['cursor'], 1)
        self.assertGreater(host.history_backoff['s'], 0)

    async def test_disabled_embedding_or_empty_batch_is_a_noop(self) -> None:
        rows = [_entry(1)]
        host, box = _backfill_host(rows, 0)
        await ServiceChunk3.backfill_history_embeddings(host, 's')
        self.assertEqual(box['calls'], [])
        self.assertEqual(box['state']['extensions'], {'unrelated': 'preserved'})

        host.config = {'model': {'embedding': {'enabled': True, 'semantic_history': False}}}
        await ServiceChunk3.backfill_history_embeddings(host, 's')
        self.assertEqual(box['calls'], [])


# =========================================================================== #
# 6. sweep（上游 m10-cooperation 逐条移植）
# =========================================================================== #

@unittest.skipUnless(_HELPERS_LANDED, 'helpers.py / config.py 的依赖尚未落地')
class TestSweep(unittest.IsolatedAsyncioTestCase):

    async def test_mixed_due_work_does_not_starve_committed_typing(self) -> None:
        """上游「mixed due work does not starve committed typing or start background
        narration during a live turn」逐条移植。"""
        deliveries: list[str] = []
        advances: list[Any] = []

        class Host(FakeService):
            async def get_canonical_story(self) -> Any:
                return {'id': 's'}

            def can_handle_story(self, story: Any) -> bool:
                return True

            def has_pending_narrative(self, story_id: str) -> bool:
                return True

            async def due_intents(self, story_id: str, now: Any) -> list[Any]:
                return [
                    {'id': 1, 'type': 'split-message'},
                    {'id': 2, 'type': 'follow-up-commitment'},
                    {'id': 3, 'type': 'browser-research'},
                ]

            async def deliver_due_split_segments(self, story_id: str) -> None:
                deliveries.append(story_id)

            async def advance_story(self, story: Any, force: bool = True) -> list[Any]:
                advances.append(story)
                return []

        host = Host()
        await ServiceChunk3.sweep(host)

        self.assertEqual(len(deliveries), 1)
        self.assertEqual(advances, [])
        self.assertFalse(host.sweep_running)

    async def test_sweep_marks_itself_running_and_releases_it_on_failure(self) -> None:
        class Host(FakeService):
            async def get_canonical_story(self) -> Any:
                raise RuntimeError('db down')

        host = Host()
        with self.assertRaises(RuntimeError):
            await ServiceChunk3.sweep(host)
        self.assertFalse(host.sweep_running)

    async def test_sweep_is_not_reentrant(self) -> None:
        class Host(FakeService):
            def __init__(self) -> None:
                super().__init__(sweep_running=True)

            async def get_canonical_story(self) -> Any:
                raise AssertionError('重入时不得触碰数据库')

        await ServiceChunk3.sweep(Host())

    async def test_sweep_advances_and_delivers_when_nothing_is_pending(self) -> None:
        calls: list[Any] = []

        class Host(FakeService):
            async def get_canonical_story(self) -> Any:
                return {'id': 's', 'state': {'automation': {'nextAdvanceAt': NOW.isoformat()}},
                        'setting': {'timezone': 'Asia/Shanghai'}, 'cursorAt': NOW}

            def can_handle_story(self, story: Any) -> bool:
                return True

            def has_pending_narrative(self, story_id: str) -> bool:
                return False

            async def advance_story(self, story: Any, force: bool = True) -> list[Any]:
                calls.append(('advance', force))
                return [{'content': '写完了'}]

            async def send_scheduled_messages(self, story: Any, messages: Any) -> list[Any]:
                calls.append(('send', list(messages)))
                return [{'ok': True}]

        host = Host()
        await ServiceChunk3.sweep(host)
        self.assertEqual(calls, [('advance', False), ('send', [{'content': '写完了'}])])
        self.assertFalse(host.sweep_running)


# =========================================================================== #
# 7. advanceStory / deliverMessages / compactStory / compactOverlay / adminOverlayStatus
# =========================================================================== #

class TestLifecycleEntrypoints(unittest.IsolatedAsyncioTestCase):

    async def test_advance_story_skips_when_paused_or_unauthorised(self) -> None:
        for field in ('paused', 'unauthorised'):

            class Host(FakeService):
                def can_handle_story(self, story: Any) -> bool:
                    return field != 'unauthorised'

            host = Host()
            if field == 'paused':
                host.desktop_runtime_phase = 'paused'
            result = await ServiceChunk3.advance_story(host, {'id': 's'})
            self.assertEqual(result, [], field)

    async def test_advance_story_runs_advance_unlocked_and_reports(self) -> None:
        seen: list[Any] = []

        class Host(FakeService):
            def can_handle_story(self, story: Any) -> bool:
                return True

            async def get_story(self, story_id: str) -> Any:
                return {'id': story_id, 'status': 'active'}

            async def advance_unlocked(self, story: Any, now: Any, force: bool) -> list[Any]:
                seen.append(('advance_unlocked', story['id'], now, force))
                return [{'content': '一'}]

            def schedule_compaction(self, story_id: str) -> None:
                seen.append(('schedule_compaction', story_id))

        host = Host()
        messages = await ServiceChunk3.advance_story(host, {'id': 's'}, True)
        self.assertEqual(messages, [{'content': '一'}])
        self.assertEqual(seen[0][0], 'advance_unlocked')
        self.assertEqual(seen[0][3], True)
        self.assertEqual(seen[1], ('schedule_compaction', 's'))
        self.assertTrue(any(entry[0] == 'operation' and entry[1] == 'summary' for entry in host.logs))

    async def test_advance_story_does_not_report_when_forced_with_no_messages(self) -> None:
        class Host(FakeService):
            def can_handle_story(self, story: Any) -> bool:
                return True

            async def get_story(self, story_id: str) -> Any:
                return {'id': story_id}

            async def advance_unlocked(self, story: Any, now: Any, force: bool) -> list[Any]:
                return []

            def schedule_compaction(self, story_id: str) -> None:
                return None

        host = Host()
        await ServiceChunk3.advance_story(host, {'id': 's'}, False)
        self.assertFalse(any(entry[0] == 'operation' and entry[1] == 'summary' for entry in host.logs))

    async def test_deliver_messages_resolves_the_participant_and_confirms(self) -> None:
        seen: list[Any] = []
        session = {'platform': 'onebot', 'selfId': 'bot', 'userId': 'u'}

        class Host(FakeService):
            async def find_participant(self, session: Any, story: Any) -> Any:
                seen.append(('find_participant', session, story['id']))
                return {'id': 'p'}

            async def send_outgoing_messages(self, story: Any, messages: Any, participant: Any, s: Any) -> list[Any]:
                seen.append(('send', participant, s, list(messages)))
                return [{'content': '好'}]

            async def confirm_outgoing_deliveries(self, story: Any, delivered: Any) -> None:
                seen.append(('confirm', list(delivered)))

        host = Host()
        delivered = await ServiceChunk3.deliver_messages(host, {'id': 's'}, [{'content': '好'}], session)
        self.assertEqual(delivered, [{'content': '好'}])
        self.assertEqual([entry[0] for entry in seen], ['find_participant', 'send', 'confirm'])

    async def test_deliver_messages_without_session_passes_no_participant(self) -> None:
        seen: list[Any] = []

        class Host(FakeService):
            async def find_participant(self, session: Any, story: Any) -> Any:
                raise AssertionError('没有 session 时不得查参与者')

            async def send_outgoing_messages(self, story: Any, messages: Any, participant: Any, s: Any) -> list[Any]:
                seen.append((participant, s))
                return []

            async def confirm_outgoing_deliveries(self, story: Any, delivered: Any) -> None:
                seen.append(('confirm', delivered))

        host = Host()
        await ServiceChunk3.deliver_messages(host, {'id': 's'}, [])
        self.assertEqual(seen, [(None, None), ('confirm', [])])

    async def test_compact_story_and_overlay_guards(self) -> None:
        class Host(FakeService):
            def can_handle_story(self, story: Any) -> bool:
                return False

        host = Host()
        self.assertFalse(await ServiceChunk3.compact_story(host, {'id': 's'}))
        self.assertFalse(await ServiceChunk3.compact_overlay(host, {'id': 's'}))
        host.desktop_runtime_phase = 'paused'
        self.assertFalse(await ServiceChunk3.compact_story(host, {'id': 's'}))

    async def test_compact_story_passes_force_to_compact_unlocked(self) -> None:
        seen: list[Any] = []

        class Host(FakeService):
            def can_handle_story(self, story: Any) -> bool:
                return True

            async def get_story(self, story_id: str) -> Any:
                return {'id': story_id, 'state': empty_story_state()}

            async def compact_unlocked(self, story: Any, now: Any, force: bool) -> bool:
                seen.append(('compact', story['id'], force))
                return True

            async def compact_overlay_unlocked(self, story: Any, now: Any) -> bool:
                seen.append(('overlay', story['id']))
                return True

        host = Host()
        self.assertTrue(await ServiceChunk3.compact_story(host, {'id': 's'}, False))
        self.assertTrue(await ServiceChunk3.compact_overlay(host, {'id': 's'}))
        self.assertEqual(seen, [('compact', 's', False), ('overlay', 's')])

    async def test_admin_overlay_status_buckets_patches_and_participants(self) -> None:
        patches = [
            {'id': 1, 'status': 'proposed'},
            {'id': 2, 'status': 'applied'},
            {'id': 3, 'status': 'compacted'},
            {'id': 4, 'status': 'cleared'},
        ]
        snapshots = [{'id': 9, 'status': 'active'}]

        class Host(FakeService):
            async def get_story(self, story_id: str) -> Any:
                return {'id': story_id, 'state': {'setting_overlay': {'location': '图书馆'}}}

            async def db_get(self, table: str, query: Any, options: Any = None) -> list[Any]:
                if table == 'interlude_state_patch':
                    return patches
                if table == 'interlude_overlay_snapshot':
                    return snapshots
                raise AssertionError(table)

            async def participants(self, story_id: str, include_paused: bool = False) -> list[Any]:
                self.seen_include_paused = include_paused
                return [
                    {'id': 'a', 'state': {'relationshipOverlay': '更亲近了'}},
                    {'id': 'b', 'state': {}},
                ]

        host = Host()
        view = await ServiceChunk3.admin_overlay_status(host, 's')
        self.assertEqual(view['state'], {'location': '图书馆'})
        self.assertEqual([patch['id'] for patch in view['proposed']], [1])
        self.assertEqual([patch['id'] for patch in view['applied']], [2, 3])
        self.assertEqual([patch['id'] for patch in view['cleared']], [4])
        self.assertEqual(view['snapshots'], snapshots)
        self.assertEqual([item['id'] for item in view['participantOverlays']], ['a'])
        self.assertTrue(host.seen_include_paused)


# =========================================================================== #
# 8. 原生音频 / 视觉（含 PIL 降级）
# =========================================================================== #

class _MediaHost(FakeService):
    def __init__(self, **fields: Any) -> None:
        super().__init__(**fields)
        self.sent: list[Any] = []


class TestNativeAudio(unittest.IsolatedAsyncioTestCase):

    def _host(self, **audio: Any) -> _MediaHost:
        return _MediaHost(config={'model': {'audio': {'enabled': True, **audio}}})

    async def test_inline_adapter_audio_is_accepted_only_for_model_readable_formats(self) -> None:
        host = self._host()
        item = await ServiceChunk3.fetch_native_audio(host, 'data:audio/mp3;base64,QUJD')
        self.assertEqual(item, {'format': 'mp3', 'base64': 'QUJD'})
        self.assertIsNone(await ServiceChunk3.fetch_native_audio(host, 'data:audio/silk;base64,QUJD'))
        self.assertIsNone(await ServiceChunk3.fetch_native_audio(host, 'data:audio/mp3;base64,'))

    async def test_plain_http_record_url_is_skipped_without_a_file_token(self) -> None:
        host = self._host()
        self.assertIsNone(await ServiceChunk3.fetch_native_audio(host, 'https://example.com/raw.silk'))

    async def test_file_url_downloads_and_guesses_the_format(self) -> None:
        payload = b'RIFF\x00\x00\x00\x00WAVEfmt '
        host = self._host()
        host.transport = FakeTransport(fetch_audio=self._fetch(payload))
        source = 'file-url:https://cdn.example.com/a.mp3#%s:%d' % ('sample.mp3', len(payload))
        item = await ServiceChunk3.fetch_native_audio(host, source)
        self.assertEqual(item, {'format': 'mp3', 'base64': base64.b64encode(payload).decode('ascii')})

    async def test_file_url_rejects_urls_over_the_declared_size_limit(self) -> None:
        host = self._host(maxFileSizeMB=1)
        source = 'file-url:https://cdn.example.com/a.mp3#sample.mp3:%d' % (2 * 1024 * 1024)
        self.assertIsNone(await ServiceChunk3.fetch_native_audio(host, source))

    async def test_onebot_file_requires_a_transcode_channel(self) -> None:
        host = self._host()
        self.assertIsNone(await ServiceChunk3.fetch_native_audio(host, 'onebot-file:ABC.silk'))
        host.transport = FakeTransport(transcode_record=self._transcode('QUJD'))
        item = await ServiceChunk3.fetch_native_audio(host, 'onebot-file:ABC.silk')
        self.assertEqual(item, {'format': 'mp3', 'base64': 'QUJD'})

    async def test_onebot_file_transcode_errors_surface_to_the_caller(self) -> None:
        host = self._host()

        async def broken(file: str, out_format: str) -> Any:
            return {'retcode': 100, 'wording': 'not found'}

        host.transport = FakeTransport(transcode_record=broken)
        with self.assertRaises(RuntimeError):
            await ServiceChunk3.fetch_native_audio(host, 'onebot-file:ABC.silk')

    async def test_load_native_audio_wraps_items_and_reports_failures(self) -> None:
        host = self._host(maxPerMessage=2)
        host.transport = FakeTransport(fetch_audio=self._fetch(b'ID3\x04\x00\x00'))
        audio = await ServiceChunk3.load_native_audio(
            host, {'id': 's'}, ['data:audio/mp3;base64,QUJD', 'onebot-file:missing.silk'], None,
        )
        self.assertEqual([item['id'] for item in audio], ['turn-audio-1'])
        self.assertTrue(any('语音读取失败' in str(entry) for entry in host.logs) is False)
        self.assertEqual(audio[0]['format'], 'mp3')

    async def test_load_native_audio_is_disabled_by_default(self) -> None:
        host = _MediaHost(config={})
        host.transport = FakeTransport(fetch_audio=self._fetch(b'ID3\x04\x00\x00'))
        audio = await ServiceChunk3.load_native_audio(host, {'id': 's'}, ['data:audio/mp3;base64,QUJD'])
        self.assertEqual(audio, [])

    @staticmethod
    def _fetch(payload: bytes) -> Any:
        async def fetch(url: str) -> bytes:
            return payload

        return fetch

    @staticmethod
    def _transcode(value: str) -> Any:
        async def transcode(file: str, out_format: str) -> str:
            return value

        return transcode


class TestVisionHelpers(unittest.IsolatedAsyncioTestCase):

    def test_describe_vision_event_strips_attachment_markup_and_keeps_sources(self) -> None:
        host = FakeService()
        event = ServiceChunk3.describe_vision_event(host, {
            'content': '看这个<img src="https://gchat.qpic.cn/a.png"/>好看吗[CQ:record,file=v.silk]',
        })
        self.assertEqual(event['content'], '看这个好看吗')
        self.assertEqual(event['sources'], ['https://gchat.qpic.cn/a.png'])

    def test_describe_vision_event_keeps_image_only_input_wordless(self) -> None:
        """上游注释：抓取失败/被过滤必须表现为「没有视觉输入」，不邀请模型编造。"""
        host = FakeService()
        event = ServiceChunk3.describe_vision_event(host, {'content': '<img src="https://gchat.qpic.cn/a.png"/>'})
        self.assertEqual(event['content'], '')
        self.assertEqual(event['sources'], ['https://gchat.qpic.cn/a.png'])

    def test_extract_session_image_sources_prefers_cdn_url_then_file_token(self) -> None:
        host = FakeService()
        self.assertEqual(
            # 上游 `add(fields.url || fields.cache_url, 'adapter-url')` → 带 onebot-url: 前缀
            chunk3._extract_session_image_sources({'content': '[CQ:image,file=a.jpg,url=https://x/a.jpg]'}),
            ['onebot-url:https://x/a.jpg'],
        )
        self.assertEqual(
            chunk3._extract_session_image_sources({'content': '[CQ:image,file=abc.jpg]'}),
            ['onebot-file:abc.jpg'],
        )
        self.assertEqual(
            chunk3._extract_session_image_sources({'content': '<img src="https://x/b.png"/><img src="https://x/b.png"/>'}),
            ['https://x/b.png'],
        )
        self.assertEqual(chunk3._extract_session_image_sources({'content': '普通文字'}), [])

    def test_format_buffered_user_messages_matches_upstream_text(self) -> None:
        single = chunk3._format_buffered_user_messages([{'content': '在吗', 'occurredAt': NOW}])
        self.assertEqual(single, '在吗')
        merged = chunk3._format_buffered_user_messages([
            {'content': '在吗', 'occurredAt': NOW},
            {'content': '看这个', 'occurredAt': NOW},
        ])
        stamp = chunk3.iso(NOW)
        self.assertEqual(
            merged,
            '[连续消息 1，收到时间 %s]\n在吗\n\n[连续消息 2，收到时间 %s]\n看这个' % (stamp, stamp),
        )

    async def test_fetch_native_image_rejects_untrusted_hosts(self) -> None:
        host = _MediaHost(config={'model': {'vision': {'enabled': True}}})

        async def fetch(url: str) -> bytes:
            raise AssertionError('不可信主机不得被抓取：%s' % url)

        host.transport = FakeTransport(fetch_image=fetch)
        self.assertIsNone(await ServiceChunk3.fetch_native_image(host, 'https://example.com/a.png'))
        self.assertIsNone(await ServiceChunk3.fetch_native_image(host, 'ftp://gchat.qpic.cn/a.png'))

    async def test_fetch_native_image_accepts_adapter_provided_and_trusted_urls(self) -> None:
        png = _png_bytes()
        host = _MediaHost(config={'model': {'vision': {'enabled': True}}})

        async def fetch(url: str) -> bytes:
            return png

        host.transport = FakeTransport(fetch_image=fetch)
        trusted = await ServiceChunk3.fetch_native_image(host, 'https://gchat.qpic.cn/a.png')
        self.assertEqual(trusted['mime_type'], 'image/png')
        self.assertTrue(trusted['data_uri'].startswith('data:image/png;base64,'))
        adapter = await ServiceChunk3.fetch_native_image(
            host, 'onebot-url:https://example.com/a.png',
        )
        self.assertEqual(adapter['mime_type'], 'image/png')

    async def test_fetch_native_image_decodes_data_uris_and_rejects_non_images(self) -> None:
        host = _MediaHost(config={'model': {'vision': {'enabled': True}}})
        png = _png_bytes()
        image = await ServiceChunk3.fetch_native_image(
            host, 'data:image/png;base64,' + base64.b64encode(png).decode('ascii'),
        )
        self.assertEqual(image['mime_type'], 'image/png')
        self.assertIsNone(await ServiceChunk3.fetch_native_image(host, 'data:text/plain;base64,QUJD'))

    async def test_image_bytes_to_native_passes_through_when_pil_is_missing(self) -> None:
        host = _MediaHost(
            config={'model': {'vision': {'enabled': True, 'max_image_dimension': 1024}}},
        )
        gif = _gif_bytes()
        original = chunk3._PIL_IMAGE
        chunk3._PIL_IMAGE = None  # 模拟未安装 Pillow
        try:
            image = await ServiceChunk3.image_bytes_to_native(host, gif, 'image/gif')
        finally:
            chunk3._PIL_IMAGE = original
        # 抽帧不可用 → 透传原图（而不是编造描述、也不抛异常）。
        self.assertEqual(image['mime_type'], 'image/gif')
        self.assertTrue(any('抽帧' in str(entry) for entry in host.logs))

    async def test_image_bytes_to_native_rejects_non_image_mime(self) -> None:
        host = _MediaHost(config={})
        self.assertIsNone(await ServiceChunk3.image_bytes_to_native(host, b'hello', 'text/plain'))

    async def test_load_native_images_respects_vision_switch_and_sources_limit(self) -> None:
        png = _png_bytes()
        host = _MediaHost(config={'model': {'vision': {'enabled': True}}})

        async def fetch(url: str) -> bytes:
            return png

        host.transport = FakeTransport(fetch_image=fetch)
        images = await ServiceChunk3.load_native_images(
            host, {'id': 's'},
            ['https://gchat.qpic.cn/1.png', 'https://gchat.qpic.cn/2.png',
             'https://gchat.qpic.cn/3.png', 'https://gchat.qpic.cn/4.png'],
            None,
        )
        self.assertEqual([item['id'] for item in images], ['turn-image-1', 'turn-image-2', 'turn-image-3'])
        disabled = _MediaHost(config={'model': {'vision': {'enabled': False}}})
        self.assertEqual(await ServiceChunk3.load_native_images(disabled, {'id': 's'}, ['x'], None), [])

    async def test_describe_current_images_skips_without_a_vision_provider(self) -> None:
        host = _MediaHost(config={})

        class Describer:
            def available(self) -> bool:
                return False

        host.vision_describer = Describer()
        self.assertIsNone(await ServiceChunk3.describe_current_images(host, {'id': 's'}, [{'id': 'i'}], '看看'))
        self.assertTrue(any('侧端识图跳过' in str(entry) for entry in host.logs))

    async def test_describe_current_images_returns_observations(self) -> None:
        host = _MediaHost(config={'model': {'vision': {'detail': 'low'}}})

        class Describer:
            def __init__(self) -> None:
                self.seen: Any = None

            def available(self) -> bool:
                return True

            async def describe_images(self, images: Any, user_text: str = '', detail: str = 'auto') -> Any:
                self.seen = (list(images), user_text, detail)
                return ['1. 一只橘猫。']

        describer = Describer()
        host.vision_describer = describer
        result = await ServiceChunk3.describe_current_images(host, {'id': 's'}, [{'id': 'i'}], '看看')
        self.assertEqual(result, ['1. 一只橘猫。'])
        self.assertEqual(describer.seen, ([{'id': 'i'}], '看看', 'low'))

    async def test_describe_current_images_swallows_provider_failure(self) -> None:
        host = _MediaHost(config={})

        class Describer:
            def available(self) -> bool:
                return True

            async def describe_images(self, images: Any, user_text: str = '', detail: str = 'auto') -> Any:
                raise RuntimeError('provider down')

        host.vision_describer = Describer()
        self.assertIsNone(await ServiceChunk3.describe_current_images(host, {'id': 's'}, [{'id': 'i'}], None))
        self.assertTrue(any('侧端识图失败' in str(entry) for entry in host.logs))

    async def test_downscale_is_skipped_without_a_configured_dimension(self) -> None:
        host = _MediaHost(config={'model': {'vision': {'max_image_dimension': 0}}})
        data_uri = 'data:image/png;base64,' + base64.b64encode(_png_bytes()).decode('ascii')
        self.assertIsNone(await ServiceChunk3.downscale_image_for_vision(
            host, {'mime_type': 'image/png', 'data_uri': data_uri},
        ))

    async def test_downscale_is_skipped_for_small_images(self) -> None:
        host = _MediaHost(config={'model': {'vision': {'max_image_dimension': 64}}})
        data_uri = 'data:image/png;base64,' + base64.b64encode(_png_bytes()).decode('ascii')
        self.assertIsNone(await ServiceChunk3.downscale_image_for_vision(
            host, {'mime_type': 'image/png', 'data_uri': data_uri},
        ))

    async def test_downscale_degrades_safely_without_pil(self) -> None:
        host = _MediaHost(config={'model': {'vision': {'max_image_dimension': 64}}})
        data_uri = 'data:image/png;base64,' + base64.b64encode(_noise_png()).decode('ascii')
        original = chunk3._PIL_IMAGE
        chunk3._PIL_IMAGE = None
        try:
            self.assertIsNone(await ServiceChunk3.downscale_image_for_vision(
                host, {'mime_type': 'image/png', 'data_uri': data_uri},
            ))
        finally:
            chunk3._PIL_IMAGE = original
        self.assertTrue(any('Pillow' in str(entry) for entry in host.logs))

    @unittest.skipUnless(chunk3.PIL_AVAILABLE, '未安装 Pillow')
    async def test_downscale_returns_a_smaller_jpeg_when_pil_is_available(self) -> None:
        host = _MediaHost(config={'model': {'vision': {'max_image_dimension': 64}}})
        payload = _noise_png()
        data_uri = 'data:image/png;base64,' + base64.b64encode(payload).decode('ascii')
        scaled = await ServiceChunk3.downscale_image_for_vision(
            host, {'mime_type': 'image/png', 'data_uri': data_uri},
        )
        self.assertIsNotNone(scaled)
        self.assertEqual(scaled['mime_type'], 'image/jpeg')
        self.assertLess(len(chunk3._data_uri_payload(scaled['data_uri'])), len(payload))

    @unittest.skipUnless(chunk3.PIL_AVAILABLE, '未安装 Pillow')
    async def test_animated_frame_is_rendered_as_png(self) -> None:
        host = _MediaHost(config={})
        data_uri = 'data:image/gif;base64,' + base64.b64encode(_gif_bytes(frames=3)).decode('ascii')
        frame = await ServiceChunk3.render_animated_image_frame(host, data_uri)
        self.assertEqual(frame['mime_type'], 'image/png')
        self.assertTrue(chunk3._data_uri_payload(frame['data_uri']).startswith(b'\x89PNG'))


# =========================================================================== #
# 9. 跨 chunk 契约（chunk4 的 tryDecide 必须接住本文件的位置参数）
# =========================================================================== #

@unittest.skipUnless(_CHUNK4_LANDED, '并行任务 chunk4 尚未落地')
class TestChunk4Contract(unittest.TestCase):

    def test_try_decide_accepts_the_positional_call_shape(self) -> None:
        signature = inspect.signature(ServiceChunk4.try_decide)
        names = list(signature.parameters)
        self.assertEqual(names[:17], [
            'self', 'story', 'participant', 'phase', 'from_', 'now', 'user_message',
            'due_intents', 'superseded_intents', 'group_context', 'images', 'audio',
            'chat_capabilities', 'quoted_messages', 'sticker_catalog', 'turn_query_embedding',
            'visual_observations',
        ])
        self.assertEqual(names[17], 'on_early_reply')

    def test_persist_decision_accepts_the_positional_call_shape(self) -> None:
        signature = inspect.signature(ServiceChunk4.persist_decision)
        names = list(signature.parameters)
        self.assertEqual(names[:10], [
            'self', 'story', 'participant', 'raw', 'from_', 'now', 'permit_messages',
            'phase', 'context_intents', 'immediate_reply_already_delivered',
        ])


# =========================================================================== #
# 图片夹具
# =========================================================================== #

def _png_bytes() -> bytes:
    """1x1 透明 PNG（不依赖 Pillow）。"""
    return base64.b64decode(
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=='
    )


def _gif_bytes(frames: int = 2) -> bytes:
    """最小多帧 GIF（不依赖 Pillow）：每帧 1x1 像素。

    注意每个帧的 Graphic Control Extension 必须**先于** Image Descriptor，
    否则 Pillow 解不出帧（这也是这段夹具最初的 bug）。
    """
    header = b'GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff'
    control = b'\x21\xf9\x04\x00\x00\x00\x00\x00'
    frame = control + b'\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00' + b'\x02\x02\x44\x01\x00'
    return header + frame * max(1, frames) + b'\x3b'


def _noise_png() -> bytes:
    """≥150KB 的噪声 PNG（触发 `shouldDownscaleImage` 的体积门槛）。

    无 Pillow 时用 PNG 的「未压缩」zlib 流手工拼一张 400x400 灰度图；
    有 Pillow 时直接用 Pillow 生成，保证体积门槛稳定达标。
    """
    import struct
    import zlib

    size = 400
    raw = bytearray()
    generator = random.Random(20260906)
    for _ in range(size):
        raw.append(0)  # filter type
        raw.extend(generator.getrandbits(8) for _ in range(size * 3))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack('>I', len(payload)) + tag + payload
            + struct.pack('>I', zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack('>IIBBBBB', size, size, 8, 2, 0, 0, 0)
    png = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr) + chunk(b'IDAT', zlib.compress(bytes(raw), 0)) + chunk(b'IEND', b'')
    if len(png) < 150 * 1024:  # pragma: no cover - 夹具自身的气泡检查
        raise AssertionError('噪声 PNG 未达到降采样体积门槛：%d' % len(png))
    return png


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
