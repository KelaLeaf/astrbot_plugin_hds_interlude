"""时间感知模块 —— 上游 `src/time.ts` 的逐句移植。

上游用 `Intl.DateTimeFormat` + IANA 时区；本移植版改用标准库 `zoneinfo.ZoneInfo`，
并在需要字符串的地方自行拼装与 Intl / `Date` 完全同形的输出：

* `Date#toISOString()` → `_iso_z()`：毫秒三位 + `Z`；
* `timeZoneName: 'shortOffset'` → `_short_offset()`：`GMT+8` / `GMT+5:30` / `GMT-5` / `GMT+0`；
* `hourCycle: 'h23'` → `f"{hour:02d}"`：`00`–`23`。

`formatterCache` / `timezoneCache` 两个模块级缓存按上游保留（`dict` 实现），
`time_formatter_cache_size()` 用于断言「每回合不重建格式化器」。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone as _timezone
from typing import Any, NamedTuple, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# 上游 `const formatterCache = new Map<string, Intl.DateTimeFormat>()`
_formatter_cache: dict[str, "_Formatter"] = {}
# 上游 `const timezoneCache = new Map<string, boolean>()`
_timezone_cache: dict[str, bool] = {}

_UTC = _timezone.utc
_EPOCH = datetime(1970, 1, 1, tzinfo=_UTC)

# 上游 `weekday: 'long'` 且 locale 为 en-US；显式常量表，不依赖进程 locale。
# 注意 Python `datetime.weekday()` 是周一=0（JS `getDay()` 是周日=0），故表从 Monday 起。
_WEEKDAY_LONG = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

# 上游 `periodZh` 字面表。
_PERIOD_ZH = {
    "morning": "上午",
    "afternoon": "下午",
    "evening": "傍晚/晚上",
    "night": "夜间",
}

# 上游 `Intl.DateTimeFormat` 可被读取的部件名（`hourCycle` 等选项不是部件）。
_PART_NAMES = frozenset({"year", "month", "day", "weekday", "hour", "minute", "second", "timeZoneName"})

# 上游 formatLogTime 的 options：zh-CN + month/day/hour/minute/second 两位 + h23。
# 注：ICU 的 zh-CN 数字日期分隔符实际是 '/'（`08/23 16:00:00`），本移植工程按约定固定为 '-'。
_LOG_TIME_FORMAT = "%m-%d %H:%M:%S"


class _Part(NamedTuple):
    """`Intl.DateTimeFormatPart`：`{ type, value }`。"""

    type: str
    value: str


def _ensure_aware(value: datetime) -> datetime:
    """上游 `Date` 恒为绝对时刻；naive `datetime` 按 UTC 解释。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=_UTC)
    return value


def _iso_z(value: datetime) -> str:
    """等价于 JS `Date#toISOString()`：UTC、毫秒三位、`Z` 结尾。"""
    instant = _ensure_aware(value).astimezone(_UTC)
    return (
        f"{instant.year:04d}-{instant.month:02d}-{instant.day:02d}"
        f"T{instant.hour:02d}:{instant.minute:02d}:{instant.second:02d}"
        f".{instant.microsecond // 1000:03d}Z"
    )


def _short_offset(local: datetime) -> str:
    """等价于 `timeZoneName: 'shortOffset'`：`GMT+0` / `GMT+8` / `GMT+5:30` / `GMT-5`。"""
    offset = local.utcoffset()
    if offset is None:
        return ""
    seconds = int(offset.total_seconds())
    sign = "-" if seconds < 0 else "+"
    hours, minutes = divmod(abs(seconds) // 60, 60)
    if minutes:
        return f"GMT{sign}{hours}:{minutes:02d}"
    return f"GMT{sign}{hours}"


def _find_part(parts: list[_Part], type_: str) -> Optional[str]:
    """上游 `parts.find(item => item.type === type)?.value`。"""
    for item in parts:
        if item.type == type_:
            return item.value
    return None


def _part(parts: list[_Part], type_: str) -> str:
    """上游 `part = type => parts.find(...)?.value ?? ''`。"""
    return _find_part(parts, type_) or ""


def _number(text: str) -> int:
    """上游 `Number(part('hour'))`：缺失部件时 `Number('') === 0`。"""
    try:
        return int(text)
    except (TypeError, ValueError):
        return 0


def _is_valid_date(value: Any) -> bool:
    """上游 `!value || Number.isNaN(value.getTime())`：null / 非法时间一律无效。"""
    return isinstance(value, datetime)


class _Formatter:
    """占位上游缓存的 `Intl.DateTimeFormat` 实例：缓存「已解析时区 + 选项」。"""

    __slots__ = ("kind", "locale", "timezone", "fields", "_zone")

    def __init__(self, kind: str, locale: str, timezone: str, fields: tuple[str, ...], zone: ZoneInfo):
        self.kind = kind
        self.locale = locale
        self.timezone = timezone
        self.fields = fields
        self._zone = zone

    def _local(self, value: datetime) -> datetime:
        return _ensure_aware(value).astimezone(self._zone)

    def part(self, value: datetime, type_: str) -> str:
        """按部件名取值；未请求/未知部件返回空串（与 Intl 的 `find` + `?? ''` 一致）。"""
        local = self._local(value)
        if type_ == "year":
            return f"{local.year:04d}"
        if type_ == "month":
            return f"{local.month:02d}"
        if type_ == "day":
            return f"{local.day:02d}"
        if type_ == "hour":
            return f"{local.hour:02d}"
        if type_ == "minute":
            return f"{local.minute:02d}"
        if type_ == "second":
            return f"{local.second:02d}"
        if type_ == "weekday":
            return _WEEKDAY_LONG[local.weekday()]
        if type_ == "timeZoneName":
            return _short_offset(local)
        return ""

    def format_to_parts(self, value: datetime) -> list[_Part]:
        return [_Part(name, self.part(value, name)) for name in self.fields]

    def format(self, value: datetime) -> str:
        """按 kind 固定上游各调用点的输出形状（locale 拼接规则在 Intl 里是隐含的）。"""
        local = self._local(value)
        if self.kind == "log":
            return local.strftime(_LOG_TIME_FORMAT)
        if self.kind == "day":
            return local.strftime("%Y-%m-%d")
        if self.kind == "clock":
            return local.strftime("%H:%M")
        # story（en-US）：`Sunday, 08/23/2026, 16:00:00 GMT+8`；上游只用其 formatToParts。
        return (
            f"{self.part(value, 'weekday')}, "
            f"{self.part(value, 'month')}/{self.part(value, 'day')}/{self.part(value, 'year')}, "
            f"{self.part(value, 'hour')}:{self.part(value, 'minute')}:{self.part(value, 'second')} "
            f"{self.part(value, 'timeZoneName')}"
        )


def _formatter(kind: str, locale: str, timezone: str, options: dict[str, str]) -> _Formatter:
    resolved = resolve_timezone(timezone)
    key = f"{kind}:{locale}:{resolved}"
    existing = _formatter_cache.get(key)
    if existing is not None:
        return existing
    fields = tuple(name for name in options if name in _PART_NAMES)
    created = _Formatter(kind, locale, resolved, fields, ZoneInfo(resolved))
    _formatter_cache[key] = created
    return created


def resolve_timezone(timezone: Optional[str]) -> str:
    """校验 IANA 时区名；非法（或空）时回落到 `UTC`，结果进 `timezoneCache`。"""
    candidate = (timezone or "").strip() or "UTC"
    cached = _timezone_cache.get(candidate)
    if cached is not None:
        return candidate if cached else "UTC"
    try:
        # 上游用 `new Intl.DateTimeFormat('en-US', { timeZone }).format(0)` 试探合法性。
        ZoneInfo(candidate)
        _timezone_cache[candidate] = True
        return candidate
    except (ZoneInfoNotFoundError, KeyError, ValueError, OSError):
        # ZoneInfoNotFoundError 是 KeyError 子类；ValueError/OSError 覆盖绝对路径等非法输入。
        _timezone_cache[candidate] = False
        return "UTC"


def story_local_time_context(value: datetime, timezone: str) -> dict[str, Any]:
    """主叙事使用的权威时间上下文：UTC + 故事本地时间 + 时段 + 日照预期。"""
    resolved_timezone = resolve_timezone(timezone)
    parts = _formatter("story", "en-US", resolved_timezone, {
        "year": "numeric", "month": "2-digit", "day": "2-digit", "weekday": "long",
        "hour": "2-digit", "minute": "2-digit", "second": "2-digit", "hourCycle": "h23",
        "timeZoneName": "shortOffset",
    }).format_to_parts(value)
    hour = _number(_part(parts, "hour"))
    period = "morning" if 5 <= hour < 12 \
        else "afternoon" if 12 <= hour < 18 \
        else "evening" if 18 <= hour < 22 \
        else "night"
    period_zh = _PERIOD_ZH[period]
    daylight_expectation = (
        "normally daylight unless current weather, season, or setting explicitly says otherwise"
        if period in ("morning", "afternoon")
        else "transitioning toward darkness; use the established season and setting"
        if period == "evening"
        else "normally dark outside unless the setting explicitly says otherwise"
    )
    date = f"{_part(parts, 'year')}-{_part(parts, 'month')}-{_part(parts, 'day')}"
    time = f"{_part(parts, 'hour')}:{_part(parts, 'minute')}:{_part(parts, 'second')}"
    return {
        "timezone": resolved_timezone,
        "utc": _iso_z(value),
        "local": f"{date} {time}",
        "date": date,
        "time": time,
        "hour": hour,
        "weekday": _part(parts, "weekday"),
        "offset": _part(parts, "timeZoneName"),
        "period": period,
        "periodZh": period_zh,
        "daylightExpectation": daylight_expectation,
    }


def format_log_time(value: Optional[datetime], timezone: str) -> str:
    """日志时间：`MM-DD HH:MM:SS`（故事时区）；无时间/非法时间返回 `-`。"""
    if not _is_valid_date(value):
        return "-"
    return _formatter("log", "zh-CN", timezone, {
        "month": "2-digit", "day": "2-digit", "hour": "2-digit", "minute": "2-digit",
        "second": "2-digit", "hourCycle": "h23",
    }).format(value)


def format_story_display_time(value: Optional[datetime], timezone: str) -> str:
    """面向用户的命令输出：与日志不同，附带故事时区偏移，避免时间轴看起来像 UTC。"""
    if not _is_valid_date(value):
        return "-"
    context = story_local_time_context(value, timezone)
    return f"{context['local']} {context['offset'] or 'GMT+0'}"


def local_clock_minutes(value: datetime, timezone: str) -> int:
    """本地时钟的「当日分钟数」（本地小时 * 60 + 分钟）。"""
    parts = _formatter("clock", "en-GB", timezone, {
        "hour": "2-digit", "minute": "2-digit", "hourCycle": "h23",
    }).format_to_parts(value)
    utc_value = _ensure_aware(value).astimezone(_UTC)
    hour_part = _find_part(parts, "hour")
    minute_part = _find_part(parts, "minute")
    hour = _number(hour_part) if hour_part is not None else utc_value.hour
    minute = _number(minute_part) if minute_part is not None else utc_value.minute
    return hour * 60 + minute


def calendar_day_key(value: datetime, timezone: str) -> str:
    """本地日历日 `YYYY-MM-DD`（上游 locale 为 en-CA，其数字日期正是 ISO 形状）。"""
    return _formatter("day", "en-CA", timezone, {
        "year": "numeric", "month": "2-digit", "day": "2-digit",
    }).format(value)


def time_formatter_cache_size() -> int:
    return len(_formatter_cache)


# --------------------------------------------------------------------------------------
# 以下四个助手是**本移植版新增**（上游 `src/time.ts` 没有对应函数），
# 依据 docs/PORT_PLAN.md §2「时间」：其它模块统一从这里 import，不要各写各的。
# --------------------------------------------------------------------------------------


def utc_now() -> datetime:
    """当前时刻（timezone-aware，UTC）。"""
    return datetime.now(_UTC)


def _from_ms(milliseconds: float) -> Optional[datetime]:
    try:
        return _EPOCH + timedelta(milliseconds=float(milliseconds))
    except (OverflowError, ValueError):
        return None


def parse_dt(value: Any) -> Optional[datetime]:
    """解析上游时间：`datetime` / ISO 字符串 / 毫秒数；无法解析时返回 `None`。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return _ensure_aware(value)
    if isinstance(value, (int, float)):
        return _from_ms(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        normalized = f"{text[:-1]}+00:00" if text[-1] in ("Z", "z") else text
        try:
            return _ensure_aware(datetime.fromisoformat(normalized))
        except ValueError:
            pass
        try:
            return _from_ms(int(text))
        except ValueError:
            return None
    return None


def iso(value: Any) -> Optional[str]:
    """ISO-8601 字符串：UTC、毫秒三位、`Z` 结尾（与上游 `toISOString()` 同形）。"""
    parsed = parse_dt(value)
    if parsed is None:
        return None
    return _iso_z(parsed)


def dt_ms(value: Any) -> int:
    """毫秒时间戳（等价上游 `Date#getTime()`）；无法解析时返回 `0`。"""
    parsed = parse_dt(value)
    if parsed is None:
        return 0
    delta = parsed.astimezone(_UTC) - _EPOCH
    return (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000
