"""AstrBot 平台适配层：把 `AstrMessageEvent` / `Context` 翻译进 `plugin/core/`。

本模块是 `plugin/core/` 与 AstrBot 之间**唯一**的接缝（移植约定）。
`core/` 里任何模块都不得 `import astrbot`；平台相关的一切都在这里落地。

对应关系
--------

| 上游（Koishi） | 本模块 |
| --- | --- |
| `Session` | `session_view()` → `plugin.core.service.session.SessionView` |
| `session.bot.sendPrivateMessage` / `sendGroupMessage` / `session.send` | `AstrbotTransport` |
| `ctx.logger` / `ctx.database` / `ctx.http` / `ctx.setTimeout` | `AstrbotInterludeContext`（内部用 `InterludeContext`） |
| `apply(ctx, config)` | `AstrbotBridge`（`main.py` 的 `Star` 子类持有它） |

键名约定
------------------------------

发给模型的 payload、数据库列名保持上游 camelCase（由 `core/` 负责）；
本模块只做两件事：把 AstrBot 的对象翻成 `core/` 要的 snake_case 视图，
以及把 `core/` 的 camelCase 出站内容翻成 AstrBot 消息链。
从外部读入的行（数据库行 / 参与者 dict）一律用 `pick()` 双读。

降级路径清单（移植约定）
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
import json
import os
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Iterable, Mapping, Optional
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
from ..core import platform_actions as platform_action_catalog
from ..core.database import Database
from ..core.forward_message import (
    DEFAULT_LIMITS as FORWARD_DEFAULT_LIMITS,
    ForwardReadResult,
    extract_forward_ids,
    failure_result as forward_failure_result,
    forward_read_ids,
    forward_read_limits,
)
from ..core.model_routing import match_usable_connection_row
from ..core.narrator import HttpxHttpClient
from ..core.schedule_preplan import resolve_schedule_preplan_config, schedule_preplan_window
from ..core.service import (
    InterludeContext,
    InterludeService,
    SessionView,
    log_fallback,
    pick,
)
#: 媒体种类与"像不像表情包"的**结构信号**都住在 core（§49.1）：适配层判 kind 时调
#: 同一个 `sticker_media_signal`，第二层预筛 `sticker_guess_candidate` 也是它 —— 一份实现两处用。
from ..core.service.helpers import STICKER_CANDIDATE_KIND, sticker_media_signal
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
    'forward_read_context',
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


def _plugin_version() -> str:
    """本插件自身版本（`metadata.yaml` 的 `version`）。

    读不到就回落 `core/meta.py` 的**上游版本**常量——导出的信封里这个字段只是给人看的，
    不值得为它抛异常。
    """
    from ..core.meta import HDS_INTERLUDE_VERSION  # noqa: PLC0415

    path = Path(__file__).resolve().parent.parent / 'metadata.yaml'
    try:
        text = path.read_text(encoding='utf-8')
    except OSError:
        return HDS_INTERLUDE_VERSION
    match = re.search(r'^version:\s*(\S+)\s*$', text, re.MULTILINE)
    return match.group(1) if match else HDS_INTERLUDE_VERSION


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
    """组件类型名（小写类名，跨 AstrBot 版本稳定，不依赖 `ComponentType` 枚举）。

    `_RawSegment` 这类"把原始段包成组件形状"的包装会自带 `_hdsi_kind`：它的类名
    不是段类型（`_rawsegment`），照类名判会全部落到未知分支。
    """
    hint = getattr(component, '_hdsi_kind', None)
    if hint:
        return str(hint).lower()
    return type(component).__name__.lower()


#: OneBot/NapCat 原始段类型 → 本模块 `serialize_component` 认的组件类型名。
_RAW_SEGMENT_ALIASES: dict[str, str] = {
    'text': 'plain', 'plain': 'plain',
    'image': 'image', 'img': 'image',
    'record': 'record', 'audio': 'record', 'voice': 'record',
    'video': 'video',
    'at': 'at',
    'face': 'face',
    # `mface`（QQ 商城表情）**不能冒充 `face`**：它带 summary，而且 AstrBot 的
    # `AiocqhttpMessageAdapter` 直接把 `mface` 段 `continue` 掉，结构化链里根本没有它。
    'mface': 'mface',
    'file': 'file',
    'reply': 'reply',
    'json': 'json',
}


class _RawSegment:
    """把原始消息段（`{'type': 'image', 'data': {...}}`）包成组件形状。

    为什么需要它：AstrBot 的 `message_obj.message`（结构化消息链）在某些链路上会是空的
    ——用户实测"图片 + 文字分两条发"时，图片那条事件的链为空，于是我们既看不到图、
    又把事件漏给了宿主默认 Agent。而 `message_obj.raw_message` 里**仍然留着**适配器的
    原始段（aiocqhttp 直接放了 OneBot 事件对象），把它包一层就能继续走同一条序列化路径。
    """

    def __init__(self, kind: str, data: Any = None) -> None:
        self._hdsi_kind = kind
        self.type = kind
        payload = data if isinstance(data, dict) else {}
        self.data = payload
        for key, value in payload.items():
            if isinstance(key, str) and not hasattr(self, key):
                setattr(self, key, value)


def _session_file_facts(session: Any) -> list[Any]:
    """`extract_session_file_facts(session)` 的弱调用版（诊断用，失败就当空）。"""
    try:
        from ..core.service.helpers import extract_session_file_facts  # noqa: PLC0415

        return list(extract_session_file_facts(session) or [])
    except Exception:  # pragma: no cover - 诊断路径不抛
        return []


def _raw_segments(event: Any) -> list[dict[str, Any]]:
    """从 `message_obj.raw_message` 里取出 OneBot 原始段（取不到就回空列表）。"""
    raw = getattr(getattr(event, 'message_obj', None), 'raw_message', None)
    if raw is None:
        return []
    segments = raw.get('message') if isinstance(raw, dict) else getattr(raw, 'message', None)
    if segments is None and isinstance(raw, (list, tuple)):
        segments = raw
    if not isinstance(segments, (list, tuple)):
        return []
    return [segment for segment in segments if isinstance(segment, dict)]


def _raw_segment_chain(event: Any) -> list[Any]:
    """从 `message_obj.raw_message` 里取出原始段并包成组件（取不到就回空列表）。"""
    chain: list[Any] = []
    for segment in _raw_segments(event):
        kind = _RAW_SEGMENT_ALIASES.get(_text(segment.get('type')).lower())
        if not kind:
            continue
        chain.append(_RawSegment(kind, segment.get('data')))
    return chain


#: OneBot `image` 段的 `sub_type`：**QQ 的 `picSubType` 原值**，这是唯一的机器可读信号
#: （AstrBot 的 `Image` 组件只留 `file`/`url`/`path`，`sub_type` 与 `summary` 在 pydantic
#: 解析层就被丢掉，只能回原始段取）。
#:
#: **三档表**（不许猜；来源逐条可核，快照见 `docs/PORTING_NOTES.md` §49.1）：
#:
#: | sub_type | NapCat 枚举名 | 语义 | 我们怎么用 | 依据 |
#: | --- | --- | --- | --- | --- |
#: | 0 | `KNORMAL` | 普通图 | `image`（不收） | 名字自述 |
#: | 1 | `KCUSTOM` | 自定义图片（收藏表情等） | `sticker`（**确定档：观测到就是表情**） | 名字自述 |
#: | 2 | `KHOT` | 热门图 / 平台推荐的热门表情 | **候选档**（`sticker-candidate`，过结构检查才收） | 用户给的表 |
#: | 3 | `KDIPPERCHART` | 斗图 / 表情包相关 | **候选档** | 用户给的表 |
#: | 4 | `KSMART` | 智能图片（智能裁剪 / 滤镜 / 以图搜图） | `image`（不收） | 用户给的表 |
#: | 5 | `KSPACE` | QQ 空间图片（相册分享到聊天） | `image`（不收；**实拍照片，绝不能当表情收**） | 用户给的表 |
#: | 6 | `KUNKNOW` | 未知 | `image`（不收） | 名字自述 |
#: | 7 | `KRELATED` | 关联图片（输入文字时弹出的关联图 / **用表情搜索搜出来的表情包**） | **候选档** | 用户给的表 + 真机样本 |
#:
#: 枚举来源：NapCat 源码 `packages/napcat-core/types/msg.ts` 的 `enum PicSubType`
#: （快照 commit `26d7533e0f5800fdff865ab2f2ad7692917e1076`，2026-09-29）；
#: OneBot 段的取值点：`packages/napcat-onebot/api/msg.ts:142`（`sub_type: element.picSubType`）；
#: 缺省与占位口径：`packages/napcat-core/packet/message/element.ts:369`
#: （`picSubType ?? 0`）与 `:372`（summary 为空时 `picSubType === 0 ? '[图片]' : '[动画表情]'`
#: —— **平台自己把非 0 一律当表情**）。NapCat 接口文档只写「图片子类型 number」，
#: **没有枚举表**：https://napcat.apifox.cn/246111200d0.md 。
#:
#: 一档与二档的区别（一句话）：**一档是"观测到确实是表情"，二档是"平台标了候选、
#: 但我们不认它的语义，必须再过一道结构检查"**（方括号名字 / GIF / alpha PNG /
#: 近方形小图，唯一实现在 `helpers.sticker_media_signal`）。
#:
#: ⚠️ **`4 → market` 已在 v1.8.4 撤回**（§49.1）：它自 v1.4.2 起就只是猜测，与
#: "未知平台语义一律不收"的纪律冲突。真正的商城表情走 `mface` 段（另一条链，§29）。
#: **已经收进库的旧 `market` 素材不动**（那是既往事实，删它才是静默毁数据）；
#: `kind` 词表里的 `market` 也保留 —— 别的来源（老适配器 / 桌面桥 / 手搓 media）
#: 仍可能这么标注，收藏判据不收窄。
_ONEBOT_IMAGE_SUB_TYPES = {'0': 'image', '1': 'sticker'}

#: **候选档**：平台标了"可能是表情"，但没到"观测到确实是"的程度 —— 必须再过一道
#: 结构检查（`helpers.sticker_media_signal`）才决定收不收（§49.1 的第二档）。
_ONEBOT_IMAGE_CANDIDATE_SUB_TYPES = frozenset({'2', '3', '7'})

#: `sub_type` → NapCat `PicSubType` 枚举名（**只用于文档 / 诊断**，不参与判据）。
_ONEBOT_IMAGE_SUB_TYPE_NAMES = {
    '0': 'KNORMAL', '1': 'KCUSTOM', '2': 'KHOT', '3': 'KDIPPERCHART',
    '4': 'KSMART', '5': 'KSPACE', '6': 'KUNKNOW', '7': 'KRELATED',
}

#: 入站媒体"再取一次字节"登记表的容量（§49.3）。收藏只在入站当次发生，所以不需要
#: 记很久：64 条足够覆盖"一次翻出十几张表情"的连发，又不会把事件对象长期留住。
_INCOMING_MEDIA_LIMIT = 64

#: 单次"按入站坐标取图片字节"的超时。比 `Transport.fetch_image` 宽松：这条路上
#: 要做的事更多（宿主下载器 / OneBot 动作），但**必须**有上限 —— 收藏是旁路，
#: 不许把事件循环挂住（core 侧还有一层 `STICKER_FETCH_TIMEOUT_SECONDS`）。
_INCOMING_IMAGE_TIMEOUT_SECONDS = 20.0


def _image_media_kind(data: Any) -> tuple[str, str]:
    """从 OneBot 图片段的 data 里读出 `(kind, summary)`（三档表见上面那段注释）。

    * **确定档**（`sub_type` 0 / 1）：直接定 `image` / `sticker`；
    * **候选档**（`2` / `3` / `7`）：先用**只有名字**的那一半结构信号判一次
      （方括号名字如 `[中午好]` —— **入站就有、不用下载**）：命中 → `sticker`；
      不命中 → `sticker-candidate`，留给收藏路径拿到字节后过同一把尺子的其余信号
      （GIF / 带 alpha 的 PNG / 近方形小图）。判据实现只有一处：
      `helpers.sticker_media_signal`（第二层的 `sticker_guess_candidate` 也是它）；
    * **不收档**（`4` / `5` / `6` / 未知 / 缺字段）：回 `image`（"拿不准=没有"）。

    `summary` 含「动画」时升级 `animated`：这是 **NapCat 自己的占位口径**
    （`packet/message/element.ts:372` 把非 0 的 `picSubType` 一律写成 `[动画表情]`），
    不是我们猜的。升级只发生在两处：**普通图**（`image` → `animated`，§29 起就有的行为）
    与**候选档命中名字的**那一种（`2/3/7` + `[动画表情]`）；确定档 `sub_type=1` 保持
    `sticker` 不变（用户口径："1 → sticker，今天的行为不变"）。

    真机那条 `sub_type=7` + `summary='[中午好]'` 因此是 `sticker`（候选档 + 名字信号命中）
    —— 旧版本把它当成普通图，那正是它收不进来的原因（§49.2）。
    """
    payload = data if isinstance(data, dict) else {}
    summary = _text(payload.get('summary'))
    sub_type = _text(payload.get('sub_type'))
    kind = _ONEBOT_IMAGE_SUB_TYPES.get(sub_type, '')
    from_candidate = False
    if not kind and sub_type in _ONEBOT_IMAGE_CANDIDATE_SUB_TYPES:
        from_candidate = True
        # 没有字节时唯一可能命中的信号就是"方括号名字"（GIF/alpha/尺寸都要字节）。
        kind = 'sticker' if sticker_media_signal(name=summary) else STICKER_CANDIDATE_KIND
    if not kind:
        kind = 'image'
    if '动画' in summary and (kind == 'image' or (from_candidate and kind == 'sticker')):
        kind = 'animated'
    return kind, summary


def _mface_attrs(data: Any) -> dict[str, str]:
    """QQ 商城表情的可见属性（name 优先取 summary）。"""
    payload = data if isinstance(data, dict) else {}
    attrs: dict[str, str] = {}
    for key, source in (('id', 'emoji_id'), ('package', 'emoji_package_id'), ('summary', 'summary'), ('name', 'name')):
        value = _text(payload.get(source))
        if value:
            attrs[key] = value
    return attrs


def raw_media_hints(event: Any) -> dict[str, Any]:
    """从 OneBot 原始段里捞出**结构化消息链丢掉**的媒体信号。

    为什么必须回原始段：
    * AstrBot 的 `Image` 组件只保留 `file`/`url`/`path`，`sub_type`（表情包与否）
      与 `summary` 在 pydantic extra 里被丢掉 —— 于是"表情包"和"实拍照片"在下游
      长得一模一样；
    * `mface`（QQ 商城表情）被适配器 `continue` 掉，**整段消失**，连占位都没有。

    返回：`images`（按 `file`/`url` 索引的 kind+summary+原始段 data）、
    `image_order`（图片段按出现顺序的同一批条目 —— 组件与段**链接不上**时的位置兜底，
    见 `serialize_message_chain`，§46.9）、`faces`（表情 id → 文本）、
    `extras`（结构化链里不存在的段的元素定义，按原文顺序）、
    `forwards`（合并转发的资源 id 列表）。

    **`forwards` 为什么必须回原始段取**：AstrBot 的 `Forward` 组件只有 `id`，而
    `get_message_str()` 把它渲染成 `[转发消息]`（**不带 id**）——只按消息文本抠 id 的话
    永远读不到正文。`message_obj.raw_message` 里的 `{'type': 'forward', 'data': {'id': …}}`
    是唯一还带 id 的地方（和图片的 `sub_type` 是同一条路子）。
    """
    hints: dict[str, Any] = {
        'images': {}, 'image_order': [], 'faces': {}, 'extras': [], 'forwards': [],
    }
    for segment in _raw_segments(event):
        kind = _text(segment.get('type')).lower()
        data = segment.get('data')
        payload = data if isinstance(data, dict) else {}
        if kind == 'image':
            media_kind, summary = _image_media_kind(payload)
            entry = {'kind': media_kind, 'summary': summary, 'raw': dict(payload)}
            hints['image_order'].append(entry)
            for key in ('file', 'url'):
                value = _text(payload.get(key))
                if value:
                    hints['images']['%s:%s' % (key, value)] = entry
        elif kind == 'face':
            face_id = _text(payload.get('id') if payload.get('id') is not None else payload.get('faceIndex'))
            face_text = _text(payload.get('faceText') or payload.get('text'))
            if face_id and face_text:
                hints['faces'][face_id] = face_text
        elif kind == 'mface':
            hints['extras'].append({'type': 'mface', 'attrs': _mface_attrs(payload)})
        elif kind == 'forward':
            for key in ('id', 'res_id', 'forward_id'):
                value = _text(payload.get(key))
                if value and value not in hints['forwards']:
                    hints['forwards'].append(value)
    return hints


#: OneBot 的非消息事件（`post_type` 不是 `message`）：通知 / 元事件 / 请求。
#: 宿主也会把它们派到消息处理器上，最典型的是 NapCat 的 `input_status`
#: （「对方正在输入…」与停止输入），实测一分钟能来十几条。
_NON_MESSAGE_POST_TYPES = frozenset({'notice', 'meta_event', 'request'})


def is_non_message_event(event: Any) -> tuple[bool, str]:
    """这条事件是不是 OneBot 的非消息事件；返回 `(是否非消息, 类型标签)`。

    标签用来打 debug 日志（`notice:notify:input_status` 比 `post_type=notice` 有用）。
    取不到 `post_type` 就一律当成消息——telegram / webchat / 官方 QQ 的原始事件里
    没有这个键，**绝不能误伤**。
    """
    raw = getattr(getattr(event, 'message_obj', None), 'raw_message', None)
    if not isinstance(raw, dict):
        return False, ''
    post_type = _text(raw.get('post_type')).lower()
    if post_type not in _NON_MESSAGE_POST_TYPES:
        return False, ''
    kind = _text(raw.get('notice_type') or raw.get('meta_event_type') or raw.get('request_type'))
    sub_type = _text(raw.get('sub_type'))
    label = post_type + ((':' + kind) if kind else '') + ((':' + sub_type) if sub_type else '')
    return True, label


# =========================================================================== #
# AstrBot 消息链 → Koishi `session.content` / `elements`
# =========================================================================== #

def _json_card_attrs(component: Any) -> dict[str, str]:
    """QQ 小程序 / 分享卡片 → 可见属性（`app` / `title` / `desc` / `prompt`）。

    卡片原先只会落成裸 `<json/>`：模型既不知道那是张卡片，也不知道卡片是什么内容，
    只能当成"他发了点什么"含糊过去。这里把平台给的标题与描述取出来（都截断），
    让下游能说清"他转了个农场小程序的宝箱分享"。
    """
    import json as _json

    raw = getattr(component, 'data', None)
    if isinstance(raw, str):
        try:
            raw = _json.loads(raw)
        except Exception:  # noqa: BLE001 - 解析不了就只留类型名
            raw = {}
    payload = raw if isinstance(raw, dict) else {}
    attrs: dict[str, str] = {}
    app = _text(payload.get('app') or payload.get('appID'))
    if app:
        attrs['app'] = app[:60]
    prompt = _text(payload.get('prompt'))
    if prompt.startswith('[QQ小程序]'):
        prompt = prompt[len('[QQ小程序]'):]
    if prompt:
        attrs['prompt'] = prompt[:80]
    detail = payload.get('meta')
    if isinstance(detail, dict):
        for value in detail.values():
            if not isinstance(value, dict):
                continue
            title = _text(value.get('title'))
            desc = _text(value.get('desc'))
            if title:
                attrs['title'] = title[:60]
            if desc:
                attrs['desc'] = desc[:80]
            if title or desc:
                break
    return attrs


def _observed_image_data(component: Any) -> dict[str, Any]:
    """一张图**观测到的原始判据**（原始段 data，加上组件上还留着的种类字段）。

    进 `session.media[].raw`。留它是为了"以后要加判据时不必回消息文本里找"——
    文本是用户可写的，判据只能来自适配器看见的段。
    """
    observed: dict[str, Any] = {}
    payload = _attr(component, 'data')
    if isinstance(payload, dict):
        observed.update(payload)
    for key in ('sub_type', 'subType', 'summary', 'file', 'url'):
        value = _attr(component, key)
        if value in (None, '') or key in observed:
            continue
        observed[key] = value
    return observed


def _media_source_from_attrs(attrs: Any, path: Any = None) -> str:
    """从**刚写出来的** `<img>` 属性里取来源（媒体条目的 `source`）。

    优先级与 `core/service/chunk3._fallback_extract_session_image_sources`
    从标签里读出来的那一套一致（`src` → `url` → `onebot-file:`）。

    **本地落盘图（`Image(path=…)`）归一成 `onebot-file:<路径>`**（§46.9）：core 的
    图片来源（`chunk3._structured_image_sources`）把本地路径读成 `onebot-file:…`，
    媒体这一侧必须写**同一个字面量**，否则"按 `source` 对齐种类"对不上 ——
    宿主把别人发来的收藏表情落成本地文件时，私聊就永远收不到它（§46.9 修正）。
    """
    if not isinstance(attrs, dict):
        return ''
    src = _text(attrs.get('src')).strip()
    url = _text(attrs.get('url')).strip()
    file_value = _text(attrs.get('file')).strip()
    if src:
        if src.lower().startswith('file://'):
            local = os.path.abspath(_text(path)) if path else unquote(urlparse(src).path)
            return ('onebot-file:%s' % local) if local else ''
        return src
    if url:
        return url
    return 'onebot-file:%s' % file_value if file_value else ''


def _media_source_kind(source: str) -> str:
    """来源坐标属于哪一类（`url` / `file` / `path` / `data`）——给人看的判据。"""
    if source.startswith('onebot-file:'):
        return 'file'
    lowered = source.lower()
    if lowered.startswith('data:'):
        return 'data'
    if lowered.startswith('file://'):
        return 'path'
    return 'url'


def _local_image_path(value: Any) -> str:
    """把一个坐标归一成**此刻确实可读的本地图片路径**；不是本地文件就回空串。

    只认三类（其余一律回空串，**不去 CWD 里撞运气**）：

    * `onebot-file:<路径>` / 裸 `file://` URI（`%20` 之类要解码）；
    * 绝对路径（`/…` 或 `C:\\…`）**且文件真的在**；
    * 上面两类解出来的路径 `os.path.isfile()` 为真。

    为什么要它（§49.3）：入站图片的 URL 里带的是短效 `rkey`，而宿主（AstrBot 的
    `Image(path=…)` / 别的适配器落盘）手上可能**已经有本地文件** —— 那是最稳的字节
    来源：不发网络请求、也不受 `rkey` 过期影响。判断"到底是不是本地文件"只能靠
    这里一次 `isfile()`，别让下游各自猜前缀。
    """
    text = _text(value).strip()
    if not text:
        return ''
    if text.startswith('onebot-file:'):
        text = text[len('onebot-file:'):]
    if text.lower().startswith('file://'):
        try:
            text = unquote(urlparse(text).path)
        except Exception:  # noqa: BLE001 - 解不开就按原文试
            text = text[len('file://'):]
    # Windows 的 `file:///C:/x.png` 会解出 `/C:/x.png`：把多出来的前导斜杠去掉。
    if re.match(r'^/[A-Za-z]:[\\/]', text):
        text = text[1:]
    if not (os.path.isabs(text) or re.match(r'^[A-Za-z]:[\\/]', text)):
        return ''
    try:
        return os.path.abspath(text) if os.path.isfile(text) else ''
    except OSError:  # pragma: no cover - 路径里有非法字符（Windows）
        return ''


def _decode_inline_image(value: Any) -> Optional[bytes]:
    """解出内联载荷（`data:image/…;base64,…` / AstrBot 的 `base64://…`）；否则 `None`。

    AstrBot 的 `Image.fromBase64()` 写的就是 `base64://…`（组件 `file` 字段），
    而 `Image.fromBytes` / 别的平台适配器可能给 `data:` URI —— 两种都在这里认掉。
    """
    text = _text(value).strip()
    if not text:
        return None
    prefix = 'data:' if text.lower().startswith('data:') else ('base64://' if text.startswith('base64://') else '')
    if not prefix:
        return None
    payload = text.partition(',')[2] if prefix == 'data:' else text[len('base64://'):]
    if not payload:
        return None
    try:
        return base64.b64decode(payload)
    except Exception:  # noqa: BLE001 - 坏 base64 按拿不到字节处理
        return None


#: 下载用的 `User-Agent`。**不是** httpx 默认的 `python-httpx/x.y`：图床/CDN 把
#: 非客户端 UA 当爬虫挡掉是常态。这是**预防性补齐**（见 `AstrbotBridge.http_get_bytes`
#: 的口径说明：没有有效 `rkey` 就没法在真机外核实"加了就一定下得来"）。
_DOWNLOAD_USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'
)

#: 下载超时（秒）。httpx 的默认值是 **5 秒**（实测 `httpx.AsyncClient().timeout`
#: = `Timeout(timeout=5.0)`）—— QQ 图床握手 + 17KB 图在慢网络上完全可能超过它，
#: 而超时在旧代码里只会变成一句 debug。
_DOWNLOAD_TIMEOUT_SECONDS = 20.0

#: 腾讯系的图床链带一个来源页（`multimedia.nt.qq.com.cn` / `gchat.qpic.cn` 之类）。
_DOWNLOAD_REFERER = 'https://im.qq.com/'

#: 认哪些域名要带 `Referer`（后缀匹配，含子域）。
_DOWNLOAD_REFERER_SUFFIXES = ('.qq.com', '.qq.com.cn', '.qpic.cn', '.gtimg.cn')


def _download_headers(url: str) -> dict[str, str]:
    """下载请求头：常见客户端 UA；腾讯图床链另带 `Referer`（§49.2）。"""
    headers = {'User-Agent': _DOWNLOAD_USER_AGENT, 'Accept': '*/*'}
    try:
        host = (urlparse(_text(url)).hostname or '').lower()
    except Exception:  # noqa: BLE001 - 解不出域名就当普通地址
        host = ''
    if any(host.endswith(suffix) or host == suffix[1:] for suffix in _DOWNLOAD_REFERER_SUFFIXES):
        headers['Referer'] = _DOWNLOAD_REFERER
    return headers


def _image_component_for_raw(event: Any, raw: Any) -> Any:
    """按 `file` / `url` 把原始图段对回 AstrBot 的 `Image` 组件（对不上回 `None`）。

    对上了就能用宿主**官方**的取字节方法（`Image.convert_to_file_path()` /
    `convert_to_base64()`，见 §49.3）—— 它自带 certifi 根证书、SSL 失败降级与
    长超时，比我们自己发请求稳。对不上**不猜**：按文件 / URL 字面量相等匹配，
    这也正是媒体表与原始段的链接方式（`raw_media_hints` 的键同源）。
    """
    if event is None or not isinstance(raw, dict):
        return None
    file_token = _text(raw.get('file')).strip()
    url = _text(raw.get('url')).strip()
    if not file_token and not url:
        return None
    chain = _call(event, 'get_messages', []) or []
    for component in chain:
        if _component_kind(component) not in ('image', 'img'):
            continue
        if file_token and _text(_attr(component, 'file', 'file_')).strip() == file_token:
            return component
        if url and _text(_attr(component, 'url')).strip() == url:
            return component
    return None



def _sole_image_hint(chain: list[Any], hints: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """「只有一个图段、也只有一个图片组件」时那个原始段条目（否则 `None`）。

    为什么需要它（§46.9）：组件与原始段是靠 `file` / `url` 字符串链接的，而宿主把图片
    **落到本地**时组件可能两者都没有（只有 `path`）—— 链接断了，`kind` 就丢，收藏表情
    会被当成普通照片（私聊永远收不到）。"整条消息只有一个图段"是唯一不需要**排序假设**
    的情形（与 `extras` 那条注释同一条理由：只有一个时位置本来就是对的）。

    段数或组件数不是 1 时一律回 `None`：适配器丢段 / 补段时位置就不可信，**不猜**。
    """
    if not isinstance(hints, dict):
        return None
    order = hints.get('image_order')
    if not isinstance(order, list) or len(order) != 1:
        return None
    if sum(1 for component in chain if _component_kind(component) in ('image', 'img')) != 1:
        return None
    return order[0] if isinstance(order[0], dict) else None


def serialize_component(
    component: Any,
    hints: Optional[dict[str, Any]] = None,
    media: Optional[list[dict[str, Any]]] = None,
    sole_image: Optional[dict[str, Any]] = None,
) -> tuple[str, Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    """把一个 AstrBot 组件翻成 `(content 片段, element, quote)`。

    `element` 是 `h.parse(session.content)` 的等价结构（`type` / `attrs` / `children`），
    `quote` 只在 `Reply` 组件上产生（上游 `session.quote`）。
    返回的 `content` 片段与上游 Koishi 的序列化形式一致：文本原样，元素写成
    自闭合标签，`core/service/helpers.py` 的 `_parse_mini_xml_elements` 直接可读。

    `media` 非 `None` 时，图片 / 卡片会往这个列表里追加**结构化媒体条目**
    （见 `serialize_message_chain`）：种类与来源只在这里判定一次，core 不再回读
    `<img>` 文本（`docs/PORTING_NOTES.md` §46）。

    `sole_image` 是"这条消息只有一个图段、也只有一个图片组件"时那个原始段条目
    （由 `serialize_message_chain` 判定）：组件按 `file`/`url` **链接不上**时（宿主把图
    落到本地、组件没有 url/file token）用它兜底，否则种类会丢（§46.9）。
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
        # 媒体种类（照片 / 表情包 / 动图 / 商城表情）只能回原始段取：AstrBot 的
        # `Image` 组件把 `sub_type` 与 `summary` 丢在 pydantic extra 里了。
        media_kind, summary = _image_media_kind({
            'sub_type': _attr(component, 'sub_type', 'subType'),
            'summary': _attr(component, 'summary'),
        })
        observed = _observed_image_data(component)
        lookup = hints.get('images') if isinstance(hints, dict) else None
        linked = False
        if isinstance(lookup, dict):
            for key in ('file:%s' % _text(file_value), 'url:%s' % url):
                found = lookup.get(key)
                if isinstance(found, dict):
                    media_kind = _text(found.get('kind')) or media_kind
                    summary = _text(found.get('summary')) or summary
                    if isinstance(found.get('raw'), dict):
                        observed = dict(found['raw'])
                    linked = True
                    break
        if not linked and isinstance(sole_image, dict):
            # 链接不上但这条消息只有一个图段 ⇒ 那就是它（不需要排序假设；§46.9）。
            media_kind = _text(sole_image.get('kind')) or media_kind
            summary = _text(sole_image.get('summary')) or summary
            if isinstance(sole_image.get('raw'), dict):
                observed = dict(sole_image['raw'])
        if media_kind and media_kind != 'image':
            attrs['kind'] = media_kind
        if summary:
            attrs['summary'] = summary
        if media is not None:
            source = _media_source_from_attrs(attrs, path)
            # 宿主手上**已经有本地文件**时，字节坐标优先用它（§49.3）：URL 里的 `rkey`
            # 会过期、还要再发一次网络请求，而本地文件读一次就完事。只改结构化这一侧 ——
            # 正文标签（`<img src=…>`，给模型看的文本）与既有坐标字面量都不动。
            local = _local_image_path(path) or _local_image_path(file_value)
            if local and not source.startswith('onebot-file:'):
                source = 'onebot-file:%s' % local
            if source:
                media.append({
                    'kind': media_kind or 'image',
                    'source': source,
                    'source_kind': _media_source_kind(source),
                    'summary': summary,
                    'raw': observed,
                })
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

    if kind == 'face':
        face_id = _attr(component, 'id', 'face_id', 'faceIndex')
        attrs = {'id': _text(face_id)}
        # 表里没有的新表情只能靠平台给的 faceText；有就带上，别让模型去猜 ID。
        name = _text(_attr(component, 'faceText', 'text', 'name'))
        if not name and isinstance(hints, dict):
            name = _text((hints.get('faces') or {}).get(attrs['id']))
        if name:
            attrs['name'] = name
        tag = ' '.join('%s="%s"' % (key, _escape_attr(value)) for key, value in attrs.items() if value != '')
        return ('<face %s/>' % tag) if tag else '<face/>', {'type': 'face', 'attrs': attrs, 'children': []}, None

    if kind == 'mface':
        attrs = _mface_attrs(_attr(component, 'data')) if _attr(component, 'data') is not None else {}
        for key in ('id', 'package', 'summary', 'name'):
            value = _text(_attr(component, key))
            if value and not attrs.get(key):
                attrs[key] = value
        tag = ' '.join('%s="%s"' % (key, _escape_attr(value)) for key, value in attrs.items() if value != '')
        return ('<mface %s/>' % tag) if tag else '<mface/>', {'type': 'mface', 'attrs': attrs, 'children': []}, None

    if kind == 'json':
        attrs = _json_card_attrs(component)
        if media is not None:
            # 卡片没有可下载来源，但也**只有适配器看得见**：正文里手打的 `<card …/>`
            # 不再是媒体条目（与 `<img>` 同一条纪律）。
            media.append({
                'kind': 'card',
                'source': '',
                'source_kind': '',
                'summary': '',
                'raw': dict(attrs),
            })
        tag = ' '.join('%s="%s"' % (key, _escape_attr(value)) for key, value in attrs.items() if value != '')
        return ('<card %s/>' % tag) if tag else '<card/>', {'type': 'card', 'attrs': attrs, 'children': []}, None

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
    if kind == 'forward':
        # 合并转发：**把资源 id 写进标记**。AstrBot 的 `Forward` 组件只有 `id`，而
        # `get_message_str()` 渲染成 `[转发消息]`（id 丢失）——标记里带上 id 之后，
        # 卡片的可见线索与"读了正文要贴在哪"都有据可依（见 `forward_read_context`）。
        identifier = _text(_attr(component, 'id', 'res_id', 'forward_id'))
        if identifier:
            attrs['id'] = identifier
    url = _attr(component, 'url')
    if _downloadable(url):
        attrs['src'] = _text(url)
    tag = ' '.join('%s="%s"' % (key, _escape_attr(value)) for key, value in attrs.items() if value != '')
    literal = '<%s %s/>' % (kind, tag) if tag else '<%s/>' % kind
    return literal, {'type': kind, 'attrs': attrs, 'children': []}, None


def serialize_message_chain(
    chain: Iterable[Any],
    hints: Optional[dict[str, Any]] = None,
    media: Optional[list[dict[str, Any]]] = None,
) -> tuple[str, list[dict[str, Any]], Optional[dict[str, Any]]]:
    """把整条 AstrBot 消息链翻成 `(content, elements, quote)`。

    `content` 是上游 `session.content` 的等价物——`core/service/helpers.py` 会按
    Koishi 的 mini-xml 语法重新解析它（图片 / 语音 / 文件事实、`<at>` 提及检测），
    所以文本必须原样保留、元素必须写成自闭合标签。

    `media` 非 `None` 时**顺带**收一份结构化媒体表（`session_view` 就是这么用的）：

    ``[{'kind', 'source', 'source_kind', 'summary', 'raw'}, …]``

    * `kind`：观测到的种类（`image` / `sticker` / `animated` / `market` / `card`），
      来自 OneBot 原始段的 `sub_type` / `summary`（§29）；
    * `source`：归一化来源，与 `extract_session_image_sources` 从标签里读出来的
      那个字符串逐字一致（"按来源对齐种类"靠它）；卡片为空串；
    * `raw`：这条媒体**观测到的原始判据**（原始段 data / 卡片属性）。

    ⚠️ 纪律：core 只认这一份，**绝不**再从 `content` 的 `<img kind=…>` 反解析
    （那等于把"用户手打的文本"当判据，`docs/PORTING_NOTES.md` §46）。
    """
    parts: list[str] = []
    elements: list[dict[str, Any]] = []
    quote: Optional[dict[str, Any]] = None
    items = [component for component in (chain or []) if component is not None]
    sole_image = _sole_image_hint(items, hints)
    for component in items:
        fragment, element, quoted = serialize_component(component, hints, media, sole_image)
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


def forward_read_context(content: Any, read: Optional[ForwardReadResult]) -> str:
    """把读到的合并转发正文**贴到**原有卡片文案后面（纯函数，无副作用）。

    为什么要"贴"而不是"换"：`serialize_message_chain` 本来就会把 `forward` 组件写成
    `<forward id="…"/>` 这种最小标记（下游的附件事实路径靠它），而用户要求的可见线索
    ——"这是一条合并转发"——正是这个标记。**两边都要留**：

    * 读到了 → `<forward id="x"/>\n[合并转发内容｜节点数 2]…`；
    * 读不到（`read.failed`）→ 卡片标记原样留下，**绝不**把失败占位文案塞进消息正文：
      适配层已经为此打了 warn，而卡片标记本身就是那句可见线索——"有一条合并转发、
      读不到内容"（移植任务书原话）。原内容为空时补一个 `<forward />`：空串会被
      `handle_event` 当成"没有可用内容"，整条消息就消失了——那正是要避免的结果。
    * 压根不认（`read is None`）→ 原样返回。
    """
    text = '' if content is None else str(content)
    if read is None:
        return text
    if read.failed:
        return text or '<forward />'
    if not text:
        return read.content
    return '%s\n%s' % (text, read.content)


def session_view(
    event: AstrMessageEvent,
    endpoint: Optional[AstrbotEndpoint] = None,
    forward_read: Optional[ForwardReadResult] = None,
) -> SessionView:
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
    | `content` | `serialize_message_chain()`（Koishi mini-xml 形式）+ `forward_read` 的正文 |
    | `elements` | 同上，结构化段列表 |
    | `media` | 同上，结构化媒体表（种类 / 来源 / 摘要 / 原始判据；§46）；只有 `message_str` 兜底时交 `None` |
    | `quote` | `Reply` 组件（上游 `session.quote`） |
    | `messageId` | `event.message_obj.message_id` |
    | `username` | `event.get_sender_name()` |
    | `event` | 原始 `AstrMessageEvent` 引用 |

    `forward_read` 是 `AstrbotBridge.read_forward_for_event()` 已经读回来的合并转发正文
    （读不到就别传 `None`，见那条路径的失败分支）：**只由它决定注入形态**，本函数
    不做任何 await、也不自己发请求。
    """
    resolved = endpoint if endpoint is not None else endpoint_for_event(event)
    hints = raw_media_hints(event)
    chain = _call(event, 'get_messages', []) or []
    # 结构化媒体表：**只在这里**（同一条序列化走查里）产生 —— 种类来自原始段的
    # `sub_type` / `summary`，来源来自适配器组件（`docs/PORTING_NOTES.md` §46）。
    # `None` 只表示"这个宿主根本没有观测通道"（见下面的 `message_str` 兜底）。
    media: Optional[list[dict[str, Any]]] = []
    content, elements, quote = serialize_message_chain(chain, hints, media)
    used_raw_chain = False
    if not content and not elements:
        # 结构化链是空的：退回适配器的原始段（见 `_raw_segment_chain`）。
        raw_chain = _raw_segment_chain(event)
        if raw_chain:
            media = []  # 前面那份属于空链，作废重来（不许把两份拼起来）
            content, elements, quote = serialize_message_chain(raw_chain, None, media)
            used_raw_chain = True
    if not used_raw_chain and hints.get('extras'):
        # 结构化链**存在但缺段**：适配器 `continue` 掉的段（QQ 商城表情）只能在这里补。
        # 顺序上它们落在文末——混合消息里位置会略偏，但"她确实收到一个商城表情"
        # 这条事实比顺序精确更重要（整条消息只有一个表情时位置本来就是对的）。
        # 这些补齐段都不是图片，因此不产生媒体条目（`mface` 是另一种东西）。
        for extra in hints['extras']:
            fragment, element, _quoted = serialize_component(_RawSegment(extra['type'], extra['attrs']))
            if fragment:
                content = (content + fragment) if content else fragment
            if element is not None:
                elements.append(element)
    if not content:
        # 有些适配器只填 `message_str`（纯文本）而没有结构化段：**没有观测通道**。
        # 媒体表交 `None`：core 只对"来源"那一半回退到文本抽取，而文本坐标一律
        # `text:` 惰性前缀（永不取回）；"种类"那一半没有回退（§46.8）。
        media = None
        content = _text(_call(event, 'get_message_str', ''))
        if content and not elements:
            elements = [{'type': 'text', 'attrs': {'content': content}, 'children': []}]
    if forward_read is not None:
        # 合并转发读取（上游 `forwardMessage` 组）：正文贴到卡片标记之后，
        # 注入形态与失败分支见 `forward_read_context`。
        content = forward_read_context(content, forward_read)
        if not elements:
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
        media=media,
        quote=quote,
        message_id=resolved.message_id or None,
        username=_text(_call(event, 'get_sender_name', '')),
        event=event,
    )


def _card_markup_from_chain(event: Any) -> str:
    """把消息链里的合并转发组件渲染成 `<forward id="…"/>`（只取 id，别的不关心）。

    `Forward` 组件的 `id` 是 AstrBot 唯一给出来的资源坐标（`get_message_str()` 只渲染
    `[转发消息]`）。这里顺手把它变成 core 认的那种标记，`extract_forward_ids` 就能复用
    同一条解析路径——**也为后面写进注入正文的"卡片线索"做准备**。
    """
    chain = _call(event, 'get_messages', []) or []
    parts: list[str] = []
    for component in chain:
        if component is None:
            continue
        if _component_kind(component) != 'forward':
            continue
        identifier = _text(_attr(component, 'id', 'res_id', 'forward_id'))
        if identifier:
            parts.append('<forward id="%s"/>' % _escape_attr(identifier))
    return ''.join(parts)


def looks_like_management_command(content: Any) -> bool:
    """上游 `looksLikeInterludeCommand`（`upstream/src/index.ts:915`）+ 本移植版前缀。"""
    return bool(COMMAND_WORD_RE.match(_text(content).strip()))


# --------------------------------------------------------------------------- #
# 合并转发读取的接缝（上游 `readForwardContent` 的适配层那一半）
# --------------------------------------------------------------------------- #

def _forward_section_keys(section: dict[str, Any]) -> dict[str, Any]:
    """配置段里的合并转发预算 → `core/forward_message` 认的两种拼写都带上。

    上游只认 camelCase（`maxNodes` …），本移植版 schema 里是 snake_case
    （`max_nodes` …），而 core 的 `forward_read_limits` 优先读 camelCase、snake 兜底。
    这里把两个拼写都填上（值相同），免得"配置页写 snake、代码读 camel"那类静默失效
    （`AGENTS.md` 坑 41 / 65 的同类）。
    """
    keys = {
        'enabled': ('enabled',),
        'maxNodes': ('maxNodes', 'max_nodes'),
        'maxCharacters': ('maxCharacters', 'max_characters'),
        'maxDepth': ('maxDepth', 'max_depth'),
    }
    normalized: dict[str, Any] = {}
    for target, names in keys.items():
        for name in names:
            if name in section:
                normalized[target] = section[name]
                break
    for name, value in section.items():
        normalized.setdefault(name, value)
    normalized.setdefault('enabled', True)
    return normalized


def _limits_payload(limits: Any) -> dict[str, Any]:
    """`ForwardReadLimits`（或字典）→ core 认的 camelCase 字典。"""
    if isinstance(limits, Mapping):
        return {key: limits.get(key) for key in ('maxNodes', 'maxCharacters', 'maxDepth')}
    return {
        'maxNodes': getattr(limits, 'max_nodes', FORWARD_DEFAULT_LIMITS.max_nodes),
        'maxCharacters': getattr(limits, 'max_characters', FORWARD_DEFAULT_LIMITS.max_characters),
        'maxDepth': getattr(limits, 'max_depth', FORWARD_DEFAULT_LIMITS.max_depth),
    }


def _onebot_forward_fetcher(client: Any) -> Callable[[str], Any]:
    """`get_forward_msg` 的取一页入口（原生 OneBot 动作，非 SnowLuma）。

    core 那边只认"`fetch(id)` 返回 awaitable"这一条契约，参数怎么拼是这里的事
    （OneBot 要 `{'id': …}`）。用 `client.call_action` 直连：`get_forward_msg` 是
    OneBot v11 标准动作，不走 `AstrbotTransport` 的动作目录（那是给**模型动作**用的，
    见交接说明）。
    """

    async def fetch(identifier: str) -> Any:
        frame = client.call_action('get_forward_msg', id=identifier)
        if inspect.isawaitable(frame):
            frame = await frame
        return frame

    return fetch


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
# 宿主回执 → 消息号（「撤回最近一条」依赖它）
# =========================================================================== #

#: 回执里可能承载消息号的键（不同平台 / 不同版本形状不一样）。
_MESSAGE_ID_KEYS = ('message_id', 'messageId', 'message_ids', 'messageIds', 'msg_id', 'msgId')
#: 回执里可能再嵌套一层结果（`{'status': 'ok', 'data': {...}}`）。
_MESSAGE_ID_CONTAINERS = ('data', 'result', 'ret', 'response', 'messages')


def _dedupe_ids(values: Iterable[Any]) -> list[str]:
    seen: list[str] = []
    for value in values:
        text = _text(value).strip()
        if text and text not in seen:
            seen.append(text)
    return seen


def _stringify_ids(value: Any, depth: int = 0) -> list[str]:
    """把「消息号位置上的值」变成字符串列表（容器就再往里看一眼）。"""
    if value is None or isinstance(value, bool):
        return []
    if isinstance(value, Mapping) or isinstance(value, (list, tuple)):
        return _extract_message_ids(value, depth + 1)
    if isinstance(value, (str, int)):
        text = _text(value).strip()
        return [text] if text else []
    return []


def _extract_message_ids(value: Any, depth: int = 0) -> list[str]:
    """尽力从宿主 `send_message` 的返回值里取出消息号。

    宿主的公开契约是 `-> bool`（4.28 实测：`context.send_message()` 只回答
    "找到平台了吗"），所以**正常情况下这里返回空列表**；但不同平台适配器 /
    未来版本可能回消息号（dict / list / 带属性的对象），照原样丢掉就等于
    "撤回"永远没得撤——于是逐层取值、取不到就空列表，形状再变也不抛。
    """
    if value is None or isinstance(value, bool) or depth > 4:
        return []
    ids: list[str] = []
    if isinstance(value, Mapping):
        for key in _MESSAGE_ID_KEYS:
            if key in value:
                ids += _stringify_ids(value[key], depth)
        for key in _MESSAGE_ID_CONTAINERS:
            if key in value:
                ids += _extract_message_ids(value[key], depth + 1)
        return _dedupe_ids(ids)
    if isinstance(value, (list, tuple)):
        for item in value:
            ids += _stringify_ids(item, depth)
        return _dedupe_ids(ids)
    for key in _MESSAGE_ID_KEYS:
        item = getattr(value, key, None)
        if item is not None:
            ids += _stringify_ids(item, depth)
    for key in _MESSAGE_ID_CONTAINERS:
        item = getattr(value, key, None)
        if item is not None:
            ids += _extract_message_ids(item, depth + 1)
    return _dedupe_ids(ids)


# =========================================================================== #
# 平台动作执行层（`plugin/core/platform_actions.py` 的目录 → 平台调用）
# =========================================================================== #
#
# 目录（`ACTIONS`，59 条）是唯一事实源：动作 id、参数、风险、开关分组都在那边。
# 这里只回答两个问题：
#
#   1. **这个动作在平台上叫什么、参数怎么改名**（`_PLATFORM_CALLS`）；
#   2. **目录里没写、但平台需要的东西从哪来**（会话坐标缺省 `_SESSION_FILLS`、
#      平台固定参数 `_PLATFORM_DEFAULTS`、枚举取值翻译 `_PLATFORM_VALUE_MAPS`）。
#
# 动作 id 用 snake_case 且与 NapCat 的 API 名一致（见 `platform_actions` 的模块
# 注释），所以映射表里**同名参数也照写一遍**：这张表要能一眼看出"这个动作到底
# 打给谁、带哪些参数"，而不是靠"没写就是同名"去脑补。改 NapCat / SnowLuma 的
# 拼写时只动这一处。

#: 值 = `@local`：由适配层自己实现（宿主 TTS / 组合多个平台调用），不经 OneBot 直通。
_PLATFORM_ACTION_LOCAL = '@local'
#: 值 = `@unsupported`：当前宿主没有这条能力（调用方拿到 `unsupported-platform-action: <id>`）。
_PLATFORM_ACTION_UNSUPPORTED = '@unsupported'

#: 目录动作 id → `(平台动作名, 参数名映射)`；参数名映射是「目录里的 snake_case → 平台要的拼写」。
_PLATFORM_CALLS: dict[str, tuple[str, dict[str, str]]] = {
    # ---------------- 互动 ----------------
    # 戳一戳：SnowLuma 有自动路由的 `send_poke`，NapCat 只有 `group_poke` / `friend_poke`。
    # 这里的 `@poke` 只是**占位标记**（不是说要把 `@poke` 发出去）：真正的动作名在
    # `_resolve_platform_call` 里按会话类型挑，两个协议端都能用。
    'send_poke': ('@poke', {'user_id': 'user_id', 'group_id': 'group_id'}),
    'send_like': ('send_like', {'user_id': 'user_id', 'times': 'times'}),
    'recall_message': ('delete_msg', {'message_id': 'message_id'}),
    # ---------------- 消息与定时 ----------------
    # 定时排程是**插件自己的账**（`interlude_intent` / `interlude_scheduled_command`），
    # 平台侧没有对应动作：放在这里显式拒绝，而不是打一个不存在的 OneBot 动作。
    'schedule_message': (_PLATFORM_ACTION_UNSUPPORTED, {}),
    'list_scheduled_messages': (_PLATFORM_ACTION_UNSUPPORTED, {}),
    'cancel_scheduled_message': (_PLATFORM_ACTION_UNSUPPORTED, {}),
    'schedule_command': (_PLATFORM_ACTION_UNSUPPORTED, {}),
    'list_scheduled_commands': (_PLATFORM_ACTION_UNSUPPORTED, {}),
    'cancel_scheduled_command': (_PLATFORM_ACTION_UNSUPPORTED, {}),
    # ---------------- 历史消息 ----------------
    'get_group_msg_history': (
        'get_group_msg_history', {'group_id': 'group_id', 'count': 'count', 'before': 'message_seq'},
    ),
    'get_friend_msg_history': (
        'get_friend_msg_history', {'user_id': 'user_id', 'count': 'count', 'before': 'message_seq'},
    ),
    # ---------------- QQ 状态 ----------------
    'update_qq_status': ('set_online_status', {'status': 'status'}),
    # 目录里 `get_qq_status` 没有参数（"查自己的状态"），而 `nc_get_user_status`
    # 要一个 user_id：由 `_PLATFORM_SESSION_PARAMS` 从会话坐标补机器人自己。
    'get_qq_status': ('nc_get_user_status', {}),
    # NapCat / SnowLuma 的公开动作表里都没有这一条；按目录 id 原样试一次，
    # 平台不认识时会带着它的错误文案失败（不静默、也不假装成功）。
    'get_fun_status_list': ('get_fun_status_list', {}),
    # ---------------- 群信息（只读） ----------------
    'get_group_members_info': ('get_group_member_list', {'group_id': 'group_id'}),
    'get_user_group_role': ('get_group_member_info', {'group_id': 'group_id', 'user_id': 'user_id'}),
    'get_group_honor_info': ('get_group_honor_info', {'group_id': 'group_id', 'type': 'type'}),
    'get_group_shut_list': ('get_group_shut_list', {'group_id': 'group_id'}),
    'get_group_notice_list': ('_get_group_notice', {'group_id': 'group_id'}),
    'get_group_at_all_remain': ('get_group_at_all_remain', {'group_id': 'group_id'}),
    'list_group_files': ('get_group_root_files', {'group_id': 'group_id'}),
    # ---------------- 群管理（写入） ----------------
    'send_group_notice': (
        '_send_group_notice', {'content': 'content', 'group_id': 'group_id', 'image': 'image'},
    ),
    'delete_group_notice': (
        '_del_group_notice', {'notice_id': 'notice_id', 'group_id': 'group_id'},
    ),
    'set_essence_msg': ('set_essence_msg', {'message_id': 'message_id'}),
    'delete_essence_msg': ('delete_essence_msg', {'message_id': 'message_id'}),
    # NapCat 的 go-cqhttp 兼容名是 `send_group_sign`，SnowLuma 是 `set_group_sign`；
    # 目录 id 取的是前者（目录 id 与 NapCat 的 API 名一致）。
    'send_group_sign': ('send_group_sign', {'group_id': 'group_id'}),
    'set_group_card': ('set_group_card', {'user_id': 'user_id', 'card': 'card', 'group_id': 'group_id'}),
    'set_group_special_title': (
        'set_group_special_title', {'user_id': 'user_id', 'title': 'special_title'},
    ),
    'set_group_add_option': ('set_group_add_option', {'option': 'add_type'}),
    'set_group_portrait': ('set_group_portrait', {'file': 'file', 'group_id': 'group_id'}),
    'set_group_name': ('set_group_name', {'name': 'group_name', 'group_id': 'group_id'}),
    'set_group_ban': (
        'set_group_ban', {'user_id': 'user_id', 'duration': 'duration', 'group_id': 'group_id'},
    ),
    'set_group_whole_ban': ('set_group_whole_ban', {'enable': 'enable', 'group_id': 'group_id'}),
    'set_group_kick': (
        'set_group_kick', {'user_id': 'user_id', 'reject_add': 'reject_add', 'group_id': 'group_id'},
    ),
    'set_group_admin': (
        'set_group_admin', {'user_id': 'user_id', 'enable': 'enable', 'group_id': 'group_id'},
    ),
    'delete_group_file': ('delete_group_file', {'file_id': 'file_id', 'group_id': 'group_id'}),
    'upload_group_file': (
        'upload_group_file',
        {'file': 'file', 'name': 'name', 'folder': 'folder', 'group_id': 'group_id'},
    ),
    # NapCat 的 rename/move 都要求 `current_parent_directory`（文件当前所在目录）；
    # 目录里叫 `current_folder`，缺省 `/`（根目录）。
    'rename_group_file': (
        'rename_group_file',
        {'file_id': 'file_id', 'name': 'new_name', 'current_folder': 'current_parent_directory',
         'group_id': 'group_id'},
    ),
    'move_group_file': (
        'move_group_file',
        {'file_id': 'file_id', 'folder': 'target_parent_directory',
         'current_folder': 'current_parent_directory', 'group_id': 'group_id'},
    ),
    'create_group_file_folder': (
        'create_group_file_folder', {'name': 'name', 'group_id': 'group_id'},
    ),
    'delete_group_folder': ('delete_group_folder', {'folder': 'folder_id', 'group_id': 'group_id'}),
    'trans_group_file': ('trans_group_file', {'file_id': 'file_id', 'group_id': 'group_id'}),
    # ---------------- 个人资料 ----------------
    'set_qq_profile': (
        'set_qq_profile', {'nickname': 'nickname', 'personal_note': 'personal_note'},
    ),
    'set_qq_avatar': ('set_qq_avatar', {'file': 'file'}),
    # 自己的昵称 + 个性签名：`get_stranger_info` 带 `long_nick`（个性签名），
    # 而 `get_login_info` 只有昵称；user_id 由 `_PLATFORM_SESSION_PARAMS` 补机器人自己。
    'get_qq_profile': ('get_stranger_info', {}),
    # ---------------- 语音（走宿主 TTS，不用 NapCat 的 AI 声聊） ----------------
    'send_voice': (_PLATFORM_ACTION_LOCAL, {'content': 'content'}),
    'list_voices': (_PLATFORM_ACTION_LOCAL, {}),
    # ---------------- QQ 空间 ----------------
    # **NapCat 原生的只有发/删说说**（`send_qzone_msg` / `delete_qzone_msg`，见
    # napcat.apifox.cn/496813058e0 / 496813059e0）；评论、点赞、看说说列表、看好友动态是
    # **SnowLuma 扩展动作**（`comment_qzone` / `like_qzone` / `get_qzone_msg_list` /
    # `get_qzone_feeds`）——只装 NapCat 时这四个会以平台原话失败，不会假装成功。
    'publish_qzone_post': (
        'send_qzone_msg',
        {'content': 'content', 'ugc_right': 'ugc_right', 'images': 'images',
         'target_uins': 'target_uins'},
    ),
    'comment_qzone_post': (
        'comment_qzone', {'tid': 'tid', 'content': 'content', 'target_uin': 'target_uin'},
    ),
    'like_qzone_post': ('like_qzone', {'tid': 'tid', 'target_uin': 'target_uin'}),
    # v1.7.1：转发与"看好友动态"。**首选 NapCat WebSocket 方案**（chunk13 里 CGI 优先），
    # 这里的 SnowLuma 名只是回退通道；映射必须存在（目录动作不许有"没有出口"的）。
    'forward_qzone_post': (
        'forward_qzone', {'tid': 'tid', 'content': 'content', 'target_uin': 'target_uin'},
    ),
    # v1.7.5：改说说可见范围（`emotion_cgi_update`）。**NapCat 与 SnowLuma 都没有**
    # 这条原生动作（NapCat 的扩展动作只有 `send_qzone_msg` / `delete_qzone_msg`），
    # 它只能走本机的 QZone CGI 通道（`chunk13` 的 `qzone_execute`，需要
    # `get_cookies`）。这里显式标 `@unsupported`：万一它绕到传输层，
    # 会拿到 `unsupported-platform-action`，而不是往平台打一个不存在的动作名。
    'set_qzone_visibility': (_PLATFORM_ACTION_UNSUPPORTED, {}),
    # v1.7.8 复核：下面两条**只是目录↔平台的双向对账 + SnowLuma 直通口**，
    # **默认路径走不到**——`chunk12.CORE_HANDLED_ACTIONS` 把这两个目录动作收在本机，
    # 由 `chunk13.qzone_read` 走 CGI 读通道（拿不到 cookie 才回落这里）。
    # NapCat 没有这两个原生动作，所以任何"把它们当平台动作派出去"的路径都会拿到
    # `retcode 1404 不支持的Api`（用户贴过日志）——别再让哪条新路径从这儿过。
    'list_qzone_feeds': ('get_qzone_feeds', {'count': 'num'}),
    # 指了归属 QQ = 看那个人的说说列表；没指 = 看好友动态（见 `_resolve_platform_call`）。
    'list_qzone_posts': ('get_qzone_msg_list', {'target_uin': 'target_uin', 'count': 'num'}),
    'delete_qzone_post': ('delete_qzone_msg', {'tid': 'tid'}),
    # ---------------- 联系人与群 ----------------
    'list_contacts': (_PLATFORM_ACTION_LOCAL, {'type': 'type', 'limit': 'limit'}),
    'search_contacts': (_PLATFORM_ACTION_LOCAL, {'keyword': 'keyword', 'limit': 'limit'}),
    'get_user_profile': ('get_stranger_info', {'user_id': 'user_id'}),
    'get_group_info': ('get_group_info', {'group_id': 'group_id'}),
    'handle_friend_request': (
        'set_friend_add_request', {'flag': 'flag', 'approve': 'approve', 'remark': 'remark'},
    ),
    'handle_group_request': (
        'set_group_add_request',
        {'flag': 'flag', 'approve': 'approve', 'sub_type': 'sub_type', 'reason': 'reason'},
    ),
    'delete_friend': ('delete_friend', {'user_id': 'user_id', 'block': 'temp_block'}),
}

#: `list_qzone_posts` 在「没指归属 QQ」时的映射：好友动态用 `get_qzone_feeds`。
_QZONE_FEEDS_CALLS: dict[str, str] = {'count': 'count'}

#: 目录动作 → 「目录参数 ← 会话坐标」的缺省补全表。
#: 目录里标了「留空＝本回合对话对象」的参数都在这里补；`set_group_card.user_id`
#: **刻意不补**——目录写的是「留空＝改机器人自己」。
_SESSION_FILLS: dict[str, dict[str, str]] = {
    'send_poke': {'user_id': 'user_id', 'group_id': 'group_id'},
    'send_like': {'user_id': 'user_id'},
    'recall_message': {},
    'schedule_message': {},
    'get_group_msg_history': {'group_id': 'group_id'},
    'get_friend_msg_history': {'user_id': 'user_id'},
    'update_qq_status': {},
    'get_group_members_info': {'group_id': 'group_id'},
    'get_user_group_role': {'group_id': 'group_id'},
    'get_group_honor_info': {'group_id': 'group_id'},
    'get_group_shut_list': {'group_id': 'group_id'},
    'get_group_notice_list': {'group_id': 'group_id'},
    'get_group_at_all_remain': {'group_id': 'group_id'},
    'list_group_files': {'group_id': 'group_id'},
    'send_group_notice': {'group_id': 'group_id'},
    'delete_group_notice': {'group_id': 'group_id'},
    'send_group_sign': {'group_id': 'group_id'},
    'set_group_card': {'group_id': 'group_id'},
    'set_group_portrait': {'group_id': 'group_id'},
    'set_group_name': {'group_id': 'group_id'},
    'set_group_ban': {'group_id': 'group_id'},
    'set_group_whole_ban': {'group_id': 'group_id'},
    'set_group_kick': {'group_id': 'group_id'},
    'set_group_admin': {'group_id': 'group_id'},
    'delete_group_file': {'group_id': 'group_id'},
    'upload_group_file': {'group_id': 'group_id'},
    'rename_group_file': {'group_id': 'group_id'},
    'move_group_file': {'group_id': 'group_id'},
    'create_group_file_folder': {'group_id': 'group_id'},
    'delete_group_folder': {'group_id': 'group_id'},
    'trans_group_file': {'group_id': 'group_id'},
    'get_group_info': {'group_id': 'group_id'},
    'send_voice': {},
    'list_voices': {},
    'list_contacts': {},
    'search_contacts': {},
}

#: 目录**没有**声明、但平台要的参数：平台参数名 ← 会话坐标键（在目录校验之后补）。
#:
#: 与 `_SESSION_FILLS` 分开是因为它不能进 `validate_action` —— 目录里没有的键一律
#: 按"未知参数"拒绝（模型乱写参数名是最常见的错），所以这几个只能在校验之后、
#: 发给平台之前注入。
_PLATFORM_SESSION_PARAMS: dict[str, dict[str, str]] = {
    # 查自己的状态 / 资料：平台要 user_id，目录没这个参数。
    'get_qq_status': {'user_id': 'self_id'},
    'get_qq_profile': {'user_id': 'self_id'},
    # 目录里这两个动作没写 group_id（就是"当前的群"），平台却要。
    'set_group_special_title': {'group_id': 'group_id'},
    'set_group_add_option': {'group_id': 'group_id'},
}

#: 目录动作 → 平台侧**固定**参数（目录没声明、平台却要的）。
_PLATFORM_DEFAULTS: dict[str, dict[str, Any]] = {
    'get_group_msg_history': {'count': 20},
    'get_friend_msg_history': {'count': 20},
    'get_group_honor_info': {'type': 'all'},
    'get_user_profile': {'no_cache': False},
    'get_qq_profile': {'no_cache': False},
    'update_qq_status': {'ext_status': 0, 'battery_status': 0},
    # 目录写的是「4 好友（默认）可见」，平台的缺省是 1（所有人）——以目录为准。
    'publish_qzone_post': {'ugc_right': 4},
    'upload_group_file': {'folder': '/'},
    'create_group_file_folder': {'parent_directory': '/'},
    'move_group_file': {'current_parent_directory': '/'},
    'rename_group_file': {'current_parent_directory': '/'},
}

#: 目录动作 → 参数 → 「目录里的枚举值 → 平台取值」。
_PLATFORM_VALUE_MAPS: dict[str, dict[str, dict[str, Any]]] = {
    # NapCat 的加群方式是个数字：1 允许所有人 / 2 需要审核 / 3 禁止。
    'set_group_add_option': {'option': {'allow': 1, 'audit': 2, 'refuse': 3}},
}

#: **调度层混在动作参数里传下来的会话坐标**（`chunk12._resolve_action_target` 会把它
#: 们并进 params：`user_id` / `group_id` / `channel_id` / `platform` / `self_id` /
#: `is_group`）。它们不是模型写的动作参数、目录里也没有，但必须接受：这些坐标是
#: 「本回合的对话对象」，既用来补缺省，也用来定位平台实例。**不接受它们 = 生产里
#: 每一条动作都会被自己的"未知参数"闸门拒掉**。
#:
#: 值 = 规范键名（camelCase 拼写也认）。
_TARGET_PARAMS: dict[str, str] = {
    'platform': 'platform',
    'self_id': 'self_id', 'selfId': 'self_id',
    'user_id': 'user_id', 'userId': 'user_id',
    'group_id': 'group_id', 'groupId': 'group_id',
    'channel_id': 'channel_id', 'channelId': 'channel_id',
    'is_group': 'is_group', 'isGroup': 'is_group',
    'participant_id': 'participant_id', 'participantId': 'participant_id',
}

#: 列表类回执的限幅（提示词与账本都不该被一整页群成员灌爆）。
_HISTORY_LIMIT = 50
_CONTACT_LIMIT = 200
_SEARCH_CONTACT_LIMIT = 50
_MEMBER_LIMIT = 100

#: 传输异常固定结尾：调用方据此判 `ambiguous`（动作可能已在平台侧生效）。
_AMBIGUOUS_TAIL = '结果未知，请勿自动重试'


def _camel_param(name: str) -> str:
    """`user_id` → `userId`（模型偶尔写 camelCase，校验时双读）。"""
    head, *rest = str(name).split('_')
    return head + ''.join(part.title() for part in rest)


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, Mapping):
        return [value]
    return []


def _clean(value: Any) -> str:
    return _text(value).strip()


def _segments_text(value: Any) -> str:
    """把 OneBot 的 `message` 段数组压成一行人读文本（只保留必要信息）。"""
    if isinstance(value, str):
        return value.strip()[:600]
    if not isinstance(value, (list, tuple)):
        return ''
    parts: list[str] = []
    for segment in value:
        if not isinstance(segment, Mapping):
            continue
        kind = _text(segment.get('type'))
        data = segment.get('data') if isinstance(segment.get('data'), Mapping) else {}
        if kind == 'text':
            parts.append(_text(data.get('text')))
        elif kind == 'image':
            parts.append('[图片]')
        elif kind in ('record', 'audio', 'voice'):
            parts.append('[语音]')
        elif kind == 'video':
            parts.append('[视频]')
        elif kind == 'face':
            parts.append('[表情]')
        elif kind == 'mface':
            parts.append('[表情包]')
        elif kind == 'at':
            parts.append('@%s' % _text(data.get('qq')))
        elif kind == 'reply':
            continue
        elif kind:
            parts.append('[%s]' % kind)
    return ''.join(parts).strip()[:600]


def _brief_message(row: Any) -> dict[str, Any]:
    """历史消息：只留发送者 / 时间 / 文本。"""
    if not isinstance(row, Mapping):
        return {'text': _text(row)[:600]}
    sender = row.get('sender') if isinstance(row.get('sender'), Mapping) else {}
    return {
        'message_id': _text(row.get('message_id') or row.get('messageId')),
        'user_id': _text(sender.get('user_id') or row.get('user_id') or row.get('sender_id')),
        'nickname': _text(sender.get('card') or sender.get('nickname') or row.get('sender_name')),
        'time': row.get('time') or row.get('timestamp') or 0,
        'text': _segments_text(row.get('message') or row.get('raw_message') or row.get('message_str')),
    }


def _brief_member(row: Any) -> dict[str, Any]:
    if not isinstance(row, Mapping):
        return {'raw': _text(row)[:200]}
    return {
        'user_id': _text(row.get('user_id')),
        'nickname': _text(row.get('nickname')),
        'card': _text(row.get('card')),
        'role': _text(row.get('role')),
        'join_time': row.get('join_time') or 0,
    }


def _brief_notice(row: Any) -> dict[str, Any]:
    if not isinstance(row, Mapping):
        return {'text': _text(row)[:600]}
    message = row.get('message') if isinstance(row.get('message'), Mapping) else {}
    return {
        'notice_id': _text(row.get('notice_id')),
        'sender_id': _text(row.get('sender_id')),
        'publish_time': row.get('publish_time') or 0,
        'text': _text(message.get('text') or row.get('text'))[:600],
    }


def _brief_group_file(row: Any) -> dict[str, Any]:
    if not isinstance(row, Mapping):
        return {'name': _text(row)[:200]}
    return {
        'file_id': _text(row.get('file_id')),
        'file_name': _text(row.get('file_name')),
        'file_size': row.get('file_size') or 0,
        'upload_time': row.get('upload_time') or 0,
        'uploader_name': _text(row.get('uploader_name')),
    }


def _brief_folder(row: Any) -> dict[str, Any]:
    if not isinstance(row, Mapping):
        return {'name': _text(row)[:200]}
    return {
        'folder_id': _text(row.get('folder_id')),
        'folder_name': _text(row.get('folder_name')),
        'total_file_count': row.get('total_file_count') or 0,
    }


def _brief_qzone_entry(row: Any) -> dict[str, Any]:
    if not isinstance(row, Mapping):
        return {'content': _text(row)[:600]}
    return {
        'tid': _text(row.get('tid') or row.get('id')),
        'content': _text(row.get('content') or row.get('text'))[:600],
        'create_time': row.get('create_time') or row.get('createTime') or row.get('time') or 0,
        'uin': _text(row.get('uin') or row.get('target_uin')),
        'nickname': _text(row.get('nickname') or row.get('name')),
    }


def _brief_friend(row: Any) -> dict[str, Any]:
    if not isinstance(row, Mapping):
        return {'nickname': _text(row)[:200]}
    return {
        'user_id': _text(row.get('user_id')),
        'nickname': _text(row.get('nickname')),
        'remark': _text(row.get('remark')),
    }


def _brief_group(row: Any) -> dict[str, Any]:
    if not isinstance(row, Mapping):
        return {'group_name': _text(row)[:200]}
    return {
        'group_id': _text(row.get('group_id')),
        'group_name': _text(row.get('group_name')),
        'member_count': row.get('member_count') or 0,
        'max_member_count': row.get('max_member_count') or 0,
    }


def _project_action_data(action_id: str, data: Any) -> Any:
    """裁剪平台回执：只留必要字段 + 限幅（原样塞回去就是一堆提示词噪声）。"""
    if action_id in ('get_group_msg_history', 'get_friend_msg_history'):
        rows = data.get('messages') if isinstance(data, Mapping) else data
        return {'messages': [_brief_message(row) for row in _as_list(rows)[:_HISTORY_LIMIT]]}
    if action_id == 'get_group_members_info':
        rows = data.get('members') if isinstance(data, Mapping) else data
        return {'members': [_brief_member(row) for row in _as_list(rows)[:_MEMBER_LIMIT]]}
    if action_id == 'get_group_notice_list':
        rows = data.get('notices') if isinstance(data, Mapping) else data
        return {'notices': [_brief_notice(row) for row in _as_list(rows)[:20]]}
    if action_id == 'list_group_files':
        files = data.get('files') if isinstance(data, Mapping) else None
        folders = data.get('folders') if isinstance(data, Mapping) else None
        return {
            'files': [_brief_group_file(row) for row in _as_list(files)[:_CONTACT_LIMIT]],
            'folders': [_brief_folder(row) for row in _as_list(folders)[:_MEMBER_LIMIT]],
        }
    if action_id == 'list_qzone_posts':
        rows = data
        if isinstance(data, Mapping):
            for key in ('msglist', 'feeds', 'posts', 'messages'):
                if key in data:
                    rows = data[key]
                    break
        return {'posts': [_brief_qzone_entry(row) for row in _as_list(rows)[:_HISTORY_LIMIT]]}
    return data


def _validate_platform_action(
    action_id: str,
    params: Mapping[str, Any],
) -> tuple[Optional[dict[str, Any]], str]:
    """目录校验 + **未知参数拒绝**。

    目录自带的 `validate_action` 只遍历**已声明**的参数（未知键被静默忽略），
    而模型乱写参数名是最常见的一种错——这里先按声明把未知键挑出来拒掉。
    """
    action = platform_action_catalog.ACTIONS.get(action_id)
    if action is None:
        return None, '未知动作：%s' % action_id
    allowed: set[str] = set(_TARGET_PARAMS)
    for param in action.params:
        allowed.add(param.name)
        allowed.add(_camel_param(param.name))
    unknown = sorted(str(key) for key in params if str(key) not in allowed)
    if unknown:
        return None, '动作 %s 不支持参数 %s' % (action_id, '、'.join(unknown))
    normalized, reason = platform_action_catalog.validate_action(action_id, params)
    if normalized is None:
        return None, reason
    return dict(normalized.get('params') or {}), ''


def _session_target(params: Mapping[str, Any], base: Mapping[str, Any]) -> dict[str, Any]:
    """本回合的会话坐标 = 桥登记的坐标 + 调度层随参数带下来的坐标（后者优先）。

    调度层给的才是"这一回合正在跟谁说话"的权威值（桥的登记表在后台回合里可能是
    上一条消息的），所以它覆盖而不是被覆盖。
    """
    target: dict[str, Any] = dict(base)
    for key, canonical in _TARGET_PARAMS.items():
        value = params.get(key)
        if value not in (None, ''):
            target[canonical] = value
    if target.get('is_group') in (None, ''):
        target['is_group'] = bool(_clean(target.get('group_id')))
    if not _clean(target.get('channel_id')):
        target['channel_id'] = _clean(target.get('group_id')) or _clean(target.get('user_id'))
    return target


def _session_value(key: str, target: Mapping[str, Any]) -> str:
    if key == 'self_id':
        return _clean(target.get('self_id'))
    if key == 'user_id':
        return _clean(target.get('user_id'))
    if key == 'group_id':
        return _clean(target.get('group_id'))
    if key == 'channel_id':
        return _clean(target.get('channel_id'))
    return ''


def _apply_session_defaults(
    action_id: str,
    params: Mapping[str, Any],
    target: Mapping[str, Any],
) -> dict[str, Any]:
    """按 `_SESSION_FILLS` 把「本回合的对话对象」补进缺省的参数里。

    在目录校验**之前**跑：群聊里 `get_group_info` 这类「目录标了必填、实际就是
    当前群」的参数，靠这一步满足必填；用户显式给了值就一律不动。
    """
    filled: dict[str, Any] = dict(params)
    for param_name, session_key in (_SESSION_FILLS.get(action_id) or {}).items():
        # 两种拼写都算"给了值"：模型写 camelCase 时不能被会话缺省顶掉。
        if _clean(filled.get(param_name)) or _clean(filled.get(_camel_param(param_name))):
            continue
        value = _session_value(session_key, target)
        if value:
            filled[param_name] = value
    return filled


def _translate_value(action_id: str, param_name: str, value: Any) -> Any:
    table = (_PLATFORM_VALUE_MAPS.get(action_id) or {}).get(param_name)
    if not table:
        return value
    if isinstance(value, str) and value.strip() in table:
        return table[value.strip()]
    return value


def _resolve_platform_call(
    action_id: str,
    params: Mapping[str, Any],
    target: Mapping[str, Any],
) -> tuple[str, dict[str, Any], list[str]]:
    """把一条目录动作翻成「平台动作名 + 平台参数 + 给调用方的说明」。"""
    action_name, mapping = _PLATFORM_CALLS[action_id]
    notes: list[str] = []
    if action_id == 'send_poke':
        # 群聊打 `group_poke`，私聊打 `friend_poke`：这两个名字 NapCat / SnowLuma 都有。
        action_name = 'group_poke' if _clean(params.get('group_id')) else 'friend_poke'
    elif action_id == 'list_qzone_posts' and not _clean(params.get('target_uin')):
        # 没指归属 QQ = 看好友动态；SnowLuma 的 `get_qzone_msg_list` 只吃目标 QQ。
        action_name = 'get_qzone_feeds'
        mapping = _QZONE_FEEDS_CALLS
        notes.append('未指定 target_uin：按好友动态读取（get_qzone_feeds）')
    mapped: dict[str, Any] = {}
    for param_name, platform_name in mapping.items():
        if param_name not in params:
            continue
        value = params[param_name]
        if value is None or value == '':
            continue
        mapped[platform_name] = _translate_value(action_id, param_name, value)
    for platform_name, value in (_PLATFORM_DEFAULTS.get(action_id) or {}).items():
        mapped.setdefault(platform_name, value)
    for platform_param, session_key in (_PLATFORM_SESSION_PARAMS.get(action_id) or {}).items():
        if _clean(mapped.get(platform_param)):
            continue
        value = _session_value(session_key, target)
        if value:
            mapped[platform_param] = value
    if action_name == 'get_qzone_feeds':
        mapped.setdefault('page_num', 1)
    if action_id == 'update_qq_status':
        dropped = [name for name in ('minutes', 'text') if _clean(params.get(name))]
        if dropped:
            # NapCat / SnowLuma 的 `set_online_status` 只有 status/ext_status/battery_status：
            # 状态能改，但「到点自动恢复」「自定义文本」没有落点。
            notes.append('%s 在当前平台没有对应字段，已忽略（状态不会到点自动恢复）' % '、'.join(dropped))
    return action_name, mapped, notes


def _frame_ok(frame: Any) -> bool:
    """OneBot / SnowLuma 的回执是不是成功（`status == 'ok'` 或 `retcode == 0`）。"""
    if not isinstance(frame, Mapping):
        return False
    return frame.get('status') == 'ok' or frame.get('retcode') == 0


def _frame_has_verdict(frame: Any) -> bool:
    """平台到底有没有就这条动作表态（`status` / `retcode` / `message` 任一有值）。

    即发即忘的接口（`set_input_status`）用得到：NapCat 不保证给回执，"什么都没说"
    不是失败，只有**明确说了** `status`/`retcode`/`message` 且不是成功帧才算失败。
    """
    if not isinstance(frame, Mapping):
        return False
    if frame.get('status') is not None or frame.get('retcode') is not None:
        return True
    return bool(frame.get('message') or frame.get('msg') or frame.get('wording'))


def _frame_error_text(action_name: str, frame: Any) -> str:
    """失败回执 → 带 status / retcode / message 的中文错误串。"""
    row = frame if isinstance(frame, Mapping) else {}
    status = row.get('status')
    retcode = row.get('retcode')
    message = row.get('message') or row.get('msg') or row.get('wording') or ''
    if status is None and retcode is None and not message:
        return '%s 失败：平台没有回执（%s）' % (action_name, type(frame).__name__)
    head = _text(status) if status is not None else _text(retcode)
    parts = [head]
    if retcode is not None and status is not None:
        parts.append('retcode=%s' % retcode)
    detail = ' '.join(part for part in (_text(message),) if part).strip()
    if detail:
        parts.append(detail)
    return '%s 失败：%s' % (action_name, ' '.join(part for part in parts if part))


def _action_result(result: Any, action_id: str, notes: Optional[list[str]] = None) -> dict[str, Any]:
    """统一 `platform_action` 的回执形状：一定有 `ok` / `error` / `data`。"""
    payload = dict(result) if isinstance(result, Mapping) else {}
    payload.setdefault('ok', False)
    payload.setdefault('error', '')
    payload.setdefault('data', None)
    payload['action'] = action_id
    merged = list(dict.fromkeys(list(notes or []) + list(payload.get('notes') or [])))
    if merged:
        payload['notes'] = merged
    return payload


def _contact_matches(entry: Mapping[str, Any], keyword: str) -> bool:
    """联系人条目是否命中关键词（昵称 / 备注 / 群名 / 号码都要能搜到）。"""
    for value in entry.values():
        if isinstance(value, str) and keyword in value.lower():
            return True
    return False


#: TTS 服务商可能声明的模态写法（宿主各版本 / 各家服务商拼写不一，全部小写比对）。
#: 只用来**排除**"声明了但没有文字转语音能力"的服务商；没声明的一律照常。
_TTS_MODALITIES = frozenset({'t2s', 'tts', 'text_to_speech', 'speech'})


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
            response = await self.context.send_message(umo, MessageChain(chain=components))
        except Exception as error:  # noqa: BLE001 - 投递失败必须走降级分支
            log_fallback('warn', 'AstrBot 消息投递失败 会话=%s 错误=%s', umo, error)
            return {'ok': False, 'error': str(error)}
        # 宿主 4.28 只回 `bool`（找不到平台就 False），但别的平台适配器 / 以后
        # 可能回消息号：能取就取出来，撤回动作（`recall_message target=last`）
        # 才有"最近一条已投递消息"可定位。取不到就是空列表（形状再变也不抛）。
        message_ids = _extract_message_ids(response)
        if message_ids:
            self.bridge.remember_outbound_message_ids(message_ids, umo)
        return self._result(message_ids, umo)

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
        voice: bool = False,
    ) -> dict[str, Any]:
        """给一条关系分支发私聊消息。上游 `sendPrivateMessage(participant...)`。

        `voice=True`（v1.7.7）：这一段由正文 `<tts/>` 标记指定要发语音。合成不了就
        **退回发文字**（内容一个字都不少），并在日志里留一条 warn。
        """
        participant = participant if isinstance(participant, dict) else {}
        platform = _text(pick(participant, 'platform'))
        self_id = _text(pick(participant, 'selfId', 'self_id'))
        user_id = _text(pick(participant, 'userId', 'user_id'))
        channel_id = _text(pick(participant, 'channelId', 'channel_id')) or user_id
        umo = self.bridge.private_umo(platform, self_id, user_id or channel_id)
        if voice:
            voiced = await self._send_marked_voice(umo, content, reply_to)
            if voiced is not None:
                return voiced
        components = chain_from_content(content)
        if reply_to:
            components.insert(0, Reply(id=_text(reply_to)))
        return await self._send_chain(umo, components)

    async def send_group(
        self,
        channel_id: str,
        content: str,
        reply_to: Optional[str] = None,
        voice: bool = False,
    ) -> dict[str, Any]:
        """向群频道发送。上游 `sendGroupMessage(story, channelId, content, quoteMessageId)`。

        `voice=True` 同 `send_private`（正文 `<tts/>` 标记的分段语音）。
        """
        umo = self.bridge.group_umo(channel_id)
        if voice:
            voiced = await self._send_marked_voice(umo, content, reply_to)
            if voiced is not None:
                return voiced
        components = chain_from_content(content)
        if reply_to:
            components.insert(0, Reply(id=_text(reply_to)))
        return await self._send_chain(umo, components)

    async def send_session(self, session: Any, content: str, voice: bool = False) -> dict[str, Any]:
        """对当前入站会话原路回复。上游 `session.send(outgoingContent)`（`:5156`）。

        `voice=True`（v1.7.7）：这一段由正文 `<tts/>` 指定。回合内的回复先落进
        捕获缓冲、由 `main.py` 交回宿主，所以这里**先把语音合成成文件**再进缓冲
        （合不出来就照旧收文字，warn 说明原因）——顺序与分段投递的顺序都跟着缓冲走。
        """
        capture = self.bridge.capture
        if capture is not None and capture.matches(session):
            if voice:
                path, error = await self.synthesize_voice(content, capture.umo)
                if error:
                    log_fallback(
                        'warn', '正文语音标记投递失败，已退回发文字（内容不丢）会话=%s 原因=%s',
                        capture.umo, error,
                    )
                else:
                    capture.append(content, voice_path=path)
                    return self._result([], '', captured=True, voice=True, file=path)
            capture.append(content)
            return self._result([], '', captured=True)
        umo = self.bridge.session_umo(session)
        if voice:
            voiced = await self._send_marked_voice(umo, content)
            if voiced is not None:
                return voiced
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

        平台不是 OneBot 家族时按移植约定 降级：返回
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
        调用 `get_image`（那条路在 `fetch_incoming_image()` 里，见下）。
        """
        return await self._fetch_bytes(url, '图片')

    async def fetch_incoming_image(self, source: str) -> Optional[bytes]:
        """按**入站媒体坐标**取图片字节（宿主通道优先；取不到回 `None`）。**可选能力。**

        与 `fetch_image()`（只管把 URL 下回来）的分工：这条回答的是"这次入站事件里那张图，
        宿主还能怎么把字节给我"。坐标由适配层在 `session_view()` 时写进 `SessionView.media`
        （`{'source': …, 'raw': {…}}`），事件引用由 `remember_event()` 登记；`source` 之外
        不额外要求调用方知道任何平台细节。

        顺序（每条都有理由，`docs/PORTING_NOTES.md` §49.3）：

        1. **本地文件**（`onebot-file:` / `file://` / 绝对路径且真的在）→ 读盘；
        2. **内联载荷**（`data:` / AstrBot 的 `base64://`）→ 解码，不发请求；
        3. **宿主官方方法** `AstrBot Image.convert_to_file_path()`：URL 交给宿主的下载器
           （自带 certifi 根证书、SSL 校验失败降级、长超时），本地文件直接回路径；
        4. **OneBot 侧** `get_image`：平台自己下载，字节经 `base64` 回来（条件见 §49.4）。

        直链下载**不在这里**做：那是 `fetch_image()` 的活，core 侧按"宿主通道 → 直链"
        的顺序各试一次。任何一步失败都继续下一步，最后回 `None` —— **绝不抛**
        （core 那边按"拿不到字节"记一条可见 warn）。
        """
        value = _text(source).strip()
        if not value:
            return None
        event, raw = self.bridge.incoming_media_handle(value)
        candidates = (value, raw.get('file'))
        for candidate in candidates:
            local = _local_image_path(candidate)
            if local:
                data = await self._read_local_image(local)
                if data:
                    return data
        for candidate in candidates:
            inline = _decode_inline_image(candidate)
            if inline:
                return inline
        data = await self._host_image_bytes(_image_component_for_raw(event, raw))
        if data:
            return data
        return await self._onebot_image_bytes(event, raw)

    async def _read_local_image(self, path: str) -> Optional[bytes]:
        """读一个本地图片文件（放到线程里，别阻塞事件循环）；失败回 `None`。"""
        try:
            return await asyncio.to_thread(Path(path).read_bytes)
        except Exception as error:  # noqa: BLE001 - 文件没了 / 权限不够都算拿不到
            log_fallback('debug', '入站图片本地文件读取失败 路径=%s 错误=%s', path, error)
            return None

    async def _host_image_bytes(self, component: Any) -> Optional[bytes]:
        """宿主官方取字节路：`AstrBot Image.convert_to_file_path()`（§49.3 第 3 条）。

        为什么优先它：这个方法把"URL 下载 / `file:///` 本地读 / `base64://` 解码"三种
        形态归一成一个本地路径，而且用的是宿主自己的下载器（aiohttp + certifi；
        证书校验失败时它会降级到不校验证书再试一次），比插件自己发请求稳。
        组件不在场（链接不上 / 老宿主）或方法不存在时回 `None`，由调用方继续走后面的通道。
        """
        if component is None:
            return None
        converter = getattr(component, 'convert_to_file_path', None)
        if not callable(converter):
            return None
        try:
            result = converter()
            if inspect.isawaitable(result):
                result = await asyncio.wait_for(result, timeout=_INCOMING_IMAGE_TIMEOUT_SECONDS)
        except Exception as error:  # noqa: BLE001 - 宿主取不到就换下一条通道
            log_fallback('debug', '宿主取图失败（继续走后面的通道） 错误=%s', error)
            return None
        path = _text(result).strip()
        return await self._read_local_image(path) if path else None

    async def _onebot_image_bytes(self, event: Any, raw: Any) -> Optional[bytes]:
        """OneBot 侧取图：`get_image`（NapCat）。**条件性可用**，见 `docs/PORTING_NOTES.md` §49.4。

        能用的两个前提（源码核对，不是猜）：

        * `file` 得能解析成 NapCat 的 file-id 令牌，或者**按文件名**在它的缓存里搜到
          （`packages/napcat-onebot/action/file/GetFile.ts` 的三段模式：UUID 解码 →
          modelId → `searchForFile`）；入站图片段给的 `file` 是 `fileName`，所以通常
          走"按名字搜"那一段；
        * `base64` **只在 NapCat 开了 `enableLocalFile2Url` 时**才回；没开就只有一个
          在 **NapCat 主机**上的本地路径 —— 那台机器不是我们这台时读不到。

        所以这条是**兜底**，不是主路：拿不到就回 `None`，由 core 记那条可见 warn。
        动作失败**不打日志**（`fire_and_forget=True`）：它只是降级链的一环，
        刷屏的是 core 那条按会话节流的 warn。
        """
        token = _text(raw.get('file') if isinstance(raw, dict) else '').strip()
        if not token or event is None:
            return None
        endpoint = endpoint_for_event(event)
        client = self.bridge.onebot_client(endpoint.platform, endpoint.self_id)
        if client is None:
            return None
        try:
            result = await asyncio.wait_for(
                self._call_onebot_on(client, 'get_image', {'file': token}, fire_and_forget=True),
                timeout=_INCOMING_IMAGE_TIMEOUT_SECONDS,
            )
        except Exception as error:  # noqa: BLE001 - 超时 / 断连都按拿不到
            log_fallback('debug', 'OneBot 取图失败 错误=%s', error)
            return None
        payload = result.get('data') if isinstance(result, dict) else None
        data = payload if isinstance(payload, Mapping) else {}
        inline = _text(data.get('base64')).strip()
        if inline:
            try:
                return base64.b64decode(inline)
            except Exception as error:  # noqa: BLE001 - 坏 base64 按拿不到
                log_fallback('debug', 'OneBot 取图 base64 解码失败 错误=%s', error)
        local = _local_image_path(data.get('file'))
        if local:
            return await self._read_local_image(local)
        return None

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
        inline = _decode_inline_image(source)
        if inline is not None:
            return inline
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

        `voice=True`（v1.7.7，可选键）：这一段由正文 `<tts/>` 指定要发语音；
        合成不了就退回发文字（`send_private` / `send_group` 里那条降级）。
        """
        payload = delivery if isinstance(delivery, dict) else {}
        content = _text(pick(payload, 'content'))
        quote_id = pick(payload, 'quoteMessageId', 'quote_message_id')
        kind = _text(pick(payload, 'kind')).lower()
        voice = pick(payload, 'voice') is True
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
                voice=voice,
            )
        return await self.send_private(participant, content, _text(quote_id) or None, voice=voice)

    # ---- 平台动作执行层（`plugin/core/platform_actions.py` 的目录） ----

    def _client_for(self, target: Mapping[str, Any]) -> Any:
        """按会话坐标取 OneBot 客户端（拿不到返回 `None`，绝不抛）。"""
        return self.bridge.onebot_client(_clean(target.get('platform')), _clean(target.get('self_id')))

    async def _call_onebot_on(
        self, client: Any, action: str, params: Any, *, fire_and_forget: bool = False,
    ) -> dict[str, Any]:
        """在对外的 `call_onebot` 与 `set_input_status` 之间复用的一段。

        `fire_and_forget=True`（`set_input_status` 这种**即发即忘**的接口）：
        平台回了 non-dict / 空帧一律**当成功**——NapCat 不保证给回执，"没有回执"
        不是失败；这一路**完全不打日志**（失败原因照样原样回给调用方）。降噪与
        "确实不支持"的那一条 warn 由 `core` 侧按会话节流后打——那边知道真实会话
        类型，标签才不会被平台文案里的"群聊"两个字带偏（用户贴日志点名）。
        """
        name = _clean(action)
        if not name:
            return {'ok': False, 'error': 'OneBot 动作名为空', 'data': None}
        if client is None:
            if not fire_and_forget:
                log_fallback(
                    'warn',
                    '没有可用的 OneBot 客户端，动作 %s 未执行'
                    '（平台不是 aiocqhttp/NapCat，或平台实例 / 机器人连接未就绪）',
                    name,
                )
            return {
                'ok': False,
                'unsupported': True,
                'error': '当前平台实例没有可用的 OneBot 客户端（aiocqhttp/NapCat 才有），%s 未执行' % name,
                'data': None,
            }
        call = getattr(client, 'call_action', None)
        if not callable(call):
            if not fire_and_forget:
                log_fallback('warn', 'OneBot 客户端没有 call_action，动作 %s 未执行', name)
            return {
                'ok': False,
                'unsupported': True,
                'error': 'OneBot 客户端不支持 call_action，%s 未执行' % name,
                'data': None,
            }
        payload = dict(params) if isinstance(params, Mapping) else {}
        try:
            frame = call(name, **payload)
            if inspect.isawaitable(frame):
                frame = await frame
        except Exception as error:  # noqa: BLE001 - 传输异常可能已在平台侧生效
            # 超时 / 断连时请求可能已经被服务端执行：**不能**让调用方自动重试。
            if not fire_and_forget:
                log_fallback('warn', 'OneBot 动作 %s 传输异常（%s）：%s', name, _AMBIGUOUS_TAIL, error)
            return {
                'ok': False,
                'error': '%s 调用异常：%s；%s' % (name, error, _AMBIGUOUS_TAIL),
                'data': None,
                'ambiguous': True,
            }
        if _frame_ok(frame):
            return {'ok': True, 'error': '', 'data': frame.get('data') if isinstance(frame, Mapping) else None}
        if fire_and_forget and not _frame_has_verdict(frame):
            # 即发即忘的接口：平台没给 dict 回执（`None` / 空帧 / 别的形状）——按成功记。
            # NapCat 的 `set_input_status` 就是这样：早先判成"平台没有回执"失败，
            # 于是每次点亮都打两条 warn（用户贴日志点名）。
            return {
                'ok': True, 'error': '',
                'data': frame.get('data') if isinstance(frame, Mapping) else None,
            }
        error_text = _frame_error_text(name, frame)
        if not fire_and_forget:
            # 即发即忘的那一路不打这里：失败原因原样回给 core，由它按会话节流后记一条
            # debug（那里才知道真实会话类型，标签不会跟着平台文案跑）。
            log_fallback('warn', 'OneBot 动作执行失败：%s', error_text)
        result: dict[str, Any] = {'ok': False, 'error': error_text, 'data': None}
        if isinstance(frame, Mapping) and frame.get('retcode') is not None:
            result['retcode'] = frame.get('retcode')
        return result

    async def request_text(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[dict[str, str]] = None,
        data: Optional[dict[str, str]] = None,
        timeout_ms: int = 20_000,
    ) -> Optional[str]:
        """原始 HTTP（QQ 空间的 NapCat WebSocket 方案要用：自带 Cookie / Referer）。

        与 `AstrbotBridge.request_text` 同一口径：失败回 `None`；万一抛了异常
        （超时 / 断连），`call_qzone_cgi` 也按"请求可能已发生"处理（v1.7.6）。
        """
        return await self.bridge.request_text(
            method, url, headers=headers, data=data, timeout_ms=timeout_ms,
        )

    async def call_onebot(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        """原生 OneBot / SnowLuma 动作直通（QQ 空间等扩展动作走这里）。"""
        return await self._call_onebot_on(self._client_for(self.bridge.current_target()), action, params)

    async def platform_action(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        """执行一条目录动作（`platform_actions.ACTIONS` 里的 id）。

        流程：**会话坐标缺省**（`_SESSION_FILLS`）→ **目录校验**（类型 / 范围 / 枚举 /
        未知参数，全部按目录，见 `_validate_platform_action`）→ **翻成平台调用**
        （`_PLATFORM_CALLS` + `_PLATFORM_DEFAULTS` + `_PLATFORM_VALUE_MAPS`）→
        走 `call_onebot` 或适配层的本地实现（宿主 TTS / 好友群列表）→ **回执裁剪**
        （历史 ≤50、联系人 ≤200、群成员 ≤100，且每条只留必要字段）。

        任何一步失败都返回 `{'ok': False, 'error': <中文原因>, 'data': None}`：
        未知动作 `unknown-platform-action: …`、平台没有这条能力
        `unsupported-platform-action: <id>`（并记一条 warn）；**绝不抛异常**，
        也绝不假装成功。
        """
        name = _clean(action)
        if name not in _PLATFORM_CALLS:
            log_fallback('warn', '未知平台动作：%s（不在 platform_actions.ACTIONS 里）', name or '(空)')
            return _action_result(
                {'ok': False, 'error': 'unknown-platform-action: %s（动作不在平台动作目录里）' % (name or '(空)')},
                name,
            )
        raw = dict(params) if isinstance(params, Mapping) else {}
        # 会话坐标：桥登记的 + 调度层随参数带下来的（见 `_TARGET_PARAMS`）。
        target = _session_target(raw, self.bridge.current_target())
        # 会话坐标缺省先补（群号 / 用户号 / 机器人自己），再用目录校验类型与范围。
        filled = _apply_session_defaults(name, raw, target)
        normalized, reason = _validate_platform_action(name, filled)
        if normalized is None:
            log_fallback('warn', '平台动作参数不合法：%s', reason)
            return _action_result({'ok': False, 'error': reason}, name)
        # 之后一律用目录归一化后的参数：camelCase 已翻成 snake_case，越界值已被拒。
        filled = normalized
        platform_name, mapped, notes = _resolve_platform_call(name, filled, target)
        if platform_name == _PLATFORM_ACTION_UNSUPPORTED:
            log_fallback(
                'warn',
                '平台动作 %s 在当前宿主没有对应能力（排程由插件自己的意图 / 定时命令账本处理），已拒绝',
                name,
            )
            return _action_result({'ok': False, 'error': 'unsupported-platform-action: %s' % name}, name)
        if name == 'recall_message':
            message_id, note, error = await self._resolve_recall_message_id(filled, target)
            if error:
                log_fallback('warn', '%s', error)
                return _action_result({'ok': False, 'error': error}, name)
            mapped['message_id'] = message_id
            if note:
                notes.append(note)
        if platform_name == _PLATFORM_ACTION_LOCAL:
            result = await self._run_local_action(name, filled, target)
        elif not self.bridge.platform_is_onebot(target):
            log_fallback(
                'warn',
                '平台 %s 不支持 QQ/OneBot 动作，%s 已拒绝',
                _clean(target.get('platform')) or '(未知)', name,
            )
            return _action_result({'ok': False, 'error': 'unsupported-platform-action: %s' % name}, name)
        else:
            result = await self.call_onebot(platform_name, mapped)
            if result.get('ok'):
                result['data'] = _project_action_data(name, result.get('data'))
        return _action_result(result, name, notes)

    async def is_super_admin(self, user_id: str) -> bool:
        """宿主管理员名单判定（`admins_id`）；用于动作权限表的 `admin` 档。

        逐层判空：拿不到配置 / 名单不是 list / 用户号为空 → **False**。
        这是危险动作的唯一闸门，"读不到"只能等价于"没权限"。
        """
        target = _text(user_id)
        if not target:
            return False
        try:
            config = self.context.get_config() or {}
        except Exception:  # noqa: BLE001 - 宿主没这个 API 就当没有管理员名单
            return False
        admins = config.get('admins_id') if isinstance(config, dict) else None
        if not isinstance(admins, (list, tuple, set)):
            return False
        return any(_text(item) == target for item in admins)

    async def set_input_status(self, target: dict[str, Any], typing: bool) -> dict[str, Any]:
        """设置「正在输入」状态（NapCat `set_input_status`，`event_type` 1=开始 / 2=结束）。

        三条口径（用户 2026-09-28 点名）：

        * **只对私聊做**：群聊直接返回"不做"，一个平台调用都不发；
        * **即发即忘**：NapCat 不保证回 dict 回执，"没有回执"**当成功**；只有
          `status`/`retcode`/`message` 明确说失败才算失败；
        * **降噪**：传输层这一路**不打任何日志**（失败原因原样回给调用方）；
          "确实不支持"的一次性 warn 由 core 按会话节流后打
          （`chunk12._set_input_status`）——那里才知道真实会话类型。
        """
        coordinates = target if isinstance(target, Mapping) else {}
        user_id = _clean(pick(coordinates, 'userId', 'user_id'))
        if not user_id:
            log_fallback('debug', '设置输入状态已跳过：会话坐标里没有 user_id')
            return {'ok': False, 'error': '缺少 user_id，无法设置输入状态', 'data': None}
        group_id = _clean(pick(coordinates, 'groupId', 'group_id'))
        if coordinates.get('is_group') is True or (group_id and group_id != '0'):
            # 输入状态只对私聊做：群聊不点亮、不熄灭、不调用、不告警。
            return {'ok': False, 'error': 'group-typing-skipped', 'data': None}
        client = self._client_for(coordinates)
        # 这一路**完全静音**：失败原因原样回给 core，由它记一条 debug / 一条按会话
        # 节流的"不支持"warn——那边知道真实会话类型，标签不会被平台文案里的
        # "群聊"两个字带偏（用户贴日志点名）。
        return await self._call_onebot_on(
            client, 'set_input_status', {'user_id': user_id, 'event_type': 1 if typing else 2},
            fire_and_forget=True,
        )

    async def _resolve_recall_message_id(
        self,
        params: Mapping[str, Any],
        target: Mapping[str, Any],
    ) -> tuple[str, str, str]:
        """定位要撤回的平台消息号：返回 `(message_id, 说明, 错误)`。"""
        explicit = _clean(params.get('message_id'))
        if explicit:
            return explicit, '', ''
        choice = _clean(pick(params, 'target')).lower() or 'last'
        if choice == 'entry':
            return '', '', (
                '撤回失败：按条目编号（entry_id=%s）撤回需要「剧本条目 → 平台消息号」的映射表，'
                '当前宿主不提供；请改用 target=last 或直接给 message_id'
                % (_clean(params.get('entry_id')) or '?')
            )
        recorded = self.bridge.last_delivered_message_id()
        if recorded:
            return recorded, '撤回的是最近一条已投递消息（%s）' % recorded, ''
        found, reason = await self._last_self_message_id(target)
        if found:
            return found, '撤回的是历史里最近一条机器人自己发的消息（%s）' % found, ''
        return '', '', '撤回失败：拿不到要撤回的平台消息号（%s）' % (reason or '最近没有已投递记录')

    async def _last_self_message_id(self, target: Mapping[str, Any]) -> tuple[str, str]:
        """宿主不回传消息号时的兜底：翻最近 20 条历史，找机器人自己发的那条。

        只读动作（`get_group_msg_history` / `get_friend_msg_history`），不改任何东西。
        """
        self_id = _clean(target.get('self_id'))
        if target.get('is_group'):
            group_id = _clean(target.get('group_id'))
            if not group_id:
                return '', '当前会话没有群号'
            result = await self.call_onebot('get_group_msg_history', {'group_id': group_id, 'count': 20})
        else:
            user_id = _clean(target.get('user_id'))
            if not user_id:
                return '', '当前会话没有对话对象'
            result = await self.call_onebot('get_friend_msg_history', {'user_id': user_id, 'count': 20})
        if not result.get('ok'):
            return '', _text(result.get('error')) or '拉历史消息失败'
        data = result.get('data')
        rows = data.get('messages') if isinstance(data, Mapping) else data
        for row in reversed(_as_list(rows)):
            if not isinstance(row, Mapping):
                continue
            sender = row.get('sender') if isinstance(row.get('sender'), Mapping) else {}
            uid = _clean(sender.get('user_id') or row.get('user_id') or row.get('sender_id'))
            message_id = _clean(row.get('message_id') or row.get('messageId'))
            if message_id and uid and (not self_id or uid == self_id):
                return message_id, ''
        return '', '最近 20 条历史里没有机器人自己发的消息'

    # ---- `@local` 动作：适配层自己实现 ----

    async def _run_local_action(
        self,
        action_id: str,
        params: Mapping[str, Any],
        target: Mapping[str, Any],
    ) -> dict[str, Any]:
        if action_id == 'send_voice':
            return await self._action_send_voice(params, target)
        if action_id == 'list_voices':
            return self._action_list_voices()
        if action_id in ('list_contacts', 'search_contacts'):
            return await self._action_contacts(action_id, params)
        log_fallback('warn', '平台动作 %s 标记为本地实现，但适配层没有对应分支', action_id)
        return {'ok': False, 'error': 'unsupported-platform-action: %s' % action_id}

    def _target_umo(self, target_text: str, target: Mapping[str, Any]) -> str:
        """`send_voice` 的目标：完整 UMO / 用户号 / 空（=本回合对话对象）。"""
        value = _clean(target_text)
        if value and ':' in value:
            return value
        if value:
            platform = _clean(target.get('platform'))
            self_id = _clean(target.get('self_id'))
            if platform and self_id:
                umo = self.bridge.private_umo(platform, self_id, value)
                if umo:
                    return umo
        return _clean(target.get('umo'))

    async def _action_send_voice(
        self,
        params: Mapping[str, Any],
        target: Mapping[str, Any],
    ) -> dict[str, Any]:
        """`send_voice`：走**宿主 TTS**（不是 NapCat 的 AI 声聊），再发 `Record`。

        合成这一段与正文 `<tts/>` 标记**共用** `synthesize_voice()`（v1.7.7）：
        音色解析、指名服务商的纪律、失败语义只有一份，两条路径不会漂移。
        """
        text = _clean(params.get('content'))
        if not text:
            return {'ok': False, 'error': 'send_voice 需要非空文本'}
        umo = self._target_umo(_clean(params.get('target')), target)
        if not umo:
            return {'ok': False, 'error': 'send_voice 找不到投递会话（当前没有可用的回合对象）'}
        path, error = await self.synthesize_voice(text, umo, _clean(params.get('voice')))
        if error:
            log_fallback('warn', '发语音失败：%s', error)
            return {'ok': False, 'error': error}
        result = await self._send_chain(umo, [_build_record({'file': path})])
        if result.get('ok'):
            result['data'] = {'umo': umo, 'voice': self._voice_label(), 'file': path}
        return result

    # ---- 文字转语音（v1.7.7）：正文 `<tts/>` 标记与 `send_voice` 动作共用一份合成 ----

    def voice_reply_enabled(self) -> bool:
        """`model_center.audio.tts_enabled`（v1.7.7）：「文字转语音」总开关。

        **缺键按 `True`**：这个键是 v1.7.7 才有的，旧配置文件里没有它时不能把
        "她能发语音"悄悄关掉（`send_voice` 从 v1.7.2 起就是可用的）。关掉之后：
        正文 `<tts/>` 标记被忽略（退回发文字），`send_voice` / `list_voices` 也走
        既有的"动作开关关掉"路径变成不可用——**同一个开关的两种表现**。
        """
        value = self.bridge.section('audio').get('tts_enabled')
        return True if value is None else bool(value)

    def _voice_label(self) -> str:
        """回执里那句"用了哪个音色"：与 `_tts_provider` 同一套选择口径。

        指名的服务商取它自己的标签；没指名就用宿主当前那个（列表第一个）；
        列表都拿不到时退回配置里写的 id（信息性字段，取不到也不影响投递）。
        """
        wanted = self.tts_provider_id()
        if wanted:
            provider = self._tts_provider_by_id(wanted)
            return self._provider_label(provider) if provider is not None else wanted
        providers = self._tts_providers()
        return self._provider_label(providers[0]) if providers else ''

    async def synthesize_voice(
        self, text: str, umo: str, voice: str = '',
    ) -> tuple[str, str]:
        """把一段文本合成为语音文件：返回 `(音频路径, 错误原因)`，成功时错误为空串。

        **绝不抛异常**：调用方按返回值走"退回落文字"或"明确失败"分支。
        这里是**唯一**的合成本体——`send_voice` 动作与正文 `<tts/>` 标记都走它。
        """
        if not self.voice_reply_enabled():
            return '', '文字转语音已关闭（模型中心 → 语音 / 音频理解设置 → 文字转语音）'
        provider, provider_error = await self._tts_provider(umo, _clean(voice))
        if provider is None:
            return '', provider_error
        # `_tts_provider` 已经过一遍 `_tts_capability_error`：拿到手的服务商一定有 get_audio。
        getter = provider.get_audio
        try:
            audio = getter(text)
            if inspect.isawaitable(audio):
                audio = await audio
        except Exception as error:  # noqa: BLE001 - 合成失败按投递失败处理
            log_fallback('warn', 'TTS 合成失败 提供者=%s 错误=%s', self._provider_label(provider), error)
            return '', 'TTS 合成失败：%s' % error
        path = _clean(audio)
        if not path or not os.path.exists(path):
            log_fallback('warn', 'TTS 没有产出音频文件（%s）', path or '(空)')
            return '', 'TTS 没有产出可用的音频文件'
        return path, ''

    async def _send_marked_voice(
        self, umo: str, content: str, reply_to: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """正文 `<tts/>` 标记的投递：先合成语音，合成不了就**返回 `None`**（调用方发文字）。

        只有"**平台一次都没被碰过**"的失败才退回文字（没配 TTS / 服务商不可用 /
        合成报错 / 没有音频文件）——这些情况下发文字绝不会重复。
        语音**已经交给平台、回执未知**时（`ok=False`）返回那份失败：与既有的文本投递
        同一条纪律——**不重投**（重投会产生重复消息），由调用方按投递失败记账并打 warn。
        """
        path, error = await self.synthesize_voice(content, umo)
        if error:
            log_fallback(
                'warn', '正文语音标记投递失败，已退回发文字（内容不丢）会话=%s 原因=%s', umo, error,
            )
            return None
        components = [_build_record({'file': path})]
        if reply_to:
            components.insert(0, Reply(id=_text(reply_to)))
        result = await self._send_chain(umo, components)
        if result.get('ok'):
            result['data'] = {'umo': umo, 'voice': self._voice_label(), 'file': path, 'marked': True}
            return result
        log_fallback(
            'warn',
            '正文语音标记发送失败（回执未知，不再重投文字以免重复）会话=%s 错误=%s',
            umo, result.get('error') or '',
        )
        return result

    def _tts_providers(self) -> list[Any]:
        """宿主里所有 TTS 提供者（拿不到就是空列表）。"""
        for attribute in ('get_all_tts_providers',):
            getter = getattr(self.context, attribute, None)
            if callable(getter):
                try:
                    return list(getter() or [])
                except Exception as error:  # noqa: BLE001
                    log_fallback('debug', '取 TTS 提供者列表失败（%s）：%s', attribute, error)
        manager = getattr(self.context, 'provider_manager', None)
        for attribute in ('tts_provider_insts',):
            instances = getattr(manager, attribute, None)
            if isinstance(instances, (list, tuple)):
                return list(instances)
        return []

    def _provider_from_manager(self) -> Any:
        manager = getattr(self.context, 'provider_manager', None)
        current = getattr(manager, 'curr_tts_provider_inst', None)
        if current is not None:
            return current
        providers = self._tts_providers()
        return providers[0] if providers else None

    @staticmethod
    def _provider_config(provider: Any) -> Mapping[str, Any]:
        config = getattr(provider, 'provider_config', None)
        if isinstance(config, Mapping):
            return config
        config = getattr(provider, 'config', None)
        return config if isinstance(config, Mapping) else {}

    def _provider_id(self, provider: Any) -> str:
        config = self._provider_config(provider)
        for key in ('id', 'provider_id', 'providerId'):
            value = _clean(config.get(key))
            if value:
                return value
        return type(provider).__name__

    def _provider_label(self, provider: Any) -> str:
        config = self._provider_config(provider)
        model = _clean(config.get('model') or config.get('voice'))
        identifier = self._provider_id(provider)
        return '%s(%s)' % (identifier, model) if model and model != identifier else identifier

    @staticmethod
    def _voice_hint(config: Mapping[str, Any]) -> str:
        for key, value in (config or {}).items():
            if 'voice' in str(key).lower() and isinstance(value, str) and value.strip():
                return value.strip()
        return ''

    # ---- 指名的宿主 TTS 服务商（「模型中心 → 语音 / 音频理解设置」的 `tts_provider_id`） ----
    #
    # 与模型侧的「任务指名 AstrBot Provider」是同一件事：用户在配置里明确选了哪一个
    # 宿主服务商，就用哪一个；没选就沿用宿主当前的那个。
    #
    # v1.7.5：`tts_provider_id` / `default_voice` 从「机器人动作 → 会话动作」
    # （`robot_actions.chat`，**可见组**）搬到了「模型中心 → 语音 / 音频理解设置」
    # （`model_center.audio`）——它们是"用哪个模型/音色"，跟"允许她做哪些动作"不是一回事。
    # 旧位置留在 schema 里当**字段级 `invisible: true`** 的兼容位（宿主不删、界面不显示），
    # 读取优先新位置、旧位置兜底，写方向只写新位置：单一实现源是
    # `core/service/config.py` 的 `LEGACY_KEY_MERGES`（键级搬迁表）。
    # 注意：**动作开关**（`send_voice` / `list_voices`）仍在 `robot_actions.chat`，
    # 所以这里不再从动作目录算分组名。

    def voice_config_group(self) -> str:
        """语音两项的配置分组（点分路径 `model_center.audio`）。

        单一实现源是 `core/service/config.py` 的 `VOICE_SECTION`（键级搬迁表的目标），
        这里只转发一次，别写死字符串——写死会在下一次搬迁时静默读空（坑 66 那类事故）。
        """
        return VOICE_SECTION

    def _voice_option(self, key: str) -> str:
        return _clean(self.bridge.section(self.voice_config_group()).get(key))

    def tts_provider_id(self) -> str:
        """`tts_provider_id`：指名的 AstrBot TTS 服务商（留空 = 用当前默认 TTS）。"""
        return self._voice_option('tts_provider_id')

    def default_voice(self) -> str:
        """`default_voice`：**服务商内**的音色名（不是服务商 id）。"""
        return self._voice_option('default_voice')

    def _tts_list_exposed(self) -> bool:
        """宿主有没有「列出 TTS 服务商」的入口。

        用来区分两件看起来一样、处理方式却相反的事：

        * **宿主没暴露列表**（老版本 / 精简宿主）→ 无从判断这个 id 存不存在，
          硬失败等于把发语音整条功能关掉，只能 warn + 回落默认；
        * **宿主有列表，但里面没有这个 id** → 用户指名错了，明确失败、绝不回落
          （与模型侧指名 Provider、作品写手同一条纪律）。
        """
        if callable(getattr(self.context, 'get_all_tts_providers', None)):
            return True
        manager = getattr(self.context, 'provider_manager', None)
        return isinstance(getattr(manager, 'tts_provider_insts', None), (list, tuple))

    def _inst_by_id(self, provider_id: str) -> Any:
        """宿主 `inst_map` 里按 id 取实例（**只为把"不存在"与"不是 TTS"分开报**）。"""
        manager = getattr(self.context, 'provider_manager', None)
        mapping = getattr(manager, 'inst_map', None)
        if not isinstance(mapping, Mapping):
            return None
        wanted = _clean(provider_id)
        if wanted in mapping:
            return mapping[wanted]
        for key, value in mapping.items():
            if str(key).lower() == wanted.lower():
                return value
        return None

    def _tts_provider_by_id(self, provider_id: str) -> Any:
        """在宿主的 TTS 服务商里按 id 找（找不到 `None`）。

        刻意**不走** `context.get_provider_by_id`：那个入口对不存在的 id 会往宿主日志
        打一条 `Provider … was not found.`（坑 23 记过它的误导性），而"用户还没在
        AstrBot 里配这个 TTS"根本不是异常。只比对 id / name，不拿 model 当 id 用。
        """
        wanted = _clean(provider_id).lower()
        if not wanted:
            return None
        for provider in self._tts_providers():
            config = self._provider_config(provider)
            candidates = {
                self._provider_id(provider).lower(),
                _clean(config.get('id')).lower(),
                _clean(config.get('provider_id')).lower(),
                _clean(config.get('name')).lower(),
            }
            if wanted in {item for item in candidates if item}:
                return provider
        return None

    def _tts_capability_error(self, provider: Any) -> str:
        """这个服务商能不能做文字转语音；能就返回空串，不能就给一句人看的原因。

        口吻与模型侧按 `modalities` 校验一致：**只信 Provider 自己声明的能力**。
        TTS Provider 声明了模态却没有一项是 TTS 相关时才判"声明了但不支持"；
        没声明（空 / 缺失）一律照常——"没声明"不等于"不支持"。
        """
        label = self._provider_label(provider)
        config = self._provider_config(provider)
        declared = config.get('modalities') if isinstance(config, Mapping) else None
        if isinstance(declared, (list, tuple, set)):
            tokens = {_clean(item).lower() for item in declared if _clean(item)}
            if tokens and not (tokens & _TTS_MODALITIES):
                return (
                    'TTS 服务商 %s 没有声明文字转语音能力（已声明=%s）：请在 AstrBot 里换一个 '
                    'TTS 服务商，或把「语音 / 音频理解」的这一项改选'
                    % (label, '/'.join(sorted(tokens)))
                )
        if not callable(getattr(provider, 'get_audio', None)):
            return (
                'TTS 服务商 %s 没有 t2s 能力（缺 get_audio）：它不是文字转语音模型，'
                '请在 AstrBot 里换一个 TTS 服务商，或把「语音 / 音频理解」的这一项改选' % label
            )
        return ''

    def _named_tts_missing_error(self, provider_id: str) -> str:
        """指名了却找不到时的报错文案（顺带说清"它其实是别的类型的模型"）。"""
        other = self._inst_by_id(provider_id)
        if other is not None:
            return (
                '「语音 / 音频理解」指名的 %s 不是 AstrBot 的 TTS 服务商（它是 %s）：'
                '请改选一个文字转语音模型，或把这一项留空用当前默认 TTS'
                % (provider_id, type(other).__name__)
            )
        available = [self._provider_id(item) for item in self._tts_providers()]
        return (
            '「语音 / 音频理解」指名的 TTS 服务商 %s 不存在（AstrBot 里可用的：%s）：'
            '请确认这个服务商，或把这一项留空用当前默认 TTS'
            % (provider_id, '、'.join(item for item in available if item) or '无')
        )

    def _note_named_voice(self, provider: Any, voice: str) -> None:
        """指名的服务商**固定用它自己那份音色配置**：音色名对不上时留一条 debug。

        音色（`voice` 参数 / `default_voice`）是"服务商内的音色名"，不是选服务商的手段；
        指名了服务商之后它不该再把这次调用换到另一个服务商上——那会让用户配的
        「用了 A」和实际听到的声音对不上。
        """
        wanted = _clean(voice) or self.default_voice()
        if not wanted:
            return
        hint = self._voice_hint(self._provider_config(provider))
        if hint and wanted.lower() != hint.lower():
            log_fallback(
                'debug',
                '音色 %s 不是指名的 TTS 服务商 %s 当前用的音色（%s）；本次仍用指名的服务商',
                wanted, self._provider_label(provider), hint,
            )

    def _pick_voice_provider(self, voice: str) -> Any:
        wanted = _clean(voice).lower()
        if not wanted:
            return None
        for provider in self._tts_providers():
            config = self._provider_config(provider)
            candidates = {
                self._provider_id(provider).lower(),
                _clean(config.get('model')).lower(),
                _clean(config.get('name')).lower(),
                self._voice_hint(config).lower(),
            }
            if wanted in {item for item in candidates if item}:
                return provider
        return None

    async def _tts_provider(self, umo: str, voice: str = '') -> tuple[Any, str]:
        """取这次要用的宿主 TTS 服务商：返回 `(provider, 错误文案)`。

        解析优先级：

        1. **指名的 `tts_provider_id`**（「语音 / 音频理解设置」）→ 按 id 取那一个（留空跳过）；
        2. 宿主当前默认 TTS：`get_using_tts_provider_async` → `get_using_tts_provider`
           → `provider_manager.curr_tts_provider_inst`（逐层判空，与历史行为一致）。

        **指名了却找不到时不回落**（与模型侧指名 Provider、作品写手同一条纪律）：
        指名是用户在配置里做出的明确选择，偷偷换一个服务商只会把"配了没用 / 声音不对"
        变成查不出来的静默问题。唯一不硬失败的例外是宿主**根本没有**列出 TTS 的入口
        （见 `_tts_list_exposed`）——那时无从判断这个 id 存不存在，只能 warn + 回落。
        """
        wanted = self.tts_provider_id()
        if not wanted:
            return await self._default_tts_provider(umo, voice)
        if not self._tts_list_exposed():
            log_fallback(
                'warn',
                '宿主没有暴露 TTS 服务商列表，无法按 id 取 %s；本次回落 AstrBot 当前默认 TTS',
                wanted,
            )
            return await self._default_tts_provider(umo, voice)
        provider = self._tts_provider_by_id(wanted)
        if provider is None:
            # 调用方（send_voice / list_voices）会把它记成 warn，这里不重复打日志。
            return None, self._named_tts_missing_error(wanted)
        error = self._tts_capability_error(provider)
        if error:
            return None, error
        self._note_named_voice(provider, voice)
        return provider, ''

    async def _default_tts_provider(self, umo: str, voice: str = '') -> tuple[Any, str]:
        """宿主当前默认的 TTS 服务商；给了音色名就按音色挑（既有行为）。

        留空 `tts_provider_id` 时 `voice` / `default_voice` 可以指向**另一个**服务商
        实例——宿主把音色配在服务商自己的配置里，所以"挑音色"在实现上就是"挑实例"。
        """
        provider: Any = None
        for attribute in ('get_using_tts_provider_async', 'get_using_tts_provider'):
            getter = getattr(self.context, attribute, None)
            if not callable(getter):
                continue
            try:
                result = getter(umo)
                if inspect.isawaitable(result):
                    result = await result
            except Exception as error:  # noqa: BLE001 - 取不到就换下一条路
                log_fallback('debug', '取宿主 TTS 提供者失败（%s）：%s', attribute, error)
                result = None
            if result is not None:
                provider = result
                break
        if provider is None:
            provider = self._provider_from_manager()
        if provider is None:
            return None, '宿主没有可用的 TTS 提供者：发语音需要先在 AstrBot 里配置一个 TTS 服务商'
        wanted = _clean(voice) or self.default_voice()
        if wanted:
            chosen = self._pick_voice_provider(wanted)
            if chosen is not None:
                provider = chosen
            else:
                log_fallback(
                    'debug', '音色 %s 没有匹配到 TTS 提供者，改用默认提供者（可用音色见 list_voices）', wanted,
                )
        error = self._tts_capability_error(provider)
        if error:
            return None, error
        return provider, ''

    def _action_list_voices(self) -> dict[str, Any]:
        """`list_voices`：列出**这次会用的那个**服务商能用的音色。

        指名了 `tts_provider_id` 就只列它（取不到 → 与 `send_voice` 一样明确失败，
        不假装成功）；留空时把**默认服务商排在最前**，后面跟上宿主里其它 TTS 服务商
        ——留空时 `send_voice` 的 `voice` 参数本来就能指定另一个服务商，列全了模型才
        知道有哪些可选。
        """
        wanted = self.tts_provider_id()
        if wanted:
            if not self._tts_list_exposed():
                log_fallback('warn', '列音色失败：宿主没有暴露 TTS 服务商列表，读不到指名的 %s', wanted)
                return {'ok': False, 'error': '宿主没有暴露 TTS 服务商列表，列不出音色'}
            provider = self._tts_provider_by_id(wanted)
            if provider is None:
                error = self._named_tts_missing_error(wanted)
                log_fallback('warn', '列音色失败：%s', error)
                return {'ok': False, 'error': error}
            error = self._tts_capability_error(provider)
            if error:
                log_fallback('warn', '列音色失败：%s', error)
                return {'ok': False, 'error': error}
            providers = [provider]
        else:
            providers = self._tts_providers()
            if not providers:
                log_fallback('warn', '列音色失败：宿主没有配置 TTS 提供者')
                return {'ok': False, 'error': '宿主没有可用的 TTS 提供者，列不出音色'}
            default = self._provider_from_manager()
            if default is not None and any(item is default for item in providers):
                providers = [default] + [item for item in providers if item is not default]
        voices = [
            {
                'id': self._provider_id(provider),
                'provider': type(provider).__name__,
                'voice': self._voice_hint(self._provider_config(provider)),
            }
            for provider in providers
        ][:20]
        return {
            'ok': True,
            'error': '',
            'data': {'voices': voices, 'note': 'send_voice 的 voice 参数填这里的 id（音色由该服务商的配置决定）'},
        }

    async def _action_contacts(self, action_id: str, params: Mapping[str, Any]) -> dict[str, Any]:
        """`list_contacts` / `search_contacts`：好友 + 群列表（宿主 / 平台取，逐条裁剪）。"""
        keyword = _clean(params.get('keyword')).lower()
        kind = _clean(params.get('type')).lower() or ('friends' if keyword else 'all')
        default_limit = _SEARCH_CONTACT_LIMIT if keyword else _CONTACT_LIMIT
        try:
            limit = int(params.get('limit') or 0)
        except (TypeError, ValueError):
            limit = 0
        limit = min(limit if limit > 0 else default_limit, default_limit)
        wanted = {
            'friends': ('friends',), 'groups': ('groups',), 'all': ('friends', 'groups'),
        }.get(kind, ('friends', 'groups'))
        results: dict[str, dict[str, Any]] = {}
        if 'friends' in wanted:
            results['friends'] = await self.call_onebot('get_friend_list', {})
        if 'groups' in wanted:
            results['groups'] = await self.call_onebot('get_group_list', {})
        if not any(item.get('ok') for item in results.values()):
            error = next((_text(item.get('error')) for item in results.values() if item.get('error')), '平台没有回执')
            log_fallback('warn', '取联系人失败：%s', error)
            return {'ok': False, 'error': '取联系人失败：%s' % error}
        entries: list[dict[str, Any]] = []
        if results.get('friends', {}).get('ok'):
            entries += [_brief_friend(row) for row in _as_list(results['friends'].get('data'))]
        if results.get('groups', {}).get('ok'):
            entries += [_brief_group(row) for row in _as_list(results['groups'].get('data'))]
        if keyword:
            entries = [entry for entry in entries if _contact_matches(entry, keyword)]
        total = len(entries)
        notes = [
            '%s列表没取到：%s' % ('好友' if key == 'friends' else '群', item.get('error'))
            for key, item in results.items() if not item.get('ok')
        ]
        return {
            'ok': True,
            'error': '',
            'data': {'contacts': entries[:limit], 'total': total, 'type': kind, 'truncated': total > limit},
            'notes': notes,
        }


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
#: 纯装饰/导航容器：整块丢掉，否则正文提取会被语言下拉、菜单、图标标签淹没。
#: 判据是「里面不会有文章正文」——`select`/`option`（SearXNG 的语言列表能占两千多字）、
#: `svg`（图标路径里的 `<title>` 会变成乱码词）、`button`/`nav`/`footer`/`noscript`。
#: **不丢 `<header>` 与 `<form>`**：前者常常就是文章标题所在，后者可能带正文。
_CHROME_RE = re.compile(
    r'<(select|option|svg|button|nav|footer|noscript)\b[^>]*>.*?</\1>',
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r'<[^>]+>')
_TITLE_RE = re.compile(r'<title[^>]*>(.*?)</title>', re.IGNORECASE | re.DOTALL)
_WS_RE = re.compile(r'\s{2,}')


def _strip_html(html: str) -> str:
    text = _SCRIPT_STYLE_RE.sub(' ', html)
    text = _CHROME_RE.sub(' ', text)
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
    """`ctx.logger('hds-interlude')` 的等价物（`service.emit_log` 读它）。

    这里也负责把 core 的 report / standalone 日志留一份到控制台缓冲：那些日志走
    `write_report` → `emit_log` → 本适配器（**单次投递**），不再经过 `set_log_sink`
    的 sink（见坑 49），所以缓冲得在这里补上，否则「运行日志」面板会看不到叙事侧的行。
    """

    def __init__(self, logger: Any, buffer: Any = None) -> None:
        self._logger = logger
        self._buffer = buffer

    def _buffer_line(self, level: str, text: str) -> None:
        if self._buffer is None:
            return
        try:
            self._buffer.append({
                'at': _now_iso(), 'level': level, 'text': _ANSI_RE.sub('', text),
            })
        except Exception:  # pragma: no cover - 缓冲失败不影响日志本身
            pass

    def _write(self, level: str, text: str) -> None:
        self._buffer_line(level, text)
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
        #: 已经警告过的「模态不匹配」(provider_id, 需要的模态)，避免每轮刷屏
        self._modality_warned: set[tuple[str, tuple[str, ...]]] = set()

    @property
    def context(self) -> Context:
        return self.bridge.context

    async def post_json(
        self,
        url: str,
        headers: Optional[dict[str, str]] = None,
        body: Any = None,
        timeout: Optional[int] = None,
        task: Optional[str] = None,
    ) -> Any:
        payload = body if isinstance(body, dict) else {}
        # 优先级（从高到低）：
        #
        # 1. **该任务在 `model_center.task_models` 里指名了 AstrBot Provider** → 用它。
        #    这是用户针对单个任务做出的明确选择，比连接行的用途勾选更具体。
        # 2. **插件自己在 `model_center.providers` 里配了 endpoint** → 直连它。
        #    上游本来就是「每类任务一条自己的 provider」（main / compaction / timeline /
        #    alter / embedding / stickers / vision），任务级路由与连接级参数
        #    （temperature / max_tokens / response_format / extra_body / 思考开关）全都挂在
        #    那条连接上。无条件改道 AstrBot Provider 会让这些配置静默失效——实测过：
        #    插件指向桩服务、日志也显示用桩的模型名，请求却仍打到 AstrBot 的 Ollama 上。
        # 3. 两者都没有 → 回落到 AstrBot 的默认 Provider，保留「不想单独填 key，
        #    直接复用 AstrBot 里配好的模型」这条便利路径。
        bound = self.bridge.task_model_id(task)
        explicit_endpoint = isinstance(url, str) and url.strip().lower().startswith(('http://', 'https://'))
        if 'messages' in payload and (bound or not explicit_endpoint):
            routed = await self._chat(payload, timeout, task=task, provider_id=bound)
            if routed is not None:
                self._record_usage(task, routed)
                return routed
        if 'input' in payload and ('model' in payload or 'dimensions' in payload) and not explicit_endpoint:
            routed = await self._embedding(payload)
            if routed is not None:
                return routed
        response = await self._fallback.post_json(url, headers, body, timeout)
        self._record_usage(task, response)
        return response

    def _record_usage(self, task: Optional[str], response: Any) -> None:
        """把响应里的 `usage` 记进控制台的内存环形缓冲（**只做展示，不参与计费**）。

        放在传输层是有意的：无论请求走的是 AstrBot Provider 还是插件自己的连接，
        都会经过 `post_json`，所以这一处就能覆盖两条路。真正给用户看的 token 用量
        与费用仍由 core 的 `report_token_usage` 负责；这里只是让 WebUI 有个"刚刚花了多少"
        的即时视图，重启即清空。

        拿不到 usage（很多网关不报）就什么都不记，不编造数据。
        """
        if not isinstance(response, dict):
            return
        usage = response.get('usage')
        if not isinstance(usage, dict):
            return
        prompt = _usage_field(usage, 'prompt_tokens', 'input_tokens')
        completion = _usage_field(usage, 'completion_tokens', 'output_tokens')
        total = _usage_field(usage, 'total_tokens') or (prompt + completion)
        if not (prompt or completion or total):
            return
        try:
            self.bridge.usage_records.append({
                'at': _now_iso(),
                'task': task or '',
                'model': _text(response.get('model')),
                'prompt_tokens': prompt,
                'completion_tokens': completion,
                'total_tokens': total,
            })
        except Exception:  # pragma: no cover - 记账失败不能影响请求
            pass

    def iterate_sse(
        self,
        url: str,
        headers: Optional[dict[str, str]] = None,
        body: Any = None,
        timeout: Optional[int] = None,
        task: Optional[str] = None,
    ) -> AsyncIterator[str]:
        """流式请求：**该任务指名了 AstrBot Provider 时**退化成"单块流"。

        AstrBot 的 `llm_generate` 是整段返回的，没有增量通道。但调用方
        （`request_openai_compatible_streaming`）本身就有兜底：收不到 SSE 增量时
        会把累积到的原文当普通 JSON 解析一次。所以这里发一次**非流式**请求、把整个
        响应体当唯一一块吐出去——模型仍然是用户指名的那一个，只是失去"首泡加速"，
        而不是打到一个完全不相干的 endpoint 上（更不是拿空 URL 去撞 httpx）。
        """
        if self.bridge.task_model_id(task):
            return self._single_chunk_stream(url, headers, body, timeout, task)
        return self._fallback.iterate_sse(url, headers, body, timeout)

    async def _single_chunk_stream(
        self,
        url: str,
        headers: Optional[dict[str, str]],
        body: Any,
        timeout: Optional[int],
        task: Optional[str],
    ) -> AsyncIterator[str]:
        """把一次非流式路由结果包装成"只有一个块"的流。"""
        payload = body if isinstance(body, dict) else {}
        routed = await self.post_json(url, headers, payload, timeout, task=task)
        yield json.dumps(routed, ensure_ascii=False)

    # ---- chat ----

    async def _chat(
        self,
        payload: dict[str, Any],
        timeout: Optional[int],
        task: Optional[str] = None,
        provider_id: str = '',
    ) -> Optional[dict[str, Any]]:
        # 该任务指名了 AstrBot Provider 就用它（比会话默认模型更具体）；
        # 留空 / 没配过才回落到会话当前模型。
        if not provider_id:
            provider_id = self.bridge.task_model_id(task)
        if not provider_id:
            provider_id = await self.bridge.resolve_chat_provider_id()
        if not provider_id:
            log_fallback('debug', 'AstrBot 未解析到聊天 Provider：回落到直连 endpoint')
            return None
        system_parts: list[str] = []
        conversation: list[str] = []
        image_urls: list[str] = []
        audio_urls: list[str] = []
        for message in payload.get('messages') or []:
            if not isinstance(message, dict):
                continue
            role = _text(message.get('role') or 'user')
            content = message.get('content')
            if isinstance(content, list):
                # 多模态回合的 content 是分段数组（text / image_url / input_audio）。
                # **不能只取 text**：原图上文一旦被丢掉，模型看到的就是"用户在说看这张图"
                # 却没有任何图——静默失明比报错难查得多。AstrBot 的 `llm_generate` 原生
                # 支持 `image_urls` / `audio_urls`，这里原样转交。
                parts: list[str] = []
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    kind = _text(part.get('type'))
                    if kind == 'image_url':
                        url = part.get('image_url')
                        url = url.get('url') if isinstance(url, dict) else url
                        if _text(url):
                            image_urls.append(_text(url))
                    elif kind == 'input_audio':
                        audio = part.get('input_audio')
                        if isinstance(audio, dict) and _text(audio.get('data')):
                            audio_urls.append(_audio_data_uri(audio))
                    else:
                        parts.append(_text(part.get('text')))
                content = ' '.join(parts)
            text = _text(content)
            if not text and not (image_urls or audio_urls):
                continue
            if role == 'system':
                system_parts.append(text)
            elif role == 'assistant':
                conversation.append('assistant: %s' % text)
            else:
                conversation.append(text)
        # 语音：配了转写模型就转成文字并进 prompt（主模型不必支持音频）
        if audio_urls:
            transcript = await self._transcribe(audio_urls)
            if transcript:
                conversation.append(transcript)
                audio_urls = []
        # 图片：`vision.mode = native` 时主模型自己看；sidecar 模式下图片本来就不该
        # 出现在主叙事请求里（core 已经换成侧端观察），所以这里只在 native 时报能力。
        if image_urls and not self.bridge.vision_mode_native():
            image_urls = []
        image_urls, audio_urls = self._filter_unsupported_modalities(
            provider_id, image_urls, audio_urls, task,
        )
        params: dict[str, Any] = {}
        if payload.get('temperature') is not None:
            params['temperature'] = payload['temperature']
        if payload.get('top_p') is not None:
            params['top_p'] = payload['top_p']
        if payload.get('max_tokens') is not None:
            params['max_tokens'] = payload['max_tokens']
        if image_urls:
            params['image_urls'] = image_urls
        if audio_urls:
            params['audio_urls'] = audio_urls
        try:
            response = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt='\n\n'.join(conversation),
                system_prompt='\n\n'.join(system_parts) or None,
                **params,
            )
        except TypeError:
            # 某些 Provider 不接受采样参数 / 多模态参数：去掉后重试一次。
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

    def _known_modalities(self, provider_id: str) -> Optional[set[str]]:
        """读该 Provider 声明的模态；**没声明返回 `None`**（区别于"声明了但为空"）。

        `None` 表示"不知道"，调用方必须保持原行为。只有拿到确切声明时才做取舍——
        没填 `modalities` 的网关多的是，拿"没声明"当"不支持"是帮倒忙。
        """
        provider = self.bridge.provider_by_id(provider_id)
        declared = self.bridge.provider_modalities(provider)
        return declared or None

    def _filter_unsupported_modalities(
        self,
        provider_id: str,
        image_urls: list[str],
        audio_urls: list[str],
        task: Optional[str],
    ) -> tuple[list[str], list[str]]:
        """按 Provider 声明的模态**丢弃确定送不进去的内容**，并说明原因。

        为什么不是"照常发出去让服务端报错"：AstrBot 的 `Provider.text_chat` 文档写明
        「如果模型不支持图片输入，将会抛出错误」——真发过去是**整轮失败**，比丢掉图片
        严重得多。既然 Provider 自己声明了能力，就按声明处理，并把这件事说出来。

        语音有更好的出路：`model_center.audio.provider_id` 指定了语音转写模型时，
        先转文字再进主模型（见 `_transcribe`），所以这里只处理**没配转写**的情况。
        """
        declared = self._known_modalities(provider_id)
        if declared is None:
            return image_urls, audio_urls

        dropped: list[str] = []
        if image_urls and 'image' not in declared:
            dropped.append('image')
            image_urls = []
        if audio_urls and 'audio' not in declared:
            dropped.append('audio')
            audio_urls = []
        if not dropped:
            return image_urls, audio_urls

        key = (provider_id, tuple(dropped), task or '')
        if key in self._modality_warned:
            return image_urls, audio_urls
        self._modality_warned.add(key)
        # 主叙事缺图片能力时用与 `hdsi_status` 一致的措辞，便于用户对上号
        if 'image' in dropped and (task in (None, 'main')):
            log_fallback(
                'warn',
                '当前主模型未声明图片能力，图片会被忽略（部分服务商会直接报错），'
                '建议改用 sidecar；Provider=%s 已声明=%s',
                provider_id,
                '/'.join(sorted(declared)),
            )
        else:
            log_fallback(
                'warn',
                'AstrBot Provider %s 只声明了 %s，本轮丢掉了 %s：请改名为支持该模态的'
                '模型，或把该 Provider 的 modalities 补全',
                provider_id,
                '/'.join(sorted(declared)),
                '/'.join(dropped),
            )
        return image_urls, audio_urls

    async def _transcribe(self, audio_urls: list[str]) -> Optional[str]:
        """用指定的语音转写模型把音频变成文字；没配 / 失败返回 `None`。

        `model_center.audio.provider_id`（`_special: select_provider_stt`）指名
        AstrBot 的 STT Provider。转写成功就把文字并进 prompt——这样**主模型根本不需要
        支持音频**，也呼应了上游"语音只是用户消息的一种载体"的语义。
        """
        provider_id = self.bridge.task_model_id('audio')
        if not provider_id or not audio_urls:
            return None
        # v1.7.5：`model_center.audio.stt_enabled` 关掉时**不转写**——语音随后按音频
        # 证据交给主模型（`_filter_unsupported_modalities` 会按主模型声明的模态决定
        # 是丢掉还是带上），界面 hint 说的就是这件事。
        if not self.bridge.audio_transcription_enabled():
            return None
        provider = self.bridge.provider_by_id(provider_id)
        get_text = getattr(provider, 'get_text', None)
        if not callable(get_text):
            log_fallback('warn', '语音转写模型 %s 不可用；语音将交给主模型', provider_id)
            return None
        transcripts: list[str] = []
        for url in audio_urls:
            try:
                text = await get_text(url)
            except Exception as error:  # noqa: BLE001 - 转写失败不该毁掉整轮
                log_fallback('warn', '语音转写失败（%s）；语音将交给主模型：%s', provider_id, error)
                return None
            transcripts.append(_text(text).strip())
        joined = '\n'.join(item for item in transcripts if item)
        return joined or None

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


def _now_iso() -> str:
    """控制台时间戳（UTC ISO8601，与 core 的 `iso()` 一致）。"""
    try:
        return iso_time_value(utc_now())
    except Exception:  # pragma: no cover
        return ''


#: 剥掉分层日志里的 ANSI 色码（WebUI 渲染不了 256 色转义序列）。
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')


#: 控制台的环形缓冲长度（日志条数 / 用量条数）。
CONSOLE_LOG_BUFFER = 600
CONSOLE_USAGE_BUFFER = 400


#: 合成连接行的 id 前缀。合成行只活在给 core 的配置副本里（见
#: `AstrbotBridge.routing_config`），不会写进用户的配置文件。
ROUTING_ROW_PREFIX = 'hdsi-astrbot-'

#: **嵌在「模型中心」里的配置段**：schema 里它们的位置是 `model_center.<段>`，
#: core 读的是 `model.<段>`，顶层没有这几个分组。读配置时以嵌套那份为准，
#: 写配置时也只写嵌套那份（顶层同名键是旧版本控制台留下的垃圾，见 `section()`）。
NESTED_MODEL_SECTIONS: tuple[str, ...] = ('vision', 'audio', 'embedding', 'compaction')


def is_routing_row(provider: Any) -> bool:
    """判断一条连接行是不是本移植版合成的（防止重复注入）。"""
    return isinstance(provider, dict) and _text(provider.get('id')).startswith(ROUTING_ROW_PREFIX)


def _audio_data_uri(audio: dict[str, Any]) -> str:
    """`input_audio` 分段 → data URI，喂给 AstrBot 的 `audio_urls`。

    OpenAI 的音频输入是 `{'data': <base64>, 'format': 'mp3'}`；AstrBot 的
    `llm_generate(audio_urls=[...])` 收的是 URL 或本地路径，所以这里拼成 data URI。
    """
    data = _text(audio.get('data'))
    if not data:
        return ''
    if data.startswith('data:'):
        return data
    fmt = _text(audio.get('format')).lower().lstrip('.') or 'wav'
    return 'data:audio/%s;base64,%s' % (fmt, data)


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

    v1.7.7：正文 `<tts/>` 标记指定的分段在这里**已经合成成音频文件**（`voice_paths`
    与 `texts` 一一对应；合不出来时该位是空串 = 照旧发文字）。语音段也走同一个缓冲，
    所以"分段发送语音"的顺序与分段文字完全一致。
    """

    platform: str
    self_id: str
    scope: str
    umo: str
    texts: list[str] = field(default_factory=list)
    voice_paths: list[str] = field(default_factory=list)

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

    def append(self, content: Any, voice_path: str = '') -> None:
        text = _text(content).strip()
        if text:
            self.texts.append(text)
            self.voice_paths.append(_text(voice_path))

    def replies(self) -> list[dict[str, Any]]:
        """本回合的可见回复（v1.7.7）：`[{'content', 'voice'}]`，`voice` 是音频路径或空串。"""
        return [
            {'content': text, 'voice': self.voice_paths[index] if index < len(self.voice_paths) else ''}
            for index, text in enumerate(self.texts)
        ]


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
        LEGACY_SECTION_MERGES as _CORE_SECTION_MERGES,
        apply_section_aliases as _core_apply_section_aliases,
        merge_legacy_section_values as _core_merge_legacy_section_values,
        normalize_config as _core_normalize_config,
        read_section_path as _core_read_section_path,
        VOICE_SECTION as _CORE_VOICE_SECTION,
    )
except ImportError:  # pragma: no cover
    _CORE_SECTION_ALIASES = None
    _CORE_SECTION_MERGES = None
    _core_apply_section_aliases = None
    _core_merge_legacy_section_values = None
    _core_normalize_config = None
    _core_read_section_path = None
    _CORE_VOICE_SECTION = None

#: 语音两项（`tts_provider_id` / `default_voice`）的配置分组。单一实现源在
#: `plugin/core/service/config.py` 的 `VOICE_SECTION`（键级搬迁 `LEGACY_KEY_MERGES`
#: 的目标），这里只镜像一次；core 尚未落地时才用等值兜底。
VOICE_SECTION: str = _CORE_VOICE_SECTION or 'model_center.audio'

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


#: **N:1 的历史分组归并表**（`新分组 → 旧分组`）。单一实现源在
#: `plugin/core/service/config.py` 的 `LEGACY_SECTION_MERGES`；这里镜像一份给
#: "core 尚未落地"的降级路径用（正常情况下是同一个对象的内容）。
LEGACY_SECTION_MERGES: dict[str, tuple[str, ...]] = (
    dict(_CORE_SECTION_MERGES) if _CORE_SECTION_MERGES is not None else {}
)


def read_section_path(config: Any, path: str) -> Any:
    """按**点分路径**读嵌套配置（读不到回 `None`）。

    单一实现源在 `core/service/config.py`（`read_section_path`），适配层只转发一次；
    core 尚未落地时才用下面这份等值兜底。`section()` 读嵌套落点（`robot_actions.chat` /
    `runtime.input_status`）走它。
    """
    if _core_read_section_path is not None:
        try:
            return _core_read_section_path(config, path)
        except Exception:  # noqa: BLE001 - 读配置绝不因为路径异常而炸
            pass
    node: Any = config
    for step in str(path).split('.'):
        if not step or not isinstance(node, dict):
            return None
        node = node.get(step)
        if node is None:
            return None
    return node


def _merge_legacy_section_values(raw: Any, name: str, values: Any = None) -> dict[str, Any]:
    """读一个分组：**用户写过**的新分组值优先，其余从旧分组补（见 `LEGACY_SECTION_MERGES`）。

    优先用 core 的实现（唯一真源，含"等于 schema 默认值 = 没写过"那条规则——宿主每次
    加载都会把缺的键连默认值一起补进配置，见 `core/service/config.merge_legacy_section_values`）。
    core 不可用时退回下面这份**等值但更钝**的本地实现（新分组一律优先）：它读不到 schema
    默认值，所以做不到那条区分——属于"core 都还没落地"的降级路径，正常不会走到。
    """
    if _core_merge_legacy_section_values is not None:
        try:
            return _core_merge_legacy_section_values(raw, name, values)
        except Exception:  # noqa: BLE001 - 读配置绝不因为归并失败而炸
            pass
    merged = dict(values) if isinstance(values, dict) else {}
    sources = LEGACY_SECTION_MERGES.get(name)
    if not sources or not isinstance(raw, dict):
        return merged
    for source in sources:
        section = raw.get(source)
        if not isinstance(section, dict):
            continue
        for key, value in section.items():
            merged.setdefault(key, value)
    return merged


def _deep_merge(base: Any, incoming: Any) -> Any:
    """把 `incoming` **显式写出的键**叠到 `base` 上（字典递归，其余整体替换）。

    `incoming` 里没出现的键一律保持 `base` 的值——这就是配置导入「合并而不是替换」
    那条硬要求的实现本体。列表整体替换（provider / 群规则这类列表没法逐项合并）。
    """
    if isinstance(base, dict) and isinstance(incoming, dict):
        result = dict(base)
        for key, value in incoming.items():
            result[key] = _deep_merge(result[key], value) if key in result else value
        return result
    return incoming


def explicit_config(raw: Any) -> dict[str, Any]:
    """只取用户**显式写出**的配置：统一分组名与键名，**不补默认值**。

    与 `normalize_bridge_config` 的区别就在"不补默认值"——导入时必须知道文件里
    到底写了什么，否则补出来的默认值会盖掉磁盘上用户改过的值（例如手写片段只写
    `characterName`，补默认值会让 `timezone` 被默认值顶掉）。
    """
    return _fallback_normalize_config(_local_apply_section_aliases(raw))


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
        #: 保存原样的配置对象引用（AstrBot 的 `AstrBotConfig`，带 `save_config`）。
        #: 配置导入导出要用它写回磁盘；只有它不可用时才退化为直接写 JSON 文件。
        self._live_config: Any = config
        self.logger = logger
        #: 最近一次解析到的 AstrBot 聊天 / Embedding Provider id，供 `hdsi_status`
        #: 报「主模型能力」用（能力自检需要知道到底是哪个模型在服务）。
        self._resolved_chat_provider_id = ''
        self._resolved_embedding_provider_id = ''
        #: 控制台用的**内存**环形缓冲：最近若干条分层日志与 token 用量。
        #: 不落盘、不增加任何依赖，重启即清空——它只是给 WebUI 看一眼"刚刚发生了什么"。
        self.log_buffer: deque[dict[str, Any]] = deque(maxlen=CONSOLE_LOG_BUFFER)
        self.usage_records: deque[dict[str, Any]] = deque(maxlen=CONSOLE_USAGE_BUFFER)
        self.db = Database(database_path(self.data_dir))
        self.transport = AstrbotTransport(self)
        self.http_client = AstrbotHttpClient(self)
        self.interlude_context = AstrbotInterludeContext(
            context=context,
            bridge=self,
            database=self.db,
            http_client=self.http_client,
            logger=_AstrbotLoggerAdapter(logger, self.log_buffer) if logger is not None else None,
            base_dir=self.data_dir,
        )
        self.service = InterludeService(
            self.interlude_context, self.routing_config(), self.db, self.transport,
        )
        self._started = False
        self._start_lock = asyncio.Lock()
        self._capture: Optional[_TurnCapture] = None
        #: 刚结束的那个回合的捕获（`main.py` 交回宿主时还要读它的语音段）。
        self._last_capture: Optional[_TurnCapture] = None
        # 投递坐标登记表（出站时把 HDSI 的平台/账号翻回 AstrBot 的 UMO）。
        self._platform_ids: dict[tuple[str, str], str] = {}
        self._private_endpoints: dict[tuple[str, str, str], AstrbotEndpoint] = {}
        self._group_endpoints: dict[str, AstrbotEndpoint] = {}
        self._channel_events: dict[str, Any] = {}
        self._message_events: dict[str, Any] = {}
        #: 入站媒体的"再取一次字节"登记表：`source` → `(event, 原始段 data)`（§49.3）。
        #: 为什么要登记：URL 里的 `rkey` 是短效的，字节**必须在入站当次**取；而"怎么取"
        #: （宿主的本地文件 / 宿主官方下载器 / NapCat 的 `get_image`）只有适配层知道。
        #: 有界 FIFO（`_INCOMING_MEDIA_LIMIT` 条）：只用于本次入站回合，过期条目自然被挤掉，
        #: **不落盘、不进库、不进正文**。
        self._incoming_media: dict[str, tuple[Any, dict[str, Any]]] = {}
        self._current_umo = ''
        #: 最近一次入站事件的坐标：`platform_action` 的「本回合对话对象」读它。
        self._current_endpoint: Optional[AstrbotEndpoint] = None
        #: 宿主回执里取到的消息号（`(umo, message_id)`，撤回「最近一条」用）。
        self._outbound_message_ids: deque[tuple[str, str]] = deque(maxlen=64)
        # 跨重启的投递坐标（v1.4.1）：进程内的登记表在重启后是空的，而**平台实例 id**
        # （AstrBot 的 UMO 第一段）是部署属性、不会变。以前重启后没有登记表就退回用
        # 归一化平台名（`onebot`）拼 UMO，宿主于是报 `cannot find platform for session
        # onebot:FriendMessage:…`、消息静默发不出去。落盘一份就再也不会猜错。
        self._delivery_map_path = Path(self.data_dir) / 'delivery_endpoints.json'
        # 平台动作权限表（本移植版新增）：**独立 JSON**，不进 `_conf_schema.json`。
        # 理由与参考插件同源（他们 v5.2.2 踩过）：宿主每次加载都按 schema 重建配置，
        # schema 里没有的键会被删掉；而空 object 的子键尤其容易被清理，权限表会"保存即失效"。
        self._action_permissions_path = Path(self.data_dir) / 'action_permissions.json'
        self._action_permissions: dict[str, str] = {}
        self._load_action_permissions()
        self._saved_platform_ids: dict[str, str] = {}
        self._saved_private_umos: dict[str, str] = {}
        self._saved_group_umos: dict[str, str] = {}
        self._load_delivery_map()
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
            # 共同作品（works）：把库里遗留的 `running` 写手任务收成 `failed`——
            # 进程重启后那些任务永远不会回来，留着会让界面一直显示"正在写"。
            # **放在适配层而不是 `start_background_tasks()`**：它要碰 sqlite，
            # 挂进 core 的定时器后在单元测试的进程退出阶段会撞上被关掉的数据库连接
            # （实测整轮 discover 段错误，exit 139）。这里 await 完成，失败只 warn。
            recover = getattr(self.service, 'startup_recover_works', None)
            if callable(recover):
                try:
                    await recover()
                except Exception as error:  # noqa: BLE001 - 启动路径绝不因为可选特性失败
                    logger.warning('hds-interlude：共同作品恢复失败 %s' % error)
            # v1.7.2 动作开关分组收敛（10 → 4）：把旧分组里用户写过的值折进新分组，
            # 写一次盘就再也没有新旧两份打架的可能。**必须在启动时做**（放在适配层的
            # 真实启动路径，同坑 68 的理由）：宿主在加载配置时已经按 schema 把新分组
            # 补成默认值了，越早折越好——用户第一次打开配置页之前就该折完。
            try:
                await self.migrate_legacy_action_sections()
            except Exception as error:  # noqa: BLE001 - 迁移失败不影响插件启动
                logger.warning('hds-interlude：动作开关分组迁移失败 %s' % error)
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

    async def migrate_legacy_action_sections(self) -> int:
        """把旧动作开关分组里用户写过的值折进新路径，写一次盘（v1.7.2 起，幂等）。

        为什么必须在**启动**时折一次（而不是只靠读取侧归并）：

        1. 宿主每次加载都按 `_conf_schema.json` 补默认值再落盘
           （`AstrbotConfig.check_config_integrity`：缺的键连默认值一起插进配置文件）。
           升级后的第一次加载，`robot_actions` 就是这样被整组补上默认值（开关全 true）的，
           而用户真正的选择还在旧分组里。读取侧的归并靠"等于默认值算没写过"能读对，
           但**用户在新分组里把开关改回默认值**（比如重新打开升级前关掉的语音）时，
           旧分组里的旧值会永远压着它——界面上就是"改了没反应"。
        2. 折之前，宿主配置页显示的是文件里的新组值、运行期读的是旧组值：显示与行为相反
           （坑 34 的老病）。折完两处才是同一份。

        折叠规则与读取侧同一套（`core/service/config.py` 的 `fold_legacy_section_merges`
        → `merge_legacy_section_values`；"用户写过 = 不等于 schema 默认值"），目标路径
        可以是嵌套的（`robot_actions.chat` / `runtime.input_status`）。没有可折的
        东西时**不写盘**，返回 0。放在适配层的真实启动路径（同坑 68 的理由：别塞进 core
        的定时器）。失败只 warn：读侧的归并仍然兜得住，不会丢配置。
        """
        from ..core.service.config import fold_legacy_section_merges  # noqa: PLC0415

        raw = self.raw_config()
        if not isinstance(raw, dict) or not raw:
            return 0
        target = fold_legacy_section_merges(dict(raw))
        if target == raw:
            return 0
        await self.save_raw_config(target)
        log_fallback('info', '配置迁移完成：旧分组的值已折进 robot_actions.* / runtime.input_status（v1.7.4），语音两项已折进 model_center.audio（v1.7.5）')
        return 1

    async def shutdown(self) -> None:
        """停止后台计时器、关闭 HTTP 客户端与数据库（幂等）。"""
        for attribute in (
            '_sweep_timer', '_compaction_timer', '_blind_mode_timer', '_sticker_scan_timer',
            '_world_seeder_timer', '_qzone_feed_timer',
        ):
            handle = getattr(self.service, attribute, None)
            cancel = getattr(handle, 'cancel', None)
            if callable(cancel):
                try:
                    cancel()
                except Exception:  # pragma: no cover - 已触发的计时器取消是 no-op
                    pass
        self.service.background_started = False
        # 共同作品（works）：关掉在飞的写手任务，别让它在卸载后还往库里写。
        stop_works = getattr(self.service, 'stop_works', None)
        if callable(stop_works):
            try:
                stop_works()
            except Exception as error:  # noqa: BLE001 - 卸载路径绝不抛回宿主
                logger.warning('hds-interlude：关闭共同作品失败 %s' % error)
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
            # 先留一份给控制台（纯内存，失败不影响日志本身）。
            # **要剥掉 ANSI 色码**：core 的分层日志带 256 色转义序列，AstrBot 的控制台
            # 能渲染，但 WebUI 里会显示成一堆 `\u001b[38;5;222m`。
            try:
                self.log_buffer.append({
                    'at': _now_iso(), 'level': level, 'text': _ANSI_RE.sub('', text),
                })
            except Exception:  # pragma: no cover
                pass
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

    # ------------------------------------------------------------------ #
    # 按任务指定 AstrBot 模型（`model_center.task_models`）
    # ------------------------------------------------------------------ #

    #: 任务键 → 「该任务的模型来源」在配置里的路径（`(分组, 子键…)`）。
    #:
    #: 这一项与上游的连接行是**并列**的：连接行回答"这条连接给哪些任务用"，
    #: 这里回答"这个任务固定用 AstrBot 的哪个模型"。留空即回落到连接行 → 会话默认，
    #: 与历史行为完全一致。
    TASK_MODEL_PATHS: dict[str, tuple[str, ...]] = {
        'main': ('model', 'main_provider_id'),
        'compaction': ('model', 'compaction_provider_id'),
        'alter': ('model', 'alter_provider_id'),
        'vision': ('model', 'vision', 'provider_id'),
        'audio': ('model', 'audio', 'provider_id'),
        'embedding': ('model', 'embedding', 'provider_id'),
        'stickers': ('stickers', 'provider_id'),
        # 上游 1.0.1-rc24：世界播种器也是独立任务（这里让它同样能指名 AstrBot 模型）。
        'world_seeding': ('model', 'world_seeding_provider_id'),
        # v1.7.9：共同作品的独立写手（`works.model_id`）。这个键的老口径是"点名模型
        # 中心里的一条连接行"，所以它在 `routing_config()` 里要先过
        # `works_writer_named_provider()` 的双读判定，不无条件合成指名连接行。
        'works': ('works', 'model_id'),
    }

    def task_model_id(self, task: Optional[str]) -> str:
        """读「该任务用哪个 AstrBot 模型」。留空 → `''`（沿用默认 Provider）。

        为什么要有这一层：上游每类任务绑一条自己的 OpenAI 兼容连接，而 AstrBot
        把这些连接统一管在自己的 Provider 列表里。用户想让某个任务复用 AstrBot
        里配好的模型时，就在这一项里**指名**该 Provider；留空则回落到
        `resolve_chat_provider_id()`（会话当前模型），与历史行为完全一致。
        """
        if not task:
            return ''
        path = self.TASK_MODEL_PATHS.get(task)
        if not path:
            return ''
        node: Any = self.section(path[0])
        for step in path[1:]:
            if not isinstance(node, dict):
                return ''
            node = node.get(step)
        return node.strip() if isinstance(node, str) and node.strip() else ''

    def _binding_row(self, task: str, provider_id: str) -> dict[str, Any]:
        """为「指名了 AstrBot 模型」的任务合成一条连接行（**只给 core 看的副本**）。

        为什么必须合成：core 的候选筛选（`resolve_route` / `selectRouteProviders`）
        以上游的方式只看 `endpoint`——连接行不填地址就被判成 `unavailable`，
        请求根本走不到传输层。可是"只用宿主的模型"这条路本来就不该填地址。
        所以给 core 的配置副本里补一条**声明了 `transport_target`** 的行：
        core 只判断它非空（见 `model_routing.provider_reachable`），
        真正的目标是哪个模型由传输层按 `task` 决定。

        合成行**只存在于内存副本里**：`self.config` 仍是干净配置，导出 / 落盘、
        `section()` 读取走的都是它，所以用户不会在配置文件里看到这些假行。
        """
        return {
            'id': '{}{}'.format(ROUTING_ROW_PREFIX, task),
            'label': 'AstrBot · %s' % provider_id,
            'enabled': True,
            'mode': 'openai-compatible',
            'endpoint': '',
            'transport_target': 'astrbot:%s' % provider_id,
            'api_key': '',
            # `model` 必须非空，否则 core 的候选筛选同样会跳过这一行。
            'model': self.provider_model_name(provider_id) or provider_id,
            'use_for_main': task == 'main',
            'use_for_compaction': task == 'compaction',
            'use_for_alter': task == 'alter',
            'use_for_vision': task == 'vision',
            'use_for_stickers': task == 'stickers',
            'use_for_embedding': task == 'embedding',
            'use_for_world_seeding': task == 'world_seeding',
            # v1.7.9：共同作品写手（core 的 `is_assigned_to(provider, 'works')` 认它）。
            'use_for_works': task == 'works',
        }

    def works_writer_named_provider(self) -> str:
        """`works.model_id` 该不该按「AstrBot Provider id」解释；是就返回它，否则空串。

        v1.7.9 起配置页把这个键渲染成 **AstrBot 模型选择器**（写的是 Provider id），
        而它在本移植版里的老口径是"点名模型中心里的一条连接行"（core 按 `id` /
        `model` / `label` 匹配，见 `service/chunk14.py::_works_writer_providers`）。
        读取侧**双读兜底**，老用户填过的值一个都不许失效：

        1. 命中宿主的某个已加载 Provider → 新口径（改道 AstrBot，走"双轨"的①）；
        2. 点到当前可用的某条连接行 → 老口径（逐字不变）；
        3. 两条判据都够不着、而宿主的 Provider 列表**还没装好**（AstrBot 4.28 里
           插件先、模型后，见 AGENTS 坑 23）→ 按新口径处理：否则用户在选择器里选好
           的模型要等到下次保存配置才生效（重启后一直不生效）；
        4. 已就绪却没有这个 Provider、也不是连接行 → 老口径，保留原来那句
           「指名的作品写手模型不存在或不可用」的报错。

        第 2 步的"连接行优先"只影响"**不是**已加载 Provider、却点到了一条可用连接行"
        的值——也就是历史配置里那种"手填一条连接行的 id / 模型名 / 标签"。一个值
        同时是连接行名与已加载 Provider id 时按 Provider 解释（第 1 步）：那正是用户在
        选择器里能选到的东西，改道才符合"指名 → 生效"。
        """
        value = self.task_model_id('works')
        if not value:
            return ''
        ids = self.loaded_chat_provider_ids()
        # 没拿到列表的宿主（少见）才逐个问 `provider_by_id`，而且只在"模型已经装上"时问。
        loaded = bool(ids) or self.any_provider_loaded()
        hit = value in ids if ids else (loaded and self.provider_by_id(value) is not None)
        if hit:
            return value
        if self._names_usable_connection_row(value):
            return ''
        if not loaded:
            return value
        return ''

    def loaded_chat_provider_ids(self) -> set[str]:
        """宿主当前装好的**聊天** Provider id 集合（拿不到返回空集合）。

        判断"这个值是不是一个 AstrBot Provider"时用它，而不是逐个 `provider_by_id()`：
        `context.get_provider_by_id()` 对**不存在**的 id 会打一条误导性的宿主警告
        （AGENTS 坑 23），而这里每保存一次配置都要判一个普通的老口径值（连接行名）
        ——那会变成"每次保存配置都报一个找不到的 Provider"。顺带也避开了
        `get_provider_by_id` 在某些宿主版本上是协程的问题（`provider_by_id` 对协程
        一律返回 `None`，那会让新口径在这些宿主上静默失效）。
        """
        getter = getattr(self.context, 'get_all_providers', None)
        if not callable(getter):
            return set()
        try:
            providers = list(getter() or [])
        except Exception:  # noqa: BLE001 - 拿不到列表就当"还没装好"
            return set()
        ids: set[str] = set()
        for provider in providers:
            meta = getattr(provider, 'meta', None)
            if not callable(meta):
                continue
            try:
                identifier = _text(getattr(meta(), 'id', ''))
            except Exception:  # noqa: BLE001
                continue
            if identifier:
                ids.add(identifier)
        return ids

    def _names_usable_connection_row(self, value: str) -> bool:
        """`value` 是不是点名了当前可用的某条连接行（`works.model_id` 的老口径）。

        判定直接用 core 的 `match_usable_connection_row`：与 core 挑写手连接时同一个
        实现，免得"这个值点到的是不是连接行"两侧给出不同结论。
        """
        section = self.section('model')
        rows = section.get('providers') if isinstance(section, dict) else None
        candidates = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
        return bool(match_usable_connection_row(candidates, value))

    def routing_config(self, config: Any = None) -> dict[str, Any]:
        """给 `InterludeService` 用的配置副本：补上指名的任务用合成连接行。

        没有指名任何任务时**原样返回**同一个对象（零开销、也保证老配置的行为
        逐字不变）。
        """
        base = self.config if config is None else config
        if not isinstance(base, dict):
            return base if isinstance(base, dict) else {}
        rows = []
        for task in ('main', 'compaction', 'alter', 'vision', 'stickers', 'embedding', 'world_seeding', 'works'):
            provider_id = self.task_model_id(task)
            if not provider_id:
                continue
            # `works.model_id` 还有一套"点名连接行"的老口径：那种值不合成指名行，
            # 交给 core 按连接行解析（逐字保留老行为）。
            if task == 'works' and not self.works_writer_named_provider():
                continue
            rows.append(self._binding_row(task, provider_id))
        if not rows:
            return base
        result = dict(base)
        for name in ('model', 'model_center'):
            section = result.get(name)
            if not isinstance(section, dict) or not isinstance(section.get('providers'), list):
                continue
            existing = [item for item in section['providers'] if not is_routing_row(item)]
            section = dict(section)
            # 指名的排在最前：它是用户针对这个任务的明确选择，其余连接作为 failover
            section['providers'] = rows + existing
            result[name] = section
        return result

    def provider_model_name(self, provider_id: str) -> str:
        """读 AstrBot Provider 配置里的模型名（拿不到返回空串）。"""
        provider = self.provider_by_id(provider_id)
        config = getattr(provider, 'provider_config', None)
        if isinstance(config, dict):
            return _text(config.get('model'))
        model = getattr(provider, 'model', None)
        return _text(model) if isinstance(model, str) else ''

    def task_provider_modalities(self, task: Optional[str]) -> set[str]:
        """该任务指名的 AstrBot Provider 声明的模态（没指名 / 拿不到 → 空集合）。"""
        provider_id = self.task_model_id(task)
        if not provider_id:
            return set()
        return self.provider_modalities(self.provider_by_id(provider_id))

    def vision_mode_native(self) -> bool:
        """`model_center.vision.mode` 是不是 `native`（默认就是 native）。"""
        vision = self.section('model').get('vision')
        if not isinstance(vision, dict):
            return True
        mode = vision.get('mode')
        return (mode if isinstance(mode, str) else 'native').strip().lower() != 'sidecar'

    def image_capability_note(self) -> str:
        """主叙事当前能不能吃图片；不能时返回一句给人看的话，否则空串。

        `hdsi_status` 与启动日志都用它。只在**确定**不支持时说话：Provider 没声明
        `modalities` 时返回空串——猜出来的结论只会误导人。
        """
        provider_id = self.task_model_id('main') or self._resolved_chat_provider_id
        if not provider_id:
            return ''
        declared = self.provider_modalities(self.provider_by_id(provider_id))
        if not declared or 'image' in declared:
            return ''
        return (
            '当前主模型未声明图片能力，图片会被忽略（部分服务商会直接报错），'
            '建议改用 sidecar'
        )

    def any_provider_loaded(self) -> bool:
        """AstrBot 的 Provider 管理器是否已经装好模型（启动自检等它用）。

        AstrBot 4.28 的启动顺序是**插件先、模型后**：`initialize()` 里 `inst_map`
        还是空的。等这里是 `True` 再去查 Provider 的 `modalities`，才不会既拿不到
        结果、又触发宿主那条 "Provider … was not found" 的误导性警告。
        """
        for name in ('get_all_providers', 'get_all_embedding_providers'):
            getter = getattr(self.context, name, None)
            if not callable(getter):
                continue
            try:
                if list(getter() or []):
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    async def log_model_capabilities(self) -> None:
        """启动时解析一次模型、把能力结论写进日志（见 `main.HDSInterludePlugin.initialize`）。

        - 能力结论走 `warn`，**只在确定不支持时说话**，默认就能在日志里看到；
        - 「每个任务用的是哪个模型」走 `info`。注意 `log_fallback` 不读用户的日志等级
          配置（它没有实例上下文），而 sink 把 `info` 映射到 debug，所以这一份摘要
          要在插件日志等级调到 DEBUG 时才可见——这是刻意的，免得正常运行时刷屏。
        """
        if not self.task_model_id('main') and not self._resolved_chat_provider_id:
            await self.resolve_chat_provider_id()
        for task in ('main', 'compaction', 'alter', 'vision', 'stickers', 'embedding', 'audio'):
            bound = self.task_model_id(task)
            if bound:
                log_fallback('info', '模型来源：%s → AstrBot Provider %s', task, bound)
        # 共同作品的写手模型要过双读判定：老口径里这个值点的是连接行，不能报成
        # "AstrBot Provider x"（那会让用户去宿主的模型列表里找一个不存在的东西）。
        works_bound = self.works_writer_named_provider()
        if works_bound:
            log_fallback('info', '模型来源：works → AstrBot Provider %s', works_bound)
        for note in (self.image_capability_note(), self.audio_capability_note()):
            if note:
                log_fallback('warn', '%s', note)

    def audio_understanding_enabled(self) -> bool:
        """`model_center.audio.enabled`：「语音 / 音频理解」的**总开关**（上游 `audioConfig.enabled`）。

        缺键按 **False**：上游是 `configured?.enabled === true`，core 的
        `ServiceChunk3.load_native_audio` 也拿同一把闸放行附件（缺键 = 不加载）。
        这个开关管的是"语音要不要当**音频证据**"，与 `stt_enabled`（要不要调转写模型）
        是两件事，但顺序在前：总开关关着时，语音既不进主模型、也没有东西可转写。
        """
        return self.section('audio').get('enabled') is True

    def audio_transcription_enabled(self) -> bool:
        """`model_center.audio.stt_enabled`（v1.7.5）：「语音转文字」开关。

        **缺键按 `True`**：这个键是 v1.7.5 才有的，旧配置文件里没有它时不能把用户的
        转写悄悄关掉（配了转写模型就转，是历史行为）。关掉之后 `_transcribe` 不调转写
        模型，语音按音频证据原样交给主模型——所以 `audio_capability_note` 也必须认它，
        否则启动自检会说"已指定转写模型"而实际没转。
        """
        value = self.section('audio').get('stt_enabled')
        return True if value is None else bool(value)

    def audio_capability_note(self) -> str:
        """主叙事当前能不能吃音频；不能时返回一句给人看的话，否则空串。

        v1.7.6 起**先看总开关**：`enabled=false`（默认）时语音根本不会被当成音频证据
        加载（`load_native_audio` 直接返回空），所以那个组合下"已指定转写模型"是句假话
        ——配好的转写模型一次都不会被调用。只有用户明显以为它在跑（绑了转写模型）时
        才出声，免得默认配置的每次启动都刷一条警告。
        """
        transcript_provider = self.task_model_id('audio')
        if not self.audio_understanding_enabled():
            if not transcript_provider:
                return ''
            return ('「语音 / 音频理解」的「启用语音原生理解」是关的（默认如此）：语音不会'
                    '作为音频附件加载，「语音转写模型」（%s）也就不会被调用——只会留一条'
                    '"收到了一条语音"的事实。要让她听懂语音，先打开这个总开关。' % transcript_provider)
        if transcript_provider and self.audio_transcription_enabled():
            return ''  # 已指定语音转写模型且开关是开的，音频不进主模型
        provider_id = self.task_model_id('main') or self._resolved_chat_provider_id
        if not provider_id:
            return ''
        declared = self.provider_modalities(self.provider_by_id(provider_id))
        if not declared or 'audio' in declared:
            return ''
        if transcript_provider:
            # 指定了转写模型却把「语音转文字」关了：说清是开关造成的，别让用户去换模型。
            return ('「语音 / 音频理解」里的「语音转文字」是关的，而当前主模型又未声明音频能力，'
                    '语音会被忽略：打开那个开关，或把主模型换成支持音频输入的')
        return ('当前主模型未声明音频能力，语音会被忽略：先打开「语音 / 音频理解」里的'
                '「启用语音原生理解」，再指定语音转写模型')

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
                    resolved = _text(provider_id)
                    self._resolved_chat_provider_id = resolved
                    return resolved
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
                    resolved = _text(getattr(meta(), 'id', ''))
                    self._resolved_chat_provider_id = resolved
                    return resolved
                except Exception:  # pragma: no cover
                    return ''
        return ''

    def provider_by_id(self, provider_id: str) -> Any:
        """按 id 取 AstrBot Provider 实例（拿不到返回 `None`）。

        只用于读它的元信息（`modalities`）；请求本身仍然交给 `context.llm_generate`，
        不去碰 Provider 的私有调用方式。
        """
        if not provider_id:
            return None
        getter = getattr(self.context, 'get_provider_by_id', None)
        if not callable(getter):
            return None
        try:
            provider = getter(_text(provider_id))
            if inspect.isawaitable(provider):
                return None  # 这个宿主版本是协程：读元信息不值得再开一次 await 路径
            return provider
        except Exception:  # noqa: BLE001
            return None

    def provider_modalities(self, provider: Any) -> set[str]:
        """Provider 声明的模态集合（`text` / `image` / `audio` / `tool_use`）。

        AstrBot 把它放在 `provider.provider_config['modalities']`。**读不到就返回空集合**
        ——空集合表示"没声明"，调用方不该据此判断模型不支持（不填的网关多的是）。
        """
        config = getattr(provider, 'provider_config', None)
        values = config.get('modalities') if isinstance(config, dict) else None
        if not isinstance(values, (list, tuple, set)):
            return set()
        return {_text(item).lower() for item in values if _text(item)}

    def embedding_provider(self) -> Any:
        """取用于 Embedding 的 AstrBot Provider（没有就返回 `None` → 回落直连）。

        AstrBot 4.28 把 Embedding 单独列成一类 Provider
        （`Context.get_all_embedding_providers()`，`Provider.get_embedding` /
        `get_embeddings`），跟聊天 Provider 不是一回事；先找专用 Embedding
        Provider，找不到再退回当前会话的 Provider（它可能也支持向量）。

        用户在「Embedding 模型」里指名了 Provider 时优先用它（指名了一个不存在的
        id 会记一条 warning 再回落，**绝不因此让检索整体失效**）。
        """
        getter = getattr(self.context, 'get_all_embedding_providers', None)
        if callable(getter):
            try:
                providers = list(getter() or [])
            except Exception:  # noqa: BLE001
                providers = []
            if providers:
                chosen = self._pick_embedding_provider(providers)
                self._resolved_embedding_provider_id = (
                    self._embedding_provider_ids([chosen]) or ['']
                )[0]
                return chosen
        fallback = getattr(self.context, 'get_using_provider', None)
        if not callable(fallback):
            return None
        try:
            provider = fallback(self._current_umo or None)
        except Exception:  # noqa: BLE001
            return None
        return provider

    def _pick_embedding_provider(self, providers: list[Any]) -> Any:
        """按「Embedding 模型」配置挑一个 Embedding Provider。

        留空 → 列表第一个（保持历史行为）。指名了 id → 精确匹配；匹配不到就
        warning + 回落第一个，而不是抛错。
        """
        wanted = self.task_model_id('embedding')
        if not wanted:
            return providers[0]
        for provider in providers:
            meta = getattr(provider, 'meta', None)
            if not callable(meta):
                continue
            try:
                if _text(getattr(meta(), 'id', '')) == wanted:
                    return provider
            except Exception:  # noqa: BLE001
                continue
        log_fallback(
            'warn',
            'Embedding 模型指名的 Provider %s 不存在（可用的：%s）；本轮回落到第一个',
            wanted,
            ', '.join(self._embedding_provider_ids(providers)) or '无',
        )
        return providers[0]

    @staticmethod
    def _embedding_provider_ids(providers: list[Any]) -> list[str]:
        ids = []
        for provider in providers:
            meta = getattr(provider, 'meta', None)
            if callable(meta):
                try:
                    ids.append(_text(getattr(meta(), 'id', '')))
                except Exception:  # noqa: BLE001
                    continue
        return [item for item in ids if item]

    # ------------------------------------------------------------------ #
    # 投递坐标
    # ------------------------------------------------------------------ #

    def remember_event(self, event: AstrMessageEvent, session: SessionView, endpoint: AstrbotEndpoint) -> None:
        """登记本次事件的投递坐标，供后台主动投递时反查。"""
        self._platform_ids[(endpoint.platform, endpoint.self_id)] = endpoint.platform_id
        self._platform_ids.setdefault((endpoint.platform, ''), endpoint.platform_id)
        self._current_umo = endpoint.umo
        self._current_endpoint = endpoint
        if endpoint.is_group:
            self._group_endpoints[endpoint.group_id or endpoint.channel_id] = endpoint
            if endpoint.group_id:
                self._channel_events[endpoint.group_id] = event
        else:
            self._private_endpoints[(endpoint.platform, endpoint.self_id, endpoint.user_id)] = endpoint
        if endpoint.message_id:
            self._message_events[endpoint.message_id] = event
        self._remember_incoming_media(event, session)
        self._persist_endpoint(endpoint)

    def incoming_media_handle(self, source: str) -> tuple[Any, dict[str, Any]]:
        """按入站媒体坐标取"那个原始段 + 它来自的事件"（没登记过回 `(None, {})`）。

        给 `Transport.fetch_incoming_image()` 用：只有知道是哪条消息、哪个原始段，
        才谈得上"宿主的本地文件 / 官方下载器 / NapCat 的 `get_image`"（§49.3）。
        """
        handle = self._incoming_media.get(_text(source).strip())
        if handle is None:
            return None, {}
        return handle

    def _remember_incoming_media(self, event: Any, session: Any) -> None:
        """登记这条消息里每个媒体条的"坐标 → 事件 + 原始段"（§49.3）。

        `session.media` 是适配层刚写下的结构化媒体表（JSON 安全的字符串 / dict），这里
        只把条目与**事件引用**记进有界 FIFO：收藏流程稍后（同一次入站回合内）要取字节时，
        得知道是哪条消息、哪个原始段 —— 只存 URL 是不够的（`rkey` 短效，且 NapCat 的
        `get_image` 要的是文件 token，不是 URL）。
        """
        media = getattr(session, 'media', None)
        if isinstance(session, Mapping):
            media = session.get('media')
        if not isinstance(media, list):
            return
        for item in media:
            if not isinstance(item, dict):
                continue
            source = _text(item.get('source')).strip()
            if not source:
                continue
            raw = item.get('raw') if isinstance(item.get('raw'), dict) else {}
            self._incoming_media.pop(source, None)
            self._incoming_media[source] = (event, dict(raw))
        while len(self._incoming_media) > _INCOMING_MEDIA_LIMIT:
            self._incoming_media.pop(next(iter(self._incoming_media)), None)

    def _persist_endpoint(self, endpoint: AstrbotEndpoint) -> None:
        """把这条会话的投递坐标记进落盘表（只在出现新键时写文件）。"""
        if not endpoint.platform_id or not endpoint.umo:
            return
        changed = False
        key = self._delivery_key(endpoint.platform, endpoint.self_id)
        if self._saved_platform_ids.get(key) != endpoint.platform_id:
            self._saved_platform_ids[key] = endpoint.platform_id
            changed = True
        platform_key = self._delivery_key(endpoint.platform, '')
        if self._saved_platform_ids.get(platform_key) != endpoint.platform_id:
            self._saved_platform_ids[platform_key] = endpoint.platform_id
            changed = True
        if endpoint.is_group:
            channel = endpoint.group_id or endpoint.channel_id
            if channel and self._saved_group_umos.get(channel) != endpoint.umo:
                self._saved_group_umos[channel] = endpoint.umo
                changed = True
        elif endpoint.user_id and self._saved_private_umos.get(key) != endpoint.umo:
            self._saved_private_umos[key] = endpoint.umo
            changed = True
        if changed:
            self._save_delivery_map()

    def _platform_id_for(self, platform: str, self_id: str = '') -> str:
        """归一化平台名 → 平台实例 id。

        顺序：运行期登记（本次进程见过这条会话）→ 落盘登记（以前见过）→ 向宿主反查
        （按适配器类型，只有唯一候选时才用）。三条都不成立时**返回归一化名并记一条
        warn**：那正是 `cannot find platform for session onebot:…` 的来源，得让它可见。
        """
        for key in ((platform, self_id), (platform, '')):
            if key in self._platform_ids:
                return self._platform_ids[key]
        for key in (self._delivery_key(platform, self_id), self._delivery_key(platform, '')):
            if key in self._saved_platform_ids:
                return self._saved_platform_ids[key]
        host_ids = self._host_platform_ids(platform)
        if len(host_ids) == 1:
            resolved = host_ids[0]
            self._platform_ids[(platform, self_id)] = resolved
            self._platform_ids.setdefault((platform, ''), resolved)
            return resolved
        if len(host_ids) > 1:
            # 有多个同类实例：**不猜**。猜错就是把消息发进另一个账号的对话框，
            # 比发不出去严重得多（用户踩过一次：发了消息但对面什么都没收到）。
            log_fallback(
                'warn',
                '有多个 %s 平台实例（%s），无法确定该用哪一个投递；'
                '等这条会话收到一条消息后会自动登记',
                platform, '、'.join(host_ids),
            )
            return ''
        log_fallback(
            'warn',
            '没能把平台 %s 翻成 AstrBot 的平台实例 id，出站可能失败；'
            '等这条会话收到一条消息后会自动登记',
            platform,
        )
        return platform

    # ---- 投递坐标的落盘（跨重启） ----

    def _delivery_key(self, platform: str, self_id: str = '') -> str:
        return '%s|%s' % (_text(platform), _text(self_id))

    def _load_delivery_map(self) -> None:
        """读回上次运行登记的投递坐标（读失败就当没有，绝不拦住启动）。"""
        try:
            raw = self._delivery_map_path.read_text(encoding='utf-8')
            data = json.loads(raw) if raw.strip() else {}
        except FileNotFoundError:
            return
        except Exception as error:  # noqa: BLE001 - 坏文件不影响出站
            log_fallback('warn', '投递坐标文件读取失败，将按运行期登记重建 错误=%s', error)
            return
        if not isinstance(data, dict):
            return
        for key, value in (data.get('platforms') or {}).items():
            text = _text(value)
            if text:
                self._saved_platform_ids[_text(key)] = text
        for key, value in (data.get('private') or {}).items():
            text = _text(value)
            if text:
                self._saved_private_umos[_text(key)] = text
        for key, value in (data.get('groups') or {}).items():
            text = _text(value)
            if text:
                self._saved_group_umos[_text(key)] = text

    def _save_delivery_map(self) -> None:
        """把投递坐标写回磁盘（只在有新键时调用，不是每条消息都写）。"""
        payload = {
            'platforms': dict(self._saved_platform_ids),
            'private': dict(self._saved_private_umos),
            'groups': dict(self._saved_group_umos),
        }
        try:
            tmp = self._delivery_map_path.with_suffix('.json.tmp')
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding='utf-8')
            os.replace(tmp, self._delivery_map_path)
        except Exception as error:  # noqa: BLE001 - 落盘失败只降级成"重启后要重建"
            log_fallback('warn', '投递坐标写入失败，重启后需要重新登记 错误=%s', error)

    # ---- 平台动作权限表（独立 JSON；动作目录是唯一事实源） ---- #

    def _load_action_permissions(self) -> None:
        """读权限表；坏文件只 warn 并回落空表（空表 = 全部用目录默认档）。"""
        try:
            raw = self._action_permissions_path.read_text(encoding='utf-8')
            data = json.loads(raw) if raw.strip() else {}
        except FileNotFoundError:
            self._action_permissions = {}
            return
        except Exception as error:  # noqa: BLE001 - 坏文件不该让插件起不来
            log_fallback('warn', '动作权限表读取失败，按默认档运行 路径=%s 错误=%s',
                         self._action_permissions_path, error)
            self._action_permissions = {}
            return
        from ..core.platform_actions import normalize_permissions

        self._action_permissions = normalize_permissions(data)

    def action_permissions(self) -> dict[str, str]:
        """当前权限表（已归一化；控制台与运行期都读它）。"""
        return dict(self._action_permissions)

    def save_action_permissions(self, table: Any) -> dict[str, str]:
        """覆盖写入权限表并返回归一化后的结果（坏行丢弃，不写盘）。

        原子写（临时文件 + `os.replace`）：权限表写坏等于所有动作回默认档，
        而默认档里有 17 个危险动作是关的、其余是全开的，行为会突变。
        """
        from ..core.platform_actions import normalize_permissions

        normalized = normalize_permissions(table)
        try:
            self._action_permissions_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._action_permissions_path.with_suffix('.json.tmp')
            tmp.write_text(json.dumps(normalized, ensure_ascii=False, indent=1), encoding='utf-8')
            os.replace(tmp, self._action_permissions_path)
        except Exception as error:  # noqa: BLE001
            log_fallback('warn', '动作权限表写入失败，本次修改只在内存生效 错误=%s', error)
        self._action_permissions = normalized
        return dict(normalized)

    def _host_platform_ids(self, platform: str = '') -> list[str]:
        """向宿主列出候选平台实例 id。

        匹配依据是**适配器类型**（`meta().name`，如 aiocqhttp）而不是实例 id，因为
        我们的 `onebot` 是归一化名、宿主的实例 id 是配置里那个平台 ID（实测这台叫 `NapCat`）。
        传空平台名
        就列出全部实例。
        """
        ids: list[str] = []
        for instance in self.list_bots():
            try:
                meta = instance.meta()
            except Exception:  # noqa: BLE001 - 取不到元信息就跳过这个实例
                continue
            instance_id = _text(getattr(meta, 'id', ''))
            adapter_name = _text(getattr(meta, 'name', ''))
            if not instance_id:
                continue
            if platform and resolve_platform_name(adapter_name, instance_id) != platform:
                continue
            if instance_id not in ids:
                ids.append(instance_id)
        return ids

    def _sole_platform_id(self) -> str:
        """群聊用：只有唯一平台时给出它的实例 id，多个候选一律不猜。"""
        known = [value for value in self._platform_ids.values() if value]
        known += [value for value in self._saved_platform_ids.values() if value]
        unique = list(dict.fromkeys(known))
        if len(unique) == 1:
            return unique[0]
        if len(unique) > 1:
            return ''
        host_ids = self._host_platform_ids()
        if len(host_ids) == 1:
            return host_ids[0]
        if len(host_ids) > 1:
            log_fallback(
                'warn',
                '宿主机上有多个平台实例（%s），群聊投递无法确定用哪一个；'
                '等这个群收到一条消息后会自动登记',
                '、'.join(host_ids),
            )
        return ''

    def private_umo(self, platform: str, self_id: str, user_id: str) -> str:
        """私聊 UMO：`<platform_id>:FriendMessage:<user_id>`。"""
        endpoint = self._private_endpoints.get((platform, self_id, user_id))
        if endpoint is not None:
            return endpoint.build_umo()
        saved = self._saved_private_umos.get(self._delivery_key(platform, self_id))
        if saved:
            return saved
        platform_id = self._platform_id_for(platform, self_id)
        if not platform_id or not user_id:
            return ''
        return '%s:FriendMessage:%s' % (platform_id, user_id)

    def group_umo(self, channel_id: str) -> str:
        """群聊 UMO：`<platform_id>:GroupMessage:<group_id>`。"""
        channel = _text(channel_id)
        endpoint = self._group_endpoints.get(channel)
        if endpoint is not None:
            return endpoint.build_umo()
        saved = self._saved_group_umos.get(channel)
        if saved:
            return saved
        platform_id = self._sole_platform_id()
        if not platform_id or not channel:
            return ''
        return '%s:GroupMessage:%s' % (platform_id, channel)

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

    # ------------------------------------------------------------------ #
    # 平台动作执行层要用的会话坐标 / 平台实例
    # ------------------------------------------------------------------ #

    def current_target(self) -> dict[str, Any]:
        """本回合的会话坐标（`platform_action` 的「留空＝本回合对话对象」读它）。

        优先用最近一次入站事件的坐标；进程刚重启、还没有事件时退回最近登记过的
        端点；一个都没有就返回空 dict（调用方据此报"缺少必填参数 / 没有回合对象"）。
        """
        endpoint = self._current_endpoint
        if endpoint is None:
            recent = list(self._private_endpoints.values()) or list(self._group_endpoints.values())
            endpoint = recent[-1] if recent else None
        if endpoint is None:
            return {}
        return {
            'platform': endpoint.platform,
            'platform_id': endpoint.platform_id,
            'platform_name': endpoint.platform_name,
            'self_id': endpoint.self_id,
            'user_id': endpoint.user_id,
            'group_id': endpoint.group_id,
            'channel_id': endpoint.session_id or endpoint.scope,
            'is_group': endpoint.is_group,
            'message_id': endpoint.message_id,
            'umo': endpoint.umo,
        }

    def platform_instance(self, platform: str = '', self_id: str = '') -> Any:
        """按归一化平台名取宿主平台实例（取不到返回 `None`）。"""
        getter = getattr(self.context, 'get_platform_inst', None)
        if not callable(getter):
            return None
        platform_id = self._platform_id_for(platform, self_id) if platform else self._sole_platform_id()
        if not platform_id:
            return None
        try:
            return getter(platform_id)
        except Exception as error:  # noqa: BLE001 - 宿主 API 变了也不该炸
            log_fallback('debug', '取平台实例失败 平台 id=%s 错误=%s', platform_id, error)
            return None

    def onebot_client(self, platform: str = '', self_id: str = '') -> Any:
        """取 OneBot（aiocqhttp）客户端，逐层 `getattr` 判空。

        宿主把 `aiocqhttp` 的 `CQHttp` 实例挂在平台实例的 `bot` 上（`getattr` 链
        一路判空：平台换了实现、或不是 aiocqhttp 时返回 `None`，由调用方降级）。
        """
        instance = self.platform_instance(platform, self_id)
        if instance is None:
            return None
        client = getattr(instance, 'bot', None)
        if client is None:
            getter = getattr(instance, 'get_client', None)
            if callable(getter):
                try:
                    client = getter()
                except Exception as error:  # noqa: BLE001
                    log_fallback('debug', '取 OneBot 客户端失败 错误=%s', error)
                    client = None
        if client is None or not callable(getattr(client, 'call_action', None)):
            return None
        return client

    @staticmethod
    def platform_is_onebot(target: Any = None) -> bool:
        """这个会话坐标所在的平台是不是 OneBot 家族（只有它认 QQ 动作）。

        **判不出来时返回 True**：没有会话坐标（后台回合）不是"平台不支持"，
        交给客户端那一层报一条说明更准确。
        """
        platform = _text(pick(target, 'platform')).strip() if isinstance(target, Mapping) else ''
        if not platform:
            return True
        return platform == 'onebot' or _is_onebot_adapter(platform, platform)

    # ---- 已投递消息号（撤回动作定位「最近一条」） ---- #

    def remember_outbound_message_ids(self, message_ids: Any, umo: str = '') -> None:
        """把宿主回执里取到的消息号记下来（取不到就不记，不影响投递）。"""
        for value in _extract_message_ids(message_ids):
            self._outbound_message_ids.append((_text(umo), value))

    def last_delivered_message_id(self, umo: str = '') -> str:
        """最近一条已投递消息的平台消息号；`umo` 非空时只认该会话。"""
        wanted = _text(umo)
        for recorded_umo, message_id in reversed(self._outbound_message_ids):
            if wanted and recorded_umo != wanted:
                continue
            if message_id:
                return message_id
        return ''

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
        """下载原始字节（图片 / 语音共用）。失败返回 `None` 并记**一条 debug**。

        真机暴露过一个洞（`docs/PORTING_NOTES.md` §49.2）：这里原本是**裸 get** ——
        httpx 的默认超时只有 **5 秒**、`User-Agent` 是 `python-httpx/<版本>`、没有
        `Referer`；而失败只写 debug，于是"下载失败"在那条 warn 里被说成
        "fetch_image 不可用"，谁都看不出到底是超时、是 403、还是证书。现在：

        * 显式超时（`_DOWNLOAD_TIMEOUT_SECONDS`）：QQ 图床的握手 + 下载经常超过 5 秒；
        * 常见客户端 UA：默认的 `python-httpx/x.y` 不是浏览器/QQ 客户端；
        * 腾讯域名带 `Referer`：图床链（`multimedia.nt.qq.com.cn` 之类）按来源校验；
        * 失败日志带上状态码 / 异常类型（仍然是 debug，不刷屏），并且**真的会**进
          `log_fallback`，排查时能看到是哪一种失败。

        ⚠️ 口径（不许写成结论）：UA / Referer 是**预防性补齐** —— 没有有效的 `rkey`
        就没法在真机外复现腾讯图床，所以"加了就一定成功"**未核实**；能核实的是
        "不加的时候请求长什么样"（默认 5 秒超时 + `python-httpx` UA，实测）。
        """
        request_url = _text(url)
        try:
            response = await self._client().get(
                request_url,
                headers=_download_headers(request_url),
                timeout=_DOWNLOAD_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            return response.content
        except Exception as error:  # noqa: BLE001
            log_fallback(
                'debug', '下载失败 URL=%s 类型=%s 错误=%s',
                request_url[:120], type(error).__name__, error,
            )
            return None

    async def request_text(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[dict[str, str]] = None,
        data: Optional[dict[str, str]] = None,
        timeout_ms: int = 20_000,
    ) -> Optional[str]:
        """原始 HTTP（QQ 空间 CGI 走这里）：**自带 Cookie / Referer / UA**，表单编码。

        腾讯那几个 CGI 只认 `application/x-www-form-urlencoded`，所以这里不用
        `post_json`。失败只记 debug 并返回 `None`（QQ 空间是可选能力，不进叙事主链）。
        这层把异常也吞成 `None`；万一将来漏出去，`call_qzone_cgi` 照样按"可能已发生"
        处理（v1.7.6，见 `core/qzone.py`）。
        """
        verb = _text(method).strip().upper() or 'GET'
        try:
            timeout = max(1.0, int(timeout_ms) / 1000.0)
            if verb == 'POST':
                response = await self._client().post(url, data=data or {}, headers=headers or {}, timeout=timeout)
            else:
                response = await self._client().get(url, params=data or None, headers=headers or {}, timeout=timeout)
            response.raise_for_status()
            return response.text
        except Exception as error:  # noqa: BLE001
            log_fallback('debug', '原始 HTTP 失败 %s %s 错误=%s', verb, _text(url)[:120], error)
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

    #: 合并转发读取读的配置段。schema 那边已把隐藏兼容位 `forward_message_compat`
    #: 转正成真分组 `forward_message`，所以**先读新名**、旧名只作旧文件的兜底
    #: （与 QQ 空间转正时同一条路子，见 `AGENTS.md` 坑 66）。
    FORWARD_SECTION_NAMES: tuple[str, ...] = ('forward_message', 'forwardMessage', 'forward_message_compat')

    def forward_section(self) -> dict[str, Any]:
        """读合并转发读取的配置段（新名优先，旧隐藏位兜底，读不到就回空字典）。"""
        for name in self.FORWARD_SECTION_NAMES:
            data = self.section(name)
            if isinstance(data, dict) and data:
                return _forward_section_keys(data)
        return {}

    def forward_ids_for_event(self, event: AstrMessageEvent) -> list[str]:
        """一条入站事件里的合并转发资源 id（按"先原始段、后消息标记"排好序）。

        两个来源都收（去重、保序）：

        1. `raw_media_hints(event)['forwards']` —— **主路径**。AstrBot 的 `Forward` 组件
           带 `id`，但 `get_message_str()` 只给 `[转发消息]`，id 不进消息文本；原始段是
           唯一还带着它的地方。
        2. `extract_forward_ids(消息文本)` —— 兼容别的适配器、旧链路、以及真的把
           `<forward id=…/>` / `[CQ:forward,id=…]` 写进文本的情况。
        3. 消息链里的 `Forward` 组件本身（`_card_markup_from_chain`）—— 原始段缺失
           （适配器没放 `raw_message`）时仍要读得到，`id` 就在组件上。
        """
        ids: list[str] = []
        hints = raw_media_hints(event)
        for value in hints.get('forwards') or []:
            identifier = _text(value)
            if identifier and identifier not in ids:
                ids.append(identifier)
        for value in extract_forward_ids(_text(_call(event, 'get_message_str', ''))):
            if value not in ids:
                ids.append(value)
        for value in extract_forward_ids(_card_markup_from_chain(event)):
            if value not in ids:
                ids.append(value)
        return ids

    async def read_forward_for_event(self, event: AstrMessageEvent) -> Optional[ForwardReadResult]:
        """入站时读合并转发正文（上游 `readForwardContent`，返回 `None` = 没有转发）。

        契约（移植任务书）：

        * **认不出合并转发** → `None`，一行日志都不打；
        * **不是 OneBot 平台**（Telegram / WebChat…）→ 一条 `debug`，`None`。不回
          `failed`：那会在每条含 `[CQ:forward,…]` 字面量的消息上刷一条"读不到"的 warn，
          而这类平台本来就没有这个能力；
        * **OneBot 但拿不到客户端**（平台实例 / 连接没就绪）→ `failure_result()` + 一条
          `warn`（这是真正的异常路径，用户要看得到），调用方照常注入占位文案；
        * **超时 / 平台错误帧 / 响应形状怪** → 同样走 `failure_result()`；
        * **成功** → 归一化后的正文与计数。**任何一条失败分支都不抛异常、不阻断消息消费。**

        **id 从哪来**：AstrBot 的 `get_message_str()` 把合并转发渲染成 `[转发消息]`、
        `Forward` 组件的 `id` 又不进消息文本，所以只有两条路能拿到 id ——
        `message_obj.raw_message` 的 `forward` 原始段（首选，`raw_media_hints()['forwards']`）
        与消息里真正的 Koishi/CQ 标记（`extract_forward_ids`，兼容别的适配器 / 旧链路）。
        """
        ids = self.forward_ids_for_event(event)
        if not ids:
            return None
        # 预算与开关**只读一次**：`forward_section()` 已经把新名 / camelCase / 旧隐藏位
        # 都归一好了，`config_flag('forward_message', 'enabled')` 读不到旧段位那份。
        section = self.forward_section()
        if section.get('enabled') is False:
            return None
        if not self.platform_is_onebot(self.current_target()):
            log_fallback(
                'debug',
                '消息里有合并转发（资源 %s），但当前平台不是 OneBot 家族，不读取正文',
                ids[0][:64],
            )
            return None
        endpoint = self._current_endpoint
        client = self.onebot_client(
            _text(getattr(endpoint, 'platform', '')), _text(getattr(endpoint, 'self_id', '')),
        )
        if client is None:
            log_fallback(
                'warn',
                '收到一条合并转发（资源 %s），但当前平台实例没有可用的 OneBot 客户端'
                '（不是 aiocqhttp/NapCat，或机器人连接未就绪），只保留"这是一条合并转发"的线索',
                ids[0][:64],
            )
            return forward_failure_result()
        limits = forward_read_limits(section)
        try:
            result = await forward_read_ids(ids, _onebot_forward_fetcher(client), _limits_payload(limits))
        except Exception as error:  # noqa: BLE001 - 读取绝不允许打断消息消费
            log_fallback('warn', '合并转发读取异常（资源 %s）：%s', ids[0][:64], error)
            return forward_failure_result()
        if result is None:
            return None
        if result.failed:
            log_fallback('warn', '合并转发内容读取失败（资源 %s），只保留"这是一条合并转发"的线索', ids[0][:64])
        else:
            log_fallback(
                'debug',
                '合并转发已读取：资源=%s 节点=%s 嵌套=%s 截断=%s 字符=%s',
                ids[0][:64], result.node_count, result.forward_count, result.truncated, len(result.content),
            )
        return result

    async def handle_event(self, event: AstrMessageEvent) -> list[str]:
        """把一条入站事件喂给 service 的入站入口，返回本回合的可见回复。

        逐条对应上游 `apply()` 里的中间件（`upstream/src/index.ts:503-516`）：

        1. 空文本且没有语音 → 不处理；**但只要是归我们管的私聊（capture 开着、
           白名单通过），仍然吞掉事件**，别让宿主的另一个聊天 Agent 在同一段私聊里
           用第二个人格回答（受控偏离，见移植说明）；
        2. 盲区模式下的管理命令 → 静默吞掉（`blindMode.enabled` 时上游直接
           `return`，不返回 `next()`）；
        3. `runtime.ignore_command_messages` 下的管理命令 → 交回命令解析器；
        4. 群聊 → `service.receive_group()`，私聊 → `service.receive()`；
           两者返回 `True` 表示已消费，此时调用 `event.stop_event()`，
           与上游"中间件不调用 `next()`"同义（沉默也被吞掉）。

        返回值是 `send_session` 在本回合捕获到的可见回复文本列表（顺序与上游
        `session.send` 调用顺序一致），由 `main.py` 用 `yield` 交回 AstrBot。

        **合并转发（上游 `forwardMessage` 组）**：在 `non_message` 之后、判"空内容"之前
        读一次正文（`read_forward_for_event()`）。位置有两个理由：① 事件坐标必须先登记
        （`remember_event`），否则拿不到平台实例 / OneBot 客户端；② 读到的正文要注入
        `SessionView.content`，否则 `[CQ:forward,id=…]` 卡片在下游只是一段认不出的标记。
        **读取失败绝不阻断消费**：失败只留一条 warn，消息照常往下走（见那条路径的分支）。
        """
        await self.ensure_started()
        # 本回合的回复缓冲从零开始：早退的分支（通知 / 命令 / 空事件）不进 `begin_capture`，
        # 留上一回合那份会让 `main.py` 把上一条回复**再发一遍**。
        self._last_capture = None
        endpoint = endpoint_for_event(event)
        non_message, kind_label = is_non_message_event(event)
        if non_message:
            session = session_view(event, endpoint)
            # OneBot 的通知 / 元事件 / 请求不是聊天内容，永远成不了回合。以前它们走的是
            # "空内容"那条路，每条都打一条 warn——NapCat 的「对方正在输入…」一分钟能来
            # 十几条，把日志里真正该看的东西全盖掉了（用户 2026-09-25 的日志就是这样）。
            # 现在静默处理：消费策略与过去逐字一致（只有"归我们管的私聊"才吞，避免空
            # 事件漏给后面坐着的那个人格），只是不再用 warn 打扰人。
            if self.owns_private_session(session):
                event.stop_event()
                log_fallback('debug', '已吞掉非消息事件 类型=%s 平台=%s 用户=%s',
                             kind_label, session.platform, session.user_id)
            else:
                log_fallback('debug', '忽略非消息事件 类型=%s', kind_label)
            return []
        # 登记坐标要用 `session`，而 session 的构建又要等合并转发读完——所以这里先建
        # 一次**便宜的**视图（纯读、不发请求）交给 `remember_event`，读完正文后再建一次
        # 带上注入内容。两次构建只差那几个字段，比让登记与读取互相依赖划算。
        self.remember_event(event, session_view(event, endpoint), endpoint)
        forward_read = await self.read_forward_for_event(event)
        session = session_view(event, endpoint, forward_read)
        content = session.content

        if not content.strip() and not self._has_voice(session):
            # 上游这里 `next()`：把这条消息交回其它处理器。但 AstrBot 的部署里往往还有
            # 另一个聊天 Agent（默认 Agent / 别的拟人插件），交回去 = 同一个私聊里冒出
            # 第二个人格（实测：用户"一张图 + 一句文字"分两条发来，图片那条事件的消息链
            # 解析为空，于是另一个她回了「主人这是夜班的宵夜吗？」）。
            # 所以：这条私聊归我们管就**吞掉**，并留一条 warn 说明为什么什么都没发生。
            self.consume_unusable_private_event(event, session)
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
        elif not endpoint.is_group and self.owns_private_session(session):
            # 我们看了、但没能把它变成一回合（故事暂停 / 参与者不在 / 适配器给的形状怪…）：
            # **照样吞掉**。上游在这里 `next()`，而 AstrBot 的后面坐着第二个 Agent——
            # 那会变成"这段私聊换个人格答话"（见移植说明）。
            # 同时留一条可见的 warn，把"为什么什么都没发生"写在日志里。
            event.stop_event()
            await self._report_unconsumed_private(event, session)
        return list(capture.texts)

    def owns_private_session(self, session: SessionView) -> bool:
        """这条私聊归我们管吗：私聊 + capture 开着 + `can_handle_session` 通过。

        判定本身是纯读，不会动事件；调用方决定"吞掉"还是"交回"。
        """
        try:
            if not session.is_direct:
                return False
            if not self.config_flag(
                'runtime', 'capture_direct_messages', 'captureDirectMessages', default=True,
            ):
                return False
            return bool(self.service.can_handle_session(session))
        except Exception as error:  # noqa: BLE001 - 归属判定失败按"不归我们"处理
            log_fallback('warn', '私聊归属判定失败，按不消费处理 错误=%s' % error)
            return False

    async def explain_unconsumed(self, session: SessionView) -> str:
        """`receive()` 为什么没生成回合：按它的门顺序逐条查，返回第一个不满足的原因。

        为什么需要：core 里这些判断走的是 `report_operation(..., 'diagnostic', ...)`，
        `logging.verbosity` 不到 diagnostic 时**日志里一个字都看不到**——用户实测
        "私聊事件未生成回合"只有我们这条 warn，根本猜不出是哪道门（白名单？剧本暂停？
        参与者不在？还是图片没解析出来？）。这里把门重放一遍，只读、不改状态。
        """
        service = self.service
        try:
            if not service.can_handle_session(session):
                return '白名单未通过 can_handle_session'
            story = await service.find_story(session)
            if not story:
                return '找不到剧本，且 runtime.auto_create 关着'
            status = _text(pick(story, 'status')) or '?'
            if status != 'active':
                return '剧本状态=%s（不是 active）' % status
            participant = await service.find_participant(session, story)
            if not participant:
                return '参与者不存在，且 auto_create / auto_enroll_participants 都关着'
            pstatus = _text(pick(participant, 'status')) or '?'
            if pstatus != 'active':
                return '参与者状态=%s（不是 active）' % pstatus
            content = _text(session.content)
            voice = self._has_voice(session)
            if not content.strip() and not voice:
                return '没有文字也没有语音'
            observed = service.describe_vision_event(session)
            sources = pick(observed, 'sources') or []
            if (not _text(pick(observed, 'content')).strip() and not sources
                    and not voice and not _session_file_facts(session)):
                return '没有可用内容：图片/文件都没解析出来（content 里是什么见预览）'
            return '门都过了（可能是队列/串行化问题，看 core 的 debug 日志）'
        except Exception as error:  # noqa: BLE001 - 诊断自身不能抛
            return '诊断失败：%s' % error

    async def _report_unconsumed_private(self, event: AstrMessageEvent, session: SessionView) -> None:
        outline = _text(_call(event, 'get_message_outline', '')) or '(空)'
        reason = await self.explain_unconsumed(session)
        log_fallback(
            'warn',
            '私聊事件未生成回合，已吞掉以免宿主另一个聊天 Agent 接手 '
            '平台=%s 用户=%s 概要=%s 消息段=%d 内容长度=%d 原因=%s 内容预览=%s'
            % (session.platform, session.user_id, outline,
               len(session.elements or []), len(session.content or ''),
               reason, _text(session.content)[:80]),
        )

    def consume_unusable_private_event(self, event: AstrMessageEvent, session: SessionView) -> bool:
        """归我们管的私聊里"没有可用内容"的事件：吞掉，并说清楚为什么。

        返回是否真的吞了。判定三件套：**私聊** + `capture_direct_messages` 开着 +
        `can_handle_session` 通过（OneBot 白名单）。三者缺一就保持上游语义（交回其它
        处理器）——没被我们接管的聊天，别人答不答与我们无关。

        为什么需要它：宿主常配着第二个聊天 Agent（默认 Agent、别的拟人插件）。我们的
        事件一旦"看了但不消费"，同一段私聊里就会出现第二个人格回答，用户看到的是
        "她在自言自语两套词"。空内容本身也值得留痕：图片/文件这类"消息链解析为空"
        的事件（适配器把附件放在别处）就是从这里漏过去的。
        """
        # 管理命令交给命令解析器（上面的分支已经处理过），这里只兜"说不出话"的事件。
        if not self.owns_private_session(session):
            return False
        event.stop_event()
        self._report_unusable(event, session)
        return True

    @staticmethod
    def _report_unusable(event: AstrMessageEvent, session: SessionView) -> None:
        outline = _text(_call(event, 'get_message_outline', '')) or '(空)'
        log_fallback(
            'warn',
            '私聊事件没有可用内容：已吞掉以免宿主另一个聊天 Agent 接手 '
            '平台=%s 用户=%s 概要=%s 消息段=%d'
            % (session.platform, session.user_id, outline, len(session.elements or [])),
        )

    # ------------------------------------------------------------------ #
    # 回合捕获
    # ------------------------------------------------------------------ #

    def begin_capture(self, endpoint: AstrbotEndpoint) -> _TurnCapture:
        # 新回合开始：上一回合的缓冲立即作废（`turn_replies` 只读刚结束的那一份）。
        self._last_capture = None
        capture = _TurnCapture(
            platform=endpoint.platform,
            self_id=endpoint.self_id,
            scope=endpoint.scope,
            umo=endpoint.umo,
        )
        self._capture = capture
        return capture

    def end_capture(self) -> None:
        # 回合结束后 `main.py` 还要按"文字 / 语音"交回宿主（见 `turn_replies`），
        # 所以把刚结束的那一份留着；下一次 `begin_capture` 覆盖它。
        self._last_capture = self._capture
        self._capture = None

    @property
    def capture(self) -> Optional[_TurnCapture]:
        return self._capture

    def turn_replies(self) -> list[dict[str, Any]]:
        """刚结束的回合里要交回宿主的可见回复：`[{'content', 'voice'}]`。

        `voice` 是**已经合成好的音频文件路径**（正文 `<tts/>` 标记指定的分段），
        空串表示这条照旧发文字。`main.py` 据此决定 `plain_result` 还是
        `chain_result(voice_components(...))`——它不碰 astrbot 的消息组件细节。
        """
        capture = self._last_capture
        return capture.replies() if capture is not None else []

    @staticmethod
    def voice_components(path: str) -> list[Any]:
        """语音段的出站组件（`main.py` 用 `event.chain_result(...)` 交回宿主）。"""
        return [_build_record({'file': _text(path)})]

    # ------------------------------------------------------------------ #
    # 配置读取（给 main.py 用，避免它 import core）
    # ------------------------------------------------------------------ #

    def section(self, name: str) -> dict[str, Any]:
        """读一个 snake_case 配置段（兼容对象式配置）。

        `name` 既可以是顶层组名，也可以是**点分路径**（`robot_actions.chat` /
        `runtime.input_status`）：v1.7.4 起动作开关并进了一个父组、输入状态挪进了
        「运行时」当子配置，落点由 `platform_actions.ACTION_CONFIG_GROUPS` 定，是点分路径。

        `normalize_config` 已经补过分组别名，正常情况下 `name` 直接命中；这里仍
        按 `CONFIG_SECTION_ALIASES` 反向兜底一次（例如有人直接构造了未过归一化的
        `AstrbotBridge`，或将来别名表变化）。

        **嵌在「模型中心」里的段优先读嵌套的那一份**（`vision` / `audio` /
        `embedding` / `compaction`）：`_conf_schema.json` 把它们放在
        `model_center.<段>` 下，core 也读 `model.<段>`，顶层根本没有这几个分组。
        早期版本只看顶层，于是「运行开关」读出的是默认值而不是真实状态（配置里开着
        显示成关着、关着显示成开着，重启宿主剥掉历史遗留的假顶层键后必现）。
        顶层同名键只在嵌套缺失时兜底——那是旧版本控制台写出来的垃圾，宿主下次加载就会删。

        **N:1 归并**（v1.7.2 起，v1.7.4 改成点分目标）：动作开关组由十个收敛成一个父组
        下的三个子组，旧分组留在 schema 里当隐藏兼容位。读 `robot_actions.chat` 这类
        新路径时，缺的键从旧分组补（新值优先），所以用户升级前设过的开关照旧读得到；
        旧分组本身也能直接读（`actions_interaction` 仍然回它自己的那份）。写方向只写
        新路径，见 `core/service/config.py` 的 `LEGACY_SECTION_MERGES`。
        """
        if name in NESTED_MODEL_SECTIONS:
            nested = self.section('model').get(name)
            if isinstance(nested, dict):
                return nested
        section: Any = None
        if '.' in name:
            # 嵌套目标：只按**整条路径**取。不回落父级对象（那棵子树的键名对不上）。
            if isinstance(self.config, dict):
                section = read_section_path(self.config, name)
        else:
            section = self.config.get(name) if isinstance(self.config, dict) else None
            if section is None:
                for alias, target in CONFIG_SECTION_ALIASES.items():
                    if target == name and isinstance(self.config, dict):
                        section = self.config.get(alias)
                        break
        if isinstance(section, dict):
            values = section
        elif section is None:
            values = {}
        else:
            values = {key: value for key, value in vars(section).items() if not key.startswith('_')}
        return _merge_legacy_section_values(self.config, name, values)

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

    # ------------------------------------------------------------------ #
    # 配置导入 / 导出
    # ------------------------------------------------------------------ #

    def config_file_path(self) -> str:
        """AstrBot 存放本插件配置的位置：`<astrbot_data>/config/<插件名>_config.json`。"""
        return str(Path(get_astrbot_data_path()) / 'config' / f'{PLUGIN_NAME}_config.json')

    def raw_config(self) -> dict[str, Any]:
        """导出用的**原样**配置。

        优先读磁盘上的配置文件——那是用户实际存下来的东西（含我们不认识的键），
        比内存里归一化后的 `self.config` 更忠实；读不到才回落到内存副本。
        """
        path = self.config_file_path()
        try:
            with open(path, encoding='utf-8-sig') as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
        live = self._live_config
        if isinstance(live, dict):
            return {k: v for k, v in live.items()}
        try:
            return {k: v for k, v in dict(live).items()}
        except Exception:  # noqa: BLE001 - 拿不到就退回归一化后的副本
            return self.config

    def export_config(self, *, note: str | None = None) -> dict[str, Any]:
        """打包一份可长期保存的导出信封（格式与兼容契约见 `core/config_io.py`）。"""
        from ..core.config_io import build_export  # noqa: PLC0415
        from ..core.meta import HDS_INTERLUDE_VERSION  # noqa: PLC0415

        return build_export(
            self.raw_config(),
            plugin_version=_plugin_version(),
            upstream_version=HDS_INTERLUDE_VERSION,
            note=note,
        )

    def _merge_import(self, incoming: Any) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """算导入的**实际写盘目标**与变更概览。

        返回 `(current, target, diff)`。关键是 `target` 才是"导入之后配置长什么样"，
        预览与报告都必须拿它跟 `current` 比——拿文件原文比会把磁盘上"文件里没提到
        的键"全报成 `removed`，用户会以为导入会清空配置（实际不会）。
        """
        from ..core.config_io import diff_config  # noqa: PLC0415

        current = normalize_bridge_config(self.raw_config())
        # 只叠用户**显式写出**的键：补过默认值的副本会把磁盘上的用户值顶掉
        target = normalize_bridge_config(_deep_merge(current, explicit_config(incoming)))
        return current, target, diff_config(current, target)

    def preview_config_import(self, payload: Any) -> dict[str, Any]:
        """只解析与比较，**不写盘**——给导入前的确认用。"""
        from ..core.config_io import parse_import  # noqa: PLC0415

        parsed = parse_import(payload)
        _current, _target, diff = self._merge_import(parsed['config'])
        parsed['diff'] = diff
        # 分组数报告**文件里显式写的**分组数，不是补完默认值后的分组数
        parsed['section_count'] = len(explicit_config(parsed['config']))
        return parsed

    async def import_config(self, payload: Any) -> dict[str, Any]:
        """解析并按当前格式落库一份导入配置，返回给用户看的报告。

        流程：**解析 → 归一化/补默认 → 与当前配置逐键比较 → 写回**。
        归一化走 `core.service.config.normalize_config`（分组别名 + 默认值补全），
        迁移链走 `core.config_io.migrate_config`；两者都不丢未知键。
        """
        from ..core.config_io import ConfigImportError, parse_import  # noqa: PLC0415
        from ..core.service.config import to_schema_shape  # noqa: PLC0415

        try:
            parsed = parse_import(payload)
        except ConfigImportError:
            raise
        incoming = parsed['config']
        _current, target, diff = self._merge_import(incoming)

        # 落盘必须是 **schema 形状**（`model_center` / `qq_access`）：AstrBot 的配置页
        # 按 `_conf_schema.json` 渲染，写成上游名会让用户在配置页看到"全是默认值"。
        saved_via = await self._save_config(to_schema_shape(target))

        # 让运行中的服务立刻用上新配置，不必重启
        self.config = target
        try:
            self.service.config = self.routing_config()
        except Exception:  # noqa: BLE001 - 服务未就绪时忽略
            pass

        return {
            'format_version': parsed['format_version'],
            'source': parsed['source'],
            'envelope': parsed['envelope'],
            'notes': parsed['notes'],
            'warnings': parsed['warnings'],
            'diff': diff,
            'saved_via': saved_via,
            'config_path': self.config_file_path(),
        }

    async def _save_config(self, config: dict[str, Any]) -> str:
        """写回配置。优先用 AstrBot 的 `save_config`（会通知运行时），失败则直接写文件。"""
        live = self._live_config
        save = getattr(live, 'save_config_async', None)
        if callable(save):
            try:
                await save(config)
                return 'astrbot-config-api'
            except Exception as error:  # noqa: BLE001 - 回落文件写入
                log_fallback('warn', 'AstrBot 配置保存失败，改用直接写文件：%s', error)
        save = getattr(live, 'save_config', None)
        if callable(save):
            try:
                save(config)
                return 'astrbot-config-api'
            except Exception as error:  # noqa: BLE001
                log_fallback('warn', 'AstrBot 配置保存失败，改用直接写文件：%s', error)
        path = self.config_file_path()
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            with open(path, 'w', encoding='utf-8') as handle:
                handle.write('\ufeff')          # AstrBot 配置文件带 BOM
                json.dump(config, handle, ensure_ascii=False, indent=2)
            return 'config-file'
        except OSError as error:
            raise RuntimeError(f'配置写入失败：{error}') from error

    async def save_raw_config(self, config: dict[str, Any]) -> str:
        """把一份**原样配置**写回磁盘，并让运行中的服务立刻生效。

        控制台改开关 / 连接池走这里，跟配置导入共用同一条写回路径（`to_schema_shape`
        → `_save_config`），保证三件事一致：BOM、schema 分组名、AstrBot 的配置变更通知。

        **生效用的是刚写下去的这份，而不是回头再读一遍磁盘**：宿主的 `save_config`
        是同步写、但将来未必；而且连续两次写（比如"加一条再删一条"）时，回读可能拿到
        还没落盘的旧值。写什么就生效什么，最省心。
        """
        from ..core.service.config import to_schema_shape  # noqa: PLC0415

        saved_via = await self._save_config(to_schema_shape(config))
        self.apply_config(config)
        return saved_via

    def apply_config(self, config: dict[str, Any]) -> None:
        """把一份配置装进内存并交给服务（不落盘）。

        `self.config` 始终是干净配置（不含 `routing_config()` 合成的连接行），
        交给服务的是补过合成行的副本——与 `__init__` 保持一致。
        """
        self.config = normalize_bridge_config(config)
        try:
            self.service.config = self.routing_config()
        except Exception:  # noqa: BLE001 - 服务未就绪时忽略
            pass

    def reload_config(self) -> None:
        """按**当前磁盘上的配置**重建内存副本，并交给服务。"""
        self.apply_config(self.raw_config())

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
