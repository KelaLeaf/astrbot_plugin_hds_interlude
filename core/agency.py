"""Agency Window（可行动窗口）—— 上游 `src/agency.ts` 的逐句移植。

上游用「窗口状态 + 主动联系候选 + 容量判定」三件套，把「主角此刻能不能主动联系某人」
从情绪里剥离出来，只由**可执行的现实条件**决定：日程负载、隐私、设备可用性，
以及普通来源必须遵守的最小主动联系间隔。

语义要点（与上游逐句对应）：

- 窗口草稿归一化：三个枚举字段必须命中白名单；`validUntil` 严格晚于 `now`，
  且**钳制**到 `now + max(5, maxWindowMinutes) 分钟`（`maxWindowMinutes` 再小也至少 5 分钟）；
  `nextOpportunityAt` 必须晚于 `now` 且不晚于 `validUntil`；
  `basis` 非空且 `sourceEntryIds` 非空，否则整份草稿作废。
- 证据求交：`sourceEntryIds` 只保留正整数 ∩ 允许集合，无交集时用 `fallbackSourceEntryId`
  （必须 > 0）补位，最后去重并**只取尾部 20 条**。
- 主动联系候选：目标必须在允许名单内；`motive` 上限 600 字；`expiresAt` 钳制到
  `now + max(1, maxCandidateHours) 小时`；`notBefore` 必须严格落在 `(now, expiresAt)` 之间；
  `willingness` 必须是有穷数字并 clamp 到 [0, 1]。
- 容量矩阵（顺序即优先级，逐条照抄上游）：
  1. 窗口缺失或已过期 → `agency-window-missing-or-expired`；
  2. `deviceAccess === 'unavailable'` → `device-unavailable`；
  3. `deviceAccess === 'limited'` → `device-limited`；
  4. `activityLoad === 'overloaded'` → `schedule-overloaded`；
  5. `disclosure === 'personal'` 且 `privacy !== 'private'` → `privacy-insufficient`；
  6. 非 `promise` 来源且距最近一次角色发消息不足最小间隔 → `minimum-proactive-interval`
     （`nextOpportunityAt` 取「上次发言 + 最小间隔」）；
  7. `activityLoad === 'occupied'` 且来源既不是 `promise` 也不是 `practical-update`
     → `schedule-occupied`；
  8. 全部通过 → `capacity-available`。
- 指纹只由「目标 + 来源 + 证据 id（升序）」构成，措辞变化不改变身份。
- 复核时间取 `notBefore` / 容量 `nextOpportunityAt` / 窗口 `nextOpportunityAt` 中
  **严格晚于 now 的最早者**，无则回落 `now + 30 分钟`，最后与 `expiresAt` 取小。

时间映射（见 docs/PORT_PLAN.md §2）：上游 `Date` → timezone-aware UTC `datetime`；
上游 `Date#toISOString()` 的输出形状由 `core/time.py` 的 `iso()` 保证（毫秒三位 + `Z`），
因此返回对象里的时间字段与上游一样是**字符串**。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Mapping, Optional, TypedDict

from .time import dt_ms, iso, parse_dt, utc_now
from .types import (
    AgencyActivityLoad,
    AgencyConfig,
    AgencyDeviceAccess,
    AgencyPrivacy,
    AgencyWindowState,
    ProactiveContactDraft,
    ProactiveContactOrigin,
)

MINUTE = 60_000
HOUR = 60 * MINUTE

# 上游 `MINUTE` / `HOUR` 的同名毫秒常量，供内部时间轴运算使用。
_MINUTE_MS = MINUTE
_HOUR_MS = HOUR

_ACTIVITY_LOADS: tuple[AgencyActivityLoad, ...] = ('free', 'occupied', 'overloaded')
_PRIVACY_LEVELS: tuple[AgencyPrivacy, ...] = ('private', 'shared', 'public')
_DEVICE_ACCESS_LEVELS: tuple[AgencyDeviceAccess, ...] = ('available', 'limited', 'unavailable')
_ORIGINS: tuple[ProactiveContactOrigin, ...] = (
    'life-event', 'promise', 'practical-update', 'relationship-follow-up',
)
_DISCLOSURES = ('ordinary', 'personal')
_OUTCOMES = ('send-now', 'recheck-later', 'let-go')

DEFAULT_AGENCY_CONFIG: AgencyConfig = {
    'enabled': True,
    'max_window_minutes': 240,
    'minimum_proactive_interval_minutes': 60,
    'max_candidate_hours': 24,
}


class AgencyCapacityResult(TypedDict, total=False):
    """容量判定结果（上游 `AgencyCapacityResult`）。

    `next_opportunity_at` 是可选字段：上游 `nextOpportunityAt?: Date` 在
    `JSON.stringify` 下会整体省略，故这里仅在真正存在时写入该键。
    """

    allowed: bool
    reason: str
    next_opportunity_at: datetime


def resolve_agency_config(value: Optional[Mapping[str, Any]] = None) -> AgencyConfig:
    """上游 `resolveAgencyConfig`：`{ ...DEFAULT_AGENCY_CONFIG, ...value }`。"""
    resolved = {**DEFAULT_AGENCY_CONFIG, **(value or {})}
    return resolved  # type: ignore[return-value]


def normalize_agency_window_state(value: Any) -> Optional[AgencyWindowState]:
    """上游 `normalizeAgencyWindowState`：只接受形状完整、时间合法的持久窗口状态。"""
    if not _is_record(value):
        return None
    if str(_pick(value, 'activityLoad', 'activity_load')) not in _ACTIVITY_LOADS:
        return None
    if str(_pick(value, 'privacy')) not in _PRIVACY_LEVELS:
        return None
    if str(_pick(value, 'deviceAccess', 'device_access')) not in _DEVICE_ACCESS_LEVELS:
        return None
    valid_until = parse_dt(_pick(value, 'validUntil', 'valid_until'))
    updated_at = parse_dt(_pick(value, 'updatedAt', 'updated_at'))
    if valid_until is None or updated_at is None:
        return None
    next_opportunity_at = parse_dt(_pick(value, 'nextOpportunityAt', 'next_opportunity_at'))
    return {
        'activity_load': _pick(value, 'activityLoad', 'activity_load'),
        'privacy': _pick(value, 'privacy'),
        'device_access': _pick(value, 'deviceAccess', 'device_access'),
        'next_opportunity_at': iso(next_opportunity_at),
        'valid_until': iso(valid_until),
        'basis': _text(_pick(value, 'basis'), 500),
        'source_entry_ids': _positive_ids(_pick(value, 'sourceEntryIds', 'source_entry_ids'))[-20:],
        'updated_at': iso(updated_at),
    }


def normalize_agency_window_draft(
    value: Any,
    now: datetime,
    config: AgencyConfig,
    valid_source_entry_ids: frozenset[int] | set[int],
    fallback_source_entry_id: Optional[int] = None,
) -> Optional[AgencyWindowState]:
    """上游 `normalizeAgencyWindowDraft`：校验并钳制模型提出的窗口草稿。"""
    if not _is_record(value):
        return None
    if str(value.get('activityLoad')) not in _ACTIVITY_LOADS:
        return None
    if str(value.get('privacy')) not in _PRIVACY_LEVELS:
        return None
    if str(value.get('deviceAccess')) not in _DEVICE_ACCESS_LEVELS:
        return None
    maximum = now + timedelta(minutes=max(5, config.get('max_window_minutes', 0)))
    requested_until = parse_dt(value.get('validUntil'))
    valid_until = (
        _min_datetime(requested_until, maximum)
        if requested_until is not None and requested_until > now
        else maximum
    )
    requested_opportunity = parse_dt(value.get('nextOpportunityAt'))
    next_opportunity_at = (
        _min_datetime(requested_opportunity, valid_until)
        if requested_opportunity is not None and requested_opportunity > now
        else None
    )
    source_entry_ids = _grounded_ids(
        value.get('sourceEntryIds'), valid_source_entry_ids, fallback_source_entry_id
    )
    basis = _text(value.get('basis'), 500)
    if not basis or not source_entry_ids:
        return None
    return {
        'activity_load': value.get('activityLoad'),
        'privacy': value.get('privacy'),
        'device_access': value.get('deviceAccess'),
        'next_opportunity_at': iso(next_opportunity_at),
        'valid_until': iso(valid_until),
        'basis': basis,
        'source_entry_ids': source_entry_ids,
        'updated_at': iso(now),
    }


def active_agency_window(value: Any, now: Optional[datetime] = None) -> Optional[AgencyWindowState]:
    """上游 `activeAgencyWindow`：归一化后仍处于有效期内的窗口，否则 `None`。"""
    at = now if now is not None else utc_now()
    state = normalize_agency_window_state(value)
    if state is None:
        return None
    valid_until = parse_dt(state.get('valid_until'))
    return state if valid_until is not None and valid_until > at else None


def normalize_proactive_contact(
    value: Any,
    now: datetime,
    config: AgencyConfig,
    permitted_participant_ids: frozenset[str] | set[str],
    valid_source_entry_ids: frozenset[int] | set[int],
    fallback_source_entry_id: Optional[int] = None,
) -> Optional[ProactiveContactDraft]:
    """上游 `normalizeProactiveContact`：校验目标、枚举、动机、时限与证据。"""
    if not _is_record(value) or str(_pick(value, 'participantId', 'participant_id')) not in permitted_participant_ids:
        return None
    if str(_pick(value, 'origin')) not in _ORIGINS:
        return None
    if str(_pick(value, 'disclosure')) not in _DISCLOSURES:
        return None
    if str(_pick(value, 'outcome')) not in _OUTCOMES:
        return None
    motive = _text(_pick(value, 'motive'), 600)
    source_entry_ids = _grounded_ids(
        _pick(value, 'sourceEntryIds', 'source_entry_ids'), valid_source_entry_ids, fallback_source_entry_id
    )
    if not motive or not source_entry_ids:
        return None
    maximum_expiry = now + timedelta(hours=max(1, config.get('max_candidate_hours', 0)))
    requested_expiry = parse_dt(_pick(value, 'expiresAt', 'expires_at'))
    expires_at = (
        _min_datetime(requested_expiry, maximum_expiry)
        if requested_expiry is not None and requested_expiry > now
        else maximum_expiry
    )
    requested_not_before = parse_dt(_pick(value, 'notBefore', 'not_before'))
    not_before = (
        iso(requested_not_before)
        if requested_not_before is not None and now < requested_not_before < expires_at
        else None
    )
    willingness = _finite(_pick(value, 'willingness'))
    return {
        'participant_id': str(_pick(value, 'participantId', 'participant_id')),
        'origin': _pick(value, 'origin'),
        'motive': motive,
        'disclosure': _pick(value, 'disclosure'),
        'source_entry_ids': source_entry_ids,
        'willingness': None if willingness is None else _clamp(willingness, 0, 1),
        'outcome': _pick(value, 'outcome'),
        'not_before': not_before,
        'expires_at': iso(expires_at),
    }


def evaluate_agency_capacity(
    window: Optional[AgencyWindowState],
    candidate: ProactiveContactDraft,
    now: datetime,
    config: AgencyConfig,
    last_character_message_at: Optional[str] = None,
) -> AgencyCapacityResult:
    """上游 `evaluateAgencyCapacity`：容量矩阵（判定顺序即优先级）。"""
    valid_until = parse_dt(_pick(window, 'validUntil', 'valid_until')) if window else None
    if window is None or valid_until is None or valid_until <= now:
        return {'allowed': False, 'reason': 'agency-window-missing-or-expired'}
    next_opportunity_at = _future_date(
        _pick(window, 'nextOpportunityAt', 'next_opportunity_at'), now
    )
    device_access = _pick(window, 'deviceAccess', 'device_access')
    activity_load = _pick(window, 'activityLoad', 'activity_load')
    if device_access == 'unavailable':
        return _denied('device-unavailable', next_opportunity_at)
    if device_access == 'limited':
        return _denied('device-limited', next_opportunity_at)
    if activity_load == 'overloaded':
        return _denied('schedule-overloaded', next_opportunity_at)
    if _pick(candidate, 'disclosure') == 'personal' and _pick(window, 'privacy') != 'private':
        return _denied('privacy-insufficient', next_opportunity_at)
    last_contact = parse_dt(last_character_message_at)
    minimum_interval_ms = max(0, config.get('minimum_proactive_interval_minutes', 0)) * _MINUTE_MS
    origin = _pick(candidate, 'origin')
    if (
        origin != 'promise'
        and last_contact is not None
        and dt_ms(now) - dt_ms(last_contact) < minimum_interval_ms
    ):
        return {
            'allowed': False,
            'reason': 'minimum-proactive-interval',
            'next_opportunity_at': last_contact + timedelta(milliseconds=minimum_interval_ms),
        }
    if (
        activity_load == 'occupied'
        and origin != 'promise'
        and origin != 'practical-update'
    ):
        return _denied('schedule-occupied', next_opportunity_at)
    return {'allowed': True, 'reason': 'capacity-available'}


def proactive_candidate_fingerprint(candidate: ProactiveContactDraft) -> str:
    """上游 `proactiveCandidateFingerprint`：身份只看目标、来源与证据 id。"""
    source_entry_ids = sorted(_pick(candidate, 'sourceEntryIds', 'source_entry_ids') or [])
    return '|'.join([
        str(_pick(candidate, 'participantId', 'participant_id')),
        str(_pick(candidate, 'origin')),
        ','.join(str(entry_id) for entry_id in source_entry_ids),
    ])


def proactive_recheck_at(
    candidate: ProactiveContactDraft,
    capacity: AgencyCapacityResult,
    window: AgencyWindowState,
    now: datetime,
) -> datetime:
    """上游 `proactiveRecheckAt`：下一次复核时刻（受窗口与候选有效期约束）。"""
    requested = parse_dt(_pick(candidate, 'notBefore', 'not_before'))
    capacity_time = capacity.get('next_opportunity_at')
    window_time = parse_dt(_pick(window, 'nextOpportunityAt', 'next_opportunity_at'))
    fallback = now + timedelta(minutes=30)
    candidates = [
        value
        for value in (requested, capacity_time, window_time)
        if value is not None and value > now
    ]
    selected = min(candidates, key=dt_ms) if candidates else fallback
    parsed_expiry = parse_dt(_pick(candidate, 'expiresAt', 'expires_at'))
    expiry = parsed_expiry if parsed_expiry is not None else now + timedelta(hours=1)
    return _min_datetime(selected, expiry)


def proactive_origin_bypasses_ordinary_interval(origin: ProactiveContactOrigin) -> bool:
    """上游 `proactiveOriginBypassesOrdinaryInterval`：只有 `promise` 绕过普通间隔。"""
    return origin == 'promise'


# --------------------------------------------------------------------------------------
# 内部助手（上游未 export 的模块级函数，一律加 `_` 前缀，见 docs/PORT_PLAN.md §2）
# --------------------------------------------------------------------------------------


def _denied(reason: str, next_opportunity_at: Optional[datetime]) -> AgencyCapacityResult:
    """上游 `{ allowed: false, reason, nextOpportunityAt }` 的合并对象写法。"""
    result: AgencyCapacityResult = {'allowed': False, 'reason': reason}
    if next_opportunity_at is not None:
        result['next_opportunity_at'] = next_opportunity_at
    return result


def _grounded_ids(
    value: Any,
    valid: frozenset[int] | set[int],
    fallback: Optional[int] = None,
) -> list[int]:
    """上游 `groundedIds`：与允许集合求交，空则用正数 fallback 补位，去重后取尾部 20 条。"""
    ids = [entry_id for entry_id in _positive_ids(value) if entry_id in valid]
    if not ids and fallback is not None and fallback > 0:
        ids.append(fallback)
    return _unique(ids)[-20:]


def _positive_ids(value: Any) -> list[int]:
    """上游 `positiveIds`：只接受真正的正整数（`Number.isInteger` 语义，字符串不通过）。"""
    if not isinstance(value, (list, tuple)):
        return []
    result = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            continue
        if item > 0:
            result.append(item)
    return result


def _unique(ids: list[int]) -> list[int]:
    """上游 `Array.from(new Set(ids))`：按首次出现顺序去重。"""
    seen: set[int] = set()
    result = []
    for entry_id in ids:
        if entry_id not in seen:
            seen.add(entry_id)
            result.append(entry_id)
    return result


def _future_date(value: Any, now: datetime) -> Optional[datetime]:
    """上游 `futureDate`：严格晚于 `now` 的时间，否则 `None`。"""
    date = parse_dt(value)
    return date if date is not None and date > now else None


def _min_datetime(left: datetime, right: datetime) -> datetime:
    """上游 `new Date(Math.min(a.getTime(), b.getTime()))`。"""
    return left if dt_ms(left) <= dt_ms(right) else right


def _text(value: Any, limit: int) -> str:
    """上游 `text`：非字符串一律空串；否则 trim 后截断到 `limit`。"""
    return value.strip()[:limit] if isinstance(value, str) else ''


def _finite(value: Any) -> Optional[float]:
    """上游 `finite`：只接受有穷数字（`bool` 在 JS 里也不是 number 语义上的输入）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or number in (float('inf'), float('-inf')):
        return None
    return number


def _clamp(value: float, minimum: float, maximum: float) -> float:
    """上游 `clamp`：`Math.max(min, Math.min(max, value))`。"""
    return max(minimum, min(maximum, value))


def _is_record(value: Any) -> bool:
    """上游 `isRecord`：非空、对象、非数组。"""
    return isinstance(value, Mapping)


def _pick(value: Mapping[str, Any], *keys: str) -> Any:
    """按顺序读取字段：优先上游原文 `camelCase` 键，其次本移植版的 `snake_case` 键。

    上游整条链路都在同一个 `camelCase` 命名空间里，因此 `activeAgencyWindow(normalizeAgencyWindowDraft(...))`
    这类「自产自销」的调用天然成立。按 docs/PORT_PLAN.md §2，本移植版把字段名统一改成
    `snake_case`（`types.py` 的 TypedDict 如此声明），于是这两个函数必须同时认得两种写法的键，
    否则「草稿 → 状态 → 活跃窗口」的往返（以及 `story.state.agency_window` 的反序列化结果）
    会读不到字段。
    """
    for key in keys:
        found = value.get(key)
        if found is not None:
            return found
    return None
