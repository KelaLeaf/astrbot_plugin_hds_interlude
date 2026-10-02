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

import json
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
    'QZONE_QUOTA_FAMILY',
    'QZONE_VISIBILITY_VALUES',
    'QZONE_CONFIG_BOUNDS',
    'QZONE_FEED_APPID_TALK',
    'QZONE_UGC_RIGHT_VALUES',
    'QZONE_CGI_BY_ID',
    'QZONE_ACTION_LABELS',
    'QZONE_COOKIE_APIS',
    'QZONE_COOKIE_DOMAINS',
    'QZONE_CGI_READ_ACTIONS',
    'QZONE_READ_ACTIONS',
    'TID_PATTERN',
    'QzoneActionError',
    'QzoneCgiUnavailable',
    'call_qzone_action',
    'qzone_action_is_read',
    'qzone_action_label',
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
    'qzone_visible_value',
    'resolve_qzone_config',
]

#: 真正的空间动作（`post` / `comment` / `like` / `forward`）。`feed-seen` 是只读感知标记，
#: **不**参与限流计数与最小间隔（上游 `ACTION_KINDS`；上游没有导出它）。
#:
#: v1.7.5 新增 `visibility`（改已发说说的可见范围，本移植版补的动作，上游没有）：
#: 它是**写**动作，必须进这张表，否则 `evaluate_qzone_gate` 会把它当只读标记跳过、
#: 审计行也就不占配额（= 模型可以无限次改可见性）。配额按**发帖**那一档算
#: （见 `evaluate_qzone_gate`）——改的是她自己发出去的东西，跟发帖同源。
QZONE_ACTION_KINDS = frozenset({'post', 'comment', 'like', 'forward', 'visibility'})

#: 日额度按 kind 分档；v1.7.5 起 `visibility`（改可见范围）与 `post` **共用一档**。
#:
#: 依据：它改的是**她自己已经发出去的那条说说**，与发帖同源；另开一份额度等于
#: "一天能发 3 条 + 另外改 3 次可见性"，而改可见性同样会打扰好友的动态流。
#: 共用之后 `post` 与 `visibility` 的审计行都计入同一格 `daily_post_cap`。
#: 其它 kind 不进这张表 = 各算各的（历史行为不变）。
QZONE_QUOTA_FAMILY: dict[str, str] = {'visibility': 'post'}

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
    （`QZONE_ACTION_KINDS`：`post`/`comment`/`like`/`forward`/`visibility`）参与计数与
    间隔——`feed-seen` 是只读感知标记，不得挤占动作配额。同一本地日内按 kind 计数
    （`failed` 不计；`pending`/`unknown` 计入：在途与结果不明的都按已发生保守对待），
    且任意两动作间隔不小于 `minIntervalMinutes`。

    v1.7.5：`visibility` 与 `post` **共用同一格日额度**（见 `QZONE_QUOTA_FAMILY`），
    其余 kind 仍各算各的。

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
    family = QZONE_QUOTA_FAMILY.get(kind, kind)
    if family == 'post':
        # 改可见范围按**发帖**配额算（`QZONE_QUOTA_FAMILY`）：它动的是"她自己发出去的
        # 那条说说"，与发帖同源；归到评论/点赞那两档更宽的额度里会让"一天改 12 次
        # 可见性"变成可能。
        cap = _config_int(config, 'daily_post_cap', DEFAULT_QZONE_CONFIG['daily_post_cap'])
    elif family in ('comment', 'forward'):
        # 转发按**评论类互动**计配额：它是互动不是发帖，跟点赞同一档更宽的上限
        # 也不合适（转发会出现在别人动态里，比点赞重）。
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
        if QZONE_QUOTA_FAMILY.get(_pick(record, 'kind'), _pick(record, 'kind')) == family \
                and local_day_key(_from_ms(at_ms)) == today:
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
# 空间条目防御性归一化
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

    **失败口径按动作的读写性质分**（`qzone_action_is_read`）：只读动作（看好友动态 /
    看某人说说）重试永远安全，拿不到响应就明说"只读动作，重试安全"、`ambiguous=False`；
    写动作（发 / 删 / 评 / 赞 / 转 / 改可见范围）一律 `ambiguous=True`——"可能已生效"
    的东西禁止自动重试，否则就是重复发帖那条老路。
    """
    read_only = qzone_action_is_read(action)
    if not callable(call):
        raise QzoneActionError(
            _transport_failure_text(action, read_only, '传输层不可用'),
            action, None, not read_only,
        )
    try:
        frame = await call(action, dict(params or {}))
    except QzoneActionError:
        raise
    except Exception as error:  # noqa: BLE001 - 传输层异常一律收敛成 QzoneActionError
        raise QzoneActionError(
            _transport_failure_text(action, read_only, error), action, None, not read_only,
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


def _transport_failure_text(action: str, read_only: bool, detail: Any) -> str:
    """传输层失败（拿不到回执 / 抛异常）的文案。

    写动作保留「结果未知，请勿自动重试」这句话——它是调用方（`chunk13` 的审计行与
    限流闸）认的标记；只读动作用另一句，**不**许出现"重试"两个字的否定式：
    日志层的 `[自动重试]` 标签早先就是被我们自己写的"请勿自动重试"命中的
    （标签与正文说的是反话，真机日志点名）。
    """
    if read_only:
        return '%s 读取失败：%s（只读动作，重试不会产生副作用）' % (action, detail)
    return '%s 调用异常（结果未知，请勿自动重试）：%s' % (action, detail)


# --------------------------------------------------------------------------- #
# NapCat WebSocket 方案（本移植版新增，参考 Eganchiyu/qzone-sdk 的 NapCat 认证）
# --------------------------------------------------------------------------- #

#: 取 Cookie 的域，**按顺序尝试**（`qzone_cgi_auth`）。
#:
#: 腾讯把 QZone 的 `p_skey` 放在 QZone 那个域下，但"那个域"在不同实现里写法不同：
#:
#: * `user.qzone.qq.com` —— 参考实现 `Eganchiyu/qzone-sdk`（v1.7.1 的移植依据）；
#: * `qzone.qq.com` —— AstrBot 参考插件 `Wyccotccy/astrbot_plugin_qzone_tools` v5.7.5
#:   （`main.py:866` / `main.py:870`，README.md:374 也把它列为"发说说失败"的排查项）。
#:
#: NapCat 的取 Cookie 接口是按后缀匹配还是精确相等，文档（napcat.apifox.cn/226657041e0）
#: 只写了"需要获取 cookies 的域名"、没说匹配规则——**未核实**。所以两个都试，
#: 谁能给出带 `p_skey` 的 Cookie 就用谁（都拿不到才失败，失败文案里逐域写明原因）。
QZONE_COOKIE_DOMAINS: tuple[str, ...] = ('user.qzone.qq.com', 'qzone.qq.com')

#: 取 Cookie 的**两个 NapCat 接口，按顺序尝试**（`qzone_cgi_auth`）：
#:
#: * `get_credentials`（napcat.apifox.cn/226657054e0，系统接口「获取登录凭证」）：
#:   回 `data.cookies` + `data.token`（CSRF token，number）。**参考插件的主路**
#:   （`astrbot_plugin_qzone_tools` v5.7.5 `main.py:866`，失败才退到 `:870` 的
#:   `get_cookies`），它的 README.md:374 说版本要求 `> 4.17.55` 正是"需支持
#:   `get_credentials` / `get_cookies`"。
#: * `get_cookies`（napcat.apifox.cn/226657041e0，用户接口「获取 Cookies」）：
#:   回 `data.cookies` + `data.bkn`（CSRF token）。
#:
#: 两个接口的**参数完全一样**（`domain` 必填）、回执的 Cookie 字段也同名——
#: 所以解析只写一份（`_cookie_string_from_frame`），"剥壳"那一层更是全动作共用
#: （`astrbot_bridge._onebot_payload_frame`）。
#: 顺序即优先级：先用参考插件验证过的那条，不可用（不存在 / 没登录 / 没 cookie）再退。
QZONE_COOKIE_APIS: tuple[str, ...] = ('get_credentials', 'get_cookies')

#: 主域（失败文案与历史调用点引用它）。
QZONE_COOKIE_DOMAIN = QZONE_COOKIE_DOMAINS[0]

#: **目录 id → CGI 动作名**（`core/qzone_cgi.py` 里的构造函数）。
#:
#: 这张表回答"这条目录动作有没有 CGI 出口"，与 `QZONE_CGI_BY_ID`（内部键 → CGI）配对，
#: 两个方向由 `test_qzone_napcat_channel` 对账。
#:
#: ⚠️ 前两行的 CGI 出口**生产路径不走**：发/删说说用 NapCat 的原生动作
#: （`send_qzone_msg` / `delete_qzone_msg`，`_PLATFORM_CALLS` 里那两条），
#: 能不用 cookie 就不用 cookie；CGI 构造器留着是为了"平台原生那条不可用时还有路可走"
#: 的完整性（v1.7.10 复核时确认：只有 `publish` / `delete` 两个构造器有两条路）。
QZONE_CGI_ACTIONS = {
    'publish_qzone_post': 'publish',
    'delete_qzone_post': 'delete',
    'comment_qzone_post': 'comment',
    'like_qzone_post': 'like',
    'forward_qzone_post': 'forward',
    'list_qzone_posts': 'moods',       # 指定 QQ = 那个人的说说
    'list_qzone_feeds': 'feed',        # 不指定 = 好友动态
    # v1.7.5：改可见范围（`emotion_cgi_update`）——**没有**平台原生动作，只能走 CGI。
    'set_qzone_visibility': 'update_visibility',
}

#: **内部动作键 → QZone CGI 动作名**。Chunk13 用它决定"这条动作有没有 CGI 出口"
#: （`_qzone_run_action` 不传 `cgi_action` 时的默认查表）。
#: （`QZONE_CGI_ACTIONS` 是**目录 id** → CGI，两者别混。）
#:
#: ⚠️ 这里的键是**本插件内部的选择名**，**不是任何后端的 action 名**：
#: `comment_qzone` / `like_qzone` / `forward_qzone` / `get_qzone_feeds` /
#: `get_qzone_msg_list` 在 AstrBot 世界的任何后端上都不存在（v1.7.1 曾把它们当"上游
#: Koishi QQ 空间适配器的扩展动作"、发给平台做回退，换回的就是
#: `retcode 1404 不支持的Api get_qzone_feeds`；v1.7.10 整条回退通道已删）。
#: NapCat 自己的空间动作**只有** `/send_qzone_msg` 与 `/delete_qzone_msg` 两条
#: （权威清单 https://napcat.apifox.cn/llms.txt：496813058e0 / 496813059e0），
#: 评论 / 点赞 / 转发 / 看好友动态 / 看某人说说 / 改可见范围**都只能由本插件自己
#: 打 QZone CGI**（`core/qzone_cgi.py`，前提是 NapCat 通道给得出 cookie）。
#: 前两行的键恰好等于 NapCat 的原生动作名（同一条能力有原生与 CGI 两条路），
#: 这也是全表里**唯一**两个可以原样发给平台的键。
QZONE_CGI_BY_ID = {
    'send_qzone_msg': 'publish',
    'delete_qzone_msg': 'delete',
    'comment_qzone': 'comment',
    'like_qzone': 'like',
    'forward_qzone': 'forward',
    'get_qzone_msg_list': 'moods',
    'get_qzone_feeds': 'feed',
    'set_qzone_visibility': 'update_visibility',
}

#: 内部动作键 → 中文标签。失败文案要点名"哪个动作需要什么"，别让用户拿动作名去猜。
QZONE_ACTION_LABELS: dict[str, str] = {
    'send_qzone_msg': '发表说说',
    'delete_qzone_msg': '删除说说',
    'comment_qzone': '评论说说',
    'like_qzone': '点赞说说',
    'forward_qzone': '转发说说',
    'get_qzone_msg_list': '看某人的说说',
    'get_qzone_feeds': '看好友动态',
    'set_qzone_visibility': '改说说可见范围',
}

#: **只读**的 CGI 动作：不产生任何副作用，重试永远安全。
QZONE_CGI_READ_ACTIONS = frozenset({'feed', 'moods'})

#: 只读的**内部**动作键——从 `QZONE_CGI_BY_ID` 派生（别在两处各抄一份）。
QZONE_READ_ACTIONS = frozenset(
    name for name, cgi in QZONE_CGI_BY_ID.items() if cgi in QZONE_CGI_READ_ACTIONS
)


def qzone_action_is_read(action: Any) -> bool:
    """这个动作是不是**只读**（重试安全）。内部动作键与 CGI 动作名两种写法都认。

    读写性质决定失败口径：只读动作拿不到响应就明说"可以重试"；写动作一律
    `ambiguous`（"可能已生效"，禁止自动重试）——发帖 / 删除 / 评论 / 点赞 / 转发 /
    改可见范围一个都不许重试。
    """
    name = str(action or '').strip()
    return name in QZONE_READ_ACTIONS or name in QZONE_CGI_READ_ACTIONS


def qzone_action_label(action: Any) -> str:
    """动作的中文标签（查不到就用动作名本身：宁可显示机器名，也别显示空白）。"""
    name = str(action or '').strip()
    return QZONE_ACTION_LABELS.get(name) or name or 'QQ 空间动作'

#: v1.7.6 删掉了 `QZONE_NAPCAT_ONLY_ACTIONS`：它只被测试引用，运行期没有任何消费点，
#: 而"哪些动作是 NapCat 专属"的**唯一真源**是目录里的
#: `platform_actions.napcat_actions()`（由每条 `PlatformAction.backends` 派生）。
#: 留着它就是第二个真源——两处迟早对不上，界面上标的和跑起来的就会不一致。
#:
#: v1.7.10 同理删掉了"NapCat 原生动作白名单"常量：上游 Koishi 的 QQ 空间适配器回退
#: 通道整条删掉之后，没有任何运行期代码需要它（"哪些键可以发给平台"由适配层
#: `_PLATFORM_CALLS` 一处表达）。NapCat API 清单的权威依据留在 `QZONE_CGI_BY_ID`
#: 的注释与 `test_astrbot_bridge.MissingPlatformActionTests` 的对账用例里。


class QzoneCgiUnavailable(QzoneActionError):
    """NapCat 通道用不了（拿不到 cookie / 传输层没有原始 HTTP 能力）。

    与"网络不好"是两回事：重试一万次也还是同一个结果，所以要按节流把原因与
    "下一步做什么"说给用户听（`chunk13.qzone_feed_sweep` 的可见 warn）。
    `ambiguous` 恒为 `False`：这条动作**没有发出去**，不涉及"结果未知"。
    """


def _cookie_string_from_frame(frame: Any) -> str:
    """从取 Cookie 的回执里读 Cookie 串（`get_credentials` / `get_cookies` **共用这一份**）。

    两个接口都是 `{'ok': True, 'data': {'cookies': …}}` 这个形状（传输层的
    `Transport.call_onebot` 契约；"剥掉 OneBot 信封"那一步在适配层
    `astrbot_bridge._onebot_payload_frame` 一处完成，两个动作走的是同一条路，
    所以这里**不**需要按动作分支）。拿不到就是空串。
    """
    return str(_pick(_pick(frame, 'data') or {}, 'cookies') or '')


async def qzone_cgi_auth(call: Any, login_call: Any = None) -> dict[str, Any]:
    """按 NapCat 方案取认证：`get_credentials` → `get_cookies`（各自逐域）+ `get_login_info`。

    `call` 是 OneBot 直通（`Transport.call_onebot`）；`login_call` 缺省复用 `call`。
    拿不到 `p_skey` 一律抛 `QzoneCgiUnavailable`（**不静默降级**：QQ 空间动作
    没有 cookie 就是做不了，得让上层说清楚）。

    **两层顺序，都照参考实现来**：

    1. **接口顺序** `QZONE_COOKIE_APIS`：先 `get_credentials`（参考插件
       `astrbot_plugin_qzone_tools` v5.7.5 `main.py:866` 的主路），不可用再 `get_cookies`
       （它的 `:870`）。两个接口参数相同（`domain` 必填）、Cookie 字段同名，解析共用
       `_cookie_string_from_frame`；"剥壳"在适配层一处完成。
    2. **域顺序** `QZONE_COOKIE_DOMAINS`：`user.qzone.qq.com` → `qzone.qq.com`。
       光有 Cookie 串不算数——没有 `p_skey` 就算不出 `g_tk`，等于这个域白问。

    都拿不到时失败文案**按接口分段、逐域写明原因**：这样"接口不存在（1404）"、
    "没登录"、"这个域没 p_skey" 三种现场一眼分得开。
    """
    from .qzone_cgi import qzone_auth_from_cookies

    if not callable(call):
        raise QzoneCgiUnavailable('QQ 空间（NapCat 通道）不可用：传输层没接上', None, None, False)
    sections: list[str] = []
    cookies = ''
    for api in QZONE_COOKIE_APIS:
        api_reasons: list[str] = []
        for domain in QZONE_COOKIE_DOMAINS:
            try:
                cookie_frame = await call(api, {'domain': domain})
            except Exception as error:  # noqa: BLE001 - 平台异常收敛成"这个域没戏"
                api_reasons.append('%s：调用异常（%s）' % (domain, error))
                continue
            if not _is_ok_frame(cookie_frame):
                head, detail = _frame_status_text(cookie_frame if _is_mapping(cookie_frame) else {})
                api_reasons.append('%s：%s %s' % (domain, head, detail))
                continue
            candidate = _cookie_string_from_frame(cookie_frame)
            if not candidate.strip():
                api_reasons.append('%s：平台没回 cookie' % domain)
                continue
            try:
                qzone_auth_from_cookies(candidate, '')
            except Exception as error:  # noqa: BLE001
                api_reasons.append('%s：%s' % (domain, error))
                continue
            cookies = candidate
            break
        if cookies:
            break
        sections.append('%s（%s）' % (api, '；'.join(api_reasons) or '没有可用结果'))
    if not cookies:
        raise QzoneCgiUnavailable(
            'QQ 空间（NapCat 通道）不可用：取登录凭据失败——%s' % '；'.join(sections),
            '/'.join(QZONE_COOKIE_APIS), None, False,
        )
    uin = ''
    info_call = login_call if callable(login_call) else call
    try:
        info_frame = await info_call('get_login_info', {})
        uin = str(_pick(_pick(info_frame, 'data') or {}, 'user_id') or '')
    except Exception:  # noqa: BLE001 - 取不到 uin 时下面的 cookie 解析还能兜
        uin = ''
    try:
        return qzone_auth_from_cookies(cookies, uin)
    except Exception as error:  # noqa: BLE001
        raise QzoneCgiUnavailable(
            'QQ 空间（NapCat 通道）不可用：%s' % error, '/'.join(QZONE_COOKIE_APIS), None, False,
        ) from error


def qzone_cgi_request(action: str, auth: Any, params: Mapping[str, Any]) -> tuple[str, str, dict[str, str], dict[str, str]]:
    """把一次 qzone 动作翻成 `(method, url, headers, data)`（纯函数，方便单测）。"""
    from . import qzone_cgi as cgi

    params = dict(params or {})
    if action == 'publish':
        return cgi.build_publish_request(
            auth, str(params.get('content') or ''),
            visible=int(_js_int_or(params.get('ugcRight', params.get('ugc_right')), 1) or 1),
            richval=str(params.get('richval') or ''), pic_bo=str(params.get('pic_bo') or ''),
        )
    if action == 'update_visibility':
        # v1.7.9：只发可见性 + 既有字段（正文原样带回、名单按档位拼）——**没有**富文本
        # 参数可传：参考实现的编辑构造器把那几个槽位全留空串（= 不改动富文本），
        # "重新下载 + 重新上传拿新 richval"那条路已整条删除（见 `docs/PORTING_NOTES.md` §43）。
        return cgi.build_update_visibility_request(
            auth, str(params.get('tid') or ''), str(params.get('content') or ''),
            int(_js_int_or(params.get('ugcRight', params.get('ugc_right')), 4) or 4),
            target_uins=params.get('targetUins', params.get('target_uins')) or (),
        )
    if action == 'upload_image':
        # 图片上传构造器：参考实现的逐字移植，**v1.7.9 起生产路径上没有调用方**
        # （当初唯一用途是重传原图拿新 richval）。内部动作——不在
        # `QZONE_CGI_ACTIONS` / `QZONE_CGI_BY_ID` 里（模型看不到它）。
        return cgi.build_upload_image_request(
            auth, str(params.get('picBase64', params.get('pic_base64')) or ''),
            str(params.get('filename') or 'filename'),
        )
    if action == 'delete':
        return cgi.build_delete_request(
            auth, str(params.get('tid') or ''),
            str(params.get('curkey') or ''), int(_js_int_or(params.get('timestamp'), 0) or 0),
        )
    if action == 'comment':
        return cgi.build_comment_request(
            auth, str(params.get('targetUin', params.get('target_uin')) or ''),
            str(params.get('tid') or params.get('topicId') or ''), str(params.get('content') or ''),
        )
    if action == 'like':
        return cgi.build_like_request(
            auth, str(params.get('targetUin', params.get('target_uin')) or ''),
            str(params.get('fid') or ''), str(params.get('curKey', params.get('cur_key')) or ''),
            str(params.get('uniKey', params.get('uni_key')) or ''),
        )
    if action == 'forward':
        return cgi.build_forward_request(
            auth, str(params.get('targetUin', params.get('target_uin')) or ''),
            str(params.get('tid') or ''), str(params.get('content') or ''),
        )
    if action == 'moods':
        return cgi.build_mood_list_request(
            auth, str(params.get('targetUin', params.get('target_uin')) or ''),
            int(_js_int_or(params.get('count'), 10) or 10),
        )
    if action == 'feed':
        return cgi.build_feed_request(
            auth, int(_js_int_or(params.get('page'), 1) or 1),
            int(_js_int_or(params.get('count'), 10) or 10),
        )
    raise QzoneActionError('QQ 空间（NapCat 通道）不支持的动作：%s' % action, action, None, False)


async def call_qzone_cgi(request: Any, call: Any, action: str, params: Any = None,
                        login_call: Any = None, auth: Any = None) -> dict[str, Any]:
    """走 "NapCat WS 方案" 执行一次 QQ 空间动作。

    `request` 是 `Transport.request_text` 的绑定方法（原始 HTTP）。返回形状与
    `call_qzone_action` 对齐（成功回动作结果，失败抛 `QzoneActionError`），
    这样上层的限流/审计/剧本留痕那一套**不用改**。

    `auth` 是给"一次动作要连打好几个 CGI"的场合准备的（当初的用例是 v1.7.8 的带图改
    可见范围：先上传 N 张图、再 update；**那条路 v1.7.9 已删**）：传进来就复用，不传就
    自己取一次（`qzone_cgi_auth`）——**不传时的行为与加这个参数之前逐字一致**。

    **失败分类与 `call_qzone_action` 同一套口径**（别在这里另造词汇）：

    * 传输层拿不到响应（`request_text` 回 `None`，或它抛了异常 / 超时）→
      **写**动作 `ambiguous=True`：请求**可能已经打到腾讯**了，结果未知，禁止自动
      重试；**只读**动作（`feed` / `moods`）`ambiguous=False` 且文案写"重试安全"
      ——读不产生副作用，没必要按写动作的保守口径吓自己；
    * 拿到响应而 `success_or_error` 判 `code != 0` → `ambiguous=False`：
      接口明确拒绝（没登录 / 风控 / 参数不对），重试是安全的；
    * 成功 → 回动作结果。`upload_image` 回的是上传回执里的 `data`（一张图的描述：
      `albumid` / `lloc` / `sloc` / `type` / `height` / `width` / `url`）——拿不到就
      按失败抛。这条分支同样**没有生产调用方**（与 `upload_image` 构造器一起保留）。
    """
    from . import qzone_cgi as cgi

    if not callable(request):
        raise QzoneCgiUnavailable('QQ 空间（NapCat 通道）不可用：传输层没有原始 HTTP 能力', action, None, False)
    read_only = qzone_action_is_read(action)
    if auth is None:
        auth = await qzone_cgi_auth(call, login_call)
    method, url, headers, data = qzone_cgi_request(action, auth, params or {})
    try:
        # `Transport.request_text` 的约定是"失败返回 None、绝不抛"，但传输层实现
        # 未必守得住（超时 / 断连在 HTTP 客户端里本来就是异常），而**写动作**一旦
        # 漏判就会变成"记 failed → 调用方重试 → 重复发帖"。
        text = await request(method, url, headers=headers, data=data)
    except QzoneActionError:
        raise
    except Exception as error:  # noqa: BLE001 - 与 `call_qzone_action` 同一个收敛口径
        raise QzoneActionError(
            _transport_failure_text(action, read_only, error), action, None, not read_only,
        ) from error
    if text is None:
        # 与上面那条同一条文案口径：写动作的 '结果未知，请勿自动重试' 是调用方认的那句话；
        # 只读动作（feed / moods）重试安全，走 `_transport_failure_text` 的另一句。
        raise QzoneActionError(
            _transport_failure_text(action, read_only, '请求没有回执（网络或平台拦截）'),
            action, None, not read_only,
        )
    if action == 'feed':
        items = cgi.feed_items_from_text(text)
        return {'feeds': items, 'count': len(items)}
    if action == 'moods':
        parsed = cgi.parse_moods_response(text)
        # `parse_moods_response` 回的是 `{'code': …, 'moods': [...]}`：**保留 code**
        # （它是区分"没登录/被风控"与"这人没发过说说"的唯一依据），列表在 `moods` 里。
        rows = parsed.get('moods') if isinstance(parsed, Mapping) else None
        rows = list(rows) if isinstance(rows, list) else []
        return {'posts': rows, 'count': len(rows), 'code': parsed.get('code') if isinstance(parsed, Mapping) else None}
    result = cgi.success_or_error(cgi.parse_jsonp(text) if text.lstrip().startswith('_preloadCallback') else _json_or_none(text))
    if not result.get('success'):
        raise QzoneActionError(
            '%s 失败：%s' % (action, result.get('message') or '未知错误'),
            action, result.get('code'), False,
        )
    if action == 'upload_image':
        # 上传回执有用的不是 `success_or_error` 那四个通用字段，而是 `data`（图片描述）。
        # 拿不到就**当场失败**：没有它拼不出 `richval`，而调用方（改带图说说的可见范围）
        # 唯一的安全出口是"拒绝"，绝不能带着空富文本块去重建说说。
        receipt = cgi.image_upload_receipt(_json_or_none(text))
        if not receipt:
            raise QzoneActionError(
                '%s 失败：回执里没有图片描述（data）' % action, action, result.get('code'), False,
            )
        return receipt
    return result


def _json_or_none(text: Any) -> Any:
    """CGI 回 JSON 时直接解析；不是 JSON 就交给 jsonp 解析器（它自己会容错）。"""
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        from .qzone_cgi import parse_jsonp
        return parse_jsonp(text)


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


#: 「改可见范围」动作的**五档中文枚举 → `ugc_right`**（`core/platform_actions.py`
#: 的 `QZONE_VISIBILITY_LABELS` 是模型/界面看到的那一份，两边的**顺序与键逐字相同**，
#: `plugin/tests/test_platform_actions.py` 有对账用例）。
#:
#: 值取自 `core/qzone_cgi.py::QZONE_VISIBLE`（权威依据见那里的注释：NapCat
#: `ValidUgcRights = [1, 4, 16, 64, 128]`）。**标签是用户指定的原话**，与
#: `qzone_visibility_label()`（上游移植的审计标签：4=好友可见、64=仅自己可见）
#: 措辞不同但指向同一组值——那边是剧本条目里的历史文案，别改它。
QZONE_VISIBILITY_VALUES: dict[str, int] = {
    '所有人可见': 1,
    '仅 QQ 好友可见': 4,
    '部分人可见': 16,
    '部分人不可见': 128,
    '仅自己可见': 64,
}


def qzone_visible_value(label: Any) -> Optional[int]:
    """五档中文标签 → `ugc_right`；不认识的标签返回 `None`（调用方报错，绝不猜一档）。"""
    if not isinstance(label, str):
        return None
    return QZONE_VISIBILITY_VALUES.get(label.strip())


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
    if action == 'visibility':
        # 改可见范围是**回合内即时**动作：不做延迟意图。理由有二——① 意图表要额外
        # 记「五档 + 名单」这套参数，而模型在回合内直接调它没有任何损失；② "过三小时
        # 偷偷改一条老说说的可见性"是用户不会预期的行为，宁可让它只在本轮生效。
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
