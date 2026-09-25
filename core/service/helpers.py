"""上游 `src/service.ts` 第 7084–8524 行的移植：全部模块级纯函数。

本模块是 `docs/PORT_PLAN_SERVICE.md` 分解契约里的 `helpers.py`：只含**模块级函数**，
不含 `InterludeService` 的任何成员。`upstream/src/service.ts` 该范围内出现、但实际
定义在别处的辅助函数（`isRecord` / `clip` / `clampNumber` / `toDate` / `cosineSimilarity`
等）按上游同样归属本模块。

## 键名法（`docs/PORT_PLAN.md` §2「⚠️ 键名法」）

| 位置 | 键名 | 理由 |
| --- | --- | --- |
| 读入外部输入（模型输出、数据库行、Koishi 侧数据） | **camelCase 与 snake_case 双读，camelCase 优先** | 模型只按上游提示词吐 camelCase |
| 发给模型 / 桌面的 payload 与 wire format（`UserReportedTime` / `TimelinePlan` / `normalize_database_row` 的行、`normalize_timeline_plan` 输出） | **逐字保持上游 camelCase** | 改了键名模型就按错误键名输出，协议直接断 |
| 本移植版内部中间变量 | snake_case | Python 规范 |

## JS 语义逐字对齐（本模块最容易出错的地方）

* `String(x ?? '')` → `_str()`；`Number(x)` / `Number.isFinite` / `Number.isSafeInteger`
  → `_number()` / `_finite()` / `_is_safe_integer()`；`Math.floor` → `math.floor`。
* `Math.round` 用 `math.floor(x + 0.5)`（JS 的 half-up 朝 +∞），不要用 Python `round()`（银行家舍入）。
* `Number.isInteger` 在 JS 里对 `2.0` 为真 → `_is_integer()` 显式做整数值判定。
* **`String.prototype.matchAll` 的前瞻 / 后顾不吃字符**：`match.index` 与 `match[0]` 对齐，
  但下一次搜索从上一次**实际消耗长度**的下一个位置继续。Python `re.finditer` 的
  `m.end()` 已正确反映「未消耗的 lookbehind」语义，故取 `m.start()` 与
  `text[m.start():m.end()]` 即可与 JS 完全一致——`_clocks_in` 刻意依赖这一点。
* JS 正则 ``\\d``/``\\w`` 只匹配 ASCII → Python 一律用 `re.ASCII` 或显式 `[0-9]`/`[A-Za-z0-9_]`。

不含任何 `astrbot` 依赖：Session 一律走 `plugin/core/service/session.py` 的视图协议，
平台能力走 `Transport` 抽象（见 `docs/PORT_PLAN_SERVICE.md` §6–7）。
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any
from urllib.parse import quote as _urlquote

from .. import database as _database
from ..qq_face import normalize_qq_native_face_segments
from ..story_state import decode_story_state as _decode_story_state
from ..time import (
    calendar_day_key,
    dt_ms,
    iso,
    local_clock_minutes,
    parse_dt,
    story_local_time_context,
    utc_now,
)
from .config import (
    SessionFileFact,
    resolve_black_box_config,
    resolve_blind_mode_config,
)

__all__ = [
    # ---- 会话元素抽取 ----
    'extract_session_voice_count',
    'extract_session_audio_sources',
    'extract_session_file_facts',
    'describe_group_attachments',
    'guess_audio_format',
    # ---- 表情 / 表态 ----
    'calibrated_native_face_willingness',
    'stable_sticker_asset_id',
    'normalize_allowed_reactions',
    # ---- 用户自报时间 / 引用消息 ----
    'extract_user_reported_times',
    'describe_quoted_message',
    'normalize_quoted_message_content',
    # ---- 时间线计划 ----
    'normalize_timeline_plan',
    'describe_timeline_plan_rejection',
    'timeline_entry_prompt_projection',
    # ---- 群聊 ----
    'normalize_group_chat_actions',
    'format_group_speaker',
    'normalize_group_visible_reply',
    'visible_reply_mode',
    # ---- 决策结构判定 ----
    'has_required_narrative_script',
    'resolve_blind_mode_config',
    'resolve_black_box_config',
    # ---- 配置段归一化 ----
    'resolve_audio_config',
    'resolve_sticker_config',
    'AUDIO_OUT_FORMATS',
    'normalize_scene_presence_drafts',
    'normalize_interaction',
    'group_due_intents',
    'should_supersede_narrative_request',
    # ---- 数据库行 / 检索 ----
    'normalize_database_row',
    'database_date_fields',
    'history_lexical_score',
    'rank_sticker_catalog',
    'should_downscale_image',
    'detect_live_script_time_overflow',
    # ---- 常量 ----
    'SEMANTIC_STICKER_LIMIT',
    'AUDIO_FILE_EXTENSIONS',
    'CHAT_REACTION_NAMES',
    'QQ_REACTION_IDS',
    'NATIVE_FACE_SEMANTICS',
    'QQ_NATIVE_FACE_IDS',
    'TIMELINE_KIND_ALIASES',
    # ---- 内部共用小工具（其它 mixin / 模块也直接复用） ----
    'is_record',
    'clip',
    'clamp_number',
    'to_date',
    'normalize_fact',
    'normalize_participant_state',
    'limit_entries_by_characters',
    'cosine_similarity',
]


# =========================================================================== #
# 通用 JS 语义助手
# =========================================================================== #


def is_record(value: Any) -> bool:
    """上游 `isRecord`：非 None 的对象且不是数组。

    注意：JS 的 `!!{}` 为 **true**，空对象也是 record（`{}` 与 `null` 语义不同），
    所以这里不能用 `bool(value)` —— 那会把空字典判成非 record。
    """
    return value is not None and isinstance(value, dict)


def _str(value: Any) -> str:
    """上游 `String(value ?? '')`。"""
    if value is None:
        return ''
    return str(value)


def _number(value: Any) -> float:
    """上游 `Number(value)`（None → NaN，布尔 → 0/1，失败 → NaN）。"""
    if value is None:
        return float('nan')
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return float('nan')


def _finite(value: float) -> bool:
    """上游 `Number.isFinite`。"""
    return value == value and value not in (float('inf'), float('-inf'))


def _is_integer(value: Any) -> bool:
    """上游 `Number.isInteger`（JS 里 `2.0` 为真）。"""
    number = _number(value)
    return _finite(number) and float(number).is_integer()


def _is_safe_integer(value: Any) -> bool:
    """上游 `Number.isSafeInteger`。"""
    number = _number(value)
    return _finite(number) and float(number).is_integer() and abs(number) <= 2 ** 53 - 1


def _js_round(value: float) -> int:
    """JS `Math.round`：半数朝 +∞（Python `round()` 是银行家舍入，不可用）。"""
    return int(math.floor(value + 0.5))


def _js_round(value: float) -> int:
    """JS `Math.round`：半数朝 +∞（Python `round()` 是银行家舍入，不可用）。"""
    return int(math.floor(value + 0.5))


def config_get(config: Any, snake: str, camel: str | None = None) -> Any:
    """读配置字段：优先本移植版的 snake_case 键，其次上游 Console 的 camelCase 键。

    配置层裁决：`plugin/core/service/config.py` 的配置一律 snake_case，
    `normalize_config()` 会把上游 camelCase 归一。这个助手让尚未过归一
    （例如测试直接手写的 runtime 对象、或旧版缓存）的调用方也能读到值。
    """
    if not isinstance(config, dict):
        return None
    if snake in config:
        return config[snake]
    return config.get(camel) if camel else None


def _number_text(value: Any) -> str:
    """上游把数字插进模板字符串时的形态（`String(n)`）。

    JS 的 `String(3492875)` 是 `"3492875"`——整数值不带 `.0`；Python 的
    `str(3492875.0)` 会多出 `.0`，那会让缓存键与上游不一致。
    """
    number = _number(value)
    if not _finite(number):
        return 'NaN'
    return str(int(number)) if float(number).is_integer() else str(number)


def clip(value: Any, length: int) -> str:
    """上游 `clip`：只处理字符串，先 trim 再截断。"""
    return value.strip()[:length] if isinstance(value, str) else ''


def clamp_number(value: Any, fallback: float, minimum: float, maximum: float) -> float:
    """上游 `clampNumber`：非数字 / NaN 一律回落 fallback。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return fallback
    number = float(value)
    if number != number:
        return fallback
    return max(minimum, min(maximum, number))


# =========================================================================== #
# 配置段归一化（上游 `get audioConfig()` `:2247` / `get stickerConfig()` `:2260`）
# =========================================================================== #

#: 上游 `const formats = ['mp3', 'wav', 'ogg', 'm4a', 'flac', 'amr']`（`:2250`）。
AUDIO_OUT_FORMATS = ('mp3', 'wav', 'ogg', 'm4a', 'flac', 'amr')


def _config_number_or(config: Any, camel: str, snake: str, fallback: float) -> float:
    """上游 `Number(x) || fallback`：NaN / 0 / 空串 / 非法值都回落 fallback。"""
    value = _number(config_get(config, snake, camel))
    if value != value or not value:
        return float(fallback)
    return float(value)


def _config_int_or(
    config: Any, camel: str, snake: str, fallback: float, minimum: int, maximum: int,
) -> int:
    """上游 `Math.max(min, Math.min(max, Math.floor(Number(x) || fallback)))`。"""
    value = _config_number_or(config, camel, snake, fallback)
    if value == float('inf'):
        return maximum
    if value == float('-inf'):
        return minimum
    return max(minimum, min(maximum, int(math.floor(value))))


def resolve_audio_config(value: Any = None) -> dict[str, Any]:
    """上游 `get audioConfig()`（`src/service.ts:2247`）。

    读的是 **`config.model.audio`**（Console 的「模型中心 → 原生音频理解」），
    不是顶层 `config.audio`——读错段位会让原生音频在静默中永远关闭。
    输出 snake_case（`out_format` / `max_file_size_mb` / `max_per_message`），
    与 `config.py` 的 `CONFIG_DEFAULTS['model']['audio']` 一致；读取侧双读
    上游 camelCase。
    """
    configured = value if isinstance(value, dict) else {}
    out_format = config_get(configured, 'out_format', 'outFormat')
    return {
        'enabled': config_get(configured, 'enabled') is True,
        'out_format': out_format if out_format in AUDIO_OUT_FORMATS else 'mp3',
        'max_file_size_mb': _config_int_or(
            configured, 'maxFileSizeMB', 'max_file_size_mb', 10, 1, 25,
        ),
        'max_per_message': _config_int_or(
            configured, 'maxPerMessage', 'max_per_message', 1, 1, 3,
        ),
    }


def resolve_sticker_config(value: Any = None) -> dict[str, Any]:
    """上游 `get stickerConfig()`（`src/service.ts:2260`）。

    读的是**顶层** `config.stickers`（Console 的「表情包库」分组，不是
    `model.stickers`——`upstream/src/index.ts:425-431` 的 `Stickers` schema 就挂在
    配置根上）。输出 snake_case，与 `CONFIG_DEFAULTS['stickers']` 一致。
    """
    configured = value if isinstance(value, dict) else {}
    directory = config_get(configured, 'directory')
    return {
        'enabled': config_get(configured, 'enabled') is True,
        'directory': str(directory if directory else 'data/hds-interlude/stickers').strip(),
        'max_file_size_mb': max(1.0, min(30.0, _config_number_or(
            configured, 'maxFileSizeMB', 'max_file_size_mb', 10,
        ))),
        'catalog_limit': _config_int_or(
            configured, 'catalogLimit', 'catalog_limit', 40, 1, 80,
        ),
        'description_max_tokens': _config_int_or(
            configured, 'descriptionMaxTokens', 'description_max_tokens', 768, 256, 4096,
        ),
        'description_response_format': (
            'prompt-only'
            if config_get(
                configured, 'description_response_format', 'descriptionResponseFormat',
            ) == 'prompt-only'
            else 'json-object'
        ),
    }


def to_date(value: Any) -> datetime | None:
    """上游 `toDate`：接受 Date / 字符串 / 数字（毫秒），非法返回 `None`。

    JS `new Date(true)` / `new Date(null)` 语义混乱且上游已用类型判断排除，
    这里显式拒绝布尔，保持行为可预期。
    """
    if isinstance(value, datetime):
        return value
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return parse_dt(value)
    if isinstance(value, str):
        return parse_dt(value)
    return None


def normalize_fact(value: str) -> str:
    """上游 `normalizeFact`：小写化并压缩空白（用作去重指纹）。"""
    return re.sub(r'\s+', ' ', value.strip().lower())


# =========================================================================== #
# 会话元素抽取（上游 7084–7240）
# =========================================================================== #

#: 上游 `AUDIO_FILE_EXTENSIONS`。
AUDIO_FILE_EXTENSIONS = re.compile(r'\.(mp3|wav|ogg|m4a|flac|amr|aac|wma)$', re.IGNORECASE)

#: 上游 `<audio|record ... file|src|url=...>` 回退正则。
_AUDIO_TAG_RE = re.compile(
    r'<(?:audio|record)\b[^>]*(?:file|src|url)=["\']([^"\']+)["\'][^>]*>', re.IGNORECASE,
)
#: 上游 `\[CQ:record,([^\]]+)\]`。
_CQ_RECORD_RE = re.compile(r'\[CQ:record,([^\]]+)\]', re.IGNORECASE)
#: 上游 `\[CQ:record,[^\]]*\]`（只数个数）。
_CQ_RECORD_COUNT_RE = re.compile(r'\[CQ:record,[^\]]*\]', re.IGNORECASE)
#: 上游 `<file\b([^>]*)\/?>`。
_FILE_TAG_RE = re.compile(r'<file\b([^>]*)/?>', re.IGNORECASE)


def _parse_mini_xml_elements(raw: str) -> list[dict[str, Any]]:
    """上游 `h.parse(raw)` 的等价物：把 Koishi 消息链文本解析成元素列表。

    上游用 `h.parse` 拿到结构化元素（`type` / `attrs` / `children`）。本移植版
    不依赖 Koishi，改为解析 `<tag attr="v">…</tag>` 与自闭合 `<tag …/>` 的最小
    子集：只需要 `type` 与 `attrs`，足以覆盖上游对音频 / 文件的全部读取路径。
    解析失败（非法标签）返回已解析到的部分，与上游 `try { } catch {}` 同义。
    """
    elements: list[dict[str, Any]] = []
    stack: list[dict[str, Any]] = [{'children': elements}]
    position = 0
    length = len(raw)
    try:
        while position < length:
            lt = raw.find('<', position)
            if lt < 0:
                break
            gt = raw.find('>', lt)
            if gt < 0:
                break
            body = raw[lt + 1:gt]
            position = gt + 1
            if not body:
                continue
            if body.startswith('/'):
                if len(stack) > 1:
                    stack.pop()
                continue
            self_closing = body.endswith('/')
            if self_closing:
                body = body[:-1]
            parts = body.split(None, 1)
            tag = parts[0].lower() if parts else ''
            if not re.fullmatch(r'[a-z][a-z0-9_-]*', tag):
                continue
            attrs: dict[str, str] = {}
            if len(parts) > 1:
                for match in re.finditer(r'([A-Za-z_:][-\w:.]*)\s*=\s*("([^"]*)"|\'([^\']*)\'|([^\s"\'>]+))', parts[1]):
                    value = match.group(3)
                    if value is None:
                        value = match.group(4)
                    if value is None:
                        value = match.group(5) or ''
                    attrs[match.group(1).lower()] = (
                        value.replace('&quot;', '"').replace('&apos;', "'")
                        .replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&')
                    )
            element: dict[str, Any] = {'type': tag, 'attrs': attrs, 'children': []}
            stack[-1]['children'].append(element)
            if not self_closing:
                stack.append(element)
    except Exception:  # pragma: no cover - 与上游 try/catch 同义的防御分支。
        return elements
    return elements


def _visit_elements(elements: list[dict[str, Any]], visit: Any) -> None:
    """上游 `for (const element of h.parse(raw)) visit(element)` 的深度优先遍历。"""
    for element in elements:
        visit(element)
        children = element.get('children') if isinstance(element, dict) else None
        if isinstance(children, list):
            _visit_elements(children, visit)


def extract_session_voice_count(session: Any) -> int:
    """上游 `extractSessionVoiceCount`：数录音 / 语音段，保留二进制载荷之外的信息。

    同时覆盖 Koishi 元素与原始 OneBot CQ 回退，且**不保留**语音二进制。
    """
    raw = _str(session.get('content') if isinstance(session, dict) else getattr(session, 'content', None))
    count = 0

    def visit(element: Any) -> None:
        nonlocal count
        if not element:
            return
        element_type = _str(element.get('type') if isinstance(element, dict) else None).lower()
        if element_type == 'audio' or element_type == 'record':
            count += 1
        children = element.get('children') if isinstance(element, dict) else None
        if isinstance(children, list):
            for child in children:
                visit(child)

    _visit_elements(_parse_mini_xml_elements(raw), visit)
    if count:
        return count
    return len(_CQ_RECORD_COUNT_RE.findall(raw))


def extract_session_audio_sources(session: Any) -> list[str]:
    """上游 `extractSessionAudioSources`：抽出原生音频通道可取回的语音 / 音频 token。

    与图片不同，语音记录优先用 OneBot 的 file token：原始语音 URL 提供的是 SILK，
    只有 SnowLuma 的服务端转码（`get_record out_format`）才能变成模型可读的载荷。
    """
    raw = _str(session.get('content') if isinstance(session, dict) else getattr(session, 'content', None))
    sources: list[str] = []

    def add(value: Any, kind: str = 'file') -> None:
        source = _str(value).strip()
        if not source or source in sources or len(source) > 512:
            return
        if re.match(r'^data:audio/', source, re.IGNORECASE):
            sources.append(source)
            return
        # http(s) 语音 URL 提供的是原始 SILK，没有 OneBot file token 无法转码，
        # 因此它们不进原生音频通道。
        if re.match(r'^https?://', source, re.IGNORECASE):
            return
        if kind == 'file':
            sources.append('onebot-file:%s' % source)

    def visit(element: Any) -> None:
        if not element:
            return
        element_type = _str(element.get('type') if isinstance(element, dict) else None).lower()
        if element_type == 'audio' or element_type == 'record':
            attrs = dict(element.get('attrs') or {})
            attrs.update(element.get('data') or {})
            has_file = bool(attrs.get('file'))
            add(attrs.get('file') if has_file else (attrs.get('url') or attrs.get('src')),
                'file' if has_file else 'url')
        children = element.get('children') if isinstance(element, dict) else None
        if isinstance(children, list):
            for child in children:
                visit(child)

    # 只解析这条消息的原始内容；`Session.elements` 归适配器所有，可能被别的
    # 中间件跨回合复用。
    _visit_elements(_parse_mini_xml_elements(raw), visit)
    if not sources:
        for match in _AUDIO_TAG_RE.finditer(raw):
            add(match.group(1))
    # OneBot 可能只留下一个只带 file token 的 CQ record 段（典型的 NapCat /
    # SnowLuma 私聊语音），或者带一个我们无法转码的额外 url 字段。
    for match in _CQ_RECORD_RE.finditer(raw):
        fields: dict[str, str] = {}
        for part in match.group(1).split(','):
            index = part.find('=')
            if index > 0:
                fields[part[:index].strip().lower()] = part[index + 1:].strip()
        add(fields.get('file'), 'file')
    # QQ 音频文件走 `<file>` 元素（CDN 直链 + 文件名扩展），原始字节可直接
    # 作为 input_audio；与语音的 SILK 转码路径不同，标记 file-url 前缀。
    for fact in extract_session_file_facts(session):
        if not fact.get('audio') or not re.match(r'^https?://', _str(fact.get('url')), re.IGNORECASE):
            continue
        encoded = 'file-url:%s#%s:%s' % (
            fact.get('url'), _urlquote(_str(fact.get('name')), safe=''), _number_text(fact.get('size')),
        )
        if encoded not in sources:
            sources.append(encoded)
    return sources


def extract_session_file_facts(session: Any) -> list[SessionFileFact]:
    """上游 `extractSessionFileFacts`：入站 `<file>` 元素的 URL / 显示名 / 体积。

    它们是附件事实：原始标记绝不能作为文本进模型，而音频命名的文件喂给原生音频通道。
    """
    raw = _str(session.get('content') if isinstance(session, dict) else getattr(session, 'content', None))
    facts: list[SessionFileFact] = []

    def push(url: str, name: str, size: float) -> None:
        if (not url and not name) or len(facts) >= 3:
            return
        if any(item.get('url') == url and item.get('name') == name for item in facts):
            return
        facts.append({
            'name': name[:200], 'url': url[:1_000], 'size': size,
            'audio': bool(AUDIO_FILE_EXTENSIONS.search(name)),
        })

    def visit(element: Any) -> None:
        if not element:
            return
        element_type = _str(element.get('type') if isinstance(element, dict) else None).lower()
        if element_type == 'file':
            attrs = dict(element.get('attrs') or {})
            attrs.update(element.get('data') or {})
            push(
                _str(attrs.get('src') or attrs.get('url')).strip(),
                _str(attrs.get('name') or attrs.get('file') or attrs.get('title')).strip(),
                _number(attrs.get('size') if attrs.get('size') is not None else attrs.get('file-size')) or 0.0,
            )
        children = element.get('children') if isinstance(element, dict) else None
        if isinstance(children, list):
            for child in children:
                visit(child)

    _visit_elements(_parse_mini_xml_elements(raw), visit)
    if not facts:
        for match in _FILE_TAG_RE.finditer(raw):
            attrs = match.group(1)

            def pick(key: str) -> str:
                found = re.search(r'%s=["\']([^"\']*)["\']' % key, attrs, re.IGNORECASE)
                return found.group(1).strip() if found else ''

            push(
                pick('src') or pick('url'),
                pick('name') or pick('file') or pick('title'),
                _number(pick('size') or pick('file-size')) or 0.0,
            )
    return facts


def describe_group_attachments(content: Any) -> str:
    """上游 `describeGroupAttachments`：把群聊入站的附件标记转成事实占位。

    群聊入站没有原生附件通道：保留「发过什么」的信息，URL 污水不进群上下文，
    也不再被模型复述。
    """
    text = normalize_qq_native_face_segments(_str(content))
    # 注意先替换 `<file ...>` 再删闭合标签：上游用 `name|file|title` 抽文件名。
    text = re.sub(r'<(?:record|audio)\b[^>]*/?>', '[语音]', text, flags=re.IGNORECASE)
    text = re.sub(r'<(?:img|image)\b[^>]*/?>', '[图片]', text, flags=re.IGNORECASE)
    text = re.sub(r'<video\b[^>]*/?>', '[视频]', text, flags=re.IGNORECASE)

    def file_replacement(match: re.Match[str]) -> str:
        found = re.search(r'(?:name|file|title)=["\']([^"\']+)["\']', match.group(0), re.IGNORECASE)
        return '[文件：%s]' % found.group(1) if found else '[文件]'

    text = re.sub(r'<file\b[^>]*/?>', file_replacement, text, flags=re.IGNORECASE)
    text = re.sub(r'</(?:file|img|image|audio|record|video)>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\[CQ:image,[^\]]*\]', '[图片]', text, flags=re.IGNORECASE)
    text = re.sub(r'\[CQ:record,[^\]]*\]', '[语音]', text, flags=re.IGNORECASE)
    text = re.sub(r'\[CQ:video,[^\]]*\]', '[视频]', text, flags=re.IGNORECASE)

    def cq_file_replacement(match: re.Match[str]) -> str:
        found = re.search(r'(?:name|file)=([^,\]]+)', match.group(1), re.IGNORECASE)
        return '[文件：%s]' % found.group(1) if found else '[文件]'

    return re.sub(r'\[CQ:file,([^\]]*)\]', cq_file_replacement, text, flags=re.IGNORECASE)


#: HTTP 头魔数（上游 `guessImageMime` 的 `Buffer.from([137, 80, 78, 71, 13, 10, 26, 10])`）。
_PNG_MAGIC = b'\x89PNG\r\n\x1a\n'


def guess_image_mime(data: bytes, hinted: Any = None) -> str:
    """上游 `guessImageMime`：从魔数嗅探图片 MIME。"""
    hint = _str(hinted).lower()
    if hint.startswith('image/'):
        return hint
    if len(data) >= 3 and data[0] == 0xFF and data[1] == 0xD8 and data[2] == 0xFF:
        return 'image/jpeg'
    if len(data) >= 8 and data[:8] == _PNG_MAGIC:
        return 'image/png'
    if len(data) >= 6 and data[:6] in (b'GIF87a', b'GIF89a'):
        return 'image/gif'
    if len(data) >= 12 and data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    return ''


#: 上游 `guessAudioFormat` 的扩展名提示。
_AUDIO_HINT_RE = re.compile(r'\.(mp3|wav|ogg|m4a|flac|amr)\b', re.IGNORECASE)


def guess_audio_format(data: bytes, hinted_name: Any = None) -> str:
    """上游 `guessAudioFormat`：只接受 OpenAI 兼容 input_audio 通道承认的格式。

    音频文件到的是原始字节（不同于 SILK 语音记录）。
    """
    match = _AUDIO_HINT_RE.search(_str(hinted_name))
    if match:
        return match.group(1).lower()
    if len(data) >= 12 and data[:4] == b'RIFF' and data[8:12] == b'WAVE':
        return 'wav'
    if len(data) >= 4 and data[:4] == b'OggS':
        return 'ogg'
    if len(data) >= 8 and data[4:8] == b'ftyp':
        return 'm4a'
    if len(data) >= 4 and data[:4] == b'fLaC':
        return 'flac'
    if len(data) >= 5 and data[:5] == b'#!AMR':
        return 'amr'
    if len(data) >= 3 and data[:3] == b'ID3':
        return 'mp3'
    if len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return 'mp3'
    return ''


def is_animated_image_mime(mime: str) -> bool:
    """上游 `isAnimatedImageMime`。"""
    return mime in ('image/gif', 'image/webp', 'image/apng')


# =========================================================================== #
# 表情 / 表态（上游 7255–7300、7337）
# =========================================================================== #

#: 上游 `CHAT_REACTION_NAMES`。
CHAT_REACTION_NAMES: list[str] = ['like', 'smile', 'laugh', 'heart', 'surprised', 'sad', 'angry']

#: 上游 `QQ_REACTION_IDS`（OneBot 表态 id）。
QQ_REACTION_IDS: dict[str, str] = {
    'like': '76', 'smile': '14', 'laugh': '182', 'heart': '66',
    'surprised': '0', 'sad': '5', 'angry': '106',
}

#: 上游 `NATIVE_FACE_SEMANTICS`。
NATIVE_FACE_SEMANTICS: list[str] = ['smile', 'laugh', 'sweat', 'awkward', 'heart', 'surprised', 'sad', 'angry']

#: 上游 `QQ_NATIVE_FACE_IDS`（QQ 原生表情 id）。
QQ_NATIVE_FACE_IDS: dict[str, str] = {
    'smile': '14', 'laugh': '182', 'sweat': '27', 'awkward': '111',
    'heart': '66', 'surprised': '0', 'sad': '5', 'angry': '106',
}


def normalize_allowed_native_faces(value: Any) -> list[str]:
    """上游 `normalizeAllowedNativeFaces`：去重、过滤、按语义表长度截断。"""
    if not isinstance(value, list):
        return []
    seen: list[str] = []
    for item in value:
        if item in NATIVE_FACE_SEMANTICS and item not in seen:
            seen.append(item)
    return seen[:len(NATIVE_FACE_SEMANTICS)]


def normalize_expression_threshold(value: Any) -> float:
    """上游 `normalizeExpressionThreshold`：夹在 [0, 1]，非法回落 0.7。"""
    number = _number(value)
    return max(0.0, min(1.0, number)) if _finite(number) else 0.7


#: 上游 `calibratedNativeFaceWillingness` 的语义正则表。
_NATIVE_FACE_PATTERNS: dict[str, re.Pattern[str]] = {
    'smile': re.compile(r'(?:微笑|开心|高兴|谢谢|好耶|好呀|可以|行吧|嘿|哈哈)', re.IGNORECASE | re.UNICODE),
    'laugh': re.compile(r'(?:哈{2,}|笑死|好笑|乐|绷不住|蚌埠|草|救命)', re.IGNORECASE | re.UNICODE),
    'sweat': re.compile(r'(?:流汗|尴尬|无语|服了|麻了|救命|离谱|完了|累|忙|不知道怎么说)', re.IGNORECASE | re.UNICODE),
    'awkward': re.compile(r'(?:尴尬|那个|呃|emm|……|\.{3,}|我真的|怎么说呢)', re.IGNORECASE | re.UNICODE),
    'heart': re.compile(r'(?:喜欢|爱你|抱抱|可爱|谢谢|好耶|开心|高兴)', re.IGNORECASE | re.UNICODE),
    'surprised': re.compile(r'(?:不会吧|真的假的|居然|什么|怎么会|\?{1,}|？{1,}|!{1,}|！{1,})', re.IGNORECASE | re.UNICODE),
    'sad': re.compile(r'(?:难过|哭|委屈|可怜|遗憾|心疼|唉)', re.IGNORECASE | re.UNICODE),
    'angry': re.compile(r'(?:生气|气死|烦|闭嘴|别[再乱闹说]|离谱|过分|你.*(?:啊|吧|？|!|！))', re.IGNORECASE | re.UNICODE),
}


def calibrated_native_face_willingness(semantic: str, willingness: Any, reply_content: Any) -> float:
    """上游 `calibratedNativeFaceWillingness`。

    模型的意愿是**意图估计**，不是传输许可。原生表情需要一个可见文本对应物，
    模型不能靠返回 willingness=1 就把每条例行回复都变成表情。0.90 的上限
    刻意让高于 0.90 的阈值成为事实上的「近乎禁用」。
    """
    text = re.sub(r'<sep/>', ' ', _str(reply_content)).strip()
    if not text:
        return 0.0
    pattern = _NATIVE_FACE_PATTERNS.get(semantic)
    if pattern is None:
        return 0.0
    semantic_match = bool(pattern.search(text))
    evidence = 0.9 if semantic_match else 0.2
    return min(0.9, normalize_expression_threshold(willingness) * (0.25 + evidence * 0.75))


def stable_sticker_asset_id(file_path: Any, hash_value: Any) -> str:
    """上游 `stableStickerAssetId`。

    旧的「只留标点」的 id 会碰撞（例如两个文件名都归一成 `bq--6-`）。保留一段
    可读路径前缀，再附带内容哈希片段，使每一行全局唯一、且未改动的文件保持稳定。
    """
    stem = _str(file_path)
    stem = stem.replace('\\', '/')
    stem = re.sub(r'\.[^.]+$', '', stem)
    stem = re.sub(r'[^a-zA-Z0-9/_-]', '-', stem)
    stem = re.sub(r'-+', '-', stem)
    stem = re.sub(r'^[-/]+|[-/]+$', '', stem)
    stem = stem[:220] or 'sticker'
    suffix = re.sub(r'[^a-fA-F0-9]', '', _str(hash_value))[:16].lower() or 'unhashed'
    return ('%s-%s' % (stem, suffix))[:255]


# =========================================================================== #
# 用户自报时间（上游 7351–7396）
# =========================================================================== #

#: 上游阿拉伯 / 中文钟点正则（`g` 标志 → `finditer`）。
_CLOCK_ARABIC_RE = re.compile(
    r'(?:今天|今晚|下午|晚上|早上|上午)?\s*([0-9]{1,2})\s*(?:[:：.]|点)\s*([0-9]{2}|半)?',
    re.UNICODE,
)
#: 上游时段词正则。
_CLOCK_PERIOD_RE = re.compile(r'(早上|上午|中午|下午|傍晚|晚上)', re.UNICODE)
#: 上游中文数字钟点正则。
_CLOCK_CHINESE_RE = re.compile(
    r'([零一二三四五六七八九十两]+)点(?:(?:零|([一二三四五六七八九十两]+))分?|半)?',
    re.UNICODE,
)
#: 上游 `/(?:早上|上午|下午|晚上|中午)/`（用于裸钟点的 12 小时制歧义判断）。
_CLOCK_PERIOD_ANY_RE = re.compile(r'(?:早上|上午|下午|晚上|中午)', re.UNICODE)

#: 上游时段词 → 代表性钟点锚点。
_PERIOD_CLOCK_HOURS: dict[str, int] = {
    '早上': 8, '上午': 9, '中午': 12, '下午': 15, '傍晚': 18, '晚上': 20,
}

#: 上游 `chineseClockNumber` 的数字表。
_CHINESE_DIGITS: dict[str, int] = {
    '零': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4,
    '五': 5, '六': 6, '七': 7, '八': 8, '九': 9,
}


def _chinese_clock_number(value: str) -> int | None:
    """上游 `chineseClockNumber`：中文数字 → 整数，无法解析返回 `None`。"""
    if value == '十':
        return 10
    if '十' in value:
        left, _, right = value.partition('十')
        tens = _CHINESE_DIGITS.get(left) if left else 1
        ones = _CHINESE_DIGITS.get(right) if right else 0
        if tens is None or ones is None:
            return None
        return tens * 10 + ones
    return _CHINESE_DIGITS.get(value) if len(value) == 1 else None


def extract_user_reported_times(content: Any, now: datetime, timezone: str) -> list[dict[str, Any]]:
    """上游 `extractUserReportedTimes`：只抽取**显式**时钟陈述。

    这是个小的事实助手，不是「推断每个时间表达」的尝试。
    输出键名逐字保持上游 camelCase（`localTime` / `relation` / `statement`）：
    它是喂给模型 prompt 的 wire format。
    """
    text = _str(content)
    current_minutes = local_clock_minutes(now, timezone)
    date = calendar_day_key(now, timezone)
    facts: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(hour: Any, minute: Any, statement: str) -> None:
        if not isinstance(hour, int) or not isinstance(minute, int):
            return
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            return
        clock = '%02d:%02d' % (hour, minute)
        value = hour * 60 + minute
        relation = 'past' if value < current_minutes else 'future' if value > current_minutes else 'current'
        key = '%s:%s' % (clock, statement)
        if key in seen:
            return
        seen.add(key)
        facts.append({
            'localTime': '%s %s' % (date, clock),
            'relation': relation,
            'statement': clip(statement, 240).strip(),
        })

    for match in _CLOCK_ARABIC_RE.finditer(text):
        hour = int(match.group(1))
        minute = 30 if match.group(2) == '半' else int(match.group(2)) if match.group(2) else 0
        prefix = match.group(0)
        if ('下午' in prefix or '晚上' in prefix) and hour < 12:
            hour += 12
        elif not _CLOCK_PERIOD_ANY_RE.search(prefix) and 0 < hour < 12:
            # 裸写的 “6.30” 是歧义的。优先取最接近当前故事时钟的 12 小时制解释，
            # 这样晚上的报告通常意味着 18:30，而不是远得不合理的 06:30。
            morning = hour * 60 + minute
            evening = (hour + 12) * 60 + minute
            if abs(evening - current_minutes) < abs(morning - current_minutes):
                hour += 12
        start = max(0, match.start() - 48)
        add(hour, minute, text[start:min(len(text), match.start() + len(match.group(0)) + 96)])

    # 时段词（中午/下午/晚上等）作为该时段代表性的钟点锚点，供守卫与 prompt
    # 理解“中午一起吃饭”这类不含数字的时间约定。
    for match in _CLOCK_PERIOD_RE.finditer(text):
        start = max(0, match.start() - 48)
        add(_PERIOD_CLOCK_HOURS[match.group(1)], 0,
            text[start:min(len(text), match.start() + len(match.group(0)) + 96)])

    # 中文数字钟点（“八点”“八点半”“九点一刻”）：口语消息最常用的写法，此前
    # 只有守卫的正则认识它们，prompt 侧的 userReportedTimes 反而漏掉。
    for match in _CLOCK_CHINESE_RE.finditer(text):
        hour = _chinese_clock_number(match.group(1))
        minute = 30 if match.group(2) == '半' else _chinese_clock_number(match.group(2)) if match.group(2) else 0
        if hour is None or minute is None:
            continue
        start = max(0, match.start() - 48)
        add(hour, minute, text[start:min(len(text), match.start() + len(match.group(0)) + 96)])

    return facts[:4]


# =========================================================================== #
# 引用消息（上游 7399–7443）
# ===========================================================================

#: 上游 `normalizeGroupDisplayName` 的换行清洗。
_NEWLINE_RE = re.compile(r'[\r\n]', re.UNICODE)


def _normalize_group_display_name(*candidates: Any) -> str:
    """上游 `normalizeGroupDisplayName`：取第一个非空候选，截断 80 字。"""
    for candidate in candidates:
        name = _NEWLINE_RE.sub(' ', _str(candidate)).strip()
        if name:
            return name[:80]
    return ''


def describe_quoted_message(session: Any, character_name: str = '主角') -> dict[str, Any] | None:
    """上游 `describeQuotedMessage`：把 session.quote 变成有界可读快照。

    输出键名 camelCase（`senderId` / `senderName` / `speaker` / `content`）：
    它是喂给模型的 wire format。
    """
    quote = session.get('quote') if isinstance(session, dict) else getattr(session, 'quote', None)
    if not quote:
        return None
    content = normalize_quoted_message_content(quote.get('content') if isinstance(quote, dict) else None)
    if not content:
        return None
    user = (quote.get('user') if isinstance(quote, dict) else None) or {}
    member = (quote.get('member') if isinstance(quote, dict) else None) or {}
    sender_id = _str(user.get('id')).strip()
    self_id = _str(session.get('selfId') if isinstance(session, dict) else getattr(session, 'self_id', None))
    is_character = bool(sender_id) and sender_id == self_id
    if is_character:
        sender_name = _str(character_name or '主角').strip() or '主角'
    else:
        sender_name = _normalize_group_display_name(
            member.get('nick'), member.get('name'), user.get('nick'), user.get('name'), sender_id,
        ) or '未知发送者'
    if is_character:
        speaker = '主角「%s」' % sender_name
    elif sender_id:
        speaker = '消息发送者「%s」（ID：%s）' % (sender_name, sender_id)
    else:
        speaker = '消息发送者「%s」' % sender_name
    return {'senderId': sender_id, 'senderName': sender_name, 'speaker': speaker, 'content': content}


def normalize_quoted_message_content(value: Any) -> str:
    """上游 `normalizeQuotedMessageContent`：引用消息的有界纯文本化。"""
    raw = normalize_qq_native_face_segments(value)
    content = re.sub(r'<(?:img|image)\b[^>]*/?>(?:</(?:img|image)>)?', '[图片]', raw, flags=re.IGNORECASE)
    content = re.sub(r'<(?:audio|record)\b[^>]*/?>(?:</(?:audio|record)>)?', '[语音]', content, flags=re.IGNORECASE)
    content = re.sub(r'<video\b[^>]*/?>(?:</video>)?', '[视频]', content, flags=re.IGNORECASE)
    content = re.sub(r'<(?:face|mface)\b[^>]*/?>(?:</(?:face|mface)>)?', '[表情]', content, flags=re.IGNORECASE)
    content = re.sub(r'<at\b[^>]*(?:name|id)=["\']?([^\s"\'>]+)[^>]*/?>(?:</at>)?', r'[@\1]', content, flags=re.IGNORECASE)
    content = re.sub(r'\[CQ:image,[^\]]*\]', '[图片]', content, flags=re.IGNORECASE)
    content = re.sub(r'\[CQ:record,[^\]]*\]', '[语音]', content, flags=re.IGNORECASE)
    content = re.sub(r'\[CQ:video,[^\]]*\]', '[视频]', content, flags=re.IGNORECASE)
    content = re.sub(r'\[CQ:face,[^\]]*\]', '[表情]', content, flags=re.IGNORECASE)
    content = re.sub(r'<[^>]+>', '', content)
    content = re.sub(r'[\r\n]+', ' ', content)
    content = re.sub(r'\s{2,}', ' ', content)
    return clip(content.strip(), 1_500)


def normalize_quoted_message_context(value: Any) -> dict[str, Any] | None:
    """上游 `normalizeQuotedMessageContext`：归一化已持久化的引用快照。"""
    if not is_record(value):
        return None
    content = normalize_quoted_message_content(value.get('content'))
    if not content:
        return None
    sender_id = clip(_str(value.get('senderId')), 127)
    sender_name = clip(_str(value.get('senderName')), 255) or '未知发送者'
    speaker = clip(_str(value.get('speaker')), 500) or (
        '消息发送者「%s」（ID：%s）' % (sender_name, sender_id)
        if sender_id else '消息发送者「%s」' % sender_name
    )
    return {'senderId': sender_id, 'senderName': sender_name, 'speaker': speaker, 'content': content}


def normalize_allowed_reactions(value: Any) -> list[str]:
    """上游 `normalizeAllowedReactions`：只留语义表态名、去重、按表长截断。"""
    if not isinstance(value, list):
        return []
    seen: list[str] = []
    for item in value:
        if item in CHAT_REACTION_NAMES and item not in seen:
            seen.append(item)
    return seen[:len(CHAT_REACTION_NAMES)]


# =========================================================================== #
# 时间线计划（上游 7454–7528）
# =========================================================================== #

#: 上游 `TIMELINE_KIND_ALIASES`：模型常给出近义标签（"scene" / "event" / 中文），
#: 明显的近义写法一律强转，而不是丢掉整个节点。
TIMELINE_KIND_ALIASES: dict[str, str] = {
    'activity': 'activity', 'action': 'activity', 'event': 'activity', 'scene': 'activity', 'behavior': 'activity',
    'thought': 'thought', 'think': 'thought', 'feeling': 'thought', 'mood': 'thought', 'inner': 'thought',
    'state': 'state', 'status': 'state', 'condition': 'state',
    '活动': 'activity', '行动': 'activity', '事件': 'activity', '场景': 'activity',
    '想法': 'thought', '心情': 'thought', '思绪': 'thought',
    '状态': 'state',
}


def _coerce_timeline_kind(value: Any) -> str:
    """上游 `coerceTimelineKind`：未知 kind 回落 `'activity'`，非字符串返回空串。"""
    if not isinstance(value, str):
        return ''
    return TIMELINE_KIND_ALIASES.get(value.strip().lower(), 'activity')


def _coerce_timeline_position(value: Any) -> float:
    """上游 `coerceTimelinePosition`：接受数字、"0.5" 与 "50%"，非法返回 NaN。"""
    if isinstance(value, bool):
        return float('nan')
    if isinstance(value, (int, float)):
        return max(0.0, min(1.0, float(value)))
    if isinstance(value, str):
        text = re.sub(r'%$', '', value.strip())
        try:
            parsed = float(text)
        except ValueError:
            return float('nan')
        if _finite(parsed):
            return max(0.0, min(1.0, parsed if parsed <= 1 else parsed / 100))
    return float('nan')


def normalize_timeline_plan(value: Any) -> dict[str, Any] | None:
    """上游 `normalizeTimelinePlan`：只解析这条窄事件账本形状。

    未知模型字段与空计划会在变成世界状态来源之前被丢弃。
    输出键名 camelCase（`beats` / `at` / `kind` / `summary` / `carry`）：wire format。
    """
    if not is_record(value) or not isinstance(value.get('beats'), list):
        return None
    beats: list[dict[str, Any]] = []
    for item in value['beats']:
        if not is_record(item):
            continue
        at = _coerce_timeline_position(item.get('at'))
        kind = _coerce_timeline_kind(item.get('kind'))
        summary = clip(item.get('summary'), 240).strip() if isinstance(item.get('summary'), str) else ''
        beats.append({'at': at, 'kind': kind, 'summary': summary})
    beats = [
        beat for beat in beats
        if _finite(beat['at']) and beat['kind'] and beat['summary']
    ]
    beats.sort(key=lambda beat: beat['at'])
    beats = beats[:4]
    if not beats:
        return None
    raw_carry = value.get('carry')
    if isinstance(raw_carry, list):
        carry = [
            clip(item, 180).strip() for item in raw_carry if isinstance(item, str)
        ]
        carry = [item for item in carry if item][:4]
    else:
        carry = []
    plan: dict[str, Any] = {'beats': beats}
    if carry:
        plan['carry'] = carry
    return plan


def describe_timeline_plan_rejection(value: Any) -> str:
    """上游 `describeTimelinePlanRejection`：给 warn 日志用的人类可读拒绝原因。"""
    if not is_record(value):
        return '返回不是 JSON 对象'
    if not isinstance(value.get('beats'), list):
        return '缺少 beats 数组'
    if not value['beats']:
        return 'beats 为空数组（模型未产出任何节点）'
    details: list[str] = []
    for item in value['beats']:
        if not is_record(item):
            continue
        problems: list[str] = []
        at = _coerce_timeline_position(item.get('at'))
        if not _finite(at):
            problems.append('at=%s 无法解析' % _json_literal(item.get('at')))
        if not _coerce_timeline_kind(item.get('kind')):
            problems.append('kind=%s 非法' % _json_literal(item.get('kind')))
        if not isinstance(item.get('summary'), str) or not item['summary'].strip():
            problems.append('summary 为空')
        details.append('，'.join(problems) if problems else '通过')
    return '节点校验详情：%s' % '；'.join(details)


def _json_literal(value: Any) -> str:
    """上游 `JSON.stringify(item.at)` 的最小等价物（用于日志文案）。"""
    if value is None:
        return 'null'
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return '"%s"' % value.replace('\\', '\\\\').replace('"', '\\"')
    import json
    return json.dumps(value, ensure_ascii=False)


def timeline_entry_prompt_projection(entry: dict[str, Any]) -> dict[str, Any]:
    """上游 `timelineEntryPromptProjection`。

    自动生成的剧本散文是一种**渲染**，不是下一回合的时间真相来源。紧凑的宿主
    账本保留了真实次序，同时不允许上一段文字被复制进新的时间窗口。
    """
    metadata = entry.get('metadata') if isinstance(entry, dict) else None
    metadata = metadata if isinstance(metadata, dict) else {}
    if metadata.get('narrativeAuthority') == 'original-v2':
        return entry
    if entry.get('kind') != 'script':
        return entry
    plan = normalize_timeline_plan(metadata.get('timelinePlan'))
    if not plan:
        return entry
    beats = ' | '.join(
        '%d%% %s: %s' % (_js_round(beat['at'] * 100), beat['kind'], beat['summary'])
        for beat in plan['beats']
    )
    carry_values = plan.get('carry')
    carry = ' Carry: %s' % ' | '.join(carry_values) if carry_values else ''
    projected = dict(entry)
    projected['content'] = '[Host timeline ledger for this completed automatic window: %s.%s]' % (beats, carry)
    return projected


# =========================================================================== #
# 群聊动作（上游 7531–7613）
# =========================================================================== #


def normalize_group_chat_actions(
    decision: dict[str, Any], capabilities: Any, context: dict[str, Any],
) -> dict[str, Any]:
    """上游 `normalizeGroupChatActions`：只接受已声明的动作与已提供的消息引用。

    输出键名 camelCase（`messageRef` / `messageId` / `reaction` / `replyTo`）：
    它是送给适配层的 wire format。
    """
    if not capabilities:
        return {'reactions': []}
    targets: dict[str, str] = {}
    for message in (context.get('messages') or []):
        if not isinstance(message, dict):
            continue
        message_ref = message.get('messageRef')
        message_id = message.get('messageId')
        if message_ref and message_id:
            targets[message_ref] = message_id

    group_reply = decision.get('groupReply')
    interaction = decision.get('interaction')
    interaction_reply = interaction.get('reply') if isinstance(interaction, dict) else None
    if isinstance(group_reply, dict) and group_reply.get('mode') == 'immediate':
        raw_reply_to = group_reply.get('replyTo')
    elif isinstance(interaction_reply, dict) and interaction_reply.get('mode') == 'immediate':
        raw_reply_to = interaction_reply.get('replyTo')
    else:
        raw_reply_to = None
    reply_message_id = (
        targets.get(raw_reply_to)
        if capabilities.get('quoteReply') and isinstance(raw_reply_to, str)
        else None
    )
    reply_to = {'messageRef': raw_reply_to, 'messageId': reply_message_id} if reply_message_id else None

    allowed = set(capabilities.get('reactions') or [])
    raw_reactions = decision.get('messageReactions')
    reactions: list[dict[str, Any]] = []
    if isinstance(raw_reactions, list):
        for item in raw_reactions:
            if not is_record(item):
                continue
            if not isinstance(item.get('messageRef'), str) or not isinstance(item.get('reaction'), str):
                continue
            message_ref = str(item['messageRef'])
            reactions.append({
                'messageRef': message_ref,
                'reaction': str(item['reaction']),
                'messageId': targets.get(message_ref) or '',
            })
        reactions = [
            item for item in reactions
            if item['messageId'] and item['reaction'] in allowed
        ][:1]
    result: dict[str, Any] = {}
    if reply_to:
        result['replyTo'] = reply_to
    result['reactions'] = reactions
    return result


def format_group_speaker(sender_name: Any, sender_id: Any) -> str:
    """上游 `formatGroupSpeaker`：同时保留显示名与稳定的 QQ 身份。"""
    identifier = _str(sender_id or 'unknown').strip() or 'unknown'
    name = _NEWLINE_RE.sub(' ', _str(sender_name or '')).strip() or identifier
    return '群成员（QQ：%s）' % identifier if name == identifier else '群成员「%s」（QQ：%s）' % (name, identifier)


def normalize_group_visible_reply(raw: Any, interaction: Any, max_characters: int,
                                  separator: str = '<sep/>') -> str:
    """上游 `normalizeGroupVisibleReply`：显式群回复优先，其次旧的 interaction 回退。"""
    return (
        _normalize_group_reply(raw, max_characters, separator)
        or _normalize_group_interaction_reply(interaction, max_characters, separator)
    )


def _normalize_group_reply(raw: Any, max_characters: int, separator: str = '<sep/>') -> str:
    if not raw or raw.get('mode') != 'immediate':
        return ''
    return _normalize_visible_message_content(raw.get('content'), max_characters, separator)


def _normalize_group_interaction_reply(raw: Any, max_characters: int,
                                       separator: str = '<sep/>') -> str:
    if not raw or not isinstance(raw.get('reply'), dict) or raw['reply'].get('mode') != 'immediate':
        return ''
    return _normalize_visible_message_content(raw['reply'].get('content'), max_characters, separator)


#: 上游 `normalizeVisibleMessageContent` 的可见回复污染清除。
_VISIBLE_SEP_RE = re.compile(r'[<＜]\s*sep\s*/?\s*[>＞]', re.IGNORECASE | re.UNICODE)
_VISIBLE_TAG_RE = re.compile(
    r'</?(?:file|img|image|audio|record|video|flash|mface)\b[^>]*/?>', re.IGNORECASE | re.UNICODE,
)
_VISIBLE_CQ_RE = re.compile(r'\[CQ:(?:file|image|record|video|flash|mface),[^\]]*\]', re.IGNORECASE | re.UNICODE)
_VISIBLE_STICKER_RE = re.compile(r'[\[【](?:表情包?|图片|动图|GIF)[\]】]', re.IGNORECASE | re.UNICODE)
_VISIBLE_FACE_RE = re.compile(r'[\[【](?:流汗|微笑|笑哭|尴尬|爱心|惊讶|流泪|委屈)[\]】]', re.IGNORECASE | re.UNICODE)


def _normalize_visible_message_content(value: Any, max_characters: int,
                                       separator: str = '<sep/>') -> str:
    """上游 `normalizeVisibleMessageContent`：可见回复是纯文本合约。"""
    text = _str(value)
    # provider 偶尔会漏掉斜杠或写成全角括号。只归一化结构化的可见回复；
    # 剧本散文与入站用户文本保持原样。
    text = _VISIBLE_SEP_RE.sub(separator.strip() or '<sep/>', text)
    # 模型可能复述入站消息里的附件标记（如 `<file src="…qqdownload…">`）。
    # 可见回复是纯文本合约：标记一旦漏出会被适配器解析成真实附件发出去。
    text = _VISIBLE_TAG_RE.sub('', text)
    text = _VISIBLE_CQ_RE.sub('', text)
    text = _VISIBLE_STICKER_RE.sub('', text)
    text = _VISIBLE_FACE_RE.sub('', text)
    return text.strip()[:max(1, max_characters)]


def requires_visible_reply_recovery(phase: str, group_context: Any, decision: dict[str, Any]) -> bool:
    """上游 `requiresVisibleReplyRecovery`：用户回合缺少结构化可见回复时需补救。"""
    if phase != 'user-message':
        return False
    if group_context:
        return not _has_structured_group_reply(decision)
    return not _has_structured_interaction(decision.get('interaction'))


def visible_reply_mode(decision: dict[str, Any], phase: str, group_context: Any = None) -> str:
    """上游 `visibleReplyMode`：给日志用的「本回合可见投递形态」。

    返回的是**中文日志文案**（`主动联系` / `无可见投递` / `未提供或无效` …），
    必须逐字保留。
    """
    cross = decision.get('crossConversationActions')
    if phase == 'advance':
        if isinstance(cross, list) and any(is_record(action) and action.get('mode') == 'immediate' for action in cross):
            return '主动联系'
        if isinstance(cross, list) and any(is_record(action) and action.get('mode') == 'delayed' for action in cross):
            return '计划联系'
        return '无可见投递'
    if phase == 'conversation-follow-up' or phase == 'intent-due':
        interaction = decision.get('interaction')
        if _has_structured_interaction(interaction):
            return interaction['reply']['mode']
        if isinstance(cross, list) and any(is_record(action) and action.get('mode') == 'immediate' for action in cross):
            return '主动联系'
        return '无可见投递'
    interaction = decision.get('interaction')
    group_reply = decision.get('groupReply')
    if not group_context:
        return interaction['reply']['mode'] if _has_structured_interaction(interaction) else '未提供或无效'
    if _has_structured_group_reply_field(group_reply):
        return 'group:%s' % group_reply['mode']
    if _has_structured_interaction(interaction):
        return 'group-fallback:%s' % interaction['reply']['mode']
    return '未提供或无效'


def _has_structured_group_reply(decision: dict[str, Any]) -> bool:
    return _has_structured_group_reply_field(decision.get('groupReply')) or _has_structured_interaction(
        decision.get('interaction'))


def _has_structured_group_reply_field(value: Any) -> bool:
    if not is_record(value) or (value.get('mode') != 'none' and value.get('mode') != 'immediate'):
        return False
    return value['mode'] == 'none' or (
        isinstance(value.get('content'), str) and bool(value['content'].strip())
    )


def _has_structured_interaction(value: Any) -> bool:
    if not is_record(value) or not isinstance(value.get('seen'), bool) or not is_record(value.get('reply')):
        return False
    reply = value['reply']
    mode = reply.get('mode')
    if mode not in ('none', 'immediate', 'delayed'):
        return False
    if mode == 'none':
        # `mode:'none'` 有两种来源：模型真的决定不回（正常），以及**声明了 immediate 但
        # 引用的 `<say>` 动作落地不了**（`resolve_authored_actions` 退成 none 并留下
        # `unresolved_action_id`）。后者是坏回合，遇到就沿用既有的「重写一次」机制，
        # 否则用户看到的是"她读了却不回"（上游的静默语义；本移植版刻意偏离，见
        # `docs/PORTING_NOTES.md` §17）。
        return not reply.get('unresolved_action_id')
    if reply.get('unresolved_action_id'):
        return False
    if not isinstance(reply.get('content'), str) or not reply['content'].strip():
        return False
    if mode == 'immediate':
        return True
    return isinstance(reply.get('sendAt'), str) and bool(reply['sendAt'].strip())


def safe_json_preview(value: Any) -> str:
    """上游 `safeJsonPreview`：诊断日志用的紧凑 JSON，永不抛异常、长度有界。"""
    import json
    try:
        text = 'undefined' if value is None else json.dumps(value, ensure_ascii=False)
        return str(text)[:300]
    except Exception:
        return '(unserializable)'


def literal_quote_text(value: Any) -> str:
    """上游 `literalQuoteText`：识别「引用：…」字面量。"""
    match = re.match(r'^\s*[「\[]引用[:：]\s*(.*?)\s*[」\]]\s*$', _str(value), re.UNICODE)
    return match.group(1).strip() if match else ''


def is_literal_quote_only(value: Any) -> bool:
    """上游 `isLiteralQuoteOnly`。"""
    return bool(literal_quote_text(value))


def normalize_account_id(value: Any) -> str:
    """上游 `normalizeAccountId`：剥掉 `private:` / `onebot:` 等传输前缀（最多 3 层）。"""
    normalized = _str(value).strip().lower()
    for _ in range(3):
        following = re.sub(r'^(?:private|user|onebot|napcat|qq):', '', normalized, flags=re.IGNORECASE).strip()
        if following == normalized:
            break
        normalized = following
    return normalized


def signed_number(value: float) -> str:
    """上游 `signedNumber`：带符号数字文案（整数不带小数，否则两位）。"""
    if isinstance(value, float) and not float(value).is_integer():
        return '%s%.2f' % ('+' if value > 0 else '', value)
    return '%s%d' % ('+' if value > 0 else '', int(value))


def is_transient_database_error(error: Any) -> bool:
    """上游 `isTransientDatabaseError`：只把这些消息当作可重试的瞬时故障。"""
    message = str(error) if not isinstance(error, BaseException) else str(error)
    return bool(re.search(r'disk\s*i/o|database is locked|busy|unable to open', message, re.IGNORECASE))


def is_enabled_account(accounts: Any, qq: str) -> bool:
    """上游 `isEnabledAccount`：账号在白名单里且未被禁用。"""
    normalized = normalize_account_id(qq)
    if not normalized:
        return False
    if not isinstance(accounts, list):
        return False
    return any(
        is_record(account)
        and account.get('enabled') is not False
        and normalize_account_id(account.get('qq')) == normalized
        for account in accounts
    )


# =========================================================================== #
# 决策结构判定（上游 7711–7826）
# =========================================================================== #


def has_required_narrative_script(value: Any) -> bool:
    """上游 `hasRequiredNarrativeScript`：真实模型回回合必须有非空散文。"""
    return is_record(value) and isinstance(value.get('script'), str) and len(value['script'].strip()) > 0


# `resolve_blind_mode_config` / `resolve_black_box_config` 的**实现**在 `config.py`
# （它们是配置层的归一函数）。此处从定义处 import 再导出，保证
# `from plugin.core.service.helpers import resolve_blind_mode_config` 与
# `from plugin.core.service import resolve_blind_mode_config` 拿到**同一个对象**，
# 不出现两份会各自漂移的实现。


def is_automatic_narrative_phase(phase: str) -> bool:
    """上游 `isAutomaticNarrativePhase`。"""
    return phase == 'advance' or phase == 'conversation-follow-up'


def normalize_automatic_delivery_summary(value: Any) -> str:
    """上游 `normalizeAutomaticDeliverySummary`。"""
    return clip(value, 240).strip() if isinstance(value, str) else ''


def normalize_follow_up_summary(value: Any) -> str:
    """上游 `normalizeFollowUpSummary`：小写化并压缩空白的去重指纹。"""
    if not isinstance(value, str):
        return ''
    return re.sub(r'\s+', ' ', clip(value, 360).strip().lower())


def follow_up_expires_at(value: Any, now: datetime) -> datetime:
    """上游 `followUpExpiresAt`：承诺过期时间最多 24 小时，且必须晚于 now。"""
    requested = to_date(value)
    maximum = parse_dt(dt_ms(now) + 24 * 60 * 60 * 1000)
    if not requested or dt_ms(requested) <= dt_ms(now):
        return maximum
    return requested if dt_ms(requested) < dt_ms(maximum) else maximum


def normalize_follow_up_commitment(value: Any, now: datetime) -> dict[str, Any] | None:
    """上游 `normalizeFollowUpCommitment`：承诺草稿的窄形状校验。"""
    if not is_record(value):
        return None
    kind = value.get('kind')
    if kind not in ('thinking', 'checking', 'decision', 'emotional-settle'):
        kind = None
    summary = clip(value.get('summary'), 360).strip() if isinstance(value.get('summary'), str) else ''
    not_before = to_date(value.get('notBefore'))
    if not kind or not summary or not not_before:
        return None
    delta = dt_ms(not_before) - dt_ms(now)
    if delta < 5 * 60 * 1000 or delta > 12 * 60 * 60 * 1000:
        return None
    raw_ids = value.get('sourceEntryIds')
    if isinstance(raw_ids, list):
        source_entry_ids = [
            int(item) for item in raw_ids
            if isinstance(item, int) and not isinstance(item, bool) and _is_safe_integer(item) and item > 0
        ][:4]
    else:
        source_entry_ids = []
    expires_at = to_date(value.get('expiresAt'))
    result: dict[str, Any] = {'kind': kind, 'summary': summary, 'notBefore': iso(not_before)}
    if expires_at and dt_ms(expires_at) > dt_ms(not_before):
        result['expiresAt'] = iso(expires_at)
    if source_entry_ids:
        result['sourceEntryIds'] = source_entry_ids
    return result


def inferred_follow_up_commitment(content: str, now: datetime) -> dict[str, Any]:
    """上游 `inferredFollowUpCommitment`：模型没给承诺字段时兜底推断。"""
    return {
        'kind': 'thinking',
        'summary': clip('The character promised to return after thinking: %s' % content, 360),
        'notBefore': iso(parse_dt(dt_ms(now) + 20 * 60 * 1000)),
    }


def interaction_promises_follow_up(content: Any) -> bool:
    """上游 `interactionPromisesFollowUp`：可见回复是否承诺稍后答复。"""
    if not isinstance(content, str):
        return False
    return bool(re.search(
        r'我(?:先)?想想|我去(?:想想|看看|查查|确认)|晚点(?:回|说|告诉)|之后(?:回|说|告诉)|等我.{0,12}(?:回|说|告诉)|整理.{0,12}(?:回|说|告诉)',
        content, re.UNICODE,
    ))


def normalize_follow_up_resolutions(value: Any) -> list[dict[str, Any]]:
    """上游 `normalizeFollowUpResolutions`：最多两条承诺处置。"""
    if not isinstance(value, list):
        return []
    resolutions: list[dict[str, Any]] = []
    for item in value:
        if not is_record(item):
            continue
        if not isinstance(item.get('id'), int) or isinstance(item.get('id'), bool):
            continue
        if not _is_integer(item['id']) or item['id'] <= 0:
            continue
        if item.get('outcome') not in ('fulfilled', 'rescheduled', 'cancelled'):
            continue
        entry: dict[str, Any] = {'id': item['id'], 'outcome': item['outcome']}
        if isinstance(item.get('notBefore'), str):
            entry['notBefore'] = item['notBefore']
        resolutions.append(entry)
    return resolutions[:2]


def automatic_delivery_from_payload(value: Any) -> dict[str, Any] | None:
    """上游 `automaticDeliveryFromPayload`（输出 camelCase：wire format）。"""
    record = value.get('automaticDelivery') if is_record(value) else None
    record = record if is_record(record) else None
    summary = normalize_automatic_delivery_summary(record.get('summary') if record else None)
    raw_id = record.get('sourceEntryId') if record else None
    source_entry_id = raw_id if isinstance(raw_id, int) and not isinstance(raw_id, bool) and _is_safe_integer(raw_id) else None
    if not summary:
        return None
    result: dict[str, Any] = {'summary': summary}
    if source_entry_id:
        result['sourceEntryId'] = source_entry_id
    return result


def merge_delivery_summary(left: str, right: str) -> str:
    """上游 `mergeDeliverySummary`：合并两条背景投递摘要，长度上限 240。"""
    if not left or left == right or right in left:
        return left or right
    if left in right:
        return right
    return clip('%s；%s' % (left, right), 240)


def normalize_scene_presence_drafts(value: Any, entries: list[dict[str, Any]],
                                    now: datetime | None = None) -> list[dict[str, Any]]:
    """上游 `normalizeScenePresenceDrafts`。

    场景压缩只能凭**显式观察到的证据**更新一份极小的在场名单。这样具名的配角
    能保持可用，又不会被自动当作在场。
    """
    if not isinstance(value, list):
        return []
    moment = now if now is not None else utc_now()
    by_id = {
        entry.get('id'): entry for entry in entries if isinstance(entry, dict)
    }
    drafts: list[dict[str, Any]] = []
    for item in value:
        if not is_record(item):
            continue
        name = clip(item.get('name'), 80).strip() if isinstance(item.get('name'), str) else ''
        status = item.get('status') if item.get('status') in ('present', 'off-scene', 'expected') else None
        basis = clip(item.get('basis'), 300).strip() if isinstance(item.get('basis'), str) else ''
        raw_ids = item.get('sourceEntryIds')
        if isinstance(raw_ids, list):
            source_entry_ids = [
                item_id for item_id in raw_ids
                if isinstance(item_id, int) and not isinstance(item_id, bool) and item_id in by_id
            ][:8]
        else:
            source_entry_ids = []
        evidence = [by_id[entry_id] for entry_id in source_entry_ids]
        evidence = [entry for entry in evidence if name in _str(entry.get('content'))]
        if not name or not status or not basis or not evidence:
            continue
        if not _has_explicit_presence_evidence(status, evidence):
            continue
        drafts.append({
            'name': name, 'status': status, 'basis': basis,
            'sourceEntryIds': source_entry_ids, 'updatedAt': iso(moment),
        })
    from ..story_state import normalize_scene_presence_state
    return normalize_scene_presence_state(drafts)


def _has_explicit_presence_evidence(status: str, entries: list[dict[str, Any]]) -> bool:
    """上游 `hasExplicitPresenceEvidence`：每种状态各自的显式措辞证据。"""
    text = '\n'.join(_str(entry.get('content')) for entry in entries)
    if status == 'off-scene':
        return bool(re.search(r'告别|道别|分别|先走|离开|离去|回家|回去了|独自|分开|告辞', text))
    if status == 'expected':
        return bool(re.search(r'约好|约在|等会|稍后|会来|准备来|约见', text))
    return bool(re.search(r'一起|同行|身边|来到|抵达|进入|走进|拉着|坐在|站在|陪着', text))


# =========================================================================== #
# 交互契约（上游 7968–7985）
# =========================================================================== #


def normalize_interaction(value: Any, now: datetime, runtime: dict[str, Any]) -> dict[str, Any] | None:
    """上游 `normalizeInteraction`：机器可读的可见回复契约归一化。

    `seen` 只描述是否读了新消息；`reply` 是独立的发送通道。跟进 / 到期回合协议
    规定 `seen=false`，若在此处因 seen 抹掉回复，「稍后读到再回」的自救路径会被
    无声斩断（模型写进了剧本的发送与投递现实就此分裂）。

    输出键名 camelCase（`seen` / `reply` / `mode` / `content` / `sendAt`）：wire format；
    读取侧同时接受 snake_case（`send_at`）。
    """
    if not is_record(value) or not isinstance(value.get('seen'), bool) or not is_record(value.get('reply')):
        return None
    reply = value['reply']
    mode = reply.get('mode')
    if mode not in ('none', 'immediate', 'delayed'):
        return None
    content = (
        _normalize_visible_message_content(
            reply.get('content'),
            config_get(runtime, 'max_message_characters', 'maxMessageCharacters'),
            config_get(runtime, 'message_separator', 'messageSeparator'),
        )
        if isinstance(reply.get('content'), str) else None
    )
    send_at = to_date(reply.get('sendAt', reply.get('send_at')))
    seen = value.get('seen') is True
    if mode == 'none':
        return {'seen': seen, 'reply': {'mode': 'none'}}
    if not content:
        return {'seen': seen, 'reply': {'mode': 'none'}}
    if mode == 'immediate':
        return {'seen': seen, 'reply': {'mode': mode, 'content': content}}
    send_at_ms = dt_ms(send_at) if send_at else float('nan')
    delay = send_at_ms - dt_ms(now)
    if not send_at \
            or delay < _number(config_get(runtime, 'minimum_delayed_reply_seconds', 'minimumDelayedReplySeconds')) * 1_000 \
            or delay > _number(config_get(runtime, 'maximum_delayed_reply_minutes', 'maximumDelayedReplyMinutes')) * 60 * 1000:
        return {'seen': seen, 'reply': {'mode': 'none'}}
    return {'seen': seen, 'reply': {'mode': mode, 'content': content, 'sendAt': iso(send_at)}}


# =========================================================================== #
# 参与者状态 / 到期意图分组（上游 8071–8123）
# =========================================================================== #


def pick_participant_state_patch(value: dict[str, Any]) -> dict[str, Any]:
    """上游 `pickParticipantStatePatch`：只接受两个数组字段的补丁。"""
    patch: dict[str, Any] = {}
    open_threads = value.get('openThreads')
    if isinstance(open_threads, list) and all(isinstance(item, str) for item in open_threads):
        patch['openThreads'] = [clip(item, 500) for item in open_threads][:50]
    notes = value.get('relationshipNotes')
    if isinstance(notes, list) and all(isinstance(item, str) for item in notes):
        patch['relationshipNotes'] = [clip(item, 500) for item in notes][:50]
    return patch


def merge_setting(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """上游 `mergeSetting`（`:8078`）：浅合并 + character / user 两处深合并。

    JS 的 `{...undefined}` 是 `{}`，所以 nil 输入按空对象处理；`character` /
    `user` 只接受 dict 补丁（字符串补丁不得把这两个对象摊成下标键）。
    契约见 `plugin/tests/test_service_base.py::MergeSettingTests` 与
    `base.py` 的 `_fallback_merge_setting`。
    """
    base_record = base if isinstance(base, dict) else {}
    patch_record = patch if isinstance(patch, dict) else {}
    merged: dict[str, Any] = {**base_record, **patch_record}
    for key in ('character', 'user'):
        current = base_record.get(key)
        current = dict(current) if isinstance(current, dict) else {}
        incoming = patch_record.get(key)
        if isinstance(incoming, dict):
            current.update(incoming)
        merged[key] = current
    return merged


def merge_participant_state(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """上游 `mergeParticipantState`（`:8082`）：两个数组字段不得被 undefined 覆盖。

    同 `merge_setting`：JS 的 `{...undefined}` 是 `{}`，nil 输入按空对象处理。
    """
    base_record = base if isinstance(base, dict) else {}
    patch_record = patch if isinstance(patch, dict) else {}
    merged: dict[str, Any] = {**base_record, **patch_record}
    merged['openThreads'] = (
        patch_record['openThreads'] if isinstance(patch_record.get('openThreads'), list)
        else base_record.get('openThreads')
    )
    merged['relationshipNotes'] = (
        patch_record['relationshipNotes'] if isinstance(patch_record.get('relationshipNotes'), list)
        else base_record.get('relationshipNotes')
    )
    return merged


def normalize_participant_state(value: Any) -> dict[str, Any]:
    """上游 `normalizeParticipantState`。

    输出键名 camelCase（`openThreads` / `relationshipNotes` / `relationshipOverlay` /
    `unreadMessageCount` / `pendingReplyCount` / `lastUserMessageAt` /
    `lastCharacterMessageAt`）：它是 `ParticipantState` 的持久化 wire format
    （数据库 `state` JSON 列 / 上游旧 JSON 都是 camelCase），其它 mixin 直接读这些键。
    读取侧同时接受 snake_case。
    """
    record = value if is_record(value) else {}

    def _string_list(key: str, snake: str) -> list[str]:
        raw = record.get(key, record.get(snake))
        if not isinstance(raw, list):
            return []
        return [clip(item, 500) for item in raw if isinstance(item, str)][:50]

    def _count(key: str, snake: str) -> int:
        raw = record.get(key, record.get(snake))
        number = raw if isinstance(raw, (int, float)) and not isinstance(raw, bool) else 0
        return max(0, math.floor(float(number)))

    overlay_raw = record.get('relationshipOverlay', record.get('relationship_overlay'))
    last_user = record.get('lastUserMessageAt', record.get('last_user_message_at'))
    last_character = record.get('lastCharacterMessageAt', record.get('last_character_message_at'))
    return {
        'openThreads': _string_list('openThreads', 'open_threads'),
        'relationshipNotes': _string_list('relationshipNotes', 'relationship_notes'),
        'relationshipOverlay': clip(overlay_raw, 4_000) if isinstance(overlay_raw, str) else None,
        'unreadMessageCount': _count('unreadMessageCount', 'unread_message_count'),
        'pendingReplyCount': _count('pendingReplyCount', 'pending_reply_count'),
        'lastUserMessageAt': last_user if isinstance(last_user, str) else None,
        'lastCharacterMessageAt': last_character if isinstance(last_character, str) else None,
    }


def participant_relevance(participant: dict[str, Any]) -> float:
    """上游 `participantRelevance`：未读 / 待回优先，其次最近联系时间。"""
    state = normalize_participant_state(participant.get('state'))
    pending = state['pendingReplyCount'] * 2 + state['unreadMessageCount']
    last_dt = to_date(state['lastUserMessageAt']) or to_date(participant.get('updatedAt'))
    last = dt_ms(last_dt) if last_dt else 0
    return pending * 1_000_000_000 + last


def group_due_intents(intents: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """上游 `groupDueIntents`：把到期意图分批。

    让一个到期回合保持对单条关系私有，同时保证 sweep 开始时**已经到期**的每个计划
    都有机会在下一个 sweep 间隔之前被判断。
    """
    batches: dict[str, list[dict[str, Any]]] = {}
    ordered = sorted(
        intents,
        key=lambda intent: (dt_ms(intent.get('notBefore')), _intent_id(intent)),
    )
    for intent in ordered:
        family = 'agency' if intent.get('type') == 'proactive-check' else 'normal'
        key = '%s|%s' % (intent.get('participantId') or '__global__', family)
        batches.setdefault(key, []).append(intent)
    return list(batches.values())


def _intent_id(intent: dict[str, Any]) -> int:
    intent_id = intent.get('id')
    return intent_id if isinstance(intent, int) and not isinstance(intent, bool) else 0


def resolve_participant_id(explicit: Any, source_entry_ids: Any,
                           entries: list[dict[str, Any]]) -> str:
    """上游 `resolveParticipantId`：显式 participantId 优先，其次源码条目归属。"""
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    found: list[str] = []
    for entry_id in (source_entry_ids or []):
        for entry in entries:
            if entry.get('id') == entry_id and entry.get('participantId'):
                found.append(entry['participantId'])
                break
    return found[0] if found else ''


# =========================================================================== #
# 缓冲回合 dict 的字段读写（Chunk2 建 dict、Chunk3 起跑，见 `_turn_set` 的说明）
# =========================================================================== #

def _turn_get(turn: Any, camel: str, snake: Optional[str] = None) -> Any:
    """读缓冲回合 / 缓冲消息的字段：camelCase 与 snake_case 都认（优先 camelCase）。

    与 `base.pick` 同义，但**不复用 `pick`**：`pick` 住在 `base.py`，而 `base` 依赖本模块，
    反向导入会成环。这里自己走一遍 dict 查找（回合与缓冲消息都是纯内存 dict）。
    """
    if not isinstance(turn, dict):
        return None
    if camel in turn:
        return turn[camel]
    key = snake or re.sub(r'(?<!^)(?=[A-Z])', '_', camel).lower()
    return turn.get(key)


def _turn_set(turn: dict[str, Any], camel: str, snake: str, value: Any) -> Any:
    """写缓冲回合的字段：**两种拼写都写**。

    `base.py`（Chunk0）写/读 camelCase，`config.py` 的 TypedDict 声明的是 snake_case。
    曾经这里"跟随该 dict 已有的拼写"（有 snake 就写 snake，否则写 camel），听起来很干净，
    但**同一个字段的第一次写入者未必是同一个 chunk**：
    `bufferUserNarrative`（Chunk2）建 turn 时只写了 `story_id` / `participant_id` /
    `messages` / `next_revision` / `obsolete_request_ids`，`in_flight_request_id` 是
    `flushBufferedNarrative`（Chunk3）**第一次**写进去的——当时 dict 里没有 snake 键，
    于是它落在 camelCase；而 Chunk2 的 `signalIncomingInterruption` /
    `bufferUserNarrative` 两个守卫直接 `turn.get('in_flight_request_id')` → 拿到 None →
    `shouldSupersedeNarrativeRequest` 永远为假。后果（用户 2026-09-26 00:22 的日志）：
    汐雨. 连发「行吧」「晚安」+ 晚安贴图，贴图到达时上一回合正在跑模型，本该被判过时并与
    贴图合成一个回合，实际被拆成两回合，贴图那一回合她已经"睡着了、没看见"。
    写两种拼写（回合 dict 是纯内存结构、从不落库也不进 payload）彻底消灭这类"写读拼写错位"。
    """
    turn[camel] = value
    if snake != camel:
        turn[snake] = value
    return value

def should_supersede_narrative_request(
    in_flight_request_id: int | None,
    first_message_committed_request_id: int | None,
    obsolete_request_ids: Any,
) -> bool:
    """上游 `shouldSupersedeNarrativeRequest`。

    真实的用户消息被提交之后，已过时的在飞请求不得再把回复发出去。
    """
    return (
        bool(in_flight_request_id)
        and first_message_committed_request_id != in_flight_request_id
        and in_flight_request_id not in obsolete_request_ids
    )


# =========================================================================== #
# 数据库行归一（上游 8145–8190）
# =========================================================================== #

#: 上游 `DATABASE_DATE_FIELDS`：每张表需要物化成时间的列。
#: 本移植版从 `plugin/core/database.py` 的 `TABLES` 读取（列名同为 camelCase），
#: 该模块未落地时回落到这份与上游逐字一致的映射。
DATABASE_DATE_FIELDS: dict[str, list[str]] = {
    'interlude_story': ['cursorAt', 'createdAt', 'updatedAt'],
    'interlude_participant': ['createdAt', 'updatedAt'],
    'interlude_script_entry': ['occurredAt', 'createdAt'],
    'interlude_memory': ['createdAt', 'updatedAt'],
    'interlude_intent': ['notBefore', 'createdAt', 'updatedAt'],
    'interlude_scene': ['startedAt', 'endedAt', 'createdAt', 'updatedAt'],
    'interlude_arc': ['createdAt', 'updatedAt'],
    'interlude_fact': ['lastSeenAt', 'createdAt', 'updatedAt'],
    'interlude_state_patch': ['createdAt', 'appliedAt'],
    'interlude_overlay_snapshot': ['periodStart', 'periodEnd', 'createdAt', 'updatedAt'],
    'interlude_sticker': ['createdAt', 'updatedAt'],
    'interlude_web_observation': ['accessedAt', 'createdAt'],
    'interlude_schedule_preplan': ['createdAt', 'updatedAt'],
}


def database_date_fields(table: str) -> list[str]:
    """该表需要物化成时间的列名。

    优先用 `plugin/core/database.py` 的列定义（单一真相来源，列名同为上游
    camelCase）；该模块暂未落地时回落到 `DATABASE_DATE_FIELDS`（与上游逐字一致）。
    """
    try:
        columns = _database.timestamp_columns(table)
    except (KeyError, AttributeError):
        return list(DATABASE_DATE_FIELDS.get(table, []))
    declared = DATABASE_DATE_FIELDS.get(table)
    if not declared:
        return sorted(columns)
    return [field for field in declared if field in columns]


def normalize_database_row(table: str, value: Any) -> Any:
    """上游 `normalizeDatabaseRow`：把跨 service 边界的数据库行归一化。

    Minato 通常把 timestamp 列物化成 Date 对象；某些驱动与热重载路径会返回
    ISO 字符串，所以在做时间运算或构造 prompt 之前，统一把每一行归一化。

    **列名**逐字保持上游 camelCase（`storyId` / `occurredAt` / `sourceEntryIds` /
    `lastSeenAt` …）：数据库列名本身是持久化 wire format，service 层直接按
    camelCase 读写。

    **`state` 列的值**（json 列已在 `plugin/core/database.py` 里解过一层）用
    `story_state.decode_story_state()`（本移植版唯一权威解码器）再解一层，键名
    保持**本移植版内部约定 snake_case**：state 的实际持久化内容由
    `story_state.encode_story_state()`（同样是 snake_case）写出，`types.StoryState`
    与 `plugin/core/narrator.py` 也按 snake_case 读。上游 TS 的 `state.settingOverlay`
    只出现在**发给模型的 payload**里，由 `narrator_prompts.story_state_for_prompt()`
    在边界处转 camelCase，不要在数据库行上提前转。
    """
    if not is_record(value):
        return value
    row: dict[str, Any] = dict(value)
    for field in database_date_fields(table):
        if row.get(field) is None:
            continue
        row[field] = to_date(row[field])
    if table == 'interlude_story':
        created_at = to_date(row.get('createdAt')) or utc_now()
        updated_at = to_date(row.get('updatedAt')) or created_at
        row['createdAt'] = created_at
        row['updatedAt'] = updated_at
        row['cursorAt'] = to_date(row.get('cursorAt')) or updated_at
        row['state'] = _decode_story_state(row.get('state'))
    elif table == 'interlude_participant':
        row['createdAt'] = to_date(row.get('createdAt')) or utc_now()
        row['updatedAt'] = to_date(row.get('updatedAt')) or row['createdAt']
        row['state'] = normalize_participant_state(row.get('state'))
    return row


def same_timestamp(left: Any, right: Any) -> bool:
    """上游 `sameTimestamp`：两个时间是否在 2 秒内视为同一时刻。"""
    first = to_date(left)
    second = to_date(right)
    return bool(first) and bool(second) and abs(dt_ms(first) - dt_ms(second)) < 2_000


def narrative_cursor(story: dict[str, Any], now: datetime) -> datetime:
    """上游 `narrativeCursor`：损坏 / 未来的游标不得让叙事器把时间往回补。

    只夹住 prompt 区间；正常成功的持久化仍会把存储游标推进到真实墙上时间。
    """
    cursor = to_date(story.get('cursorAt')) or now
    return now if dt_ms(cursor) > dt_ms(now) else cursor


def limit_entries_by_characters(entries: list[dict[str, Any]], limit: float) -> list[dict[str, Any]]:
    """上游 `limitEntriesByCharacters`：从最新条目向前保留到字数上限。"""
    if limit <= 0:
        return []
    used = 0
    selected: list[dict[str, Any]] = []
    # 从最新条目向前保留，保证压缩请求优先看到场景接续点。
    for index in range(len(entries) - 1, -1, -1):
        entry = entries[index]
        if selected and used + len(_str(entry.get('content'))) > limit:
            break
        selected.insert(0, entry)
        used += len(_str(entry.get('content')))
    return selected


def fact_score(fact: dict[str, Any], config: dict[str, Any], query_embedding: list[float] | None = None,
               query: str = '') -> float:
    """上游 `factScore`：事实相关度打分（重要性 / 置信 / 新近 / 语义 / 词法 / 未结）。"""
    embedding = query_embedding or []
    last_seen = to_date(fact.get('lastSeenAt'))
    age_days = max(0.0, (dt_ms(utc_now()) - dt_ms(last_seen)) / (24 * 60 * 60 * 1000)) if last_seen else 0.0
    recency = math.exp(-age_days / 30)
    similarity = cosine_similarity(embedding, fact.get('embedding') or [])
    # 负相似度视为无语义支撑：避免一条无关事实仅因为余弦值数学上落在
    # [-1, 1] 就白拿半份分数。
    semantic = 0.0 if similarity is None else max(0.0, similarity)
    lexical = history_lexical_score(query, _str(fact.get('content')))
    semantic_weight = _number(config_get(config, 'semantic_weight', 'semanticWeight'))
    return (
        _number(fact.get('importance')) * _number(config_get(config, 'fact_importance_weight', 'factImportanceWeight'))
        + _number(fact.get('confidence')) * _number(config_get(config, 'fact_confidence_weight', 'factConfidenceWeight'))
        + recency * _number(config_get(config, 'fact_recency_weight', 'factRecencyWeight'))
        + semantic * semantic_weight
        + lexical * max(1.0, semantic_weight)
        + (1.0 if fact.get('scope') == 'promise' and fact.get('unresolved') else 0.0)
        * _number(config_get(config, 'unresolved_weight', 'unresolvedWeight'))
    )


def history_lexical_score(query: str, content: str) -> float:
    """上游 `historyLexicalScore`：原始剧本与长期事实共用的字面召回通道。

    中文双字组在不需要分词的前提下保留了有用的姓名与物品。
    """
    query_keys = _lexical_recall_keys(query)
    if not query_keys:
        return 0.0
    content_keys = set(_lexical_recall_keys(content))
    overlap = sum(1 for key in query_keys if key in content_keys)
    normalized_query = re.sub(r'[\W_]+', '', query.lower(), flags=re.UNICODE)
    normalized_content = re.sub(r'[\W_]+', '', content.lower(), flags=re.UNICODE)
    phrase = 0.5 if len(normalized_query) >= 3 and normalized_query in normalized_content else 0.0
    return min(1.0, overlap / len(query_keys) + phrase)


def _lexical_recall_keys(text: str) -> list[str]:
    """上游 `lexicalRecallKeys`：英文单词 + 中文双字组，去重、上限 80。"""
    normalized = _str(text).lower()
    words = re.findall(r'[a-z0-9]{3,}', normalized, re.ASCII)
    bigrams: list[str] = []
    for run in re.findall(r'[\u3400-\u9fff]{2,}', normalized, re.UNICODE):
        bigrams.extend(run[index:index + 2] for index in range(max(0, len(run) - 1)))
    seen: list[str] = []
    for key in words + bigrams:
        if key not in seen:
            seen.append(key)
    return seen[:80]


def cosine_similarity(left: list[float], right: list[float]) -> float | None:
    """上游 `cosineSimilarity`：维度不一致 / 零向量返回 `None`。"""
    if not left or len(left) != len(right):
        return None
    dot = 0.0
    left_magnitude = 0.0
    right_magnitude = 0.0
    for index in range(len(left)):
        dot += left[index] * right[index]
        left_magnitude += left[index] * left[index]
        right_magnitude += right[index] * right[index]
    if not left_magnitude or not right_magnitude:
        return None
    return dot / math.sqrt(left_magnitude * right_magnitude)


#: 一个语义过滤回合注入多少条贴纸描述（上游 `SEMANTIC_STICKER_LIMIT`）。
SEMANTIC_STICKER_LIMIT = 12


def rank_sticker_catalog(assets: list[Any], query_embedding: list[float], limit: int) -> list[Any]:
    """上游 `rankStickerCatalog`：带向量的按余弦排序，无向量的补满剩余槽位。"""
    if not query_embedding or len(assets) <= limit:
        return assets
    ranked: list[dict[str, Any]] = []
    for asset in assets:
        score = cosine_similarity(query_embedding, _asset_embedding(asset))
        ranked.append({'asset': asset, 'score': -1.0 if score is None else score})
    ranked.sort(key=lambda item: item['score'], reverse=True)
    return [item['asset'] for item in ranked[:max(1, limit)]]


def _asset_embedding(asset: Any) -> list[float]:
    """上游 `asset.embedding ?? []`（两种拼写都认）。"""
    if isinstance(asset, dict):
        embedding = asset.get('embedding')
        return embedding if isinstance(embedding, list) else []
    embedding = getattr(asset, 'embedding', None)
    return embedding if isinstance(embedding, list) else []


def should_downscale_image(mime_type: str, data_uri: str) -> bool:
    """上游 `shouldDownscaleImage`：只有值得重渲染的静态位图进入缩放流程。"""
    if not re.match(r'^image/(?:jpeg|png|webp)$', _str(mime_type), re.IGNORECASE):
        return False
    parts = _str(data_uri).split(',')
    binary_length = math.floor(len(parts[1] if len(parts) > 1 else '') * 3 / 4)
    return binary_length >= 150 * 1024


# =========================================================================== #
# 实时剧本时间越界守卫（上游 8364–8455）
# =========================================================================== #

#: 模型调用与投递存在分钟级延迟，加上分钟取整：超出 now 这个宽限内的时钟引用
#: 视为「就是现在」，不因网络抖动丢弃整段剧本。
TIME_OVERFLOW_GRACE_MINUTES = 5
#: 12 小时制的歧义视野：now=00:01 时提到「11:58」几乎总是指刚过去的 23:58
#: （午夜前 3 分钟），而不是 11 小时 57 分钟后的未来。朴素前向距离超过该视野的
#: 时钟引用一律按「刚过去的 12 小时制写法」处理，不再判未来。
TIME_FORWARD_HORIZON_MINUTES = 6 * 60
#: 剧本开头的「叙事宣告位」：中文叙事在场景起始处声明时间。只检查这一小段，
#: 中后段的钟点绝大多数是对约定 / 回忆 / 计划的引用，不应作为越界证据。
LIVE_SCRIPT_HEADLINE_CHARS = 30
#: 计划 / 约定语义：钟点作为未来安排被引用时（「八点赶到」「九点前」），不构成越界。
PLAN_SEMANTICS = re.compile(
    r'赶到|约定|答应|要在|得在|之前|以前|打算|计划|准备|约好|说好|出发|来不及|赶不上|预计|大概|左右|还没|尚未',
    re.UNICODE,
)
#: 钟点前后各 8 字的上下文窗口。
CONTEXT_WINDOW = 8

#: 上游 `clocksIn` 的阿拉伯数字钟点正则。
#:
#: 逐字对齐 JS `/(?:^|[^\d])(?:(\d{1,2})[:：](\d{2})|(\d{1,2})点(?:(\d{1,2})分?)?)/g`：
#: Python 的 `\d` 默认就是 Unicode 数字，故显式写成 `[0-9]`；`(?:^|[^0-9])` 的
#: 前瞻语义用 `(?!\d)` 补齐（JS 里 `[^\d]` 会吃掉那个字符，Python 里改用零宽前瞻，
#: 对 `match.group(0)` 的长度与后续切片无影响，但能正确拒绝 "08:40" 被切成 "8:40"）。
_CLOCKS_ARABIC_RE = re.compile(
    r'(?:^|[^0-9])(?:([0-9]{1,2})[:：]([0-9]{2})(?!\d)|([0-9]{1,2})点(?!\d)(?:([0-9]{1,2})(?!\d)分?)?)',
    re.UNICODE,
)
#: 上游 `clocksIn` 的中文数字钟点正则。
_CLOCKS_CHINESE_RE = re.compile(
    r'([零一二三四五六七八九十两]+)点(?:(?:零|([一二三四五六七八九十两]+))分?|半)?',
    re.UNICODE,
)
#: 上游 `第\s*([一二三四五六七八九十\d]+)\s*节`。
_LESSON_STAGE_RE = re.compile(r'第\s*([一二三四五六七八九十0-9]+)\s*节', re.UNICODE)


def _context_around(text: str, index: int, length: int) -> str:
    """上游 `contextAround`：钟点前后各 8 字的上下文窗口。"""
    return text[max(0, index - CONTEXT_WINDOW):min(len(text), index + length + CONTEXT_WINDOW)]


def _clocks_in(text: str) -> list[dict[str, Any]]:
    """上游 `clocksIn`：抽出所有钟点及其上下文窗口。

    `m.start()` / `m.end()` 与 JS `matchAll` 的 `match.index` / `match[0].length`
    逐字对齐——Python 的 `re` 同样不会让未消耗的 lookbehind 影响后续搜索起点，
    这正是上游那段「开头钟点必须落在宣告位内」的判定所依赖的语义。
    """
    found: list[dict[str, Any]] = []
    for match in _CLOCKS_ARABIC_RE.finditer(text):
        hour_text = match.group(1) if match.group(1) is not None else match.group(3)
        minute_text = match.group(2) if match.group(2) is not None else match.group(4)
        hour = int(hour_text)
        minute = int(minute_text) if minute_text is not None else 0
        if hour > 23 or minute > 59:
            continue
        found.append({
            'hour': hour, 'minute': minute,
            'around': _context_around(text, match.start(), len(match.group(0))),
        })
    for match in _CLOCKS_CHINESE_RE.finditer(text):
        hour = _chinese_clock_number(match.group(1))
        minute = _chinese_clock_number(match.group(2)) if match.group(2) else 0
        if hour is None or minute is None or hour > 23 or minute > 59:
            continue
        found.append({
            'hour': hour, 'minute': minute,
            'around': _context_around(text, match.start(), len(match.group(0))),
        })
    return found


def detect_live_script_time_overflow(
    script: Any, phase: str, from_dt: datetime, now: datetime, timezone: str,
    endorsed_clocks: Any = None,
) -> str | None:
    """上游 `detectLiveScriptTimeOverflow`：实时消息的窄口径最后防线。

    它刻意不解释普通散文：只拒绝短窗口内、含显式未来钟点或**多个已完成课程
    阶段**的剧本。自动推进与合法的长追赶窗口不受影响。
    """
    text = _str(script).strip()
    elapsed_minutes = max(0.0, (dt_ms(now) - dt_ms(from_dt)) / (60 * 1000))
    if phase != 'user-message' or not text or elapsed_minutes > 60:
        return None
    endpoint = story_local_time_context(now, timezone)
    now_minutes = endpoint['hour'] * 60 + int(endpoint['time'][3:5])

    def overflow(value: int) -> bool:
        return (
            value > now_minutes + TIME_OVERFLOW_GRACE_MINUTES
            and value - now_minutes <= TIME_FORWARD_HORIZON_MINUTES
        )

    # 用户给出了未来期限（"九点前赶到"）时，其之间的叙事推进被授权：落在
    # (now, maxEndorsed] 区间的钟点一并豁免；endorsed 为空时行为不变。
    has_endorsed = bool(endorsed_clocks) and len(endorsed_clocks) > 0
    endorsed_deadline = max(endorsed_clocks) if has_endorsed else None
    # 只把剧本开头（叙事宣告位）里、且没有计划语义的钟点视为「把叙事时间写过头」。
    for clock in _clocks_in(text[:LIVE_SCRIPT_HEADLINE_CHARS]):
        value = clock['hour'] * 60 + clock['minute']
        if has_endorsed and value in endorsed_clocks:
            continue
        if endorsed_deadline is not None and value > now_minutes and value <= endorsed_deadline:
            continue
        if PLAN_SEMANTICS.search(clock['around']):
            continue
        if overflow(value):
            return 'explicit clock %02d:%02d exceeds %s' % (
                clock['hour'], clock['minute'], endpoint['time'][:5],
            )
    # 全文（含开头）的课程阶段推进不变：多个已完成的课程节 = 时间被写飞。
    lesson_stages = set(_LESSON_STAGE_RE.findall(text))
    if len(lesson_stages) >= 2:
        return 'multiple lesson stages (%s) inside a %d minute live window' % (
            '→'.join(lesson_stages), _js_round(elapsed_minutes),
        )
    return None


# =========================================================================== #
# 上游 8320–8362 的模块级辅助（自动推进间隔 / 休息窗口 / 补写时刻）
# =========================================================================== #


def normalize_follow_up_minutes(values: Any) -> list[float]:
    """上游 `normalizeFollowUpMinutes`：1–240 分钟，去重升序，最多 6 条。"""
    defaults = [10, 20]
    source = values if isinstance(values, list) else defaults
    normalized: list[float] = []
    for value in source:
        number = _number(value)
        if not _finite(number):
            continue
        floored = math.floor(number)
        if floored >= 1 and floored <= 240 and floored not in normalized:
            normalized.append(floored)
    normalized.sort()
    return normalized[:6]


def clock_minutes(value: Any) -> int | None:
    """上游 `clockMinutes`：`HH:MM` → 当日分钟数，非法返回 `None`。"""
    matched = re.match(r'^([0-9]{1,2}):([0-9]{2})$', _str(value).strip(), re.UNICODE)
    if not matched:
        return None
    hour = int(matched.group(1))
    minute = int(matched.group(2))
    if hour < 0 or hour >= 24 or minute < 0 or minute >= 60:
        return None
    return hour * 60 + minute


def active_rest_window(windows: Any, timezone: str, now: datetime) -> dict[str, Any] | None:
    """上游 `activeRestWindow`：找出当前生效的休息窗口。"""
    local_minutes = local_clock_minutes(now, timezone)
    if not isinstance(windows, list):
        return None
    for window in windows:
        if not is_record(window) or not window.get('enabled'):
            continue
        start = clock_minutes(window.get('start'))
        end = clock_minutes(window.get('end'))
        if start is None or end is None:
            continue
        inside = (
            (start <= local_minutes < end)
            if start <= end
            else (local_minutes >= start or local_minutes < end)
        )
        if inside:
            return window
    return None


def automatic_interval_minutes(story: dict[str, Any], now: datetime, config: dict[str, Any]) -> float:
    """上游 `automaticIntervalMinutes`：休息窗口内用该窗口自己的间隔区间。"""
    timezone = (story.get('setting') or {}).get('timezone') if is_record(story.get('setting')) else None
    rest_window = active_rest_window(config_get(config, 'rest_windows', 'restWindows'), _str(timezone), now)
    if rest_window:
        return _random_integer(
            config_get(rest_window, 'min_interval_minutes', 'minIntervalMinutes'),
            config_get(rest_window, 'max_interval_minutes', 'maxIntervalMinutes'),
        )
    jitter = _random_integer(
        -_number(config_get(config, 'jitter_minutes', 'jitterMinutes')),
        _number(config_get(config, 'jitter_minutes', 'jitterMinutes')),
    )
    return max(1.0, _number(config_get(config, 'interval_minutes', 'intervalMinutes')) + jitter)


def _random_integer(minimum: Any, maximum: Any) -> int:
    """上游 `randomInteger`（含两端；非有限输入一律按 0 处理）。"""
    import random
    low = _number(minimum)
    high = _number(maximum)
    low = low if _finite(low) else 0.0
    high = high if _finite(high) else 0.0
    lower = math.floor(min(low, high))
    upper = math.floor(max(low, high))
    return lower + math.floor(random.random() * (upper - lower + 1))


def merge_note(existing: str | None, next_value: Any) -> str | None:
    """上游 `mergeNote`：追加注记，去重，尾部最多保留 6000 字符。"""
    value = clip(next_value, 2_000)
    if not value:
        return existing
    if not existing:
        return value
    if normalize_fact(value) in normalize_fact(existing):
        return existing
    return ('%s\n%s' % (existing, value))[-6_000:]


def patch_claims_match(left: str, right: str) -> bool:
    """上游 `patchClaimsMatch`：允许小幅措辞差异，但避免极短声明误合并。"""
    first = re.sub(r'[，。！？、,.!?；;:：]', '', normalize_fact(left))
    second = re.sub(r'[，。！？、,.!?；;:：]', '', normalize_fact(right))
    if not first or not second:
        return False
    if first == second:
        return True
    return min(len(first), len(second)) >= 8 and (first in second or second in first)


def start_of_utc_window(value: datetime, window_days: float) -> datetime:
    """上游 `startOfUtcWindow`：按 N 天对齐的 UTC 窗口起点。"""
    size = max(1, math.floor(window_days))
    epoch_day = math.floor(dt_ms(value) / (24 * 60 * 60 * 1000))
    return parse_dt(math.floor(epoch_day / size) * size * 24 * 60 * 60 * 1000)


def normalize_major_events(value: Any, patches: list[dict[str, Any]],
                           snapshots: list[dict[str, Any]] | None = None) -> list[str]:
    """上游 `normalizeMajorEvents`：保留历史重大事件 + 模型新报事件，去重后取末 20 条。"""
    model_events = (
        [clip(item, 600) for item in value if isinstance(item, str)]
        if isinstance(value, list) else []
    )
    retained: list[str] = []
    for snapshot in (snapshots or []):
        for item in (snapshot.get('majorEvents') or []):
            retained.append(item)
    for patch in patches:
        if patch.get('impact') == 'major':
            retained.append(clip(patch.get('proposedValue') or patch.get('evidence'), 600))
    combined: list[str] = []
    for item in retained + model_events:
        if item and item not in combined:
            combined.append(item)
    return combined[-20:]
