"""`plugin/core/service/chunk1.py` 的单元测试（stdlib `unittest`，零依赖）。

对应上游 `upstream/src/service.ts:1248-1928`（`ServiceChunk1`）。

上游测试的归属核对（重要，先说清楚，避免"假绿"）
------------------------------------------------
任务点名的 5 个上游测试文件里，**没有一个用例断言声明起始行落在 [1248,1928) 的成员**：

| 上游测试 | 断言的对象 | 起始行 | 归属 |
| --- | --- | --- | --- |
| `group-identity.test.ts` | `formatGroupSpeaker` / `normalizeGroupChatActions` / `calibratedNativeFaceWillingness` / `describeQuotedMessage` / `repairCanonicalOneBotStoryTransport` / `sendGroupMessage` / `confirmOutgoingDeliveries` | 1918 / 5253 / 6903 / 2147 | Helpers / Chunk2 / Chunk6 / Chunk9 |
| `sticker-vision-helpers.test.ts` | `rankStickerCatalog` / `shouldDownscaleImage` / `stableStickerAssetId` / `SEMANTIC_STICKER_LIMIT` | 1805 / 1826 / 587 | Helpers |
| `delivery-boundary.test.ts` | `src/delivery.ts` 的 4 个函数 | — | `core/delivery.py` |
| `streaming-reply.test.ts` | `extractEarlyNarrativeReply` / `systemPrompt` / `typingDelayMilliseconds` | 5379 | `narrator` / Chunk6 |
| `group-willingness.test.ts` | `evaluateGroupWillingness` / `consumeGroupWillingness` | `group-willingness.ts` | `core/group_willingness.py` |

因此本文件做两件事，二者都写在测试名里，绝不冒充上游断言：

1. `UpstreamBehaviourPortTests`：把上述文件里**行为后果落在本块成员身上**的用例，
   在本层（`receiveGroup` / `bufferGroupMessage` / `flushGroupTurn`）重放——
   群身份标签、意愿门控、投递结果记账。每条都标注上游文件与用例名。
2. `Chunk1*Tests`：本块 24 个成员自己的用例（上游没有直接测它们，但断言逐条对应
   上游源码的行为：边界值、排序键、状态机分支、软删占位符）。

守卫
----
所有集成用例都用 `@needs(...)`（`unittest.skipUnless` + `InterludeService` 上该方法
可调用）守门：其它分块尚未落地时自动 skip，而不是失败或假绿。
尚未移植的兄弟 chunk 成员（Chunk6/7 的 `record_incoming_message` 等）在本文件里
用**显式测试替身**替换，并在用例 docstring 里点名。

运行：
    cd /home/kela/文档/harness/hds-interlude && python3 -m unittest plugin.tests.test_service_chunk1 -v
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from plugin.core import logging as interlude_logging
from plugin.core.database import Database
from plugin.core.service.session import SessionView
from plugin.core.service.transport import NullTransport
from plugin.core.time import dt_ms

try:  # pragma: no cover - 取决于并行分块的落地顺序
    from plugin.core.service import InterludeService
    from plugin.core.service.chunk1 import (
        ServiceChunk1,
        _chat_capabilities_wire,
        _clear_database_fallback,
        _group_context_messages,
        _group_message_ref,
        _mentions_bot,
        _quotes_bot,
        _same_platform_family,
        _targetable_message_id,
    )
    _IMPORT_ERROR: Optional[BaseException] = None
except Exception as exc:  # pragma: no cover
    InterludeService = None  # type: ignore[assignment]
    ServiceChunk1 = None  # type: ignore[assignment]
    _IMPORT_ERROR = exc


def needs(*names: str) -> Any:
    """`unittest.skipUnless`：`InterludeService` 可导入且被测方法存在才运行。"""
    if InterludeService is None:
        return unittest.skipUnless(False, '依赖 core/service 其它分块：%s' % (_IMPORT_ERROR,))
    missing = [name for name in names if not callable(getattr(InterludeService, name, None))]
    return unittest.skipUnless(not missing, '依赖 core/service 其它分块：缺少 %s' % (missing,))


# =========================================================================== #
# 夹具
# =========================================================================== #

#: 固定时钟：所有时间断言都相对它（`InterludeContext(clock=...)` 注入）。
STORY_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)
STORY_MS = dt_ms(STORY_TIME)

PRIVATE_STORY_ID = 'onebot:1:2'

#: 共享主剧本（上游 `sharedStoryConfig.enabled` **硬编码 true**）下，私聊剧本的 canonical id。
#: `PRIVATE_STORY_ID` 是旧 beta 的"按账号"id：该账号首次到访时会被 `migrateLegacyStory`
#: 迁移成这个 id（旧的按账号 id 行转 `archived`），所以断言要用迁移**之后**的 id。
SHARED_STORY_ID = 'character:onebot:1'


def make_config(**overrides: Any) -> dict[str, Any]:
    """一份最小可用配置（只含本块读到的段，其余走默认值/缺省分支）。"""
    config: dict[str, Any] = {
        'model': {},
        'runtime': {},
        'storyDefaults': {},
        'memory': {},
        'onebot': {},
        'sharedStory': {},
        'logging': {'level': 'debug', 'verbosity': 'diagnostic', 'format': 'layered'},
        'chatActions': {},
        'stickers': {},
        'audio': {},
    }
    config.update(overrides)
    return config


def onebot_config(onebot: Optional[dict[str, Any]] = None, **overrides: Any) -> dict[str, Any]:
    """名单配置（v1.3.0 起没有总闸：要"只处理名单内"就打开对应的 `*_only`）。"""
    section: dict[str, Any] = {
        'botAccountsOnly': True,
        'botAccounts': [{'qq': '1'}],
        'userAccountsOnly': True,
        'userAccounts': [{'qq': '2'}],
        'groupChats': [],
    }
    section.update(onebot or {})
    return make_config(onebot=section, **overrides)


def group_rule_stub(**overrides: Any) -> dict[str, Any]:
    """一条群规则（键名照抄 AstrBot 配置里的 camelCase，读侧双读）。"""
    rule: dict[str, Any] = {
        'groupId': '9',
        'label': '测试群',
        # `ServiceChunk0.can_handle_group_session` 要求规则上 `enabled` 为真（非缺省即启用）。
        'enabled': True,
        'purpose': '',
        'characterRole': '',
        'responseMode': 'always',
        'contextLimit': 20,
        'debounceSeconds': 600,
        'cooldownSeconds': 0,
        'willingness': {'enabled': False},
    }
    rule.update(overrides)
    return rule


class _Sink:
    """把分层日志收进内存，避免测试输出噪音。"""

    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def __call__(self, level: str, text: str) -> None:
        self.records.append((level, text))

    def text(self) -> str:
        return '\n'.join(text for _level, text in self.records)


class ServiceHarness(unittest.IsolatedAsyncioTestCase):
    """共享夹具：内存数据库 + 固定时钟 + 内存日志 sink + 可控 transport。

    **继承 `IsolatedAsyncioTestCase`**（而不是裸 `TestCase`）：本文件绝大多数成员是
    `async def`，在裸 `TestCase` 下协程根本不会被 await —— 那会得到一批"假绿"。
    同步用例在 `IsolatedAsyncioTestCase` 下照常运行。
    """

    def setUp(self) -> None:
        self.sink = _Sink()
        interlude_logging.set_log_sink(self.sink)
        self.addCleanup(interlude_logging.set_log_sink, interlude_logging._default_sink)
        self.db = Database(':memory:')
        self.addCleanup(self.db.close)
        self.db.register_tables()
        self.clock = {'ms': STORY_MS}

    # ---- 构造 ----

    def make_service(self, config: Optional[dict[str, Any]] = None, transport: Any = None) -> Any:
        if InterludeService is None:  # pragma: no cover - 由 @needs 守门
            self.skipTest('依赖 core/service 其它分块：%s' % (_IMPORT_ERROR,))
        from plugin.core.service import InterludeContext
        ctx = InterludeContext(
            logger=None, database=self.db,
            clock=lambda: self.clock['ms'], random=lambda: 0.5,
        )
        service = InterludeService(ctx, config or make_config(), self.db, transport or NullTransport())
        self.addCleanup(self._shutdown, service)
        return service

    @staticmethod
    def _shutdown(service: Any) -> None:
        for name in ('_sweep_timer', '_compaction_timer', '_blind_mode_timer', '_sticker_scan_timer'):
            timer = getattr(service, name, None)
            if timer is not None:
                try:
                    timer.cancel()
                except Exception:  # pragma: no cover
                    pass
        for turn in list(service.buffered_group_turns.values()):
            timer = turn.get('timer')
            if timer:
                try:
                    timer.cancel()
                except Exception:  # pragma: no cover
                    pass
        for turn in list(service.buffered_narrative_turns.values()):
            timer = turn.get('timer')
            if timer:
                try:
                    timer.cancel()
                except Exception:  # pragma: no cover
                    pass

    # ---- 造数据 ----

    def insert(self, table: str, **data: Any) -> dict[str, Any]:
        return self.db.insert(table, data)

    def make_story(self, story_id: str = PRIVATE_STORY_ID, **overrides: Any) -> dict[str, Any]:
        story: dict[str, Any] = {
            'id': story_id, 'platform': 'onebot', 'selfId': '1', 'userId': '2',
            'channelId': '', 'status': 'active',
            'setting': {
                'character': {'name': 'Yukiyo', 'profile': ''},
                'user': {'display_name': '', 'profile': ''},
                'relationship': '', 'world': '', 'perspective': '', 'supporting_cast': '',
                'location': '', 'style': '', 'timezone': 'Asia/Shanghai',
            },
            'state': {
                'schema_version': 1,
                'setting_overlay': {'character_traits': []},
                'automation': {}, 'narrative_update_count': 0,
            },
            'cursorAt': STORY_TIME, 'createdAt': STORY_TIME, 'updatedAt': STORY_TIME,
        }
        story.update(overrides)
        return self.insert('interlude_story', **story)

    def make_entry(
        self,
        story_id: str = PRIVATE_STORY_ID,
        kind: str = 'user-message',
        content: str = 'x',
        occurred_at: Optional[datetime] = None,
        entry_id: Optional[int] = None,
        participant_id: str = '',
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        data: dict[str, Any] = {
            'storyId': story_id, 'participantId': participant_id, 'kind': kind, 'actor': 'user',
            'content': content, 'occurredAt': occurred_at or STORY_TIME,
            'metadata': metadata if metadata is not None else {}, 'createdAt': STORY_TIME,
        }
        if entry_id is not None:
            data['id'] = entry_id
        return self.insert('interlude_script_entry', **data)

    def make_memory(
        self, story_id: str = PRIVATE_STORY_ID, importance: float = 1, participant_id: str = '',
        updated_at: Optional[datetime] = None, status: str = 'active', content: str = 'mem',
    ) -> dict[str, Any]:
        return self.insert(
            'interlude_memory', storyId=story_id, participantId=participant_id, category='note',
            content=content, importance=importance, status=status, sourceEntryId=None,
            createdAt=STORY_TIME, updatedAt=updated_at or STORY_TIME,
        )

    def make_fact(
        self, story_id: str = PRIVATE_STORY_ID, status: str = 'active', updated_at: Optional[datetime] = None,
        content: str = 'fact', source_entry_ids: Optional[list[int]] = None,
        created_at: Optional[datetime] = None, last_seen_at: Optional[datetime] = None,
    ) -> dict[str, Any]:
        return self.insert(
            'interlude_fact', storyId=story_id, participantId='', scope='world', content=content,
            importance=0.5, confidence=1, unresolved=False, status=status,
            sourceEntryIds=source_entry_ids or [], lastSeenAt=last_seen_at or STORY_TIME,
            createdAt=created_at or STORY_TIME, updatedAt=updated_at or STORY_TIME,
        )

    def make_intent(
        self, story_id: str = PRIVATE_STORY_ID, status: str = 'pending',
        not_before: Optional[datetime] = None, created_at: Optional[datetime] = None,
        updated_at: Optional[datetime] = None, summary: str = 'intent',
    ) -> dict[str, Any]:
        return self.insert(
            'interlude_intent', storyId=story_id, participantId='', type='follow-up',
            summary=summary, notBefore=not_before or STORY_TIME, status=status, payload={},
            createdAt=created_at or STORY_TIME, updatedAt=updated_at or STORY_TIME,
        )

    def make_scene(
        self, story_id: str = PRIVATE_STORY_ID, status: str = 'active',
        started_at: Optional[datetime] = None, ended_at: Optional[datetime] = None,
        hook: str = 'hook', summary: str = 'summary', entry_count: int = 3,
    ) -> dict[str, Any]:
        return self.insert(
            'interlude_scene', storyId=story_id, status=status, startedAt=started_at or STORY_TIME,
            endedAt=ended_at, hook=hook, summary=summary, entryCount=entry_count, lastEntryId=None,
            createdAt=STORY_TIME, updatedAt=STORY_TIME,
        )

    def make_arc(
        self, story_id: str = PRIVATE_STORY_ID, status: str = 'active',
        created_at: Optional[datetime] = None, updated_at: Optional[datetime] = None,
    ) -> dict[str, Any]:
        return self.insert(
            'interlude_arc', storyId=story_id, status=status, title='arc', summary='sum',
            sceneCount=1, createdAt=created_at or STORY_TIME, updatedAt=updated_at or STORY_TIME,
        )

    def make_patch(
        self, story_id: str = PRIVATE_STORY_ID, target: str = 'character', status: str = 'proposed',
        created_at: Optional[datetime] = None, applied_at: Optional[datetime] = None,
    ) -> dict[str, Any]:
        return self.insert(
            'interlude_state_patch', storyId=story_id, participantId='', target=target,
            path='profile', proposedValue='pv', evidence='ev', confidence=0.9, impact='minor',
            status=status, sourceEntryIds=[], createdAt=created_at or STORY_TIME, appliedAt=applied_at,
        )

    def make_snapshot(
        self, story_id: str = PRIVATE_STORY_ID, target: str = 'character', status: str = 'active',
    ) -> dict[str, Any]:
        return self.insert(
            'interlude_overlay_snapshot', storyId=story_id, participantId='', target=target,
            tier='weekly', periodStart=STORY_TIME, periodEnd=STORY_TIME, summary='s',
            majorEvents=[], sourcePatchIds=[], status=status,
            createdAt=STORY_TIME, updatedAt=STORY_TIME,
        )

    def make_observation(self, story_id: str = PRIVATE_STORY_ID, accessed_at: Optional[datetime] = None,
                         created_at: Optional[datetime] = None) -> dict[str, Any]:
        return self.insert(
            'interlude_web_observation', storyId=story_id, participantId='', intentId=None,
            mode='search', query='q', url='https://example.com', title='t', excerpt='e', summary='s',
            status='success', accessedAt=accessed_at or STORY_TIME, createdAt=created_at or STORY_TIME,
        )

    def make_participant(self, story_id: str = PRIVATE_STORY_ID, state: Optional[dict[str, Any]] = None,
                         participant_id: str = 'onebot:1:2', status: str = 'active') -> dict[str, Any]:
        return self.insert(
            'interlude_participant', id=participant_id, storyId=story_id, platform='onebot',
            selfId='1', userId='2', channelId='', personId='2', displayName='Alice',
            profile='', relationship='', state=state if state is not None else {},
            status=status, createdAt=STORY_TIME, updatedAt=STORY_TIME,
        )

    def rows(self, table: str, where: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
        return self.db.all(table, where) if where else self.db.all(table)


# =========================================================================== #
# 模块级辅助（上游 `service.ts:7300-7710`，helpers 尚未导出的五个）
# =========================================================================== #

class Chunk1ModuleHelperTests(ServiceHarness):
    """本文件里那 5 个模块级辅助（上游 `service.ts:7300-7710`）。"""

    def test_targetable_message_id_accepts_only_non_zero_integers(self) -> None:
        self.assertEqual(_targetable_message_id('-12345'), '-12345')
        self.assertEqual(_targetable_message_id(42), '42')
        self.assertIsNone(_targetable_message_id('0'))
        self.assertIsNone(_targetable_message_id(''))
        self.assertIsNone(_targetable_message_id(None))
        self.assertIsNone(_targetable_message_id('abc'))
        self.assertIsNone(_targetable_message_id('12.5'))

    def test_group_message_ref_is_a_non_negative_clamped_id(self) -> None:
        self.assertEqual(_group_message_ref(7), 'msg-7')
        self.assertEqual(_group_message_ref(0), 'msg-0')
        self.assertEqual(_group_message_ref(-5), 'msg-0')
        self.assertEqual(_group_message_ref(None), 'msg-0')

    def test_mentions_bot_uses_elements_then_content(self) -> None:
        view = SessionView(self_id='1', elements=[{'type': 'at', 'attrs': {'id': '1'}}])
        self.assertTrue(_mentions_bot(view))
        self.assertFalse(_mentions_bot(SessionView(self_id='1', content='hi')))
        # 非 SessionView（dict session）走上游的内容匹配分支。
        self.assertTrue(_mentions_bot({'selfId': '10000', 'content': 'hello 10000'}))
        self.assertTrue(_mentions_bot({'selfId': '10000', 'content': '<at id="10000"/>'}))
        self.assertFalse(_mentions_bot({'selfId': '', 'content': '10000'}))

    def test_quotes_bot_compares_quote_author_to_self(self) -> None:
        # 上游是**字面量**比较（不做 normalizeAccountId），故两边拼写必须一致。
        quoted = SessionView(self_id='1', quote={'user': {'id': '1'}})
        self.assertTrue(_quotes_bot(quoted))
        self.assertFalse(_quotes_bot(SessionView(self_id='1', quote={'user': {'id': '2'}})))
        self.assertFalse(_quotes_bot(SessionView(self_id='1')))
        self.assertTrue(_quotes_bot({'selfId': '7', 'quote': {'user': {'id': '7'}}}))

    def test_same_platform_family_groups_onebot_aliases(self) -> None:
        self.assertTrue(_same_platform_family('onebot', 'napcat'))
        self.assertTrue(_same_platform_family('onebot:1', 'qq:onebot'))
        self.assertTrue(_same_platform_family('Telegram', ' telegram '))
        self.assertFalse(_same_platform_family('onebot', 'telegram'))
        # 上游 `String(undefined ?? '').toLowerCase()`：两个空值算同一族。
        self.assertTrue(_same_platform_family(None, None))
        self.assertTrue(_same_platform_family('', None))

    def test_clear_database_fallback_placeholders(self) -> None:
        self.assertEqual(_clear_database_fallback('interlude_script_entry')['kind'], 'redacted')
        self.assertEqual(_clear_database_fallback('interlude_memory')['status'], 'deleted')
        self.assertEqual(_clear_database_fallback('interlude_web_observation')['summary'], '[HDSI 数据库已清空]')
        self.assertEqual(_clear_database_fallback('interlude_schedule_preplan')['validThrough'], '1970-01-01')
        # 兜底分支（state_patch 等）。
        self.assertEqual(_clear_database_fallback('interlude_state_patch')['status'], 'rejected')

    def test_group_context_messages_restore_upstream_wire_shape(self) -> None:
        messages = _group_context_messages([
            {'sender_id': '2', 'sender_name': 'Alice', 'speaker': 'S', 'message_ref': 'msg-1',
             'message_id': '-9', 'content': 'hi', 'occurred_at': STORY_TIME, 'direction': 'user'},
        ])
        self.assertEqual(messages, [{
            'senderId': '2', 'senderName': 'Alice', 'speaker': 'S', 'content': 'hi',
            'occurredAt': STORY_TIME, 'direction': 'user', 'messageId': '-9', 'messageRef': 'msg-1',
        }])
        self.assertEqual(_group_context_messages(None), [])

    def test_chat_capabilities_wire_restores_upstream_wire_shape(self) -> None:
        wire = _chat_capabilities_wire({
            'platform': 'qq', 'quote_reply': True, 'native_faces': ['smile'],
            'expression_threshold': 0.5, 'reactions': ['like'],
        })
        self.assertEqual(wire, {
            'platform': 'qq', 'reactions': ['like'],
            'quoteReply': True, 'nativeFaces': ['smile'], 'expressionThreshold': 0.5,
        })
        self.assertIsNone(_chat_capabilities_wire(None))


# =========================================================================== #
# recentEntriesForPrompt（上游 `:1248`）
# =========================================================================== #

class RecentEntriesForPromptTests(ServiceHarness):

    @needs('recent_entries_for_prompt')
    async def test_count_floor_and_time_window_are_merged_and_deduplicated(self) -> None:
        service = self.make_service(make_config(runtime={
            'contextEntryLimit': 20, 'contextTimeWindowMinutes': 60,
        }))
        # 60 条：id=1 最早（now-590min），id=60 最新（now-10min），间隔 10 分钟。
        for index in range(1, 61):
            self.make_entry(
                entry_id=index, content='e%d' % index,
                occurred_at=STORY_TIME - timedelta(minutes=(60 - index) * 10),
            )
        rows = await service.recent_entries_for_prompt(PRIVATE_STORY_ID, STORY_TIME)
        ids = [row['id'] for row in rows]
        # count 下限 50（< 配置的 20）∪ 60 分钟窗口（id 54..60）→ id 11..60。
        self.assertEqual(ids, list(range(11, 61)))
        self.assertEqual(len(set(ids)), len(ids))  # 去重
        self.assertEqual(ids, sorted(ids))         # occurredAt 升序

    @needs('recent_entries_for_prompt')
    async def test_zero_minutes_disables_the_time_window_only(self) -> None:
        service = self.make_service(make_config(runtime={
            'contextEntryLimit': 5, 'contextTimeWindowMinutes': 0,
        }))
        for index in range(1, 61):
            self.make_entry(
                entry_id=index,
                occurred_at=STORY_TIME - timedelta(minutes=(60 - index) * 10),
            )
        rows = await service.recent_entries_for_prompt(PRIVATE_STORY_ID, STORY_TIME)
        self.assertEqual([row['id'] for row in rows], list(range(11, 61)))

    @needs('recent_entries_for_prompt')
    async def test_settings_are_clamped_to_upstream_bounds(self) -> None:
        service = self.make_service(make_config(runtime={
            'contextEntryLimit': 999, 'contextTimeWindowMinutes': 99_999,
        }))
        self.make_entry(entry_id=1, occurred_at=STORY_TIME - timedelta(minutes=5))
        rows = await service.recent_entries_for_prompt(PRIVATE_STORY_ID, STORY_TIME)
        self.assertEqual([row['id'] for row in rows], [1])

    @needs('recent_entries_for_prompt')
    async def test_other_stories_are_never_mixed_in(self) -> None:
        service = self.make_service()
        self.make_entry(entry_id=1, story_id=PRIVATE_STORY_ID)
        self.make_entry(entry_id=2, story_id='other')
        rows = await service.recent_entries_for_prompt(PRIVATE_STORY_ID, STORY_TIME)
        self.assertEqual([row['id'] for row in rows], [1])


# =========================================================================== #
# memories / 管理视图（上游 `:1264-1296`）
# =========================================================================== #

class MemoriesTests(ServiceHarness):

    @needs('memories')
    async def test_participant_filter_then_relevance_sort_and_slice(self) -> None:
        service = self.make_service()
        self.make_memory(importance=0.1, participant_id='', content='global-low')
        self.make_memory(importance=0.9, participant_id='', content='global-high')
        self.make_memory(importance=0.5, participant_id='p1', content='p1-mid')
        self.make_memory(importance=0.7, participant_id='p2', content='p2')
        self.make_memory(importance=1.0, participant_id='p1', status='superseded', content='inactive')
        rows = await service.memories(PRIVATE_STORY_ID, 2, 'p1')
        self.assertEqual([row['content'] for row in rows], ['global-high', 'p1-mid'])

    @needs('memories')
    async def test_importance_ties_break_on_updated_at_desc(self) -> None:
        service = self.make_service()
        self.make_memory(importance=1, updated_at=STORY_TIME, content='older')
        self.make_memory(importance=1, updated_at=STORY_TIME + timedelta(hours=1), content='newer')
        rows = await service.memories(PRIVATE_STORY_ID, 5)
        self.assertEqual([row['content'] for row in rows], ['newer', 'older'])

    @needs('memories')
    async def test_default_limit_reads_runtime_config(self) -> None:
        service = self.make_service(make_config(runtime={'memoryLimit': 1}))
        self.make_memory(importance=2, content='a')
        self.make_memory(importance=1, content='b')
        rows = await service.memories(PRIVATE_STORY_ID)
        self.assertEqual([row['content'] for row in rows], ['a'])

    @needs('memories')
    async def test_limit_is_applied_after_the_bounded_prefetch(self) -> None:
        service = self.make_service()
        for index in range(10):
            self.make_memory(importance=index, content='m%d' % index)
        rows = await service.memories(PRIVATE_STORY_ID, 3)
        self.assertEqual([row['content'] for row in rows], ['m9', 'm8', 'm7'])


class AdminViewTests(ServiceHarness):

    @needs('admin_facts')
    async def test_admin_facts_are_active_only_sorted_by_updated_at(self) -> None:
        service = self.make_service()
        self.make_fact(content='old', updated_at=STORY_TIME)
        self.make_fact(content='new', updated_at=STORY_TIME + timedelta(hours=1))
        self.make_fact(content='gone', status='superseded', updated_at=STORY_TIME + timedelta(hours=2))
        rows = await service.admin_facts(PRIVATE_STORY_ID)
        self.assertEqual([row['content'] for row in rows], ['new', 'old'])

    @needs('admin_facts')
    async def test_admin_facts_limit_is_clamped_to_one_hundred(self) -> None:
        service = self.make_service()
        for index in range(5):
            self.make_fact(content='f%d' % index, updated_at=STORY_TIME + timedelta(minutes=index))
        self.assertEqual(len(await service.admin_facts(PRIVATE_STORY_ID, 0)), 1)
        self.assertEqual(len(await service.admin_facts(PRIVATE_STORY_ID, 1_000)), 5)

    @needs('admin_pending_intents')
    async def test_admin_pending_intents_are_pending_sorted_by_not_before(self) -> None:
        service = self.make_service()
        self.make_intent(summary='later', not_before=STORY_TIME + timedelta(hours=2))
        self.make_intent(summary='sooner', not_before=STORY_TIME + timedelta(hours=1))
        self.make_intent(summary='done', status='completed', not_before=STORY_TIME)
        rows = await service.admin_pending_intents(PRIVATE_STORY_ID)
        self.assertEqual([row['summary'] for row in rows], ['sooner', 'later'])

    @needs('admin_state_patches')
    async def test_admin_state_patches_include_every_status_sorted_by_created_at(self) -> None:
        service = self.make_service()
        self.make_patch(status='applied', created_at=STORY_TIME)
        self.make_patch(status='proposed', created_at=STORY_TIME + timedelta(hours=1))
        rows = await service.admin_state_patches(PRIVATE_STORY_ID)
        self.assertEqual([row['status'] for row in rows], ['proposed', 'applied'])
        self.assertEqual(len(await service.admin_state_patches(PRIVATE_STORY_ID, 0)), 1)


# =========================================================================== #
# 管理写入（上游 `:1299-1345`）
# =========================================================================== #

class AdminWriteTests(ServiceHarness):

    @needs('add_admin_script_note', 'append_entry')
    async def test_admin_script_note_is_clipped_and_appended_as_system_note(self) -> None:
        service = self.make_service(make_config(runtime={'maxScriptCharacters': 5}))
        story = self.make_story()
        compressed: list[str] = []
        service.schedule_compaction = compressed.append
        self.assertTrue(await service.add_admin_script_note(story, '  记住这件事  '))
        rows = self.rows('interlude_script_entry')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['kind'], 'admin-note')
        self.assertEqual(rows[0]['actor'], 'system')
        self.assertEqual(rows[0]['content'], '[管理员注记] 记住这件事')
        self.assertEqual(rows[0]['metadata'], {'source': 'administrator'})
        self.assertEqual(compressed, [PRIVATE_STORY_ID])

    @needs('add_admin_script_note')
    async def test_blank_admin_script_note_is_rejected(self) -> None:
        service = self.make_service()
        story = self.make_story()
        self.assertFalse(await service.add_admin_script_note(story, '   '))
        self.assertFalse(await service.add_admin_script_note(story, None))

    @needs('add_admin_fact')
    async def test_admin_fact_is_high_confidence_and_embedded(self) -> None:
        service = self.make_service(make_config(memory={'factContentCharacters': 40_000}))
        story = self.make_story()
        embedded: list[str] = []

        async def embed(value: str) -> list[float]:
            embedded.append(value)
            return [0.5]

        service.embed_text = embed
        self.assertTrue(await service.add_admin_fact(story, 'world', '  地球是圆的  '))
        rows = self.rows('interlude_fact')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['content'], '地球是圆的')
        self.assertEqual(rows[0]['scope'], 'world')
        self.assertEqual(rows[0]['participantId'], '')
        self.assertEqual(rows[0]['importance'], 0.8)
        self.assertEqual(rows[0]['confidence'], 1)
        self.assertFalse(rows[0]['unresolved'])
        self.assertEqual(rows[0]['status'], 'active')
        self.assertEqual(rows[0]['embedding'], [0.5])
        self.assertEqual(embedded, ['地球是圆的'])

    @needs('add_admin_fact')
    async def test_admin_fact_content_is_clipped_by_memory_config(self) -> None:
        service = self.make_service(make_config(memory={'factContentCharacters': 3}))

        async def embed(_value: str) -> list[float]:
            return []

        service.embed_text = embed
        await service.add_admin_fact(self.make_story(), 'world', 'abcdef')
        self.assertEqual(self.rows('interlude_fact')[0]['content'], 'abc')

    @needs('forget_admin_fact')
    async def test_forget_admin_fact_soft_deletes_for_audit(self) -> None:
        service = self.make_service()
        fact = self.make_fact()
        self.assertTrue(await service.forget_admin_fact(PRIVATE_STORY_ID, fact['id']))
        self.assertEqual(self.rows('interlude_fact')[0]['status'], 'superseded')
        # 已经软删的行不再匹配 active 查询。
        self.assertFalse(await service.forget_admin_fact(PRIVATE_STORY_ID, fact['id']))
        self.assertFalse(await service.forget_admin_fact('other', fact['id']))

    @needs('cancel_admin_intent')
    async def test_cancel_admin_intent_only_cancels_pending(self) -> None:
        service = self.make_service()
        pending = self.make_intent(status='pending')
        done = self.make_intent(status='completed')
        self.assertTrue(await service.cancel_admin_intent(PRIVATE_STORY_ID, pending['id']))
        self.assertEqual(self.rows('interlude_intent', {'id': pending['id']})[0]['status'], 'cancelled')
        self.assertFalse(await service.cancel_admin_intent(PRIVATE_STORY_ID, done['id']))

    @needs('reject_admin_state_patch')
    async def test_reject_admin_state_patch_only_rejects_proposed(self) -> None:
        service = self.make_service()
        proposed = self.make_patch(status='proposed')
        applied = self.make_patch(status='applied')
        self.assertTrue(await service.reject_admin_state_patch(PRIVATE_STORY_ID, proposed['id']))
        self.assertEqual(self.rows('interlude_state_patch', {'id': proposed['id']})[0]['status'], 'rejected')
        self.assertFalse(await service.reject_admin_state_patch(PRIVATE_STORY_ID, applied['id']))


# =========================================================================== #
# clearSettingOverlay / rebaseTimeline（上游 `:1347-1429`）
# =========================================================================== #

class ClearSettingOverlayTests(ServiceHarness):

    def _seed_overlay(self) -> dict[str, Any]:
        # 旧数据用 camelCase（键名法：外部读入双读）。
        return self.make_story(state={
            'schema_version': 1,
            'settingOverlay': {
                'characterProfile': 'old profile',
                'characterTraits': ['trait-a', 'trait-b'],
                'perspective': 'p', 'relationship': 'r', 'world': 'w',
            },
            'narrativeUpdateCount': 3,
            'automation': {},
        })

    @needs('clear_setting_overlay', 'clear_setting_overlay_unlocked')
    async def test_character_target_clears_only_the_character_overlay(self) -> None:
        service = self.make_service()
        story = self._seed_overlay()
        self.make_participant(state={'relationshipOverlay': 'old'})
        cleared = await service.clear_setting_overlay(story, 'character')
        self.assertEqual(cleared, {'participantCount': 0})
        state = (await service.get_story(PRIVATE_STORY_ID))['state']
        overlay = state['setting_overlay']
        # `encodeStoryState` 会把 overlay 归一成完整的键集合，被"删除"的键因此是 None
        # （上游是 `delete` + 编码器不补键；两边对读取方等价：`_text_or_none` 认 None）。
        self.assertIsNone(overlay.get('character_profile'))
        self.assertEqual(overlay['character_traits'], [])
        self.assertEqual(overlay['perspective'], 'p')
        self.assertEqual(overlay['relationship'], 'r')
        self.assertEqual(overlay['world'], 'w')
        # 关系覆盖没被碰。
        self.assertEqual(
            self.rows('interlude_participant')[0]['state'].get('relationshipOverlay'), 'old',
        )

    @needs('clear_setting_overlay', 'clear_setting_overlay_unlocked')
    async def test_all_target_also_drops_participant_relationship_overlay(self) -> None:
        service = self.make_service()
        story = self._seed_overlay()
        self.make_participant(state={'relationshipOverlay': 'old', 'openThreads': ['a']})
        self.make_participant(participant_id='onebot:1:3', state={'openThreads': []})
        cleared = await service.clear_setting_overlay(story, 'all')
        self.assertEqual(cleared, {'participantCount': 1})
        state = (await service.get_story(PRIVATE_STORY_ID))['state']
        for key in ('character_profile', 'perspective', 'relationship', 'world'):
            self.assertIsNone(state['setting_overlay'].get(key), key)
        touched = self.rows('interlude_participant', {'id': 'onebot:1:2'})[0]['state']
        self.assertFalse(has_relationship_overlay(touched))
        self.assertEqual(touched.get('openThreads'), ['a'])

    @needs('clear_setting_overlay', 'clear_setting_overlay_unlocked')
    async def test_patches_and_snapshots_are_invalidated_by_target(self) -> None:
        service = self.make_service()
        story = self._seed_overlay()
        keep_patch = self.make_patch(target='world', status='applied')
        kill_patch = self.make_patch(target='character', status='proposed')
        audit_patch = self.make_patch(target='character', status='rejected')
        kill_snapshot = self.make_snapshot(target='character')
        keep_snapshot = self.make_snapshot(target='world')
        await service.clear_setting_overlay(story, 'character')
        self.assertEqual(self.rows('interlude_state_patch', {'id': kill_patch['id']})[0]['status'], 'cleared')
        self.assertEqual(self.rows('interlude_state_patch', {'id': keep_patch['id']})[0]['status'], 'applied')
        self.assertEqual(self.rows('interlude_state_patch', {'id': audit_patch['id']})[0]['status'], 'rejected')
        self.assertEqual(self.rows('interlude_overlay_snapshot', {'id': kill_snapshot['id']})[0]['status'], 'superseded')
        self.assertEqual(self.rows('interlude_overlay_snapshot', {'id': keep_snapshot['id']})[0]['status'], 'active')

    @needs('clear_setting_overlay', 'clear_setting_overlay_unlocked')
    async def test_buffered_narratives_are_invalidated_before_clearing(self) -> None:
        service = self.make_service()
        story = self._seed_overlay()
        calls: list[Optional[str]] = []
        service.invalidate_buffered_narratives = calls.append
        await service.clear_setting_overlay(story, 'world')
        self.assertEqual(calls, [PRIVATE_STORY_ID])


class RebaseTimelineTests(ServiceHarness):

    @needs('rebase_timeline', 'active_scene', 'append_entry')
    async def test_rebase_resets_scene_state_and_returns_scene_reset(self) -> None:
        service = self.make_service()
        self.make_story(state={
            'schema_version': 1,
            'settingOverlay': {'characterTraits': []},
            'workingDetails': [{'label': 'a', 'value': 'b', 'createdAt': '2026-01-01T00:00:00Z'}],
            'timelineCarry': ['carry'],
            'continuitySnapshot': {'current': 'x', 'next': [], 'recent': [], 'salient': []},
            'continuityDirty': False,
            'automation': {},
        })
        self.make_entry(entry_id=1, kind='script', content='first')
        self.make_entry(entry_id=2, kind='script', content='latest')
        scene = self.make_scene(entry_count=2)
        invalidated: list[Optional[str]] = []
        service.invalidate_buffered_narratives = invalidated.append

        result = await service.rebase_timeline({'id': PRIVATE_STORY_ID})
        self.assertEqual(invalidated, [PRIVATE_STORY_ID])
        self.assertTrue(result['sceneReset'])
        self.assertEqual(dt_ms(result['at']), STORY_MS)

        scene_row = self.rows('interlude_scene', {'id': scene['id']})[0]
        self.assertEqual(
            scene_row['hook'],
            'Host timeline rebased at %s.' % _expected_log_time(),
        )
        self.assertIn('Earlier script remains archived context', scene_row['summary'])
        self.assertEqual(scene_row['lastEntryId'], 2)
        self.assertEqual(scene_row['entryCount'], 0)

        state = (await service.get_story(PRIVATE_STORY_ID))['state']
        self.assertEqual(state['working_details'], [])
        self.assertEqual(state['timeline_carry'], [])
        self.assertTrue(state['continuity_dirty'])
        self.assertIsNone(state.get('continuity_snapshot'))

        rebase_entries = [row for row in self.rows('interlude_script_entry') if row['kind'] == 'timeline-rebase']
        self.assertEqual(len(rebase_entries), 1)
        self.assertEqual(rebase_entries[0]['metadata'], {'timelineRebase': True})

    @needs('rebase_timeline', 'active_scene')
    async def test_rebase_without_active_scene_reports_no_scene_reset(self) -> None:
        service = self.make_service()
        self.make_story()
        result = await service.rebase_timeline({'id': PRIVATE_STORY_ID})
        self.assertFalse(result['sceneReset'])


def _expected_log_time() -> str:
    from plugin.core.time import format_log_time
    return format_log_time(STORY_TIME, 'Asia/Shanghai')


def has_relationship_overlay(state: Any) -> bool:
    """参与者状态里是否还有关系覆盖（两种拼写都认，键名法）。"""
    if not isinstance(state, dict):
        return False
    return bool(state.get('relationshipOverlay') or state.get('relationship_overlay'))


# =========================================================================== #
# purgeAllStoryData / purgeAllData / purgePlatformData（上游 `:1431-1484`）
# =========================================================================== #

class PurgeTests(ServiceHarness):

    def _seed_everything(self) -> None:
        self.make_story()
        self.make_entry(entry_id=1, kind='script')
        self.make_memory()
        self.make_intent()
        self.make_scene()
        self.make_arc()
        self.make_fact()
        self.make_patch()
        self.make_snapshot()
        self.make_observation()

    @needs('purge_all_story_data', 'purge_table')
    async def test_purge_all_story_data_rewrites_plan_and_snapshot_rows(self) -> None:
        service = self.make_service()
        self._seed_everything()
        self.make_participant()
        callbacks: list[str] = []

        def initial_setting(name: Optional[str] = None) -> dict[str, Any]:
            callbacks.append('setting')
            return {
                'character': {'name': 'Fresh', 'profile': ''},
                'user': {'display_name': '', 'profile': ''},
                'relationship': '', 'world': '', 'perspective': '', 'supporting_cast': '',
                'location': '', 'style': '', 'timezone': 'Asia/Shanghai',
            }

        async def reset(story_id: str, now: Any) -> None:
            callbacks.append('participants')

        async def continuity(story: Any, now: Any) -> None:
            callbacks.append('continuity')

        service.initial_story_setting = initial_setting
        service.reset_participant_canon = reset
        service.ensure_continuity = continuity

        # 强制走"物理删除不可用 → 逻辑清空"的兜底路径（上游 disk-I/O 兜底）。
        # 注意错误文本**故意不写成瞬时错误**（`disk I/O` / `database is locked` / `busy`）：
        # 那种错误会（正确地）触发 Chunk9 `retryDbWrite` 的有界退避重试（约 12 秒 × 12 张表），
        # 本用例要验的是 fallback 分支，故给一个永久性失败。
        def broken_remove(table: str, where: Any) -> int:
            raise RuntimeError('simulated permanent delete failure')

        service.db.remove = broken_remove
        await service.purge_all_story_data(PRIVATE_STORY_ID)

        self.assertEqual(self.rows('interlude_script_entry')[0]['kind'], 'redacted')
        self.assertEqual(self.rows('interlude_memory')[0]['status'], 'deleted')
        self.assertEqual(self.rows('interlude_intent')[0]['summary'], '[管理员已取消意图]')
        self.assertEqual(self.rows('interlude_scene')[0]['status'], 'closed')
        self.assertEqual(self.rows('interlude_arc')[0]['sceneCount'], 0)
        self.assertEqual(self.rows('interlude_fact')[0]['status'], 'superseded')
        self.assertEqual(self.rows('interlude_state_patch')[0]['status'], 'rejected')
        self.assertEqual(self.rows('interlude_overlay_snapshot')[0]['status'], 'superseded')
        self.assertEqual(self.rows('interlude_web_observation')[0]['status'], 'deleted')
        story = self.rows('interlude_story')[0]
        self.assertEqual(story['setting']['character']['name'], 'Fresh')
        self.assertEqual(story['state']['narrative_update_count'], 0)
        self.assertEqual(callbacks, ['setting', 'participants', 'continuity'])

    @needs('purge_all_story_data')
    async def test_purge_all_story_data_physically_removes_rows(self) -> None:
        service = self.make_service()
        self.make_story()
        self.make_entry(entry_id=1, kind='script')
        self.make_memory()

        async def noop(*_args: Any, **_kwargs: Any) -> None:
            return None

        service.initial_story_setting = lambda name=None: {'character': {'name': 'F', 'profile': ''}}
        service.reset_participant_canon = noop
        service.ensure_continuity = noop
        await service.purge_all_story_data(PRIVATE_STORY_ID)
        self.assertEqual(self.rows('interlude_script_entry'), [])
        self.assertEqual(self.rows('interlude_memory'), [])

    @needs('purge_all_data')
    async def test_purge_all_data_keeps_one_canonical_story_and_archives_the_rest(self) -> None:
        service = self.make_service()
        self.make_story(story_id='character:onebot:1', updatedAt=STORY_TIME)
        self.make_story(story_id='legacy:2', updatedAt=STORY_TIME + timedelta(hours=1))
        self.make_story(story_id='paused:3', status='paused', updatedAt=STORY_TIME + timedelta(hours=2))
        purged: list[str] = []

        async def purge(story_id: str) -> None:
            purged.append(story_id)

        service.purge_all_story_data = purge
        canonical = await service.purge_all_data()
        # 没有 preferredStoryId → 最近更新的活动剧本。
        self.assertEqual(canonical, 'legacy:2')
        self.assertEqual(sorted(purged), ['character:onebot:1', 'legacy:2', 'paused:3'])
        statuses = {row['id']: row['status'] for row in self.rows('interlude_story')}
        self.assertEqual(statuses, {
            'character:onebot:1': 'archived', 'legacy:2': 'active', 'paused:3': 'archived',
        })

    @needs('purge_all_data')
    async def test_purge_all_data_honours_the_preferred_story(self) -> None:
        service = self.make_service()
        self.make_story(story_id='a', updatedAt=STORY_TIME + timedelta(hours=2))
        self.make_story(story_id='b', updatedAt=STORY_TIME)

        async def purge(_story_id: str) -> None:
            return None

        service.purge_all_story_data = purge
        self.assertEqual(await service.purge_all_data('b'), 'b')
        self.assertEqual(
            {row['id']: row['status'] for row in self.rows('interlude_story')},
            {'a': 'archived', 'b': 'active'},
        )

    @needs('purge_all_data')
    async def test_purge_all_data_without_active_story_returns_none(self) -> None:
        service = self.make_service()
        self.make_story(status='paused')
        self.assertIsNone(await service.purge_all_data())

    @needs('purge_platform_data')
    async def test_purge_platform_data_treats_onebot_aliases_as_one_family(self) -> None:
        service = self.make_service()
        self.make_story(story_id='s1', platform='onebot')
        self.make_story(story_id='s2', platform='onebot:123')
        self.make_story(story_id='s3', platform='napcat')
        self.make_story(story_id='s4', platform='telegram')
        purged: list[str] = []

        async def purge(story_id: str) -> None:
            purged.append(story_id)

        service.purge_all_story_data = purge
        self.assertEqual(await service.purge_platform_data('onebot'), 3)
        self.assertEqual(sorted(purged), ['s1', 's2', 's3'])
        statuses = {row['id']: row['status'] for row in self.rows('interlude_story')}
        self.assertEqual(statuses, {'s1': 'archived', 's2': 'archived', 's3': 'archived', 's4': 'active'})


# =========================================================================== #
# clearDatabase / purgeStoryRange（上游 `:1486-1601`）
# =========================================================================== #

class ClearDatabaseTests(ServiceHarness):

    @needs('clear_database')
    async def test_clear_database_removes_every_hdsi_table_and_reports_counts(self) -> None:
        service = self.make_service()
        self.make_story()
        self.make_entry(entry_id=1)
        self.make_memory()
        self.make_fact()
        self.make_participant()
        result = await service.clear_database()
        self.assertEqual(result['removed'], 5)
        self.assertEqual(result['logicallyCleared'], 0)
        for table in (
            'interlude_story', 'interlude_script_entry', 'interlude_memory',
            'interlude_fact', 'interlude_participant',
        ):
            self.assertEqual(self.rows(table), [], table)
        self.assertFalse(service.database_resetting)

    @needs('clear_database')
    async def test_clear_database_is_not_reentrant(self) -> None:
        service = self.make_service()
        service.database_resetting = True
        with self.assertRaises(RuntimeError):
            await service.clear_database()

    @needs('clear_database')
    async def test_clear_database_falls_back_to_logical_clearing(self) -> None:
        service = self.make_service()
        self.make_story()
        self.make_entry(entry_id=1)
        self.make_participant()

        # 同上：必须是永久性错误，否则会走 Chunk9 的瞬时写重试退避。
        def broken_remove(table: str, where: Any) -> int:
            raise RuntimeError('simulated permanent delete failure')

        service.db.remove = broken_remove
        result = await service.clear_database()
        self.assertEqual(result['removed'], 3)
        self.assertEqual(result['logicallyCleared'], 3)
        self.assertEqual(self.rows('interlude_story')[0]['status'], 'archived')
        self.assertEqual(self.rows('interlude_script_entry')[0]['kind'], 'redacted')
        self.assertEqual(self.rows('interlude_participant')[0]['status'], 'paused')
        self.assertFalse(service.database_resetting)

    @needs('clear_database')
    async def test_clear_database_resets_the_flag_after_failure(self) -> None:
        service = self.make_service()
        self.make_story()

        def broken_all(table: str, where: Any = None, order: Any = None, limit: Any = None) -> Any:
            raise RuntimeError('boom')

        service.db.all = broken_all
        with self.assertRaises(RuntimeError):
            await service.clear_database()
        self.assertFalse(service.database_resetting)


class PurgeStoryRangeTests(ServiceHarness):

    def _seed(self) -> dict[str, Any]:
        self.make_story()
        inside = self.make_entry(entry_id=1, occurred_at=STORY_TIME, metadata={'groupId': '9'})
        self.make_entry(entry_id=2, occurred_at=STORY_TIME + timedelta(days=10))
        self.make_memory(updated_at=STORY_TIME)
        self.make_fact(source_entry_ids=[1], created_at=STORY_TIME + timedelta(days=10),
                       updated_at=STORY_TIME + timedelta(days=10))
        self.make_intent(not_before=STORY_TIME + timedelta(hours=1))
        self.make_scene(started_at=STORY_TIME, ended_at=STORY_TIME + timedelta(hours=2))
        untouched_arc = self.make_arc(created_at=STORY_TIME + timedelta(days=9),
                                      updated_at=STORY_TIME + timedelta(days=9))
        self.make_patch(created_at=STORY_TIME)
        self.make_observation(accessed_at=STORY_TIME)
        self.insert(
            'interlude_schedule_preplan', storyId=PRIVATE_STORY_ID, revision=1, timezone='Asia/Shanghai',
            validFrom='2026-01-01', validThrough='2026-12-31', lastReviewedLocalDate='2026-01-01',
            lastEvidenceEntryId=1, reviewReason='ok', regimes=[], exceptions=[], materializedDays=[],
            createdAt=STORY_TIME, updatedAt=STORY_TIME,
        )
        return {'inside': inside, 'arc': untouched_arc}

    @needs('purge_story_range', 'purge_table')
    async def test_range_purge_deletes_every_overlapping_record(self) -> None:
        """默认路径：`purgeTable` 能物理删除时，行直接消失（上游同语义）。"""
        service = self.make_service()
        seeded = self._seed()
        continuity: list[str] = []

        async def noop(story: Any, now: Any) -> None:
            continuity.append('called')

        service.ensure_continuity = noop
        await service.purge_story_range(
            PRIVATE_STORY_ID, STORY_TIME, STORY_TIME + timedelta(hours=3),
        )
        entries = {row['id']: row for row in self.rows('interlude_script_entry')}
        self.assertNotIn(1, entries)                    # 区间内 → 删除
        self.assertEqual(entries[2]['kind'], 'user-message')  # 区间外 → 保留
        self.assertEqual(self.rows('interlude_memory'), [])
        # 事实虽然时间在区间外，但它的来源条目落在区间内 → 仍然被清除。
        self.assertEqual(self.rows('interlude_fact'), [])
        self.assertEqual(self.rows('interlude_intent'), [])
        self.assertEqual(self.rows('interlude_scene'), [])
        self.assertEqual(self.rows('interlude_arc', {'id': seeded['arc']['id']})[0]['status'], 'active')
        self.assertEqual(self.rows('interlude_state_patch'), [])
        self.assertEqual(self.rows('interlude_web_observation'), [])
        preplan = self.rows('interlude_schedule_preplan')[0]
        self.assertEqual(preplan['validThrough'], '1970-01-01')
        self.assertEqual(preplan['reviewReason'], 'Source range was purged; Schedule Preplan requires review.')
        self.assertEqual(continuity, ['called'])

    @needs('purge_story_range', 'purge_table')
    async def test_range_purge_redacts_when_physical_delete_fails(self) -> None:
        """兜底路径：物理删除不可用时改写成软删占位符（上游 `purgeTable` 的磁盘 I/O 兜底）。

        错误文本刻意**不是瞬时错误**，否则会走 Chunk9 `retryDbWrite` 的有界退避重试。
        """
        service = self.make_service()
        seeded = self._seed()
        calls: list[str] = []

        async def noop(story: Any, now: Any) -> None:
            calls.append('continuity')

        service.ensure_continuity = noop

        def broken_remove(table: str, where: Any) -> int:
            raise RuntimeError('simulated permanent delete failure')

        service.db.remove = broken_remove
        await service.purge_story_range(
            PRIVATE_STORY_ID, STORY_TIME, STORY_TIME + timedelta(hours=3),
        )
        entries = {row['id']: row for row in self.rows('interlude_script_entry')}
        self.assertEqual(entries[1]['kind'], 'redacted')
        self.assertEqual(entries[1]['content'], '[管理员已删除剧本内容]')
        self.assertEqual(entries[2]['kind'], 'user-message')
        self.assertEqual(self.rows('interlude_memory')[0]['status'], 'deleted')
        self.assertEqual(self.rows('interlude_fact')[0]['status'], 'superseded')
        self.assertEqual(self.rows('interlude_intent')[0]['status'], 'cancelled')
        self.assertEqual(self.rows('interlude_scene')[0]['status'], 'closed')
        self.assertEqual(self.rows('interlude_state_patch')[0]['status'], 'rejected')
        self.assertEqual(self.rows('interlude_web_observation')[0]['status'], 'deleted')
        self.assertEqual(self.rows('interlude_arc', {'id': seeded['arc']['id']})[0]['status'], 'active')
        self.assertEqual(calls, ['continuity'])

    @needs('purge_story_range')
    async def test_range_purge_skips_the_preplan_review_without_purged_entries(self) -> None:
        service = self.make_service()
        self.make_story()
        self.make_entry(entry_id=1, occurred_at=STORY_TIME + timedelta(days=30))
        self.insert(
            'interlude_schedule_preplan', storyId=PRIVATE_STORY_ID, revision=1, timezone='Asia/Shanghai',
            validFrom='2026-01-01', validThrough='2026-12-31', lastReviewedLocalDate='2026-01-01',
            lastEvidenceEntryId=1, reviewReason='ok', regimes=[], exceptions=[], materializedDays=[],
            createdAt=STORY_TIME, updatedAt=STORY_TIME,
        )

        async def noop(_story: Any, _now: Any) -> None:
            return None

        service.ensure_continuity = noop
        await service.purge_story_range(PRIVATE_STORY_ID, STORY_TIME, STORY_TIME + timedelta(hours=1))
        self.assertEqual(self.rows('interlude_schedule_preplan')[0]['reviewReason'], 'ok')
        self.assertEqual(self.rows('interlude_script_entry')[0]['kind'], 'user-message')


# =========================================================================== #
# receiveGroup（上游 `:1605`）
# =========================================================================== #

def group_session(**overrides: Any) -> SessionView:
    session = SessionView(
        platform='onebot', self_id='1', user_id='2', channel_id='9',
        content='大家好', guild_id='',
    )
    for key, value in overrides.items():
        setattr(session, key, value)
    return session


class ReceiveGroupTests(ServiceHarness):

    def _config(self, rule: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        return onebot_config(onebot={'groupChats': [rule or group_rule_stub()]})

    @needs('receive_group')
    async def test_non_onebot_platform_is_rejected(self) -> None:
        service = self.make_service(self._config())
        self.assertFalse(await service.receive_group(group_session(platform='telegram')))

    @needs('receive_group')
    async def test_an_unlisted_group_is_still_handled_by_default(self) -> None:
        """v1.3.0：`group_chats_only` 默认关闭 ⇒ 名单外的群照样接，用默认群规则。

        默认群规则与 schema 里群规则的默认值一致：`mention-only`，所以这里要 @ 才成回合。
        """
        service = self.make_service(make_config(onebot={}))
        self.make_story()
        self.assertTrue(await service.receive_group(group_session(
            channel_id='77', guild_id='77', content='@bot 在吗',
            elements=[{'type': 'at', 'attrs': {'id': '1'}}],
        )))
        turn = service.buffered_group_turns['%s:77' % SHARED_STORY_ID]
        self.assertEqual(turn['rule']['responseMode'], 'mention-only')

    @needs('receive_group')
    async def test_group_outside_the_allowlist_is_rejected_when_restricted(self) -> None:
        service = self.make_service(make_config(onebot={
            'groupChatsOnly': True, 'groupChats': [group_rule_stub()],
        }))
        self.assertFalse(await service.receive_group(group_session(channel_id='77')))

    @needs('receive_group')
    async def test_mention_only_mode_ignores_unmentioned_messages(self) -> None:
        service = self.make_service(self._config(group_rule_stub(responseMode='mention-only')))
        self.make_story()
        self.assertFalse(await service.receive_group(group_session()))
        self.assertEqual(self.rows('interlude_script_entry'), [])

    @needs('receive_group', 'append_entry', 'buffer_group_message')
    async def test_mentioned_message_is_persisted_and_buffered(self) -> None:
        service = self.make_service(self._config(group_rule_stub(responseMode='mention-only')))
        self.make_story()
        service.user_account_rule = lambda _user_id: {'label': 'Alice'}
        session = group_session(
            content='@bot 在吗', message_id='-12345',
            elements=[{'type': 'at', 'attrs': {'id': '1'}}],
        )
        self.assertTrue(await service.receive_group(session, STORY_TIME))

        entries = [row for row in self.rows('interlude_script_entry') if row['kind'] == 'group-message']
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['kind'], 'group-message')
        self.assertEqual(entries[0]['actor'], 'user')
        self.assertEqual(entries[0]['content'], '@bot 在吗')
        metadata = entries[0]['metadata']
        self.assertEqual(metadata['groupId'], '9')
        self.assertEqual(metadata['senderId'], '2')
        self.assertEqual(metadata['senderName'], 'Alice')
        self.assertEqual(metadata['messageId'], '-12345')

        turn = service.buffered_group_turns['%s:9' % SHARED_STORY_ID]
        self.assertEqual(len(turn['messages']), 1)
        message = turn['messages'][0]
        self.assertEqual(message['senderId'], '2')
        self.assertEqual(message['speaker'], '群成员「Alice」（QQ：2）')
        self.assertEqual(message['messageRef'], 'msg-%d' % entries[0]['id'])
        self.assertEqual(message['messageId'], '-12345')
        self.assertEqual(message['direction'], 'user')
        self.assertTrue(turn['mentioned_bot'])
        self.assertEqual(turn['revision'], 1)
        self.assertIsNotNone(turn['timer'])
        self.assertIn('收到群聊消息', self.sink.text())

    @needs('receive_group', 'append_entry', 'buffer_group_message')
    async def test_paused_story_is_not_accepted(self) -> None:
        service = self.make_service(self._config())
        self.make_story(status='paused')
        self.assertFalse(await service.receive_group(group_session()))
        # 共享模式下 canonical 剧本不存在时，`getPausedStory` 会把这部暂停剧本
        # 迁移成 `character:…` 再交给调用方判断状态（上游同序），迁移本身会写一条
        # `participant-joined`；**绝不能**出现群消息条目。
        self.assertEqual(
            [row for row in self.rows('interlude_script_entry') if row['kind'] == 'group-message'], [],
        )

    @needs('receive_group')
    async def test_database_reset_blocks_group_intake(self) -> None:
        service = self.make_service(self._config())
        self.make_story()
        service.database_resetting = True
        self.assertFalse(await service.receive_group(group_session()))


# =========================================================================== #
# receive（上游 `:1645`）
# =========================================================================== #

class ReceiveTests(ServiceHarness):

    def _config(self) -> dict[str, Any]:
        return onebot_config(sharedStory={'autoEnrollParticipants': True})

    def _session(self, **overrides: Any) -> SessionView:
        session = SessionView(
            platform='onebot', self_id='1', user_id='2', channel_id='private:2',
            content='你好', message_id='m-1',
        )
        for key, value in overrides.items():
            setattr(session, key, value)
        return session

    def _stub_private_dependencies(self, service: Any, participant: dict[str, Any]) -> dict[str, Any]:
        """替身：Chunk6/7 尚未落地的成员 + 参与者写入。

        上游对应成员：`ensureParticipant`（基础层真实实现依赖 Chunk7 的
        `userAccountRule` / `participantPreset`）、`recordIncomingMessage`、
        `cancelPendingOutgoingMessages`、`pauseAutomaticAdvanceAfterUserMessage`。
        """
        calls: dict[str, Any] = {'paused': [], 'records': []}

        async def ensure(story: Any, session: Any, now: Any = None, known: Any = None) -> Any:
            return participant

        async def record(current: Any, now: Any) -> Any:
            return participant

        async def cancel(_story_id: str, _participant_id: str, _now: Any, _flag: bool) -> list[Any]:
            return []

        async def pause(story_id: str, now: Any) -> None:
            calls['paused'].append(story_id)

        service.ensure_participant = ensure
        service.get_participant = lambda _pid: _async_value(participant)
        service.record_incoming_message = record
        service.cancel_pending_outgoing_messages = cancel
        service.pause_automatic_advance_after_user_message = pause
        return calls

    @needs('receive')
    async def test_unauthorized_session_never_creates_a_story(self) -> None:
        service = self.make_service(onebot_config(
            onebot={'botAccounts': [{'qq': '1'}], 'userAccounts': []},
        ))
        self.assertFalse(await service.receive(self._session()))
        self.assertEqual(self.rows('interlude_story'), [])
        # v1.3.0：拒绝原因不再走 diagnostic 报告，而是由 `explain_session_access` 给出，
        # 适配层负责把它打成人能看到的行（见 `AccessVisibilityTests`）。
        allowed, reason = service.explain_session_access(self._session())
        self.assertFalse(allowed)
        self.assertIn('仅处理名单内的用户', reason)

    @needs('receive')
    async def test_missing_story_without_auto_create_is_rejected(self) -> None:
        service = self.make_service(self._config())
        self.assertFalse(await service.receive(self._session()))
        self.assertIn('故事不存在或已暂停', self.sink.text())

    @needs('receive', 'append_entry')
    async def test_missing_participant_without_enrolment_is_rejected(self) -> None:
        # 共享模式硬开启，所以"不自动入册"只能靠 `autoEnrollParticipants: False` 表达；
        # 旧键 `enabled: False` 上游本来就会丢弃（见 `resolve_shared_story_config`）。
        # 剧本必须已经在 canonical id 上：若是旧按账号 id，`migrateLegacyStory` 会
        # 顺手把该账号入册（上游同序），那时"未入册"这个前提就不成立了。
        service = self.make_service(onebot_config(sharedStory={'autoEnrollParticipants': False}))
        self.make_story(story_id=SHARED_STORY_ID)
        self.assertFalse(await service.receive(self._session()))
        self.assertIn('参与者不存在或已暂停', self.sink.text())

    @needs('receive', 'append_entry')
    async def test_empty_content_and_no_voice_is_rejected(self) -> None:
        service = self.make_service(self._config())
        self.make_story()
        self.assertFalse(await service.receive(self._session(content='   ')))
        # 上游先 ensureParticipant 再判内容：`participant-joined` 可能已落库，
        # 但**绝不能**出现 user-message 条目。
        self.assertEqual(
            [row for row in self.rows('interlude_script_entry') if row['kind'] == 'user-message'], [],
        )

    @needs('receive', 'append_entry', 'buffer_user_narrative', 'describe_vision_event')
    async def test_private_message_is_persisted_buffered_and_reported(self) -> None:
        service = self.make_service(self._config())
        self.make_story()
        participant = {'id': 'onebot:1:2', 'status': 'active', 'personId': '2'}
        calls = self._stub_private_dependencies(service, participant)
        interruptions: list[tuple[str, str]] = []
        service.signal_incoming_interruption = (
            lambda story, part: interruptions.append((story['id'], part['id']))
        )

        self.assertTrue(await service.receive(self._session(), STORY_TIME))

        entries = self.rows('interlude_script_entry')
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['kind'], 'user-message')
        self.assertEqual(entries[0]['content'], '你好')
        self.assertEqual(entries[0]['participantId'], 'onebot:1:2')
        metadata = entries[0]['metadata']
        self.assertEqual(metadata['platform'], 'onebot')
        self.assertEqual(metadata['messageId'], 'm-1')
        self.assertEqual(metadata['personId'], '2')
        self.assertNotIn('imageCount', metadata)
        self.assertEqual(interruptions, [(SHARED_STORY_ID, 'onebot:1:2')])
        self.assertEqual(calls['paused'], [SHARED_STORY_ID])

        # 旧的按账号剧本被惰性迁移成共享剧本：新 id 是 active，旧 id 转 archived。
        stories = {row['id']: row for row in self.rows('interlude_story')}
        self.assertIn(SHARED_STORY_ID, stories)
        self.assertEqual(stories[SHARED_STORY_ID]['status'], 'active')
        self.assertEqual(stories[PRIVATE_STORY_ID]['status'], 'archived')

        turn = service.buffered_narrative_turns['onebot:1:2']
        self.assertEqual(len(turn['messages']), 1)
        self.assertEqual(turn['messages'][0]['content'], '你好')
        self.assertIn('收到参与者私聊消息', self.sink.text())
        self.assertIn('用户回合已入队', self.sink.text())

    @needs('receive', 'append_entry', 'buffer_user_narrative', 'describe_vision_event')
    async def test_incoming_images_and_audio_are_counted_in_metadata(self) -> None:
        service = self.make_service(self._config())
        self.make_story()
        participant = {'id': 'onebot:1:2', 'status': 'active', 'personId': '2'}
        self._stub_private_dependencies(service, participant)
        service.signal_incoming_interruption = lambda _story, _participant: None

        # 上游 `describeUserEvent` 是**同步**成员（`src/service.ts:2276`），替身也必须是同步的。
        def described(_story: Any, _session: Any) -> dict[str, Any]:
            return {
                'content': '看这个', 'sources': ['data:image/png;base64,AAA'],
                'audio_sources': ['data:audio/mp3;base64,BBB'], 'quote': None,
            }

        service.describe_user_event = described
        self.assertTrue(await service.receive(
            self._session(content='<img src="https://example.com/a.png"/>'), STORY_TIME,
        ))
        metadata = self.rows('interlude_script_entry')[0]['metadata']
        self.assertEqual(metadata['imageCount'], 1)
        self.assertEqual(metadata['audioCount'], 1)
        self.assertIn('当前事件包含图片附件', self.sink.text())
        self.assertIn('当前事件包含语音附件', self.sink.text())


def _async_value(value: Any) -> Any:
    async def factory() -> Any:
        return value
    return factory()


# =========================================================================== #
# 共享主剧本（上游 `sharedStoryConfig.enabled` 硬编码 true）
# =========================================================================== #

class SharedStoryTests(ServiceHarness):
    """一个角色一条时间线：所有私聊账号共用同一部剧本，各自是一条关系分支。

    上游 `get sharedStoryConfig`（`service.ts:5570`）把 `enabled` 钉死为 true
    （注释：Beta2 刻意保留单剧本守卫），因此 `findStory` 走 `character:platform:selfId`
    这条 canonical 路径；旧的"每 QQ 一部剧本"由 `migrateLegacyStory` 惰性迁移。
    本移植版曾经漏掉这个解析函数（读原始段 → 没有 enabled → 退回每人一部），
    这几条用例把行为钉住。
    """

    def _session(self, user_id: str = '2', **overrides: Any) -> SessionView:
        session = SessionView(
            platform='onebot', self_id='1', user_id=user_id, channel_id='private:%s' % user_id,
            content='你好', message_id='m-1',
        )
        for key, value in overrides.items():
            setattr(session, key, value)
        return session

    @needs('find_story', 'migrate_legacy_story', 'migrate_legacy_branch_into_shared')
    async def test_two_accounts_share_one_story_and_keep_the_old_entries(self) -> None:
        service = self.make_service(onebot_config())
        self.make_story(story_id=PRIVATE_STORY_ID, userId='2')
        self.make_entry(story_id=PRIVATE_STORY_ID, kind='script', content='旧剧本的第一段')

        first = await service.find_story(self._session('2'))
        second = await service.find_story(self._session('3'))

        self.assertEqual(first['id'], SHARED_STORY_ID)
        self.assertEqual(second['id'], SHARED_STORY_ID, '第二个账号必须落进同一部剧本')
        # 旧剧本的条目跟着迁移，一条都没丢，而且都挂在新 id 上。
        entries = self.rows('interlude_script_entry')
        self.assertIn('旧剧本的第一段', [row['content'] for row in entries])
        self.assertTrue(all(row['storyId'] == SHARED_STORY_ID for row in entries))
        # 旧 id 归档、新 id 活动，且只有一部活动剧本。
        statuses = {row['id']: row['status'] for row in self.rows('interlude_story')}
        self.assertEqual(statuses[SHARED_STORY_ID], 'active')
        self.assertEqual(statuses[PRIVATE_STORY_ID], 'archived')
        self.assertEqual(
            [sid for sid, status in statuses.items() if status == 'active'], [SHARED_STORY_ID],
        )

    @needs('find_story', 'get_canonical_story')
    async def test_single_story_guard_archives_the_other_active_stories(self) -> None:
        service = self.make_service(onebot_config())
        self.make_story(story_id=PRIVATE_STORY_ID, userId='2', updatedAt=STORY_TIME)
        self.make_story(
            story_id='onebot:1:3', userId='3', updatedAt=STORY_TIME + timedelta(hours=1),
        )
        # 单剧本守卫取"最近更新的那部"当 canonical，其余归档（内容保留，不删除）。
        story = await service.find_story(self._session('2'))
        self.assertEqual(story['id'], SHARED_STORY_ID)
        statuses = {row['id']: row['status'] for row in self.rows('interlude_story')}
        self.assertEqual(
            sorted(sid for sid, status in statuses.items() if status == 'active'), [SHARED_STORY_ID],
        )

    @needs('find_story', 'merge_story_into_canonical', 'append_entry')
    async def test_merge_brings_an_archived_story_into_the_canonical_one(self) -> None:
        """控制台「并入主剧本」：上游不会自动合并已经归档的旧分支，这里补上入口。"""
        service = self.make_service(onebot_config())
        self.make_story(story_id=SHARED_STORY_ID)
        self.make_story(story_id=PRIVATE_STORY_ID, status='archived', userId='2')
        self.make_entry(story_id=PRIVATE_STORY_ID, kind='script', content='被归档的旧段落')

        result = await service.merge_story_into_canonical(PRIVATE_STORY_ID, SHARED_STORY_ID)

        self.assertEqual(result['source'], PRIVATE_STORY_ID)
        self.assertEqual(result['target'], SHARED_STORY_ID)
        self.assertEqual(result['moved'], 1)
        entries = self.rows('interlude_script_entry')
        merged = [row for row in entries if row['content'] == '被归档的旧段落']
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]['storyId'], SHARED_STORY_ID)
        self.assertEqual(merged[0]['participantId'], result['participant_id'])
        # 参与者必须是**真 id**：`pick()` 只认 dict / `__getitem__`，自造只有属性的
        # 小对象会让这里变成 'None:None:None'（实测踩过，别只用"两边相等"断言）。
        self.assertNotIn('None', str(result['participant_id']))
        self.assertEqual(result['participant_id'], 'onebot:1:2')
        self.assertIn('legacy-branch-merged', [row['kind'] for row in entries])
        statuses = {row['id']: row['status'] for row in self.rows('interlude_story')}
        self.assertEqual(statuses[PRIVATE_STORY_ID], 'archived')

    @needs('merge_story_into_canonical')
    async def test_merge_rejects_self_missing_and_archived_targets(self) -> None:
        service = self.make_service(onebot_config())
        self.make_story(story_id=SHARED_STORY_ID)
        with self.assertRaises(ValueError):
            await service.merge_story_into_canonical(SHARED_STORY_ID, SHARED_STORY_ID)
        with self.assertRaises(LookupError):
            await service.merge_story_into_canonical('不存在', SHARED_STORY_ID)
        # 目标不能是归档剧本（内容会被搬进死档案）。
        self.make_story(story_id='onebot:1:9', status='archived', userId='9')
        self.make_story(story_id='onebot:1:8', status='archived', userId='8')
        with self.assertRaises(ValueError):
            await service.merge_story_into_canonical('onebot:1:8', 'onebot:1:9')

    @needs('find_story', 'merge_story_into_canonical')
    async def test_merge_accepts_a_still_legacy_canonical_target(self) -> None:
        """升级后还没人说过话时，canonical 仍是旧按账号剧本——并进去同样成立。

        那部剧本会在它的下一条消息里被 `migrateLegacyStory` 迁移成共享剧本，
        并进来的内容跟着一起走，所以这里不该拒绝。
        """
        service = self.make_service(onebot_config())
        self.make_story(story_id=PRIVATE_STORY_ID, userId='2')
        self.make_story(story_id='onebot:1:3', status='archived', userId='3')
        self.make_entry(story_id='onebot:1:3', kind='script', content='旧账号的段落')

        result = await service.merge_story_into_canonical('onebot:1:3', PRIVATE_STORY_ID)

        self.assertEqual(result['target'], PRIVATE_STORY_ID)
        merged = await service.find_story(self._session('2'))
        self.assertEqual(merged['id'], SHARED_STORY_ID)
        contents = [row['content'] for row in self.rows('interlude_script_entry')]
        self.assertIn('旧账号的段落', contents, '并进来的内容要跟着迁移到共享剧本')

    @needs('promote_story_to_canonical', 'shared_story_id', 'migrate_legacy_story')
    async def test_promote_makes_the_selected_story_the_main_one(self) -> None:
        """控制台「设为主剧本」：把选中的旧剧本迁移成 `character:…`，继承它的状态。"""
        service = self.make_service(onebot_config())
        self.make_story(story_id=PRIVATE_STORY_ID, userId='2')
        self.make_entry(story_id=PRIVATE_STORY_ID, kind='script', content='睡觉中的那一段')
        self.make_story(story_id='onebot:1:3', userId='3')

        result = await service.promote_story_to_canonical(PRIVATE_STORY_ID)

        self.assertEqual(result['target'], SHARED_STORY_ID)
        self.assertEqual(await service.shared_story_id(), SHARED_STORY_ID)
        statuses = {row['id']: row['status'] for row in self.rows('interlude_story')}
        self.assertEqual(statuses[SHARED_STORY_ID], 'active')
        self.assertEqual(statuses[PRIVATE_STORY_ID], 'archived')
        # 选中的那部是底座：它的条目、设定与状态被继承。
        main_entries = [row['content'] for row in self.rows('interlude_script_entry')
                        if row['storyId'] == SHARED_STORY_ID]
        self.assertIn('睡觉中的那一段', main_entries)
        main = [row for row in self.rows('interlude_story') if row['id'] == SHARED_STORY_ID][0]
        self.assertEqual(main['platform'], 'onebot')

    @needs('promote_story_to_canonical', 'shared_story_id')
    async def test_promote_revives_an_archived_legacy_story(self) -> None:
        """归档的旧剧本也能立为主剧本：先复活，否则迁移出来的主剧本也是归档的。"""
        service = self.make_service(onebot_config())
        self.make_story(story_id=PRIVATE_STORY_ID, userId='2', status='archived')

        result = await service.promote_story_to_canonical(PRIVATE_STORY_ID)

        self.assertEqual(await service.shared_story_id(), SHARED_STORY_ID)
        main = [row for row in self.rows('interlude_story') if row['id'] == SHARED_STORY_ID][0]
        self.assertEqual(main['status'], 'active')
        self.assertEqual(result['revived'], False)

    @needs('promote_story_to_canonical', 'shared_story_id')
    async def test_promote_is_refused_when_a_main_story_exists(self) -> None:
        service = self.make_service(onebot_config())
        self.make_story(story_id=SHARED_STORY_ID)
        self.make_story(story_id=PRIVATE_STORY_ID, userId='2')
        with self.assertRaises(ValueError):
            await service.promote_story_to_canonical(PRIVATE_STORY_ID)
        with self.assertRaises(ValueError):
            await service.promote_story_to_canonical(SHARED_STORY_ID)
        with self.assertRaises(LookupError):
            await service.promote_story_to_canonical('不存在')

    @needs('promote_story_to_canonical', 'shared_story_id')
    async def test_promote_revives_an_archived_main_story(self) -> None:
        """主剧本自己被人为归档/暂停过：复活它就是，不需要迁移。"""
        service = self.make_service(onebot_config())
        self.make_story(story_id=SHARED_STORY_ID, status='archived')
        result = await service.promote_story_to_canonical(SHARED_STORY_ID)
        self.assertEqual(result['target'], SHARED_STORY_ID)
        self.assertTrue(result['revived'])
        self.assertEqual(await service.shared_story_id(), SHARED_STORY_ID)

    @needs('canonical_story_id')
    async def test_canonical_story_id_prefers_the_character_story(self) -> None:
        service = self.make_service(onebot_config())
        self.assertEqual(await service.canonical_story_id(), '')
        self.make_story(story_id=PRIVATE_STORY_ID, updatedAt=STORY_TIME + timedelta(hours=2))
        self.make_story(story_id=SHARED_STORY_ID, updatedAt=STORY_TIME)
        self.assertEqual(await service.canonical_story_id(), SHARED_STORY_ID)


# =========================================================================== #
# 群成员名缓存（上游 `:1720-1748`）
# =========================================================================== #

class GroupSenderNameTests(ServiceHarness):

    @needs('group_sender_name')
    async def test_account_label_wins_and_skips_the_lookup(self) -> None:
        service = self.make_service()
        service.user_account_rule = lambda _user_id: {'label': 'Alice'}
        transport = _RecordingTransport()
        service.transport = transport
        self.assertEqual(await service.group_sender_name('9', '2', group_session()), 'Alice')
        self.assertEqual(transport.member_calls, [])
        self.assertEqual(service.group_member_name_cache, {})

    @needs('group_sender_name', 'lookup_group_member_name')
    async def test_member_name_is_looked_up_and_cached_for_twelve_hours(self) -> None:
        service = self.make_service()
        service.user_account_rule = lambda _user_id: None
        transport = _RecordingTransport(name='Bob')
        service.transport = transport
        self.assertEqual(await service.group_sender_name('group:9', '2', group_session()), 'Bob')
        self.assertEqual(transport.member_calls, [('9', '2')])
        cached = service.group_member_name_cache['9:2']
        self.assertEqual(cached['name'], 'Bob')
        self.assertEqual(cached['expires_at'], STORY_MS + 12 * 3_600_000)
        # 第二次命中缓存，不再查询；时钟推过 12 小时后再次查询。
        self.assertEqual(await service.group_sender_name('group:9', '2', group_session()), 'Bob')
        self.assertEqual(len(transport.member_calls), 1)
        self.clock['ms'] = STORY_MS + 12 * 3_600_000 + 1
        self.assertEqual(await service.group_sender_name('group:9', '2', group_session()), 'Bob')
        self.assertEqual(len(transport.member_calls), 2)

    @needs('group_sender_name', 'lookup_group_member_name')
    async def test_unknown_member_falls_back_to_the_user_id(self) -> None:
        service = self.make_service()
        service.user_account_rule = lambda _user_id: None
        service.transport = _RecordingTransport(name='')
        self.assertEqual(await service.group_sender_name('9', '2', group_session()), '2')

    @needs('group_sender_name', 'lookup_group_member_name')
    async def test_lookup_failure_falls_back_to_the_user_id(self) -> None:
        service = self.make_service()
        service.user_account_rule = lambda _user_id: None
        service.transport = _RecordingTransport(error=RuntimeError('network'))
        self.assertEqual(await service.group_sender_name('9', '2', group_session()), '2')
        self.assertEqual(service.group_member_name_cache, {})

    @needs('group_sender_name', 'lookup_group_member_name')
    async def test_concurrent_lookups_share_one_platform_call(self) -> None:
        service = self.make_service()
        service.user_account_rule = lambda _user_id: None
        transport = _RecordingTransport(name='Bob', delay=0.01)
        service.transport = transport
        results = await asyncio.gather(
            service.group_sender_name('9', '2', group_session()),
            service.group_sender_name('9', '2', group_session()),
        )
        self.assertEqual(results, ['Bob', 'Bob'])
        self.assertEqual(len(transport.member_calls), 1)

    @needs('lookup_group_member_name')
    async def test_transport_without_member_lookup_returns_empty(self) -> None:
        service = self.make_service()
        service.transport = object()
        self.assertEqual(await service.lookup_group_member_name('9:2', '9', '2', '1'), '')

    @needs('group_sender_name')
    async def test_username_is_used_when_the_account_rule_is_absent(self) -> None:
        service = self.make_service()
        service.user_account_rule = lambda _user_id: None
        self.assertEqual(
            await service.group_sender_name('9', '2', group_session(username='Carol')), 'Carol',
        )


class _RecordingTransport(NullTransport):
    """记录群成员查询的 transport 替身（其余方法走 NullTransport 的安全降级）。"""

    def __init__(self, name: str = '', error: Optional[BaseException] = None, delay: float = 0.0):
        self.name = name
        self.error = error
        self.delay = delay
        self.member_calls: list[tuple[str, str]] = []

    async def fetch_member_name(self, channel_id: str, user_id: str) -> str:
        self.member_calls.append((channel_id, user_id))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.name


# =========================================================================== #
# bufferGroupMessage（上游 `:1750`）
# =========================================================================== #

class BufferGroupMessageTests(ServiceHarness):

    def _message(self, content: str = 'hi') -> dict[str, Any]:
        return {
            'senderId': '2', 'senderName': 'Alice', 'speaker': '群成员「Alice」（QQ：2）',
            'messageId': '-9', 'messageRef': 'msg-1', 'content': content,
            'occurredAt': STORY_TIME, 'direction': 'user',
        }

    @needs('buffer_group_message')
    async def test_first_message_creates_the_turn(self) -> None:
        service = self.make_service()
        story = self.make_story()
        rule = group_rule_stub()
        service.buffer_group_message(story, rule, group_session(), self._message(), True, False)
        turn = service.buffered_group_turns['%s:9' % PRIVATE_STORY_ID]
        self.assertEqual(turn['story_id'], PRIVATE_STORY_ID)
        self.assertEqual(turn['group_id'], '9')
        self.assertEqual(turn['rule'], rule)
        self.assertEqual(turn['channel_id'], '9')
        self.assertEqual(turn['revision'], 1)
        self.assertTrue(turn['mentioned_bot'])
        self.assertFalse(turn['quoted_bot'])
        self.assertIsNotNone(turn['timer'])

    @needs('buffer_group_message')
    async def test_repeated_messages_increment_the_revision_and_accumulate_flags(self) -> None:
        service = self.make_service()
        story = self.make_story()
        rule = group_rule_stub()
        session = group_session()
        service.buffer_group_message(story, rule, session, self._message('a'), True, False)
        first_timer = service.buffered_group_turns['%s:9' % PRIVATE_STORY_ID]['timer']
        service.buffer_group_message(story, rule, session, self._message('b'), False, True)
        turn = service.buffered_group_turns['%s:9' % PRIVATE_STORY_ID]
        self.assertEqual(turn['revision'], 2)
        self.assertEqual([item['content'] for item in turn['messages']], ['a', 'b'])
        self.assertTrue(turn['mentioned_bot'])
        self.assertTrue(turn['quoted_bot'])
        # 旧计时器已被取消替换。
        self.assertIsNot(turn['timer'], first_timer)

    @needs('buffer_group_message')
    async def test_turns_are_keyed_per_story_and_group(self) -> None:
        service = self.make_service()
        story = self.make_story()
        other_story = self.make_story(story_id='character:onebot:1')
        service.buffer_group_message(story, group_rule_stub(), group_session(), self._message(), False, False)
        service.buffer_group_message(
            other_story, group_rule_stub(groupId='group:77'), group_session(), self._message(), False, False,
        )
        self.assertEqual(
            sorted(service.buffered_group_turns),
            sorted(['%s:9' % PRIVATE_STORY_ID, 'character:onebot:1:77']),
        )


# =========================================================================== #
# flushGroupTurn（上游 `:1769`）
# =========================================================================== #

class FlushGroupTurnTests(ServiceHarness):

    def _prepare(self, **rule_overrides: Any) -> tuple[Any, dict[str, Any]]:
        service = self.make_service()
        self.make_story()
        rule = group_rule_stub(debounceSeconds=0, **rule_overrides)
        service.buffered_group_turns['key'] = {
            'story_id': PRIVATE_STORY_ID, 'group_id': '9', 'rule': rule,
            'channel_id': '9', 'latest_session': group_session(), 'messages': [],
            'revision': 3, 'mentioned_bot': False, 'quoted_bot': False,
        }
        # Chunk7 / Chunk6 的成员尚未落地 → 测试替身。
        compacted: list[str] = []
        service.schedule_compaction = compacted.append
        service.schedule_conversation_follow_ups_after_turn = _noop
        return service, {'compacted': compacted, 'rule': rule}

    @needs('flush_group_turn')
    async def test_stale_revision_is_ignored(self) -> None:
        service, ctx = self._prepare()
        service.buffered_group_turns['key']['messages'].append({'content': 'hi'})
        await service.flush_group_turn('key', 2)
        self.assertEqual(len(service.buffered_group_turns['key']['messages']), 1)

    @needs('flush_group_turn')
    async def test_database_reset_blocks_flushing(self) -> None:
        service, _ctx = self._prepare()
        service.buffered_group_turns['key']['messages'].append({'content': 'hi'})
        service.database_resetting = True
        await service.flush_group_turn('key', 3)
        self.assertEqual(len(service.buffered_group_turns['key']['messages']), 1)

    @needs('flush_group_turn')
    async def test_paused_desktop_runtime_blocks_flushing(self) -> None:
        service, _ctx = self._prepare()
        service.buffered_group_turns['key']['messages'].append({'content': 'hi'})
        service.desktop_runtime_phase = 'paused'
        await service.flush_group_turn('key', 3)
        self.assertEqual(len(service.buffered_group_turns['key']['messages']), 1)

    @needs('flush_group_turn')
    async def test_an_in_flight_narration_reschedules_after_250ms(self) -> None:
        service, _ctx = self._prepare()
        service.buffered_group_turns['key']['messages'].append({'content': 'hi'})
        service.narrating_stories.add(PRIVATE_STORY_ID)
        await service.flush_group_turn('key', 3)
        self.assertIsNotNone(service.buffered_group_turns['key']['timer'])
        self.assertEqual(len(service.buffered_group_turns['key']['messages']), 1)

    @needs('flush_group_turn')
    async def test_empty_batch_drops_the_turn(self) -> None:
        service, _ctx = self._prepare()
        await service.flush_group_turn('key', 3)
        self.assertNotIn('key', service.buffered_group_turns)

    @needs('flush_group_turn', 'group_cooldown_active')
    async def test_willingness_below_threshold_skips_the_model(self) -> None:
        service, ctx = self._prepare(willingness={'enabled': True, 'threshold': 1.0})
        service.buffered_group_turns['key']['messages'].append({'content': 'hi'})

        async def explode(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError('模型不应被调用')

        service.try_decide = explode
        await service.flush_group_turn('key', 3)
        self.assertNotIn('key', service.buffered_group_turns)
        self.assertIn('群聊意愿未触发模型调用', self.sink.text())

    @needs('flush_group_turn', 'group_messages', 'group_cooldown_active')
    async def test_disabled_willingness_still_calls_the_model(self) -> None:
        service, ctx = self._prepare(willingness={'enabled': False})
        service.buffered_group_turns['key']['messages'].append({'content': 'hi'})
        seen: dict[str, Any] = {}

        async def decide(*args: Any, **_kwargs: Any) -> dict[str, Any]:
            seen['args'] = args
            return {'decision': {'groupReply': {'mode': 'none'}}, 'succeeded': True}

        async def persist(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {'messages': [], 'commit': None, 'scriptEntry': None}

        async def send(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {'deliveredSegments': ['你好'], 'complete': True,
                    'segmentOutcomes': [{'index': 0, 'content': '你好', 'status': 'delivered'}]}

        service.try_decide = decide
        service.persist_decision = persist
        service.send_group_message = send
        service.semantic_turn_embedding_enabled = lambda: False
        service.sticker_catalog_for_session = _empty_list
        service.group_chat_capabilities = lambda _session, _messages: None
        service.schedule_compaction = ctx['compacted'].append
        service.update_script_delivery_outcome = _noop

        await service.flush_group_turn('key', 3)

        self.assertIn('args', seen)
        self.assertEqual(seen['args'][2], 'user-message')
        self.assertIn('群聊消息准备进入主叙事', self.sink.text())
        # 投递成功 → 消耗意愿分数（上游 consumeGroupWillingness）。
        self.assertLess(service.group_willingness['key']['score'], 1.0)
        self.assertEqual(ctx['compacted'], [PRIVATE_STORY_ID])
        self.assertNotIn('key', service.buffered_group_turns)

    @needs('flush_group_turn', 'group_cooldown_active')
    async def test_cooldown_skips_the_model(self) -> None:
        service, _ctx = self._prepare(cooldownSeconds=600)
        self.make_entry(entry_id=1, kind='character-group-message', occurred_at=STORY_TIME,
                        metadata={'groupId': '9'})
        service.buffered_group_turns['key']['messages'].append({'content': 'hi'})

        async def explode(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError('冷却期内模型不应被调用')

        service.try_decide = explode
        await service.flush_group_turn('key', 3)
        self.assertNotIn('key', service.buffered_group_turns)
        self.assertIn('群聊仍在冷却期', self.sink.text())

    @needs('flush_group_turn', 'group_messages', 'group_cooldown_active', 'append_entry')
    async def test_partial_delivery_is_recorded_on_the_group_entry(self) -> None:
        service, ctx = self._prepare()
        service.buffered_group_turns['key']['messages'].append({'content': 'hi'})
        commit = {
            'events': [{
                'commit_id': 'commit:x', 'event_id': 'commit:x:e2', 'kind': 'group-message',
                'actor': 'character', 'delivery_mode': 'immediate',
                'caused_by_event_ids': ['commit:x:e1'], 'participant_id': '',
                'content': '第一段<sep/>第二段', 'bubbles': ['第一段', '第二段'],
            }],
        }
        outcomes: list[tuple[Any, Any, Any]] = []

        async def decide(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {'decision': {'groupReply': {'mode': 'immediate', 'content': '第一段'}},
                    'succeeded': True}

        async def persist(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {'messages': [], 'commit': commit, 'scriptEntry': {'id': 42}, 'script_entry': {'id': 42}}

        async def send(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {
                'deliveredSegments': ['第一段'], 'complete': False,
                'segmentOutcomes': [
                    {'index': 0, 'content': '第一段', 'status': 'delivered'},
                    {'index': 1, 'content': '第二段', 'status': 'failed', 'reason': 'boom'},
                ],
            }

        async def record(story_id: str, reference: Any, status: Any, at: Any, reason: Any) -> None:
            outcomes.append((reference, status, reason))

        service.try_decide = decide
        service.persist_decision = persist
        service.send_group_message = send
        service.semantic_turn_embedding_enabled = lambda: False
        service.sticker_catalog_for_session = _empty_list
        service.group_chat_capabilities = lambda _session, _messages: None
        service.schedule_compaction = ctx['compacted'].append
        service.update_script_delivery_outcome = record

        await service.flush_group_turn('key', 3)

        self.assertEqual(
            [(item[0]['segment_index'], item[1]) for item in outcomes],
            [(0, 'delivered'), (1, 'failed')],
        )
        entries = [row for row in self.rows('interlude_script_entry')
                   if row['kind'] == 'character-group-message']
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['content'], '第一段')
        metadata = entries[0]['metadata']
        self.assertEqual(metadata['groupId'], '9')
        self.assertEqual(metadata['deliverySegmentIndexes'], [0])
        self.assertTrue(metadata['partialDelivery'])
        self.assertEqual(metadata['deliveredSegments'], 1)
        self.assertEqual(metadata['commit_id'], 'commit:x')


async def _empty_list(*_args: Any, **_kwargs: Any) -> list[Any]:
    """替身：`stickerCatalogForSession` 等返回空列表的成员。"""
    return []


async def _noop(*_args: Any, **_kwargs: Any) -> None:
    """替身：尚未落地的 Chunk6/7 成员（无返回值）。"""
    return None


# =========================================================================== #
# 上游用例的在本层的重放（见文件头「归属核对」）
# =========================================================================== #

class UpstreamBehaviourPortTests(ServiceHarness):
    """上游用例在本层成员上的行为重放（每条标注上游出处）。"""

    @needs('receive_group', 'append_entry', 'buffer_group_message')
    async def test_upstream_group_speaker_labels_retain_display_name_and_qq(self) -> None:
        """上游 `group-identity.test.ts`：`group speaker labels retain both display name
        and stable QQ identity`。

        上游在 `formatGroupSpeaker` 上断言字面量；本层（`receiveGroup` → 缓冲消息）
        是它的消费者，因此这里断言同一条身份标签**经本块成员**落到群上下文里。
        """
        service = self.make_service(onebot_config(onebot={'groupChats': [group_rule_stub()]}))
        story_id = 'onebot:1:2171322646'
        self.make_story(story_id=story_id, userId='2171322646')
        service.user_account_rule = lambda _user_id: {'label': '渔社'}
        self.assertTrue(await service.receive_group(group_session(user_id='2171322646'), STORY_TIME))
        # 共享模式下这条按账号剧本在同一次调用里被迁移成 `character:onebot:1`
        # （上游 `findStory` → `migrateLegacyStory`），群缓冲挂在迁移后的 id 上。
        self.assertEqual(
            [row['id'] for row in self.rows('interlude_story') if row['status'] == 'active'],
            [SHARED_STORY_ID],
        )
        turn = service.buffered_group_turns['%s:9' % SHARED_STORY_ID]
        self.assertEqual(
            turn['messages'][0]['speaker'], '群成员「渔社」（QQ：2171322646）',
        )

    @needs('flush_group_turn', 'group_messages', 'group_cooldown_active')
    async def test_upstream_disabled_willingness_keeps_always_triggering(self) -> None:
        """上游 `group-willingness.test.ts`：`disabled group willingness preserves the
        existing always-trigger behavior`。

        上游断言 `evaluateGroupWillingness(..., {enabled:false})` → `shouldCall=true`；
        本层是它的唯一调用点，因此断言"意愿关闭时仍然进主叙事"。
        """
        service = self.make_service()
        self.make_story()
        service.buffered_group_turns['key'] = {
            'story_id': PRIVATE_STORY_ID, 'group_id': '9',
            'rule': group_rule_stub(debounceSeconds=0, cooldownSeconds=0,
                                    willingness={'enabled': False}),
            'channel_id': '9', 'latest_session': group_session(),
            'messages': [{'content': '普通消息'}], 'revision': 1,
            'mentioned_bot': False, 'quoted_bot': False,
        }
        called: list[Any] = []

        async def decide(*args: Any, **_kwargs: Any) -> dict[str, Any]:
            called.append(args)
            return {'decision': {'groupReply': {'mode': 'none'}}, 'succeeded': True}

        async def persist(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {'messages': [], 'commit': None, 'scriptEntry': None}

        async def send(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {'deliveredSegments': [], 'complete': False, 'segmentOutcomes': []}

        service.try_decide = decide
        service.persist_decision = persist
        service.send_group_message = send
        service.semantic_turn_embedding_enabled = lambda: False
        service.sticker_catalog_for_session = _empty_list
        service.group_chat_capabilities = lambda _session, _messages: None
        service.schedule_compaction = lambda _story_id: None
        service.schedule_conversation_follow_ups_after_turn = _noop
        service.update_script_delivery_outcome = _noop

        await service.flush_group_turn('key', 1)
        self.assertEqual(len(called), 1)

    @needs('flush_group_turn', 'group_chat_capabilities')
    async def test_upstream_group_actions_only_accept_advertised_targets(self) -> None:
        """上游 `group-identity.test.ts`：`chat action validation accepts only advertised
        actions and supplied message references`。

        上游直接测 helper；这里断言本块成员喂进去的 `groupContext` 是上游形状
        （camelCase `messageRef` / `messageId`），否则 `replyTo` / `messageReactions`
        会被静默丢弃。
        """
        service = self.make_service()
        self.make_story()
        service.buffered_group_turns['key'] = {
            'story_id': PRIVATE_STORY_ID, 'group_id': '9',
            'rule': group_rule_stub(debounceSeconds=0, cooldownSeconds=0),
            'channel_id': '9', 'latest_session': group_session(),
            'messages': [{'content': 'hi'}], 'revision': 1,
            'mentioned_bot': False, 'quoted_bot': False,
        }
        captured: dict[str, Any] = {}

        async def messages(story_id: str, group_id: str, limit: int) -> list[dict[str, Any]]:
            return [{
                'sender_id': '200', 'sender_name': '成员', 'speaker': '群成员「成员」（QQ：200）',
                'message_ref': 'msg-7', 'message_id': '-12345', 'content': '这条消息可以被操作',
                'occurred_at': STORY_TIME, 'direction': 'user',
            }]

        def capabilities(_session: Any, seen_messages: Any) -> dict[str, Any]:
            captured['messages'] = seen_messages
            captured['wire'] = _chat_capabilities_wire({
                'platform': 'qq', 'quote_reply': True,
                'reactions': ['like', 'heart'], 'native_faces': [],
                'expression_threshold': 0.7,
            })
            return captured['wire']

        async def decide(*args: Any, **_kwargs: Any) -> dict[str, Any]:
            captured['group_context'] = args[8]
            captured['chat_capabilities'] = args[11]
            return {'decision': {
                'groupReply': {'mode': 'immediate', 'content': '指定回复', 'replyTo': 'msg-7'},
                'messageReactions': [{'messageRef': 'msg-7', 'reaction': 'heart'}],
            }, 'succeeded': True}

        persisted: dict[str, Any] = {}

        async def persist(_story: Any, _participant: Any, raw: Any, *_args: Any, **_kwargs: Any) -> Any:
            persisted['raw'] = raw
            commit = {'events': [{
                'commit_id': 'commit:x', 'event_id': 'commit:x:e2', 'kind': 'group-message',
                'actor': 'character', 'delivery_mode': 'immediate',
                'caused_by_event_ids': ['commit:x:e1'], 'participant_id': '',
                'content': '指定回复', 'bubbles': ['指定回复'],
            }]}
            return {'messages': [], 'commit': commit, 'scriptEntry': {'id': 7}, 'script_entry': {'id': 7}}

        sent: dict[str, Any] = {}

        async def send(*args: Any, **_kwargs: Any) -> dict[str, Any]:
            sent['args'] = args
            return {'deliveredSegments': [], 'complete': False, 'segmentOutcomes': []}

        service.group_messages = messages
        service.group_chat_capabilities = capabilities
        service.try_decide = decide
        service.persist_decision = persist
        service.send_group_message = send
        service.semantic_turn_embedding_enabled = lambda: False
        service.sticker_catalog_for_session = _empty_list
        service.schedule_compaction = lambda _story_id: None
        service.schedule_conversation_follow_ups_after_turn = _noop
        service.update_script_delivery_outcome = _noop

        await service.flush_group_turn('key', 1)

        # 进模型/适配层的群上下文与能力声明都是上游 wire 形状。
        self.assertEqual(captured['group_context']['messages'][0]['messageRef'], 'msg-7')
        self.assertEqual(captured['group_context']['messages'][0]['messageId'], '-12345')
        self.assertEqual(captured['chat_capabilities']['quoteReply'], True)
        self.assertEqual(captured['messages'], captured['group_context']['messages'])
        # 传入 persistDecision 的动作引用被剥成上游允许的形状。
        self.assertEqual(
            persisted['raw']['messageReactions'], [{'messageRef': 'msg-7', 'reaction': 'heart'}],
        )
        # 群回复带上了平台回复目标（messageId 来自被引用的入站消息）。
        self.assertEqual(sent['args'][3], '-12345')


if __name__ == '__main__':
    unittest.main()
