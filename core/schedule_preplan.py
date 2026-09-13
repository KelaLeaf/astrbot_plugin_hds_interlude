"""日程预排（Schedule Preplan）—— 上游 `src/schedule-preplan.ts` 的逐句移植。

上游是 Koishi / TypeScript（v1.0.1-beta6-rebuild，432 行）；本文件是
`docs/PORT_PLAN.md` §1 目录映射表里的 `src/schedule-preplan.ts` → `core/schedule_preplan.py`。

这一层写的是**计划**，绝不是已观察到的事实
-------------------------------------------------
* 若干条**周规律**（regime）：带 `from`/`to` 生效区间（`YYYY-MM-DD`）与按星期分组的
  时间块；同一日期被多条 regime 覆盖时，取 `from` 最大（最近开始）的那条。
* 若干条**例外日**（exception）：`replace` 整天替换，`patch` 先删 `removeBlockIds`
  再追加 `blocks`。
* `materializeSchedulePreplan()` 把两者按 `horizonDays` 展开成逐日时间块（每天最多 12 块，
  重叠按 `fixed > routine > flexible > open` 优先级取舍）。
* `schedulePreplanWindow()` 只把**未来约十二小时**投影给主叙事；窗口内标记
  `tentative` 的候选（granular 模式）由稳定哈希决定当天是否成立，且远处只暴露
  「可能的个人安排」这种模糊措辞，临近 `candidateRevealMinutes` 才揭示原文。

移植约定
--------
* 时间：`now` / `createdAt` / `updatedAt` 一律 timezone-aware `datetime`（统一 UTC，
  见 `core/time.py`）；**日期键与 `SchedulePreplanRecord.validFrom` 等继续是
  `string(10)` 的 `YYYY-MM-DD`**（PORT_PLAN §2「时间」：上游存字符串的地方继续存字符串）。
* 键名：写出侧一律 snake_case（`story_id` / `valid_from` / `source_entry_ids` …
  与 `core/types.py` 的 TypedDict 对齐）；读取侧**同时接受上游 camelCase**，因为上游
  `src/database.ts` 的列名就是 camelCase，读回旧数据行必须仍能识别。
* JS 语义：`Number()` / `String()` / `Math.imul` / `Array#sort` 稳定性 / `Map` 去重保序
  / `undefined` 与 `null` 的区别（用哨兵 `_UNDEFINED`）全部按 JS 行为实现，
  见文件末尾的私有助手；`Math.max(...)`、`Array#slice` 等边界逐条照抄。
* 纯函数：无 I/O、无全局可变状态、不 import astrbot。
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone as _timezone
from typing import Any, Literal, Optional, TypedDict

from .time import calendar_day_key, local_clock_minutes, parse_dt, story_local_time_context
from .types import (
    SchedulePreplanBlock,
    SchedulePreplanDay,
    SchedulePreplanException,
    SchedulePreplanProposal,
    SchedulePreplanRecord,
    SchedulePreplanRegime,
    SchedulePreplanWindow,
    SchedulePreplanWindowBlock,
    ScriptEntry,
)

# ============================================================ 配置

#: 上游 `const WEEKDAYS`：顺序对齐 JS `Date#getUTCDay()`（0 = sunday）。
_WEEKDAYS = ('sunday', 'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday')
#: 上游 `const KINDS`。
_KINDS = ('fixed', 'routine', 'flexible', 'open')
#: 上游 `const PRIORITY`：重叠取舍时数值大者胜。
_PRIORITY = {'fixed': 4, 'routine': 3, 'flexible': 2, 'open': 1}
#: 上游 `resolveSchedulePreplanConfig` 里内联的 variationLevel 白名单。
_VARIATION_LEVELS = ('stable', 'contextual', 'granular')
#: 上游 `normalizeProposal` 里内联的 outcome 白名单（顺序照抄）。
_OUTCOMES = ('unchanged', 'extend', 'patch', 'replace')
#: 上游 `Number.MAX_SAFE_INTEGER`。
_MAX_SAFE_INTEGER = 9007199254740991
#: 上游 `new Date(0)`。
_EPOCH = datetime(1970, 1, 1, tzinfo=_timezone.utc)


class SchedulePreplanConfig(TypedDict, total=False):
    """上游 `src/schedule-preplan.ts` 的 `SchedulePreplanConfig`
    （它不在 `types.ts` 里，故随本模块移植）。

    字段名照 PORT_PLAN 转 snake_case；`resolve_schedule_preplan_config()` 的输出
    七个字段全部就位，传入的配置可以缺省（等价上游的 `Partial<...>`）。
    """

    enabled: bool
    horizon_days: int
    review_after_local_hour: int
    anchor_auto_advance: bool
    variation_level: Literal['stable', 'contextual', 'granular']
    candidate_activation_probability: float
    candidate_reveal_minutes: int


#: 上游 `DEFAULT_SCHEDULE_PREPLAN_CONFIG`（默认值逐字照抄）。
DEFAULT_SCHEDULE_PREPLAN_CONFIG: SchedulePreplanConfig = {
    'enabled': True,
    'horizon_days': 14,
    'review_after_local_hour': 3,
    'anchor_auto_advance': True,
    'variation_level': 'stable',
    'candidate_activation_probability': 0.25,
    'candidate_reveal_minutes': 120,
}


def resolve_schedule_preplan_config(value: Any = None) -> SchedulePreplanConfig:
    """上游 `resolveSchedulePreplanConfig()`：把任意输入收敛成完整配置。

    上游在这一段全靠 `value?.x` 与 `Number()`/`String()` 的隐式语义：
    键**不存在**（`undefined`）走 fallback，显式 `null` 则按 `Number(null) === 0` 参与夹取。
    本移植用哨兵 `_UNDEFINED` 区分这两种情况（见 `_pick`）。
    """
    raw: Any = value if isinstance(value, dict) else {}
    variation_level = _pick(raw, 'variationLevel', 'variation_level')
    return {
        # `value?.enabled !== false`：只有字面量 false 才关闭。
        'enabled': _pick(raw, 'enabled') is not False,
        'horizon_days': _clamp_int(_pick(raw, 'horizonDays', 'horizon_days'), 3, 30, 14),
        'review_after_local_hour': _clamp_int(_pick(raw, 'reviewAfterLocalHour', 'review_after_local_hour'), 0, 23, 3),
        'anchor_auto_advance': _pick(raw, 'anchorAutoAdvance', 'anchor_auto_advance') is not False,
        # `['stable','contextual','granular'].includes(String(value?.variationLevel))`：
        # 命中时返回**原值**（上游 `value!.variationLevel as ...`），否则回落 'stable'。
        'variation_level': variation_level if _js_string(variation_level) in _VARIATION_LEVELS else 'stable',
        'candidate_activation_probability': _clamp_number(
            _pick(raw, 'candidateActivationProbability', 'candidate_activation_probability'), 0.05, 0.5, 0.25),
        'candidate_reveal_minutes': _clamp_int(
            _pick(raw, 'candidateRevealMinutes', 'candidate_reveal_minutes'), 15, 360, 120),
    }


# ============================================================ 记录归一化


def normalize_schedule_preplan_record(value: Any) -> Optional[SchedulePreplanRecord]:
    """上游 `normalizeSchedulePreplanRecord()`：对未知输入做防御性读取。

    `validFrom` / `validThrough` 必须都是合法日期键，否则整条记录作废；
    `createdAt` / `updatedAt` 无法解析时回落 `new Date(0)`。
    """
    if not _is_record(value) or not isinstance(_pick(value, 'storyId', 'story_id'), str):
        return None
    valid_from = _date_key(_pick(value, 'validFrom', 'valid_from'))
    valid_through = _date_key(_pick(value, 'validThrough', 'valid_through'))
    if not valid_from or not valid_through:
        return None
    return {
        'story_id': _pick(value, 'storyId', 'story_id'),
        'revision': _clamp_int(_pick(value, 'revision'), 0, 1_000_000, 0),
        'timezone': _text(_pick(value, 'timezone'), 127) or 'UTC',
        'valid_from': valid_from,
        'valid_through': valid_through,
        'last_reviewed_local_date': _date_key(_pick(value, 'lastReviewedLocalDate', 'last_reviewed_local_date')) or '',
        'last_evidence_entry_id': _clamp_int(_pick(value, 'lastEvidenceEntryId', 'last_evidence_entry_id'), 0, _MAX_SAFE_INTEGER, 0),
        'review_reason': _text(_pick(value, 'reviewReason', 'review_reason'), 500),
        'regimes': _normalize_regimes(_pick(value, 'regimes')),
        'exceptions': _normalize_exceptions(_pick(value, 'exceptions')),
        'materialized_days': _normalize_days(_pick(value, 'materializedDays', 'materialized_days')),
        'created_at': _valid_date(_pick(value, 'createdAt', 'created_at')) or _EPOCH,
        'updated_at': _valid_date(_pick(value, 'updatedAt', 'updated_at')) or _EPOCH,
    }


# ============================================================ 复核判定


def schedule_preplan_review_due(
    record: Optional[SchedulePreplanRecord],
    now: datetime,
    timezone: str,
    config: SchedulePreplanConfig,
) -> bool:
    """上游 `schedulePreplanReviewDue()`：本地日切换 + 过 `reviewAfterLocalHour` 才复核。"""
    if not _pick(config, 'enabled'):
        return False
    today = calendar_day_key(now, timezone)
    if record is None:
        return True
    if _pick(record, 'timezone') != timezone:
        return True
    if _pick(record, 'lastReviewedLocalDate', 'last_reviewed_local_date') == today:
        return False
    threshold = _js_number(_pick(config, 'reviewAfterLocalHour', 'review_after_local_hour'))
    # 上游 `localClockMinutes(now, timezone) >= config.reviewAfterLocalHour * 60`；
    # 任一侧为 NaN 时 JS 比较恒为 false。
    if threshold is None:
        return False
    return local_clock_minutes(now, timezone) >= threshold * 60


def schedule_preplan_needs_model(
    record: Optional[SchedulePreplanRecord],
    evidence: list[ScriptEntry],
    today: str,
    timezone: str,
    config: SchedulePreplanConfig,
) -> bool:
    """上游 `schedulePreplanNeedsModel()`：是否值得再叫一次模型。

    一条**显式的空记录**表示「已复核过，但还没有可靠的重复日程」——它必须让扫描安静下来，
    否则每次扫都会白花一次模型请求。
    """
    if record is None or _pick(record, 'timezone') != timezone:
        return True
    last_evidence_entry_id = _num_or_nan(_pick(record, 'lastEvidenceEntryId', 'last_evidence_entry_id'))
    for entry in evidence:
        entry_id = _js_number(_pick(entry, 'id'))
        if entry_id is not None and entry_id > last_evidence_entry_id:
            return True
    regimes = _pick(record, 'regimes') or []
    # 上游 `if (!record.regimes.length) return false`
    if not regimes:
        return False
    coverage_target = _add_date(today, max(1, int(_num_or_nan(_pick(config, 'horizonDays', 'horizon_days'))) - 3))
    if not any(_regime_covers(regime, coverage_target) for regime in regimes):
        return True
    return _js_lt(_pick(record, 'validThrough', 'valid_through'), coverage_target)


# ============================================================ 写入


#: 记录字段的上游 camelCase 拼写 → 本移植版 snake_case（PORT_PLAN §2「命名」）。
_RECORD_ALIASES = {
    'storyId': 'story_id',
    'validFrom': 'valid_from',
    'validThrough': 'valid_through',
    'lastReviewedLocalDate': 'last_reviewed_local_date',
    'lastEvidenceEntryId': 'last_evidence_entry_id',
    'reviewReason': 'review_reason',
    'materializedDays': 'materialized_days',
    'createdAt': 'created_at',
    'updatedAt': 'updated_at',
}


def _canonical_record(record: Any) -> dict[str, Any]:
    """把记录里已知字段的上游 camelCase 键折叠成 snake_case（写出侧只写 snake_case）。

    上游只有一种拼写，不存在这个问题；本移植在 `refresh_schedule_preplan()` 的
    `{...record}` 展开前过一道，避免把「同一字段的 camelCase 旧值与 snake_case 新值」
    同时写进记录（后续读取会歧义）。
    """
    if not isinstance(record, dict):
        return {}
    canonical = {key: value for key, value in record.items() if key not in _RECORD_ALIASES}
    for camel, snake in _RECORD_ALIASES.items():
        if camel in record and snake not in record:
            canonical[snake] = record[camel]
    return canonical


def refresh_schedule_preplan(
    record: SchedulePreplanRecord,
    today: str,
    timezone: str,
    config: SchedulePreplanConfig,
    now: datetime,
    reason: str = 'Daily review found no schedule-changing evidence.',
) -> SchedulePreplanRecord:
    """上游 `refreshSchedulePreplan()`：只把窗口前移、重排物化日，内容一字不动。"""
    valid_through = _add_date(today, int(_num_or_nan(_pick(config, 'horizonDays', 'horizon_days'))) - 1)
    return {
        **_canonical_record(record),
        'timezone': timezone,
        'valid_from': today,
        'valid_through': valid_through,
        'last_reviewed_local_date': today,
        'review_reason': reason,
        'materialized_days': materialize_schedule_preplan(
            _pick(record, 'regimes') or [], _pick(record, 'exceptions') or [], today,
            int(_num_or_nan(_pick(config, 'horizonDays', 'horizon_days')))),
        'updated_at': now,
    }


def apply_schedule_preplan_proposal(
    current: Optional[SchedulePreplanRecord],
    proposal_value: Any,
    evidence: list[ScriptEntry],
    today: str,
    timezone: str,
    config: SchedulePreplanConfig,
    now: datetime,
    variation_level: str = 'stable',
) -> Optional[SchedulePreplanRecord]:
    """上游 `applySchedulePreplanProposal()`：把模型提案并入既有预排。

    合并语义（**逐条照抄**）：

    * `unchanged`：只刷新窗口，`revision` 不变，`lastEvidenceEntryId` 前进到本轮证据；
    * `replace`（或此前没有任何记录）：整份替换；
    * `patch` / `extend`：按 `id`（regimes）/ `date`（exceptions）逐条覆盖合并；
    * 合并后一条 regime 都不剩时：无既有记录则**持久化这份「显式空的复核」**，
      有既有记录则保留旧预排并写明忽略原因。
    """
    valid_evidence_ids = {entry.get('id') for entry in evidence if isinstance(entry, dict)}
    proposal = _normalize_proposal(proposal_value, valid_evidence_ids, variation_level)
    if proposal is None:
        if not current:
            return None
        return refresh_schedule_preplan(
            current, today, timezone, config, now,
            'Invalid proposal ignored; existing Schedule Preplan retained.')

    if proposal['outcome'] == 'unchanged' and current:
        refreshed = refresh_schedule_preplan(current, today, timezone, config, now, proposal['reason'])
        refreshed['last_evidence_entry_id'] = _max_entry_id(
            _pick(current, 'lastEvidenceEntryId', 'last_evidence_entry_id'), evidence)
        return refreshed

    regimes: list[SchedulePreplanRegime] = list(_pick(current, 'regimes') or []) if current else []
    exceptions: list[SchedulePreplanException] = list(_pick(current, 'exceptions') or []) if current else []
    if proposal['outcome'] == 'replace' or not current:
        regimes = list(proposal.get('regimes') or [])
        exceptions = list(proposal.get('exceptions') or [])
    else:
        regimes = _merge_by(regimes, proposal.get('regimes') or [], lambda item: _pick(item, 'id'))
        exceptions = _merge_by(exceptions, proposal.get('exceptions') or [], lambda item: _pick(item, 'date'))

    horizon_days = int(_num_or_nan(_pick(config, 'horizonDays', 'horizon_days')))
    if not regimes:
        # 上游注释：故事还没建立任何重复节奏时，空结果是合法的初次结果。
        # 把这次复核持久化，系统才会等新证据，而不是反复为同一个空结论付费。
        if not current:
            return {
                'story_id': '',
                'revision': 1,
                'timezone': timezone,
                'valid_from': today,
                'valid_through': _add_date(today, horizon_days - 1),
                'last_reviewed_local_date': today,
                'last_evidence_entry_id': _max_entry_id(None, evidence),
                'review_reason': proposal['reason'],
                'regimes': [],
                'exceptions': [],
                'materialized_days': [],
                'created_at': now,
                'updated_at': now,
            }
        return refresh_schedule_preplan(
            current, today, timezone, config, now,
            'Empty proposal ignored; existing Schedule Preplan retained.')

    # 上游 `current?.x ?? <默认>` / `Math.max(current.x ?? 0, ...)` 的四个读取点：
    # `??` 是「nullish」而非「falsy」，故空串 storyId / 0 revision 原样保留。
    current_story_id = _pick(current, 'storyId', 'story_id', default=None) if current else None
    current_revision = _js_number(_pick(current, 'revision')) if current else None
    current_last_evidence_entry_id = (
        _pick(current, 'lastEvidenceEntryId', 'last_evidence_entry_id') if current else None)
    current_created_at = _pick(current, 'createdAt', 'created_at', default=None) if current else None

    return {
        'story_id': current_story_id or '',
        'revision': int(current_revision if current_revision is not None else 0) + 1,
        'timezone': timezone,
        'valid_from': today,
        'valid_through': _add_date(today, horizon_days - 1),
        'last_reviewed_local_date': today,
        'last_evidence_entry_id': _max_entry_id(current_last_evidence_entry_id, evidence),
        'review_reason': proposal['reason'],
        # 上游 `regimes.slice(-6)` / `exceptions.filter(...).slice(-30)`：只留最近若干条。
        'regimes': regimes[-6:],
        'exceptions': [item for item in exceptions if _js_ge(_pick(item, 'date'), _add_date(today, -1))][-30:],
        # 注意：物化用的是**未截断**的 regimes/exceptions（上游即如此）。
        'materialized_days': materialize_schedule_preplan(regimes, exceptions, today, horizon_days),
        'created_at': current_created_at if current_created_at is not None else now,
        'updated_at': now,
    }


# ============================================================ 物化


def materialize_schedule_preplan(
    regimes: list[SchedulePreplanRegime],
    exceptions: list[SchedulePreplanException],
    start_date: str,
    horizon_days: int,
) -> list[SchedulePreplanDay]:
    """上游 `materializeSchedulePreplan()`：周规律 + 例外日 → 逐日时间块。

    同日多条 regime 命中时取 `from` 最大的一条（`sort(...).reverse` 后取首个，
    相等时保持原顺序 = JS 稳定排序）；每天最多 12 块。
    """
    days: list[SchedulePreplanDay] = []
    horizon = _js_number(horizon_days)
    # 上游 `Math.max(1, horizonDays)` 对 NaN 得 NaN，循环体一次都不执行。
    limit = 0 if horizon is None else int(max(1, math.floor(horizon)))
    for offset in range(limit):
        date = _add_date(start_date, offset)
        matching = [regime for regime in regimes if _regime_covers(regime, date)]
        # 上游 `sort((l, r) => r.from.localeCompare(l.from))[0]`：from 降序取首个。
        matching.sort(key=lambda regime: _js_string(_pick(regime, 'from')), reverse=True)
        chosen = matching[0] if matching else None
        weekday = _WEEKDAYS[_utc_weekday(date)]
        blocks: list[SchedulePreplanBlock] = []
        if chosen is not None:
            weekly = _pick(chosen, 'weekly')
            day_blocks = weekly.get(weekday) if isinstance(weekly, dict) else None
            if isinstance(day_blocks, list):
                blocks = [dict(block) for block in day_blocks if isinstance(block, dict)]
        exception = next((item for item in exceptions if _pick(item, 'date') == date), None)
        if exception is not None and _pick(exception, 'mode') == 'replace':
            raw = _pick(exception, 'blocks')
            blocks = [dict(block) for block in raw if isinstance(block, dict)] if isinstance(raw, list) else []
        elif exception is not None:
            removed = set(_pick(exception, 'removeBlockIds', 'remove_block_ids') or [])
            raw = _pick(exception, 'blocks')
            extra = [dict(block) for block in raw if isinstance(block, dict)] if isinstance(raw, list) else []
            blocks = [block for block in blocks if block.get('id') not in removed] + extra
        days.append({'date': date, 'blocks': _resolve_overlaps(blocks)[:12]})
    return days


# ============================================================ 投影给主叙事


def schedule_preplan_window(
    record: Optional[SchedulePreplanRecord],
    now: datetime,
    timezone: str,
    hours: float = 12,
    config: Optional[SchedulePreplanConfig] = None,
) -> Optional[SchedulePreplanWindow]:
    """上游 `schedulePreplanWindow()`：**只投影未来约十二小时**。

    存下来的多日视界绝不整份进入主 prompt；标记 `tentative` 的候选还要先过两层闸门：
    1. 稳定哈希 `isTentativeBlockActive(storyId, date, blockId, p)` 决定「今天它是否成立」；
    2. 距开始还早于 `candidateRevealMinutes` 时只暴露模糊措辞「可能的个人安排」，
       临近才揭示原文——**候选永不锚定时间**（见 `next_schedule_preplan_transition`
       只认 `kind === 'fixed'`）。
    """
    if config is None:
        config = DEFAULT_SCHEDULE_PREPLAN_CONFIG
    if record is None or _pick(record, 'timezone') != timezone:
        return None
    local = story_local_time_context(now, timezone)
    today = local['date']
    start_minute = local['hour'] * 60 + int(local['time'][3:5])
    end_minute = start_minute + max(1, int(_num_or_nan(hours))) * 60
    # 上游 `minutesUntil > config.candidateRevealMinutes`：NaN 时恒为 false。
    reveal_minutes = _js_number(_pick(config, 'candidateRevealMinutes', 'candidate_reveal_minutes'))
    probability = _js_number(_pick(config, 'candidateActivationProbability', 'candidate_activation_probability'))

    blocks: list[SchedulePreplanWindowBlock] = []
    for day in _pick(record, 'materializedDays', 'materialized_days') or []:
        day_offset = _date_difference(today, _pick(day, 'date'))
        # 只关心今天与明天：窗口最长也就十二小时起步。
        if day_offset < 0 or day_offset > 1:
            continue
        for block in _pick(day, 'blocks') or []:
            start = day_offset * 1_440 + _time_minutes(_pick(block, 'start'))
            end = day_offset * 1_440 + _time_minutes(_pick(block, 'end'))
            if end <= start:
                end += 1_440
            if end <= start_minute or start >= end_minute:
                continue
            if _pick(block, 'tentative'):
                if not _is_tentative_block_active(
                    _pick(record, 'storyId', 'story_id'), _pick(day, 'date'), _pick(block, 'id'), probability,
                ):
                    continue
                minutes_until = start - start_minute
                if reveal_minutes is not None and minutes_until > reveal_minutes:
                    # 上游写的是 `location: undefined`；`undefined` 在 JSON 里等同「无此键」，
                    # 故这里删键而不是写 `None`，保证序列化形状与上游一致。
                    vague = dict(block)
                    vague['label'] = '可能的个人安排'
                    vague.pop('location', None)
                    vague['date'] = _pick(day, 'date')
                    vague['tentative'] = True
                    blocks.append(vague)
                    continue
            blocks.append({**block, 'date': _pick(day, 'date')})

    to_total = end_minute
    to_date = _add_date(today, math.floor(to_total / 1_440))
    to_clock = _clock(to_total % 1_440)
    return {
        'name': 'Schedule Preplan',
        'timezone': timezone,
        'from': f'{today} {_clock(start_minute)}',
        'to': f'{to_date} {to_clock}',
        'planned_not_observed': True,
        'revision': _clamp_int(_pick(record, 'revision'), 0, 1_000_000, 0),
        'blocks': blocks[:8],
    }


def next_schedule_preplan_transition(
    record: Optional[SchedulePreplanRecord],
    now: datetime,
    timezone: str,
    max_hours: float = 12,
) -> Optional[datetime]:
    """上游 `nextSchedulePreplanTransition()`：下一个可以锚定自动推进的时刻。

    只看 `kind === 'fixed'` 的块（`tentative` 候选永不锚定时间）；候选时刻是「块开始」
    与「块结束」里最靠前的那个，返回 `now + 差值`。
    """
    window = schedule_preplan_window(record, now, timezone, max_hours)
    if window is None:
        return None
    local = story_local_time_context(now, timezone)
    current = local['hour'] * 60 + int(local['time'][3:5])
    candidates: list[float] = []
    for block in window['blocks']:
        if _pick(block, 'kind') != 'fixed':
            continue
        offset = _date_difference(local['date'], _pick(block, 'date')) * 1_440
        start = offset + _time_minutes(_pick(block, 'start'))
        end = offset + _time_minutes(_pick(block, 'end'))
        if end <= start:
            end += 1_440
        if start > current:
            candidates.append(start)
        if end > current:
            candidates.append(end)
    if not candidates:
        return None
    return now + timedelta(minutes=min(candidates) - current)


# ============================================================ 提案归一化


def _normalize_proposal(
    value: Any,
    valid_evidence_ids: set[Any],
    variation_level: str,
) -> Optional[SchedulePreplanProposal]:
    """上游 `normalizeProposal()`：提案的准入闸门。

    关键一条：`patch` / `replace` 且**本轮确实有证据**时，提案自身（或其 regime/exception）
    必须引用到有效证据 id，否则整份提案作废——没有证据的日程改写一律拒绝。
    """
    if not _is_record(value) or _js_string(_pick(value, 'outcome')) not in _OUTCOMES:
        return None
    outcome = _pick(value, 'outcome')
    reason = _text(_pick(value, 'reason'), 500)
    if not reason:
        return None
    source_entry_ids = [item for item in _ids(_pick(value, 'sourceEntryIds', 'source_entry_ids')) if item in valid_evidence_ids]
    allow_tentative = variation_level == 'granular'
    regimes = _normalize_regimes(_pick(value, 'regimes'), valid_evidence_ids, allow_tentative)
    exceptions = _normalize_exceptions(_pick(value, 'exceptions'), valid_evidence_ids, allow_tentative)
    if (
        _js_string(outcome) in ('patch', 'replace')
        and len(valid_evidence_ids)
        and not source_entry_ids
        and not any(_pick(item, 'sourceEntryIds', 'source_entry_ids') for item in regimes)
        and not any(_pick(item, 'sourceEntryIds', 'source_entry_ids') for item in exceptions)
    ):
        return None
    proposal: SchedulePreplanProposal = {
        'outcome': outcome,
        'reason': reason,
        'source_entry_ids': source_entry_ids,
        'regimes': regimes,
        'exceptions': exceptions,
    }
    confidence = _finite(_pick(value, 'confidence'))
    # 上游此处会留下 `confidence: undefined` 这个键；JSON 序列化时等同无此键。
    if confidence is not None:
        proposal['confidence'] = confidence
    return proposal


def _normalize_regimes(
    value: Any,
    valid_evidence_ids: Optional[set[Any]] = None,
    allow_tentative: bool = True,
) -> list[SchedulePreplanRegime]:
    """上游 `normalizeRegimes()`：最多 6 条，逐条归一。"""
    if not isinstance(value, list):
        return []
    regimes = [normalized for normalized in (
        _normalize_regime(item, valid_evidence_ids, allow_tentative) for item in value) if normalized]
    return regimes[:6]


def _normalize_regime(
    value: Any,
    valid_evidence_ids: Optional[set[Any]] = None,
    allow_tentative: bool = True,
) -> Optional[SchedulePreplanRegime]:
    """上游 `normalizeRegime()`：周规律主体（`weekly` 必须是对象，`to < from` 作废）。"""
    if not _is_record(value) or not _is_record(_pick(value, 'weekly')):
        return None
    regime_id = _slug(_pick(value, 'id'), 80)
    label = _text(_pick(value, 'label'), 120)
    start = _date_key(_pick(value, 'from'))
    end = _date_key(_pick(value, 'to'))
    if not regime_id or not label or not start or (end and _js_lt(end, start)):
        return None
    weekly: dict[str, list[SchedulePreplanBlock]] = {}
    raw_weekly = _pick(value, 'weekly')
    for weekday in _WEEKDAYS:
        blocks = _normalize_blocks(raw_weekly.get(weekday), valid_evidence_ids, allow_tentative)
        if blocks:
            weekly[weekday] = blocks
    regime: SchedulePreplanRegime = {
        'id': regime_id,
        'label': label,
        'from': start,
        'weekly': weekly,
        'source_entry_ids': _evidence_ids(_pick(value, 'sourceEntryIds', 'source_entry_ids'), valid_evidence_ids),
    }
    if end:
        regime['to'] = end
    return regime


def _normalize_exceptions(
    value: Any,
    valid_evidence_ids: Optional[set[Any]] = None,
    allow_tentative: bool = True,
) -> list[SchedulePreplanException]:
    """上游 `normalizeExceptions()`：最多 30 条。"""
    if not isinstance(value, list):
        return []
    exceptions = [normalized for normalized in (
        _normalize_exception(item, valid_evidence_ids, allow_tentative) for item in value) if normalized]
    return exceptions[:30]


def _normalize_exception(
    value: Any,
    valid_evidence_ids: Optional[set[Any]] = None,
    allow_tentative: bool = True,
) -> Optional[SchedulePreplanException]:
    """上游 `normalizeException()`：`date` / `mode` / `reason` 缺一不可。"""
    if not _is_record(value):
        return None
    date = _date_key(_pick(value, 'date'))
    raw_mode = _pick(value, 'mode')
    mode = 'replace' if raw_mode == 'replace' else 'patch' if raw_mode == 'patch' else None
    reason = _text(_pick(value, 'reason'), 300)
    if not date or not mode or not reason:
        return None
    raw_remove = _pick(value, 'removeBlockIds', 'remove_block_ids')
    remove_block_ids = [slug for slug in (
        _slug(item, 80) for item in raw_remove) if slug][:20] if isinstance(raw_remove, list) else []
    return {
        'date': date,
        'mode': mode,
        'reason': reason,
        'remove_block_ids': remove_block_ids,
        'blocks': _normalize_blocks(_pick(value, 'blocks'), valid_evidence_ids, allow_tentative),
        'source_entry_ids': _evidence_ids(_pick(value, 'sourceEntryIds', 'source_entry_ids'), valid_evidence_ids),
    }


def _normalize_days(value: Any) -> list[SchedulePreplanDay]:
    """上游 `normalizeDays()`：物化日（最多 31 天），日期键非法则整天丢弃。"""
    if not isinstance(value, list):
        return []
    days: list[SchedulePreplanDay] = []
    for item in value:
        if not _is_record(item):
            continue
        date = _date_key(_pick(item, 'date'))
        if not date:
            continue
        days.append({'date': date, 'blocks': _normalize_blocks(_pick(item, 'blocks'))})
    return days[:31]


def _normalize_blocks(
    value: Any,
    valid_evidence_ids: Optional[set[Any]] = None,
    allow_tentative: bool = True,
) -> list[SchedulePreplanBlock]:
    """上游 `normalizeBlocks()`：最多 20 块。"""
    if not isinstance(value, list):
        return []
    blocks = [normalized for normalized in (
        _normalize_block(item, valid_evidence_ids, allow_tentative) for item in value) if normalized]
    return blocks[:20]


def _normalize_block(
    value: Any,
    valid_evidence_ids: Optional[set[Any]] = None,
    allow_tentative: bool = True,
) -> Optional[SchedulePreplanBlock]:
    """上游 `normalizeBlock()`：`tentative` 只有 granular 模式且 `flexible`/`open` 才保留。"""
    if not _is_record(value):
        return None
    block_id = _slug(_pick(value, 'id'), 80)
    start = _time_key(_pick(value, 'start'))
    end = _time_key(_pick(value, 'end'))
    label = _text(_pick(value, 'label'), 160)
    kind = _pick(value, 'kind')
    if not block_id or not start or not end or start == end or not label or kind not in _KINDS:
        return None
    location = _text(_pick(value, 'location'), 120)
    tentative = allow_tentative and _pick(value, 'tentative') is True and kind in ('flexible', 'open')
    block: SchedulePreplanBlock = {'id': block_id, 'start': start, 'end': end, 'label': label, 'kind': kind}
    if location:
        block['location'] = location
    if tentative:
        block['tentative'] = True
    block['source_entry_ids'] = _evidence_ids(_pick(value, 'sourceEntryIds', 'source_entry_ids'), valid_evidence_ids)
    return block


# ============================================================ 私有助手（照抄 JS 语义）


def _regime_covers(regime: Any, date: str) -> bool:
    """上游 `regime.from <= date && (!regime.to || regime.to >= date)`。"""
    start = _pick(regime, 'from')
    if not _js_le(start, date):
        return False
    end = _pick(regime, 'to')
    return True if not end else _js_ge(end, date)


def _is_tentative_block_active(story_id: Any, date: Any, block_id: Any, probability: Optional[float]) -> bool:
    """上游 `isTentativeBlockActive()`：FNV-1a（32 位）+ `Math.imul` 的稳定哈希。

    同一个 `storyId|date|blockId` 每次得到同一个结论，所以「今天这个候选成不成立」
    不会随扫描次数抖动。
    """
    # 上游模板字符串 `${storyId}|${date}|${blockId}`：非字符串按 JS `String()` 语义拼接；
    # `charCodeAt` 取的是 **UTF-16 码元**，故这里显式编码成 UTF-16-LE 逐码元迭代
    # （中文在 BMP 内，与 `ord()` 同值；补充平面字符则与 JS 一致地拆成代理对）。
    source = f'{_js_string(story_id)}|{_js_string(date)}|{_js_string(block_id)}'
    units = memoryview(source.encode('utf-16-le')).cast('H')
    hash_value = 2166136261
    for unit in units:
        hash_value = _imul(hash_value ^ unit, 16777619)
    if probability is None:
        # 上游 `x < undefined/NaN` 恒为 false。
        return False
    return ((hash_value & 0xFFFFFFFF) / 0x1_0000_0000) < probability


def _imul(left: int, right: int) -> int:
    """等价 JS `Math.imul(a, b)`：32 位有符号整数乘法（溢出回绕）。"""
    product = (left * right) & 0xFFFFFFFF
    return product - 0x1_0000_0000 if product >= 0x8000_0000 else product


def _resolve_overlaps(blocks: list[SchedulePreplanBlock]) -> list[SchedulePreplanBlock]:
    """上游 `resolveOverlaps()`：按优先级取舍重叠块，再按开始时间排序。"""
    chosen: list[SchedulePreplanBlock] = []
    ordered = sorted(
        blocks,
        key=lambda block: (-_PRIORITY.get(_pick(block, 'kind'), 0), _time_minutes(_pick(block, 'start'))),
    )
    for candidate in ordered:
        start = _time_minutes(_pick(candidate, 'start'))
        end = _time_minutes(_pick(candidate, 'end'))
        if end <= start:
            end += 1_440
        overlaps = False
        for block in chosen:
            other_start = _time_minutes(_pick(block, 'start'))
            other_end = _time_minutes(_pick(block, 'end'))
            if other_end <= other_start:
                other_end += 1_440
            if start < other_end and end > other_start:
                overlaps = True
                break
        if not overlaps and not any(_pick(block, 'id') == _pick(candidate, 'id') for block in chosen):
            chosen.append(candidate)
    chosen.sort(key=lambda block: _time_minutes(_pick(block, 'start')))
    return chosen


def _evidence_ids(value: Any, valid: Optional[set[Any]]) -> list[int]:
    """上游 `evidenceIds()`：有 `valid` 集合（哪怕为空集）时就求交。"""
    normalized = _ids(value)
    if valid is None:
        return normalized
    return [item for item in normalized if item in valid]


def _ids(value: Any) -> list[int]:
    """上游 `ids()`：正整数、去重保序、最多 30 个。"""
    if not isinstance(value, list):
        return []
    numbers: list[int] = []
    for item in value:
        number = _js_number(item)
        # 上游 `Number.isSafeInteger(id) && id > 0`。
        if number is None or not number.is_integer() or abs(number) > _MAX_SAFE_INTEGER or number <= 0:
            continue
        numbers.append(int(number))
    return list(dict.fromkeys(numbers))[:30]


def _merge_by(
    current: list[Any],
    changes: list[Any],
    key: Any,
) -> list[Any]:
    """上游 `mergeBy()`：`Map` 覆盖合并——同名键保留原位置、值被替换。"""
    merged: dict[Any, Any] = {}
    for item in current:
        merged[key(item)] = item
    for item in changes:
        merged[key(item)] = item
    return list(merged.values())


def _max_entry_id(current_value: Any, evidence: list[ScriptEntry]) -> int:
    """上游 `Math.max(current?.lastEvidenceEntryId ?? 0, ...evidence.map(e => e.id), 0)`。

    上游在字段缺失 / NaN 时会得到 `NaN`；本移植统一收敛到 `0`，
    避免把非 JSON 值写进记录（其余情况逐字等价）。
    """
    candidates = [0.0]
    current_number = _js_number(current_value)
    if current_number is not None:
        candidates.append(current_number)
    for entry in evidence:
        entry_number = _js_number(_pick(entry, 'id'))
        if entry_number is not None:
            candidates.append(entry_number)
    return int(max(candidates))


# ---------------------------------------------------------- 日期 / 时间


_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_TIME_RE = re.compile(r'^(\d{2}):(\d{2})$')


def _days_from_civil(year: int, month: int, day: int) -> int:
    """Howard Hinnant 的 `days_from_civil`：与 JS `Date` 相同的公历算法。

    返回相对 1970-01-01 的天数；可处理 year 0 与负年份（JS `Date` 亦然）。
    原算法用 C++ 的「向零截断」除法，故负年份处有 `y - 399` 的补偿；Python 的 `//`
    本身就是向下取整，直接 `// 400` 即等价（多写补偿会少一年）。
    """
    adjusted_year = year - (1 if month <= 2 else 0)
    era = adjusted_year // 400
    year_of_era = adjusted_year - era * 400
    month_prime = (month + 9) % 12
    day_of_year = (153 * month_prime + 2) // 5 + day - 1
    day_of_era = year_of_era * 365 + year_of_era // 4 - year_of_era // 100 + day_of_year
    return era * 146097 + day_of_era - 719468


def _civil_from_days(days: int) -> tuple[int, int, int]:
    """`days_from_civil` 的逆运算（同样把 C++ 的截断除法改写成 Python 的 `//`）。"""
    shifted = days + 719468
    era = shifted // 146097
    day_of_era = shifted - era * 146097
    year_of_era = (day_of_era - day_of_era // 1460 + day_of_era // 36524 - day_of_era // 146096) // 365
    year = year_of_era + era * 400
    day_of_year = day_of_era - (365 * year_of_era + year_of_era // 4 - year_of_era // 100)
    month_prime = (5 * day_of_year + 2) // 153
    day = day_of_year - (153 * month_prime + 2) // 5 + 1
    month = month_prime + (3 if month_prime < 10 else -9)
    return (year + (1 if month <= 2 else 0), month, day)


def _parse_date_key(value: Any) -> Optional[tuple[int, int, int]]:
    """上游 `dateKey` 的校验内核：shape → 往返一致（等价 `toISOString().slice(0, 10) === raw`）。"""
    raw = value.strip() if isinstance(value, str) else ''
    if not _DATE_RE.match(raw):
        return None
    parts = (int(raw[0:4]), int(raw[5:7]), int(raw[8:10]))
    # 往返校验能挡下 `2026-02-30`（会滚到 3 月 2 日）这类「形状合法但日期不存在」的输入。
    return parts if _civil_from_days(_days_from_civil(*parts)) == parts else None


def _date_key(value: Any) -> Optional[str]:
    """上游 `dateKey()`：合法则原样返回字符串（**继续存 `YYYY-MM-DD` 字符串**）。"""
    raw = value.strip() if isinstance(value, str) else ''
    return raw if _parse_date_key(raw) is not None else None


def _add_date(value: str, days: int) -> str:
    """上游 `addDate()`：UTC 日历日加减。

    上游对非法输入会得到 Invalid Date 并在 `toISOString()` 抛 `RangeError`；
    这里对应地抛 `ValueError`（调用方传的都是 `dateKey` 校验过的键）。
    四位数年份（0000–9999）与 JS 输出同形；负年份 JS 用扩展形式，本移植不涉及。
    """
    parts = _parse_date_key(value)
    if parts is None:
        raise ValueError(f'addDate: invalid date key {value!r}')
    year, month, day = _civil_from_days(_days_from_civil(*parts) + int(days))
    return f'{year:04d}-{month:02d}-{day:02d}'


def _date_difference(left: str, right: str) -> int:
    """上游 `dateDifference()`：两个日期键相差几天（左减右取负）。"""
    left_parts = _parse_date_key(left)
    right_parts = _parse_date_key(right)
    if left_parts is None or right_parts is None:
        raise ValueError(f'dateDifference: invalid date key {left!r} / {right!r}')
    return _days_from_civil(*right_parts) - _days_from_civil(*left_parts)


def _utc_weekday(date: str) -> int:
    """等价 `new Date(`${date}T00:00:00.000Z`).getUTCDay()`（0 = sunday）。"""
    parts = _parse_date_key(date)
    if parts is None:
        raise ValueError(f'getUTCDay: invalid date key {date!r}')
    # 1970-01-01 是星期四（4）。
    return (_days_from_civil(*parts) + 4) % 7


def _time_key(value: Any) -> Optional[str]:
    """上游 `timeKey()`：`HH:MM` 且时/分在范围内，否则作废。"""
    raw = value.strip() if isinstance(value, str) else ''
    match = _TIME_RE.match(raw)
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        return None
    return raw


def _time_minutes(value: Any) -> float:
    """上游 `timeMinutes()`：`HH:MM` → 当日分钟数；非法时按 JS 语义给 `NaN`。"""
    parts = value.split(':') if isinstance(value, str) else []
    if len(parts) != 2:
        return float('nan')
    try:
        return int(parts[0]) * 60 + int(parts[1])
    except ValueError:
        return float('nan')


def _clock(minutes: float) -> str:
    """上游 `clock()`：分钟数 → `HH:MM`（两位补零；负数时 JS 会保留 `-`）。"""
    hour = math.floor(minutes / 60)
    return f'{hour:02d}:{int(minutes % 60):02d}'


# ---------------------------------------------------------- 字符串 / 数字


def _slug(value: Any, limit: int) -> str:
    """上游 `slug()`：保留 Unicode 字母/数字/`_`/`-`，其余折叠成单个 `-`，再截断。

    `\\p{L}\\p{N}` 与 Python `\\w`（Unicode 模式下 = 字母/数字/下划线）基本同集，
    故直接用 `[^\\w-]`。
    """
    if not isinstance(value, str):
        return ''
    text = re.sub(r'[^\w-]', '-', value.strip())
    text = re.sub(r'-+', '-', text)
    text = re.sub(r'^-|-$', '', text)
    return text[:limit]


def _text(value: Any, limit: int) -> str:
    """上游 `text()`：trim、把换行折成空格、截断到 `limit`。"""
    if not isinstance(value, str):
        return ''
    return re.sub(r'[\r\n]+', ' ', value.strip())[:limit]


def _finite(value: Any) -> Optional[float]:
    """上游 `finite()`：只接受有穷数字（JS `typeof` 下 `bool` 不是 number），再夹到 [0, 1]。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return max(0.0, min(1.0, number))


def _valid_date(value: Any) -> Optional[datetime]:
    """上游 `validDate()`：`Date` / 字符串 / 数字 → 合法 `datetime`，否则 `None`。"""
    if isinstance(value, bool):
        return None
    return parse_dt(value)


def _clamp_int(value: Any, minimum: int, maximum: int, fallback: int) -> int:
    """上游 `clampInt()`：`Math.max(min, Math.min(max, Math.floor(Number(value))))`。"""
    number = _js_number(value)
    if number is None:
        return fallback
    return int(max(minimum, min(maximum, math.floor(number))))


def _clamp_number(value: Any, minimum: float, maximum: float, fallback: float) -> float:
    """上游 `clampNumber()`。"""
    number = _js_number(value)
    if number is None:
        return fallback
    return max(minimum, min(maximum, number))


def _is_record(value: Any) -> bool:
    """上游 `isRecord()`：非空、对象、非数组。"""
    return isinstance(value, dict)


class _Undefined:
    """JS `undefined` 的占位（区别于 `None` ≡ JSON `null`）。

    只在「键不存在」时使用：`clampInt(undefined)` 回落 fallback，而
    `clampInt(null)` 是 `Number(null) === 0` 再夹取。为便于 `or []` 这类写法，
    哨兵本身是假值。
    """

    __slots__ = ()

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return 'undefined'


#: 上游 `undefined` 的哨兵（见 `_Undefined`）。
_UNDEFINED = _Undefined()


def _pick(source: Any, *names: str, default: Any = _UNDEFINED) -> Any:
    """按顺序取第一个存在的键：优先上游 camelCase，其次本移植版 snake_case。

    键存在但值为 `None`（JSON `null`）时**原样返回 `None`**，与「键不存在」（`_UNDEFINED`）
    区分开——这两者在 JS 的数值/字符串强转下行为不同。
    """
    if isinstance(source, dict):
        for name in names:
            if name in source:
                return source[name]
    return default


_JS_DECIMAL_RE = re.compile(r'^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$')
_JS_HEX_RE = re.compile(r'^0[xX][0-9a-fA-F]+$')
_JS_BINARY_RE = re.compile(r'^0[bB][01]+$')
_JS_OCTAL_RE = re.compile(r'^0[oO][0-7]+$')


def _js_number_string(text: str) -> Optional[float]:
    """等价 `Number(字符串)`；得不到有穷数时返回 `None`（= NaN / ±Infinity）。"""
    stripped = text.strip()
    if not stripped:
        # `Number('') === 0`
        return 0.0
    if stripped in ('Infinity', '+Infinity', '-Infinity'):
        return None
    if _JS_HEX_RE.match(stripped):
        return float(int(stripped[2:], 16))
    if _JS_BINARY_RE.match(stripped):
        return float(int(stripped[2:], 2))
    if _JS_OCTAL_RE.match(stripped):
        return float(int(stripped[2:], 8))
    if not _JS_DECIMAL_RE.match(stripped):
        return None
    number = float(stripped)
    return number if math.isfinite(number) else None


def _js_number(value: Any) -> Optional[float]:
    """等价 JS `Number(value)`，但把「非有穷」统一折叠成 `None`。

    折叠是安全的：上游每个调用点后面都跟着 `Number.isFinite(...)`，或处在
    「NaN 参与比较恒为 false」的位置。
    """
    if value is _UNDEFINED:
        return None
    if value is None:
        # `Number(null) === 0`
        return 0.0
    if value is True:
        return 1.0
    if value is False:
        return 0.0
    if isinstance(value, int):
        return float(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return _js_number_string(value)
    if isinstance(value, (list, tuple)):
        # JS `Number(arr)` 就是 `Number(arr.toString())`：`[]` → 0、`[5]` → 5、
        # `[null]` → 0、`[{}]` → NaN；`_js_string` 已按 `Array#toString()` 拼装。
        return _js_number_string(_js_string(value))
    return None


def _num_or_nan(value: Any) -> float:
    """等价 JS 里一个可能为 NaN 的数值（用在比较/算术中，让两侧自然得 false）。"""
    number = _js_number(value)
    return float('nan') if number is None else number


def _js_string(value: Any) -> str:
    """等价 JS `String(value)`（用于上游的模板串与 `includes(String(...))`）。"""
    if value is _UNDEFINED:
        return 'undefined'
    if value is None:
        return 'null'
    if value is True:
        return 'true'
    if value is False:
        return 'false'
    if isinstance(value, str):
        return value
    if isinstance(value, float):
        if math.isfinite(value) and value.is_integer() and abs(value) < 1e21:
            return str(int(value))
        return repr(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (list, tuple)):
        return ','.join('' if item is None or item is _UNDEFINED else _js_string(item) for item in value)
    return '[object Object]'


def _js_lt(left: Any, right: Any) -> bool:
    """等价 JS `left < right`（两侧同为字符串按码元序，否则按数值序；任一非数 ⇒ false）。"""
    if isinstance(left, str) and isinstance(right, str):
        return left < right
    left_number, right_number = _js_number(left), _js_number(right)
    return left_number is not None and right_number is not None and left_number < right_number


def _js_le(left: Any, right: Any) -> bool:
    """等价 JS `left <= right`。"""
    if isinstance(left, str) and isinstance(right, str):
        return left <= right
    left_number, right_number = _js_number(left), _js_number(right)
    return left_number is not None and right_number is not None and left_number <= right_number


def _js_ge(left: Any, right: Any) -> bool:
    """等价 JS `left >= right`。"""
    if isinstance(left, str) and isinstance(right, str):
        return left >= right
    left_number, right_number = _js_number(left), _js_number(right)
    return left_number is not None and right_number is not None and left_number >= right_number
