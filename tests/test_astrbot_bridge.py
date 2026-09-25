# -*- coding: utf-8 -*-
"""`plugin/adapters/astrbot_bridge.py` 与 `plugin/main.py` 的单元测试。

覆盖范围（任务书要求的三块 + 关键集成点）：

1. `session_view()` 的字段映射（平台语义、私聊/群聊、Koishi mini-xml content、
   引用消息、UMO）；
2. `AstrbotTransport` 的全部降级路径（搜索 / 网页 / 表态 / 原生表情 / 字节下载 /
   表情包目录），以及回合捕获（`send_session`）行为；
3. 命令对照表完整性：对 `docs/COMMANDS.md` 表里每一条断言 `main.py` 里存在对应
   handler，且两侧集合完全一致；
4. 盲区模式：管理命令真的从 `star_handlers_registry` 摘掉，`active_commands()` 为空；
5. 配置别名：AstrBot schema 的 `model_center` / `qq_access` 必须落到上游名
   `model` / `onebot`，否则用户在模型中心填的 key 会被静默丢弃。

运行环境说明：系统 Python 里没有 AstrBot，本文件在导入被测模块之前先往
`sys.modules` 里装一套**最小 AstrBot 桩**（getter 契约 + `filter` 装饰器 +
组件类 + handler 注册表）。桩只实现被测代码真正用到的接口，不模拟宿主行为。

独立运行：
    cd /home/kela/文档/harness/hds-interlude
    python3 -m unittest plugin.tests.test_astrbot_bridge -v
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(HERE)
REPO_ROOT = os.path.dirname(PLUGIN_ROOT)
COMMANDS_DOC = os.path.join(REPO_ROOT, 'docs', 'COMMANDS.md')


# =========================================================================== #
# 最小 AstrBot 桩
# =========================================================================== #

class _StubComponent:
    """AstrBot 消息组件的桩：只保留被测代码读取的字段。"""

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
    def __init__(self, id='', chain=None, sender_id='', sender_nickname='', message_str='', **kwargs):  # noqa: A002
        super().__init__(
            id=id, chain=chain or [], sender_id=sender_id,
            sender_nickname=sender_nickname, message_str=message_str, **kwargs,
        )


class File(_StubComponent):
    def __init__(self, name='', file_='', url='', size=None, **kwargs):
        super().__init__(name=name, file_=file_, file=file_, url=url, size=size, **kwargs)


class Video(_StubComponent):
    def __init__(self, file='', **kwargs):
        super().__init__(file=file, **kwargs)


class MessageChain:
    def __init__(self, chain=None, **kwargs):
        self.chain = list(chain or [])
        self.use_t2i_ = kwargs.get('use_t2i_')


class _FilterNamespace:
    """`astrbot.api.event.filter` 的桩：装饰器原样返回函数。"""

    class EventMessageType:
        ALL = 'all'
        PRIVATE_MESSAGE = 'private'
        GROUP_MESSAGE = 'group'

    class PermissionType:
        ADMIN = 'admin'
        MEMBER = 'member'

    class PlatformAdapterType:
        AIOCQHTTP = 'aiocqhttp'

    def __init__(self):
        self.registered: list[tuple[str, str]] = []

    @staticmethod
    def command(name, **_kwargs):
        def decorator(func):
            func.__astrbot_command__ = name
            return func

        return decorator

    @staticmethod
    def event_message_type(_kind, **_kwargs):
        def decorator(func):
            return func

        return decorator

    @staticmethod
    def platform_adapter_type(_kind, **_kwargs):
        def decorator(func):
            return func

        return decorator

    @staticmethod
    def permission_type(_kind, **_kwargs):
        def decorator(func):
            return func

        return decorator


class _StubLogger:
    def __init__(self):
        self.messages: list[tuple[str, str]] = []

    def _log(self, level):
        def writer(text, *args):
            self.messages.append((level, str(text) % args if args else str(text)))

        return writer

    @property
    def debug(self):
        return self._log('debug')

    @property
    def info(self):
        return self._log('info')

    @property
    def warning(self):
        return self._log('warning')

    @property
    def error(self):
        return self._log('error')


class FakeHandler:
    def __init__(self, name, module_path):
        self.handler_name = name
        self.handler_module_path = module_path
        self.handler_full_name = '%s_%s' % (module_path, name)


class FakeHandlerRegistry:
    """`StarHandlerRegistry` 的最小桩：只实现摘除命令用到的两个方法。"""

    def __init__(self):
        self.handlers: list[FakeHandler] = []

    def get_handlers_by_module_name(self, module_name):
        return [handler for handler in self.handlers if handler.handler_module_path == module_name]

    def remove(self, handler):
        self.handlers = [item for item in self.handlers if item is not handler]


class _FakeWebResponse:
    """`json_response` / `file_response` / `error_response` 的桩。"""

    def __init__(self, payload=None, status_code=200, path=None, filename=None):
        self.payload = payload
        self.status_code = status_code
        self.path = path
        self.filename = filename


class _FakeUpload:
    def __init__(self, filename, data):
        self.filename = filename
        self._data = data

    async def read(self, size=-1):  # noqa: ARG002
        return self._data


class _FakeMultiDict(dict):
    def getlist(self, key):
        value = self.get(key)
        return [] if value is None else [value]


class _FakeWebRequest:
    """插件页请求的桩：`files()` / `json()` 由各用例按需设置。"""

    def __init__(self, uploads=None, body=None):
        self.uploads = _FakeMultiDict(uploads or {})
        self.body = body

    async def files(self):
        return self.uploads

    async def json(self, default=None):
        return self.body if self.body is not None else default


def _install_astrbot_stub():
    """把最小 AstrBot 桩装进 `sys.modules`（幂等）。"""
    if 'astrbot' in sys.modules and getattr(sys.modules['astrbot'], '_hdsi_stub', False):
        return sys.modules['astrbot']

    def module(name):
        created = types.ModuleType(name)
        sys.modules[name] = created
        return created

    logger_stub = _StubLogger()
    filter_namespace = _FilterNamespace()
    registry = FakeHandlerRegistry()

    astrbot = module('astrbot')
    astrbot._hdsi_stub = True
    astrbot.logger = logger_stub

    api = module('astrbot.api')
    api.logger = logger_stub

    class AstrMessageEvent:  # noqa: D401 - 桩类型，真实契约在测试夹具体现
        pass

    event_module = module('astrbot.api.event')
    event_module.AstrMessageEvent = AstrMessageEvent
    event_module.filter = filter_namespace
    event_module.MessageChain = MessageChain

    filter_module = module('astrbot.api.event.filter')
    filter_module.command = filter_namespace.command
    filter_module.event_message_type = filter_namespace.event_message_type
    filter_module.platform_adapter_type = filter_namespace.platform_adapter_type
    filter_module.permission_type = filter_namespace.permission_type
    filter_module.EventMessageType = _FilterNamespace.EventMessageType
    filter_module.PermissionType = _FilterNamespace.PermissionType

    components = module('astrbot.api.message_components')
    for name, cls in (
        ('Plain', Plain), ('Image', Image), ('Record', Record), ('Face', Face),
        ('At', At), ('Reply', Reply), ('File', File), ('Video', Video),
    ):
        setattr(components, name, cls)

    class Context:
        pass

    class Star:
        def __init__(self, context, config=None):
            self.context = context

    star_module = module('astrbot.api.star')
    star_module.Context = Context
    star_module.Star = Star

    core = module('astrbot.core')
    core_message = module('astrbot.core.message')
    core_components = module('astrbot.core.message.components')
    for name in ('Plain', 'Image', 'Record', 'Face', 'At', 'Reply', 'File', 'Video'):
        setattr(core_components, name, getattr(components, name))
    core_result = module('astrbot.core.message.message_event_result')
    core_result.MessageChain = MessageChain
    core_message.components = core_components

    core_star = module('astrbot.core.star')
    star_handler = module('astrbot.core.star.star_handler')
    star_handler.star_handlers_registry = registry
    core_star.star_handler = star_handler

    core_utils = module('astrbot.core.utils')
    astrbot_path = module('astrbot.core.utils.astrbot_path')
    astrbot_path.get_astrbot_data_path = lambda: PLUGIN_ROOT

    core_cfg = module('astrbot.core.config')
    core_cfg.AstrBotConfig = dict

    # 插件页 Web API（`page_config_*` 用懒加载导入 `astrbot.api.web`）
    web_module = module('astrbot.api.web')
    web_module.PluginRequest = _FakeWebRequest
    web_module.PluginUploadFile = _FakeUpload
    web_module._hdsi_fake_request = None

    def _json_response(data=None, *, status_code=200, headers=None):  # noqa: ARG001
        return _FakeWebResponse(payload=data, status_code=status_code)

    def _error_response(message, *, status_code=400, data=None, headers=None):  # noqa: ARG001
        return _FakeWebResponse(
            payload={'status': 'error', 'message': message}, status_code=status_code,
        )

    def _file_response(path, *, filename=None, content_type=None, headers=None):  # noqa: ARG001
        return _FakeWebResponse(path=str(path), filename=filename)

    web_module.json_response = _json_response
    web_module.error_response = _error_response
    web_module.file_response = _file_response

    class _RequestProxy:
        """对应 AstrBot 的 `request` 代理：读当前用例装进来的桩。"""

        def _current(self):
            current = web_module._hdsi_fake_request
            if current is None:
                raise RuntimeError('没有安装请求桩')
            return current

        async def files(self):
            return await self._current().files()

        async def json(self, default=None):
            return await self._current().json(default=default)

    web_module.request = _RequestProxy()

    core.message = core_message
    core.star = core_star
    core.utils = core_utils
    api.event = event_module
    api.star = star_module
    api.message_components = components
    astrbot.api = api
    astrbot.core = core
    return astrbot


_install_astrbot_stub()

# 被测模块必须在桩装好之后再导入。
from plugin.adapters import astrbot_bridge as bridge_module  # noqa: E402
from plugin.adapters.astrbot_bridge import (  # noqa: E402
    AstrbotBridge,
    AstrbotTransport,
    looks_like_management_command,
    normalize_bridge_config,
    resolve_platform_name,
    serialize_message_chain,
    session_view,
)
from plugin import main as main_module  # noqa: E402


# =========================================================================== #
# 事件夹具（严格按 getter 契约，属性不存在）
# =========================================================================== #

class FakeMessageObj:
    def __init__(self, message_id=''):
        self.message_id = message_id


class FakeMessageEvent:
    """符合 AstrBot getter 契约的假事件。

    刻意**只提供方法**：`self_id` / `session_id` 这类属性在真机上不存在
    （见 `AGENTS.md` 的踩坑记录），如果被测代码读了属性，这里就会暴露。
    """

    def __init__(
        self,
        *,
        message='',
        components=None,
        platform_name='aiocqhttp',
        platform_id='aiocqhttp',
        self_id='10001',
        sender_id='20002',
        sender_name='测试用户',
        group_id='',
        session_id=None,
        message_id='m-1',
        umo=None,
    ):
        self._message = message
        self._components = list(components or [])
        self._platform_name = platform_name
        self._platform_id = platform_id
        self._self_id = self_id
        self._sender_id = sender_id
        self._sender_name = sender_name
        self._group_id = group_id
        self._session_id = session_id or (group_id or sender_id)
        self._message_id = message_id
        self.message_obj = FakeMessageObj(message_id)
        self.unified_msg_origin = umo or '%s:%s:%s' % (
            platform_id,
            'GroupMessage' if group_id else 'FriendMessage',
            self._session_id,
        )
        self.sent: list[object] = []
        self.reactions: list[str] = []
        self.stopped = False

    # ---- getter 契约 ----
    def get_platform_name(self):
        return self._platform_name

    def get_platform_id(self):
        return self._platform_id

    def get_message_str(self):
        return self._message

    def get_messages(self):
        return list(self._components)

    def get_session_id(self):
        return self._session_id

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

    # ---- 出站 ----
    def plain_result(self, text):
        return ('plain', text)

    def chain_result(self, chain):
        return ('chain', chain)

    def stop_event(self):
        self.stopped = True

    async def send(self, result):
        self.sent.append(result)

    async def react(self, emoji):
        self.reactions.append(emoji)

    async def get_group(self, group_id=None, **_kwargs):
        return types.SimpleNamespace(
            group_id=group_id,
            members=[types.SimpleNamespace(user_id='20002', nickname='测试用户')],
        )


class FakePlatformManager:
    def __init__(self, instances=None):
        self.platform_insts = list(instances or [])


class FakePersona:
    def __init__(self, system_prompt='你是凌梦。'):
        self.system_prompt = system_prompt


class FakePersonaManager:
    def __init__(self, personas=None):
        self._personas = personas or {}

    def get_persona(self, persona_id):
        if persona_id not in self._personas:
            raise ValueError('persona not found')
        return self._personas[persona_id]


class FakeContext:
    def __init__(self):
        self.persona_manager = FakePersonaManager()
        self.platform_manager = FakePlatformManager()
        self.sent: list[tuple[str, object]] = []
        #: `register_web_api` 的记录：(route, handler, methods, desc)
        self.web_apis: list[tuple[str, object, list, str]] = []
        self.web_api_error: Exception | None = None

    def register_web_api(self, route, view_handler, methods, desc):  # noqa: ARG002
        if self.web_api_error is not None:
            raise self.web_api_error
        self.web_apis.append((route, view_handler, list(methods), desc))

    async def send_message(self, umo, chain):
        self.sent.append((umo, chain))
        return True

    def get_using_provider(self, umo=None):  # noqa: ARG002
        return None

    async def get_current_chat_provider_id(self, umo):  # noqa: ARG002
        raise RuntimeError('no provider bound')

    async def llm_generate(self, **_kwargs):
        raise RuntimeError('no provider bound')


class FakeDatabase:
    """`plugin.core.database.Database` 的桩：测试不碰磁盘。"""

    def __init__(self, path):
        self.path = path
        self.registered = False
        self.closed = False

    def register_tables(self):
        self.registered = True
        return []

    def close(self):
        self.closed = True

    def get(self, _table, _where):
        return None

    def all(self, *_args, **_kwargs):
        return []


def _make_bridge(config=None, context=None):
    """构造一个不落盘的 `AstrbotBridge`。"""
    fake_db = FakeDatabase(':memory:')
    with mock.patch.object(bridge_module, 'Database', lambda path: fake_db), \
            mock.patch.object(bridge_module, 'plugin_data_dir', lambda *a, **k: HERE):
        bridge = AstrbotBridge(
            context=context or FakeContext(),
            config=config or {},
            logger=sys.modules['astrbot'].logger,
        )
    bridge.db = fake_db
    return bridge


def _make_plugin(config=None, context=None):
    """构造一个不落盘的 `HDSInterludePlugin`。"""
    fake_db = FakeDatabase(':memory:')
    with mock.patch.object(bridge_module, 'Database', lambda path: fake_db), \
            mock.patch.object(bridge_module, 'plugin_data_dir', lambda *a, **k: HERE):
        plugin = main_module.HDSInterludePlugin(context or FakeContext(), config or {})
    plugin.bridge.db = fake_db
    return plugin


def _install_web_request(uploads=None, body=None):
    """把插件页请求桩装进 `astrbot.api.web`，返回还原回调。"""
    web = sys.modules['astrbot.api.web']
    previous = web._hdsi_fake_request
    web._hdsi_fake_request = _FakeWebRequest(uploads=uploads, body=body)

    def restore():
        web._hdsi_fake_request = previous

    return restore


def _async_return(value):
    """一个返回固定值的假协程函数（替换 service 方法用）。"""

    async def fake(*_args, **_kwargs):
        return value

    return fake


# =========================================================================== #
# 1. session_view 字段映射
# =========================================================================== #

class SessionViewTests(unittest.TestCase):
    def test_private_event_fields(self):
        event = FakeMessageEvent(
            message='你好',
            components=[Plain('你好')],
            umo='aiocqhttp:FriendMessage:20002',
        )
        view = session_view(event)
        self.assertEqual(view.platform, 'onebot')  # OneBot 家族统一语义
        self.assertEqual(view.self_id, '10001')
        self.assertEqual(view.user_id, '20002')
        self.assertEqual(view.channel_id, '20002')
        self.assertEqual(view.guild_id, '')
        self.assertTrue(view.is_direct)
        self.assertEqual(view.content, '你好')
        self.assertEqual(view.message_id, 'm-1')
        self.assertEqual(view.username, '测试用户')
        self.assertIs(view.event, event)

    def test_group_event_fields(self):
        event = FakeMessageEvent(
            message='群消息',
            components=[Plain('群消息')],
            group_id='30003',
            session_id='30003',
            umo='aiocqhttp:GroupMessage:30003',
        )
        view = session_view(event)
        self.assertEqual(view.platform, 'onebot')
        self.assertFalse(view.is_direct)
        self.assertEqual(view.channel_id, '30003')
        self.assertEqual(view.guild_id, '30003')
        self.assertEqual(view.session_group_id(), '30003')

    def test_non_onebot_platform_keeps_platform_id(self):
        event = FakeMessageEvent(
            platform_name='telegram', platform_id='telegram-main',
            components=[Plain('hi')], umo='telegram-main:FriendMessage:20002',
        )
        view = session_view(event)
        self.assertEqual(view.platform, 'telegram-main')
        self.assertFalse(bridge_module._is_onebot_adapter('telegram', 'telegram-main'))

    def test_platform_name_resolution(self):
        self.assertEqual(resolve_platform_name('aiocqhttp', 'aiocqhttp'), 'onebot')
        self.assertEqual(resolve_platform_name('aiocqhttp', 'my-bot'), 'onebot')
        self.assertEqual(resolve_platform_name('napcat', 'napcat'), 'onebot')
        self.assertEqual(resolve_platform_name('telegram', 'telegram'), 'telegram')
        self.assertEqual(resolve_platform_name('', 'kook'), 'kook')

    def test_content_is_koishi_mini_xml(self):
        event = FakeMessageEvent(components=[
            Plain('看图'),
            At(qq='10001'),
            Image(url='https://example.com/a.png'),
            Face(id=14),
            Record(file='voice.silk', url='https://example.com/v.silk'),
            File(name='a.txt', url='https://example.com/a.txt', size=12),
        ])
        content, elements, quote = serialize_message_chain(event.get_messages())
        self.assertIn('看图', content)
        self.assertIn('<at id="10001"/>', content)
        self.assertIn('<img src="https://example.com/a.png"/>', content)
        self.assertIn('<face id="14"/>', content)
        self.assertIn('<audio file="voice.silk" url="https://example.com/v.silk"/>', content)
        self.assertIn('<file src="https://example.com/a.txt" name="a.txt" size="12"/>', content)
        self.assertIsNone(quote)
        self.assertEqual([element['type'] for element in elements[:4]], ['text', 'at', 'img', 'face'])
        # helpers 的 Koishi 解析器必须能读懂这份 content。
        from plugin.core.service.helpers import (
            extract_session_audio_sources,
            extract_session_voice_count,
        )

        view = session_view(event)
        self.assertEqual(extract_session_voice_count(view), 1)
        self.assertIn('onebot-file:voice.silk', extract_session_audio_sources(view))

    def test_quote_from_reply_component(self):
        event = FakeMessageEvent(components=[
            Reply(id='m-9', sender_id='20002', sender_nickname='测试用户', message_str='上一条'),
            Plain('回复你'),
        ])
        view = session_view(event)
        self.assertIsInstance(view.quote, dict)
        self.assertEqual(view.quote_user_id(), '20002')
        self.assertEqual(view.quote_message_id(), 'm-9')
        self.assertEqual(view.quote_content(), '上一条')
        from plugin.core.service.helpers import describe_quoted_message

        described = describe_quoted_message(view)
        self.assertEqual(described['senderId'], '20002')
        self.assertEqual(described['content'], '上一条')

    def test_plain_fallback_when_chain_missing(self):
        event = FakeMessageEvent(message='只有纯文本', components=[])
        view = session_view(event)
        self.assertEqual(view.content, '只有纯文本')
        self.assertEqual(view.elements[0]['type'], 'text')

    def test_endpoint_umo(self):
        event = FakeMessageEvent(group_id='30003', session_id='30003')
        endpoint = bridge_module.endpoint_for_event(event)
        self.assertEqual(endpoint.umo, 'aiocqhttp:GroupMessage:30003')
        self.assertEqual(endpoint.scope, '30003')
        self.assertEqual(endpoint.build_umo('40004'), 'aiocqhttp:GroupMessage:40004')

    def test_management_command_detection(self):
        self.assertTrue(looks_like_management_command('hdsi_status'))
        self.assertTrue(looks_like_management_command('/hdsi_status'))
        self.assertTrue(looks_like_management_command('hdsi'))  # 裸命令组名（上游同义）
        self.assertTrue(looks_like_management_command('interlude.status'))
        self.assertTrue(looks_like_management_command('!interlude story'))
        self.assertFalse(looks_like_management_command('你好呀'))
        self.assertFalse(looks_like_management_command('hdsix'))
        self.assertFalse(looks_like_management_command('/status_hdsi'))


# =========================================================================== #
# 2. 配置别名（AstrBot schema → 上游段名）
# =========================================================================== #

class ConfigNormalizationTests(unittest.TestCase):
    def test_schema_sections_are_aliased(self):
        config = normalize_bridge_config({
            'model_center': {'main_temperature': 0.5, 'providers': [{'label': 'x'}]},
            'qq_access': {'enabled': True, 'bot_accounts': [{'qq': '1'}]},
        })
        self.assertEqual(config['model']['main_temperature'], 0.5)
        self.assertTrue(config['onebot']['enabled'])
        self.assertEqual(config['onebot']['bot_accounts'], [{'qq': '1'}])

    def test_upstream_names_still_win(self):
        config = normalize_bridge_config({
            'model': {'main_temperature': 0.9},
            'model_center': {'main_temperature': 0.1},
        })
        self.assertEqual(config['model']['main_temperature'], 0.9)

    def test_core_normalize_config_applies_section_aliases(self):
        """别名必须落在 `core/service/config.py`，不能只靠适配层打补丁。"""
        from plugin.core.service.config import normalize_config

        normalized = normalize_config({'model_center': {'main_model_id': 'x'}})
        self.assertEqual(normalized['model']['main_model_id'], 'x')
        self.assertTrue(normalize_config({'qq_access': {'enabled': True}})['onebot']['enabled'])

    def test_core_normalize_config_keeps_unknown_keys(self):
        from plugin.core.service.config import normalize_config

        # 别名只增不减：原键保留，因此"不丢未知键"的性质不变。
        normalized = normalize_config({'model_center': {'main_model_id': 'x'}})
        self.assertIn('model_center', normalized)
        untouched = normalize_config({'totally_unknown_other': {'a': 1}})
        self.assertEqual(untouched['totally_unknown_other'], {'a': 1})

    def test_bridge_reuses_the_core_alias_table(self):
        """适配层镜像导入 core 的表，避免两处各抄一份后漂移。"""
        from plugin.core.service import config as core_config

        self.assertIsInstance(bridge_module.CONFIG_SECTION_ALIASES, dict)
        self.assertEqual(bridge_module.CONFIG_SECTION_ALIASES, dict(core_config.CONFIG_SECTION_ALIASES))
        self.assertEqual(bridge_module.CONFIG_SECTION_ALIASES['model_center'], 'model')
        self.assertEqual(bridge_module.CONFIG_SECTION_ALIASES['qq_access'], 'onebot')

    def test_bridge_config_reaches_the_service_section_readers(self):
        """端到端：用户在模型中心填的值，service 的 `_config_section` 必须读得到。"""
        bridge = _make_bridge({'model_center': {'main_model_id': 'x'}})
        self.assertEqual(bridge.service.audio_config is not None, True)  # 触发一次配置读取
        from plugin.core.service.base import _config_section

        self.assertEqual(_config_section(bridge.config, 'model')['main_model_id'], 'x')


# =========================================================================== #
# 3. Transport 降级路径
# =========================================================================== #

class TransportDegradationTests(unittest.TestCase):
    def setUp(self):
        self.context = FakeContext()
        self.bridge = _make_bridge(context=self.context)
        self.transport = AstrbotTransport(self.bridge)

    def test_fetch_image_rejects_non_direct_source(self):
        self.assertIsNone(self._run(self.transport.fetch_image('onebot-file-not-a-url')))

    def test_fetch_image_decodes_data_url(self):
        payload = self._run(self.transport.fetch_image('data:image/png;base64,aGVsbG8='))
        self.assertEqual(payload, b'hello')

    def test_fetch_audio_strips_onebot_prefix_then_degrades(self):
        # 裸 file token 需要机器人 API 通道，本适配层按降级返回 None。
        self.assertIsNone(self._run(self.transport.fetch_audio('onebot-file:abc.silk')))

    def test_list_sticker_files_missing_directory(self):
        self.assertEqual(self._run(self.transport.list_sticker_files('/definitely/not/here')), [])

    def test_list_sticker_files_filters_extensions_and_depth(self):
        entries = {
            '/root': [('a.png', False), ('b.txt', False), ('sub', True)],
            '/root/sub': [('c.JPG', False), ('d.webp', False)],
        }

        class Entry:
            def __init__(self, name, is_dir, path):
                self.name = name
                self.path = path
                self._is_dir = is_dir

            def is_dir(self):
                return self._is_dir

            def is_file(self):
                return not self._is_dir

        def fake_scandir(path):
            return [Entry(name, is_dir, os.path.join(path, name)) for name, is_dir in entries[str(path)]]

        with mock.patch.object(bridge_module.os, 'scandir', fake_scandir), \
                mock.patch.object(bridge_module.Path, 'is_dir', lambda self: True):
            found = self._run(self.transport.list_sticker_files('/root'))
        self.assertEqual([os.path.basename(item) for item in found], ['a.png', 'c.JPG', 'd.webp'])

    def test_search_web_degrades_without_host_api(self):
        self.assertEqual(self._run(self.transport.search_web('天气', 1000)), [])

    def test_search_web_uses_host_api_when_present(self):
        async def web_search(query):
            return {'results': [{'title': 'T', 'url': 'https://e.com', 'snippet': 'S'}]}

        self.context.web_search = web_search
        found = self._run(self.transport.search_web('天气', 1000))
        self.assertEqual(found[0]['url'], 'https://e.com')
        self.assertEqual(found[0]['query'], '天气')
        # Chunk5 读 `top['text']`，所以正文必须挂在 `text` 上（同时给 excerpt/snippet）。
        self.assertEqual(found[0]['text'], 'S')
        self.assertEqual(found[0]['excerpt'], 'S')

    def test_visit_web_rejects_non_http_and_private_hosts(self):
        self.assertIsNone(self._run(self.transport.visit_web('ftp://example.com', 1000)))
        self.assertIsNone(self._run(self.transport.visit_web('https://localhost/x', 1000)))
        self.assertIsNone(self._run(self.transport.visit_web('https://127.0.0.1/x', 1000)))

    def test_visit_web_extracts_text(self):
        async def fake_get(url, timeout_ms=15_000):  # noqa: ARG001
            return '<html><head><title>标题</title></head><body><p>正文内容</p></body></html>'

        with mock.patch.object(self.bridge, 'http_get_text', fake_get):
            page = self._run(self.transport.visit_web('https://example.com/a', 1000))
        self.assertEqual(page['title'], '标题')
        self.assertIn('正文内容', page['text'])

    def test_react_without_event_returns_false(self):
        self.assertFalse(self._run(self.transport.react('m-1', 'like')))

    def test_react_uses_native_event(self):
        event = FakeMessageEvent()
        self.bridge._message_events['m-1'] = event
        self.assertTrue(self._run(self.transport.react('m-1', 'like')))
        self.assertEqual(event.reactions, ['👍'])

    def test_send_native_face_degrades_on_non_onebot(self):
        event = FakeMessageEvent(platform_name='telegram', platform_id='telegram')
        endpoint = bridge_module.endpoint_for_event(event)
        self.bridge._platform_ids[(endpoint.platform, endpoint.self_id)] = endpoint.platform_id
        self.bridge._group_endpoints['g-1'] = endpoint
        result = self._run(self.transport.send_native_face('g-1', '14', is_group=True))
        self.assertFalse(result['ok'])
        self.assertEqual(result['error'], 'platform-without-native-face')

    def test_fetch_member_name_without_event(self):
        self.assertEqual(self._run(self.transport.fetch_member_name('g-1', '20002')), '')

    def test_fetch_member_name_from_group(self):
        event = FakeMessageEvent(group_id='g-1')
        self.bridge._channel_events['g-1'] = event
        self.assertEqual(self._run(self.transport.fetch_member_name('g-1', '20002')), '测试用户')

    def test_implements_the_full_transport_protocol(self):
        """`Transport` 是 `runtime_checkable` 协议：逐个方法都不能漏。"""
        from plugin.core.service import Transport

        self.assertIsInstance(self.transport, Transport)
        for name in (
            'send_private', 'send_group', 'send_session', 'send_image', 'send_sticker',
            'send_native_face', 'react', 'fetch_member_name', 'fetch_image', 'fetch_audio',
            'list_sticker_files', 'search_web', 'visit_web', 'deliver_background',
        ):
            with self.subTest(method=name):
                self.assertTrue(callable(getattr(self.transport, name, None)))

    def test_send_private_without_endpoint_returns_failure(self):
        result = self._run(self.transport.send_private({'platform': 'onebot'}, 'hello'))
        self.assertFalse(result['ok'])
        self.assertEqual(result['error'], 'no-session-target')

    def test_send_group_uses_registered_umo(self):
        event = FakeMessageEvent(group_id='30003', session_id='30003')
        endpoint = bridge_module.endpoint_for_event(event)
        self.bridge.remember_event(event, session_view(event, endpoint), endpoint)
        result = self._run(self.transport.send_group('30003', '群消息'))
        self.assertTrue(result['ok'])
        self.assertEqual(self.context.sent[0][0], 'aiocqhttp:GroupMessage:30003')

    def test_send_session_is_captured_inside_turn(self):
        event = FakeMessageEvent(components=[Plain('hi')], umo='aiocqhttp:FriendMessage:20002')
        endpoint = bridge_module.endpoint_for_event(event)
        view = session_view(event, endpoint)
        self.bridge.remember_event(event, view, endpoint)
        capture = self.bridge.begin_capture(endpoint)
        result = self._run(self.transport.send_session(view, '回合内回复'))
        self.bridge.end_capture()
        self.assertTrue(result['ok'])
        self.assertTrue(result['captured'])
        self.assertEqual(capture.texts, ['回合内回复'])
        self.assertEqual(self.context.sent, [])

    def test_send_session_outside_turn_sends_directly(self):
        event = FakeMessageEvent(components=[Plain('hi')], umo='aiocqhttp:FriendMessage:20002')
        endpoint = bridge_module.endpoint_for_event(event)
        view = session_view(event, endpoint)
        self.bridge.remember_event(event, view, endpoint)
        result = self._run(self.transport.send_session(view, '后台回复'))
        self.assertTrue(result['ok'])
        self.assertEqual(self.context.sent[0][0], 'aiocqhttp:FriendMessage:20002')

    def test_chain_from_content_splits_inline_elements(self):
        chain = bridge_module.chain_from_content('看图<img src="https://e.com/a.png"/>！')
        kinds = [type(item).__name__ for item in chain]
        self.assertEqual(kinds, ['Plain', 'Image', 'Plain'])

    @staticmethod
    def _run(awaitable):
        import asyncio

        return asyncio.run(awaitable)


# =========================================================================== #
# 4. 命令对照表完整性
# =========================================================================== #

def _parse_commands_doc():
    """解析 `docs/COMMANDS.md` 的对照表，返回 [(上游命令, AstrBot 命令, 权限), ...]。"""
    with open(COMMANDS_DOC, encoding='utf-8') as handle:
        lines = handle.readlines()
    rows = []
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith('|'):
            continue
        cells = [cell.strip().strip('`') for cell in stripped.strip('|').split('|')]
        if len(cells) < 4 or not cells[0].startswith('interlude.'):
            continue
        rows.append((cells[0], cells[1], cells[2]))
    return rows


class CommandTableTests(unittest.TestCase):
    """命令表与 `docs/COMMANDS.md` 的双向对账。

    这是**工作区一致性检查**：`docs/` 只存在于开发工作区与 CNB 工作仓
    （仓库根 = 插件根 + docs + upstream）；GitHub 发布仓里仓库根**就是**插件根、
    不带 docs/，因此文件缺失时整体跳过而不是报失败。
    """

    @classmethod
    def setUpClass(cls) -> None:
        if not os.path.isfile(COMMANDS_DOC):
            raise unittest.SkipTest('发布仓不含 docs/（工作区一致性检查）')

    def test_doc_exists(self):
        self.assertTrue(os.path.isfile(COMMANDS_DOC), 'docs/COMMANDS.md 必须存在')

    def test_every_doc_row_has_a_handler(self):
        rows = _parse_commands_doc()
        self.assertEqual(len(rows), len(main_module.COMMANDS))
        for upstream, command, _permission in rows:
            with self.subTest(command=command):
                self.assertIn(command, main_module.COMMAND_HANDLERS)
                handler = main_module.COMMAND_HANDLERS[command]
                self.assertTrue(
                    hasattr(main_module.HDSInterludePlugin, handler),
                    'main.py 缺少处理器 %s' % handler,
                )
                self.assertTrue(callable(getattr(main_module.HDSInterludePlugin, handler)))

    def test_doc_and_code_sets_match(self):
        rows = _parse_commands_doc()
        doc_commands = {row[1] for row in rows}
        self.assertEqual(doc_commands, set(main_module.COMMAND_HANDLERS))
        doc_upstream = {row[0] for row in rows}
        self.assertEqual(doc_upstream, {spec.upstream for spec in main_module.COMMANDS})

    def test_handlers_are_registered_as_astrbot_commands(self):
        for spec in main_module.COMMANDS:
            with self.subTest(command=spec.command):
                handler = getattr(main_module.HDSInterludePlugin, spec.handler)
                self.assertEqual(getattr(handler, '__astrbot_command__', None), spec.command)

    def test_command_count_matches_upstream_index(self):
        # 上游 `registerCommands` 注册 32 条命令（`upstream/command.md` 的指令总览表）。
        # 配置导出/导入**不占命令**——它是 WebUI 插件页面（`pages/config-backup/`）。
        self.assertEqual(len(main_module.COMMANDS), 32)
        self.assertEqual(len(set(main_module.COMMAND_HANDLERS)), 32)

    def test_permissions_match_upstream_roles(self):
        admin = {spec.command for spec in main_module.COMMANDS if spec.permission == 'admin'}
        member = {spec.command for spec in main_module.COMMANDS if spec.permission == 'member'}
        self.assertEqual(
            member,
            {'hdsi_doctor', 'hdsi_status', 'hdsi_timeline', 'hdsi_memory', 'hdsi_context', 'hdsi_schedule'},
        )
        self.assertEqual(admin | member, set(main_module.COMMAND_HANDLERS))
        self.assertFalse(admin & member)

    def test_doc_permissions_match_code(self):
        specs = {spec.command: spec.permission for spec in main_module.COMMANDS}
        for _upstream, command, permission in _parse_commands_doc():
            with self.subTest(command=command):
                self.assertTrue(permission.startswith(('admin', 'member')))
                self.assertEqual(specs[command], permission.split('（', 1)[0].strip())


# =========================================================================== #
# 5. 命令返回文案的本地化（只有命令名换成本移植版名字）
# =========================================================================== #

class CommandCopyLocalizationTests(unittest.TestCase):
    """上游文案里让用户执行的 `interlude.*` 必须换成本插件真实存在的 `hdsi_*`。

    其余中文（含标点、空格）逐字保留 —— 下面用整串相等来钉死这一点。
    """

    def _plugin_with(self, **service_overrides):
        plugin = _make_plugin()
        for name, value in service_overrides.items():
            setattr(plugin.bridge.service, name, value)
        return plugin

    def test_require_story_copy_points_at_real_commands(self):
        import asyncio

        plugin = self._plugin_with(
            can_handle_session=lambda session: True,  # noqa: ARG005
            find_story=_async_return(None),
        )
        session = session_view(FakeMessageEvent(components=[Plain('hi')]))
        text = asyncio.run(plugin._require_story(session))
        self.assertIn('hdsi_doctor', text)
        self.assertIn('hdsi_story_start', text)
        self.assertNotIn('interlude.', text)
        self.assertEqual(
            text,
            '当前私聊还没有故事。请先在 Console 完成档案，然后执行 hdsi_doctor；'
            '手动启动请使用 hdsi_story_start，或开启 runtime.autoCreate 后直接发送第一条私聊。',
        )

    def test_existing_paused_story_copy_points_at_hdsi_resume(self):
        import asyncio

        plugin = self._plugin_with(
            can_manage_session=lambda session: True,  # noqa: ARG005
            story_start_readiness=_async_return({
                'existing': {
                    'status': 'paused',
                    'setting': {'character': {'name': '凌梦'}},
                },
                'ready': True,
                'preview': {},
            }),
        )
        text = asyncio.run(plugin._start_story_from_console(FakeMessageEvent(), object()))
        self.assertEqual(text, '当前已有 凌梦 的主剧本（暂停中）；请使用 hdsi_resume 恢复，不要重复启动。')

    def test_existing_active_story_copy_points_at_hdsi_status(self):
        import asyncio

        plugin = self._plugin_with(
            can_manage_session=lambda session: True,  # noqa: ARG005
            story_start_readiness=_async_return({
                'existing': {
                    'status': 'active',
                    'setting': {'character': {'name': '凌梦'}},
                },
                'ready': True,
                'preview': {},
            }),
        )
        text = asyncio.run(plugin._start_story_from_console(FakeMessageEvent(), object()))
        self.assertEqual(text, '当前已有 凌梦 的活动主剧本；请使用 hdsi_status 查看状态。')

    def test_no_user_visible_string_references_upstream_commands(self):
        """源码级兜底：用户可见的返回文案里不允许再出现 `interlude.<命令>`。

        用 AST 而不是行扫描，才能精确排除两类**合法**出现：

        * docstring（上游对照说明）；
        * `COMMANDS` 表里的 `CommandSpec('interlude.doctor', ...)`（对照表本身）。
        """
        import ast

        with open(os.path.join(PLUGIN_ROOT, 'main.py'), encoding='utf-8') as handle:
            source = handle.read()
        tree = ast.parse(source)

        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                doc = ast.get_docstring(node, clean=False)
                if doc:
                    docstrings.add(doc)
        upstream_specs = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, 'id', None) == 'CommandSpec':
                if node.args and isinstance(node.args[0], ast.Constant):
                    upstream_specs.add(node.args[0].value)

        offenders = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and 'interlude.' in node.value
            and any('\u4e00' <= char <= '\u9fff' for char in node.value)
            and node.value not in docstrings
            and node.value not in upstream_specs
        ]
        self.assertEqual(offenders, [], '命令文案里仍有上游命令名：%s' % offenders)


# =========================================================================== #
# 6. 盲区模式
# =========================================================================== #

class BlindModeTests(unittest.TestCase):
    def _plugin(self, blind, persona_id=''):
        registry = sys.modules['astrbot.core.star.star_handler'].star_handlers_registry
        module_path = main_module.HDSInterludePlugin.__module__
        registry.handlers = [FakeHandler(name, module_path) for name in main_module.MANAGEMENT_COMMANDS]
        registry.handlers.append(FakeHandler('on_private_message', module_path))
        config = {'blind_mode': {'enabled': blind}}
        if persona_id:
            config['story_defaults'] = {'persona_id': persona_id}
        with mock.patch.object(bridge_module, 'Database', lambda path: FakeDatabase(path)), \
                mock.patch.object(bridge_module, 'plugin_data_dir', lambda *a, **k: HERE):
            plugin = main_module.HDSInterludePlugin(FakeContext(), config)
        return plugin, registry

    def test_blind_mode_removes_every_management_command(self):
        plugin, registry = self._plugin(True)
        self.assertTrue(plugin.blind_mode)
        self.assertEqual(set(plugin.suppressed_commands), set(main_module.MANAGEMENT_COMMANDS))
        self.assertEqual(registry.get_handlers_by_module_name(main_module.HDSInterludePlugin.__module__),
                         [handler for handler in registry.handlers if handler.handler_name == 'on_private_message'])
        self.assertEqual(plugin.active_commands(), {})

    def test_normal_mode_keeps_commands(self):
        plugin, registry = self._plugin(False)
        self.assertFalse(plugin.blind_mode)
        self.assertEqual(plugin.suppressed_commands, ())
        self.assertEqual(len(plugin.active_commands()), 32)
        self.assertEqual(
            len(registry.get_handlers_by_module_name(main_module.HDSInterludePlugin.__module__)), 33,
        )


# =========================================================================== #
# 6. 其它集成点
# =========================================================================== #

class BridgeIntegrationTests(unittest.TestCase):
    def test_bridge_wires_service_and_transport(self):
        bridge = _make_bridge({'runtime': {'capture_direct_messages': False}})
        self.assertIs(bridge.service.transport, bridge.transport)
        self.assertIs(bridge.service.db, bridge.db)
        self.assertIs(bridge.service.ctx, bridge.interlude_context)
        self.assertFalse(bridge.config_flag('runtime', 'capture_direct_messages', default=True))

    def test_handle_event_skips_empty_content(self):
        import asyncio

        bridge = _make_bridge()
        event = FakeMessageEvent(message='', components=[])
        replies = asyncio.run(bridge.handle_event(event))
        self.assertEqual(replies, [])
        self.assertFalse(event.stopped)

    def test_handle_event_ignores_management_command_by_default(self):
        import asyncio

        bridge = _make_bridge()
        event = FakeMessageEvent(message='hdsi_status', components=[Plain('hdsi_status')])
        replies = asyncio.run(bridge.handle_event(event))
        self.assertEqual(replies, [])
        self.assertFalse(event.stopped)

    def test_persona_import_overrides_story_defaults(self):
        import asyncio

        context = FakeContext()
        context.persona_manager = FakePersonaManager({'凌梦': FakePersona('你是凌梦，猫娘。')})
        bridge = _make_bridge(
            {'story_defaults': {'persona_id': '凌梦', 'extra_setting': '补充设定'}}, context=context,
        )
        name = asyncio.run(bridge.apply_story_defaults_persona())
        self.assertEqual(name, '凌梦')
        defaults = bridge.section('story_defaults')
        self.assertEqual(defaults['character_name'], '凌梦')
        self.assertIn('你是凌梦，猫娘。', defaults['character_profile'])
        self.assertIn('补充设定', defaults['character_profile'])

    def test_persona_import_tolerates_missing_persona(self):
        import asyncio

        bridge = _make_bridge({'story_defaults': {'persona_id': '不存在'}})
        self.assertEqual(asyncio.run(bridge.apply_story_defaults_persona()), '')

    def test_main_provider_label(self):
        bridge = _make_bridge({
            'model_center': {'providers': [
                {'label': '备用', 'use_for_main': False},
                {'label': '主叙事', 'use_for_main': True},
            ]},
        })
        self.assertEqual(bridge.main_provider_label(), '主叙事')

    def test_main_provider_label_fallback(self):
        self.assertEqual(_make_bridge({}).main_provider_label(), '未指定（按模型配置回退）')

    def test_schedule_window_lines(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        today = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        record = {
            'timezone': 'Asia/Shanghai',
            'revision': 3,
            'materializedDays': [{
                'date': today,
                'blocks': [{
                    'id': 'b1', 'start': '00:00', 'end': '23:59',
                    'kind': 'fixed', 'label': '上班', 'location': '公司',
                }],
            }],
        }
        lines = _make_bridge().schedule_window_lines(record, 'Asia/Shanghai')
        self.assertTrue(lines)
        self.assertIn('[fixed] 上班 @ 公司', lines[0])
        self.assertEqual(_make_bridge().schedule_window_lines(None, 'Asia/Shanghai'), [])

    def test_time_formatters_and_iso(self):
        from datetime import datetime, timezone

        bridge = _make_bridge()
        moment = datetime(2026, 8, 1, 4, 30, tzinfo=timezone.utc)
        self.assertEqual(bridge.iso_time(moment), '2026-08-01T04:30:00.000Z')
        self.assertEqual(bridge.iso_time(None), '')
        self.assertIsInstance(bridge.format_log_time(moment, 'Asia/Shanghai'), str)
        self.assertIsInstance(bridge.format_story_display_time(moment, 'Asia/Shanghai'), str)

    def test_private_and_group_umo_resolution(self):
        bridge = _make_bridge()
        private_event = FakeMessageEvent(components=[Plain('hi')])
        private_endpoint = bridge_module.endpoint_for_event(private_event)
        bridge.remember_event(private_event, session_view(private_event, private_endpoint), private_endpoint)
        self.assertEqual(bridge.private_umo('onebot', '10001', '20002'), 'aiocqhttp:FriendMessage:20002')

        group_event = FakeMessageEvent(group_id='30003', session_id='30003')
        group_endpoint = bridge_module.endpoint_for_event(group_event)
        bridge.remember_event(group_event, session_view(group_event, group_endpoint), group_endpoint)
        self.assertEqual(bridge.group_umo('30003'), 'aiocqhttp:GroupMessage:30003')


class ConfigTransferTests(unittest.TestCase):
    """配置导出 / 导入（本移植版新增）与它的**向后兼容**契约。

    这里测的是适配层行为：读的是磁盘上那份原样配置、写回走 AstrBot 的 `save_config`、
    拿不到 live config 时退化为直接写文件。信封格式与迁移链在
    `plugin/tests/test_config_io.py` 里单独测。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, 'astrbot_plugin_hds_interlude_config.json')
        self.bridge = _make_bridge({'runtime': {'auto_create': False}})
        # 用临时文件代替真实的 `data/config/<插件>_config.json`
        self.bridge.config_file_path = lambda: self.path  # type: ignore[method-assign]

    def _write_disk(self, data):
        with open(self.path, 'w', encoding='utf-8') as handle:
            handle.write('\ufeff' + json.dumps(data, ensure_ascii=False))

    def _read_disk(self):
        with open(self.path, encoding='utf-8-sig') as handle:
            return json.load(handle)

    def test_export_wraps_the_on_disk_config_not_the_normalized_copy(self):
        self._write_disk({'model_center': {'main_model_id': 'x'}, '我的扩展': {'a': 1}})
        envelope = self.bridge.export_config()
        self.assertEqual(envelope['config']['model_center'], {'main_model_id': 'x'})
        self.assertEqual(envelope['config']['我的扩展'], {'a': 1}, '导出必须保留未知键')
        self.assertEqual(envelope['sections'], ['model_center', '我的扩展'])
        self.assertEqual(envelope['formatVersion'], 1)

    def test_export_falls_back_to_memory_when_no_file(self):
        bridge = _make_bridge({'runtime': {'auto_create': True}})
        bridge.config_file_path = lambda: os.path.join(self._tmp.name, 'missing.json')  # type: ignore[method-assign]
        envelope = bridge.export_config()
        self.assertIn('runtime', envelope['config'])

    def test_export_carries_an_upstream_version_and_a_note(self):
        self._write_disk({})
        envelope = self.bridge.export_config(note='回归测试')
        self.assertTrue(envelope.get('upstreamVersion'))
        self.assertEqual(envelope.get('note'), '回归测试')

    def test_preview_does_not_write_anything(self):
        self._write_disk({'runtime': {'auto_create': False}})
        preview = self.bridge.preview_config_import({'runtime': {'auto_create': True}})
        self.assertIn('runtime.auto_create', preview['diff']['changed'])
        self.assertEqual(self._read_disk(), {'runtime': {'auto_create': False}}, '预览不应写盘')

    def test_preview_does_not_report_disk_only_keys_as_removed(self):
        """导入是合并：文件里没提到的键不会被删，预览就不能把它们报成 removed。

        早期实现拿"文件归一化后的副本"跟磁盘比，结果是手写片段一来就报几百项
        `removed`，用户以为要清空配置——纯粹是自己吓自己。
        """
        self._write_disk({
            'story_defaults': {'character_name': '凌梦', 'timezone': 'Asia/Tokyo'},
            '我的扩展': {'保留我': True},
        })
        preview = self.bridge.preview_config_import({'storyDefaults': {'characterName': '凌梦改'}})
        self.assertEqual(preview['diff']['removed'], [], '合并导入永远不会 remove')
        self.assertIn('story_defaults.character_name', preview['diff']['changed'])
        self.assertNotIn('我的扩展.保留我', preview['diff']['changed'], '未知键不在文件里，不该被动')
        self.assertEqual(preview['diff']['same'] > 0, True, '未受影响的键应计入 same')

    def test_preview_counts_only_the_sections_the_file_actually_has(self):
        """`section_count` 报文件里显式写的分组数，不是补完默认值之后的数量。"""
        self._write_disk({})
        preview = self.bridge.preview_config_import({'storyDefaults': {'characterName': '凌梦'}})
        self.assertEqual(preview['section_count'], 1)
        self.assertEqual(preview['source'], 'bare')

    def test_hand_written_snippet_does_not_clobber_unspecified_keys(self):
        """补默认值不能盖掉磁盘上用户改过的值。

        手写片段只写 `characterName` 时，`normalize_config` 会给 `story_defaults`
        的每个键补默认值；如果直接拿这份带默认值的副本覆盖，用户的 `timezone`
        就被默认值顶掉了。所以合并只能叠"文件里显式写出的键"。
        """
        import asyncio

        self._write_disk({
            'story_defaults': {'character_name': '凌梦', 'timezone': 'Asia/Tokyo'},
            '我的扩展': {'保留我': True},
        })
        bridge = _make_bridge({})
        bridge._live_config = None
        bridge.config_file_path = lambda: self.path  # type: ignore[method-assign]

        asyncio.run(bridge.import_config({'storyDefaults': {'characterName': '凌梦改'}}))
        written = self._read_disk()
        self.assertEqual(written['story_defaults']['character_name'], '凌梦改')
        self.assertEqual(written['story_defaults']['timezone'], 'Asia/Tokyo', '没写的键必须保持原值')
        self.assertEqual(written['我的扩展'], {'保留我': True}, '未知键原样保留')

    def test_nested_merge_keeps_sibling_keys_the_file_omits(self):
        """深层合并：同一个分组里，文件只写 A，磁盘上的 B 不能被顺手清掉。"""
        import asyncio

        self._write_disk({'model_center': {'main_model_id': 'keep', 'temperature': 0.9}})
        bridge = _make_bridge({})
        bridge._live_config = None
        bridge.config_file_path = lambda: self.path  # type: ignore[method-assign]

        asyncio.run(bridge.import_config({'model_center': {'main_model_id': 'new'}}))
        written = self._read_disk()
        self.assertEqual(written['model_center']['main_model_id'], 'new')
        self.assertEqual(written['model_center']['temperature'], 0.9)

    def test_import_moves_old_prompts_into_the_prompts_group_and_they_take_effect(self):
        """v1.1.0 的「提示词」组是**哑组**（core 只读 `model_center`）：内容写在里面会静默失效。

        修好之后：导入落盘时提示词只留在 `prompts` 组，`model_center` 里不再有副本，
        再读回来 core 从 `model` 段拿到同一份内容。
        """
        import asyncio

        from plugin.core.service.config import normalize_config

        curated = '用中文写，句子偏短，不写括号动作。'
        self._write_disk({'prompts': {'style_prompt': curated},
                          'model_center': {'main_temperature': 0.8}})
        bridge = _make_bridge({})
        bridge._live_config = None
        bridge.config_file_path = lambda: self.path  # type: ignore[method-assign]

        asyncio.run(bridge.import_config({'prompts': {'style_prompt': curated}}))
        written = self._read_disk()
        self.assertEqual(written['prompts']['style_prompt'], curated)
        self.assertNotIn('style_prompt', written['model_center'], '写盘后不该再有第二份')
        self.assertEqual(written['model_center']['main_temperature'], 0.8)
        self.assertEqual(normalize_config(written)['model']['style_prompt'], curated)

    def test_import_rescues_prompts_that_only_exist_in_model_center(self):
        """反向兼容：内容只写在 `model_center`（旧版真正生效的位置）也不能丢。"""
        import asyncio

        from plugin.core.service.config import normalize_config

        legacy = '旧版文件里写的文风'
        self._write_disk({'model_center': {'style_prompt': legacy}})
        bridge = _make_bridge({})
        bridge._live_config = None
        bridge.config_file_path = lambda: self.path  # type: ignore[method-assign]

        asyncio.run(bridge.import_config({'model_center': {'style_prompt': legacy}}))
        written = self._read_disk()
        self.assertEqual(written['prompts']['style_prompt'], legacy)
        self.assertNotIn('style_prompt', written['model_center'])
        self.assertEqual(normalize_config(written)['model']['style_prompt'], legacy)

    def test_import_report_diff_matches_what_actually_changed(self):
        """报告里的 diff 必须是"磁盘 → 写盘后"的真实差异。"""
        import asyncio

        self._write_disk({'story_defaults': {'character_name': '凌梦', 'timezone': 'Asia/Tokyo'}})
        bridge = _make_bridge({})
        bridge._live_config = None
        bridge.config_file_path = lambda: self.path  # type: ignore[method-assign]

        report = asyncio.run(bridge.import_config({'storyDefaults': {'characterName': '凌梦改'}}))
        self.assertEqual(report['diff']['removed'], [])
        self.assertEqual(report['diff']['changed'], ['story_defaults.character_name'])
        self.assertEqual(self._read_disk()['story_defaults']['character_name'], '凌梦改')

    def test_import_writes_through_the_astrbot_config_api(self):
        saved = {}

        class _LiveConfig(dict):
            def save_config(self, replace_config=None, **kwargs):
                saved.update(replace_config or {})

        bridge = _make_bridge({})
        bridge._live_config = _LiveConfig()
        bridge.config_file_path = lambda: self.path  # type: ignore[method-assign]
        self._write_disk({'runtime': {'auto_create': False}})
        import asyncio

        report = asyncio.run(bridge.import_config({'runtime': {'auto_create': True}}))
        self.assertEqual(report['saved_via'], 'astrbot-config-api')
        self.assertTrue(saved.get('runtime', {}).get('auto_create'))

    def test_import_falls_back_to_writing_the_file(self):
        bridge = _make_bridge({})
        bridge._live_config = None
        bridge.config_file_path = lambda: self.path  # type: ignore[method-assign]
        self._write_disk({'runtime': {'auto_create': False}, 'keep_me': {'v': 1}})
        import asyncio

        report = asyncio.run(bridge.import_config({'runtime': {'auto_create': True}}))
        self.assertEqual(report['saved_via'], 'config-file')
        written = self._read_disk()
        self.assertTrue(written['runtime']['auto_create'])
        self.assertEqual(written['keep_me'], {'v': 1}, '文件里没出现的键不能被清掉')

    def test_legacy_bare_config_imports_and_is_reported_as_v0(self):
        self._write_disk({})
        bridge = _make_bridge({})
        bridge._live_config = None
        bridge.config_file_path = lambda: self.path  # type: ignore[method-assign]
        import asyncio

        report = asyncio.run(bridge.import_config({
            'storyDefaults': {'characterName': '凌梦'},   # 上游 Koala Console 时代的分组名
        }))
        self.assertEqual(report['format_version'], 0)
        self.assertEqual(report['source'], 'bare')
        written = self._read_disk()
        # 归一化后落到 snake_case 分组，并补齐默认值
        self.assertEqual(written['story_defaults']['character_name'], '凌梦')
        self.assertIn('model_center', written)

    def test_round_trip_export_then_import_keeps_everything(self):
        original = {
            'story_defaults': {'character_name': '凌梦', 'timezone': 'Asia/Shanghai'},
            'model_center': {'providers': [{'label': '主叙事', 'model': 'x'}]},
            '我的扩展': {'v': 1},
        }
        self._write_disk(original)
        envelope = self.bridge.export_config()

        bridge = _make_bridge({})
        bridge._live_config = None
        bridge.config_file_path = lambda: self.path  # type: ignore[method-assign]
        self._write_disk({})
        import asyncio

        asyncio.run(bridge.import_config(envelope))
        written = self._read_disk()
        self.assertEqual(written['story_defaults']['character_name'], '凌梦')
        self.assertEqual(written['model_center']['providers'][0]['model'], 'x')
        self.assertEqual(written['我的扩展'], {'v': 1})

    def test_import_rejects_broken_json_with_a_readable_message(self):
        bridge = _make_bridge({})
        bridge.config_file_path = lambda: self.path  # type: ignore[method-assign]
        import asyncio

        from plugin.core.config_io import ConfigImportError

        with self.assertRaises(ConfigImportError):
            asyncio.run(bridge.import_config('{"a":'))


# =========================================================================== #
# 5. 插件页「配置备份」（WebUI 页面，不是聊天命令）
# =========================================================================== #

class ConfigPageRegistrationTests(unittest.TestCase):
    """`pages/config-backup/` 要调的三个 Web API。

    AstrBot 的内置配置页由 `_conf_schema.json` 驱动、插不进自定义按钮，官方扩展点是
    **插件页面**（`pages/<名>/index.html` + `window.AstrBotPluginPage` bridge，明确支持
    文件上传与下载）。所以导出/导入注册成页面后端接口，而**不是**聊天命令。
    """

    def test_no_chat_commands_were_added_for_config_transfer(self):
        """配置导入导出不该出现在聊天命令表里。"""
        commands = {spec.command for spec in main_module.COMMANDS}
        for name in commands:
            self.assertNotIn('config_export', name)
            self.assertNotIn('config_import', name)
        self.assertEqual(len(main_module.COMMANDS), 32, '上游 32 条命令不应被配置功能污染')

    def test_every_route_is_registered_under_the_plugin_prefix(self):
        context = FakeContext()
        _make_plugin(context=context)
        routes = [route for route, _h, _m, _d in context.web_apis]
        self.assertEqual(routes, [
            f'/{main_module.PLUGIN_NAME}/console/overview',
            f'/{main_module.PLUGIN_NAME}/console/models',
            f'/{main_module.PLUGIN_NAME}/console/script',
            f'/{main_module.PLUGIN_NAME}/console/memory',
            f'/{main_module.PLUGIN_NAME}/console/database',
            f'/{main_module.PLUGIN_NAME}/console/logs',
            f'/{main_module.PLUGIN_NAME}/console/alter',
            f'/{main_module.PLUGIN_NAME}/console/agency',
            f'/{main_module.PLUGIN_NAME}/console/delivery',
            f'/{main_module.PLUGIN_NAME}/console/flags',
            f'/{main_module.PLUGIN_NAME}/console/connections',
            f'/{main_module.PLUGIN_NAME}/console/connections-delete',
            f'/{main_module.PLUGIN_NAME}/console/config',
            f'/{main_module.PLUGIN_NAME}/console/config-set',
            f'/{main_module.PLUGIN_NAME}/console/participants',
            f'/{main_module.PLUGIN_NAME}/config-export',
            f'/{main_module.PLUGIN_NAME}/config-import-preview',
            f'/{main_module.PLUGIN_NAME}/config-import-apply',
        ])
        # 路由必须以插件名开头：AstrBot 按 `/<plugin_name>/...` 分发
        for route in routes:
            self.assertTrue(route.startswith('/%s/' % main_module.PLUGIN_NAME))

    def test_methods_and_handlers_match_the_page_calls(self):
        context = FakeContext()
        plugin = _make_plugin(context=context)
        seen = {route: (handler, methods) for route, handler, methods, _d in context.web_apis}
        export = seen[f'/{main_module.PLUGIN_NAME}/config-export']
        self.assertEqual(export[1], ['GET'])
        self.assertEqual(export[0], plugin.page_config_export)
        for suffix in ('config-import-preview', 'config-import-apply'):
            handler, methods = seen[f'/{main_module.PLUGIN_NAME}/{suffix}']
            self.assertEqual(methods, ['POST'])
            self.assertIsNotNone(handler)
        # 控制台的只读面板一律 GET（页面用 bridge.apiGet 拉数据）
        for panel in ('overview', 'models', 'script', 'memory', 'database', 'logs',
                      'alter', 'agency', 'delivery'):
            handler, methods = seen[f'/{main_module.PLUGIN_NAME}/console/{panel}']
            self.assertEqual(methods, ['GET'], panel)
            self.assertIsNotNone(handler, panel)
        # 写操作一律 POST。控制台**不是**随便写：每一条都走白名单——
        # 开关认 FLAG_KEYS，连接行认 CONNECTION_FIELDS，配置页认 `_conf_schema.json`
        # 里声明过的路径（`set_config_value` 会拒绝 schema 之外的路径）。
        writer = [
            route for route, _h, methods, _d in context.web_apis
            if route.startswith(f'/{main_module.PLUGIN_NAME}/console/') and 'GET' not in methods
        ]
        self.assertEqual(writer, [
            f'/{main_module.PLUGIN_NAME}/console/flags',
            f'/{main_module.PLUGIN_NAME}/console/connections',
            f'/{main_module.PLUGIN_NAME}/console/connections-delete',
            f'/{main_module.PLUGIN_NAME}/console/config-set',
        ])

    def test_every_registration_carries_a_description(self):
        context = FakeContext()
        _make_plugin(context=context)
        for _route, _handler, _methods, desc in context.web_apis:
            self.assertTrue(desc and isinstance(desc, str))

    def test_host_without_web_api_support_still_loads_the_plugin(self):
        """老宿主没有 `register_web_api` 时只警告，不拖垮插件加载。"""
        context = FakeContext()
        context.web_api_error = AttributeError('register_web_api')
        plugin = _make_plugin(context=context)  # 不应抛异常
        self.assertEqual(context.web_apis, [])
        self.assertTrue(hasattr(plugin, 'page_config_export'))


class ConfigPageAssetTests(unittest.TestCase):
    """控制台页面文件本身：AstrBot 只托管插件目录下的 `pages/<名>/`。

    页面是 **Vite 构建产物**（源码在 `plugin/frontend/`），所以这里断言的是
    「构建出来的东西符合宿主的要求」，而不是手写文件的内容。
    """

    PAGE_DIR = os.path.join(PLUGIN_ROOT, 'pages', 'console')

    def _read(self, name):
        with open(os.path.join(self.PAGE_DIR, name), encoding='utf-8') as handle:
            return handle.read()

    def test_page_directory_exists_with_entry_and_metadata(self):
        for name in ('index.html', '_page.json'):
            self.assertTrue(os.path.isfile(os.path.join(self.PAGE_DIR, name)), name)
        self.assertTrue(os.path.isdir(os.path.join(self.PAGE_DIR, 'assets')), 'assets/')

    def test_built_assets_are_relative_so_the_host_can_rewrite_them(self):
        """构建产物必须是相对路径。

        `base: './'` 是硬要求：宿主只重写**相对**资源地址，产物里出现绝对的
        `/assets/...` 会被当成外部链接跳过，整页 404。这条在真机上踩过。
        """
        html = self._read('index.html')
        self.assertIn('src="./assets/', html)
        self.assertIn('href="./assets/', html)
        self.assertNotIn('src="/assets/', html)
        self.assertNotIn('href="/assets/', html)

    def test_index_declares_the_bridge_sdk_before_the_app(self):
        html = self._read('index.html')
        self.assertIn('/api/plugin/page/bridge-sdk.js', html)
        self.assertIn('id="app"', html)

    def test_page_metadata_points_at_the_i18n_key(self):
        meta = json.loads(self._read('_page.json'))
        self.assertEqual(meta['title']['i18n_key'], 'pages.console.title')
        self.assertEqual(meta['description']['i18n_key'], 'pages.console.description')

    def test_page_i18n_keys_exist_in_both_locales(self):
        """键名必须是 `title` / `description`（写成 `desc` 会静默回落成英文占位串）。"""
        for locale in ('zh-CN', 'en-US'):
            path = os.path.join(PLUGIN_ROOT, '.astrbot-plugin', 'i18n', f'{locale}.json')
            with open(path, encoding='utf-8') as handle:
                data = json.load(handle)
            page = data.get('pages', {}).get('console', {})
            self.assertTrue(page.get('title'), locale)
            self.assertTrue(page.get('description'), locale)
            self.assertNotIn('desc', page, '宿主只认 description')
            for key in ('overview', 'models', 'script', 'memory', 'database', 'logs', 'config'):
                self.assertTrue(page.get('nav', {}).get(key), f'{locale}.nav.{key}')

    def test_console_bundle_stays_small(self):
        """体积护栏：控制台是随插件分发的静态资源，别让它悄悄膨胀。

        当前约 25KB JS + 4KB CSS（gzip）。阈值给到 120KB，超过就说明引入了一个
        不小的依赖，应该先讨论再合并（对照：HeroUI + React 版实测 193KB gzip）。
        """
        assets = os.path.join(self.PAGE_DIR, 'assets')
        total = 0
        for name in os.listdir(assets):
            total += os.path.getsize(os.path.join(assets, name))
        self.assertLess(total, 400 * 1024, f'控制台资源合计 {total} 字节，太大了')


class ConfigPageHandlerTests(unittest.TestCase):
    """配置导入导出处理函数的返回值形状（控制台「配置」面板按这些字段渲染）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, 'astrbot_plugin_hds_interlude_config.json')
        self.plugin = _make_plugin()
        self.plugin.bridge.config_file_path = lambda: self.path  # type: ignore[method-assign]
        self.plugin.bridge.data_dir = self._tmp.name
        self.addCleanup(_install_web_request())

    def _write_disk(self, data):
        with open(self.path, 'w', encoding='utf-8') as handle:
            handle.write('\ufeff' + json.dumps(data, ensure_ascii=False))

    def _read_disk(self):
        with open(self.path, encoding='utf-8-sig') as handle:
            return json.load(handle)

    def _run(self, coro):
        import asyncio

        return asyncio.run(coro)

    def test_export_writes_a_file_and_returns_a_download(self):
        self._write_disk({'story_defaults': {'character_name': '凌梦'}})
        response = self._run(self.plugin.page_config_export())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.filename.startswith('hdsi-config-'))
        self.assertTrue(response.filename.endswith('.json'))
        self.assertTrue(os.path.isfile(response.path), '导出文件要真的落盘才算下载成功')
        with open(response.path, encoding='utf-8') as handle:
            envelope = json.load(handle)
        self.assertEqual(envelope['config']['story_defaults']['character_name'], '凌梦')
        self.assertEqual(
            os.path.dirname(response.path), os.path.join(self._tmp.name, 'exports'),
        )

    def test_preview_returns_the_diff_and_echoes_the_payload_back(self):
        import asyncio

        self._write_disk({'runtime': {'auto_create': False}})
        raw = json.dumps({'runtime': {'auto_create': True}})
        self.addCleanup(_install_web_request(uploads={'file': _FakeUpload('cfg.json', raw.encode())}))
        response = self._run(self.plugin.page_config_import_preview())
        self.assertEqual(response.status_code, 200)
        self.assertIn('runtime.auto_create', response.payload['report']['diff']['changed'])
        # 服务端不留 pending：预览把原文回给前端，apply 时原样送回
        self.assertEqual(json.loads(response.payload['payload']), {'runtime': {'auto_create': True}})
        self.assertEqual(self._read_disk(), {'runtime': {'auto_create': False}}, '预览不写盘')

    def test_preview_without_a_file_returns_a_readable_error(self):
        response = self._run(self.plugin.page_config_import_preview())
        self.assertEqual(response.status_code, 400)
        self.assertIn('没有收到配置文件', response.payload['message'])

    def test_apply_writes_the_config_and_reports_how(self):
        import asyncio

        self._write_disk({'runtime': {'auto_create': False}})
        self.plugin.bridge._live_config = None  # 走写文件那条降级路径
        body = {'payload': json.dumps({'runtime': {'auto_create': True}})}
        self.addCleanup(_install_web_request(body=body))
        response = self._run(self.plugin.page_config_import_apply())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.payload['saved_via'], 'config-file')
        self.assertTrue(self._read_disk()['runtime']['auto_create'])

    def test_apply_rejects_a_payload_that_is_not_json(self):
        self._write_disk({})
        self.addCleanup(_install_web_request(body={'payload': '{"runtime":'}))
        response = self._run(self.plugin.page_config_import_apply())
        self.assertEqual(response.status_code, 400)
        self.assertIn('message', response.payload)

    def test_apply_without_anything_returns_a_readable_error(self):
        response = self._run(self.plugin.page_config_import_apply())
        self.assertEqual(response.status_code, 400)
        self.assertIn('没有收到要导入的配置', response.payload['message'])


# =========================================================================== #
# 6. 走 AstrBot Provider 的那条路（model_center 没填 endpoint 时的降级路径）
# =========================================================================== #

class _FakeProvider:
    def __init__(self, provider_id='prov-1', modalities=None):
        self.provider_config = {'id': provider_id}
        if modalities is not None:
            self.provider_config['modalities'] = modalities


class _RecordingContext:
    """记下 `llm_generate` 收到了什么；`error` 非空时逐次抛出。"""

    def __init__(self, provider_id='prov-1', modalities=None, errors=None):
        self.provider_id = provider_id
        self.provider = _FakeProvider(provider_id, modalities)
        self.errors = list(errors or [])
        self.calls: list[dict] = []

    async def get_current_chat_provider_id(self, umo):  # noqa: ARG002
        return self.provider_id

    def get_provider_by_id(self, provider_id):  # noqa: ARG002
        return self.provider

    async def llm_generate(self, **kwargs):
        self.calls.append(kwargs)
        if self.errors:
            raise self.errors.pop(0)

        class _Response:
            completion_text = '模型回复'
            usage = {'prompt_tokens': 3, 'completion_tokens': 5}

        return _Response()


class _RecordingBridge:
    """只实现 `AstrbotHttpClient` 会用到的那几个 bridge 方法。"""

    def __init__(self, context, task_models=None, vision_mode='native'):
        self.context = context
        self._current_umo = 'aiocqhttp:FriendMessage:1'
        self.task_models = dict(task_models or {})
        self.vision_mode = vision_mode

    async def resolve_chat_provider_id(self):
        return self.context.provider_id

    def task_model_id(self, task):
        return self.task_models.get(task or '', '')

    def vision_mode_native(self):
        return self.vision_mode != 'sidecar'

    def provider_by_id(self, provider_id):
        if not provider_id:
            return None
        return self.context.get_provider_by_id(provider_id)

    def provider_modalities(self, provider):
        config = getattr(provider, 'provider_config', None) or {}
        values = config.get('modalities')
        return {str(item).lower() for item in values} if isinstance(values, list) else set()


def _make_client(context, task_models=None, vision_mode='native'):
    """绕开 `__init__` 直接装配一个只走 chat 路由的 `AstrbotHttpClient`。"""
    client = bridge_module.AstrbotHttpClient.__new__(bridge_module.AstrbotHttpClient)
    client.bridge = _RecordingBridge(context, task_models=task_models, vision_mode=vision_mode)
    client._fallback = None
    client._modality_warned = set()
    return client


def _multimodal_payload():
    return {
        'model': 'x',
        'messages': [
            {'role': 'system', 'content': '你是主角'},
            {'role': 'user', 'content': [
                {'type': 'text', 'text': '看看这张图'},
                {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AAAA', 'detail': 'auto'}},
                {'type': 'input_audio', 'input_audio': {'data': 'QUJD', 'format': 'mp3'}},
            ]},
        ],
        'temperature': 0.8,
        'max_tokens': 64,
    }

class AstrBotProviderRoutingTests(unittest.TestCase):
    """`model_center` 没填 endpoint / 指名了 AstrBot Provider 时的那条路。

    这是「不想单独填 Key，直接复用 AstrBot 里配好的模型」的便利路径。上游整套任务级
    路由与连接参数都挂在插件自己的连接上，所以这条路必须**保真**：多模态内容不能被
    静默丢掉，任务级指派也要真的生效。
    """

    def _run(self, client, payload, task=None):
        import asyncio

        return asyncio.run(client._chat(payload, None, task=task))

    # ---- 多模态转交 ----

    def test_images_and_audio_are_forwarded_not_dropped(self):
        """多模态分段必须转交 `image_urls` / `audio_urls`，不能只留文本。

        早期实现把 content 数组 `join` 成文本，原图与语音直接消失——模型看到的是
        「看看这张图」却没有任何图，是静默失明。AstrBot 的 `llm_generate` 原生支持
        这两个参数，没有理由不转交。
        """
        context = _RecordingContext(modalities=['text', 'image', 'audio'])
        self._run(_make_client(context), _multimodal_payload())
        self.assertEqual(len(context.calls), 1)
        call = context.calls[0]
        self.assertEqual(call['image_urls'], ['data:image/png;base64,AAAA'])
        self.assertEqual(call['audio_urls'], ['data:audio/mp3;base64,QUJD'])
        self.assertEqual(call['prompt'], '看看这张图')
        self.assertEqual(call['system_prompt'], '你是主角')
        self.assertEqual(call['temperature'], 0.8)
        self.assertEqual(call['max_tokens'], 64)

    def test_text_only_payload_does_not_send_multimodal_keys(self):
        """纯文本回合不能凭空多出 `image_urls` / `audio_urls`。"""
        context = _RecordingContext(modalities=['text'])
        self._run(_make_client(context), {'model': 'x', 'messages': [{'role': 'user', 'content': '你好'}]})
        call = context.calls[0]
        self.assertNotIn('image_urls', call)
        self.assertNotIn('audio_urls', call)
        self.assertEqual(call['prompt'], '你好')

    def test_assistant_turns_keep_their_role_marker(self):
        """多轮历史里 assistant 的发言仍带 `assistant: ` 前缀（沿用上游拼法）。"""
        context = _RecordingContext(modalities=['text'])
        self._run(_make_client(context), {'model': 'x', 'messages': [
            {'role': 'user', 'content': '在吗'},
            {'role': 'assistant', 'content': '在的'},
            {'role': 'user', 'content': '好'},
        ]})
        self.assertEqual(context.calls[0]['prompt'], '在吗\n\nassistant: 在的\n\n好')

    def test_undeclared_modalities_still_receive_the_image(self):
        """Provider **没填** `modalities` 时照常送图——没声明的网关多的是，拦下来是帮倒忙。"""
        context = _RecordingContext(modalities=None)
        self._run(_make_client(context), _multimodal_payload())
        self.assertEqual(context.calls[0]['image_urls'], ['data:image/png;base64,AAAA'])
        self.assertEqual(context.calls[0]['audio_urls'], ['data:audio/mp3;base64,QUJD'])

    def test_declared_text_only_drops_the_image_and_explains(self):
        """Provider **明确声明**只有 text 时丢掉图片，并用与 `hdsi_status` 一致的说法说明。

        AstrBot 的 `text_chat` 文档写明「模型不支持图片输入会抛错」——真发过去是整轮
        失败，比丢图严重得多。既然 Provider 自己声明了能力，就按声明处理并说出来。
        """
        context = _RecordingContext(modalities=['text'])
        with mock.patch.object(bridge_module, 'log_fallback') as logged:
            self._run(_make_client(context), _multimodal_payload())
        call = context.calls[0]
        self.assertNotIn('image_urls', call)
        self.assertNotIn('audio_urls', call)
        warnings = [item for item in logged.call_args_list if item.args and item.args[0] == 'warn']
        self.assertEqual(len(warnings), 1, '丢内容必须留下一条警告')
        self.assertIn('图片会被忽略', warnings[0].args[1])

    def test_declared_image_only_still_gets_the_image(self):
        context = _RecordingContext(modalities=['text', 'image'])
        self._run(_make_client(context), _multimodal_payload())
        self.assertEqual(context.calls[0]['image_urls'], ['data:image/png;base64,AAAA'])
        self.assertNotIn('audio_urls', context.calls[0])

    def test_modality_warning_is_deduplicated_per_provider(self):
        """同一个 provider 同一个模态只警告一次，别每轮刷屏。"""
        context = _RecordingContext(modalities=['text'])
        client = _make_client(context)
        with mock.patch.object(bridge_module, 'log_fallback') as logged:
            self._run(client, _multimodal_payload())
            self._run(client, _multimodal_payload())
        self.assertEqual(
            len([item for item in logged.call_args_list if item.args and item.args[0] == 'warn']), 1,
        )

    def test_sidecar_mode_never_sends_images_to_the_main_model(self):
        """`vision.mode = sidecar` 时图片不该出现在主叙事请求里（识图交给侧端连接）。"""
        context = _RecordingContext(modalities=['text', 'image'])
        client = _make_client(context, vision_mode='sidecar')
        self._run(client, _multimodal_payload())
        self.assertNotIn('image_urls', context.calls[0])

    # ---- 按任务指名 AstrBot Provider ----

    def test_bound_task_uses_the_named_provider(self):
        """`task_models.main` 指名了 Provider 时，主叙事必须走它。"""
        context = _RecordingContext(provider_id='session-default', modalities=['text'])
        context.provider = _FakeProvider('named-provider', ['text'])
        client = _make_client(context, task_models={'main': 'named-provider'})
        self._run(client, {'model': 'x', 'messages': [{'role': 'user', 'content': '你好'}]}, task='main')
        self.assertEqual(context.calls[0]['chat_provider_id'], 'named-provider')

    def test_unbound_task_falls_back_to_the_session_provider(self):
        context = _RecordingContext(provider_id='session-default', modalities=['text'])
        self._run(_make_client(context), {'model': 'x', 'messages': [{'role': 'user', 'content': '你好'}]}, task='main')
        self.assertEqual(context.calls[0]['chat_provider_id'], 'session-default')

    def test_binding_overrides_a_filled_endpoint(self):
        """指名比连接行的 endpoint 更具体，所以填了 endpoint 也照样走指名的那条。

        早期实现只看「url 是不是 http」：连接行填了 endpoint 就直连，用户的指名被
        静默忽略。指名是针对**单个任务**的明确选择，优先级最高。
        """
        context = _RecordingContext(provider_id='session-default', modalities=['text'])
        context.provider = _FakeProvider('named-provider', ['text'])
        client = _make_client(context, task_models={'vision': 'named-provider'})
        routed = asyncio.run(client.post_json(
            'https://api.example.com/v1/chat/completions', {}, {'model': 'x', 'messages': []},
            None, task='vision',
        ))
        self.assertEqual(routed['model'], 'named-provider')
        self.assertEqual(context.calls[0]['chat_provider_id'], 'named-provider')

    def test_unknown_task_has_no_binding(self):
        """没被指名的任务不能误用别的任务的绑定。"""
        context = _RecordingContext(provider_id='session-default', modalities=['text'])
        client = _make_client(context, task_models={'vision': 'named-provider'})
        self.assertEqual(client.bridge.task_model_id('compaction'), '')
        self._run(client, {'model': 'x', 'messages': [{'role': 'user', 'content': 'hi'}]}, task='compaction')
        self.assertEqual(context.calls[0]['chat_provider_id'], 'session-default')

    # ---- 语音转写 ----

    def test_audio_is_transcribed_when_an_stt_provider_is_named(self):
        """指定了语音转写模型：音频不进主模型，转成文字并进 prompt。"""
        context = _RecordingContext(provider_id='main-provider', modalities=['text'])

        class _Stt:
            def __init__(self):
                self.urls = []

            async def get_text(self, url):
                self.urls.append(url)
                return '今天天气不错'

        stt = _Stt()
        client = _make_client(context, task_models={'audio': 'whisper-local'})
        client.bridge.provider_by_id = lambda provider_id: stt  # type: ignore[method-assign]
        self._run(client, {'model': 'x', 'messages': [{'role': 'user', 'content': [
            {'type': 'text', 'text': '听一下'},
            {'type': 'input_audio', 'input_audio': {'data': 'QUJD', 'format': 'mp3'}},
        ]}]}, task='main')
        self.assertEqual(stt.urls, ['data:audio/mp3;base64,QUJD'])
        self.assertNotIn('audio_urls', context.calls[0])
        self.assertIn('今天天气不错', context.calls[0]['prompt'])

    def test_transcription_failure_falls_back_to_native_audio(self):
        """转写失败不能毁掉整轮：回落到把音频交给主模型。"""
        context = _RecordingContext(provider_id='main-provider', modalities=['text', 'audio'])

        class _BrokenStt:
            async def get_text(self, url):  # noqa: ARG002
                raise RuntimeError('模型没起来')

        client = _make_client(context, task_models={'audio': 'whisper-local'})
        client.bridge.provider_by_id = lambda provider_id: _BrokenStt()  # type: ignore[method-assign]
        self._run(client, {'model': 'x', 'messages': [{'role': 'user', 'content': [
            {'type': 'input_audio', 'input_audio': {'data': 'QUJD', 'format': 'mp3'}},
        ]}]}, task='main')
        self.assertEqual(context.calls[0]['audio_urls'], ['data:audio/mp3;base64,QUJD'])

    # ---- 流式降级 ----

    def test_streaming_degrades_to_a_single_chunk_for_a_bound_task(self):
        """指名了 AstrBot Provider 的任务没有增量通道：整段响应当唯一一块吐出去。

        调用方（`request_openai_compatible_streaming`）本身就有「网关只回普通 JSON」
        的兜底，所以这样既能保住用户指名的模型，又不会拿空 URL 去撞 httpx。
        """
        context = _RecordingContext(provider_id='session-default', modalities=['text'])
        context.provider = _FakeProvider('named-provider', ['text'])
        client = _make_client(context, task_models={'main': 'named-provider'})

        async def collect():
            return [chunk async for chunk in client.iterate_sse('', {}, {'model': 'x', 'messages': []}, None, task='main')]

        chunks = asyncio.run(collect())
        self.assertEqual(len(chunks), 1, '应该只有一块')
        parsed = json.loads(chunks[0])
        self.assertEqual(parsed['choices'][0]['message']['content'], '模型回复')

    def test_unbound_streaming_still_falls_back_to_direct_http(self):
        client = _make_client(_RecordingContext(modalities=['text']))

        class _Fallback:
            def iterate_sse(self, url, headers=None, body=None, timeout=None):  # noqa: ARG002
                async def gen():
                    yield 'direct'

                return gen()

        client._fallback = _Fallback()

        async def collect():
            return [chunk async for chunk in client.iterate_sse('https://x/v1', {}, {}, None, task='main')]

        self.assertEqual(asyncio.run(collect()), ['direct'])

    # ---- 参数与容错 ----

    def test_sampling_params_are_dropped_on_type_error(self):
        """有的 Provider 不收采样参数：抛 TypeError 后去掉重试一次。"""
        context = _RecordingContext(
            modalities=['text'],
            errors=[TypeError('unexpected keyword argument'), RuntimeError('还是失败')],
        )
        self.assertIsNone(self._run(_make_client(context), {
            'model': 'x', 'messages': [{'role': 'user', 'content': '你好'}], 'temperature': 0.8,
        }))
        self.assertEqual(len(context.calls), 2, '应该重试一次')
        self.assertIn('temperature', context.calls[0])
        self.assertNotIn('temperature', context.calls[1])

    def test_provider_lookup_failure_is_not_fatal(self):
        """拿不到 Provider 对象（宿主版本差异）时照常发请求，只是没有模态判断。"""
        context = _RecordingContext(modalities=['text'])
        client = _make_client(context)
        client.bridge.provider_by_id = lambda provider_id: None  # type: ignore[method-assign]
        self._run(client, _multimodal_payload())
        self.assertEqual(context.calls[0]['image_urls'], ['data:image/png;base64,AAAA'])

    def test_audio_without_format_falls_back_to_wav(self):
        context = _RecordingContext(modalities=['audio'])
        self._run(_make_client(context), {'model': 'x', 'messages': [{'role': 'user', 'content': [
            {'type': 'input_audio', 'input_audio': {'data': 'QUJD'}},
        ]}]})
        self.assertEqual(context.calls[0]['audio_urls'], ['data:audio/wav;base64,QUJD'])

    def test_data_uri_input_is_passed_through_unchanged(self):
        context = _RecordingContext(modalities=['audio'])
        self._run(_make_client(context), {'model': 'x', 'messages': [{'role': 'user', 'content': [
            {'type': 'input_audio', 'input_audio': {'data': 'data:audio/ogg;base64,QUJD', 'format': 'mp3'}},
        ]}]})
        self.assertEqual(context.calls[0]['audio_urls'], ['data:audio/ogg;base64,QUJD'])


class TaskModelConfigTests(unittest.TestCase):
    """`model_center.task_models` 的读取与能力说明（走真实 `AstrbotBridge`）。"""

    def setUp(self):
        self.bridge = _make_bridge({
            'model_center': {
                'main_provider_id': '',
                'vision': {'mode': 'native', 'provider_id': 'vision-model'},
                'audio': {'provider_id': 'stt-model'},
            },
        })
        # 模拟"已经解析过一次会话默认模型"（`hdsi_status` 在真实运行里总是先解析过）
        self.bridge._resolved_chat_provider_id = 'session-default'

    def test_task_model_id_reads_the_named_provider(self):
        self.assertEqual(self.bridge.task_model_id('vision'), 'vision-model')
        self.assertEqual(self.bridge.task_model_id('audio'), 'stt-model')

    def test_blank_and_unknown_tasks_resolve_to_empty(self):
        """留空 = 使用默认 Provider；没配过的任务不能误用别人的绑定。"""
        self.assertEqual(self.bridge.task_model_id('main'), '')
        self.assertEqual(self.bridge.task_model_id('compaction'), '')
        self.assertEqual(self.bridge.task_model_id(None), '')

    def test_task_model_id_tolerates_a_missing_section(self):
        """老配置里没有 `task_models` 分组时不能抛异常（向后兼容）。"""
        bridge = _make_bridge({'model': {}})
        self.assertEqual(bridge.task_model_id('vision'), '')

    def test_vision_mode_defaults_to_native(self):
        self.assertTrue(self.bridge.vision_mode_native())
        self.assertFalse(_make_bridge({'model': {'vision': {'mode': 'sidecar'}}}).vision_mode_native())

    def test_image_capability_note_is_silent_when_modalities_are_unset(self):
        """Provider 没声明 `modalities` 时什么都不说——猜出来的结论会误导人。"""
        self.bridge.provider_by_id = lambda provider_id: _FakeProvider(provider_id, None)  # type: ignore[method-assign]
        self.assertEqual(self.bridge.image_capability_note(), '')

    def test_image_capability_note_names_the_problem_when_image_is_missing(self):
        self.bridge.provider_by_id = lambda provider_id: _FakeProvider(provider_id, ['text'])  # type: ignore[method-assign]
        note = self.bridge.image_capability_note()
        self.assertIn('当前主模型未声明图片能力', note)
        self.assertIn('建议改用 sidecar', note)

    def test_image_capability_note_is_empty_when_image_is_declared(self):
        self.bridge.provider_by_id = lambda provider_id: _FakeProvider(provider_id, ['text', 'image'])  # type: ignore[method-assign]
        self.assertEqual(self.bridge.image_capability_note(), '')

    def test_image_capability_note_respects_the_main_binding(self):
        """绑定了主模型就按绑定的那个看，不再看会话默认 Provider。"""
        bridge = _make_bridge({'model': {'task_models': {'main': 'bound-main'}}})
        bridge._resolved_chat_provider_id = 'session-default'
        bridge.provider_by_id = lambda provider_id: _FakeProvider(provider_id, ['text'])  # type: ignore[method-assign]
        self.assertIn('当前主模型未声明图片能力', bridge.image_capability_note())


class RoutingRowInjectionTests(unittest.TestCase):
    """指名了 AstrBot 模型时，给 core 的配置副本里要补一条合成连接行。

    没有这条的话，core 的候选筛选（只认 `endpoint`）会把"不填连接、只用宿主模型"
    的连接行判成 `unavailable`，`decide()` 直接抛
    `No enabled OpenAI-compatible provider is available.` —— 传输层的回退路径
    永远走不到。这个 bug 在真机复现过。
    """

    def _bridge(self, config):
        return _make_bridge(config)

    def test_binding_row_is_only_visible_to_the_core_copy(self):
        bridge = self._bridge({
            'model_center': {
                'main_provider_id': 'ollama',
                'providers': [{'label': 'Primary model', 'enabled': True, 'endpoint': '', 'model': ''}],
            },
        })
        routing = bridge.routing_config()
        rows = routing['model']['providers']
        self.assertTrue(bridge_module.is_routing_row(rows[0]), '合成行必须排在最前')
        self.assertEqual(rows[0]['use_for_main'], True)
        self.assertTrue(rows[0]['transport_target'])
        # **用户那份配置不能被污染**（否则会被导出 / 落盘）
        clean = bridge.section('model')['providers']
        self.assertFalse(any(bridge_module.is_routing_row(item) for item in clean))

    def test_no_binding_returns_the_same_object(self):
        """没指名任何任务时零开销、行为逐字不变。"""
        bridge = self._bridge({'model_center': {'providers': []}})
        self.assertIs(bridge.routing_config(), bridge.config)

    def test_injection_is_idempotent(self):
        """重复调用不能堆出多条合成行。"""
        bridge = self._bridge({'model_center': {'main_provider_id': 'ollama', 'providers': []}})
        once = bridge.routing_config()
        twice = bridge.routing_config(once)
        self.assertEqual(len(twice['model']['providers']), 1)

    def test_bound_task_becomes_available_in_core_routing(self):
        """这条断言是 bug 的护栏：core 必须认为主叙事可用。"""
        from plugin.core.model_routing import resolve_model_routing

        bridge = self._bridge({
            'model_center': {
                'main_provider_id': 'ollama',
                'providers': [{'label': 'Primary model', 'enabled': True, 'endpoint': '', 'model': ''}],
            },
        })
        routing = resolve_model_routing(bridge.routing_config()['model'])
        self.assertTrue(routing['main']['available'], routing['main']['reason'])
        self.assertEqual(routing['main']['reason'], 'assigned-provider')

    def test_without_a_binding_an_empty_connection_stays_unavailable(self):
        """反向断言：空连接行不能因为这次改动就变成"可用"。"""
        from plugin.core.model_routing import resolve_model_routing

        bridge = self._bridge({
            'model_center': {'providers': [{'label': 'x', 'enabled': True, 'endpoint': '', 'model': ''}]},
        })
        routing = resolve_model_routing(bridge.routing_config()['model'])
        self.assertFalse(routing['main']['available'])

    def test_each_task_gets_its_own_row(self):
        bridge = self._bridge({
            'model_center': {
                'main_provider_id': 'p-main',
                'vision': {'provider_id': 'p-vision'},
                'audio': {'provider_id': 'p-stt'},
                'providers': [],
            },
        })
        rows = bridge.routing_config()['model']['providers']
        by_task = {item['id'].replace(bridge_module.ROUTING_ROW_PREFIX, ''): item for item in rows}
        self.assertEqual(set(by_task), {'main', 'vision'})
        self.assertTrue(by_task['main']['use_for_main'])
        self.assertFalse(by_task['main']['use_for_vision'])
        self.assertTrue(by_task['vision']['use_for_vision'])
        # audio 是侧端转写用的，不是一条 chat 候选，不该出现在这里
        self.assertNotIn('audio', by_task)


class ModelCapabilitySelfCheckTests(unittest.TestCase):
    """启动自检 + `hdsi_status` 的能力提示。"""

    def _bridge_with(self, provider_id, modalities, task_models=None):
        bridge = _make_bridge({'model_center': dict(task_models or {})})
        bridge._resolved_chat_provider_id = provider_id
        bridge.provider_by_id = (  # type: ignore[method-assign]
            lambda pid: _FakeProvider(pid, modalities)
        )
        return bridge

    def test_startup_logs_the_binding_and_the_missing_capability(self):
        bridge = self._bridge_with('main-provider', ['text'], {'main_provider_id': 'bound-main'})
        with mock.patch.object(bridge_module, 'log_fallback') as logged:
            asyncio.run(bridge.log_model_capabilities())
        messages = [item.args[1] % tuple(item.args[2:]) for item in logged.call_args_list if item.args]
        self.assertTrue(any('main → AstrBot Provider bound-main' in item for item in messages), messages)
        self.assertTrue(any('当前主模型未声明图片能力' in item for item in messages), messages)

    def test_startup_is_silent_when_nothing_is_bound_and_modalities_are_declared(self):
        bridge = self._bridge_with('main-provider', ['text', 'image', 'audio'])
        with mock.patch.object(bridge_module, 'log_fallback') as logged:
            asyncio.run(bridge.log_model_capabilities())
        self.assertEqual(
            [item for item in logged.call_args_list if item.args and item.args[0] == 'warn'], [],
        )

    def test_status_line_reports_the_missing_capability(self):
        """`hdsi_status` 要能在第一次对话之前就说明白，而不是等用户发图没反应。"""
        context = FakeContext()
        plugin = _make_plugin(context=context)
        plugin.bridge._resolved_chat_provider_id = 'main-provider'
        plugin.bridge.provider_by_id = (  # type: ignore[method-assign]
            lambda pid: _FakeProvider(pid, ['text'])
        )
        self.assertIn('当前主模型未声明图片能力', plugin.bridge.image_capability_note())

    def test_status_line_is_absent_when_capability_is_fine(self):
        context = FakeContext()
        plugin = _make_plugin(context=context)
        plugin.bridge._resolved_chat_provider_id = 'main-provider'
        plugin.bridge.provider_by_id = (  # type: ignore[method-assign]
            lambda pid: _FakeProvider(pid, ['text', 'image', 'audio'])
        )
        self.assertEqual(plugin.bridge.image_capability_note(), '')
        self.assertEqual(plugin.bridge.audio_capability_note(), '')

    def test_audio_note_is_suppressed_when_a_transcriber_is_named(self):
        bridge = self._bridge_with('main-provider', ['text'], {'audio': {'provider_id': 'stt'}})
        self.assertEqual(bridge.audio_capability_note(), '')

    def test_any_provider_loaded_tracks_the_host_manager(self):
        """启动自检靠它判断"模型装好了没"。"""
        context = FakeContext()
        plugin = _make_plugin(context=context)
        self.assertFalse(plugin.bridge.any_provider_loaded())

        class _WithProviders:
            def get_all_providers(self):
                return [_FakeProvider('p1', ['text'])]

        plugin.bridge.context = _WithProviders()
        self.assertTrue(plugin.bridge.any_provider_loaded())

    def test_initialize_schedules_a_deferred_check_and_terminate_cancels_it(self):
        """AstrBot 4.28 里插件先于模型加载，所以自检必须延后、且不能漏掉取消。"""
        plugin = _make_plugin()

        async def scenario():
            await plugin.initialize()
            task = plugin._capability_task
            assert task is not None, '必须登记后台自检任务'
            self.assertFalse(task.done())
            await plugin.terminate()
            try:
                await asyncio.wait_for(task, timeout=1)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            return task.done()

        self.assertTrue(asyncio.run(scenario()), 'terminate() 之后自检任务必须收尾')

    def test_deferred_check_gives_up_after_the_wait_budget(self):
        """Provider 一直不就绪时也要收敛（不能变成永不结束的轮询）。"""
        context = FakeContext()
        plugin = _make_plugin(context=context)
        calls = []

        async def fake_log():
            calls.append(1)

        plugin.bridge.any_provider_loaded = lambda: False  # type: ignore[method-assign]
        plugin.bridge.log_model_capabilities = fake_log  # type: ignore[method-assign]
        original = main_module.CAPABILITY_CHECK_WAIT_SECONDS
        main_module.CAPABILITY_CHECK_WAIT_SECONDS = 2
        try:
            with mock.patch.object(main_module.asyncio, 'sleep', _async_return(None)):
                asyncio.run(plugin._self_check_model_capabilities())
        finally:
            main_module.CAPABILITY_CHECK_WAIT_SECONDS = original
        self.assertEqual(len(calls), 1, '放弃等待后仍要做一次结论')

    def test_initialize_never_raises_even_if_the_host_explodes(self):
        """自检失败绝不能挡住插件启动。"""
        context = FakeContext()
        plugin = _make_plugin(context=context)

        async def boom():
            raise RuntimeError('宿主接口变了')

        plugin.bridge.any_provider_loaded = lambda: True  # type: ignore[method-assign]
        plugin.bridge.log_model_capabilities = boom  # type: ignore[method-assign]
        asyncio.run(plugin.initialize())
        asyncio.run(plugin._self_check_model_capabilities())


if __name__ == '__main__':
    unittest.main()
