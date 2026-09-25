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

    def test_overview_flags_read_the_nested_model_groups(self):
        """嵌在「模型中心」下的四个开关必须按**嵌套**那份读。

        setUp 的配置里 `model_center.vision/audio/embedding.enabled = true`。
        早期实现只看顶层分组（顶层压根没有 `vision` 这个键），于是读出来是默认值：
        开着的显示成关着、关着的显示成开着——用户在控制台看到的和角色实际行为相反。
        """
        flags = _run(self.api.overview())['flags']
        self.assertTrue(flags['vision'])
        self.assertTrue(flags['audio'])
        self.assertTrue(flags['embedding'])

    def test_overview_flags_follow_nested_compaction_off(self):
        bridge = _make_bridge({'model_center': {'compaction': {'enabled': False}}})
        self.assertFalse(_run(ConsoleApi(bridge).overview())['flags']['compaction'])

    def test_nested_flag_read_survives_a_restart_without_junk_keys(self):
        """重启后宿主会删掉历史遗留的假顶层键，此时也不能读回默认值。"""
        bridge = _make_bridge({'model_center': {'vision': {'enabled': True}}})
        # 磁盘上没有顶层 `vision`（宿主按 schema 重建过），读的必须是嵌套那份。
        self.assertTrue(bridge.section('vision')['enabled'])
        self.assertTrue(_run(ConsoleApi(bridge).overview())['flags']['vision'])

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
        """`vision` / `compaction` 等嵌在 model_center 下，写盘只能写那一处。

        顶层同名分组**不是**合法配置：schema 里没有它，宿主下次加载会当未知键删掉
        （日志 `Config key removed: vision`），还会混进导出的配置文件。所以这里断言
        写盘结果里没有假顶层键，而且重载之后控制台仍然读得到刚写的值。
        """
        import asyncio

        saved = self._wire_writes()
        result = asyncio.run(self.api.set_flag('vision', True))
        self.assertTrue(saved['model_center']['vision']['enabled'])
        self.assertNotIn('vision', saved, '不该写出一个 schema 里没有的顶层分组')
        self.assertTrue(result['flags']['vision'])
        # 关键回归：脱离那次写入的内存副本，从磁盘重新归一化后仍然读得到。
        self.bridge.reload_config()
        self.assertTrue(_run(self.api.overview())['flags']['vision'])

    def test_set_flag_cleans_up_the_legacy_top_level_junk_group(self):
        """旧版本控制台写出来的假顶层键，在下次切换时顺手清掉。"""
        import asyncio

        saved = self._wire_writes()
        saved['vision'] = {'enabled': False}   # 历史遗留
        asyncio.run(self.api.set_flag('vision', True))
        self.assertNotIn('vision', saved)
        self.assertTrue(saved['model_center']['vision']['enabled'])

    def test_set_flag_still_writes_real_top_level_sections(self):
        """顶层分组（runtime / agency / …）还是写原来那一处，别顺手搬家。"""
        import asyncio

        saved = self._wire_writes()
        asyncio.run(self.api.set_flag('allow_proactive_messages', True))
        asyncio.run(self.api.set_flag('agency', False))
        self.assertTrue(saved['runtime']['allow_proactive_messages'])
        self.assertFalse(saved['agency']['enabled'])

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

    # ---- 剧本清单与「并入主剧本」 ----

    def _story_row(self, story_id: str, *, status: str = 'active', updated: str = '2026-01-02T00:00:00Z',
                   character: str = '凌梦', entries: int = 0) -> None:
        self.bridge.db.upsert('interlude_story', {
            'id': story_id, 'platform': 'qq', 'selfId': '20000', 'status': status,
            'setting': json.dumps({'character': {'name': character}, 'timezone': 'Asia/Shanghai'}),
            'state': json.dumps({}), 'cursorAt': updated, 'createdAt': '2026-01-01T00:00:00Z',
            'updatedAt': updated,
        })
        for index in range(entries):
            self.bridge.db.insert('interlude_script_entry', {
                'storyId': story_id, 'kind': 'script', 'actor': 'narrator',
                'content': '第 %d 段' % index, 'occurredAt': updated, 'metadata': json.dumps({}),
                'createdAt': updated,
            })

    def test_stories_lists_every_story_including_archived(self):
        """用户报过「新的人发消息前面的剧本就没了」——清单必须把归档的也列出来。"""
        self._story_row('character:qq:20000', entries=3, updated='2026-01-03T00:00:00Z')
        self._story_row('qq:20000:10001', status='archived', entries=5, updated='2026-01-01T00:00:00Z')
        payload = _run(self.api.stories())
        ids = [item['id'] for item in payload['stories']]
        self.assertEqual(ids, ['character:qq:20000', 'qq:20000:10001'])
        self.assertEqual(payload['main'], 'character:qq:20000')
        shared, old = payload['stories']
        self.assertTrue(shared['shared'])
        self.assertEqual(shared['entries'], 3)
        self.assertFalse(old['shared'])
        self.assertEqual(old['status'], 'archived')
        self.assertEqual(old['entries'], 5)

    def test_stories_without_a_main_story_reports_it_explicitly(self):
        """还没有共享主剧本时 `main` 为空——控制台靠它显示「设为主剧本」而不是「并入」。
        面板默认读的那一部（`active_story`）仍然是最近更新的旧剧本。"""
        self._story_row('qq:20000:10001', entries=1)
        payload = _run(self.api.stories())
        self.assertEqual(payload['main'], '')
        self.assertEqual(payload['active_story'], 'qq:20000:10001')
        self.assertFalse(payload['stories'][0]['main'])

    def test_stories_marks_the_active_character_story_as_main(self):
        self._story_row('character:qq:20000', entries=3, updated='2026-01-03T00:00:00Z')
        self._story_row('qq:20000:10001', status='archived', entries=5, updated='2026-01-01T00:00:00Z')
        payload = _run(self.api.stories())
        self.assertEqual(payload['main'], 'character:qq:20000')
        marked = {item['id']: item['main'] for item in payload['stories']}
        self.assertEqual(marked, {'character:qq:20000': True, 'qq:20000:10001': False})

    def test_an_archived_character_story_is_not_the_main_story(self):
        """归档的 `character:…` 不算主剧本——否则「并入」会把内容搬进死档案。"""
        self._story_row('character:qq:20000', status='archived', entries=3)
        self._story_row('qq:20000:10001', entries=1, updated='2026-01-04T00:00:00Z')
        payload = _run(self.api.stories())
        self.assertEqual(payload['main'], '')
        self.assertEqual(payload['active_story'], 'qq:20000:10001')

    def test_promote_story_requires_a_service(self):
        self._story_row('qq:20000:10001')
        with self.assertRaises(ConsoleError):
            _run(self.api.promote_story('qq:20000:10001'))
        with self.assertRaises(ConsoleError):
            _run(self.api.promote_story(''))

    def test_merge_story_reports_a_clear_error_without_a_service(self):
        self._story_row('qq:20000:10001')
        with self.assertRaises(ConsoleError):
            _run(self.api.merge_story('qq:20000:10001'))

    def test_merge_story_requires_a_source(self):
        with self.assertRaises(ConsoleError):
            _run(self.api.merge_story(''))

    # ---- 承诺与意图：内部调度折叠 ----

    def _intent_row(self, story_id: str, kind: str, status: str = 'completed') -> None:
        self.bridge.db.insert('interlude_intent', {
            'storyId': story_id, 'type': kind, 'status': status,
            'summary': 'The character is still typing the next message segment.'
                       if kind == 'split-message' else 'Retry the interrupted narrative turn (attempt 1/6).',
            'notBefore': '2026-09-25T14:33:22Z', 'payload': json.dumps({}),
            'createdAt': '2026-09-25T14:33:00Z', 'updatedAt': '2026-09-25T14:33:22Z',
        })

    def test_memory_marks_host_scheduler_intents_as_internal(self):
        """`split-message`（气泡节拍）与 `narrative-retry`（失败重试）是宿主自己的调度账。

        用户报「承诺与意图」里整列都是 'The character is still typing the next message
        segment.'，看着像坏了——它们不该混在"她答应了什么"里，控制台默认折叠。
        """
        self._story_row('character:qq:20000', entries=1)
        for kind in ('split-message', 'narrative-retry', 'follow-up-commitment', 'proactive-check'):
            self._intent_row('character:qq:20000', kind)
        payload = _run(self.api.memory())
        flags = {item['type']: item['internal'] for item in payload['intents']}
        self.assertEqual(flags, {
            'split-message': True,
            'narrative-retry': True,
            'follow-up-commitment': False,
            'proactive-check': False,
        }, '人话层面的意图不能被误标成内部调度')

    def test_memory_without_intents_still_returns_the_key(self):
        self._story_row('character:qq:20000', entries=1)
        payload = _run(self.api.memory())
        self.assertEqual(payload['intents'], [])

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


class ConfigEditorTests(unittest.TestCase):
    """控制台配置页：schema 取数 + **按 schema 路径**写配置。

    这一层存在的理由：AstrBot 自带配置页把 `type: list` 渲染成字符串数组控件
    （`ListConfigItem`，props 里连 `itemMeta` 都没有），对象行字段在那儿编辑会被
    压成一个字符串。所以控制台按 `_conf_schema.json` 自己渲染表单——写入门禁也
    随之从"手写 10 个开关"升级为"整份 schema"：路径必须在 schema 里。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, 'astrbot_plugin_hds_interlude_config.json')
        with open(self.path, 'w', encoding='utf-8') as handle:
            handle.write('\ufeff' + json.dumps({
                'qq_access': {'enabled': False, 'user_accounts': []},
                'model_center': {'providers': [{'label': 'P', 'api_key': 'sk-real'}]},
            }, ensure_ascii=False))
        self.bridge = _make_bridge({})
        self.bridge._live_config = None
        self.bridge.config_file_path = lambda: self.path  # type: ignore[method-assign]
        self.api = ConsoleApi(self.bridge)

    def _read(self):
        with open(self.path, encoding='utf-8-sig') as handle:
            return json.load(handle)

    # ---- 读 ----

    def test_schema_payload_covers_every_group_and_field(self):
        payload = _run(self.api.config_schema())
        self.assertEqual(len(payload['groups']), 22, '22 个顶层分组都要下发给配置页')
        qa = next(group for group in payload['groups'] if group['key'] == 'qq_access')
        fields = {field['key']: field for field in qa['fields']}
        self.assertEqual(fields['user_accounts']['value'], [])
        self.assertTrue(fields['user_accounts']['rows'], '对象行要把行字段一起给前端')
        self.assertIn('node', fields['user_accounts'], '原始 schema 节点：前端据此递归渲染')

    def test_every_group_carries_a_short_title(self):
        """分组要有短标题：下拉与卡片用它，`description` 是那句话说明。

        没标题就会出现「模型中心：先添加连接并勾选用途，主叙事参数与高级模块同处。…」
        这种塞满下拉的标签（用户直接指出太长）。
        """
        payload = _run(self.api.config_schema())
        for group in payload['groups']:
            with self.subTest(group=group['key']):
                self.assertTrue(group['title'].strip(), group['key'])
                self.assertLessEqual(len(group['title']), 16, group['title'])
                # 短标题不该是整句说明
                self.assertNotIn('。', group['title'])

    def test_object_row_lists_get_the_host_editor_warning(self):
        payload = _run(self.api.config_schema())
        notes = {
            field['path']: field['note']
            for group in payload['groups'] for field in group['fields']
        }
        for path in ('qq_access.user_accounts', 'qq_access.bot_accounts',
                     'qq_access.group_chats', 'model_center.providers'):
            self.assertIsNotNone(notes[path], path)
            self.assertEqual(notes[path]['level'], 'info' if path == 'model_center.providers' else 'warn', path)
        # 标量列表不受影响：宿主的字符串数组控件编辑它是 OK 的。
        self.assertIsNone(notes.get('browser.allowed_domains'))
        # 连接池有专用面板：这里只给说明，不给通用控件。
        self.assertTrue(
            next(
                field for group in payload['groups'] if group['key'] == 'model_center'
                for field in group['fields'] if field['key'] == 'providers'
            )['delegated'],
        )

    def test_secrets_never_reach_the_browser(self):
        payload = _run(self.api.config_schema())
        blob = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn('sk-real', blob)

    # ---- 写 ----

    def test_writes_a_whitelist_row_and_takes_effect(self):
        _run(self.api.set_config_value('qq_access.user_accounts', [{
            'qq': '10001', 'label': '主人', 'person_id': 'kela',
            'profile': '爱喝冷萃', 'relationship': '恋人', 'enabled': True,
        }]))
        written = self._read()['qq_access']['user_accounts']
        self.assertEqual(written[0]['label'], '主人')
        # 写什么就生效什么：内存里的配置立刻能读到
        self.assertEqual(self.bridge.section('onebot')['user_accounts'][0]['qq'], '10001')

    def test_paths_outside_the_schema_are_rejected(self):
        for path in ('不存在.字段', 'qq_access.nope', 'qq_access', ''):
            with self.subTest(path=path):
                with self.assertRaises(ConsoleError):
                    _run(self.api.set_config_value(path, 1))

    def test_list_fields_cannot_be_written_through(self):
        """`…providers.api_key` 这种路径会把整个列表写成一个字典，必须拒绝。"""
        with self.assertRaises(ConsoleError):
            _run(self.api.set_config_value('model_center.providers.api_key', 'x'))
        self.assertIsInstance(self._read()['model_center']['providers'], list)

    def test_types_are_coerced_and_checked(self):
        _run(self.api.set_config_value('qq_access.enabled', 'true'))
        self.assertIs(self._read()['qq_access']['enabled'], True)
        _run(self.api.set_config_value('runtime.max_message_characters', '1500'))
        self.assertEqual(self._read()['runtime']['max_message_characters'], 1500)
        # 行被压成字符串（宿主那个控件干的事）→ 报可读的错，而不是写坏配置
        with self.assertRaises(ConsoleError):
            _run(self.api.set_config_value('qq_access.bot_accounts', ['10001']))
        with self.assertRaises(ConsoleError):
            _run(self.api.set_config_value('qq_access.user_accounts', {'qq': '1'}))

    def test_clearing_a_key_falls_back_to_the_schema_default(self):
        _run(self.api.set_config_value('runtime.max_message_characters', 1500))
        _run(self.api.set_config_value('runtime.max_message_characters', None))
        self.assertNotIn('max_message_characters', self._read()['runtime'])

    def test_nested_object_field_is_writable(self):
        _run(self.api.set_config_value('model_center.vision.enabled', True))
        self.assertIs(self._read()['model_center']['vision']['enabled'], True)
        # 同组其它字段没被顺手改掉
        self.assertIn('providers', self._read()['model_center'])

    # ---- 参与者（白名单一键填入） ----

    def test_participants_endpoint_exposes_platform_accounts(self):
        # 用真实的内存库：控制台读的是"某个会话真的来过"这件事，桩库读不出行。
        database = Database(':memory:')
        self.addCleanup(database.close)
        database.register_tables()
        database.upsert('interlude_participant', {
            'id': 'p1', 'storyId': 's1', 'platform': 'qq', 'selfId': '20000',
            'userId': '10001', 'channelId': '', 'personId': 'kela',
            'displayName': '主人', 'relationship': '恋人', 'status': 'active',
        })
        self.bridge.db = database
        payload = _run(self.api.participants())
        self.assertEqual(payload['participants'][0]['user_id'], '10001')
        self.assertEqual(payload['participants'][0]['display_name'], '主人')
