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
from plugin.core.service.base import ServiceChunk0
from plugin.core.service.chunk3 import ServiceChunk3, _load_group_batch_audio
from plugin.core.service.session import SessionView
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

    def report_standalone(self, level: str, message: str, *args: Any, **_kwargs: Any) -> None:
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

    #: 生产链路上 `note_access_skip`（按原因节流的 warn）来自 Chunk0；本文件的替身只
    #: 继承 Chunk3，这里把**生产那一份实现**借过来（不另写一套节流，免得替身比生产更宽）。
    note_access_skip = ServiceChunk0.note_access_skip

    def __init__(self, config: Any = None) -> None:
        super().__init__(
            config=config if config is not None else {'runtime': {'message_separator': '<sep/>'}},
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

    async def sticker_selection_for_session(
        self, session: Any, turn_query_embedding: Any = None,
    ) -> dict[str, Any]:
        # §48 甲：本文件只关心"位置参数与上游对齐"，目录一律走 `inline` + 空条目
        # （两级选择本身由 `test_service_chunk2` 覆盖）。
        return {'mode': 'inline', 'assets': [], 'groups': []}

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

    async def resolve_sticker_selection(
        self, decision: Any, selection: Any, follow_up_budget: Any = None,
    ) -> Any:
        # §48 甲：inline 模式下 `resolve_sticker_selection` 就是旧的 `resolve_sticker`
        # （本替身里两条都回 None = 没有选中的表情）。
        return None

    def resolve_native_face(self, decision: Any, capabilities: Any) -> Any:
        return None

    async def persist_decision(self, *args: Any) -> Any:
        self.calls['persist_decision'] = args
        return {'messages': [{'content': '在的。'}], 'commit': None, 'scriptEntry': None}

    def can_handle_participant(self, participant: Any) -> bool:
        return True

    async def send_outgoing_messages(
        self, story: Any, messages: Any, participant: Any, session: Any,
        should_cancel: Any = None, record_failures: bool = True, request_started_at: Any = None,
        **kwargs: Any,
    ) -> list[Any]:
        self.calls['send_outgoing_messages'] = (messages, participant, session)
        # 上游 1.0.1-rc21：叙事请求发起时刻必须传到投递侧（首条打字时间下限的基准）。
        self.calls['request_started_at'] = request_started_at
        # 逐条「正在输入」窗口只在"立即回复"这条路径打开（用户 2026-09-28 点名）。
        self.calls['typing_window'] = kwargs.get('typing_window')
        return list(messages)

    async def confirm_outgoing_deliveries(self, story: Any, delivered: Any) -> None:
        self.calls['confirm_outgoing_deliveries'] = delivered

    def schedule_compaction(self, story_id: str) -> None:
        self.calls['schedule_compaction'] = story_id

    async def schedule_conversation_follow_ups_after_turn(self, *args: Any) -> None:
        self.calls['follow_ups'] = args


class TestFlushBufferedNarrativePipeline(unittest.IsolatedAsyncioTestCase):

    async def test_private_sidecar_failure_stays_on_the_diagnostic_channel(self) -> None:
        """**反向（私聊逐字不变）**：私聊 + 侧端识图失败 → 观察结果为空、**没有**可见 warn。

        v1.9.9 给 `describe_current_images` 加了 `visible`（群回合用），私聊那条路
        （本用例走的就是它）不传它 → 报告仍走 `report_operation('diagnostic', ...)`；
        私聊日志因此与 v1.9.9 逐字相同（"没有报告"是既有口径，本批只给**群**补可见性）。
        """
        import os  # noqa: PLC0415
        import tempfile  # noqa: PLC0415

        host = _FlushHost(config={
            'model': {'vision': {'enabled': True, 'mode': 'sidecar'}},
            'runtime': {'message_separator': '<sep/>'},
        })

        class Describer:
            def __init__(self) -> None:
                self.called = 0

            def available(self) -> bool:
                return True

            async def describe_images(self, images: Any, user_text: str = '',
                                      detail: str = 'auto', kinds: Any = None) -> Any:
                self.called += 1
                raise RuntimeError('provider down')

        describer = Describer()
        host.vision_describer = describer
        directory = tempfile.mkdtemp(prefix='hdsi-private-sidecar-')
        self.addCleanup(__import__('shutil').rmtree, directory, True)
        path = os.path.join(directory, '私聊的图.png')
        with open(path, 'wb') as handle:
            handle.write(_png_bytes())

        turn = {
            'storyId': 's', 'participantId': 'p',
            'messages': [{'content': '看这个', 'occurredAt': NOW,
                          'imageSources': ['onebot-file:%s' % path], 'audioSources': []}],
            'latestSession': SessionView(platform='onebot', self_id='bot', user_id='u',
                                         content='看这个'),
            'timer': None, 'nextRevision': 3,
            'inFlightRequestId': None, 'obsoleteRequestIds': set(),
        }
        host.buffered_narrative_turns = {'k': turn}

        await ServiceChunk3.flush_buffered_narrative(host, 'k', 3)

        # ① 失败 → 观察结果为空（绝不拿一段编的话顶上）。
        self.assertEqual(describer.called, 1, '侧端识图真的被调过一次（否则本用例是空转）')
        self.assertIsNone(host.calls['try_decide'][15])
        # ② 私聊**没有**可见 warn：`report` 通道一条侧端识图的记录都不许有。
        self.assertEqual(
            [entry for entry in host.logs if entry[0] == 'report' and '侧端识图' in str(entry)],
            [], host.logs,
        )
        # ③ 旧的那条 diagnostic 报告照旧在（与 v1.9.9 逐字同一条）。
        self.assertTrue(
            any(entry[0] == 'operation' and '侧端识图失败' in str(entry) for entry in host.logs),
            host.logs,
        )

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
        self.assertIsNotNone(host.calls.get('request_started_at'), '投递侧必须拿到请求发起时刻')
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
# 4.5 每回合图片预算（v1.9.4）
# =========================================================================== #

class TestPerTurnImageBudget(unittest.IsolatedAsyncioTestCase):
    """v1.9.4：每回合图片预算**可配**，且截断**必须可见**。

    真机现场（用户 2026-10-04）：一条 17 节点的合并转发（节点 2–15 全是图）+ 同批 3 张
    直发图，模型只拿到 3 张 —— 而上下文里**没有一句"一共几张、给了几张"**：她既看不到
    其余的，也不知道还有。根因是 `chunk3` 里写死的两处 `[:3]`（flush 与
    `load_native_images`），转发那道预算（`forward_message.max_images`）调多大都改不动它。

    这几条用例钉住修法的四件事：默认仍是 3、**改配置真的多给**、没截断时闭嘴、
    截断时线索里两个数（一共 / 给了）都在。
    """

    HOST = 'https://gchat.qpic.cn/turn/%02d.png'

    def _sources(self, count: int) -> list[str]:
        return [self.HOST % index for index in range(1, count + 1)]

    def _host(self, sources: Any, *, vision: Any = None, forward: Any = None) -> Any:
        host = _FlushHost(config={
            'runtime': {'message_separator': '<sep/>'},
            'model': {'vision': {'enabled': True, **(vision or {})}},
            # v1.9.4：合并转发那一组（单卡 `max_images` 决定每回合上限的**默认值**）。
            'forward_message': dict(forward) if forward else {},
        })

        async def fetch(url: str) -> bytes:
            # 每张 URL 给一份不同字节的**纯色**图：`image_perceptual_hash` 对纯色图
            # 返回空串（不参与同图去重），所以这里数的确实是"预算给了几张"。
            return _png_bytes() + url.encode('utf-8')

        host.transport = FakeTransport(fetch_image=fetch)
        host.buffered_narrative_turns = {'k': {
            'storyId': 's', 'participantId': 'p',
            'messages': [{
                'content': '你看', 'occurredAt': NOW,
                'imageSources': list(sources), 'audioSources': [],
            }],
            'latestSession': SessionView(
                platform='onebot', self_id='1', user_id='u', channel_id='private:u',
                content='你看', media=[],
            ),
            'timer': None, 'nextRevision': 3, 'inFlightRequestId': None,
            'obsoleteRequestIds': set(),
        }}
        return host

    async def _flush(self, sources: Any, *, vision: Any = None, forward: Any = None) -> Any:
        host = self._host(sources, vision=vision, forward=forward)
        await ServiceChunk3.flush_buffered_narrative(host, 'k', 3)
        return host

    @staticmethod
    def _message(host: Any) -> str:
        """`tryDecide` 的第 6 个位置参数 = 当前事件正文（`userMessage`）。"""
        return host.calls['try_decide'][5]

    @staticmethod
    def _images(host: Any) -> Any:
        """`tryDecide` 的第 10 个位置参数 = 本回合进 payload 的原生图。"""
        return host.calls['try_decide'][9]

    @staticmethod
    def _budget_warns(host: Any) -> list[str]:
        return [
            entry[-1] for entry in host.logs
            if isinstance(entry, tuple) and entry and isinstance(entry[-1], str)
            and '每回合图片数上限' in entry[-1]
        ]

    async def test_default_budget_gives_three_and_the_clue_counts_fourteen(self) -> None:
        """① 预算=3 + 候选 14 张 → 只给 3 张，**且线索里 14 与 3 都在**。"""
        host = await self._flush(self._sources(14))
        self.assertEqual(len(self._images(host)), 3, '默认预算就是省成本那侧的 3')
        self.assertIn('[图片×14，本回合仅取前 3 张]', self._message(host),
                      '可数线索必须给模型：一共几张、给了几张')
        warns = self._budget_warns(host)
        self.assertEqual(len(warns), 1, '日志里也要有一条（丢内容必须看得见）')
        self.assertIn('14', warns[0])
        self.assertIn('只交给模型前 3 张', warns[0])
        # 给了几张，payload 里的 `attachments` / `imageCount` 也必须是几张（不虚报）。
        self.assertEqual(len(host.calls['try_decide'][17]), 3)

    async def test_raising_the_budget_really_gives_more(self) -> None:
        """② 预算改成 6 → **真的给 6 张**（证明配置生效，不是摆设）。

        `attachments` 也要跟着变 6：它就是"这条消息带了什么"那张表（先于视频帧算完的
        那一刀），只改预算而不改它 = 模型看到 6 张图却只被告知带了 3 张。
        """
        for spelling in ({'max_per_turn': 6}, {'maxPerTurn': 6}):
            with self.subTest(spelling=spelling):
                host = await self._flush(self._sources(14), vision=spelling)
                self.assertEqual(len(self._images(host)), 6, spelling)
                self.assertIn('[图片×14，本回合仅取前 6 张]', self._message(host))
                self.assertEqual(len(host.calls['try_decide'][17]), 6, 'attachments 同步')

    async def test_no_clue_when_nothing_was_cut(self) -> None:
        """③ 候选 2 张 → **不出现**截断线索（没截断就不许说"仅取前 N"）。"""
        host = await self._flush(self._sources(2))
        self.assertEqual(len(self._images(host)), 2)
        self.assertNotIn('[图片×', self._message(host))
        self.assertEqual(self._budget_warns(host), [], '也没截断就没这条 warn')

    async def test_no_clue_when_image_understanding_is_off(self) -> None:
        """③ 反向：图片理解关着时一张都不给模型，就**不许**说"取了前 3 张"。

        那时 `load_native_images` 直接回空表（既有闸），线索说"取了 3 张"就是假话；
        关着时的正确表述由既有链路管（"没有视觉输入"），这里一个字都不加。
        """
        host = _FlushHost(config={
            'runtime': {'message_separator': '<sep/>'},
            'model': {'vision': {'enabled': False}},
        })
        host.buffered_narrative_turns = {'k': {
            'storyId': 's', 'participantId': 'p',
            'messages': [{
                'content': '你看', 'occurredAt': NOW,
                'imageSources': self._sources(14), 'audioSources': [],
            }],
            'latestSession': 'session', 'timer': None, 'nextRevision': 3,
            'inFlightRequestId': None, 'obsoleteRequestIds': set(),
        }}
        await ServiceChunk3.flush_buffered_narrative(host, 'k', 3)
        self.assertEqual(self._images(host), [], '关着时一张都没有')
        self.assertNotIn('[图片×', self._message(host))
        self.assertEqual(self._budget_warns(host), [])

    async def test_image_understanding_off_still_counts_every_attachment(self) -> None:
        """① 关识图 + 14 张 → 标签**数得出 14**，且不出现视觉预算线索（v1.9.4 §59）。

        预算管的是"给模型**看**几张"；关着时一张都不看，预算就无权改写"他带了 14 张"
        这件事实。按截断后的 `image_sources` 列，模型连"一共几张"都数不出来 —— 那正是
        用户抱怨的那类"她不知道还有更多"。这一档必须按**候选全表**列（事实给全）。
        """
        host = await self._flush(self._sources(14), vision={'enabled': False})
        self.assertEqual(self._images(host), [], '关着时一张都不给模型')
        attachments = host.calls['try_decide'][17]
        self.assertEqual(len(attachments), 14, '来了几张是事实，不许被视觉预算削到 3')
        self.assertEqual([item['index'] for item in attachments], list(range(1, 15)))
        self.assertNotIn('[图片×', self._message(host), '关着时不许说"仅取前 N"（那是假话）')
        self.assertEqual(self._budget_warns(host), [], '也没截断就没这条 warn')

    async def test_image_understanding_off_never_inflates_a_small_batch(self) -> None:
        """①b 关识图 + 2 张 → 就是 2 条（事实给全 ≠ 造一张汇总）。"""
        host = await self._flush(self._sources(2), vision={'enabled': False})
        attachments = host.calls['try_decide'][17]
        self.assertEqual(len(attachments), 2)
        self.assertEqual([item['index'] for item in attachments], [1, 2])

    async def test_the_truncation_warn_is_throttled_per_reason_and_session(self) -> None:
        """同一条原因按会话节流：连打两次只有一条 warn（与既有的节流口径一致）。"""
        from plugin.core.vision_budget import note_image_budget_skip

        host = self._host([])
        session = SessionView(
            platform='onebot', self_id='1', user_id='u', channel_id='private:u',
        )
        self.assertTrue(note_image_budget_skip(host, session, 14, 3, 3))
        self.assertFalse(note_image_budget_skip(host, session, 14, 3, 3))
        self.assertEqual(len(self._budget_warns(host)), 1)
        # 换会话（另一个 channelId）可以重新报一条。
        other = SessionView(
            platform='onebot', self_id='1', user_id='v', channel_id='private:v',
        )
        self.assertTrue(note_image_budget_skip(host, other, 9, 3, 3))

    async def test_native_loader_uses_the_same_configured_budget(self) -> None:
        """第二处截断（`load_native_images`）读的是**同一个**预算，不是写死的 3。"""
        png = _png_bytes()
        fetched: list[str] = []

        async def fetch(url: str) -> bytes:
            fetched.append(url)
            return png + url.encode('utf-8')

        sources = self._sources(8)
        host = _MediaHost(config={'model': {'vision': {'enabled': True, 'max_per_turn': 5}}})
        host.transport = FakeTransport(fetch_image=fetch)
        images = await ServiceChunk3.load_native_images(host, {'id': 's'}, sources, None)
        self.assertEqual(len(images), 5, '配置 5 就必须取 5 —— 两处下刀同源')
        self.assertEqual(fetched, sources[:5], '取的是排在前面的 5 张')

    async def test_forwarded_and_direct_sources_share_one_budget(self) -> None:
        """转发来的图与直发的图**抢同一个预算**（§53 既有口径，预算只有一道）。"""
        direct = self._sources(2)
        forwarded = ['https://gchat.qpic.cn/ft/%d.png' % index for index in (1, 2, 3, 4)]
        host = await self._flush([*direct, *forwarded], vision={'max_per_turn': 4})
        self.assertEqual(len(self._images(host)), 4)
        self.assertIn('[图片×6，本回合仅取前 4 张]', self._message(host))

    async def test_raising_only_the_forward_cap_really_gives_more(self) -> None:
        """① 只把「单条转发最多读取的图片数」调到 6（每回合上限没动）→ **真的给 6 张**。

        这就是用户那次真机报告的修法：他改的只有那一个键，模型就该真的多看到几张
        （而不是被一个他没动过的默认 3 削掉）。

        **变异保护（反向）**：把跟随规则删掉 = 两个键各算一份默认值 →
        `len(self._images(host))` 会回到 3，这条当场红。
        """
        host = await self._flush(self._sources(14), forward={'max_images': 6})
        self.assertEqual(len(self._images(host)), 6, '单卡调到 6，模型就该拿到 6 张')
        self.assertIn('[图片×14，本回合仅取前 6 张]', self._message(host))
        self.assertEqual(len(host.calls['try_decide'][17]), 6, 'attachments 同步（不虚报）')

    async def test_an_explicit_turn_cap_still_wins(self) -> None:
        """反向：本项改成了别的数 → 以本项为准（跟随只管"没改过"的那一档）。"""
        host = await self._flush(
            self._sources(14), vision={'max_per_turn': 2}, forward={'max_images': 6},
        )
        self.assertEqual(len(self._images(host)), 2)
        self.assertIn('[图片×14，本回合仅取前 2 张]', self._message(host))

    async def test_the_second_cut_follows_the_same_effective_budget(self) -> None:
        """第二处下刀（`load_native_images`）也必须跟随同一个有效值。

        不跟随的话 6 张会在那里被削回 3 —— 用户看到的就是"改了配置没用"（真机报告）。
        """
        png = _png_bytes()
        fetched: list[str] = []

        async def fetch(url: str) -> bytes:
            fetched.append(url)
            return png + url.encode('utf-8')

        sources = self._sources(8)
        host = _MediaHost(config={
            'model': {'vision': {'enabled': True}},
            'forward_message': {'max_images': 6},
        })
        host.transport = FakeTransport(fetch_image=fetch)
        images = await ServiceChunk3.load_native_images(host, {'id': 's'}, sources, None)
        self.assertEqual(len(images), 6, '两处下刀必须同源，否则 6 张在这里被削回 3')
        self.assertEqual(fetched, sources[:6])


# =========================================================================== #
# 4.6 每回合音频预算（v1.9.5）
# =========================================================================== #

class TestPerTurnAudioBudget(unittest.IsolatedAsyncioTestCase):
    """v1.9.5：多段视频的音轨被「每个事件音频数上限」切开时**必须可数**。

    多段视频的音轨与直发语音走**同一条**语音通道，`maxPerMessage`（默认 1）以前把
    第二段起的音轨**静默**切掉：模型不知道有，日志也不说。修法两半共用一处实现
    （`chunk3.audio_turn_slice`）：线索进当前事件、节流 warn 进日志。

    反向用例：把切片留着、把那句线索删掉 → `test_the_clue_counts_what_was_cut` 红；
    总开关关掉还硬报"仅取前 0 段" → `test_master_switch_off_is_not_a_budget_cut` 红。
    """

    #: 三段**互不相同**的内联音频（`flush` 会先去重，同一串重复三次只会剩一段）。
    AUDIO = 'data:audio/mp3;base64,QUJD'

    def _sources(self, count: int) -> list[str]:
        return ['data:audio/mp3;base64,%s' % ('QUJD' * (index + 1))
                for index in range(count)]

    def _host(self, count: int, *, audio: Any = None) -> Any:
        host = _FlushHost(config={
            'runtime': {'message_separator': '<sep/>'},
            'model': {'audio': {'enabled': True, 'maxPerMessage': 1, **(audio or {})}},
        })
        host.buffered_narrative_turns = {'k': {
            'storyId': 's', 'participantId': 'p',
            'messages': [{
                'content': '听听', 'occurredAt': NOW,
                'imageSources': [], 'audioSources': self._sources(count),
            }],
            'latestSession': SessionView(
                platform='onebot', self_id='1', user_id='u', channel_id='private:u',
                content='听听', media=[],
            ),
            'timer': None, 'nextRevision': 3, 'inFlightRequestId': None,
            'obsoleteRequestIds': set(),
        }}
        return host

    async def _flush(self, count: int, *, audio: Any = None) -> Any:
        host = self._host(count, audio=audio)
        await ServiceChunk3.flush_buffered_narrative(host, 'k', 3)
        return host

    @staticmethod
    def _message(host: Any) -> str:
        return host.calls['try_decide'][5]

    @staticmethod
    def _audio(host: Any) -> Any:
        """`tryDecide` 的第 11 个位置参数 = 本回合进 payload 的原生音频。"""
        return host.calls['try_decide'][10]

    @staticmethod
    def _warns(host: Any) -> list[str]:
        return [
            entry[-1] for entry in host.logs
            if isinstance(entry, tuple) and entry and isinstance(entry[-1], str)
            and '每个事件音频数上限' in entry[-1]
        ]

    async def test_the_clue_counts_what_was_cut(self) -> None:
        host = await self._flush(3)
        self.assertEqual(len(self._audio(host)), 1, '默认上限就是 1 段')
        self.assertIn('[音轨×3，本回合仅取前 1 段]', self._message(host),
                      '可数线索必须给模型：一共几段、给了几段')
        warns = self._warns(host)
        self.assertEqual(len(warns), 1, '丢内容必须看得见（坑 25）')
        self.assertIn('本回合收到 3 段音轨', warns[0])
        self.assertIn('只交给模型前 1 段', warns[0])

    async def test_raising_the_limit_really_reads_more(self) -> None:
        host = await self._flush(3, audio={'maxPerMessage': 2})
        self.assertEqual(len(self._audio(host)), 2)
        self.assertIn('[音轨×3，本回合仅取前 2 段]', self._message(host))
        self.assertIn('只交给模型前 2 段', self._warns(host)[0])

    async def test_no_clue_when_nothing_was_cut(self) -> None:
        host = await self._flush(1)
        self.assertEqual(len(self._audio(host)), 1)
        self.assertNotIn('[音轨×', self._message(host))
        self.assertEqual(self._warns(host), [])

    async def test_master_switch_off_is_not_a_budget_cut(self) -> None:
        host = await self._flush(3, audio={'enabled': False})
        self.assertEqual(self._audio(host), [], '总开关关着 = 整条语音通道不读')
        self.assertNotIn('[音轨×', self._message(host), '关着时不许说"仅取前 0 段"')
        self.assertEqual(self._warns(host), [])

    def test_the_helper_is_the_single_judgement(self) -> None:
        """切片与线索共用一处：`audio_turn_slice` 的数就是线索里的数。"""
        from plugin.core.service.chunk3 import (  # noqa: PLC0415
            audio_turn_budget_note, audio_turn_slice,
        )

        host = _MediaHost(config={'model': {'audio': {'enabled': True, 'maxPerMessage': 1}}})
        sources = self._sources(3)
        to_take, available, granted = audio_turn_slice(host, sources)
        self.assertEqual((len(to_take), available, granted), (1, 3, 1))
        self.assertEqual(audio_turn_budget_note(available, granted), '[音轨×3，本回合仅取前 1 段]')
        self.assertEqual(audio_turn_budget_note(1, 1), '')
        self.assertEqual(audio_turn_slice(host, []), ([], 0, 0))


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

    async def test_the_master_switch_blocks_loading_even_with_the_stt_switch_on(self) -> None:
        """（关音频 / 开转写）：总开关关着时**一条音频都不加载**，转写开关开着也没用。

        两个开关的分工：`enabled` 决定"语音要不要当音频证据加载"，`stt_enabled` 决定
        "加载之后要不要调转写模型"。总开关在前——这一条不成立的话，用户配好的转写模型
        会被"看不见的闸"绕过（或者反过来，语音静默进了主模型）。
        """
        host = _MediaHost(config={'model': {'audio': {
            'enabled': False, 'stt_enabled': True, 'maxPerMessage': 3,
        }}})
        host.transport = FakeTransport(fetch_audio=self._fetch(b'ID3\x04\x00\x00'))
        sources = ['data:audio/mp3;base64,QUJD'] * 3
        self.assertEqual(
            await ServiceChunk3.load_native_audio(host, {'id': 's'}, sources), [],
            '总开关关着 = 没有音频证据，转写开关开着也无从转起',
        )
        self.assertEqual(
            await _load_group_batch_audio(host, {'id': 's'}, [{'audio_sources': sources}]),
            [],
        )

    async def test_the_master_switch_on_keeps_todays_loading_behaviour(self) -> None:
        """（开音频 / 关转写）：加载照旧（要不要转写由适配层那一侧决定，core 不参与）。"""
        host = _MediaHost(config={'model': {'audio': {
            'enabled': True, 'stt_enabled': False, 'maxPerMessage': 2,
        }}})
        host.transport = FakeTransport(fetch_audio=self._fetch(b'ID3\x04\x00\x00'))
        audio = await ServiceChunk3.load_native_audio(
            host, {'id': 's'}, ['data:audio/mp3;base64,QUJD', 'data:audio/mp3;base64,QUJD'],
        )
        self.assertEqual([item['id'] for item in audio], ['turn-audio-1', 'turn-audio-2'])
        self.assertTrue(all('base64' in item for item in audio))

    async def test_the_group_batch_budget_caps_count_and_bytes(self) -> None:
        """上游 2197-2215 的**群音频批次预算**（v1.7.6 补上的 `audioConfig` 消费点）。

        条数上限 = `maxPerMessage × 4`、字节上限 = `maxFileSizeMB × 4MB`；超出的部分
        延后 / 跳过，各留一条 warn。少了这道闸，一个群里连发语音会把上下文一次塞满。
        """
        payload = b'ID3\x04\x00\x00' * 4
        host = _MediaHost(config={'model': {'audio': {
            'enabled': True, 'maxPerMessage': 1, 'maxFileSizeMB': 1,
        }}})
        host.transport = FakeTransport(fetch_audio=self._fetch(payload))
        batch = [{'audio_sources': ['data:audio/mp3;base64,QUJD']} for _ in range(6)]

        audio = await _load_group_batch_audio(host, {'id': 's'}, batch)

        # maxPerMessage=1 → 条数上限 4；每条几百字节，远小于 4MB，所以先撞条数。
        self.assertEqual([item['id'] for item in audio],
                         ['group-audio-1', 'group-audio-2', 'group-audio-3', 'group-audio-4'])
        self.assertTrue(
            any('群音频批次达到资源上限' in str(entry) for entry in host.logs),
            host.logs,
        )

    async def test_the_group_batch_budget_stops_on_bytes_and_says_so(self) -> None:
        """字节上限那条分支：一条音频就超预算时**跳过它**并留 warn（不是静默丢掉）。"""
        host = _MediaHost(config={'model': {'audio': {
            'enabled': True, 'maxPerMessage': 3, 'maxFileSizeMB': 1,
        }}})

        async def fake_load(story, sources, session=None, max_count=None):
            return [{'format': 'mp3', 'base64': 'A' * (4 * 1_000_000 + 10)}]

        host.load_native_audio = fake_load  # type: ignore[method-assign]
        audio = await _load_group_batch_audio(host, {'id': 's'}, [{'audio_sources': ['x']}])
        self.assertEqual(audio, [])
        self.assertTrue(any('群音频批次达到字节上限' in str(entry) for entry in host.logs), host.logs)

    async def test_the_group_batch_never_touches_audio_when_the_master_switch_is_off(self) -> None:
        """总开关关着：群批次连平台都不问（不加载、不留 warn）。"""
        host = _MediaHost(config={'model': {'audio': {'enabled': False, 'maxPerMessage': 3}}})
        host.transport = FakeTransport(fetch_audio=self._fetch(b'ID3\x04\x00\x00'))
        audio = await _load_group_batch_audio(
            host, {'id': 's'}, [{'audio_sources': ['data:audio/mp3;base64,QUJD']}],
        )
        self.assertEqual(audio, [])
        self.assertEqual(host.logs, [])

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


class LocalImageSourceTests(unittest.TestCase):
    """适配器把图片落成本地文件时的图片源识别（v1.2.13）。

    上游 Koishi 的 `<img>` 永远带 CDN 地址，所以 `add()` 只认 http/data；AstrBot 的
    NapCat 适配器可能给**本地路径**。丢掉它 → `describeVisionEvent` 得到"既无文字也无来源"
    → `receive` 的视觉门把整条消息判死（用户实测：图片消息全部不产生回合）。
    ⚠️ 本地路径**只认适配器给的元素**——正文是用户可控的，认它等于开一个本地读文件的口子。
    """

    def _sources(self, content, elements):
        session = SessionView(platform='onebot', self_id='1', user_id='2',
                              content=content, elements=elements)
        return chunk3._fallback_extract_session_image_sources(session)

    def test_adapter_provided_local_path_is_kept(self):
        for path, expected in (('file:///AstrBot/data/temp/napcat/a.jpg',
                                '/AstrBot/data/temp/napcat/a.jpg'),
                               ('/tmp/napcat/b.png', '/tmp/napcat/b.png')):
            content = '<img src="%s"/>' % path
            elements = [{'type': 'img', 'attrs': {'src': path}, 'children': []}]
            # `file://` 前缀会被归一成文件系统路径（读取时要用的就是它）。
            self.assertEqual(self._sources(content, elements), ['onebot-file:%s' % expected])

    def test_local_path_in_user_text_is_refused(self):
        # 任何人都能在正文里写这个；认了就等于让模型读本地文件。
        self.assertEqual(self._sources('<img src="/etc/passwd"/>', []), [])
        self.assertEqual(self._sources('<img src="file:///etc/passwd"/>', []), [])

    def test_http_sources_are_recorded_but_inert(self):
        """正文里的 http 来源仍被记下（`imageCount` / 附件条目照旧），但带 `text:` 惰性前缀。

        受控偏离 §46.8：上游这里给的是**裸 URL**（`[CQ:image,…]` 更是 `onebot-url:`，
        取回时跳过主机白名单）—— 那正是"手打的正文能让她下载任意地址"的入口。
        条目还在，只是永不取回（`fetch_native_image()` 见到 `text:` 直接回 None）。
        """
        url = 'https://multimedia.nt.qq.com.cn/download?appid=1406&fileid=x'
        self.assertEqual(self._sources('<img src="%s"/>' % url, []), ['text:%s' % url])
        self.assertEqual(
            self._sources('<img src="%s"/>' % url,
                          [{'type': 'img', 'attrs': {'src': url}, 'children': []}]),
            ['text:%s' % url],
        )

    def test_text_file_tokens_are_inert_while_adapter_elements_stay_trusted(self):
        """正文里的 `file=` token 惰性化；`session.elements`（适配器直给）仍是可信坐标。"""
        content = '<img src="file:///tmp/a.png"/><img file="/tmp/b.png"/>'
        elements = [{'type': 'img', 'attrs': {'src': 'file:///tmp/a.png'}, 'children': []}]
        session = SessionView(
            platform='onebot', self_id='1', user_id='2', content=content, elements=elements,
        )
        self.assertEqual(
            chunk3._fallback_extract_session_image_sources(session),
            ['onebot-file:/tmp/a.png', 'text:/tmp/b.png'],
        )


class ForwardedMediaPipelineTests(unittest.TestCase):
    """转发媒体（v1.8.7）走到 core 这一侧的三个出口：来源 / 附件 / 收藏原料。

    适配层把转发节点的图并进 `SessionView.media` 之后，**下游一行代码都没改**：
    这里钉的就是"同一份数据真的贯通了"，而不是新造一条链路（§46/§52）。
    """

    #: 15 张转发来的图（与真机那条 17 节点 / 15 图同形）。
    MEDIA = [
        {'kind': 'image' if index else 'sticker', 'source': 'https://gchat.qpic.cn/ft/%d' % index,
         'source_kind': 'url', 'summary': '[中午好]' if not index else '', 'raw': {}}
        for index in range(15)
    ]

    def _session(self, media):
        return SessionView(platform='onebot', self_id='1', user_id='2',
                           content='<forward id="res-1"/>', media=media)

    def test_forwarded_images_become_visual_sources(self):
        """视觉来源表 = 媒体表的规范化形式；`currentEvent.imageCount` 取的就是它。"""
        sources = chunk3._extract_session_image_sources(self._session(self.MEDIA))
        self.assertEqual(sources, [item['source'] for item in self.MEDIA])
        self.assertEqual(len(sources), 15, '预算在**上游**（适配层）就截断了，这里如实透传')

    def test_empty_forward_media_keeps_the_session_media_table(self):
        """转发里没有图：表照旧是 `[]`（观测到零媒体），一行的行为都不变。"""
        self.assertEqual(chunk3._extract_session_media(self._session([])), [])
        self.assertEqual(chunk3._extract_session_image_sources(self._session([])), [])

    def test_forwarded_stickers_keep_their_kind_and_label(self):
        """转发来的收藏表情走**同一套**判据：种类照旧、标签照旧（判据只有一处）。"""
        media = chunk3._extract_session_media(self._session(self.MEDIA))
        self.assertEqual(media[0]['kind'], 'sticker')
        self.assertEqual(media[0]['label'], '[表情包]')
        self.assertEqual(media[1]['kind'], 'image')
        self.assertEqual(media[1]['label'], '[图片]')


class TestVisionHelpers(unittest.IsolatedAsyncioTestCase):

    def test_describe_vision_event_strips_attachment_markup_and_keeps_sources(self) -> None:
        """正文里的来源照旧进 `sources`（`imageCount` 与附件条目不变），但带 `text:` 惰性前缀。"""
        host = FakeService()
        event = ServiceChunk3.describe_vision_event(host, {
            'content': '看这个<img src="https://gchat.qpic.cn/a.png"/>好看吗[CQ:record,file=v.silk]',
        })
        self.assertEqual(event['content'], '看这个好看吗')
        self.assertEqual(event['sources'], ['text:https://gchat.qpic.cn/a.png'])

    def test_describe_vision_event_keeps_image_only_input_wordless(self) -> None:
        """上游注释：抓取失败/被过滤必须表现为「没有视觉输入」，不邀请模型编造。"""
        host = FakeService()
        event = ServiceChunk3.describe_vision_event(host, {'content': '<img src="https://gchat.qpic.cn/a.png"/>'})
        self.assertEqual(event['content'], '')
        self.assertEqual(event['sources'], ['text:https://gchat.qpic.cn/a.png'])

    def test_extract_session_image_sources_structured_first_then_inert_text(self) -> None:
        """§46.8：表在 → **只读结构化**；表不在（`None`/缺失）→ 文本降级但全部惰性。"""
        # 表在：正文里的三种写法**一条都不进来源表**（种类与来源都只认观测到的那份）。
        structured = SessionView(
            platform='onebot', self_id='1', user_id='2',
            content=('<img src="https://gchat.qpic.cn/real.png"/>'
                     '<img src="https://gchat.qpic.cn/typed.png"/>'
                     '[CQ:image,url=https://gchat.qpic.cn/cq.png]'),
            media=[{'kind': 'image', 'source': 'https://gchat.qpic.cn/real.png',
                    'source_kind': 'url', 'summary': '', 'raw': {}}],
        )
        self.assertEqual(
            chunk3._extract_session_image_sources(structured),
            ['https://gchat.qpic.cn/real.png'],
        )
        # 空表也算"表在"：观测到零媒体 → 不回退文本。
        self.assertEqual(
            chunk3._extract_session_image_sources(
                SessionView(content='<img src="https://x/b.png"/>', media=[]),
            ),
            [],
        )

        # 没有观测通道（老宿主 / 手搓 session）→ 上游那套文本抽取，坐标一律 `text:`。
        self.assertEqual(
            # 上游 `add(fields.url || fields.cache_url, 'adapter-url')` → 带 onebot-url: 前缀
            chunk3._extract_session_image_sources({'content': '[CQ:image,file=a.jpg,url=https://x/a.jpg]'}),
            ['text:https://x/a.jpg'],
        )
        self.assertEqual(
            chunk3._extract_session_image_sources({'content': '[CQ:image,file=abc.jpg]'}),
            ['text:abc.jpg'],
        )
        self.assertEqual(
            chunk3._extract_session_image_sources({'content': '<img src="https://x/b.png"/><img src="https://x/b.png"/>'}),
            ['text:https://x/b.png'],
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

    async def test_text_sources_are_never_fetched_even_on_trusted_hosts(self) -> None:
        """红线（§46.8）：`text:`（正文里读出来的坐标）**永不取回**，连主机白名单都不认。

        这条钉的是"来源"这一半的洞：从前正文里的 `[CQ:image,url=…]` 会被记成
        `onebot-url:`（= 适配器提供，取回时跳过白名单），手打一句就能让她下载任意地址；
        裸 URL 也会因为 QQ CDN 白名单被取回。
        """
        seen: list[str] = []
        png = _png_bytes()
        host = _MediaHost(config={'model': {'vision': {'enabled': True}}})

        async def fetch(url: str) -> bytes:
            seen.append(url)
            return png

        host.transport = FakeTransport(fetch_image=fetch)
        for source in (
            'text:https://gchat.qpic.cn/a.png',       # 可信主机也不认
            'text:https://multimedia.nt.qq.com.cn/b.png',
            'text:https://example.com/c.png',
            'text:/tmp/secret.png',                   # 也没有本地读文件的口子
        ):
            with self.subTest(source=source):
                self.assertIsNone(await ServiceChunk3.fetch_native_image(host, source))
        self.assertEqual(seen, [], '正文里的坐标一次都不许取回')

        # 正向对照：适配器观测到的同一批坐标照旧取回（白名单 / 适配器标记不变）。
        self.assertEqual(
            (await ServiceChunk3.fetch_native_image(host, 'https://gchat.qpic.cn/a.png'))['mime_type'],
            'image/png',
        )
        self.assertEqual(
            (await ServiceChunk3.fetch_native_image(host, 'onebot-url:https://example.com/a.png'))['mime_type'],
            'image/png',
        )
        self.assertEqual(seen, ['https://gchat.qpic.cn/a.png', 'https://example.com/a.png'])

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
            # 每条 URL 给一份**不同**的字节：v1.4.0 起完全相同的图会在回合内去重
            # （见下面的 `test_the_same_image_in_one_message_is_only_kept_once`），
            # 这里测的是"来源上限"，所以让三张图各不相同。
            return png + url.encode('utf-8')

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

    async def test_the_same_image_in_one_message_is_only_kept_once(self) -> None:
        """v1.4.0：同一条消息里重复贴同一张图只留一张（识图按图计费）。"""
        from PIL import Image  # noqa: F401 - 没有 Pillow 时下面会跳过

        import io as _io

        buffer = _io.BytesIO()
        Image.new('L', (32, 32), 0).save(buffer, 'PNG')
        pattern = Image.open(_io.BytesIO(buffer.getvalue()))
        for x in range(0, 32, 8):
            for y in range(32):
                pattern.putpixel((x, y), 255)
        buffer = _io.BytesIO()
        pattern.save(buffer, 'PNG')
        png = buffer.getvalue()
        host = _MediaHost(config={'model': {'vision': {'enabled': True}}})

        async def fetch(url: str) -> bytes:
            return png

        host.transport = FakeTransport(fetch_image=fetch)
        images = await ServiceChunk3.load_native_images(
            host, {'id': 's'}, ['https://gchat.qpic.cn/1.png', 'https://gchat.qpic.cn/2.png'], None,
        )
        self.assertEqual([item['id'] for item in images], ['turn-image-1'],
                         '第二张是同一张图，应当被跳过')
        self.assertTrue(images[0].get('perceptualHash'), '留下的那张要带哈希')

    async def test_describe_current_images_skips_without_a_vision_provider(self) -> None:
        host = _MediaHost(config={})

        class Describer:
            def available(self) -> bool:
                return False

        host.vision_describer = Describer()
        self.assertIsNone(await ServiceChunk3.describe_current_images(host, {'id': 's'}, [{'id': 'i'}], '看看'))
        self.assertTrue(any('侧端识图跳过' in str(entry) for entry in host.logs))

    def test_extract_session_media_reads_the_structured_table_only(self) -> None:
        """媒体种类必须和图片来源对齐，并带上卡片（受控偏离 §29）。

        输入是适配层写下的**结构化媒体表**（`SessionView.media`，§46）：
        `kind`/`summary` 来自 OneBot 原始段，`raw` 是原始判据，卡片没有来源。
        """
        media = chunk3._extract_session_media({
            'media': [
                {'kind': 'sticker', 'source': 'https://x/a.png', 'source_kind': 'url',
                 'summary': '[动画表情]', 'raw': {'sub_type': '1'}},
                {'kind': 'card', 'source': '', 'source_kind': '', 'summary': '',
                 'raw': {'app': 'com.tencent.miniapp', 'title': '宝箱'}},
            ],
            'content': '<img src="https://x/a.png" kind="sticker" summary="[动画表情]"/>'
                       '<card app="com.tencent.miniapp" title="宝箱"/>',
        })
        self.assertEqual(media[0]['source'], 'https://x/a.png')
        self.assertEqual(media[0]['kind'], 'sticker')
        self.assertEqual(media[0]['label'], '[动画表情]')
        self.assertEqual(media[1]['kind'], 'card')
        self.assertEqual(media[1]['label'], '[QQ小程序：宝箱]')

    def test_extract_session_media_never_reads_the_message_text(self) -> None:
        """红线（§46）：正文里手打的 `<img … kind="sticker"/>` **一个都不算数**。

        内容与上一条**逐字相同**，区别只在没有结构化媒体表。判据曾经是从这段文本里
        正则抠出来的 —— 那样谁都能手打一个 `<img src="http://任意地址" kind="sticker"/>`
        冒充表情包（会真的去下载那个地址）。取不到结构化数据必须回**空表**，
        绝不回退解析文本。
        """
        content = ('<img src="https://evil.example/x.png" kind="sticker"/>'
                   '<img src="https://evil.example/x.png" kind="animated"/>'
                   '<card app="com.tencent.miniapp" title="宝箱"/>')
        for session in (
            {'content': content, 'elements': []},
            {'content': content},
            SessionView(platform='onebot', self_id='1', user_id='2', content=content),
            # 老宿主 / 结构缺失：`media` 不是列表也一律按"没有"处理。
            {'content': content, 'media': None},
            {'content': content, 'media': 'sticker'},
        ):
            with self.subTest(session=session):
                self.assertEqual(chunk3._extract_session_media(session), [])

    def test_vision_event_keeps_cards_as_text(self) -> None:
        """卡片是可读内容，留成标签；图片标记照旧拿掉（上游语义）。"""
        host = _MediaHost(config={})
        event = ServiceChunk3.describe_vision_event(host, {
            'content': '看这个<img src="https://x/a.png"/>'
                       '<card app="com.tencent.miniapp" title="宝箱"/>',
            'elements': [],
        })
        self.assertNotIn('[图片]', event['content'])
        self.assertNotIn('<card', event['content'])
        self.assertIn('[QQ小程序：宝箱]', event['content'])
        self.assertEqual(event['sources'], ['text:https://x/a.png'])

    async def test_describe_current_images_returns_observations(self) -> None:
        host = _MediaHost(config={'model': {'vision': {'detail': 'low'}}})

        class Describer:
            def __init__(self) -> None:
                self.seen: Any = None

            def available(self) -> bool:
                return True

            async def describe_images(self, images: Any, user_text: str = '', detail: str = 'auto',
                                      kinds: Any = None) -> Any:
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

            async def describe_images(self, images: Any, user_text: str = '', detail: str = 'auto',
                                      kinds: Any = None) -> Any:
                raise RuntimeError('provider down')

        host.vision_describer = Describer()
        self.assertIsNone(await ServiceChunk3.describe_current_images(host, {'id': 's'}, [{'id': 'i'}], None))
        self.assertTrue(any('侧端识图失败' in str(entry) for entry in host.logs))

    # ---- `visible`（v1.9.9）：群回合要看得见，私聊一个字都不变 ----

    @staticmethod
    def _failure_host(error: Any = None) -> Any:
        """一个识图失败的 host（`error=None` = 没有可用的侧端连接）。"""
        host = _MediaHost(config={})

        if error is None:
            class Describer:
                def available(self) -> bool:
                    return False
        else:
            class Describer:
                def available(self) -> bool:
                    return True

                async def describe_images(self, images: Any, user_text: str = '',
                                          detail: str = 'auto', kinds: Any = None) -> Any:
                    raise error
        host.vision_describer = Describer()
        return host

    async def test_describe_current_images_keeps_the_diagnostic_channel_by_default(self) -> None:
        """**反向（私聊不变）**：不传 `visible` 时报告走 `report_operation('diagnostic')`。

        私聊那条路（`chunk3.flush_buffered_narrative`）不传它，所以日志与
        v1.9.9 逐字一致；本用例把"没有可见 warn"钉住，`visible` 一旦漏进私聊这条红。
        """
        for error in (None, RuntimeError('provider down')):
            with self.subTest(error=error):
                host = self._failure_host(error)
                await ServiceChunk3.describe_current_images(host, {'id': 's'}, [{'id': 'i'}], '看看')
                self.assertEqual(
                    [entry for entry in host.logs if entry[0] == 'report'], [],
                    '默认（私聊）不许走可见 warn 频道',
                )
                self.assertTrue(
                    any(entry[0] == 'operation' and entry[1] == 'diagnostic'
                        for entry in host.logs),
                    host.logs,
                )

    async def test_describe_current_images_reports_visibly_when_the_group_asks(self) -> None:
        """`visible=True`（群回合）：三条失败路径都变成**可见 + 可行动**的 warn。"""
        cases = (
            (None, '侧端识图没有可用的视觉连接'),
            (RuntimeError('provider down'), '侧端识图失败'),
        )
        for error, expected in cases:
            with self.subTest(error=error):
                host = self._failure_host(error)
                await ServiceChunk3.describe_current_images(
                    host, {'id': 's'}, [{'id': 'i'}], '看看', True,
                )
                visible = [entry for entry in host.logs if entry[0] == 'report']
                self.assertEqual(len(visible), 1, host.logs)
                self.assertEqual(visible[0][1], 'warn')
                self.assertIn(expected, visible[0][3])
                # 可行动：点名去哪调。
                self.assertIn('模型中心 → 模型连接', visible[0][3])
                self.assertIn('用于侧端识图', visible[0][3])
                if error is not None:
                    self.assertIn('provider down', visible[0][3], '错误原文要留着')

    async def test_describe_current_images_reports_visibly_when_nothing_comes_back(self) -> None:
        """`visible=True` + 识图返回空 → 同样是可见、可行动的 warn（不是静默）。"""
        host = _MediaHost(config={})

        class Describer:
            def available(self) -> bool:
                return True

            async def describe_images(self, images: Any, user_text: str = '',
                                      detail: str = 'auto', kinds: Any = None) -> Any:
                return []

        host.vision_describer = Describer()
        result = await ServiceChunk3.describe_current_images(
            host, {'id': 's'}, [{'id': 'i'}], '看看', True,
        )
        self.assertFalse(result)
        visible = [entry for entry in host.logs if entry[0] == 'report']
        self.assertEqual(len(visible), 1)
        self.assertIn('侧端识图没有返回观察结果', visible[0][3])
        self.assertIn('模型中心 → 模型连接', visible[0][3])

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
