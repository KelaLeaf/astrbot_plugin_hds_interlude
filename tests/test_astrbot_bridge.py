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

import os
import sys
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


if __name__ == '__main__':
    unittest.main()
