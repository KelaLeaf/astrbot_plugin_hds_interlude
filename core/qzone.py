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


# --------------------------------------------------------------------------- #
# NapCat WebSocket 方案（本移植版新增，参考 Eganchiyu/qzone-sdk 的 NapCat 认证）
# --------------------------------------------------------------------------- #

#: 取 Cookie 的域（腾讯只认这个域下的 p_skey）。
QZONE_COOKIE_DOMAIN = 'user.qzone.qq.com'

#: 走 "NapCat WS 方案" 的 qzone 动作 → CGI 动作名（`core/qzone_cgi.py` 里的构造函数）。
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

#: 平台动作名 → CGI 动作名。Chunk13 用它决定"这条动作能不能走 NapCat WS 通道"
#: （`_qzone_run_action` 不传 `cgi_action` 时的默认查表）。
#: （`QZONE_CGI_ACTIONS` 是**目录 id** → CGI，两者别混。）
#:
#: **唯一例外**：`set_qzone_visibility`（v1.7.5）。NapCat 与 SnowLuma 都**没有**
#: "改说说可见范围"这条原生动作（NapCat 扩展动作只有 `send_qzone_msg` /
#: `delete_qzone_msg`），所以这里的键直接用**目录 id**——它没有平台名字可用。
#: 这个动作也只能走 CGI（`chunk13` 拿不到 cookie 时明确失败，不回落平台）。
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

#: 这几个动作**只有** NapCat 的 `get_cookies` 通道能做——SnowLuma 那套扩展动作
#: 在我们这里只当回退，纯 NapCat 环境下也能工作。
QZONE_NAPCAT_ONLY_ACTIONS = frozenset({
    'forward_qzone_post', 'list_qzone_feeds',
})


class QzoneCgiUnavailable(QzoneActionError):
    """拿不到 NapCat cookie（没装 NapCat / 没登录 / 平台不是 OneBot）。"""


async def qzone_cgi_auth(call: Any, login_call: Any = None) -> dict[str, Any]:
    """按 qzone-sdk 的 NapCat 方案取认证：`get_cookies` + `get_login_info`。

    `call` 是 OneBot 直通（`Transport.call_onebot`）；`login_call` 缺省复用 `call`。
    拿不到 `p_skey` 一律抛 `QzoneCgiUnavailable`（**不静默降级**：QQ 空间写动作
    没有 cookie 就是做不了，得让上层说清楚）。
    """
    from .qzone_cgi import qzone_auth_from_cookies

    if not callable(call):
        raise QzoneCgiUnavailable('QQ 空间（NapCat 通道）不可用：传输层没接上', None, None, False)
    try:
        cookie_frame = await call('get_cookies', {'domain': QZONE_COOKIE_DOMAIN})
    except Exception as error:  # noqa: BLE001 - 平台异常收敛成"不可用"
        raise QzoneCgiUnavailable(
            'QQ 空间（NapCat 通道）不可用：get_cookies 失败（%s）' % error, 'get_cookies', None, True,
        ) from error
    if not _is_ok_frame(cookie_frame):
        head, detail = _frame_status_text(cookie_frame if _is_mapping(cookie_frame) else {})
        raise QzoneCgiUnavailable(
            'QQ 空间（NapCat 通道）不可用：%s %s' % (head, detail), 'get_cookies', None, False,
        )
    cookies = _pick(_pick(cookie_frame, 'data') or {}, 'cookies') or ''
    if not str(cookies).strip():
        raise QzoneCgiUnavailable('QQ 空间（NapCat 通道）不可用：平台没回 cookie', 'get_cookies', None, False)
    uin = ''
    info_call = login_call if callable(login_call) else call
    try:
        info_frame = await info_call('get_login_info', {})
        uin = str(_pick(_pick(info_frame, 'data') or {}, 'user_id') or '')
    except Exception:  # noqa: BLE001 - 取不到 uin 时下面的 cookie 解析还能兜
        uin = ''
    try:
        return qzone_auth_from_cookies(str(cookies), uin)
    except Exception as error:  # noqa: BLE001
        raise QzoneCgiUnavailable(
            'QQ 空间（NapCat 通道）不可用：%s' % error, 'get_cookies', None, False,
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
        return cgi.build_update_visibility_request(
            auth, str(params.get('tid') or ''), str(params.get('content') or ''),
            int(_js_int_or(params.get('ugcRight', params.get('ugc_right')), 4) or 4),
            target_uins=params.get('targetUins', params.get('target_uins')) or (),
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
                        login_call: Any = None) -> dict[str, Any]:
    """走 "NapCat WS 方案" 执行一次 QQ 空间动作。

    `request` 是 `Transport.request_text` 的绑定方法（原始 HTTP）。返回形状与
    `call_qzone_action` 对齐（成功回动作结果，失败抛 `QzoneActionError`），
    这样上层的限流/审计/剧本留痕那一套**不用改**。
    """
    from . import qzone_cgi as cgi

    if not callable(request):
        raise QzoneCgiUnavailable('QQ 空间（NapCat 通道）不可用：传输层没有原始 HTTP 能力', action, None, False)
    auth = await qzone_cgi_auth(call, login_call)
    method, url, headers, data = qzone_cgi_request(action, auth, params or {})
    text = await request(method, url, headers=headers, data=data)
    if text is None:
        raise QzoneActionError('%s 失败：请求没有回执（网络或平台拦截）' % action, action, None, True)
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
