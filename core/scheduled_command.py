"""定时命令的纯逻辑（本移植版新增）：cron 解析、下次执行时刻、可排期命令目录。

为什么自己写 cron 而不引依赖：只需要 5 段标准 cron 的**下一次触发时刻**，标准库
`datetime` 足够；引一个第三方库只为这一件事不值得（项目硬约束是"Python 侧不加依赖"）。

支持的标准语法（够用且可解释）：
- 5 段：`分 时 日 月 周`
- 每段：`*`、`a`、`a-b`、`a-b/n`、`*/n`、`a,b,c`
- 周：0–6（0 = 周日），也接受 7 当周日
不支持：`@daily` 之类的别名、秒级、`L`/`W`/`#` 等扩展（解析不出来就**明确拒绝**，
不含糊地当成"每天"）。

口径与 Token 统计一致：**按服务器本地时间**判定"到点"。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

__all__ = [
    'DEFAULT_COMMAND_CATALOG',
    'MAX_LOOKAHEAD_DAYS',
    'cron_next_run',
    'describe_cron',
    'normalize_cron',
    'parse_cron',
    'parse_iso_datetime',
]

#: 往后最多找 366 天；找不到就当"永不匹配"（例如 `0 0 30 2 *`）。
MAX_LOOKAHEAD_DAYS = 366

_FIELD_BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))


def _parse_field(text: str, low: int, high: int) -> Optional[frozenset[int]]:
    """解析一段 cron 字段；非法返回 None。"""
    raw = str(text or '').strip()
    if not raw:
        return None
    values: set[int] = set()
    for chunk in raw.split(','):
        chunk = chunk.strip()
        if not chunk:
            return None
        step = 1
        if '/' in chunk:
            chunk, _, step_text = chunk.partition('/')
            if not step_text.strip().isdigit():
                return None
            step = int(step_text)
            if step <= 0:
                return None
            chunk = chunk.strip() or '*'
        if chunk == '*':
            start, end = low, high
        elif '-' in chunk.lstrip('-'):
            start_text, _, end_text = chunk.partition('-')
            if not (start_text.strip().isdigit() and end_text.strip().isdigit()):
                return None
            start, end = int(start_text), int(end_text)
        else:
            if not chunk.isdigit():
                return None
            start = end = int(chunk)
        if start < low or end > high or start > end:
            return None
        values.update(range(start, end + 1, step))
    if not values:
        return None
    # 周日的两种写法（0 与 7）都归一到 0。
    if (low, high) == (0, 6) and 7 in values:
        values.discard(7)
        values.add(0)
    return frozenset(values)


def parse_cron(expression: Any) -> Optional[tuple[frozenset[int], ...]]:
    """把 5 段 cron 解析成 5 个取值集合；非法返回 None。"""
    if not isinstance(expression, str):
        return None
    parts = expression.split()
    if len(parts) != 5:
        return None
    parsed: list[frozenset[int]] = []
    for text, (low, high) in zip(parts, _FIELD_BOUNDS):
        field = _parse_field(text, low, high)
        if field is None:
            return None
        parsed.append(field)
    return tuple(parsed)


def normalize_cron(expression: Any) -> Optional[str]:
    """解析成功就回一段**规范化的 5 段文本**（写库前统一形状），失败回 None。"""
    parsed = parse_cron(expression)
    if parsed is None:
        return None
    parts = []
    for field, (low, high) in zip(parsed, _FIELD_BOUNDS):
        # 覆盖整段就还原成 `*`：既好读，也不至于把 `string(64)` 的列写爆。
        parts.append('*' if field == frozenset(range(low, high + 1)) else ','.join(
            str(value) for value in sorted(field)))
    return ' '.join(parts)


def cron_next_run(expression: Any, after: Any = None, *, max_days: int = MAX_LOOKAHEAD_DAYS) -> Optional[datetime]:
    """`after` 之后的第一个触发时刻（严格大于 `after`）。

    实现是**分钟级暴力推进**（一年最多 52 万次预判，但每步只做集合判定，实测毫秒级），
    比把 cron 展开成日历简单得多，也不容易写错闰年/跨月边界。
    """
    parsed = parse_cron(expression)
    if parsed is None:
        return None
    moment = parse_iso_datetime(after)
    if moment is None:
        moment = datetime.now(timezone.utc) if after is None else None
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone()  # 本地时间口径
    cursor = moment.replace(second=0, microsecond=0) + timedelta(minutes=1)
    minutes, hours, days, months, weekdays = parsed
    for _ in range(max_days * 24 * 60):
        # 周日的 0/7 归一后，`datetime.weekday()`（周一=0）要换算成 cron 的（周日=0）。
        if (cursor.minute in minutes and cursor.hour in hours and cursor.day in days
                and cursor.month in months and ((cursor.weekday() + 1) % 7) in weekdays):
            return cursor
        cursor += timedelta(minutes=1)
    return None


def describe_cron(expression: Any) -> str:
    """给用户看的一句话说明（控制台/日志用）。解析不了就照原样回。"""
    parsed = parse_cron(expression)
    if parsed is None:
        return str(expression or '')
    minutes, hours, days, months, weekdays = parsed
    if len(minutes) == 60 and len(hours) == 24:
        return '每分钟'
    if len(hours) == 24 and minutes == frozenset({0}):
        return '每小时整点'
    if len(days) == 31 and len(months) == 12 and len(weekdays) == 7 and len(minutes) == 1 and len(hours) == 1:
        return '每天 %02d:%02d' % (next(iter(hours)), next(iter(minutes)))
    return str(expression)


def parse_iso_datetime(value: Any) -> Optional[datetime]:
    """宽容地解析 ISO-8601 / epoch 秒 / epoch 毫秒；解析不出来回 None。

    `Z` 结尾与 `+08:00` 都要认（宿主与模型都可能写这两种）；裸数字按**秒**理解、
    超过 1e11 视为毫秒（与 JS 的 `Date` 习惯对齐）。
    """
    if value is None or value == '':
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if number > 1e11:
            number /= 1000.0
        try:
            return datetime.fromtimestamp(number, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith(('Z', 'z')):
        text = text[:-1] + '+00:00'
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


#: 可排期的命令目录（白名单）。每一项的 `handler` 是 service 上的成员名。
#: 刻意**只放本插件自己的内部动作**：定时命令是"她的日常作息"，不是"给用户开一个后门"。
DEFAULT_COMMAND_CATALOG: dict[str, dict[str, str]] = {
    'advance': {
        'label': '推进一次',
        'summary': '按当前情景推进一段叙事（相当于一次后台回合）',
        'handler': 'scheduled_advance',
    },
    'compact': {
        'label': '整理记忆',
        'summary': '整理一次场景并压缩成长期记忆',
        'handler': 'scheduled_compact',
    },
    'world_seed': {
        'label': '播种世界事件',
        'summary': '让世界播种器产出下一批环境事件',
        'handler': 'scheduled_world_seed',
    },
    'qzone_feed': {
        'label': '浏览好友动态',
        'summary': '看一轮 QQ 空间好友动态（需要空间功能已开）',
        'handler': 'scheduled_qzone_feed',
    },
}
