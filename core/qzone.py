"""QQ 空间（说说）通道层 —— 上游 `src/qzone.ts`（318 行）的逐条移植。

边界与上游一致：**本模块只做通道**——动作调用、限流门（风控保护）、能力探测与
防御性归一化。"何时发 / 发什么 / 对哪条好友动态反应"的决策属于叙事层
（`service/chunk13`）。纯策略、无副作用，状态由调用方持有（审计表
`interlude_qzone_post`）。

## 与上游的受控偏离（除了下面这几条，其余逐条照抄）

1. **内部结构的键名用 snake_case**（`daily_post_cap` / `used_today` /
   `comment_num` …）。本仓库的键名法：发给模型的 payload 与**数据库列名**保上游
   camelCase，Python 内部结构用 snake_case。这些结构只在 Python 内部流转（配置段、
   归一化结果、门控结论），不发给模型。**审计行的列名仍是上游 camelCase**
   （`storyId` / `kind` / `tid` / `targetUin` / `content` / `ugcRight` /
   `endpointId` / `status` / `error` / `createdAt` / `postedAt`），读写一律双读。
2. **配置解析双拼写**：上游 `resolveQzoneConfig` 只认 camelCase；本移植版的配置段
   是 snake_case（`daily_post_cap` …），所以解析函数两种拼写都认（优先 camelCase）。
   配置段名见 `service/chunk13.py::_qzone_config_section`：**`qzone`**（旧文件里
   的隐藏兼容位 `qzone_compat` 只作兜底）。
3. **时间输入不限 `datetime`**：上游 `record.createdAt instanceof Date` 只认 Date
   对象；本移植版数据库行是 ISO-8601 字符串或 `datetime`，统一过 `parse_dt()`。
   "同一本地日"与「Token 统计」同口径：用**服务器本地日期**（`token_stats.day_key`）。
4. **失败分类要自己补齐**：上游能靠"抛异常（ambiguous=True）/ 失败帧（False）"
   区分"结果是否未知"；本移植版的 `Transport.call_onebot` 按契约把两者都收敛成
   `{'ok': False, 'error': 文案}`（"绝不上抛"），因此这里额外按几个**传输类错误
   标记**判定 ambiguous。宁可保守记 `unknown`（计入配额、禁止自动重试），也不把
   可能已经发出去的动作记成 `failed` 之后再补发一次。
5. **`call_qzone_action` 吃两种帧**：`Transport.call_onebot` 的
   `{'ok', 'error', 'data'}` 契约（`ok` 为权威），以及上游的裸 OneBot 帧
   （`status === 'ok' || retcode === 0`，供直连测试与旧调用方使用）。
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

from .time import dt_ms, parse_dt
from .token_stats import day_key

__all__ = [
    'DEFAULT_QZONE_CONFIG',
    'QZONE_ACTION_KINDS',
    'QZONE_AMBIGUOUS_MARKERS',
    'QZONE_CONFIG_BOUNDS',
    'QZONE_FEED_APPID_TALK',
    'QZONE_UGC_RIGHT_VALUES',
    'TID_PATTERN',
    'QzoneActionError',
    'call_qzone_action',
    'evaluate_qzone_gate',
    'fresh_qzone_feeds',
    'local_day_key',
    'match_qzone_feed_content',
    'normalize_qzone_feed_entry',
    'normalize_qzone_msg_entry',
    'probe_qzone_available',
    'qzone_feed_candidates',
    'qzone_intent_from_payload',
    'qzone_records_for_endpoint',
    'qzone_visibility_label',
    'resolve_qzone_config',
]

#: 真正的空间动作（`post` / `comment` / `like`）。`feed-seen` 是只读感知标记，
#: **不**参与限流计数与最小间隔（上游 `ACTION_KINDS`；上游没有导出它）。
QZONE_ACTION_KINDS = frozenset({'post', 'comment', 'like'})

#: 审计与剧本条目用的可见性档位白名单（上游 `QZONE_UGC_RIGHT_VALUES`）。
QZONE_UGC_RIGHT_VALUES = frozenset({1, 4, 16, 64, 128})

#: `comment` / `like` 的 tid 字符集（上游 `TID_PATTERN`）。用 `.match()` 语义：
#: JS 的 `$` 允许"结尾换行符之前"命中，Python `re.fullmatch` 不允许。
TID_PATTERN = re.compile(r'^[A-Za-z0-9_-]{4,64}$')

#: appid=311 是说说；6600 是广告位、5000 是官方号、202 等为杂项，全部排除。
QZONE_FEED_APPID_TALK = 311

#: 未配置时的保守默认（上游 `DEFAULT_QZONE_CONFIG` + 本移植版新增的 `auto_feed`）。
#: 数值键走 `QZONE_CONFIG_BOUNDS` 夹取；`enabled` / `auto_feed` 是布尔键，只认
#: **严格 `true`**（`1` / `'true'` 都不算），不进边界表。
DEFAULT_QZONE_CONFIG: dict[str, Any] = {
    'enabled': False,
    'daily_post_cap': 3,
    'daily_comment_cap': 6,
    'daily_like_cap': 12,
    'min_interval_minutes': 90,
    'feed_window_minutes': 120,
    'auto_feed': False,
}

#: 配置夹取边界（上游 `CONFIG_BOUNDS`，键名转 snake_case）。
QZONE_CONFIG_BOUNDS: dict[str, tuple[int, int]] = {
    'daily_post_cap': (0, 20),
    'daily_comment_cap': (0, 60),
    'daily_like_cap': (0, 120),
    'min_interval_minutes': (10, 1440),
    'feed_window_minutes': (15, 720),
}

#: 配置键的两种拼写（camelCase 优先，snake_case 兜底）。
_CONFIG_KEY_SPELLINGS: dict[str, str] = {
    'daily_post_cap': 'dailyPostCap',
    'daily_comment_cap': 'dailyCommentCap',
    'daily_like_cap': 'dailyLikeCap',
    'min_interval_minutes': 'minIntervalMinutes',
    'feed_window_minutes': 'feedWindowMinutes',
}

#: `Transport.call_onebot` 把"传输异常 / 超时"和"服务端显式失败帧"收敛成同一种
#: `{'ok': False, 'error': …}`，于是只能按文案判定"请求是否可能已经到达服务端"。
#: 命中者记 `unknown`（保守计入配额、禁止自动重试）。传输层实现按约定会给
#: 结果未知的回执打上 `ambiguous: True`（优先看它）并把错误以「结果未知，请勿
#: 自动重试」结尾；`transport-unavailable` 这类"压根没发出去"的文案**不在**表内，
#: 仍记 `failed`。
QZONE_AMBIGUOUS_MARKERS: tuple[str, ...] = (
    'timeout', 'timed out', '超时', 'socket', 'connection', 'connect failed',
    'broken pipe', 'disconnect', 'econnreset', 'econnrefused', 'etimedout',
    'network', 'unreachable', 'eof', 'reset by peer', '结果未知',
)


# --------------------------------------------------------------------------- #
# 小工具：JS 语义（`String()` / `Number()` / `Math.floor`）与双拼写读键
# --------------------------------------------------------------------------- #


def _pick(value: Any, *keys: str) -> Any:
    """按键名依次取值（第一种拼写优先）；非 Mapping 一律 None。"""
    if not isinstance(value, Mapping):
        return None
    for key in keys:
        if key in value and value[key] is not None:
            return value[key]
    return None


def _js_string(value: Any) -> str:
    """等价 JS `String(value)`（只覆盖本模块遇到的标量与数组）。"""
    if value is None:
        return 'null'
    if value is True:
        return 'true'
    if value is False:
        return 'false'
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return 'NaN'
        if math.isinf(value):
            return 'Infinity' if value > 0 else '-Infinity'
        return str(int(value)) if value.is_integer() else repr(value)
    if isinstance(value, (list, tuple)):
        return ','.join(_js_string(item) for item in value)
    if isinstance(value, Mapping):
        return '[object Object]'
    return str(value)


def _nullish_string(value: Any) -> str:
    """`String(value ?? '')`：null/undefined 先塌成空串。"""
    return '' if value is None else _js_string(value)


def _js_number(value: Any) -> float:
    """等价 JS `Number(value)`；不可解析返回 NaN（`Number(null) === 0`）。"""
    if value is None:
        return 0.0
    if value is True:
        return 1.0
    if value is False:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0.0
        try:
            return float(text)
        except ValueError:
            pass
        signed = text[1:] if text[:1] in ('+', '-') else text
        if signed[:2].lower() == '0x':  # JS `Number('0x10') === 16`
            try:
                return float(int(text, 16))
            except ValueError:
                return math.nan
        return math.nan
    if isinstance(value, (list, tuple)):
        if not value:
            return 0.0
        if len(value) == 1:
            return _js_number(value[0])
        return math.nan
    return math.nan


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _strict_int_eq(value: Any, expected: int) -> bool:
    """JS `value === expected`（数字严格相等；`True` 不是 `1`，`'1'` 不是 `1`）。"""
    return _finite(value) and value == expected


def _is_mapping(value: Any) -> bool:
    """JS `typeof value === 'object' && value !== null` 的 Mapping 近似（列表不算）。"""
    return isinstance(value, Mapping)


def _raw_number(config: Any, snake: str, camel: str) -> float:
    """上游 `Number(config?.[key])`：键不存在 → NaN（保持默认）；显式 `null` → 0。"""
    if not isinstance(config, Mapping):
        return math.nan
    if camel in config:
        return _js_number(config[camel])
    if snake in config:
        return _js_number(config[snake])
    return math.nan


def _config_int(config: Any, snake: str, fallback: int) -> int:
    """读一个已解析配置项；缺失/非法时回落默认值。"""
    raw = _raw_number(config, snake, _CONFIG_KEY_SPELLINGS.get(snake, snake))
    if not _finite(raw):
        return fallback
    return int(math.floor(raw))


def _js_int_or(value: Any, fallback: int = 0) -> Any:
    """`Number(value) || fallback`：NaN 与 0 都塌成 `fallback`，其余取数值。"""
    number = _js_number(value)
    if not _finite(number) or number == 0:
        return fallback
    return int(number) if float(number).is_integer() else number


def _to_datetime(value: Any) -> Optional[datetime]:
    """datetime / ISO 字符串 / 毫秒数 → aware datetime；非法返回 None。"""
    if isinstance(value, bool) or value is None:
        return None
    return parse_dt(value)


def _ms(value: Any) -> Optional[int]:
    parsed = _to_datetime(value)
    if parsed is None:
        return None
    return dt_ms(parsed)


def _from_ms(milliseconds: float) -> Optional[datetime]:
    if not _finite(milliseconds):
        return None
    try:
        return datetime.fromtimestamp(milliseconds / 1000.0, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def local_day_key(value: Any) -> str:
    """某个时刻的**服务器本地**日期键（`YYYY-MM-DD`），与「Token 统计」同口径。"""
    parsed = _to_datetime(value)
    if parsed is None:
        parsed = datetime.now(timezone.utc)
    return day_key(parsed)


# --------------------------------------------------------------------------- #
# 配置与限流门
# --------------------------------------------------------------------------- #


def resolve_qzone_config(config: Any = None) -> dict[str, Any]:
    """上游 `resolveQzoneConfig`：保守默认 + 逐项夹取边界（`Math.floor` 后夹取）。

    键**不存在**时保持默认（上游 `Number(undefined) === NaN`）；键存在但为
    `null` 时按 `Number(null) === 0` 参与夹取——这两条语义不同，别合并。
    布尔键（`enabled` / `auto_feed`）不夹取，只认严格 `true`。
    """
    resolved = dict(DEFAULT_QZONE_CONFIG)
    for snake, (minimum, maximum) in QZONE_CONFIG_BOUNDS.items():
        raw = _raw_number(config, snake, _CONFIG_KEY_SPELLINGS[snake])
        if _finite(raw):
            resolved[snake] = min(maximum, max(minimum, math.floor(raw)))
    enabled = _pick(config, 'enabled') if isinstance(config, Mapping) else None
    resolved['enabled'] = enabled is True
    # 上游没有 auto_feed（`qzoneFeedSweep` 恒开）：本移植版用它表达「允许她浏览
    # 好友动态并在合适时评论/点赞」，默认关（感知是低频背景行为，别默认打开）。
    auto_feed = _pick(config, 'autoFeed', 'auto_feed') if isinstance(config, Mapping) else None
    resolved['auto_feed'] = auto_feed is True
    return resolved


def qzone_records_for_endpoint(
    records: Sequence[Any],
    endpoint_id: Optional[str] = None,
) -> list[Any]:
    """限流门的端点过滤（上游 `qzoneRecordsForEndpoint`）。

    只计本端点的动作行；无 `endpointId` 的历史行（回填前）保守计入所有端点的
    配额——风控安全优先于配额精确。
    """
    if not endpoint_id:
        return list(records)
    kept: list[Any] = []
    for record in records:
        record_endpoint = _pick(record, 'endpointId', 'endpoint_id')
        if not record_endpoint or record_endpoint == endpoint_id:
            kept.append(record)
    return kept


def evaluate_qzone_gate(
    records: Sequence[Any],
    config: Any,
    kind: Any = None,
    now: Any = None,
) -> dict[str, Any]:
    """空间动作限流门（上游 `evaluateQzoneGate`）。

    `records` 传近期审计行（调用方取 48h 窗口）；只有真正的动作
    （`post`/`comment`/`like`）参与计数与间隔——`feed-seen` 是只读感知标记，不得
    挤占动作配额。同一本地日内按 kind 计数（`failed` 不计；`pending`/`unknown`
    计入：在途与结果不明的都按已发生保守对待），且任意两动作间隔不小于
    `minIntervalMinutes`。

    `kind` 也可直接传上游形状的 `{'kind': …, 'now': …}`（两种调用方式都收）。
    """
    if isinstance(kind, Mapping):
        payload = kind
        kind = _pick(payload, 'kind')
        if now is None:
            now = _pick(payload, 'now')
    if not (_pick(config, 'enabled') is True):
        return {'allowed': False, 'reason': 'disabled', 'used_today': 0, 'cap': 0}
    now_dt = _to_datetime(now) or datetime.now(timezone.utc)
    now_ms = dt_ms(now_dt)
    today = day_key(now_dt)
    if kind == 'post':
        cap = _config_int(config, 'daily_post_cap', DEFAULT_QZONE_CONFIG['daily_post_cap'])
    elif kind == 'comment':
        cap = _config_int(config, 'daily_comment_cap', DEFAULT_QZONE_CONFIG['daily_comment_cap'])
    else:
        cap = _config_int(config, 'daily_like_cap', DEFAULT_QZONE_CONFIG['daily_like_cap'])
    used_today = 0
    last_action_at: Optional[int] = None
    for record in records:
        if _pick(record, 'kind') not in QZONE_ACTION_KINDS:
            continue
        at_ms = _ms(_pick(record, 'createdAt', 'created_at'))
        if at_ms is None:
            continue
        if _pick(record, 'status') == 'failed':
            continue
        if _pick(record, 'kind') == kind and local_day_key(_from_ms(at_ms)) == today:
            used_today += 1
        if last_action_at is None or at_ms > last_action_at:
            last_action_at = at_ms
    if used_today >= cap:
        return {'allowed': False, 'reason': 'daily-cap', 'used_today': used_today, 'cap': cap}
    min_interval = _config_int(
        config, 'min_interval_minutes', DEFAULT_QZONE_CONFIG['min_interval_minutes'],
    )
    if last_action_at is not None and now_ms - last_action_at < min_interval * 60_000:
        return {'allowed': False, 'reason': 'min-interval', 'used_today': used_today, 'cap': cap}
    return {'allowed': True, 'reason': 'ok', 'used_today': used_today, 'cap': cap}


# --------------------------------------------------------------------------- #
# SnowLuma 动作防御性归一化
# --------------------------------------------------------------------------- #


def normalize_qzone_msg_entry(raw: Any) -> Optional[dict[str, Any]]:
    """`get_qzone_msg_list` 条目归一化（上游 `normalizeQzoneMsgEntry`）：坏行丢弃。

    字段强转：`tid` 空串即丢；`time` 是**秒**级时间戳，非法时塌成 epoch；
    `comment_num` 走 `Number(x) || 0`；`is_private` 只认严格 `true`；
    `images` 取字符串化后的前 9 个非空值。
    """
    if not _is_mapping(raw):
        return None
    tid = _nullish_string(raw.get('tid')).strip()
    if not tid:
        return None
    seconds = _js_number(raw.get('time')) if 'time' in raw else 0.0
    moment = _from_ms(seconds * 1000)
    if moment is None:
        moment = datetime.fromtimestamp(0, tz=timezone.utc)
    comment = raw['comment_num'] if 'comment_num' in raw else raw.get('commentNum')
    images_raw = raw.get('images')
    images: list[str] = []
    if isinstance(images_raw, (list, tuple)):
        for image in images_raw:
            text = _js_string(image)
            if text:
                images.append(text)
            if len(images) >= 9:
                break
    return {
        'tid': tid,
        'content': _nullish_string(raw.get('content')),
        'time': moment,
        'comment_num': _js_int_or(comment),
        'is_private': _pick(raw, 'is_private', 'isPrivate') is True,
        'images': images,
    }


def normalize_qzone_feed_entry(raw: Any, now: Any = None) -> Optional[dict[str, Any]]:
    """`get_qzone_feeds` 条目归一化（上游 `normalizeQzoneFeedEntry`）。

    `key`（定位句柄）与 `uin` 任一为空即丢弃；时间非法时**回落 `now`**（不是 epoch）。
    appid 311=说说；正文 html 本阶段不解析。
    """
    if not _is_mapping(raw):
        return None
    key = _nullish_string(raw.get('key')).strip()
    uin = _nullish_string(raw.get('uin')).strip()
    if not key or not uin:
        return None
    seconds = _js_number(raw.get('time')) if 'time' in raw else 0.0
    moment = _from_ms(seconds * 1000)
    if moment is None:
        moment = _to_datetime(now) or datetime.now(timezone.utc)
    appid = raw['appid'] if 'appid' in raw else None
    return {
        'uin': uin,
        'nickname': _nullish_string(raw.get('nickname')),
        'time': moment,
        'appid': _js_int_or(appid),
        'key': key,
    }


def fresh_qzone_feeds(feeds: Sequence[Any], config: Any, now: Any = None) -> list[Any]:
    """好友动态新鲜度过滤（上游 `freshQzoneFeeds`）：只保留时间窗内的条目。"""
    now_dt = _to_datetime(now) or datetime.now(timezone.utc)
    window = _config_int(config, 'feed_window_minutes', DEFAULT_QZONE_CONFIG['feed_window_minutes'])
    min_time = dt_ms(now_dt) - window * 60_000
    kept: list[Any] = []
    for feed in feeds:
        at_ms = _ms(_pick(feed, 'time'))
        if at_ms is not None and at_ms >= min_time:
            kept.append(feed)
    return kept


# --------------------------------------------------------------------------- #
# 动作调用（本移植版走 `Transport.call_onebot` 契约）
# --------------------------------------------------------------------------- #


class QzoneActionError(Exception):
    """一次空间动作的失败（上游 `QzoneActionError`）。

    `ambiguous=True` 表示请求**可能**已到达服务端（传输异常/超时），结果未知，
    禁止自动重试；`retcode` 保留供风控分类（12xxx 段为 Qzone 风控）。
    """

    def __init__(
        self,
        message: str,
        action: str = '',
        retcode: Optional[int] = None,
        ambiguous: bool = False,
    ) -> None:
        super().__init__(message)
        self.name = 'QzoneActionError'
        self.action = action
        self.retcode = retcode
        self.ambiguous = ambiguous


def _frame_status_text(frame: Mapping[str, Any]) -> tuple[str, str]:
    """失败帧的 `(状态词, 细节)`。

    `Transport.call_onebot` 的失败帧只有 `error`（没有 OneBot 的
    status/retcode/message），这时整段 `error` 就是失败原因；上游裸帧则照抄
    `status ?? retcode ?? 'unknown'` + `message ?? msg ?? wording`。
    """
    status = _pick(frame, 'status')
    if status is None:
        status = _pick(frame, 'retcode')
    detail = _pick(frame, 'message', 'msg', 'wording')
    if detail is None:
        detail = _nullish_string(_pick(frame, 'error'))
    if status is None:
        return (detail, '') if detail else ('unknown', '')
    return (_js_string(status), detail)


def _frame_ambiguous(frame: Mapping[str, Any]) -> bool:
    """请求是否"可能已到达服务端"（→ `unknown`，禁止自动重试）。"""
    for key in ('ambiguous', 'resultUnknown', 'result_unknown'):
        if frame.get(key) is True:
            return True
    text = ' '.join(
        _nullish_string(frame.get(key)).lower() for key in ('error', 'message', 'msg', 'wording')
    )
    return any(marker in text for marker in QZONE_AMBIGUOUS_MARKERS)


def _is_ok_frame(frame: Any) -> bool:
    """回执是否代表成功。

    有 `ok` 键时以它为准（本移植版 `Transport` 契约）；否则按上游裸帧判定
    （`status === 'ok' || retcode === 0`）。
    """
    if not _is_mapping(frame):
        return False
    if 'ok' in frame:
        return frame.get('ok') is True
    return frame.get('status') == 'ok' or _strict_int_eq(frame.get('retcode'), 0)


async def call_qzone_action(call: Any, action: str, params: Any = None) -> Any:
    """调用一次 qzone 动作并校验回执（上游 `callQzoneAction`）。

    `call` 就是 `Transport.call_onebot` 的绑定方法（或任何
    `async (action, params) -> frame` 的可调用对象）；失败抛 `QzoneActionError`。
    成功时返回回执的 `data`（缺失按 `{}`，与上游 `frame.data ?? {}` 一致）。
    """
    if not callable(call):
        raise QzoneActionError(
            '%s 调用异常（结果未知，请勿自动重试）：传输层不可用' % action,
            action, None, True,
        )
    try:
        frame = await call(action, dict(params or {}))
    except QzoneActionError:
        raise
    except Exception as error:  # noqa: BLE001 - 传输层异常一律收敛成 QzoneActionError
        raise QzoneActionError(
            '%s 调用异常（结果未知，请勿自动重试）：%s' % (action, error), action, None, True,
        ) from error
    if not _is_ok_frame(frame):
        row = frame if _is_mapping(frame) else {}
        head, detail = _frame_status_text(row)
        retcode_raw = _js_number(row.get('retcode')) if _is_mapping(row) else math.nan
        retcode = int(retcode_raw) if _finite(retcode_raw) else None
        message = ('%s 失败：%s %s' % (action, head, detail)).strip()
        raise QzoneActionError(message, action, retcode, _frame_ambiguous(row))
    data = frame.get('data')
    return {} if data is None else data


async def probe_qzone_available(call: Any) -> bool:
    """能力探测（上游 `probeQzoneAvailable`）：只读 `get_qzone_msg_list` 是否可用。"""
    try:
        await call_qzone_action(call, 'get_qzone_msg_list', {'num': 1})
        return True
    except Exception:  # noqa: BLE001 - 探测失败就是"不可用"，绝不外抛
        return False


# --------------------------------------------------------------------------- #
# 叙事决策侧：意图解析与好友动态过滤（纯函数，service 执行侧调用）
# --------------------------------------------------------------------------- #


def qzone_visibility_label(ugc_right: Any) -> str:
    """审计与剧本条目用的可见性中文标签（上游 `qzoneVisibilityLabel`）。"""
    if _strict_int_eq(ugc_right, 1):
        return '所有人可见'
    if _strict_int_eq(ugc_right, 4):
        return '好友可见'
    if _strict_int_eq(ugc_right, 16):
        return '部分好友可见'
    if _strict_int_eq(ugc_right, 64):
        return '仅自己可见'
    if _strict_int_eq(ugc_right, 128):
        return '部分好友不可见'
    return '好友可见'


def qzone_intent_from_payload(payload: Any) -> Optional[dict[str, Any]]:
    """意图 payload → 受限动作请求（上游 `qzoneIntentFromPayload`）。

    非法（类型错 / 该有的字段缺 / 超长 / tid 字符集异常）返回 `None`，执行侧记失败
    并完成意图——坏 payload 永远不该挡住账本排水。

    返回的键按上游构造的对象形状给出（不适用的键不出现），值用 `None` 表示
    `undefined`。
    """
    if not _is_mapping(payload):
        return None
    action = _nullish_string(payload.get('action'))
    if action not in QZONE_ACTION_KINDS:
        return None
    content = payload.get('content').strip() if isinstance(payload.get('content'), str) else ''
    tid = payload.get('tid').strip() if isinstance(payload.get('tid'), str) else ''
    explicit_uin = payload.get('targetUin')
    target_uin = (
        explicit_uin.strip()
        if isinstance(explicit_uin, str) and re.match(r'[0-9]{4,12}$', explicit_uin.strip())
        else ''
    )
    explicit_name = payload.get('targetName')
    target_name = explicit_name.strip()[:40] if isinstance(explicit_name, str) else ''
    raw_right = _js_number(payload.get('ugcRight'))
    right = int(raw_right) if _finite(raw_right) and raw_right in QZONE_UGC_RIGHT_VALUES else None
    if action == 'post':
        if not content or len(content) > 2_000:
            return None
        return {
            'action': action, 'content': content,
            'targetUin': target_uin or None, 'targetName': target_name or None,
            'ugcRight': right if right is not None else 4,
        }
    # 评论/点赞的目标 tid 只收窄字符集；来源绑定（必须来自已入账动态）由执行侧校验。
    if not TID_PATTERN.match(tid):
        return None
    if action == 'comment':
        if not content or len(content) > 500:
            return None
        return {
            'action': action, 'content': content, 'tid': tid,
            'targetUin': target_uin or None, 'targetName': target_name or None,
        }
    return {
        'action': action, 'tid': tid,
        'targetUin': target_uin or None, 'targetName': target_name or None,
    }


def qzone_feed_candidates(
    feeds: Sequence[Any],
    seen_keys: Any,
    config: Any,
    now: Any = None,
) -> list[Any]:
    """好友动态候选（上游 `qzoneFeedCandidates`）。

    说说类（appid 311）、有效 uin、时间窗内、未在 `seen_keys` 里；每轮最多 2 条
    ——感知是低频背景行为，不让一次轮询刷屏剧本。
    """
    now_dt = _to_datetime(now) or datetime.now(timezone.utc)
    window = _config_int(config, 'feed_window_minutes', DEFAULT_QZONE_CONFIG['feed_window_minutes'])
    min_time = dt_ms(now_dt) - window * 60_000
    seen = seen_keys if seen_keys is not None else ()
    candidates: list[Any] = []
    for feed in feeds:
        if len(candidates) >= 2:
            break
        if not _strict_int_eq(_pick(feed, 'appid'), QZONE_FEED_APPID_TALK):
            continue
        uin = _pick(feed, 'uin')
        if not uin or uin == '0':
            continue
        if _pick(feed, 'key') in seen:
            continue
        at_ms = _ms(_pick(feed, 'time'))
        if at_ms is None or at_ms < min_time:
            continue
        candidates.append(feed)
    return candidates


def match_qzone_feed_content(entries: Sequence[Any], feed: Any) -> str:
    """用好友自己的说说列表对齐 feed 正文（上游 `matchQzoneFeedContent`）。

    **只认 tid 精确命中**——连发多条时按时间近似配对会错配正文，宁可只记元数据
    （空串），错的内容比没有内容更糟。
    """
    key = _pick(feed, 'key')
    for entry in entries:
        if _pick(entry, 'tid') == key:
            return _nullish_string(_pick(entry, 'content'))
    return ''
