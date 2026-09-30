# -*- coding: utf-8 -*-
"""平台动作执行层的单元测试（`Transport.platform_action` / `call_onebot` / `set_input_status`）。

覆盖范围（任务书要求的全部要点）：

1. **目录全覆盖**：`core/platform_actions.ACTIONS` 的 59 条动作在
   `astrbot_bridge._PLATFORM_CALLS` 里**每一条都有映射**（少一条就红）；
2. 未知动作 / 未知参数 / 类型越界 / 缺必填 → `ok=False` + 中文原因，绝不抛；
3. 平台直通：桩 client 收到**正确的 action 与参数**（含改名、改名后仍同名、
   枚举翻译、平台固定默认值、会话坐标缺省）；
4. **回执校验**：`status=='ok'` 或 `retcode==0` 才算成功；失败时 error 带上
   status/retcode/message；传输异常时 error 以「结果未知，请勿自动重试」结尾
   并标 `ambiguous`；
5. `SendResult['messageIds']` 从宿主返回值里提取（dict / 嵌套 / 列表 / 对象 /
   bool 各种形状），以及 `recall_message target=last` 的两条定位路径
   （已投递记录 → 历史里机器人自己发的那条）；
6. 回执裁剪：历史消息 ≤50、联系人 ≤200、群成员 ≤100，且每条只留必要字段；
7. `set_input_status` 的 on/off → `event_type` 1/2；
8. 平台不支持（非 OneBot 平台 / 无客户端 / 目录里标 `@unsupported` 的排程动作）
   一律 `ok=False` + warn 级可见日志；
9. `NullTransport` 的三个新方法仍然安全降级。

运行环境说明：系统 Python 里没有 AstrBot，本文件在导入被测模块之前先装一套
**最小 AstrBot 桩**（只实现被测代码真正用到的接口）。桩宿主 / 桩 OneBot client
由本文件自己的 `Fake*` 类提供，不依赖别的测试文件（并行开发时互不牵连）。

独立运行：
    cd <仓库根目录>
    python3 -m unittest plugin.tests.test_platform_transport -v
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(HERE)
REPO_ROOT = os.path.dirname(PLUGIN_ROOT)


# =========================================================================== #
# 最小 AstrBot 桩（与 test_astrbot_bridge.py 同源，只留本文件需要的部分）
# =========================================================================== #

class _StubComponent:
    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


class Plain(_StubComponent):
    def __init__(self, text, **kwargs):
        super().__init__(text=text, **kwargs)


class Image(_StubComponent):
    def __init__(self, file='', url='', path=None, **kwargs):
        super().__init__(file=file, url=url, path=path, **kwargs)

    @staticmethod
    def fromFileSystem(path, **_kwargs):
        return Image(file='file://%s' % os.path.abspath(path), path=path)


class Record(_StubComponent):
    def __init__(self, file='', url='', path=None, **kwargs):
        super().__init__(file=file, url=url, path=path, **kwargs)


class Face(_StubComponent):
    def __init__(self, id=0, **kwargs):  # noqa: A002 - 与 AstrBot 字段名一致
        super().__init__(id=id, **kwargs)


class At(_StubComponent):
    def __init__(self, qq='', name='', **kwargs):
        super().__init__(qq=qq, name=name, **kwargs)


class Reply(_StubComponent):
    def __init__(self, id='', chain=None, **kwargs):  # noqa: A002
        super().__init__(id=id, chain=chain or [], **kwargs)


class File(_StubComponent):
    def __init__(self, name='', file_='', url='', size=None, **kwargs):
        super().__init__(name=name, file_=file_, file=file_, url=url, size=size, **kwargs)


class Video(_StubComponent):
    def __init__(self, file='', **kwargs):
        super().__init__(file=file, **kwargs)


class MessageChain:
    def __init__(self, chain=None, **kwargs):
        self.chain = list(chain or [])


def _install_astrbot_stub() -> None:
    """把最小 AstrBot 桩装进 `sys.modules`。

    **刻意不设 `_hdsi_stub` 标记**：本文件这套桩只实现桥真正用到的接口，一旦打了
    标记，后跑的 `test_astrbot_bridge.py` 会以为"桩已经有了"而跳过它那份完整桩，
    于是 `plugin.main` 的 `from astrbot.api import logger` 直接 ImportError——
    单独跑本文件全绿、按顺序跑就炸（坑 16 的同一类顺序污染）。别人装过完整桩时
    我们直接复用（它的组件类是我们的超集）。
    """
    existing = sys.modules.get('astrbot')
    if existing is not None and getattr(existing, '_hdsi_stub', False):
        return

    def module(name):
        created = types.ModuleType(name)
        sys.modules[name] = created
        return created

    astrbot = module('astrbot')
    api = module('astrbot.api')

    event_module = module('astrbot.api.event')
    event_module.AstrMessageEvent = type('AstrMessageEvent', (), {})
    event_module.MessageChain = MessageChain
    event_module.filter = types.SimpleNamespace()

    components = module('astrbot.api.message_components')
    for name, cls in (
        ('Plain', Plain), ('Image', Image), ('Record', Record), ('Face', Face),
        ('At', At), ('Reply', Reply), ('File', File), ('Video', Video),
    ):
        setattr(components, name, cls)

    star_module = module('astrbot.api.star')
    star_module.Context = type('Context', (), {})
    star_module.Star = type('Star', (), {})

    core = module('astrbot.core')
    core_message = module('astrbot.core.message')
    core_components = module('astrbot.core.message.components')
    for name in ('Plain', 'Image', 'Record', 'Face', 'At', 'Reply', 'File', 'Video'):
        setattr(core_components, name, getattr(components, name))
    core_result = module('astrbot.core.message.message_event_result')
    core_result.MessageChain = MessageChain
    core_message.components = core_components
    core_utils = module('astrbot.core.utils')
    astrbot_path = module('astrbot.core.utils.astrbot_path')
    astrbot_path.get_astrbot_data_path = lambda: PLUGIN_ROOT

    core.message = core_message
    core.utils = core_utils
    api.event = event_module
    api.star = star_module
    api.message_components = components
    astrbot.api = api
    astrbot.core = core


_install_astrbot_stub()

from plugin.core import logging as interlude_logging  # noqa: E402
from plugin.core import platform_actions as pa  # noqa: E402
from plugin.core.service.transport import NullTransport  # noqa: E402
from plugin.adapters import astrbot_bridge as bridge_module  # noqa: E402
from plugin.adapters.astrbot_bridge import (  # noqa: E402
    AstrbotBridge,
    endpoint_for_event,
    session_view,
)


# =========================================================================== #
# 桩：OneBot client / 平台实例 / 宿主 Context / 事件
# =========================================================================== #

class FakeOneBotClient:
    """记录每一次 `call_action(action, **params)`，按脚本回帧。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        #: action → 回帧；值是 dict 直接返回，是可调用的就用它的返回值。
        self.frames: dict[str, object] = {}
        self.default_frame: object = {'status': 'ok', 'retcode': 0, 'data': {}}
        self.raise_error: BaseException | None = None

    async def call_action(self, action: str, **params):
        self.calls.append((action, dict(params)))
        if self.raise_error is not None:
            raise self.raise_error
        frame = self.frames.get(action, self.default_frame)
        return frame() if callable(frame) else frame

    # ---- 断言辅助 ----
    @property
    def actions(self) -> list[str]:
        return [name for name, _params in self.calls]

    def params_of(self, action: str) -> dict:
        for name, params in self.calls:
            if name == action:
                return params
        raise AssertionError('桩 client 没收到动作 %s（收到的是 %s）' % (action, self.actions))


class _FakePlatformMeta:
    def __init__(self, name: str, platform_id: str) -> None:
        self.name = name
        self.id = platform_id


class FakePlatformInstance:
    def __init__(self, client=None, name: str = 'aiocqhttp', platform_id: str = 'NapCat') -> None:
        self._meta = _FakePlatformMeta(name, platform_id)
        self.bot = client

    def meta(self):
        return self._meta


class FakeTTSProvider:
    def __init__(self, path: str = '', config=None, error: BaseException | None = None) -> None:
        self.provider_config = config or {
            'id': 'edge-tts', 'model': 'edge-tts', 'edge-tts-voice': 'zh-CN-XiaoxiaoNeural',
        }
        self.path = path
        self.error = error
        self.texts: list[str] = []

    async def get_audio(self, text: str) -> str:
        self.texts.append(text)
        if self.error is not None:
            raise self.error
        return self.path


class FakeContext:
    def __init__(self, client=None) -> None:
        self.instance = FakePlatformInstance(client)
        self.platform_manager = types.SimpleNamespace(platform_insts=[self.instance])
        self.sent: list[tuple[str, object]] = []
        #: `send_message` 的返回值（宿主 4.28 是 bool；这里可换成别的形状）。
        self.send_result: object = True
        self.send_error: BaseException | None = None
        self.tts_providers: list[object] = []

    # ---- 宿主 API ----
    def get_platform_inst(self, platform_id):
        return self.instance if platform_id == self.instance.meta().id else None

    async def send_message(self, umo, chain):
        self.sent.append((umo, chain))
        if self.send_error is not None:
            raise self.send_error
        return self.send_result

    def get_all_tts_providers(self):
        return list(self.tts_providers)

    async def get_using_tts_provider_async(self, umo=None):  # noqa: ARG002
        return self.tts_providers[0] if self.tts_providers else None


class FakeDatabase:
    def __init__(self, path):
        self.path = path

    def register_tables(self):
        return []

    def get(self, _table, _where):
        return None

    def all(self, *_args, **_kwargs):
        return []

    def close(self):
        return None


class FakeMessageObj:
    def __init__(self, message_id='m-1'):
        self.message_id = message_id


class FakeMessageEvent:
    """符合 AstrBot getter 契约的假事件（**只有方法**，没有同名属性）。"""

    def __init__(
        self,
        *,
        platform_name: str = 'aiocqhttp',
        platform_id: str = 'NapCat',
        self_id: str = '100001357',
        sender_id: str = '1000008890',
        sender_name: str = '主人',
        group_id: str = '',
        message_id: str = 'm-1',
    ) -> None:
        self._platform_name = platform_name
        self._platform_id = platform_id
        self._self_id = self_id
        self._sender_id = sender_id
        self._sender_name = sender_name
        self._group_id = group_id
        self.message_obj = FakeMessageObj(message_id)
        session_id = group_id or sender_id
        self.unified_msg_origin = '%s:%s:%s' % (
            platform_id, 'GroupMessage' if group_id else 'FriendMessage', session_id,
        )

    def get_platform_name(self):
        return self._platform_name

    def get_platform_id(self):
        return self._platform_id

    def get_message_str(self):
        return ''

    def get_messages(self):
        return []

    def get_session_id(self):
        return self._group_id or self._sender_id

    def get_group_id(self):
        return self._group_id

    def get_self_id(self):
        return self._self_id

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return self._sender_name

    def is_private_chat(self):
        return not self._group_id


# =========================================================================== #
# 用例基类
# =========================================================================== #

class PlatformTransportTestCase(unittest.TestCase):
    """装好桥 + 桩 client，并进入一个「本回合」的会话坐标。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.client = FakeOneBotClient()
        self.context = FakeContext(self.client)
        self.logs: list[tuple[str, str]] = []
        interlude_logging.set_log_sink(self._sink)
        self.addCleanup(interlude_logging.set_log_sink, interlude_logging._default_sink)
        with mock.patch.object(bridge_module, 'Database', FakeDatabase):
            self.bridge = AstrbotBridge(
                context=self.context, config={}, data_dir=self._tmp.name, logger=None,
            )
        # 桥自己可能换过 sink（装了 logger 才会），这里再钉一次。
        interlude_logging.set_log_sink(self._sink)
        self.transport = self.bridge.transport

    def _sink(self, level: str, text: str) -> None:
        self.logs.append((level, text))

    # ---- 工具 ----

    def enter_session(self, *, group_id: str = '', **kwargs):
        event = FakeMessageEvent(group_id=group_id, **kwargs)
        endpoint = endpoint_for_event(event)
        self.bridge.remember_event(event, session_view(event, endpoint), endpoint)
        self.bridge.begin_capture(endpoint)
        self.addCleanup(self.bridge.end_capture)
        return endpoint

    def run_action(self, action, params=None):
        return asyncio.run(self.transport.platform_action(action, params if params is not None else {}))

    def warnings(self) -> list[str]:
        return [text for level, text in self.logs if level in ('warn', 'warning', 'error')]

    def assertWarned(self, fragment: str) -> None:
        joined = '\n'.join(self.warnings())
        self.assertIn(fragment, joined, '没有看到 warn 级日志：%s' % fragment)


# =========================================================================== #
# 1. 目录覆盖与参数拒绝
# =========================================================================== #

class CatalogCoverageTests(PlatformTransportTestCase):

    def test_every_catalog_action_has_a_platform_mapping(self):
        """59 条目录动作**每一条**都要有映射——缺一条就是静默失效。"""
        table = bridge_module._PLATFORM_CALLS
        self.assertEqual(sorted(set(pa.ACTIONS) - set(table)), [], '这些目录动作没有映射')
        self.assertEqual(sorted(set(table) - set(pa.ACTIONS)), [], '映射表里有目录外的动作')
        self.assertEqual(len(table), len(pa.ACTIONS))
        self.assertGreaterEqual(len(table), 59)

    def test_mapping_shape_is_action_name_plus_param_renames(self):
        for action_id, entry in bridge_module._PLATFORM_CALLS.items():
            self.assertIsInstance(entry, tuple, action_id)
            self.assertEqual(len(entry), 2, action_id)
            target, renames = entry
            self.assertIsInstance(target, str, action_id)
            self.assertTrue(target, action_id)
            self.assertIsInstance(renames, dict, action_id)
            for catalog_param, platform_param in renames.items():
                # 映射表里写的目录参数必须是目录里真有的（写错参数名等于没生效）。
                self.assertIsNotNone(pa.ACTIONS[action_id].param(catalog_param), action_id)
                self.assertIsInstance(platform_param, str, action_id)

    def test_session_fill_tables_only_mention_declared_params(self):
        for action_id, fills in bridge_module._SESSION_FILLS.items():
            self.assertIn(action_id, pa.ACTIONS, action_id)
            for param_name in fills:
                self.assertIsNotNone(pa.ACTIONS[action_id].param(param_name), (action_id, param_name))

    def test_unknown_action_is_rejected(self):
        result = self.run_action('make_me_a_sandwich', {})
        self.assertFalse(result['ok'])
        self.assertIn('unknown-platform-action', result['error'])
        self.assertIsNone(result['data'])
        self.assertWarned('未知平台动作')
        self.assertEqual(self.client.calls, [])

    def test_unknown_param_is_rejected(self):
        result = self.run_action('send_like', {'user_id': '2', 'timez': 3})
        self.assertFalse(result['ok'])
        self.assertIn('不支持参数', result['error'])
        self.assertIn('timez', result['error'])
        self.assertEqual(self.client.calls, [])

    def test_camel_case_params_are_accepted(self):
        """模型偶尔写 camelCase：目录双读，映射表仍然按 snake_case 取值。"""
        self.enter_session()
        result = self.run_action('send_like', {'userId': '2', 'times': 3})
        self.assertTrue(result['ok'], result)
        self.assertEqual(self.client.params_of('send_like')['user_id'], '2')

    def test_out_of_range_and_wrong_type_are_rejected(self):
        for params, fragment in (
            ({'user_id': '2', 'times': 99}, '超过上限'),
            ({'user_id': '2', 'times': 'abc'}, '类型不对'),
        ):
            result = self.run_action('send_like', params)
            self.assertFalse(result['ok'], params)
            self.assertIn(fragment, result['error'])
        self.assertEqual(self.client.calls, [])

    def test_missing_required_param_is_rejected(self):
        result = self.run_action('set_group_ban', {'duration': 60})
        self.assertFalse(result['ok'])
        self.assertIn('user_id', result['error'])
        self.assertEqual(self.client.calls, [])

    def test_garbage_params_never_raise(self):
        self.enter_session()
        for params in (None, [], 'nope', 42, {'user_id': object()}):
            result = asyncio.run(self.transport.platform_action('send_like', params))
            self.assertIn('ok', result)
            self.assertIn('error', result)
            self.assertIn('data', result)

    @staticmethod
    def _sample_params(action):
        """按目录声明造一组合法参数（枚举取第一个，数值取下限）。"""
        params = {}
        for param in action.params:
            if param.choices:
                params[param.name] = param.choices[0]
            elif param.type == 'int':
                params[param.name] = int(param.minimum if param.minimum is not None else 1)
            elif param.type == 'bool':
                params[param.name] = True
            elif param.type == 'list':
                params[param.name] = ['x']
            elif param.type == 'object':
                params[param.name] = {'a': 1}
            else:
                params[param.name] = '示例'
        return params

    def test_every_action_id_returns_the_result_contract(self):
        """**每一条**目录动作都要能跑完并回一个合法 `SendResult`（绝不抛）。"""
        self.enter_session(group_id='7788')
        for action_id, action in pa.ACTIONS.items():
            self.client.calls.clear()
            self.client.default_frame = {'status': 'ok', 'retcode': 0, 'data': {}}
            result = self.run_action(action_id, self._sample_params(action))
            self.assertIsInstance(result, dict, action_id)
            self.assertTrue(result.get('ok') or result.get('error'), action_id)
            self.assertIsInstance(result['error'], str, action_id)
            self.assertIn('data', result, action_id)
            self.assertEqual(result['action'], action_id)


# =========================================================================== #
# 2. 平台直通：动作名与参数
# =========================================================================== #

class PlatformCallMappingTests(PlatformTransportTestCase):

    def test_special_title_and_group_name_renames(self):
        self.enter_session(group_id='7788')
        result = self.run_action('set_group_special_title', {'user_id': '2', 'title': '小可爱'})
        self.assertTrue(result['ok'], result)
        self.assertEqual(
            self.client.params_of('set_group_special_title'),
            {'group_id': '7788', 'user_id': '2', 'special_title': '小可爱'},
        )
        self.run_action('set_group_name', {'name': '新群名'})
        self.assertEqual(
            self.client.params_of('set_group_name'), {'group_id': '7788', 'group_name': '新群名'},
        )

    def test_delete_friend_block_maps_to_temp_block(self):
        self.enter_session()
        self.run_action('delete_friend', {'user_id': '2', 'block': True})
        self.assertEqual(
            self.client.params_of('delete_friend'), {'user_id': '2', 'temp_block': True},
        )

    def test_recall_message_uses_delete_msg(self):
        self.enter_session()
        result = self.run_action('recall_message', {'message_id': 'm-42'})
        self.assertTrue(result['ok'], result)
        self.assertEqual(self.client.params_of('delete_msg'), {'message_id': 'm-42'})

    def test_group_notice_uses_underscore_actions(self):
        self.enter_session(group_id='7788')
        self.run_action('get_group_notice_list', {})
        self.assertEqual(self.client.params_of('_get_group_notice'), {'group_id': '7788'})
        self.run_action('send_group_notice', {'content': '通知'})
        self.assertEqual(
            self.client.params_of('_send_group_notice'), {'group_id': '7788', 'content': '通知'},
        )
        self.run_action('delete_group_notice', {'notice_id': 'n1'})
        self.assertEqual(
            self.client.params_of('_del_group_notice'), {'group_id': '7788', 'notice_id': 'n1'},
        )

    def test_history_pagination_param_and_default_count(self):
        self.enter_session(group_id='7788')
        self.run_action('get_group_msg_history', {'count': 10, 'before': 555})
        self.assertEqual(
            self.client.params_of('get_group_msg_history'),
            {'group_id': '7788', 'count': 10, 'message_seq': 555},
        )

    def test_send_poke_routes_by_session_kind(self):
        self.enter_session(group_id='7788')
        self.run_action('send_poke', {'user_id': '2'})
        self.assertEqual(
            self.client.params_of('group_poke'), {'group_id': '7788', 'user_id': '2'},
        )
        self.enter_session()  # 换成私聊
        self.run_action('send_poke', {'user_id': '2'})
        self.assertEqual(self.client.params_of('friend_poke'), {'user_id': '2'})

    def test_session_defaults_fill_user_and_group(self):
        endpoint = self.enter_session(group_id='7788')
        self.assertEqual(endpoint.user_id, '1000008890')
        self.run_action('send_like', {'times': 3})
        self.assertEqual(
            self.client.params_of('send_like'), {'user_id': '1000008890', 'times': 3},
        )
        # 目录标为必填的群号也能由本回合坐标补上。
        self.run_action('get_group_info', {})
        self.assertEqual(self.client.params_of('get_group_info'), {'group_id': '7788'})

    def test_set_group_card_does_not_fill_user_id(self):
        """目录写的是「user_id 留空＝改机器人自己」，缺省不补。"""
        self.enter_session(group_id='7788')
        self.run_action('set_group_card', {'card': '新名片'})
        self.assertEqual(
            self.client.params_of('set_group_card'), {'group_id': '7788', 'card': '新名片'},
        )

    def test_qq_profile_actions_use_self_id(self):
        self.enter_session()
        self.run_action('get_qq_profile', {})
        self.assertEqual(
            self.client.params_of('get_stranger_info'),
            {'user_id': '100001357', 'no_cache': False},
        )
        self.run_action('get_qq_status', {})
        self.assertEqual(self.client.params_of('nc_get_user_status'), {'user_id': '100001357'})

    def test_update_qq_status_drops_unsupported_fields_with_note(self):
        self.enter_session()
        result = self.run_action('update_qq_status', {'status': 50, 'minutes': 30, 'text': '忙'})
        self.assertTrue(result['ok'], result)
        self.assertEqual(
            self.client.params_of('set_online_status'),
            {'status': 50, 'ext_status': 0, 'battery_status': 0},
        )
        self.assertTrue(any('minutes' in note for note in result.get('notes') or []))

    def test_group_add_option_value_map(self):
        self.enter_session(group_id='7788')
        self.run_action('set_group_add_option', {'option': 'audit'})
        self.assertEqual(
            self.client.params_of('set_group_add_option'), {'group_id': '7788', 'add_type': 2},
        )

    def test_qzone_actions(self):
        self.enter_session()
        result = self.run_action('publish_qzone_post', {'content': '今天天气不错'})
        self.assertTrue(result['ok'], result)
        # 目录写「4 好友（默认）可见」，平台缺省是 1：以目录为准。
        self.assertEqual(
            self.client.params_of('send_qzone_msg'),
            {'content': '今天天气不错', 'ugc_right': 4},
        )
        self.run_action('comment_qzone_post', {'tid': 't1', 'content': '好看'})
        self.assertEqual(
            self.client.params_of('comment_qzone'),
            {'tid': 't1', 'content': '好看'},
        )
        self.run_action('like_qzone_post', {'tid': 't1', 'target_uin': '123'})
        self.assertEqual(
            self.client.params_of('like_qzone'), {'tid': 't1', 'target_uin': '123'},
        )
        self.run_action('list_qzone_posts', {'target_uin': '123', 'count': 5})
        self.assertEqual(
            self.client.params_of('get_qzone_msg_list'), {'target_uin': '123', 'num': 5},
        )
        # 没指归属 QQ = 看好友动态；`count` 在 feeds 上就叫 `count`。
        result = self.run_action('list_qzone_posts', {'count': 5})
        self.assertEqual(
            self.client.params_of('get_qzone_feeds'), {'count': 5, 'page_num': 1},
        )
        self.assertTrue(any('get_qzone_feeds' in note for note in result.get('notes') or []))
        self.run_action('delete_qzone_post', {'tid': 't1'})
        self.assertEqual(self.client.params_of('delete_qzone_msg'), {'tid': 't1'})

    def test_request_actions(self):
        self.enter_session()
        self.run_action('handle_friend_request', {'flag': 'f1', 'approve': True, 'remark': '同事'})
        self.assertEqual(
            self.client.params_of('set_friend_add_request'),
            {'flag': 'f1', 'approve': True, 'remark': '同事'},
        )
        self.run_action('handle_group_request', {'flag': 'f2', 'approve': False, 'sub_type': 'invite'})
        self.assertEqual(
            self.client.params_of('set_group_add_request'),
            {'flag': 'f2', 'approve': False, 'sub_type': 'invite'},
        )


    def test_dispatch_style_params_carry_session_coordinates(self):
        """调度层（`service/chunk12._resolve_action_target`）会把会话坐标混进参数。

        这些键（platform / self_id / is_group / channel_id …）目录里没有，但**必须
        接受**：它们既是"本回合的对话对象"，也是定位平台实例的坐标；拒掉它们等于
        生产里每一条动作都被自己的闸门拦下。
        """
        self.enter_session()
        result = self.run_action('send_poke', {
            'user_id': '10001', 'group_id': '20002', 'channel_id': '20002',
            'platform': 'onebot', 'self_id': '999', 'is_group': True,
        })
        self.assertTrue(result['ok'], result)
        self.assertEqual(
            self.client.params_of('group_poke'), {'group_id': '20002', 'user_id': '10001'},
        )

    def test_session_coordinates_do_not_leak_into_platform_params(self):
        self.enter_session()
        self.run_action('send_like', {
            'user_id': '2', 'platform': 'onebot', 'self_id': '9', 'is_group': False, 'channel_id': '2',
        })
        self.assertEqual(self.client.params_of('send_like'), {'user_id': '2'})

    def test_dispatch_coordinates_alone_can_resolve_the_client(self):
        """后台回合没有登记过事件：坐标只从参数里来，也要能取到平台实例。"""
        result = self.run_action('send_like', {'user_id': '2', 'platform': 'onebot', 'self_id': '100001357'})
        self.assertTrue(result['ok'], result)
        self.assertEqual(self.client.params_of('send_like'), {'user_id': '2'})

    def test_recall_uses_dispatch_group_coordinates_for_the_history_scan(self):
        self.enter_session(group_id='7788')
        self.client.frames['get_group_msg_history'] = {
            'status': 'ok', 'retcode': 0,
            'data': {'messages': [{'message_id': 'm-5', 'sender': {'user_id': '100001357'}}]},
        }
        result = self.run_action('recall_message', {
            'target': 'last', 'group_id': '7788', 'platform': 'onebot',
            'self_id': '100001357', 'is_group': True,
        })
        self.assertTrue(result['ok'], result)
        self.assertEqual(self.client.params_of('delete_msg'), {'message_id': 'm-5'})


# =========================================================================== #
# 3. 回执校验与传输异常
# =========================================================================== #

class ReceiptValidationTests(PlatformTransportTestCase):

    def test_ok_frame_returns_data(self):
        self.enter_session()
        self.client.frames['get_group_root_files'] = {
            'status': 'ok', 'retcode': 0,
            'data': {'files': [{'file_id': 'f1', 'file_name': 'a.txt'}], 'folders': []},
        }
        result = self.run_action('list_group_files', {})
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['data']['files'][0]['file_name'], 'a.txt')

    def test_retcode_zero_counts_as_ok_even_without_status(self):
        self.enter_session()
        self.client.default_frame = {'retcode': 0, 'data': {'ok': True}}
        result = self.run_action('send_like', {'user_id': '2'})
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['data'], {'ok': True})

    def test_failed_frame_carries_status_retcode_message(self):
        self.enter_session()
        self.client.default_frame = {'status': 'failed', 'retcode': 1401, 'message': '权限不足'}
        result = self.run_action('set_group_ban', {'user_id': '2', 'duration': 60, 'group_id': '1'})
        self.assertFalse(result['ok'])
        self.assertIn('1401', result['error'])
        self.assertIn('权限不足', result['error'])
        self.assertEqual(result.get('retcode'), 1401)
        self.assertWarned('执行失败')

    def test_non_mapping_frame_is_a_failure(self):
        self.enter_session()
        self.client.default_frame = None
        result = self.run_action('send_like', {'user_id': '2'})
        self.assertFalse(result['ok'])
        self.assertIn('没有回执', result['error'])

    def test_transport_exception_is_ambiguous(self):
        self.enter_session()
        self.client.raise_error = asyncio.TimeoutError('websocket closed')
        result = self.run_action('send_like', {'user_id': '2'})
        self.assertFalse(result['ok'])
        self.assertTrue(result['error'].endswith(bridge_module._AMBIGUOUS_TAIL), result['error'])
        self.assertTrue(result.get('ambiguous'))
        self.assertWarned(bridge_module._AMBIGUOUS_TAIL)

    def test_call_onebot_direct(self):
        self.enter_session()
        result = asyncio.run(self.transport.call_onebot('send_qzone_msg', {'content': 'hi'}))
        self.assertTrue(result['ok'], result)
        self.assertEqual(self.client.params_of('send_qzone_msg'), {'content': 'hi'})
        self.client.default_frame = {'status': 'failed', 'retcode': 12000, 'message': '风控'}
        result = asyncio.run(self.transport.call_onebot('send_qzone_msg', {'content': 'hi'}))
        self.assertFalse(result['ok'])
        self.assertIn('12000', result['error'])

    def test_call_onebot_without_client(self):
        self.enter_session()
        self.context.instance.bot = None
        result = asyncio.run(self.transport.call_onebot('send_like', {'user_id': '2'}))
        self.assertFalse(result['ok'])
        self.assertIn('OneBot', result['error'])
        self.assertWarned('没有可用的 OneBot 客户端')


# =========================================================================== #
# 4. messageIds 提取与撤回
# =========================================================================== #

class MessageIdExtractionTests(PlatformTransportTestCase):

    def test_bool_return_yields_no_ids(self):
        self.enter_session()
        self.context.send_result = True
        result = asyncio.run(self.transport.send_group('7788', '在的'))
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['messageIds'], [])

    def test_ids_from_dict_and_nested_shapes(self):
        for value, expected in (
            ({'message_id': 'm-9'}, ['m-9']),
            ({'status': 'ok', 'data': {'message_id': 1234}}, ['1234']),
            ([{'message_id': 'a'}, {'message_id': 'b'}], ['a', 'b']),
            (types.SimpleNamespace(message_id='obj-1'), ['obj-1']),
            ({'message_ids': ['x', 'x', 'y']}, ['x', 'y']),
        ):
            self.client.calls.clear()
            self.enter_session()
            self.context.send_result = value
            result = asyncio.run(self.transport.send_group('7788', '在的'))
            self.assertEqual(result['messageIds'], expected, value)
            self.assertEqual(result['message_ids'], expected, value)

    def test_ids_are_remembered_for_recall(self):
        self.enter_session(group_id='7788')
        self.context.send_result = {'message_id': 'm-9'}
        asyncio.run(self.transport.send_group('7788', '在的'))
        self.assertEqual(self.bridge.last_delivered_message_id(), 'm-9')
        self.client.calls.clear()
        result = self.run_action('recall_message', {'target': 'last'})
        self.assertTrue(result['ok'], result)
        self.assertEqual(self.client.params_of('delete_msg'), {'message_id': 'm-9'})
        self.assertTrue(any('最近一条已投递' in note for note in result.get('notes') or []))

    def test_recall_falls_back_to_history_scan(self):
        """宿主不回传消息号时：翻历史找机器人自己发的那条（只读、不改任何东西）。"""
        self.enter_session(group_id='7788')
        self.client.frames['get_group_msg_history'] = {
            'status': 'ok',
            'retcode': 0,
            'data': {'messages': [
                {'message_id': 'm-1', 'sender': {'user_id': '1000008890'}},
                {'message_id': 'm-2', 'sender': {'user_id': '100001357'}},
                {'message_id': 'm-3', 'sender': {'user_id': '1000008890'}},
            ]},
        }
        result = self.run_action('recall_message', {'target': 'last'})
        self.assertTrue(result['ok'], result)
        self.assertEqual(self.client.params_of('get_group_msg_history'), {'group_id': '7788', 'count': 20})
        self.assertEqual(self.client.params_of('delete_msg'), {'message_id': 'm-2'})

    def test_recall_falls_back_to_private_history(self):
        self.enter_session()
        self.client.frames['get_friend_msg_history'] = {
            'status': 'ok', 'retcode': 0,
            'data': [{'message_id': 'p-7', 'sender': {'user_id': '100001357'}}],
        }
        result = self.run_action('recall_message', {'target': 'last'})
        self.assertTrue(result['ok'], result)
        self.assertEqual(self.client.params_of('get_friend_msg_history'), {'user_id': '1000008890', 'count': 20})
        self.assertEqual(self.client.params_of('delete_msg'), {'message_id': 'p-7'})

    def test_recall_without_any_target_is_a_clear_failure(self):
        self.enter_session(group_id='7788')
        self.client.frames['get_group_msg_history'] = {'status': 'ok', 'retcode': 0, 'data': {'messages': []}}
        result = self.run_action('recall_message', {'target': 'last'})
        self.assertFalse(result['ok'])
        self.assertIn('撤回失败', result['error'])
        self.assertEqual(self.client.actions, ['get_group_msg_history'])

    def test_recall_by_entry_id_is_rejected_with_reason(self):
        self.enter_session(group_id='7788')
        result = self.run_action('recall_message', {'target': 'entry', 'entry_id': 42})
        self.assertFalse(result['ok'])
        self.assertIn('entry_id=42', result['error'])
        self.assertEqual(self.client.calls, [])


# =========================================================================== #
# 5. 回执裁剪与限幅
# =========================================================================== #

class ReceiptProjectionTests(PlatformTransportTestCase):

    def test_history_is_truncated_and_projected(self):
        self.enter_session(group_id='7788')
        rows = [
            {
                'message_id': 'm-%d' % index,
                'time': 1700000000 + index,
                'sender': {'user_id': '2', 'nickname': '小明', 'card': '小明同学'},
                'message': [{'type': 'text', 'data': {'text': '第 %d 条' % index}}, {'type': 'image', 'data': {}}],
                'raw_message': 'ignored',
                'extra_noise': {'big': 'object' * 100},
            }
            for index in range(80)
        ]
        self.client.frames['get_group_msg_history'] = {'status': 'ok', 'retcode': 0, 'data': {'messages': rows}}
        result = self.run_action('get_group_msg_history', {'count': 50})
        messages = result['data']['messages']
        self.assertEqual(len(messages), 50)
        self.assertEqual(
            sorted(messages[0]), ['message_id', 'nickname', 'text', 'time', 'user_id'],
        )
        self.assertEqual(messages[0]['text'], '第 0 条[图片]')
        self.assertEqual(messages[0]['nickname'], '小明同学')

    def test_members_are_truncated_and_projected(self):
        self.enter_session(group_id='7788')
        rows = [
            {'user_id': str(index), 'nickname': 'n%d' % index, 'role': 'member',
             'join_time': 1, 'level': '9', 'unused': 'x' * 50}
            for index in range(150)
        ]
        self.client.frames['get_group_member_list'] = {'status': 'ok', 'retcode': 0, 'data': rows}
        result = self.run_action('get_group_members_info', {})
        members = result['data']['members']
        self.assertEqual(len(members), 100)
        self.assertEqual(sorted(members[0]), ['card', 'join_time', 'nickname', 'role', 'user_id'])

    def test_group_files_are_projected(self):
        self.enter_session(group_id='7788')
        self.client.frames['get_group_root_files'] = {
            'status': 'ok', 'retcode': 0,
            'data': {
                'files': [{'file_id': 'f1', 'file_name': 'a.txt', 'file_size': 10,
                           'uploader_name': '小明', 'dead_time': 0}],
                'folders': [{'folder_id': 'd1', 'folder_name': '资料', 'total_file_count': 2,
                             'create_name': '小明'}],
            },
        }
        result = self.run_action('list_group_files', {})
        self.assertEqual(sorted(result['data']), ['files', 'folders'])
        self.assertEqual(sorted(result['data']['files'][0]), ['file_id', 'file_name', 'file_size',
                                                             'upload_time', 'uploader_name'])
        self.assertEqual(sorted(result['data']['folders'][0]), ['folder_id', 'folder_name',
                                                               'total_file_count'])

    def test_contacts_are_limited_and_projected(self):
        self.enter_session()
        self.client.frames['get_friend_list'] = {
            'status': 'ok', 'retcode': 0,
            'data': [
                {'user_id': str(index), 'nickname': '好友%d' % index, 'remark': 'r', 'big': 'x' * 50}
                for index in range(250)
            ],
        }
        result = self.run_action('list_contacts', {'type': 'friends'})
        data = result['data']
        self.assertEqual(len(data['contacts']), bridge_module._CONTACT_LIMIT)
        self.assertEqual(data['total'], 250)
        self.assertTrue(data['truncated'])
        self.assertEqual(sorted(data['contacts'][0]), ['nickname', 'remark', 'user_id'])
        self.assertEqual(self.client.actions, ['get_friend_list'])

    def test_contacts_include_groups(self):
        self.enter_session()
        self.client.frames['get_friend_list'] = {
            'status': 'ok', 'retcode': 0,
            'data': [{'user_id': '1', 'nickname': '小明', 'remark': ''}],
        }
        self.client.frames['get_group_list'] = {
            'status': 'ok', 'retcode': 0,
            'data': [{'group_id': '7788', 'group_name': '测试群', 'member_count': 3, 'max_member_count': 200}],
        }
        result = self.run_action('list_contacts', {'type': 'all'})
        data = result['data']
        self.assertEqual(data['total'], 2)
        self.assertFalse(data['truncated'])
        self.assertEqual(data['contacts'][-1], {'group_id': '7788', 'group_name': '测试群',
                                                'member_count': 3, 'max_member_count': 200})

    def test_contact_search_filters_by_keyword(self):
        self.enter_session()
        self.client.frames['get_friend_list'] = {
            'status': 'ok', 'retcode': 0,
            'data': [{'user_id': '1', 'nickname': '小明', 'remark': '同事'},
                     {'user_id': '2', 'nickname': '小红', 'remark': ''}],
        }
        result = self.run_action('search_contacts', {'keyword': '同事'})
        self.assertTrue(result['ok'], result)
        self.assertEqual([item['user_id'] for item in result['data']['contacts']], ['1'])
        # 号码也能搜到
        result = self.run_action('search_contacts', {'keyword': '2'})
        self.assertEqual([item['user_id'] for item in result['data']['contacts']], ['2'])

    def test_contacts_report_partial_failure(self):
        self.enter_session()
        self.client.frames['get_friend_list'] = {'status': 'failed', 'retcode': 1404, 'message': '无权限'}
        self.client.frames['get_group_list'] = {'status': 'ok', 'retcode': 0, 'data': []}
        result = self.run_action('list_contacts', {})
        self.assertTrue(result['ok'], result)
        self.assertTrue(any('好友' in note for note in result.get('notes') or []), result.get('notes'))


# =========================================================================== #
# 6. 语音（走宿主 TTS）与 set_input_status
# =========================================================================== #

class VoiceAndInputStatusTests(PlatformTransportTestCase):

    def _voice_file(self) -> str:
        path = os.path.join(self._tmp.name, 'voice.wav')
        with open(path, 'wb') as handle:
            handle.write(b'RIFF0000WAVEfmt ')
        return path

    def test_send_voice_uses_host_tts_provider(self):
        self.enter_session()
        provider = FakeTTSProvider(self._voice_file())
        self.context.tts_providers = [provider]
        result = self.run_action('send_voice', {'content': '晚安'})
        self.assertTrue(result['ok'], result)
        self.assertEqual(provider.texts, ['晚安'])
        umo, chain = self.context.sent[-1]
        self.assertTrue(umo.endswith(':FriendMessage:1000008890'), umo)
        self.assertEqual(len(chain.chain), 1)
        self.assertEqual(chain.chain[0].file, provider.path)
        self.assertIn('edge-tts', result['data']['voice'])

    def test_send_voice_without_provider_degrades(self):
        self.enter_session()
        result = self.run_action('send_voice', {'content': '晚安'})
        self.assertFalse(result['ok'])
        self.assertIn('TTS', result['error'])
        self.assertEqual(self.context.sent, [])
        self.assertWarned('TTS')

    def test_send_voice_synthesis_failure_degrades(self):
        self.enter_session()
        self.context.tts_providers = [FakeTTSProvider(error=RuntimeError('edge 挂了'))]
        result = self.run_action('send_voice', {'content': '晚安'})
        self.assertFalse(result['ok'])
        self.assertIn('edge 挂了', result['error'])

    def test_send_voice_requires_text(self):
        self.enter_session()
        result = self.run_action('send_voice', {})
        self.assertFalse(result['ok'])
        self.assertIn('content', result['error'])

    def test_list_voices(self):
        self.enter_session()
        result = self.run_action('list_voices', {})
        self.assertFalse(result['ok'])
        self.assertIn('TTS', result['error'])
        self.context.tts_providers = [FakeTTSProvider(self._voice_file())]
        result = self.run_action('list_voices', {})
        self.assertTrue(result['ok'], result)
        voices = result['data']['voices']
        self.assertEqual(voices[0]['id'], 'edge-tts')
        self.assertEqual(voices[0]['voice'], 'zh-CN-XiaoxiaoNeural')

    def test_set_input_status_on_and_off(self):
        target = {'platform': 'onebot', 'self_id': '100001357', 'user_id': '1000008890',
                  'group_id': '', 'channel_id': '1000008890', 'is_group': False}
        result = asyncio.run(self.transport.set_input_status(target, True))
        self.assertTrue(result['ok'], result)
        self.assertEqual(
            self.client.params_of('set_input_status'), {'user_id': '1000008890', 'event_type': 1},
        )
        self.client.calls.clear()
        result = asyncio.run(self.transport.set_input_status(target, False))
        self.assertTrue(result['ok'], result)
        self.assertEqual(
            self.client.params_of('set_input_status'), {'user_id': '1000008890', 'event_type': 2},
        )

    def test_set_input_status_missing_user_id(self):
        result = asyncio.run(self.transport.set_input_status({'platform': 'onebot'}, True))
        self.assertFalse(result['ok'])
        self.assertIn('user_id', result['error'])
        self.assertEqual(self.client.calls, [])

    def test_set_input_status_failure_does_not_raise(self):
        """群聊不一定支持输入状态：失败就是 ok=False + 一条 warn。"""
        target = {'platform': 'onebot', 'self_id': '100001357', 'user_id': '2', 'is_group': True}
        self.client.default_frame = {'status': 'failed', 'retcode': 1400, 'message': '群聊不支持'}
        result = asyncio.run(self.transport.set_input_status(target, True))
        self.assertFalse(result['ok'])
        self.assertIn('群聊不支持', result['error'])
        self.assertWarned('输入状态设置失败')


# =========================================================================== #
# 7. 降级路径
# =========================================================================== #

class DegradationTests(PlatformTransportTestCase):

    def test_scheduling_actions_are_unsupported_on_this_host(self):
        """排程是插件自己的账本，平台侧没有对应动作：显式拒绝 + warn。"""
        self.enter_session()
        cases = {
            'schedule_message': {'content': '早安'},
            'list_scheduled_messages': {},
            'cancel_scheduled_message': {'id': 1},
            'schedule_command': {'command': 'hdsi_status'},
            'list_scheduled_commands': {},
            'cancel_scheduled_command': {'id': 1},
        }
        for action, params in cases.items():
            result = self.run_action(action, params)
            self.assertFalse(result['ok'], action)
            self.assertEqual(result['error'], 'unsupported-platform-action: %s' % action)
        self.assertEqual(self.client.calls, [])
        self.assertWarned('没有对应能力')

    def test_non_onebot_platform_is_unsupported(self):
        self.enter_session(platform_name='telegram', platform_id='telegram-id')
        result = self.run_action('send_like', {'user_id': '2'})
        self.assertFalse(result['ok'])
        self.assertEqual(result['error'], 'unsupported-platform-action: send_like')
        self.assertWarned('不支持 QQ/OneBot 动作')

    def test_missing_platform_instance_degrades(self):
        self.enter_session()
        self.context.instance.bot = None
        result = self.run_action('send_like', {'user_id': '2'})
        self.assertFalse(result['ok'])
        self.assertIn('OneBot', result['error'])
        self.assertWarned('没有可用的 OneBot 客户端')

    def test_get_fun_status_list_goes_through_and_reports_platform_message(self):
        """NapCat / SnowLuma 的公开动作表里都没有这条：打过去、失败就把平台原话带回来。"""
        self.enter_session()
        self.client.frames['get_fun_status_list'] = {
            'status': 'failed', 'retcode': 1404, 'message': '不支持的 API',
        }
        result = self.run_action('get_fun_status_list', {})
        self.assertFalse(result['ok'])
        self.assertIn('不支持的 API', result['error'])

    def test_null_transport_returns_transport_unavailable(self):
        transport = NullTransport()
        for coro in (
            transport.platform_action('send_like', {}),
            transport.call_onebot('send_like', {}),
            transport.set_input_status({'user_id': '2'}, True),
        ):
            result = asyncio.run(coro)
            self.assertFalse(result['ok'])
            self.assertEqual(result['error'], 'transport-unavailable')
            self.assertIn('data', result)


# =========================================================================== #
# 8. 会话坐标：bridge.current_target()
# =========================================================================== #

class CurrentTargetTests(PlatformTransportTestCase):

    def test_private_session_coordinates(self):
        self.enter_session()
        target = self.bridge.current_target()
        self.assertEqual(target['platform'], 'onebot')
        self.assertEqual(target['platform_id'], 'NapCat')
        self.assertEqual(target['self_id'], '100001357')
        self.assertEqual(target['user_id'], '1000008890')
        self.assertEqual(target['group_id'], '')
        self.assertFalse(target['is_group'])
        self.assertEqual(target['umo'], 'NapCat:FriendMessage:1000008890')

    def test_group_session_coordinates(self):
        self.enter_session(group_id='7788')
        target = self.bridge.current_target()
        self.assertTrue(target['is_group'])
        self.assertEqual(target['group_id'], '7788')
        self.assertEqual(target['channel_id'], '7788')

    def test_target_is_empty_without_any_session(self):
        self.assertEqual(self.bridge.current_target(), {})

    def test_unknown_platform_is_not_blocked(self):
        """没有会话坐标 ≠ 平台不支持：拦不拦交给客户端那一层报错。"""
        self.assertTrue(self.bridge.platform_is_onebot({}))
        self.assertTrue(self.bridge.platform_is_onebot({'platform': 'onebot'}))
        self.assertFalse(self.bridge.platform_is_onebot({'platform': 'telegram'}))


if __name__ == '__main__':  # pragma: no cover
    unittest.main(verbosity=2)
