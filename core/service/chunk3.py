"""Chunk3：`upstream/src/service.ts` 第 2573–3216 行的成员。

按 `docs/PORT_PLAN_SERVICE.md` 的分块表，本文件只包含**声明起始行落在
[2573, 3216) 内**的 20 个成员，成员顺序与上游一致：

| 上游行 | 成员 | 主题 |
| --- | --- | --- |
| 2573 | `invalidateHistoryVectors` | 历史向量缓存失效 |
| 2590 | `backfillHistoryEmbeddings` | 历史向量后台回填 |
| 2651 | `loadNativeAudio` | 原生音频装载 |
| 2666 | `fetchNativeAudio` | 单条音频取回 |
| 2719 | `describeVisionEvent` | 图片事件文本化 |
| 2733 | `loadNativeImages` | 原生图片装载 |
| 2749 | `describeCurrentImages` | 侧端识图 |
| 2769 | `fetchNativeImage` | 单张图片取回 |
| 2817 | `imageBytesToNative` | 字节 → 原生图片附件 |
| 2834 | `downscaleImageForVision` | 视觉降采样（上游 Puppeteer → 本移植版 PIL） |
| 2868 | `renderAnimatedImageFrame` | 动态图抽帧（上游 Puppeteer → 本移植版 PIL） |
| 2898 | `invalidateBufferedNarratives` | 缓冲回合作废 |
| 2926 | `hasPendingNarrative` | 前台回合占位判定 |
| 2937 | `flushBufferedNarrative` | 缓冲叙事 flush（合并写作回合） |
| 3140 | `advanceStory` | 剧本推进入口 |
| 3150 | `deliverMessages` | 混合投递入口 |
| 3157 | `compactStory` | 压缩入口 |
| 3165 | `compactOverlay` | Overlay 合并入口 |
| 3171 | `adminOverlayStatus` | 管理命令只读视图 |
| 3188 | `sweep` | 后台扫描主循环 |

`advanceUnlocked`(3217) / `decide`(3437) / `shouldRefreshContinuity`(3604) /
`planAutomaticTimeline`(3613) / `isTimelineDirectorFused`(3689) /
`persistTimelineRetry`(3698) / `tryDecide`(3720) 的**声明起始行都 ≥ 3217**，
归 Chunk4，本文件刻意不定义（两个 mixin 重复定义同名成员会让 MRO 静默覆盖）。

## 键名法（`docs/PORT_PLAN.md` §2）

* 类字段/方法名一律 snake_case；跨成员调用 `self.other_method()`。
* **发给模型的 payload / 持久化 wire format 保持上游 camelCase**：
  `NarrativeDecision` 的 `localMedia` / `nativeFace` / `interaction.reply.sendAt`、
  剧本条目 `metadata`、`state.extensions.historyBackfill`、
  落库的 camelCase 列名。本文件里对 `decision` 的**改写**因此写 camelCase。
* **从外部读入**（数据库行、旧数据、其它 chunk 的中间 dict、会话视图）一律
  `pick()` 双读，优先上游 camelCase。

## 与已落地代码的互操作（三处刻意妥协）

1. **缓冲回合 dict 的拼写**：`config.py` 的 `BufferedNarrativeTurn` 用 snake_case
   （`next_revision` / `in_flight_request_id`），而已经落地的 `base.py`（Chunk0
   `set_desktop_runtime_phase`）读写的是 camelCase（`nextRevision` /
   `inFlightRequestId`）。两者都能创建这个 dict，所以本文件用 `_turn_get` 双读、
   用 `_turn_set` **跟随该 dict 已有的拼写**（空 dict 默认上游 camelCase），
   两种拼写都不会被读漏。
2. **`id > cursor` 查询**：`Database` 只支持等值 `where`（见 `base.db_get`
   文档串），所以上游 `{storyId, id: {$gt: cursor}}` 的归档窗口改为
   「按 id 升序取回该剧本条目后在 Python 侧裁剪出 cursor 之后的 128 行」。
   消费侧仍然只使用 128 行、并且每次最多写入 `batchSize` 条向量，
   上游「有界查询 / 有界写入」的意图由 `_archive_window_after` 的切片保留。
3. **`{$in: ids}` 更新**：上游 `dbSet('interlude_intent', {id: {$in: consumedDueIds}})`
   在本移植版拆成逐 id 的 `db_set`（`Database` 无算子支持）。

## 降级（`docs/PORT_PLAN_SERVICE.md` §8，不是 TODO）

* Puppeteer 的降采样 / 抽帧 → `PIL`（try-import）。`PIL` 缺失时
  `downscale_image_for_vision` / `render_animated_image_frame` 返回 `None`，
  调用方继续透传原图并记一条 debug 日志，**不抛异常**。
* `onebot-file:` 语音的 SnowLuma 服务端转码 → `session.bot.internal._request`
  或 `Transport.transcode_record` 的可选钩子；两者都不可用时返回 `None`
  （等价上游 `typeof internal?._request !== 'function' ? undefined`）。
* 图片 `content-type` 头：`Transport.fetch_image` 只回字节，故 MIME 走魔数嗅探
  `guess_image_mime`（上游优先用响应头）。
"""

from __future__ import annotations

import asyncio
import base64
import io
import math
import re
from typing import Any, Optional
from urllib.parse import unquote, urlsplit

from ..delivery import message_event_reference
from ..qq_face import normalize_qq_native_face_segments
from ..script.commit_builder import find_outgoing_script_event
from ..script.delivery_ledger import platform_action_reference
from ..script.intent_lifecycle import consumed_live_intent_ids, live_narrative_intents
from ..story_state import decode_story_state, encode_story_state
from ..time import format_log_time, iso, parse_dt
from .base import ServiceBase, pick
from .config import RECALLABLE_ENTRY_KINDS, is_trusted_image_host
from .helpers import (
    guess_audio_format,
    guess_image_mime,
    is_animated_image_mime,
    is_record,
    narrative_cursor,
    normalize_participant_state,
    should_downscale_image,
)

try:  # pragma: no cover - 取决于同批任务的落地顺序
    from . import helpers as _helpers_module
except ImportError:  # pragma: no cover
    _helpers_module = None  # type: ignore[assignment]

try:  # 上游用 Puppeteer 做图像缩放 / 抽帧；本移植版用 PIL，缺失即安全降级。
    from PIL import Image as _PIL_IMAGE
    from PIL import ImageOps as _PIL_IMAGE_OPS
except Exception:  # pragma: no cover - 环境未装 Pillow
    _PIL_IMAGE = None  # type: ignore[assignment]
    _PIL_IMAGE_OPS = None  # type: ignore[assignment]

#: `PIL` 是否可用（测试用 `@unittest.skipUnless` 守门）。
PIL_AVAILABLE = _PIL_IMAGE is not None

__all__ = ['PIL_AVAILABLE', 'ServiceChunk3']

#: 视觉链路的硬上限（上游 `fetchNativeImage` / `renderAnimatedImageFrame` 内联值）。
MAX_NATIVE_IMAGE_BYTES = 4 * 1024 * 1024
#: 上游 `imageBytesToNative` 注释里给动态图片抽帧声明的最大边。
MAX_ANIMATED_FRAME_DIMENSION = 4096
#: 上游 `fetchNativeAudio` 的 `withTimeout(..., 30_000)`。
MEDIA_FETCH_TIMEOUT_SECONDS = 30.0
#: 上游回填循环的软时限：`Date.now() - started >= 20_000`。
BACKFILL_TIME_BUDGET_MS = 20_000
#: 上游 `this.historyBackoff.set(storyId, Date.now() + 60_000)`。
BACKFILL_FAILURE_COOLDOWN_MS = 60_000
#: 上游归档窗口 `{ limit: 128, sort: { id: 'asc' } }`。
ARCHIVE_WINDOW_ROWS = 128
#: 上游最近窗口 `{ limit: 64, sort: { id: 'desc' } }`。
RECENT_WINDOW_ROWS = 64
#: 本移植版音频通道默认值（`config.py` 的 CONFIG_DEFAULTS 同值）。
DEFAULT_AUDIO_MAX_FILE_SIZE_MB = 10
DEFAULT_AUDIO_MAX_PER_MESSAGE = 1


# =========================================================================== #
# 通用小工具
# =========================================================================== #

def _to_snake(name: str) -> str:
    """`camelCase` → `snake_case`（只用于生成双读的第二个键名）。"""
    return re.sub(r'(?<!^)(?=[A-Z])', '_', name).lower()


def _text(value: Any) -> str:
    """上游 `String(value ?? '')`。"""
    return '' if value is None else str(value)


def _member(value: Any, camel: str, snake: Optional[str] = None) -> Any:
    """读一个「可能是 dict（外部输入）也可能是对象（`SessionView`）」的字段。"""
    if value is None:
        return None
    target = snake or _to_snake(camel)
    if isinstance(value, dict):
        return pick(value, camel, target)
    if hasattr(value, target):
        return getattr(value, target)
    return getattr(value, camel, None)


def _mapping(value: Any) -> dict[str, Any]:
    """把 dict / 对象统一读成 dict（用于适配器返回的 `getImage` 结果）。"""
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    data = getattr(value, '__dict__', None)
    if isinstance(data, dict):
        return {key: item for key, item in data.items() if not key.startswith('_')}
    return {}


def _unique(values: Any) -> list[Any]:
    """上游 `Array.from(new Set(...))`：保序去重。"""
    seen: list[Any] = []
    for item in values or []:
        if item not in seen:
            seen.append(item)
    return seen


def _int_value(value: Any, default: int = 0) -> int:
    """宽松整数读取（JS `Math.floor(Number(x))` 的可用子集）。"""
    if isinstance(value, bool) or value is None:
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return default if value != value else int(math.floor(value))
    try:
        return int(math.floor(float(value)))
    except (TypeError, ValueError):
        return default


def _number_value(value: Any, default: float) -> float:
    """宽松浮点读取；非法值回落 `default`。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    number = float(value)
    return default if number != number else number


def _is_safe_integer(value: Any) -> bool:
    """上游 `Number.isSafeInteger`。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return abs(value) <= 2 ** 53 - 1


def _base64_bytes_length(text: str) -> int:
    """上游 `Buffer.byteLength(text, 'base64')`（忽略非法字符与填充）。"""
    clean = re.sub(r'[^A-Za-z0-9+/=]', '', text)
    padding = len(clean) - len(clean.rstrip('='))
    return max(0, (len(clean) * 3) // 4 - padding)


def _decode_base64(text: str) -> Optional[bytes]:
    """宽松 base64 解码；非法输入返回 `None`。"""
    clean = re.sub(r'\s+', '', text)
    try:
        return base64.b64decode(clean + '=' * (-len(clean) % 4), validate=False)
    except Exception:
        return None


def _data_uri_payload(data_uri: Any) -> Optional[bytes]:
    """取 `data:...;base64,<payload>` 的原始字节；形状不对返回 `None`。"""
    text = _text(data_uri)
    marker = text.find(',')
    if marker < 0 or ';base64' not in text[:marker]:
        return None
    return _decode_base64(text[marker + 1:])


def _read_local_file(path: str) -> bytes:
    """上游 `readFile(candidate)`：读不到就抛（调用方 catch 后试下一个候选）。"""
    with open(path, 'rb') as handle:
        return handle.read()


def _helper(name: str) -> Any:
    """取 `helpers.py` 里的移植版实现（优先）；缺失返回 `None`。"""
    candidate = getattr(_helpers_module, name, None) if _helpers_module is not None else None
    return candidate if callable(candidate) else None


# =========================================================================== #
# 配置读取（`config.py` 的 Config 可能已被 `normalize_config` 归一为 snake_case，
# 也可能是上游 Console 的 camelCase 原文 —— 一律双读）
# =========================================================================== #

def _group(config: Any, name: str) -> dict[str, Any]:
    """读一个配置分组（dict 双读；对象走属性）。"""
    if config is None:
        return {}
    if isinstance(config, dict):
        return pick(config, name, _to_snake(name)) or {}
    data = getattr(config, _to_snake(name), None)
    if data is None:
        data = getattr(config, name, None)
    return _mapping(data)


def _value(config: Any, camel: str, default: Any = None) -> Any:
    """读一个配置项；缺失/`None` 回落 `default`。"""
    got = pick(config, camel, _to_snake(camel))
    return default if got is None else got


def _audio_config(service: Any) -> dict[str, Any]:
    """上游 `this.audioConfig`（`resolveAudioConfig(config.model.audio)`）。

    注意：上游把音频配置放在 `config.model.audio`；`ServiceBase.audio_config`
    读的是顶层 `config.audio`。这里按**上游位置**读，顶层缺失时才回落到基类属性。
    """
    raw = pick(_group(service.config, 'model'), 'audio')
    if isinstance(raw, dict) and raw:
        return raw
    fallback = getattr(service, 'audio_config', None)
    return fallback if isinstance(fallback, dict) else {}


def _vision_config(service: Any) -> dict[str, Any]:
    """上游 `this.config.model.vision`。"""
    raw = pick(_group(service.config, 'model'), 'vision')
    return raw if isinstance(raw, dict) else {}


def _embedding_config(service: Any) -> dict[str, Any]:
    """上游 `this.config.model.embedding`。"""
    raw = pick(_group(service.config, 'model'), 'embedding')
    return raw if isinstance(raw, dict) else {}


def _runtime_config(service: Any) -> dict[str, Any]:
    """上游 `this.config.runtime`。"""
    return _group(service.config, 'runtime')


# =========================================================================== #
# 缓冲回合 dict 的拼写自适应（见模块文档串「互操作」第 1 条）
# =========================================================================== #

def _turn_get(turn: Any, camel: str, snake: Optional[str] = None) -> Any:
    """读缓冲回合 / 缓冲消息的字段：camelCase 与 snake_case 都认。"""
    return pick(turn, camel, snake or _to_snake(camel))


def _turn_set(turn: dict[str, Any], camel: str, snake: str, value: Any) -> Any:
    """写缓冲回合的字段：**跟随该 dict 已有的拼写**，空 dict 用上游 camelCase。

    `base.py`（Chunk0）写/读 camelCase，`config.py` 的 TypedDict 声明的是
    snake_case；创建这个 dict 的 chunk 未知，所以不能单方面固定一种拼写。
    """
    key = snake if (snake in turn and camel not in turn) else camel
    turn[key] = value
    return value


# =========================================================================== #
# 上游 8317 `formatBufferedUserMessages` / 7036 `extractSessionImageSources`
# —— 本行范围内调用、但 helpers.py 尚未落地的模块级辅助函数（等价实现兜底）
# =========================================================================== #

def _fallback_format_buffered_user_messages(messages: list[Any]) -> str:
    """上游 `formatBufferedUserMessages`（`src/service.ts:8317`）逐字移植。

    单条消息直接返回内容；多条按上游格式拼接（用于让模型知道这是连续消息）。
    """
    if len(messages) == 1:
        return _text(_turn_get(messages[0], 'content'))
    blocks: list[str] = []
    for index, message in enumerate(messages):
        occurred = iso(parse_dt(_turn_get(message, 'occurredAt', 'occurred_at'))) or ''
        blocks.append('[连续消息 %d，收到时间 %s]\n%s' % (
            index + 1, occurred, _text(_turn_get(message, 'content')),
        ))
    return '\n\n'.join(blocks)


def _local_image_path(value: Any) -> str:
    """把适配器给的本地图片路径归一成文件系统路径（`file:///x` → `/x`）。"""
    text = _text(value).strip()
    if text.lower().startswith('file://'):
        text = text[len('file://'):]
        # `file:///a/b` → `/a/b`；`file://host/a` 这种少见形式只取路径段。
        if not text.startswith('/'):
            slash = text.find('/')
            text = text[slash:] if slash >= 0 else ''
        try:
            from urllib.parse import unquote  # noqa: PLC0415

            text = unquote(text)
        except Exception:  # pragma: no cover - 解码失败就用原串
            pass
    return text.strip()


def _fallback_extract_session_image_sources(session: Any) -> list[str]:
    """上游 `extractSessionImageSources`（`src/service.ts:7036`）逐字移植。

    只解析这条消息的原始内容（`session.elements` 归适配器所有，可能被别的
    中间件跨回合复用，否则旧图片元素会被误挂到后续纯文本回合上）。
    """
    raw = _text(_member(session, 'content'))
    sources: list[str] = []

    def add(value: Any, kind: str = 'url') -> None:
        source = _text(value).strip()
        if not source or source in sources:
            return
        if len(source) > 8 * 1024 * 1024:
            return
        if re.match(r'^https?://', source, re.IGNORECASE):
            sources.append('onebot-url:%s' % source if kind == 'adapter-url' else source)
        elif re.match(r'^data:image/', source, re.IGNORECASE):
            sources.append(source)
        elif kind == 'file':
            sources.append('onebot-file:%s' % source)

    def image_src(element: Any) -> Any:
        if not is_record(element):
            return None
        attrs = element.get('attrs') if is_record(element.get('attrs')) else {}
        data = element.get('data') if is_record(element.get('data')) else {}
        return attrs.get('src') or attrs.get('url') or data.get('src') or data.get('url')

    # ---- 第一遍：**适配器直给的元素**（`session.elements`），这一份是可信的 ----
    #
    # AstrBot 的 NapCat 适配器可能把图片落到本地再交给我们（`file:///…` / 绝对路径），
    # 而上游 Koishi 那边 `<img>` 永远带 CDN 地址，所以上游的 `add()` 只认 http/data。
    # 若这里也丢掉，`describeVisionEvent` 会得到"既无文字也无来源" → `receive` 的视觉门
    # 把整条消息判死（用户实测：图片消息全部不产生回合，日志里只有"未生成回合"）。
    #
    # ⚠️ 本地路径**只认适配器给的元素**：正文（`session.content`）是用户可控的字符串，
    # 谁都能发一句 `<img src="/etc/passwd"/>`，从正文里认路径就等于给模型开一个本地读文件的口子。
    trusted = _member(session, 'elements')
    if isinstance(trusted, list):
        for element in trusted:
            src = image_src(element)
            if src and not re.match(r'^(?:https?://|data:image/)', _text(src), re.IGNORECASE):
                add(_local_image_path(src), 'file')

    parse = _helper('_parse_mini_xml_elements')
    visit_elements = _helper('_visit_elements')
    if parse is not None and visit_elements is not None:
        def visit(element: Any) -> None:
            if not is_record(element):
                return
            element_type = _text(element.get('type')).lower()
            if element_type in ('img', 'image'):
                attrs = element.get('attrs') if is_record(element.get('attrs')) else {}
                data = element.get('data') if is_record(element.get('data')) else {}
                src = image_src(element)
                if src:
                    add(src)
                else:
                    add(attrs.get('file') or data.get('file'), 'file')

        visit_elements(parse(raw), visit)
    if not sources:
        for match in re.finditer(r'<(?:img|image)\b[^>]*(?:src|url)=["\']([^"\']+)["\'][^>]*>', raw, re.IGNORECASE):
            add(match.group(1))
    for match in re.finditer(r'\[CQ:image,([^\]]+)\]', raw, re.IGNORECASE):
        fields: dict[str, str] = {}
        for part in match.group(1).split(','):
            index = part.find('=')
            if index > 0:
                fields[part[:index].strip().lower()] = part[index + 1:].strip()
        add(fields.get('url') or fields.get('cache_url'), 'adapter-url')
        if not fields.get('url') and not fields.get('cache_url'):
            add(fields.get('file'), 'file')
    return sources


def _format_buffered_user_messages(messages: list[Any]) -> str:
    """`format_buffered_user_messages`：优先 helpers.py 的移植版。"""
    return (_helper('format_buffered_user_messages') or _fallback_format_buffered_user_messages)(messages)


def _extract_session_image_sources(session: Any) -> list[str]:
    """`extract_session_image_sources`：优先 helpers.py 的移植版。"""
    return (_helper('extract_session_image_sources') or _fallback_extract_session_image_sources)(session)


# =========================================================================== #
# 归档窗口 / 音频转码 / PIL 降级
# =========================================================================== #

async def _archive_window_after(service: Any, story_id: str, cursor: int) -> list[Any]:
    """上游 `dbGet(..., { storyId, id: { $gt: cursor } }, { limit: 128, sort: asc })`。

    `Database` 只支持等值 `where`（`base.db_get` 文档串明确要求调用方
    「先取足够行，再在 Python 侧过滤」），因此这里按 id 升序取回该剧本的条目，
    再在 Python 侧裁出 `id > cursor` 的前 128 行。消费侧只使用这 128 行，
    每次最多写入 `batchSize` 条向量 —— 上游「有界查询 + 有界写入」的意图保留。
    """
    rows = await service.db_get(
        'interlude_script_entry', {'storyId': story_id}, {'sort': {'id': 'ASC'}},
    )
    window: list[Any] = []
    for row in rows:
        row_id = pick(row, 'id')
        if not isinstance(row_id, int) or isinstance(row_id, bool) or row_id <= cursor:
            continue
        window.append(row)
        if len(window) >= ARCHIVE_WINDOW_ROWS:
            break
    return window


async def _request_record_transcode(
    service: Any, session: Any, file: str, out_format: str, max_bytes: int,
) -> Optional[dict[str, Any]]:
    """上游 `fetchNativeAudio` 的 `onebot-file:` 分支（SnowLuma 服务端转码）。

    优先走适配器原生 `session.bot.internal._request('get_record', ...)`，
    其次走 `Transport.transcode_record(file, out_format)` 可选钩子；两条路都
    不可用时返回 `None`（等价上游 `typeof internal?._request !== 'function'`）。
    协议错误按上游抛异常，由 `load_native_audio` 的 catch 记「语音读取失败」。
    """
    internal = _member(_member(session, 'bot'), 'internal')
    request = getattr(internal, '_request', None) if internal is not None else None
    hook = getattr(service.transport, 'transcode_record', None) if service.transport is not None else None

    if callable(request):
        async def invoke() -> Any:
            result = request('get_record', {'file': file, 'out_format': out_format})
            return await result if asyncio.iscoroutine(result) else result
    elif callable(hook):
        async def invoke() -> Any:
            result = hook(file, out_format)
            return await result if asyncio.iscoroutine(result) else result
    else:
        return None

    try:
        response = await asyncio.wait_for(invoke(), MEDIA_FETCH_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        raise RuntimeError('get_record timeout after 30s')

    if isinstance(response, dict):
        retcode = response.get('retcode')
        if retcode is not None and _int_value(retcode, 0) != 0:
            raise RuntimeError(_text(response.get('wording') or response.get('message') or ('retcode=%s' % retcode)))
        status = response.get('status')
        if status and status != 'ok':
            raise RuntimeError(_text(response.get('wording') or response.get('message') or status))
        data = response.get('data') if response.get('data') is not None else response
        base64_text = data.get('base64') if isinstance(data, dict) else ''
    elif isinstance(response, str):
        base64_text = response
    else:
        base64_text = ''
    base64_text = re.sub(r'\s+', '', _text(base64_text))
    if not base64_text:
        raise RuntimeError('SnowLuma get_record returned no transcoded payload')
    if _base64_bytes_length(base64_text) > max_bytes:
        return None
    return {'format': out_format, 'base64': base64_text}


async def _fetch_remote_bytes(service: Any, url: str, kind: str) -> Optional[bytes]:
    """下载原始字节：优先 `Transport.fetch_image` / `fetch_audio`，其次 `ctx.http_get`。"""
    method = getattr(service.transport, 'fetch_image' if kind == 'image' else 'fetch_audio', None)
    if callable(method):
        try:
            data = await asyncio.wait_for(method(url), MEDIA_FETCH_TIMEOUT_SECONDS)
        except Exception as error:
            service.report_standalone('debug', '远程附件下载失败 类型=%s 错误=%s', kind, error)
            return None
        if data:
            return bytes(data)
    http_get = getattr(getattr(service, 'ctx', None), 'http_get', None)
    if callable(http_get):
        try:
            data = await asyncio.wait_for(http_get(url), MEDIA_FETCH_TIMEOUT_SECONDS)
        except Exception as error:
            service.report_standalone('debug', '远程附件下载失败 类型=%s 错误=%s', kind, error)
            return None
        return bytes(data) if data else None
    return None


def _pil_downscale(data: bytes, max_dimension: int) -> Optional[bytes]:
    """上游 `downscaleImageForVision` 的 PIL 等价物：EXIF 定向 + 等比缩放 + JPEG q85。

    返回编码后的字节；`PIL` 缺失、解码失败或无法编码时返回 `None`
    （调用方按上游「降采样失败，已透传原图」继续）。
    """
    if _PIL_IMAGE is None:  # pragma: no cover - 取决于环境
        return None
    try:
        with _PIL_IMAGE.open(io.BytesIO(data)) as opened:
            opened.load()
            image = _PIL_IMAGE_OPS.exif_transpose(opened) if _PIL_IMAGE_OPS is not None else opened
            width, height = image.size
            if width <= 0 or height <= 0:
                return None
            longest = max(width, height)
            if longest > max_dimension:
                scale = max_dimension / float(longest)
                image = image.resize(
                    (max(1, int(width * scale)), max(1, int(height * scale))),
                    _PIL_IMAGE.LANCZOS,
                )
            if image.mode != 'RGB':
                image = image.convert('RGB')
            out = io.BytesIO()
            image.save(out, format='JPEG', quality=85)
            return out.getvalue()
    except Exception:
        return None


def _pil_first_frame(data: bytes) -> Optional[bytes]:
    """上游 `renderAnimatedImageFrame` 的 PIL 等价物：取第一帧并编码为 PNG。"""
    if _PIL_IMAGE is None:  # pragma: no cover - 取决于环境
        return None
    try:
        with _PIL_IMAGE.open(io.BytesIO(data)) as opened:
            opened.seek(0)
            image = opened.convert('RGBA')
            width, height = image.size
            if width <= 0 or height <= 0:
                return None
            longest = max(width, height)
            if longest > MAX_ANIMATED_FRAME_DIMENSION:
                scale = MAX_ANIMATED_FRAME_DIMENSION / float(longest)
                image = image.resize(
                    (max(1, int(width * scale)), max(1, int(height * scale))),
                    _PIL_IMAGE.LANCZOS,
                )
            out = io.BytesIO()
            image.save(out, format='PNG')
            return out.getvalue()
    except Exception:
        return None


def _obsolete_result(requeue: bool) -> dict[str, Any]:
    """上游 `{ obsolete: true, requeue, messages: [], commit: undefined, scriptEntry: undefined }`。"""
    return {'obsolete': True, 'requeue': requeue, 'messages': [], 'commit': None, 'script_entry': None}


def _embedder_identity(embedder: Any) -> Any:
    """上游 `this.embedder.identity?.()`：没有身份的 embedder 返回 `None`。"""
    identity = getattr(embedder, 'identity', None)
    if not callable(identity):
        return None
    try:
        return identity()
    except Exception:  # pragma: no cover - 提供者实现异常不应中断回填
        return None


async def _resolve_adapter_image(transport: Any, file: str, bot: Any) -> dict[str, Any]:
    """上游 `bot.getImage(file)`：适配器原生解析，其次 `Transport` 可选钩子。

    两条路都没有时返回 `{}`（等价上游 `!bot?.getImage → undefined`）。
    """
    getter = getattr(bot, 'get_image', None) if bot is not None else None
    if callable(getter):
        result = getter(file)
        if asyncio.iscoroutine(result):
            result = await result
        return _mapping(result)
    hook = getattr(transport, 'resolve_image_file', None) if transport is not None else None
    if callable(hook):
        result = hook(file)
        if asyncio.iscoroutine(result):
            result = await result
        return _mapping(result)
    return {}


# =========================================================================== #
# ServiceChunk3
# =========================================================================== #

class ServiceChunk3(ServiceBase):
    """对应 upstream/src/service.ts 第 2573–3216 行的成员。"""

    # ------------------------------------------------------------------ #
    # 历史向量缓存（上游 2573–2645）
    # ------------------------------------------------------------------ #

    def invalidate_history_vectors(self, story_id: Optional[str] = None) -> None:
        """上游 `invalidateHistoryVectors(storyId?)`（`src/service.ts:2573`）逐字移植。

        源行被删除或涂改时丢弃内存副本；下一次召回只重新加载幸存的行。
        """
        if story_id:
            self.history_vectors.pop(story_id, None)
            self.history_vectors_ready.discard(story_id)
            self.history_vector_loads.pop(story_id, None)
            self.automatic_recall_cache.pop(story_id, None)
            return
        self.history_vectors.clear()
        self.history_vectors_ready.clear()
        self.history_vector_loads.clear()
        self.automatic_recall_cache.clear()

    async def backfill_history_embeddings(self, story_id: str) -> None:
        """上游 `backfillHistoryEmbeddings(storyId)`（`src/service.ts:2590`）逐条移植。

        语义历史召回的后台向量化：**最新条目优先**（实时召回质量先起来），
        整表在后续维护轮次里逐步覆盖。游标与模型身份一起持久化在
        `story.state.extensions.historyBackfill`；模型身份变化时游标归零以便重扫，
        但**不**批量清掉旧向量（没有破坏性重建）。
        """
        embedding_config = _embedding_config(self)
        if not _value(embedding_config, 'enabled', False):
            return
        if not _value(embedding_config, 'semanticHistory', False):
            return
        batch_size = min(20, max(0, _int_value(_value(embedding_config, 'backfillBatchSize', 5), 5)))
        if not batch_size:
            return
        if story_id in self.history_backfills:
            return
        if self.now_ms() < (self.history_backoff.get(story_id) or 0):
            return
        self.history_backfills.add(story_id)
        started = self.now_ms()
        try:
            identity = _embedder_identity(self.embedder)
            story = await self.get_story(story_id)
            state = decode_story_state(pick(story, 'state'))
            extensions = state.get('extensions') if is_record(state.get('extensions')) else {}
            saved = pick(extensions, 'historyBackfill', 'history_backfill')
            saved = saved if is_record(saved) else {}
            saved_cursor = pick(saved, 'cursor')
            cursor = (
                max(0, saved_cursor)
                if pick(saved, 'identity') == identity and _is_safe_integer(saved_cursor)
                else 0
            )
            older = await _archive_window_after(self, story_id, cursor)
            recent = await self.db_get(
                'interlude_script_entry', {'storyId': story_id},
                {'limit': RECENT_WINDOW_ROWS, 'sort': {'id': 'DESC'}},
            )
            attempts = 0
            completed: set[Any] = set()

            def needs_vector(row: Any) -> bool:
                """上游 `needsVector`：可召回类型 + 有内容 + （没向量或模型身份过期）。"""
                if pick(row, 'kind') not in RECALLABLE_ENTRY_KINDS:
                    return False
                if not _text(pick(row, 'content')).strip():
                    return False
                embedding = pick(row, 'embedding')
                if not isinstance(embedding, list) or not embedding:
                    return True
                if not identity:
                    return False
                metadata = pick(row, 'metadata')
                return pick(metadata if is_record(metadata) else {}, 'embeddingIdentity', 'embedding_identity') != identity

            async def fill(row: Any) -> bool:
                """写入一条向量；返回 `False` 表示本轮必须停止（失败/退避/身份变化）。"""
                nonlocal attempts
                attempts += 1
                embedding = await self.embed_text(_text(pick(row, 'content')))
                if not embedding:
                    self.history_backoff[story_id] = self.now_ms() + BACKFILL_FAILURE_COOLDOWN_MS
                    return False
                if _embedder_identity(self.embedder) != identity:
                    return False

                async def task() -> None:
                    fresh_rows = await self.db_get(
                        'interlude_script_entry', {'storyId': story_id, 'id': pick(row, 'id')},
                    )
                    fresh = fresh_rows[0] if fresh_rows else None
                    if not fresh or pick(fresh, 'content') != pick(row, 'content'):
                        return
                    metadata = dict(pick(fresh, 'metadata')) if is_record(pick(fresh, 'metadata')) else {}
                    if identity:
                        metadata['embeddingIdentity'] = identity
                    await self.db_set(
                        'interlude_script_entry', {'storyId': story_id, 'id': pick(row, 'id')},
                        {'embedding': embedding, 'metadata': metadata},
                    )
                    cached = (self.history_vectors.get(story_id) or {}).get(pick(row, 'id'))
                    if isinstance(cached, dict):
                        cached['vector'] = embedding
                        cached['embeddingIdentity'] = identity

                await self.serial(story_id, task)
                completed.add(pick(row, 'id'))
                return True

            # 只有还留着归档名额时才占一个实时名额：batch=1 时仍能沿历史前进。
            live = None
            if batch_size > 1:
                live = next((row for row in recent if needs_vector(row)), None)
            if live is not None and not await fill(live):
                return
            for row in older:
                if needs_vector(row) and pick(row, 'id') not in completed:
                    if attempts >= batch_size or self.now_ms() - started >= BACKFILL_TIME_BUDGET_MS:
                        break
                    if not await fill(row):
                        break
                cursor = pick(row, 'id')
            if not older:
                cursor = 0

            async def persist_cursor() -> None:
                fresh = await self.get_story(story_id)
                fresh_state = decode_story_state(pick(fresh, 'state'))
                fresh_extensions = (
                    dict(pick(fresh_state, 'extensions'))
                    if is_record(pick(fresh_state, 'extensions')) else {}
                )
                fresh_extensions['historyBackfill'] = {'cursor': cursor, 'identity': identity}
                await self.db_set(
                    'interlude_story', {'id': story_id},
                    {'state': encode_story_state({**fresh_state, 'extensions': fresh_extensions})},
                )

            await self.serial(story_id, persist_cursor)
        finally:
            self.history_backfills.discard(story_id)

    # ------------------------------------------------------------------ #
    # 原生音视频（上游 2651–2717）
    # ------------------------------------------------------------------ #

    async def load_native_audio(self, story: Any, sources: list[str], session: Any = None) -> list[Any]:
        """上游 `loadNativeAudio(story, sources, session?)`（`src/service.ts:2651`）逐条移植。

        QQ 语音是 SILK，多模态模型读不了，所以 `onebot-file:` 一律要求服务端
        转码（`out_format`）后回传 base64；本方法只返回瞬时附件，**不落库**。
        """
        config = _audio_config(self)
        if not _value(config, 'enabled', False) or not sources:
            return []
        audio: list[Any] = []
        max_per_message = _int_value(_value(config, 'maxPerMessage', DEFAULT_AUDIO_MAX_PER_MESSAGE),
                                    DEFAULT_AUDIO_MAX_PER_MESSAGE)
        for index, source in enumerate(sources[:max(0, max_per_message)]):
            try:
                item = await self.fetch_native_audio(source, session)
                if item:
                    audio.append({'id': 'turn-audio-%d' % (index + 1), **item})
            except Exception as error:
                self.report('warn', story, 'user-message', '语音读取失败，已保留语音事实 错误=%s', error)
        return audio

    async def fetch_native_audio(self, source: str, session: Any = None) -> Optional[dict[str, Any]]:
        """上游 `fetchNativeAudio(source, session?)`（`src/service.ts:2666`）逐条移植。

        与原生图片获取路径对称：什么都不持久化。
        """
        config = _audio_config(self)
        audio_format = _value(config, 'outFormat', 'mp3') or 'mp3'
        value = _text(source).strip()
        max_bytes = _number_value(_value(config, 'maxFileSizeMB', DEFAULT_AUDIO_MAX_FILE_SIZE_MB),
                                  float(DEFAULT_AUDIO_MAX_FILE_SIZE_MB)) * 1024 * 1024

        if value.startswith('onebot-file:'):
            file = value[len('onebot-file:'):]
            if not file:
                return None
            return await _request_record_transcode(self, session, file, audio_format, int(max_bytes))

        if value.startswith('file-url:'):
            # QQ 音频文件的 CDN 直链（带 rkey，短期有效）：原始字节可直接喂给
            # 模型；格式用文件名扩展 + 魔数双保险，避免把 SILK 误当文件。
            payload = value[len('file-url:'):]
            split = payload.rfind('#')
            url = (payload[:split] if split > 0 else payload).strip()
            fragment = payload[split + 1:] if split > 0 else ''
            name = unquote(re.sub(r':\d+$', '', fragment)) if fragment else ''
            declared_match = re.search(r':(\d+)$', fragment)
            declared_size = _int_value(declared_match.group(1), 0) if declared_match else 0
            if not re.match(r'^https?://', url, re.IGNORECASE):
                return None
            if declared_size > max_bytes:
                return None
            data = await _fetch_remote_bytes(self, url, 'audio')
            if not data or len(data) > max_bytes:
                return None
            guessed = guess_audio_format(data, name)
            if not guessed:
                return None
            return {'format': guessed, 'base64': base64.b64encode(data).decode('ascii')}

        if re.match(r'^data:audio/', value, re.IGNORECASE):
            # 适配器内联音频：只接受模型能读的容器格式。
            match = re.match(r'^data:audio/([a-z0-9]+);base64,([a-z0-9+/=\s]+)$', value, re.IGNORECASE)
            inline_format = (match.group(1) if match else '').lower()
            if not match or inline_format not in ('mp3', 'wav', 'ogg', 'm4a', 'flac', 'amr'):
                return None
            base64_text = re.sub(r'\s+', '', match.group(2))
            if not base64_text or _base64_bytes_length(base64_text) > max_bytes:
                return None
            return {'format': inline_format, 'base64': base64_text}

        # 裸 http(s) 语音 URL 提供的是原始 SILK；没有 file token 就没有服务端
        # 转码路径，因此刻意跳过该附件。
        return None

    # ------------------------------------------------------------------ #
    # 视觉（上游 2719–2894）
    # ------------------------------------------------------------------ #

    def describe_vision_event(self, session: Any) -> dict[str, Any]:
        """上游 `describeVisionEvent(session)`（`src/service.ts:2719`）逐字移植。

        附件本身走原生多模态通道；普通文本里**不留**图片占位符：抓取失败/被过滤
        必须表现为「没有视觉输入」，而不是邀请模型编一个。
        """
        raw = _text(_member(session, 'content'))
        sources = _extract_session_image_sources(session)
        text = normalize_qq_native_face_segments(raw)
        text = re.sub(r'</?(?:img|image|audio|record|file)\b[^>]*>', '', text, flags=re.IGNORECASE)
        text = re.sub(r'\[CQ:(?:image|record|file),[^\]]*\]', '', text, flags=re.IGNORECASE)
        return {'content': text.strip(), 'sources': sources}

    async def load_native_images(self, story: Any, sources: list[str], session: Any = None) -> list[Any]:
        """上游 `loadNativeImages(story, sources, session?)`（`src/service.ts:2733`）逐条移植。"""
        if not _value(_vision_config(self), 'enabled', False) or not sources:
            return []
        images: list[Any] = []
        for index, source in enumerate(sources[:3]):
            try:
                image = await self.fetch_native_image(source, _member(session, 'bot'))
                if image:
                    images.append({'id': 'turn-image-%d' % (index + 1), **image})
            except Exception as error:
                self.report('warn', story, 'user-message', '图片读取失败，已继续处理文字消息 错误=%s', error)
        return images

    async def describe_current_images(
        self, story: Any, images: list[Any], user_message: Optional[str],
    ) -> Optional[list[str]]:
        """上游 `describeCurrentImages(story, images, userMessage)`（`src/service.ts:2749`）。

        侧端识图与原生图片获取镜像对称，但只把它的**事实性结果**塞进文本叙事器的
        当前事件。
        """
        if not images:
            return None
        describer = self.vision_describer
        available = getattr(describer, 'available', None)
        if describer is None or not callable(available) or not available():
            self.report_operation(
                'diagnostic', 'warn', story, 'user-message',
                '侧端识图跳过：没有配置 useForVision 的视觉模型',
            )
            return None
        try:
            observations = await describer.describe_images(
                images, user_message or '', _value(_vision_config(self), 'detail', 'auto') or 'auto',
            )
            if observations:
                self.report_operation(
                    'diagnostic', 'debug', story, 'user-message',
                    '侧端识图完成 图片=%d 观察=%d', len(images), len(observations),
                )
            else:
                self.report_operation(
                    'diagnostic', 'warn', story, 'user-message',
                    '侧端识图未返回内容，已继续处理文字消息',
                )
            return observations
        except Exception as error:
            self.report_operation(
                'diagnostic', 'warn', story, 'user-message',
                '侧端识图失败，已继续处理文字消息 错误=%s', error,
            )
            return None

    async def fetch_native_image(
        self, source: str, bot: Any = None, adapter_provided: bool = False,
    ) -> Optional[dict[str, Any]]:
        """上游 `fetchNativeImage(source, bot?, adapterProvided = false)`
        （`src/service.ts:2769`）逐条移植。

        只有 QQ/OneBot 的 CDN 主机在原生视觉路径里被取回，避免任意用户 URL
        变成内网抓取代理；适配器直接给的 URL（`adapterProvided`）例外。
        """
        value = _text(source).strip()
        if value.startswith('onebot-url:'):
            return await self.fetch_native_image(value[len('onebot-url:'):], bot, True)
        if value.startswith('onebot-file:'):
            file = value[len('onebot-file:'):]
            if not file:
                return None
            info = await _resolve_adapter_image(self.transport, file, bot)
            if not info:
                # 适配器没提供 resolver（本移植版的 AstrBot 传输层就没有）：这条路只在
                # `onebot-file:` 上开放，而它**只能由适配器所有的 element 产生**
                # （见 `_fallback_extract_session_image_sources`），因此直接读本地文件是安全的。
                try:
                    data = await asyncio.to_thread(_read_local_file, _local_image_path(file))
                except Exception:
                    return None
                return await self.image_bytes_to_native(data, guess_image_mime(data))
            candidates = [
                _text(info.get(key)).strip() for key in ('url', 'file', 'path')
            ]
            for candidate in candidates:
                if not candidate:
                    continue
                if re.match(r'^https?://', candidate, re.IGNORECASE):
                    image = await self.fetch_native_image(candidate, None, True)
                    if image:
                        return image
                else:
                    try:
                        data = await asyncio.to_thread(_read_local_file, candidate)
                        image = await self.image_bytes_to_native(
                            data, guess_image_mime(data, info.get('type')),
                        )
                        if image:
                            return image
                    except Exception:
                        # 适配器可能返回非本地别名；继续试下一个字段。
                        continue
            return None
        if re.match(r'^data:image/', value, re.IGNORECASE):
            match = re.match(r'^data:(image/[a-z0-9.+-]+);base64,([a-z0-9+/=\s]+)$', value, re.IGNORECASE)
            if not match:
                return None
            data = _decode_base64(match.group(2))
            if not data or len(data) > MAX_NATIVE_IMAGE_BYTES:
                return None
            return await self.image_bytes_to_native(data, match.group(1).lower())

        parsed = urlsplit(value)
        if parsed.scheme not in ('http', 'https'):
            return None
        if not adapter_provided and not is_trusted_image_host(parsed.hostname or ''):
            return None
        data = await _fetch_remote_bytes(self, value, 'image')
        if not data or len(data) > MAX_NATIVE_IMAGE_BYTES:
            return None
        # `Transport.fetch_image` 只回字节（没有响应头），因此 MIME 一律走魔数嗅探。
        return await self.image_bytes_to_native(data, guess_image_mime(data))

    async def image_bytes_to_native(self, data: bytes, mime_type: str) -> Optional[dict[str, Any]]:
        """上游 `imageBytesToNative(bytes, mimeType)`（`src/service.ts:2817`）逐条移植。

        把适配器/抓取到的字节转成**一个有界**的原生视觉附件。输出键名
        `mime_type` / `data_uri`（`types.NarrativeImage`）：data URI 是发给模型的
        wire format，形状与上游一致。
        """
        normalized = _text(mime_type or guess_image_mime(data)).lower()
        if not normalized.startswith('image/'):
            return None
        encoded = base64.b64encode(data).decode('ascii')
        data_uri = 'data:%s;base64,%s' % (normalized, encoded)
        if is_animated_image_mime(normalized):
            frame = await self.render_animated_image_frame(data_uri)
            if frame:
                return frame
            self.report_standalone(
                'warn', '动态图片未能抽帧，已使用原始图片输入；请安装 Pillow 以提高识别兼容性。',
            )
        scaled = await self.downscale_image_for_vision({'mime_type': normalized, 'data_uri': data_uri})
        return scaled or {'mime_type': normalized, 'data_uri': data_uri}

    async def downscale_image_for_vision(self, image: Any) -> Optional[dict[str, Any]]:
        """上游 `downscaleImageForVision(image)`（`src/service.ts:2834`）。

        上游用 Puppeteer 把静态图按配置上限重渲染（顺带修 EXIF 方向）；本移植版
        用 PIL 做等价的重编码。多模态提供者会把大图切成很多 token，一次有界重编码
        同时省上传时间和每回合 token；只有真能变小的结果才被采纳。
        """
        mime_type = _text(_turn_get(image, 'mimeType', 'mime_type') or '')
        data_uri = _text(_turn_get(image, 'dataUri', 'data_uri') or '')
        max_dimension = _int_value(_value(_vision_config(self), 'maxImageDimension', 0), 0)
        if not max_dimension or not should_downscale_image(mime_type, data_uri):
            return None
        original = _data_uri_payload(data_uri)
        if original is None:
            return None
        if _PIL_IMAGE is None:
            self.report_standalone('debug', '视觉图片降采样不可用：未安装 Pillow，已透传原图')
            return None
        scaled = await asyncio.to_thread(_pil_downscale, original, max_dimension)
        if not scaled or len(scaled) >= len(original):
            self.report_standalone('debug', '视觉图片降采样未取得更小结果，已透传原图')
            return None
        self.report_standalone_operation(
            'diagnostic', 'debug', '视觉图片已降采样 原始=%dB 降采样后=%dB 上限=%dpx',
            len(original), len(scaled), max_dimension,
        )
        return {
            'mime_type': 'image/jpeg',
            'data_uri': 'data:image/jpeg;base64,%s' % base64.b64encode(scaled).decode('ascii'),
        }

    async def render_animated_image_frame(self, data_uri: str) -> Optional[dict[str, Any]]:
        """上游 `renderAnimatedImageFrame(dataUri)`（`src/service.ts:2868`）。

        上游用 Puppeteer 截一帧代表画面；本移植版用 PIL 取第一帧并编码为 PNG。
        PIL 缺失 / 解码失败 / 超出 4MB 都返回 `None`（调用方继续透传原图）。
        """
        original = _data_uri_payload(data_uri)
        if original is None:
            return None
        if _PIL_IMAGE is None:
            self.report_standalone('debug', '动态图片抽帧不可用：未安装 Pillow')
            return None
        frame = await asyncio.to_thread(_pil_first_frame, original)
        if not frame or len(frame) > MAX_NATIVE_IMAGE_BYTES:
            self.report_standalone('debug', '动态图片抽帧失败')
            return None
        return {
            'mime_type': 'image/png',
            'data_uri': 'data:image/png;base64,%s' % base64.b64encode(frame).decode('ascii'),
        }

    # ------------------------------------------------------------------ #
    # 缓冲回合（上游 2898–3138）
    # ------------------------------------------------------------------ #

    def invalidate_buffered_narratives(self, story_id: Optional[str] = None) -> None:
        """上游 `invalidateBufferedNarratives(storyId?)`（`src/service.ts:2898`）逐条移植。

        阻止计时器或已经在返回路上的模型调用在管理员重置剧本 / 清空 HDSI 表之后
        把数据复活。
        """
        if story_id:
            self.compaction_backoff.pop(story_id, None)
        else:
            self.compaction_backoff.clear()
        for key, turn in list(self.buffered_narrative_turns.items()):
            if story_id and _turn_get(turn, 'storyId', 'story_id') != story_id:
                continue
            timer = _turn_get(turn, 'timer')
            if timer:
                timer()
            in_flight = _turn_get(turn, 'inFlightRequestId', 'in_flight_request_id')
            if in_flight:
                obsolete = _turn_get(turn, 'obsoleteRequestIds', 'obsolete_request_ids')
                if not isinstance(obsolete, set):
                    obsolete = set(obsolete or [])
                    _turn_set(turn, 'obsoleteRequestIds', 'obsolete_request_ids', obsolete)
                obsolete.add(in_flight)
            self.buffered_narrative_turns.pop(key, None)
        # 群回合有自己的 debounce 计时器：必须由同一条重置/清理路径取消，否则
        # 一条旧的缓冲群消息会在管理员清空剧本之后再写一条新条目。
        for key, turn in list(self.buffered_group_turns.items()):
            if story_id and _turn_get(turn, 'storyId', 'story_id') != story_id:
                continue
            timer = _turn_get(turn, 'timer')
            if timer:
                timer()
            self.buffered_group_turns.pop(key, None)
        for key in list(self.group_willingness.keys()):
            if not story_id or key.startswith('%s:' % story_id):
                self.group_willingness.pop(key, None)
        for key, wake in list(self.due_intent_wake_timers.items()):
            if story_id and key != story_id:
                continue
            cancel = wake.get('cancel') if isinstance(wake, dict) else getattr(wake, 'cancel', None)
            if callable(cancel):
                cancel()
            self.due_intent_wake_timers.pop(key, None)

    def has_pending_narrative(self, story_id: str) -> bool:
        """上游 `hasPendingNarrative(storyId)`（`src/service.ts:2926`）逐字移植。

        实时或 debounce 中的对话都应优先于后台作业。
        """
        if story_id in self.narrating_stories:
            return True
        for turn in self.buffered_narrative_turns.values():
            if _turn_get(turn, 'storyId', 'story_id') != story_id:
                continue
            if (
                _turn_get(turn, 'messages')
                or _turn_get(turn, 'timer')
                or _turn_get(turn, 'inFlightRequestId', 'in_flight_request_id')
            ):
                return True
        for turn in self.buffered_group_turns.values():
            if _turn_get(turn, 'storyId', 'story_id') != story_id:
                continue
            if _turn_get(turn, 'messages') or _turn_get(turn, 'timer'):
                return True
        return False

    async def flush_buffered_narrative(self, key: str, revision: int) -> None:
        """上游 `flushBufferedNarrative(key, revision)`（`src/service.ts:2937`）逐条移植。

        这是「合并写作回合」的主流程：一次持续写作 + 结构化决策，最后把决策落库
        并投递可见消息。跨 mixin 调用（按上游顺序、位置参数）：

        * `self.try_decide(story, participant, phase, from_, now, user_message,
          due_intents, superseded_intents, group_context, images, audio,
          chat_capabilities, quoted_messages, sticker_catalog, turn_query_embedding,
          visual_observations, on_early_reply)`（Chunk4）
        * `self.persist_decision(current, current_participant, decision, from_, now,
          True, 'user-message', [], early_delivered)`（Chunk4）
        """
        if self.database_resetting or self.desktop_runtime_phase == 'paused':
            return
        turn = self.buffered_narrative_turns.get(key)
        if not turn or _turn_get(turn, 'nextRevision', 'next_revision') != revision:
            return
        story_id = _turn_get(turn, 'storyId', 'story_id')
        participant_id = _turn_get(turn, 'participantId', 'participant_id')
        # 一部共享剧本同时只有一个叙事器。若另一条关系分支正卡在提供者上，
        # 保留本批次并稍后重试，而不是取一个不一致的游标快照。
        if story_id in self.narrating_stories:
            _turn_set(
                turn, 'timer', 'timer',
                self.ctx.set_timeout(lambda: self.flush_buffered_narrative(key, revision), 250),
            )
            return
        self.narrating_stories.add(story_id)
        _turn_set(turn, 'timer', 'timer', None)
        pending = _turn_get(turn, 'messages')
        if not isinstance(pending, list):
            pending = []
            _turn_set(turn, 'messages', 'messages', pending)
        batch = pending[:]
        del pending[:]
        if not batch:
            self.narrating_stories.discard(story_id)
            return
        request_id = revision
        _turn_set(turn, 'inFlightRequestId', 'in_flight_request_id', request_id)
        latest_session = _turn_get(turn, 'latestSession', 'latest_session')
        try:
            # 只在剧本锁内做轻量决策输入的快照。网络请求留在锁外，新用户消息
            # 可以立刻被记录并在必要时作废本次请求。
            async def snapshot_task() -> Optional[dict[str, Any]]:
                story = await self.get_story(story_id)
                participant = await self.get_participant(participant_id)
                if not participant or pick(participant, 'status') != 'active' or pick(story, 'status') != 'active':
                    return None
                now = self.now()
                due = live_narrative_intents(await self.due_intents(pick(story, 'id'), now))
                due = [
                    intent for intent in due
                    if not pick(intent, 'participantId', 'participant_id')
                    or pick(intent, 'participantId', 'participant_id') == pick(participant, 'id')
                ]
                return {
                    'story': story, 'participant': participant,
                    'from': narrative_cursor(story, now), 'now': now, 'due': due,
                }

            snapshot = await self.serial(story_id, snapshot_task)
            if not snapshot:
                return
            user_message = _format_buffered_user_messages(batch)
            trimmed = (user_message or '').strip()
            turn_query_embedding = None
            if trimmed and self.semantic_turn_embedding_enabled():
                max_characters = _int_value(
                    _value(_embedding_config(self), 'maxInputCharacters', 4_000), 4_000,
                )
                turn_query_embedding = await self.embed_text(trimmed[:max_characters])
            quoted_messages: list[Any] = []
            for index, message in enumerate(batch):
                quote = _turn_get(message, 'quote')
                if quote:
                    # `messageIndex` 是发给模型的 wire format（上游 camelCase）。
                    quoted_messages.append({**_mapping(quote), 'messageIndex': index + 1})
            sticker_catalog = await self.sticker_catalog_for_session(latest_session, turn_query_embedding)
            chat_capabilities = self.private_chat_capabilities(latest_session)
            image_sources = _unique([
                source for message in batch
                for source in (_turn_get(message, 'imageSources', 'image_sources') or [])
            ])[:3]
            loaded_images = await self.load_native_images(snapshot['story'], image_sources, latest_session)
            vision_mode = _value(_vision_config(self), 'mode', 'native') or 'native'
            visual_observations = (
                await self.describe_current_images(snapshot['story'], loaded_images, user_message)
                if vision_mode == 'sidecar' else None
            )
            images = loaded_images if vision_mode == 'native' else []
            # 语音走原生音频通道：SnowLuma 服务端逐条转码，主模型以 input_audio 收到。
            audio_sources = _unique([
                source for message in batch
                for source in (_turn_get(message, 'audioSources', 'audio_sources') or [])
            ])
            audio = await self.load_native_audio(snapshot['story'], audio_sources, latest_session)
            # 图片下载期间又来了新消息：把本批次放回去，让更新的 revision 合成一个事件。
            if _turn_get(turn, 'nextRevision', 'next_revision') != revision:
                pending[0:0] = batch
                return
            superseded = [
                intent for message in batch
                for intent in (_turn_get(message, 'supersededIntents', 'superseded_intents') or [])
            ]
            early: dict[str, Any] = {'delivered': False, 'interaction': None, 'delivery_entry': None}

            async def on_early_reply(reply: Any) -> bool:
                """上游 `onEarlyReply`：首条可见回复提前投递成功后不再重发。"""
                if early['delivered']:
                    return False
                delivery_entry = await self.deliver_early_private_reply(
                    snapshot['story'], snapshot['participant'], latest_session, turn, request_id, reply,
                )
                if delivery_entry:
                    early['delivered'] = True
                    early['interaction'] = pick(reply, 'interaction')
                    early['delivery_entry'] = delivery_entry
                return bool(delivery_entry)

            narrative = await self.try_decide(
                snapshot['story'], snapshot['participant'], 'user-message',
                snapshot['from'], snapshot['now'], user_message, snapshot['due'], superseded,
                None, images, audio, chat_capabilities, quoted_messages, sticker_catalog,
                turn_query_embedding, visual_observations, on_early_reply,
            )
            succeeded = bool(pick(narrative, 'succeeded'))
            effective_now = pick(narrative, 'effectiveNow', 'effective_now')
            immediate_observations = pick(narrative, 'immediateObservations', 'immediate_observations') or []
            decision = pick(narrative, 'decision')
            decision = dict(decision) if is_record(decision) else {}
            if early['delivered'] and early['interaction']:
                decision = {**decision, 'interaction': early['interaction']}
            sticker = self.resolve_sticker(pick(decision, 'localMedia', 'local_media'), sticker_catalog)
            native_face = None if sticker else self.resolve_native_face(decision, chat_capabilities)

            async def commit_task() -> dict[str, Any]:
                """上游 `this.serial(turn.storyId, async () => {...})` 的落库段。"""
                if self.database_resetting:
                    return _obsolete_result(False)
                obsolete = _turn_get(turn, 'obsoleteRequestIds', 'obsolete_request_ids')
                if request_id in (obsolete if isinstance(obsolete, (set, list)) else set()):
                    return _obsolete_result(True)
                current = await self.get_story(story_id)
                current_participant = await self.get_participant(participant_id)
                if (
                    not current_participant
                    or pick(current_participant, 'status') != 'active'
                    or pick(current, 'status') != 'active'
                ):
                    return _obsolete_result(False)
                now = self.now()
                # 立即观察只在本请求活过 debounce 作废之后才落库：过期结果不得
                # 污染下一个合并用户回合。
                for observation in immediate_observations:
                    await self.persist_collected_web_observation(observation)
                if early['delivered'] and not succeeded:
                    await self.append_entry(pick(current, 'id'), {
                        'kind': 'stream-finalization-failed', 'actor': 'system',
                        'content': '主角首条消息已经提前投递，但流式叙事未能完成；本轮不会自动重发可见回复。',
                        'occurredAt': iso(now), 'metadata': {'requestId': request_id},
                    }, now, pick(current_participant, 'id'))
                    await self.schedule_stream_script_recovery(
                        pick(current, 'id'), pick(current_participant, 'id'), now,
                    )
                    self.report_operation(
                        'standard', 'warn', current, 'user-message',
                        '流式叙事在首条回复后未完成，已保留投递且停止自动重试 参与者=%s 请求=%d',
                        pick(current_participant, 'id'), request_id,
                    )
                    return {'obsolete': False, 'requeue': False, 'messages': [], 'commit': None, 'script_entry': None}
                interaction = pick(decision, 'interaction')
                reply = pick(interaction, 'reply') if is_record(interaction) else None
                reply_content = pick(reply, 'content') if is_record(reply) else None
                commits_first_reply = (
                    succeeded
                    and is_record(reply) and pick(reply, 'mode') == 'immediate'
                    and isinstance(reply_content, str) and bool(reply_content.strip())
                )
                if commits_first_reply:
                    _turn_set(
                        turn, 'firstMessageCommittedRequestId',
                        'first_message_committed_request_id', request_id,
                    )
                # `localMedia` / `nativeFace` 是模型的 camelCase wire 键名。
                persisted = await self.persist_decision(current, current_participant, {
                    **decision,
                    'localMedia': pick(decision, 'localMedia', 'local_media') if sticker else None,
                    'nativeFace': pick(decision, 'nativeFace', 'native_face') if native_face else None,
                }, snapshot['from'], effective_now, True, 'user-message', [], early['delivered'])
                commit = pick(persisted, 'commit')
                script_entry = pick(persisted, 'scriptEntry', 'script_entry')
                if early['delivery_entry'] and commit and reply_content:
                    event = find_outgoing_script_event(
                        commit, pick(current_participant, 'id'), 'immediate', reply_content,
                        _value(_runtime_config(self), 'messageSeparator'),
                    )
                    script_event = (
                        message_event_reference(event, 0, pick(script_entry, 'id')) if event else None
                    )
                    if script_event:
                        metadata = dict(pick(early['delivery_entry'], 'metadata')) if is_record(
                            pick(early['delivery_entry'], 'metadata')) else {}
                        metadata.update(script_event)
                        metadata['earlyStreamingDelivery'] = True
                        await self.db_set(
                            'interlude_script_entry', {'id': pick(early['delivery_entry'], 'id')},
                            {'metadata': metadata},
                        )
                        await self.update_script_delivery_outcome(
                            pick(current, 'id'), script_event, 'delivered', now,
                        )
                if succeeded:
                    await self.db_set(
                        'interlude_story', {'id': pick(current, 'id')},
                        {'cursorAt': effective_now, 'updatedAt': now},
                    )
                    # 上游是 `{ id: { $in: consumedDueIds } }`；本移植版无算子支持，
                    # 拆成逐 id 更新（见模块文档串「互操作」第 3 条）。
                    for consumed_id in consumed_live_intent_ids(snapshot['due']):
                        await self.db_set(
                            'interlude_intent', {'id': consumed_id},
                            {'status': 'completed', 'updatedAt': now},
                        )
                else:
                    await self.schedule_narrative_retry(
                        pick(current, 'id'), pick(current_participant, 'id'), now,
                    )
                if succeeded:
                    await self.schedule_conversation_follow_ups_after_turn(
                        pick(current, 'id'), effective_now, pick(decision, 'interaction'),
                        pick(current_participant, 'id'),
                    )
                self.report_operation(
                    'diagnostic', 'debug', current, 'user-message',
                    '写作回合统计 参与者=%s 合并消息=%d 成功=%s 可见消息=%d',
                    pick(current_participant, 'id'), len(batch), succeeded,
                    len(pick(persisted, 'messages') or []),
                )
                return {
                    'obsolete': False, 'requeue': False,
                    'messages': pick(persisted, 'messages') or [],
                    'commit': commit, 'script_entry': script_entry,
                }

            result = await self.serial(story_id, commit_task)
            if result['obsolete']:
                if result['requeue']:
                    pending[0:0] = batch
                self.report_operation(
                    'standard', 'info', snapshot['story'], 'user-message',
                    '已丢弃过期主模型结果 参与者=%s 请求=%d', pick(snapshot['participant'], 'id'), request_id,
                )
                return
            if self.can_handle_participant(snapshot['participant']):
                delivered = await self.send_outgoing_messages(
                    snapshot['story'], result['messages'], snapshot['participant'], latest_session,
                )
                await self.confirm_outgoing_deliveries(snapshot['story'], delivered)
                channel_id = pick(snapshot['participant'], 'channelId', 'channel_id')
                if sticker and latest_session:
                    await self.send_sticker(
                        snapshot['story'], latest_session, channel_id, sticker, None,
                        platform_action_reference(
                            result['commit'], pick(result['script_entry'], 'id'),
                            'local-media', pick(sticker, 'assetId', 'asset_id'),
                        ),
                    )
                if native_face and latest_session:
                    await self.send_native_face(
                        snapshot['story'], latest_session, channel_id, native_face, None,
                        platform_action_reference(
                            result['commit'], pick(result['script_entry'], 'id'),
                            'native-face', native_face,
                        ),
                    )
            self.schedule_compaction(story_id)
        except Exception as error:
            self.report_standalone('warn', '合并写作任务失败：参与者=%s 错误=%s', participant_id, error)
        finally:
            if _turn_get(turn, 'inFlightRequestId', 'in_flight_request_id') == request_id:
                _turn_set(turn, 'inFlightRequestId', 'in_flight_request_id', None)
                _turn_set(
                    turn, 'firstMessageCommittedRequestId',
                    'first_message_committed_request_id', None,
                )
                self.narrating_stories.discard(story_id)
            obsolete_now = _turn_get(turn, 'obsoleteRequestIds', 'obsolete_request_ids')
            if isinstance(obsolete_now, set):
                obsolete_now.discard(request_id)
            if not (
                _turn_get(turn, 'messages')
                or _turn_get(turn, 'timer')
                or _turn_get(turn, 'inFlightRequestId', 'in_flight_request_id')
            ):
                self.buffered_narrative_turns.pop(key, None)

    # ------------------------------------------------------------------ #
    # 推进 / 投递 / 压缩入口（上游 3140–3186）
    # ------------------------------------------------------------------ #

    async def advance_story(self, story: Any, force: bool = True) -> list[Any]:
        """上游 `advanceStory(story, force = true)`（`src/service.ts:3140`）逐字移植。"""
        if self.desktop_runtime_phase == 'paused':
            return []
        if not self.can_handle_story(story):
            return []

        async def task() -> list[Any]:
            return await self.advance_unlocked(await self.get_story(pick(story, 'id')), self.now(), force)

        messages = await self.serial(pick(story, 'id'), task)
        if force or messages:
            self.report_operation(
                'summary', 'info', story, 'advance', '剧本推进完成 可见消息=%d', len(messages),
            )
        self.schedule_compaction(pick(story, 'id'))
        return messages

    async def deliver_messages(self, story: Any, messages: list[Any], session: Any = None) -> list[Any]:
        """上游 `deliverMessages(story, messages, session?)`（`src/service.ts:3150`）逐字移植。

        命令与测试用它安全地投递一组「按账号定向」的混合动作。
        """
        participant = await self.find_participant(session, story) if session else None
        delivered = await self.send_outgoing_messages(story, messages, participant, session)
        await self.confirm_outgoing_deliveries(story, delivered)
        return delivered

    async def compact_story(self, story: Any, force: bool = True) -> bool:
        """上游 `compactStory(story, force = true)`（`src/service.ts:3157`）逐字移植。"""
        if self.desktop_runtime_phase == 'paused':
            return False
        if not self.can_handle_story(story):
            return False

        async def task() -> bool:
            return await self.compact_unlocked(await self.get_story(pick(story, 'id')), self.now(), force)

        return await self.serial(pick(story, 'id'), task)

    async def compact_overlay(self, story: Any) -> bool:
        """上游 `compactOverlay(story)`（`src/service.ts:3165`）逐字移植。

        只合并压缩已经生效的 overlay 补丁，不跑完整的场景/事实压缩；手工维护安全。
        """
        if not self.can_handle_story(story):
            return False

        async def task() -> bool:
            return await self.compact_overlay_unlocked(await self.get_story(pick(story, 'id')), self.now())

        return await self.serial(pick(story, 'id'), task)

    async def admin_overlay_status(self, story_id: str) -> dict[str, Any]:
        """上游 `adminOverlayStatus(storyId)`（`src/service.ts:3171`）逐字移植。

        Console 命令用的管理侧 overlay 只读视图。
        """
        story, patches, snapshots, participants = await asyncio.gather(
            self.get_story(story_id),
            self.db_get('interlude_state_patch', {'storyId': story_id}, {'sort': {'createdAt': 'DESC'}}),
            self.db_get(
                'interlude_overlay_snapshot', {'storyId': story_id, 'status': 'active'},
                {'sort': {'periodEnd': 'DESC'}},
            ),
            self.participants(story_id, True),
        )
        state = pick(story, 'state')
        return {
            'state': pick(state if is_record(state) else {}, 'settingOverlay', 'setting_overlay') or {},
            'proposed': [patch for patch in patches if pick(patch, 'status') == 'proposed'],
            'applied': [
                patch for patch in patches if pick(patch, 'status') in ('applied', 'compacted')
            ],
            'cleared': [patch for patch in patches if pick(patch, 'status') == 'cleared'],
            'snapshots': snapshots,
            'participantOverlays': [
                participant for participant in participants
                if normalize_participant_state(pick(participant, 'state')).get('relationshipOverlay')
            ],
        }

    async def sweep(self) -> None:
        """上游 `sweep()`（`src/service.ts:3188`）逐字移植：后台扫描主循环。"""
        if (
            self.desktop_runtime_phase == 'paused'
            or self.database_resetting
            or self.sweep_running
        ):
            return
        self.sweep_running = True
        started_at = self.now_ms()
        try:
            story = await self.get_canonical_story()
            if not story or not self.can_handle_story(story):
                self.report_standalone_operation('diagnostic', 'debug', '后台扫描跳过：没有可处理的活动主剧本')
                return
            if self.has_pending_narrative(pick(story, 'id')):
                # 分段消息是已经决策好的「正在输入」碎片：别的分支在等提供者时它
                # 也可以开始传输，但它自己参与者的新消息仍然优先打断它。
                pending_due = await self.due_intents(pick(story, 'id'), self.now())
                if any(pick(intent, 'type') == 'split-message' for intent in pending_due):
                    await self.deliver_due_split_segments(pick(story, 'id'))
                self.report_operation(
                    'diagnostic', 'debug', story, 'advance',
                    '后台写作跳过：前台回合处理中；已独立检查到期分段投递',
                )
                return
            timezone = pick(pick(story, 'setting'), 'timezone') or ''
            state = pick(story, 'state')
            automation = pick(state if is_record(state) else {}, 'automation')
            self.report_operation(
                'diagnostic', 'debug', story, 'advance', '后台扫描开始 游标=%s 下次自动推进=%s',
                format_log_time(parse_dt(pick(story, 'cursorAt')), timezone),
                format_log_time(
                    parse_dt(pick(automation if is_record(automation) else {}, 'nextAdvanceAt', 'next_advance_at')),
                    timezone,
                ),
            )
            messages = await self.advance_story(story, False)
            delivered = await self.send_scheduled_messages(story, messages) if messages else []
            self.report_operation(
                'diagnostic', 'debug', story, 'advance', '后台扫描完成 耗时=%dms 已投递=%d',
                self.now_ms() - started_at, len(delivered),
            )
        finally:
            self.sweep_running = False
