"""上游 `upstream/src/script/timeline-routing.ts`（17 行，v1.0.1-beta6-rebuild）的 Python 对应物。

时间路由判定
------------
上游注释 ——
``Routing changes model work, never prose resolution. A known schedule
boundary keeps the temporal editor even during a short conversation.``

也就是说：本函数只决定「要不要多花一次模型调用去做时间编辑」，绝不决定散文怎么写；
但只要窗口跨过了一个**已知的日程边界**，即使只是一次很短的对话，也要把时间编辑叫起来。

判定顺序（逐条照抄上游，短路行为必须一致）：
1. `phase == 'user-message'` → 否（实时用户回合永远不走时间导演）；
2. `phase == 'advance'` 或 `now - from > 20 分钟` → 是；
3. `from` 与 `now` 落在不同本地日历日 → 是；
4. 日程窗口里存在一个属于 `now` 那天、且其 `start`/`end` 本地时钟落在 `(start, end]`
   区间内的块 → 是；否则否。

时间助手统一从 `..time` 导入（`calendar_day_key` / `local_clock_minutes` 是
上游 `src/time.ts` 的对应物，`parse_dt` / `dt_ms` 是本移植版新增的统一助手）。

命名：上游参数 `from` 是 Python 保留字，改名为 `from_`（只改参数名）。
"""

from __future__ import annotations

from typing import Any, Optional

from ..time import calendar_day_key, dt_ms, local_clock_minutes, parse_dt
from ..types import NarrativePhase, SchedulePreplanWindow

__all__ = ['needs_timeline_director']

#: 上游 `20 * 60_000`：超过 20 分钟的窗口必须走时间导演。
_LONG_WINDOW_MS = 20 * 60_000


def _clock_minutes(clock: Any) -> Optional[int]:
    """把 ``'HH:MM'`` 解析成当日分钟数；非 ``H:MM`` 形状按"不匹配"处理。

    对应上游 ``const [hour, minute] = clock.split(':').map(Number)``：
    上游解析失败会得到 `NaN`，而 `NaN` 的任何比较都是 false，等价于本函数返回 None。
    """
    if not isinstance(clock, str):
        return None
    parts = clock.split(':')
    if len(parts) < 2:
        return None
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        return None
    return hour * 60 + minute


def needs_timeline_director(
    phase: NarrativePhase,
    from_: Any,
    now: Any,
    timezone: str,
    schedule: Optional[SchedulePreplanWindow] = None,
) -> bool:
    """上游 `needsTimelineDirector(phase, from, now, timezone, schedule?)`。"""
    if phase == 'user-message':
        return False
    from_dt = parse_dt(from_)
    now_dt = parse_dt(now)
    if from_dt is None or now_dt is None:
        # 上游签名保证是合法 Date；这里对不可解析输入保守返回 False，
        # 不因为一个坏时间戳就凭空发起一次模型调用。
        return False
    if phase == 'advance' or dt_ms(now_dt) - dt_ms(from_dt) > _LONG_WINDOW_MS:
        return True
    day = calendar_day_key(now_dt, timezone)
    if calendar_day_key(from_dt, timezone) != day:
        return True
    start = local_clock_minutes(from_dt, timezone)
    end = local_clock_minutes(now_dt, timezone)
    blocks = schedule.get('blocks') if isinstance(schedule, dict) else None
    if not isinstance(blocks, list):
        return False
    for block in blocks:
        if not isinstance(block, dict) or block.get('date') != day:
            continue
        for clock in (block.get('start'), block.get('end')):
            minutes = _clock_minutes(clock)
            if minutes is not None and minutes > start and minutes <= end:
                return True
    return False
