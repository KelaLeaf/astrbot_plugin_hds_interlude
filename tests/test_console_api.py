"""控制台后端（`adapters/console_api.py`）的数据形状与容错。

这些用例都走**真实** `AstrbotBridge` + 内存数据库（沿用 `test_astrbot_bridge.py`
里的桩），因为控制台的整个价值就在于"读出来的是真数据"。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# 复用桥接测试里的 AstrBot 桩与夹具（导入即装桩）
from plugin.tests.test_astrbot_bridge import TEST_DATA_DIR, FakeContext, _make_bridge, bridge_module
from plugin.adapters import console_api as console_module
from plugin.adapters.console_api import ConsoleApi, ConsoleError, CONSOLE_TASKS, mask_endpoint
from plugin.core import platform_actions
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
        self.assertEqual(mask_endpoint('http://127.0.0.1:9/v1/chat/completions'),
                         'http://127.0.0.1:9/v1/chat/completions')
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

    # ---- token 统计 ----

    def test_token_stats_summarises_the_ledger_and_handles_an_empty_table(self):
        """空表也要回一个完整空壳（面板显示 0，不是 500）——§29 的控制台取数约定。"""
        payload = _run(self.api.token_stats('day'))
        self.assertEqual(payload['range'], 'day')
        self.assertEqual(payload['totals']['inputTokens'], 0)
        self.assertEqual(len(payload['series']), 1, '按天视图至少给今天这一格')

        self.bridge.db.insert('interlude_token_usage', {
            'day': '2026-09-30', 'storyId': 's1', 'task': '主叙事', 'model': 'deepseek-chat',
            'provider': '连接A', 'inputTokens': 1200, 'outputTokens': 300, 'cachedTokens': 600,
            'calls': 3, 'createdAt': '2026-09-30T00:00:00Z', 'updatedAt': '2026-09-30T00:00:00Z',
        })
        self.bridge.db.insert('interlude_token_usage', {
            'day': '2026-09-29', 'storyId': 's1', 'task': '压缩', 'model': 'flash',
            'provider': '连接B', 'inputTokens': 400, 'outputTokens': 100, 'cachedTokens': 0,
            'calls': 1, 'createdAt': '2026-09-29T00:00:00Z', 'updatedAt': '2026-09-29T00:00:00Z',
        })
        week = _run(self.api.token_stats('week', '', ''))
        self.assertEqual(week['range'], 'week')
        self.assertEqual(week['totals']['inputTokens'], 1600)
        self.assertAlmostEqual(week['totals']['hitRate'], 600 / 1600)
        self.assertEqual({item['model'] for item in week['byModel']}, {'deepseek-chat', 'flash'})
        self.assertEqual({item['task'] for item in week['byTask']}, {'主叙事', '压缩'})
        self.assertEqual(len(week['series']), 7)

        # 自选范围能精确圈住一天（8 月那类老数据不进本周视图这件事由纯函数测试覆盖）。
        custom = _run(self.api.token_stats('custom', '2026-09-30', '2026-09-30'))
        self.assertEqual(custom['totals']['inputTokens'], 1200)
        self.assertEqual(custom['from'], '2026-09-30')

    # ---- 平台动作目录与权限（面板「动作」） ----

    def _temp_permissions(self) -> str:
        """把权限表指到本用例自己的临时目录：验证真的落成 JSON，且不污染别的用例。"""
        path = os.path.join(self._tmp.name, 'action_permissions.json')
        self.bridge._action_permissions_path = Path(path)
        self.bridge._action_permissions = {}
        return path

    def test_actions_catalog_mirrors_the_core_directory(self):
        """面板的目录必须与 `core/platform_actions.ACTIONS` 逐条一致（不许在控制台重算）。"""
        payload = _run(self.api.actions_catalog())
        rows = payload['actions']
        self.assertEqual([row['id'] for row in rows], list(platform_actions.ACTIONS))
        self.assertEqual(payload['stats']['total'], len(platform_actions.ACTIONS))
        json.dumps(payload, ensure_ascii=False)  # 面板直接吃它，必须可序列化
        for row in rows:
            action = platform_actions.ACTIONS[row['id']]
            with self.subTest(action=row['id']):
                self.assertEqual(row['label'], action.label)
                self.assertEqual(row['summary'], action.summary)
                self.assertEqual(row['risk'], action.risk)
                self.assertEqual(row['category'], action.category)
                self.assertEqual(row['category_label'],
                                 platform_actions.ACTION_CATEGORIES[action.category])
                self.assertEqual(row['default_permission'], action.default_permission)
                self.assertEqual(row['group'], platform_actions.action_config_group(action))
                self.assertEqual([param['name'] for param in row['params']],
                                 [param.name for param in action.params])
                for param in row['params']:
                    self.assertEqual(sorted(param), sorted(
                        ['name', 'label', 'type', 'required', 'minimum', 'maximum', 'choices', 'note'],
                    ))
        # 四档 + 中文说明；分组表覆盖全部可见动作组（面板要说明"开关在哪一组"）
        self.assertEqual([tier['id'] for tier in payload['tiers']],
                         list(platform_actions.PERMISSION_TIERS))
        self.assertTrue(all(tier['label'] and tier['description'] for tier in payload['tiers']))
        for row in rows:
            self.assertIn(row['group'], payload['groups'])
        # v1.7.4：动作开关并进一个父组 `robot_actions`，落点是三个**子组**（点分路径）。
        # 文案必须是子组中文名，而不是"先到的类别"（`robot_actions.chat` 里坐着互动/消息/
        # 历史/状态/资料/语音/联系人七类，叫「互动」是错的）。
        self.assertEqual(payload['groups'],
                         dict(platform_actions.ACTION_CONFIG_GROUP_LABELS))
        self.assertEqual(sorted(payload['groups']),
                         ['robot_actions.chat', 'robot_actions.group', 'robot_actions.qzone'])
        self.assertEqual(payload['groups']['robot_actions.chat'], '会话动作')
        self.assertEqual(set(payload['groups'].values()),
                         {'会话动作', '群管理动作', 'QQ 空间动作'})
        legacy_names = {'actions_chat', 'actions_group', 'actions_qzone', 'actions_risks'}
        self.assertEqual(legacy_names & {row['group'] for row in rows}, set(),
                         '旧组名不许再作为落点出现在动作页 payload 里')
        # 各 risk 计数与档位分布都要对得上
        counts = {}
        for row in rows:
            counts[row['risk']] = counts.get(row['risk'], 0) + 1
        for level in platform_actions.RISK_LEVELS:
            self.assertEqual(payload['stats']['risk'][level], counts.get(level, 0), level)
        self.assertEqual(
            sum(payload['stats']['permissions'].values()), len(rows),
            '档位分布必须覆盖每一个动作',
        )

    def test_dangerous_actions_default_to_disabled_and_the_warning_is_verbatim(self):
        payload = _run(self.api.actions_catalog())
        risky = [row for row in payload['actions'] if row['risk'] == 'dangerous']
        self.assertTrue(risky)
        self.assertEqual(payload['risky'], [row['id'] for row in risky])
        for row in risky:
            with self.subTest(action=row['id']):
                self.assertEqual(row['default_permission'], 'disabled')
                self.assertEqual(row['permission'], 'disabled')
                self.assertFalse(row['enabled'])
        self.assertEqual(payload['stats']['risky'], len(risky))
        self.assertEqual(payload['stats']['risky_enabled'], 0, '默认没有任何危险动作在跑')
        # 警示语必须是 core 里那一句原文，不许在控制台另写一句
        self.assertEqual(platform_actions.RISK_WARNING,
                         '此标签下功能具有一定风险，易误操作，请谨慎开启。')
        self.assertEqual(payload['risk_warning'], platform_actions.RISK_WARNING)

    def test_catalog_survives_an_unconfigured_empty_plugin(self):
        """没有配置 / 没有数据库 / 没有任何剧本时也要回完整目录（§29 的控制台取数约定）。"""
        bare = _make_bridge({})
        bare.db = None
        payload = _run(ConsoleApi(bare).actions_catalog())
        self.assertEqual(len(payload['actions']), len(platform_actions.ACTIONS))
        self.assertEqual(payload['stats']['enabled'], len(platform_actions.ACTIONS)
                         - payload['stats']['risky'])
        self.assertTrue(all(row['config_enabled'] is None for row in payload['actions']),
                        '分组不存在 = 未配置 = 不限制')
        self.assertTrue(payload['permissions_path'].endswith('action_permissions.json'))

    def test_config_switch_is_the_master_switch(self):
        """配置开关关掉时档位无论选什么都不生效（与关系），但档位本身照原样显示。"""
        self.bridge.config['actions_interaction'] = {'send_poke': False}
        payload = _run(self.api.actions_catalog())
        row = next(item for item in payload['actions'] if item['id'] == 'send_poke')
        self.assertIs(row['config_enabled'], False)
        self.assertEqual(row['permission'], 'global', '档位是权限表的值，不因开关而改写')
        self.assertFalse(row['enabled'], '开关是总闸，关掉就不生效')
        self.assertEqual(payload['stats']['enabled'],
                         len(payload['actions']) - payload['stats']['risky'] - 1)

    def test_enabling_a_dangerous_action_drives_the_warning_counter(self):
        """危险动作的开关现在落在它自己类别所属的子组里（群管理类 → `robot_actions.group`）。"""
        self.bridge.config['robot_actions'] = {'group': {'set_group_kick': True}}
        _run(self.api.set_action_permission('set_group_kick', 'admin'))
        payload = _run(self.api.actions_catalog())
        row = next(item for item in payload['actions'] if item['id'] == 'set_group_kick')
        self.assertEqual(row['permission'], 'admin')
        self.assertEqual(row['group'], 'robot_actions.group')
        self.assertTrue(row['enabled'])
        self.assertEqual(payload['stats']['risky_enabled'], 1)

    # ---- v1.7.2 分组收敛 / v1.7.3 取消风险组：旧格式配置仍要读得到 ----

    def test_action_switches_read_old_format_groups(self):
        """旧格式：开关写在 `actions_interaction` 等旧组里，没有 `robot_actions`。

        这是本任务的**核心验收（用户点名）**——分组名收敛了，用户升级前设过的开关必须
        照旧读得到（通过 `robot_actions.chat / group / qzone` 的嵌套路径），并且面板上
        报告的落点是**新子组**（写方向也只写新路径）。
        """
        # ① 归一化路径：`normalize_config` 的 N:1 归并（服务层/桥接装配置时走它）
        bridge = _make_bridge({
            'actions_interaction': {'enabled': True, 'send_poke': False, 'send_like': True},
            'actions_voice': {'send_voice': False, 'default_voice': 'zh-CN-YunxiNeural'},
        })
        bridge.db = self.bridge.db
        payload = _run(ConsoleApi(bridge).actions_catalog())
        switches = {row['id']: row['config_enabled'] for row in payload['actions']}
        self.assertIs(switches['send_poke'], False, '关掉的开关升级后还是关着')
        self.assertIs(switches['send_like'], True)
        self.assertIs(switches['send_voice'], False)
        self.assertIsNone(switches['set_group_kick'], '没配过的组照旧 = 未配置 = 不限制')
        for row in payload['actions']:
            with self.subTest(action=row['id']):
                self.assertTrue(row['group'].startswith('robot_actions.'),
                                '落点必须是父组下的三个子组之一')

        # ② 直接改内存里的旧组（`bridge.section()` 自己会归并，不依赖归一化）
        self.bridge.config.pop('robot_actions', None)
        self.bridge.config['actions_interaction'] = {'send_poke': False}
        row = next(item for item in _run(self.api.actions_catalog())['actions']
                   if item['id'] == 'send_poke')
        self.assertIs(row['config_enabled'], False)
        self.assertEqual(row['group'], 'robot_actions.chat')

    def test_retired_risk_group_is_a_dead_compat_slot(self):
        """`actions_risks` 从 v1.7.4 起**不再参与归并**：它里面的开关不影响任何动作。

        用户判断那些配置目前没人用（危险开关在 v1.7.3 就搬进各自类别组了）。兼容位照旧留在
        schema 里（回退到旧版本仍读得到它），但读取侧不认——面板照旧回"未配置 = 不限制"，
        而不是把一个谁都没在用的旧值当成用户的选择。
        """
        bridge = _make_bridge({'actions_risks': {
            'enabled': True, 'set_group_kick': True, 'delete_qzone_post': True,
            'delete_friend': True,
        }})
        bridge.db = self.bridge.db
        payload = _run(ConsoleApi(bridge).actions_catalog())
        rows = {row['id']: row for row in payload['actions']}
        for action_id, group in (('set_group_kick', 'robot_actions.group'),
                                 ('delete_qzone_post', 'robot_actions.qzone'),
                                 ('delete_friend', 'robot_actions.chat')):
            with self.subTest(action=action_id):
                self.assertIsNone(rows[action_id]['config_enabled'],
                                  '退休的风险组不再是归并源')
                self.assertEqual(rows[action_id]['group'], group)
        self.assertIsNone(rows['send_poke']['config_enabled'])

    def test_writing_a_nested_switch_path_lands_in_the_nested_group(self):
        """控制台写 `robot_actions.group.set_group_kick` 必须落到**嵌套子组**里。

        v1.7.4 的关键写方向：路径是点分的（`_resolve_schema_field` 只沿 `type: object`
        的 items 往下走），落盘不能变成 `"robot_actions.group"` 这种平铺假键——宿主下次
        加载会把它当未知键删掉（坑 22），用户的改动等于没写。
        """
        self.bridge.raw_config = lambda: {
            'actions_group': {'enabled': True, 'set_group_kick': True},
            'robot_actions': {'chat': {'send_poke': False}},
        }
        written: dict[str, Any] = {}

        async def fake_save(target):
            written.clear()
            written.update(target)
            return 'test'

        self.bridge.save_raw_config = fake_save
        _run(self.api.set_config_value('robot_actions.group.set_group_kick', False))
        self.assertIs(written['robot_actions']['group']['set_group_kick'], False)
        self.assertNotIn('robot_actions.group', written, '不许写成点号平铺的假键')
        self.assertIs(written['robot_actions']['chat']['send_poke'], False,
                      '兄弟子组不许被覆盖掉')

    def test_config_page_shows_the_merged_value_of_the_nested_groups(self):
        """配置页显示的必须是运行期**真正生效**的值（含旧分组归并），见坑 34。

        嵌套目标（`robot_actions.chat` / `runtime.input_status`）尤其要：内嵌表单拿到的是
        整个子组对象，显示成默认值的话，用户在控制台里改一项就把旧分组里没读出来的选择
        整块覆盖掉。
        """
        # 磁盘上（这里用 `_live_config` 代表）只有旧分组，没有 `robot_actions`
        self.bridge._live_config['actions_interaction'] = {'send_poke': False}
        self.bridge._live_config['input_status'] = {'enabled': False}
        payload = _run(self.api.config_schema())
        groups = {group['key']: group for group in payload['groups']}
        chat = next(item for item in groups['robot_actions']['fields'] if item['key'] == 'chat')
        self.assertEqual(chat['type'], 'object')
        self.assertIs(chat['value']['send_poke'], False)
        self.assertTrue(chat['present'], '旧分组里的值也算"设过"')
        status = next(item for item in groups['runtime']['fields']
                      if item['key'] == 'input_status')
        self.assertEqual(status['type'], 'object')
        self.assertIs(status['value']['enabled'], False)
        self.assertTrue(groups['actions_interaction']['invisible'],
                        '旧组下发的数据仍带 invisible 标记（宿主配置页据此隐藏）')
        self.assertTrue(groups['input_status']['invisible'])

    def test_permission_write_round_trips_to_the_temp_data_dir(self):
        path = self._temp_permissions()
        result = _run(self.api.set_action_permission('send_poke', 'admin'))
        self.assertEqual(result['action'], 'send_poke')
        self.assertEqual(result['tier'], 'admin')
        self.assertEqual(result['permissions'], {'send_poke': 'admin'})
        # 面板显示的是"插件数据目录 / action_permissions.json"（生产路径与桥接的写入
        # 目标同源；用例里把桥接的写入位置改到了临时目录，所以只对文件名做断言）。
        self.assertTrue(result['permissions_path'].endswith('action_permissions.json'))
        # 落成 JSON 文件（独立表，不进 `_conf_schema.json`）
        self.assertTrue(os.path.isfile(path))
        with open(path, encoding='utf-8') as handle:
            self.assertEqual(json.load(handle), {'send_poke': 'admin'})
        # 读回：目录里的档位跟着变
        payload = _run(self.api.actions_catalog())
        row = next(item for item in payload['actions'] if item['id'] == 'send_poke')
        self.assertEqual(row['permission'], 'admin')
        self.assertEqual(payload['stats']['permissions']['admin'], 1)

    def test_permission_write_rejects_unknown_action_and_tier(self):
        path = self._temp_permissions()
        for action, tier in (('send_poke', 'owner'), ('send_poke', ''), ('nope', 'global'), ('', 'global')):
            with self.subTest(action=action, tier=tier):
                with self.assertRaises(ConsoleError):
                    _run(self.api.set_action_permission(action, tier))
        self.assertFalse(os.path.exists(path), '被拒绝的写入不能碰权限表')

    def test_permission_reset_clears_the_table(self):
        path = self._temp_permissions()
        _run(self.api.set_action_permission('send_poke', 'disabled'))
        result = _run(self.api.reset_action_permissions())
        self.assertEqual(result['permissions'], {})
        with open(path, encoding='utf-8') as handle:
            self.assertEqual(json.load(handle), {})
        payload = _run(self.api.actions_catalog())
        row = next(item for item in payload['actions'] if item['id'] == 'send_poke')
        self.assertEqual(row['permission'], 'global', '清空后回目录默认档')

    def test_read_only_console_pages_have_no_permission_entry(self):
        """「Token 统计」这类只读页面不属于权限表：目录里没有它，写入也会被拒。

        v1.7.3 起「动作」页不再解释"只读页面"这件事，`permissionless_panels` 也随之
        下线（前端那段说明整段删掉了）——边界本身仍然钉在这里。
        """
        payload = _run(self.api.actions_catalog())
        self.assertNotIn('permissionless_panels', payload)
        for name in ('tokens', 'token-stats', 'token_stats'):
            with self.subTest(name=name):
                self.assertNotIn(name, platform_actions.ACTIONS)
                with self.assertRaises(ConsoleError):
                    _run(self.api.set_action_permission(name, 'global'))
        blob = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn('console/token-stats', blob)

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
        # 构造 bridge 时 core 就会报一条「服务初始化完成」；v1.2.17 起它也会进日志缓冲
        # （core 的 report 走 `emit_log` → 适配器单次投递，见坑 49）。这条断言问的是
        # 排序与过滤，所以先把缓冲清干净，别让启动那一条混进来。
        self.bridge.log_buffer.clear()
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

    def test_save_connection_keeps_the_protocol_fields(self):
        """协议两项必须在白名单里——不然用户填了 Anthropic 协议，**保存时被静默丢掉**。

        这是 v1.7.0 加 Anthropic 时真实存在的缺口：schema 里早就有 `protocol` /
        `anthropic_cache`，`CONNECTION_FIELDS` 却没放行，控制台连接编辑器读得到、
        存不回。断言直接对着白名单 + 一次真实保存往返。
        """
        import asyncio

        self.assertIn('protocol', console_module.ConsoleApi.CONNECTION_FIELDS)
        self.assertIn('anthropic_cache', console_module.ConsoleApi.CONNECTION_FIELDS)
        saved = self._wire_writes()
        asyncio.run(self.api.save_connection({
            'label': 'Claude', 'endpoint': 'https://api.anthropic.com/v1/messages',
            'api_key': 'sk-ant', 'model': 'claude-sonnet-4-5',
            'protocol': 'anthropic-messages', 'anthropic_cache': True,
        }))
        row = saved['model_center']['providers'][-1]
        self.assertEqual(row['protocol'], 'anthropic-messages')
        self.assertIs(row['anthropic_cache'], True)

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

    # ---- 上轮上下文构成（v1.4.0） ----

    def test_context_metrics_are_labelled_and_sorted(self):
        sid = 'qq:20000:10001'
        self._story_row(sid)
        extensions = {}
        extensions['last_context_metrics'] = {
            'at': '2026-09-27T02:00:00Z', 'phase': 'user-message', 'participant_id': 'p1',
            'assembly_ms': 37, 'items': 9, 'characters': 4200, 'payload_characters': 5100,
            'estimated_tokens': 1800,
            'sections': {
                'facts': {'items': 4, 'characters': 1200},
                'recentEntries': {'items': 5, 'characters': 3000},
                'unknownSection': {'items': 0, 'characters': 0},
            },
        }
        self.bridge.db.update('interlude_story', {'id': sid}, {
            'state': json.dumps({'extensions': extensions}),
        })
        payload = _run(self.api.overview(sid))
        metrics = payload['context_metrics']
        self.assertEqual(metrics['estimated_tokens'], 1800)
        self.assertEqual(metrics['assembly_ms'], 37)
        self.assertEqual(metrics['phase'], 'user-message')
        self.assertEqual([row['key'] for row in metrics['sections']], ['recentEntries', 'facts', 'unknownSection'],
                         '按占用字符数排序，未知段名也给出行')
        self.assertEqual(metrics['sections'][0]['label'], '近期条目')
        self.assertEqual(metrics['sections'][2]['label'], 'unknownSection')

    def test_context_metrics_are_empty_without_a_story(self):
        self.assertEqual(_run(self.api.overview())['context_metrics'], {})

    def test_context_metrics_survive_a_broken_state(self):
        sid = 'qq:20000:10001'
        self._story_row(sid)
        self.bridge.db.update('interlude_story', {'id': sid}, {'state': 'not-json'})
        self.assertEqual(_run(self.api.overview(sid))['context_metrics'], {})

    # ---- 设定改写候选：审批与回滚（v1.4.0） ----

    def _patch_row(self, story_id: str, status: str = 'proposed') -> int:
        row = self.bridge.db.insert('interlude_state_patch', {
            'storyId': story_id, 'participantId': '', 'target': 'character',
            'path': 'development.trait', 'proposedValue': '她把伞留在了门口',
            'evidence': 'seen once', 'confidence': 0.83, 'impact': 'minor',
            'status': status, 'sourceEntryIds': json.dumps([1]),
            'createdAt': '2026-09-25T14:33:00Z', 'appliedAt': None,
        })
        return int(row) if isinstance(row, int) else int(row.get('id'))

    class _DecisionService:
        def __init__(self, calls: list, error: Exception | None = None):
            self.calls = calls
            self.error = error

        async def decide_state_patch(self, story_id, patch_id, action, note=''):
            self.calls.append(('decide', story_id, patch_id, action, note))
            if self.error:
                raise self.error
            return {'id': patch_id, 'status': 'applied' if action == 'approve' else 'rejected',
                    'target': 'character', 'path': 'development.trait',
                    'decided_at': '2026-09-27T00:00:00Z', 'note': note}

        async def rollback_state_patch(self, story_id, patch_id, note=''):
            self.calls.append(('rollback', story_id, patch_id, note))
            if self.error:
                raise self.error
            return {'id': patch_id, 'status': 'rolled-back', 'target': 'character',
                    'path': 'development.trait', 'decided_at': '2026-09-27T00:00:00Z', 'note': note}

    def test_decide_patch_requires_a_service(self):
        self._story_row('qq:20000:10001')
        with self.assertRaises(ConsoleError):
            _run(self.api.decide_patch('qq:20000:10001', 1, 'approve'))

    def test_decide_patch_reports_an_unknown_action(self):
        sid = 'qq:20000:10001'
        self._story_row(sid)
        self.bridge.service = self._DecisionService([])
        with self.assertRaises(ConsoleError) as caught:
            _run(self.api.decide_patch(sid, 1, 'maybe'))
        self.assertIn('未知的操作', str(caught.exception))

    def test_decide_patch_passes_the_decision_through_and_returns_the_panel(self):
        sid = 'qq:20000:10001'
        self._story_row(sid)
        patch_id = self._patch_row(sid)
        calls: list = []
        self.bridge.service = self._DecisionService(calls)
        payload = _run(self.api.decide_patch(sid, patch_id, 'approve', '看着像对的'))
        self.assertEqual(calls[0][0], 'decide')
        self.assertEqual(calls[0][3], 'approve')
        self.assertEqual(payload['patch']['status'], 'applied')
        self.assertEqual(payload['changed'], 'patch-approve #%d' % patch_id)
        self.assertIn('patches', payload, '写操作回整页数据，前端不用再拉一次')

    def test_decide_patch_explains_a_compacted_candidate(self):
        sid = 'qq:20000:10001'
        self._story_row(sid)
        self.bridge.service = self._DecisionService([], ValueError('already-compacted'))
        with self.assertRaises(ConsoleError) as caught:
            _run(self.api.decide_patch(sid, 1, 'reject'))
        self.assertIn('周期摘要', str(caught.exception))

    def test_rollback_patch_requires_a_service_and_a_story(self):
        self.bridge.service = self._DecisionService([])
        with self.assertRaises(ConsoleError):
            _run(self.api.rollback_patch('qq:20000:10001', 1))

    def test_rollback_patch_explains_a_non_applied_candidate(self):
        sid = 'qq:20000:10001'
        self._story_row(sid)
        self.bridge.service = self._DecisionService([], ValueError('not-applied'))
        with self.assertRaises(ConsoleError) as caught:
            _run(self.api.rollback_patch(sid, 1))
        self.assertIn('已经生效', str(caught.exception))

    def test_rollback_patch_returns_the_panel(self):
        sid = 'qq:20000:10001'
        self._story_row(sid)
        patch_id = self._patch_row(sid, status='applied')
        calls: list = []
        self.bridge.service = self._DecisionService(calls)
        payload = _run(self.api.rollback_patch(sid, patch_id, '写错了'))
        self.assertEqual(calls[0][0], 'rollback')
        self.assertEqual(payload['patch']['status'], 'rolled-back')
        self.assertEqual(payload['changed'], 'patch-rollback #%d' % patch_id)

    def test_patch_brief_exposes_the_decision_trail(self):
        sid = 'qq:20000:10001'
        self._story_row(sid)
        patch_id = self._patch_row(sid, status='rolled-back')
        self.bridge.db.update('interlude_state_patch', {'id': patch_id}, {
            'decidedAt': '2026-09-27T01:02:03Z', 'decisionNote': '主人说这条不算',
        })
        payload = _run(self.api.memory(sid))
        brief = next(item for item in payload['patches'] if item['id'] == patch_id)
        self.assertTrue(brief['decided_at'].startswith('2026-09-27'), brief['decided_at'])
        self.assertEqual(brief['decision_note'], '主人说这条不算')

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


class ConfigNoteTests(unittest.TestCase):
    """配置页字段说明的回归：开关自己那行标题已经说清了，别再叠一句重复的。"""

    def test_the_three_switches_have_no_extra_note(self) -> None:
        from plugin.adapters import console_api
        for key in ('bot_accounts_only', 'user_accounts_only', 'group_chats_only'):
            self.assertNotIn('qq_access.%s' % key, console_api.FIELD_NOTES, key)

    def test_the_lists_keep_their_notes(self) -> None:
        from plugin.adapters import console_api
        for key in ('user_accounts', 'bot_accounts', 'group_chats'):
            self.assertIn('qq_access.%s' % key, console_api.FIELD_NOTES, key)


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
        # 与 schema 文件对账（而不是写死数字）：平台动作的开关组（`actions_*`）会随动作目录
        # 增长，写死 25 每加一组都要来改这里；这里只钉"schema 里每个顶层分组都下发了"。
        self.assertEqual(len(payload['groups']), len(console_module.load_config_schema()))
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
        _run(self.api.set_config_value('qq_access.group_chats_only', 'true'))
        self.assertIs(self._read()['qq_access']['group_chats_only'], True)
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

    # ---- 聊天记录（按参与者 / 按群） ----

    def _chat_db(self) -> Database:
        """造一部带私聊、群聊与两条投递失败的账。"""
        database = Database(':memory:')
        self.addCleanup(database.close)
        database.register_tables()
        database.insert('interlude_story', {
            'id': 's1', 'platform': 'onebot', 'selfId': '20000', 'userId': '10001',
            'channelId': 'private:10001', 'status': 'active',
            'setting': {'character': {'name': '凌梦'}, 'user': {'displayName': '主人'}},
            'state': {}, 'cursorAt': '2026-09-26T20:00:00.000Z',
            'createdAt': '2026-09-26T00:00:00.000Z', 'updatedAt': '2026-09-27T00:00:00.000Z',
        })
        database.upsert('interlude_participant', {
            'id': 'p-kela', 'storyId': 's1', 'platform': 'onebot', 'selfId': '20000',
            'userId': '1000008890', 'channelId': 'private:1000008890', 'personId': 'kela',
            'displayName': '主人', 'status': 'active',
            'state': {'unreadMessageCount': 2, 'pendingReplyCount': 2},
            'createdAt': '2026-09-26T00:00:00.000Z', 'updatedAt': '2026-09-27T00:00:00.000Z',
        })
        database.upsert('interlude_participant', {
            'id': 'p-xishi', 'storyId': 's1', 'platform': 'onebot', 'selfId': '20000',
            'userId': '100002551', 'channelId': 'private:100002551', 'personId': 'xishi',
            'displayName': '汐雨.', 'status': 'active',
            'state': {'unreadMessageCount': 0, 'pendingReplyCount': 0},
            'createdAt': '2026-09-26T00:00:00.000Z', 'updatedAt': '2026-09-26T12:00:00.000Z',
        })
        entries = [
            ('p-kela', 'user-message', '在么', '2026-09-26T14:37:49.000Z', {}),
            ('p-kela', 'user-message', '睡了睡了，晚安', '2026-09-26T16:38:51.000Z', {}),
            ('p-kela', 'character-message', '早～', '2026-09-26T22:19:16.000Z', {}),
            ('p-xishi', 'user-message', '我是汐雨，交个朋友？', '2026-09-25T16:19:00.000Z', {}),
            ('p-xishi', 'character-message', '先说清楚你怎么加的我', '2026-09-26T00:35:00.000Z', {}),
            ('p-xishi', 'outgoing-delivery-failed', '醒了', '2026-09-26T00:36:00.000Z', {'status': 'failed'}),
            ('', 'group-message', '在吗', '2026-09-26T10:00:00.000Z',
             {'groupId': '100002770', 'senderId': '10002', 'senderName': '群友甲'}),
            ('', 'character-group-message', '在的', '2026-09-26T10:00:05.000Z',
             {'groupId': '100002770', 'channelId': 'group:100002770'}),
            ('', 'group-message', '再来一条', '2026-09-26T10:05:00.000Z',
             {'groupId': '100002770', 'senderId': '10002', 'senderName': '群友甲'}),
            ('', 'script', '她合上本子。', '2026-09-26T11:00:00.000Z', {}),
        ]
        for participant_id, kind, content, occurred_at, metadata in entries:
            database.insert('interlude_script_entry', {
                'storyId': 's1', 'participantId': participant_id, 'kind': kind,
                'actor': 'user' if kind.endswith('user-message') or kind == 'group-message' else 'character',
                'content': content, 'occurredAt': occurred_at, 'metadata': metadata,
                'createdAt': occurred_at,
            })
        return database

    def test_chats_lists_every_conversation_without_narration(self):
        self.bridge.db = self._chat_db()
        payload = _run(self.api.chats('s1'))

        private = {item['participant_id']: item for item in payload['private']}
        self.assertEqual(sorted(private), ['p-kela', 'p-xishi'])
        self.assertEqual(private['p-kela']['name'], '主人')
        self.assertEqual(private['p-kela']['unread'], 2)
        self.assertEqual(private['p-kela']['incoming'], 2)
        self.assertEqual(private['p-kela']['outgoing'], 1)
        self.assertEqual(private['p-kela']['last_text'], '早～')
        self.assertEqual(private['p-xishi']['failed'], 1)
        self.assertEqual(private['p-xishi']['awaiting'], 0)
        # 旁白条目不属于任何一条对话
        self.assertNotIn('script', {item.get('kind') for item in payload['private']})

    def test_configured_group_without_history_still_shows_its_label(self):
        # 配置里列着、还没说过话的群也要出现在清单里，并且用配置里的 label。
        self.bridge.config['qq_access'] = {
            'group_chats': [{'group_id': '100002770', 'label': '测试群'}, {'group_id': '999', 'label': '安静群'}],
        }
        self.bridge.db = self._chat_db()
        payload = _run(self.api.chats('s1'))

        groups = {item['group_id']: item for item in payload['groups']}
        self.assertEqual(groups['100002770']['name'], '测试群')
        self.assertEqual(groups['999']['configured'], True)
        self.assertEqual(groups['999']['messages'], 0)
        self.assertEqual(_run(self.api.chat_history('s1', 'group:999'))['title'], '安静群')

    def test_group_conversation_is_its_own_row(self):
        self.bridge.db = self._chat_db()
        payload = _run(self.api.chats('s1'))

        self.assertEqual([item['group_id'] for item in payload['groups']], ['100002770'])
        group = payload['groups'][0]
        self.assertEqual(group['incoming'], 2)
        self.assertEqual(group['outgoing'], 1)
        self.assertEqual(group['awaiting'], 1)
        self.assertEqual(group['last_text'], '再来一条')

    def test_history_is_chronological_and_paginates(self):
        self.bridge.db = self._chat_db()
        payload = _run(self.api.chat_history('s1', 'private:p-kela', 2))

        self.assertEqual(payload['title'], '主人')
        self.assertEqual([item['text'] for item in payload['messages']], ['睡了睡了，晚安', '早～'])
        self.assertTrue(payload['has_more'])

        older = _run(self.api.chat_history('s1', 'private:p-kela', 5, payload['messages'][0]['at']))
        self.assertEqual([item['text'] for item in older['messages']], ['在么'])
        self.assertFalse(older['has_more'])

    def test_history_marks_who_spoke(self):
        self.bridge.db = self._chat_db()
        payload = _run(self.api.chat_history('s1', 'group:100002770', 10))

        self.assertEqual(payload['title'], '100002770')
        sides = [(item['side'], item['sender']) for item in payload['messages']]
        self.assertEqual(sides, [
            ('in', '群友甲'), ('out', '凌梦'), ('in', '群友甲'),
        ])

    def test_missing_story_or_conversation_is_an_empty_shell(self):
        empty = Database(':memory:')
        self.addCleanup(empty.close)
        empty.register_tables()
        self.bridge.db = empty
        payload = _run(self.api.chats('nope'))
        self.assertIsNone(payload['story'])
        self.assertEqual((payload['private'], payload['groups']), ([], []))

        self.bridge.db = self._chat_db()
        self.assertEqual(_run(self.api.chat_history('s1', ''))['messages'], [])
        self.assertEqual(
            _run(self.api.chat_history('s1', 'private:不存在的'))['messages'], [],
        )

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

    # ---- v1.7.2 升级现场：启动时把旧动作分组折进新分组 ----

    def test_startup_migration_folds_legacy_action_switches_into_the_nested_group(self):
        """宿主已把 `actions_chat` 按 schema 补成默认值，用户的开关还在旧分组里。

        启动迁移必须把用户的选择折进**嵌套**新路径、清空旧组并写盘；折完之后用户在新路径里
        **把开关改回默认值**（关掉→打开）也必须真的生效——这正是"只靠读取侧归并"
        做不到的那一步（旧组里的旧值会永远压着新组）。
        """
        with open(self.path, 'w', encoding='utf-8') as handle:
            handle.write('\ufeff' + json.dumps({
                'actions_chat': {
                    'enabled': True, 'send_poke': True, 'send_like': True,
                    'send_voice': True, 'default_voice': '',
                },
                'actions_interaction': {'enabled': True, 'send_poke': False, 'send_like': True},
                'actions_voice': {'enabled': True, 'send_voice': False,
                                  'default_voice': 'zh-CN-YunxiNeural'},
            }, ensure_ascii=False))
        self.assertEqual(_run(self.bridge.migrate_legacy_action_sections()), 1)
        data = self._read()
        chat = data['robot_actions']['chat']
        self.assertIs(chat['send_poke'], False)
        self.assertIs(chat['send_voice'], False)
        # v1.7.5：音色跟着**键级搬迁**走到「模型中心 → 语音 / 音频理解设置」；
        # 旧位置（可见组）清回 schema 默认值，但键还在（宿主按 schema 重建配置）。
        self.assertEqual(data['model_center']['audio']['default_voice'], 'zh-CN-YunxiNeural')
        self.assertEqual(chat['default_voice'], '')
        self.assertEqual(data['actions_chat'], {}, '退休的顶层组折完清空')
        self.assertEqual(data['actions_interaction'], {})
        self.assertEqual(data['actions_voice'], {})
        # 运行期立刻读到折完的那份（`save_raw_config` 会用刚写下去的那份生效）
        self.assertIs(self.bridge.section('robot_actions.chat')['send_poke'], False)
        # 幂等：再跑一次没有可折的东西，不写盘、内容不变
        self.assertEqual(_run(self.bridge.migrate_legacy_action_sections()), 0)
        self.assertEqual(self._read(), data)
        # 折完之后"改回默认值"必须生效（否则界面上就是"改了没反应"）
        _run(self.api.set_config_value('robot_actions.chat.send_poke', True))
        self.assertIs(self.bridge.section('robot_actions.chat')['send_poke'], True)
        self.assertIs(self._read()['robot_actions']['chat']['send_poke'], True)

    def test_startup_migration_leaves_the_retired_risk_group_untouched(self):
        """v1.7.4 升级现场：退休的 `actions_risks` 只留兼容位，迁移既不读它也不清它。

        它不再是归并源（用户判断那些配置没人用），所以折叠不碰它——回退到 v1.7.2 时那个
        版本读的就是它，留着才不丢；而可见子组的内容只由 `actions_group` 这类真正的源决定。
        """
        with open(self.path, 'w', encoding='utf-8') as handle:
            handle.write('\ufeff' + json.dumps({
                # `get_group_members_info` 默认 true，"用户关掉"才算写过。
                'actions_group': {'enabled': True, 'get_group_members_info': False},
                'actions_risks': {'enabled': True, 'set_group_kick': True,
                                  'delete_qzone_post': True, 'delete_friend': True},
            }, ensure_ascii=False))
        self.assertEqual(_run(self.bridge.migrate_legacy_action_sections()), 1)
        data = self._read()
        self.assertIs(data['robot_actions']['group']['get_group_members_info'], False,
                      '用户关掉的开关折进嵌套子组')
        self.assertEqual(data['actions_group'], {}, '独占旧组折完清空')
        self.assertEqual(data['actions_risks']['set_group_kick'], True,
                         '不参与归并的兼容位原样保留')
        # 运行期读到的是同一份；退休组里的键**不会**因此漏进可见子组。
        group = self.bridge.section('robot_actions.group')
        self.assertIs(group['get_group_members_info'], False)
        self.assertNotIn('set_group_kick', group)
        # 幂等：没有可折的东西了，不写盘
        self.assertEqual(_run(self.bridge.migrate_legacy_action_sections()), 0)
        self.assertEqual(self._read(), data)
        # 用户在新子组里改回默认值：只写嵌套路径，兼容位不动
        _run(self.api.set_config_value('robot_actions.group.get_group_members_info', True))
        stored = self._read()
        self.assertIs(stored['robot_actions']['group']['get_group_members_info'], True)
        self.assertIs(stored['actions_risks']['set_group_kick'], True)
        self.assertIs(self.bridge.section('robot_actions.group')['get_group_members_info'], True)


# =========================================================================== #
# 共同作品（「作品」面板）
# =========================================================================== #

class _StubWorksService:
    """桩服务层：只实现面板用到的成员，形状与 `core/service/chunk14.py` 一致。

    用它钉住控制台**自己**的形状与空壳 / 400 路径（另一 agent 的 chunk14 还在改的时候
    这些断言也必须能跑）；"接线真的通了"由 `WorksIntegrationTests` 用真服务层钉。
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        snapshot=None,
        projection=None,
        parts=None,
        resolve_result=None,
        edit_result=None,
        generate_result=None,
        create_result=None,
        cancel_result=None,
        calls=None,
    ) -> None:
        self.enabled = enabled
        self.snapshot = snapshot
        self.projection = projection
        self.parts = parts
        self.resolve_result = resolve_result if resolve_result is not None else {'ok': True, 'error': ''}
        self.edit_result = edit_result if edit_result is not None else {'ok': True, 'error': ''}
        self.generate_result = generate_result if generate_result is not None else {'ok': True, 'error': ''}
        self.create_result = create_result if create_result is not None else {'ok': True, 'error': ''}
        self.cancel_result = cancel_result if cancel_result is not None else {'ok': True, 'error': ''}
        self.calls = calls if calls is not None else []

    def works_config(self):
        return {'enabled': self.enabled, 'generation_mode': 'main', 'model_id': ''}

    def explain_works_state(self):
        return ('共同作品已启用：主连接模式（提案由主叙事回合给出）' if self.enabled
                else '共同作品未启用（配置 works.enabled 为 false 或缺失）')

    async def works_snapshot(self, story, participant):
        self.calls.append(('snapshot', story, participant))
        # 可调用时按参与者给不同形状（清单里每一行都该是自己的那件作品）。
        return self.snapshot(participant) if callable(self.snapshot) else self.snapshot

    async def shared_work_state(self, story, participant):
        self.calls.append(('state', story, participant))
        return self.projection

    async def works_dump(self, story, participant):
        self.calls.append(('dump', story, participant))
        return self.parts

    async def create_work(self, story, participant, title, content):
        self.calls.append(('create', story, participant, title, content))
        return self.create_result

    async def cancel_work_generation(self, story, participant, job_id):
        self.calls.append(('cancel', story, participant, job_id))
        return self.cancel_result

    async def accept_work_proposal(self, work_id, proposal_id):
        self.calls.append(('accept', work_id, proposal_id))
        return self.resolve_result

    async def reject_work_proposal(self, work_id, proposal_id):
        self.calls.append(('reject', work_id, proposal_id))
        return self.resolve_result

    async def edit_work(self, story, participant, edit, source_entry_id=None, operation_key=''):
        self.calls.append(('edit', story, participant, edit))
        return self.edit_result

    async def start_work_generation(self, story, participant, request, source_entry_id=None, operation_key=''):
        self.calls.append(('generate', story, participant, request))
        return self.generate_result


class _LegacyWorksService(_StubWorksService):
    """契约摘要里那一版签字：`edit_work(story, participant, content, reason)`。

    控制台按签名挑调用形状（`_call_work_edit` / `_call_work_generate`），两种都要能跑。
    """

    async def edit_work(self, story, participant, content, reason=''):
        self.calls.append(('edit-legacy', story, participant, content, reason))
        return self.edit_result

    async def start_work_generation(self, story, participant, brief):
        self.calls.append(('generate-legacy', story, participant, brief))
        return self.generate_result


def _works_bridge(config):
    """真库 + 真服务层的桥：控制台与 `chunk14` 读同一份数据（接线真的通了才算过）。"""
    database = Database(':memory:')
    with mock.patch.object(bridge_module, 'Database', lambda path: database), \
            mock.patch.object(bridge_module, 'plugin_data_dir', lambda *a, **k: TEST_DATA_DIR):
        bridge = bridge_module.AstrbotBridge(
            context=FakeContext(), config=config, logger=sys.modules['astrbot'].logger,
        )
    database.register_tables()
    return bridge, database


def _work_state(head='rev-2', *, title='海边的信', revisions=None, proposals=None, jobs=None):
    """一份合法的 `interlude_work.state`（上游形状：camelCase）。"""
    revisions = revisions if revisions is not None else [
        {'id': 'rev-1', 'parentId': None, 'author': 'user', 'content': '第一版正文',
         'createdAt': '2026-09-01T10:00:00.000Z'},
        {'id': 'rev-2', 'parentId': 'rev-1', 'author': 'protagonist', 'proposalId': 'prop-1',
         'content': '她改过的正文', 'createdAt': '2026-09-02T10:00:00.000Z'},
    ]
    state = {
        'schemaVersion': 1,
        'title': title,
        'head': head,
        'revisions': revisions,
        'proposals': proposals if proposals is not None else [],
    }
    if jobs is not None:
        state['jobs'] = jobs
    return state


class WorksPanelTests(unittest.TestCase):
    """「作品」面板的后端：清单 / 详情 / 接受 / 驳回 / 手改 / 起草 / 导出。"""

    SID = 'character:qq:20000'
    PID = 'p-kela'
    WID = 'character:qq:20000:p-kela'

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.bridge = _make_bridge({'works': {'enabled': True}})
        self.bridge.service = None
        self.database = Database(':memory:')
        self.addCleanup(self.database.close)
        self.database.register_tables()
        self.bridge.db = self.database
        self.api = ConsoleApi(self.bridge)
        self._story_row()
        self._participant_row()

    def _story_row(self, story_id=None, status='active'):
        self.database.upsert('interlude_story', {
            'id': story_id or self.SID, 'status': status, 'platform': 'qq',
            'setting': {'character': {'name': '凌梦'}, 'user': {'name': '主人'}},
            'state': {}, 'createdAt': '2026-09-01T00:00:00.000Z',
            'updatedAt': '2026-09-02T00:00:00.000Z',
        })

    def _participant_row(self, participant_id=None, name='主人', story_id=None):
        self.database.upsert('interlude_participant', {
            'id': participant_id or self.PID, 'storyId': story_id or self.SID,
            'platform': 'onebot', 'selfId': '20000', 'userId': '10001',
            'channelId': 'private:10001', 'personId': 'kela', 'displayName': name,
            'status': 'active', 'state': {},
            'createdAt': '2026-09-01T00:00:00.000Z', 'updatedAt': '2026-09-02T00:00:00.000Z',
        })

    @staticmethod
    def _calls(calls, kind):
        """按类型挑服务层调用（读详情会先调一次 `works_snapshot`，别按位置取）。"""
        return [call for call in calls if call[0] == kind]

    def _work_row(self, state=None, work_id=None, story_id=None, participant_id=None, generation=3):
        self.database.upsert('interlude_work', {
            'id': work_id or self.WID,
            'storyId': story_id or self.SID,
            'participantId': participant_id or self.PID,
            'generation': generation,
            'state': state if state is not None else _work_state(),
        })
        return work_id or self.WID

    def _snapshot(self, **overrides):
        """`works_snapshot` 的返回形状（服务层原样投影，camelCase）。"""
        snapshot = {
            'workId': self.WID,
            'title': '海边的信',
            'head': 'rev-2',
            'generation': 3,
            'revisions': [
                {'id': 'rev-1', 'parentId': None, 'author': 'user', 'proposalId': None,
                 'createdAt': '2026-09-01T10:00:00.000Z', 'content': '第一版正文'},
                {'id': 'rev-2', 'parentId': 'rev-1', 'author': 'protagonist', 'proposalId': 'prop-1',
                 'createdAt': '2026-09-02T10:00:00.000Z', 'content': '她改过的正文'},
            ],
            'proposals': [
                {'id': 'prop-2', 'baseRevisionId': 'rev-2', 'author': 'protagonist',
                 'content': '再补一段', 'reason': '把结尾收一下', 'status': 'pending',
                 'operationKey': 'live', 'createdAt': '2026-09-03T10:00:00.000Z'},
                {'id': 'prop-1', 'baseRevisionId': 'rev-1', 'author': 'protagonist',
                 'content': '她改过的正文', 'reason': '第一处修改', 'status': 'accepted',
                 'operationKey': 'entry:7', 'createdAt': '2026-09-02T10:00:00.000Z'},
            ],
            'jobs': [
                {'id': 'job-1', 'status': 'completed', 'modelId': 'demo', 'brief': '写一版',
                 'createdAt': '2026-09-03T09:00:00.000Z', 'baseRevisionId': 'rev-2'},
            ],
            'lastFailure': None,
            'revisionLimit': 64,
        }
        snapshot.update(overrides)
        return snapshot

    # ---- 服务层未就绪：空壳，不是 500 ----

    def test_reads_without_a_service_are_shells_not_errors(self):
        overview = _run(self.api.works_overview(self.SID))
        self.assertFalse(overview['available'])
        self.assertIn('服务层未就绪', overview['hint'])
        self.assertEqual(overview['works'], [])
        detail = _run(self.api.work_detail(self.WID))
        self.assertFalse(detail['available'])
        self.assertEqual(detail['work_id'], self.WID)
        export = _run(self.api.export_work(self.WID))
        self.assertFalse(export['available'])
        self.assertEqual(export['parts'], [])

    def test_writes_without_a_service_report_a_shell_instead_of_pretending(self):
        self.assertEqual(_run(self.api.accept_work_proposal(self.WID, 'prop-2'))['available'], False)
        self.assertEqual(_run(self.api.reject_work_proposal(self.WID, 'prop-2'))['available'], False)
        self.assertEqual(_run(self.api.edit_work(self.WID, '新正文'))['available'], False)
        self.assertEqual(_run(self.api.start_work_generation(self.WID, '写一版'))['available'], False)

    def test_the_shell_carries_the_service_explanation_when_it_can(self):
        """拿不到 `works_snapshot` 但有 `explain_works_state` 时，说明文案要用它那句。"""

        class _ConfigOnly:
            def explain_works_state(self):
                return '共同作品未启用（配置 works.enabled 为 false 或缺失）'

        self.bridge.service = _ConfigOnly()
        payload = _run(self.api.works_overview(self.SID))
        self.assertIn('works_snapshot', payload['hint'], '缺哪个成员要说出来')
        self.assertIn('共同作品未启用', payload['hint'])

    # ---- 清单 ----

    def test_overview_lists_one_row_per_participant(self):
        self._work_row()
        self._participant_row(participant_id='p-xishi', name='汐雨.')
        self._work_row(work_id='%s:p-xishi' % self.SID, participant_id='p-xishi', generation=1,
                       state=_work_state(head='rev-1', title='另一件',
                                         revisions=[{'id': 'rev-1', 'parentId': None, 'author': 'user',
                                                     'content': 'x', 'createdAt': '2026-08-01T00:00:00.000Z'}],
                                         proposals=[]))
        older = self._snapshot(
            title='另一件', head='rev-1', generation=1, proposals=[], jobs=[],
            revisions=[{'id': 'rev-1', 'parentId': None, 'author': 'user', 'proposalId': None,
                        'createdAt': '2026-08-01T00:00:00.000Z', 'content': 'x'}],
        )
        calls: list = []
        self.bridge.service = _StubWorksService(
            snapshot=lambda participant: self._snapshot() if participant == self.PID else older,
            calls=calls,
        )

        payload = _run(self.api.works_overview(self.SID))
        self.assertTrue(payload['available'])
        self.assertTrue(payload['enabled'])
        self.assertEqual(payload['story']['character'], '凌梦')
        rows = {item['participant_id']: item for item in payload['works']}
        self.assertEqual(sorted(rows), ['p-kela', 'p-xishi'])
        self.assertEqual(rows['p-kela']['participant'], '主人')
        self.assertEqual(rows['p-kela']['title'], '海边的信')
        self.assertEqual(rows['p-kela']['revision'], 2, '当前版本号 = head 在时间线里的位置')
        self.assertEqual(rows['p-kela']['revision_count'], 2)
        self.assertEqual(rows['p-kela']['pending_count'], 1)
        self.assertEqual(rows['p-kela']['jobs_running'], 0)
        self.assertEqual(rows['p-kela']['updated_at'], '2026-09-03T10:00:00.000Z')
        self.assertTrue(rows['p-kela']['may_propose'])
        self.assertNotIn('content', rows['p-kela'], '清单行不带正文（正文在详情里）')
        self.assertEqual(rows['p-xishi']['title'], '另一件')
        self.assertEqual(rows['p-xishi']['revision'], 1)
        self.assertEqual(rows['p-xishi']['pending_count'], 0)
        self.assertEqual(rows['p-xishi']['updated_at'], '2026-08-01T00:00:00.000Z')
        # 参与者按更新时间倒序：p-kela 更新 → 排前面
        self.assertEqual([item['participant_id'] for item in payload['works']], ['p-kela', 'p-xishi'])
        json.dumps(payload, ensure_ascii=False)

    def test_overview_marks_an_unreadable_row_instead_of_hiding_it(self):
        self._work_row()
        self.bridge.service = _StubWorksService(snapshot=None)
        payload = _run(self.api.works_overview(self.SID))
        self.assertEqual(len(payload['works']), 1)
        self.assertTrue(payload['works'][0]['broken'])
        self.assertEqual(payload['works'][0]['revision_count'], 2, '坏行也得把能读的读出来')

    def test_overview_without_a_story_or_work_is_an_empty_shell(self):
        self.bridge.service = _StubWorksService(snapshot=self._snapshot())
        clean = Database(':memory:')
        self.addCleanup(clean.close)
        clean.register_tables()
        self.bridge.db = clean
        empty = _run(self.api.works_overview(self.SID))
        self.assertIsNone(empty['story'])
        self.assertEqual(empty['works'], [])
        self.assertTrue(empty['hint'])

        self.bridge.db = self.database
        payload = _run(self.api.works_overview(self.SID))
        self.assertTrue(payload['available'])
        self.assertEqual(payload['works'], [], '开了但还没作品：空清单，不是空壳')

    def test_works_disabled_says_so_instead_of_looking_empty(self):
        self.bridge.service = _StubWorksService(enabled=False, snapshot=self._snapshot())
        payload = _run(self.api.works_overview(self.SID))
        self.assertTrue(payload['available'], '功能没开不代表面板打不开')
        self.assertFalse(payload['enabled'], '前端据此提示"去配置页打开共同作品"')

    # ---- 详情 ----

    def test_detail_returns_the_head_text_and_the_version_timeline(self):
        self._work_row()
        self.bridge.service = _StubWorksService(snapshot=self._snapshot())
        payload = _run(self.api.work_detail(self.WID))

        self.assertTrue(payload['available'])
        self.assertEqual(payload['participant'], '主人')
        self.assertEqual(payload['title'], '海边的信')
        self.assertEqual(payload['content'], '她改过的正文', '正文原样回，不做任何安全改写')
        self.assertEqual(payload['content_chars'], 6)
        self.assertEqual(payload['revision'], 2)
        self.assertEqual(payload['revision_count'], 2)
        revisions = {item['id']: item for item in payload['revisions']}
        self.assertEqual([item['ordinal'] for item in payload['revisions']], [1, 2])
        self.assertTrue(revisions['rev-2']['current'])
        self.assertFalse(revisions['rev-1']['current'])
        self.assertEqual(revisions['rev-1']['author'], 'user')
        self.assertEqual(revisions['rev-2']['author'], 'protagonist')
        self.assertEqual(revisions['rev-1']['created_at'], '2026-09-01T10:00:00.000Z')
        self.assertNotIn('content', revisions['rev-1'], '历史版本只给预览，不给全文')
        self.assertEqual(revisions['rev-1']['preview'], '第一版正文')
        proposals = {item['id']: item for item in payload['proposals']}
        self.assertEqual(proposals['prop-2']['status'], 'pending')
        self.assertTrue(proposals['prop-2']['pending'])
        self.assertEqual(proposals['prop-2']['reason'], '把结尾收一下')
        self.assertEqual(proposals['prop-2']['base_revision_id'], 'rev-2')
        self.assertEqual(proposals['prop-2']['base_revision'], 2)
        self.assertFalse(proposals['prop-1']['pending'])
        self.assertEqual(payload['pending_count'], 1)
        self.assertEqual(payload['jobs'][0]['status'], 'completed')
        self.assertEqual(payload['limits']['content'], console_module.WORK_CONTENT_MAX)
        json.dumps(payload, ensure_ascii=False)

    def test_detail_uses_the_service_projection_for_may_propose(self):
        self._work_row(state=_work_state(jobs=[{'id': 'job-9', 'status': 'running',
                                                'createdAt': '2026-09-04T00:00:00.000Z'}]))
        calls: list = []
        self.bridge.service = _StubWorksService(
            snapshot=self._snapshot(jobs=[{'id': 'job-9', 'status': 'running',
                                           'createdAt': '2026-09-04T00:00:00.000Z'}]),
            projection={'mayPropose': False, 'lastFailure': {'sourceEntryId': 7,
                                                             'status': 'proposal-not-saved',
                                                             'at': '2026-09-04T01:00:00.000Z'}},
            calls=calls,
        )
        payload = _run(self.api.work_detail(self.WID))
        self.assertFalse(payload['may_propose'])
        self.assertIn('在跑', payload['may_propose_reason'])
        self.assertEqual(payload['jobs_running'], 1)
        self.assertEqual(payload['last_failure']['status'], 'proposal-not-saved')
        self.assertIn(('state', self.SID, self.PID), calls)

    def test_detail_explains_a_disabled_feature_instead_of_a_dead_button(self):
        self._work_row()
        self.bridge.service = _StubWorksService(enabled=False, snapshot=self._snapshot())
        payload = _run(self.api.work_detail(self.WID))
        self.assertFalse(payload['may_propose'])
        self.assertIn('配置', payload['may_propose_reason'])
        self.assertEqual(payload['content'], '她改过的正文', '没开也得看得到她写过什么')

    def test_detail_reports_a_broken_row_without_touching_it(self):
        self._work_row(state={'schemaVersion': 99})
        self.bridge.service = _StubWorksService(snapshot=None)
        payload = _run(self.api.work_detail(self.WID))
        self.assertTrue(payload['available'])
        self.assertTrue(payload['broken'])
        self.assertEqual(payload['revisions'], [])
        self.assertIn('原数据', payload['hint'])
        still = self.database.get('interlude_work', {'id': self.WID})
        self.assertEqual(still['state'], {'schemaVersion': 99}, '坏行必须保持原样')

    def test_detail_rejects_unknown_ids_with_a_400(self):
        self.bridge.service = _StubWorksService(snapshot=self._snapshot())
        with self.assertRaises(ConsoleError) as caught:
            _run(self.api.work_detail(''))
        self.assertIn('请选择', str(caught.exception))
        with self.assertRaises(ConsoleError) as caught:
            _run(self.api.work_detail('不存在'))
        self.assertIn('找不到', str(caught.exception))

    # ---- 接受 / 驳回（只有用户能做） ----

    def test_accept_passes_the_ids_through_and_returns_the_panel(self):
        self._work_row()
        calls: list = []
        self.bridge.service = _StubWorksService(
            snapshot=self._snapshot(),
            resolve_result={'ok': True, 'error': '', 'workId': self.WID, 'head': 'rev-9', 'revisions': 3},
            calls=calls,
        )
        payload = _run(self.api.accept_work_proposal(self.WID, 'prop-2'))
        self.assertIn(('accept', self.WID, 'prop-2'), calls)
        self.assertEqual(self._calls(calls, 'reject'), [], '接受不该顺带调驳回')
        self.assertEqual(payload['changed'], 'work-accept prop-2')
        self.assertEqual(payload['result']['head'], 'rev-9')
        self.assertIn('proposals', payload, '写完备回整页数据，前端不用再拉一次')

    def test_reject_goes_through_the_reject_member(self):
        self._work_row(state=_work_state(proposals=[{'id': 'prop-2', 'status': 'pending',
                                                    'baseRevisionId': 'rev-2'}]))
        calls: list = []
        self.bridge.service = _StubWorksService(snapshot=self._snapshot(), calls=calls)
        payload = _run(self.api.reject_work_proposal(self.WID, 'prop-2'))
        self.assertIn(('reject', self.WID, 'prop-2'), calls)
        self.assertEqual(payload['changed'], 'work-reject prop-2')

    def test_unknown_work_or_proposal_is_a_400(self):
        self.bridge.service = _StubWorksService(snapshot=self._snapshot())
        with self.assertRaises(ConsoleError) as caught:
            _run(self.api.accept_work_proposal('不存在的作品', 'prop-2'))
        self.assertIn('找不到这件共同作品', str(caught.exception))
        self._work_row()
        with self.assertRaises(ConsoleError) as caught:
            _run(self.api.accept_work_proposal(self.WID, '不存在的提案'))
        self.assertIn('找不到这条提案', str(caught.exception))
        with self.assertRaises(ConsoleError):
            _run(self.api.accept_work_proposal(self.WID, ''))

    def test_a_decided_proposal_can_not_be_resolved_again(self):
        self._work_row(state=_work_state(proposals=[{'id': 'prop-1', 'status': 'accepted',
                                                     'baseRevisionId': 'rev-1'}]))
        calls: list = []
        self.bridge.service = _StubWorksService(snapshot=self._snapshot(), calls=calls)
        with self.assertRaises(ConsoleError) as caught:
            _run(self.api.reject_work_proposal(self.WID, 'prop-1'))
        self.assertIn('已经处理过', str(caught.exception))
        self.assertEqual(self._calls(calls, 'reject'), [], '结论已定就别再去打扰服务层')

    def test_a_service_side_failure_becomes_a_400_with_the_reason(self):
        self._work_row()
        self.bridge.service = _StubWorksService(
            snapshot=self._snapshot(),
            resolve_result={'ok': False, 'error': '提案基于旧版本；保留提案，不覆盖当前作品。'},
        )
        with self.assertRaises(ConsoleError) as caught:
            _run(self.api.accept_work_proposal(self.WID, 'prop-2'))
        self.assertIn('基于旧版本', str(caught.exception))

    # ---- 新建作品（整条链的起点） ----

    def test_create_without_a_service_is_a_shell(self):
        self.assertEqual(_run(self.api.create_work(self.SID, self.PID, '标题', '正文'))['available'], False)

    def test_create_validates_the_inputs(self):
        self.bridge.service = _StubWorksService(snapshot=self._snapshot())
        with self.assertRaises(ConsoleError) as caught:
            _run(self.api.create_work(self.SID, '', '标题', '正文'))
        self.assertIn('参与者', str(caught.exception))
        with self.assertRaises(ConsoleError) as caught:
            _run(self.api.create_work(self.SID, 'p-不存在', '标题', '正文'))
        self.assertIn('不在当前剧本', str(caught.exception))
        with self.assertRaises(ConsoleError) as caught:
            _run(self.api.create_work(self.SID, self.PID, '  ', '正文'))
        self.assertIn('标题', str(caught.exception))
        with self.assertRaises(ConsoleError):
            _run(self.api.create_work(self.SID, self.PID, '标' * (console_module.WORK_TITLE_MAX + 1), '正文'))
        with self.assertRaises(ConsoleError) as caught:
            _run(self.api.create_work(self.SID, self.PID, '标题', '   '))
        self.assertIn('不能为空', str(caught.exception))
        with self.assertRaises(ConsoleError) as caught:
            _run(self.api.create_work(self.SID, self.PID, '标题', '字' * (console_module.WORK_CONTENT_MAX + 1)))
        self.assertIn('最多', str(caught.exception))

    def test_create_passes_the_fields_through_and_returns_the_new_work(self):
        wid = 'w-new'
        self._work_row(work_id=wid, state=_work_state(head='rev-1', revisions=[
            {'id': 'rev-1', 'parentId': None, 'author': 'user', 'content': '第一版正文',
             'createdAt': '2026-09-01T10:00:00.000Z'},
        ]))
        calls: list = []
        self.bridge.service = _StubWorksService(
            snapshot=self._snapshot(workId=wid, head='rev-1', generation=0),
            create_result={'ok': True, 'error': '', 'workId': wid, 'head': 'rev-1'},
            calls=calls,
        )
        payload = _run(self.api.create_work('', self.PID, '海边的信', '第一版正文'))
        self.assertEqual(self._calls(calls, 'create')[0],
                         ('create', self.SID, self.PID, '海边的信', '第一版正文'))
        self.assertEqual(payload['work_id'], wid)
        self.assertEqual(payload['changed'], 'work-create %s' % wid)
        self.assertEqual(payload['result']['head'], 'rev-1')

    def test_create_reports_the_duplicate_refusal_verbatim(self):
        self.bridge.service = _StubWorksService(
            snapshot=self._snapshot(),
            create_result={'ok': False, 'error': '该私聊已有共同作品；请提出修改，不覆盖旧版本。'},
        )
        with self.assertRaises(ConsoleError) as caught:
            _run(self.api.create_work(self.SID, self.PID, '标题', '正文'))
        self.assertIn('已有共同作品', str(caught.exception), '服务层那条"绝不覆盖"必须原样透给用户')

    def test_create_without_a_story_is_a_400(self):
        self.bridge.service = _StubWorksService(snapshot=self._snapshot())
        empty = Database(':memory:')
        self.addCleanup(empty.close)
        empty.register_tables()
        self.bridge.db = empty
        with self.assertRaises(ConsoleError) as caught:
            _run(self.api.create_work('', self.PID, '标题', '正文'))
        self.assertIn('剧本', str(caught.exception))

    # ---- 取消写手任务 ----

    def test_cancel_without_a_service_is_a_shell(self):
        self.assertEqual(_run(self.api.cancel_work_generation(self.WID, 'job-1'))['available'], False)

    def test_cancel_validates_the_job(self):
        self._work_row(state=_work_state(jobs=[
            {'id': 'job-1', 'status': 'completed', 'createdAt': '2026-09-03T09:00:00.000Z'},
        ]))
        calls: list = []
        self.bridge.service = _StubWorksService(snapshot=self._snapshot(), calls=calls)
        with self.assertRaises(ConsoleError) as caught:
            _run(self.api.cancel_work_generation(self.WID, ''))
        self.assertIn('请选择', str(caught.exception))
        with self.assertRaises(ConsoleError) as caught:
            _run(self.api.cancel_work_generation(self.WID, 'job-不存在'))
        self.assertIn('找不到', str(caught.exception))
        with self.assertRaises(ConsoleError) as caught:
            _run(self.api.cancel_work_generation(self.WID, 'job-1'))
        self.assertIn('已经结束', str(caught.exception))
        self.assertEqual(self._calls(calls, 'cancel'), [], '结论已定的任务别去打扰服务层')
        with self.assertRaises(ConsoleError):
            _run(self.api.cancel_work_generation('不存在的作品', 'job-1'))

    def test_cancel_passes_the_ids_through(self):
        self._work_row(state=_work_state(jobs=[
            {'id': 'job-1', 'status': 'running', 'createdAt': '2026-09-03T09:00:00.000Z'},
        ]))
        calls: list = []
        self.bridge.service = _StubWorksService(
            snapshot=self._snapshot(jobs=[{'id': 'job-1', 'status': 'running',
                                           'createdAt': '2026-09-03T09:00:00.000Z'}]),
            calls=calls,
        )
        payload = _run(self.api.cancel_work_generation(self.WID, 'job-1'))
        self.assertEqual(self._calls(calls, 'cancel')[0], ('cancel', self.SID, self.PID, 'job-1'))
        self.assertEqual(payload['changed'], 'work-cancel job-1')

    def test_cancel_accepts_an_interrupted_job(self):
        """插件重启过、库里还留着 running 的遗留任务：它一直压着 mayPropose，得能取消。"""
        self._work_row(state=_work_state(jobs=[
            {'id': 'job-1', 'status': 'running', 'createdAt': '2026-09-03T09:00:00.000Z'},
        ]))
        calls: list = []
        self.bridge.service = _StubWorksService(
            snapshot=self._snapshot(jobs=[{'id': 'job-1', 'status': 'interrupted',
                                           'createdAt': '2026-09-03T09:00:00.000Z'}]),
            calls=calls,
        )
        _run(self.api.cancel_work_generation(self.WID, 'job-1'))
        self.assertEqual(self._calls(calls, 'cancel')[0][3], 'job-1')

    # ---- 用户手改 ----

    def test_edit_uses_the_current_head_and_keeps_the_text_verbatim(self):
        self._work_row()
        calls: list = []
        self.bridge.service = _StubWorksService(
            snapshot=self._snapshot(),
            edit_result={'ok': True, 'error': '', 'workId': self.WID, 'head': 'rev-3',
                         'revision': {'id': 'rev-3'}},
            calls=calls,
        )
        payload = _run(self.api.edit_work(self.WID, '我重写的一段\n带换行', '改个结尾'))
        kind, story, participant, edit = self._calls(calls, 'edit')[0]
        self.assertEqual((kind, story, participant), ('edit', self.SID, self.PID))
        self.assertEqual(edit['baseRevisionId'], 'rev-2', '基础版本必须是当前 head')
        self.assertEqual(edit['content'], '我重写的一段\n带换行')
        self.assertEqual(edit['reason'], '改个结尾')
        self.assertEqual(payload['changed'], 'work-edit %s' % self.WID)
        self.assertEqual(payload['result']['revision_id'], 'rev-3')

    def test_edit_fills_in_a_reason_when_the_user_left_it_empty(self):
        self._work_row()
        calls: list = []
        self.bridge.service = _StubWorksService(snapshot=self._snapshot(), calls=calls)
        _run(self.api.edit_work(self.WID, '新正文'))
        self.assertTrue(self._calls(calls, 'edit')[0][3]['reason'],
                        '服务层要求理由非空，控制台别把空串塞进去')

    def test_edit_rejects_empty_and_oversized_content(self):
        self._work_row()
        self.bridge.service = _StubWorksService(snapshot=self._snapshot())
        with self.assertRaises(ConsoleError) as caught:
            _run(self.api.edit_work(self.WID, '   '))
        self.assertIn('不能为空', str(caught.exception))
        with self.assertRaises(ConsoleError) as caught:
            _run(self.api.edit_work(self.WID, '字' * (console_module.WORK_CONTENT_MAX + 1)))
        self.assertIn('最多', str(caught.exception))
        with self.assertRaises(ConsoleError):
            _run(self.api.edit_work(self.WID, '正文', '理由' * (console_module.WORK_REASON_MAX + 1)))
        # 上限内照常通过
        payload = _run(self.api.edit_work(self.WID, '字' * console_module.WORK_CONTENT_MAX))
        self.assertTrue(payload['available'])

    def test_edit_rejects_an_unknown_work(self):
        self.bridge.service = _StubWorksService(snapshot=self._snapshot())
        with self.assertRaises(ConsoleError):
            _run(self.api.edit_work('不存在', '正文'))

    # ---- 让她起草 ----

    def test_generate_requires_a_brief_and_caps_it(self):
        self._work_row()
        self.bridge.service = _StubWorksService(snapshot=self._snapshot())
        with self.assertRaises(ConsoleError) as caught:
            _run(self.api.start_work_generation(self.WID, '  '))
        self.assertIn('创作意图', str(caught.exception))
        with self.assertRaises(ConsoleError) as caught:
            _run(self.api.start_work_generation(self.WID, '意' * (console_module.WORK_BRIEF_MAX + 1)))
        self.assertIn('最多', str(caught.exception))

    def test_generate_starts_a_job_with_the_current_head(self):
        self._work_row()
        calls: list = []
        self.bridge.service = _StubWorksService(
            snapshot=self._snapshot(),
            generate_result={'ok': True, 'error': '', 'job': {'id': 'job-2', 'status': 'running'},
                             'modelId': 'demo', 'generationMode': 'main'},
            calls=calls,
        )
        payload = _run(self.api.start_work_generation(self.WID, '写一段海边的结尾'))
        kind, story, participant, request = self._calls(calls, 'generate')[0]
        self.assertEqual((kind, story, participant), ('generate', self.SID, self.PID))
        self.assertEqual(request['baseRevisionId'], 'rev-2')
        self.assertEqual(request['brief'], '写一段海边的结尾')
        self.assertEqual(payload['job']['id'], 'job-2')
        self.assertEqual(payload['changed'], 'work-generate %s' % self.WID)

    def test_generate_reports_a_service_side_refusal(self):
        self._work_row()
        self.bridge.service = _StubWorksService(
            snapshot=self._snapshot(),
            generate_result={'ok': False, 'error': '共同作品未启用。'},
        )
        with self.assertRaises(ConsoleError) as caught:
            _run(self.api.start_work_generation(self.WID, '写一段'))
        self.assertIn('未启用', str(caught.exception))

    # ---- 调用形状自适应（服务层两种签字） ----

    def test_edit_and_generate_adapt_to_the_content_reason_signature(self):
        self._work_row()
        calls: list = []
        self.bridge.service = _LegacyWorksService(snapshot=self._snapshot(), calls=calls)
        _run(self.api.edit_work(self.WID, '新正文', '理由'))
        self.assertEqual(self._calls(calls, 'edit-legacy')[0],
                         ('edit-legacy', self.SID, self.PID, '新正文', '理由'))
        _run(self.api.start_work_generation(self.WID, '写一版'))
        self.assertEqual(self._calls(calls, 'generate-legacy')[0],
                         ('generate-legacy', self.SID, self.PID, '写一版'))

    # ---- 导出 ----

    def test_export_returns_the_service_parts_without_resplitting(self):
        self._work_row()
        parts = ['a' * 2400, 'b' * 2400, 'c' * 10]
        self.bridge.service = _StubWorksService(snapshot=self._snapshot(), parts=parts)
        payload = _run(self.api.export_work(self.WID))
        self.assertEqual(payload['parts'], parts, '分段归服务层切，控制台不许再切一遍')
        self.assertEqual(payload['count'], 3)
        self.assertEqual(payload['chars'], sum(len(part) for part in parts))
        self.assertEqual(payload['title'], '海边的信')

    def test_export_of_a_work_without_text_says_so(self):
        self._work_row()
        self.bridge.service = _StubWorksService(snapshot=self._snapshot(), parts=[])
        payload = _run(self.api.export_work(self.WID))
        self.assertEqual(payload['count'], 0)
        self.assertTrue(payload['hint'])

    def test_export_rejects_an_unknown_work(self):
        self.bridge.service = _StubWorksService(parts=['x'])
        with self.assertRaises(ConsoleError):
            _run(self.api.export_work('不存在'))
        with self.assertRaises(ConsoleError):
            _run(self.api.export_work(''))

    # ---- 容错 ----

    def test_works_endpoints_survive_a_broken_database(self):
        """表被删了（旧库 / 手工改过）也不能让面板 500。"""

        class _Broken:
            path = ''

            def all(self, *_args, **_kwargs):
                raise RuntimeError('no such table')

            def get(self, *_args, **_kwargs):
                raise RuntimeError('no such table')

        self.bridge.service = _StubWorksService(snapshot=self._snapshot())
        with mock.patch.object(self.bridge, 'db', _Broken()):
            payload = _run(self.api.works_overview(self.SID))
            self.assertEqual(payload['works'], [])
            with self.assertRaises(ConsoleError):
                _run(self.api.work_detail(self.WID))


class WorksIntegrationTests(unittest.TestCase):
    """真的接上了才算数：控制台 ↔ `chunk14` ↔ `interlude_work` 表跑一遍完整链路。

    作品主键是 `work_key()` 的 sha256（不是 `story:participant`），所以 id 一律从服务层
    的返回值里拿——测试里手拼一个 id 就等于自己骗自己。
    """

    SID = 'character:qq:20000'
    PID = 'p-kela'

    def setUp(self):
        self.bridge, self.database = _works_bridge({'works': {'enabled': True}})
        self.addCleanup(self.database.close)
        self.api = ConsoleApi(self.bridge)
        self.database.upsert('interlude_story', {
            'id': self.SID, 'status': 'active', 'platform': 'qq',
            'setting': {'character': {'name': '凌梦'}, 'user': {'name': '主人'}},
            'state': {}, 'createdAt': '2026-09-01T00:00:00.000Z',
            'updatedAt': '2026-09-02T00:00:00.000Z',
        })
        self.database.upsert('interlude_participant', {
            'id': self.PID, 'storyId': self.SID, 'platform': 'onebot', 'selfId': '20000',
            'userId': '10001', 'channelId': 'private:10001', 'personId': 'kela',
            'displayName': '主人', 'status': 'active', 'state': {},
            'createdAt': '2026-09-01T00:00:00.000Z', 'updatedAt': '2026-09-02T00:00:00.000Z',
        })
        self.wid = ''

    def _create(self, title='海边的信', content='第一版正文'):
        """用**服务层**建一件作品（与真实写法一致），返回 workId。"""
        created = _run(self.bridge.service.create_work(self.SID, self.PID, title, content))
        self.assertTrue(created['ok'], created)
        self.wid = created['workId']
        self.assertTrue(self.wid, 'workId 是服务层生成的（sha256），不许自己拼')
        return self.wid

    def _head(self):
        row = self.database.get('interlude_work', {'id': self.wid})
        return row['state']['head']

    def test_the_whole_panel_flow_against_the_real_service(self):
        service = self.bridge.service
        self.assertTrue(callable(getattr(service, 'works_snapshot', None)),
                        '服务层没接线时这条用例要红（面板就只剩空壳了）')
        self._create()

        # 她还什么都没提议时：清单一行、0 待决、可以让她起草
        overview = _run(self.api.works_overview(self.SID))
        self.assertTrue(overview['available'])
        self.assertTrue(overview['enabled'])
        self.assertEqual([item['work_id'] for item in overview['works']], [self.wid])
        self.assertEqual(overview['works'][0]['participant'], '主人')
        self.assertEqual(overview['works'][0]['title'], '海边的信')
        self.assertEqual(overview['works'][0]['revision'], 1)
        self.assertEqual(overview['works'][0]['pending_count'], 0)
        self.assertTrue(overview['works'][0]['may_propose'])

        # 她提了一条（作者 = protagonist，待决）→ 清单的待决徽章 +1、详情看得到正文与理由
        proposal = _run(service.apply_work_proposal(self.SID, self.PID, {
            'baseRevisionId': self._head(), 'content': '她提议的正文', 'reason': '把结尾收一下',
        }))
        self.assertIsNotNone(proposal, '模型侧的提案该落成待决提案')
        overview = _run(self.api.works_overview(self.SID))
        self.assertEqual(overview['works'][0]['pending_count'], 1)
        detail = _run(self.api.work_detail(self.wid))
        self.assertEqual(detail['content'], '第一版正文', '待决提案不该动 head')
        self.assertEqual(detail['pending_count'], 1)
        self.assertEqual(detail['proposals'][0]['author'], 'protagonist')
        self.assertEqual(detail['proposals'][0]['reason'], '把结尾收一下')
        self.assertEqual(detail['proposals'][0]['base_revision'], 1)

        # 只有用户能接受：接受后 head 前移、版本数 +1
        accepted = _run(self.api.accept_work_proposal(self.wid, proposal['id']))
        self.assertEqual(accepted['changed'], 'work-accept %s' % proposal['id'])
        detail = _run(self.api.work_detail(self.wid))
        self.assertEqual(detail['content'], '她提议的正文')
        self.assertEqual(detail['revision_count'], 2)
        self.assertEqual(detail['revision'], 2)
        self.assertEqual(detail['pending_count'], 0)
        self.assertEqual([item['author'] for item in detail['revisions']], ['user', 'protagonist'])
        self.assertTrue(detail['revisions'][1]['current'])
        self.assertNotIn('content', detail['revisions'][0], '历史版本只给预览')

        # 用户手改：一条新版本，作者 = user
        edited = _run(self.api.edit_work(self.wid, '我自己改的正文', '还是我来收尾'))
        self.assertEqual(edited['changed'], 'work-edit %s' % self.wid)
        detail = _run(self.api.work_detail(self.wid))
        self.assertEqual(detail['content'], '我自己改的正文')
        self.assertEqual(detail['revision_count'], 3)
        self.assertEqual(detail['revisions'][-1]['author'], 'user')
        self.assertEqual(detail['revisions'][-1]['ordinal'], 3)
        self.assertEqual(detail['content_chars'], len('我自己改的正文'))

        # 导出：分段由服务层切好，拼回去就是完整的一行 JSON
        exported = _run(self.api.export_work(self.wid))
        self.assertGreaterEqual(exported['count'], 1)
        self.assertEqual(''.join(exported['parts']), json.dumps(
            self.database.get('interlude_work', {'id': self.wid}),
            ensure_ascii=False, separators=(',', ':'),
        ))
        json.dumps(exported, ensure_ascii=False)

    def test_a_long_work_is_split_into_message_sized_parts(self):
        """长文导出要真的分段：每段都在单条消息的安全长度内，拼回来一字不差。"""
        from plugin.core.works import DUMP_PART_MAX_LEN

        self._create(content='正文。' * 2000)
        exported = _run(self.api.export_work(self.wid))
        self.assertGreater(exported['count'], 1)
        for part in exported['parts']:
            self.assertLessEqual(len(part), DUMP_PART_MAX_LEN, '每段都要能单条消息发出去')
        self.assertEqual(exported['chars'], sum(len(part) for part in exported['parts']))
        self.assertEqual(json.loads(''.join(exported['parts']))['id'], self.wid)

    def test_create_through_the_panel_starts_the_chain(self):
        """整条链的起点：没有它，保存提案 / 手改 / 起草全都无从下手。"""
        payload = _run(self.api.create_work('', self.PID, '海边的信', '第一版正文'))
        self.assertTrue(payload['available'])
        self.assertTrue(payload['work_id'])
        self.assertEqual(payload['changed'], 'work-create %s' % payload['work_id'])
        self.assertEqual(payload['title'], '海边的信')
        self.assertEqual(payload['content'], '第一版正文')
        self.assertEqual(payload['revision_count'], 1)
        self.assertEqual(payload['revisions'][0]['author'], 'user')
        overview = _run(self.api.works_overview(self.SID))
        self.assertEqual([item['work_id'] for item in overview['works']], [payload['work_id']])
        self.assertEqual(overview['works'][0]['participant'], '主人')

        # 再建一次：服务层拒绝（**绝不覆盖**），文案原样给用户
        with self.assertRaises(ConsoleError) as caught:
            _run(self.api.create_work(self.SID, self.PID, '另一个标题', '另一份正文'))
        self.assertIn('已有共同作品', str(caught.exception))
        self.assertEqual(_run(self.api.work_detail(payload['work_id']))['content'], '第一版正文')

    def test_create_uses_the_current_story_when_story_id_is_empty(self):
        payload = _run(self.api.create_work('', self.PID, '海边的信', '第一版正文'))
        row = self.database.get('interlude_work', {'id': payload['work_id']})
        self.assertEqual(row['storyId'], self.SID)
        self.assertEqual(row['participantId'], self.PID)

    def test_cancel_frees_a_stale_running_job(self):
        """插件重启过、库里留着 `running`：面板说成"中断"，取消是放开 mayPropose 的路。"""
        self._create()
        row = self.database.get('interlude_work', {'id': self.wid})
        state = dict(row['state'])
        state['jobs'] = [{
            'id': 'job-stale', 'status': 'running', 'brief': '写一版结尾',
            'baseRevisionId': state['head'], 'modelId': 'demo',
            'createdAt': '2026-10-01T00:00:00.000Z',
        }]
        self.database.update('interlude_work', {'id': self.wid}, {'state': state})

        detail = _run(self.api.work_detail(self.wid))
        self.assertEqual(detail['jobs'][0]['status'], 'interrupted', '进程里没有的 running = 中断')
        self.assertFalse(detail['may_propose'])

        payload = _run(self.api.cancel_work_generation(self.wid, 'job-stale'))
        self.assertEqual(payload['changed'], 'work-cancel job-stale')
        detail = _run(self.api.work_detail(self.wid))
        self.assertEqual(detail['jobs'][0]['status'], 'cancelled')
        self.assertTrue(detail['may_propose'], '取消掉遗留任务后要能重新起草')
        with self.assertRaises(ConsoleError) as caught:
            _run(self.api.cancel_work_generation(self.wid, 'job-stale'))
        self.assertIn('已经结束', str(caught.exception))

    def test_reject_keeps_the_head_and_the_record(self):
        service = self.bridge.service
        self._create()
        proposal = _run(service.apply_work_proposal(self.SID, self.PID, {
            'baseRevisionId': self._head(), 'content': '不想要的改法', 'reason': '试试',
        }))
        payload = _run(self.api.reject_work_proposal(self.wid, proposal['id']))
        self.assertEqual(payload['changed'], 'work-reject %s' % proposal['id'])
        detail = _run(self.api.work_detail(self.wid))
        self.assertEqual(detail['content'], '第一版正文', '驳回不动正文')
        self.assertEqual(detail['revision_count'], 1)
        self.assertEqual(detail['proposals'][0]['status'], 'rejected')
        self.assertFalse(detail['proposals'][0]['pending'])

    def test_disabled_works_still_show_what_was_written(self):
        """关掉功能后旧作品还在库里，控制台要看得见（否则用户再也清理不掉它）。"""
        self._create()
        for holder in (self.bridge.config, self.bridge.service.config):
            if isinstance(holder.get('works'), dict):
                holder['works']['enabled'] = False
        payload = _run(self.api.works_overview(self.SID))
        self.assertTrue(payload['available'])
        self.assertFalse(payload['enabled'])
        self.assertEqual(len(payload['works']), 1, '关掉功能不等于把作品藏起来')
        detail = _run(self.api.work_detail(self.wid))
        self.assertEqual(detail['content'], '第一版正文')
        self.assertFalse(detail['may_propose'])
        self.assertIn('配置', detail['may_propose_reason'])

    def test_overview_without_any_work_is_an_empty_list_not_an_error(self):
        payload = _run(self.api.works_overview(self.SID))
        self.assertTrue(payload['available'])
        self.assertTrue(payload['enabled'])
        self.assertEqual(payload['works'], [])
        self.assertEqual(payload['hint'], '')
        with self.assertRaises(ConsoleError):
            _run(self.api.work_detail('404'))
