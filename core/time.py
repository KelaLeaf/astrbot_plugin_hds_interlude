"""时间感知模块。

从上游 `src/time.ts` 移植。区分 UTC 与故事本地时间，并计算时段/日照预期，
为主叙事提供准确的时间锚点。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from functools import lru_cache
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_UTC = timezone.utc


@lru_cache(maxsize=64)
def resolve_timezone(timezone: str) -> str:
    """校验 IANA 时区；无效时回退到 'UTC'。"""
    candidate = (timezone or "").strip() or "UTC"
    try:
        ZoneInfo(candidate)
        return candidate
    except (ZoneInfoNotFoundError, ValueError):
        return "UTC"


def _local_parts(value: datetime, tz: str) -> dict:
    """返回该时刻在某时区下的本地时间部件。"""
    local = value.astimezone(ZoneInfo(tz))
    return {
        "utc": value.astimezone(_UTC).isoformat(),
        "local": local.strftime("%Y-%m-%d %H:%M:%S"),
        "date": local.strftime("%Y-%m-%d"),
        "time": local.strftime("%H:%M:%S"),
        "hour": local.hour,
        "weekday": local.strftime("%A"),
        "offset": local.strftime("%z"),
        "tzname": local.tzname() or "",
    }


def _period(hour: int) -> str:
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 18:
        return "afternoon"
    if 18 <= hour < 22:
        return "evening"
    return "night"


_PERIOD_ZH = {
    "morning": "上午",
    "afternoon": "下午",
    "evening": "傍晚/晚上",
    "night": "夜间",
}


def story_local_time_context(value: datetime, timezone: str) -> dict:
    """主叙事使用的权威时间上下文（UTC + 本地时间 + 时段 + 日照预期）。"""
    resolved = resolve_timezone(timezone)
    parts = _local_parts(value, resolved)
    hour = parts["hour"]
    period = _period(hour)
    if period in ("morning", "afternoon"):
        daylight = "normally daylight unless current weather, season, or setting explicitly says otherwise"
    elif period == "evening":
        daylight = "transitioning toward darkness; use the established season and setting"
    else:
        daylight = "normally dark outside unless the setting explicitly says otherwise"
    return {
        "timezone": resolved,
        "utc": parts["utc"],
        "local": parts["local"],
        "date": parts["date"],
        "time": parts["time"],
        "hour": hour,
        "weekday": parts["weekday"],
        "offset": parts["offset"],
        "period": period,
        "periodZh": _PERIOD_ZH[period],
        "daylightExpectation": daylight,
    }


def format_log_time(value: Optional[datetime], timezone: str) -> str:
    if not value:
        return "-"
    resolved = resolve_timezone(timezone)
    return value.astimezone(ZoneInfo(resolved)).strftime("%m-%d %H:%M:%S")


def local_clock_minutes(value: datetime, timezone: str) -> int:
    resolved = resolve_timezone(timezone)
    local = value.astimezone(ZoneInfo(resolved))
    return local.hour * 60 + local.minute


def calendar_day_key(value: datetime, timezone: str) -> str:
    resolved = resolve_timezone(timezone)
    return value.astimezone(ZoneInfo(resolved)).strftime("%Y-%m-%d")


_HHMM = re.compile(r"^\d{1,2}:\d{2}$")


def valid_hhmm(value: str) -> bool:
    return bool(_HHMM.match(value))
