"""`plugin/core/service` 基础层（`base.py` / `session.py` / `transport.py`）的单元测试。

覆盖范围刻意只挑**可独立验证**的部分（不依赖其它 9 个 chunk、不依赖模型）：

- 键名法工具：`pick` / `normalize_account_id` / `is_one_bot_platform` /
  `is_enabled_account` / `normalize_group_id` / id 生成 / `merge_setting`
- `InterludeContext`：计时器、`bots()`、ready 事件、注入点
- `SessionView`：字段清单 + Koishi `Session` 的读取等价物
- `Transport` / `NullTransport`：协议方法齐全 + 全部安全降级
- `ServiceBase`：`src/service.ts:624-717` 的全部字段默认值
- `ServiceChunk0`：`write()` 串行化、`run_in_queue()` 队列串行化、构造期行为、
  以及不依赖数据库的判定分支（`canHandleSession` / `canHandleGroupSession` /
  `groupRule` / `canManageSession` / `canHandleParticipant` / `canHandleStory`）

运行：`python3 -m unittest plugin.tests.test_service_base -v`
"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any

from plugin.core import logging as interlude_logging
from plugin.core.database import Database
from plugin.core.service import (
    InterludeContext,
    InterludeService,
    NullTransport,
    ServiceBase,
    ServiceChunk0,
    SessionView,
    TimerHandle,
    Transport,
    is_enabled_account,
    is_one_bot_platform,
    legacy_story_id_for,
    merge_setting,
    normalize_account_id,
    normalize_group_id,
    participant_id_for,
    participant_id_for_story,
    pick,
    same_participant_endpoint,
    story_id_for_character,
)
from plugin.core.service.base import (
    is_enabled_account as base_is_enabled_account,
    is_one_bot_platform as base_is_one_bot_platform,
    normalize_account_id as base_normalize_account_id,
    normalize_database_row,
)

# =========================================================================== #
# 测试夹具
# =========================================================================== #

#: `ServiceBase.__init__` 必须逐一声明的全部实例字段（`src/service.ts:624-717`）。
REQUIRED_FIELDS = (
    'narrator', 'compactor', 'embedder', 'sticker_describer', 'vision_describer',
    'sticker_catalog', 'history_vectors', 'history_vectors_ready', 'history_vector_loads',
    'history_backfills', 'history_backoff', 'automatic_recall_cache',
    'schedule_preplan_backoff', 'timeline_backoff', 'timeline_director_failures',
    'compaction_backoff', 'sticker_by_id', 'sticker_scan_running', 'queues',
    'buffered_narrative_turns', 'buffered_group_turns', 'group_member_name_cache',
    'group_member_name_lookups', 'group_willingness', 'due_intent_wake_timers',
    'interrupted_typing_participants', 'narrating_stories', 'fact_backfills',
    'scheduled_compactions', 'scheduled_alter_analyses', 'database_write_queue',
    'browser_active', 'browser_waiters', 'service_logger', 'background_started',
    'database_resetting', 'sweep_running', 'compaction_sweep_running',
    'blind_mode_health_issue', 'model_routing', 'reported_state_migrations',
    'desktop_runtime_phase', 'ctx', 'config', 'db', 'transport',
    'desktop_event_sink', 'desktop_delivery_handler',
    'cached_audio_config', 'cached_sticker_config', 'cached_alter_system_config',
    'cached_agency_config', 'cached_schedule_preplan_config', 'cached_blind_mode_config',
    'cached_auto_advance_config', 'cached_shared_story_config', 'cached_memory_config',
    'cached_browser_config',
)


def make_config(**overrides: Any) -> dict[str, Any]:
    """一份最小可用配置（只含本层读到的键，其余走默认值）。"""
    config: dict[str, Any] = {
        'model': {},
        'runtime': {},
        'storyDefaults': {},
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

    def text(self) -> str:
        return '\n'.join(text for _level, text in self.records)


class ServiceFixtureMixin:
    """共享的构造夹具：内存数据库 + 内存日志 sink + NullTransport。

    不继承 `unittest.TestCase`，以便同时混入同步与异步测试基类
    （`IsolatedAsyncioTestCase` 必须在 MRO 中靠前，否则它的 `run` 会被覆盖）。
    """

    def setUp(self) -> None:
        self.sink = _Sink()
        interlude_logging.set_log_sink(self.sink)
        self.addCleanup(interlude_logging.set_log_sink, interlude_logging._default_sink)
        self.db = Database(':memory:')
        self.addCleanup(self.db.close)
        self.db.register_tables()

    def make_service(self, config: dict[str, Any] | None = None, **kwargs: Any) -> ServiceChunk0:
        """构造一个不启动后台调度的 `ServiceChunk0`（同步上下文，无 running loop）。"""
        ctx = InterludeContext(logger=None, database=self.db)
        return ServiceChunk0(ctx, config or make_config(), self.db, NullTransport(), **kwargs)

    def make_context(self) -> InterludeContext:
        return InterludeContext(logger=None, database=self.db)


class ServiceTestCase(ServiceFixtureMixin, unittest.TestCase):
    """同步测试基类。"""


class AsyncServiceTestCase(unittest.IsolatedAsyncioTestCase, ServiceFixtureMixin):
    """异步测试基类（`IsolatedAsyncioTestCase` 必须排在前面）。"""

    def setUp(self) -> None:
        # 显式转发：`IsolatedAsyncioTestCase` 排在 MRO 前面，避免夹具被绕过。
        ServiceFixtureMixin.setUp(self)


# =========================================================================== #
# 键名法工具
# =========================================================================== #

class PickTests(unittest.TestCase):
    """`pick(value, camel, snake=None)`：外部读入双读，优先 camelCase。"""

    def test_prefers_camel_case(self) -> None:
        self.assertEqual(pick({'activityLoad': 'free', 'activity_load': 'occupied'}, 'activityLoad', 'activity_load'), 'free')

    def test_falls_back_to_snake_case(self) -> None:
        self.assertEqual(pick({'activity_load': 'occupied'}, 'activityLoad', 'activity_load'), 'occupied')

    def test_camel_case_none_still_wins(self) -> None:
        # 上游 `'key' in value` 的判断：键存在即返回，哪怕是 None。
        self.assertIsNone(pick({'activityLoad': None, 'activity_load': 'free'}, 'activityLoad', 'activity_load'))

    def test_non_dict_is_none(self) -> None:
        for value in (None, 3, 'text', [1, 2]):
            self.assertIsNone(pick(value, 'a', 'b'))

    def test_without_snake_argument(self) -> None:
        self.assertEqual(pick({'a': 1}, 'a'), 1)
        self.assertIsNone(pick({'b': 1}, 'a'))


class NormalizeAccountIdTests(unittest.TestCase):
    """上游 `normalizeAccountId`（`src/service.ts:7682`）。"""

    def test_strips_known_prefixes(self) -> None:
        self.assertEqual(normalize_account_id('private:123'), '123')
        self.assertEqual(normalize_account_id('user:123'), '123')
        self.assertEqual(normalize_account_id('onebot:123'), '123')
        self.assertEqual(normalize_account_id('napcat:123'), '123')
        self.assertEqual(normalize_account_id('QQ:123'), '123')

    def test_strips_at_most_three_layers(self) -> None:
        # 上游循环三次：`qq:onebot:private:123` → 逐层剥到 `123`。
        self.assertEqual(normalize_account_id('qq:onebot:private:123'), '123')
        # 第四层不再剥（上游同样只跑三次）。
        self.assertEqual(normalize_account_id('qq:qq:qq:qq:123'), 'qq:123')

    def test_lowercases_and_trims(self) -> None:
        self.assertEqual(normalize_account_id('  PRIVATE:ABC  '), 'abc')

    def test_empty_values(self) -> None:
        self.assertEqual(normalize_account_id(None), '')
        self.assertEqual(normalize_account_id(''), '')

    def test_matches_base_module_export(self) -> None:
        self.assertIs(base_normalize_account_id, normalize_account_id)


class OneBotPlatformTests(unittest.TestCase):
    """上游 `isOneBotPlatform`（`src/service.ts:7026`）。"""

    def test_known_onebot_platforms(self) -> None:
        for value in ('onebot', 'onebot:123', 'napcat', 'napcat:x', 'qq:onebot', 'qq:onebot:1', 'OneBot'):
            self.assertTrue(is_one_bot_platform(value), value)

    def test_other_platforms(self) -> None:
        for value in ('telegram', 'discord', 'satori', '', None, 'qq'):
            self.assertFalse(is_one_bot_platform(value), value)

    def test_matches_base_module_export(self) -> None:
        self.assertIs(base_is_one_bot_platform, is_one_bot_platform)


class EnabledAccountTests(unittest.TestCase):
    """上游 `isEnabledAccount`（`src/service.ts:7705`）：`enabled !== false` 才计数。"""

    def test_matches_normalized_qq(self) -> None:
        accounts = [{'qq': 'private:123', 'enabled': True}]
        self.assertTrue(is_enabled_account(accounts, '123'))
        self.assertTrue(is_enabled_account(accounts, 'onebot:123'))
        self.assertFalse(is_enabled_account(accounts, '456'))

    def test_explicitly_disabled_account_is_skipped(self) -> None:
        accounts = [{'qq': '123', 'enabled': False}]
        self.assertFalse(is_enabled_account(accounts, '123'))

    def test_missing_enabled_counts_as_enabled(self) -> None:
        # 上游 `account.enabled !== false`。
        self.assertTrue(is_enabled_account([{'qq': '123'}], '123'))

    def test_blank_qq_never_matches(self) -> None:
        self.assertFalse(is_enabled_account([{'qq': '123'}], ''))
        self.assertFalse(is_enabled_account([{'qq': '123'}], None))

    def test_empty_or_invalid_list(self) -> None:
        self.assertFalse(is_enabled_account([], '123'))
        self.assertFalse(is_enabled_account(None, '123'))

    def test_matches_base_module_export(self) -> None:
        self.assertIs(base_is_enabled_account, is_enabled_account)


class IdentifierTests(unittest.TestCase):
    """上游 id 生成器（`src/service.ts:7009-7017`）与 `sessionGroupId` 归一化。"""

    def test_story_id_for_character(self) -> None:
        self.assertEqual(story_id_for_character('onebot', '10000'), 'character:onebot:10000')

    def test_legacy_and_participant_ids_agree(self) -> None:
        self.assertEqual(legacy_story_id_for('onebot', '1', '2'), 'onebot:1:2')
        self.assertEqual(participant_id_for('onebot', '1', '2'), 'onebot:1:2')

    def test_participant_id_for_story_is_capped_at_255(self) -> None:
        long_story = 's' * 400
        value = participant_id_for_story(long_story, 'onebot', '1', '2')
        self.assertEqual(len(value), 255)
        self.assertTrue(value.startswith('onebot:1:2:'))

    def test_normalize_group_id_strips_group_and_guild(self) -> None:
        self.assertEqual(normalize_group_id('group:123'), '123')
        self.assertEqual(normalize_group_id('GUILD:123'), '123')
        self.assertEqual(normalize_group_id(' 123 '), '123')
        self.assertEqual(normalize_group_id(''), '')
        self.assertEqual(normalize_group_id(None), '')

    def test_same_participant_endpoint(self) -> None:
        participant = {'platform': 'onebot', 'selfId': '1', 'userId': 'private:2'}
        self.assertTrue(same_participant_endpoint(participant, SessionView(platform='onebot', self_id='1', user_id='2')))
        self.assertFalse(same_participant_endpoint(participant, SessionView(platform='onebot', self_id='1', user_id='3')))
        # 不同平台族不互相匹配。
        self.assertFalse(same_participant_endpoint(participant, SessionView(platform='telegram', self_id='1', user_id='2')))


class MergeSettingTests(unittest.TestCase):
    """上游 `mergeSetting`（`src/service.ts:8078`）：顶层浅合并 + character/user 浅合并。"""

    def test_nested_character_and_user_merge(self) -> None:
        base = {'character': {'name': 'A', 'profile': 'p'}, 'user': {'display_name': 'U', 'profile': 'x'}, 'world': 'w'}
        patch = {'character': {'name': 'B'}, 'relationship': 'r'}
        merged = merge_setting(base, patch)
        self.assertEqual(merged['character'], {'name': 'B', 'profile': 'p'})
        self.assertEqual(merged['user'], {'display_name': 'U', 'profile': 'x'})
        self.assertEqual(merged['world'], 'w')
        self.assertEqual(merged['relationship'], 'r')

    def test_missing_nested_sections_preserved(self) -> None:
        base = {'character': {'name': 'A'}, 'user': {'display_name': 'U'}}
        merged = merge_setting(base, {'world': 'w'})
        self.assertEqual(merged['character'], {'name': 'A'})
        self.assertEqual(merged['user'], {'display_name': 'U'})

    def test_tolerates_non_dict_inputs(self) -> None:
        self.assertEqual(merge_setting(None, None), {'character': {}, 'user': {}})
        self.assertEqual(merge_setting({}, {'character': 'bad'}), {'character': {}, 'user': {}})

    def test_string_patch_is_rejected_and_base_kept(self) -> None:
        base = {'character': {'name': 'A'}, 'user': {'display_name': 'U'}}
        self.assertEqual(
            merge_setting(base, {'character': 'bad'}),
            {'character': {'name': 'A'}, 'user': {'display_name': 'U'}},
        )


# =========================================================================== #
# InterludeContext
# =========================================================================== #

class InterludeContextTests(unittest.IsolatedAsyncioTestCase):
    """Koishi `Context` 的等价容器。"""

    async def test_defaults(self) -> None:
        ctx = InterludeContext()
        self.assertIsNone(ctx.logger)
        self.assertIsNone(ctx.database)
        self.assertIsNone(ctx.provider_resolver)
        self.assertEqual(ctx.base_dir, '')
        self.assertEqual(ctx.bots(), [])
        self.assertIsNone(await ctx.http_get('https://example.com'))
        self.assertTrue(0.0 <= ctx.random() < 1.0)
        self.assertIsNotNone(ctx.clock())

    async def test_injected_sources(self) -> None:
        ctx = InterludeContext(
            logger='L', database='DB', provider_resolver='PR',
            clock=lambda: 42, random=lambda: 0.25,
            bots=lambda: ['bot1'], base_dir='/tmp/root',
        )
        self.assertEqual(ctx.logger, 'L')
        self.assertEqual(ctx.database, 'DB')
        self.assertEqual(ctx.provider_resolver, 'PR')
        self.assertEqual(ctx.clock(), 42)
        self.assertEqual(ctx.random(), 0.25)
        self.assertEqual(ctx.bots(), ['bot1'])
        self.assertEqual(ctx.base_dir, '/tmp/root')

    async def test_set_timeout_fires_and_cancels(self) -> None:
        ctx = InterludeContext()
        fired: list[str] = []
        handle = ctx.set_timeout(lambda: fired.append('a'), 1)
        self.assertIsInstance(handle, TimerHandle)
        await asyncio.sleep(0.02)
        self.assertEqual(fired, ['a'])
        # 再取消一个尚未触发的计时器。
        handle2 = ctx.set_timeout(lambda: fired.append('b'), 100_000)
        handle2.cancel()
        handle2()  # 可调用式取消：等价 `turn.timer()`。
        await asyncio.sleep(0)
        self.assertEqual(fired, ['a'])

    async def test_set_interval_repeats_until_cancelled(self) -> None:
        ctx = InterludeContext()
        ticks: list[int] = []
        handle = ctx.set_interval(lambda: ticks.append(1), 1)
        await asyncio.sleep(0.02)
        handle.cancel()
        count = len(ticks)
        self.assertGreaterEqual(count, 2)
        await asyncio.sleep(0.02)
        self.assertEqual(len(ticks), count)

    async def test_coroutine_callback_is_scheduled(self) -> None:
        ctx = InterludeContext()
        seen: list[str] = []

        async def work() -> None:
            seen.append('done')

        ctx.set_timeout(lambda: work(), 1)
        await asyncio.sleep(0.02)
        self.assertEqual(seen, ['done'])

    async def test_callback_exception_is_swallowed(self) -> None:
        ctx = InterludeContext()

        def boom() -> None:
            raise RuntimeError('boom')

        ctx.set_timeout(boom, 1)
        await asyncio.sleep(0.02)  # 不抛异常即通过

    async def test_ready_handlers(self) -> None:
        ctx = InterludeContext()
        seen: list[str] = []
        ctx.on_ready(lambda: seen.append('ready'))
        self.assertEqual(seen, [])
        ctx.emit_ready()
        self.assertEqual(seen, ['ready'])


# =========================================================================== #
# SessionView
# =========================================================================== #

SESSION_FIELDS = (
    'platform', 'self_id', 'user_id', 'channel_id', 'guild_id', 'is_direct',
    'content', 'elements', 'quote', 'message_id', 'event',
)


class SessionViewTests(unittest.TestCase):
    """`SessionView`：字段清单 + service 层用到的 Koishi `Session` 读取。"""

    def test_field_list(self) -> None:
        view = SessionView()
        for field in SESSION_FIELDS:
            self.assertTrue(hasattr(view, field), field)

    def test_defaults(self) -> None:
        view = SessionView()
        self.assertEqual(view.platform, '')
        self.assertEqual(view.self_id, '')
        self.assertIsNone(view.quote)
        self.assertIsNone(view.message_id)
        self.assertIsNone(view.event)
        self.assertEqual(view.elements, [])
        self.assertFalse(view.is_direct)

    def test_mutable_defaults_are_per_instance(self) -> None:
        first = SessionView()
        second = SessionView()
        first.elements.append({'type': 'text'})
        self.assertEqual(second.elements, [])

    def test_session_group_id_prefers_guild(self) -> None:
        self.assertEqual(SessionView(guild_id='group:9', channel_id='1').session_group_id(), '9')
        self.assertEqual(SessionView(channel_id='guild:8').session_group_id(), '8')
        self.assertEqual(SessionView().session_group_id(), '')

    def test_quote_readers(self) -> None:
        view = SessionView(
            self_id='10000',
            quote={'id': 'm-quoted', 'user': {'id': 'private:20000'}, 'content': 'hi'},
        )
        # 上游 `describeQuotedMessage` 原样保留 `quote.user.id`，不做前缀归一化。
        self.assertEqual(view.quote_user_id(), 'private:20000')
        self.assertEqual(view.quote_message_id(), 'm-quoted')
        self.assertEqual(view.quote_content(), 'hi')
        self.assertFalse(view.quoted_bot())

    def test_quoted_bot_true_for_self_sender(self) -> None:
        view = SessionView(self_id='10000', quote={'user': {'id': '10000'}})
        self.assertTrue(view.quoted_bot())

    def test_quoted_bot_false_for_other_sender(self) -> None:
        view = SessionView(self_id='10000', quote={'user': {'id': '20000'}})
        self.assertFalse(view.quoted_bot())

    def test_quote_readers_without_quote(self) -> None:
        view = SessionView()
        self.assertEqual(view.quote_user_id(), '')
        self.assertEqual(view.quote_message_id(), '')
        self.assertIsNone(view.quote_content())
        self.assertIsNone(view.message_index())
        self.assertFalse(view.quoted_bot())

    def test_mentioned_bot(self) -> None:
        elements = [{'type': 'text'}, {'type': 'at', 'attrs': {'id': '10000'}}]
        self.assertTrue(SessionView(self_id='10000', elements=elements).mentioned_bot())
        self.assertFalse(SessionView(self_id='99999', elements=elements).mentioned_bot())
        self.assertFalse(SessionView(self_id='10000').mentioned_bot())

    def test_snake_case_quote_user_key_is_accepted(self) -> None:
        # 键名法：外部读入的数据可能用 snake_case。
        view = SessionView(quote={'message_id': 'm1', 'user': {'user_id': '7'}})
        self.assertEqual(view.quote_user_id(), '7')
        self.assertEqual(view.quote_message_id(), 'm1')


# =========================================================================== #
# Transport / NullTransport
# =========================================================================== #

TRANSPORT_METHODS = (
    'send_private', 'send_group', 'send_session', 'send_image', 'send_sticker',
    'send_native_face', 'react', 'fetch_member_name', 'fetch_image', 'fetch_audio',
    'list_sticker_files', 'search_web', 'visit_web', 'deliver_background',
)


class TransportTests(unittest.IsolatedAsyncioTestCase):
    """`Transport` 协议 + `NullTransport` 的安全降级。"""

    def test_protocol_declares_every_method(self) -> None:
        for name in TRANSPORT_METHODS:
            self.assertTrue(callable(getattr(Transport, name, None)), name)

    def test_null_transport_satisfies_protocol(self) -> None:
        self.assertIsInstance(NullTransport(), Transport)

    async def test_delivery_methods_fail_safely(self) -> None:
        transport = NullTransport()
        for result in (
            await transport.send_private({'id': 'p'}, 'hello'),
            await transport.send_group('c', 'hello'),
            await transport.send_session(SessionView(), 'hello'),
            await transport.send_image('c', '/tmp/x.png'),
            await transport.send_sticker('c', '/tmp/x.png'),
            await transport.send_native_face('c', '14'),
            await transport.deliver_background({'kind': 'private'}),
        ):
            self.assertEqual(result, {'ok': False, 'error': 'transport-unavailable'})

    async def test_reply_to_argument_is_accepted(self) -> None:
        transport = NullTransport()
        self.assertFalse((await transport.send_private({'id': 'p'}, 'x', 'msg-1'))['ok'])
        self.assertFalse((await transport.send_group('c', 'x', 'msg-1'))['ok'])

    async def test_read_methods_degrade_to_empty(self) -> None:
        transport = NullTransport()
        self.assertFalse(await transport.react('msg-1', 'like'))
        self.assertEqual(await transport.fetch_member_name('c', 'u'), '')
        self.assertIsNone(await transport.fetch_image('https://example.com/a.png'))
        self.assertIsNone(await transport.fetch_audio('https://example.com/a.mp3'))
        self.assertEqual(await transport.list_sticker_files('/root'), [])

    async def test_web_methods_degrade_to_empty(self) -> None:
        transport = NullTransport()
        self.assertEqual(await transport.search_web('query', 1000), [])
        self.assertIsNone(await transport.visit_web('https://example.com', 1000))


# =========================================================================== #
# ServiceBase
# =========================================================================== #

class ServiceBaseFieldTests(ServiceTestCase):
    """`src/service.ts:624-717` 的字段必须逐一声明且默认值照抄。"""

    def test_all_fields_declared(self) -> None:
        service = self.make_service()
        for field in REQUIRED_FIELDS:
            self.assertTrue(hasattr(service, field), field)

    def test_container_defaults(self) -> None:
        service = self.make_service()
        for field in (
            'sticker_catalog', 'history_vectors', 'history_vectors_ready', 'history_vector_loads',
            'history_backfills', 'history_backoff', 'automatic_recall_cache',
            'schedule_preplan_backoff', 'timeline_backoff', 'timeline_director_failures',
            'compaction_backoff', 'sticker_by_id', 'queues', 'buffered_narrative_turns',
            'buffered_group_turns', 'group_member_name_cache', 'group_member_name_lookups',
            'group_willingness', 'due_intent_wake_timers', 'interrupted_typing_participants',
            'narrating_stories', 'fact_backfills', 'scheduled_compactions',
            'scheduled_alter_analyses', 'reported_state_migrations', 'browser_waiters',
        ):
            value = getattr(service, field)
            self.assertEqual(len(value), 0, field)

    def test_provider_defaults(self) -> None:
        # `narrator.py` 落地后 `_create_providers` 会注入真实提供者；
        # 未落地时必须保持 None（上游"模型未配置"的降级态）。
        service = self.make_service()
        for field, method in (
            ('narrator', 'decide'),
            ('compactor', 'compact'),
            ('embedder', 'embed'),
        ):
            provider = getattr(service, field)
            if provider is not None:
                self.assertTrue(callable(getattr(provider, method, None)), field)

    def test_providers_can_be_replaced(self) -> None:
        service = self.make_service()
        service.set_narrator(None)
        service.set_compactor(None)
        service.set_embedder(None)
        self.assertIsNone(service.get_narrator())
        self.assertIsNone(service.narrator)
        self.assertIsNone(service.compactor)
        self.assertIsNone(service.embedder)

    def test_scalar_defaults(self) -> None:
        service = self.make_service()
        self.assertIsNone(service.desktop_event_sink)
        self.assertIsNone(service.desktop_delivery_handler)
        self.assertFalse(service.sticker_scan_running)
        self.assertFalse(service.background_started)
        self.assertFalse(service.database_resetting)
        self.assertFalse(service.sweep_running)
        self.assertFalse(service.compaction_sweep_running)
        self.assertFalse(service.blind_mode_health_issue)
        self.assertEqual(service.browser_active, 0)
        self.assertEqual(service.desktop_runtime_phase, 'running')
        self.assertEqual(service.get_desktop_runtime_phase(), 'running')

    def test_cached_configs_start_empty(self) -> None:
        service = self.make_service()
        # 构造期会解析 blindMode（`_create_providers` 需要它）；抓取当前真相即可。
        self.assertIsInstance(service.cached_blind_mode_config, dict)
        # 其余缓存要么尚未解析（None），要么已被本次构造路径解析成 dict。
        for field in (
            'cached_audio_config', 'cached_sticker_config', 'cached_alter_system_config',
            'cached_agency_config', 'cached_schedule_preplan_config',
            'cached_auto_advance_config', 'cached_shared_story_config', 'cached_memory_config',
            'cached_browser_config',
        ):
            cached = getattr(service, field)
            self.assertTrue(cached is None or isinstance(cached, dict), field)

    def test_config_cache_is_instance_level(self) -> None:
        service = self.make_service()
        first = service.blind_mode_config
        self.assertIs(service.blind_mode_config, first)
        self.assertIs(service.cached_blind_mode_config, first)

    def test_constructor_binds_ctx_config_db_transport(self) -> None:
        service = self.make_service()
        self.assertIsInstance(service.ctx, InterludeContext)
        self.assertIs(service.db, self.db)
        self.assertIsInstance(service.transport, NullTransport)
        self.assertIsInstance(service.config, dict)

    def test_default_transport_is_none_when_not_passed(self) -> None:
        ctx = InterludeContext(database=self.db)
        service = ServiceChunk0(ctx, make_config(), self.db)
        self.assertIsNone(service.transport)

    def test_database_falls_back_to_ctx(self) -> None:
        ctx = InterludeContext(database=self.db)
        service = ServiceChunk0(ctx, make_config())
        self.assertIs(service.db, self.db)

    def test_setters_round_trip(self) -> None:
        service = self.make_service()
        narrator = object()
        compactor = object()
        embedder = object()
        service.set_narrator(narrator)
        service.set_compactor(compactor)
        service.set_embedder(embedder)
        self.assertIs(service.get_narrator(), narrator)
        self.assertIs(service.narrator, narrator)
        self.assertIs(service.compactor, compactor)
        self.assertIs(service.embedder, embedder)

    def test_desktop_bridge_setters(self) -> None:
        service = self.make_service()
        sink = lambda event, payload: None  # noqa: E731
        handler = lambda delivery: None  # noqa: E731
        service.set_desktop_event_sink(sink)
        service.set_desktop_delivery_handler(handler)
        self.assertIs(service.desktop_event_sink, sink)
        self.assertIs(service.desktop_delivery_handler, handler)
        service.set_desktop_event_sink(None)
        service.set_desktop_delivery_handler(None)
        self.assertIsNone(service.desktop_event_sink)
        self.assertIsNone(service.desktop_delivery_handler)

    def test_now_and_rng_from_injected_sources(self) -> None:
        from plugin.core.time import utc_now
        ctx = InterludeContext(database=self.db, clock=lambda: 1_700_000_000_000, random=lambda: 0.5)
        service = ServiceChunk0(ctx, make_config(), self.db)
        self.assertEqual(int(service.now().timestamp() * 1000), 1_700_000_000_000)
        self.assertEqual(service.now_ms(), 1_700_000_000_000)
        self.assertEqual(service.rng(), 0.5)
        self.assertEqual(service.rng_int(10), 5)
        service.ctx.clock = lambda: None
        self.assertLess(abs((service.now() - utc_now()).total_seconds()), 5)

    def test_normalize_database_row_is_exported(self) -> None:
        row = normalize_database_row('interlude_script_entry', {'id': 1, 'occurredAt': '2024-01-01T00:00:00Z'})
        self.assertEqual(row['occurredAt'].year, 2024)


# =========================================================================== #
# ServiceChunk0：队列与写串行化
# =========================================================================== #

class WriteQueueTests(AsyncServiceTestCase):
    """`write(fn)` 等价上游 `databaseWriteQueue`：全局串行。"""

    async def test_write_runs_serially_in_order(self) -> None:
        service = self.make_service()
        events: list[str] = []

        async def first(_db: Any) -> str:
            events.append('first-start')
            await asyncio.sleep(0.01)
            events.append('first-end')
            return 'first'

        async def second(_db: Any) -> str:
            events.append('second-start')
            return 'second'

        results = await asyncio.gather(service.write(first), service.write(second))
        self.assertEqual(results, ['first', 'second'])
        self.assertEqual(events, ['first-start', 'first-end', 'second-start'])

    async def test_write_receives_database(self) -> None:
        service = self.make_service()
        seen: list[Any] = []

        def capture(db: Any) -> str:
            seen.append(db)
            return 'ok'

        self.assertEqual(await service.write(capture), 'ok')
        self.assertIs(seen[0], self.db)

    async def test_write_error_does_not_block_later_writes(self) -> None:
        service = self.make_service()

        async def boom(_db: Any) -> None:
            raise RuntimeError('write failed')

        with self.assertRaises(RuntimeError):
            await service.write(boom)
        self.assertEqual(await service.write(lambda _db: 'after'), 'after')

    async def test_db_write_wraps_zero_arg_task(self) -> None:
        service = self.make_service()

        async def task() -> str:
            return 'value'

        self.assertEqual(await service.db_write(task), 'value')


class QueueSerializationTests(AsyncServiceTestCase):
    """`run_in_queue(key, task)` / `serial(key, task)` 等价上游 `serial`。"""

    async def test_same_key_runs_serially(self) -> None:
        service = self.make_service()
        events: list[str] = []

        async def first() -> str:
            events.append('a-start')
            await asyncio.sleep(0.01)
            events.append('a-end')
            return 'a'

        async def second() -> str:
            events.append('b-start')
            return 'b'

        results = await asyncio.gather(
            service.run_in_queue('story', first),
            service.run_in_queue('story', second),
        )
        self.assertEqual(results, ['a', 'b'])
        self.assertEqual(events, ['a-start', 'a-end', 'b-start'])

    async def test_different_keys_run_concurrently(self) -> None:
        service = self.make_service()
        order: list[str] = []

        async def slow() -> str:
            order.append('slow-start')
            await asyncio.sleep(0.02)
            order.append('slow-end')
            return 'slow'

        async def fast() -> str:
            order.append('fast')
            return 'fast'

        results = await asyncio.gather(
            service.run_in_queue('story-a', slow),
            service.run_in_queue('story-b', fast),
        )
        self.assertEqual(results, ['slow', 'fast'])
        # 不同 key 不相互阻塞：fast 在 slow 结束前完成。
        self.assertEqual(order, ['slow-start', 'fast', 'slow-end'])

    async def test_previous_failure_does_not_block_queue(self) -> None:
        service = self.make_service()

        async def boom() -> None:
            raise RuntimeError('queue failure')

        failed = service.run_in_queue('story', boom)
        with self.assertRaises(RuntimeError):
            await failed

        async def after() -> str:
            return 'ok'

        self.assertEqual(await service.run_in_queue('story', after), 'ok')

    async def test_queue_tail_is_cleared(self) -> None:
        service = self.make_service()

        async def task() -> str:
            return 'x'

        await service.run_in_queue('story', task)
        await asyncio.sleep(0)
        self.assertIsNone(service.queue_for('story'))
        self.assertEqual(service.queues, {})

    async def test_serial_alias_is_same_implementation(self) -> None:
        self.assertIs(ServiceBase.serial, ServiceBase.run_in_queue)
        service = self.make_service()

        async def task() -> str:
            return 'serial'

        self.assertEqual(await service.serial('story', task), 'serial')


# =========================================================================== #
# ServiceChunk0：构造期行为
# =========================================================================== #

class Chunk0LifecycleTests(ServiceTestCase):
    """构造 + `startBackgroundTasks`（`src/service.ts:717-756`）。"""

    def test_constructor_reports_startup_lines(self) -> None:
        self.make_service()
        text = self.sink.text()
        self.assertIn('服务初始化完成', text)
        self.assertIn('模型任务路由', text)

    def test_ready_handler_is_registered(self) -> None:
        ctx = InterludeContext(logger=None, database=self.db)
        ServiceChunk0(ctx, make_config(), self.db)
        before = len(self.sink.records)
        ctx.emit_ready()
        self.assertGreater(len(self.sink.records), before)
        self.assertIn('服务已就绪', self.sink.text())

    def test_background_tasks_start_once_without_loop(self) -> None:
        service = self.make_service()
        # 同步上下文里构造不会启动定时器（没有 running loop）。
        self.assertFalse(service.background_started)
        self.assertIsNone(service._sweep_timer)

    def test_all_declared_members_exist(self) -> None:
        service = self.make_service()
        for name in (
            'start_background_tasks', 'set_narrator', 'get_narrator', 'set_compactor',
            'set_embedder', 'set_desktop_event_sink', 'set_desktop_delivery_handler',
            'get_desktop_runtime_phase', 'set_desktop_runtime_phase',
            'desktop_runtime_snapshot', 'desktop_timeline_snapshot', 'desktop_purge_range',
            'desktop_timeline_range', 'set_desktop_cursor_at', 'receive_desktop_event',
            'can_handle_session', 'can_handle_group_session', 'group_rule',
            'can_handle_participant', 'can_manage_session', 'can_handle_story',
            'find_story', 'get_paused_story', 'get_canonical_story', 'find_participant',
            'participants', 'create_story', 'story_start_readiness', 'ensure_participant',
            'update_setting', 'set_status', 'recent_entries',
        ):
            self.assertTrue(callable(getattr(service, name, None)), name)


class Chunk0AsyncLifecycleTests(AsyncServiceTestCase):
    """需要 running loop 的构造期行为（计时器注册、桌面相位切换）。"""

    async def test_background_tasks_start_inside_loop(self) -> None:
        service = ServiceChunk0(self.make_context(), make_config(), self.db)
        await asyncio.sleep(0)
        self.assertTrue(service.background_started)
        self.assertIsNotNone(service._sweep_timer)
        service._sweep_timer.cancel()

    async def test_start_background_tasks_is_idempotent(self) -> None:
        service = ServiceChunk0(self.make_context(), make_config(), self.db)
        await asyncio.sleep(0)
        first_timer = service._sweep_timer
        service.start_background_tasks()
        self.assertIs(service._sweep_timer, first_timer)
        first_timer.cancel()

    async def test_start_background_tasks_registers_optional_channels(self) -> None:
        config = make_config(
            runtime={'sweepIntervalMinutes': 3},
            memory={'enabled': True, 'backgroundIntervalMinutes': 7},
            blindMode={'enabled': True, 'healthReportMinutes': 11},
            stickers={'enabled': True},
            logging={'level': 'debug', 'verbosity': 'diagnostic', 'format': 'layered'},
        )
        service = ServiceChunk0(self.make_context(), config, self.db)
        await asyncio.sleep(0)
        for timer in (
            service._sweep_timer, service._compaction_timer,
            service._blind_mode_timer, service._sticker_scan_timer,
        ):
            self.assertIsNotNone(timer)
            timer.cancel()
        # 盲区模式下**只有**健康汇报这一条日志，启动摘要被刻意抑制（上游 writeStandalone）。
        self.assertNotIn('后台调度已启动', self.sink.text())

    async def test_start_background_tasks_skips_disabled_channels(self) -> None:
        # 上游：memory/schedulePreplan 未启用时不起记忆扫描；贴纸库未启用时不起扫描。
        config = make_config(runtime={'sweepIntervalMinutes': 3})
        service = ServiceChunk0(self.make_context(), config, self.db)
        await asyncio.sleep(0)
        self.assertIsNotNone(service._sweep_timer)
        self.assertIsNone(service._compaction_timer)
        self.assertIsNone(service._blind_mode_timer)
        self.assertIsNone(service._sticker_scan_timer)
        self.assertIn('后台调度已启动', self.sink.text())
        service._sweep_timer.cancel()

    async def test_desktop_runtime_phase_pause_clears_timers(self) -> None:
        service = ServiceChunk0(self.make_context(), make_config(), self.db)
        cancelled: list[str] = []

        class _Wake:
            def cancel(self) -> None:
                cancelled.append('wake')

        service.due_intent_wake_timers['story'] = _Wake()
        service.buffered_narrative_turns['story'] = {
            'messages': [{'content': 'hi'}], 'timer': lambda: cancelled.append('narrative'),
        }
        service.buffered_group_turns['group'] = {
            'messages': [{'content': 'hi'}], 'timer': lambda: cancelled.append('group'),
        }
        snapshots: list[tuple[str, Any]] = []
        service.set_desktop_event_sink(lambda event, payload: snapshots.append((event, payload)))

        await service.set_desktop_runtime_phase('paused')
        self.assertEqual(service.get_desktop_runtime_phase(), 'paused')
        self.assertEqual(cancelled, ['wake', 'narrative', 'group'])
        self.assertEqual(service.due_intent_wake_timers, {})
        self.assertIsNone(service.buffered_narrative_turns['story']['timer'])
        self.assertIsNone(service.buffered_group_turns['group']['timer'])
        self.assertEqual(snapshots, [('runtime-snapshot', {'phase': 'paused', 'stories': []})])

    async def test_desktop_runtime_phase_resume_rearms_persisted_turns(self) -> None:
        service = ServiceChunk0(self.make_context(), make_config(), self.db)
        flushed: list[Any] = []

        async def fake_flush(key: str, revision: int) -> None:
            flushed.append((key, revision))

        service.flush_buffered_narrative = fake_flush  # type: ignore[method-assign]
        # 缓冲回合 dict 统一 snake_case（`config.py` 的 `BufferedNarrativeTurn`，
        # 创建方 `Chunk2.buffer_user_narrative` 也这么写）。
        service.buffered_narrative_turns['story'] = {
            'messages': [{'content': 'hi'}], 'timer': None, 'next_revision': 4,
        }
        # 已有 timer 或没有消息的回合不恢复。
        service.buffered_narrative_turns['busy'] = {
            'messages': [{'content': 'hi'}], 'timer': lambda: None, 'next_revision': 0,
        }
        service.buffered_narrative_turns['empty'] = {
            'messages': [], 'timer': None, 'next_revision': 0,
        }
        await service.set_desktop_runtime_phase('running')
        await asyncio.sleep(0.01)
        self.assertEqual(flushed, [('story', 5)])
        timer = service.buffered_narrative_turns['story']['timer']
        self.assertIsNotNone(timer)
        timer.cancel()

    async def test_desktop_runtime_snapshot_shape(self) -> None:
        service = ServiceChunk0(self.make_context(), make_config(), self.db)
        self.assertEqual(await service.desktop_runtime_snapshot(), {'phase': 'running', 'stories': []})

    async def test_desktop_timeline_snapshot_without_story(self) -> None:
        service = ServiceChunk0(self.make_context(), make_config(), self.db)
        self.assertEqual(
            await service.desktop_timeline_snapshot(),
            {'storyId': '', 'entries': [], 'scenes': [], 'facts': []},
        )


# =========================================================================== #
# ServiceChunk0：权限判定（纯逻辑分支）
# =========================================================================== #

#: 不是 onebot 段、而是 Config 顶层段的键（`onebot_config` 里写它们时提到顶层）。
_TOP_LEVEL_SECTIONS = ('sharedStory', 'logging', 'runtime', 'memory', 'stickers', 'blindMode')


def onebot_config(**overrides: Any) -> dict[str, Any]:
    """构造一份 onebot 闸门配置；`sharedStory` 等顶层段自动提到顶层。"""
    top_level = {key: overrides.pop(key) for key in list(overrides) if key in _TOP_LEVEL_SECTIONS}
    onebot: dict[str, Any] = {'enabled': True}
    onebot.update(overrides)
    return make_config(onebot=onebot, **top_level)


class CanHandleSessionTests(ServiceTestCase):
    """上游 `canHandleSession`（`src/service.ts:932`）。"""

    def test_non_onebot_platform_always_allowed(self) -> None:
        service = self.make_service(onebot_config(userAccounts=[]))
        self.assertTrue(service.can_handle_session(SessionView(platform='telegram', user_id='1')))

    def test_gate_disabled_allows_everything(self) -> None:
        service = self.make_service(make_config(onebot={'enabled': False, 'userAccounts': []}))
        self.assertTrue(service.can_handle_session(SessionView(platform='onebot', self_id='1', user_id='2')))

    def test_missing_onebot_section_allows_everything(self) -> None:
        service = self.make_service(make_config())
        self.assertTrue(service.can_handle_session(SessionView(platform='onebot', self_id='1', user_id='2')))

    def test_enabled_gate_with_empty_allowlist_denies_all(self) -> None:
        service = self.make_service(onebot_config(botAccounts=[], userAccounts=[]))
        self.assertFalse(service.can_handle_session(SessionView(platform='onebot', self_id='1', user_id='2')))

    def test_bot_and_user_accounts_must_both_match(self) -> None:
        service = self.make_service(onebot_config(
            botAccounts=[{'qq': '1'}],
            userAccounts=[{'qq': '2'}],
        ))
        self.assertTrue(service.can_handle_session(SessionView(platform='onebot', self_id='1', user_id='2')))
        self.assertFalse(service.can_handle_session(SessionView(platform='onebot', self_id='1', user_id='3')))
        self.assertFalse(service.can_handle_session(SessionView(platform='onebot', self_id='9', user_id='2')))

    def test_account_ids_are_normalized(self) -> None:
        service = self.make_service(onebot_config(
            botAccounts=[{'qq': 'private:1'}],
            userAccounts=[{'qq': '2'}],
        ))
        self.assertTrue(service.can_handle_session(SessionView(platform='onebot', self_id='onebot:1', user_id='user:2')))

    def test_ignore_self_messages(self) -> None:
        service = self.make_service(onebot_config(
            botAccounts=[{'qq': '1'}],
            userAccounts=[{'qq': '1'}],
            ignoreSelfMessages=True,
        ))
        # 被忽略的自消息直接拒绝；其它账号仍走白名单（'2' 不在 userAccounts 里）。
        self.assertFalse(service.can_handle_session(SessionView(platform='onebot', self_id='1', user_id='1')))
        self.assertFalse(service.can_handle_session(SessionView(platform='onebot', self_id='1', user_id='2')))

    def test_denial_is_logged(self) -> None:
        service = self.make_service(onebot_config(botAccounts=[{'qq': '1'}], userAccounts=[]))
        service.can_handle_session(SessionView(platform='onebot', self_id='1', user_id='2'))
        self.assertIn('OneBot 白名单拒绝用户账号', self.sink.text())


class CanHandleGroupSessionTests(ServiceTestCase):
    """上游 `canHandleGroupSession`（`src/service.ts:955`）与 `groupRule`（`:967`）。"""

    def test_non_onebot_denied(self) -> None:
        service = self.make_service(onebot_config(groupChats=[{'groupId': '9'}]))
        self.assertFalse(service.can_handle_group_session(SessionView(platform='telegram', channel_id='9')))

    def test_gate_disabled_denied(self) -> None:
        service = self.make_service(make_config(onebot={'enabled': False}))
        self.assertFalse(service.can_handle_group_session(SessionView(platform='onebot', channel_id='9')))

    def test_group_allowlist(self) -> None:
        service = self.make_service(onebot_config(
            botAccounts=[{'qq': '1'}],
            groupChats=[{'groupId': 'group:9', 'enabled': True}],
        ))
        allowed = SessionView(platform='onebot', self_id='1', user_id='2', channel_id='9')
        self.assertTrue(service.can_handle_group_session(allowed))
        self.assertFalse(service.can_handle_group_session(
            SessionView(platform='onebot', self_id='1', user_id='2', channel_id='8'),
        ))

    def test_guild_id_takes_priority_over_channel(self) -> None:
        service = self.make_service(onebot_config(
            botAccounts=[{'qq': '1'}],
            groupChats=[{'groupId': '9', 'enabled': True}],
        ))
        session = SessionView(platform='onebot', self_id='1', channel_id='77', guild_id='9')
        self.assertTrue(service.can_handle_group_session(session))

    def test_group_rule_defaults_to_enabled(self) -> None:
        service = self.make_service(make_config(onebot={'groupChats': [{'groupId': '9'}]}))
        self.assertIsNotNone(service.group_rule('group:9'))
        self.assertIsNone(service.group_rule('10'))

    def test_group_rule_skips_explicitly_disabled(self) -> None:
        service = self.make_service(make_config(onebot={'groupChats': [{'groupId': '9', 'enabled': False}]}))
        self.assertIsNone(service.group_rule('9'))

    def test_group_rule_missing_section(self) -> None:
        service = self.make_service(make_config())
        self.assertIsNone(service.group_rule('9'))

    def test_rule_without_an_explicit_enabled_flag_is_accepted(self) -> None:
        """Console 的 `enabled` 默认 true（`index.ts:392`）：缺省不得把整个群拒收。"""
        service = self.make_service(onebot_config(
            botAccounts=[{'qq': '1'}], groupChats=[{'groupId': '9'}],
        ))
        self.assertTrue(service.can_handle_group_session(
            SessionView(platform='onebot', self_id='1', user_id='2', channel_id='9'),
        ))

    def test_explicitly_disabled_rule_is_still_rejected(self) -> None:
        service = self.make_service(onebot_config(
            botAccounts=[{'qq': '1'}], groupChats=[{'groupId': '9', 'enabled': False}],
        ))
        self.assertFalse(service.can_handle_group_session(
            SessionView(platform='onebot', self_id='1', user_id='2', channel_id='9'),
        ))


class ConfigAccessorTests(ServiceTestCase):
    """`cachedXxxConfig` 的段位（上游 `:2247` / `:2260`）。"""

    def test_audio_config_reads_the_model_section_through_the_group_alias(self) -> None:
        from plugin.core.service.config import normalize_config
        # AstrBot schema 的分组叫 `model_center`，归一化时补出上游名 `model`。
        service = self.make_service(normalize_config(
            {'model_center': {'audio': {'enabled': True, 'out_format': 'wav', 'max_file_size_mb': 99}}},
        ))
        self.assertTrue(service.audio_config['enabled'])
        self.assertEqual(service.audio_config['out_format'], 'wav')
        self.assertEqual(service.audio_config['max_file_size_mb'], 25, '上游钳位 [1, 25]')

    def test_audio_config_never_reads_a_top_level_audio_section(self) -> None:
        from plugin.core.service.config import normalize_config
        service = self.make_service(normalize_config({'audio': {'enabled': True}}))
        self.assertFalse(service.audio_config['enabled'], '上游读的是 config.model.audio')

    def test_sticker_config_reads_the_top_level_stickers_section(self) -> None:
        from plugin.core.service.config import normalize_config
        service = self.make_service(normalize_config(
            {'stickers': {'enabled': True, 'directory': '  my/stickers  ', 'catalog_limit': 999}},
        ))
        self.assertTrue(service.sticker_config['enabled'])
        self.assertEqual(service.sticker_config['directory'], 'my/stickers')
        self.assertEqual(service.sticker_config['catalog_limit'], 80, '上游钳位 [1, 80]')
        nested = self.make_service(normalize_config({'model': {'stickers': {'enabled': True}}}))
        self.assertFalse(nested.sticker_config['enabled'], '上游读的是顶层 config.stickers')

    def test_row_state_stays_snake_case(self) -> None:
        """`state` 列的值是落库结构：保持 `encode_story_state()` 的 snake_case。"""
        normalized = normalize_database_row('interlude_story', {
            'state': {'schemaVersion': 1, 'settingOverlay': {'characterTraits': ['quiet']}},
        })
        self.assertEqual(normalized['state']['setting_overlay']['character_traits'], ['quiet'])
        self.assertNotIn('settingOverlay', normalized['state'])


class CanHandleParticipantAndStoryTests(ServiceTestCase):
    """上游 `canHandleParticipant`（`:973`）与 `canHandleStory`（`:991`）。"""

    def test_participant_non_onebot_allowed(self) -> None:
        service = self.make_service(onebot_config(userAccounts=[]))
        self.assertTrue(service.can_handle_participant({'platform': 'telegram'}))

    def test_participant_gate_disabled_allowed(self) -> None:
        service = self.make_service(make_config(onebot={'enabled': False, 'userAccounts': []}))
        self.assertTrue(service.can_handle_participant({'platform': 'onebot', 'selfId': '1', 'userId': '2'}))

    def test_participant_both_accounts_required(self) -> None:
        service = self.make_service(onebot_config(
            botAccounts=[{'qq': '1'}], userAccounts=[{'qq': '2'}],
        ))
        self.assertTrue(service.can_handle_participant({'platform': 'onebot', 'selfId': '1', 'userId': '2'}))
        self.assertFalse(service.can_handle_participant({'platform': 'onebot', 'selfId': '1', 'userId': '3'}))
        self.assertFalse(service.can_handle_participant({'platform': 'onebot', 'selfId': '9', 'userId': '2'}))

    def test_story_only_requires_bot_account(self) -> None:
        service = self.make_service(onebot_config(
            botAccounts=[{'qq': '1'}], userAccounts=[],
        ))
        self.assertTrue(service.can_handle_story({'platform': 'onebot', 'selfId': '1'}))
        self.assertFalse(service.can_handle_story({'platform': 'onebot', 'selfId': '2'}))

    def test_story_non_onebot_or_disabled_gate_allowed(self) -> None:
        service = self.make_service(make_config(onebot={'enabled': False, 'botAccounts': []}))
        self.assertTrue(service.can_handle_story({'platform': 'onebot', 'selfId': '1'}))
        self.assertTrue(service.can_handle_story({'platform': 'telegram', 'selfId': '1'}))


class CanManageSessionTests(ServiceTestCase):
    """上游 `canManageSession`（`src/service.ts:981`）。"""

    def test_empty_manager_list_allows_everyone(self) -> None:
        service = self.make_service(onebot_config(
            botAccounts=[{'qq': '1'}], userAccounts=[{'qq': '2'}], sharedStory={},
        ))
        self.assertTrue(service.can_manage_session(SessionView(platform='onebot', self_id='1', user_id='2')))

    def test_manager_list_restricts(self) -> None:
        service = self.make_service(onebot_config(
            botAccounts=[{'qq': '1'}],
            userAccounts=[{'qq': '2'}, {'qq': '3'}],
            sharedStory={'managerAccounts': ['2']},
        ))
        self.assertTrue(service.can_manage_session(SessionView(platform='onebot', self_id='1', user_id='2')))
        self.assertFalse(service.can_manage_session(SessionView(platform='onebot', self_id='1', user_id='3')))
        self.assertIn('2', service.shared_story_config.get('managerAccounts') or [])

    def test_blank_manager_entries_are_ignored(self) -> None:
        service = self.make_service(onebot_config(
            botAccounts=[{'qq': '1'}],
            userAccounts=[{'qq': '2'}],
            sharedStory={'managerAccounts': ['', '   ', '2']},
        ))
        self.assertTrue(service.can_manage_session(SessionView(platform='onebot', self_id='1', user_id='2')))

    def test_denied_session_is_rejected_and_logged(self) -> None:
        service = self.make_service(onebot_config(botAccounts=[{'qq': '1'}], userAccounts=[]))
        self.assertFalse(service.can_manage_session(SessionView(platform='onebot', self_id='1', user_id='2')))
        self.assertIn('私聊被 OneBot 白名单拦截', self.sink.text())


# =========================================================================== #
# 组装层
# =========================================================================== #

class AssemblyTests(unittest.TestCase):
    """`plugin.core.service` 的组装与 re-export。"""

    def test_interlude_service_is_assembled(self) -> None:
        self.assertTrue(issubclass(InterludeService, ServiceBase))
        self.assertTrue(issubclass(InterludeService, ServiceChunk0))

    def test_chunk0_is_first_in_mro(self) -> None:
        mro = [cls.__name__ for cls in InterludeService.__mro__]
        self.assertEqual(mro[1], 'ServiceChunk0')
        self.assertEqual(mro[-2:], ['ServiceBase', 'object'])

    def test_chunk_bookkeeping(self) -> None:
        loaded = InterludeService._loaded_chunks
        missing = InterludeService._missing_chunks
        self.assertEqual(loaded[0], 'chunk0')
        self.assertEqual(sorted(list(loaded) + list(missing)), sorted(['chunk0'] + ['chunk%d' % i for i in range(1, 10)]))

    def test_public_symbols_importable(self) -> None:
        import plugin.core.service as service_package
        for name in (
            'InterludeService', 'ServiceBase', 'ServiceChunk0', 'SessionView',
            'Transport', 'NullTransport', 'InterludeContext', 'Config',
            'pick', 'normalize_account_id', 'is_one_bot_platform', 'is_enabled_account',
            'normalize_group_id', 'story_id_for_character', 'participant_id_for',
            'merge_setting', 'normalize_database_row',
        ):
            self.assertTrue(hasattr(service_package, name), name)

    def test_no_astrbot_import_in_core(self) -> None:
        """`core/` 不得 import astrbot —— 必须与**其它测试模块的导入顺序**无关。

        早期版本直接断言 `'astrbot' not in sys.modules`，但同一进程里先跑了
        `test_astrbot_bridge` 之类会 import astrbot 的模块，这个断言就会误报。
        因此改为**在干净的子进程里**只导入本包，再检查 `sys.modules`。
        """
        import subprocess
        import sys

        from . import PACKAGE_NAME, REPO_ROOT

        # 两种仓库布局都能跑：本地工作区包名是 `plugin`（cwd=仓库根），
        # 发布仓里仓库根**就是**插件根，包名是仓库目录名（PEP 420 命名空间包）。
        program = (
            'import sys, importlib\n'
            f'importlib.import_module({PACKAGE_NAME + ".core.service"!r})\n'
            f'importlib.import_module({PACKAGE_NAME + ".core.narrator"!r})\n'
            'leaked = sorted(m for m in sys.modules\n'
            '                if m == "astrbot" or m.startswith("astrbot."))\n'
            'print("LEAKED:" + ",".join(leaked))\n'
        )
        proc = subprocess.run(
            [sys.executable, '-c', program],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        marker = [line for line in proc.stdout.splitlines() if line.startswith('LEAKED:')]
        self.assertTrue(marker, proc.stdout)
        self.assertEqual(marker[0], 'LEAKED:', f'core 泄漏了 astrbot 依赖：{marker[0]}')


if __name__ == '__main__':
    unittest.main()
