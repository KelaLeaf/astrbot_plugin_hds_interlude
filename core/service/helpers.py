"""上游 `src/service.ts` 第 7084–8524 行的移植：全部模块级纯函数。

本模块是 移植约定 分解契约里的 `helpers.py`：只含**模块级函数**，
不含 `InterludeService` 的任何成员。`upstream/src/service.ts` 该范围内出现、但实际
定义在别处的辅助函数（`isRecord` / `clip` / `clampNumber` / `toDate` / `cosineSimilarity`
等）按上游同样归属本模块。

## 键名约定

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
平台能力走 `Transport` 抽象（见移植约定–7）。
"""

from __future__ import annotations

import json
import math
import os
import re
from datetime import datetime
from typing import Any, Optional
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
    'normalize_media_segments',
    'describe_image_media',
    'describe_card_media',
    'media_kind_label',
    'card_media_label',
    'guess_audio_format',
    # ---- 表情 / 表态 ----
    'calibrated_native_face_willingness',
    'stable_sticker_asset_id',
    'normalize_allowed_reactions',
    # ---- 自动收藏入站表情包（本移植版新增）----
    'collectible_sticker_kind',
    'verify_sticker_image_bytes',
    'collected_sticker_asset_id',
    'COLLECTED_STICKER_DIR',
    'COLLECTIBLE_STICKER_KINDS',
    'STICKER_CANDIDATE_KIND',
    'WIRE_MEDIA_KINDS',
    'wire_media_kind',
    'STICKER_IMAGE_MIMES',
    'STICKER_FILE_SUFFIX',
    # ---- 插件数据目录 / 表情库根目录（本移植版新增，§54）----
    'STICKER_DEFAULT_DIRECTORY',
    'host_data_dir',
    'sticker_root_from',
    #: 行里的 `filePath` 与盘上文件对不上时的三条候选路径 / 按哈希兜底（§56）。
    'sticker_path_inside',
    'sticker_path_candidates',
    'sticker_hash_prefixes',
    'find_sticker_by_hash',
    'STICKER_HASH_PREFIX_LENGTH',
    'STICKER_HASH_MIN_LENGTH',
    # ---- 表情库分组 / 上传（本移植版新增，§47）----
    'COLLECTED_STICKER_GROUP_ID',
    'COLLECTED_STICKER_GROUP_NAME',
    'COLLECTED_STICKER_GROUP_DESCRIPTION',
    'STICKER_GROUP_NAME_MAX_BYTES',
    'STICKER_GROUP_NAME_FORBIDDEN',
    'STICKER_GROUP_RESERVED_NAMES',
    'STICKER_GROUP_ROOT_BUCKET',
    'STICKER_GROUP_DESCRIPTION_MAX',
    'STICKER_NAME_MAX',
    'STICKER_DESCRIPTION_MAX',
    'safe_sticker_group_name',
    'sticker_group_name_problem',
    'uploaded_sticker_asset_id',
    # ---- 两级表情选择 / 描述时定组（本移植版新增，§48）----
    'STICKER_GROUP_INLINE_ASSET_LIMIT',
    'STICKER_GROUP_ITEM_LIMIT',
    'STICKER_FOLLOW_UP_MAX_PER_TURN',
    'STICKER_FOLLOW_UP_TIMEOUT_SECONDS',
    'STICKER_AUTO_GROUP_MAX_GROUPS',
    'STICKER_AUTO_GROUP_MAX_NEW_PER_DAY',
    'STICKER_AUTO_GROUP_WINDOW_HOURS',
    'sticker_group_directory',
    'sticker_group_directory_ids',
    'sticker_group_items',
    'parse_sticker_group_choice',
    'parse_sticker_selection_receipt',
    'parse_sticker_auto_group',
    'visible_reply_text',
    'apply_sticker_follow_up_content',
    # ---- 第二层：普通图片的模型判定（本移植版新增，§45.7）----
    'GUESS_STICKER_KIND',
    'GUESS_STICKER_MAX_DIMENSION',
    'GUESS_STICKER_MAX_ASPECT',
    'GUESS_STICKER_MIN_CONFIDENCE',
    'STICKER_GUESS_KINDS',
    'guess_image_dimensions',
    'sticker_media_signal',
    'sticker_guess_candidate',
    'sticker_guess_result',
    'sticker_disabled_by',
    'sticker_not_sticker_verdict',
    'STICKER_NOT_A_STICKER',
    'STICKER_NAME_RE',
    'STICKER_NAME_PLACEHOLDERS',
    'STICKER_SIGNAL_NONE',
    'STICKER_SIGNAL_NAME',
    'STICKER_SIGNAL_GIF',
    'STICKER_SIGNAL_ALPHA',
    'STICKER_SIGNAL_SHAPE',
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
    # ---- 群聊历史图片证据（1.0.1-rc31）----
    'DEFAULT_HISTORICAL_IMAGE_LIMIT',
    'GROUP_IMAGE_REF_MAX_BYTES',
    'GROUP_IMAGE_FILE_PREFIX',
    'group_image_refs_for_storage',
    'normalize_stored_group_image_ref',
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
        # 两级表情选择 / 描述时定组（v1.8.4，§48）：**默认真**，只有显式 false 才关。
        # `is not False` 是刻意的：缺失 / NULL / 字符串一律按默认（开）走——
        # 这两把闸的默认行为就是今天已经验收过的那套（关掉即回到平铺目录）。
        'group_selection': config_get(configured, 'group_selection', 'groupSelection') is not False,
        'auto_group': config_get(configured, 'auto_group', 'autoGroup') is not False,
        # 描述时判"不是表情包"就停用（v1.8.4，§50）：**默认真**，只有显式 false 才关。
        # 同一把尺子（`is not False`）：缺失 / NULL / 字符串一律按默认（开）走；
        # 关掉它就只写描述、一个字的启用状态都不动。
        'auto_disable': config_get(configured, 'auto_disable', 'autoDisable') is not False,
        'directory': str(directory if directory else STICKER_DEFAULT_DIRECTORY).strip(),
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
    只有服务端转码（NapCat `get_record` 的 `out_format`）才能变成模型可读的载荷。
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
    # 私聊语音），或者带一个我们无法转码的额外 url 字段。
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


# ---------------------------------------------------------------------------
# 入站媒体标记 → 叙述者可见的语义标签（受控偏离，见 `docs/PORTING_NOTES.md` §29）
#
# 上游把一切图片都记成 `[图片]`：实拍照片、截图、收藏表情包、QQ 商城表情、
# 小程序卡片在文本里长得一模一样，模型只能靠猜。这里把适配器从原始段里捞回来的
# 种类（`kind` / `summary`）翻译成稳定的中文标签。**普通图片仍然是 `[图片]`**
# （上游行为），只有确实能分出来的种类才换词。
# ---------------------------------------------------------------------------

#: `<img kind="...">` → 标签。`market` 只作为兜底（商城表情正常走 `<mface>`）。
IMAGE_MEDIA_KIND_LABELS = {
    'sticker': '[表情包]',
    'animated': '[动画表情]',
    'market': '[QQ 商城表情]',
}


def _media_attr(attributes: Any, key: str) -> str:
    """从标签属性串里读一个属性（属性串来自适配器，永远当成不可信文本）。"""
    found = re.search(r'%s=["\']([^"\']*)["\']' % re.escape(key), _str(attributes), re.IGNORECASE)
    return found.group(1).strip() if found else ''


def describe_image_media(attributes: Any) -> str:
    """`<img>` → `[图片]` / `[表情包]` / `[动画表情]` / `[QQ 商城表情]`（文本入口）。"""
    return media_kind_label(_media_attr(attributes, 'kind'), _media_attr(attributes, 'summary'))


def media_kind_label(kind: Any, summary: Any) -> str:
    """媒体种类 + 平台原文 → 给人/模型看的标签（**唯一判据，两个入口共用**）。

    入口一：`describe_image_media()`（文本里的 `<img kind=… summary=…>`，只用来出标签）；
    入口二：`chunk3` 读**结构化媒体表**（`session.media`，种类与来源都在里面，
    §46）。标签必须逐字一致，所以两处都走这一个函数。
    """
    kind_text = _str(kind).strip().lower()
    summary_text = _str(summary).strip()
    # 平台给的 summary 比我们推测的 kind 更具体：`[动画表情]` 说明它还会动。
    if '动画' in summary_text:
        return '[动画表情]'
    if kind_text in IMAGE_MEDIA_KIND_LABELS:
        return IMAGE_MEDIA_KIND_LABELS[kind_text]
    if '表情' in summary_text:
        return '[表情包]'
    return '[图片]'


#: wire（`currentEvent.attachments[].kind` 与侧端视觉的 `mediaKind`）**只认**这几个值 ——
#: 提示词就是按这份枚举教模型的。
WIRE_MEDIA_KINDS = frozenset({'image', 'sticker', 'animated', 'market', 'card'})


def wire_media_kind(kind: Any) -> str:
    """内部媒体种类 → **wire 词汇表**：候选档对外就是普通图（§49.1）。

    为什么必须收口：`sticker-candidate` 只说明"平台标了候选、结构检查还没做" ——
    它既不是"观测到的表情"，也不是提示词教过的种类（提示词枚举的是
    `image / sticker / animated / market / card`），漏出去等于让模型读一个没定义的词。
    其余种类**原样透传**（`photo` 这种外部写法不许被悄悄改掉）。
    """
    text = _str(kind).strip().lower()
    if not text or text == STICKER_CANDIDATE_KIND:
        return GUESS_STICKER_KIND  # `image`
    return text


def describe_card_media(attributes: Any) -> str:
    """`<card>`（QQ 小程序 / 分享卡片）→ `[QQ小程序：标题]` / `[分享卡片：标题]`（文本入口）。

    卡片必须带上**是什么**：只有 `<card/>` 时模型只能含糊成"他发了点什么"。
    """
    return card_media_label(
        _media_attr(attributes, 'app'),
        _media_attr(attributes, 'title') or _media_attr(attributes, 'prompt'),
    )


def card_media_label(app: Any, title: Any) -> str:
    """卡片属性 → 标签（**唯一判据，两个入口共用**，同 `media_kind_label`）。"""
    app_text = _str(app).strip()
    title_text = _str(title).strip()
    mini = app_text.startswith('com.tencent.miniapp') or app_text.startswith('110')
    if title_text:
        return ('[QQ小程序：%s]' if mini else '[分享卡片：%s]') % title_text[:40]
    return '[QQ小程序]' if mini else '[分享卡片]'


def normalize_media_segments(content: Any) -> str:
    """把入站的图片 / 表情 / 卡片标记换成带种类的语义标签。

    比上游的"全记 `[图片]`"多一层：`kind` 与 `summary` 由适配层从 OneBot 原始段里
    捞回来（见 `astrbot_bridge.raw_media_hints`），到这里才变成模型看得懂的词。
    原生表情仍走 `normalize_qq_native_face_segments`（唯一入口，别在这里重复处理）。
    """
    text = normalize_qq_native_face_segments(content)
    text = re.sub(
        r'<(?:img|image)\b([^>]*)/?>(?:</(?:img|image)>)?',
        lambda match: describe_image_media(match.group(1)),
        text, flags=re.IGNORECASE,
    )
    text = re.sub(
        r'<card\b([^>]*)/?>(?:</card>)?',
        lambda match: describe_card_media(match.group(1)),
        text, flags=re.IGNORECASE,
    )
    # CQ 码没有 kind/summary（原始段的种类信息在适配器那层就取了），保守回落 `[图片]`。
    text = re.sub(r'\[CQ:image,[^\]]*\]', '[图片]', text, flags=re.IGNORECASE)
    return text


def describe_group_attachments(content: Any) -> str:
    """上游 `describeGroupAttachments`：把群聊入站的附件标记转成事实占位。

    群聊入站没有原生附件通道：保留「发过什么」的信息，URL 污水不进群上下文，
    也不再被模型复述。
    """
    # 注意先替换 `<file ...>` 再删闭合标签：上游用 `name|file|title` 抽文件名。
    text = normalize_media_segments(_str(content))
    text = re.sub(r'<(?:record|audio)\b[^>]*/?>', '[语音]', text, flags=re.IGNORECASE)
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
# 插件数据目录 / 表情库根目录（**全工程唯一判据**；受控偏离，见 §54）
#
# "收藏写进了 A 目录、扫描与控制台看的是 B 目录"是这类功能的头号故障（真机症状：
# 库里能看到这条素材，缩略图却 404）。所以路径推导只留这两个纯函数：谁要根目录都
# 调它们，一处漂移就一处修。
# =========================================================================== #

#: 表情库根目录的**默认相对路径**（相对插件数据目录）。
#: `_conf_schema.json` 的 `default`、`config.CONFIG_DEFAULTS`、服务层与适配层的回落
#: 全都照这一处读——再多写一份字面量，就是第二个真相。
STICKER_DEFAULT_DIRECTORY = 'data/hds-interlude/stickers'


def host_data_dir(host: Any) -> str:
    """宿主对象 → 插件数据目录（核心侧与适配层共用的**唯一判据**）。

    三种情形都要有定义，且**绝不抛**：

    * 生产形状：`host.ctx.base_dir`（`ServiceBase` 存的是 `self.ctx`）；
    * 裸宿主：`ctx` 没有 `base_dir`（或压根没有 `ctx`）→ 退到宿主注入的
      `host.context.base_dir`；
    * 单测的 `ServiceChunkN.__new__` 宿主 → 最后退到自己的 `host.base_dir`。

    一个都拿不到 → 回空串。**空串不是"当前工作目录"**：调用方必须把它当"根目录
    不可知"处理（可见 warn / 直接失败），`os.path.abspath('')` 那种回落会静默写到
    cwd 去——那正是"文件不在数据目录里"的来源。
    """
    for holder in (getattr(host, 'ctx', None), getattr(host, 'context', None), None):
        base = getattr(host, 'base_dir', '') if holder is None else getattr(holder, 'base_dir', '')
        if base:
            return str(base)
    return ''


def sticker_root_from(data_dir: Any, directory: Any = '') -> str:
    """插件数据目录 + `stickers.directory` → 表情库根目录的绝对路径（唯一判据）。

    收藏写入、扫盘、控制台读图、删组搬迁**全部**从这里取根。数据目录拿不到时回
    空串（**不是** cwd 相对路径），由调用方按"根目录不可知"可见地失败。
    """
    base = _str(data_dir).strip()
    if not base:
        return ''
    relative = _str(directory).strip() or STICKER_DEFAULT_DIRECTORY
    return os.path.abspath(os.path.join(base, relative))


def sticker_path_inside(root: Any, path: Any) -> bool:
    """`path` 是不是**落在库根里**（跨盘 / 空根 / 相对根一律 `False`，**绝不抛**）。

    `os.path.commonpath` 在"绝对 + 相对混用"与"不同盘符"时抛 `ValueError`——库根拿不到
    （空串）时它就是这么炸的，而那正是最需要给出诊断的时刻（真机：取图 500，前端只看到
    "取不到图"）。所以这里把它压成布尔：**越界 / 判不出来 = 不许读**。
    """
    base = _str(root).strip()
    target = _str(path).strip()
    if not base or not target:
        return False
    try:
        return os.path.commonpath(
            [os.path.abspath(target), os.path.abspath(base)],
        ) == os.path.abspath(base)
    except ValueError:
        return False


def sticker_path_candidates(root: Any, data_dir: Any, name: Any, raw: Any = '') -> tuple[str, ...]:
    """`filePath` → 依次尝试的绝对路径（去重、保序）。**根仍然只有调用方给的那一个**。

    三种写法都要能读——"库里有行、取不到图"的成因就在这三种之间：

    1. **相对库根**：当前唯一写入方（`chunk2.store_collected_sticker` 的
       `collected/<hash>.jpg`）与扫盘（`Cat/a.png`）用的写法；
    2. **绝对路径**：继承来的旧数据（别的版本 / 手工塞进库的行）；
    3. **相对插件数据目录**：多带了一截库根前缀的写法
       （`data/hds-interlude/stickers/collected/x.jpg`），旧版本或手工写入可能是这一种。

    这里只拼候选，**归属判定与"哪个真实存在"由调用方做**（`sticker_path_inside` +
    `os.path.isfile`）：拼出来的绝对路径不等于可以读。
    """
    base = _str(root).strip()
    if not base:
        # 没有根就**一个候选都不给**：空根拼出来的相对路径是"进程当前目录"，
        # 那正是本工程反复踩过的那口井（调用方按"根不可知"显式失败）。
        return ()
    relative = _str(name).strip().replace('\\', '/').lstrip('/')
    candidates: list[str] = []

    def add(value: Any) -> None:
        text = _str(value).strip()
        if not text:
            return
        absolute = os.path.abspath(text)
        if absolute not in candidates:
            candidates.append(absolute)

    if relative:
        add(os.path.join(base, relative))
    raw_text = _str(raw).strip()
    if raw_text and os.path.isabs(raw_text):
        add(raw_text)
    base_dir = _str(data_dir).strip()
    if relative and base and base_dir:
        prefix = os.path.relpath(base, base_dir).replace('\\', '/').strip('/')
        if prefix and prefix != '.' and relative.startswith(prefix + '/'):
            add(os.path.join(base, relative[len(prefix) + 1:]))
    return tuple(candidates)


#: "按内容哈希找回"认的十六进制前缀长度（= 自动收藏落地名里那一段 `digest[:32]`）。
STICKER_HASH_PREFIX_LENGTH = 32

#: 十六进制哈希的判定（大小写都认）。长度不足 `STICKER_HASH_MIN_LENGTH` 的一律不参与。
_HEX_RE = re.compile(r'^[0-9a-fA-F]+$')

#: "按哈希找回"接受的最短前缀。低于这个长度多半是**文件名**而不是哈希，
#: 拿它去扫全库会撞出一堆别人的素材。
STICKER_HASH_MIN_LENGTH = 16


def sticker_hash_prefixes(hash_value: Any, file_path: Any = '') -> tuple[str, ...]:
    """`hash` 列 / `filePath` → 可用于"按哈希找回"的文件名前缀（去重、保序）。

    自动收藏的落地名是 `<内容哈希前 32 位><扩展名>`，而 `hash` 列记的是**整条** sha256，
    所以前缀取 `hash[:32]`；`filePath` 的 basename 往往就是那个名字（旧版本把分组目录
    削掉过、写方也可能只记了文件名），所以它自己也算一个候选。
    """
    out: list[str] = []
    raw_names = (
        _str(hash_value).strip(),
        os.path.splitext(os.path.basename(_str(file_path).replace('\\', '/')))[0].strip(),
    )
    for raw in raw_names:
        text = raw.lower()
        if len(text) < STICKER_HASH_MIN_LENGTH or not _HEX_RE.match(text):
            continue
        prefix = text[:STICKER_HASH_PREFIX_LENGTH]
        if prefix not in out:
            out.append(prefix)
    return tuple(out)


def find_sticker_by_hash(root: Any, hash_value: Any, file_path: Any = '') -> str:
    """在库根下按**内容哈希前缀**找回文件：找到回绝对路径，没有回空串。

    只在 `<根>` 与 `<根>/<一级子目录>` 里找（分组就是一级子目录，扫盘也只收一级），
    扩展名按"同名前缀 + 任意后缀"匹配——行里记的扩展名可能是错的 / 被改过的。
    这是"行与盘对不上"的**兜底**：根仍然只由 `sticker_root_from()` 给出，这里不另算根。
    """
    base = _str(root).strip()
    if not base or not os.path.isdir(base):
        return ''
    prefixes = sticker_hash_prefixes(hash_value, file_path)
    if not prefixes:
        return ''
    directories = [base]
    try:
        with os.scandir(base) as entries:
            subdirs = sorted(
                entry.path for entry in entries
                if entry.is_dir(follow_symlinks=False)
            )
    except OSError:
        subdirs = []
    directories.extend(subdirs)
    for directory in directories:
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for name in names:
            stem, extension = os.path.splitext(name)
            if not extension or stem.lower() not in prefixes:
                continue
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate):
                return os.path.abspath(candidate)
    return ''


# =========================================================================== #
# 自动收藏入站表情包（本移植版新增，受控偏离；见 `docs/PORTING_NOTES.md` §45）
#
# 判据是**适配层观测到的种类**，不是名字、不是画面内容、更不是"看着像表情包"。
# 这些是纯函数：种类的归一化、字节的图片校验、落地文件名的派生。
# =========================================================================== #

#: 表情库根目录下自动收藏落地的子目录（`scan_sticker_library` 会把它当分组）。
COLLECTED_STICKER_DIR = 'collected'

#: 「值得收藏」的入站种类。**`image` 不在里面**（普通照片 / 截图一律不收），
#: `card`（小程序 / 分享卡片）不在里面（它不是图片，也没有可下载的图片字节）。
COLLECTIBLE_STICKER_KINDS = frozenset({'sticker', 'animated', 'market'})

#: **只在内部流通**的媒体种类：平台标了"表情包候选"（OneBot `sub_type` 2/3/7），
#: 但"像不像表情包"的结构检查还没做完（§49.1 的第二档）。
#: 它**绝不能**出现在模型看得见的 wire 上 —— 出口统一走 `wire_media_kind()`。
STICKER_CANDIDATE_KIND = 'sticker-candidate'

#: 自动收藏接受的图片 MIME（校验用的魔数就在 `guess_image_mime` 里，一份实现两处用）。
STICKER_IMAGE_MIMES = frozenset({'image/png', 'image/jpeg', 'image/gif', 'image/webp'})

#: 落地文件的扩展名（按**嗅探出来的** MIME 取，不按入站 URL 的后缀——URL 是对方给的）。
STICKER_FILE_SUFFIX = {
    'image/png': '.png',
    'image/jpeg': '.jpg',
    'image/gif': '.gif',
    'image/webp': '.webp',
}

# --------------------------------------------------------------------------- #
# 表情库分组（本移植版新增，见 `docs/PORTING_NOTES.md` §47）
#
# **磁盘目录结构是分组的唯一事实来源**：`interlude_sticker.group` 一直就是
# "素材落在哪个子目录"的字符串（上游 `database.ts:164` 本来就有这一列 +
# `group` 索引），子目录名 = 分组名。本移植版只补了两件上游没有的东西：
# **描述**（给模型看的那一份）与**建组 / 改名 / 上传的操作面**。
# 表 `interlude_sticker_groups` 因此只存"描述与时间"，键就是目录名。
# --------------------------------------------------------------------------- #

#: 内置默认组的目录名（= `groupId`）。它**永远**出现在分组列表里（哪怕没有素材、
#: 表里也没有行），否则自动收藏与手动上传的素材无所属。
COLLECTED_STICKER_GROUP_ID = COLLECTED_STICKER_DIR

#: 内置默认组的**显示名**。这是全库**唯一**一个"显示名 ≠ 目录名"的特例：
#: `collected` 是老库里已有素材的目录（改目录名 = 让已有素材集体搬家），
#: 所以它的显示名固定成「未整理」；它同时**不许改名**（改名 = 换目录）。
#:
#: v1.8.4 起叫「未整理」：它不是一个"风格分组"，而是**落脚点**——自动收藏与
#: 手动上传都先落这里，等着被（人或模型）归组。旧名「自动收藏」只描述了来源，
#: 会让模型以为"这一组 = 自动收来的"，从而永远不去动它。
COLLECTED_STICKER_GROUP_NAME = '未整理'

#: 内置默认组的**模型可见描述**（用户在控制台写了描述就覆盖它）。它同时也是给模型的
#: 那句"这一组怎么用"：没有它，模型只看到一个叫「未整理」的桶，不会想到把里面的素材
#: 归到更合适的组里（§48 乙）。
COLLECTED_STICKER_GROUP_DESCRIPTION = '自动收藏与手动上传都先落这里，还没归组。'

#: 分组名 = **目录名**，所以规则按文件系统来（不是"标识符"）：
#:
#: * 字节上限（不是字符数）：文件系统按字节算，100 字节 ≈ 33 个汉字；
#: * 禁字符集：路径分隔符与各平台的保留字符（Windows 上这几个真的建不出目录）；
#: * 首字符不许 `.`（`..` / 隐藏目录因此**结构性**进不来）、首尾空白一律去掉、
#:   空名字拒、控制字符拒。
#:
#: **允许**中日韩文字、字母、数字、空格、`-` `_` `.` `（）` 这类常见符号——
#: 中文目录名是这个库的常态，上一版"ASCII only"的白名单会把它整个挡在门外。
STICKER_GROUP_NAME_MAX_BYTES = 100
STICKER_GROUP_NAME_FORBIDDEN = frozenset('/\\:*?"<>|')

#: 根目录素材的桶名：`scan_sticker_library()` 给**没有子目录**的文件打的 `group` 值
#: （`<根>/a.png` → `group = 'default'`）。它**不是目录**，所以它同时是**保留名**——
#: 见下。一处定义：扫描写它、保留名集合也读它。
STICKER_GROUP_ROOT_BUCKET = 'default'

#: **保留名**：这些名字已经被"别的桶"占用，不能当**新写入的**分组名（新建 / 改名 /
#: 移动 / 上传的目标一律 400）。现在只有一个：
#:
#: * `default` = 根目录素材的桶（上面那个常量）。允许建同名目录的话，`default` 这个
#:   计数会把"散在根目录的"和"`default/` 里的"混在一起——用户根本分不清谁是谁。
#:
#: ⚠️ 只在**写入**这一侧判：**既有**的 `default/` 目录照常收录、照常显示、照常写描述
#: （规则管的是"新写入的名字"，不是"让别人的素材消失"，与扫描那条同一条纪律）。
STICKER_GROUP_RESERVED_NAMES = frozenset({STICKER_GROUP_ROOT_BUCKET})

#: 分组描述上限（它要进提示词：50 组 × 500 字就是上限量级）。
STICKER_GROUP_DESCRIPTION_MAX = 500

#: 一条素材的短名 / 描述上限。单一事实源在这里：控制台（`console_api`）与服务层
#: （`chunk2` 的上传管线）读同一份数字，不各抄一个（抄一份就会漂移一次）。
STICKER_NAME_MAX = 60
STICKER_DESCRIPTION_MAX = 2_000

# --------------------------------------------------------------------------- #
# 两级表情选择 / 描述时定组（本移植版新增，见 `docs/PORTING_NOTES.md` §48）
#
# 模型想发表情时的两步：先看**分组目录**（只有 id / 名字 / 描述 / 条数）点名一组，
# 宿主再把该组的条目给它挑。省一次模型调用的办法是"条目本来就少"——直接内联。
# --------------------------------------------------------------------------- #

#: 内联阈值：目录里的条目**总数**不超过它就整份平铺（不花第二次调用）。
STICKER_GROUP_INLINE_ASSET_LIMIT = 8

#: 追问时一次最多给模型看多少条候选（与 `catalog_limit` 同量级；超出的截断）。
STICKER_GROUP_ITEM_LIMIT = 40

#: 一个回合最多额外问几次模型。**1 是铁律**：追问的回执里再点名一组也不许接着问，
#: 否则"模型想说话 → 宿主无限追问"会变成一个死循环。
STICKER_FOLLOW_UP_MAX_PER_TURN = 1

#: 追问的总超时（秒）。追问是**锦上添花**：超时按"没有候选"继续，
#: 正文照发、绝不让一次表情选择把整个回合卡住。
STICKER_FOLLOW_UP_TIMEOUT_SECONDS = 20.0

#: 自动定组的分组总数上限（模型看到的目录再大也不该长过这个数）。
STICKER_AUTO_GROUP_MAX_GROUPS = 30

#: 自动定组的**新建速率**上限与窗口：`STICKER_AUTO_GROUP_MAX_NEW_PER_DAY` 个 /
#: `STICKER_AUTO_GROUP_WINDOW_HOURS` 小时（滚动窗口，看注册行的 `createdAt`）。
#: 用滚动窗口而不是"自然日"是因为 `Database` 只有等值 `where`（没有范围算子，见坑 54 一带），
#: 而读回来的注册表本来就在手上——在 Python 侧数一遍比加一张计数表省得多。
STICKER_AUTO_GROUP_MAX_NEW_PER_DAY = 5
STICKER_AUTO_GROUP_WINDOW_HOURS = 24


def sticker_group_name_problem(value: Any, *, reserved: bool = False) -> str:
    """分组名（= 目录名）不合法时回**中文原因**，合法回空串。

    这是命名规则的**唯一定义处**：新建分组 / 改名 / 上传 / 移动 / 自动定组全走它，
    控制台与服务层读的是同一句话（两处各写一份判据，迟早会一处宽一处严）。
    扫描遇到不合规的既有目录名**照常收录**（只记 debug）——规则管的是"新写入的名字"，
    不是"删掉别人已经放好的文件"。

    首尾空白**归一化**（去掉）而不是拒绝：写进磁盘的名字里因此永远没有首尾空白，
    比"拒一次、让用户自己回去删空格"更省事，也不留"名字看起来一样、实际两个目录"的坑。
    中间的换行 / 制表符则直接拒——它会让一个目录名在界面上显示成两行。

    `reserved=True` 是**写入侧**的加严：连保留名（`default` = 根目录素材的桶）也拒。
    给既有的 `default/` 目录写描述、或把它改名成别的名字时**不传**它（历史目录要能管理）。
    """
    text = _str(value).strip()
    if not text:
        return '分组名不能为空'
    if reserved and text in STICKER_GROUP_RESERVED_NAMES:
        return '「%s」是保留名（那是根目录素材用的桶，不是一个分组），不能当分组名' % text
    if text.startswith('.'):
        return '分组名不能以「.」开头'
    if len(text.encode('utf-8')) > STICKER_GROUP_NAME_MAX_BYTES:
        return '分组名最长 %d 字节（一个汉字算 3 字节）' % STICKER_GROUP_NAME_MAX_BYTES
    for character in text:
        if character in STICKER_GROUP_NAME_FORBIDDEN:
            return '分组名不能包含 %s' % character
        if ord(character) < 32 or ord(character) == 127:
            return '分组名不能包含控制字符'
        if character.isspace() and character != ' ':
            return '分组名不能包含换行或制表符'
    return ''


def safe_sticker_group_name(value: Any, *, reserved: bool = False) -> str:
    """分组名归一化：合法回**去首尾空白后**的名字，不合法回空串（调用方据此拒绝）。

    "拿不准 = 拒绝"：这个名字会被拼进文件路径（`<表情库根>/<目录名>/<hash>.<ext>`），
    所以 `/` `\\` `:` 等一律拒；`..` 因为"首字符不许 `.`"**结构性**进不来，不靠调用方
    记得过滤。落盘前控制台 / 服务层还会再复验一次 `commonpath`（第二道闸）。
    `reserved=True` 见 `sticker_group_name_problem()`。
    """
    text = _str(value).strip()
    if sticker_group_name_problem(text, reserved=reserved):
        return ''
    return text


def _dual_field(value: Any, camel: str, snake: Optional[str] = None) -> Any:
    """读**外部**（模型原样返回 / 跨 chunk 传递）的 dict：两种拼写都认，**优先 camelCase**。

    与 `config_get()` 的方向刻意相反：配置层由 `normalize_config()` 归一成 snake_case，
    而模型回的是 camelCase（键名法）。本模块只读模型与数据库那一侧，所以这里 camel 优先。
    """
    if not isinstance(value, dict):
        return None
    if camel in value:
        return value[camel]
    if snake is not None and snake in value:
        return value[snake]
    return None


def sticker_group_directory(
    assets: Any, rows: Any, limit: Optional[int] = STICKER_AUTO_GROUP_MAX_GROUPS,
    include_empty: bool = False, directories: Any = None,
) -> list[dict[str, Any]]:
    """模型可见的**分组目录**：`[{groupId, name, description, count}]`（**不列条目**）。

    这是 §48 的**唯一事实源**：两级选择的第一次 payload 与「描述时定组」的提示词
    都读这一份。目录文本写两份，迟早会漂移成"模型挑组时看到的描述"与"整理时看到的
    描述"不一样——那正是这一轮要避免的事。

    **分组的名字就是目录名**（`groupId`）。`rows` 是描述表 `interlude_sticker_groups`
    的行——表里没有行不是"未注册"，只是**这一组还没有描述**；组的成员资格来自
    "有素材挂着它"或"磁盘上真有这个目录"（`directories`）。

    口径：

    * `include_empty=False`（甲：**要把表情发出去**）：只列真的有条目的组——选一个空组
      只能空手而归，白烧一次调用；
    * `include_empty=True`（乙：**要把素材归进去**）：没有条目的组也要列（描述行里的、
      以及 `directories` 里那些刚建好的空目录）——不然第一条素材永远进不了一个
      刚建好、还没东西的分组（这是这一功能的入口本身）；
    * 内置默认组永远排第一，名字固定 `COLLECTED_STICKER_GROUP_NAME`（唯一一个
      "显示名 ≠ 目录名"的特例），描述缺省用 `COLLECTED_STICKER_GROUP_DESCRIPTION`
      （用户在控制台写过就听用户的）；
    * 空 `group`（"未分组"桶）**不列**：它不是合法目标（目录名规则会拒）；
    * `limit=None` = 不截断（"这个组合法吗"的校验用得上，提示词那边一定带上限）。
    """
    counts: dict[str, int] = {}
    for asset in assets or []:
        if not isinstance(asset, dict):
            continue
        group_id = _str(asset.get('group')).strip()
        if not group_id:
            continue
        counts[group_id] = counts.get(group_id, 0) + 1
    described: dict[str, dict[str, Any]] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        group_id = _str(row.get('groupId')).strip()
        if group_id and group_id not in described:
            described[group_id] = row
    on_disk = {
        _str(name).strip() for name in (directories or []) if _str(name).strip()
    }
    items: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(group_id: str, name: str, description: str) -> None:
        seen.add(group_id)
        items.append({
            'groupId': group_id, 'name': name, 'description': description,
            'count': counts.get(group_id, 0),
        })

    def describe(group_id: str) -> str:
        row = described.get(group_id) or {}
        return _str(row.get('description')).strip()

    if counts.get(COLLECTED_STICKER_GROUP_ID) or include_empty:
        add(
            COLLECTED_STICKER_GROUP_ID,
            COLLECTED_STICKER_GROUP_NAME,
            describe(COLLECTED_STICKER_GROUP_ID) or COLLECTED_STICKER_GROUP_DESCRIPTION,
        )
    # 描述表按 `createdAt` 升序（与控制台列表同一条规矩），同刻按目录名定序保证可复现。
    for group_id in sorted(described, key=lambda key: (_str(described[key].get('createdAt')), key)):
        if group_id in seen or not (counts.get(group_id) or include_empty):
            continue
        add(group_id, group_id, describe(group_id))
    # 剩下的：磁盘上有目录的、以及有素材挂着的（都没描述行）——按目录名排序。
    for group_id in sorted(
        key for key in (set(counts) | on_disk)
        if key and key not in seen and (counts.get(key) or include_empty)
    ):
        add(group_id, group_id, '')
    if limit is None:
        return items
    return items[:max(1, int(limit))]


def sticker_group_directory_ids(directory: Any) -> set[str]:
    """目录里出现过的 groupId 集合（"这一组存在吗"的**唯一**判据）。"""
    return {
        _str(item.get('groupId')).strip()
        for item in (directory or [])
        if isinstance(item, dict) and _str(item.get('groupId')).strip()
    }


def sticker_group_items(
    assets: Any, group_id: Any, limit: int = STICKER_GROUP_ITEM_LIMIT,
) -> list[dict[str, Any]]:
    """某分组里**可以让模型挑**的条目：`[{assetId, description}]`（**不含图字节**）。

    没描述的条目不进候选：模型只能靠描述挑图，给一条没有描述的等于让它瞎猜
    （而投递时真会发出去那张图）。
    """
    wanted = _str(group_id).strip()
    if not wanted:
        return []
    items: list[dict[str, Any]] = []
    for asset in assets or []:
        if not isinstance(asset, dict):
            continue
        if _str(asset.get('group')).strip() != wanted:
            continue
        asset_id = _str(asset.get('assetId')).strip()
        description = _str(asset.get('description')).strip()
        if not asset_id or not description:
            continue
        items.append({'assetId': asset_id, 'description': description})
        if len(items) >= max(1, int(limit)):
            break
    return items


def parse_sticker_group_choice(decision: Any) -> str:
    """模型**点名了哪个分组**：`localMedia.stickerGroupId` 优先，其次顶层同名字段。

    双读 camelCase / snake_case（键名法：模型原样返回的 JSON 两种拼写都认）。
    拿不准一律回空串——调用方据此走"没有候选"的兜底，绝不替模型猜一个组。
    """
    local_media = _dual_field(decision, 'localMedia', 'local_media')
    for container in (local_media, decision):
        if not isinstance(container, dict):
            continue
        for key in ('stickerGroupId', 'sticker_group_id'):
            if key in container:
                candidate = safe_sticker_group_name(container.get(key))
                if candidate:
                    return candidate
    return ''


def parse_sticker_selection_receipt(payload: Any) -> dict[str, Any]:
    """追问回执 → `{assetId, content, willingness}`；**只归一化，不做决定**。

    发不发那张图仍由 `resolve_sticker()`（意愿阈值 + 目录成员资格）一处判——
    这里多一个判据，就会出现"两处都以为对方会拦"的空档。
    """
    if not isinstance(payload, dict):
        return {}
    asset_id = ''
    for camel, snake in (('stickerAssetId', 'sticker_asset_id'), ('assetId', 'asset_id')):
        value = _dual_field(payload, camel, snake)
        if isinstance(value, str) and value.strip():
            asset_id = value.strip()
            break
    content = ''
    value = _dual_field(payload, 'content')
    if isinstance(value, str) and value.strip():
        content = value
    return {
        'assetId': asset_id,
        'content': content,
        'willingness': _dual_field(payload, 'willingness'),
    }


def parse_sticker_auto_group(receipt: Any) -> Optional[dict[str, Any]]:
    """描述回执里的分组选择 → `{'mode': 'existing'|'new', …}`；没有 / 坏形状回 `None`。

    形状契约（§48 乙）：

    ```jsonc
    {"description": "…", "group": {"existing": "<groupId>"}}
    {"description": "…", "group": {"new": {"name": "…", "description": "…"}}}
    ```

    这里只做**形状与字面**校验：名字压掉控制字符与多余空白、过一遍与人工建组**同一条**
    命名规则（`sticker_group_name_problem`：允许中文，禁路径分隔符等）；描述压掉控制字符、
    截到 `STICKER_GROUP_DESCRIPTION_MAX`。名字会变成**磁盘目录名**，所以这条规则不能松。
    **新建**那一支还要连**保留名**（`default`）一起拒（`reserved=True`）——那是根目录素材
    的桶，不是分组；**点名已有组**那一支不传它（模型指着一个既有的 `default` 桶说
    "归这里"是合法的）。
    "这个组到底存不存在 / 该不该新建"是策略，在服务层判（那里才知道磁盘与描述表）。
    """
    raw = _dual_field(receipt, 'group')
    if not isinstance(raw, dict):
        return None
    existing = _clean_group_text(_dual_field(raw, 'existing'))
    if existing and not sticker_group_name_problem(existing):
        return {'mode': 'existing', 'groupId': existing}
    new = _dual_field(raw, 'new')
    if isinstance(new, dict):
        name = _clean_group_text(_dual_field(new, 'name'))
        if name and not sticker_group_name_problem(name, reserved=True):
            return {
                'mode': 'new',
                'name': name,
                'description': _clean_group_text(_dual_field(new, 'description'))[
                    :STICKER_GROUP_DESCRIPTION_MAX
                ],
            }
    return None


def _clean_group_text(value: Any) -> str:
    """模型给的分组名 / 描述：控制字符换空格、压缩空白、去首尾（一行可读文本）。"""
    text = re.sub(r'[\x00-\x1f\x7f]+', ' ', _str(value))
    return re.sub(r'\s+', ' ', text).strip()


def visible_reply_text(decision: Any) -> str:
    """本回合**已经写好的可见正文**（群回复优先，其次 `interaction.reply`）。

    只有 `mode == 'immediate'` 才算"要说的话"：`none` / `deferred` 是模型明确的
    沉默或延后，追问回执不许把它们变成一条消息。
    """
    if not isinstance(decision, dict):
        return ''
    group_reply = _dual_field(decision, 'groupReply', 'group_reply')
    if isinstance(group_reply, dict) and group_reply.get('mode') == 'immediate':
        text = group_reply.get('content')
        if isinstance(text, str) and text.strip():
            return text
    interaction = _dual_field(decision, 'interaction')
    reply = interaction.get('reply') if isinstance(interaction, dict) else None
    if isinstance(reply, dict) and reply.get('mode') == 'immediate':
        text = reply.get('content')
        if isinstance(text, str) and text.strip():
            return text
    return ''


def apply_sticker_follow_up_content(decision: Any, content: Any) -> bool:
    """把追问回执里的正文**补**进 decision 的空正文处；没补成回 `False`。

    **正文以第一段为准**（§48.1）：第一段带着完整叙事上下文写出来的，第二段手里只有
    "分组条目 + 那一句话"——让它重写等于用信息更少的一次调用覆盖信息更多的一次，
    文本质量风险 > 收益。所以这里**只填空**：`mode == 'immediate'`（本来就要说话）
    但正文是空 / 全空白时，才用回执里的正文补进去；第一段已经写了正文，一个字都不动
    （返回 `False`，调用方据它记 debug）。`mode == 'none'` 是模型明确的沉默，同样不补。
    """
    text = _str(content).strip()
    if not text or not isinstance(decision, dict):
        return False
    group_reply = _dual_field(decision, 'groupReply', 'group_reply')
    if (
        isinstance(group_reply, dict) and group_reply.get('mode') == 'immediate'
        and not _str(group_reply.get('content')).strip()
    ):
        group_reply['content'] = text
        return True
    interaction = _dual_field(decision, 'interaction')
    reply = interaction.get('reply') if isinstance(interaction, dict) else None
    if (
        isinstance(reply, dict) and reply.get('mode') == 'immediate'
        and not _str(reply.get('content')).strip()
    ):
        reply['content'] = text
        return True
    return False


#: 自动收藏资产的命名空间词（`assetId` 里**恰好出现一次**，判据见 `collected_sticker_asset_id`）。
STICKER_ASSET_NAMESPACE = 'sticker'


def uploaded_sticker_asset_id(content_hash: Any) -> str:
    """上传素材的 `assetId`：`upload-<哈希前 16 位>`。

    与自动收藏（`sticker-…`）分居两个命名空间，日志与控制台里一眼可分来源；
    同一个文件重复上传**必然**被内容哈希去重掉，所以这个 id 不会撞。
    """
    digest = re.sub(r'[^a-fA-F0-9]', '', _str(content_hash)).lower()[:16] or 'unhashed'
    return 'upload-%s' % digest


def collectible_sticker_kind(value: Any) -> str:
    """入站附件的种类 → 「可收藏的种类」；**拿不准一律回空串**。

    这是本功能的红线（用户点名的那条）：只有适配层从 OneBot 原始段**观测到**的
    `sticker` / `animated` / `market` 才算数。缺失、未知、`image`、`card`、
    大小写噪声、`None`、非字符串——全部回空串，调用方据此跳过并记 debug。

    读外部输入两种拼写都认（`kind` / `kinds` 这种多值形态不在契约里，不认）。
    """
    text = _str(value).strip().lower()
    return text if text in COLLECTIBLE_STICKER_KINDS else ''


def verify_sticker_image_bytes(data: Any) -> str:
    """字节是不是真图片？是就回 MIME（`image/…`），否则回空串。

    要求「字节要真的验过是图片」：`guess_image_mime` 认的正是 gif / png / jpg / webp
    四种魔数，嗅不出来（HTML 错误页、纯文本、空字节、别的格式）一律回空串。
    空结果与 `random` 之类的碰撞无关——这里只看头几个字节。
    """
    if not data:
        return ''
    mime = guess_image_mime(data)
    return mime if mime in STICKER_IMAGE_MIMES else ''


def collected_sticker_asset_id(name: Any, content_hash: Any) -> str:
    """自动收藏资产的 `assetId`：`sticker-<名字>-<哈希片段>`（名字为空时省掉那一段）。

    带 `sticker-` 前缀是为了让 id **自述来源**（控制台的 `source` 字段据此派生，
    不必给表加一列），也让自动资产与磁盘扫描出来的资产在日志里一眼可分。
    哈希片段让同内容永远得到同一个 id —— `assetId` 有唯一索引，撞了就是写失败。

    **命名空间只出现一次**（这条判据只有这一处）：调用方传进来的"名字"往往就是
    入站种类（`sticker` / `animated` / `market`），也可能本身就是一份旧 id。早先
    直接把名字接在前缀后面，于是最常见的 `kind='sticker'` 落库成
    `sticker-sticker-<hash>` —— 真机上模型把它原样抄回来，id 自述来源的用途反而成了噪音。
    """
    digest = re.sub(r'[^a-fA-F0-9]', '', _str(content_hash)).lower()[:16] or 'unhashed'
    stem = re.sub(r'[^a-zA-Z0-9_-]+', '-', _str(name).strip())[:80].strip('-') or 'inbound'
    # 把名字里打头的命名空间词剥干净（`sticker` / `sticker-…` / 旧的双前缀 id 都算），
    # 免得拼出 `sticker-sticker-…`。
    while stem == STICKER_ASSET_NAMESPACE or stem.startswith(STICKER_ASSET_NAMESPACE + '-'):
        stem = stem[len(STICKER_ASSET_NAMESPACE):].lstrip('-')
    if not stem:
        return ('%s-%s' % (STICKER_ASSET_NAMESPACE, digest))[:255]
    return ('%s-%s-%s' % (STICKER_ASSET_NAMESPACE, stem, digest))[:255]


# =========================================================================== #
# 第二层判据：让识图模型确认"这张普通图片到底是不是表情包"（v1.8.0，受控偏离 §45.7）
#
# 第一层 `collectible_sticker_kind()` 只认**适配层观测到的**三类；这一层处理那些
# **被当成普通图片发过来**的表情包。两层互不越权：
#
# * 第一层认了的种类**直接收**，永远不走模型（也永远不经过下面任何函数）；
# * 这一层只处理 `kind == 'image'`，**永远不能否决第一层**；
# * 这一层先做便宜的预筛（纯 Python 解析图片头），值得问才调模型。
# =========================================================================== #

#: 第二层唯一处理的入站种类（普通图片）。别的种类要么第一层已经收了，要么不收。
GUESS_STICKER_KIND = 'image'

#: —— 以下三个是**启发式阈值，不是平台规则** ——
#:
#: 聊天软件里的表情包几乎都是小尺寸、近方形的图；实拍照片与截图通常是长宽比明显的
#: 大图。这三个数只用来把"明显不像"的图挡在模型调用之前（省 token 是这条功能的成败点），
#: 判定权在模型回执与用户的开关，不在它们身上。
GUESS_STICKER_MAX_DIMENSION = 512
GUESS_STICKER_MAX_ASPECT = 1.6
#: 判成"是表情包"的最低置信度（同样是**启发式**）：低于它按"拿不准"处理 = 不收。
GUESS_STICKER_MIN_CONFIDENCE = 0.6

#: 模型回执里认得的 `kind`（提示词逐字要求这几档）；白名单外一律归 `other`。
STICKER_GUESS_KINDS = ('meme', 'reaction', 'caption_photo', 'photo', 'screenshot', 'other')

#: 描述回执里"这不是表情包"的判定名（§50）。与第二层共用同一个置信度门槛。
STICKER_NOT_A_STICKER = 'not-a-sticker'


def sticker_disabled_by(value: Any) -> str:
    """归一新列 `disabledBy`：只认 `''` / `'model'` / `'manual'`，其余一律 `''`。

    `'model'` = 模型读描述时判定它不是表情包而停用；`'manual'` = 人对启用状态表过态
    （停用**或**启用都算）—— 见 `interlude_sticker` 的列注释与 §50。
    """
    text = _str(value).strip().lower()
    return text if text in ('model', 'manual') else ''


def sticker_not_sticker_verdict(value: Any) -> bool:
    """描述回执 → "这行不是表情包，应当停用"？**拿不准一律 False（不动）**（§50）。

    收的充要条件（都在这一处，别在服务层再判一遍）：

    * 回执是对象，且 `is_sticker` **显式是布尔 `False`**（缺字段 / 字符串 / `None` 都不算）；
    * `confidence` 是数字且 `>= GUESS_STICKER_MIN_CONFIDENCE`（与第二层同一把尺子）。

    与 `sticker_guess_result()` 的分工：那个回答"**收不收**"（第二层），这个回答
    "**要不要停用**"（描述之后）；两者共用同一个置信度常量，但方向相反、都不接受
    "拿不准"。回执坏 / 超时 / 没配模型时调用方压根走不到这里，判据本身也回 `False`。
    """
    if not isinstance(value, dict):
        return False
    if value.get('is_sticker') is not False:
        return False
    confidence = value.get('confidence')
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        return False
    return float(confidence) >= GUESS_STICKER_MIN_CONFIDENCE

#: JPEG 里带尺寸的段（`SOF0`…`SOF15`，跳过 `DHT`=0xC4 / `JPG`=0xC8 / `DAC`=0xCC）。
_JPEG_SOF_MARKERS = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF},
)


def guess_image_dimensions(data: Any) -> 'tuple[int, int] | None':
    """按**图片头**解析 `(宽, 高)`；认不出来回 `None`（**不引入 Pillow**）。

    只认自动收藏放行的四种格式的常见头：PNG `IHDR` / GIF 逻辑屏幕 / JPEG `SOF*` /
    WebP `VP8X`、`VP8 `、`VP8L`。为什么不用 Pillow：它是**可选依赖**（`requirements.txt`
    里 try-import 降级），把"能不能判定"绑在可选依赖上不值当——与 §45.1 里
    "去重不做 dHash"是同一条理由。

    解析不出来（截断 / 冷门变体 / 不是这四种）回 `None`，调用方按"拿不准"处理。
    """
    if not data or len(data) < 16:
        return None
    if bytes(data[:8]) == b'\x89PNG\r\n\x1a\n':
        return _png_dimensions(data)
    if bytes(data[:6]) in (b'GIF87a', b'GIF89a'):
        width = int.from_bytes(data[6:8], 'little')
        height = int.from_bytes(data[8:10], 'little')
        return (width, height) if width and height else None
    if bytes(data[:2]) == b'\xff\xd8':
        return _jpeg_dimensions(data)
    if bytes(data[:4]) == b'RIFF' and bytes(data[8:12]) == b'WEBP':
        return _webp_dimensions(data)
    return None


def _png_dimensions(data: Any) -> 'tuple[int, int] | None':
    """PNG：8 字节签名 + 4 字节块长 + `IHDR`，宽高各 4 字节**大端**（偏移 16 / 20）。"""
    if len(data) < 24 or bytes(data[12:16]) != b'IHDR':
        return None
    width = int.from_bytes(data[16:20], 'big')
    height = int.from_bytes(data[20:24], 'big')
    return (width, height) if width and height else None


def _png_has_alpha(data: Any) -> bool:
    """PNG 带透明通道？（色彩类型 4=灰+alpha / 6=RGBA，或有 `tRNS` 块）。

    `tRNS` 按规范必须在 `IDAT` 之前，所以扫到 `IDAT` / `IEND` 就可以停。
    """
    if len(data) < 26:
        return False
    if data[25] in (4, 6):
        return True
    index = 8
    while index + 8 <= len(data):
        length = int.from_bytes(data[index:index + 4], 'big')
        name = bytes(data[index + 4:index + 8])
        if name == b'tRNS':
            return True
        if name in (b'IDAT', b'IEND'):
            break
        index += 12 + length
    return False


def _jpeg_dimensions(data: Any) -> 'tuple[int, int] | None':
    """JPEG：扫到 `SOF*` 段读高 / 宽（各 2 字节大端，偏移 +5 / +7）。"""
    index = 2
    size = len(data)
    while index + 4 <= size:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        # 0xFF 填充、0x00 转义、TEM(0x01) 与 RST0..RST7 / SOI 都没有长度字段。
        if marker in (0xFF, 0x00, 0x01) or 0xD0 <= marker <= 0xD8:
            index += 2
            continue
        if marker == 0xDA:  # SOS：之后是压缩数据，不会再有段头
            return None
        length = int.from_bytes(data[index + 2:index + 4], 'big')
        if length < 2:
            return None
        if marker in _JPEG_SOF_MARKERS:
            if index + 9 > size:
                return None
            height = int.from_bytes(data[index + 5:index + 7], 'big')
            width = int.from_bytes(data[index + 7:index + 9], 'big')
            return (width, height) if width and height else None
        index += 2 + length
    return None


def _webp_dimensions(data: Any) -> 'tuple[int, int] | None':
    """WebP：`VP8X`（扩展）/ `VP8 `（有损）/ `VP8L`（无损）三种头各读各的。

    RIFF 头 12 字节，块负载从偏移 20 开始；三种块的尺寸字段位置与位宽都不同。
    """
    if len(data) < 20:
        return None
    chunk = bytes(data[12:16])
    if chunk == b'VP8X':
        # 负载 = flags(1) + reserved(3) + (宽-1)(3, 小端) + (高-1)(3, 小端)。
        if len(data) < 30:
            return None
        width = int.from_bytes(data[24:27], 'little') + 1
        height = int.from_bytes(data[27:30], 'little') + 1
        return (width, height)
    if chunk == b'VP8 ':
        # 帧头：3 字节 frame tag + 3 字节起始码，然后各 16 位（有效 14 位）。
        if len(data) < 30 or bytes(data[23:26]) != b'\x9d\x01\x2a':
            return None
        width = int.from_bytes(data[26:28], 'little') & 0x3FFF
        height = int.from_bytes(data[28:30], 'little') & 0x3FFF
        return (width, height) if width and height else None
    if chunk == b'VP8L':
        if len(data) < 25 or data[20] != 0x2F:  # 25 字节够读满 14+14 位；0x2F 是无损签名
            return None
        bits = int.from_bytes(data[21:25], 'little')
        return ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
    return None


#: 表情包的"名字"形状：平台给表情名时是**方括号包起来的**（`[中午好]`）。
#: `[图片]` 是**普通图的占位**（NapCat `packet/message/element.ts:372` 对 `picSubType === 0`
#: 写的就是它），所以它不算"名字" —— `sub_type=7 + summary=[图片]` 必须**不收**。
STICKER_NAME_PLACEHOLDERS = frozenset({'[图片]'})
STICKER_NAME_RE = re.compile(r'^\[[^\[\]]{1,32}\]$')

#: 结构信号名（诊断用；也是"它为什么被当成表情包候选"的唯一解释）。
STICKER_SIGNAL_NONE = ''
STICKER_SIGNAL_NAME = 'name'
STICKER_SIGNAL_GIF = 'gif'
STICKER_SIGNAL_ALPHA = 'alpha'
STICKER_SIGNAL_SHAPE = 'shape'


def sticker_media_signal(name: Any = '', mime_type: Any = '', data: Any = None) -> str:
    """这张图"像不像表情包"的**结构信号** → 命中的信号名（都不命中回空串）。

    从便宜到贵，**字节是可选的** —— §49.1 的三档表正是靠这一点把"不用下载"与
    "要下载"分开：

    1. `name`：方括号包起来的平台命名（`[中午好]`）—— **入站就有，不用下载**；
       `[图片]` 这种普通图占位不算（`STICKER_NAME_PLACEHOLDERS`）；
    2. `mime_type` / `data`：GIF（聊天里几乎只有动图 / 表情用途）；
    3. `data`：带 alpha 的 PNG（表情通常是透明底）；
    4. `data`：近方形 + 两边都不大（表情包的典型尺寸）。

    阈值与判据都是**启发式**（`GUESS_STICKER_*`），不是平台规则。这个函数是
    "像不像表情包"的**唯一实现**，两处共用（§49.1）：

    * 第一层·候选档：`astrbot_bridge._image_media_kind()` 先用**只有名字**的那一半
      （入站、零下载）判一次；剩下的（GIF / alpha / 尺寸）在 `chunk2` 拿到字节后判；
    * 第二层：`sticker_guess_candidate()` 是它在"值不值得花一次识图调用"上的薄包装。
    """
    text = _str(name).strip()
    if text and text not in STICKER_NAME_PLACEHOLDERS and STICKER_NAME_RE.match(text):
        return STICKER_SIGNAL_NAME
    mime = _str(mime_type).strip().lower()
    if mime == 'image/gif':
        return STICKER_SIGNAL_GIF
    if mime == 'image/png' and _png_has_alpha(data or b''):
        return STICKER_SIGNAL_ALPHA
    dimensions = guess_image_dimensions(data)
    if dimensions is None:
        return STICKER_SIGNAL_NONE
    width, height = dimensions
    if not width or not height:
        return STICKER_SIGNAL_NONE
    if max(width, height) > GUESS_STICKER_MAX_DIMENSION:
        return STICKER_SIGNAL_NONE
    longest, shortest = max(width, height), min(width, height)
    return STICKER_SIGNAL_SHAPE if longest <= shortest * GUESS_STICKER_MAX_ASPECT else STICKER_SIGNAL_NONE


def sticker_guess_candidate(data: Any, mime_type: Any = '') -> bool:
    """**便宜的预筛**：这张图值不值得花一次识图调用？

    只排除"明显不像表情包"的：GIF（聊天里几乎只有动图 / 表情用途）、带 alpha 的
    PNG（表情通常是透明底），或者"近方形 + 两边都小"（表情包的典型尺寸）。
    大图、长宽比明显像照片 / 截图的，以及**解析不出宽高**的，一律回 False——
    "拿不准就不收"在这里等价于"拿不准就不花钱问"。

    这些阈值都是**启发式**（见上面的常量），不是平台规则；真正的判定权在
    `sticker_guess_result()` 与用户开关手里。

    ⚠️ 实现**委托** `sticker_media_signal()`（§49.1 收口）：候选档与这一层用的是
    同一把尺子，改阈值只改一处；这里的入参没有"名字"，所以名字信号天然不参与。
    """
    return bool(sticker_media_signal(mime_type=mime_type, data=data))


def sticker_guess_result(value: Any) -> 'dict[str, Any] | None':
    """模型回执 → 判定结果；**"收"的充要条件只有这一处**（第二层）。

    收：`is_sticker == true` **且** `confidence >= GUESS_STICKER_MIN_CONFIDENCE`。
    其余全部回 `None`（= 不收）：不是对象 / 缺 `is_sticker` / 不是布尔 / 缺 `confidence` /
    置信度不是数 / 置信度不够。阈值是启发式常量。

    `is_sticker` 这个键名**逐字保持**（提示词就是这么要求的，模型照这个键回）；
    从外部读入按本仓库的规矩双读 `isSticker`，万一中转站改写了键名也认。
    `kind` 只做白名单归一（不认识 → `other`），它**不参与**收不收的判断。
    """
    if not isinstance(value, dict):
        return None
    flag = value.get('is_sticker', value.get('isSticker'))
    if flag is not True:
        return None
    confidence = value.get('confidence')
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        return None
    if float(confidence) < GUESS_STICKER_MIN_CONFIDENCE:
        return None
    kind = _str(value.get('kind')).strip().lower()
    description = value.get('description')
    return {
        'is_sticker': True,
        'kind': kind if kind in STICKER_GUESS_KINDS else 'other',
        'confidence': float(confidence),
        'description': description.strip()[:180] if isinstance(description, str) else '',
    }


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
    content = normalize_media_segments(value)
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


# =========================================================================== #
# 群聊历史图片证据（上游 1.0.1-rc31）
# =========================================================================== #

#: 一次群聊主叙事默认回流几张历史图片（上游 `index.ts:444`
#: `historicalImageLimit: Schema.natural().min(0).max(6).default(3)`）。**0 = 关闭**。
#: ⚠️ 上界不在这边拍：上游那个 `max(6)` 与 `Math.min(6, …)` 按本项目 v1.9.7 的新规矩
#: 撤掉，配多少读多少（见 `docs/PORTING_NOTES.md` §78）。
DEFAULT_HISTORICAL_IMAGE_LIMIT = 3

#: 单条历史图片引用的大小上限（逐字 = 上游 `groupImageRefsForStorage` 的 8 MiB）。
GROUP_IMAGE_REF_MAX_BYTES = 8 * 1024 * 1024

#: `onebot-file:` 前缀 = "这个坐标要去当前端点的 `getImage(file)` 回源"。
#: 其余引用一律按 URL 处理（`sourceType: 'url'`），与上游同一判据。
GROUP_IMAGE_FILE_PREFIX = 'onebot-file:'

#: 内联图片**永不落库**：持久化层只存可重新获取的引用（URL / OneBot file），
#: 不存 `data:image/…;base64,…` 这种回合级长度的二进制串。
GROUP_IMAGE_DATA_URI_RE = re.compile(r'^data:image/', re.IGNORECASE)


def group_image_refs_for_storage(sources: Any) -> list[dict[str, Any]]:
    """上游 `groupImageRefsForStorage(sources)`（`src/service.ts:9780`）。

    `[来源字符串]` → `[{'source', 'ordinal', 'sourceType'}]`（**camelCase**：这是写进
    `interlude_script_entry.metadata` 的持久化形状，与既有群元数据同一拼写）。

    跳过空值、`data:image/…` 与超过 8 MiB 的串；`sourceType` 只有两种 ——
    `onebot-file:` 前缀算 `file`（回源时走当前端点的 `getImage(file)`），
    其余算 `url`。
    """
    refs: list[dict[str, Any]] = []
    for ordinal, source in enumerate(sources if isinstance(sources, (list, tuple)) else []):
        value = _str(source).strip()
        if not value or GROUP_IMAGE_DATA_URI_RE.match(value) or len(value) > GROUP_IMAGE_REF_MAX_BYTES:
            continue
        source_type = 'file' if value.startswith(GROUP_IMAGE_FILE_PREFIX) else 'url'
        if source_type == 'file' and not value[len(GROUP_IMAGE_FILE_PREFIX):].strip():
            continue
        refs.append({'source': value, 'ordinal': ordinal, 'sourceType': source_type})
    return refs


def normalize_stored_group_image_ref(raw: Any) -> Optional[dict[str, Any]]:
    """读一条落库的 `groupImageRefs` 项（上游 `groupMessages` 里的那段校验）。

    与 `group_image_refs_for_storage` 是**同一条判据的两端**：写的时候按这一套过滤，
    读的时候再按同一套收一遍（老库、手改过的 metadata、别的版本写下的行都可能不干净）。
    不合规返回 `None`，由调用方跳过。键名双读（`sourceType` / `source_type`）。
    """
    if not isinstance(raw, dict):
        return None
    source = _str(raw.get('source')).strip()
    if not source or GROUP_IMAGE_DATA_URI_RE.match(source) or len(source) > GROUP_IMAGE_REF_MAX_BYTES:
        return None
    source_type = raw.get('sourceType', raw.get('source_type'))
    if source_type not in ('file', 'url'):
        return None
    return {'source': source, 'ordinal': raw.get('ordinal'), 'sourceType': source_type}


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


#: `reply.mode` 的别名容错集（上游 1.0.1-rc15）：弱模型把 mode 写成这几个词。
_REPLY_MODE_ALIASES = (None, 'text', 'send', 'reply', 'message')


def coerce_reply_mode(mode: Any, has_content: bool) -> Optional[str]:
    """把模型写的 `reply.mode` 归一到 `none|immediate|delayed`；认不出时返回 `None`。

    **判据一处**（v1.2.9 的 `seen` 就是在这上面踩过一次）：`normalize_interaction`
    （真正决定发不发）与 `_has_structured_interaction`（决定要不要抛弃草稿重写）
    必须共用同一套宽容——两边各写一份，就会出现"归一化之后完全合法的回复被判定成
    结构化可见回复缺失、白重写一次"。见 `docs/PORTING_NOTES.md` §79。
    """
    if not isinstance(mode, str):
        mode = None
    if mode in ('none', 'immediate', 'delayed'):
        return mode
    # 带了内容 + 别名（含缺键）→ 按 immediate 处理；缺键且无内容才是真的沉默。
    if has_content and mode in _REPLY_MODE_ALIASES:
        return 'immediate'
    if mode is None:
        return 'none'
    return None


def _has_structured_interaction(value: Any) -> bool:
    # 这是恢复判定，读的是**归一化之前**的 decision：只要 `normalize_interaction`
    # 之后会产出一条可见回复，这里就必须判 True，否则一个合法回复会被白扔、整篇重写
    # （`seen` 的宽容见 PORTING_NOTES 的 rc28 条目；`mode` 的宽容见 §79）。
    if not is_record(value) or not is_record(value.get('reply')):
        return False
    reply = value['reply']
    content = reply.get('content')
    # `seen` 只是"读没读到这条消息"的信息性字段，`normalize_interaction` 对它的类型
    # 一律宽恕（缺省/字符串/数字都按已读处理），判定也必须一样。
    # `mode` 走 `coerce_reply_mode`（与 `normalize_interaction` 同一份容错）：
    # 模型把 immediate 写成 text/send/reply/message 时，归一化会照常发出这条回复，
    # 判定却在这里把它当成"缺失"→ 整篇白重写。
    mode = coerce_reply_mode(reply.get('mode'), isinstance(content, str) and bool(content.strip()))
    if mode is None:
        return False
    if mode == 'none':
        # `mode:'none'` 有两种来源：模型真的决定不回（正常），以及**声明了 immediate 但
        # 引用的 `<say>` 动作落地不了**（`resolve_authored_actions` 退成 none 并留下
        # `unresolved_action_id`）。后者是坏回合，遇到就沿用既有的「重写一次」机制，
        # 否则用户看到的是"她读了却不回"（上游的静默语义；本移植版刻意偏离，见
        # 移植说明）。
        return not reply.get('unresolved_action_id')
    if reply.get('unresolved_action_id'):
        return False
    if not isinstance(content, str) or not content.strip():
        return False
    if mode == 'immediate':
        return True
    return isinstance(reply.get('sendAt'), str) and bool(reply['sendAt'].strip())


#: 上游 `detectMessageRepetition`：最多看 8 批、触发要求 bubbles>=2 且连续 >=2 批。
_REPETITION_MAX_BATCHES = 8


def _safe_bubble_count(value: Any) -> Optional[int]:
    """上游 `safeCount`：非负安全整数才算数（bool 不算）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def detect_message_repetition(entries: Any) -> Optional[dict[str, int]]:
    """上游 1.0.1-rc18 `detectMessageRepetition(entries)`：条数锚定检测。

    倒序把 `character-message` 归批：**批次首领的投递元数据是权威**
    （`bubbleIndex == 0` 且 `bubbleCount` 合法），其余 `character-message` 累计成
    回退批次；任何其它 kind 的条目都会截断当前批次。尾部连续 >=2 批同为 x 条（x>=2）
    时返回 `{'bubbles': x, 'consecutive': n}`，否则 `None`（单条习惯 x=1 永不触发）。

    我方把气泡元数据**平铺**在 `script_entry.metadata` 里（见 `delivery.py` 的
    `message_event_reference`），而上游是 `metadata.scriptEvent.bubbleIndex/bubbleCount`；
    这里两种形状都读（优先上游嵌套形态）。
    """
    if not isinstance(entries, list):
        return None
    batches: list[int] = []
    pending = 0
    for entry in reversed(entries):
        if len(batches) >= _REPETITION_MAX_BATCHES:
            break
        if not isinstance(entry, dict) or entry.get('kind') != 'character-message':
            if pending:
                batches.append(pending)
                pending = 0
            continue
        metadata = entry.get('metadata')
        metadata = metadata if isinstance(metadata, dict) else {}
        nested = metadata.get('scriptEvent', metadata.get('script_event'))
        nested = nested if isinstance(nested, dict) else {}
        bubble_count = _safe_bubble_count(
            nested.get('bubbleCount', nested.get('bubble_count')) if nested
            else metadata.get('bubbleCount', metadata.get('bubble_count'))
        )
        bubble_index = _safe_bubble_count(
            nested.get('bubbleIndex', nested.get('bubble_index')) if nested
            else metadata.get('bubbleIndex', metadata.get('bubble_index'))
        )
        if bubble_count is not None and bubble_index == 0:
            batches.append(bubble_count)
            pending = 0
            continue
        pending += 1
    if pending and len(batches) < _REPETITION_MAX_BATCHES:
        batches.append(pending)
    if not batches:
        return None
    bubbles = batches[0]
    if not isinstance(bubbles, int) or isinstance(bubbles, bool) or bubbles < 2:
        return None
    consecutive = 1
    while consecutive < len(batches) and batches[consecutive] == bubbles:
        consecutive += 1
    return {'bubbles': bubbles, 'consecutive': consecutive} if consecutive >= 2 else None


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


#: 承诺处置的**动作词**词表：模型写的同义词一律映射到上游那三个词。
#: 上游只认 `outcome`（`service.ts:10598`），而真机（2026-10-05 12:30 / 12:51 / 13:31
#: 三次「即将处理到期计划」）模型写的是 `status: "fulfilled"` —— 形状只差一个键名，
#: 于是三次都被静默丢掉、承诺永远结不清。读外部输入两种拼写都认（AGENTS 坑 41/46/53/65）。
_FOLLOW_UP_OUTCOME_ALIASES: dict[str, str] = {
    'fulfilled': 'fulfilled', 'done': 'fulfilled', 'completed': 'fulfilled', 'complete': 'fulfilled',
    'rescheduled': 'rescheduled', 'reschedule': 'rescheduled', 'delayed': 'rescheduled',
    'cancelled': 'cancelled', 'canceled': 'cancelled', 'cancel': 'cancelled',
}


def read_follow_up_resolution(item: Any) -> tuple[Optional[dict[str, Any]], str]:
    """读一条承诺处置：``(归一化处置, 丢弃原因)``；可用时原因是空串。

    **判据只有这一处**：`normalize_follow_up_resolutions`（收下可用的）与
    `follow_up_resolution_problems`（说出为什么丢）都是它的视图——两边各写一套判据
    正是「一个判通过、另一个判不通过」的温床。
    """
    if not is_record(item):
        return None, '不是对象'
    raw_id = item.get('id')
    intent_id: Optional[int] = None
    if isinstance(raw_id, int) and not isinstance(raw_id, bool) and _is_integer(raw_id) and raw_id > 0:
        intent_id = int(raw_id)
    elif isinstance(raw_id, str) and raw_id.strip().isdigit():
        # 模型偶尔把 id 写成字符串（"153"）：它指的还是同一个正整数主键。
        intent_id = int(raw_id.strip())
    if intent_id is None:
        return None, 'id 必须是正整数（收到 %r）' % (raw_id,)
    outcome = ''
    for key in ('outcome', 'status'):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            outcome = _FOLLOW_UP_OUTCOME_ALIASES.get(value.strip().lower(), '')
            if outcome:
                break
    if not outcome:
        return None, (
            'outcome 必须是 fulfilled|rescheduled|cancelled（收到 outcome=%r status=%r）'
            % (item.get('outcome'), item.get('status'))
        )
    entry: dict[str, Any] = {'id': intent_id, 'outcome': outcome}
    if isinstance(item.get('notBefore'), str):
        entry['notBefore'] = item['notBefore']
    return entry, ''


def normalize_follow_up_resolutions(value: Any) -> list[dict[str, Any]]:
    """上游 `normalizeFollowUpResolutions`：最多两条承诺处置（判据见 `read_follow_up_resolution`）。"""
    if not isinstance(value, list):
        return []
    resolutions: list[dict[str, Any]] = []
    for item in value:
        entry, _problem = read_follow_up_resolution(item)
        if entry is not None:
            resolutions.append(entry)
    return resolutions[:2]


def follow_up_resolution_problems(value: Any) -> list[str]:
    """``followUpResolutions`` 里每一条**为什么被丢掉**（空列表 = 全都可用）。

    调用方用它打一条可见 warn：坏形状绝不许静默（否则就是"三次没结算却查不出为什么"）。
    """
    if not isinstance(value, list):
        return []
    problems: list[str] = []
    for index, item in enumerate(value):
        _entry, problem = read_follow_up_resolution(item)
        if problem:
            problems.append('第%d条：%s' % (index + 1, problem))
    return problems


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
    if not is_record(value) or not is_record(value.get('reply')):
        return None
    reply = value['reply']
    raw_content = reply.get('content')
    # 上游 1.0.1-rc15：`mode` 的宽容归一。弱模型（Gemini Flash 等）常把 mode 写成
    # text/send/reply/message；只要带了 content 就按 immediate 处理，整条丢掉等于
    # 白扔一条有效回复。缺失 mode 且没有 content 才算 none；其它任何词、或者
    # 缺 mode 却带 content 之外的情形，才丢整条。
    # 容错本身抽到 `coerce_reply_mode`：恢复判定 `_has_structured_interaction` 读的是
    # **归一化之前**的 decision，两边必须是同一套（判据一处，见 §79）。
    has_content = isinstance(raw_content, str) and bool(raw_content.strip())
    mode = coerce_reply_mode(reply.get('mode'), has_content)
    if mode is None:
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
    # 上游 1.0.1-rc28（DeepSeek V4.1 修复）：`seen` 非布尔时**不再丢弃整条**。
    # V4.1 偶发漏 seen，旧写法会把一条有效的 immediate 回复打成 none，并正好落进
    # 「结构化可见回复缺失 → 重写一次」的环里（用户实测重写无效、只会烧调用）。
    # reply 本身有效时按已读处理；seen 只是"是否读了这条消息"的信息性字段。
    raw_seen = value.get('seen')
    seen = raw_seen if isinstance(raw_seen, bool) else True
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
    # 上游 1.0.1-rc4：`lastSeenAt` 可能为空（rc2/rc3 写入的补充事实、更老的自由写入行），
    # 回退到 updatedAt → createdAt → now——检索排序不能因为一个空时间戳崩掉，也不能
    # 把一条旧事实当成"刚刚见过"（那会让它白拿满分近因）。
    last_seen = (
        to_date(fact.get('lastSeenAt') or fact.get('last_seen_at'))
        or to_date(fact.get('updatedAt') or fact.get('updated_at'))
        or to_date(fact.get('createdAt') or fact.get('created_at'))
        or utc_now()
    )
    age_days = max(0.0, (dt_ms(utc_now()) - dt_ms(last_seen)) / (24 * 60 * 60 * 1000))
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


#: 遗忘评分的权重与参考值（`docs/MEMORY_MAINTENANCE.md`）。
#: 评分越高越值得留：近因 / 频次 / 置信 / 重要度四项加权。
FORGETTING_WEIGHTS: dict[str, float] = {
    'recency': 0.35,
    'frequency': 0.25,
    'confidence': 0.20,
    'importance': 0.20,
}

#: 频次项到达 0.5 分所需的召回次数。
FORGETTING_FREQUENCY_REFERENCE = 6.0


def fact_forgetting_score(fact: dict[str, Any], now: datetime, half_life_days: float = 30.0) -> float:
    """事实的遗忘评分（受控偏离，`docs/MEMORY_MAINTENANCE.md` §4）。

    与参考实现的差别：我们只有事实一层，没有图谱"孤立度"项，所以取
    近因 / 频次 / 置信 / 重要度四项。近因优先看**最后一次被召回**的时间
    （`lastAccessAt`），没有召回记录时退回写入时的 `lastSeenAt` / `createdAt`。

    返回 [0, 1]，越低越接近被淘汰。
    """
    weights = FORGETTING_WEIGHTS
    life = max(1.0, float(half_life_days or 30.0))
    stamp = to_date(fact.get('lastAccessAt')) or to_date(fact.get('lastSeenAt')) or to_date(fact.get('createdAt'))
    if stamp is None:
        recency = 0.5
    else:
        age_days = max(0.0, (dt_ms(now) - dt_ms(stamp)) / (24 * 60 * 60 * 1000))
        recency = math.exp(-age_days / life)
    count = max(0.0, _number(fact.get('accessCount')))
    frequency = count / (count + FORGETTING_FREQUENCY_REFERENCE) if count > 0 else 0.0
    confidence = max(0.0, min(1.0, _number(fact.get('confidence'))))
    importance = max(0.0, min(1.0, _number(fact.get('importance'))))
    return (
        recency * weights['recency']
        + frequency * weights['frequency']
        + confidence * weights['confidence']
        + importance * weights['importance']
    )


#: 我们自己发出去的消息用 `msg-<条目id>` 当平台消息 id（见 `chunk2.group_message_ref`）。
_SYNTHETIC_MESSAGE_REF = re.compile(r'^msg-(\d+)$')


def backfilled_quote_content(quote: Any, entries: list[Any]) -> str:
    """用**我们自己的记录**补出被回复消息的正文（v1.4.0，`docs/MEMORY_MAINTENANCE.md` §5.5）。

    平台给不给被引内容不一致：多数 QQ 客户端会给 `message_str`，但只给一个 id 的情况也常见
    （尤其转发、跨端回复、部分平台适配器）。给不出正文时，模型看到的就是一条"她引用了某条
    看不见的消息"——那比不引用更糟。

    回填按两种 id 找：

    1. `msg-<条目id>`（我们自己的投递账本用的合成 id）→ 直接取那条剧本条目；
    2. 平台消息 id → 在条目 `metadata.messageId` 里找（群里收到的消息会带这个键）。

    找不到就返回空串：**不编内容**，宁可保持"引用了但看不到"。
    平台已经给了正文时也返回空串——只补空，绝不覆盖平台给的事实。
    """
    if not isinstance(quote, dict):
        return ''
    if _str(quote.get('content')).strip():
        return ''
    for key in ('messageId', 'message_id', 'messageRef', 'message_ref', 'id'):
        raw = _str(quote.get(key)).strip()
        if not raw:
            continue
        synthetic = _SYNTHETIC_MESSAGE_REF.match(raw)
        if synthetic:
            wanted = int(synthetic.group(1))
            match = next(
                (entry for entry in (entries or [])
                 if isinstance(entry, dict) and entry.get('id') == wanted),
                None,
            )
            content = _str((match or {}).get('content')).strip()
            if content:
                return content
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            metadata = entry.get('metadata') if isinstance(entry.get('metadata'), dict) else {}
            platform_id = _str(metadata.get('messageId') or metadata.get('message_id') or '').strip()
            if platform_id and platform_id == raw:
                content = _str(entry.get('content')).strip()
                if content:
                    return content
    return ''


#: 图片感知哈希的去重容差：汉明距离 ≤ 该值视为同一张图。
#: 4/64 位能容忍重新编码与轻微压缩，又不会把两张相似的图混为一谈。
IMAGE_HASH_TOLERANCE = 4


def image_perceptual_hash(data: Any) -> str:
    """图片的 64 位感知哈希（aHash，十六进制字符串；算不出给空串）。

    只在 `PIL` 可用时工作——`PIL` 是本插件的**可选**依赖，缺了就当没有这个能力
    （不抛异常、不阻断投递，与降采样同一条降级路径）。用 aHash 而不是更精细的
    pHash：识图去重只需要"这张图是不是刚发过"，aHash 够用且零依赖（PIL 自带）。
    """
    if not data:
        return ''
    try:
        import io as _io

        from PIL import Image  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 - 没装 Pillow 就是没有这个能力
        return ''
    try:
        with Image.open(_io.BytesIO(data)) as image:
            small = image.convert('L').resize((8, 8))
            pixels = list(small.getdata())
    except Exception:  # noqa: BLE001 - 坏图/不支持格式都只是"算不出哈希"
        return ''
    if len(pixels) != 64:
        return ''
    # 纯色图没有可比较的结构：aHash 会给出全 0 / 全 1，两张不同的纯色图就"相等"了。
    # 这种图不参与去重（返回空串 = 算不出可用的哈希），宁可不省那一次识图。
    if max(pixels) - min(pixels) < 8:
        return ''
    average = sum(pixels) / len(pixels)
    bits = ''.join('1' if value >= average else '0' for value in pixels)
    return '%016x' % int(bits, 2)


def hamming_distance(left: str, right: str) -> int:
    """两个十六进制哈希的汉明距离；形状不对时给一个"必然不等"的大值。"""
    left_text, right_text = _str(left).strip(), _str(right).strip()
    if not left_text or len(left_text) != len(right_text):
        return 64
    try:
        return bin(int(left_text, 16) ^ int(right_text, 16)).count('1')
    except ValueError:
        return 64


def split_described_images(
    images: list[Any], known: list[str], tolerance: int = IMAGE_HASH_TOLERANCE,
) -> tuple[list[Any], list[str]]:
    """按"已经识过的图"分流：返回 `(要识的图, 被跳过的哈希列表)`。

    重复贴同一张图（表情包、截图）在真机上很常见，而识图按图计费。
    """
    fresh: list[Any] = []
    skipped: list[str] = []
    for image in images or []:
        digest = _str(_value_of(image, 'perceptualHash')).strip()
        if digest and find_seen_image([digest], known, tolerance) >= 0:
            skipped.append(digest)
            continue
        fresh.append(image)
    return fresh, skipped


def remember_described_hashes(
    known: list[str], images: list[Any], limit: int = 32,
) -> list[str]:
    """把这一批识过的图片哈希并进"最近识过"的列表（先进先出，上限默认 32）。"""
    merged = list(known or [])
    for image in images or []:
        digest = _str(_value_of(image, 'perceptualHash')).strip()
        if digest and digest not in merged:
            merged.append(digest)
    return merged[-max(1, limit):]


def _value_of(value: Any, key: str) -> Any:
    """从 dict 或对象上读一个键（引文/图片既可能是 dict 也可能是领域对象）。"""
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)


def find_seen_image(hashes: list[str], known: list[str], tolerance: int = IMAGE_HASH_TOLERANCE) -> int:
    """在 `known` 里找与 `hashes` 任一项近似的那张图，返回它的下标，找不到给 -1。

    `hashes` 是这一批待判断的图片（按顺序），`known` 是已经见过的哈希。
    返回的是 `known` 的下标，方便调用方报告"和第几张重复"。
    """
    for index, candidate in enumerate(known or []):
        for value in hashes or []:
            if hamming_distance(value, candidate) <= max(0, tolerance):
                return index
    return -1


#: 上下文构成的统计范围：wire 键 → 说明它在"这一轮模型看到什么"里算什么。
#: 只统计这几个键，因为它们才是**装配侧**真正按预算裁剪出来的量。
CONTEXT_METRIC_SECTIONS: tuple[tuple[str, str], ...] = (
    ('recentEntries', 'items'),
    ('recalledHistory', 'items'),
    ('memories', 'items'),
    ('facts', 'items'),
    ('overlaySnapshots', 'items'),
    ('followUpCommitments', 'items'),
    ('dueIntents', 'items'),
    ('upcomingIntents', 'items'),
    ('activeConsequences', 'items'),
    ('workingDetails', 'items'),
    ('participants', 'items'),
    ('webContext', 'items'),
    ('quotedMessages', 'items'),
    ('automaticDeliverySummaries', 'items'),
)


def estimate_tokens(text: str) -> int:
    """粗估 token 数：CJK 一字 ≈ 一 token，其余按 4 字符 ≈ 1 token。

    只用来回答"这一轮大概喂了多少"，**不是账单**——真实用量在模型中心的用量账里。
    不要拿它做预算判断，口径不同。
    """
    value = _str(text)
    if not value:
        return 0
    cjk = len(re.findall(r'[\u3400-\u9fff\u3040-\u30ff\uff00-\uffef]', value))
    rest = len(value) - cjk
    return int(cjk + (rest + 3) // 4)


def context_metrics(
    request: dict[str, Any], elapsed_ms: float = 0.0, phase: str = '',
    participant_id: str = '', now: Optional[datetime] = None,
) -> dict[str, Any]:
    """算出"这一轮上下文由什么组成"（v1.4.0，`docs/MEMORY_MAINTENANCE.md` §5.4）。

    纯函数：只读请求体，不碰数据库。写进 `story.state.extensions.last_context_metrics`，
    控制台总览读它，用来回答"她为什么突然变笨 / 这一轮怎么这么贵"。
    """
    sections: dict[str, Any] = {}
    total_items = 0
    total_characters = 0
    for key, _kind in CONTEXT_METRIC_SECTIONS:
        value = request.get(key)
        if value in (None, '', [], {}):
            continue
        if isinstance(value, list):
            items = len(value)
        elif isinstance(value, dict):
            items = len(value)
        else:
            items = 1
        characters = len(json.dumps(value, ensure_ascii=False, default=str))
        sections[key] = {'items': items, 'characters': characters}
        total_items += items
        total_characters += characters
    scene = request.get('sceneContext') if isinstance(request.get('sceneContext'), dict) else {}
    scene_characters = len(json.dumps(scene, ensure_ascii=False, default=str)) if scene else 0
    other_characters = len(json.dumps(request, ensure_ascii=False, default=str))
    return {
        'at': iso(now or utc_now()),
        'phase': _str(phase),
        'participant_id': _str(participant_id),
        'assembly_ms': int(max(0.0, elapsed_ms)),
        'sections': sections,
        'items': total_items,
        'characters': total_characters + scene_characters,
        'payload_characters': other_characters,
        'estimated_tokens': estimate_tokens(json.dumps(request, ensure_ascii=False, default=str)),
    }


#: 召回查询里"问法"的固定句式：这些词对"她记不记得"没有信息量，
#: 但会稀释双字组重合率（`history_lexical_score` 是按查询键数取平均的）。
RECALL_QUERY_PREFIXES = (
    '你还记得', '你还记不记得', '你还记得吗', '你记不记得', '你记得',
    '还记得', '记不记得', '你是否记得', '你知道', '你还知道',
    '我之前跟你说的', '我之前说过的', '我上次跟你说的', '我上次说过的',
    '上次说的那个', '上次那个', '之前说的那个', '之前那个',
    '我问你', '我想问', '我问一下',
)

#: 问句里的语气与疑问成分（去掉之后剩下的才是要检索的词）。
RECALL_QUERY_NOISE = (
    # 单字语气词：不含「么」——它是「怎么 / 什么 / 这么」的一部分，
    # 先剥单字会把它们打断（`怎么` → `怎`），所以那类词交给下面的停用词表。
    '吗', '呢', '吧', '啊', '呀', '嘛', '哦', '噢', '哈',
    '请问', '告诉我', '说一下', '讲讲', '是什么', '是什么来着', '来着',
)

#: 代词与虚词：双字组检索里它们几乎只制造噪声。
RECALL_QUERY_STOPWORDS = (
    '我们', '你们', '他们', '她们', '它们', '这个', '那个', '这些', '那些',
    '什么', '怎么', '为什么', '是不是', '有没有',
)


def rewrite_recall_query(query: str) -> str:
    """把"问法"压成检索用关键词（v1.4.0，`docs/MEMORY_MAINTENANCE.md` §5.2）。

    纯本地规则，不花模型调用：只剥掉固定句式、语气词与代词，**不做同义改写**——
    改写错一个词就会把该想起来的事挤掉，代价比收益大。
    结果太短（< 2 个字）或没有变化时返回原查询，让调用方按原样检索。
    """
    text = _str(query).strip()
    if not text:
        return ''
    rewritten = text
    for prefix in RECALL_QUERY_PREFIXES:
        if rewritten.startswith(prefix):
            rewritten = rewritten[len(prefix):]
    for stopword in RECALL_QUERY_STOPWORDS:
        rewritten = rewritten.replace(stopword, '')
    for noise in RECALL_QUERY_NOISE:
        rewritten = rewritten.replace(noise, '')
    rewritten = re.sub(r'[\s\u3000，。！？、；：""' + "'" + r'（）《》【】,.!?;:()\[\]{}<>"\-—…~]+', '', rewritten)
    if len(rewritten) < 2:
        return text
    return rewritten


#: 排名融合的默认常数（`docs/MEMORY_MAINTENANCE.md` §5.2）。
DEFAULT_RRF_K = 60


def _rrf_weight(rank: Optional[int], k: float, raw: float = 1.0) -> float:
    """单路的"排名 × 原始相关度"：第 1 名给满，之后按 1/(k+rank) 衰减。

    为什么不能照抄纯 RRF：纯 RRF 的贡献是 `1/(k+rank)`，**与原始分无关**。
    候选池只有两三百条时，最后一名的 `1/(62)` 只比第一名的 `1/(61)` 小 1.6%——
    于是一条**毫无字面与语义重合**的事实照样能拿到接近满额的排名分，
    重要度高的旧事实会把真正相关的那条顶掉（实测：0 重合那条反而排前面）。

    所以这里把排名当**相关系数**用：原始分为 0 的路不贡献，
    有重合的才按名次衰减。两路都把它排前面的事实因此被顶到前面（这才是融合的意义），
    而量纲与上游给这一路的原始权重一致（`raw × [0,1]`），不引入新的调参维度。
    """
    if rank is None or raw <= 0:
        return 0.0
    top = 1.0 / (k + 1.0)
    return raw * ((1.0 / (k + rank)) / top) if top else 0.0


def _ranks(values: list[float]) -> list[Optional[int]]:
    """按分数降序给出每一名（并列同分给同一个名次，与常见 RRF 实现一致）。"""
    order = sorted(range(len(values)), key=lambda index: -values[index])
    ranks: list[Optional[int]] = [None] * len(values)
    previous: Optional[float] = None
    current_rank = 0
    for position, index in enumerate(order, start=1):
        if previous is None or values[index] < previous:
            current_rank = position
            previous = values[index]
        ranks[index] = current_rank
    return ranks


def fact_lane_scores(
    facts: list[dict[str, Any]], config: dict[str, Any],
    query_embedding: Optional[list[float]] = None, query: str = '',
    rewrite: bool = True, rrf_k: float = DEFAULT_RRF_K,
) -> list[dict[str, Any]]:
    """算两条召回通道的原始分与排名（纯函数，便于断言与调参）。

    返回与 `facts` 等长的列表，每项是 `{'id', 'lexical', 'semantic', 'lexicalRank',
    'semanticRank', 'rewritten', 'lexicalScore'}`。
    """
    rewritten = rewrite_recall_query(query) if rewrite else _str(query)
    query_embedding = query_embedding or []
    lane: list[dict[str, Any]] = []
    for fact in facts or []:
        content = _str(fact.get('content')) if isinstance(fact, dict) else ''
        lexical = history_lexical_score(query, content)
        lexical_score = lexical
        if rewritten and rewritten != _str(query):
            # 改写后的查询与原查询取**较高**的一条：改写只允许帮忙，不允许帮倒忙。
            lexical_score = max(lexical, history_lexical_score(rewritten, content))
        similarity = cosine_similarity(query_embedding, fact.get('embedding') or []) \
            if isinstance(fact, dict) else None
        lane.append({
            'id': fact.get('id') if isinstance(fact, dict) else None,
            'lexical': lexical,
            'lexicalScore': lexical_score,
            'semantic': 0.0 if similarity is None else max(0.0, similarity),
            'rewritten': rewritten,
        })
    lexical_ranks = _ranks([item['lexicalScore'] for item in lane])
    semantic_ranks = _ranks([item['semantic'] for item in lane])
    has_semantic = any(item['semantic'] > 0 for item in lane)
    has_lexical = any(item['lexicalScore'] > 0 for item in lane)
    for index, item in enumerate(lane):
        item['lexicalRank'] = lexical_ranks[index] if has_lexical else None
        item['semanticRank'] = semantic_ranks[index] if has_semantic else None
        item['lexicalRrf'] = _rrf_weight(item['lexicalRank'], rrf_k, item['lexicalScore'])
        item['semanticRrf'] = _rrf_weight(item['semanticRank'], rrf_k, item['semantic'])
    return lane


def fact_structural_score(fact: dict[str, Any], config: dict[str, Any]) -> float:
    """事实的"结构性"分数：重要度 / 置信 / 新近 / 未结承诺。

    与上游 `factScore` 的区别只有一条：这里**不含**词法与语义两项，
    那两项在融合模式下改由排名贡献（见 `fact_hybrid_score`）。
    """
    last_seen = to_date(fact.get('lastSeenAt'))
    age_days = max(0.0, (dt_ms(utc_now()) - dt_ms(last_seen)) / (24 * 60 * 60 * 1000)) if last_seen else 0.0
    recency = math.exp(-age_days / 30)
    return (
        _number(fact.get('importance')) * _number(config_get(config, 'fact_importance_weight', 'factImportanceWeight'))
        + _number(fact.get('confidence')) * _number(config_get(config, 'fact_confidence_weight', 'factConfidenceWeight'))
        + recency * _number(config_get(config, 'fact_recency_weight', 'factRecencyWeight'))
        + (1.0 if fact.get('scope') == 'promise' and fact.get('unresolved') else 0.0)
        * _number(config_get(config, 'unresolved_weight', 'unresolvedWeight'))
    )


def fact_hybrid_score(
    fact: dict[str, Any], config: dict[str, Any], lane: dict[str, Any],
) -> float:
    """融合后的相关度：结构分 + 两路的倒数排名（RRF）。

    权重沿用上游给这两路的量级（词法 `max(1.0, semantic_weight)`、语义 `semantic_weight`），
    所以"只有一路可用"时排序与上游接近；两路都命中同一条事实时它会被顶到前面——
    这正是排名融合想要的效果（两路都同意 ⇒ 比单路第一名更可信）。
    """
    semantic_weight = _number(config_get(config, 'semantic_weight', 'semanticWeight'))
    relevance = max(1.0, semantic_weight)
    return (
        fact_structural_score(fact, config)
        + _number(lane.get('lexicalRrf')) * relevance
        + _number(lane.get('semanticRrf')) * semantic_weight
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
