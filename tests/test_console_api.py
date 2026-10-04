"""控制台后端（`adapters/console_api.py`）的数据形状与容错。

这些用例都走**真实** `AstrbotBridge` + 内存数据库（沿用 `test_astrbot_bridge.py`
里的桩），因为控制台的整个价值就在于"读出来的是真数据"。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

# 复用桥接测试里的 AstrBot 桩与夹具（导入即装桩）
from plugin.tests.test_astrbot_bridge import (
    TEST_DATA_DIR,
    FakeContext,
    _FakeTokenUsage,
    _FakeUpload,
    _ProviderStub,
    _RecordingFakeContext,
    _install_web_request,
    _make_bridge,
    _make_plugin,
    bridge_module,
)
from plugin.adapters import console_api as console_module
from plugin.adapters.console_api import ConsoleApi, ConsoleError, CONSOLE_TASKS, mask_endpoint
from plugin.core import platform_actions
from plugin.core.database import Database
from plugin.core.narrator import parse_token_usage
from plugin.core.logging import set_log_sink
from plugin.core.service import helpers as helpers_module
from plugin.core.service.base import InterludeContext
from plugin.core.service.chunk2 import ServiceChunk2
from plugin.core.service.chunk2 import _list_sticker_files
from plugin.core.service.helpers import STICKER_DEFAULT_DIRECTORY, sticker_root_from
from plugin.core.service.transport import NullTransport


def _run(coro):
    return asyncio.run(coro)


#: 仓库根（`plugin/tests/x.py` → `tests` → `plugin` → 仓库根）。发布仓布局里没有 `docs/`。
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: 「对象行列表」在宿主配置页的那句提示：**三处逐字一致**（`_conf_schema.json` 的 5 处
#: hint / `console_api.HOST_LIST_DEGRADED_NOTE` / 两份 docs）。初版写的是「⚠️此配置项不
#: 生效…」——那是个**没在真机上点过**的断言（依据见 `docs/PORTING_NOTES.md` §54.4），
#: v1.9.1 改成"不下断言 + 给出路 + 怎么验证"。
HOST_LIST_NOTE = (
    '这一项在宿主配置页可查看；若改不动、或保存后没生效，'
    '请在「幕间控制台 → 配置」里改（那边会按 schema 显示每项的生效值，可对照验证）。'
)

#: 五处对象行列表（`type: list` + 行内字段映射）在 schema 里的路径。
HOST_LIST_PATHS = (
    ('model_center', 'providers'),
    ('qq_access', 'bot_accounts'),
    ('qq_access', 'user_accounts'),
    ('qq_access', 'group_chats'),
    ('runtime', 'rest_windows'),
)


#: 一张最小合法 PNG（魔数正确即可，内容不参与判据）。
_PNG_BYTES = b'\x89PNG\r\n\x1a\n' + b'x' * 32


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

    def test_a_field_that_only_has_a_schema_default_is_not_reported_as_unset(self):
        """用户点名的误导（v1.9.1 修）：磁盘上没写过的键，显示的是**生效值**而不是"未设置"。

        真机现场：宿主按 schema 重建配置，新版本新增的键在用户去过宿主配置页之前
        根本不在文件里（整个 `model_center.video` 组就是这样）。旧口径会把这一项报成
        `present=False` + `value=None`，界面上就是「未设置」+「—」，而运行期明明跑的是
        默认值——界面与行为相反。现在：值 = schema 默认值，来源 = `default`。
        """
        # 夹具的磁盘配置里没有 `video` 组（= 用户从没在宿主配置页动过它）。
        payload = _run(self.api.config_schema())
        groups = {group['key']: group for group in payload['groups']}
        video = next(item for item in groups['model_center']['fields'] if item['key'] == 'video')
        self.assertEqual(video['value'], console_module.effective_field_value(
            'video', video['node'], {},
        )[0])
        self.assertEqual(video['value']['enabled'], False)
        self.assertEqual(video['value']['timeout_seconds'], 20)
        self.assertEqual(video['value_source'], 'default', '用的是默认值')
        self.assertTrue(video['present'], '「有生效值」——界面不该再挂"未设置"')
        # 对象节点递归填默认值：嵌套表单的每个子项都要有值。
        failover = next(item for item in groups['model_center']['fields'] if item['key'] == 'failover')
        self.assertEqual(failover['value']['strategy'], 'priority')

    def test_the_value_source_tells_explicit_from_default(self):
        """`explicit` = 磁盘上写过；`default` = 用 schema 默认值；`missing` = 真没有值。"""
        self.bridge.raw_config = lambda: {
            'model_center': {'vision': {'enabled': True}},
            'runtime': {},
        }
        payload = _run(self.api.config_schema())
        groups = {group['key']: group for group in payload['groups']}
        fields = {item['key']: item for item in groups['model_center']['fields']}
        self.assertEqual(fields['vision']['value_source'], 'explicit')
        self.assertTrue(fields['vision']['value']['enabled'])
        self.assertEqual(fields['video']['value_source'], 'default')
        # schema 里没有默认值的项（提示词组那几个 text 有默认值；这里挑一个真的没有的）。
        self.assertEqual(
            console_module.effective_field_value('not_a_key', {'type': 'string'}, {}),
            (None, 'missing'),
        )
        self.assertFalse(
            bool(console_module.effective_field_value('not_a_key', {'type': 'string'}, {})[1] != 'missing'),
        )

    def test_connection_task_badges_cover_every_use_for_flag(self):
        """勾了的用途**必须**在连接行上显示出来（含 world_seeding / works / video）。

        旧实现拿路由任务表（`CONSOLE_TASKS`）拼 `use_for_<key>`：`timeline` 没有独立
        开关（永远读不到），而 world_seeding / works / video 三个键不在那张表里
        （勾了也看不见）——正是用户点名的"指明了却不生效"那类误导。
        """
        # `normalize_config` 把 schema 分组名搬成了上游段名，直接改那一份才是运行期读的。
        self.bridge.config['model'] = {'providers': [{
            'label': '全部用途', 'enabled': True, 'model': 'demo',
            'endpoint': 'https://gw.example.com/v1/chat/completions',
            'use_for_main': True, 'use_for_world_seeding': True,
            'use_for_works': True, 'use_for_video': True,
        }]}
        row = _run(self.api.models())['connections'][0]
        self.assertEqual(row['tasks'], ['主叙事', '世界播种', '共同作品写手', '视频理解'])
        self.assertNotIn('时间导演', row['tasks'], 'timeline 没有独立开关，不许拿它去拼键')

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

    def test_the_service_reads_the_table_the_panel_writes(self):
        """**接线用例（v1.7.9）**：面板写的那份权限表，服务层必须读得到。

        写侧落点是 `bridge.data_dir`（= 构造 `AstrbotInterludeContext` 时给的 `base_dir`），
        读侧原先读的是 core 里**根本不存在**的 `self.context` → 恒回空表，
        于是"界面能改、运行期不生效"（真 bug）。这里两侧都走**真实对象**：
        目录同源 + 真写文件 + 真读回来。
        """
        service = self.bridge.service
        self.assertEqual(
            service.interlude_data_dir(), str(self.bridge.data_dir),
            '写侧（bridge.data_dir）与读侧（ctx.base_dir）必须同源',
        )
        path = Path(self.bridge.data_dir) / 'action_permissions.json'
        backup = path.read_text(encoding='utf-8') if path.exists() else None

        def restore():
            if backup is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(backup, encoding='utf-8')

        self.addCleanup(restore)
        # 默认档：`send_poke` 是 global（私聊可用）
        self.assertIn('send_poke', service.available_platform_actions('', ('private',)))
        path.write_text(json.dumps({'send_poke': 'admin'}), encoding='utf-8')
        self.assertEqual(service.action_permission_table(), {'send_poke': 'admin'})
        self.assertNotIn(
            'send_poke', service.available_platform_actions('', ('private',)),
            '表里降到「仅管理员」之后，普通私聊会话就不该再拿得到这条动作',
        )

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

    def test_models_shows_the_works_writer_task_with_the_dual_read_binding(self):
        """v1.7.9：写手模型也进「模型」页，但它有两套填法——老口径的值不能被报成
        「某个 AstrBot Provider」（那会让用户去宿主的模型列表里找一个不存在的东西）。"""
        row = _run(self.api.models())['task_models']['works']
        self.assertEqual(row['label'], '共同作品写手')
        self.assertEqual(row['astrbot_provider'], '', '夹具里没配写手模型')

        # 新口径：指名了一个真实 Provider → 显示它，并算进那个 Provider 的 used_by。
        bridge = _make_bridge({'works': {'enabled': True, 'model_id': 'ollama'}})
        bridge.context.get_all_providers = lambda: [_ProviderStub('ollama')]
        api = ConsoleApi(bridge)
        self.assertEqual(_run(api.models())['task_models']['works']['astrbot_provider'], 'ollama')

        # 老口径：值点的是一条连接行 → 不报成 Provider（值本身仍在配置里）。
        legacy = _make_bridge({
            'works': {'enabled': True, 'model_id': 'writer-conn'},
            'model_center': {'providers': [{
                'id': 'writer-conn', 'label': '写手连接', 'enabled': True, 'model': 'w',
                'endpoint': 'https://gw.example.com/v1/chat/completions',
            }]},
        })
        legacy.context.get_all_providers = lambda: [_ProviderStub('ollama')]
        payload = _run(ConsoleApi(legacy).models())
        self.assertEqual(payload['task_models']['works']['astrbot_provider'], '')
        self.assertEqual(legacy.task_model_id('works'), 'writer-conn', '值本身不动')

    def test_the_astrbot_provider_list_names_the_works_writer_task(self):
        """「这个 Provider 被哪些任务在用」也要认写手（否则用户看到的是"没人用它"）。"""
        bridge = _make_bridge({'works': {'enabled': True, 'model_id': 'ollama'}})
        bridge.context.get_all_providers = lambda: [_ProviderStub('ollama')]
        bridge.provider_by_id = lambda pid: _ProviderStub(pid)  # type: ignore[method-assign]
        row = _run(ConsoleApi(bridge).models())['astrbot_providers'][0]
        self.assertIn('共同作品写手', row['used_by'])

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


class HostListNoteWordingTests(unittest.TestCase):
    """宿主配置页那句提示的**文案对账**（变异保护：写回「不生效」→ 红）。

    三处必须逐字一致：`_conf_schema.json` 的 5 处对象行列表 `hint`、
    `console_api.HOST_LIST_DEGRADED_NOTE`、`docs/PORTING_NOTES.md` §54.4 与
    `docs/CONFIG_MAP.md`。只改一处 → 红；改回旧断言 → 红。
    """

    def test_the_console_constant_is_the_agreed_sentence(self) -> None:
        self.assertEqual(console_module.HOST_LIST_DEGRADED_NOTE, HOST_LIST_NOTE)
        # 旧断言是"没在真机上点过"的结论，不许回来。
        self.assertNotIn('不生效', console_module.HOST_LIST_DEGRADED_NOTE)
        # 控制台前端按「幕间控制台」这个词过滤这类 hint，措辞里必须留着它。
        self.assertIn('幕间控制台', console_module.HOST_LIST_DEGRADED_NOTE)

    def test_every_object_row_list_hint_is_the_same_sentence(self) -> None:
        schema = console_module.load_config_schema()
        self.assertTrue(schema, '_conf_schema.json 读不到')
        for group, field in HOST_LIST_PATHS:
            node = schema[group]['items'][field]
            self.assertEqual(node.get('hint'), HOST_LIST_NOTE, '%s.%s' % (group, field))
        # 宿主对 `obvious_hint` 自己加 ‼️（文案里别再写一个）。
        self.assertIs(schema['model_center']['items']['providers'].get('obvious_hint'), True)

    def test_the_docs_quote_the_same_sentence(self) -> None:
        for name in ('PORTING_NOTES.md', 'CONFIG_MAP.md'):
            path = os.path.join(REPO_ROOT, 'docs', name)
            if not os.path.exists(path):
                self.skipTest('发布仓布局没有 docs/%s' % name)
            with open(path, encoding='utf-8') as handle:
                self.assertIn(HOST_LIST_NOTE, handle.read(), '%s 缺那句逐字文案' % name)


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


# =========================================================================== #
# 控制台「表情库」面板（v1.8.0）
#
# 这里跑的是**真实** `ServiceChunk2` + 真实内存库 + 真实临时目录：
# 面板的全部价值是"读出来 / 写下去的确实是那件事"，桩掉服务层就什么都没验到。
# 契约（端点 + 字段）冻结在 `docs/PORTING_NOTES.md` §45.3。
# =========================================================================== #

class _FilesystemStickerTransport(NullTransport):
    """`NullTransport` + 真实的 `list_sticker_files`。

    生产里这一步走适配器的同一个实现；`NullTransport` 的桩实现**故意回空**
    （没有平台连接器时的降级），拿它跑扫描会把库里每一行都标成 missing。

    ⚠️ `staticmethod` 不是装饰性写法：直接把模块级函数塞进类体，它就变成了方法，
    `self._lister(root)` 会多传一个 `self` → `TypeError`；而扫描把任何异常都收成一条
    `表情包库扫描失败` 的 warn（**静默**），于是夹具坏了、用例照样"全绿"——v1.9.1
    实测踩到：控制台那几条扫描用例其实一张图都没进过库。
    """

    _lister = staticmethod(_list_sticker_files)

    async def list_sticker_files(self, root: str) -> list:
        return self._lister(root)


class _StickerService(ServiceChunk2):
    """只装配表情库需要的那几个字段的真实服务（绕开模型装配）。

    `db` / `_db_write_lock` 与 `test_service_chunk2._host` 同一套：走**真实**的
    `db_get` / `db_set` / `db_create`（含写队列），而不是把 CRUD 也桩掉。
    """

    def __init__(self, tmp: str, database: Any, directory: str = 'stickers') -> None:
        self.ctx = InterludeContext(base_dir=tmp)
        self.db = database
        self._db_write_lock = asyncio.Lock()
        self.config = {'stickers': {'enabled': True, 'directory': directory, 'catalogLimit': 40}}
        self.transport = _FilesystemStickerTransport()
        self.service_logger = None
        self.cached_sticker_config = None
        self.cached_audio_config = None
        self.cached_blind_mode_config = None
        self.embedder = None
        self.sticker_catalog = []
        self.sticker_by_id = {}
        self.sticker_scan_running = False
        self.sticker_describer = None
        self._sticker_collect_warn_at = 0
        self.reports: list[Any] = []
        self.report = lambda *args, **kwargs: self.reports.append(args)
        self.report_standalone = lambda *args, **kwargs: self.reports.append(args)
        self.report_standalone_operation = lambda *args, **kwargs: self.reports.append(args)


class ConsoleStickerLibraryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = self._tmp.name
        self.root = os.path.join(self.tmp, 'stickers')
        os.makedirs(os.path.join(self.root, 'collected'))
        self.bridge = _make_bridge({'stickers': {'enabled': True, 'directory': 'stickers'}})
        self.database = Database(':memory:')
        self.addCleanup(self.database.close)
        self.database.register_tables()
        self.bridge.db = self.database
        self.service = _StickerService(self.tmp, self.database)
        self.bridge.service = self.service
        self.api = ConsoleApi(self.bridge)

    # ---- 造数据 ----

    def _row(self, asset_id, **patch):
        base = {
            'assetId': asset_id,
            'filePath': '%s.png' % asset_id,
            'group': 'collected',
            'mimeType': 'image/png',
            'animated': False,
            'size': 40,
            'hash': asset_id,
            'description': '一只挥手的猫',
            'aliases': ['打招呼'],
            'status': 'active',
            'embedding': [],
            'name': '',
            'source': 'auto' if asset_id.startswith('sticker-') else 'manual',
            'uses': 0,
            'descriptionManual': False,
            # v1.8.0 第二层判据（§45.7）：默认不是"模型猜的"。
            'guessed': False,
            'createdAt': '2026-09-01T10:00:00.000Z',
            'updatedAt': '2026-09-01T10:00:00.000Z',
        }
        base.update(patch)
        return base

    def _insert(self, asset_id, body: bytes = _PNG_BYTES, **patch):
        with open(os.path.join(self.root, '%s.png' % asset_id), 'wb') as handle:
            handle.write(body)
        # `hash` 必须是**文件内容**的 sha256（生产的写入方就是这么写的）：
        # 夹具曾经把它设成 assetId，于是"重扫"看到的永远是"文件变了"，
        # 而扫描又恰好被夹具的 `list_sticker_files` 缺陷静默掉——两边一起错，
        # 用例全绿而实际一张图都没进过库（v1.9.1 实测踩到）。
        patch.setdefault('hash', hashlib.sha256(body).hexdigest())
        row = self._row(asset_id, **patch)
        row['id'] = self.database.insert('interlude_sticker', row)
        return row

    # ---- 列表 ----

    def test_empty_library_is_an_empty_shell_not_a_500(self):
        payload = _run(self.api.stickers())
        self.assertEqual(payload['items'], [])
        self.assertEqual(payload['total'], 0)
        self.assertFalse(payload['truncated'])
        self.assertEqual(payload['counts'], {'total': 0, 'active': 0, 'pending': 0,
                                             'missing': 0, 'disabled': 0})
        self.assertTrue(payload['enabled'])
        self.assertEqual(payload['root'], self.root)

    def test_items_carry_the_frozen_contract_fields(self):
        self._insert('sticker-abc-1')
        payload = _run(self.api.stickers())
        self.assertEqual(len(payload['items']), 1)
        item = payload['items'][0]
        for key in ('assetId', 'name', 'description', 'kind', 'source', 'addedAt',
                    'uses', 'disabled', 'file', 'thumbnailUrl'):
            self.assertIn(key, item, key)
        self.assertEqual(item['assetId'], 'sticker-abc-1')
        self.assertEqual(item['description'], '一只挥手的猫')
        self.assertEqual(item['kind'], 'image')
        self.assertEqual(item['source'], 'auto')
        self.assertEqual(item['addedAt'], '2026-09-01T10:00:00+00:00',
                         '时间列折成 ISO 文本（原始行回来的是 datetime）')
        self.assertEqual(item['uses'], 0)
        self.assertFalse(item['disabled'])
        self.assertEqual(item['file'], 'sticker-abc-1.png')
        self.assertEqual(
            item['thumbnailUrl'], 'console/sticker-file?assetId=sticker-abc-1',
        )

    def test_gif_rows_report_the_animated_kind(self):
        """`animated` 在原始行里是 SQLite 的 `1`，`is True` 会漏（实测踩到过）。"""
        self._insert('sticker-gif-1', mimeType='image/gif', animated=True)
        item = _run(self.api.stickers())['items'][0]
        self.assertEqual(item['kind'], 'animated')
        # `scan_sticker_library` 按扩展名建出来的行走的是 `mimeType` 这条路。
        self._insert('sticker-gif-2', mimeType='image/gif', animated=False)
        items = {entry['assetId']: entry for entry in _run(self.api.stickers())['items']}
        self.assertEqual(items['sticker-gif-2']['kind'], 'animated')

    def test_the_guessed_badge_is_exposed_as_an_extra_field(self):
        """第二层判据（`§45.7`）：`guessed` 是**额外字段**（契约只有 auto / manual 两个 source）。

        `source` 仍回 `auto`；"这一条是识图模型猜出来的"只能靠 `guessed` 暴露——
        控制台**暂未显示**这个徽章，留给下一轮（`docs/PORTING_NOTES.md` §45.7）。
        """
        self._insert('sticker-guessed-1', guessed=True)
        self._insert('sticker-plain-1', guessed=False)
        items = {entry['assetId']: entry for entry in _run(self.api.stickers())['items']}
        self.assertIn('guessed', items['sticker-guessed-1'], '额外字段也要在响应里')
        self.assertTrue(items['sticker-guessed-1']['guessed'])
        self.assertFalse(items['sticker-plain-1']['guessed'])
        self.assertEqual(items['sticker-guessed-1']['source'], 'auto', 'source 不加第三个取值')
        # 旧库补列前写入的行是 NULL，读取侧一律当 False（不是 None、不是报错）。
        self.database.conn.execute(
            "UPDATE interlude_sticker SET guessed = NULL WHERE assetId = 'sticker-guessed-1'",
        )
        self.database.conn.commit()
        stale = {entry['assetId']: entry for entry in _run(self.api.stickers())['items']}
        self.assertIs(stale['sticker-guessed-1']['guessed'], False)

    def test_pagination_window_and_truncated_flag(self):
        for index in range(5):
            self._insert('sticker-%d' % index)
        first = _run(self.api.stickers(limit=2, offset=0))
        self.assertEqual(first['total'], 5)
        self.assertEqual(len(first['items']), 2)
        self.assertFalse(first['truncated'], '5 条远没到窗口上限')
        second = _run(self.api.stickers(limit=2, offset=2))
        self.assertEqual(len(second['items']), 2)
        self.assertEqual(
            [item['assetId'] for item in first['items']],
            [item['assetId'] for item in first['items']],
        )
        self.assertEqual(
            set(item['assetId'] for item in first['items'])
            & set(item['assetId'] for item in second['items']),
            set(), '两页不该重叠',
        )

    def test_filters_are_applied_server_side(self):
        self._insert('sticker-a', description='一只猫')
        self._insert('manual-b', description='一张狗', source='manual')
        self._insert('sticker-c', description='', status='pending')
        self._insert('sticker-d', description='被停用的', status='disabled')
        self.assertEqual(_run(self.api.stickers(source='manual'))['total'], 1)
        self.assertEqual(_run(self.api.stickers(status='pending'))['total'], 1)
        self.assertEqual(_run(self.api.stickers(status='disabled'))['total'], 1)
        self.assertEqual(_run(self.api.stickers(query='猫'))['total'], 1)
        self.assertEqual(_run(self.api.stickers(query='sticker-'))['total'], 3)
        counts = _run(self.api.stickers())['counts']
        self.assertEqual(counts['total'], 4)
        self.assertEqual(counts['active'], 2)
        self.assertEqual(counts['disabled'], 1)

    def test_source_falls_back_to_the_asset_id_namespace_for_old_rows(self):
        """旧库没有 `source` 列时按 id 前缀判来源（不回填旧数据）。"""
        row = self._row('sticker-legacy-1')
        row['source'] = None
        row['id'] = self.database.insert('interlude_sticker', row)
        self.assertEqual(_run(self.api.stickers())['items'][0]['source'], 'auto')
        row2 = self._row('manual-legacy-2')
        row2['source'] = None
        row2['id'] = self.database.insert('interlude_sticker', row2)
        items = {item['assetId']: item for item in _run(self.api.stickers())['items']}
        self.assertEqual(items['manual-legacy-2']['source'], 'manual')

    # ---- 原图 ----

    def test_file_endpoint_returns_a_path_inside_the_library(self):
        self._insert('sticker-abc-1')
        path = _run(self.api.sticker_file('sticker-abc-1'))
        self.assertEqual(path, os.path.join(self.root, 'sticker-abc-1.png'))
        self.assertTrue(os.path.isfile(path))

    def test_file_endpoint_rejects_unknown_and_illegal_ids(self):
        with self.assertRaises(ConsoleError):
            _run(self.api.sticker_file(''))
        with self.assertRaises(ConsoleError):
            _run(self.api.sticker_file('nope'))
        with self.assertRaises(ConsoleError):
            _run(self.api.sticker_file('x' * 300))

    def test_file_endpoint_refuses_to_escape_the_library(self):
        """被改坏的 `filePath` 不许读出库外的文件（basename + 目录归属双保险）。"""
        secret = os.path.join(self.tmp, 'secret.txt')
        with open(secret, 'w', encoding='utf-8') as handle:
            handle.write('top secret')
        row = self._row('sticker-escape')
        row['filePath'] = '../secret.txt'
        self.database.insert('interlude_sticker', row)
        with self.assertRaises(FileNotFoundError):
            _run(self.api.sticker_file('sticker-escape'))

    def test_file_endpoint_missing_file_is_a_404_shape(self):
        self._insert('sticker-gone')
        os.remove(os.path.join(self.root, 'sticker-gone.png'))
        with self.assertRaises(FileNotFoundError):
            _run(self.api.sticker_file('sticker-gone'))

    # ---- 取图路由的两条分支（`inline=1` 的 JSON 信封 vs 缺省的图片字节） ----
    #
    # 这几条跑的是**真实** `main.page_console_sticker_file`（宿主响应桩），不是直接调
    # `ConsoleApi`：两条分支的差别在**响应通道**上（JSON vs blob），只调 API 方法断言不到。
    # 形状冻结在 `docs/PORTING_NOTES.md` §45.8，前端 `src/sticker-images.ts` 按它接。

    def _page(self, **query):
        """按查询参数跑一次 `page_console_sticker_file`（返回宿主响应桩）。"""
        self.addCleanup(_install_web_request(query=query))
        plugin = _make_plugin({})
        plugin._console = self.api
        return _run(plugin.page_console_sticker_file())

    def test_inline_returns_a_base64_envelope_that_round_trips_the_file(self):
        """`inline=1`：四字段信封 + `base64` 解回**与原文件逐字节相同**的内容。"""
        self._insert('sticker-abc-1')
        response = self._page(assetId='sticker-abc-1', inline='1')
        self.assertEqual(response.status_code, 200)
        payload = response.payload
        self.assertEqual(set(payload), {'assetId', 'mimeType', 'size', 'base64'},
                         '信封字段是冻结的（前端按这四个键解析）')
        self.assertEqual(payload['assetId'], 'sticker-abc-1')
        self.assertEqual(payload['mimeType'], 'image/png')
        self.assertEqual(payload['size'], len(_PNG_BYTES))
        self.assertFalse(payload['base64'].startswith('data:'), '`base64` 不含 data: 前缀')
        with open(os.path.join(self.root, 'sticker-abc-1.png'), 'rb') as handle:
            on_disk = handle.read()
        self.assertEqual(base64.b64decode(payload['base64']), on_disk, '必须逐字节相同')
        self.assertEqual(payload['size'], len(on_disk))

    def test_the_envelope_survives_the_host_pages_data_unwrap(self):
        """**真机那条链的最后一跳**：信封顶层不许有 `data` 键（宿主 bridge 会整包取走）。

        宿主父页面 `PluginPagePage-*.js` 的 `api:get` 分支递进 iframe 的值是
        `response.data?.data ?? response.data`，而 `json_response()` 是**扁平**的
        （`astrbot/api/web.py::json_response` 直接 `JSONResponse(data)`，不套
        `{status,data}`；`error_response` 才是手写那层信封）。所以信封里那个叫 `data`
        的键（装 base64）会被当成"整包"取走：iframe 只收到一条裸 base64 字符串，前端
        `parseImageEnvelope`（只认对象信封 / `data:` URL）判 `null` → 整屏缩略图全挂，
        而后端是 200、日志一个字都没有（真机 v1.9.1 / v1.9.2 的现场）。

        变异保护：键名改回 `data`（或再加一个 `data`）→ 本用例立刻红。
        """
        self._insert('sticker-abc-1')
        payload = self._page(assetId='sticker-abc-1', inline='1').payload
        self.assertNotIn('data', payload, '`data` 会被宿主 bridge 当成整包取走')
        # 逐字照宿主那一跳：有顶层 `data` 就取它，否则原样递整个信封。
        delivered = payload['data'] if 'data' in payload else payload
        self.assertIsInstance(delivered, dict, 'iframe 收到的必须是信封对象，不是裸 base64')
        self.assertIsInstance(delivered['base64'], str)
        with open(os.path.join(self.root, 'sticker-abc-1.png'), 'rb') as handle:
            self.assertEqual(base64.b64decode(delivered['base64']), handle.read())

    def test_without_inline_the_response_is_still_the_raw_byte_stream(self):
        """反向用例：缺省 / `inline=0` / `inline=` 一律**还是字节流**，不是 JSON 信封。"""
        self._insert('sticker-abc-1')
        expected = os.path.join(self.root, 'sticker-abc-1.png')
        for query in ({'assetId': 'sticker-abc-1'}, {'assetId': 'sticker-abc-1', 'inline': '0'},
                      {'assetId': 'sticker-abc-1', 'inline': ''}):
            with self.subTest(query=query):
                response = self._page(**query)
                self.assertEqual(response.status_code, 200)
                self.assertIsNone(response.payload, '老客户端拿到的不能是 JSON')
                self.assertEqual(response.path, expected)
                self.assertEqual(response.content_type, 'image/png', 'Content-Type 仍是图片')
                self.assertEqual(response.filename, 'sticker-abc-1.png')
                # `file_response` 是宿主侧的 blob 通道：body 就是这个路径的字节。
                with open(response.path, 'rb') as handle:
                    self.assertEqual(handle.read(), _PNG_BYTES)

    def test_inline_keeps_the_same_400_and_404_wording(self):
        """400 / 404 的措辞两条分支**逐字一致**（同一批校验、同一批 `except`）。"""
        cases = (
            ({'assetId': ''}, '缺少 assetId'),
            ({'assetId': 'nope'}, '找不到这条素材：nope'),
            ({'assetId': 'x' * 300}, 'assetId 过长'),
        )
        for params, message in cases:
            with self.subTest(params=params):
                plain = self._page(**params)
                inline = self._page(inline='1', **params)
                self.assertEqual(plain.status_code, 400, params)
                self.assertEqual(inline.status_code, 400, params)
                self.assertEqual(plain.payload['message'], message)
                self.assertEqual(plain.payload['message'], inline.payload['message'])
        self._insert('sticker-gone')
        os.remove(os.path.join(self.root, 'sticker-gone.png'))
        plain = self._page(assetId='sticker-gone')
        inline = self._page(assetId='sticker-gone', inline='1')
        self.assertEqual((plain.status_code, inline.status_code), (404, 404))
        self.assertEqual(plain.payload['message'], inline.payload['message'])
        # 404 不再是一句"可能已被删除"的空壳（§56）：短提示里必须能看到**它找的是哪儿**
        # （面板只有 message 可看——宿主 bridge 把 `data` 丢了）。
        expected = os.path.abspath(os.path.join(self.root, 'sticker-gone.png'))
        self.assertIn(
            console_module.shorten_path(expected), plain.payload['message'],
            '面板那句提示要能看出找的是哪儿',
        )
        self.assertLess(len(plain.payload['message']), 160, '只留状态词与必要信息，不是小作文')
        self.assertEqual(plain.payload['data'], inline.payload['data'],
                         '两条分支的诊断逐字一致')

    def test_inline_refuses_huge_files_without_reading_them(self):
        """超上限 → 400；**一个字节都不读进内存**（防轰炸：先判体积再读）。"""
        path = os.path.join(self.root, 'sticker-huge.png')
        with open(path, 'wb') as handle:
            handle.write(_PNG_BYTES)
            # 稀疏文件：体积够大但不真占盘（这里要的是 `getsize` 的值）。
            handle.truncate(console_module.STICKER_INLINE_MAX_BYTES + 1)
        self.database.insert('interlude_sticker', self._row('sticker-huge'))
        opened: list[str] = []
        real_open = open

        def spy(file, *args, **kwargs):
            opened.append(str(file))
            return real_open(file, *args, **kwargs)

        with mock.patch('builtins.open', spy):
            response = self._page(assetId='sticker-huge', inline='1')
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(response.payload['data'], '拒绝时不许回任何内容')
        self.assertIn('太大', response.payload['message'])
        self.assertNotIn(path, opened, '先判体积：超限时不该打开文件')

    def test_library_escape_and_unknown_ids_are_refused_on_both_branches(self):
        """越界防护（被改坏的 `filePath`）两条分支都要挡住——库外文件一个字都不许回。"""
        secret = os.path.join(self.tmp, 'secret.txt')
        with open(secret, 'w', encoding='utf-8') as handle:
            handle.write('top secret')
        row = self._row('sticker-escape')
        row['filePath'] = '../secret.txt'
        self.database.insert('interlude_sticker', row)
        plain = self._page(assetId='sticker-escape')
        inline = self._page(assetId='sticker-escape', inline='1')
        self.assertEqual((plain.status_code, inline.status_code), (404, 404))
        self.assertEqual(plain.payload['message'], inline.payload['message'])
        # 404 现在带诊断（"找的是哪儿"），但诊断里**一个字都不许提到库外**：
        # 每个候选都必须落在库根里，`../secret.txt` 绝不能被当成候选路径。
        for payload in (plain.payload, inline.payload):
            tried = payload['data']['tried']
            self.assertTrue(tried, '越界也要说清找过哪儿')
            for candidate in tried:
                self.assertTrue(candidate.startswith(os.path.abspath(self.root) + os.sep),
                                candidate)
            self.assertNotIn(os.path.abspath(secret), tried, '库外路径不许进候选')
        self.assertNotIn('top secret', json.dumps(inline.payload, ensure_ascii=False))
        self.assertIsNone(inline.payload.get('path'), '信封里也不许泄露库外路径')

    # ---- 改描述 / 名字 / 停用 ----

    def test_description_edit_wins_over_the_automatic_one_and_enters_the_catalog(self):
        """接线用例：改描述 → 写回 → **下一次 payload 里的目录文本真的变了**。"""
        row = self._insert('sticker-abc-1', description='模型写的旧描述')
        _run(self.service.refresh_sticker_catalog())
        before = _run(self.service.sticker_catalog_for_session({'platform': 'onebot'}))
        self.assertEqual([item['description'] for item in before], ['模型写的旧描述'])

        payload = _run(self.api.update_sticker({
            'assetId': 'sticker-abc-1', 'description': '她手写的：一只挥手的猫',
        }))
        self.assertEqual(payload['changed'], ['description'])
        self.assertEqual(payload['item']['description'], '她手写的：一只挥手的猫')
        self.assertTrue(payload['item']['manual'])

        stored = self.database.all('interlude_sticker', {'assetId': 'sticker-abc-1'})[0]
        self.assertEqual(stored['description'], '她手写的：一只挥手的猫')
        self.assertTrue(stored['descriptionManual'], '手工标记必须落库（重扫据此跳过）')
        self.assertEqual(stored['status'], 'active')

        after = _run(self.service.sticker_catalog_for_session({'platform': 'onebot'}))
        self.assertEqual([item['description'] for item in after], ['她手写的：一只挥手的猫'])
        self.assertNotEqual(before, after, '目录文本必须随描述变化')

    def test_manual_description_survives_a_full_rescan(self):
        """手改 → 立刻重扫：描述不得被视觉模型顶掉（§45.2）。"""
        self._insert('sticker-abc-1', description='模型写的')
        _run(self.api.update_sticker({'assetId': 'sticker-abc-1', 'description': '人的描述'}))

        async def _fake_describe(asset, payload, config=None):
            raise AssertionError('手工描述的素材不该再被描述')

        self.service.describe_sticker_asset = _fake_describe
        _run(self.service.scan_sticker_library())
        stored = self.database.all('interlude_sticker', {'assetId': 'sticker-abc-1'})[0]
        self.assertEqual(stored['description'], '人的描述')
        self.assertEqual(stored['status'], 'active')

    def test_restore_hands_the_description_back_to_the_model(self):
        self._insert('sticker-abc-1')
        _run(self.api.update_sticker({'assetId': 'sticker-abc-1', 'description': '人的描述'}))
        payload = _run(self.api.restore_sticker_description({'assetId': 'sticker-abc-1'}))
        self.assertEqual(payload['changed'], ['description'])
        self.assertFalse(payload['item']['manual'])
        stored = self.database.all('interlude_sticker', {'assetId': 'sticker-abc-1'})[0]
        self.assertEqual(stored['description'], '')
        self.assertEqual(stored['status'], 'pending')

    def test_name_edit_round_trips(self):
        self._insert('sticker-abc-1')
        payload = _run(self.api.update_sticker({'assetId': 'sticker-abc-1', 'name': '坏笑的猫'}))
        self.assertEqual(payload['changed'], ['name'])
        self.assertEqual(payload['item']['name'], '坏笑的猫')
        self.assertEqual(
            self.database.all('interlude_sticker', {'assetId': 'sticker-abc-1'})[0]['name'],
            '坏笑的猫',
        )

    def test_disabled_toggles_out_of_the_model_catalog(self):
        self._insert('sticker-abc-1')
        _run(self.service.refresh_sticker_catalog())
        self.assertEqual(len(self.service.sticker_catalog), 1)
        payload = _run(self.api.update_sticker({'assetId': 'sticker-abc-1', 'disabled': True}))
        self.assertTrue(payload['item']['disabled'])
        self.assertEqual(
            self.database.all('interlude_sticker', {'assetId': 'sticker-abc-1'})[0]['status'],
            'disabled',
        )
        self.assertEqual(self.service.sticker_catalog, [], '停用后立刻退出模型目录')
        _run(self.api.update_sticker({'assetId': 'sticker-abc-1', 'disabled': False}))
        self.assertEqual(len(self.service.sticker_catalog), 1, '启用后回到目录（有描述 → active）')

    def test_disabled_asset_is_not_resurrected_by_a_rescan(self):
        """重扫不许把用户刻意停用的素材复活成 pending（那是"改了没反应"）。"""
        self._insert('sticker-abc-1', description='')
        _run(self.api.update_sticker({'assetId': 'sticker-abc-1', 'disabled': True}))
        _run(self.service.scan_sticker_library())
        stored = self.database.all('interlude_sticker', {'assetId': 'sticker-abc-1'})[0]
        self.assertEqual(stored['status'], 'disabled')

    def test_fields_outside_the_whitelist_are_rejected(self):
        self._insert('sticker-abc-1')
        for body in (
            {'assetId': 'sticker-abc-1', 'status': 'active'},
            {'assetId': 'sticker-abc-1', 'aliases': ['x']},
            {'assetId': 'sticker-abc-1', 'hash': 'x'},
            {'assetId': 'sticker-abc-1', 'filePath': '/etc/passwd'},
            {'assetId': 'sticker-abc-1', 'description': 'x', 'embedding': [1.0]},
        ):
            with self.subTest(body=body):
                with self.assertRaises(ConsoleError) as caught:
                    _run(self.api.update_sticker(body))
                self.assertIn('只能修改', str(caught.exception))
        # 一个字段都没给也算错误（不是"什么都没改"的静默成功）。
        with self.assertRaises(ConsoleError):
            _run(self.api.update_sticker({'assetId': 'sticker-abc-1'}))

    def test_write_operations_reject_an_unknown_asset_id(self):
        for coro in (
            self.api.update_sticker({'assetId': 'nope', 'description': 'x'}),
            self.api.delete_sticker({'assetId': 'nope'}),
            self.api.restore_sticker_description({'assetId': 'nope'}),
        ):
            with self.assertRaises(ConsoleError):
                _run(coro)
        for body in ({}, {'assetId': ''}, {'assetId': None}):
            with self.assertRaises(ConsoleError):
                _run(self.api.update_sticker(body))

    def test_invalid_values_are_rejected_before_writing(self):
        self._insert('sticker-abc-1')
        with self.assertRaises(ConsoleError):
            _run(self.api.update_sticker({'assetId': 'sticker-abc-1', 'description': 'x' * 5000}))
        with self.assertRaises(ConsoleError):
            _run(self.api.update_sticker({'assetId': 'sticker-abc-1', 'name': 'x' * 500}))
        with self.assertRaises(ConsoleError):
            _run(self.api.update_sticker({'assetId': 'sticker-abc-1', 'disabled': 'yes'}))
        with self.assertRaises(ConsoleError):
            _run(self.api.update_sticker({'assetId': 'sticker-abc-1', 'description': None}))
        # 一次都没写下去。
        stored = self.database.all('interlude_sticker', {'assetId': 'sticker-abc-1'})[0]
        self.assertEqual(stored['description'], '一只挥手的猫')
        self.assertFalse(stored['descriptionManual'])

    # ---- 删除 ----

    def test_delete_defaults_to_marking_only(self):
        self._insert('sticker-abc-1')
        payload = _run(self.api.delete_sticker({'assetId': 'sticker-abc-1'}))
        self.assertFalse(payload['purged'])
        self.assertFalse(payload['deletedFile'])
        self.assertTrue(os.path.isfile(os.path.join(self.root, 'sticker-abc-1.png')),
                        '默认不许删文件')
        stored = self.database.all('interlude_sticker', {'assetId': 'sticker-abc-1'})[0]
        self.assertEqual(stored['status'], 'missing')
        self.assertEqual(self.service.sticker_catalog, [])

    def test_delete_with_purge_removes_the_file(self):
        self._insert('sticker-abc-1')
        path = os.path.join(self.root, 'sticker-abc-1.png')
        payload = _run(self.api.delete_sticker({'assetId': 'sticker-abc-1', 'purge': True}))
        self.assertTrue(payload['purged'])
        self.assertTrue(payload['deletedFile'])
        self.assertFalse(os.path.isfile(path))
        # 行仍在（留痕：她曾经有过这个表情），但不再是 active。
        stored = self.database.all('interlude_sticker', {'assetId': 'sticker-abc-1'})
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]['status'], 'missing')

    def test_delete_rejects_a_non_boolean_purge(self):
        self._insert('sticker-abc-1')
        with self.assertRaises(ConsoleError):
            _run(self.api.delete_sticker({'assetId': 'sticker-abc-1', 'purge': 'yes'}))
        self.assertTrue(os.path.isfile(os.path.join(self.root, 'sticker-abc-1.png')))

    # ---- 重扫 ----

    def test_rescan_reports_what_it_found(self):
        scan_calls: list[Any] = []

        async def scan() -> None:
            scan_calls.append(1)

        self.service.scan_sticker_library = scan
        payload = _run(self.api.rescan_stickers({}))
        self.assertTrue(payload['scanned'])
        self.assertEqual(len(scan_calls), 1)
        self.assertIn('assets', payload)
        self.assertIn('added', payload)

    def test_rescan_refuses_when_the_library_is_off(self):
        self.bridge.config = {'stickers': {'enabled': False, 'directory': 'stickers'}}
        self.service.config = {'stickers': {'enabled': False, 'directory': 'stickers'}}
        with self.assertRaises(ConsoleError) as caught:
            _run(self.api.rescan_stickers({}))
        self.assertIn('未启用', str(caught.exception))

    def test_rescan_without_a_service_is_a_clear_error(self):
        self.bridge.service = None
        with self.assertRaises(ConsoleError):
            _run(self.api.rescan_stickers({}))

    def test_reads_survive_a_missing_service(self):
        """服务层没起来时列表仍然是空壳 + 配置里的根目录（面板打得开）。"""
        self.bridge.service = None
        payload = _run(self.api.stickers())
        self.assertEqual(payload['items'], [])
        self.assertTrue(payload['root'].endswith('stickers'))

    # ---- v1.9.1 §54：收藏 / 扫盘 / 读图**同一个根**，且落盘失败不许建档 ----

    def _collected_row(self, asset_id: str) -> dict[str, Any]:
        rows = self.database.all('interlude_sticker', {'assetId': asset_id})
        self.assertEqual(len(rows), 1, '库里应当正好有一行')
        return rows[0]

    def _set_directory(self, directory: str) -> None:
        """把服务与 bridge 的 `stickers.directory` 同时换掉（生产里两处读同一份配置）。"""
        section = {'enabled': True, 'directory': directory}
        self.service.config = {'stickers': dict(section)}
        self.service.cached_sticker_config = None
        self.bridge.config = {'stickers': dict(section)}

    def test_a_collected_sticker_is_on_disk_and_comes_back_through_the_console(self):
        """**端到端**：真字节 → 收藏 → 控制台取图拿到同样的字节。

        这条钉的是真机的那个症结：库里有档、缩略图 404。三步缺一不可——
        ① 文件真的在盘上（写出后 `isfile` 且字节数相等）；
        ② DB 行的 `filePath` 指向它；③ 控制台按**同一个根**把它读出来。
        """
        asset = _run(self.service.store_collected_sticker(_PNG_BYTES, 'sticker'))
        self.assertIsNotNone(asset, '落盘与建档都成功才回资产行')
        asset_id = asset['assetId']
        digest = hashlib.sha256(_PNG_BYTES).hexdigest()
        row = self._collected_row(asset_id)
        # ① 文件在盘上，大小 > 0 且**逐字节相等**
        target = os.path.join(self.root, row['filePath'].replace('/', os.sep))
        self.assertTrue(os.path.isfile(target), '收藏必须真的落盘')
        self.assertEqual(os.path.getsize(target), len(_PNG_BYTES))
        # ② 行的 filePath 指向它（相对根的路径，不是 basename）
        self.assertEqual(row['filePath'], 'collected/%s.png' % digest[:32])
        # ③ 控制台取图入口拿到同样的字节（路径分支 + inline 信封分支）
        path = _run(self.api.sticker_file(asset_id))
        self.assertEqual(os.path.abspath(path), os.path.abspath(target))
        with open(path, 'rb') as handle:
            self.assertEqual(handle.read(), _PNG_BYTES)
        envelope = _run(self.api.sticker_file_inline(asset_id))
        self.assertEqual(base64.b64decode(envelope['base64']), _PNG_BYTES)
        self.assertEqual(envelope['size'], len(_PNG_BYTES))

    def test_a_collected_sticker_in_a_subdirectory_is_not_read_as_a_basename(self):
        """反向：把相对路径削成 basename 就取不到图——这正是真机 404 的写法。

        `collected/x.png` 落在 `root/collected/x.png`；按 basename 拼成 `root/x.png`
        必然不存在。这条同时钉住"分组目录不许被削掉"。

        ⚠️ 断言必须看 `found`（命中的是**行里那条路径**，不是兜底找回）：§56 的哈希
        兜底正好能救回 basename 拼法——只看"取到了图"的话，拼法退化了也照样绿。
        """
        asset = _run(self.service.store_collected_sticker(_PNG_BYTES, 'sticker'))
        row = self._collected_row(asset['assetId'])
        self.assertIn('/', row['filePath'], '自动收藏落在 collected/ 子目录里')
        self.assertFalse(
            os.path.exists(os.path.join(self.root, os.path.basename(row['filePath']))),
            '根目录下没有这个文件——basename 拼法必然 404',
        )
        path, diagnostics = _run(self.api._sticker_file_detail(asset['assetId']))
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(
            diagnostics['found'], 'filePath',
            '分组目录必须真的参与拼路径：靠兜底找回说明拼法已经退化了',
        )

    def test_a_failed_write_never_creates_a_lookalike_row(self):
        """落盘失败 → **不建档** + 一条可行动 warn（不许出现"库里有、盘上没有"）。"""
        blocker = os.path.join(self.tmp, 'blocker')
        with open(blocker, 'w', encoding='utf-8') as handle:
            handle.write('not a directory')
        # `blocker/stickers` 的父级是普通文件 → `makedirs` 必失败（跨平台确定，不靠权限位）。
        self._set_directory('blocker/stickers')
        asset = _run(self.service.store_collected_sticker(_PNG_BYTES, 'sticker'))
        self.assertIsNone(asset)
        self.assertEqual(self.database.count('interlude_sticker'), 0, '失败即不许建档')
        warnings = [
            entry for entry in self.service.reports
            if '表情包收藏落盘失败' in str(entry[2] if len(entry) > 2 else entry)
        ]
        self.assertEqual(len(warnings), 1, '失败必须留一条 warn')
        message = warnings[0][2] % warnings[0][3:]
        self.assertIn('未建档', message)
        self.assertIn('collected/', message, '要说清是哪个文件')
        self.assertTrue('错误=' in message and len(message) > 20, '要带可行动的错误原文')

    def test_a_partial_write_is_treated_as_a_failure(self):
        """写了但字节数不对（配额 / 同步盘截断）也**不算成功**：不建档 + warn。"""
        real_getsize = os.path.getsize
        with mock.patch('os.path.getsize', lambda path: 1):
            asset = _run(self.service.store_collected_sticker(_PNG_BYTES, 'sticker'))
        self.assertIsNone(asset)
        self.assertEqual(self.database.count('interlude_sticker'), 0)
        self.assertTrue(
            any('落盘不完整' in str(entry[2] if len(entry) > 2 else entry)
                for entry in self.service.reports),
            '字节数不符要明说，不许静默成功',
        )
        self.assertEqual(real_getsize, os.path.getsize, '补丁必须还原')

    def test_a_missing_library_directory_is_created_on_write(self):
        """目录不存在时自建（`parents=True` 的那一层）：收藏写完仍然可读。"""
        self._set_directory('deep/nested/stickers')
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'deep')))
        asset = _run(self.service.store_collected_sticker(_PNG_BYTES, 'sticker'))
        self.assertIsNotNone(asset)
        row = self._collected_row(asset['assetId'])
        target = os.path.join(self.tmp, 'deep', 'nested', 'stickers', row['filePath'])
        self.assertTrue(os.path.isfile(target), '不存在的目录要被建出来')
        self.assertEqual(_run(self.api.sticker_file(asset['assetId'])), os.path.abspath(target))

    def test_purging_a_collected_sticker_really_removes_the_file(self):
        """同一个路径判据的另一半：`purge` 删的必须是**子目录里那个真文件**。

        按 basename 拼的话，`os.path.isfile(root/<name>)` 恒为假 → `deletedFile=False`
        ——"删了、其实没删"（又一次静默）。
        """
        asset = _run(self.service.store_collected_sticker(_PNG_BYTES, 'sticker'))
        row = self._collected_row(asset['assetId'])
        target = os.path.join(self.root, row['filePath'].replace('/', os.sep))
        self.assertTrue(os.path.isfile(target))
        payload = _run(self.api.delete_sticker({'assetId': asset['assetId'], 'purge': True}))
        self.assertTrue(payload['deletedFile'], '子目录里的文件也要真的删掉')
        self.assertFalse(os.path.isfile(target))
        self.assertEqual(self._collected_row(asset['assetId'])['status'], 'missing', '行留着留痕')

    def test_a_scanned_file_in_a_group_directory_is_readable_by_the_console(self):
        """扫盘（`scan_sticker_library`）收进来的图，控制台按**同一个根**读得到。"""
        os.makedirs(os.path.join(self.root, 'Cat'), exist_ok=True)
        scanned = os.path.join(self.root, 'Cat', 'paw.png')
        with open(scanned, 'wb') as handle:
            handle.write(_PNG_BYTES)
        _run(self.service.scan_sticker_library())
        rows = {row['filePath']: row for row in self.database.all('interlude_sticker', {})}
        self.assertIn('Cat/paw.png', rows, '分组目录里的图要能收进库')
        asset_id = rows['Cat/paw.png']['assetId']
        path = _run(self.api.sticker_file(asset_id))
        self.assertEqual(os.path.abspath(path), os.path.abspath(scanned))
        envelope = _run(self.api.sticker_file_inline(asset_id))
        self.assertEqual(base64.b64decode(envelope['base64']), _PNG_BYTES)

    def test_collect_scan_and_console_read_compute_one_root(self):
        """三处（收藏写入 / 扫盘 / 控制台取图）的根**相等**，而且只由一处判据算出。

        断言的是"与 `helpers.sticker_root_from()` 算出来的**同一个值**"，不是两处
        字面量互相比较——两处一起写错时，比较相等是抓不到的。
        """
        expected = sticker_root_from(self.tmp, 'stickers')
        self.assertEqual(self.service.sticker_library_root(), expected)
        self.assertEqual(self.api._sticker_root(), expected)
        self.assertEqual(_run(self.api.stickers())['root'], expected)
        # 默认目录也只有一个字面量（schema / CONFIG_DEFAULTS / 两侧回落都照它读）。
        self.assertEqual(STICKER_DEFAULT_DIRECTORY, 'data/hds-interlude/stickers')
        self.service.config = {}
        self.service.cached_sticker_config = None
        self.assertEqual(
            self.service.sticker_library_root(),
            sticker_root_from(self.tmp, STICKER_DEFAULT_DIRECTORY),
        )

    def test_an_unresolvable_data_directory_is_visible_and_never_writes(self):
        """拿不到插件数据目录 → 可见 warn，**不写盘、不建档**（不许静默回落到 cwd）。"""
        self.service.ctx = InterludeContext()
        self.assertEqual(getattr(self.service.ctx, 'base_dir', ''), '', '夹具：空的 ctx')
        self.assertEqual(self.service.sticker_library_root(), '')
        self.assertTrue(
            any('表情库根目录不可用' in str(entry[2] if len(entry) > 2 else entry)
                for entry in self.service.reports),
            '这是能力缺失，必须可见',
        )
        asset = _run(self.service.store_collected_sticker(_PNG_BYTES, 'sticker'))
        self.assertIsNone(asset)
        self.assertEqual(self.database.count('interlude_sticker'), 0)

    # ---- §56：取图失败必须**自报家门**（诊断），路径对不上按哈希兜底 ----

    def _page_diagnostics(self, asset_id: str) -> dict[str, Any]:
        """跑一次取图路由，取出 404 的**诊断**（顺带钉住它真的是 404）。"""
        response = self._page(assetId=asset_id, inline='1')
        self.assertEqual(response.status_code, 404, response.payload)
        return response.payload['data']

    def test_a_row_whose_path_lost_its_directory_is_recovered_by_hash(self):
        """② `filePath` 指向不存在的地方，但库根下有同哈希文件 → 兜底找回 + 可见 info。

        真机形状：行里只记着 `<hash>.jpg`（分组目录那一段丢了），而文件确实在
        `collected/<hash>.jpg`——按行里的路径拼就是 404，按**内容哈希**扫库根就能找到。
        """
        digest = hashlib.sha256(_PNG_BYTES).hexdigest()
        on_disk = os.path.join(self.root, 'collected', '%s.jpg' % digest[:32])
        with open(on_disk, 'wb') as handle:
            handle.write(_PNG_BYTES)
        self.database.insert('interlude_sticker', self._row(
            'sticker-hash-1', filePath='%s.jpg' % digest[:32], hash=digest,
        ))
        captured: list[tuple[str, str]] = []
        set_log_sink(lambda level, text: captured.append((level, text)))
        self.addCleanup(set_log_sink, None)
        path, diagnostics = _run(self.api._sticker_file_detail('sticker-hash-1'))
        self.assertEqual(os.path.abspath(path), os.path.abspath(on_disk))
        self.assertEqual(diagnostics['found'], 'hash')
        self.assertEqual(diagnostics['reason'], 'hash-recovered')
        self.assertTrue(diagnostics['isfile'])
        self.assertEqual(diagnostics['size'], len(_PNG_BYTES))
        self.assertIn(os.path.abspath(on_disk), diagnostics['tried'])
        # **可见 info**：自我修复不许静默（用户得知道"记录路径对不上、靠内容找回来了"）。
        self.assertTrue(
            any('按哈希找回' in text for _level, text in captured),
            '兜底找回必须留一条可见 info：%r' % (captured,),
        )
        # 字节分支与信封分支都要读得出来（同一个 `_sticker_file_detail`）。
        self.assertEqual(_run(self.api.sticker_file('sticker-hash-1')),
                         os.path.abspath(on_disk))
        envelope = _run(self.api.sticker_file_inline('sticker-hash-1'))
        self.assertEqual(base64.b64decode(envelope['base64']), _PNG_BYTES)

    def test_a_readable_row_never_goes_through_the_hash_fallback(self):
        """反向：行里的路径**就在盘上**时不许走兜底（也不许刷"按哈希找回"）。"""
        digest = hashlib.sha256(_PNG_BYTES).hexdigest()
        on_disk = os.path.join(self.root, 'collected', '%s.jpg' % digest[:32])
        with open(on_disk, 'wb') as handle:
            handle.write(_PNG_BYTES)
        self.database.insert('interlude_sticker', self._row(
            'sticker-plain-1', filePath='collected/%s.jpg' % digest[:32], hash=digest,
        ))
        captured: list[tuple[str, str]] = []
        set_log_sink(lambda level, text: captured.append((level, text)))
        self.addCleanup(set_log_sink, None)
        _path, diagnostics = _run(self.api._sticker_file_detail('sticker-plain-1'))
        self.assertEqual(diagnostics['found'], 'filePath')
        self.assertEqual(diagnostics['reason'], '')
        self.assertFalse(
            any('按哈希找回' in text for _level, text in captured),
            '主路径就命中时不许打兜底日志：%r' % (captured,),
        )

    def test_neither_side_present_reports_the_root_and_every_tried_path(self):
        """③ 两边都没有 → 诊断里能读到**库根**与**尝试过的绝对路径**（不许静默空壳）。"""
        self._insert('sticker-lost-1')
        os.remove(os.path.join(self.root, 'sticker-lost-1.png'))
        diagnostics = self._page_diagnostics('sticker-lost-1')
        self.assertEqual(diagnostics['root'], os.path.abspath(self.root))
        self.assertEqual(
            diagnostics['tried'], [os.path.abspath(os.path.join(self.root, 'sticker-lost-1.png'))],
        )
        self.assertFalse(diagnostics['isfile'])
        self.assertIsNone(diagnostics['size'])
        self.assertIsNone(diagnostics['found'])
        self.assertEqual(diagnostics['reason'], 'missing')
        # 两个来源的当前值都要在："写成 A、读成 B"一眼可见。
        self.assertEqual(diagnostics['dataDir'], self.bridge.data_dir)
        self.assertEqual(diagnostics['directory'], 'stickers')
        self.assertEqual(diagnostics['filePath'], 'sticker-lost-1.png')
        self.assertEqual(diagnostics['relative'], 'sticker-lost-1.png')
        self.assertEqual(diagnostics['assetId'], 'sticker-lost-1')

    def test_an_absolute_file_path_inside_the_library_is_readable(self):
        """`filePath` 是**绝对路径**（继承来的旧数据）也要能读——两种写法都认。"""
        self._insert('sticker-abs-1')
        absolute = os.path.join(self.root, 'sticker-abs-1.png')
        self._set_file_path('sticker-abs-1', absolute)
        self.assertEqual(
            _run(self.api.sticker_file('sticker-abs-1')), os.path.abspath(absolute),
        )

    def test_an_absolute_file_path_outside_the_library_is_still_refused(self):
        """反向：绝对路径**在库外**照样不许读（文件真的存在也不行）。"""
        outside = os.path.join(self.tmp, 'outside.png')
        with open(outside, 'wb') as handle:
            handle.write(_PNG_BYTES)
        self.database.insert('interlude_sticker', self._row('sticker-outside', filePath=outside))
        with self.assertRaises(FileNotFoundError):
            _run(self.api.sticker_file('sticker-outside'))

    def test_a_file_path_relative_to_the_data_directory_is_readable(self):
        """第三种写法：相对**插件数据目录**（多带一截库根前缀）也要能读。

        生产里库根就落在数据目录下面（`<data>/data/hds-interlude/stickers`），
        旧写方完全可能把 `data/hds-interlude/stickers/collected/x.jpg` 整条记进去。
        """
        self.bridge.data_dir = self.tmp
        self.assertEqual(self.api._sticker_root(), os.path.abspath(self.root))
        self._insert('sticker-nested-1', filePath='stickers/sticker-nested-1.png')
        self.assertEqual(
            _run(self.api.sticker_file('sticker-nested-1')),
            os.path.abspath(os.path.join(self.root, 'sticker-nested-1.png')),
        )

    def test_an_unknown_root_is_a_diagnostic_not_a_crash(self):
        """根拿不到（数据目录未知）→ **带诊断的 404**，不是 `commonpath` 的 ValueError 500。

        旧实现走 `os.path.commonpath(['/abs', ''])`（绝对 + 相对混用）会抛 `ValueError`：
        异常分支兜不住 → 500，前端只看到"取不到图"（真机上就是这么瞎的）。
        """
        self._insert('sticker-noroot-1')
        self.service.ctx = InterludeContext()
        self.bridge.data_dir = ''
        self.assertEqual(self.api._sticker_root(), '', '夹具：根真的拿不到')
        response = self._page(assetId='sticker-noroot-1', inline='1')
        self.assertEqual(response.status_code, 404, response.payload)
        self.assertEqual(response.payload['data']['reason'], 'root-unknown')
        self.assertEqual(response.payload['data']['root'], '')
        self.assertEqual(response.payload['data']['tried'], [])
        self.assertIn('库根不可知', response.payload['message'], '要把原因说清楚')

    def test_the_console_root_comes_from_the_service(self):
        """反向：控制台**不许自己算一套根**——服务层给的根就是唯一判据（§54 / §56）。

        真机 404 的头号成因就是"写的根 ≠ 读的根"。这里把服务层的根换成一个可辨认的
        哨兵：控制台若又去自己拼一份（数据目录 + 配置），这条立刻红。
        """
        sentinel = os.path.join(self.tmp, 'sentinel-root')
        self.service.sticker_library_root = lambda: sentinel
        self.assertEqual(self.api._sticker_root(), os.path.abspath(sentinel))

    def test_shorten_path_keeps_short_paths_and_trims_long_ones(self):
        """日志/短提示里的路径：短的照原样，长的只留末尾几段（不写整条磁盘结构）。"""
        self.assertEqual(console_module.shorten_path('/a/b/c.png'), '/a/b/c.png')
        long_path = ('/srv/astrbot/data/plugin_data/astrbot_plugin_hds_interlude'
                     '/data/hds-interlude/stickers/collected/x.jpg')
        shortened = console_module.shorten_path(long_path)
        self.assertTrue(shortened.startswith('…/'), shortened)
        self.assertEqual(
            shortened,
            '…/astrbot_plugin_hds_interlude/data/hds-interlude/'
            'stickers/collected/x.jpg',
        )
        self.assertEqual(console_module.shorten_path(''), '')
        self.assertLess(len(shortened), len(long_path))

    def _set_file_path(self, asset_id: str, file_path: str) -> None:
        """直接改行里的 `filePath`（模拟继承来的旧数据）。"""
        self.database.conn.execute(
            'UPDATE interlude_sticker SET filePath = ? WHERE assetId = ?',
            (file_path, asset_id),
        )
        self.database.conn.commit()


class ConsoleStickerGroupTests(unittest.TestCase):
    """表情库**分组 = 目录**与**上传**（v1.8.3 起，v1.8.5 返工；`docs/PORTING_NOTES.md` §47）。

    跑的是真实的 `ConsoleApi` + 真实的 `ServiceChunk2` 写路径（只有模型/传输是桩），
    与 `ConsoleStickerLibraryTests` 同一套夹具。**磁盘是真的**：移动 / 改名 / 删除
    都会真的搬文件，所以夹具按"文件的第一个目录段 = `group`"来造。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = self._tmp.name
        self.root = os.path.join(self.tmp, 'stickers')
        os.makedirs(os.path.join(self.root, 'collected'))
        self.bridge = _make_bridge({'stickers': {'enabled': True, 'directory': 'stickers'}})
        self.database = Database(':memory:')
        self.addCleanup(self.database.close)
        self.database.register_tables()
        self.bridge.db = self.database
        self.service = _StickerService(self.tmp, self.database)
        self.bridge.service = self.service
        self.api = ConsoleApi(self.bridge)

    # ---- 造数据 ----

    def _row(self, asset_id, **patch):
        base = {
            'assetId': asset_id,
            'filePath': '%s.png' % asset_id,
            'group': 'collected',
            'mimeType': 'image/png',
            'animated': False,
            'size': len(_PNG_BYTES),
            'hash': asset_id,
            'description': '一只挥手的猫',
            'aliases': [],
            'status': 'active',
            'embedding': [],
            'name': '',
            'source': 'manual',
            'uses': 0,
            'descriptionManual': False,
            'guessed': False,
            'createdAt': '2026-09-01T10:00:00.000Z',
            'updatedAt': '2026-09-01T10:00:00.000Z',
        }
        base.update(patch)
        if 'filePath' not in patch:
            # 组名就是一级目录名：默认让文件真的躺在自己那一组的目录里。
            group = str(base.get('group') or '').strip()
            base['filePath'] = ('%s/%s.png' % (group, asset_id)) if group else ('%s.png' % asset_id)
        return base

    def _insert(self, asset_id, body=_PNG_BYTES, **patch):
        row = self._row(asset_id, **patch)
        row['id'] = self.database.insert('interlude_sticker', row)
        path = os.path.join(self.root, row['filePath'].replace('/', os.sep))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as handle:
            handle.write(body)
        return row

    def _group(self, item):
        return {group['groupId']: group for group in item['items']}

    def _stored(self, asset_id):
        rows = self.database.all('interlude_sticker', {'assetId': asset_id})
        self.assertEqual(len(rows), 1, asset_id)
        return rows[0]

    # ---- 分组列表 ----

    def test_the_builtin_default_group_is_always_in_the_list(self):
        """没有素材、表里也没有行时也要有内置默认组（「未整理」）——否则收藏的素材无所属。"""
        payload = _run(self.api.sticker_groups())
        self.assertEqual(payload['total'], 1)
        self.assertEqual(payload['defaultGroupId'], 'collected')
        item = payload['items'][0]
        self.assertEqual(item['groupId'], 'collected')
        # v1.8.4（§48）：显示名从「自动收藏」改成「未整理」（目录名 `collected` 一字未动）。
        # 这是**唯一**一个"显示名 ≠ 目录名"的特例。
        self.assertEqual(item['name'], '未整理')
        # 描述：描述行没写过就回落到**内置那句默认描述**（v1.8.4，§48.5）——控制台与
        # 模型目录看到的必须是同一句话，两处说法不一致就是"两处判据"。
        self.assertEqual(item['description'], console_module.COLLECTED_STICKER_GROUP_DESCRIPTION)
        self.assertEqual(
            item['description'], '自动收藏与手动上传都先落这里，还没归组。',
        )
        self.assertEqual(item['count'], 0)
        self.assertTrue(item['builtin'])
        self.assertTrue(item['registered'], '兼容字段恒 true：表里有行 = 有描述，不是"注册"')
        self.assertFalse(payload['truncated'])

    def test_the_builtin_group_description_prefers_the_description_row(self):
        """§48.5：描述行写了自己的描述就用它，没写才回落内置那句（两处同一句话）。"""
        default = console_module.COLLECTED_STICKER_GROUP_DESCRIPTION
        before = self._group(_run(self.api.sticker_groups()))['collected']
        self.assertEqual(before['description'], default, '没写过 → 内置那句')
        _run(self.api.save_sticker_group({
            'groupId': 'collected', 'name': '未整理', 'description': '她自己写的',
        }))
        after = self._group(_run(self.api.sticker_groups()))['collected']
        self.assertEqual(after['description'], '她自己写的', '描述行有描述 → 用描述行的')
        self.assertTrue(after['builtin'], '写描述不改"内置组"这件事')
        self.assertEqual(after['name'], '未整理', '内置组的显示名是常量，写描述不会改它')

    def test_sticker_items_expose_the_two_group_ownership_flags(self):
        """§48 / §50：素材 item **只加**三个字段（归属两枚 + `disabledBy`），旧字段一个没动。"""
        self._insert('a-1', group='collected')
        self.database.update('interlude_sticker', {'assetId': 'a-1'},
                             {'groupGuessed': True, 'groupManual': False})
        self._insert('a-2', group='g-1')
        self.database.update('interlude_sticker', {'assetId': 'a-2'},
                             {'groupGuessed': False, 'groupManual': True})
        # §50：**启用状态是谁定的**（模型停的 / 人停过或启用的），如实带出去。
        self.database.update('interlude_sticker', {'assetId': 'a-1'},
                             {'status': 'disabled', 'disabledBy': 'model'})
        self.database.update('interlude_sticker', {'assetId': 'a-2'},
                             {'status': 'active', 'disabledBy': 'manual'})
        self._insert('a-3', group='g-1')
        listing = _run(self.api.stickers())
        by_id = {item['assetId']: item for item in listing['items']}
        self.assertTrue(by_id['a-1']['groupGuessed'])
        self.assertFalse(by_id['a-1']['groupManual'])
        self.assertTrue(by_id['a-2']['groupManual'])
        self.assertFalse(by_id['a-2']['groupGuessed'])
        self.assertTrue(by_id['a-1']['disabled'])
        self.assertEqual(by_id['a-1']['disabledBy'], 'model', '模型停的')
        self.assertFalse(by_id['a-2']['disabled'])
        self.assertEqual(by_id['a-2']['disabledBy'], 'manual', '人启用的（模型不许再停）')
        # 旧库补列前写入的行是 NULL → 布尔当 false、字符串当空串（不是 None / 缺键）。
        self.assertIs(by_id['a-3']['groupGuessed'], False)
        self.assertIs(by_id['a-3']['groupManual'], False)
        self.assertEqual(by_id['a-3']['disabledBy'], '')
        # 既有字段名一个都没动。
        for key in ('assetId', 'group', 'groupId', 'groupName', 'manual', 'guessed'):
            with self.subTest(key=key):
                self.assertIn(key, by_id['a-1'])

    def test_every_group_comes_from_the_disk_and_no_asset_disappears(self):
        """目录即分组：磁盘上的目录 / 有素材挂着的值**一律是正常分组**，素材一条不少。"""
        self._insert('a-1', group='collected')
        self._insert('a-2', group='collected')
        self._insert('b-1', group='default')
        self._insert('c-1', group='', source='manual')
        os.makedirs(os.path.join(self.root, '手工建的', 'sub'))  # 盘上有目录、没素材、没描述
        groups = self._group(_run(self.api.sticker_groups()))
        self.assertEqual(groups['collected']['count'], 2)
        self.assertEqual(groups['collected']['name'], '未整理')
        self.assertTrue(groups['collected']['builtin'])
        # 有素材挂着的目录名：它就是一个正常分组（名字就是目录名，没有"未注册"这一等）。
        self.assertEqual(groups['default']['name'], 'default')
        self.assertTrue(groups['default']['registered'])
        self.assertEqual(groups['default']['count'], 1)
        # 磁盘上**真的存在**的空目录也在列表里（新建分组 / 手动 mkdir 之后看得见）。
        self.assertEqual(groups['手工建的']['count'], 0)
        self.assertEqual(groups['手工建的']['name'], '手工建的')
        # 空 group 的旧行单列"未分组"，计数不被吞掉。
        self.assertEqual(groups['']['name'], console_module.STICKER_UNGROUPED_NAME)
        self.assertEqual(groups['']['count'], 1)
        # 素材一个都没少，而且每一条都能说出自己的组名。
        payload = _run(self.api.stickers())
        self.assertEqual(payload['total'], 4)
        by_id = {item['assetId']: item for item in payload['items']}
        self.assertEqual(by_id['a-1']['groupId'], 'collected')
        self.assertEqual(by_id['a-1']['groupName'], '未整理')
        self.assertEqual(by_id['b-1']['groupName'], 'default')
        self.assertEqual(by_id['c-1']['groupName'], console_module.STICKER_UNGROUPED_NAME)
        # 老字段 `group` 没变（只加字段，不动已有字段名）。
        self.assertEqual(by_id['b-1']['group'], 'default')

    def test_group_list_survives_a_missing_service_and_a_missing_table(self):
        """服务层没起来 / 表没建：列表仍是空壳 + 内置默认组，从不 500。"""
        self.bridge.service = None
        payload = _run(self.api.sticker_groups())
        self.assertEqual([item['groupId'] for item in payload['items']], ['collected'])

        # 没建表的库（`_safe_all` / `count_by` 都取不到 → 空壳）。
        bare = Database(':memory:')
        self.addCleanup(bare.close)
        self.bridge.db = bare
        self.bridge.service = self.service
        payload = _run(self.api.sticker_groups())
        self.assertEqual([item['groupId'] for item in payload['items']], ['collected'])
        self.assertEqual(payload['items'][0]['count'], 0)

    def test_stickers_can_be_filtered_by_group_id(self):
        self._insert('a-1', group='collected')
        self._insert('b-1', group='default')
        self.assertEqual(_run(self.api.stickers(group='collected'))['total'], 1)
        self.assertEqual(
            _run(self.api.stickers(group='collected'))['items'][0]['assetId'], 'a-1',
        )
        self.assertEqual(_run(self.api.stickers(group='nope'))['total'], 0)
        # 空 = 不过滤（与其他筛选项同一条规矩）。
        self.assertEqual(_run(self.api.stickers(group=''))['total'], 2)

    # ---- 新建 / 改名 ----

    def test_create_group_makes_a_directory_named_after_the_group(self):
        """**新建分组 = 建目录**：`groupId` 就是目录名（这里用的是中文组名）。"""
        payload = _run(self.api.save_sticker_group({
            'name': '猫猫', 'description': '撒娇、求摸头的时候用',
        }))
        group_id = payload['groupId']
        self.assertEqual(group_id, '猫猫', 'id 的字面量就是磁盘目录名，不再由服务端生成')
        self.assertTrue(os.path.isdir(os.path.join(self.root, '猫猫')))
        self.assertEqual(payload['item']['name'], '猫猫', '组名 = 目录名')
        self.assertEqual(payload['item']['description'], '撒娇、求摸头的时候用')
        self.assertEqual(payload['item']['count'], 0)
        self.assertFalse(payload['item']['builtin'])
        self.assertTrue(payload['item']['registered'])
        self.assertTrue(payload['item']['createdAt'])
        stored = self.database.all('interlude_sticker_groups', {'groupId': group_id})[0]
        self.assertEqual(stored['description'], '撒娇、求摸头的时候用')

        # 改名 = **重命名目录**（`groupId` 跟着变），描述跟着走。
        renamed = _run(self.api.save_sticker_group({
            'groupId': group_id, 'name': '猫猫们', 'description': '换了个说法',
        }))
        self.assertEqual(renamed['groupId'], '猫猫们')
        self.assertEqual(renamed['item']['name'], '猫猫们')
        self.assertEqual(renamed['item']['description'], '换了个说法')
        self.assertFalse(os.path.exists(os.path.join(self.root, '猫猫')))
        self.assertTrue(os.path.isdir(os.path.join(self.root, '猫猫们')))
        self.assertEqual(self.database.count('interlude_sticker_groups'), 1)

    def test_asset_items_report_the_directory_name(self):
        created = _run(self.api.save_sticker_group({'name': '猫猫'}))
        group_id = created['groupId']
        self._insert('a-1', group=group_id)
        item = _run(self.api.stickers())['items'][0]
        self.assertEqual(item['groupId'], group_id)
        self.assertEqual(item['groupName'], '猫猫', '组名就是目录名')

    def test_duplicate_group_name_is_rejected(self):
        _run(self.api.save_sticker_group({'name': '猫猫'}))
        for name in ('猫猫', ' 猫猫 '):
            with self.subTest(name=name):
                with self.assertRaises(ConsoleError) as caught:
                    _run(self.api.save_sticker_group({'name': name}))
                self.assertIn('已存在', str(caught.exception))
        self.assertEqual(self.database.count('interlude_sticker_groups'), 1)
        self.assertEqual(sorted(os.listdir(self.root)), ['collected', '猫猫'], '不许建出第二个目录')

    def test_group_name_and_description_are_validated(self):
        cases = (
            ({'name': ''}, '不能为空'),
            ({'name': '   '}, '不能为空'),
            # 上限按**字节**：33 个汉字（99 字节）行，40 个（120 字节）不行。
            ({'name': '猫' * 40}, '最长'),
            ({'name': 'x' * (helpers_module.STICKER_GROUP_NAME_MAX_BYTES + 1)}, '最长'),
            ({'name': 'a/b'}, '不能包含'),
            ({'name': '..'}, '开头'),
            ({'name': 'ok', 'description': 'x' * (helpers_module.STICKER_GROUP_DESCRIPTION_MAX + 1)}, '最长'),
            ({'name': 'ok', 'description': 5}, 'description'),
            ({'description': '没有名字'}, 'name'),
        )
        for body, needle in cases:
            with self.subTest(body=str(body)[:40]):
                with self.assertRaises(ConsoleError) as caught:
                    _run(self.api.save_sticker_group(body))
                self.assertIn(needle, str(caught.exception))
        # 一律先校验后写：上面这些一条都没落库、也没建目录。
        self.assertEqual(self.database.count('interlude_sticker_groups'), 0)
        self.assertEqual(os.listdir(self.root), ['collected'])

    def test_the_root_bucket_name_is_reserved_for_write_targets(self):
        """收尾②：`default` 是保留名——新建 / 改名 / 移动 / 上传 / 删组目标全是 400。"""
        with self.assertRaises(ConsoleError) as caught:
            _run(self.api.save_sticker_group({'name': 'default'}))
        self.assertIn('保留名', str(caught.exception))
        group_id = _run(self.api.save_sticker_group({'name': '猫猫'}))['groupId']
        for body in (
            {'groupId': group_id, 'name': 'default'},
            {'groupId': 'collected', 'name': 'default'},
        ):
            with self.subTest(body=body):
                with self.assertRaises(ConsoleError) as caught:
                    _run(self.api.save_sticker_group(body))
                self.assertIn('default', str(caught.exception))
        self._insert('a-1')
        with self.assertRaises(ConsoleError) as caught:
            _run(self.api.move_stickers({'assetIds': ['a-1'], 'groupId': 'default'}))
        self.assertIn('保留名', str(caught.exception))
        with self.assertRaises(ConsoleError):
            _run(self.api.upload_sticker(_PNG_BYTES, group_id='default'))
        with self.assertRaises(ConsoleError) as caught:
            _run(self.api.delete_sticker_group({'groupId': group_id, 'moveTo': 'default'}))
        self.assertIn('保留名', str(caught.exception))
        self.assertEqual(self._stored('a-1')['group'], 'collected')
        self.assertEqual(sorted(os.listdir(self.root)), ['collected', '猫猫'], '一个目录都不许建出来')

    def test_a_legacy_default_group_still_shows_up_and_can_be_managed(self):
        """既有 `default` 目录/桶：照样在列表里、照样能写描述、能当**来源**删掉。"""
        self._insert('a-1', group='default')
        groups = self._group(_run(self.api.sticker_groups()))
        self.assertEqual(groups['default']['count'], 1)
        self.assertEqual(groups['default']['name'], 'default')
        saved = _run(self.api.save_sticker_group({
            'groupId': 'default', 'name': 'default', 'description': '根桶遗留',
        }))
        self.assertEqual(saved['item']['description'], '根桶遗留')
        payload = _run(self.api.delete_sticker_group({'groupId': 'default'}))
        self.assertTrue(payload['deleted'])
        self.assertEqual(payload['moved'], 1)
        self.assertEqual(self._stored('a-1')['group'], 'collected')

    def test_group_items_expose_auto_created(self):
        """收尾①：接口如实带出 `autoCreated`（要不要显示由前端定；人工建的一律 false）。"""
        _run(self.api.save_sticker_group({'name': '猫猫'}))
        self.database.insert('interlude_sticker_groups', {
            'groupId': '模型建的', 'description': '', 'autoCreated': True,
            'createdAt': '2026-09-01T10:00:00.000Z', 'updatedAt': '2026-09-01T10:00:00.000Z',
        })
        groups = self._group(_run(self.api.sticker_groups()))
        self.assertIs(groups['collected']['autoCreated'], False, '内置组不是自动建的')
        self.assertIs(groups['猫猫']['autoCreated'], False)
        self.assertIs(groups['模型建的']['autoCreated'], True)
        # 旧库补列前的行是 NULL → false（不是 None / 缺键）。
        self.database.insert('interlude_sticker_groups', {
            'groupId': '老行', 'description': '',
            'createdAt': '2026-09-01T10:00:00.000Z', 'updatedAt': '2026-09-01T10:00:00.000Z',
        })
        self.assertIs(self._group(_run(self.api.sticker_groups()))['老行']['autoCreated'], False)
    def test_group_endpoints_reject_fields_outside_the_whitelist(self):
        for body in (
            {'name': 'ok', 'count': 3},
            {'name': 'ok', 'builtin': True},
            {'groupId': 'g-1', 'name': 'ok', 'createdAt': 'x'},
        ):
            with self.subTest(body=body):
                with self.assertRaises(ConsoleError) as caught:
                    _run(self.api.save_sticker_group(body))
                self.assertIn('不认识这些字段', str(caught.exception))

    def test_unknown_group_id_cannot_be_saved(self):
        with self.assertRaises(ConsoleError) as caught:
            _run(self.api.save_sticker_group({'groupId': 'nope', 'name': '凭空捏造'}))
        self.assertIn('找不到这个分组', str(caught.exception))

    def test_writing_a_description_for_an_existing_directory_is_just_a_save(self):
        """**没有"采纳"这个动作了**：给一个历史目录补描述 = `groupId == name` 的写入。

        唯一的门槛是"这个分组真的存在"（有素材挂着 / 盘上有目录）——否则就是凭空捏造。
        """
        with self.assertRaises(ConsoleError) as caught:
            _run(self.api.save_sticker_group({
                'groupId': 'default', 'name': 'default', 'description': '顺手存的',
            }))
        self.assertIn('找不到这个分组', str(caught.exception))
        self._insert('b-1', group='default')
        payload = _run(self.api.save_sticker_group({
            'groupId': 'default', 'name': 'default', 'description': '顺手存的',
        }))
        self.assertEqual(payload['groupId'], 'default')
        self.assertTrue(payload['item']['registered'])
        self.assertEqual(payload['item']['count'], 1)
        self.assertEqual(payload['item']['name'], 'default', '写描述不会顺手改名')
        self.assertEqual(payload['item']['description'], '顺手存的')
        # 素材的 groupName 就是目录名（没有被改名）。
        self.assertEqual(_run(self.api.stickers())['items'][0]['groupName'], 'default')
        # 盘上真有目录的也一样（哪怕还没有素材）。
        os.makedirs(os.path.join(self.root, '手工建的'))
        written = _run(self.api.save_sticker_group({
            'groupId': '手工建的', 'name': '手工建的', 'description': '手动放的目录',
        }))
        self.assertEqual(written['item']['description'], '手动放的目录')

    def test_unsafe_group_ids_are_rejected(self):
        for group_id in ('../evil', 'a/b', 'a\\b', '.hidden', 'a:b', 'x' * 200):
            with self.subTest(group_id=group_id):
                with self.assertRaises(ConsoleError):
                    _run(self.api.save_sticker_group({'groupId': group_id, 'name': 'ok'}))
        self.assertEqual(os.listdir(self.root), ['collected'], '一个目录都不许建出来')

    def test_the_builtin_group_cannot_be_renamed_but_takes_a_description(self):
        """内置组的目录名不许改（老库里已经有素材在 `collected/`），写描述照常。"""
        payload = _run(self.api.save_sticker_group({
            'groupId': 'collected', 'name': '未整理', 'description': '别人发来的表情包',
        }))
        self.assertEqual(payload['groupId'], 'collected')
        self.assertTrue(payload['item']['builtin'])
        self.assertEqual(payload['item']['name'], '未整理')
        self.assertEqual(payload['item']['description'], '别人发来的表情包')
        self._insert('a-1', group='collected')
        item = _run(self.api.stickers())['items'][0]
        self.assertEqual(item['groupName'], '未整理')
        # 改名请求：400，而且目录一个字节都没动。
        with self.assertRaises(ConsoleError) as caught:
            _run(self.api.save_sticker_group({'groupId': 'collected', 'name': '收藏夹'}))
        self.assertIn('不能改', str(caught.exception))
        self.assertTrue(os.path.isdir(os.path.join(self.root, 'collected')))

    def test_group_writes_need_a_service(self):
        self.bridge.service = None
        with self.assertRaises(ConsoleError):
            _run(self.api.save_sticker_group({'name': 'ok'}))
        with self.assertRaises(ConsoleError):
            _run(self.api.save_sticker_group({'groupId': 'collected', 'name': 'ok'}))
        with self.assertRaises(ConsoleError):
            _run(self.api.delete_sticker_group({'groupId': 'g-1'}))
        with self.assertRaises(ConsoleError):
            _run(self.api.move_stickers({'assetIds': ['a-1'], 'groupId': 'collected'}))
        with self.assertRaises(ConsoleError):
            _run(self.api.upload_sticker(_PNG_BYTES))

    # ---- 删除分组 ----

    def test_deleting_a_group_moves_its_assets_instead_of_deleting_them(self):
        """红线：**绝不悄悄删素材**——删组 = 把文件搬进目标目录 + 删目录与描述行。"""
        group_id = _run(self.api.save_sticker_group({'name': '猫猫'}))['groupId']
        for index in range(3):
            self._insert('cat-%d' % index, group=group_id)
        self.assertTrue(os.path.isdir(os.path.join(self.root, '猫猫')))
        payload = _run(self.api.delete_sticker_group({'groupId': group_id}))
        self.assertTrue(payload['deleted'])
        self.assertEqual(payload['moved'], 3)
        self.assertEqual(payload['moveTo'], 'collected')
        self.assertEqual(self.database.count('interlude_sticker_groups'), 0)
        self.assertFalse(os.path.exists(os.path.join(self.root, '猫猫')), '组目录该没了')
        for index in range(3):
            stored = self._stored('cat-%d' % index)
            self.assertEqual(stored['group'], 'collected')
            self.assertEqual(stored['filePath'], 'collected/cat-%d.png' % index)
            self.assertTrue(os.path.isfile(os.path.join(self.root, stored['filePath'])))
        # 三条素材一条都没少，而且现在属于默认组。
        listing = _run(self.api.stickers())
        self.assertEqual(listing['total'], 3)
        self.assertEqual({item['groupName'] for item in listing['items']}, {'未整理'})

    def test_delete_group_honours_move_to(self):
        first = _run(self.api.save_sticker_group({'name': '猫猫'}))['groupId']
        second = _run(self.api.save_sticker_group({'name': '狗狗'}))['groupId']
        self._insert('cat-1', group=first)
        payload = _run(self.api.delete_sticker_group({
            'groupId': first, 'moveTo': second,
        }))
        self.assertEqual(payload['moved'], 1)
        self.assertEqual(payload['moveTo'], second)
        self.assertEqual(self._stored('cat-1')['group'], second)

    def test_delete_rejects_the_builtin_group_and_unknown_targets(self):
        with self.assertRaises(ConsoleError) as caught:
            _run(self.api.delete_sticker_group({'groupId': 'collected'}))
        self.assertIn('内置分组不能删除', str(caught.exception))
        group_id = _run(self.api.save_sticker_group({'name': '猫猫'}))['groupId']
        # 带 `moveTo`（指向一个真实存在的分组）也照样 400：内置组的素材不能被搬空。
        with self.assertRaises(ConsoleError) as caught:
            _run(self.api.delete_sticker_group({'groupId': 'collected', 'moveTo': group_id}))
        self.assertIn('内置分组不能删除', str(caught.exception))
        self._insert('cat-1', group=group_id)
        # moveTo 指向不存在的组：400，而且**什么都没动**（素材还在原组，组也还在）。
        with self.assertRaises(ConsoleError) as caught:
            _run(self.api.delete_sticker_group({'groupId': group_id, 'moveTo': 'nope'}))
        self.assertIn('找不到要挪入的分组', str(caught.exception))
        self.assertEqual(self._stored('cat-1')['group'], group_id)
        self.assertEqual(self.database.count('interlude_sticker_groups'), 1)
        # 挪进"正在删除的组"也是 400。
        with self.assertRaises(ConsoleError):
            _run(self.api.delete_sticker_group({'groupId': group_id, 'moveTo': group_id}))
        # 删一个根本没注册的 id：400。
        with self.assertRaises(ConsoleError) as caught:
            _run(self.api.delete_sticker_group({'groupId': 'nope'}))
        self.assertIn('找不到这个分组', str(caught.exception))
        with self.assertRaises(ConsoleError):
            _run(self.api.delete_sticker_group({}))
        with self.assertRaises(ConsoleError):
            _run(self.api.delete_sticker_group({'groupId': group_id, 'purge': True}))

    # ---- 批量移动 ----

    def test_move_reassigns_assets_and_returns_the_fresh_rows(self):
        group_id = _run(self.api.save_sticker_group({'name': '猫猫'}))['groupId']
        self._insert('a-1')
        self._insert('a-2')
        payload = _run(self.api.move_stickers({
            'assetIds': ['a-1', 'a-2'], 'groupId': group_id,
        }))
        self.assertEqual(payload['moved'], 2)
        self.assertEqual([item['assetId'] for item in payload['item']], ['a-1', 'a-2'])
        self.assertEqual({item['groupName'] for item in payload['item']}, {'猫猫'})
        self.assertEqual(self._stored('a-1')['group'], group_id)
        self.assertEqual(self._stored('a-2')['group'], group_id)
        # 已经在目标组里的也算"移动成功"（幂等：重复点不会报错）。
        again = _run(self.api.move_stickers({'assetIds': ['a-1'], 'groupId': group_id}))
        self.assertEqual(again['moved'], 1)

    def test_move_validates_every_id_before_writing_anything(self):
        """先校验后写：有一条不合法就一条都不写（批量操作"改了一半"最难收拾）。"""
        group_id = _run(self.api.save_sticker_group({'name': '猫猫'}))['groupId']
        self._insert('a-1')
        with self.assertRaises(ConsoleError) as caught:
            _run(self.api.move_stickers({'assetIds': ['a-1', 'nope'], 'groupId': group_id}))
        self.assertIn('找不到这条素材', str(caught.exception))
        self.assertEqual(self._stored('a-1')['group'], 'collected', '前一条也不许被改')

    def test_move_rejects_bad_payloads(self):
        group_id = _run(self.api.save_sticker_group({'name': '猫猫'}))['groupId']
        self._insert('a-1')
        cases = (
            ({'assetIds': [], 'groupId': group_id}, 'assetIds'),
            ({'assetIds': 'a-1', 'groupId': group_id}, 'assetIds'),
            ({'assetIds': [''], 'groupId': group_id}, '空值'),
            ({'assetIds': [None], 'groupId': group_id}, '空值'),
            ({'assetIds': ['x' * 300], 'groupId': group_id}, '过长'),
            ({'assetIds': ['a-1']}, 'groupId'),
            ({'assetIds': ['a-1'], 'groupId': ''}, 'groupId'),
            ({'assetIds': ['a-1'], 'groupId': '../evil'}, '分组名'),
            # 不存在的组名不能当目标（先建组再挪），否则它会变成谁都能写进去的暗号。
            # `default` 是保留名（根目录素材的桶）：先撞保留名那条闸。
            ({'assetIds': ['a-1'], 'groupId': 'default'}, '保留名'),
            ({'assetIds': ['a-1'], 'groupId': group_id, 'purge': True}, '不认识这些字段'),
        )
        for body, needle in cases:
            with self.subTest(body=str(body)[:50]):
                with self.assertRaises(ConsoleError) as caught:
                    _run(self.api.move_stickers(body))
                self.assertIn(needle, str(caught.exception))
        with self.assertRaises(ConsoleError) as caught:
            _run(self.api.move_stickers({
                'assetIds': ['a-%d' % index for index in range(console_module.STICKER_MOVE_MAX + 1)],
                'groupId': group_id,
            }))
        self.assertIn('最多', str(caught.exception))
        self.assertEqual(self._stored('a-1')['group'], 'collected')

    def test_the_service_layer_owns_the_same_gates_on_its_own(self):
        """服务层的写入路径**自己**也要挡：控制台只是第一层。

        路径安全不嫌两层——控制台判一次（请求形状 + 快速失败 + 好文案），服务层再判一次
        （它是**唯一写入路径**，别的调用方也会走它）。这条**直接调服务层**，
        所以哪一层被改坏都能分别被抓住（只测控制台的话，服务层那道闸永远不会红）。
        """
        group_id = _run(self.api.save_sticker_group({'name': '猫猫'}))['groupId']
        row = self._insert('a-1')

        with self.assertRaises(ValueError):
            _run(self.service.move_sticker_assets([row['id']], '../evil'))
        with self.assertRaises(ValueError):
            _run(self.service.delete_sticker_group('collected'))
        # 带 `moveTo` 指向**另一个真实分组**时，唯一挡得住的就是"内置组不许删"那一条
        # （没有 moveTo 时 `target == wanted` 也会挡，所以这一条才是真正的闸门用例）。
        with self.assertRaises(ValueError):
            _run(self.service.delete_sticker_group('collected', move_to=group_id))
        with self.assertRaises(ValueError):
            _run(self.service.delete_sticker_group(group_id, move_to='nope'))
        with self.assertRaises(ValueError):
            _run(self.service.upload_sticker_asset(_PNG_BYTES, '../evil'))
        with self.assertRaises(ValueError):
            _run(self.service.upload_sticker_asset(_PNG_BYTES, group_id='nope'))
        with self.assertRaises(ValueError):
            _run(self.service.save_sticker_group('', '   '))
        with self.assertRaises(ValueError):
            _run(self.service.save_sticker_group('', 'a/b'))
        with self.assertRaises(ValueError):
            _run(self.service.save_sticker_group('nope', '凭空捏造'))
        # 内置组的目录名不许改（改名 = 让已有素材集体换目录）。
        with self.assertRaises(ValueError):
            _run(self.service.save_sticker_group('collected', '收藏夹'))

        # 服务层拒绝之后**什么都没动**（素材还在原组、组目录还在、没多出素材）。
        self.assertEqual(self._stored('a-1')['group'], 'collected')
        self.assertEqual(self.database.count('interlude_sticker_groups'), 1)
        self.assertEqual(self.database.count('interlude_sticker'), 1)
        self.assertEqual(sorted(os.listdir(self.root)), ['collected', '猫猫'])

    # ---- 上传 ----

    def test_upload_writes_the_file_by_content_hash_and_never_trusts_the_name(self):
        payload = _run(self.api.upload_sticker(_PNG_BYTES, name='坏笑的猫'))
        digest = hashlib.sha256(_PNG_BYTES).hexdigest()
        self.assertFalse(payload['duplicated'])
        self.assertEqual(payload['assetId'], 'upload-%s' % digest[:16])
        item = payload['item']
        self.assertEqual(item['name'], '坏笑的猫')
        self.assertEqual(item['mimeType'], 'image/png')
        self.assertEqual(item['size'], len(_PNG_BYTES))
        self.assertEqual(item['source'], 'manual')
        self.assertEqual(item['groupId'], 'collected')
        self.assertEqual(item['groupName'], '未整理')
        self.assertFalse(item['manual'])
        # 落盘名 = 内容哈希 + 嗅探出来的扩展名（文件名参数根本不在路径里）。
        # v1.9.1（§54）：`file` 是**相对根的路径**（与 `filePath` 逐字相同）——
        # 曾经它被削成 basename，而取图也按 basename 拼，于是带分组目录的素材
        # （自动收藏的 `collected/…`、扫盘的 `Cat/…`）**库里有行、缩略图 404**。
        # 前端 `fileLabel()` 自己取 basename 显示，界面文案因此一个字没变。
        self.assertEqual(item['file'], 'collected/%s.png' % digest)
        stored = self._stored(payload['assetId'])
        self.assertEqual(stored['hash'], digest)
        self.assertEqual(stored['filePath'], 'collected/%s.png' % digest)
        self.assertEqual(stored['status'], 'pending', '没给描述 → 等模型/重扫')
        self.assertTrue(os.path.isfile(os.path.join(self.root, 'collected', '%s.png' % digest)))

    def test_upload_rejects_non_image_bytes(self):
        with self.assertRaises(ConsoleError) as caught:
            _run(self.api.upload_sticker(b'<html>not an image</html>'))
        self.assertIn('不是图片', str(caught.exception))
        with self.assertRaises(ConsoleError):
            _run(self.api.upload_sticker(b''))
        with self.assertRaises(ConsoleError):
            _run(self.api.upload_sticker('not bytes'))
        self.assertEqual(self.database.count('interlude_sticker'), 0)
        self.assertEqual(os.listdir(os.path.join(self.root, 'collected')), [])

    def test_upload_rejects_oversized_images_before_touching_the_disk(self):
        self.service.cached_sticker_config = {
            'enabled': True, 'directory': 'stickers', 'max_file_size_mb': 0.0001,
            'catalog_limit': 40,
        }
        big = b'\x89PNG\r\n\x1a\n' + b'x' * 400
        with self.assertRaises(ConsoleError) as caught:
            _run(self.api.upload_sticker(big))
        self.assertIn('体积上限', str(caught.exception))
        self.assertEqual(self.database.count('interlude_sticker'), 0)
        self.assertEqual(os.listdir(os.path.join(self.root, 'collected')), [])

    def test_duplicate_upload_returns_the_existing_row_and_writes_nothing(self):
        """同一个文件再传一次：回已存在那条，**不新建文件、不覆盖、不改行**。"""
        first = _run(self.api.upload_sticker(_PNG_BYTES, description='第一次的描述'))
        digest = hashlib.sha256(_PNG_BYTES).hexdigest()
        path = os.path.join(self.root, 'collected', '%s.png' % digest)
        before = os.stat(path).st_mtime_ns
        second = _run(self.api.upload_sticker(
            _PNG_BYTES, group_id='', description='第二次想改的描述', name='另一个名字',
        ))
        self.assertTrue(second['duplicated'])
        self.assertEqual(second['assetId'], first['assetId'])
        # 已存在那条的描述 / 名字 / 手工标记都没被第二次上传顶掉。
        self.assertEqual(second['item']['description'], '第一次的描述')
        self.assertTrue(second['item']['manual'])
        self.assertEqual(second['item']['name'], 'upload-%s' % digest[:8])
        self.assertEqual(self.database.count('interlude_sticker'), 1)
        self.assertEqual(os.stat(path).st_mtime_ns, before, '重复上传不该重写文件')

    def test_duplicate_upload_matches_disabled_and_missing_rows_too(self):
        """去重看**内容**，不看状态：停用过的素材再传一次仍然回那一条（不复活、不顶替）。"""
        first = _run(self.api.upload_sticker(_PNG_BYTES))
        _run(self.api.update_sticker({'assetId': first['assetId'], 'disabled': True}))
        again = _run(self.api.upload_sticker(_PNG_BYTES))
        self.assertTrue(again['duplicated'])
        self.assertTrue(again['item']['disabled'], '不许把停用决定顶掉')
        self.assertEqual(self.database.count('interlude_sticker'), 1)

    def test_upload_without_a_describer_stays_pending_and_warns(self):
        """没配识图模型：行留在 `pending`、描述为空，而且**有一条 warn**。

        这是"能力缺失"（坑 25）：用户刚上传完，得能看见"它还没被描述"，
        而不是以为模型会自己搞定。下一次「重扫表情库」会补上。
        """
        self.service.sticker_describer = None
        payload = _run(self.api.upload_sticker(_PNG_BYTES))
        self.assertEqual(payload['item']['status'], 'pending')
        self.assertEqual(payload['item']['description'], '')
        self.assertFalse(payload['item']['manual'])
        self.assertTrue(
            any('视觉模型' in str(args) for args in self.service.reports),
            '能力缺失必须留一条 warn：%s' % (self.service.reports,),
        )

    def test_upload_losing_a_concurrent_race_returns_the_winning_row(self):
        """并发上传同一个文件：一边赢了写入，另一边**回那一行**，而不是 400。

        模拟：去重检查那一次假装库里没有（同时"另一边"已经把行写进去了），
        于是 `db_create` 撞 `assetId` 唯一索引——这时必须回过头按内容哈希找回那一行。
        """
        digest = hashlib.sha256(_PNG_BYTES).hexdigest()
        winner_id = 'upload-%s' % digest[:16]
        original = self.service._sticker_prior_by_hash
        calls = []

        async def _first_miss_then_hit(value):
            calls.append(value)
            if len(calls) == 1:
                row = self._row(winner_id, hash=digest, filePath='collected/%s.png' % digest)
                row['id'] = self.database.insert('interlude_sticker', row)
                return None
            return await original(value)

        self.service._sticker_prior_by_hash = _first_miss_then_hit
        payload = _run(self.api.upload_sticker(_PNG_BYTES))
        self.assertTrue(payload['duplicated'])
        self.assertEqual(payload['assetId'], winner_id)
        self.assertEqual(len(calls), 2, '去重查一次、撞索引后再查一次')
        self.assertEqual(self.database.count('interlude_sticker'), 1)

    def test_upload_with_a_description_pins_it_and_never_calls_the_model(self):
        async def _boom(*_args, **_kwargs):
            raise AssertionError('上传时给了描述就不该再调视觉模型')

        self.service.describe_sticker_asset = _boom
        payload = _run(self.api.upload_sticker(
            _PNG_BYTES, description='她手写的：一只挥手的猫', name='挥手',
        ))
        item = payload['item']
        self.assertEqual(item['description'], '她手写的：一只挥手的猫')
        self.assertTrue(item['manual'], '人写的描述要置 descriptionManual（扫描不得覆盖）')
        self.assertFalse(item['disabled'])
        stored = self._stored(payload['assetId'])
        self.assertEqual(stored['status'], 'active', '有描述 = 立刻进目录')
        self.assertEqual(stored['descriptionManual'], 1)

    def test_upload_without_a_description_asks_the_model_then_refreshes_the_catalog(self):
        calls = []

        async def _describe(asset, data, config=None):
            calls.append((asset.get('assetId'), bytes(data)))
            # 模拟真实描述成功：写库 + 刷新目录（真实实现在 chunk2.describe_sticker_asset）。
            await self.service.db_set('interlude_sticker', {'id': asset.get('id')}, {
                'description': '模型写的描述', 'status': 'active',
            })
            return True

        self.service.describe_sticker_asset = _describe
        payload = _run(self.api.upload_sticker(_PNG_BYTES))
        self.assertEqual(len(calls), 1, '没给描述就必须问一次模型')
        self.assertEqual(calls[0][1], _PNG_BYTES, '把**原始字节**交给描述器')
        self.assertEqual(payload['item']['description'], '模型写的描述')
        self.assertFalse(payload['item']['manual'])
        self.assertEqual(
            [row.get('assetId') for row in self.service.sticker_catalog],
            [payload['assetId']],
            '描述完要刷目录，下一回合的 stickerCatalog 里才有它',
        )

    def test_upload_lands_in_the_named_group(self):
        group_id = _run(self.api.save_sticker_group({'name': '猫猫'}))['groupId']
        payload = _run(self.api.upload_sticker(_PNG_BYTES, group_id=group_id))
        digest = hashlib.sha256(_PNG_BYTES).hexdigest()
        self.assertEqual(payload['item']['groupId'], group_id)
        self.assertEqual(payload['item']['groupName'], '猫猫')
        self.assertEqual(self._stored(payload['assetId'])['group'], group_id)
        self.assertTrue(os.path.isfile(os.path.join(self.root, group_id, '%s.png' % digest)))
        # 分组计数跟着涨（面板的"每组多少张"是精确的）。
        groups = self._group(_run(self.api.sticker_groups()))
        self.assertEqual(groups[group_id]['count'], 1)

    def test_upload_rejects_an_unknown_or_unsafe_group(self):
        for group_id in ('nope', 'default', '../evil', 'a/b'):
            with self.subTest(group_id=group_id):
                with self.assertRaises(ConsoleError):
                    _run(self.api.upload_sticker(_PNG_BYTES, group_id=group_id))
        self.assertEqual(self.database.count('interlude_sticker'), 0)
        self.assertEqual(os.listdir(os.path.join(self.root, 'collected')), [])

    def test_upload_is_refused_while_the_library_is_off(self):
        self.bridge.config = {'stickers': {'enabled': False, 'directory': 'stickers'}}
        with self.assertRaises(ConsoleError) as caught:
            _run(self.api.upload_sticker(_PNG_BYTES))
        self.assertIn('未启用', str(caught.exception))
        self.assertEqual(self.database.count('interlude_sticker'), 0)

    def test_upload_refuses_to_overwrite_a_different_file_at_the_same_path(self):
        """同名不同内容（只可能是哈希碰撞）：**拒绝**，绝不覆盖。"""
        digest = hashlib.sha256(_PNG_BYTES).hexdigest()
        path = os.path.join(self.root, 'collected', '%s.png' % digest)
        with open(path, 'wb') as handle:
            handle.write(b'\x89PNG\r\n\x1a\n' + b'DIFFERENT' * 8)
        with self.assertRaises(ConsoleError) as caught:
            _run(self.api.upload_sticker(_PNG_BYTES))
        self.assertIn('拒绝覆盖', str(caught.exception))
        with open(path, 'rb') as handle:
            self.assertIn(b'DIFFERENT', handle.read(), '盘上那份必须原封不动')
        self.assertEqual(self.database.count('interlude_sticker'), 0)

    def test_upload_reuses_an_orphan_file_with_identical_bytes(self):
        """盘上有同名文件但库里没行（比如手删了行）：内容一致就直接复用，不重写。"""
        digest = hashlib.sha256(_PNG_BYTES).hexdigest()
        path = os.path.join(self.root, 'collected', '%s.png' % digest)
        with open(path, 'wb') as handle:
            handle.write(_PNG_BYTES)
        before = os.stat(path).st_mtime_ns
        payload = _run(self.api.upload_sticker(_PNG_BYTES))
        self.assertFalse(payload['duplicated'])
        self.assertEqual(os.stat(path).st_mtime_ns, before)
        self.assertEqual(self.database.count('interlude_sticker'), 1)

    # ---- 上传路由（multipart 的两条参数通道） ----

    def _page(self, **kwargs):
        self.addCleanup(_install_web_request(**kwargs))
        plugin = _make_plugin({})
        plugin._console = self.api
        return _run(plugin.page_console_sticker_upload())

    def test_group_routes_are_wired_to_the_real_handlers(self):
        """四条分组路由的接线（`_console_json` / `_console_write`）+ 400 映射。

        只调 `ConsoleApi` 断言不到"路由注册没注册、方法对不对"——所以这里跑真的 handler
        （宿主响应用桩），与 `console/sticker-file` 那两条分支同一套做法。
        """
        plugin = _make_plugin({})
        plugin._console = self.api

        self.addCleanup(_install_web_request(query={}))
        listed = _run(plugin.page_console_sticker_groups())
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.payload['defaultGroupId'], 'collected')
        self.assertEqual(len(listed.payload['items']), 1)

        self.addCleanup(_install_web_request(body={'name': '猫猫', 'description': '撒娇用'}))
        saved = _run(plugin.page_console_sticker_group_save())
        self.assertEqual(saved.status_code, 200)
        group_id = saved.payload['groupId']
        self.assertEqual(saved.payload['item']['name'], '猫猫')

        self._insert('a-1')
        self.addCleanup(_install_web_request(
            body={'assetIds': ['a-1'], 'groupId': group_id},
        ))
        moved = _run(plugin.page_console_sticker_move())
        self.assertEqual(moved.status_code, 200)
        self.assertEqual(moved.payload['moved'], 1)
        self.assertEqual(moved.payload['item'][0]['groupName'], '猫猫')

        self.addCleanup(_install_web_request(body={'groupId': group_id}))
        deleted = _run(plugin.page_console_sticker_group_delete())
        self.assertEqual(deleted.status_code, 200)
        self.assertTrue(deleted.payload['deleted'])
        self.assertEqual(deleted.payload['moved'], 1)
        self.assertEqual(deleted.payload['moveTo'], 'collected')

        # 校验失败一律 400 + 原文案（不是 500）。
        self.addCleanup(_install_web_request(body={'name': '   '}))
        bad = _run(plugin.page_console_sticker_group_save())
        self.assertEqual(bad.status_code, 400)
        self.assertIn('不能为空', bad.payload['message'])

    def test_upload_route_reads_the_file_field_and_form_options(self):
        response = self._page(
            uploads={'file': _FakeUpload('../../evil.png', _PNG_BYTES)},
            form={'groupId': '', 'description': '上传时写的描述', 'name': '猫猫图'},
        )
        self.assertEqual(response.status_code, 200)
        item = response.payload['item']
        self.assertFalse(response.payload['duplicated'])
        self.assertEqual(item['name'], '猫猫图')
        self.assertEqual(item['description'], '上传时写的描述')
        self.assertTrue(item['manual'])
        # 上传的文件名（`../../evil.png`）绝不进路径。
        self.assertNotIn('..', item['file'])
        self.assertNotIn('evil', item['file'])

    def test_upload_route_falls_back_to_query_params(self):
        """宿主 bridge 的 `upload(endpoint, file)` 只能发 `file` 一个字段——
        所以参数必须能从查询串带进来（老宿主没有 `form()` 也一样）。"""
        group_id = _run(self.api.save_sticker_group({'name': '猫猫'}))['groupId']
        response = self._page(
            uploads={'file': _FakeUpload('x.png', _PNG_BYTES)},
            query={'group_id': group_id, 'description': '走查询串的描述'},
            form_error=RuntimeError('老宿主没有 form()'),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.payload['item']['groupId'], group_id)
        self.assertEqual(response.payload['item']['description'], '走查询串的描述')

    def test_upload_route_without_a_file_is_a_400(self):
        response = self._page()
        self.assertEqual(response.status_code, 400)
        self.assertIn('没有收到文件内容', response.payload['message'])
        response = self._page(uploads={'file': _FakeUpload('x.png', b'')})
        self.assertEqual(response.status_code, 400)

    def test_upload_route_maps_console_errors_to_400(self):
        response = self._page(uploads={'file': _FakeUpload('x.txt', b'not an image')})
        self.assertEqual(response.status_code, 400)
        self.assertIn('不是图片', response.payload['message'])
        self.assertEqual(self.database.count('interlude_sticker'), 0)

    # ---- 根目录不可知：动盘的操作必须当场拒绝（§54） ----

    def _unavailable_root(self):
        """把数据目录变成"不可知"（`ctx.base_dir` 空）并给一个假的 cwd 沙箱。

        返回沙箱路径。断言"什么都没写"时看它——真跑起来 cwd 就是仓库根目录，
        修复前的实现会在这里建目录 / 写文件，测试反而会污染工作区。
        """
        sandbox = tempfile.TemporaryDirectory(prefix='hdsi_sticker_cwd_')
        self.addCleanup(sandbox.cleanup)
        self.service.ctx = InterludeContext()  # 数据目录不可知
        self.assertEqual(self.service.sticker_library_root(), '')
        return sandbox.name

    def test_an_unavailable_root_refuses_to_create_a_group_in_the_cwd(self):
        """根不可知 → 新建分组**当场拒绝**，绝不落到进程当前目录。

        反向：修复前这里是 `os.path.abspath('')` = cwd，于是目录真的建出来了、
        返回值还说成功，而用户在数据目录里永远找不到它（同族：§54 的
        "收藏写进了 A 目录、扫描看的是 B 目录"）。
        """
        sandbox = self._unavailable_root()
        with mock.patch('os.getcwd', return_value=sandbox):
            with self.assertRaises(ValueError) as caught:
                _run(self.service.save_sticker_group(name='ProbeGroup'))
        message = str(caught.exception)
        self.assertIn('表情库根目录不可用', message)
        self.assertIn('下一步', message, '拒绝文案必须可行动')
        self.assertEqual(os.listdir(sandbox), [], '一个字节都不许落到进程当前目录')

    def test_an_unavailable_root_refuses_an_upload_before_writing_anything(self):
        """同一道闸的另一半：上传素材也不许把字节写进 cwd，而且**不许建档**。"""
        sandbox = self._unavailable_root()
        with mock.patch('os.getcwd', return_value=sandbox):
            with self.assertRaises(ValueError) as caught:
                _run(self.service.upload_sticker_asset(_PNG_BYTES))
        self.assertIn('表情库根目录不可用', str(caught.exception))
        self.assertEqual(os.listdir(sandbox), [])
        self.assertEqual(self.database.count('interlude_sticker'), 0, '拒绝即不许建档')

    def test_an_unavailable_root_keeps_read_only_views_empty(self):
        """闸只装在**动盘**那一侧：只读视图照旧回空列表，不抛、也不写。"""
        self._unavailable_root()
        self.assertEqual(_run(self.service.sticker_group_directories()), [])


class _DeclaringProvider:
    """宿主 Provider 桩：`meta().id` + `provider_config`（模型名与声明的模态）。

    `_ProviderStub` 只有 `meta()`；"能力声明"这一列读的是 `provider_config['modalities']`，
    所以这条链上的夹具必须两样都给。
    """

    def __init__(self, provider_id, modalities=(), model=''):
        self.provider_config = {
            'id': provider_id,
            'model': model or provider_id,
            'modalities': list(modalities),
        }

    def meta(self):
        return type('Meta', (), {'id': self.provider_config['id']})()


def _bridge_with_providers(config, providers):
    """`_make_bridge(config)` + 一个装了 `providers`（{id: 模态}）的宿主列表。

    注意顺序：`_make_bridge` 构造时服务就已经解析过路由（合成绑定行走的是配置，
    与宿主列表无关），这里换掉的只是"读模态"要用的那两份入口。
    """
    bridge = _make_bridge(config)
    stubs = {pid: _DeclaringProvider(pid, mods) for pid, mods in providers.items()}
    bridge.context.get_all_providers = lambda: list(stubs.values())
    bridge.provider_by_id = lambda pid: stubs.get(pid)  # type: ignore[method-assign]
    return bridge


class ModelCenterRoutingSourceTests(unittest.TestCase):
    """「任务 → 模型」表的**来源 / 连接 / 模型 / 能力声明 / 判定**必须同一处判据（v1.9.4）。

    真机症状：时间导演那一行"来源=连接行 + 判定=assigned-provider + 连接写着
    AstrBot · xxx"三件事互相矛盾。根因不是路由走了另一条轨（它本来就跟随压缩），
    而是显示层把"来源"另按 `task_model_id(任务)` 算了一份——`timeline` 没有自己的
    指名键，于是同一行里两套判据各说各话。
    """

    #: 每个任务被指名的 Provider（夹具里各给一个不同的，好让断言能抓错配）。
    #: `timeline` 跟随 `compaction`：这是上游的接线（`resolve_model_routing` 里
    #: timeline 直接复用 compaction 的候选），不是本移植版的自由发挥。
    NAMED = {
        'main': 'p-main',
        'compaction': 'p-compact',
        'timeline': 'p-compact',
        'alter': 'p-alter',
        'embedding': 'p-embed',
        'stickers': 'p-stickers',
        'vision': 'p-vision',
    }
    MODALITIES = {
        'p-main': ['text'],
        'p-compact': ['text', 'tool_use'],
        'p-alter': ['text'],
        'p-embed': ['text'],
        'p-stickers': ['image'],
        'p-vision': ['image'],
    }

    def _payload(self):
        config = {
            'model_center': {
                'main_provider_id': 'p-main',
                'compaction_provider_id': 'p-compact',
                'alter_provider_id': 'p-alter',
                'embedding': {'enabled': True, 'provider_id': 'p-embed'},
                'vision': {'enabled': True, 'mode': 'sidecar', 'provider_id': 'p-vision'},
                'providers': [],
            },
            'stickers': {'provider_id': 'p-stickers'},
        }
        bridge = _bridge_with_providers(config, self.MODALITIES)
        return bridge, _run(ConsoleApi(bridge).models())

    def test_every_task_row_takes_its_source_and_modalities_from_the_route(self):
        """参数化：**每个**任务（不只时间导演）都用同一条判据推来源与能力声明。"""
        _, payload = self._payload()
        rows = {row['task']: row for row in payload['tasks']}
        self.assertEqual(set(rows), {key for key, _ in CONSOLE_TASKS})
        for key, provider_id in self.NAMED.items():
            with self.subTest(task=key):
                row = rows[key]
                self.assertEqual(row['source'], 'astrbot', row)
                self.assertEqual(row['source_label'], 'AstrBot', row)
                self.assertEqual(row['provider_label'], 'AstrBot · %s' % provider_id, row)
                self.assertEqual(row['model'], provider_id, row)
                self.assertEqual(row['reason'], 'assigned-provider', row)
                self.assertEqual(
                    payload['task_models'][key]['astrbot_provider'], provider_id,
                    '来源那一列说 AstrBot，就只能是这个 Provider',
                )
                self.assertEqual(
                    payload['task_models'][key]['modalities'], sorted(self.MODALITIES[provider_id]),
                    '能力声明必须来自**同一个** Provider',
                )

    def test_timeline_reports_the_provider_compaction_follows(self):
        """回归：时间导演与压缩同源（没有独立指名键时不许被报成"连接行"）。"""
        bridge, payload = self._payload()
        rows = {row['task']: row for row in payload['tasks']}
        self.assertEqual(bridge.task_model_id('timeline'), '', '夹具前提：timeline 没有自己的键')
        self.assertEqual(rows['timeline']['source'], rows['compaction']['source'])
        self.assertEqual(rows['timeline']['provider_label'], rows['compaction']['provider_label'])
        self.assertEqual(rows['timeline']['reason'], rows['compaction']['reason'])
        self.assertEqual(
            payload['task_models']['timeline']['modalities'],
            payload['task_models']['compaction']['modalities'],
        )
        self.assertEqual(payload['task_models']['timeline']['modalities'], ['text', 'tool_use'])

    def test_a_task_that_only_has_a_connection_row_reports_the_connection(self):
        """② 只勾连接行 → 来源=连接行（参数化到每个任务）。"""
        config = {
            'model_center': {
                'providers': [{
                    'label': '直连A', 'enabled': True, 'model': 'demo',
                    'endpoint': 'https://gw.example.com/v1/chat/completions',
                    'use_for_main': True, 'use_for_stickers': True, 'use_for_vision': True,
                }],
                'embedding': {'enabled': True},
            },
        }
        payload = _run(ConsoleApi(_make_bridge(config)).models())
        rows = {row['task']: row for row in payload['tasks']}
        for key, _label in CONSOLE_TASKS:
            with self.subTest(task=key):
                self.assertEqual(rows[key]['source'], 'connection', rows[key])
                self.assertEqual(rows[key]['source_label'], '连接行', rows[key])
                self.assertEqual(rows[key]['provider_label'], '直连A', rows[key])
                self.assertEqual(payload['task_models'][key]['astrbot_provider'], '')

    def test_a_task_with_neither_names_nor_connections_is_not_configured(self):
        """③ 没指名、没连接、**也没被关掉** → **未配置**（参数化到每个任务，断言字面量）。

        这一刻 core 的路由**就是 `unavailable`**（`resolve_route` 只在没有候选时给
        `available: False`），`create_narrator` 于是交 `SilentNarrator` —— 运行期根本
        不会去调什么"默认 Provider"。旧断言写的是 `source == 'default'`（前端翻成
        「默认 Provider」），那正是"界面与运行期相反"；本轮由用户点名改掉。

        ⚠️ 夹具里 `embedding` 没开 → 那一行的判定就是 `disabled`，归第四档「已关闭」
        （见 `test_explicitly_disabled_features_say_closed_not_unconfigured`）。
        这里把那一行**显式**钉住再继续，不许写成"跳过 disabled 行"——被跳过的行
        正是这一档要防的漏网。
        """
        payload = _run(ConsoleApi(_make_bridge({'model_center': {'providers': []}})).models())
        self.assertEqual(
            {row['task'] for row in payload['tasks']}, {key for key, _ in CONSOLE_TASKS},
            '参数化覆盖每个任务（不许给时间导演之类单独特判）',
        )
        self.assertEqual(
            [row['task'] for row in payload['tasks'] if row['reason'] == 'disabled'],
            ['embedding'], '夹具前提：只有没开的 embedding 被显式关掉',
        )
        for row in payload['tasks']:
            with self.subTest(task=row['task']):
                if row['task'] == 'embedding':
                    self.assertEqual(row['source'], 'disabled', row)
                    self.assertEqual(row['source_label'], '已关闭', row)
                    continue
                self.assertEqual(row['source'], 'none', row)
                self.assertEqual(row['source_label'], '未配置', row)
                self.assertEqual(row['provider_label'], '')
                self.assertEqual(row['candidates'], 0)
                self.assertFalse(row['available'], 'core 的判定就是 unavailable')
        # 这一页一个字都不许再写"默认 Provider"（模型页 / 总览页共用的就是这份 payload）。
        self.assertNotIn('默认 Provider', json.dumps(payload, ensure_ascii=False))

    def test_explicitly_disabled_features_say_closed_not_unconfigured(self):
        """④ 显式关掉的功能 → 第四档 **`已关闭`**，同一行不再"两个说法"（v1.9.4 §60）。

        真机症状：`compaction` / `embedding` 关掉后，「来源」列写 `未配置`，同一行的
        「判定」列却写 `disabled` —— 一行两个说法。判据仍然只有一处：就是这一行路由表里
        core 已经算好的那个 `reason`，不是另拿配置开关再算一遍。
        """
        config = {'model_center': {
            'providers': [],
            'compaction': {'enabled': False},
            'embedding': {'enabled': False},
        }}
        payload = _run(ConsoleApi(_make_bridge(config)).models())
        rows = {row['task']: row for row in payload['tasks']}
        # 关掉的两个（以及跟随压缩的时间导演）→ 已关闭。
        for key in ('compaction', 'timeline', 'embedding'):
            with self.subTest(task=key):
                self.assertEqual(rows[key]['reason'], 'disabled', rows[key])
                self.assertEqual(rows[key]['source'], 'disabled', rows[key])
                self.assertEqual(rows[key]['source_label'], '已关闭', rows[key])
                self.assertNotEqual(rows[key]['source_label'], '未配置', '一行两个说法就是本轮要修的')
                self.assertFalse(rows[key]['available'], rows[key])
                self.assertEqual(rows[key]['candidates'], 0, rows[key])
        # 没被关掉的普通任务仍然说"未配置"（不许因为这一档而全体改成"已关闭"）。
        for key in ('main', 'alter', 'stickers', 'vision'):
            with self.subTest(task=key):
                self.assertEqual(rows[key]['reason'], 'unavailable', rows[key])
                self.assertEqual(rows[key]['source'], 'none', rows[key])
                self.assertEqual(rows[key]['source_label'], '未配置', rows[key])
        # 总览页与模型页共用这一处（同一个 `_routing_rows()`）——两页不许各说一句。
        overview = _run(ConsoleApi(_make_bridge(config)).overview())
        self.assertEqual(
            {row['task']: row['source_label'] for row in overview['routing']},
            {row['task']: row['source_label'] for row in payload['tasks']},
            '总览页 / 模型页来源文案必须同源',
        )

    def test_each_row_never_says_two_things_about_one_row(self):
        """⑤ 不变式（参数化到每个任务 × 每种配置）：判定 `disabled` ⇔ 来源 `已关闭`。

        只要判定列说 `disabled`、来源列就必须说「已关闭」；反过来来源说「已关闭」也
        只能是判定真的 `disabled`。**另一头也钉住**：`embedding` 开着但没有候选
        （`enabled: True` + 空连接池）是 `unavailable`，仍然说「未配置」——
        不许把"没配"也塞进「已关闭」。
        """
        connection = {
            'label': '直连A', 'enabled': True, 'model': 'demo',
            'endpoint': 'https://gw.example.com/v1/chat/completions', 'use_for_main': True,
        }
        configs = {
            '空配置': {'model_center': {'providers': []}},
            '两个功能关掉': {'model_center': {
                'providers': [], 'compaction': {'enabled': False}, 'embedding': {'enabled': False},
            }},
            'Alter 关掉': {'model_center': {'providers': []}, 'alter_system': {'enabled': False}},
            '开关开着但没候选': {'model_center': {'providers': [], 'embedding': {'enabled': True}}},
            '有连接行': {'model_center': {'providers': [connection]}},
            '关闭 + 有连接行': {'model_center': {
                'providers': [connection], 'compaction': {'enabled': False},
            }},
        }
        seen: set[str] = set()
        scenes: list[tuple[str, dict[str, Any]]] = [
            (name, _run(ConsoleApi(_make_bridge(config)).models()))
            for name, config in configs.items()
        ]
        # 指名 AstrBot Provider 那一档要借夹具的宿主 Provider 桩（`_payload`），
        # 单靠 `_make_bridge` 造不出合成绑定行。
        scenes.append(('指名 Provider', self._payload()[1]))
        for name, payload in scenes:
            for row in payload['tasks']:
                with self.subTest(config=name, task=row['task']):
                    seen.add(row['source'])
                    disabled = row['reason'] == 'disabled'
                    self.assertEqual(
                        row['source'] == 'disabled', disabled,
                        '判定 disabled ⇔ 来源 已关闭（同一行一个说法）',
                    )
                    if disabled:
                        self.assertEqual(row['source_label'], '已关闭', row)
                    else:
                        self.assertNotEqual(row['source_label'], '已关闭', row)
                    # 文案唯一来源：标签必须就是那张表给这一档的那一个词。
                    self.assertEqual(
                        row['source_label'], console_module.ROUTING_SOURCE_LABELS[row['source']], row,
                    )
        self.assertEqual(seen, {'astrbot', 'connection', 'disabled', 'none'}, '四档都要被覆盖到')

    def test_the_page_follows_a_config_change_without_a_restart(self):
        """④ 改完配置 → 这一页（以及真实路由）立刻跟着变（v1.9.4 §61）。

        真机症状：在控制台加 / 改完连接，页面与运行期都还是启动时那份。
        """
        bridge = _make_bridge({'model_center': {'providers': []}})
        rows = {row['task']: row for row in _run(ConsoleApi(bridge).models())['tasks']}
        self.assertEqual(rows['main']['source'], 'none')
        self.assertFalse(rows['main']['available'])

        bridge.apply_config({'model_center': {'providers': [{
            'label': '直连A', 'enabled': True, 'model': 'demo',
            'endpoint': 'https://gw.example.com/v1/chat/completions',
            'use_for_main': True,
        }]}})

        rows = {row['task']: row for row in _run(ConsoleApi(bridge).models())['tasks']}
        self.assertEqual(rows['main']['source'], 'connection')
        self.assertEqual(rows['main']['source_label'], '连接行')
        self.assertTrue(rows['main']['available'])
        self.assertEqual(rows['main']['reason'], 'assigned-provider')
        self.assertEqual(rows['main']['candidates'], 1)

    def test_the_source_labels_come_from_one_place(self):
        """来源文案只有一处：四档 → 四个词，且不含被推翻的那句。

        `disabled` 是第四档（显式关闭），不是第二套判据——它就取自同一行路由表里
        core 算好的 `reason`。
        """
        self.assertEqual(
            console_module.ROUTING_SOURCE_LABELS,
            {'astrbot': 'AstrBot', 'connection': '连接行', 'disabled': '已关闭', 'none': '未配置'},
        )
        self.assertNotIn('默认 Provider', console_module.ROUTING_SOURCE_LABELS.values())
        self.assertEqual(
            len(set(console_module.ROUTING_SOURCE_LABELS.values())),
            len(console_module.ROUTING_SOURCE_LABELS),
            '四档四个词，不许两档共用一个词（否则前端画出来还是"两个说法"）',
        )

    def test_keys_outside_the_seven_task_table_keep_the_bridge_entry_points(self):
        """`audio` / `works` 不在七任务表里 → 判据仍退回桥接层原入口（双读不破）。

        本轮的第三档改动只动七任务表那一处，不许顺手把这俩也改了。
        """
        bridge = _make_bridge({'model_center': {
            'providers': [],
            'audio': {'enabled': True, 'provider_id': 'whisper'},
        }})
        payload = _run(ConsoleApi(bridge).models())
        self.assertNotIn('audio', {row['task'] for row in payload['tasks']})
        self.assertNotIn('works', {row['task'] for row in payload['tasks']})
        self.assertEqual(payload['task_models']['audio']['astrbot_provider'], 'whisper')
        self.assertEqual(bridge.task_model_id('audio'), 'whisper', 'audio 仍走原入口')
        self.assertEqual(
            ConsoleApi(bridge)._named_provider('audio'), 'whisper',
            '不在七任务表里的键，判据退回桥接层原入口',
        )
        self.assertEqual(payload['task_models']['works']['astrbot_provider'], '')

    def test_the_provider_list_says_which_tasks_actually_use_it(self):
        """`used_by` 与来源同一处判据：时间导演要算在它真正调用的那个 Provider 上。"""
        _, payload = self._payload()
        used = {row['id']: row['used_by'] for row in payload['astrbot_providers']}
        self.assertIn('时间导演', used['p-compact'])
        self.assertIn('压缩与总结', used['p-compact'])
        self.assertIn('主叙事', used['p-main'])
        self.assertNotIn('时间导演', used['p-main'])

    def test_the_works_writer_keeps_its_dual_read_binding(self):
        """`works` 不在七任务路由表里 → 仍旧走桥接层的双读判定（老口径不许失效）。"""
        bridge = _make_bridge({'works': {'enabled': True, 'model_id': 'writer-conn'},
                               'model_center': {'providers': [
                                   {'id': 'writer-conn', 'label': '写手连接', 'enabled': True,
                                    'model': 'w', 'endpoint': 'https://gw.example.com/v1/chat/completions'},
                               ]}})
        bridge.context.get_all_providers = lambda: [_ProviderStub('ollama')]
        payload = _run(ConsoleApi(bridge).models())
        self.assertEqual(payload['task_models']['works']['astrbot_provider'], '')
        self.assertEqual(bridge.task_model_id('works'), 'writer-conn', '值本身不动')


class TokenStatsAvailabilityTests(unittest.TestCase):
    """Token 统计页的口径：有调用没 token 时显示「—」，不拿一排 0 冒充统计（v1.9.4）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.bridge = _make_bridge({})
        self.database = Database(':memory:')
        self.addCleanup(self.database.close)
        self.database.register_tables()
        self.bridge.db = self.database
        self.api = ConsoleApi(self.bridge)

    def _insert(self, day, task, model, *, calls, input_tokens=0, output_tokens=0, cached=0, provider=''):
        self.database.insert('interlude_token_usage', {
            'day': day, 'storyId': 's1', 'task': task, 'model': model, 'provider': provider,
            'inputTokens': input_tokens, 'outputTokens': output_tokens, 'cachedTokens': cached,
            'calls': calls, 'createdAt': '2026-01-01T00:00:00Z', 'updatedAt': '2026-01-01T00:00:00Z',
        })

    def test_calls_without_tokens_are_counted_and_flagged_unavailable(self):
        today = datetime.now().date().isoformat()
        self._insert(today, '主叙事', 'AstrBot · p', calls=4, provider='AstrBot · p')
        payload = _run(self.api.token_stats('week'))
        self.assertEqual(payload['totals']['calls'], 4, '经宿主 Provider 的调用次数必须数上')
        self.assertFalse(payload['totals']['hasTokens'])
        self.assertFalse(payload['byTask'][0]['hasTokens'])
        self.assertFalse(payload['byModel'][0]['hasTokens'])
        self.assertFalse(payload['series'][-1]['hasTokens'])
        self.assertEqual(payload['byTask'][0]['task'], '主叙事')
        self.assertEqual(payload['byModel'][0]['provider'], 'AstrBot · p')

    def test_token_rows_keep_their_numbers(self):
        today = datetime.now().date().isoformat()
        self._insert(today, '主叙事', 'm', calls=2, input_tokens=1000, output_tokens=200, cached=400)
        payload = _run(self.api.token_stats('week'))
        self.assertTrue(payload['totals']['hasTokens'])
        self.assertEqual(payload['totals']['inputTokens'], 1000)
        self.assertEqual(payload['totals']['calls'], 2)
        self.assertTrue(payload['byTask'][0]['hasTokens'])

    def test_a_day_without_calls_is_zero_not_unavailable(self):
        """空白天补的 0 是真 0（那天没调用），不该被当成"拿不到用量"。"""
        payload = _run(self.api.token_stats('week'))
        self.assertEqual(payload['totals']['calls'], 0)
        self.assertFalse(payload['totals']['hasTokens'])
        self.assertEqual(payload['series'][0]['calls'], 0)

    def test_mixed_rows_report_availability_per_row(self):
        today = datetime.now().date().isoformat()
        self._insert(today, '主叙事', 'm', calls=1, input_tokens=10)
        self._insert(today, '压缩', 'AstrBot · p', calls=3)
        payload = _run(self.api.token_stats('week'))
        self.assertTrue(payload['totals']['hasTokens'])
        by_task = {row['task']: row for row in payload['byTask']}
        self.assertTrue(by_task['主叙事']['hasTokens'])
        self.assertFalse(by_task['压缩']['hasTokens'], '这一桶只有次数、没有 token')
        self.assertEqual(by_task['压缩']['calls'], 3)


class UsageLedgerFromProviderCallTests(unittest.TestCase):
    """端到端：**经宿主 Provider 的一次调用** → 账本一行 → 页面上的具体数字（v1.9.4）。

    这条链以前断在两处：宿主的 `TokenUsage` 字段名读不到（token 全 0），以及
    "没有 token 就不 emit"（次数也 0）。这里从传输层一路走到「Token 统计」页。
    """

    def _bridge(self, usage):
        context = _RecordingFakeContext(usage=usage)
        bridge = _make_bridge(
            {'model_center': {'main_provider_id': 'ollama', 'providers': []}},
            context=context,
        )
        database = Database(':memory:')
        self.addCleanup(database.close)
        database.register_tables()
        bridge.db = database
        bridge.service.db = database
        return bridge

    def _call_and_record(self, bridge):
        body = {'model': 'ollama', 'messages': [{'role': 'user', 'content': 'hi'}]}
        response = _run(bridge.http_client.post_json('', None, body, None, task='main'))
        # core 交给账本的那条增量：`_emit_usage` 组装、`report_token_usage` 收到的就是它。
        record = {
            'task': '主叙事', 'provider_label': 'AstrBot · ollama',
            'model': response['model'], **parse_token_usage(response['usage']), 'calls': 1,
        }
        self.assertTrue(_run(bridge.service.record_token_usage(record, 's1')))
        return response

    def test_a_call_without_usage_is_still_counted_on_the_page(self):
        bridge = self._bridge({})
        self._call_and_record(bridge)
        payload = _run(ConsoleApi(bridge).token_stats('week'))
        self.assertEqual(payload['totals']['calls'], 1, '连次数都是 0 就是这次要修的硬缺陷')
        self.assertEqual(payload['byTask'][0]['task'], '主叙事')
        self.assertEqual(payload['byModel'][0]['model'], 'ollama')
        self.assertEqual(payload['byModel'][0]['provider'], 'AstrBot · ollama', '目标也要记上')
        self.assertFalse(payload['totals']['hasTokens'], '没回用量 → 页面显示不可用，不冒充 0')

    def test_a_call_with_host_usage_lands_with_its_tokens(self):
        bridge = self._bridge(_FakeTokenUsage(input_other=30, input_cached=70, output=12))
        self._call_and_record(bridge)
        payload = _run(ConsoleApi(bridge).token_stats('week'))
        self.assertEqual(payload['totals']['calls'], 1)
        self.assertEqual(payload['totals']['inputTokens'], 100, 'prompt_tokens 含缓存')
        self.assertEqual(payload['totals']['outputTokens'], 12)
        self.assertEqual(payload['totals']['cachedTokens'], 70)
        self.assertAlmostEqual(payload['totals']['hitRate'], 0.7)
        self.assertTrue(payload['totals']['hasTokens'])
