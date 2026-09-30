"""Token 用量统计（本移植版新增：控制台「Token 统计」页的数据层）。

与 `health.py` 的区别：健康指标是**内存滚动、重载归零**的运行时视图；这里要的是
**跨重启可查**的账本（按天/周/月/自选范围看历史），所以靠数据库持久化。

设计要点：

- 账本按 **(day, storyId, task, model)** 聚合，一行就是"某天、某部剧本、某个任务、
  某个模型"的累计量。**不存每次调用的明细**——一次长对话几百次调用，明细表会爆，
  而面板要看的本来就是聚合视图。
- `day` 用**本地日期**（`YYYY-MM-DD`）：用户按"今天/最近 7 天"看账，跨时区对齐到
  UTC 日界只会让人困惑。这一点在页面文案里写明。
- 命中率 = 缓存输入 / 总输入；输入为 0 时按 0 处理（不是 100%）。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional

__all__ = [
    'TOKEN_RANGES',
    'merge_usage',
    'normalize_range',
    'normalize_usage_record',
    'range_bounds',
    'summarize_usage',
    'usage_row_key',
]

#: 页面上可选的三个预设范围 + 自选。
TOKEN_RANGES = ('day', 'week', 'month', 'custom')


def _pick(value: Any, *keys: str) -> Any:
    if not isinstance(value, Mapping):
        return None
    for key in keys:
        found = value.get(key)
        if found is not None:
            return found
    return None


def _as_int(value: Any) -> int:
    """宽容取整：None / 非数字 / 负数一律按 0（账本里不该出现负数）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


def _as_date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace('Z', '+00:00')).date()
        except ValueError:
            try:
                return date.fromisoformat(text[:10])
            except ValueError:
                return None
    return None


def day_key(moment: Any = None, timezone_offset: Any = None) -> str:
    """某一时刻的**本地**日期键（`YYYY-MM-DD`）。

    规则（页面上按这个口径写文案）：

    - `timezone_offset` 给定时按该偏移换算（故事时区）；
    - 不给时一律用**服务器本地时间**——用户按"今天/最近 7 天"看账，跨时区对齐到
      UTC 日界只会让人困惑（东八区的晚上 8 点不该算成第二天）。
    """
    if isinstance(moment, datetime):
        stamp = moment
    elif isinstance(moment, str):
        parsed = None
        try:
            parsed = datetime.fromisoformat(moment.replace('Z', '+00:00'))
        except ValueError:
            parsed = None
        stamp = parsed or datetime.now(tz=timezone.utc)
    else:
        stamp = datetime.now(tz=timezone.utc)
    if isinstance(timezone_offset, timedelta):
        # 显式时区：按那个偏移切日（**不能再转回服务器本地**，否则偏移白给）。
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.astimezone(timezone(timezone_offset)).date().isoformat()
    if stamp.tzinfo is None:
        # 裸 datetime 当"本地墙上时间"处理，不做换算。
        return stamp.date().isoformat()
    return stamp.astimezone().date().isoformat()


def normalize_usage_record(record: Any, moment: Any = None, story_id: str = '') -> Optional[dict[str, Any]]:
    """把一次模型调用的用量记录转成账本增量；**没有任何 token 字段时返回 None**。

    `calls` 恒为 1（一次调用），即使某些网关不回 token 数也记账——面板上的
    "调用次数"因此不会因为网关不报用量而失真。
    """
    if not isinstance(record, Mapping):
        return None
    input_tokens = _pick(record, 'input_tokens', 'inputTokens')
    output_tokens = _pick(record, 'output_tokens', 'outputTokens')
    cached_tokens = _pick(record, 'cached_input_tokens', 'cachedInputTokens', 'cached_tokens')
    if input_tokens is None and output_tokens is None and cached_tokens is None:
        return None
    model = str(_pick(record, 'model') or '').strip()
    provider = str(_pick(record, 'provider_label', 'providerLabel', 'provider') or '').strip()
    task = str(_pick(record, 'task') or '').strip()
    return {
        'day': day_key(moment),
        'storyId': str(story_id or _pick(record, 'storyId', 'story_id') or ''),
        'task': task,
        'model': model,
        'provider': provider,
        'inputTokens': _as_int(input_tokens),
        'outputTokens': _as_int(output_tokens),
        'cachedTokens': _as_int(cached_tokens),
        'calls': 1,
    }


def usage_row_key(row: Any) -> tuple[str, str, str, str]:
    """账本行的聚合键：`(day, storyId, task, model)`。"""
    return (
        str(_pick(row, 'day') or ''),
        str(_pick(row, 'storyId', 'story_id') or ''),
        str(_pick(row, 'task') or ''),
        str(_pick(row, 'model') or ''),
    )


def merge_usage(existing: Any, delta: Any, moment: Any = None) -> dict[str, Any]:
    """把增量叠到已有行上（纯函数，调用方负责写库）。

    返回**完整的行**（含聚合键字段与 `updatedAt`）：调用方据此决定是
    `db_create` 还是 `db_set`，不需要再读一次库。
    """
    base = existing if isinstance(existing, Mapping) else {}
    merged: dict[str, Any] = {
        'day': str(_pick(base, 'day') or _pick(delta, 'day') or ''),
        'storyId': str(_pick(base, 'storyId', 'story_id') or _pick(delta, 'storyId', 'story_id') or ''),
        'task': str(_pick(base, 'task') or _pick(delta, 'task') or ''),
        'model': str(_pick(base, 'model') or _pick(delta, 'model') or ''),
        'provider': str(_pick(base, 'provider') or _pick(delta, 'provider') or ''),
        'inputTokens': _as_int(_pick(base, 'inputTokens', 'input_tokens')) + _as_int(_pick(delta, 'inputTokens', 'input_tokens')),
        'outputTokens': _as_int(_pick(base, 'outputTokens', 'output_tokens')) + _as_int(_pick(delta, 'outputTokens', 'output_tokens')),
        'cachedTokens': _as_int(_pick(base, 'cachedTokens', 'cached_tokens')) + _as_int(_pick(delta, 'cachedTokens', 'cached_tokens')),
        'calls': _as_int(_pick(base, 'calls')) + max(1, _as_int(_pick(delta, 'calls'))),
    }
    if not merged['provider']:
        merged['provider'] = ''
    merged['updatedAt'] = moment if moment is not None else datetime.now(tz=timezone.utc)
    if _pick(base, 'id') is not None:
        merged['id'] = _pick(base, 'id')
    return merged


def normalize_range(value: Any) -> str:
    """范围名：认不出来就回落 `day`（页面默认视图）。"""
    text = str(value or '').strip().lower()
    return text if text in TOKEN_RANGES else 'day'


def range_bounds(
    kind: Any, now: Any = None, from_value: Any = None, to_value: Any = None,
) -> dict[str, str]:
    """范围 → `{'from': 'YYYY-MM-DD', 'to': 'YYYY-MM-DD'}`（**闭区间**）。

    - `day` = 今天；`week` = 最近 7 天（含今天）；`month` = 最近 30 天（含今天）；
    - `custom` = 传进来的起止日期；缺一头就按另一头补（只给 `from` 时 `to` = 今天）；
    - 起止反了就交换；范围上限 366 天（防止一次查询把整张表拉进内存）。
    """
    anchor = _as_date(now) or datetime.now().date()
    resolved = normalize_range(kind)
    if resolved == 'day':
        return {'from': anchor.isoformat(), 'to': anchor.isoformat()}
    if resolved == 'week':
        return {'from': (anchor - timedelta(days=6)).isoformat(), 'to': anchor.isoformat()}
    if resolved == 'month':
        return {'from': (anchor - timedelta(days=29)).isoformat(), 'to': anchor.isoformat()}
    start = _as_date(from_value)
    end = _as_date(to_value)
    if start is None and end is None:
        return {'from': anchor.isoformat(), 'to': anchor.isoformat()}
    if start is None:
        start = end
    if end is None:
        end = anchor
    assert start is not None and end is not None
    if start > end:
        start, end = end, start
    if (end - start).days > 365:
        start = end - timedelta(days=365)
    return {'from': start.isoformat(), 'to': end.isoformat()}


def _rate(numerator: int, denominator: int) -> float:
    return (numerator / denominator) if denominator > 0 else 0.0


def _bucket(rows: Iterable[Mapping[str, Any]], key: str) -> list[dict[str, Any]]:
    """按某个字段分桶（模型 / 任务），并给出每桶的命中率。"""
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        label = str(_pick(row, key) or '') or '（未标注）'
        entry = grouped.setdefault(label, {
            key: label, 'inputTokens': 0, 'outputTokens': 0, 'cachedTokens': 0, 'calls': 0,
            'provider': '',
        })
        entry['inputTokens'] += _as_int(_pick(row, 'inputTokens', 'input_tokens'))
        entry['outputTokens'] += _as_int(_pick(row, 'outputTokens', 'output_tokens'))
        entry['cachedTokens'] += _as_int(_pick(row, 'cachedTokens', 'cached_tokens'))
        entry['calls'] += max(1, _as_int(_pick(row, 'calls')))
        # 连接名只做展示：同一模型行可能来自多条连接，取第一个非空的。
        if not entry['provider']:
            entry['provider'] = str(_pick(row, 'provider') or '')
    listed = sorted(grouped.values(), key=lambda item: (-item['inputTokens'] - item['outputTokens'], item[key]))
    for item in listed:
        item['totalTokens'] = item['inputTokens'] + item['outputTokens']
        item['hitRate'] = _rate(item['cachedTokens'], item['inputTokens'])
    return listed


def summarize_usage(
    rows: Iterable[Mapping[str, Any]],
    bounds: Optional[Mapping[str, Any]] = None,
    timezone_offset: Any = None,
) -> dict[str, Any]:
    """把账本行汇总成面板需要的形状：总量 / 按模型 / 按任务 / 按天序列。

    `bounds` 给定 `from`/`to` 时按闭区间过滤，并把**没有数据的日子补 0**——
    折线图不能因为某天没调用就断一段（"这天没说话"本身就是信息）。
    """
    listed = [row for row in rows if isinstance(row, Mapping)]
    start = _as_date(_pick(bounds or {}, 'from'))
    end = _as_date(_pick(bounds or {}, 'to'))
    if start is not None:
        listed = [row for row in listed if (day := _as_date(_pick(row, 'day'))) is not None and day >= start]
    if end is not None:
        listed = [row for row in listed if (day := _as_date(_pick(row, 'day'))) is not None and day <= end]

    totals = {
        'inputTokens': sum(_as_int(_pick(row, 'inputTokens', 'input_tokens')) for row in listed),
        'outputTokens': sum(_as_int(_pick(row, 'outputTokens', 'output_tokens')) for row in listed),
        'cachedTokens': sum(_as_int(_pick(row, 'cachedTokens', 'cached_tokens')) for row in listed),
        'calls': sum(max(1, _as_int(_pick(row, 'calls'))) for row in listed),
    }
    totals['totalTokens'] = totals['inputTokens'] + totals['outputTokens']
    totals['hitRate'] = _rate(totals['cachedTokens'], totals['inputTokens'])

    by_day: dict[str, dict[str, Any]] = {}
    for row in listed:
        key = str(_pick(row, 'day') or '')
        entry = by_day.setdefault(key, {
            'day': key, 'inputTokens': 0, 'outputTokens': 0, 'cachedTokens': 0, 'calls': 0,
        })
        entry['inputTokens'] += _as_int(_pick(row, 'inputTokens', 'input_tokens'))
        entry['outputTokens'] += _as_int(_pick(row, 'outputTokens', 'output_tokens'))
        entry['cachedTokens'] += _as_int(_pick(row, 'cachedTokens', 'cached_tokens'))
        entry['calls'] += max(1, _as_int(_pick(row, 'calls')))
    series: list[dict[str, Any]] = []
    if start is not None and end is not None:
        cursor = start
        while cursor <= end:
            key = cursor.isoformat()
            entry = by_day.get(key) or {
                'day': key, 'inputTokens': 0, 'outputTokens': 0, 'cachedTokens': 0, 'calls': 0,
            }
            entry['hitRate'] = _rate(entry['cachedTokens'], entry['inputTokens'])
            series.append(entry)
            cursor += timedelta(days=1)
    else:
        series = sorted(by_day.values(), key=lambda item: item['day'])
        for entry in series:
            entry['hitRate'] = _rate(entry['cachedTokens'], entry['inputTokens'])

    return {
        'totals': totals,
        'byModel': _bucket(listed, 'model'),
        'byTask': _bucket(listed, 'task'),
        'series': series,
    }
