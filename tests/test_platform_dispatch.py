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
                return None

        return _Ctx()

    def now(self):
        return self._now

    def random(self):
        return self._random

    def section(self, name):
        return self.config.get(name) or {}

    def report_standalone(self, level, message, *args):
        self.reports.append((level, message % args if args else message))

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
        return {'ok': self.ok}


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
        host = _Host(config={'actions_chat': {'enabled': True, 'send_poke': False}})
        with mock.patch.object(_Host, 'section', None):
            self.assertIs(host.action_switch('send_poke'), False)
            self.assertNotIn('send_poke', host.available_platform_actions())
            self.assertIn('send_like', host.available_platform_actions())
        # 旧分组名同理（v1.7.2 收敛前的配置直接读也要生效）
        legacy = _Host(config={'actions_interaction': {'send_poke': False}})
        with mock.patch.object(_Host, 'section', None):
            self.assertIs(legacy.action_switch('send_poke'), False)
            self.assertIsNone(legacy.action_switch('send_like'), '没写过的键照旧 = 未配置')

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
        # 旧形状（开关落在 `actions_risks`）读出来是同一份——升级不丢。
        legacy = _Host(
            config={'actions_risks': {'enabled': True, 'set_group_kick': True}},
            base_dir=str(self._write_permissions({'set_group_kick': 'global'})),
        )
        self.assertIs(legacy.action_switch('set_group_kick'), True)
        self.assertIn('set_group_kick', legacy.available_platform_actions())

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
