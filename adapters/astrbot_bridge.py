"""AstrBot 平台适配层：把 `AstrMessageEvent` / `Context` 翻译进 `plugin/core/`。

本模块是 `plugin/core/` 与 AstrBot 之间**唯一**的接缝（`docs/PORT_PLAN.md` §0.3）。
`core/` 里任何模块都不得 `import astrbot`；平台相关的一切都在这里落地。

对应关系
--------

| 上游（Koishi） | 本模块 |
| --- | --- |
| `Session` | `session_view()` → `plugin.core.service.session.SessionView` |
| `session.bot.sendPrivateMessage` / `sendGroupMessage` / `session.send` | `AstrbotTransport` |
| `ctx.logger` / `ctx.database` / `ctx.http` / `ctx.setTimeout` | `AstrbotInterludeContext`（内部用 `InterludeContext`） |
| `apply(ctx, config)` | `AstrbotBridge`（`main.py` 的 `Star` 子类持有它） |

键名法（`docs/PORT_PLAN.md` §2）
------------------------------

发给模型的 payload、数据库列名保持上游 camelCase（由 `core/` 负责）；
本模块只做两件事：把 AstrBot 的对象翻成 `core/` 要的 snake_case 视图，
以及把 `core/` 的 camelCase 出站内容翻成 AstrBot 消息链。
从外部读入的行（数据库行 / 参与者 dict）一律用 `pick()` 双读。

降级路径清单（`docs/PORT_PLAN_SERVICE.md` §8）
--------------------------------------------

AstrBot 没有对应能力的上游功能一律**返回失败/空值并记日志**，绝不抛异常：

1. `search_web` —— AstrBot 4.x 未向插件暴露通用网页搜索。若宿主 Context 上出现
   `web_search` / `search_web`（未来版本或其它插件注入）则调用它；否则记
   `debug` 日志并返回 `[]`。调用方按"没有网页观察"继续叙事。
2. `visit_web` —— 没有 Puppeteer。改用 `httpx` 直连 + 正则提取正文（标题、
   节选、可见文本），失败返回 `None`。不登录、不填表、不下载、不发布。
3. `react` —— 用 `AstrMessageEvent.react(emoji)`；平台不支持时返回 `False`。
4. `send_native_face` —— 用 `astrbot.api.message_components.Face`；非 OneBot
   平台发不出去时返回 `{'ok': False}`（上游同样只在 QQ 上生效）。
5. `fetch_member_name` —— 用 `event.get_group()`；取不到返回 `''`。
6. `iterate_sse` —— 本适配层不接管流式（`main_streaming_mode: experimental`
   时才走），转交 `HttpxHttpClient` 直连。
7. `send_sticker` —— 表情包就是图片文件，走 `Image`；失败返回 `{'ok': False}`。
8. 图片 / 语音下载 —— `httpx` 直连 `http(s)` 与 `data:` / `file:`；OneBot 的
   裸 file token 无法在适配层解析成字节，记日志后返回 `None`。
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Iterable, Optional
from urllib.parse import unquote, urlparse

from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context

try:  # 公开路径（skill-astrbot-dev `messages/components.md`）
    from astrbot.api.message_components import At, Face, File, Image, Plain, Record, Reply
except ImportError:  # pragma: no cover - 版本漂移时的实装路径
    from astrbot.core.message.components import At, Face, File, Image, Plain, Record, Reply

try:
    from astrbot.api.event import MessageChain
except ImportError:  # pragma: no cover
    from astrbot.core.message.message_event_result import MessageChain

from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from ..core import logging as interlude_logging
from ..core.database import Database
from ..core.narrator import HttpxHttpClient
from ..core.schedule_preplan import resolve_schedule_preplan_config, schedule_preplan_window
from ..core.service import (
    InterludeContext,
    InterludeService,
    SessionView,
    log_fallback,
    pick,
)
from ..core.time import format_log_time, format_story_display_time, iso as iso_time_value, utc_now

__all__ = [
    'AstrbotBridge',
    'AstrbotHttpClient',
    'AstrbotInterludeContext',
    'AstrbotTransport',
    'COMMAND_WORD_RE',
    'ONEBOT_ADAPTER_NAMES',
    'PLUGIN_NAME',
    'build_bridge',
    'looks_like_management_command',
    'plugin_data_dir',
    'serialize_message_chain',
    'session_view',
]

#: AstrBot 插件目录名（`data/plugin_data/<PLUGIN_NAME>/`、`metadata.yaml` 的 `name`）。
PLUGIN_NAME = 'astrbot_plugin_hds_interlude'

#: 上游 `looksLikeInterludeCommand` 的正则（`upstream/src/index.ts:915`）扩展了
#: 本移植版的命令前缀：上游 `interlude.*` 与 AstrBot 的 `hdsi_*` 都算管理命令。
COMMAND_WORD_RE = re.compile(r'^[!/.]?(?:interlude|hdsi)(?:[\s._-]|$)', re.IGNORECASE)

#: AstrBot 里属于 OneBot v11 家族的适配器名。
#: 上游 `session.platform === 'onebot'`（Koishi 的 OneBot 适配器）；
#: AstrBot 对应 `aiocqhttp` 适配器，另有第三方 `onebot` / `napcat` 适配器。
ONEBOT_ADAPTER_NAMES = frozenset({
    'aiocqhttp', 'onebot', 'onebot11', 'onebot_v11', 'onebot-v11', 'napcat', 'lagrange',
})

#: 语义表态名 → 平台可发送的 emoji（上游由平台连接器映射成 QQ 表情编号）。
REACTION_EMOJI = {
    'like': '👍',
    'smile': '😊',
    'laugh': '😂',
    'heart': '❤️',
    'surprised': '😮',
    'sad': '😢',
    'angry': '😠',
}

#: 表情包扫描允许的扩展名（上游 `listStickerFiles` 的 `/\.(?:png|jpe?g|webp|gif)$/i`）。
STICKER_EXTENSIONS = ('.png', '.jpg', '.jpeg', '.webp', '.gif')


# =========================================================================== #
# 数据目录
# =========================================================================== #

def plugin_data_dir(plugin_name: str = PLUGIN_NAME) -> str:
    """插件的私有数据目录：`<astrbot_data>/plugin_data/<plugin_name>/`。"""
    path = Path(get_astrbot_data_path()) / 'plugin_data' / plugin_name
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def database_path(data_dir: Optional[str] = None) -> str:
    """HDSI 自有 SQLite 路径（与上游一样是插件私有库，不碰 AstrBot 主库）。"""
    base = Path(data_dir) if data_dir else Path(plugin_data_dir())
    base.mkdir(parents=True, exist_ok=True)
    return str(base / 'hdsi.sqlite3')


# =========================================================================== #
# 平台判定
# =========================================================================== #

def _is_onebot_adapter(adapter_name: str, platform_id: str) -> bool:
    """AstrBot 适配器是否属于 OneBot v11 家族。"""
    for value in (adapter_name, platform_id):
        lowered = str(value or '').strip().lower()
        if not lowered:
            continue
        if lowered in ONEBOT_ADAPTER_NAMES:
            return True
        if lowered.startswith(('onebot', 'napcat')):
            return True
    return False


def resolve_platform_name(adapter_name: str, platform_id: str) -> str:
    """把 AstrBot 的适配器名翻成上游 `session.platform` 的语义。

    上游的 OneBot 白名单闸门（`canHandleSession` / `canHandleGroupSession`）只在
    `isOneBotPlatform(session.platform)` 为真时生效；AstrBot 里承载 OneBot v11 的
    是 `aiocqhttp` 适配器，因此这里统一报 `'onebot'`，否则 `onebot.*` 白名单会
    被静默绕过（安全语义必须与上游一致）。
    其它平台原样返回 AstrBot 的平台实例 id（`isOneBotPlatform` 会返回 False）。
    """
    if _is_onebot_adapter(adapter_name, platform_id):
        return 'onebot'
    return str(platform_id or adapter_name or 'unknown')


def _attr(obj: Any, *names: str) -> Any:
    """按顺序读取第一个存在且非 None 的属性（AstrBot 组件的字段名会随版本增删）。"""
    for name in names:
        if isinstance(obj, dict):
            if name in obj and obj[name] is not None:
                return obj[name]
            continue
        if hasattr(obj, name):
            value = getattr(obj, name)
            if value is not None:
                return value
    return None


def _text(value: Any) -> str:
    return '' if value is None else str(value)


def _escape_attr(value: Any) -> str:
    """Koishi 属性值的转义（`&quot;` 与 `&`），`helpers._parse_mini_xml_elements` 会反转义。"""
    return _text(value).replace('&', '&amp;').replace('"', '&quot;')


def _downloadable(value: Any) -> bool:
    source = _text(value).strip()
    return bool(re.match(r'^(?:https?://|data:|file://)', source, re.IGNORECASE))


def _component_kind(component: Any) -> str:
    """组件类型名（小写类名，跨 AstrBot 版本稳定，不依赖 `ComponentType` 枚举）。"""
    return type(component).__name__.lower()


# =========================================================================== #
# AstrBot 消息链 → Koishi `session.content` / `elements`
# =========================================================================== #

def serialize_component(component: Any) -> tuple[str, Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    """把一个 AstrBot 组件翻成 `(content 片段, element, quote)`。

    `element` 是 `h.parse(session.content)` 的等价结构（`type` / `attrs` / `children`），
    `quote` 只在 `Reply` 组件上产生（上游 `session.quote`）。
    返回的 `content` 片段与上游 Koishi 的序列化形式一致：文本原样，元素写成
    自闭合标签，`core/service/helpers.py` 的 `_parse_mini_xml_elements` 直接可读。
    """
    kind = _component_kind(component)

    if kind == 'plain' or kind == 'text':
        text = _text(_attr(component, 'text', 'content'))
        return text, {'type': 'text', 'attrs': {'content': text}, 'children': []}, None

    if kind in ('image', 'img'):
        url = _text(_attr(component, 'url'))
        file_value = _attr(component, 'file', 'file_')
        path = _attr(component, 'path')
        if _downloadable(url):
            attrs = {'src': url}
        elif file_value:
            attrs = {'file': _text(file_value), 'url': url}
        elif path:
            attrs = {'src': 'file://%s' % os.path.abspath(_text(path))}
        else:
            attrs = {}
        tag = ' '.join('%s="%s"' % (key, _escape_attr(value)) for key, value in attrs.items() if value != '')
        return ('<img %s/>' % tag) if tag else '<img/>', {'type': 'img', 'attrs': attrs, 'children': []}, None

    if kind in ('record', 'audio', 'voice'):
        file_value = _attr(component, 'file', 'file_')
        url = _text(_attr(component, 'url'))
        path = _attr(component, 'path')
        attrs: dict[str, Any] = {}
        if file_value:
            attrs['file'] = _text(file_value)
        if url:
            attrs['url'] = url
        if not attrs and path:
            attrs['src'] = 'file://%s' % os.path.abspath(_text(path))
        tag = ' '.join('%s="%s"' % (key, _escape_attr(value)) for key, value in attrs.items() if value != '')
        return ('<audio %s/>' % tag) if tag else '<audio/>', {'type': 'audio', 'attrs': attrs, 'children': []}, None

    if kind == 'video':
        url = _text(_attr(component, 'url'))
        file_value = _attr(component, 'file', 'file_')
        attrs = {'src': url} if _downloadable(url) else ({'file': _text(file_value)} if file_value else {})
        tag = ' '.join('%s="%s"' % (key, _escape_attr(value)) for key, value in attrs.items() if value != '')
        return ('<video %s/>' % tag) if tag else '<video/>', {'type': 'video', 'attrs': attrs, 'children': []}, None

    if kind in ('atatall', 'at'):
        target = _attr(component, 'qq', 'user_id', 'id')
        attrs = {'id': _text(target)}
        name = _attr(component, 'name')
        if name:
            attrs['name'] = _text(name)
        return '<at id="%s"/>' % _escape_attr(attrs['id']), {'type': 'at', 'attrs': attrs, 'children': []}, None

    if kind in ('face', 'mface'):
        face_id = _attr(component, 'id', 'face_id')
        attrs = {'id': _text(face_id)}
        return '<face id="%s"/>' % _escape_attr(attrs['id']), {'type': 'face', 'attrs': attrs, 'children': []}, None

    if kind == 'file':
        name = _text(_attr(component, 'name'))
        url = _text(_attr(component, 'url'))
        file_value = _text(_attr(component, 'file_', 'file'))
        attrs = {}
        if url:
            attrs['src'] = url
        elif file_value:
            attrs['src'] = file_value
        if name:
            attrs['name'] = name
        size = _attr(component, 'size')
        if size is not None:
            attrs['size'] = _text(size)
        tag = ' '.join('%s="%s"' % (key, _escape_attr(value)) for key, value in attrs.items() if value != '')
        return ('<file %s/>' % tag) if tag else '<file/>', {'type': 'file', 'attrs': attrs, 'children': []}, None

    if kind == 'reply':
        message_id = _text(_attr(component, 'id', 'message_id'))
        sender_id = _text(_attr(component, 'sender_id'))
        nickname = _text(_attr(component, 'sender_nickname'))
        chain = _attr(component, 'chain') or []
        message_str = _text(_attr(component, 'message_str'))
        # 上游 `session.quote` 是一个独立字段（不在 `session.content` 里），
        # 因此这里不产出 content 片段。
        quoted_content = message_str or ''.join(
            serialize_component(item)[0] for item in chain if item is not None
        )
        user: dict[str, Any] = {'id': sender_id}
        member: dict[str, Any] = {}
        if nickname:
            user['nick'] = nickname
            user['name'] = nickname
            member['nick'] = nickname
            member['name'] = nickname
        quote = {
            'id': message_id,
            'messageId': message_id,
            'user': user,
            'member': member,
            'content': quoted_content,
        }
        element = {'type': 'quote', 'attrs': {'id': message_id}, 'children': []}
        return '', element, quote

    # 其它组件（Poke / Forward / Node / Json / …）：保留一个最小标记，
    # 让上游的附件事实路径仍然能看到它，但绝不把标记当正文喂给模型。
    attrs = {}
    url = _attr(component, 'url')
    if _downloadable(url):
        attrs['src'] = _text(url)
    tag = ' '.join('%s="%s"' % (key, _escape_attr(value)) for key, value in attrs.items() if value != '')
    literal = '<%s %s/>' % (kind, tag) if tag else '<%s/>' % kind
    return literal, {'type': kind, 'attrs': attrs, 'children': []}, None


def serialize_message_chain(chain: Iterable[Any]) -> tuple[str, list[dict[str, Any]], Optional[dict[str, Any]]]:
    """把整条 AstrBot 消息链翻成 `(content, elements, quote)`。

    `content` 是上游 `session.content` 的等价物——`core/service/helpers.py` 会按
    Koishi 的 mini-xml 语法重新解析它（图片 / 语音 / 文件事实、`<at>` 提及检测），
    所以文本必须原样保留、元素必须写成自闭合标签。
    """
    parts: list[str] = []
    elements: list[dict[str, Any]] = []
    quote: Optional[dict[str, Any]] = None
    for component in chain or []:
        if component is None:
            continue
        fragment, element, quoted = serialize_component(component)
        if fragment:
            parts.append(fragment)
        if element is not None:
            elements.append(element)
        if quoted is not None:
            quote = quoted
    return ''.join(parts), elements, quote


# =========================================================================== #
# AstrMessageEvent → SessionView
# =========================================================================== #

def _call(event: Any, name: str, default: Any = None) -> Any:
    """调用事件 getter（AstrBot 只提供方法，没有同名属性——本项目的踩坑记录）。"""
    getter = getattr(event, name, None)
    if not callable(getter):
        return default
    try:
        return getter()
    except Exception:  # pragma: no cover - 适配器缺字段时保持弱读
        return default


@dataclass
class AstrbotEndpoint:
    """一次入站事件在 AstrBot 侧的投递坐标（UMO 与回退查找用）。"""

    platform: str
    platform_id: str
    platform_name: str
    self_id: str
    user_id: str
    group_id: str
    is_group: bool
    umo: str
    message_id: str
    session_id: str

    @property
    def scope(self) -> str:
        """会话作用域：群用群号，私聊用用户号。"""
        return self.group_id if self.is_group else self.user_id

    @property
    def message_type_value(self) -> str:
        return 'GroupMessage' if self.is_group else 'FriendMessage'

    def build_umo(self, scope: Optional[str] = None) -> str:
        """按 AstrBot 的 `platform_id:message_type:session_id` 组装 UMO。"""
        target = self.scope if scope is None else scope
        return '%s:%s:%s' % (self.platform_id, self.message_type_value, target)


def _is_private_event(event: Any) -> bool:
    """`session.isDirect` 的等价物。"""
    check = getattr(event, 'is_private_chat', None)
    if callable(check):
        try:
            return bool(check())
        except Exception:  # pragma: no cover
            pass
    message_type = _call(event, 'get_message_type')
    value = _text(getattr(message_type, 'value', message_type))
    if value:
        return value == 'FriendMessage'
    return not _text(_call(event, 'get_group_id', ''))


def endpoint_for_event(event: AstrMessageEvent) -> AstrbotEndpoint:
    """读出事件的投递坐标（`session_view` 与 `Transport` 共用）。"""
    platform_name = _text(_call(event, 'get_platform_name', ''))
    platform_id = _text(_call(event, 'get_platform_id', '')) or platform_name
    self_id = _text(_call(event, 'get_self_id', ''))
    user_id = _text(_call(event, 'get_sender_id', ''))
    is_group = not _is_private_event(event)
    group_id = _text(_call(event, 'get_group_id', '')) if is_group else ''
    session_id = _text(_call(event, 'get_session_id', '')) or (group_id or user_id)
    message_id = _text(_call(event, 'get_message_id', '')) or _message_id_from_event(event)
    umo = _text(getattr(event, 'unified_msg_origin', '')) or '%s:%s:%s' % (
        platform_id,
        'GroupMessage' if is_group else 'FriendMessage',
        session_id,
    )
    return AstrbotEndpoint(
        platform=resolve_platform_name(platform_name, platform_id),
        platform_id=platform_id,
        platform_name=platform_name,
        self_id=self_id,
        user_id=user_id,
        group_id=group_id,
        is_group=is_group,
        umo=umo,
        message_id=message_id,
        session_id=session_id,
    )


def _message_id_from_event(event: Any) -> str:
    """`event.message_obj.message_id`（AstrBot 没有公开的 getter）。"""
    obj = getattr(event, 'message_obj', None)
    return _text(getattr(obj, 'message_id', ''))


def session_view(event: AstrMessageEvent, endpoint: Optional[AstrbotEndpoint] = None) -> SessionView:
    """把 `AstrMessageEvent` 翻成 `SessionView`（`plugin/core/service/session.py`）。

    字段映射（上游 `Session` → `SessionView`）：

    | 上游 | 来源 |
    | --- | --- |
    | `platform` | `resolve_platform_name()`（OneBot 家族统一 `'onebot'`） |
    | `selfId` | `event.get_self_id()` |
    | `userId` | `event.get_sender_id()` |
    | `channelId` | 群 = `get_group_id()`；私聊 = `get_sender_id()`（AstrBot 的会话 id） |
    | `guildId` | 群 = `get_group_id()`；私聊 = `''` |
    | `isDirect` | 私聊判定 |
    | `content` | `serialize_message_chain()`（Koishi mini-xml 形式） |
    | `elements` | 同上，结构化段列表 |
    | `quote` | `Reply` 组件（上游 `session.quote`） |
    | `messageId` | `event.message_obj.message_id` |
    | `username` | `event.get_sender_name()` |
    | `event` | 原始 `AstrMessageEvent` 引用 |
    """
    resolved = endpoint if endpoint is not None else endpoint_for_event(event)
    chain = _call(event, 'get_messages', []) or []
    content, elements, quote = serialize_message_chain(chain)
    if not content:
        # 有些适配器只填 `message_str`（纯文本）而没有结构化段。
        content = _text(_call(event, 'get_message_str', ''))
        if content and not elements:
            elements = [{'type': 'text', 'attrs': {'content': content}, 'children': []}]
    return SessionView(
        platform=resolved.platform,
        self_id=resolved.self_id,
        user_id=resolved.user_id,
        channel_id=resolved.group_id if resolved.is_group else resolved.user_id,
        guild_id=resolved.group_id if resolved.is_group else '',
        is_direct=not resolved.is_group,
        content=content,
        elements=elements,
        quote=quote,
        message_id=resolved.message_id or None,
        username=_text(_call(event, 'get_sender_name', '')),
        event=event,
    )


def looks_like_management_command(content: Any) -> bool:
    """上游 `looksLikeInterludeCommand`（`upstream/src/index.ts:915`）+ 本移植版前缀。"""
    return bool(COMMAND_WORD_RE.match(_text(content).strip()))


# =========================================================================== #
# 出站内容 → AstrBot 消息链
# =========================================================================== #

#: 上游 `OutgoingMessageDraft.content` 里可能带的 Koishi 元素标签。
_INLINE_ELEMENT_RE = re.compile(
    r'<(img|image|audio|record|video|face)\b([^>]*?)/?>',
    re.IGNORECASE,
)
_ATTR_RE = re.compile(r'([A-Za-z_:][-\w:.]*)\s*=\s*"([^"]*)"')


def _tag_attrs(raw: str) -> dict[str, str]:
    return {match.group(1).lower(): match.group(2) for match in _ATTR_RE.finditer(raw or '')}


def chain_from_content(content: Any) -> list[Any]:
    """把上游出站内容（可能是纯文本，也可能夹带 Koishi 元素标签）转成消息链组件。

    `upstream/src/service.ts` 的可见回复是纯文本（分段靠 `<sep/>`，已由 service
    切成多条），但 `delivery.py` 的历史实现可能带上 `<img src=...>` / `<face id=...>`；
    这里统一支持，无法识别的标签按文本原样保留。
    """
    text = _text(content)
    components: list[Any] = []
    position = 0
    for match in _INLINE_ELEMENT_RE.finditer(text):
        if match.start() > position:
            components.append(Plain(text[position:match.start()]))
        kind = match.group(1).lower()
        attrs = _tag_attrs(match.group(2))
        if kind in ('img', 'image'):
            source = attrs.get('src') or attrs.get('url') or ''
            components.append(_build_image(source, attrs.get('file')))
        elif kind in ('audio', 'record'):
            components.append(_build_record(attrs))
        elif kind == 'video':
            components.append(_build_video(attrs))
        elif kind == 'face':
            try:
                components.append(Face(id=int(attrs.get('id') or 0)))
            except (TypeError, ValueError):
                components.append(Plain(match.group(0)))
        position = match.end()
    if position < len(text):
        components.append(Plain(text[position:]))
    return [component for component in components if component is not None]


def _build_image(source: str, file_value: Optional[str] = None) -> Any:
    """构造 `Image` 组件：`http(s)` 用 URL，本地路径用文件系统构造器。"""
    if _downloadable(source):
        return Image(file=source, url=source if source.lower().startswith(('http://', 'https://')) else '')
    local = source or file_value or ''
    if local.lower().startswith('file://'):
        local = unquote(urlparse(local).path)
    if local and os.path.exists(local):
        factory = getattr(Image, 'fromFileSystem', None)
        if callable(factory):
            return factory(local)
        return Image(file=local)
    return Image(file=local or source or '')


def _build_record(attrs: dict[str, str]) -> Any:
    file_value = attrs.get('file') or attrs.get('src') or ''
    url = attrs.get('url') or ''
    if file_value.lower().startswith('file://'):
        file_value = unquote(urlparse(file_value).path)
    if not file_value and url:
        file_value = url
    try:
        return Record(file=file_value, url=url)
    except TypeError:  # pragma: no cover - 旧版签名
        return Record(file_value)


def _build_video(attrs: dict[str, str]) -> Any:
    source = attrs.get('src') or attrs.get('url') or attrs.get('file') or ''
    if source.lower().startswith('file://'):
        source = unquote(urlparse(source).path)
    try:
        from astrbot.api.message_components import Video  # noqa: PLC0415 - 可选组件
    except ImportError:  # pragma: no cover
        try:
            from astrbot.core.message.components import Video  # noqa: PLC0415
        except ImportError:
            return Plain(source)
    return Video(file=source)


# =========================================================================== #
# Transport：平台出站能力
# =========================================================================== #

class AstrbotTransport:
    """`plugin/core/service/transport.py` 的 `Transport` 协议实现。

    出站分两条路：

    * **回合内原路回复**（`send_session`）：写进 `AstrbotBridge` 的回合捕获缓冲，
      由 `main.py` 用 `yield event.plain_result(...)` 交回 AstrBot。这样回复的
      发送顺序、`stop_event()` 吞消息与 AstrBot 的发送管线完全一致，也避免同一条
      回复被"发送 + yield"两遍。
    * **主动投递**（`send_private` / `send_group` / `deliver_background`）：
      直接 `context.send_message(umo, MessageChain)`。
    """

    def __init__(self, bridge: 'AstrbotBridge') -> None:
        self.bridge = bridge

    # ---- 内部工具 ----

    @property
    def context(self) -> Context:
        return self.bridge.context

    async def _send_chain(self, umo: str, components: list[Any]) -> dict[str, Any]:
        if not umo:
            return {'ok': False, 'error': 'no-session-target'}
        if not components:
            return {'ok': False, 'error': 'empty-message'}
        try:
            await self.context.send_message(umo, MessageChain(chain=components))
        except Exception as error:  # noqa: BLE001 - 投递失败必须走降级分支
            log_fallback('warn', 'AstrBot 消息投递失败 会话=%s 错误=%s', umo, error)
            return {'ok': False, 'error': str(error)}
        return self._result([], umo)

    @staticmethod
    def _result(message_ids: list[str], umo: str = '', **extra: Any) -> dict[str, Any]:
        """`SendResult`：同时给 camelCase 与 snake_case 两种键名。

        上游 `SendMessageResult` 的形状由 service 的投递账本读取；本移植版的
        `core/service/transport.py` 用的是 snake_case 契约，而账本代码沿用了
        上游的 camelCase 字段名——两种键都给，调用方读哪个都不会 `KeyError`。
        """
        result: dict[str, Any] = {
            'ok': True,
            'messageIds': list(message_ids),
            'message_ids': list(message_ids),
            'umo': umo,
            'error': '',
        }
        result.update(extra)
        return result

    # ---- 私聊 / 群聊投递 ----

    async def send_private(
        self,
        participant: dict[str, Any],
        content: str,
        reply_to: Optional[str] = None,
    ) -> dict[str, Any]:
        """给一条关系分支发私聊消息。上游 `sendPrivateMessage(participant...)`。"""
        participant = participant if isinstance(participant, dict) else {}
        platform = _text(pick(participant, 'platform'))
        self_id = _text(pick(participant, 'selfId', 'self_id'))
        user_id = _text(pick(participant, 'userId', 'user_id'))
        channel_id = _text(pick(participant, 'channelId', 'channel_id')) or user_id
        umo = self.bridge.private_umo(platform, self_id, user_id or channel_id)
        components = chain_from_content(content)
        if reply_to:
            components.insert(0, Reply(id=_text(reply_to)))
        return await self._send_chain(umo, components)

    async def send_group(
        self,
        channel_id: str,
        content: str,
        reply_to: Optional[str] = None,
    ) -> dict[str, Any]:
        """向群频道发送。上游 `sendGroupMessage(story, channelId, content, quoteMessageId)`。"""
        umo = self.bridge.group_umo(channel_id)
        components = chain_from_content(content)
        if reply_to:
            components.insert(0, Reply(id=_text(reply_to)))
        return await self._send_chain(umo, components)

    async def send_session(self, session: Any, content: str) -> dict[str, Any]:
        """对当前入站会话原路回复。上游 `session.send(outgoingContent)`（`:5156`）。"""
        capture = self.bridge.capture
        if capture is not None and capture.matches(session):
            capture.append(content)
            return self._result([], '', captured=True)
        umo = self.bridge.session_umo(session)
        return await self._send_chain(umo, chain_from_content(content))

    async def send_image(self, channel_id: str, file_path: str, is_group: bool = False) -> dict[str, Any]:
        """发送本地图片。上游 `:2076`。"""
        umo = self.bridge.group_umo(channel_id) if is_group else self.bridge.channel_umo(channel_id)
        if not umo:
            log_fallback('debug', '图片发送跳过：无法解析会话 频道=%s', channel_id)
            return {'ok': False, 'error': 'no-session-target'}
        return await self._send_chain(umo, [_build_image(_text(file_path))])

    async def send_sticker(self, channel_id: str, file_path: str, is_group: bool = False) -> dict[str, Any]:
        """发送表情包资产（表情包就是图片文件）。上游 Chunk1/Chunk2 的本地表情投递。"""
        return await self.send_image(channel_id, file_path, is_group)

    async def send_native_face(
        self,
        channel_id: str,
        face_id: str,
        is_group: bool = False,
    ) -> dict[str, Any]:
        """发送平台原生表情。上游 `:2119`（只有 OneBot 平台支持）。

        平台不是 OneBot 家族时按 `docs/PORT_PLAN_SERVICE.md` §8 降级：返回
        `{'ok': False}`，调用方走既有的"投递失败"分支。
        """
        umo = self.bridge.group_umo(channel_id) if is_group else self.bridge.channel_umo(channel_id)
        if not umo:
            return {'ok': False, 'error': 'no-session-target'}
        if not self.bridge.is_onebot_umo(umo):
            log_fallback('debug', '原生表情降级：平台不支持 QQ 原生表情 频道=%s', channel_id)
            return {'ok': False, 'error': 'platform-without-native-face'}
        try:
            numeric = int(_text(face_id).strip() or 0)
        except (TypeError, ValueError):
            return {'ok': False, 'error': 'invalid-face-id'}
        if numeric <= 0:
            return {'ok': False, 'error': 'invalid-face-id'}
        return await self._send_chain(umo, [Face(id=numeric)])

    async def react(self, message_ref: str, reaction: str) -> bool:
        """给一条消息加表态。上游 `ExecutableMessageReaction` 的执行路径。

        AstrBot 的事件上带原生 `react(emoji)`；拿不到对应事件（或平台不支持）时
        返回 `False`，调用方按"未执行"记账。
        """
        event = self.bridge.event_for_message(message_ref)
        if event is None:
            log_fallback('debug', '消息表态降级：没有可用的入站事件 目标=%s', message_ref)
            return False
        emoji = REACTION_EMOJI.get(_text(reaction).strip().lower(), _text(reaction).strip())
        if not emoji:
            return False
        react = getattr(event, 'react', None)
        if not callable(react):
            log_fallback('debug', '消息表态降级：当前平台事件不支持表态 目标=%s', message_ref)
            return False
        try:
            result = react(emoji)
            if inspect.isawaitable(result):
                await result
        except Exception as error:  # noqa: BLE001
            log_fallback('debug', '消息表态失败 目标=%s 错误=%s', message_ref, error)
            return False
        return True

    # ---- 读取辅助 ----

    async def fetch_member_name(self, channel_id: str, user_id: str) -> str:
        """取群成员显示名。上游 `bot.getGroupMemberInfo`（Chunk2 `groupMessages`）。"""
        event = self.bridge.event_for_channel(channel_id)
        if event is None:
            return ''
        getter = getattr(event, 'get_group', None)
        if not callable(getter):
            return ''
        try:
            group = getter(channel_id)
            if inspect.isawaitable(group):
                group = await group
        except Exception as error:  # noqa: BLE001
            log_fallback('debug', '群成员显示名读取失败 群=%s 错误=%s', channel_id, error)
            return ''
        for member in getattr(group, 'members', None) or []:
            if _text(_attr(member, 'user_id', 'userId', 'id')) == _text(user_id):
                return _text(_attr(member, 'nickname', 'nick', 'name', 'card'))
        return ''

    async def fetch_image(self, url: str) -> Optional[bytes]:
        """下载图片原始字节。上游 `ctx.http.get(url, {responseType:'arraybuffer'})`。

        支持 `http(s)` / `data:` / `file:` 与 Koishi 语义的 `onebot-file:` /
        `onebot-url:` / `file-url:` 前缀。裸 OneBot file token 需要机器人账号
        调用 `get_image`，本适配层拿不到该通道，按降级返回 `None` 并记日志。
        """
        return await self._fetch_bytes(url, '图片')

    async def fetch_audio(self, url: str) -> Optional[bytes]:
        """下载音频原始字节。上游原生音频通道（Chunk2）。

        与图片相同的降级面：SILK 语音需要 OneBot 服务端转码，本适配层只能取
        已经是 mp3/wav 等可解码载荷的直链。
        """
        return await self._fetch_bytes(url, '音频')

    async def _fetch_bytes(self, url: str, label: str) -> Optional[bytes]:
        source = _text(url).strip()
        if not source:
            return None
        for prefix in ('onebot-file:', 'onebot-url:', 'file-url:'):
            if source.startswith(prefix):
                source = source[len(prefix):]
                break
        source = source.split('#', 1)[0] if source.startswith('file-url:') else source
        if not source:
            return None
        if source.startswith('data:'):
            try:
                _, _, payload = source.partition(',')
                return base64.b64decode(payload)
            except Exception as error:  # noqa: BLE001
                log_fallback('debug', '%s data: URL 解析失败 错误=%s', label, error)
                return None
        if source.startswith('file://'):
            path = unquote(urlparse(source).path)
            try:
                return Path(path).read_bytes()
            except OSError as error:
                log_fallback('debug', '%s 本地文件读取失败 路径=%s 错误=%s', label, path, error)
                return None
        if not re.match(r'^https?://', source, re.IGNORECASE):
            log_fallback(
                'debug', '%s 降级：非直链地址（OneBot file token 需要机器人 API 通道） 来源=%s', label, source[:120],
            )
            return None
        return await self.bridge.http_get_bytes(source)

    async def list_sticker_files(self, root: str) -> list[str]:
        """列出表情包目录下的图片文件。上游 `listStickerFiles(root)`（`:7309`）。

        逐字等价：递归深度 ≤ 3，只收 `png/jpg/jpeg/webp/gif`，结果排序。
        """
        base = Path(_text(root).strip())
        if not base.is_dir():
            return []
        files: list[str] = []

        def visit(directory: Path, depth: int) -> None:
            if depth > 3:
                return
            try:
                entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
            except OSError:
                return
            for entry in entries:
                try:
                    if entry.is_dir():
                        visit(Path(entry.path), depth + 1)
                    elif entry.is_file() and Path(entry.name).suffix.lower() in STICKER_EXTENSIONS:
                        files.append(str(Path(entry.path).resolve()))
                except OSError:  # pragma: no cover - 目录在扫描中被删除
                    continue

        visit(base, 0)
        return sorted(files)

    # ---- 网页观察（上游 Puppeteer 的抽象替身） ----

    async def search_web(self, query: str, timeout_ms: int) -> list[dict[str, Any]]:
        """执行一次搜索。上游 `mode: 'search'` 的网页观察路径（Chunk5）。

        **降级路径**：AstrBot 4.x 没有给插件暴露通用网页搜索 API。这里按顺序探测
        宿主 Context 上的 `web_search` / `search_web`（未来版本或其它插件可能注入），
        都没有时记 `debug` 日志并返回 `[]`——调用方按"没有网页观察"继续叙事，
        后台浏览意图会被标记为失败观察，不会阻塞剧本。
        """
        query = _text(query).strip()
        if not query:
            return []
        for attribute in ('web_search', 'search_web'):
            searcher = getattr(self.context, attribute, None)
            if not callable(searcher):
                continue
            try:
                result = searcher(query)
                if inspect.isawaitable(result):
                    result = await asyncio.wait_for(result, timeout=max(1.0, timeout_ms / 1000.0))
            except Exception as error:  # noqa: BLE001
                log_fallback('warn', '网页搜索失败（宿主 API） 查询=%s 错误=%s', query, error)
                return []
            return _normalize_search_results(result, query)
        log_fallback(
            'debug', '网页搜索降级：AstrBot 未提供插件级搜索 API 查询=%s（返回空观察）', query,
        )
        return []

    async def visit_web(self, url: str, timeout_ms: int) -> Optional[dict[str, Any]]:
        """访问一个 URL 并返回 `{url, title, excerpt, text}`。上游 `mode: 'visit'`。

        **降级路径**：没有 Puppeteer，改用 `httpx` 直连 + 正则提取可见文本。
        只做只读 GET：不登录、不填表、不下载、不发布；非 `http(s)` 地址与
        私网地址直接拒绝，失败返回 `None`。
        """
        target = _text(url).strip()
        if not re.match(r'^https?://', target, re.IGNORECASE):
            log_fallback('debug', '网页观察拒绝：非 http(s) 地址 %s', target[:120])
            return None
        host = (urlparse(target).hostname or '').lower()
        if not host or host in ('localhost', '127.0.0.1', '::1') or host.endswith('.local'):
            log_fallback('debug', '网页观察拒绝：私网/本机地址 %s', target[:120])
            return None
        html = await self.bridge.http_get_text(target, timeout_ms)
        if not html:
            return None
        return {
            'url': target,
            'title': _extract_title(html),
            'excerpt': _extract_text(html)[:3_000],
            'text': _extract_text(html)[:12_000],
        }

    # ---- typ-0 后台投递出口 ----

    async def deliver_background(self, delivery: dict[str, Any]) -> dict[str, Any]:
        """后台（无实时 Session）投递。上游 `desktopDeliveryHandler`（`:712-715`）。

        入参是 `BackgroundDelivery`：`participant_id / self_id / platform /
        channel_id / kind / content / quote_message_id`。这里用 `participant_id`
        回查参与者行拿到投递坐标，查不到时退回入参里的字段。
        """
        payload = delivery if isinstance(delivery, dict) else {}
        content = _text(pick(payload, 'content'))
        quote_id = pick(payload, 'quoteMessageId', 'quote_message_id')
        kind = _text(pick(payload, 'kind')).lower()
        participant = await self.bridge.load_participant(_text(pick(payload, 'participantId', 'participant_id')))
        if participant is None:
            participant = {
                'platform': pick(payload, 'platform'),
                'selfId': pick(payload, 'selfId', 'self_id'),
                'userId': pick(payload, 'userId', 'user_id'),
                'channelId': pick(payload, 'channelId', 'channel_id'),
            }
        if kind == 'group':
            return await self.send_group(
                _text(pick(participant, 'channelId', 'channel_id') or pick(payload, 'channelId', 'channel_id')),
                content,
                _text(quote_id) or None,
            )
        return await self.send_private(participant, content, _text(quote_id) or None)


def _pick_any(value: Any, *names: str) -> Any:
    """按顺序取第一个存在的键（`pick()` 只支持 camel/snake 两种拼写）。"""
    if not isinstance(value, dict):
        return None
    for name in names:
        if name in value and value[name] is not None:
            return value[name]
    return None


def _normalize_search_results(result: Any, query: str) -> list[dict[str, Any]]:
    """把宿主返回的搜索结果归一成上游 `WebSearchResult` 的形状。"""
    if not result:
        return []
    if isinstance(result, dict):
        items = result.get('results') or result.get('items') or result.get('data') or []
    elif isinstance(result, (list, tuple)):
        items = list(result)
    else:
        return []
    normalized: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        url = _text(_pick_any(item, 'url', 'link', 'href'))
        title = _text(_pick_any(item, 'title', 'name'))
        snippet = _text(_pick_any(item, 'snippet', 'content', 'description', 'text', 'excerpt'))
        if not url and not title:
            continue
        # 上游搜索结果是 `{url, title, text}`（Chunk5 读 `top.text`）；这里同时给出
        # `text` / `excerpt` / `snippet` 三种拼写，宿主返回哪种字段名都能取到正文。
        normalized.append({
            'query': query,
            'title': title,
            'url': url,
            'text': snippet,
            'excerpt': snippet,
            'snippet': snippet,
        })
    return normalized


_SCRIPT_STYLE_RE = re.compile(r'<(script|style)\b[^>]*>.*?</\1>', re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r'<[^>]+>')
_TITLE_RE = re.compile(r'<title[^>]*>(.*?)</title>', re.IGNORECASE | re.DOTALL)
_WS_RE = re.compile(r'\s{2,}')


def _strip_html(html: str) -> str:
    text = _SCRIPT_STYLE_RE.sub(' ', html)
    text = _TAG_RE.sub(' ', text)
    for entity, char in (('&nbsp;', ' '), ('&amp;', '&'), ('&lt;', '<'), ('&gt;', '>'), ('&quot;', '"')):
        text = text.replace(entity, char)
    return _WS_RE.sub(' ', text).strip()


def _extract_title(html: str) -> str:
    match = _TITLE_RE.search(html or '')
    return _strip_html(match.group(1)) if match else ''


def _extract_text(html: str) -> str:
    return _strip_html(html or '')


# =========================================================================== #
# InterludeContext：Koishi `ctx` 的 AstrBot 实现
# =========================================================================== #

class _AstrbotLoggerAdapter:
    """`ctx.logger('hds-interlude')` 的等价物（`service.emit_log` 读它）。"""

    def __init__(self, logger: Any) -> None:
        self._logger = logger

    def _write(self, level: str, text: str) -> None:
        writer = getattr(self._logger, level, None)
        if not callable(writer):
            log_fallback('info', '%s', text)
            return
        try:
            writer(text)
        except Exception:  # pragma: no cover - 日志通道绝不抛异常
            log_fallback('info', '%s', text)

    def debug(self, text: str) -> None:
        self._write('debug', text)

    def info(self, text: str) -> None:
        self._write('info', text)

    def warning(self, text: str) -> None:
        self._write('warning', text)

    def warn(self, text: str) -> None:
        self._write('warning', text)

    def error(self, text: str) -> None:
        self._write('error', text)


class AstrbotHttpClient:
    """`plugin.core.narrator.HttpClient` 的 AstrBot 实现。

    `core/narrator.py` 按上游写法直接 POST 一个 OpenAI 兼容 endpoint；本移植版把
    chat 请求改道 AstrBot 自己的 Provider（`context.get_current_chat_provider_id`
    + `context.llm_generate`），于是：

    * 用户在 AstrBot 里配的模型 / 密钥 / 备用 provider 全部生效，插件不再重复
      持有一份密钥；
    * 返回值被重新包装成 OpenAI 形状（`choices[0].message.content` +
      `usage`），`core/narrator.py` 的解析逻辑一个字都不用改。

    没有可用的 AstrBot Provider 时（或不是 chat/embedding 请求）回落到
    `HttpxHttpClient`：这样在 `model_center.providers` 里直接填 endpoint/key
    的旧配置也仍然可用。流式（`iterate_sse`）本版本不接管，一律回落。
    """

    #: 任务名 → AstrBot Provider 的解析策略都相同；保留参数是为了日志可读。
    def __init__(self, bridge: 'AstrbotBridge', fallback: Any = None) -> None:
        self.bridge = bridge
        self._fallback = fallback if fallback is not None else HttpxHttpClient()

    @property
    def context(self) -> Context:
        return self.bridge.context

    async def post_json(
        self,
        url: str,
        headers: Optional[dict[str, str]] = None,
        body: Any = None,
        timeout: Optional[int] = None,
    ) -> Any:
        payload = body if isinstance(body, dict) else {}
        # 优先级：**插件自己在 `model_center.providers` 里配了 endpoint 就用它**。
        #
        # 上游本来就是「每类任务一条自己的 provider」（main / compaction / timeline /
        # alter / embedding / stickers / vision），任务级路由与连接级参数
        # （temperature / max_tokens / response_format / extra_body / 思考开关）全都挂在
        # 那条连接上。无条件改道 AstrBot Provider 会让 `model_center` 里的全部配置静默
        # 失效——实测过：插件指向桩服务、日志也显示用桩的模型名，请求却仍打到 AstrBot
        # 的 Ollama 上。
        #
        # 只有插件没给出可用 endpoint 时（url 为空/非 http），才回落到 AstrBot Provider，
        # 保留「不想单独填 key，直接复用 AstrBot 里配好的模型」这条便利路径。
        explicit_endpoint = isinstance(url, str) and url.strip().lower().startswith(('http://', 'https://'))
        if 'messages' in payload and not explicit_endpoint:
            routed = await self._chat(payload, timeout)
            if routed is not None:
                return routed
        if 'input' in payload and ('model' in payload or 'dimensions' in payload) and not explicit_endpoint:
            routed = await self._embedding(payload)
            if routed is not None:
                return routed
        return await self._fallback.post_json(url, headers, body, timeout)

    def iterate_sse(
        self,
        url: str,
        headers: Optional[dict[str, str]] = None,
        body: Any = None,
        timeout: Optional[int] = None,
    ) -> AsyncIterator[str]:
        """流式请求不接管（见类文档）；直接回落 `HttpxHttpClient`。"""
        return self._fallback.iterate_sse(url, headers, body, timeout)

    # ---- chat ----

    async def _chat(self, payload: dict[str, Any], timeout: Optional[int]) -> Optional[dict[str, Any]]:
        provider_id = await self.bridge.resolve_chat_provider_id()
        if not provider_id:
            log_fallback('debug', 'AstrBot 未解析到聊天 Provider：回落到直连 endpoint')
            return None
        system_parts: list[str] = []
        conversation: list[str] = []
        for message in payload.get('messages') or []:
            if not isinstance(message, dict):
                continue
            role = _text(message.get('role') or 'user')
            content = message.get('content')
            if isinstance(content, list):
                content = ' '.join(
                    _text(part.get('text')) for part in content if isinstance(part, dict)
                )
            text = _text(content)
            if not text:
                continue
            if role == 'system':
                system_parts.append(text)
            elif role == 'assistant':
                conversation.append('assistant: %s' % text)
            else:
                conversation.append(text)
        params: dict[str, Any] = {}
        if payload.get('temperature') is not None:
            params['temperature'] = payload['temperature']
        if payload.get('top_p') is not None:
            params['top_p'] = payload['top_p']
        if payload.get('max_tokens') is not None:
            params['max_tokens'] = payload['max_tokens']
        try:
            response = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt='\n\n'.join(conversation),
                system_prompt='\n\n'.join(system_parts) or None,
                **params,
            )
        except TypeError:
            # 某些 Provider 不接受采样参数：去掉后重试一次。
            try:
                response = await self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt='\n\n'.join(conversation),
                    system_prompt='\n\n'.join(system_parts) or None,
                )
            except Exception as error:  # noqa: BLE001
                log_fallback('warn', 'AstrBot llm_generate 失败；回落直连 endpoint 错误=%s', error)
                return None
        except Exception as error:  # noqa: BLE001
            log_fallback('warn', 'AstrBot llm_generate 失败；回落直连 endpoint 错误=%s', error)
            return None
        text = _text(getattr(response, 'completion_text', ''))
        usage = getattr(response, 'usage', None) or {}
        return {
            'id': 'astrbot-%s' % provider_id,
            'object': 'chat.completion',
            'model': provider_id,
            'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': text}, 'finish_reason': 'stop'}],
            'usage': {
                'prompt_tokens': _usage_field(usage, 'prompt_tokens', 'input_tokens'),
                'completion_tokens': _usage_field(usage, 'completion_tokens', 'output_tokens'),
                'total_tokens': _usage_field(usage, 'total_tokens'),
            },
        }

    # ---- embedding ----

    async def _embedding(self, payload: dict[str, Any]) -> Optional[dict[str, Any]]:
        provider = self.bridge.embedding_provider()
        if provider is None:
            return None
        inputs = payload.get('input')
        texts = [inputs] if isinstance(inputs, str) else list(inputs or [])
        texts = [_text(item) for item in texts]
        if not texts:
            return None
        embedder = getattr(provider, 'get_embedding', None)
        batch_embedder = getattr(provider, 'get_embeddings', None)
        if not callable(embedder) and not callable(batch_embedder):
            log_fallback('debug', 'AstrBot Provider 不支持 Embedding：回落直连 endpoint')
            return None
        try:
            if len(texts) == 1 and callable(embedder):
                result = embedder(texts[0])
                if inspect.isawaitable(result):
                    result = await result
                result = [result]
            elif callable(batch_embedder):
                result = batch_embedder(texts)
                if inspect.isawaitable(result):
                    result = await result
            else:
                vectors: list[Any] = []
                for text in texts:
                    item = embedder(text)
                    if inspect.isawaitable(item):
                        item = await item
                    vectors.append(item)
                result = vectors
        except Exception as error:  # noqa: BLE001
            log_fallback('warn', 'AstrBot Embedding 失败；回落直连 endpoint 错误=%s', error)
            return None
        vectors = _normalize_embeddings(result, len(texts))
        if vectors is None:
            return None
        return {
            'object': 'list',
            'model': payload.get('model') or 'astrbot-embedding',
            'data': [{'object': 'embedding', 'index': index, 'embedding': vector} for index, vector in enumerate(vectors)],
        }


def _usage_field(usage: Any, *names: str) -> int:
    for name in names:
        value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
        if isinstance(value, (int, float)):
            return int(value)
    return 0


def _normalize_embeddings(result: Any, expected: int) -> Optional[list[list[float]]]:
    """把 AstrBot 的 embedding 返回值归一成逐条向量列表。"""
    if result is None:
        return None
    if isinstance(result, dict):
        data = result.get('data')
        if isinstance(data, list):
            vectors = []
            for item in data:
                vector = item.get('embedding') if isinstance(item, dict) else item
                if isinstance(vector, list):
                    vectors.append([float(value) for value in vector])
            return vectors or None
        for key in ('embeddings', 'embedding', 'vectors'):
            if isinstance(result.get(key), list):
                result = result[key]
                break
    if isinstance(result, list):
        if expected == 1 and result and not isinstance(result[0], (list, tuple)):
            return [[float(value) for value in result]]
        vectors = []
        for item in result:
            if isinstance(item, (list, tuple)):
                vectors.append([float(value) for value in item])
        return vectors or None
    return None


class AstrbotInterludeContext(InterludeContext):
    """`InterludeContext` 的 AstrBot 装配。

    * `logger` 接 AstrBot 的 `logger`（`service.emit_log` 读它）；
    * `plugin.core.logging.set_log_sink` 接同一个 logger（结构化分层日志）；
    * `database` 是插件私有的 `plugin/core/database.py`；
    * `http` 接 `AstrbotHttpClient`，把模型调用交给 AstrBot Provider；
    * `bots()` 从 AstrBot 的平台实例列表取（上游 `ctx.bots`）；
    * `base_dir` 是插件数据目录（上游 `ctx.baseDir`，表情库相对路径的根）。
    """

    def __init__(
        self,
        context: Context,
        bridge: 'AstrbotBridge',
        database: Database,
        http_client: Any,
        logger: Any,
        base_dir: str,
    ) -> None:
        super().__init__(
            logger=logger,
            database=database,
            provider_resolver=bridge.resolve_chat_provider_id,
            http=http_client,
            bots=bridge.list_bots,
            base_dir=base_dir,
        )
        self.context = context
        self.bridge = bridge


# =========================================================================== #
# 回合捕获缓冲
# =========================================================================== #

@dataclass
class _TurnCapture:
    """一个叙事回合内 `send_session` 收集到的可见回复。

    上游 `session.send()` 直接把消息投递出去；AstrBot 的插件宿主要求把结果
    `yield` 回去（否则会和默认 LLM 管线抢发送）。因此回合内的原路回复先落进
    这个缓冲，`main.py` 再用 `event.plain_result()` 交回，顺序与上游一致，
    也不会重复发送。
    """

    platform: str
    self_id: str
    scope: str
    umo: str
    texts: list[str] = field(default_factory=list)

    def matches(self, session: Any) -> bool:
        if session is None:
            return False
        if isinstance(session, SessionView):
            platform = session.platform
            self_id = session.self_id
            scope = session.guild_id or session.channel_id if not session.is_direct else session.user_id
        elif isinstance(session, dict):
            platform = _text(pick(session, 'platform'))
            self_id = _text(pick(session, 'selfId', 'self_id'))
            scope = _text(pick(session, 'channelId', 'channel_id') or pick(session, 'userId', 'user_id'))
        else:
            return False
        return (platform, self_id, scope) == (self.platform, self.self_id, self.scope)

    def append(self, content: Any) -> None:
        text = _text(content).strip()
        if text:
            self.texts.append(text)


# =========================================================================== #
# 配置归一化
# =========================================================================== #

def _to_snake_key(name: str) -> str:
    return re.sub(r'(?<!^)(?=[A-Z])', '_', str(name)).lower()


def _fallback_normalize_config(raw: Any) -> dict[str, Any]:
    """`normalize_config` 的本地降级实现（递归 camelCase → snake_case）。

    仅在 `plugin.core.service.config` 尚未产出时使用：不做默认值补全，只保证
    键名拼写统一，`core/service` 的 `_config_section` 双读仍然能取到值。
    """
    if not isinstance(raw, dict):
        return {}
    result: dict[str, Any] = {}
    for key, value in raw.items():
        result[_to_snake_key(key)] = _fallback_normalize_config(value) if isinstance(value, dict) else value
    return result


try:  # pragma: no cover - 取决于并行任务落地顺序
    from ..core.service.config import (
        CONFIG_SECTION_ALIASES as _CORE_SECTION_ALIASES,
        apply_section_aliases as _core_apply_section_aliases,
        normalize_config as _core_normalize_config,
    )
except ImportError:  # pragma: no cover
    _CORE_SECTION_ALIASES = None
    _core_apply_section_aliases = None
    _core_normalize_config = None

#: AstrBot `_conf_schema.json` 顶层分组名 → 上游 Console 分组名。
#: **单一实现源在 `plugin/core/service/config.py`**（`CONFIG_SECTION_ALIASES`），
#: 适配层只做一次镜像导入，避免两处各抄一份表后漂移；core 尚未落地时才用下面
#: 这份等值兜底（值必须与 core 保持一致）。
CONFIG_SECTION_ALIASES: dict[str, str] = (
    dict(_CORE_SECTION_ALIASES) if _CORE_SECTION_ALIASES is not None
    else {'model_center': 'model', 'qq_access': 'onebot'}
)


def _local_apply_section_aliases(raw: Any) -> dict[str, Any]:
    """`core.service.config.apply_section_aliases` 的本地等值兜底。"""
    if not isinstance(raw, dict):
        return {}
    source = dict(raw)
    for alias, target in CONFIG_SECTION_ALIASES.items():
        if alias in source and target not in source:
            source[target] = source[alias]
    return source


def normalize_bridge_config(raw: Any) -> dict[str, Any]:
    """把 AstrBot 配置归一成 `core/service` 认得的 snake_case 结构。

    直接调用 `plugin.core.service.config.normalize_config`：它现在自己就带分组别名
    （`apply_section_aliases`，`model_center` → `model`、`qq_access` → `onebot`）
    与默认值补全，因此适配层**不再自己打补丁**——任何调用方（测试、其它适配）
    走 core 都能拿到正确结果。

    只有 core 的 `normalize_config` 缺失（并行移植期）或它自己抛异常时，才退回
    本地降级：先做一遍等值别名，再递归 camelCase → snake_case（见
    `_fallback_normalize_config`，不补默认值）。
    """
    source = dict(raw) if isinstance(raw, dict) else {}
    if _core_normalize_config is not None:
        try:
            return _core_normalize_config(source)
        except Exception as error:  # noqa: BLE001 - 归一化失败不能挡住插件加载
            log_fallback('warn', '配置归一化失败，使用本地降级实现 错误=%s', error)
    return _fallback_normalize_config(_local_apply_section_aliases(source))


# =========================================================================== #
# Bridge
# =========================================================================== #

class AstrbotBridge:
    """`apply(ctx, config)` 的 AstrBot 等价物：装配 + 入站分发 + 生命周期。

    `main.py` 只跟这个类打交道，不 import 任何 `plugin/core/` 的实现细节：
    入站走 `handle_event()`，出站走 `transport`，配置与展示辅助走本类的小工具方法。
    """

    def __init__(
        self,
        context: Context,
        config: Any = None,
        data_dir: Optional[str] = None,
        logger: Any = None,
    ) -> None:
        self.context = context
        self.data_dir = data_dir or plugin_data_dir()
        Path(self.data_dir).mkdir(parents=True, exist_ok=True)
        self.config = normalize_bridge_config(config)
        self.logger = logger
        self.db = Database(database_path(self.data_dir))
        self.transport = AstrbotTransport(self)
        self.http_client = AstrbotHttpClient(self)
        self.interlude_context = AstrbotInterludeContext(
            context=context,
            bridge=self,
            database=self.db,
            http_client=self.http_client,
            logger=_AstrbotLoggerAdapter(logger) if logger is not None else None,
            base_dir=self.data_dir,
        )
        self.service = InterludeService(self.interlude_context, self.config, self.db, self.transport)
        self._started = False
        self._start_lock = asyncio.Lock()
        self._capture: Optional[_TurnCapture] = None
        # 投递坐标登记表（出站时把 HDSI 的平台/账号翻回 AstrBot 的 UMO）。
        self._platform_ids: dict[tuple[str, str], str] = {}
        self._private_endpoints: dict[tuple[str, str, str], AstrbotEndpoint] = {}
        self._group_endpoints: dict[str, AstrbotEndpoint] = {}
        self._channel_events: dict[str, Any] = {}
        self._message_events: dict[str, Any] = {}
        self._current_umo = ''
        self._httpx_client: Any = None
        self._register_log_sink()

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def ensure_started(self) -> None:
        """建表 + 启动后台任务 + 触发 ready（幂等）。"""
        if self._started:
            return
        async with self._start_lock:
            if self._started:
                return
            self.db.register_tables()
            await self.apply_story_defaults_persona()
            self.service.start_background_tasks()
            self.interlude_context.emit_ready()
            self._started = True
            missing = getattr(self.service, '_missing_chunks', ())
            if missing:
                log_fallback(
                    'warn', 'HDS Interlude 分块缺失，相关命令将不可用：%s', '、'.join(missing),
                )
            log_fallback('info', 'HDS Interlude 已就绪 数据目录=%s', self.data_dir)

    async def apply_story_defaults_persona(self) -> str:
        """把 `story_defaults.persona_id` 指向的 AstrBot 人格导入为角色设定。

        **本移植版新增的配置桥：`plugin/core/` 完全不读 `persona_id` /
        `extra_setting`，也不 import astrbot。** 这两个键只存在于
        `plugin/_conf_schema.json`（`story_defaults` 分组），语义的落地**全部**在
        适配层这一处；把 core 换个宿主（或做纯函数测试）时它们自然消失，不影响
        叙事核心。

        导入规则：

        * 角色名 = `persona_id`（`AGENTS.md` 的约定：角色名优先取 persona_id）；
        * 角色设定 = 该人格的完整 `system_prompt`；
        * `story_defaults.extra_setting`（自由文本补充）追加在角色设定之后。

        必须发生在第一份剧本创建之前，所以放在 `ensure_started()` 里；
        `ServiceChunk7.initial_story_setting()` 每次都现读 `self.story_defaults`，
        因此直接改写配置字典即可生效（无缓存需要失效）。取不到人格时保持原配置，
        只在日志里提示一次。AstrBot 4.28 的 `PersonaManager.get_persona` 是协程
        （旧版是同步），这里按 awaitable 兼容两代签名。
        """
        section = self.section('story_defaults')
        persona_id = _text(pick(section, 'personaId', 'persona_id')).strip()
        if not persona_id:
            return ''
        persona = None
        manager = getattr(self.context, 'persona_manager', None)
        getter = getattr(manager, 'get_persona', None)
        if callable(getter):
            try:
                persona = getter(persona_id)
                if inspect.isawaitable(persona):
                    persona = await persona
            except Exception as error:  # noqa: BLE001 - 人格不存在是正常配置错误
                log_fallback('warn', '人格导入失败 persona=%s 错误=%s', persona_id, error)
                return ''
        if persona is None:
            log_fallback('warn', '人格导入跳过：AstrBot 中找不到 persona %s', persona_id)
            return ''
        system_prompt = _text(_attr(persona, 'system_prompt', 'prompt'))
        if not system_prompt.strip():
            log_fallback('warn', '人格导入跳过：persona %s 没有 system_prompt', persona_id)
            return ''
        extra = _text(pick(section, 'extraSetting', 'extra_setting')).strip()
        profile = system_prompt.strip()
        if extra:
            profile = '%s\n\n%s' % (profile, extra)
        section['character_name'] = persona_id
        section['characterName'] = persona_id
        section['character_profile'] = profile
        section['characterProfile'] = profile
        if isinstance(self.config, dict):
            self.config['story_defaults'] = section
        log_fallback('info', '已从 AstrBot 人格导入角色设定 persona=%s 字数=%d', persona_id, len(profile))
        return persona_id

    async def shutdown(self) -> None:
        """停止后台计时器、关闭 HTTP 客户端与数据库（幂等）。"""
        for attribute in ('_sweep_timer', '_compaction_timer', '_blind_mode_timer', '_sticker_scan_timer'):
            handle = getattr(self.service, attribute, None)
            cancel = getattr(handle, 'cancel', None)
            if callable(cancel):
                try:
                    cancel()
                except Exception:  # pragma: no cover - 已触发的计时器取消是 no-op
                    pass
        self.service.background_started = False
        client = self._httpx_client
        self._httpx_client = None
        if client is not None:
            closer = getattr(client, 'aclose', None)
            if callable(closer):
                try:
                    result = closer()
                    if inspect.isawaitable(result):
                        await result
                except Exception:  # pragma: no cover
                    pass
        try:
            self.db.close()
        except Exception:  # pragma: no cover
            pass
        self._started = False
        log_fallback('info', 'HDS Interlude 已停止')

    # ------------------------------------------------------------------ #
    # 日志 / 账号 / 模型
    # ------------------------------------------------------------------ #

    def _register_log_sink(self) -> None:
        """把 `plugin.core.logging` 的结构化日志接到 AstrBot 的 logger。"""
        target = self.logger
        if target is None:
            return

        def sink(level: str, text: str) -> None:
            writer = getattr(target, 'debug' if level in ('debug', 'info') else 'warning', None)
            if level == 'error':
                writer = getattr(target, 'error', writer)
            if callable(writer):
                try:
                    writer(text)
                    return
                except Exception:  # pragma: no cover
                    pass
            print(text)  # noqa: T201 - 与 core/logging.py 的默认 sink 行为一致

        try:
            interlude_logging.set_log_sink(sink)
        except Exception:  # pragma: no cover
            pass

    def list_bots(self) -> list[Any]:
        """上游 `ctx.bots`：AstrBot 的平台适配器实例列表。"""
        manager = getattr(self.context, 'platform_manager', None)
        for attribute in ('platform_insts', 'platforms'):
            instances = getattr(manager, attribute, None)
            if instances is not None:
                try:
                    return list(instances)
                except TypeError:  # pragma: no cover
                    return []
        return []

    async def resolve_chat_provider_id(self) -> str:
        """解析当前会话的 AstrBot 聊天 Provider id。

        优先 `context.get_current_chat_provider_id(umo)`（会话级 / 分支级人格与
        模型选择都会生效）；拿不到 umo 或抛异常时退回 `context.get_using_provider()`。
        """
        getter = getattr(self.context, 'get_current_chat_provider_id', None)
        if callable(getter) and self._current_umo:
            try:
                provider_id = getter(self._current_umo)
                if inspect.isawaitable(provider_id):
                    provider_id = await provider_id
                if provider_id:
                    return _text(provider_id)
            except Exception:  # noqa: BLE001 - 会话没有绑定模型是正常情况
                pass
        fallback = getattr(self.context, 'get_using_provider', None)
        if callable(fallback):
            try:
                provider = fallback(self._current_umo or None)
                if inspect.isawaitable(provider):
                    provider = await provider
            except Exception:  # noqa: BLE001
                provider = None
            meta = getattr(provider, 'meta', None)
            if callable(meta):
                try:
                    return _text(getattr(meta(), 'id', ''))
                except Exception:  # pragma: no cover
                    return ''
        return ''

    def embedding_provider(self) -> Any:
        """取用于 Embedding 的 AstrBot Provider（没有就返回 `None` → 回落直连）。

        AstrBot 4.28 把 Embedding 单独列成一类 Provider
        （`Context.get_all_embedding_providers()`，`Provider.get_embedding` /
        `get_embeddings`），跟聊天 Provider 不是一回事；先找专用 Embedding
        Provider，找不到再退回当前会话的 Provider（它可能也支持向量）。
        """
        getter = getattr(self.context, 'get_all_embedding_providers', None)
        if callable(getter):
            try:
                providers = list(getter() or [])
            except Exception:  # noqa: BLE001
                providers = []
            if providers:
                return providers[0]
        fallback = getattr(self.context, 'get_using_provider', None)
        if not callable(fallback):
            return None
        try:
            provider = fallback(self._current_umo or None)
        except Exception:  # noqa: BLE001
            return None
        return provider

    # ------------------------------------------------------------------ #
    # 投递坐标
    # ------------------------------------------------------------------ #

    def remember_event(self, event: AstrMessageEvent, session: SessionView, endpoint: AstrbotEndpoint) -> None:
        """登记本次事件的投递坐标，供后台主动投递时反查。"""
        self._platform_ids[(endpoint.platform, endpoint.self_id)] = endpoint.platform_id
        self._platform_ids.setdefault((endpoint.platform, ''), endpoint.platform_id)
        self._current_umo = endpoint.umo
        if endpoint.is_group:
            self._group_endpoints[endpoint.group_id or endpoint.channel_id] = endpoint
            if endpoint.group_id:
                self._channel_events[endpoint.group_id] = event
        else:
            self._private_endpoints[(endpoint.platform, endpoint.self_id, endpoint.user_id)] = endpoint
        if endpoint.message_id:
            self._message_events[endpoint.message_id] = event

    def _platform_id_for(self, platform: str, self_id: str = '') -> str:
        for key in ((platform, self_id), (platform, '')):
            if key in self._platform_ids:
                return self._platform_ids[key]
        return platform

    def private_umo(self, platform: str, self_id: str, user_id: str) -> str:
        """私聊 UMO：`<platform_id>:FriendMessage:<user_id>`。"""
        endpoint = self._private_endpoints.get((platform, self_id, user_id))
        if endpoint is not None:
            return endpoint.build_umo()
        platform_id = self._platform_id_for(platform, self_id)
        return '%s:FriendMessage:%s' % (platform_id, user_id) if user_id else ''

    def group_umo(self, channel_id: str) -> str:
        """群聊 UMO：`<platform_id>:GroupMessage:<group_id>`。"""
        endpoint = self._group_endpoints.get(_text(channel_id))
        if endpoint is not None:
            return endpoint.build_umo()
        platform_id = next(iter(self._platform_ids.values()), '') if len(set(self._platform_ids.values())) <= 1 else ''
        return '%s:GroupMessage:%s' % (platform_id, channel_id) if platform_id and channel_id else ''

    def channel_umo(self, channel_id: str) -> str:
        """按会话 id 猜 UMO（私聊与群聊都试一遍）。"""
        return self.group_umo(channel_id) or self.private_umo_for_scope(channel_id)

    def private_umo_for_scope(self, scope: str) -> str:
        for endpoint in self._private_endpoints.values():
            if endpoint.user_id == scope:
                return endpoint.build_umo()
        return ''

    def session_umo(self, session: Any) -> str:
        """从一个 `SessionView` / dict / 原始事件解析 UMO。"""
        if session is None:
            return ''
        if isinstance(session, SessionView):
            event_umo = _text(getattr(session.event, 'unified_msg_origin', '')) if session.event is not None else ''
            if event_umo:
                return event_umo
            if session.is_direct:
                return self.private_umo(session.platform, session.self_id, session.user_id)
            return self.group_umo(session.guild_id or session.channel_id)
        if isinstance(session, dict):
            platform = _text(pick(session, 'platform'))
            self_id = _text(pick(session, 'selfId', 'self_id'))
            if pick(session, 'isDirect', 'is_direct'):
                return self.private_umo(platform, self_id, _text(pick(session, 'userId', 'user_id')))
            return self.group_umo(_text(pick(session, 'guildId', 'guild_id') or pick(session, 'channelId', 'channel_id')))
        return _text(getattr(session, 'unified_msg_origin', ''))

    def is_onebot_umo(self, umo: str) -> bool:
        platform_id = _text(umo).split(':', 1)[0]
        for (platform, _self), registered in self._platform_ids.items():
            if registered == platform_id:
                return platform == 'onebot'
        return _is_onebot_adapter(platform_id, platform_id)

    def event_for_message(self, message_ref: str) -> Any:
        """按消息 id 找最近的入站事件（表态要挂在原生事件上）。"""
        reference = _text(message_ref).strip()
        if reference in self._message_events:
            return self._message_events[reference]
        return next(reversed(self._message_events.values()), None) if self._message_events else None

    def event_for_channel(self, channel_id: str) -> Any:
        """按群号找最近的入站事件（群成员查询要挂在原生事件上）。"""
        return self._channel_events.get(_text(channel_id))

    async def load_participant(self, participant_id: str) -> Optional[dict[str, Any]]:
        """按 id 读参与者行（后台投递需要它的 platform/selfId/userId）。"""
        identifier = _text(participant_id).strip()
        if not identifier:
            return None
        try:
            row = self.db.get('interlude_participant', {'id': identifier})
        except Exception as error:  # noqa: BLE001
            log_fallback('debug', '参与者读取失败 id=%s 错误=%s', identifier, error)
            return None
        return dict(row) if row is not None else None

    # ------------------------------------------------------------------ #
    # HTTP（图片 / 音频 / 网页）
    # ------------------------------------------------------------------ #

    def _client(self) -> Any:
        if self._httpx_client is None:
            import httpx  # noqa: PLC0415 - 延迟导入，没有 httpx 时给出明确错误

            self._httpx_client = httpx.AsyncClient(follow_redirects=True)
        return self._httpx_client

    async def http_get_bytes(self, url: str) -> Optional[bytes]:
        """下载原始字节（图片 / 语音共用）。失败返回 `None` 并记日志。"""
        try:
            response = await self._client().get(url)
            response.raise_for_status()
            return response.content
        except Exception as error:  # noqa: BLE001
            log_fallback('debug', '下载失败 URL=%s 错误=%s', _text(url)[:120], error)
            return None

    async def http_get_text(self, url: str, timeout_ms: int = 15_000) -> Optional[str]:
        """GET 一个网页并返回解码后的 HTML（超时按上游 `navigationTimeout`）。"""
        try:
            response = await self._client().get(url, timeout=max(1.0, timeout_ms / 1000.0))
            response.raise_for_status()
            return response.text
        except Exception as error:  # noqa: BLE001
            log_fallback('debug', '网页读取失败 URL=%s 错误=%s', _text(url)[:120], error)
            return None

    # ------------------------------------------------------------------ #
    # 入站分发（上游 `ctx.middleware`）
    # ------------------------------------------------------------------ #

    async def handle_event(self, event: AstrMessageEvent) -> list[str]:
        """把一条入站事件喂给 service 的入站入口，返回本回合的可见回复。

        逐条对应上游 `apply()` 里的中间件（`upstream/src/index.ts:503-516`）：

        1. 空文本且没有语音 → 不处理，交回其它处理器；
        2. 盲区模式下的管理命令 → 静默吞掉（`blindMode.enabled` 时上游直接
           `return`，不返回 `next()`）；
        3. `runtime.ignore_command_messages` 下的管理命令 → 交回命令解析器；
        4. 群聊 → `service.receive_group()`，私聊 → `service.receive()`；
           两者返回 `True` 表示已消费，此时调用 `event.stop_event()`，
           与上游"中间件不调用 `next()`"同义（沉默也被吞掉）。

        返回值是 `send_session` 在本回合捕获到的可见回复文本列表（顺序与上游
        `session.send` 调用顺序一致），由 `main.py` 用 `yield` 交回 AstrBot。
        """
        await self.ensure_started()
        endpoint = endpoint_for_event(event)
        session = session_view(event, endpoint)
        self.remember_event(event, session, endpoint)
        content = session.content

        if not content.strip() and not self._has_voice(session):
            return []

        if looks_like_management_command(content):
            if self.blind_mode_enabled:
                # 上游：失明模式下命令被 `command/before-execute` 静默消费。
                event.stop_event()
                return []
            if self.config_flag('runtime', 'ignore_command_messages', 'ignoreCommandMessages', default=True):
                # 交回 AstrBot 的命令解析器（不消费）。
                return []

        capture = self.begin_capture(endpoint)
        try:
            if endpoint.is_group:
                consumed = await self.service.receive_group(session)
            else:
                consumed = await self.service.receive(session)
        finally:
            self.end_capture()
        if consumed:
            event.stop_event()
        return list(capture.texts)

    # ------------------------------------------------------------------ #
    # 回合捕获
    # ------------------------------------------------------------------ #

    def begin_capture(self, endpoint: AstrbotEndpoint) -> _TurnCapture:
        capture = _TurnCapture(
            platform=endpoint.platform,
            self_id=endpoint.self_id,
            scope=endpoint.scope,
            umo=endpoint.umo,
        )
        self._capture = capture
        return capture

    def end_capture(self) -> None:
        self._capture = None

    @property
    def capture(self) -> Optional[_TurnCapture]:
        return self._capture

    def capture_texts(self) -> list[str]:
        capture = self._capture
        return list(capture.texts) if capture is not None else []

    # ------------------------------------------------------------------ #
    # 配置读取（给 main.py 用，避免它 import core）
    # ------------------------------------------------------------------ #

    def section(self, name: str) -> dict[str, Any]:
        """读一个 snake_case 配置段（兼容对象式配置）。

        `normalize_config` 已经补过分组别名，正常情况下 `name` 直接命中；这里仍
        按 `CONFIG_SECTION_ALIASES` 反向兜底一次（例如有人直接构造了未过归一化的
        `AstrbotBridge`，或将来别名表变化）。
        """
        section = self.config.get(name) if isinstance(self.config, dict) else None
        if section is None:
            for alias, target in CONFIG_SECTION_ALIASES.items():
                if target == name and isinstance(self.config, dict):
                    section = self.config.get(alias)
                    break
        if section is None:
            return {}
        if isinstance(section, dict):
            return section
        return {key: value for key, value in vars(section).items() if not key.startswith('_')}

    def config_flag(self, section: str, *names: str, default: Any = None) -> Any:
        """读一个配置项：双拼写（camelCase / snake_case）都认。"""
        data = self.section(section)
        for name in names:
            if name in data and data[name] is not None:
                return data[name]
            snake = _to_snake_key(name)
            if snake in data and data[snake] is not None:
                return data[snake]
        return default

    @property
    def blind_mode_enabled(self) -> bool:
        """`blindMode.enabled`（等价上游的 `config.blackBox?.enabled` 兼容读取）。"""
        for name in ('blind_mode', 'black_box'):
            value = self.config_flag(name, 'enabled', default=None)
            if value is not None:
                return bool(value)
        return False

    def _has_voice(self, session: SessionView) -> bool:
        """上游 `extractSessionVoiceCount(session)` 的等价判定。"""
        from ..core.service.helpers import extract_session_voice_count  # noqa: PLC0415

        try:
            return extract_session_voice_count(session) > 0
        except Exception:  # pragma: no cover
            return False

    # ------------------------------------------------------------------ #
    # 展示辅助（main.py 的命令文案）
    # ------------------------------------------------------------------ #

    def main_provider_label(self) -> str:
        """`interlude.status` 的"主模型连接"：上游 `providers.find(useForMain)`。"""
        providers = self.section('model').get('providers') or []
        for provider in providers:
            if not isinstance(provider, dict):
                continue
            if pick(provider, 'useForMain', 'use_for_main') and pick(provider, 'enabled') is not False:
                label = _text(pick(provider, 'label'))
                if label:
                    return label
        return '未指定（按模型配置回退）'

    @staticmethod
    def format_log_time(value: Any, timezone: str) -> str:
        """上游 `formatLogTime(result.at, story.setting.timezone)`。"""
        return format_log_time(value, timezone)

    @staticmethod
    def format_story_display_time(value: Any, timezone: str) -> str:
        """上游 `formatStoryDisplayTime(entry.occurredAt, story.setting.timezone)`。"""
        return format_story_display_time(value, timezone)

    @staticmethod
    def iso_time(value: Any) -> str:
        """上游 `date.toISOString()` 的等价物（`None` → 空串）。"""
        if value is None:
            return ''
        return iso_time_value(value) or ''

    def schedule_window_lines(self, record: Any, timezone: str) -> list[str]:
        """`interlude.schedule` 的未来 12 小时日程块文案。"""
        if not record:
            return []
        config = resolve_schedule_preplan_config(self.section('schedule_preplan'))
        window = schedule_preplan_window(record, utc_now(), timezone, 12, config)
        blocks = pick(window, 'blocks') if window is not None else None
        lines: list[str] = []
        for block in blocks or []:
            date = _text(pick(block, 'date'))
            start = _text(pick(block, 'start'))
            end = _text(pick(block, 'end'))
            kind = _text(pick(block, 'kind'))
            label = _text(pick(block, 'label'))
            location = _text(pick(block, 'location'))
            line = '%s %s-%s [%s] %s' % (date, start, end, kind, label)
            if location:
                line = '%s @ %s' % (line, location)
            lines.append(line)
        return lines

    @staticmethod
    def build_schedule_preplan_config(section: Any) -> Any:
        """`resolveSchedulePreplanConfig(config.schedulePreplan)` 的公开入口。"""
        return resolve_schedule_preplan_config(section)


def build_bridge(context: Context, config: Any = None, logger: Any = None) -> AstrbotBridge:
    """按插件默认数据目录装配 `AstrbotBridge`。"""
    return AstrbotBridge(context=context, config=config, data_dir=plugin_data_dir(), logger=logger)
