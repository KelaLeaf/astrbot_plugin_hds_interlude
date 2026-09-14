"""控制台后端（`adapters/console_api.py`）的数据形状与容错。

这些用例都走**真实** `AstrbotBridge` + 内存数据库（沿用 `test_astrbot_bridge.py`
里的桩），因为控制台的整个价值就在于"读出来的是真数据"。
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from unittest import mock

# 复用桥接测试里的 AstrBot 桩与夹具（导入即装桩）
from plugin.tests.test_astrbot_bridge import FakeContext, _make_bridge, bridge_module
from plugin.adapters import console_api as console_module
from plugin.adapters.console_api import ConsoleApi, ConsoleError, CONSOLE_TASKS, mask_endpoint
from plugin.core.database import Database


def _run(coro):
    return asyncio.run(coro)


class MaskEndpointTests(unittest.TestCase):
    """endpoint 会进浏览器，query 里可能有 key，必须脱敏。"""

    def test_query_string_is_dropped(self):
        self.assertEqual(
            mask_endpoint('https://api.example.com/v1/chat/completions?api_key=SECRET'),
            'https://api.example.com/v1/chat/completions',
        )

    def test_plain_and_empty_values(self):
        self.assertEqual(mask_endpoint('http://127.0.0.1:11434/v1/chat/completions'),
                         'http://127.0.0.1:11434/v1/chat/completions')
        self.assertEqual(mask_endpoint(''), '')
        self.assertEqual(mask_endpoint(None), '')

    def test_non_url_is_returned_verbatim(self):
        self.assertEqual(mask_endpoint('not a url'), 'not a url')


class ConsoleApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.bridge = _make_bridge({
            'model_center': {
                'main_provider_id': 'ollama',
                'providers': [{
                    'label': 'Primary model', 'enabled': True,
                    'endpoint': 'https://gw.example.com/v1/chat/completions?api_key=SECRET',
                    'api_key': 'sk-secret', 'model': 'demo',
                    'use_for_main': True, 'price_input': 1.5, 'price_output': 6.0,
                }],
                'embedding': {'enabled': True, 'dimensions': 1024},
                'vision': {'enabled': True, 'mode': 'sidecar', 'provider_id': 'ollama'},
                'audio': {'enabled': True, 'provider_id': 'whisper'},
            },
            'runtime': {'allow_proactive_messages': True},
            'urge': {'enabled': True},
        })
        # 换成**真实**的内存库：控制台的价值就在于读真数据，这里也顺带把 SQL 路径跑通
        self.database = Database(':memory:')
        self.addCleanup(self.database.close)
        self.database.register_tables()
        self.bridge.db = self.database
        self.api = ConsoleApi(self.bridge)

    # ---- overview ----

    def test_overview_shape_is_stable(self):
        payload = _run(self.api.overview())
        for key in ('plugin', 'service', 'story', 'stories', 'flags', 'routing', 'capability', 'counts'):
            self.assertIn(key, payload, key)
        self.assertEqual(payload['plugin']['name'], bridge_module.PLUGIN_NAME)
        self.assertTrue(payload['plugin']['version'])
        self.assertTrue(payload['plugin']['upstream_version'])
        # routing 必须覆盖全部任务，且页面靠 `label` 渲染
        self.assertEqual([row['task'] for row in payload['routing']], [key for key, _ in CONSOLE_TASKS])
        self.assertTrue(all(row['label'] for row in payload['routing']))

    def test_overview_works_without_any_story(self):
        """空库也要能打开控制台（页面要显示"还没有剧本"）。"""
        payload = _run(self.api.overview())
        self.assertIsNone(payload['story'])
        self.assertEqual(payload['stories'], [])
        self.assertEqual(payload['service']['story_count'], 0)

    def test_overview_reads_real_rows(self):
        self.bridge.db.insert('interlude_story', {
            'id': 's1', 'platform': 'webchat', 'status': 'active',
            'setting': json.dumps({'character': {'name': '凌梦'}, 'timezone': 'Asia/Shanghai'}),
            'state': json.dumps({}), 'cursorAt': '2026-01-01T00:00:00Z',
            'createdAt': '2026-01-01T00:00:00Z', 'updatedAt': '2026-01-02T00:00:00Z',
        })
        payload = _run(self.api.overview())
        self.assertEqual(payload['story']['id'], 's1')
        self.assertEqual(payload['story']['character'], '凌梦')
        self.assertEqual(payload['service']['story_count'], 1)

    def test_overview_can_select_a_specific_story(self):
        for index in (1, 2):
            self.bridge.db.insert('interlude_story', {
                'id': f's{index}', 'platform': 'webchat', 'status': 'active',
                'setting': json.dumps({'character': {'name': f'角色{index}'}}),
                'state': json.dumps({}), 'createdAt': '2026-01-01T00:00:00Z',
                'updatedAt': f'2026-01-0{index + 1}T00:00:00Z',
            })
        payload = _run(self.api.overview('s1'))
        self.assertEqual(payload['story']['id'], 's1')
        # 未知 id 回落到最近更新的那个，而不是报错
        self.assertEqual(_run(self.api.overview('不存在'))['story']['id'], 's2')

    def test_overview_flags_follow_config(self):
        flags = _run(self.api.overview())['flags']
        self.assertTrue(flags['allow_proactive_messages'])
        self.assertTrue(flags['urge'])
        self.assertFalse(flags['blind_mode'])

    # ---- models ----

    def test_models_never_leaks_the_api_key(self):
        payload = _run(self.api.models())
        blob = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn('sk-secret', blob)
        self.assertNotIn('SECRET', blob)
        row = payload['connections'][0]
        self.assertTrue(row['has_key'])
        self.assertEqual(row['endpoint'], 'https://gw.example.com/v1/chat/completions')
        self.assertEqual(row['tasks'], ['主叙事'])
        self.assertEqual(row['prices']['input'], 1.5)

    def test_models_reports_task_bindings_and_modalities(self):
        payload = _run(self.api.models())
        self.assertEqual(payload['task_models']['main']['astrbot_provider'], 'ollama')
        self.assertEqual(payload['task_models']['audio']['astrbot_provider'], 'whisper')
        self.assertEqual(payload['task_models']['vision']['label'], '侧端识图')
        self.assertEqual(payload['vision']['mode'], 'sidecar')
        self.assertEqual(payload['embedding']['dimensions'], 1024)
        self.assertTrue(payload['failover']['enabled'])

    def test_usage_buffer_starts_empty_and_accumulates(self):
        self.assertEqual(_run(self.api.models())['usage']['sum']['calls'], 0)
        self.bridge.usage_records.append({
            'at': 'now', 'task': 'main', 'model': 'demo',
            'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15,
        })
        self.bridge.usage_records.append({
            'at': 'now', 'task': 'main', 'model': 'demo',
            'prompt_tokens': 2, 'completion_tokens': 3, 'total_tokens': 5,
        })
        usage = _run(self.api.models())['usage']
        self.assertEqual(usage['sum']['total_tokens'], 20)
        self.assertEqual(usage['totals'][0]['task'], 'main')
        self.assertEqual(usage['totals'][0]['calls'], 2)
        # 最近记录是倒序的（页面直接渲染，不用再排）
        self.assertEqual(usage['recent'][0]['total_tokens'], 5)

    # ---- script / memory / database / logs ----

    def test_script_paginates(self):
        self.bridge.db.insert('interlude_story', {
            'id': 's1', 'platform': 'webchat', 'status': 'active',
            'setting': json.dumps({}), 'state': json.dumps({}),
            'createdAt': '2026-01-01T00:00:00Z', 'updatedAt': '2026-01-01T00:00:00Z',
        })
        for index in range(5):
            self.bridge.db.insert('interlude_script_entry', {
                'storyId': 's1', 'kind': 'user-message', 'actor': 'user',
                'content': f'第 {index} 条', 'occurredAt': f'2026-01-0{index + 1}T00:00:00Z',
                'createdAt': '2026-01-01T00:00:00Z',
            })
        payload = _run(self.api.script('s1', limit=2, offset=0))
        self.assertEqual(payload['total'], 5)
        self.assertEqual(len(payload['entries']), 2)
        # 故事时间倒序：最新那条在前
        self.assertEqual(payload['entries'][0]['content'], '第 4 条')
        page2 = _run(self.api.script('s1', limit=2, offset=2))
        self.assertEqual(page2['entries'][0]['content'], '第 2 条')

    def test_script_without_a_story_is_an_empty_shell(self):
        payload = _run(self.api.script(''))
        self.assertIsNone(payload['story'])
        self.assertEqual(payload['entries'], [])

    def test_memory_returns_all_five_buckets(self):
        payload = _run(self.api.memory())
        for key in ('facts', 'memories', 'intents', 'patches', 'overlays', 'participants'):
            self.assertIn(key, payload, key)

    def test_database_lists_every_table_with_row_counts(self):
        payload = _run(self.api.database())
        names = {table['name'] for table in payload['tables']}
        self.assertEqual(names, set(console_module.TABLES))
        self.assertGreaterEqual(len(payload['tables']), 13)
        self.assertEqual(payload['total_rows'], sum(table['rows'] for table in payload['tables']))

    def test_logs_are_newest_first_and_filterable(self):
        self.bridge.log_buffer.append({'at': '1', 'level': 'info', 'text': '第一条'})
        self.bridge.log_buffer.append({'at': '2', 'level': 'warn', 'text': '第二条'})
        payload = _run(self.api.logs())
        self.assertEqual([item['text'] for item in payload['records']], ['第二条', '第一条'])
        self.assertEqual(payload['capacity'], console_module.CONSOLE_LOG_MAX)
        only_warn = _run(self.api.logs(level='warn'))
        self.assertEqual([item['text'] for item in only_warn['records']], ['第二条'])

    def test_logs_limit_is_clamped(self):
        for index in range(10):
            self.bridge.log_buffer.append({'at': str(index), 'level': 'info', 'text': f't{index}'})
        self.assertEqual(len(_run(self.api.logs(limit=3))['records']), 3)
        # 负数 / 垃圾值回落到默认值，而不是抛异常
        self.assertTrue(len(_run(self.api.logs(limit=-5))['records']) <= 10)

    # ---- Alter / Agency / 投递账本 ----

    def _seed_story(self, state=None, story_id='s1'):
        self.database.insert('interlude_story', {
            'id': story_id, 'platform': 'webchat', 'status': 'active',
            'setting': json.dumps({'character': {'name': '凌梦'}}),
            'state': json.dumps(state or {}),
            'createdAt': '2026-01-01T00:00:00Z', 'updatedAt': '2026-01-02T00:00:00Z',
        })
        return story_id

    def test_alter_reads_the_story_state(self):
        self._seed_story({'alter_system': {
            'alterValue': 0.4, 'alterWeight': 0.9, 'lastTriggerDirection': -1,
            'emotionalOffset': {'direction': 'serious', 'description': '今天有点沉', 'intensity': 0.6},
            'history': [
                {'turn': 1, 'phase': 'user-message', 'alter': 0.2, 'alterValue': 0.2, 'timestamp': 't1'},
                {'turn': 2, 'phase': 'auto', 'alter': 0.2, 'alterValue': 0.4, 'timestamp': 't2'},
            ],
            'pendingScopes': [{'participantId': 'p1', 'alterValue': 0.3}],
        }})
        payload = _run(self.api.alter())
        self.assertEqual(payload['state']['value'], 0.4)
        self.assertEqual(payload['state']['direction'], -1)
        self.assertEqual(payload['state']['offset']['direction'], 'serious')
        self.assertEqual([item['alter_value'] for item in payload['history']], [0.2, 0.4])
        self.assertEqual(payload['pending'][0]['participant_id'], 'p1')

    def test_alter_without_state_is_an_empty_shell(self):
        self._seed_story({})
        payload = _run(self.api.alter())
        self.assertEqual(payload['history'], [])
        self.assertEqual(payload['state']['value'], 0)
        self.assertIsNone(payload['state']['offset'])

    def test_agency_reads_window_and_plan(self):
        self._seed_story({'agency_window': {
            'activityLoad': 'busy', 'privacy': 'private', 'deviceAccess': 'limited',
            'nextOpportunityAt': '2026-01-03T10:00:00Z', 'validUntil': '2026-01-03T12:00:00Z',
            'basis': '刚开完会', 'sourceEntryIds': [7, 8],
        }})
        self.database.insert('interlude_schedule_preplan', {
            'storyId': 's1', 'revision': 3, 'timezone': 'Asia/Shanghai',
            'validFrom': '2026-01-01', 'validThrough': '2026-01-07',
            'regimes': [{'name': '上班'}], 'exceptions': [], 'materializedDays': ['2026-01-01'],
            'createdAt': '2026-01-01T00:00:00Z', 'updatedAt': '2026-01-01T00:00:00Z',
        })
        payload = _run(self.api.agency())
        self.assertEqual(payload['window']['activity_load'], 'busy')
        self.assertEqual(payload['window']['source_entry_ids'], [7, 8])
        self.assertEqual(payload['plan']['revision'], 3)
        self.assertEqual(len(payload['plan']['regimes']), 1)

    def test_delivery_flattens_the_ledger(self):
        self._seed_story({})
        self.database.insert('interlude_script_entry', {
            'storyId': 's1', 'kind': 'character-message', 'actor': 'character', 'content': '在的',
            'occurredAt': '2026-01-02T00:00:00Z', 'createdAt': '2026-01-02T00:00:00Z',
            'metadata': json.dumps({'delivery_actions': [{
                'commitId': 'c1', 'eventId': 'e1', 'status': 'partial', 'attempts': 2,
                'segments': [
                    {'index': 0, 'kind': 'text', 'status': 'delivered', 'attempts': 1},
                    {'index': 1, 'kind': 'image', 'status': 'failed', 'attempts': 2, 'error': '平台不支持'},
                ],
            }]}),
        })
        payload = _run(self.api.delivery())
        self.assertEqual(len(payload['actions']), 1)
        action = payload['actions'][0]
        self.assertEqual(action['commit_id'], 'c1')
        self.assertEqual(action['status'], 'partial')
        self.assertEqual(action['done'], 1)
        self.assertEqual(action['segments'][1]['error'], '平台不支持')
        self.assertEqual(payload['totals'], {'partial': 1})

    def test_delivery_can_filter_by_status(self):
        self._seed_story({})
        for status in ('delivered', 'failed'):
            self.database.insert('interlude_script_entry', {
                'storyId': 's1', 'kind': 'character-message', 'actor': 'character', 'content': status,
                'occurredAt': '2026-01-02T00:00:00Z', 'createdAt': '2026-01-02T00:00:00Z',
                'metadata': json.dumps({'delivery_actions': [{'commitId': status, 'status': status}]}),
            })
        payload = _run(self.api.delivery(status='failed'))
        self.assertEqual([item['status'] for item in payload['actions']], ['failed'])
        # 过滤不影响总计——页面上的筛选按钮要能显示每个状态有多少条
        self.assertEqual(payload['totals'], {'delivered': 1, 'failed': 1})

    def test_delivery_ignores_entries_without_a_ledger(self):
        self._seed_story({})
        self.database.insert('interlude_script_entry', {
            'storyId': 's1', 'kind': 'user-message', 'actor': 'user', 'content': '你好',
            'occurredAt': '2026-01-02T00:00:00Z', 'createdAt': '2026-01-02T00:00:00Z',
            'metadata': json.dumps({}),
        })
        payload = _run(self.api.delivery())
        self.assertEqual(payload['actions'], [])
        self.assertEqual(payload['totals'], {})

    # ---- 写操作 ----

    def _wire_writes(self):
        """写操作要读**磁盘上的原样配置**，所以给 bridge 一个真实文件 + 一个假 save_config。

        返回 `saved`：`save_config` 收到的 schema 形状配置（断言用）。
        """
        saved: dict = {}
        path = os.path.join(self._tmp.name, 'astrbot_plugin_hds_interlude_config.json')
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write('\ufeff' + json.dumps({
                'model_center': {'providers': [{
                    'label': 'Primary model', 'enabled': True,
                    'endpoint': 'https://gw.example.com/v1/chat/completions',
                    'api_key': 'sk-secret', 'model': 'demo', 'use_for_main': True,
                    'price_input': 1.5, 'price_output': 6.0,
                }]},
                'runtime': {'allow_proactive_messages': False},
            }, ensure_ascii=False))

        class _Live(dict):
            # 真实宿主的 `save_config` 会**写文件**；测试替身照做，否则连续两次写
            # 读回的还是旧内容（曾经因此误判成产品 bug）。
            def save_config(self, replace_config=None, **kwargs):  # noqa: ARG002
                saved.clear()
                saved.update(replace_config or {})
                with open(path, 'w', encoding='utf-8') as handle:
                    handle.write('\ufeff' + json.dumps(saved, ensure_ascii=False))

        self.bridge._live_config = _Live()
        self.bridge.config_file_path = lambda: path  # type: ignore[method-assign]
        return saved

    def test_set_flag_writes_through_and_reloads(self):
        import asyncio

        saved = self._wire_writes()
        result = asyncio.run(self.api.set_flag('allow_proactive_messages', True))
        self.assertTrue(result['flags']['allow_proactive_messages'])
        self.assertIn('开启', result['changed'])
        # 落盘是 schema 形状：runtime 分组名不变，值已更新
        self.assertTrue(saved.get('runtime', {}).get('allow_proactive_messages'))

    def test_set_flag_rejects_unknown_names(self):
        import asyncio

        with self.assertRaises(ConsoleError):
            asyncio.run(self.api.set_flag('rm -rf', True))
        with self.assertRaises(ConsoleError):
            asyncio.run(self.api.set_flag('', True))

    def test_set_flag_writes_model_center_nested_groups_too(self):
        """`vision` / `compaction` 等嵌在 model_center 下，两处都要写。"""
        import asyncio

        saved = self._wire_writes()
        asyncio.run(self.api.set_flag('vision', True))
        self.assertTrue(saved['model_center']['vision']['enabled'])

    def test_save_connection_creates_and_keeps_the_key_hidden(self):
        import asyncio

        saved = self._wire_writes()
        result = asyncio.run(self.api.save_connection({
            'label': '备用连接', 'endpoint': 'https://api.example.com/v1/chat/completions',
            'api_key': 'sk-brand-new', 'model': 'demo', 'use_for_main': True,
        }))
        self.assertIn('新增', result['changed'])
        rows = saved['model_center']['providers']
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]['api_key'], 'sk-brand-new')
        # 返回给前端的列表里没有密钥原文
        self.assertNotIn('sk-brand-new', json.dumps(result, ensure_ascii=False))

    def test_save_connection_without_api_key_keeps_the_old_one(self):
        """编辑时前端拿不到旧密钥，所以"不传"必须等于"不改"。"""
        import asyncio

        saved = self._wire_writes()
        asyncio.run(self.api.save_connection({'index': 0, 'label': '改过名字'}))
        row = saved['model_center']['providers'][0]
        self.assertEqual(row['label'], '改过名字')
        self.assertEqual(row['api_key'], 'sk-secret', '留空必须保留原密钥')

    def test_save_connection_can_explicitly_clear_the_key(self):
        import asyncio

        saved = self._wire_writes()
        asyncio.run(self.api.save_connection({'index': 0, 'api_key': None}))
        self.assertEqual(saved['model_center']['providers'][0]['api_key'], '')

    def test_save_connection_validates_input(self):
        import asyncio

        with self.assertRaises(ConsoleError):
            asyncio.run(self.api.save_connection({'label': ''}))
        with self.assertRaises(ConsoleError):
            asyncio.run(self.api.save_connection({'label': 'x', 'endpoint': 'ftp://nope'}))
        with self.assertRaises(ConsoleError):
            asyncio.run(self.api.save_connection({'index': 99, 'label': 'x'}))
        with self.assertRaises(ConsoleError):
            asyncio.run(self.api.save_connection('not a dict'))

    def test_delete_connection_removes_the_row(self):
        import asyncio

        saved = self._wire_writes()
        result = asyncio.run(self.api.delete_connection(0))
        self.assertIn('删除', result['changed'])
        self.assertEqual(saved['model_center']['providers'], [])
        with self.assertRaises(ConsoleError):
            asyncio.run(self.api.delete_connection(0))

    def test_reads_and_writes_together_stay_serializable(self):
        self._seed_story({'alter_system': {'alterValue': 1}, 'agency_window': None})
        for coro in (self.api.alter(), self.api.agency(), self.api.delivery()):
            json.dumps(_run(coro), ensure_ascii=False)

    # ---- 容错 ----

    def test_endpoints_survive_a_broken_database(self):
        """表被删了（旧库 / 手工改过）也不能让整个面板 500。"""

        class _Broken:
            path = ''

            def count(self, *_args, **_kwargs):
                raise RuntimeError('no such table')

            def all(self, *_args, **_kwargs):
                raise RuntimeError('no such table')

        with mock.patch.object(self.bridge, 'db', _Broken()):
            overview = _run(self.api.overview())
            self.assertEqual(overview['stories'], [])
            self.assertEqual(set(overview['counts'].values()), {0})
            self.assertEqual(_run(self.api.script())['entries'], [])
            self.assertEqual(_run(self.api.database())['total_rows'], 0)

    def test_every_panel_returns_json_serializable_data(self):
        for coro in (
            self.api.overview(), self.api.models(), self.api.script(),
            self.api.memory(), self.api.database(), self.api.logs(),
        ):
            json.dumps(_run(coro), ensure_ascii=False)


class AnsiStrippingTests(unittest.TestCase):
    def test_layered_colors_never_reach_the_console_buffer(self):
        """core 的分层日志带 256 色转义序列，WebUI 里得是纯文本。"""
        bridge = _make_bridge({})
        sink_holder = {}

        class _Logger:
            def debug(self, text):
                sink_holder['text'] = text

            def warning(self, text):
                sink_holder['text'] = text

            def error(self, text):
                sink_holder['text'] = text

        bridge.logger = _Logger()
        bridge._register_log_sink()
        from plugin.core import logging as interlude_logging

        interlude_logging.log_layered({
            'level': 'warn', 'message': '带颜色的 %s', 'args': ['内容'], 'standalone': True,
        })
        records = list(bridge.log_buffer)
        self.assertTrue(records, '控制台缓冲必须收到这条日志')
        self.assertNotIn('\x1b', records[-1]['text'])
        self.assertIn('内容', records[-1]['text'])


if __name__ == '__main__':
    unittest.main()
