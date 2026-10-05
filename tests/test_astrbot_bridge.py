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
    cd <仓库根目录>
    python3 -m unittest plugin.tests.test_astrbot_bridge -v
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
#: 桥测试统一用一个进程级临时数据目录：桥会把投递坐标落盘
#: （`delivery_endpoints.json`），指向源码树会在仓库里留垃圾文件。
TEST_DATA_DIR = tempfile.mkdtemp(prefix='hdsi-bridge-tests-')
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


class Forward(_StubComponent):
    """合并转发卡片（OneBot 的 `forward` 段）。"""

    def __init__(self, id='', **kwargs):  # noqa: A002 - 与 AstrBot 字段名一致
        super().__init__(id=id, **kwargs)


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
    """`json_response` / `file_response` / `error_response` 的桩。

    `content_type` 只有 blob 通道（`file_response`）会给：`sticker-file` 的两条分支
    要能被区分（JSON 信封 vs 图片字节），断言得看得到这个头。
    """

    def __init__(self, payload=None, status_code=200, path=None, filename=None,
                 content_type=None):
        self.payload = payload
        self.status_code = status_code
        self.path = path
        self.filename = filename
        self.content_type = content_type


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
    """插件页请求的桩：`files()` / `form()` / `json()` / `query` 由各用例按需设置。

    `form()` 对应真实宿主的 `PluginRequest.form()`（multipart 里的**文本字段**）：
    上传表情的 `groupId` / `description` / `name` 就走它（v1.8.3，§47）。
    """

    def __init__(self, uploads=None, body=None, query=None, form=None, form_error=None):
        self.uploads = _FakeMultiDict(uploads or {})
        self.body = body
        #: 真实宿主的 `request.query` 是个 MultiDict（`sticker-file` 的 `assetId` /
        #: `inline` 就从这里读）；桩里缺了它 = 拿不到查询参数的插件页测不了。
        self.query = _FakeMultiDict(query or {})
        self.form_fields = _FakeMultiDict(form or {})
        #: 让 `form()` 抛异常：真实宿主在没有 form()（老版本）/ 请求不是 multipart 时
        #: 会走到这条分支，上传路径必须照样能用查询串把参数带进去。
        self.form_error = form_error

    async def files(self):
        return self.uploads

    async def form(self):
        if self.form_error is not None:
            raise self.form_error
        return self.form_fields

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
        ('At', At), ('Reply', Reply), ('File', File), ('Video', Video), ('Forward', Forward),
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
    for name in ('Plain', 'Image', 'Record', 'Face', 'At', 'Reply', 'File', 'Video', 'Forward'):
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
        # 与真宿主逐字同形（`astrbot/api/web.py` 的 `error_response`）：
        # `{"status", "message", "data"}`。`data` 是取图失败的**诊断**落点（§56），
        # 桩里把它丢掉就等于那批断言测不到东西。
        return _FakeWebResponse(
            payload={'status': 'error', 'message': message, 'data': data},
            status_code=status_code,
        )

    def _file_response(path, *, filename=None, content_type=None, headers=None):  # noqa: ARG001
        return _FakeWebResponse(path=str(path), filename=filename, content_type=content_type)

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

        async def form(self):
            return await self._current().form()

        async def json(self, default=None):
            return await self._current().json(default=default)

        @property
        def query(self):
            return self._current().query

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
from plugin.core import logging as interlude_logging  # noqa: E402
from plugin.adapters import astrbot_bridge as bridge_module  # noqa: E402
#: 候选档的种类名**从生产常量导入**（别在夹具里另写一个字面量 —— 坑 39/46）。
from plugin.core.service.helpers import STICKER_CANDIDATE_KIND  # noqa: E402
from plugin.adapters.astrbot_bridge import (  # noqa: E402
    AstrbotBridge,
    AstrbotTransport,
    looks_like_management_command,
    normalize_bridge_config,
    raw_media_hints,
    resolve_platform_name,
    serialize_message_chain,
    session_view,
)
from plugin import main as main_module  # noqa: E402
#: 用量形状的判据在 core：宿主的 `TokenUsage` 映射对不对，最终由它来判（别在夹具里另写一套）。
from plugin.core.narrator import parse_token_usage  # noqa: E402


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
        raw_message=None,
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
        if raw_message is not None:
            self.message_obj.raw_message = raw_message
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

    def get_platform_inst(self, platform_id):
        """对应宿主 `Context.get_platform_inst(platform_id)`（按 `meta().id` 找实例）。

        合并转发读取（`onebot_client()`）走的就是这条宿主 API——以前测试夹具没有它，
        所以那条路径从来没被真跑过。
        """
        for instance in self.platform_manager.platform_insts:
            meta = instance.meta()
            if getattr(meta, 'id', '') == platform_id:
                return instance
        return None

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


class _FakePlatformMeta:
    def __init__(self, name: str, platform_id: str) -> None:
        self.name = name
        self.id = platform_id


class _FakePlatform:
    """够用的宿主平台实例桩：只提供 `meta()`。"""

    def __init__(self, name: str, platform_id: str) -> None:
        self._meta = _FakePlatformMeta(name, platform_id)

    def meta(self):
        return self._meta


class IncomingMediaKindTests(unittest.TestCase):
    """入站媒体种类：从 OneBot 原始段捞回 AstrBot 在解析层丢掉的信息。

    `Image` 组件只留 `file`/`url`/`path`（`sub_type` 与 `summary` 落在 pydantic
    extra 里被丢掉），`mface` 段更是被适配器直接 `continue`。没有这一步，
    "表情包"和"实拍照片"在提示词里长得一模一样。
    """

    @staticmethod
    def _event(segments, components=None):
        message_obj = types.SimpleNamespace(raw_message={'message': segments}, message_id='m1')
        event = types.SimpleNamespace(
            message_obj=message_obj,
            get_messages=lambda: list(components or []),
            get_message_str=lambda: '',
            get_platform_id=lambda: 'NapCat',
            get_platform_name=lambda: 'aiocqhttp',
            get_self_id=lambda: '100001357',
            get_sender_id=lambda: '1000008890',
            get_sender_name=lambda: '主人',
            get_group_id=lambda: '',
        )
        event.is_private_chat = lambda: True
        return event

    def _content(self, event):
        return bridge_module.session_view(event).content

    def test_collected_image_keeps_its_kind(self):
        event = self._event([{'type': 'image', 'data': {
            'file': 'a.jpg', 'url': 'https://x/a.jpg', 'sub_type': 1, 'summary': '[动画表情]',
        }}], components=[bridge_module.Image(file='a.jpg', url='https://x/a.jpg')])
        content = self._content(event)
        # 种类是"收藏表情"，`[动画表情]` 由 summary 在标签那一层判（见 helpers）。
        self.assertIn('kind="sticker"', content)
        self.assertIn('summary="[动画表情]"', content)

    def test_plain_photo_has_no_kind_attribute(self):
        event = self._event([{'type': 'image', 'data': {
            'file': 'a.jpg', 'url': 'https://x/a.jpg', 'sub_type': 0, 'summary': '[图片]',
        }}], components=[bridge_module.Image(file='a.jpg', url='https://x/a.jpg')])
        content = self._content(event)
        self.assertNotIn('kind=', content)

    def test_market_face_survives_even_though_astrbot_drops_it(self):
        """QQ 商城表情整段消失过：结构化链里没有它，只能从原始段补回来。"""
        event = self._event([
            {'type': 'text', 'data': {'text': '看这个'}},
            {'type': 'mface', 'data': {'emoji_id': 'abc', 'summary': '[动画表情]'}},
        ], components=[bridge_module.Plain('看这个')])
        content = self._content(event)
        self.assertIn('看这个', content)
        self.assertIn('<mface', content)
        self.assertIn('summary="[动画表情]"', content)

    def test_face_name_from_raw_is_carried(self):
        event = self._event([{'type': 'face', 'data': {'id': '9999', 'faceText': '新表情'}}],
                            components=[bridge_module.Face(id=9999)])
        self.assertIn('name="新表情"', self._content(event))

    def test_mini_program_card_keeps_title(self):
        payload = {'app': 'com.tencent.miniapp_01', 'prompt': '[QQ小程序]开门！收宝藏！',
                   'meta': {'detail_1': {'title': 'QQ经典农场', 'desc': '快乐不独享'}}}
        card = types.SimpleNamespace(data=payload)
        card._hdsi_kind = 'json'
        event = self._event([{'type': 'json', 'data': payload}], components=[card])
        content = self._content(event)
        self.assertIn('<card', content)
        self.assertIn('title="QQ经典农场"', content)
        self.assertIn('app="com.tencent.miniapp_01"', content)

    def test_raw_media_hints_indexes_by_file_and_url(self):
        event = self._event([{'type': 'image', 'data': {
            'file': 'a.jpg', 'url': 'https://x/a.jpg', 'sub_type': '1',
        }}])
        hints = bridge_module.raw_media_hints(event)
        self.assertEqual(hints['images']['file:a.jpg']['kind'], 'sticker')
        self.assertEqual(hints['images']['url:https://x/a.jpg']['kind'], 'sticker')
        # 结构化媒体表要用的**原始判据**也一并带出来（`session.media[].raw`）。
        self.assertEqual(hints['images']['file:a.jpg']['raw']['sub_type'], '1')

    def test_sub_type_four_is_no_longer_a_market_sticker(self):
        """v1.8.6 撤回 `4 → market`（§49.1）：4 现在是**未知**，观测种类回 `image`。

        依据：权威枚举里 4 是 `KSMART`，语义未核实；"商城表情"是 v1.4.2 起的猜测，
        与「未知平台语义一律不收」的纪律冲突。真正的商城表情走 `mface` 段（§29）。
        """
        event = self._event([{'type': 'image', 'data': {
            'file': 'a.jpg', 'url': 'https://x/a.jpg', 'sub_type': 4,
        }}], components=[bridge_module.Image(file='a.jpg', url='https://x/a.jpg')])
        hints = bridge_module.raw_media_hints(event)
        self.assertEqual(hints['images']['file:a.jpg']['kind'], 'image')
        self.assertEqual(hints['images']['file:a.jpg']['raw']['sub_type'], 4)
        view = bridge_module.session_view(event)
        self.assertEqual([item['kind'] for item in view.media], ['image'])
        self.assertNotIn('kind=', view.content, '正文标签里也不该再出现 kind="market"')

    # ---- 结构化媒体表（§46）：core 只读这一份，判据不许再从正文文本里来 ----

    def test_structured_media_carries_kind_source_and_raw_criteria(self):
        """一条真实表情包：种类 / 来源 / 原始判据都在结构化表里（core 的唯一切入点）。"""
        event = self._event([{'type': 'image', 'data': {
            'file': 'a.jpg', 'url': 'https://x/a.jpg', 'sub_type': 1, 'summary': '[动画表情]',
        }}], components=[bridge_module.Image(file='a.jpg', url='https://x/a.jpg')])
        self.assertEqual(bridge_module.session_view(event).media, [{
            'kind': 'sticker',
            'source': 'https://x/a.jpg',
            'source_kind': 'url',
            'summary': '[动画表情]',
            'raw': {'file': 'a.jpg', 'url': 'https://x/a.jpg', 'sub_type': 1, 'summary': '[动画表情]'},
        }])

    def test_structured_media_covers_photos_local_paths_and_cards(self):
        from plugin.core.service.chunk3 import _extract_session_image_sources
        photo = self._event([{'type': 'image', 'data': {
            'file': 'a.jpg', 'url': 'https://x/a.jpg', 'sub_type': 0,
        }}], components=[bridge_module.Image(file='a.jpg', url='https://x/a.jpg')])
        self.assertEqual(bridge_module.session_view(photo).media[0]['kind'], 'image')

        local = self._event([{'type': 'image', 'data': {'file': 'b.jpg'}}],
                            components=[bridge_module.Image(path='/tmp/b.jpg')])
        media = bridge_module.session_view(local).media
        # 本地落盘图与**图片来源**归一成同一个字面量（§46.9）：`file://…` → `onebot-file:…`。
        self.assertEqual(media[0]['source'], 'onebot-file:/tmp/b.jpg')
        self.assertEqual(media[0]['source_kind'], 'file')
        self.assertEqual(
            _extract_session_image_sources(bridge_module.session_view(local)),
            ['onebot-file:/tmp/b.jpg'],
            '两边必须逐字相等，否则私聊按 source 对齐种类时对不上',
        )

        payload = {'app': 'com.tencent.miniapp_01', 'meta': {'detail_1': {'title': 'QQ经典农场'}}}
        card = types.SimpleNamespace(data=payload)
        card._hdsi_kind = 'json'
        carded = self._event([{'type': 'json', 'data': payload}], components=[card])
        self.assertEqual(bridge_module.session_view(carded).media, [{
            'kind': 'card', 'source': '', 'source_kind': '', 'summary': '',
            'raw': {'app': 'com.tencent.miniapp_01', 'title': 'QQ经典农场'},
        }])

    def test_hand_typed_img_tags_in_the_body_produce_no_media(self):
        """红线（§46）：正文里手打的 `<img kind=…>` / `<card …/>` **不产生媒体条目**。

        这一段文本从前会被 core 正则解析成"种类 = sticker"，于是自动收藏会真的去
        下载那个地址（SSRF-lite）。适配层现在只看**组件与原始段**，不看正文。
        """
        text = ('<img src="https://evil.example/x.png" kind="sticker"/>'
                '<img src="https://evil.example/x.png" kind="animated"/>'
                '<card app="com.tencent.miniapp" title="宝箱"/>')
        event = self._event([{'type': 'text', 'data': {'text': text}}],
                            components=[bridge_module.Plain(text)])
        view = bridge_module.session_view(event)
        self.assertEqual(view.media, [])
        self.assertIn('kind="sticker"', view.content)  # 正文原样保留（那是给人看的文本）

    def test_structured_media_sources_align_with_image_sources(self):
        """对齐契约：`media[].source` 必须与 `extract_session_image_sources()` 逐字一致。

        payload 里的 `attachments` 与 `load_native_images()` 都是"拿图片来源去媒体表里
        对齐种类"（§29/§45）—— 两个字符串只要差一个字符，表情包就会被当成普通图片。
        这条把契约钉在适配层这一侧（`media` 与内容标签同一次走查里写出来）。
        """
        from plugin.core.service.chunk3 import _extract_session_image_sources

        url = 'https://multimedia.nt.qq.com.cn/download?appid=1406&fileid=x'
        event = self._event([{'type': 'image', 'data': {
            'file': 'a.jpg', 'url': url, 'sub_type': 1,
        }}], components=[bridge_module.Image(file='a.jpg', url=url)])
        view = bridge_module.session_view(event)
        self.assertEqual(_extract_session_image_sources(view), [url])
        self.assertEqual([item['source'] for item in view.media], [url])

        file_only = self._event([{'type': 'image', 'data': {'file': 'a.jpg'}}],
                                components=[bridge_module.Image(file='a.jpg')])
        view = bridge_module.session_view(file_only)
        self.assertEqual(_extract_session_image_sources(view), ['onebot-file:a.jpg'])
        self.assertEqual([item['source'] for item in view.media], ['onebot-file:a.jpg'])

    def test_sub_type_table_is_the_napcat_pic_sub_type_enum(self):
        """`sub_type` 口径（§49.1）：只有 1（KCUSTOM）算表情，其余非 0 一律未知。

        权威来源是 NapCat 源码里的 `enum PicSubType`（见 `_ONEBOT_IMAGE_SUB_TYPE_NAMES`
        的注释与 `docs/PORTING_NOTES.md` §49.1）；NapCat 的 apifox 文档只写
        「图片子类型 number」，**没有枚举表** —— 所以谁都不能"看着像"就补一格。
        v1.8.6 起 `4 → market` 的历史猜测也已撤回（4 归未知）。
        """
        from plugin.core.service.helpers import collectible_sticker_kind

        self.assertEqual(bridge_module._ONEBOT_IMAGE_SUB_TYPE_NAMES, {
            '0': 'KNORMAL', '1': 'KCUSTOM', '2': 'KHOT', '3': 'KDIPPERCHART',
            '4': 'KSMART', '5': 'KSPACE', '6': 'KUNKNOW', '7': 'KRELATED',
        })
        self.assertEqual(
            bridge_module._ONEBOT_IMAGE_SUB_TYPES,
            {'0': 'image', '1': 'sticker'},
            '映射只有 `0`/`1` 两格；其余非 0 值（含 4）一律"未知即不收"',
        )
        for sub_type in ('4', '5', '6', '', None):
            with self.subTest(sub_type=sub_type):
                kind, summary = bridge_module._image_media_kind({'sub_type': sub_type})
                self.assertEqual(kind, 'image')
                self.assertEqual(summary, '')
                self.assertEqual(collectible_sticker_kind(kind), '', '第一层判据必须说不')
        # —— 候选档（`2`/`3`/`7`）：平台标了候选，但没有名字就走"要字节的结构检查" ——
        self.assertEqual(
            bridge_module._ONEBOT_IMAGE_CANDIDATE_SUB_TYPES, frozenset({'2', '3', '7'}),
            '候选集合只有这三个；改它要连文档三档表一起改（§49.1）',
        )
        for sub_type in ('2', '3', '7'):
            with self.subTest(sub_type=sub_type):
                kind, _summary = bridge_module._image_media_kind({'sub_type': sub_type})
                self.assertEqual(kind, STICKER_CANDIDATE_KIND)
                self.assertEqual(
                    collectible_sticker_kind(kind), '',
                    '候选**不是**"观测到的表情"：第一层不收，收不收由结构检查/第二层定',
                )
                # `[图片]` 是普通图占位，不是"名字"：候选档必须仍然停在候选。
                self.assertEqual(
                    bridge_module._image_media_kind({'sub_type': sub_type, 'summary': '[图片]'})[0],
                    STICKER_CANDIDATE_KIND,
                )
        # `kind` 词表里的 `market` 仍然收（别的来源可能这么标注），但**没有任何
        # `sub_type` 再映射到它** —— 上面的穷举就是这条不变量的守卫。
        self.assertEqual(collectible_sticker_kind('market'), 'market')
        self.assertNotIn('market', bridge_module._ONEBOT_IMAGE_SUB_TYPES.values())

    def test_candidate_sub_types_with_a_bracketed_name_are_stickers(self):
        """候选档的**零下载**那一半：方括号名字（`[中午好]`）在入站就够定成表情（§49.1）。

        真机那条就是这么发出来的：`sub_type=7` + `summary=[中午好]` —— 用表情搜索搜出来的
        表情包。名字信号命中 → 直接 `sticker`（**不必等字节**），第一层判据也就收了；
        其余信号（GIF / alpha / 近方形）要字节，留给 `chunk2` 拿到字节后再判。
        """
        from plugin.core.service.helpers import collectible_sticker_kind

        for sub_type in ('2', '3', '7'):
            with self.subTest(sub_type=sub_type):
                kind, summary = bridge_module._image_media_kind(
                    {'sub_type': sub_type, 'summary': '[中午好]'},
                )
                self.assertEqual((kind, summary), ('sticker', '[中午好]'))
                self.assertEqual(collectible_sticker_kind(kind), 'sticker')
        # 确定档不变：`sub_type=1` 就是 `sticker`，哪怕 summary 写的是 `[动画表情]`。
        self.assertEqual(
            bridge_module._image_media_kind({'sub_type': 1, 'summary': '[动画表情]'}),
            ('sticker', '[动画表情]'),
            '用户口径："1 → sticker（今天的行为不变）"',
        )

    def test_real_device_segment_sub_type_seven_is_now_a_sticker(self):
        """★ 真机那条（§49.2）：`sub_type=7` + `summary=[中午好]` 现在是 **sticker**。

        用户补的表：7 = `KRELATED`（关联图片），包括"用表情搜索搜出来的表情包" ——
        他 11:03 那条就是这么发出来的。于是它进候选档，并被**方括号名字**当场定成表情：
        第一层判据直接收，不再依赖识图模型（上一版把它当普通图，正是它收不进来的原因）。
        """
        from plugin.core.service.helpers import collectible_sticker_kind

        url = 'https://multimedia.nt.qq.com.cn/download?appid=1406&fileid=abc&rkey=CAMSMMtwVq2'
        event = self._event([{'type': 'image', 'data': {
            'summary': '[中午好]', 'file': 'E734AC389ADCCE0D94883AE67607170B.jpg', 'sub_type': 7,
            'url': url, 'file_size': '17097',
        }}], components=[bridge_module.Image(
            file='E734AC389ADCCE0D94883AE67607170B.jpg', url=url)])
        view = bridge_module.session_view(event)

        self.assertEqual([item['kind'] for item in view.media], ['sticker'])
        self.assertEqual([item['source'] for item in view.media], [url])
        self.assertEqual(view.media[0]['raw']['sub_type'], 7)
        self.assertEqual(collectible_sticker_kind(view.media[0]['kind']), 'sticker')
        self.assertIn('kind="sticker"', view.content)
        self.assertIn('summary="[中午好]"', view.content, '平台原文照留（它同时是"名字"信号）')

    def test_animated_still_comes_from_napcats_own_placeholder(self):
        """`[动画表情]` → `animated` 的依据是 **NapCat 自己的占位**（不是我们猜的）。

        两处会升级：普通图（`image`，§29 起就有），以及**候选档命中名字**的那一种；
        确定档 `sub_type=1` 保持 `sticker`（见上一条用例）。
        """
        self.assertEqual(
            bridge_module._image_media_kind({'sub_type': '7', 'summary': '[动画表情]'}),
            ('animated', '[动画表情]'),
        )
        self.assertEqual(
            bridge_module._image_media_kind({'sub_type': '0', 'summary': '[动画表情]'}),
            ('animated', '[动画表情]'),
        )
        # 候选档但名字没命中（`[图片]` 是占位）→ 仍是候选，不许被"动画"两字提前放行。
        self.assertEqual(
            bridge_module._image_media_kind({'sub_type': '7', 'summary': '[图片]'}),
            (STICKER_CANDIDATE_KIND, '[图片]'),
        )

    def test_host_local_file_wins_over_the_url_coordinate(self):
        """宿主已经落盘时，**字节坐标**优先用本地文件（§49.3 第 1 条）。

        正文标签（给模型看的文本）保持 URL 不变；只有结构化那一侧的 `source` 换成
        `onebot-file:<路径>` —— 而且必须与 `extract_session_image_sources()` **逐字相等**，
        否则"按 source 对齐种类"又会像 §46.9 那样对不上。
        """
        from plugin.core.service.chunk3 import _extract_session_image_sources

        with tempfile.TemporaryDirectory() as tmp:
            local = os.path.join(tmp, 'inbound.png')
            with open(local, 'wb') as handle:
                handle.write(b'\x89PNG\r\n\x1a\n')
            url = 'https://multimedia.nt.qq.com.cn/download?appid=1406&rkey=abc'
            event = self._event([{'type': 'image', 'data': {
                'file': 'a.jpg', 'url': url, 'sub_type': 1,
            }}], components=[bridge_module.Image(file='a.jpg', url=url, path=local)])
            view = bridge_module.session_view(event)

            self.assertEqual([item['source'] for item in view.media], ['onebot-file:%s' % local])
            self.assertEqual(view.media[0]['source_kind'], 'file')
            self.assertEqual(_extract_session_image_sources(view), ['onebot-file:%s' % local])
            self.assertIn(
                'src="https://multimedia.nt.qq.com.cn/download?appid=1406&amp;rkey=abc"', view.content,
                '正文标签仍写 URL（人读文本不变；`&` 按 mini-xml 转义）',
            )

    def test_a_nonexistent_local_path_is_not_a_local_file(self):
        """路径字段指向一个**不存在**的文件 / 只是相对名 → 不算本地文件（不猜）。"""
        self.assertEqual(bridge_module._local_image_path('/definitely/not/here.png'), '')
        self.assertEqual(bridge_module._local_image_path('E734AC389ADCCE0D94883AE67607170B.jpg'), '')
        self.assertEqual(bridge_module._local_image_path(None), '')

    def test_pure_text_chain_hands_over_an_empty_media_table(self):
        """结构化链在（哪怕是纯文本）→ `media == []`：**观测到零媒体**，core 不回退文本。"""
        text = '<img src="https://evil.example/x.png" kind="sticker"/>'
        event = self._event([{'type': 'text', 'data': {'text': text}}],
                            components=[bridge_module.Plain(text)])
        view = bridge_module.session_view(event)
        self.assertEqual(view.media, [])

    def test_plain_text_only_host_hands_over_no_media_table(self):
        """只有 `message_str` 的宿主：`media=None` = 没有观测通道（core 的文本降级入口）。"""
        event = self._event([], components=[])
        event.get_message_str = lambda: '看这个<img src="https://x/a.png"/>'
        view = bridge_module.session_view(event)
        self.assertIsNone(view.media)
        self.assertIn('<img', view.content)

    def test_path_only_component_with_a_single_image_segment_keeps_its_kind(self):
        """§46.9：宿主把图落到本地（组件没有 url/file）时，只有一个图段 ⇒ 那就是它。

        从前组件与原始段只靠 `file`/`url` 字符串链接，路径型组件两者都没有 → 链接断 →
        `kind` 丢 → 收藏表情在私聊被当成普通照片（永远收不到）。
        """
        event = self._event(
            [{'type': 'image', 'data': {'file': 's.png', 'sub_type': 1, 'summary': '[动画表情]'}}],
            components=[bridge_module.Image(path='/tmp/s.png')],
        )
        view = bridge_module.session_view(event)
        self.assertEqual([item['kind'] for item in view.media], ['sticker'])
        self.assertEqual(view.media[0]['raw']['sub_type'], 1)
        self.assertIn('kind="sticker"', view.content)
        self.assertIn('summary="[动画表情]"', view.content)

    def test_two_image_segments_are_never_matched_by_position(self):
        """两个图段、组件又都链接不上 → **不猜**：种类退回普通图片（不给位置配对）。"""
        event = self._event(
            [
                {'type': 'image', 'data': {'file': 'a.png', 'sub_type': 1}},
                {'type': 'image', 'data': {'file': 'b.png', 'sub_type': 0}},
            ],
            components=[
                bridge_module.Image(path='/tmp/a.png'),
                bridge_module.Image(path='/tmp/b.png'),
            ],
        )
        view = bridge_module.session_view(event)
        self.assertEqual([item['kind'] for item in view.media], ['image', 'image'])
        self.assertNotIn('kind=', view.content)

    def test_structured_media_survives_the_raw_segment_fallback(self):
        """结构化链为空、退回原始段时，媒体表照样产生（且种类来自原始段的 sub_type）。"""
        event = self._event([{'type': 'image', 'data': {
            'file': 'a.jpg', 'url': 'https://x/a.jpg', 'sub_type': 1,
        }}], components=[])
        view = bridge_module.session_view(event)
        self.assertIn('kind="sticker"', view.content)
        self.assertEqual(view.media, [{
            'kind': 'sticker', 'source': 'https://x/a.jpg', 'source_kind': 'url',
            'summary': '', 'raw': {'file': 'a.jpg', 'url': 'https://x/a.jpg', 'sub_type': 1},
        }])


class DeliveryCoordinateTests(unittest.TestCase):
    """出站 UMO 的解析（v1.4.1 修：重启后没有登记表也不许猜错平台 id）。

    用户报的现象：剧本与聊天记录里显示发了消息，实际一条没到。日志里宿主的原话是
    `cannot find platform for session onebot:FriendMessage:1000008890`——我们把**归一化
    平台名**当成了 AstrBot 的平台实例 id（这台机器上正确的第一段是 `NapCat`）。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.logs: list[str] = []

        def sink(level, text):  # noqa: ARG001
            self.logs.append(text)

        self._sink = sink
        interlude_logging.set_log_sink(sink)
        self.addCleanup(interlude_logging.set_log_sink, interlude_logging._default_sink)

    def _bridge(self, instances=None):
        context = FakeContext()
        if instances is not None:
            context.platform_manager = FakePlatformManager(instances)
        fake_db = FakeDatabase(':memory:')
        with mock.patch.object(bridge_module, 'Database', lambda path: fake_db), \
                mock.patch.object(bridge_module, 'plugin_data_dir', lambda *a, **k: self._tmp.name):
            bridge = AstrbotBridge(
                context=context, config={}, logger=sys.modules['astrbot'].logger,
            )
        bridge.db = fake_db
        # `AstrbotBridge.__init__` 末尾会把 sink 换成自己的转发器：这里再装回测试用的，
        # 否则测不到桥自己打的那几条 warn。
        interlude_logging.set_log_sink(self._sink)
        return bridge

    def test_host_platform_id_is_used_when_nothing_is_registered(self):
        """这条就是用户踩到的路径：进程刚重启，谁都没登记过。"""
        bridge = self._bridge([_FakePlatform('aiocqhttp', 'default')])
        umo = bridge.private_umo('onebot', '100001357', '1000008890')
        self.assertEqual(umo, 'default:FriendMessage:1000008890')
        self.assertNotIn('onebot:', umo)

    def test_group_umo_also_resolves_through_the_host(self):
        bridge = self._bridge([_FakePlatform('aiocqhttp', 'default')])
        self.assertEqual(bridge.group_umo('100004964'), 'default:GroupMessage:100004964')

    def test_an_unresolvable_platform_says_so_instead_of_guessing(self):
        bridge = self._bridge([])
        self.assertEqual(bridge.private_umo('onebot', '100001357', '1000008890'),
                         'onebot:FriendMessage:1000008890')
        self.assertTrue(any('没能把平台 onebot 翻成' in item for item in self.logs), self.logs)

    def test_multiple_onebot_instances_are_not_guessed_at(self):
        bridge = self._bridge([
            _FakePlatform('aiocqhttp', 'default'),
            _FakePlatform('aiocqhttp', 'second'),
        ])
        self.assertEqual(bridge.private_umo('onebot', '100001357', '1000008890'), '')
        self.assertTrue(any('多个 onebot 平台实例' in str(item) for item in self.logs))

    def test_coordinates_are_persisted_and_survive_a_restart(self):
        """登记过之后就算重启、就算宿主实例换了一茬，也按登记的那份走。"""
        first = self._bridge([_FakePlatform('aiocqhttp', 'default')])
        endpoint = bridge_module.AstrbotEndpoint(
            platform='onebot', platform_id='default', platform_name='aiocqhttp',
            self_id='100001357', user_id='1000008890', group_id='', is_group=False,
            umo='default:FriendMessage:1000008890', message_id='', session_id='1000008890',
        )
        first.remember_event(object(), object(), endpoint)
        saved = json.loads((Path(self._tmp.name) / 'delivery_endpoints.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['platforms']['onebot|100001357'], 'default')
        self.assertEqual(saved['private']['onebot|100001357'], 'default:FriendMessage:1000008890')

        # 重启：新的 bridge、宿主这边一个平台实例都没有（还没连上/查询失败），仍要发得出去。
        second = self._bridge([])
        self.assertEqual(second.private_umo('onebot', '100001357', '1000008890'),
                         'default:FriendMessage:1000008890')

    def test_group_registration_is_persisted_too(self):
        first = self._bridge([])
        endpoint = bridge_module.AstrbotEndpoint(
            platform='onebot', platform_id='default', platform_name='aiocqhttp',
            self_id='100001357', user_id='', group_id='100004964', is_group=True,
            umo='default:GroupMessage:100004964', message_id='', session_id='100004964',
        )
        first.remember_event(object(), object(), endpoint)
        second = self._bridge([])
        self.assertEqual(second.group_umo('100004964'), 'default:GroupMessage:100004964')

    def test_background_delivery_reaches_the_host_with_the_right_umo(self):
        """端到端：后台主动私聊投出去时，宿主拿到的 UMO 必须是 `default:...`。

        这就是用户报的那条路：重启后没有任何登记，她主动发消息，剧本与聊天记录都显示
        发了，对面一条没收到，宿主日志写 `cannot find platform for session onebot:...`。
        """
        bridge = self._bridge([_FakePlatform('aiocqhttp', 'default')])
        result = asyncio.run(bridge.transport.send_private({
            'platform': 'onebot', 'selfId': '100001357', 'userId': '1000008890',
            'channelId': '1000008890',
        }, '在吗'))
        self.assertTrue(result.get('ok'), result)
        sent = [umo for umo, _chain in bridge.context.sent]
        self.assertEqual(sent, ['default:FriendMessage:1000008890'])

    def test_background_group_delivery_reaches_the_host_with_the_right_umo(self):
        bridge = self._bridge([_FakePlatform('aiocqhttp', 'default')])
        result = asyncio.run(bridge.transport.send_group('100004964', '在吗'))
        self.assertTrue(result.get('ok'), result)
        self.assertEqual([umo for umo, _chain in bridge.context.sent],
                         ['default:GroupMessage:100004964'])

    def test_a_broken_map_file_never_blocks_startup(self):
        (Path(self._tmp.name) / 'delivery_endpoints.json').write_text('{ not json', encoding='utf-8')
        bridge = self._bridge([_FakePlatform('aiocqhttp', 'default')])
        self.assertEqual(bridge.private_umo('onebot', '100001357', '1000008890'),
                         'default:FriendMessage:1000008890')


class MediaDeliveryConversationTypeTests(unittest.TestCase):
    """媒体 / 动作投递的 UMO 必须按**真实会话类型**拼（真机：私聊表情包落到 GroupMessage）。

    真机现场（私聊回合）：`localMedia` 选中一张本地表情包，日志里
    `AstrBot 消息投递失败 会话=NapCat:GroupMessage:1000008890`、
    `retcode 1200 rich media transfer failed` —— 用户号被拼成了群号，**表情包实际没发出去**。
    根因是 `channel_umo()` 的"先群后私"：`group_umo()` 在没登记群端点时会拿唯一平台实例
    凭空造一个群 UMO，于是私聊的用户号必然落进群分支。

    这里的夹具刻意**只有私聊端点、没有群端点**（真机就是这么回事）：谁把判据改回
    "先群后私"，`test_private_turn_media_lands_on_friend_message` 立刻红。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.logs: list[str] = []

        def sink(level, text):  # noqa: ARG001
            self.logs.append(text)

        self._sink = sink
        interlude_logging.set_log_sink(sink)
        self.addCleanup(interlude_logging.set_log_sink, interlude_logging._default_sink)

    def _bridge(self, instances=None):
        context = FakeContext()
        if instances is not None:
            context.platform_manager = FakePlatformManager(instances)
        fake_db = FakeDatabase(':memory:')
        with mock.patch.object(bridge_module, 'Database', lambda path: fake_db), \
                mock.patch.object(bridge_module, 'plugin_data_dir', lambda *a, **k: self._tmp.name):
            bridge = AstrbotBridge(
                context=context, config={}, logger=sys.modules['astrbot'].logger,
            )
        bridge.db = fake_db
        interlude_logging.set_log_sink(self._sink)
        return bridge

    @staticmethod
    def _endpoint(**overrides):
        base = dict(
            platform='onebot', platform_id='default', platform_name='aiocqhttp',
            self_id='100001357', user_id='', group_id='', is_group=False,
            umo='', message_id='', session_id='',
        )
        base.update(overrides)
        return bridge_module.AstrbotEndpoint(**base)

    def _private_bridge(self, instances=None):
        """只登记**私聊**坐标的桥（这台机器上只有 `default` 一个平台实例）。"""
        bridge = self._bridge(instances if instances is not None else [_FakePlatform('aiocqhttp', 'default')])
        bridge.remember_event(object(), object(), self._endpoint(
            user_id='1000008890', is_group=False,
            umo='default:FriendMessage:1000008890', session_id='1000008890',
        ))
        return bridge

    def _group_bridge(self, instances=None):
        bridge = self._bridge(instances if instances is not None else [_FakePlatform('aiocqhttp', 'default')])
        bridge.remember_event(object(), object(), self._endpoint(
            group_id='100004964', is_group=True,
            umo='default:GroupMessage:100004964', session_id='100004964',
        ))
        return bridge

    def test_private_turn_media_lands_on_friend_message(self):
        """私聊回合：表情包 / 图片 / 原生表情三段投递都必须是 `FriendMessage`。"""
        bridge = self._private_bridge()
        # 夹具证伪力：同一条记录走老口径（先群后私）会拼出群号——真机就是这么被拒收的。
        self.assertEqual(bridge.group_umo('1000008890'), 'default:GroupMessage:1000008890')

        sticker = asyncio.run(bridge.transport.send_sticker('1000008890', '/tmp/x.png', is_group=False))
        image = asyncio.run(bridge.transport.send_image('1000008890', '/tmp/x.png', is_group=False))
        face = asyncio.run(bridge.transport.send_native_face('1000008890', '14', is_group=False))
        self.assertTrue(sticker.get('ok'), sticker)
        self.assertTrue(image.get('ok'), image)
        self.assertTrue(face.get('ok'), face)

        umos = [umo for umo, _chain in bridge.context.sent]
        self.assertEqual(umos, ['default:FriendMessage:1000008890'] * 3)
        for umo in umos:
            self.assertNotIn('GroupMessage', umo, '私聊回合不许把用户号当群号发出去')

    def test_group_turn_media_still_lands_on_group_message(self):
        """群聊回合不受影响：登记过的群端点照旧 `GroupMessage`。"""
        bridge = self._group_bridge()
        result = asyncio.run(bridge.transport.send_sticker('100004964', '/tmp/x.png', is_group=True))
        self.assertTrue(result.get('ok'), result)
        self.assertEqual([umo for umo, _chain in bridge.context.sent],
                         ['default:GroupMessage:100004964'])

    def test_registered_conversation_type_beats_the_old_guess(self):
        """私聊登记过的群号不同：`channel_umo` 不给类型时也只在登记过的会话里找。

        真机那次用户号与群号不同，所以两个都试也救不了；这里钉的是"判据"本身：
        私聊坐标在册 → 私聊；群坐标在册 → 群；**不凭空造**。
        """
        bridge = self._bridge([_FakePlatform('aiocqhttp', 'default')])
        bridge.remember_event(object(), object(), self._endpoint(
            user_id='1000008890', is_group=False,
            umo='default:FriendMessage:1000008890', session_id='1000008890',
        ))
        self.assertEqual(bridge.channel_umo('1000008890'), 'default:FriendMessage:1000008890')
        # 没登记过的会话 id：回空串（调用方按"没有投递目标"降级），不猜成群。
        self.assertEqual(bridge.channel_umo('999999999'), '')

    def test_unregistered_private_media_uses_friend_message_from_the_sole_platform(self):
        """重启后没有登记表（但宿主只有一个平台实例）：私聊兜底也必须是 `FriendMessage`。"""
        bridge = self._bridge([_FakePlatform('aiocqhttp', 'default')])
        self.assertEqual(bridge.channel_umo('1000008890', False),
                         'default:FriendMessage:1000008890')
        result = asyncio.run(bridge.transport.send_image('1000008890', '/tmp/x.png', is_group=False))
        self.assertTrue(result.get('ok'), result)
        self.assertEqual([umo for umo, _chain in bridge.context.sent],
                         ['default:FriendMessage:1000008890'])

    def test_ambiguous_platform_is_not_guessed_for_private_media(self):
        """反向：两个平台实例时**不猜**——私聊图片宁可报"没有投递目标"，也不乱发。"""
        bridge = self._bridge([
            _FakePlatform('aiocqhttp', 'default'),
            _FakePlatform('aiocqhttp', 'second'),
        ])
        result = asyncio.run(bridge.transport.send_image('1000008890', '/tmp/x.png', is_group=False))
        self.assertFalse(result.get('ok'), result)
        self.assertEqual(result.get('error'), 'no-session-target')
        self.assertEqual(bridge.context.sent, [])
        self.assertTrue(any('私聊投递无法确定用哪一个' in item for item in self.logs), self.logs)


def _make_bridge(config=None, context=None):
    """构造一个不落盘的 `AstrbotBridge`。"""
    fake_db = FakeDatabase(':memory:')
    with mock.patch.object(bridge_module, 'Database', lambda path: fake_db), \
            mock.patch.object(bridge_module, 'plugin_data_dir', lambda *a, **k: TEST_DATA_DIR):
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
            mock.patch.object(bridge_module, 'plugin_data_dir', lambda *a, **k: TEST_DATA_DIR):
        plugin = main_module.HDSInterludePlugin(context or FakeContext(), config or {})
    plugin.bridge.db = fake_db
    return plugin


def _install_web_request(uploads=None, body=None, query=None, form=None, form_error=None):
    """把插件页请求桩装进 `astrbot.api.web`，返回还原回调。"""
    web = sys.modules['astrbot.api.web']
    previous = web._hdsi_fake_request
    web._hdsi_fake_request = _FakeWebRequest(
        uploads=uploads, body=body, query=query, form=form, form_error=form_error,
    )

    def restore():
        web._hdsi_fake_request = previous

    return restore


def _async_return(value):
    """一个返回固定值的假协程函数（替换 service 方法用）。"""

    async def fake(*_args, **_kwargs):
        return value

    return fake


#: "这个动作没登记过"与"登记成 None"必须分得开（后者是平台真的回了 `data: null`）。
_MISSING = object()


class _FakeActionFailed(Exception):
    """`aiocqhttp.ActionFailed` 的最小替身（只保留探针用得到的两个属性）。

    真实那个类在 `aiocqhttp/exceptions.py:45`：`.result` 是整只失败回执、
    `.retcode` 是它的返回码，`__repr__` 打印成 `<ActionFailed status:'failed', …>`。
    桥接层**不 import** 它（宿主换实现时要能降级），只用属性探针认，所以替身要一样。
    """

    def __init__(self, result: dict):
        super().__init__(
            "<ActionFailed " + ", ".join('%s:%r' % item for item in result.items()) + ">"
        )
        self.result = result

    @property
    def retcode(self):
        return self.result['retcode']


class _FakeOneBotClient:
    """`aiocqhttp` 客户端的桩：把 `call_action` 的调用记下来并按 id / 动作名回帧。

    **回执形状逐字照抄宿主的真实契约**（`aiocqhttp/api_impl.py:28-39` 的
    `_handle_api_result`）：`status == 'failed'` 时**抛** `ActionFailed`，成功时
    **只回 `result['data']`**。早先这个桩回的是整只信封，于是桥接层那个
    "把剥了壳的 `data` 当成'平台没有回执'"的 bug 在测试里永远看不见
    （真机上 `get_cookies` 就是这么废掉的，坑 39）。

    * `pages`：按 `params['id']` 取页（合并转发那批用例）；
    * `actions`：按动作名取（QQ 空间的 `get_cookies` / `get_login_info` 等）；
    * 每条可以是 dict（`data` 本身）、要抛的异常、或 `(action, params) -> 值` 的函数；
    * `envelope=True` 改回"整只信封"的老形状：用来覆盖**另一种** OneBot 客户端实现
      （不是 aiocqhttp 那种会自己抛异常的），桥接层对两种都得认。
    """

    def __init__(self, pages=None, actions=None, envelope: bool = False):  # noqa: D107
        self.pages = dict(pages or {})
        self.actions = dict(actions or {})
        self.envelope = envelope
        self.calls: list[tuple[str, dict]] = []

    async def call_action(self, action, **params):
        self.calls.append((action, dict(params)))
        value = _MISSING
        if action in self.actions:
            value = self.actions[action]
        elif params.get('id') in self.pages:
            value = self.pages[params.get('id')]
        if isinstance(value, BaseException):
            raise value
        if callable(value):
            value = value(action, params)
        if value is _MISSING:
            data: Any = {'messages': []}
        elif value is None:
            return None  # 平台回 `data: null`（显式登记的 None ≠ 没登记）
        elif isinstance(value, dict):
            data = value
        else:
            return value  # 非 dict 的形状原样回（桥接层要能报"平台没有回执（X）"）
        if self.envelope:
            return data
        status = data.get('status')
        if status == 'failed':
            raise _FakeActionFailed(data)
        return data.get('data') if 'data' in data else data

    def request_ids(self):
        return [params.get('id') for _action, params in self.calls]


def _bridge_with_bot(config, bot):
    """装一个"宿主的 OneBot 平台实例"的 bridge（`onebot_client()` 走得通）。"""

    class _PlatformWithBot(_FakePlatform):
        def __init__(self, name, platform_id, client):
            super().__init__(name, platform_id)
            self.bot = client

    context = FakeContext()
    context.platform_manager = FakePlatformManager([_PlatformWithBot('aiocqhttp', 'aiocqhttp', bot)])
    bridge = _make_bridge(config, context=context)
    return bridge


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
            # v1.7.1：QQ 空间的 NapCat WebSocket 方案要自带 Cookie/Referer 的原始 HTTP。
            'request_text',
            # v1.8.6：按入站媒体坐标取字节（宿主本地文件 / 官方下载器 / OneBot get_image）。
            'fetch_incoming_image',
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
# 3b. 入站取字节（§49）：宿主通道与直链下载
# =========================================================================== #

class _HostImageStub:
    """宿主的 `Image` 组件桩：只提供 `convert_to_file_path()`（官方取字节方法）。

    真组件的这个方法会把 URL / `file:///` / `base64://` 归一成一个本地路径
    （`astrbot/core/message/components.py` 的 `Image.convert_to_file_path`）；
    这里直接回一个真文件，好把"宿主通道拿到了就不该走网络"钉住。
    """

    def __init__(self, file, url, path, downloaded=None):
        self._hdsi_kind = 'image'
        self.file = file
        self.url = url
        self.path = path
        #: `convert_to_file_path()` 的返回值：与组件上的 `path` 字段**不是一回事** ——
        #: 前者是"取字节之后得到的本地路径"（可能要下载），后者是宿主手上**已经有**的文件。
        self.downloaded = downloaded

    async def convert_to_file_path(self):
        return self.downloaded or self.path


class _GetImageBot:
    """`aiocqhttp` 客户端桩：只回 `get_image` 的帧，并把调用记下来。"""

    def __init__(self, frame):
        self.frame = frame
        self.calls = []

    async def call_action(self, action, **params):
        self.calls.append((action, dict(params)))
        return self.frame


class IncomingImageByteTests(unittest.TestCase):
    """`Transport.fetch_incoming_image`：宿主通道优先，拿不到才由 core 回退直链（§49.3）。"""

    def setUp(self):
        self.context = FakeContext()
        self.bridge = _make_bridge(context=self.context)
        self.transport = AstrbotTransport(self.bridge)

    @staticmethod
    def _run(awaitable):
        return asyncio.run(awaitable)

    def test_local_file_source_is_read_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'inbound.png')
            with open(path, 'wb') as handle:
                handle.write(b'\x89PNG-local')
            self.assertEqual(
                self._run(self.transport.fetch_incoming_image('onebot-file:%s' % path)),
                b'\x89PNG-local',
            )

    def test_inline_payloads_are_decoded(self):
        import base64

        encoded = base64.b64encode(b'hello').decode('ascii')
        self.assertEqual(self._run(self.transport.fetch_incoming_image('base64://%s' % encoded)), b'hello')
        self.assertEqual(
            self._run(self.transport.fetch_incoming_image('data:image/png;base64,%s' % encoded)), b'hello',
        )

    def test_host_downloader_is_used_for_the_recorded_segment(self):
        """登记过入站事件时走宿主官方方法（`Image.convert_to_file_path()`）。"""
        url = 'https://multimedia.nt.qq.com.cn/download?appid=1406&rkey=CAMSMMtwVq2'
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'host.png')
            with open(path, 'wb') as handle:
                handle.write(b'\x89PNG-host')
            event = IncomingMediaKindTests._event(
                [{'type': 'image', 'data': {'file': 'a.jpg', 'url': url, 'sub_type': 7}}],
                # `path=None`：宿主**还没**落盘，字节只能靠它自己的下载器现取 —— 这正是
                # 真机那条的形状（组件只有 file token + rkey URL）。
                components=[_HostImageStub('a.jpg', url, None, downloaded=path)],
            )
            view = bridge_module.session_view(event)
            self.bridge.remember_event(event, view, bridge_module.endpoint_for_event(event))
            source = view.media[0]['source']
            self.assertEqual(source, url)
            self.assertEqual(self._run(self.transport.fetch_incoming_image(source)), b'\x89PNG-host')

    def test_onebot_get_image_road_returns_bytes(self):
        """NapCat 那条路：`get_image` 的 `base64` 直接变成字节（§49.4）。"""
        import base64

        url = 'https://multimedia.nt.qq.com.cn/download?appid=1406&rkey=xyz'
        bot = _GetImageBot({'status': 'ok', 'retcode': 0, 'data': {
            'base64': base64.b64encode(b'\x89PNG-onebot').decode('ascii'),
        }})
        bridge = _bridge_with_bot({}, bot)
        transport = AstrbotTransport(bridge)
        event = FakeMessageEvent(raw_message={'message': [{
            'type': 'image',
            'data': {'file': 'E734AC389ADCCE0D94883AE67607170B.jpg', 'url': url, 'sub_type': 7},
        }]})
        view = bridge_module.session_view(event)
        bridge.remember_event(event, view, bridge_module.endpoint_for_event(event))
        source = view.media[0]['source']

        self.assertEqual(self._run(transport.fetch_incoming_image(source)), b'\x89PNG-onebot')
        self.assertEqual(bot.calls, [('get_image', {'file': 'E734AC389ADCCE0D94883AE67607170B.jpg'})])

    def test_unknown_source_without_a_recorded_event_degrades_quietly(self):
        """没登记过、也不是本地/内联 → 回 `None`（**绝不抛**、绝不联网）。"""
        self.assertIsNone(
            self._run(self.transport.fetch_incoming_image('https://cdn.example.com/never-seen.png')),
        )
        self.assertIsNone(self._run(self.transport.fetch_incoming_image('')))

    def test_direct_download_carries_user_agent_timeout_and_qq_referer(self):
        """直链下载的请求形状（§49.2）：不再是裸 `httpx.get`。

        真机暴露的正是这里：httpx 默认 **5 秒**超时 + `python-httpx/x.y` UA +
        没有 Referer，而且失败只写 debug。差异本身是可测的（这里用假客户端把
        出站请求抓下来）：UA / 超时 / 腾讯域名的 Referer 必须都在。
        """
        calls = []

        class _Response:
            content = b'\x89PNG-net'

            def raise_for_status(self):
                return None

        class _Client:
            async def get(self, url, **kwargs):
                calls.append((url, kwargs))
                return _Response()

        self.bridge._httpx_client = _Client()
        qq_url = 'https://multimedia.nt.qq.com.cn/download?appid=1406&rkey=a'
        self.assertEqual(self._run(self.bridge.http_get_bytes(qq_url)), b'\x89PNG-net')
        _url, kwargs = calls[0]
        self.assertNotIn('python-httpx', kwargs['headers']['User-Agent'])
        self.assertEqual(kwargs['headers']['Referer'], bridge_module._DOWNLOAD_REFERER)
        self.assertEqual(kwargs['timeout'], bridge_module._DOWNLOAD_TIMEOUT_SECONDS)
        self.assertGreater(kwargs['timeout'], 5.0, 'httpx 默认 5 秒太短，必须显式放大')

        other_url = 'https://cdn.example.com/a.png'
        self.assertEqual(self._run(self.bridge.http_get_bytes(other_url)), b'\x89PNG-net')
        _url, kwargs = calls[1]
        self.assertNotIn('Referer', kwargs['headers'], '非腾讯域名不带来源页')


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
        # 上游 `registerCommands` 注册 38 条命令（32 条原有 + rc28 的 6 条端点/别名/重置）。
        # 配置导出/导入**不占命令**——它是 WebUI 插件页面（`pages/config-backup/`）。
        self.assertEqual(len(main_module.COMMANDS), 38)
        self.assertEqual(len(set(main_module.COMMAND_HANDLERS)), 38)

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
            manage_session_denial=lambda session: None,  # noqa: ARG005
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
            manage_session_denial=lambda session: None,  # noqa: ARG005
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
                mock.patch.object(bridge_module, 'plugin_data_dir', lambda *a, **k: TEST_DATA_DIR):
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
        self.assertEqual(len(plugin.active_commands()), 38)
        self.assertEqual(
            # 32 条原有命令 + 6 条 rc28 端点/别名/重置 + 入站消息处理器。
            len(registry.get_handlers_by_module_name(main_module.HDSInterludePlugin.__module__)), 39,
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

    def test_handle_event_consumes_an_empty_private_event_so_no_second_persona_answers(self):
        """空内容的私聊事件：**吞掉**（受控偏离，见 PORTING_NOTES §18）。

        上游在这里 `next()`（交回其它处理器）。AstrBot 的部署里通常还有第二个聊天
        Agent，交回去就是同一段私聊里冒出第二个人格——实测：用户"一张图 + 一句文字"
        分两条发来，图片那条的消息链解析为空，另一个她回了「主人这是夜班的宵夜吗？」。
        """
        import asyncio

        bridge = _make_bridge()
        event = FakeMessageEvent(message='', components=[])
        replies = asyncio.run(bridge.handle_event(event))
        self.assertEqual(replies, [])
        self.assertTrue(event.stopped, '归我们管的私聊必须吞掉，不能让别的 Agent 接手')

    def test_handle_event_leaves_an_empty_group_event_alone(self):
        import asyncio

        bridge = _make_bridge()
        event = FakeMessageEvent(message='', components=[], group_id='90001')
        self.assertEqual(asyncio.run(bridge.handle_event(event)), [])
        self.assertFalse(event.stopped, '群聊没内容时保持上游语义，交回其它处理器')

    def test_handle_event_leaves_an_empty_private_event_alone_when_capture_is_off(self):
        import asyncio

        bridge = _make_bridge({'runtime': {'capture_direct_messages': False}})
        event = FakeMessageEvent(message='', components=[])
        self.assertEqual(asyncio.run(bridge.handle_event(event)), [])
        self.assertFalse(event.stopped)

    def test_onebot_notices_are_not_mistaken_for_empty_messages(self):
        """OneBot 的 notice（「对方正在输入…」）不是消息：静默吞掉，**不打 warn**。

        用户 2026-09-25 的日志里，NapCat 的 `input_status` 通知一分钟刷出十几条
        「私聊事件没有可用内容」warn，把它真正该看的东西全盖掉了。
        """
        import asyncio

        bridge = _make_bridge()
        event = FakeMessageEvent(message='', components=[])
        event.message_obj.raw_message = {
            'post_type': 'notice', 'notice_type': 'notify', 'sub_type': 'input_status',
            'status_text': '对方正在输入...',
        }
        with mock.patch.object(bridge_module, 'log_fallback') as logged:
            self.assertEqual(asyncio.run(bridge.handle_event(event)), [])
        self.assertTrue(event.stopped, '归我们管的私聊仍然要吞掉，空事件不能漏给第二个人格')
        levels = [item.args[0] for item in logged.call_args_list if item.args]
        self.assertNotIn('warn', levels, '输入状态通知不是内容缺失，不许 warn 刷屏')
        self.assertIn('debug', levels, '静默不等于无痕：debug 里要留下类型')
        self.assertIn('notice:notify:input_status',
                      [str(item.args[1]) % tuple(item.args[2:]) for item in logged.call_args_list
                       if len(item.args) > 2][-1])

    def test_non_message_events_are_classified_without_touching_other_platforms(self):
        """只有显式声明 `post_type` 且不是 `message` 的原始事件才算非消息。"""
        notice = FakeMessageEvent(message='', components=[])
        notice.message_obj.raw_message = {'post_type': 'notice', 'notice_type': 'notify',
                                          'sub_type': 'input_status'}
        self.assertEqual(bridge_module.is_non_message_event(notice),
                         (True, 'notice:notify:input_status'))
        request = FakeMessageEvent(message='', components=[])
        request.message_obj.raw_message = {'post_type': 'request', 'request_type': 'friend'}
        self.assertEqual(bridge_module.is_non_message_event(request), (True, 'request:friend'))
        # 真消息、以及别的平台（没有 `post_type`）一律当消息。
        plain = FakeMessageEvent(message='在吗', components=[Plain('在吗')])
        self.assertEqual(bridge_module.is_non_message_event(plain), (False, ''))
        telegram = FakeMessageEvent(message='hi', components=[Plain('hi')], platform_name='telegram')
        telegram.message_obj.raw_message = {'message': [{'type': 'text', 'data': {'text': 'hi'}}]}
        self.assertEqual(bridge_module.is_non_message_event(telegram), (False, ''))

    def test_a_notice_that_is_not_ours_is_left_to_other_handlers(self):
        """未接管私聊里的通知不消费：capture 关着时仍然交回其它处理器。"""
        import asyncio

        bridge = _make_bridge({'runtime': {'capture_direct_messages': False}})
        event = FakeMessageEvent(message='', components=[])
        event.message_obj.raw_message = {'post_type': 'notice', 'notice_type': 'notify'}
        with mock.patch.object(bridge_module, 'log_fallback'):
            self.assertEqual(asyncio.run(bridge.handle_event(event)), [])
        self.assertFalse(event.stopped)

    def test_session_view_falls_back_to_raw_segments_when_the_chain_is_empty(self):
        """结构化消息链为空时，退回 `message_obj.raw_message` 里的原始段。

        实测：用户"图片 + 文字分两条发"，图片那条事件的链为空 → 我们既看不到图，
        又把事件漏给了宿主默认 Agent（另一个"她"回了「主人这是夜班的宵夜吗？」）。
        """
        event = FakeMessageEvent(message='', components=[])
        event.message_obj.raw_message = {
            'message': [{'type': 'image', 'data': {'url': 'https://example.com/a.jpg'}}],
        }
        view = session_view(event)
        self.assertIn('<img src="https://example.com/a.jpg"/>', view.content)
        self.assertEqual(view.elements[0]['type'], 'img')

    def test_session_view_raw_fallback_handles_text_and_audio(self):
        event = FakeMessageEvent(message='', components=[])
        event.message_obj.raw_message = {'message': [
            {'type': 'text', 'data': {'text': '看图'}},
            {'type': 'record', 'data': {'file': 'v.silk', 'url': 'https://example.com/v.silk'}},
        ]}
        view = session_view(event)
        self.assertIn('看图', view.content)
        self.assertIn('<audio', view.content)
        from plugin.core.service.helpers import extract_session_voice_count

        self.assertEqual(extract_session_voice_count(view), 1)

    def test_explain_unconsumed_names_the_failing_gate(self):
        """`receive()` 返回 False 时必须说得出是哪道门——core 的 debug 报告在默认
        verbosity 下完全看不见（用户实测：只有"未生成回合"一句话，根本猜不出来）。"""
        import asyncio

        bridge = _make_bridge()
        notes = []
        bridge.service.report_standalone_operation = lambda *args, **kwargs: notes.append(args)

        async def no_story(_session):
            return None

        async def no_participant(_session, _story=None):
            return None

        cases = []
        event = FakeMessageEvent(message='看图', components=[Plain('看图')])
        view = session_view(event)

        bridge.service.can_handle_session = lambda _session: False
        cases.append(('白名单未通过', asyncio.run(bridge.explain_unconsumed(view))))
        bridge.service.can_handle_session = lambda _session: True

        bridge.service.find_story = no_story
        cases.append(('找不到剧本', asyncio.run(bridge.explain_unconsumed(view))))

        async def paused_story(_session):
            return {'id': 's', 'status': 'paused'}

        bridge.service.find_story = paused_story
        cases.append(('剧本状态=paused', asyncio.run(bridge.explain_unconsumed(view))))

        async def active_story(_session):
            return {'id': 's', 'status': 'active'}

        bridge.service.find_story = active_story
        bridge.service.find_participant = no_participant
        cases.append(('参与者不存在', asyncio.run(bridge.explain_unconsumed(view))))

        async def active_participant(_session, _story=None):
            return {'id': 'p', 'status': 'paused'}

        bridge.service.find_participant = active_participant
        cases.append(('参与者状态=paused', asyncio.run(bridge.explain_unconsumed(view))))

        async def ok_participant(_session, _story=None):
            return {'id': 'p', 'status': 'active'}

        bridge.service.find_participant = ok_participant
        cases.append(('门都过了', asyncio.run(bridge.explain_unconsumed(view))))

        for expected, reason in cases:
            self.assertIn(expected, reason, '%s → %s' % (expected, reason))

    def test_handle_event_consumes_a_private_event_it_cannot_turn_into_a_turn(self):
        """我们看了却没生成回合时也要吞掉（否则宿主第二个 Agent 接手）。"""
        import asyncio

        bridge = _make_bridge()

        async def fake_receive(_session):
            return False

        bridge.service.receive = fake_receive
        event = FakeMessageEvent(message='普通一句话', components=[Plain('普通一句话')])
        self.assertEqual(asyncio.run(bridge.handle_event(event)), [])
        self.assertTrue(event.stopped)

    def test_handle_event_keeps_an_image_only_private_event(self):
        """只有图片（没有文字）的私聊必须照常进叙事——图片是合法的原生输入。"""
        import asyncio

        bridge = _make_bridge()
        calls = []

        async def fake_receive(session):
            calls.append(session)
            return True

        bridge.service.receive = fake_receive
        event = FakeMessageEvent(message='', components=[Image(url='https://example.com/a.jpg')])
        replies = asyncio.run(bridge.handle_event(event))
        self.assertEqual(replies, [])
        self.assertEqual(len(calls), 1, '图片消息不能被当成"空消息"丢掉')
        self.assertIn('<img', calls[0].content)
        self.assertTrue(event.stopped)

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

        # 今天与明天各排一块：测试正好压在零点前后时，只排"今天"会取不到（2026-09-27 00:00 实拍撞上）。
        from datetime import timedelta
        base = datetime.now(ZoneInfo('Asia/Shanghai')).date()
        block = {'id': 'b1', 'start': '00:00', 'end': '23:59',
                 'kind': 'fixed', 'label': '上班', 'location': '公司'}
        record = {
            'timezone': 'Asia/Shanghai',
            'revision': 3,
            'materializedDays': [
                {'date': (base + timedelta(days=offset)).isoformat(), 'blocks': [block]}
                for offset in (0, 1)
            ],
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


# =========================================================================== #
# 6b. 合并转发读取（上游 `runtime.forwardMessage`，P3）
# =========================================================================== #

def _forward_pages():
    """一页合并转发节点（甲说"你好" + 一张图）。"""
    return {'status': 'ok', 'retcode': 0, 'data': {'messages': [
        {'user_id': '100', 'nickname': '甲', 'message_type': 'group', 'message': [
            {'type': 'text', 'data': {'text': '你好'}},
            {'type': 'image', 'data': {'url': 'https://example.invalid/x'}},
        ]},
    ]}}


class ForwardMessageReadTests(unittest.TestCase):
    """合并转发正文读取：认得出、按预算读、失败只 warn、且**绝不吞掉整条消息**。

    上游 `forward-message.ts` 的读取与归一化是纯逻辑（由 `test_forward_message.py`
    逐条钉住），这里只测适配层那一半：id 从哪来、OneBot 客户端从哪来、读到的正文
    怎么注入 `SessionView`、失败怎么降级。
    """

    def _event(self, bot=None, config=None, raw=None, platform='aiocqhttp', message=''):
        event = FakeMessageEvent(
            message=message, components=[Forward(id='res-1')], raw_message=raw,
            platform_name=platform, platform_id=platform,
        )
        return _bridge_with_bot(config or {}, bot if bot is not None else _FakeOneBotClient()), event

    def test_reads_the_forward_through_the_current_platform_client(self):
        """正常路径：`get_forward_msg` 打到本会话的 OneBot 客户端，正文注入本回合。"""
        import asyncio

        bot = _FakeOneBotClient({'res-1': _forward_pages()})
        bridge, event = self._event(bot)
        calls = []

        async def fake_receive(session):
            calls.append(session)
            return True

        bridge.service.receive = fake_receive
        self.assertEqual(asyncio.run(bridge.handle_event(event)), [])
        self.assertEqual(bot.calls, [('get_forward_msg', {'id': 'res-1'})])
        self.assertEqual(len(calls), 1, '读到了正文就更要走叙事，不能被当成空消息')
        content = calls[0].content
        self.assertIn('<forward id="res-1"/>', content, '原卡片标记必须留着（可见线索）')
        self.assertIn('[合并转发内容｜节点数 1]', content)
        self.assertIn('甲（100）', content)
        self.assertIn('你好', content)
        self.assertIn('[图片]', content)
        self.assertTrue(event.stopped)

    def test_forward_id_is_taken_from_the_raw_onebot_segment(self):
        """`get_message_str()` 只给 `[转发消息]`（不带 id）时，回原始段取 id 才读得到。"""
        import asyncio

        raw = {'post_type': 'message', 'message': [
            {'type': 'forward', 'data': {'id': 'raw-9'}},
        ]}
        event = FakeMessageEvent(message='[转发消息]', components=[], raw_message=raw)
        bot = _FakeOneBotClient({'raw-9': _forward_pages()})
        bridge = _bridge_with_bot({}, bot)
        bridge.service.receive = _async_return(True)
        asyncio.run(bridge.handle_event(event))
        self.assertEqual(bot.request_ids(), ['raw-9'])

    def test_no_forward_means_no_request_and_no_log(self):
        """没有合并转发：一个请求都不发，一行日志都不打（平凡路径必须零成本）。"""
        import asyncio

        bot = _FakeOneBotClient()
        bridge = _bridge_with_bot({}, bot)
        event = FakeMessageEvent(message='普通一句话', components=[Plain('普通一句话')])
        with mock.patch.object(bridge_module, 'log_fallback') as logged:
            result = asyncio.run(bridge.read_forward_for_event(event))
        self.assertIsNone(result)
        self.assertEqual(bot.calls, [])
        self.assertEqual(logged.call_args_list, [], '没有转发就不是异常，不许留日志')

    def test_cq_markup_in_the_text_is_still_recognised(self):
        """兼容路径：别的适配器把 `[CQ:forward,id=…]` 写进了文本。"""
        import asyncio

        bot = _FakeOneBotClient({'cq-1': _forward_pages()})
        bridge = _bridge_with_bot({}, bot)
        event = FakeMessageEvent(message='[CQ:forward,id=cq-1]', components=[])
        result = asyncio.run(bridge.read_forward_for_event(event))
        self.assertIsNotNone(result)
        self.assertFalse(result.failed)
        self.assertEqual(bot.request_ids(), ['cq-1'])

    def test_http_status_and_retcode_are_double_read(self):
        """错误帧两种写法都认：`retcode != 0` 与 `status != ok`（缺一个也要拦住）。"""
        import asyncio

        cases = [
            ({'retcode': 1200, 'wording': '合并转发已过期'}, True),
            ({'status': 'failed', 'message': 'boom'}, True),
            ({'status': 'ok', 'retcode': 0, 'data': {'messages': []}}, False),
        ]
        for page, failed in cases:
            bot = _FakeOneBotClient({'res-1': page})
            bridge = _bridge_with_bot({}, bot)
            event = FakeMessageEvent(message='', components=[Forward(id='res-1')])
            result = asyncio.run(bridge.read_forward_for_event(event))
            self.assertEqual(result.failed, failed, page)

    def test_missing_client_is_a_visible_failure_that_does_not_eat_the_message(self):
        """没有可用的 OneBot 客户端：一条 warn + 只留卡片线索，**消息照常进叙事**。"""
        import asyncio

        bridge = _make_bridge()  # FakeContext 没有平台实例 → `onebot_client()` 回 None
        calls = []

        async def fake_receive(session):
            calls.append(session)
            return True

        bridge.service.receive = fake_receive
        event = FakeMessageEvent(message='', components=[Forward(id='res-1')])
        with mock.patch.object(bridge_module, 'log_fallback') as logged:
            asyncio.run(bridge.handle_event(event))
        levels = [item.args[0] for item in logged.call_args_list if item.args]
        self.assertIn('warn', levels, '真正的异常路径必须让用户看得见')
        self.assertTrue(any('没有可用的 OneBot 客户端' in str(item) for item in logged.call_args_list))
        self.assertEqual(len(calls), 1, '读不到也要进叙事——宁可只说"有一条合并转发"')
        self.assertIn('<forward id="res-1"/>', calls[0].content)
        self.assertNotIn('暂时无法读取内容', calls[0].content,
                         '失败文案不进正文：卡片标记本身就是那条可见线索')
        self.assertTrue(event.stopped)

    def test_timeout_degrades_to_a_warning_and_keeps_the_message(self):
        import asyncio

        # 客户端**直接抛超时**（core 的 `with_timeout` 到期就是这个异常形状）：
        # 适配层必须把它收敛成失败分支，而不是让它冒到 `handle_event` 外面。
        bot = _FakeOneBotClient({'res-1': asyncio.TimeoutError()})
        bridge, event = self._event(bot)
        calls = []

        async def fake_receive(session):
            calls.append(session)
            return True

        bridge.service.receive = fake_receive
        with mock.patch.object(bridge_module, 'log_fallback') as logged:
            asyncio.run(bridge.handle_event(event))
        failures = [item for item in logged.call_args_list
                    if item.args and item.args[0] == 'warn']
        self.assertTrue(failures, '超时是异常路径，必须留一条可见 warn')
        self.assertTrue(any('合并转发' in str(item) for item in failures))
        self.assertEqual(len(calls), 1)
        self.assertIn('<forward id="res-1"/>', calls[0].content)
        self.assertTrue(event.stopped)

    def test_client_exception_degrades_to_a_warning(self):
        import asyncio

        bot = _FakeOneBotClient({'res-1': RuntimeError('连接断了')})
        bridge, event = self._event(bot)
        bridge.service.receive = _async_return(True)
        with mock.patch.object(bridge_module, 'log_fallback') as logged:
            asyncio.run(bridge.handle_event(event))
        warnings = [str(item) for item in logged.call_args_list
                    if item.args and item.args[0] == 'warn']
        # core 会把坏帧 / 坏嵌套包成读取失败（`failure_result()`），适配层据此留一条 warn。
        self.assertTrue(any('合并转发' in item for item in warnings), warnings)

    def test_enabled_false_keeps_the_old_card_behaviour(self):
        """`enabled: false` → 行为与历史版本逐字一致：不请求、只留卡片标记。"""
        import asyncio

        bot = _FakeOneBotClient({'res-1': _forward_pages()})
        bridge, event = self._event(bot, config={'forward_message': {'enabled': False}})
        calls = []

        async def fake_receive(session):
            calls.append(session)
            return True

        bridge.service.receive = fake_receive
        asyncio.run(bridge.handle_event(event))
        self.assertEqual(bot.calls, [])
        self.assertIn('<forward id="res-1"/>', calls[0].content)
        self.assertNotIn('[合并转发内容', calls[0].content)

    def test_legacy_compat_section_is_still_read(self):
        """旧文件里的隐藏兼容位 `forward_message_compat` 仍要认（转正不能弄丢旧配置）。"""
        import asyncio

        bot = _FakeOneBotClient({'res-1': {'status': 'ok', 'retcode': 0, 'data': {'messages': [
            {'nickname': '甲', 'message': [{'type': 'text', 'data': {'text': '旧段位'}}]},
            {'nickname': '乙', 'message': [{'type': 'text', 'data': {'text': '第二条'}}]},
        ]}}})
        bridge, event = self._event(bot, config={'forward_message_compat': {'max_nodes': 1}})
        result = asyncio.run(bridge.read_forward_for_event(event))
        self.assertEqual(result.node_count, 1, '预算要从旧段位读出来并生效')
        self.assertNotIn('第二条', result.content)

        # 旧段位里的 `enabled: false` 同样要生效（开关也走同一条归一）。
        off = _bridge_with_bot(
            {'forward_message_compat': {'enabled': False}},
            _FakeOneBotClient({'res-1': _forward_pages()}),
        )
        off_event = FakeMessageEvent(message='', components=[Forward(id='res-1')])
        self.assertIsNone(asyncio.run(off.read_forward_for_event(off_event)))

    def test_non_onebot_platform_is_not_read_and_not_consumed(self):
        """非 OneBot 平台：一条 debug、不请求、**也不替别的平台消费事件**。"""
        import asyncio

        bot = _FakeOneBotClient({'res-1': _forward_pages()})
        bridge, event = self._event(bot, platform='telegram')
        # 走完整的 `handle_event`：那条路径才会先 `remember_event`（坐标 = telegram），
        # 直接调 `read_forward_for_event` 时 `_current_endpoint` 还是空的。
        calls = []

        async def fake_receive(session):
            calls.append(session)
            return True

        bridge.service.receive = fake_receive
        with mock.patch.object(bridge_module, 'log_fallback') as logged:
            asyncio.run(bridge.handle_event(event))
        self.assertEqual(bot.calls, [], '不是 OneBot 平台，一个请求都不该发')
        levels = [item.args[0] for item in logged.call_args_list if item.args]
        self.assertNotIn('warn', levels, '不是异常，别用 warn 刷屏')
        # 消息照常进叙事（那是既有的私聊归属逻辑，与合并转发无关），只是**没有正文注入**。
        self.assertEqual(len(calls), 1)
        self.assertNotIn('[合并转发内容', calls[0].content)

    def test_forward_read_context_shapes(self):
        """注入形态（纯函数）：贴、只留线索、以及"正文为空"那一种边界。"""
        from plugin.core.forward_message import ForwardReadResult, failure_result

        read = ForwardReadResult(
            content='[合并转发内容｜节点数 1]\n你好', node_count=1,
            forward_count=0, truncated=False, failed=False,
        )
        self.assertEqual(bridge_module.forward_read_context('<forward id="x"/>', read),
                         '<forward id="x"/>\n[合并转发内容｜节点数 1]\n你好')
        self.assertEqual(bridge_module.forward_read_context('<forward id="x"/>', None), '<forward id="x"/>')
        self.assertEqual(bridge_module.forward_read_context('<forward id="x"/>', failure_result()),
                         '<forward id="x"/>', '失败时不把占位文案塞进正文')
        self.assertEqual(bridge_module.forward_read_context('', failure_result()), '<forward />',
                         '原内容为空时必须补个卡片标记：空串会被当成"没有可用内容"')
        self.assertEqual(bridge_module.forward_read_context('', read), read.content)

    def test_forward_section_normalises_both_spellings(self):
        """配置段双拼写：schema 里的 snake_case 与上游的 camelCase 都要读出来。"""
        bridge = _make_bridge({'forward_message': {
            'enabled': True, 'max_nodes': 5, 'max_characters': 900, 'max_depth': 1,
        }})
        section = bridge.forward_section()
        self.assertEqual(section['max_nodes'], 5)
        self.assertEqual(section['maxNodes'], 5, 'camelCase 也要在，core 优先读它')
        self.assertEqual(section['maxCharacters'], 900)
        self.assertEqual(section['maxDepth'], 1)
        self.assertTrue(section['enabled'])

    def test_forward_ids_are_collected_from_both_sources(self):
        """id 收集：原始段优先、CQ 标记与卡片组件兜底、去重保序。"""
        raw = {'post_type': 'message', 'message': [
            {'type': 'forward', 'data': {'id': 'raw-9'}},
        ]}
        event = FakeMessageEvent(
            message='[CQ:forward,id=cq-1]', components=[Forward(id='res-1')], raw_message=raw,
        )
        bridge = _make_bridge()
        self.assertEqual(bridge.forward_ids_for_event(event), ['raw-9', 'cq-1', 'res-1'])


# =========================================================================== #
# 6c. 转发媒体的三个出口（v1.8.7）：attachments / 视觉 sources / 表情包收藏
# =========================================================================== #

def _forward_image_pages(count, prefix='https://gchat.qpic.cn/ft/', **segment):
    """一页合并转发节点：一个节点里 `count` 张图（坐标各不相同）。"""
    return {'status': 'ok', 'retcode': 0, 'data': {'messages': [
        {'user_id': '100', 'nickname': '甲', 'message_type': 'group', 'message': [
            {'type': 'image', 'data': {
                'url': '%s%d' % (prefix, index), 'sub_type': '0', 'summary': '', **segment,
            }} for index in range(1, count + 1)
        ]},
    ]}}


class ForwardMediaIntegrationTests(unittest.TestCase):
    """转发节点里的图**怎么被交给模型**：并进同一条结构化媒体链路（§46/§52）。

    这里只走适配层那半段（`SessionView.media`），核心那半段的规矩由
    `test_forward_message.py` 与 `test_service_chunk*.py` 分别钉着。
    """

    def test_merged_media_keeps_the_direct_ones_first_and_dedupes(self):
        media = bridge_module.merge_forward_media(
            [{'kind': 'image', 'source': 'https://gchat.qpic.cn/direct/1', 'summary': ''}],
            [
                {'source': 'https://gchat.qpic.cn/ft/1', 'kind': 'image', 'summary': ''},
                {'source': 'https://gchat.qpic.cn/direct/1', 'kind': 'image', 'summary': ''},
                {'source': 'https://gchat.qpic.cn/ft/2', 'kind': 'sticker', 'summary': '[中午好]'},
            ],
        )
        self.assertEqual(
            [item['source'] for item in media],
            ['https://gchat.qpic.cn/direct/1', 'https://gchat.qpic.cn/ft/1',
             'https://gchat.qpic.cn/ft/2'],
            '直发在前；与直发同坐标的那张不重复挂',
        )
        self.assertEqual(media[-1]['kind'], 'sticker', '种类原样带过来（判据不在这里）')
        self.assertEqual(media[-1]['source_kind'], 'url')

    def test_merge_treats_a_missing_media_table_as_missing(self):
        """一条转发媒体都没有时**原样返回**：`None` 与 `[]` 是两件事。"""
        self.assertIsNone(bridge_module.merge_forward_media(None, []))
        self.assertEqual(bridge_module.merge_forward_media([], []), [])

    def test_merge_caps_the_total_and_counts_cards_outside_the_cap(self):
        forwarded = [{'source': 'https://gchat.qpic.cn/ft/%d' % i, 'kind': 'image'} for i in range(1, 9)]
        forwarded.append({'source': '', 'kind': 'card'})
        media = bridge_module.merge_forward_media([], forwarded, max_per_turn=3)
        images = [item for item in media if item['kind'] == 'image']
        self.assertEqual(len(images), 3, '整条消息的转发媒体总数要封顶')
        # 卡片没有来源、也不花视觉 token：它不占图片额度（这里它没有来源，所以被过滤）。
        self.assertEqual([item['source'] for item in images],
                         ['https://gchat.qpic.cn/ft/1', 'https://gchat.qpic.cn/ft/2',
                          'https://gchat.qpic.cn/ft/3'])

    def test_the_merge_cap_follows_the_configured_image_budget(self):
        """v1.9.4 反向：`max_images` 调到 10 时，媒体表**不许**被老常量 6 削回 6 张。

        用户那次真机报告的另一半：改了 `forward_message.max_images` 却"没用"。
        媒体表削掉的条目下游连"一共几张"都数不出来，所以上限必须跟着预算走。
        """
        forwarded = [{'source': 'https://gchat.qpic.cn/ft/%d' % i, 'kind': 'image'} for i in range(1, 11)]
        # 老常量单用会削掉 4 张 —— 那正是要堵的现场。
        self.assertEqual(len(bridge_module.merge_forward_media([], forwarded, 6)), 6)
        cap = bridge_module.forward_media_turn_cap(3, {'maxImages': 10})
        self.assertEqual(cap, 10)
        self.assertEqual(len(bridge_module.merge_forward_media([], forwarded, cap)), 10)

    def test_the_bridge_reads_the_per_turn_image_budget_from_the_schema_group(self):
        """`model_center.vision.max_per_turn` 真的接到适配层（三处同改里的读取侧）。"""
        bot = _FakeOneBotClient({'res-1': _forward_image_pages(10)})
        config = {
            'forward_message': {'max_images': 10},
            'model_center': {'vision': {'enabled': True, 'max_per_turn': 10}},
        }
        bridge = _bridge_with_bot(config, bot)
        self.assertEqual(bridge.image_budget(), 10)
        self.assertEqual(bridge.forward_media_turn_cap(), 10, '媒体表上限跟着预算走')
        event = FakeMessageEvent(message='', components=[Forward(id='res-1')], raw_message={
            'post_type': 'message', 'message': [{'type': 'forward', 'data': {'id': 'res-1'}}],
        })
        received = []

        async def fake_receive(session):
            received.append(session)
            return True

        bridge.service.receive = fake_receive
        asyncio.run(bridge.handle_event(event))
        self.assertEqual(len(received[0].media), 10, '十张读到的图全都进媒体表，一张不丢')

    def test_the_budget_default_reaches_the_bridge_when_the_key_is_absent(self):
        """没配新键 → 3（省成本那侧），媒体表上限不因此变小。"""
        bridge = _bridge_with_bot({'forward_message': {'max_images': 3}}, _FakeOneBotClient({}))
        self.assertEqual(bridge.image_budget(), 3)
        self.assertGreaterEqual(bridge.forward_media_turn_cap(), 6)

    def test_raising_only_the_card_cap_reaches_the_bridge(self):
        """v1.9.4：只把 `forward_message.max_images` 调到 10、`max_per_turn` 没动 →
        适配层算出来的每回合预算就是 10（与 core 的 `chunk3` 同一个数）。

        变异保护：适配层自己再算一份默认值（不把单卡上限交给
        `vision_budget.resolve_image_budget`）→ 这条红。
        """
        bot = _FakeOneBotClient({'res-1': _forward_image_pages(10)})
        bridge = _bridge_with_bot({'forward_message': {'max_images': 10}}, bot)
        self.assertEqual(bridge.image_budget(), 10)
        self.assertEqual(bridge.forward_media_turn_cap(), 10)
        # 显式改过每回合上限时以它为准（跟随只管"没改过"的那一档）。
        other = _bridge_with_bot({
            'forward_message': {'max_images': 10},
            'model_center': {'vision': {'enabled': True, 'max_per_turn': 2}},
        }, _FakeOneBotClient({}))
        self.assertEqual(other.image_budget(), 2)

    def test_forward_images_become_sources_and_attachments_on_the_session(self):
        """端到端那一半：转发的图进 `SessionView.media` → 视觉来源 / 附件原料。"""
        bot = _FakeOneBotClient({'res-1': _forward_image_pages(2)})
        bridge = _bridge_with_bot({'forward_message': {'max_images': 3}}, bot)
        event = FakeMessageEvent(message='', components=[Forward(id='res-1')], raw_message={
            'post_type': 'message', 'message': [{'type': 'forward', 'data': {'id': 'res-1'}}],
        })
        received = []

        async def fake_receive(session):
            received.append(session)
            return True

        bridge.service.receive = fake_receive
        asyncio.run(bridge.handle_event(event))
        self.assertEqual(len(received), 1, '读到了就该走叙事')
        session = received[0]
        sources = [item['source'] for item in session.media]
        self.assertEqual(sources, ['https://gchat.qpic.cn/ft/1', 'https://gchat.qpic.cn/ft/2'])
        # 视觉来源就是媒体表的规范化形式（core 侧同一份数据）——`imageCount` 取的就是它。
        from plugin.core.service.chunk3 import _extract_session_image_sources
        self.assertEqual(_extract_session_image_sources(session), sources)
        self.assertGreater(len(sources), 0, 'imageCount 要真的 > 0（这次修的正是它）')

    def test_over_budget_keeps_the_text_clue_and_the_card_mark(self):
        bot = _FakeOneBotClient({'res-1': _forward_image_pages(15)})
        bridge = _bridge_with_bot({'forward_message': {'max_images': 2}}, bot)
        event = FakeMessageEvent(message='', components=[Forward(id='res-1')], raw_message={
            'post_type': 'message', 'message': [{'type': 'forward', 'data': {'id': 'res-1'}}],
        })
        received = []

        async def fake_receive(session):
            received.append(session)
            return True

        bridge.service.receive = fake_receive
        asyncio.run(bridge.handle_event(event))
        session = received[0]
        self.assertIn('<forward id="res-1"/>', session.content, '卡片线索不能丢')
        self.assertIn('[图片×15，仅取前 2 张]', session.content, '超预算要留下可数线索')
        self.assertEqual(len(session.media), 2)

    def test_config_max_images_is_read_from_the_schema_group(self):
        """`forward_message.max_images` 真的接到读取侧（三处同改里的第三处）。"""
        bot = _FakeOneBotClient({'res-1': _forward_image_pages(5)})
        bridge = _bridge_with_bot({'forward_message': {'max_images': 1}}, bot)
        event = FakeMessageEvent(message='', components=[Forward(id='res-1')], raw_message={
            'post_type': 'message', 'message': [{'type': 'forward', 'data': {'id': 'res-1'}}],
        })
        received = []

        async def fake_receive(session):
            received.append(session)
            return True

        bridge.service.receive = fake_receive
        asyncio.run(bridge.handle_event(event))
        self.assertEqual(len(received[0].media), 1)

    def test_fetch_failure_keeps_the_text_and_the_placeholders_verbatim(self):
        """**反向用例**：取不到节点 → 正文与占位符逐字不变 + 一条 warn，媒体为空。"""
        bot = _FakeOneBotClient({'res-1': RuntimeError('连接断了')})
        bridge = _bridge_with_bot({}, bot)
        event = FakeMessageEvent(message='', components=[Forward(id='res-1')], raw_message={
            'post_type': 'message', 'message': [{'type': 'forward', 'data': {'id': 'res-1'}}],
        })
        received = []

        async def fake_receive(session):
            received.append(session)
            return True

        bridge.service.receive = fake_receive
        with mock.patch.object(bridge_module, 'log_fallback') as logged:
            asyncio.run(bridge.handle_event(event))
        session = received[0]
        self.assertEqual(session.content, '<forward id="res-1"/>',
                         '失败时正文逐字不变（不塞失败文案、也不丢卡片线索）')
        self.assertEqual(session.media, [], '取不到就没有媒体条目')
        warnings = [str(item) for item in logged.call_args_list
                    if item.args and item.args[0] == 'warn']
        self.assertTrue(any('合并转发' in item for item in warnings), warnings)

    def test_forward_sticker_kind_reaches_the_collection_hook(self):
        """转发里的收藏表情走**同一套三档判据**被收藏（转发 ≠ 另一套判据）。

        * `sub_type=1`（观测到就是收藏表情）→ 一档，直接是 `sticker`；
        * `sub_type=7` + 方括号名字（候选档）→ 判据在**入站当次**判名字信号，
          「方括号名字」那把尺子只有一处实现（`helpers.sticker_media_signal`），
          转发来的候选走的是**同一次调用**。
        """
        pages = {'res-1': {'status': 'ok', 'retcode': 0, 'data': {'messages': [
            {'user_id': '100', 'nickname': '甲', 'message': [
                {'type': 'image', 'data': {
                    'url': 'https://gchat.qpic.cn/ft/sticker', 'sub_type': '1',
                    'summary': '[中午好]',
                }},
                {'type': 'image', 'data': {
                    'url': 'https://gchat.qpic.cn/ft/candidate', 'sub_type': '7',
                    'summary': '[猫猫叹气]',
                }},
                {'type': 'image', 'data': {
                    'url': 'https://gchat.qpic.cn/ft/photo', 'sub_type': '0', 'summary': '',
                }},
            ]},
        ]}}}
        bot = _FakeOneBotClient({'res-1': pages['res-1']})
        bridge = _bridge_with_bot({}, bot)
        event = FakeMessageEvent(message='', components=[Forward(id='res-1')], raw_message={
            'post_type': 'message', 'message': [{'type': 'forward', 'data': {'id': 'res-1'}}],
        })
        received = []

        async def fake_receive(session):
            received.append(session)
            return True

        bridge.service.receive = fake_receive
        asyncio.run(bridge.handle_event(event))
        from plugin.core.service.chunk3 import _extract_session_media
        from plugin.core.service.helpers import collectible_sticker_kind
        media = _extract_session_media(received[0])
        self.assertEqual([item['kind'] for item in media], ['sticker', 'sticker', 'image'])
        # 一档判据（唯一入口）认了前两条；那条普通照片照旧不收。
        self.assertEqual([collectible_sticker_kind(item['kind']) for item in media],
                         ['sticker', 'sticker', ''],
                         '转发与直发同一条判据')
        self.assertEqual(media[0]['label'], '[表情包]', '标签也走同一个 media_kind_label')
        self.assertEqual(media[1]['label'], '[表情包]')
        self.assertEqual(media[2]['label'], '[图片]')

    def test_the_media_does_not_survive_into_the_next_event(self):
        """媒体只在入站当次活着：下一条没有转发的事件不许挂上一条的图。"""
        bot = _FakeOneBotClient({'res-1': _forward_image_pages(2)})
        bridge = _bridge_with_bot({}, bot)
        first = FakeMessageEvent(message='', components=[Forward(id='res-1')], raw_message={
            'post_type': 'message', 'message': [{'type': 'forward', 'data': {'id': 'res-1'}}],
        })
        received = []

        async def fake_receive(session):
            received.append(session)
            return True

        bridge.service.receive = fake_receive
        asyncio.run(bridge.handle_event(first))
        self.assertEqual(len(received[0].media), 2)
        second = FakeMessageEvent(message='普通一句话', components=[Plain('普通一句话')])
        asyncio.run(bridge.handle_event(second))
        self.assertEqual(received[1].media, [], '上一条转发的图不许漏到下一条')


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
        self.assertEqual(len(main_module.COMMANDS), 38, '上游 38 条命令不应被配置功能污染')

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
            f'/{main_module.PLUGIN_NAME}/console/chats',
            f'/{main_module.PLUGIN_NAME}/console/chat-history',
            f'/{main_module.PLUGIN_NAME}/console/flags',
            f'/{main_module.PLUGIN_NAME}/console/connections',
            f'/{main_module.PLUGIN_NAME}/console/connections-delete',
            f'/{main_module.PLUGIN_NAME}/console/config',
            f'/{main_module.PLUGIN_NAME}/console/config-set',
            f'/{main_module.PLUGIN_NAME}/console/participants',
            f'/{main_module.PLUGIN_NAME}/console/stories',
            # v1.5.1：Token 用量统计（本移植版新增面板）。
            f'/{main_module.PLUGIN_NAME}/console/token-stats',
            # 平台动作目录与权限（本移植版新增面板「动作」）。
            f'/{main_module.PLUGIN_NAME}/console/actions',
            f'/{main_module.PLUGIN_NAME}/console/action-permission',
            f'/{main_module.PLUGIN_NAME}/console/action-permissions-reset',
            f'/{main_module.PLUGIN_NAME}/console/patch-decide',
            f'/{main_module.PLUGIN_NAME}/console/patch-rollback',
            f'/{main_module.PLUGIN_NAME}/console/story-merge',
            f'/{main_module.PLUGIN_NAME}/console/story-promote',
            # 共同作品（上游 rc28 `works.ts` 的界面；入口由本移植版补）。
            f'/{main_module.PLUGIN_NAME}/console/works',
            f'/{main_module.PLUGIN_NAME}/console/work',
            f'/{main_module.PLUGIN_NAME}/console/work-create',
            f'/{main_module.PLUGIN_NAME}/console/work-accept',
            f'/{main_module.PLUGIN_NAME}/console/work-reject',
            f'/{main_module.PLUGIN_NAME}/console/work-edit',
            f'/{main_module.PLUGIN_NAME}/console/work-generate',
            f'/{main_module.PLUGIN_NAME}/console/work-export',
            f'/{main_module.PLUGIN_NAME}/console/work-cancel',
            # 表情库（v1.8.0）：列表 / 原图 / 改描述 / 交回自动描述 / 删除 / 重扫。
            f'/{main_module.PLUGIN_NAME}/console/stickers',
            f'/{main_module.PLUGIN_NAME}/console/sticker-file',
            f'/{main_module.PLUGIN_NAME}/console/sticker-update',
            f'/{main_module.PLUGIN_NAME}/console/sticker-restore-description',
            f'/{main_module.PLUGIN_NAME}/console/sticker-delete',
            f'/{main_module.PLUGIN_NAME}/console/sticker-rescan',
            # 表情库分组与上传（v1.8.3，§47）：分组清单 / 新建改名 / 删除 / 批量移动 / 上传。
            f'/{main_module.PLUGIN_NAME}/console/sticker-groups',
            f'/{main_module.PLUGIN_NAME}/console/sticker-group-save',
            f'/{main_module.PLUGIN_NAME}/console/sticker-group-delete',
            f'/{main_module.PLUGIN_NAME}/console/sticker-move',
            f'/{main_module.PLUGIN_NAME}/console/sticker-upload',
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
            # 平台动作权限：写权限表（未知动作 / 未知档位由后端 400 拒绝）
            f'/{main_module.PLUGIN_NAME}/console/action-permission',
            f'/{main_module.PLUGIN_NAME}/console/action-permissions-reset',
            f'/{main_module.PLUGIN_NAME}/console/patch-decide',
            f'/{main_module.PLUGIN_NAME}/console/patch-rollback',
            f'/{main_module.PLUGIN_NAME}/console/story-merge',
            f'/{main_module.PLUGIN_NAME}/console/story-promote',
            # 共同作品：新建 / 接受 / 驳回 / 手改 / 让她起草 / 取消任务
            # （接受与驳回**只有用户能做**——她只能提议）
            f'/{main_module.PLUGIN_NAME}/console/work-create',
            f'/{main_module.PLUGIN_NAME}/console/work-accept',
            f'/{main_module.PLUGIN_NAME}/console/work-reject',
            f'/{main_module.PLUGIN_NAME}/console/work-edit',
            f'/{main_module.PLUGIN_NAME}/console/work-generate',
            f'/{main_module.PLUGIN_NAME}/console/work-cancel',
            # 表情库：改描述 / 交回自动描述 / 删除 / 重扫（写操作一律 POST）
            f'/{main_module.PLUGIN_NAME}/console/sticker-update',
            f'/{main_module.PLUGIN_NAME}/console/sticker-restore-description',
            f'/{main_module.PLUGIN_NAME}/console/sticker-delete',
            f'/{main_module.PLUGIN_NAME}/console/sticker-rescan',
            # 表情库分组与上传（§47）：新建改名 / 删除 / 批量移动 / 上传（一律 POST）
            f'/{main_module.PLUGIN_NAME}/console/sticker-group-save',
            f'/{main_module.PLUGIN_NAME}/console/sticker-group-delete',
            f'/{main_module.PLUGIN_NAME}/console/sticker-move',
            f'/{main_module.PLUGIN_NAME}/console/sticker-upload',
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


class HtmlTextExtractionTests(unittest.TestCase):
    """网页观察的正文提取：装饰性容器整块丢掉（v1.3.4）。

    为什么值得钉住：SearXNG 的语言下拉有两千多字，全落在 `max_excerpt_characters`（默认 3000）
    里的话，她"读到"的就只有一份语言列表——搜索结果一个字都进不去。
    """

    def test_decorative_containers_are_dropped(self):
        from plugin.adapters.astrbot_bridge import _strip_html
        page = (
            '<html><head><title>标题</title></head><body>'
            '<nav>首页 关于 联系</nav>'
            '<select><option>Afrikaans [af]</option><option>Dansk [da]</option></select>'
            '<svg><title>search</title><path d="M10 10"/></svg>'
            '<button>提交</button><footer>© 2026 某某</footer><noscript>请开 JS</noscript>'
            '<header><h1>文章标题</h1></header>'
            '<form><label>邮箱</label><input/></form>'
            '<script>var a = 1;</script><style>.a{}</style>'
            '<p>正文第一段。</p><p>正文第二段。</p>'
            '</body></html>'
        )
        text = _strip_html(page)
        for noise in ('首页 关于', 'Afrikaans', 'search', '提交', '© 2026', '请开 JS', 'var a = 1'):
            self.assertNotIn(noise, text, noise)
        # `<header>` 与 `<form>` 保留：文章标题常常就在 header 里。
        self.assertIn('文章标题', text)
        self.assertIn('邮箱', text)
        self.assertIn('正文第一段。', text)
        self.assertIn('正文第二段。', text)

    def test_wrapped_console_landing_keeps_results_at_the_front(self):
        from plugin.adapters.astrbot_bridge import _strip_html
        languages = ''.join('<option>%s [x]</option>' % ('L%d' % i) for i in range(200))
        page = '<select>%s</select><article><h3>结果一</h3><p>摘要一</p></article>' % languages
        text = _strip_html(page)
        self.assertLess(text.index('结果一'), 40, '结果必须排在很前面')
        self.assertNotIn('L199', text)


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

    def __init__(self, provider_id='prov-1', modalities=None, errors=None, finish_reason=None):
        self.provider_id = provider_id
        self.provider = _FakeProvider(provider_id, modalities)
        self.errors = list(errors or [])
        self.calls: list[dict] = []
        #: 宿主 `LLMResponse.raw_completion` 里报的停止原因（None = 拿不到）。
        self.finish_reason = finish_reason

    async def get_current_chat_provider_id(self, umo):  # noqa: ARG002
        return self.provider_id

    def get_provider_by_id(self, provider_id):  # noqa: ARG002
        return self.provider

    async def llm_generate(self, **kwargs):
        self.calls.append(kwargs)
        if self.errors:
            raise self.errors.pop(0)
        reason = self.finish_reason

        class _Response:
            completion_text = '模型回复'
            usage = {'prompt_tokens': 3, 'completion_tokens': 5}
            raw_completion = (
                types.SimpleNamespace(choices=[types.SimpleNamespace(finish_reason=reason)])
                if reason else None
            )

        return _Response()


class _RecordingBridge:
    """只实现 `AstrbotHttpClient` 会用到的那几个 bridge 方法。"""

    def __init__(self, context, task_models=None, vision_mode='native'):
        self.context = context
        self._current_umo = 'aiocqhttp:FriendMessage:1'
        self.task_models = dict(task_models or {})
        self.vision_mode = vision_mode
        self.stt_enabled = True

    async def resolve_chat_provider_id(self):
        return self.context.provider_id

    def task_model_id(self, task):
        return self.task_models.get(task or '', '')

    def vision_mode_native(self):
        return self.vision_mode != 'sidecar'

    def audio_transcription_enabled(self):
        """`model_center.audio.stt_enabled`（v1.7.5）：默认 `True`（= 历史行为）。

        单独有 `test_the_stt_switch_actually_gates_the_transcribe_path` 钉住它真的
        会拦住 `_transcribe`。
        """
        return self.stt_enabled

    def provider_by_id(self, provider_id):
        if not provider_id:
            return None
        return self.context.get_provider_by_id(provider_id)

    def provider_modalities(self, provider):
        config = getattr(provider, 'provider_config', None) or {}
        values = config.get('modalities')
        return {str(item).lower() for item in values} if isinstance(values, list) else set()


def _make_client(context, task_models=None, vision_mode='native', stt_enabled=True):
    """绕开 `__init__` 直接装配一个只走 chat 路由的 `AstrbotHttpClient`。"""
    client = bridge_module.AstrbotHttpClient.__new__(bridge_module.AstrbotHttpClient)
    client.bridge = _RecordingBridge(context, task_models=task_models, vision_mode=vision_mode)
    client.bridge.stt_enabled = stt_enabled
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
        warnings = [
            item for item in logged.call_args_list
            if item.args and item.args[0] == 'warn' and '图片会被忽略' in str(item.args[1])
        ]
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
            len([
                item for item in logged.call_args_list
                if item.args and item.args[0] == 'warn' and '图片会被忽略' in str(item.args[1])
            ]), 1,
        )

    def test_sidecar_mode_never_sends_images_to_the_main_model(self):
        """`vision.mode = sidecar` 时图片不该出现在主叙事请求里（识图交给侧端连接）。"""
        context = _RecordingContext(modalities=['text', 'image'])
        client = _make_client(context, vision_mode='sidecar')
        self._run(client, _multimodal_payload())
        self.assertNotIn('image_urls', context.calls[0])

    def test_sampling_parameters_the_host_drops_are_reported_once(self):
        """走 AstrBot 模型时 `temperature` / `max_tokens` **到不了请求**——必须说出来。

        AstrBot 的 `llm_generate(**kwargs)` 只把额外参数转给 Provider 的
        `text_chat(**kwargs)`，内置实现把它们丢掉（`_prepare_chat_payload` 造出的
        payloads 里只有 `messages` / `model`）。插件改不了宿主的这条边界，但"用户改了
        「主叙事最大输出 token」却什么都没发生"必须能在日志里看见（坑 25）。
        同一条原因只说一次：连跑两轮只留一条 warn。
        文案要说**当前请求里的值**（v1.9.7）：用户拿它跟自己填的数对得上账。
        """
        context = _RecordingContext(modalities=['text', 'image'])
        client = _make_client(context)
        with mock.patch.object(bridge_module, 'log_fallback') as logged:
            self._run(client, _multimodal_payload())
            self._run(client, _multimodal_payload())
        said = [
            item for item in logged.call_args_list
            if item.args and item.args[0] == 'warn' and '不吃这些参数' in str(item.args)
        ]
        self.assertEqual(len(said), 1, '同一条原因只说一次，别每轮刷屏')
        text = str(said[0].args)
        self.assertIn('temperature', text)
        self.assertIn('max_tokens', text)
        self.assertIn('max_tokens=%s' % _multimodal_payload()['max_tokens'], text,
                      '要带出实际值，别只说"不生效"')
        self.assertIn('「模型连接」', text, '要说清去哪儿改')
        self.assertIn('直连 endpoint', text, '要说清怎么改')

    def test_the_host_finish_reason_is_carried_through(self):
        """宿主报"输出到顶了"时，插件不许把它改写成 `stop`（v1.9.7）。

        截断与"模型胡说"在日志里长得一模一样；`finish_reason` 是 core 唯一拿得到的
        那条线索（`narrator._warn_output_truncated()`）。这里恒写 `'stop'` 就等于
        把宿主唯一一句真话吞掉。反向：改回恒 `'stop'` → 本条红。
        """
        context = _RecordingContext(modalities=['text'], finish_reason='length')
        result = self._run(_make_client(context), {
            'model': 'x', 'messages': [{'role': 'user', 'content': '你好'}],
        })
        self.assertEqual(result['choices'][0]['finish_reason'], 'length')
        # 拿不到（老宿主 / 自造响应）就照旧 `'stop'`，不猜。
        self.assertEqual(bridge_module._host_finish_reason(types.SimpleNamespace()), 'stop')
        self.assertEqual(bridge_module._host_finish_reason(None), 'stop')

    def test_a_payload_without_sampling_parameters_says_nothing(self):
        """没设采样参数就无话可说——不许对每一轮都念一遍。"""
        context = _RecordingContext(modalities=['text'])
        with mock.patch.object(bridge_module, 'log_fallback') as logged:
            self._run(_make_client(context), {'model': 'x', 'messages': [{'role': 'user', 'content': '你好'}]})
        self.assertFalse([
            item for item in logged.call_args_list
            if item.args and item.args[0] == 'warn' and '不吃这些参数' in str(item.args)
        ])

    def test_the_typeerror_retry_names_the_parameters_it_dropped(self):
        """宿主不吃这些参数时的重发**不是无痕降级**：丢掉了什么，日志里要有名字。

        重试那一趟连 `image_urls` / `audio_urls` 一起丢——"看起来发了、模型根本没
        看到图"是静默失明，比报错难查得多。
        """
        context = _RecordingContext(modalities=['text', 'image', 'audio'], errors=[TypeError('boom')])
        with mock.patch.object(bridge_module, 'log_fallback') as logged:
            self._run(_make_client(context), _multimodal_payload())
        said = [
            item for item in logged.call_args_list
            if item.args and item.args[0] == 'warn' and '已去掉它们重发一次' in str(item.args)
        ]
        self.assertEqual(len(said), 1)
        text = str(said[0].args)
        self.assertIn('image_urls', text)
        self.assertIn('max_tokens', text)

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

    def test_the_stt_switch_actually_gates_the_transcribe_path(self):
        """`stt_enabled=False` → **不调**转写模型，语音按音频证据交给主模型（v1.7.5）。

        这是「加了开关没接线」的反面用例：开关必须真的拦在那条通路上，否则用户会以为
        关掉就不转写了，实际上照转（死开关比没有更糟）。
        """
        context = _RecordingContext(provider_id='main-provider', modalities=['text', 'audio'])

        class _Stt:
            def __init__(self):
                self.urls = []

            async def get_text(self, url):
                self.urls.append(url)
                return '不该被调到的转写结果'

        stt = _Stt()
        client = _make_client(context, task_models={'audio': 'whisper-local'}, stt_enabled=False)
        client.bridge.provider_by_id = lambda provider_id: stt  # type: ignore[method-assign]
        self._run(client, {'model': 'x', 'messages': [{'role': 'user', 'content': [
            {'type': 'text', 'text': '听一下'},
            {'type': 'input_audio', 'input_audio': {'data': 'QUJD', 'format': 'mp3'}},
        ]}]}, task='main')
        self.assertEqual(stt.urls, [], '关掉开关后不许再调转写模型')
        self.assertEqual(context.calls[0]['audio_urls'], ['data:audio/mp3;base64,QUJD'],
                         '语音按音频证据原样交给主模型')
        self.assertNotIn('不该被调到的转写结果', context.calls[0]['prompt'])

    def test_the_stt_switch_is_missing_key_tolerant(self):
        """老配置里没有 `stt_enabled` 这个键 → 当作**开着**（历史行为，别偷偷关掉转写）。"""
        bridge = bridge_module.AstrbotBridge.__new__(bridge_module.AstrbotBridge)
        bridge.config = {'model_center': {'audio': {}}}
        self.assertTrue(bridge.audio_transcription_enabled())
        bridge.config = {'model_center': {'audio': {'stt_enabled': False}}}
        self.assertFalse(bridge.audio_transcription_enabled())
        bridge.config = {'model_center': {'audio': {'stt_enabled': True}}}
        self.assertTrue(bridge.audio_transcription_enabled())

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


class ApplyConfigRecomputesRoutingTests(unittest.TestCase):
    """改完配置要**重算路由**并按需重建 provider（v1.9.4 §61）。

    真机症状：在控制台加 / 改完模型连接，模型中心那一页与真实路由还是启动时那份 ——
    `apply_config` / `reload_config` 只换了 `service.config`，`service.model_routing`
    与 narrator / compactor 一个都没动。
    """

    @staticmethod
    def _connection(label: str = '直连A') -> dict[str, Any]:
        return {
            'label': label, 'enabled': True, 'model': 'demo',
            'endpoint': 'https://gw.example.com/v1/chat/completions',
            'use_for_main': True,
        }

    def test_applying_a_config_recomputes_the_route_and_its_candidates(self):
        """① 改配置 → 路由表**真的变了**（断言新的候选与判定），provider 跟着重建。"""
        bridge = _make_bridge({'model_center': {'providers': []}})
        service = bridge.service
        self.assertFalse(service.model_routing['main']['available'])
        self.assertEqual(service.model_routing['main']['reason'], 'unavailable')
        self.assertEqual(service.model_routing['main']['providers'], [])
        before = service.narrator

        bridge.apply_config({'model_center': {'providers': [self._connection()]}})

        route = service.model_routing['main']
        self.assertTrue(route['available'], route)
        self.assertEqual(route['reason'], 'assigned-provider', '勾了 use_for_main 的连接行')
        self.assertEqual([row['label'] for row in route['providers']], ['直连A'])
        from plugin.core.narrator import SilentNarrator  # noqa: PLC0415

        self.assertIsInstance(before, SilentNarrator, '改造前：没有可用路由')
        self.assertIsNot(service.narrator, before, 'provider 必须跟着路由重建')
        self.assertNotIsInstance(service.narrator, SilentNarrator, '改造后：真实客户端')

    def test_the_provider_a_turn_already_started_is_never_yanked_away(self):
        """② 进行中的那一跳不受影响：它手里那份实例带着**发起时**的路由。

        重建是同步、原子的（`refresh_model_routing` 里没有 `await`），旧实例由发起那次
        调用的强引用兜住，既不会被清空也不会被改写 —— 这一支刻意选"进行中不受影响"
        （另一支"明确失败并可见"见下面那条 warn 用例），文档 §61 写明。
        """
        bridge = _make_bridge({'model_center': {'providers': [self._connection()]}})
        service = bridge.service
        in_flight = service.narrator
        self.assertEqual(in_flight.routing['main']['reason'], 'assigned-provider')

        bridge.apply_config({'model_center': {'providers': []}})

        self.assertEqual(service.model_routing['main']['reason'], 'unavailable', '新配置已生效')
        self.assertEqual(
            in_flight.routing['main']['reason'], 'assigned-provider',
            '进行中那一跳的实例没被抽走 / 改写',
        )
        self.assertEqual(
            [row['label'] for row in in_flight.routing['main']['providers']], ['直连A'],
        )

    def test_repeated_apply_is_idempotent(self):
        """③ 重复 apply 幂等：路由不变、对象不换、合成行不堆叠。"""
        config = {'model_center': {'main_provider_id': 'ollama', 'providers': []}}
        bridge = _make_bridge(config)
        service = bridge.service
        narrator = service.narrator
        route = service.model_routing['main']

        bridge.apply_config(config)
        self.assertIs(service.narrator, narrator, '第一遍就不该动（配置没变）')
        bridge.apply_config(config)
        self.assertIs(service.narrator, narrator, '第二遍同样不动任何对象')
        self.assertEqual(service.model_routing['main'], route)
        self.assertEqual(len(bridge.routing_config()['model']['providers']), 1, '合成行不重复堆叠')

    def test_a_failed_refresh_is_visible_and_never_pretends_it_worked(self):
        """④ 重算失败 → 一条可见 warn + 旧路由继续用（不许假装已生效）。"""
        bridge = _make_bridge({'model_center': {'providers': []}})
        old_route = bridge.service.model_routing['main']

        def boom() -> bool:
            raise RuntimeError('boom')

        bridge.service.refresh_model_routing = boom  # type: ignore[method-assign]
        with mock.patch.object(bridge_module, 'log_fallback') as fallback:
            bridge.apply_config({'model_center': {'providers': [self._connection()]}})

        self.assertTrue(fallback.called, '失败必须留痕')
        self.assertEqual(fallback.call_args[0][0], 'warn')
        self.assertIn('模型路由重算失败', fallback.call_args[0][1])
        self.assertEqual(
            bridge.service.model_routing['main'], old_route,
            '旧路由继续用（不是假装新配置生效）',
        )


class _FakeTokenUsage:
    """AstrBot 4.28 `TokenUsage` 的最小桩：字段名逐字（`input_other` / `input_cached` / `output`）。

    真身：`astrbot/core/provider/entities.py` 的 `TokenUsage`；`LLMResponse.usage` 装的就是它。
    夹具要用**宿主真名**——这里换成 OpenAI 的名字，测出来的就不是真机行为了。
    """

    def __init__(self, input_other=0, input_cached=0, output=0):
        self.input_other = input_other
        self.input_cached = input_cached
        self.output = output

    @property
    def total(self):
        return self.input_other + self.input_cached + self.output

    @property
    def input(self):
        return self.input_other + self.input_cached


class _RecordingFakeContext(FakeContext):
    """真桥 + 真 `AstrbotHttpClient` 用的桩宿主：记下 `llm_generate` 收到的 Provider。"""

    def __init__(self, provider_id='session-default', usage=None, completion_text='写手草稿'):
        super().__init__()
        self.provider_id = provider_id
        self.usage = usage if usage is not None else {}
        self.completion_text = completion_text
        self.calls: list[dict] = []

    def get_provider_by_id(self, provider_id):
        return _FakeProvider(provider_id, ['text'])

    async def get_current_chat_provider_id(self, umo):  # noqa: ARG002
        return self.provider_id

    async def llm_generate(self, **kwargs):
        self.calls.append(kwargs)
        completion_text = self.completion_text
        usage = self.usage

        class _Response:
            pass

        response = _Response()
        response.completion_text = completion_text
        response.usage = usage
        return response


class _ProviderStub:
    """宿主 Provider 的最小桩：只有 `meta().id`（`get_all_providers()` 那条读法用）。"""

    def __init__(self, provider_id):
        self._id = provider_id

    def meta(self):
        return type('Meta', (), {'id': self._id})()


class _RecordingFallback:
    """记下"直连 endpoint"那条路收到的 URL（测试环境里没有 httpx）。"""

    def __init__(self):
        self.urls: list[str] = []

    async def post_json(self, url, headers=None, body=None, timeout=None):  # noqa: ARG002
        self.urls.append(url)
        return {'choices': [{'message': {'content': '直连回复'}}]}


class WorksWriterBindingTests(unittest.TestCase):
    """共同作品的写手模型（v1.7.9）：配置页的选择器 ↔ `works.model_id` 的双读接线。

    这个键有两套填法——老口径"点名模型中心里的一条连接行"（core 按 `id` / 模型名 /
    标签匹配）与新口径"指名一个 AstrBot Provider"（配置页的选择器写的就是它）。
    适配层必须：① 为指名合成一条挂了 `use_for_works` 的连接行（否则 core 只认
    `endpoint`，指派了也走不到传输层——坑 26）；② 老口径的值**一个都不许失效**。
    """

    def _named_bridge(self, works_model_id, providers=()):
        bridge = _make_bridge({
            'works': {'enabled': True, 'generation_mode': 'separate', 'model_id': works_model_id},
            'model_center': {'providers': [dict(row) for row in providers]},
        })
        # 桩宿主没有 Provider 管理器：默认"模型还没装上"（坑 23 的真实启动早期）。
        bridge.any_provider_loaded = lambda: False  # type: ignore[method-assign]
        return bridge

    @staticmethod
    def _row(**overrides):
        row = {
            'id': 'writer-conn', 'label': '写手连接', 'enabled': True, 'model': 'writer-model',
            'endpoint': 'https://example.invalid/v1/chat/completions',
        }
        row.update(overrides)
        return row

    @staticmethod
    def _loaded(bridge, *ids):
        """把"宿主已经装好这些聊天 Provider"桩进去（走 `get_all_providers` 那条读法）。"""
        bridge.loaded_chat_provider_ids = lambda: set(ids)  # type: ignore[method-assign]
        return bridge

    def test_the_task_table_points_at_the_works_section(self):
        bridge = self._named_bridge('ollama')
        self.assertEqual(bridge.TASK_MODEL_PATHS['works'], ('works', 'model_id'))
        self.assertEqual(bridge.task_model_id('works'), 'ollama')

    def test_a_named_provider_becomes_a_works_row(self):
        bridge = self._loaded(self._named_bridge('ollama', [self._row()]), 'ollama')
        self.assertEqual(bridge.works_writer_named_provider(), 'ollama')
        rows = bridge.routing_config()['model']['providers']
        works = [row for row in rows if row['id'] == '%sworks' % bridge_module.ROUTING_ROW_PREFIX]
        self.assertEqual(len(works), 1, rows)
        self.assertTrue(works[0]['use_for_works'])
        self.assertFalse(works[0]['use_for_main'], '写手的指名不能变成主叙事的指派')
        self.assertEqual(works[0]['transport_target'], 'astrbot:ollama')
        # 用户那份配置不能被污染（导出 / 落盘读的都是它）
        clean = bridge.section('model')['providers']
        self.assertFalse(any(bridge_module.is_routing_row(item) for item in clean))

    def test_a_works_row_never_becomes_the_narrative_fallback(self):
        """给写手选个模型不能把主叙事也换掉（合成行排在候选最前，必须被隔离）。"""
        from plugin.core.model_routing import resolve_model_routing

        bridge = self._loaded(self._named_bridge('ollama', [self._row()]), 'ollama')
        routing = resolve_model_routing(bridge.routing_config()['model'])
        self.assertEqual([item['id'] for item in routing['main']['providers']], ['writer-conn'])
        self.assertEqual(routing['main']['reason'], 'legacy-fallback')

    def test_a_value_naming_a_connection_row_keeps_the_old_meaning(self):
        """老口径：值点到一条可用连接行 → 不合成指名行，core 照旧按连接行解析。"""
        bridge = self._named_bridge('writer-conn', [self._row()])
        self.assertEqual(bridge.works_writer_named_provider(), '')
        rows = bridge.routing_config()['model']['providers']
        self.assertFalse(any(row.get('use_for_works') for row in rows), rows)
        self.assertIs(bridge.routing_config(), bridge.config, '没有别的指名时零开销原样返回')

    def test_a_connection_row_can_also_be_named_by_model_or_label(self):
        row = self._row(id='x')
        for value in ('writer-model', '写手连接'):
            bridge = self._named_bridge(value, [row])
            self.assertEqual(bridge.works_writer_named_provider(), '', value)

    def test_models_not_loaded_yet_still_bind_the_named_provider(self):
        """坑 23：AstrBot 插件先、模型后。那时按新口径处理，否则用户选的模型要等到
        下次保存配置才生效。"""
        bridge = self._named_bridge('ollama', [self._row()])
        self.assertFalse(bridge.any_provider_loaded())
        self.assertEqual(bridge.works_writer_named_provider(), 'ollama')

    def test_a_missing_provider_with_models_loaded_keeps_the_legacy_failure(self):
        """已就绪却没有这个 id、也不是连接行 → 老口径（保留原来那句报错）。"""
        bridge = self._named_bridge('nope', [self._row()])
        bridge.any_provider_loaded = lambda: True  # type: ignore[method-assign]
        bridge.provider_by_id = lambda pid: None  # type: ignore[method-assign]
        self.assertEqual(bridge.works_writer_named_provider(), '')
        rows = bridge.routing_config()['model']['providers']
        self.assertFalse(any(bridge_module.is_routing_row(row) for row in rows),
                         '不合成任何行（老口径的「指名不存在」由 core 报）')
        self.assertEqual(bridge.task_model_id('works'), 'nope', '值本身不动，core 才报得出名字')

    def test_a_loaded_provider_wins_over_a_same_named_connection_row(self):
        """值同时是连接行名与已加载 Provider id → 按 Provider 解释（"指名 → 生效"）。"""
        bridge = self._loaded(self._named_bridge('ollama', [self._row(id='ollama', model='ollama-local')]), 'ollama')
        self.assertEqual(bridge.works_writer_named_provider(), 'ollama')
        rows = bridge.routing_config()['model']['providers']
        self.assertTrue(any(row.get('use_for_works') for row in rows), rows)

    def test_the_provider_lookup_never_pokes_the_host_with_a_legacy_value(self):
        """坑 23：`get_provider_by_id()` 对不存在的 id 会打一条误导性的宿主警告。

        老口径的值（连接行名）每保存一次配置判一次，所以装了模型时判断"是不是
        Provider"必须走 `get_all_providers()`——`provider_by_id` 一次都不该被调到。
        """
        bridge = self._named_bridge('writer-conn', [self._row()])
        calls: list[str] = []
        bridge.provider_by_id = lambda pid: calls.append(pid) or None  # type: ignore[method-assign]
        bridge.any_provider_loaded = lambda: True  # type: ignore[method-assign]
        bridge.context.get_all_providers = lambda: [_ProviderStub('ollama')]
        self.assertEqual(bridge.works_writer_named_provider(), '')
        self.assertEqual(calls, [])

    def test_loaded_chat_provider_ids_reads_the_host_list(self):
        bridge = self._named_bridge('ollama')
        self.assertEqual(bridge.loaded_chat_provider_ids(), set(), '桩宿主没有列表 → 空集合')
        bridge.context.get_all_providers = lambda: [_ProviderStub('ollama'), _ProviderStub(''), 'not-a-provider']
        self.assertEqual(bridge.loaded_chat_provider_ids(), {'ollama'})

        def _boom():
            raise RuntimeError('host exploded')

        bridge.context.get_all_providers = _boom
        self.assertEqual(bridge.loaded_chat_provider_ids(), set(), '拿不到列表不能抛')

    def test_the_transport_lands_the_works_task_on_the_named_provider(self):
        """端到端的那一跳：`task='works'` → `chat_provider_id` = `works.model_id`。

        `SIDE_TASK_ROUTES['作品创作'] = 'works'`（见 `test_works_wiring`）负责把任务键
        传下来，这里负责证明它真的落到那个 Provider；留空时仍走会话默认模型。
        """
        context = _RecordingFakeContext()
        bridge = _make_bridge({
            'works': {'enabled': True, 'generation_mode': 'separate', 'model_id': 'ollama'},
            'model_center': {'providers': []},
        }, context=context)
        body = {'model': 'ollama', 'messages': [{'role': 'user', 'content': '写点什么'}]}
        response = asyncio.run(bridge_module.AstrbotHttpClient(bridge).post_json('', None, body, None, task='works'))
        self.assertEqual(context.calls[0]['chat_provider_id'], 'ollama')
        self.assertEqual(response['model'], 'ollama')

        plain_context = _RecordingFakeContext('session-default')
        plain = _make_bridge({'model_center': {'providers': []}}, context=plain_context)
        fallback = _RecordingFallback()
        asyncio.run(bridge_module.AstrbotHttpClient(plain, fallback=fallback).post_json(
            'https://gw.example.invalid/v1/chat/completions', None, body, None, task='works'))
        self.assertEqual(fallback.urls, ['https://gw.example.invalid/v1/chat/completions'],
                         '留空 = 直连连接行（与历史行为一致，不改道宿主 Provider）')
        self.assertEqual(plain_context.calls, [])

    def test_an_empty_value_changes_nothing(self):
        bridge = _make_bridge({'works': {'enabled': True}, 'model_center': {'providers': []}})
        self.assertEqual(bridge.task_model_id('works'), '')
        self.assertEqual(bridge.works_writer_named_provider(), '')
        self.assertIs(bridge.routing_config(), bridge.config)

    def test_the_startup_log_says_works_only_when_it_is_a_provider(self):
        bridge = self._loaded(self._named_bridge('ollama', [self._row()]), 'ollama')
        with mock.patch.object(bridge_module, 'log_fallback') as logged:
            asyncio.run(bridge.log_model_capabilities())
        messages = [item.args[1] % tuple(item.args[2:]) for item in logged.call_args_list if item.args]
        self.assertTrue(any('works → AstrBot Provider ollama' in item for item in messages), messages)
        # 老口径的值不能被报成"某个 AstrBot Provider"（那会让用户去宿主里找一个不存在的东西）
        legacy = self._named_bridge('writer-conn', [self._row()])
        with mock.patch.object(bridge_module, 'log_fallback') as logged:
            asyncio.run(legacy.log_model_capabilities())
        messages = [item.args[1] % tuple(item.args[2:]) for item in logged.call_args_list if item.args]
        self.assertFalse(any('works' in item for item in messages), messages)


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
        bridge = self._bridge_with('main-provider', ['text'], {
            'audio': {'enabled': True, 'provider_id': 'stt'},
        })
        self.assertEqual(bridge.audio_capability_note(), '')

    def test_master_switch_reads_the_nested_section_and_is_off_by_default(self):
        """`model_center.audio.enabled`：缺键 = 关（与 core `load_native_audio` 同一把闸）。"""
        bridge = self._bridge_with('main-provider', ['text', 'audio'], {'audio': {}})
        self.assertFalse(bridge.audio_understanding_enabled())
        bridge = self._bridge_with('main-provider', ['text', 'audio'],
                                   {'audio': {'enabled': True}})
        self.assertTrue(bridge.audio_understanding_enabled())
        # 顶层 `audio` 是旧版本留下的错误段位，不算数（坑 34）。
        bridge = _make_bridge({'audio': {'enabled': True}})
        self.assertFalse(bridge.audio_understanding_enabled())

    def test_master_off_with_a_named_transcriber_says_the_truth(self):
        """（关音频 / 开转写）：配了转写模型但总开关关着 —— **不能**报"没问题"。

        这是 v1.7.5 留下的那句假话：总开关关着时 core 一条音频都不加载，配好的转写
        模型一次都不会被调用，而旧口径返回空串（= 一切正常）。
        """
        bridge = self._bridge_with('main-provider', ['text'], {
            'audio': {'enabled': False, 'stt_enabled': True, 'provider_id': 'whisper-local'},
        })
        note = bridge.audio_capability_note()
        self.assertIn('启用语音原生理解', note)
        self.assertIn('whisper-local', note)
        self.assertIn('不会被调用', note)
        self.assertNotIn('已指定', note)

    def test_master_off_stays_quiet_when_nobody_configured_voice(self):
        """默认配置（总开关关、没配转写）不刷警告：语音理解本来就是 opt-in。"""
        bridge = self._bridge_with('main-provider', ['text'], {'audio': {'enabled': False}})
        self.assertEqual(bridge.audio_capability_note(), '')

    def test_the_master_switch_is_reported_even_when_the_main_model_has_no_audio(self):
        """总开关关着 + 主模型也没有音频能力：说清"先开总开关"，别只说换模型/配转写。"""
        bridge = self._bridge_with('main-provider', ['text'], {
            'audio': {'enabled': False, 'provider_id': 'stt'},
        })
        self.assertIn('启用语音原生理解', bridge.audio_capability_note())

    def test_the_stt_off_main_model_gap_is_still_explained_when_the_master_is_on(self):
        """（开音频 / 关转写）：沿用 v1.7.5 的解释（说清是开关，别让用户去换模型）。"""
        bridge = self._bridge_with('main-provider', ['text'], {
            'audio': {'enabled': True, 'stt_enabled': False, 'provider_id': 'stt'},
        })
        note = bridge.audio_capability_note()
        self.assertIn('语音转文字', note)
        self.assertIn('是关的', note)

    def test_master_on_and_stt_off_with_an_audio_capable_model_is_fine(self):
        """（开音频 / 关转写）+ 主模型声明了音频：语音按音频证据进主模型，没有提示。"""
        bridge = self._bridge_with('main-provider', ['text', 'audio'], {
            'audio': {'enabled': True, 'stt_enabled': False, 'provider_id': 'stt'},
        })
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

def _aiocqhttp_available() -> bool:
    """宿主库在不在当前解释器里（CI 的纯工作区环境没有它）。"""
    try:
        import aiocqhttp  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True

# --------------------------------------------------------------------------- #
# OneBot 动作回执的**真实形状**（aiocqhttp 剥过信封）
# --------------------------------------------------------------------------- #


class OnebotFrameShapeTests(unittest.TestCase):
    """`_call_onebot_on` 必须认**宿主真正回的那只形状**。

    真机现场（用户贴的日志）：

        [WARN] OneBot 动作执行失败：get_cookies 失败：平台没有回执（dict）

    宿主的 OneBot 客户端就是 `aiocqhttp.CQHttp`（`AiocqhttpAdapter` 把它挂在平台实例的
    `bot` 上），而 `call_action` **不回整只信封**：`aiocqhttp/api_impl.py:28-39` 的
    `_handle_api_result` 在 `status == 'failed'` 时抛 `ActionFailed`，成功只回
    `result['data']`。`get_cookies` 的成功回执因此是 `{'cookies': …, 'bkn': …}`
    ——一个**没有 status / retcode 的 dict**，早先被判成"平台没有回执"，
    于是整条 QZone CGI 通道（评论 / 点赞 / 转发 / 看好友动态）跟着废掉。
    """

    def _transport(self, bot):
        """装好平台实例**并登记一次会话坐标**——`call_onebot` 是按坐标取客户端的。"""
        bridge = _bridge_with_bot({}, bot)
        event = FakeMessageEvent(components=[Plain('hi')])
        endpoint = bridge_module.endpoint_for_event(event)
        bridge.remember_event(event, session_view(event, endpoint), endpoint)
        return bridge.transport

    def _call(self, bot, action='get_cookies', params=None):
        return asyncio.run(self._transport(bot).call_onebot(action, dict(params or {})))

    def test_unwrapped_data_is_a_success_not_a_missing_receipt(self):
        """`{'cookies': …, 'bkn': …}`（剥壳后的 `data`）= **成功**，且值原样带出。"""
        bot = _FakeOneBotClient(actions={'get_cookies': {
            'cookies': 'uin=o010001; p_skey=pin1n2x3', 'bkn': '1869525896',
        }})
        result = self._call(bot, params={'domain': 'user.qzone.qq.com'})
        self.assertIs(result['ok'], True, result)
        self.assertEqual(result['error'], '')
        self.assertEqual(result['data'], {
            'cookies': 'uin=o010001; p_skey=pin1n2x3', 'bkn': '1869525896',
        })
        # 参数逐字一致：`domain` 是 NapCat `/get_cookies` 的**必填**参数
        # （权威文档 https://napcat.apifox.cn/226657041e0.md 的 requestBody.required）。
        self.assertEqual(bot.calls, [('get_cookies', {'domain': 'user.qzone.qq.com'})])

    def test_get_credentials_has_the_same_unwrapped_shape(self):
        """取凭据的**另一个**接口（`get_credentials`）也是剥壳 data：判据只有一份。

        `/get_credentials`（napcat.apifox.cn/226657054e0）回 `data.cookies` + `data.token`，
        `/get_cookies`（226657041e0）回 `data.cookies` + `data.bkn` —— 参数与 Cookie 字段
        都一样，所以桥接层与 `core/qzone` 都不按接口名分支。
        """
        bot = _FakeOneBotClient(actions={'get_credentials': {
            'cookies': 'uin=o010001; p_skey=pin1n2x3', 'token': 1869525896,
        }})
        result = self._call(bot, action='get_credentials', params={'domain': 'qzone.qq.com'})
        self.assertIs(result['ok'], True, result)
        self.assertEqual(result['data']['cookies'], 'uin=o010001; p_skey=pin1n2x3')
        self.assertEqual(bot.calls, [('get_credentials', {'domain': 'qzone.qq.com'})])

    def test_send_qzone_msg_tid_survives_the_unwrapping(self):
        """`/send_qzone_msg` 的成功回执同样是剥壳后的 `data`（`{'tid': …}`）。"""
        bot = _FakeOneBotClient(actions={'send_qzone_msg': {'tid': 'TID-0001'}})
        result = self._call(bot, action='send_qzone_msg')
        self.assertIs(result['ok'], True, result)
        self.assertEqual(result['data'], {'tid': 'TID-0001'})

    def test_none_and_scalars_are_still_reported_as_no_receipt(self):
        """非 dict（`None` / bool / 字符串）= 平台什么都没给：照旧报"没有回执"。

        这几种形状**不能**当成功——`data: null` 与"客户端根本没说"从这里分不开，
        宁可让调用方看见一句明确的失败（`get_cookies` 拿不到 cookie 会自己再失败一次）。
        """
        for value, shown in ((None, 'NoneType'), (True, 'bool'), ('oops', 'str'), (17, 'int')):
            with self.subTest(value=value):
                bot = _FakeOneBotClient(actions={'get_cookies': value})
                result = self._call(bot)
                self.assertIs(result['ok'], False, value)
                self.assertIn('平台没有回执（%s）' % shown, result['error'])
                self.assertNotIn('ambiguous', result)

    def test_a_platform_failure_frame_is_definite_not_ambiguous(self):
        """`ActionFailed`（`status:failed` + retcode）是**明确答复**：不许标 ambiguous。

        真机日志里那句 `不支持的Api get_qzone_feeds` 被写成
        "传输异常（结果未知，请勿自动重试）"，与 `[自动重试]` 标签自相矛盾——
        根因就是这里把平台的明确拒绝并进了"可能没送到"那一支。
        """
        bot = _FakeOneBotClient(actions={'get_qzone_feeds': {
            'status': 'failed', 'retcode': 1404, 'message': '不支持的Api get_qzone_feeds',
        }})
        with mock.patch.object(bridge_module, 'log_fallback') as logged:
            result = self._call(bot, action='get_qzone_feeds')
        self.assertIs(result['ok'], False)
        self.assertEqual(result['retcode'], 1404)
        self.assertIn('retcode=1404', result['error'])
        self.assertIn('不支持的Api get_qzone_feeds', result['error'])
        self.assertNotIn('ambiguous', result, '明确答复不是"结果未知"')
        self.assertNotIn('结果未知', result['error'])
        levels = [item.args[0] for item in logged.call_args_list if item.args]
        self.assertIn('warn', levels, '平台明确拒绝也要留一条可见 warn')

    def test_a_transport_exception_stays_ambiguous(self):
        """真·传输异常（超时 / 断连）仍然 `ambiguous`：调用方不得自动重试。"""
        bot = _FakeOneBotClient(actions={'comment_qzone': asyncio.TimeoutError('timed out')})
        result = self._call(bot, action='comment_qzone')
        self.assertIs(result['ok'], False)
        self.assertIs(result['ambiguous'], True)
        self.assertIn('结果未知，请勿自动重试', result['error'])

    def test_the_debug_shape_line_never_prints_cookie_values(self):
        """debug 的形状行只报类型与键名——**cookie 值绝不进日志**。"""
        secret = 'p_skey=THIS-MUST-NOT-APPEAR-IN-LOGS'
        bot = _FakeOneBotClient(actions={'get_cookies': {'cookies': secret, 'bkn': '1869525896'}})
        with mock.patch.object(bridge_module, 'log_fallback') as logged:
            self._call(bot)
        rendered = ' '.join(str(item) for item in logged.call_args_list)
        self.assertNotIn('THIS-MUST-NOT-APPEAR-IN-LOGS', rendered)
        self.assertIn('cookies=<已隐去>', rendered)
        self.assertIn('bkn=<已隐去>', rendered)
        # 形状本身要看得到（真机排障就靠它分辨"整只信封"与"剥了壳的 data"）。
        # `log_fallback` 在这一路被 mock 掉了，拿到的是**模板 + 参数**，所以分开断。
        self.assertIn('回执形状', rendered)
        self.assertIn('dict{cookies=<已隐去>, bkn=<已隐去>}', rendered)

    def test_an_envelope_from_another_client_is_still_understood(self):
        """另一种 OneBot 客户端实现（回整只信封）也要能跑：两种形状都认。"""
        bot = _FakeOneBotClient(actions={'get_cookies': {
            'status': 'ok', 'retcode': 0, 'data': {'cookies': 'uin=o1; p_skey=x'},
        }}, envelope=True)
        result = self._call(bot)
        self.assertIs(result['ok'], True, result)
        self.assertEqual(result['data'], {'cookies': 'uin=o1; p_skey=x'})

    @unittest.skipUnless(_aiocqhttp_available(), '装了 aiocqhttp 才核对宿主的真实契约')
    def test_the_host_library_contract_is_what_we_modelled(self):
        """**契约哨兵**：直接读宿主库里那段代码，确认桩与实现说的是同一件事。

        `_handle_api_result` 必须是"失败抛 `ActionFailed`、成功只回 `data`"。
        宿主哪天换了这套契约，这条用例先红，而不是等真机日志再来一次。
        """
        import inspect
        import aiocqhttp.api_impl as api_impl

        source = inspect.getsource(api_impl._handle_api_result)
        self.assertIn("result['status'] == 'failed'", source)
        self.assertIn('raise ActionFailed(result=result)', source)
        self.assertIn("return result.get('data')", source)

# --------------------------------------------------------------------------- #
# 与 NapCat 的 API 清单对账：**不许把不存在的动作名发给平台**
# --------------------------------------------------------------------------- #


#: 上游 Koishi 的 QQ 空间适配器才有的扩展动作名 —— AstrBot 世界的**任何**后端都没有。
#:
#: 权威依据：NapCat 的整份 API 清单（https://napcat.apifox.cn/llms.txt）里只有两条
#: QQ 空间动作：`496813058e0`「发表QQ空间说说」`/send_qzone_msg` 与 `496813059e0`
#: 「删除QQ空间说说」`/delete_qzone_msg`。真机上把这几个名字发给 NapCat 的回报就是
#: `retcode 1404 不支持的Api get_qzone_feeds`。
MISSING_PLATFORM_ACTIONS = (
    'get_qzone_feeds', 'get_qzone_msg_list', 'comment_qzone', 'like_qzone', 'forward_qzone',
)


class MissingPlatformActionTests(unittest.TestCase):
    """目录动作 → 平台动作名这张表里，**一个不存在的名字都不许有**。

    这几个名字在 v1.7.1–v1.7.9 是"回退通道"用的出口；NapCat 上没有它们，
    发出去只会失败。现在它们只能作为**插件内部的 CGI 选择键**存在
    （`core/qzone.py::QZONE_CGI_BY_ID`），永远不许流到平台调用这一层。
    """

    def test_no_catalog_action_maps_to_a_name_no_backend_has(self):
        offenders = {
            action_id: name
            for action_id, (name, _mapping) in bridge_module._PLATFORM_CALLS.items()
            if name in MISSING_PLATFORM_ACTIONS
        }
        self.assertEqual(
            offenders, {},
            '这些动作名在平台的 API 清单里不存在：发过去只会换回 retcode 1404',
        )

    def test_the_cgi_only_qzone_actions_are_explicitly_unsupported(self):
        """只能走 QZone CGI 的那几条，在适配层必须显式 `@unsupported`（不是"同名"）。"""
        for action_id in (
            'comment_qzone_post', 'like_qzone_post', 'forward_qzone_post',
            'list_qzone_feeds', 'list_qzone_posts', 'set_qzone_visibility',
        ):
            with self.subTest(action_id=action_id):
                self.assertEqual(
                    bridge_module._PLATFORM_CALLS[action_id][0],
                    bridge_module._PLATFORM_ACTION_UNSUPPORTED,
                    '这几条没有平台出口（只有 CGI），标错了就会往平台打不存在的动作',
                )

    def test_the_two_native_qzone_actions_still_go_to_the_platform(self):
        """NapCat 真的有的那两条照旧直发平台（别把能用的也一并关掉）。"""
        self.assertEqual(bridge_module._PLATFORM_CALLS['publish_qzone_post'][0], 'send_qzone_msg')
        self.assertEqual(bridge_module._PLATFORM_CALLS['delete_qzone_post'][0], 'delete_qzone_msg')

    def test_the_qzone_internal_keys_never_reach_the_platform_layer(self):
        """源码哨兵：`_qzone_run_action` 的内部动作键只能是 `QZONE_CGI_BY_ID` 的键。

        它们出现在 core 里是**对的**（那是插件自己的 CGI 选择名）；出现在
        `_PLATFORM_CALLS` 的值里就是错的（上一条用例盯着）。这条防止有人
        "顺手把回退接回来"时只改一边。
        """
        from plugin.core import qzone as q

        self.assertEqual(set(q.QZONE_CGI_BY_ID), {
            'send_qzone_msg', 'delete_qzone_msg', 'comment_qzone', 'like_qzone',
            'forward_qzone', 'get_qzone_msg_list', 'get_qzone_feeds', 'set_qzone_visibility',
        })


# --------------------------------------------------------------------------- #
# 端到端：真机形状的客户端 → 桥接 → core 的空间读通道
# --------------------------------------------------------------------------- #


class QzoneEndToEndTests(unittest.TestCase):
    """把真机那条链整条跑通一遍：`aiocqhttp` 形状的客户端 → `AstrbotTransport` →
    `ServiceChunk13.qzone_read` → QZone CGI。

    真机上坏掉的正是这条链：`get_cookies` 的剥壳回执被判成"没有回执" →
    拿不到 cookie → 读通道整条不通（而每一段单独看都正常）。
    这里**不发任何真实请求**：OneBot 侧是桩，HTTP 侧换成返回一页动态文本的假函数。
    """

    def test_reading_feeds_works_end_to_end_with_the_hosts_real_reply_shape(self):
        from plugin.tests import test_qzone as qz

        bot = _FakeOneBotClient(actions={
            # 宿主（aiocqhttp）成功时只回 `data`：两个取凭据接口的真实形状各一份。
            'get_credentials': {'cookies': 'uin=o010001; p_skey=pin1n2x3', 'token': 1869525896},
            'get_cookies': {'cookies': 'uin=o010001; p_skey=pin1n2x3', 'bkn': '1869525896'},
            'get_login_info': {'user_id': 10001},
        })
        bridge = _bridge_with_bot({}, bot)
        event = FakeMessageEvent(components=[Plain('hi')])
        endpoint = bridge_module.endpoint_for_event(event)
        bridge.remember_event(event, session_view(event, endpoint), endpoint)

        http_calls: list[dict] = []

        async def fake_request_text(method, url, *, headers=None, data=None, timeout_ms=20000):
            http_calls.append({'method': method, 'url': url, 'data': dict(data or {})})
            return (
                "{ver:1,key:'K1',appid:311,uin:10002,nickname:'\u597d\u53cb',"
                "abstime:%d,html:'<div>\u4eca\u5929\u5929\u6c14\u5f88\u597d</div>',}"
                % int(qz.NOW.timestamp() - 1200)
            )

        bridge.request_text = fake_request_text  # type: ignore[method-assign]
        host = qz._Host(transport=bridge.transport)
        result = asyncio.run(host.qzone_read(qz.STORY, 'feed', {'count': 5}))

        self.assertIs(result['ok'], True, result)
        self.assertEqual([item['key'] for item in result['feeds']], ['K1'])
        # 平台侧只出现了取登录态那几条：主路 `get_credentials` 通了就不问备路，
        # **没有**任何空间动作被发出去。
        self.assertEqual(
            [name for name, _ in bot.calls], ['get_credentials', 'get_login_info'],
        )
        self.assertEqual(bot.calls[0][1], {'domain': 'user.qzone.qq.com'})
        # CGI 那一页真的被打了，且带上了由 p_skey 算出的 g_tk。
        self.assertEqual(len(http_calls), 1)
        self.assertIn('feeds3_html_more', http_calls[0]['url'])


class HostProviderUsageTests(unittest.TestCase):
    """经宿主 Provider 的调用：token 要读得到、次数要数得上（v1.9.4）。

    真机症状：模型全部来自 AstrBot Provider 时，「Token 统计」页连**调用次数**都是 0。
    两个根因各有一条守卫：① 只按 OpenAI 的字段名读 AstrBot 的 `TokenUsage`（读到 0）；
    ② 没有 usage 就整条不记（次数也丢）。
    """

    def _bridge(self, context, provider_id='ollama'):
        return _make_bridge(
            {'model_center': {
                'main_provider_id': provider_id,
                'compaction_provider_id': provider_id,
                'providers': [],
            }},
            context=context,
        )

    def _call(self, client, provider_id='ollama', task='main'):
        body = {'model': provider_id, 'messages': [{'role': 'user', 'content': '写点什么'}]}
        return asyncio.run(client.post_json('', None, body, None, task=task))

    def test_the_host_token_usage_is_mapped_to_the_openai_shape(self):
        """宿主的 `input_other` / `input_cached` / `output` → `prompt_tokens`（含缓存）/ 缓存明细。"""
        context = _RecordingFakeContext(usage=_FakeTokenUsage(input_other=10, input_cached=90, output=5))
        bridge = self._bridge(context)
        response = self._call(bridge_module.AstrbotHttpClient(bridge))
        # core 的 `parse_token_usage` 只认这一种形状：映射错了，账本三列就永远是 0。
        self.assertEqual(
            parse_token_usage(response['usage']),
            {'input_tokens': 100, 'output_tokens': 5, 'cached_input_tokens': 90},
        )

    def test_an_older_host_usage_shape_is_still_read(self):
        context = _RecordingFakeContext(usage={'prompt_tokens': 7, 'completion_tokens': 2})
        bridge = self._bridge(context)
        response = self._call(bridge_module.AstrbotHttpClient(bridge))
        self.assertEqual(
            parse_token_usage(response['usage']),
            {'input_tokens': 7, 'output_tokens': 2},
        )

    def test_a_provider_call_is_counted_even_when_no_usage_comes_back(self):
        """网关不回 usage：token 三列是 0，但这一次调用**必须在**。"""
        context = _RecordingFakeContext(usage={})
        bridge = self._bridge(context)
        response = self._call(bridge_module.AstrbotHttpClient(bridge))
        self.assertEqual(response['usage']['prompt_tokens'], 0)
        records = list(bridge.usage_records)
        self.assertEqual(len(records), 1, records)
        self.assertEqual(records[0]['task'], 'main')
        self.assertEqual(records[0]['target'], 'AstrBot · ollama')
        self.assertIs(records[0]['ok'], True)
        self.assertIn('ms', records[0])
        self.assertEqual(records[0]['total_tokens'], 0, '拿不到就留 0，页面按不可用显示')

    def test_two_calls_are_two_records_with_their_tokens(self):
        context = _RecordingFakeContext(usage=_FakeTokenUsage(input_other=3, input_cached=0, output=1))
        bridge = self._bridge(context)
        client = bridge_module.AstrbotHttpClient(bridge)
        self._call(client)
        self._call(client, task='compaction')
        records = list(bridge.usage_records)
        self.assertEqual(len(records), 2)
        self.assertEqual([item['task'] for item in records], ['main', 'compaction'])
        self.assertEqual([item['total_tokens'] for item in records], [4, 4])

    def test_the_direct_endpoint_still_records_its_tokens(self):
        """直连那条路（自带 usage）口径不变。"""

        class _Fallback:
            async def post_json(self, url, headers=None, body=None, timeout=None):  # noqa: ARG002
                return {
                    'model': 'demo', 'usage': {'prompt_tokens': 12, 'completion_tokens': 3,
                                               'total_tokens': 15},
                }

        bridge = _make_bridge({'model_center': {'providers': []}})
        body = {'model': 'demo', 'messages': [{'role': 'user', 'content': 'x'}]}
        response = asyncio.run(bridge_module.AstrbotHttpClient(bridge, fallback=_Fallback()).post_json(
            'https://gw.example.com/v1/chat/completions', None, body, None, task='main',
        ))
        self.assertEqual(parse_token_usage(response['usage'])['input_tokens'], 12)
        records = list(bridge.usage_records)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['target'], 'https://gw.example.com', 'query 里的密钥不进缓冲')
        self.assertEqual(records[0]['total_tokens'], 15)

    def test_a_failed_direct_call_is_recorded_as_failed(self):
        class _Broken:
            async def post_json(self, url, headers=None, body=None, timeout=None):  # noqa: ARG002
                raise RuntimeError('网关挂了')

        bridge = _make_bridge({'model_center': {'providers': []}})
        body = {'model': 'demo', 'messages': [{'role': 'user', 'content': 'x'}]}
        with self.assertRaises(RuntimeError):
            asyncio.run(bridge_module.AstrbotHttpClient(bridge, fallback=_Broken()).post_json(
                'https://gw.example.com/v1/chat/completions', None, body, None, task='main',
            ))
        records = list(bridge.usage_records)
        self.assertEqual(len(records), 1)
        self.assertIs(records[0]['ok'], False)


# =========================================================================== #
# 3.5 管理员名单（同步）——§86.4
# =========================================================================== #

class AdminRosterTests(unittest.TestCase):
    """`AstrbotTransport.known_super_admin_ids()`：动作权限表 `admin` 档的**同步**取值口。

    生产现场（`docs/PORTING_NOTES.md` §86.3/§86.4）：核心侧的权限判定是同步的
    （`chunk12.resolve_action_session_role()` 是 `def`，`chunk4` 的提示词组装也同步调它），
    而这份名单原来**没人喂** —— 生产上 `admin` 档恒假。这里钉住四件事：
    读得到 / 归一化从严 / 读不到**可见** / 空名单是"答过的没有"（不报警），
    并把"名单真的影响能调什么"接到 `chunk12` 的可用集上（不是只测方法返回值）。
    """

    ADMIN = '1000008890'
    OTHER = '10001'

    def setUp(self):
        self.context = FakeContext()
        self.bridge = _make_bridge(context=self.context)
        self.transport = AstrbotTransport(self.bridge)
        self.logs: list[tuple[str, str]] = []

        def sink(level, text):  # noqa: ARG001
            self.logs.append((level, text))

        # `_make_bridge` 的构造期会把 sink 换成桥自己的转发器 → 造完再装测试的。
        interlude_logging.set_log_sink(sink)
        self.addCleanup(interlude_logging.set_log_sink, interlude_logging._default_sink)

    def _host_config(self, value):
        self.context.get_config = lambda: value  # type: ignore[method-assign]

    def _warns(self) -> list[str]:
        return [text for level, text in self.logs if level == 'warn']

    # ---- 读得到 / 归一化 ----

    def test_the_roster_is_read_and_normalized(self):
        self._host_config({'admins_id': [self.ADMIN, 10001, None, '']})
        self.assertEqual(self.transport.known_super_admin_ids(), (self.ADMIN, '10001'))
        self.assertEqual(self._warns(), [], '读得到名单时不该有任何 warn')

    def test_a_string_roster_is_refused_not_split_into_single_characters(self):
        """`'12345'` 绝不能被当成五个单字符 id（否则可能碰巧放行一个单字符用户号）。"""
        self._host_config({'admins_id': self.ADMIN})
        self.assertEqual(self.transport.known_super_admin_ids(), ())
        self.assertTrue(any('admins_id' in text for text in self._warns()))

    def test_an_explicit_empty_roster_is_an_answer_and_stays_silent(self):
        """`admins_id: []` 是宿主**答过**的"没有超管"，不是"读不到"——不报警。"""
        self._host_config({'admins_id': []})
        self.assertEqual(self.transport.known_super_admin_ids(), ())
        self.assertEqual(self._warns(), [])

    # ---- 读不到：空名单 + 可见 warn（不静默、不猜） ----

    def test_a_missing_roster_gives_an_empty_list_and_one_visible_warning(self):
        self._host_config({})
        self.assertEqual(self.transport.known_super_admin_ids(), ())
        warns = self._warns()
        self.assertEqual(len(warns), 1)
        self.assertIn('admins_id', warns[0])
        self.assertIn('下一步', warns[0], 'warn 必须可行动（点名下一步）')
        self.assertNotIn('None', warns[0], '缺项不说"详情=None"（说给用户看的句子）')

    def test_an_unreadable_host_config_gives_an_empty_list_and_a_visible_warning(self):
        def explode():
            raise RuntimeError('config store is gone')

        self.context.get_config = explode  # type: ignore[method-assign]
        self.assertEqual(self.transport.known_super_admin_ids(), ())
        warns = self._warns()
        self.assertEqual(len(warns), 1)
        self.assertIn('admins_id', warns[0])

    def test_a_non_dict_host_config_is_refused_with_a_warning(self):
        self._host_config(['not', 'a', 'dict'])
        self.assertEqual(self.transport.known_super_admin_ids(), ())
        self.assertTrue(self._warns())

    def test_the_warning_is_emitted_once_per_reason(self):
        """这个口每个回合都被提示词组装调到 → 同类原因只报一次（不刷屏）。"""
        self._host_config({})
        for _ in range(5):
            self.assertEqual(self.transport.known_super_admin_ids(), ())
        self.assertEqual(len(self._warns()), 1)

    # ---- 名单真的影响"能调什么" ----

    def _chunk12_host(self):
        from plugin.tests.test_platform_dispatch import _Host

        host = _Host(config={}, transport=self.transport)
        host.action_permission_table = lambda: {'set_group_kick': 'admin'}
        return host

    def test_the_roster_actually_decides_the_admin_tier(self):
        """管理员 session → `admin`，且该档动作**真进可用集**；非管理员 → 空档。"""
        self._host_config({'admins_id': [self.ADMIN]})
        host = self._chunk12_host()
        self.assertEqual(host.resolve_action_session_role({'userId': self.ADMIN}), 'admin')
        self.assertIn('set_group_kick', host.available_platform_actions('admin', ('group',)))
        self.assertEqual(host.resolve_action_session_role({'userId': self.OTHER}), '')
        self.assertNotIn('set_group_kick', host.available_platform_actions('', ('group',)))

    def test_reverse_an_empty_roster_keeps_the_admin_tier_out(self):
        """反向：名单喂空 → `admin` 档断言必须红（这就是生产事故的形状）。"""
        self._host_config({'admins_id': []})
        host = self._chunk12_host()
        self.assertEqual(host.resolve_action_session_role({'userId': self.ADMIN}), '')
        with self.assertRaises(AssertionError):
            self.assertIn('set_group_kick', host.available_platform_actions('', ('group',)))

    def test_the_protocol_still_does_not_require_the_roster(self):
        """`known_super_admin_ids` 是协议**之外**的可选能力（core 侧 `getattr` 探）。

        加进 `Transport` 协议就会让"只实现了协议方法"的替身 / 其它适配器
        `isinstance(..., Transport)` 当场变假（`@runtime_checkable` 只查 `hasattr`），
        而它本来就允许缺席（缺席 = 没有超管，安全侧）。`dir()` 是这条的守卫：
        真加进协议时它会出现，这条用例立刻红。
        """
        from plugin.core.service import Transport

        self.assertIsInstance(self.transport, Transport)
        self.assertNotIn('known_super_admin_ids', dir(Transport))


if __name__ == '__main__':
    unittest.main()
