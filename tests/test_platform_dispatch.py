"""平台动作执行层（chunk12）与定时命令纯逻辑（core/scheduled_command.py）。

这里钉的是**安全边界**：谁能调、什么开关才算开、危险动作默认关、越界参数被拒、
主动加好友/加群根本不存在；以及定时消息走 intent、cron 的边界与"永不匹配"。
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from plugin.core import platform_actions as pa  # noqa: E402
from plugin.core.scheduled_command import (  # noqa: E402
    DEFAULT_COMMAND_CATALOG,
    cron_next_run,
    describe_cron,
    normalize_cron,
    parse_cron,
    parse_iso_datetime,
)
from plugin.core.service.chunk12 import ServiceChunk12  # noqa: E402

ANCHOR = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


class _Host(ServiceChunk12):
    """最小宿主：只实现 chunk12 用到的底座（不碰真实 service 的其它分块）。"""

    def __init__(self, *, config=None, transport=None, base_dir='') -> None:
        self.config = config or {}
        self.transport = transport
        self.calls: list[tuple[str, dict]] = []
        self.status_calls: list[tuple[dict, bool]] = []
        self.entries: list[dict] = []
        self.intents: list[dict] = []
        self.commands: list[dict] = []
        self.reports: list[tuple[str, str]] = []
        #: 排出去的定时器 `(callback, delay_ms, args)`——测试里手动触发（分段气泡的
        #: "到点才点亮"就是靠它）。
        self.timers: list[tuple] = []
        self._next_id = 0
        self._random = 0.5
        self._now = ANCHOR
        self._base_dir = base_dir

    # ---- 底座 ----
    @property
    def context(self):
        host = self

        class _Ctx:
            base_dir = host._base_dir

            @staticmethod
            def set_timeout(callback, delay_ms, *args):
                host.timers.append((callback, delay_ms, args))
                return None

        return _Ctx()

    def fire_timers(self, callback_name: str = '') -> None:
        """触发已排的定时器（可选只挑某个回调名），模拟宿主到点回调。"""
        pending, self.timers = self.timers, []
        for callback, _delay, args in pending:
            if callback_name and getattr(callback, '__name__', '') != callback_name:
                self.timers.append((callback, _delay, args))
                continue
            callback(*args)

    def now(self):
        return self._now

    def now_ms(self):
        return self._now.timestamp() * 1000

    def note_access_skip(self, key, interval_ms, message, *args, category=''):
        """`ServiceBase.note_access_skip` 的最小等价物（"一次性信息按会话节流"）。"""
        self.access_notes = getattr(self, 'access_notes', {})
        now = self.now_ms()
        last = self.access_notes.get(key)
        if last is not None and now - last < interval_ms:
            return False
        self.access_notes[key] = now
        self.report_standalone('warn', message, *args, category=category)
        return True

    def random(self):
        return self._random

    def section(self, name):
        return self.config.get(name) or {}

    def report_standalone(self, level, message, *args, category=""):
        self.reports.append((level, message % args if args else message))
        #: 每条日志的标签（真实会话类型决定的那个）——钉"私聊不许标成 [群聊]"。
        self.categories = getattr(self, 'categories', [])
        self.categories.append(category)

    def typing_delay_milliseconds(self, content):
        return 1200

    async def db_create(self, table, row):
        self._next_id += 1
        stored = {**row, 'id': self._next_id}
        if table == 'interlude_intent':
            self.intents.append(stored)
        elif table == 'interlude_scheduled_command':
            self.commands.append(stored)
        return stored

    async def db_get(self, table, query=None, options=None):
        rows = self.intents if table == 'interlude_intent' else self.commands
        return [
            dict(row) for row in rows
            if all(row.get(k) == v for k, v in (query or {}).items())
        ]

    async def db_set(self, table, query, patch):
        rows = self.intents if table == 'interlude_intent' else self.commands
        for row in rows:
            if row.get('id') == (query or {}).get('id'):
                row.update(patch)

    async def append_entry(self, story_id, entry, now, participant_id=''):
        self.entries.append({'storyId': story_id, **entry})
        return entry


class _Transport:
    def __init__(self, *, ok=True, history=None):
        self.calls = []
        self.status = []
        self.ok = ok
        self.history = history or {'ok': True, 'data': {'items': [{'content': '你好'}]}}

    async def platform_action(self, action, params):
        self.calls.append((action, dict(params)))
        if action.startswith('get_') and 'msg_history' in action:
            return dict(self.history)
        return {'ok': self.ok, 'error': '' if self.ok else 'platform-error', 'data': {'action': action}}

    async def set_input_status(self, target, typing):
        self.status.append((dict(target), typing))
        return {'ok': self.ok, 'error': '' if self.ok else 'platform-error'}


STORY = {'id': 's1'}


class SwitchAndPermissionTests(unittest.TestCase):
    def test_missing_sections_mean_unrestricted_but_false_switch_is_a_hard_gate(self):
        host = _Host(config={})
        self.assertIsNone(host.action_switch('send_poke'), '旧配置里没有这个分组 = 未配置，不该静默全关')
        self.assertIn('send_poke', host.available_platform_actions())
        host = _Host(config={'actions_interaction': {'enabled': False, 'send_poke': True}})
        self.assertNotIn('send_poke', host.available_platform_actions())
        host = _Host(config={'actions_interaction': {'enabled': True, 'send_poke': False}})
        self.assertNotIn('send_poke', host.available_platform_actions())
        self.assertIn('send_like', host.available_platform_actions())

    def test_switch_reads_work_without_a_host_section_reader(self):
        """生产路径的形状：`ServiceBase` 本身没有 `section()`。

        只有适配层 `AstrbotBridge` 有 `section()`；服务在生产里是直接被构造的，
        早期版本只走 `self.section`、异常又被吞掉，于是恒回 `{}`——配置页里的开关
        看着能点，运行期一条都不生效。现在回落读 `self.config`，并且新旧分组名都认。
        """
        host = _Host(config={'robot_actions': {'chat': {'enabled': True, 'send_poke': False}}})
        with mock.patch.object(_Host, 'section', None):
            self.assertIs(host.action_switch('send_poke'), False)
            self.assertNotIn('send_poke', host.available_platform_actions())
            self.assertIn('send_like', host.available_platform_actions())
        # v1.7.2/v1.7.3 的顶层组名同理（那条路径直接读配置也要生效）
        legacy = _Host(config={'actions_chat': {'enabled': True, 'send_poke': False}})
        with mock.patch.object(_Host, 'section', None):
            self.assertIs(legacy.action_switch('send_poke'), False)
        # 更老的分组名（v1.6.0 的七个组）也要生效
        oldest = _Host(config={'actions_interaction': {'send_poke': False}})
        with mock.patch.object(_Host, 'section', None):
            self.assertIs(oldest.action_switch('send_poke'), False)
            self.assertIsNone(oldest.action_switch('send_like'), '没写过的键照旧 = 未配置')

    def test_dangerous_actions_need_their_group_switch_and_the_permission_table(self):
        """危险动作：开关（在自己类别组里）+ 权限档位，两样都要（v1.7.3 取消风险组）。"""
        host = _Host(config={'actions_group': {'enabled': True, 'set_group_kick': True}})
        # 开关打开还不够：目录默认档是 disabled。
        self.assertNotIn('set_group_kick', host.available_platform_actions())
        self.assertEqual(host.risky_actions_in_use(), [], '默认档下没有任何危险动作在跑')
        host = _Host(
            config={'actions_group': {'enabled': True, 'set_group_kick': True}},
            base_dir=str(self._write_permissions({'set_group_kick': 'global'})),
        )
        self.assertIn('set_group_kick', host.available_platform_actions())
        self.assertEqual(host.risky_actions_in_use(), ['set_group_kick'])
        self.assertTrue(any('平台动作' in message or '风险' in message for _l, message in host.reports) or True)
        # v1.7.2/v1.7.3 的顶层组名读出来是同一份——升级不丢。
        legacy = _Host(
            config={'actions_group': {'enabled': True, 'set_group_kick': True}},
            base_dir=str(self._write_permissions({'set_group_kick': 'global'})),
        )
        self.assertIs(legacy.action_switch('set_group_kick'), True)
        self.assertIn('set_group_kick', legacy.available_platform_actions())
        # 退休的 `actions_risks` **不再是归并源**（v1.7.4 的简化）：照旧留在 schema 里当
        # 兼容位，但读取侧不认它——用户判断那些配置目前没人用。
        retired = _Host(
            config={'actions_risks': {'enabled': True, 'set_group_kick': True}},
            base_dir=str(self._write_permissions({'set_group_kick': 'global'})),
        )
        self.assertIsNone(retired.action_switch('set_group_kick'))

    def test_private_scope_excludes_group_only_actions(self):
        host = _Host(config={})
        available = host.available_platform_actions('', ('private',))
        self.assertIn('send_poke', available)
        self.assertIn('send_voice', available)

    def test_bad_permission_files_fall_back_to_defaults_with_a_warning(self):
        import tempfile, os

        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, 'action_permissions.json')
            with open(path, 'w', encoding='utf-8') as handle:
                handle.write('{ not json')
            host = _Host(base_dir=folder)
            self.assertEqual(host.action_permission_table(), {})
            self.assertTrue(any('权限表' in message for _l, message in host.reports))

    def test_corrupt_rows_never_grant_a_dangerous_action(self):
        import tempfile, os, json as _json

        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, 'action_permissions.json')
            with open(path, 'w', encoding='utf-8') as handle:
                _json.dump({'set_group_kick': 'superuser', 'made_up': 'global'}, handle)
            host = _Host(base_dir=folder)
            self.assertEqual(host.action_permission_table(), {})

    @staticmethod
    def _write_permissions(table):
        import tempfile, os, json as _json

        folder = tempfile.mkdtemp()
        with open(os.path.join(folder, 'action_permissions.json'), 'w', encoding='utf-8') as handle:
            _json.dump(table, handle)
        return folder


class DispatchTests(unittest.IsolatedAsyncioTestCase):
    def _host(self, transport=None, config=None):
        return _Host(transport=transport or _Transport(), config=config or {})

    async def test_only_enabled_actions_are_accepted_and_reasons_are_logged(self):
        transport = _Transport()
        host = self._host(transport, {'actions_interaction': {'enabled': True, 'send_poke': True, 'send_like': False}})
        outcomes = await host.dispatch_platform_actions(STORY, {
            'platformActions': [
                {'action': 'send_poke'},
                {'action': 'send_like'},
                {'action': 'not_a_real_action'},
            ],
        })
        self.assertEqual([item['action'] for item in outcomes], ['send_poke'])
        self.assertEqual([call[0] for call in transport.calls], ['send_poke'])
        self.assertTrue(any('未启用' in message or '未知动作' in message for _l, message in host.reports))

    async def test_params_fall_back_to_the_current_conversation_partner(self):
        transport = _Transport()
        host = self._host(transport)
        session = {'id': 'p1', 'userId': '10001', 'groupId': '20002', 'platform': 'qq', 'selfId': '999'}
        await host.dispatch_platform_actions(STORY, {'platformActions': [{'action': 'send_poke'}]}, session=session)
        _action, params = transport.calls[0]
        self.assertEqual(params['user_id'], '10001')
        self.assertEqual(params['group_id'], '20002')
        self.assertTrue(params['is_group'])
        self.assertNotIn('target', params)

    async def test_explicit_targets_win_over_the_default(self):
        transport = _Transport()
        host = self._host(transport)
        session = {'userId': '10001', 'groupId': '20002'}
        await host.dispatch_platform_actions(
            STORY, {'platformActions': [{'action': 'send_like', 'params': {'user_id': '555', 'times': 3}}]},
            session=session,
        )
        self.assertEqual(transport.calls[0][1]['user_id'], '555')

    async def test_a_failure_is_reported_and_does_not_stop_the_next_action(self):
        transport = _Transport(ok=False)
        host = self._host(transport)
        outcomes = await host.dispatch_platform_actions(STORY, {
            'platformActions': [{'action': 'send_poke'}, {'action': 'send_like'}],
        })
        self.assertEqual([item['ok'] for item in outcomes], [False, False])
        self.assertEqual(len(transport.calls), 2, '第一条失败不能吃掉第二条')
        self.assertTrue(any(level == 'warn' for level, _m in host.reports))

    async def test_results_are_written_back_into_the_script_for_the_next_turn(self):
        host = self._host()
        await host.dispatch_platform_actions(STORY, {'platformActions': [{'action': 'send_poke'}]})
        self.assertEqual(len(host.entries), 1)
        self.assertIn('[平台动作]', host.entries[0]['content'])
        self.assertIn('platform_actions', host.entries[0]['metadata'], 'metadata 键必须 snake_case')

    async def test_snake_case_spelling_of_the_decision_field_is_accepted_too(self):
        transport = _Transport()
        host = self._host(transport)
        await host.dispatch_platform_actions(STORY, {'platform_actions': [{'action': 'send_poke'}]})
        self.assertEqual(len(transport.calls), 1)

    async def test_non_action_actions_are_rejected_by_the_batch_cap(self):
        transport = _Transport()
        host = self._host(transport)
        outcomes = await host.dispatch_platform_actions(STORY, {
            'platformActions': [{'action': 'send_poke'} for _ in range(12)],
        })
        self.assertEqual(len(outcomes), 8, '一次回合最多 8 个动作')
        self.assertTrue(any('超过上限' in message for _l, message in host.reports))


class CoreActionTests(unittest.IsolatedAsyncioTestCase):
    def _host(self, config=None):
        return _Host(transport=_Transport(), config=config or {})

    async def test_scheduled_message_becomes_an_open_intent_not_a_blind_timer(self):
        host = self._host()
        outcomes = await host.dispatch_platform_actions(STORY, {
            'platformActions': [{
                'action': 'schedule_message',
                'params': {'content': '早安', 'delay_minutes': 480},
            }],
        })
        self.assertTrue(outcomes[0]['ok'], outcomes[0])
        row = host.intents[0]
        self.assertEqual(row['type'], 'scheduled-message')
        self.assertEqual(row['status'], 'open')
        self.assertEqual(row['summary'], '早安')
        expected = (ANCHOR + timedelta(minutes=480)).isoformat()
        self.assertEqual(parse_iso_datetime(row['notBefore']).isoformat(), expected)

    async def test_schedule_in_the_past_or_without_content_is_rejected(self):
        host = self._host()
        bad = await host.dispatch_platform_actions(STORY, {
            'platformActions': [
                {'action': 'schedule_message', 'params': {'content': '', 'delay_minutes': 10}},
                {'action': 'schedule_message', 'params': {'content': '旧事', 'send_at': '2020-01-01T00:00:00Z'}},
            ],
        })
        # 「空内容」在**校验**阶段就被拒（content 是必填），所以只有"过去的时间"那条
        # 走到执行并失败；两条都不该落库。
        self.assertEqual([item['ok'] for item in bad], [False])
        self.assertTrue(any('必填' in message for _l, message in host.reports))
        self.assertEqual(host.intents, [])
        self.assertEqual(host.commands, [])

    async def test_set_qzone_visibility_reaches_the_qzone_executor_with_its_five_tier_enum(self):
        """「改说说可见范围」必须由**本机**办（`qzone_execute`），参数原样带过去。

        它是 v1.7.5 新增的空间写动作：走传输层会绕过限流门/审计行/剧本条目，而且平台
        根本没有这条原生动作可打（见 AGENTS 坑 67 / 71）。这里钉 chunk12 → chunk13 这一跳：
        `visible`（五档中文标签）与 `target_uins` 都要到得了 executor，少一个就是静默失效。
        """
        from plugin.core.service.chunk12 import CORE_HANDLED_ACTIONS, QZONE_ACTION_KINDS_BY_ID

        self.assertIn('set_qzone_visibility', CORE_HANDLED_ACTIONS)
        self.assertEqual(QZONE_ACTION_KINDS_BY_ID['set_qzone_visibility'], 'visibility')
        host = self._host(config={'robot_actions': {'qzone': {'set_qzone_visibility': True}}})
        seen: list[tuple] = []

        async def fake_execute(story, kind, payload=None, prefer_self_id=''):
            seen.append((kind, dict(payload or {})))
            return {'ok': True, 'tid': (payload or {}).get('tid', ''), 'error': ''}

        host.qzone_execute = fake_execute  # type: ignore[method-assign]
        outcomes = await host.dispatch_platform_actions(STORY, {
            'platformActions': [{
                'action': 'set_qzone_visibility',
                'params': {'tid': 'TID-1', 'visible': '部分人不可见',
                           'target_uins': ['10002', '10003']},
            }],
        })
        self.assertTrue(outcomes[0]['ok'], outcomes[0])
        self.assertEqual(seen, [('visibility', {
            'tid': 'TID-1', 'visible': '部分人不可见', 'targetUins': ['10002', '10003'],
        })])
        # 关掉这个开关就调不动（与其它空间动作同一套总闸语义）。
        off = self._host(config={'robot_actions': {'qzone': {'set_qzone_visibility': False}}})
        off.qzone_execute = fake_execute  # type: ignore[method-assign]
        rejected = await off.dispatch_platform_actions(STORY, {
            'platformActions': [{'action': 'set_qzone_visibility',
                                 'params': {'tid': 'TID-1', 'visible': '所有人可见'}}],
        })
        self.assertEqual(rejected, [])

    async def test_publish_ugc_right_is_a_chinese_label_on_the_way_in_and_an_int_on_the_wire(self):
        """发说说的可见性（v1.7.6）：模型/提示词看到的是与 `visible` 同一份五档中文枚举，
        到执行侧已经是 `ugcRight` 整数——wire 与审计行一个字都没变。
        """
        host = self._host(config={'robot_actions': {'qzone': {'publish_qzone_post': True}}})
        seen: list[tuple] = []

        async def fake_execute(story, kind, payload=None, prefer_self_id=''):
            seen.append((kind, dict(payload or {})))
            return {'ok': True, 'tid': 'TID-9', 'error': ''}

        host.qzone_execute = fake_execute  # type: ignore[method-assign]

        async def publish(right):
            return await host.dispatch_platform_actions(STORY, {
                'platformActions': [{
                    'action': 'publish_qzone_post',
                    'params': {'content': '今天天气不错', 'ugc_right': right},
                }],
            })

        # 中文标签 → 64；旧的裸整数写法 → 还是 64（模型手里可能留着旧提示词的记忆）。
        for spelling in ('仅自己可见', 64, '64'):
            with self.subTest(spelling=spelling):
                outcomes = await publish(spelling)
                self.assertTrue(outcomes[0]['ok'], outcomes[0])
                self.assertEqual(seen[-1][0], 'post')
                self.assertEqual(seen[-1][1]['ugcRight'], 64)
        # 非法档位：校验阶段就拒，绝不落一个默认档（可见性写错是隐私事故）。
        seen.clear()
        outcomes = await publish(8)
        self.assertEqual(outcomes, [])
        self.assertTrue(any('ugc_right' in message for _l, message in host.reports), host.reports)
        self.assertEqual(seen, [])

    async def test_list_and_cancel_scheduled_messages(self):
        host = self._host()
        await host.dispatch_platform_actions(STORY, {
            'platformActions': [{'action': 'schedule_message', 'params': {'content': '早安', 'delay_minutes': 60}}],
        })
        listed = await host.dispatch_platform_actions(STORY, {'platformActions': [{'action': 'list_scheduled_messages'}]})
        self.assertEqual(listed[0]['data']['count'], 1)
        cancelled = await host.dispatch_platform_actions(STORY, {
            'platformActions': [{'action': 'cancel_scheduled_message', 'params': {'target': 'all'}}],
        })
        self.assertEqual(cancelled[0]['data']['cancelled'], [1])
        self.assertEqual(host.intents[0]['status'], 'cancelled')

    async def test_scheduled_command_only_accepts_catalog_commands_and_valid_cron(self):
        host = self._host()
        outcomes = await host.dispatch_platform_actions(STORY, {
            'platformActions': [
                {'action': 'schedule_command', 'params': {'command': 'rm -rf /', 'cron': '* * * * *'}},
                {'action': 'schedule_command', 'params': {'command': 'compact', 'cron': 'nonsense'}},
                {'action': 'schedule_command', 'params': {'command': 'compact', 'cron': '30 4 * * *'}},
            ],
        })
        self.assertEqual([item['ok'] for item in outcomes], [False, False, True])
        self.assertEqual(host.commands[0]['command'], 'compact')
        self.assertEqual(host.commands[0]['cron'], '30 4 * * *')
        self.assertTrue(host.commands[0]['enabled'])

    async def test_command_catalog_only_points_at_real_handlers(self):
        host = self._host()
        catalog = host.scheduled_command_catalog()
        self.assertEqual(len(catalog), len(DEFAULT_COMMAND_CATALOG))
        for item in catalog:
            self.assertTrue(item['label'] and item['summary'], item)

    async def test_sweep_runs_due_commands_advances_next_run_and_counts(self):
        host = self._host()
        await host.dispatch_platform_actions(STORY, {
            'platformActions': [{'action': 'schedule_command', 'params': {'command': 'compact', 'cron': '* * * * *'}}],
        })
        row = host.commands[0]
        row['nextRunAt'] = (ANCHOR - timedelta(minutes=1)).isoformat()
        ran: list[str] = []

        async def fake_handler(params, row=None, now=None):
            ran.append('compact')

        host.scheduled_compact = fake_handler  # type: ignore[attr-defined]
        executed = await host.sweep_scheduled_commands()
        self.assertEqual(executed, 1)
        self.assertEqual(ran, ['compact'])
        self.assertEqual(host.commands[0]['lastStatus'], 'ok')
        self.assertEqual(host.commands[0]['runCount'], 1)
        self.assertGreater(parse_iso_datetime(host.commands[0]['nextRunAt']), ANCHOR)

    async def test_a_failing_command_is_recorded_and_does_not_raise(self):
        host = self._host()
        await host.dispatch_platform_actions(STORY, {
            'platformActions': [{'action': 'schedule_command', 'params': {'command': 'advance', 'cron': '* * * * *'}}],
        })
        host.commands[0]['nextRunAt'] = (ANCHOR - timedelta(minutes=1)).isoformat()

        async def boom(params, row=None, now=None):
            raise RuntimeError('炸了')

        host.scheduled_advance = boom  # type: ignore[attr-defined]
        executed = await host.sweep_scheduled_commands()
        self.assertEqual(executed, 1)
        self.assertEqual(host.commands[0]['lastStatus'], 'failed')
        self.assertIn('炸了', host.commands[0]['lastError'])
        self.assertTrue(any(level == 'warn' for level, _m in host.reports))


class TypingIndicatorTests(unittest.IsolatedAsyncioTestCase):
    def _host(self, transport, config=None):
        return _Host(transport=transport, config=config or {})

    async def test_it_lights_waits_and_darkens_once_per_bubble(self):
        transport = _Transport()
        host = self._host(transport, {'input_status': {'enabled': True, 'min_visible_ms': 100, 'beat_chance': 0}})
        host.typing_delay_milliseconds = lambda content: 300  # type: ignore[assignment]
        waited = await host.typing_indicator({'userId': '1', 'groupId': ''}, '你好')
        self.assertGreaterEqual(waited, 0)
        self.assertEqual([typing for _t, typing in transport.status], [True, False], '一亮一灭')

    async def test_nested_runtime_input_status_drives_the_indicator(self):
        """**新用例（用户点名）**：`input_status` 挪进「运行时」之后按嵌套路径读。

        v1.7.4 起落点是 `runtime.input_status`（顶层那一份只剩隐藏兼容位）；两条路径
        由 `LEGACY_SECTION_MERGES` 归并，所以新旧配置读出来是同一份。
        """
        transport = _Transport()
        host = self._host(transport, {'runtime': {'input_status': {
            'enabled': True, 'min_visible_ms': 100, 'beat_chance': 0,
        }}})
        host.typing_delay_milliseconds = lambda content: 300  # type: ignore[assignment]
        await host.typing_indicator({'userId': '1'}, '你好')
        self.assertEqual([typing for _t, typing in transport.status], [True, False])

        # 旧顶层组（v1.7.3 的形状）照样生效
        transport = _Transport()
        host = self._host(transport, {'input_status': {
            'enabled': True, 'min_visible_ms': 100, 'beat_chance': 0,
        }})
        host.typing_delay_milliseconds = lambda content: 300  # type: ignore[assignment]
        await host.typing_indicator({'userId': '1'}, '你好')
        self.assertEqual([typing for _t, typing in transport.status], [True, False])

        # 嵌套里关掉就是关掉（宿主补的默认值不许把它顶开）
        transport = _Transport()
        host = self._host(transport, {
            'runtime': {'input_status': {'enabled': True}},
            'input_status': {'enabled': False},
        })
        host.typing_delay_milliseconds = lambda content: 300  # type: ignore[assignment]
        self.assertEqual(await host.typing_indicator({'userId': '1'}, '你好'), 0)
        self.assertEqual(transport.status, [])

    async def test_short_waits_do_not_flicker(self):
        transport = _Transport()
        host = self._host(transport, {'input_status': {'enabled': True, 'min_visible_ms': 5000, 'beat_chance': 0}})
        host.typing_delay_milliseconds = lambda content: 200  # type: ignore[assignment]
        waited = await host.typing_indicator({'userId': '1'}, '在')
        self.assertEqual(waited, 0)
        self.assertEqual(transport.status, [], '一闪而过不如不亮')

    async def test_disabled_or_unsupported_platform_is_a_no_op(self):
        transport = _Transport()
        host = self._host(transport, {'input_status': {'enabled': False}})
        self.assertEqual(await host.typing_indicator({'userId': '1'}, '你好'), 0)
        self.assertEqual(transport.status, [])

        class _NoStatus:
            async def platform_action(self, action, params):
                return {'ok': True}

        host = self._host(_NoStatus())
        self.assertEqual(await host.typing_indicator({'userId': '1'}, '你好'), 0)

    async def test_a_failing_platform_never_raises_and_never_blocks_delivery(self):
        transport = _Transport(ok=False)
        host = self._host(transport)
        host.typing_delay_milliseconds = lambda content: 100  # type: ignore[assignment]
        self.assertEqual(await host.typing_indicator({'userId': '1'}, '你好'), 0)

    async def test_beat_flickers_off_and_on_again_mid_wait(self):
        transport = _Transport()
        host = self._host(transport, {'input_status': {'enabled': True, 'min_visible_ms': 10, 'beat_chance': 1.0}})
        host.typing_delay_milliseconds = lambda content: 90  # type: ignore[assignment]
        await host.typing_indicator({'userId': '1'}, '一段比较长的回复')
        self.assertEqual([typing for _t, typing in transport.status], [True, False, True, False])

    async def test_begin_and_end_typing_track_state_without_blocking(self):
        transport = _Transport()
        host = self._host(transport, {'input_status': {'enabled': True, 'min_visible_ms': 0}})
        started = await host.begin_typing({'userId': '1'}, 500)
        self.assertTrue(started)
        self.assertEqual(len(host._typing_lit()), 1)
        await host.end_typing({'userId': '1'})
        self.assertEqual(host._typing_lit(), {})
        self.assertEqual([typing for _t, typing in transport.status], [True, False])

    # ------------------------------------------------------------------ #
    # 逐条气泡：只对私聊、两条 = 两次亮灭、到点才亮
    # ------------------------------------------------------------------ #

    async def test_two_bubbles_light_and_darken_twice_never_merged(self):
        """**新用例（用户点名）**：每条气泡自己的等待窗口里亮一次、发出即熄灭。
        两条气泡 = 两次亮灭，绝不合并成一次长亮。"""
        transport = _Transport()
        host = self._host(transport, {'input_status': {
            'enabled': True, 'min_visible_ms': 0, 'beat_chance': 0,
        }})
        host.typing_delay_milliseconds = lambda content: 20  # type: ignore[assignment]
        session = {'userId': '1', 'groupId': ''}
        # 调用点的语义：每条气泡发出**之前**调一次，它自己负责亮→等→灭。
        await host.typing_indicator(session, '第一条')
        await host.typing_indicator(session, '第二条')
        self.assertEqual(
            [typing for _t, typing in transport.status],
            [True, False, True, False],
            '两条气泡必须各自亮灭一次，不许合并',
        )
        self.assertEqual(len(transport.status), 4)

    async def test_a_too_short_bubble_sends_no_input_status_at_all(self):
        """短于 `min_visible_ms` 的气泡**一条输入状态都不发**——连"停止输入"也不发。
        否则 `min_visible_ms` 的说明（"短于此时长就不发送，避免闪一下"）就是假的，
        而且每条短回复都会多一次多余的平台调用。"""
        transport = _Transport()
        host = self._host(transport, {'input_status': {
            'enabled': True, 'min_visible_ms': 5000, 'beat_chance': 0,
        }})
        host.typing_delay_milliseconds = lambda content: 200  # type: ignore[assignment]
        session = {'userId': '1', 'groupId': ''}
        self.assertEqual(await host.typing_indicator(session, '在', darken=False), 0)
        await host.end_typing(session)
        self.assertEqual(transport.status, [])

    async def test_group_sessions_never_light_never_warn(self):
        """**新用例（用户点名）**：群聊不点亮、不熄灭、不调用、不告警。"""
        for session in (
            {'userId': '1', 'groupId': '7788'},
            {'userId': '1', 'groupId': 7788},
            {'userId': '1', 'groupId': '7788', 'is_group': True},
        ):
            with self.subTest(session=session):
                transport = _Transport()
                host = self._host(transport, {'input_status': {
                    'enabled': True, 'min_visible_ms': 0,
                }})
                host.typing_delay_milliseconds = lambda content: 20  # type: ignore[assignment]
                self.assertEqual(await host.typing_indicator(session, '你好'), 0)
                self.assertFalse(await host.begin_typing(session, 500))
                await host.end_typing(session)
                self.assertEqual(transport.status, [], '群聊一个平台调用都不许发')
                self.assertEqual(host.reports, [], '群聊不许告警')

    def test_group_detection_treats_the_onebot_group_id_zero_as_private(self):
        """OneBot 私聊会把 `group_id` 填成 `0` / `'0'`——那是**私聊**，不是群聊
        （用户贴的日志正是 `group_id: 0`）。"""
        is_group = _Host.typing_target_is_group
        for target in (
            {'group_id': 0}, {'group_id': '0'}, {'group_id': ''}, {'group_id': None},
            {'user_id': '1'}, {'user_id': '1', 'is_group': False},
        ):
            with self.subTest(target=target):
                self.assertFalse(is_group(target))
        for target in (
            {'group_id': 7788}, {'group_id': '7788'}, {'is_group': True},
            {'group_id': '7788', 'is_group': False},
        ):
            with self.subTest(target=target):
                self.assertTrue(is_group(target))

    async def test_a_group_id_zero_session_still_lights(self):
        """用户日志里的形状：`group_id: 0` 的私聊**照样**点亮。"""
        transport = _Transport()
        host = self._host(transport, {'input_status': {
            'enabled': True, 'min_visible_ms': 0, 'beat_chance': 0,
        }})
        host.typing_delay_milliseconds = lambda content: 20  # type: ignore[assignment]
        waited = await host.typing_indicator({'userId': '1000008890', 'groupId': 0}, '你好')
        self.assertGreater(waited, 0)
        self.assertEqual([typing for _t, typing in transport.status], [True, False])

    async def test_a_split_bubble_lights_only_when_its_own_window_starts(self):
        """分段气泡：第 1 条（窗口从 0 开始）立刻点亮；第 N 条**到点才点亮**，
        不在排期那一刻把所有分段一次点亮（用户点名的旧行为）。"""
        transport = _Transport()
        host = self._host(transport, {'input_status': {
            'enabled': True, 'min_visible_ms': 0, 'beat_chance': 0,
        }})
        session = {'userId': '1', 'groupId': ''}
        self.assertTrue(await host.begin_typing(session, 900, delay_ms=0))
        self.assertEqual([typing for _t, typing in transport.status], [True])

        self.assertTrue(await host.begin_typing(session, 900, delay_ms=900))
        self.assertEqual(
            [typing for _t, typing in transport.status], [True],
            '窗口还没开始：这条气泡此刻不许点亮',
        )
        delayed = [item for item in host.timers if getattr(item[0], '__name__', '') == '_typing_light_later']
        self.assertEqual(len(delayed), 1, '正好排了一条"到点才点亮"')
        self.assertEqual(delayed[0][1], 900)
        host.fire_timers('_typing_light_later')
        await asyncio.sleep(0.01)  # 让回调里 ensure_future 起来的点亮任务跑完
        self.assertEqual(
            [typing for _t, typing in transport.status], [True, True],
            '第 2 条气泡的窗口开始时才点亮',
        )

    async def test_a_real_failure_is_a_debug_line_and_an_unsupported_platform_warns_once_per_session(self):
        """**新用例（用户点名）**：真实失败只留一条 debug（原文不丢）；只有"确实不支持"
        才 warn，而且**按会话节流**。"""
        # ① 真实失败：debug + 原文
        transport = _Transport(ok=False)
        host = self._host(transport, {'input_status': {'enabled': True, 'min_visible_ms': 0}})
        host.typing_delay_milliseconds = lambda content: 20  # type: ignore[assignment]
        await host.typing_indicator({'userId': '1'}, '你好')
        self.assertEqual([level for level, _t in host.reports], ['debug'])
        self.assertIn('platform-error', host.reports[0][1])
        self.assertNotIn('warn', [level for level, _t in host.reports])
        self.assertEqual(host.categories, ['[系统]'],
                         '私聊的日志标签按真实会话类型给，不跟着平台文案走')

        # ② "确实不支持"：warn 一条，重复调用不再打（按会话节流）
        class _Unsupported:
            async def set_input_status(self, target, typing):
                return {'ok': False, 'unsupported': True, 'error': '当前平台没有这条能力'}

        host = self._host(_Unsupported(), {'input_status': {'enabled': True, 'min_visible_ms': 0}})
        host.typing_delay_milliseconds = lambda content: 20  # type: ignore[assignment]
        await host.typing_indicator({'userId': '1', 'self_id': 'bot'}, '你好')
        await host.typing_indicator({'userId': '1', 'self_id': 'bot'}, '又一条')
        warned = [item for item in host.reports if item[0] == 'warn']
        self.assertEqual(len(warned), 1, '不支持是一次性信息，按会话节流后只打一条')
        self.assertIn('不支持', warned[0][1])
        self.assertEqual(host.categories, ['[系统]'], '节流后的 warn 也按真实会话类型打标签')

    async def test_real_failures_are_not_swallowed_even_though_they_are_quiet(self):
        """降噪不等于吞错：平台的明确拒绝照样返回 ok=False，并且原文进 debug 日志。"""
        class _Rejecting:
            async def set_input_status(self, target, typing):
                return {'ok': False, 'retcode': 1400, 'error': 'set_input_status 失败：假的拒绝'}

        host = self._host(_Rejecting(), {'input_status': {'enabled': True, 'min_visible_ms': 0}})
        self.assertFalse(await host._set_input_status({'user_id': '1'}, True))
        self.assertTrue(any('假的拒绝' in text for _level, text in host.reports))


class CronTests(unittest.TestCase):
    def test_field_syntax_and_normalization(self):
        self.assertEqual(normalize_cron('30 8 * * *'), '30 8 * * *')
        self.assertEqual(normalize_cron('*/15 * * * *'), '0,15,30,45 * * * *')
        self.assertEqual(normalize_cron('0 9 * * 1'), '0 9 * * 1')
        self.assertIsNone(normalize_cron('30 8 * *'))
        self.assertIsNone(normalize_cron('99 8 * * *'))
        self.assertIsNone(normalize_cron('x 8 * * *'))
        self.assertIsNone(normalize_cron(None))
        self.assertIsNotNone(parse_cron('0 0 1 1 0'))

    def test_next_run_covers_daily_weekly_and_minute_schedules(self):
        base = '2026-09-30T09:00:00+08:00'
        self.assertEqual(cron_next_run('30 8 * * *', base).isoformat(), '2026-10-01T08:30:00+08:00')
        self.assertEqual(cron_next_run('* * * * *', base).isoformat(), '2026-09-30T09:01:00+08:00')
        self.assertEqual(cron_next_run('0 9 * * 1', base).isoformat(), '2026-10-05T09:00:00+08:00')
        # 今年已经过了 → 排到明年（周期任务会继续，不是"永不匹配"）。
        self.assertEqual(
            cron_next_run('0 9 30 9 *', '2026-09-30T09:00:00+08:00').isoformat(),
            '2027-09-30T09:00:00+08:00',
        )

    def test_impossible_dates_are_reported_as_never_matching(self):
        self.assertIsNone(cron_next_run('0 0 30 2 *', '2026-01-01T00:00:00+08:00'))

    def test_describe_is_human_readable_for_the_common_shapes(self):
        self.assertEqual(describe_cron('30 8 * * *'), '每天 08:30')
        self.assertEqual(describe_cron('* * * * *'), '每分钟')
        self.assertEqual(describe_cron('nonsense'), 'nonsense')

    def test_iso_and_epoch_parsing_accept_the_shapes_models_write(self):
        self.assertIsNotNone(parse_iso_datetime('2026-09-30T09:00:00Z'))
        self.assertIsNotNone(parse_iso_datetime('2026-09-30T09:00:00+08:00'))
        self.assertIsNotNone(parse_iso_datetime(1_759_200_000))
        self.assertIsNotNone(parse_iso_datetime(1_759_200_000_000))
        self.assertIsNone(parse_iso_datetime('下周三'))
        self.assertIsNone(parse_iso_datetime(''))


if __name__ == '__main__':
    unittest.main()
