"""Urge 调度器。

逐句移植上游 `src/urge.ts`（Koishi / TypeScript，v1.0.1-beta6-rebuild）。

语义：**仅调度元数据**——Urge 不是联系决策，也不是记忆来源。它把「真实消息热度」
与「已提交原文的 urge/slow 交接」统一折算成下一次推进的时刻与理由。

时间约定
--------
* 对外接受的 ``now`` 首选 timezone-aware ``datetime``（统一 UTC）；为兼容旧 JSON /
  JS 端调用，也接受毫秒数（epoch ms，上游 ``number``）与 ISO 字符串。
* 与上游一致，**状态里存的仍是毫秒整数**（``buckets`` / ``armed.at`` /
  ``burst.started``）。
* ISO 字符串输出走 ``core/time.py`` 的 ``iso()``（与上游 ``toISOString()`` 同形：
  UTC、毫秒三位、``Z`` 结尾）。

命名约定
--------
上游 camelCase 字段按 `docs/PORT_PLAN.md` 转 snake_case（``source_entry_id`` /
``participant_id`` / ``basis_quote`` / ``suggested_delay_minutes`` …）。
读取侧**两种拼写都接受**（旧 JSON 可能仍是 camelCase），写出侧只写 snake_case。
"""

from __future__ import annotations

import math
import random as _random
from typing import Any, Callable, Dict, List, Optional, Sequence, TypedDict

from .time import dt_ms, iso

MINUTE = 60_000  # 上游 `const minute = 60_000`
_FREQUENCIES = ("low", "medium", "high", "custom")


class UrgeConfig(TypedDict, total=False):
    """上游 `UrgeConfig`：配置原始形态（未解析）。"""

    enabled: bool
    frequency: str
    proactive_willingness_threshold: float
    advanced: Dict[str, Any]


class ResolvedUrgeConfig(TypedDict):
    """上游 `ResolvedUrgeConfig`：`resolve_urge_config()` 的输出。"""

    enabled: bool
    frequency: str
    willingness: float
    hot: List[float]
    idle: List[float]
    burst: List[float]
    slow: List[float]
    half_life: float
    threshold: float
    jitter: float
    extreme_chance: float
    ttl: float
    budget: int
    contact_min: float


class ArmedState(TypedDict, total=False):
    participant_id: str
    entry_id: int
    at: int


class BurstState(TypedDict, total=False):
    participant_id: str
    started: int
    used: int


class UrgeState(TypedDict, total=False):
    """上游 `UrgeState`（可选字段缺失即视为 `None`，与 TS 的 `undefined` 对应）。"""

    version: int
    mode: str
    buckets: List[int]
    value: float
    pace: str
    suggested: float
    source_entry_id: int
    armed: Optional[ArmedState]
    burst: Optional[BurstState]
    spent: bool
    reason: str


# ---------------------------------------------------------------------------
# 时间助手：统一走 core/time.py（`iso` / `dt_ms`）
# ---------------------------------------------------------------------------


def _to_ms(value: Any) -> int:
    """归一为 epoch 毫秒；无法解析时退化为 0（调用方永远应传有效时间）。"""
    return dt_ms(value)


# ---------------------------------------------------------------------------
# 防御读取原语（上游 `record` / `finite` / `timestamp`）
# ---------------------------------------------------------------------------


def _is_number(value: Any) -> bool:
    """等价于 JS `typeof value === 'number'`（bool 不算 number）。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_finite_number(value: Any) -> bool:
    """等价于 JS `typeof value === 'number' && Number.isFinite(value)`。"""
    if not _is_number(value):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, ValueError):
        return False


def _record(value: Any) -> Dict[str, Any]:
    """等价于上游 `record`：非（非数组）对象一律读作空对象。"""
    return value if isinstance(value, dict) else {}


def _finite(value: Any, fallback: float, minimum: float, maximum: float) -> float:
    """等价于上游 `finite`：有限数才夹取，否则回落 fallback。"""
    if _is_finite_number(value):
        return max(minimum, min(maximum, value))
    return fallback


def _pick(source: Any, *names: str) -> Any:
    """按顺序取第一个存在的键（用于同时兼容 snake_case 与上游 camelCase）。"""
    source = _record(source)
    for name in names:
        if name in source:
            return source[name]
    return None


def _timestamp(value: Any, now_ms: int) -> bool:
    """上游 `timestamp`：有限数、>0、且不晚于 now。"""
    return _is_finite_number(value) and value > 0 and value <= now_ms


# ------------------------------------------------resolve_urge_config

def resolve_urge_config(raw: Any = None) -> Dict[str, Any]:
    """解析 Urge 配置：档位默认值 + 夹取 + 防御读取。对应上游 `resolveUrgeConfig`。"""
    c = _record(raw)
    a = _record(_pick(c, "advanced"))
    frequency_raw = _pick(c, "frequency")
    frequency = frequency_raw if isinstance(frequency_raw, str) and frequency_raw in _FREQUENCIES else "medium"
    if frequency == "low":
        defaults = [20, 30, 60, 90, 5, 10, 120, 180]
    elif frequency == "high":
        defaults = [6, 12, 20, 35, 2, 5, 90, 120]
    else:
        defaults = [10, 20, 35, 55, 3, 7, 110, 130]

    def range_(name: str, index: int) -> List[float]:
        lo = _finite(_pick(a, name + "Min", name + "_min"), defaults[index], 1, 1440)
        hi = _finite(_pick(a, name + "Max", name + "_max"), defaults[index + 1], 1, 1440)
        return [lo, max(lo, hi)]

    return {
        "enabled": _pick(c, "enabled") is True,
        "frequency": frequency,
        "willingness": _finite(_pick(c, "proactiveWillingnessThreshold", "proactive_willingness_threshold"), 0.4, 0, 1),
        "hot": range_("hot", 0),
        "idle": range_("idle", 2),
        "burst": range_("burst", 4),
        "slow": range_("slow", 6),
        "half_life": _finite(_pick(a, "halfLifeMinutes", "half_life_minutes"), 45, 5, 240),
        "threshold": _finite(_pick(a, "burstThreshold", "burst_threshold"), 0.75, 0, 1),
        "jitter": _finite(_pick(a, "jitter"), 0.15, 0, 1),
        "extreme_chance": _finite(_pick(a, "extremeChance", "extreme_chance"), 0.03, 0, 1),
        "ttl": _finite(_pick(a, "burstTtlMinutes", "burst_ttl_minutes"), 35, 5, 120),
        "budget": int(math.floor(_finite(_pick(a, "burstBudget", "burst_budget"), 3, 0, 10))),
        "contact_min": _finite(_pick(a, "burstContactMinMinutes", "burst_contact_min_minutes"), 5, 1, 60),
    }


_RESOLVED_ALIASES = {
    "willingness": ("proactiveWillingnessThreshold", "proactive_willingness_threshold"),
    "half_life": ("halfLife", "halfLifeMinutes", "half_life_minutes"),
    "threshold": ("burstThreshold",),
    "extreme_chance": ("extremeChance",),
    "ttl": ("burstTtl", "burstTtlMinutes"),
    "budget": ("burstBudget",),
    "contact_min": ("burstContactMinMinutes",),
}


def _resolved_config(c: Any) -> Dict[str, Any]:
    """把已解析配置归一为规范 snake_case 键（同时接受上游 camelCase 形态）。

    仅做键名归一与缺省补齐，不做夹取（夹取已在 `resolve_urge_config` 完成）。
    """
    resolved = resolve_urge_config({})
    source = _record(c)
    for key, aliases in _RESOLVED_ALIASES.items():
        value = _pick(source, key, *aliases)
        if value is not None:
            resolved[key] = value
    for key in resolved:
        if key in _RESOLVED_ALIASES:
            continue
        value = _pick(source, key)
        if value is not None:
            resolved[key] = value
    return resolved


# ---------------------------------------------------normalize_urge_state

def normalize_urge_state(raw: Any, now: Any) -> Dict[str, Any]:
    """防御性归一 Urge 状态；版本不符（非 1）时返回干净空状态。对应上游 `normalizeUrgeState`。"""
    now_ms = _to_ms(now)
    r = _record(raw)
    version = _pick(r, "version")
    if not (_is_number(version) and version == 1):
        return {"version": 1, "buckets": []}
    a = _record(_pick(r, "armed"))
    b = _record(_pick(r, "burst"))

    buckets: List[Any] = []
    raw_buckets = _pick(r, "buckets")
    if isinstance(raw_buckets, list):
        kept: List[Any] = []
        for value in raw_buckets:
            if _timestamp(value, now_ms) and (now_ms - value) < 240 * MINUTE:
                if value not in kept:
                    kept.append(value)
        kept.sort()
        buckets = kept[-120:]

    value_raw = _pick(r, "value")
    suggested_raw = _pick(r, "suggested")
    source_entry_raw = _pick(r, "sourceEntryId", "source_entry_id")
    reason_raw = _pick(r, "reason")
    mode_raw = _pick(r, "mode")

    participant_id = _pick(a, "participantId", "participant_id")
    entry_id = _pick(a, "entryId", "entry_id")
    armed_at = _pick(a, "at")
    armed = (
        {"participant_id": participant_id, "entry_id": entry_id, "at": armed_at}
        if isinstance(participant_id, str)
        and _is_number(entry_id)
        and _timestamp(armed_at, now_ms)
        and (now_ms - armed_at) < 10 * MINUTE
        else None
    )

    burst_participant = _pick(b, "participantId", "participant_id")
    burst_started = _pick(b, "started")
    burst = (
        {
            "participant_id": burst_participant,
            "started": burst_started,
            "used": int(math.floor(_finite(_pick(b, "used"), 0, 0, 10))),
        }
        if isinstance(burst_participant, str) and _timestamp(burst_started, now_ms)
        else None
    )

    return {
        "version": 1,
        "mode": mode_raw if isinstance(mode_raw, str) else None,
        "buckets": buckets,
        "value": _finite(value_raw, 0, 0, 1) if _is_number(value_raw) else None,
        "pace": "slow" if _pick(r, "pace") == "slow" else "normal",
        "suggested": _finite(suggested_raw, 120, 1, 1440) if _is_number(suggested_raw) else None,
        "source_entry_id": source_entry_raw if _is_number(source_entry_raw) else None,
        "spent": _pick(r, "spent") is True,
        "reason": reason_raw[:160] if isinstance(reason_raw, str) else None,
        "armed": armed,
        "burst": burst,
    }


# ------------------------------------------------------urge_user_event

def urge_user_event(state: Any, now: Any) -> Dict[str, Any]:
    """真实来信事件：记账两分钟桶、清空慢速/授权/连发。对应上游 `urgeUserEvent`。"""
    now_ms = _to_ms(now)
    # 两分钟桶合并打字碎片；只有真实来信会调用这里。
    bucket = math.floor(now_ms / (2 * MINUTE)) * 2 * MINUTE
    s = _record(state)
    existing = _pick(s, "buckets")
    merged: List[Any] = []
    for value in list(existing if isinstance(existing, list) else []) + [bucket]:
        if value not in merged:
            merged.append(value)
    return {
        **s,
        "buckets": merged[-120:],
        "pace": "normal",
        "suggested": None,
        "armed": None,
        "burst": None,
        "spent": False,
    }


# ----------------------------------------------------------urge_density

def urge_density(state: Any, now: Any, c: Any) -> float:
    """真实热度：近 15 分钟消息桶密度 × 半衰期衰减。对应上游 `urgeDensity`。"""
    now_ms = _to_ms(now)
    s = _record(state)
    buckets = _pick(s, "buckets")
    if not isinstance(buckets, list) or not buckets:
        return 0
    last = buckets[-1]
    if not _is_number(last):
        return 0
    recent = sum(1 for t in buckets if _is_number(t) and t >= last - 15 * MINUTE)
    cfg = _resolved_config(c)
    return min(1, recent / 6) * (0.5 ** (max(0, now_ms - last) / (cfg["half_life"] * MINUTE)))


# -----------------------------------------------------------commit_urge

def commit_urge(
    state: Any,
    raw: Any,
    script: str,
    entry_id: Any,
    target: Optional[str],
    now: Any,
    c: Any,
    random: Optional[Callable[[], float]] = None,
) -> Dict[str, Any]:
    """提交一次 urge 交接（必须引用已提交剧本原文）。对应上游 `commitUrge`。"""
    rng = random or _random.random
    s = _record(state)
    cfg = _resolved_config(c)
    r = _record(raw)
    quote_raw = _pick(r, "basisQuote", "basis_quote")
    quote = quote_raw.strip() if isinstance(quote_raw, str) else ""
    value_raw = _pick(r, "value")
    # 缺失/非法的交接不能延续旧的 slow 相位或 burst。
    if not quote or quote not in (script or "") or not _is_finite_number(value_raw):
        return {**s, "pace": "normal", "suggested": None, "armed": None, "burst": None}
    if rng() < cfg["extreme_chance"]:
        value = rng()
    else:
        value = _finite(value_raw + (rng() * 2 - 1) * cfg["jitter"], 0, 0, 1)
    pace = "slow" if _pick(r, "pace") == "slow" else "normal"
    high = value >= cfg["threshold"] and pace != "slow"
    suggested_raw = _pick(r, "suggestedDelayMinutes", "suggested_delay_minutes")
    armed = None
    if high and target and not s.get("spent") and not s.get("burst") and cfg["budget"] > 0:
        armed = {"participant_id": target, "entry_id": entry_id, "at": _to_ms(now)}
    return {
        **s,
        "value": value,
        "pace": pace,
        "source_entry_id": entry_id,
        "suggested": _finite(suggested_raw, 120, 1, 1440) if _is_number(suggested_raw) else None,
        "burst": s.get("burst") if high else None,
        "armed": armed,
    }


# ------------------------------------------------------acknowledge_urge

def acknowledge_urge(state: Any, participant_id: str, entry_id: Any, now: Any) -> Dict[str, Any]:
    """投递回执确认授权：开启连发窗口。对应上游 `acknowledgeUrge`。"""
    s = _record(state)
    armed = _pick(s, "armed")
    if not isinstance(armed, dict):
        return state
    if (
        _pick(armed, "participantId", "participant_id") != participant_id
        or _pick(armed, "entryId", "entry_id") != entry_id
        or s.get("spent")
    ):
        return state
    return {
        **s,
        "armed": None,
        "spent": True,
        "burst": {"participant_id": participant_id, "started": _to_ms(now), "used": 0},
    }


# -----------------------------------------------------urge_burst_active

def urge_burst_active(state: Any, now: Any, c: Any, participant_id: Optional[str] = None) -> bool:
    """连发窗口是否仍然有效。对应上游 `urgeBurstActive`。"""
    cfg = _resolved_config(c)
    s = _record(state)
    burst = _pick(s, "burst")
    if not isinstance(burst, dict):
        return False
    started = _pick(burst, "started")
    used = _pick(burst, "used")
    if not _is_finite_number(started) or not _is_finite_number(used):
        return False
    if participant_id and participant_id != _pick(burst, "participantId", "participant_id"):
        return False
    return (
        (_to_ms(now) - started) < cfg["ttl"] * MINUTE
        and used <= cfg["budget"]
        and _pick(s, "pace") != "slow"
    )


# -------------------------------------------------------------plan_urge

def plan_urge(
    state: Any,
    now: Any,
    c: Any,
    rest_minutes: float = 0,
    unavailable: bool = False,
    random: Optional[Callable[[], float]] = None,
) -> Dict[str, Any]:
    """采样下一次推进时刻。对应上游 `planUrge`。

    返回 ``{"state", "next_advance_at", "minutes", "reason"}``；``next_advance_at``
    是 ISO 字符串（与上游 `toISOString()` 对齐，由 core/time.py 的 ``iso()`` 产出）。
    """
    rng = random or _random.random
    now_ms = _to_ms(now)
    s = _record(state)
    cfg = _resolved_config(c)

    def sample(range_: Sequence[float]) -> float:
        return range_[0] + rng() * (range_[1] - range_[0])

    nxt = dict(s)
    suggested = _pick(s, "suggested")
    if rest_minutes > 0 or _pick(s, "pace") == "slow":
        if _is_number(suggested):
            minutes = max(cfg["slow"][0], min(cfg["slow"][1], suggested * (0.9 + rng() * 0.2)))
        else:
            # 上游在 suggested 为 undefined 时采样；错类型（会算出 NaN）按同一路径防御。
            minutes = sample(cfg["slow"])
        minutes = max(rest_minutes, minutes)
        reason = "rest-window" if rest_minutes else "script-slow"
        nxt["burst"] = None
        nxt["armed"] = None
    elif not unavailable and urge_burst_active(s, now, cfg) and _pick(s["burst"], "used") < cfg["budget"]:
        burst = _record(s.get("burst"))
        used = burst.get("used")
        minutes = sample(cfg["burst"]) * (2 ** used)
        minutes = min(minutes, max(1, (burst.get("started") + cfg["ttl"] * MINUTE - now_ms) / MINUTE))
        reason = "confirmed-contact-burst"
        nxt["burst"] = {**burst, "used": used + 1}
    else:
        idle = sample(cfg["idle"])
        hot = min(idle, sample(cfg["hot"]))
        minutes = idle - urge_density(s, now, cfg) * (idle - hot)
        reason = "conversation-density-decay"
        nxt["burst"] = None
        if unavailable:
            nxt["armed"] = None
    nxt["reason"] = reason
    return {
        "state": nxt,
        "next_advance_at": iso(now_ms + minutes * MINUTE),
        "minutes": minutes,
        "reason": reason,
    }


# -------------------------------------------------------urge_instruction

def urge_instruction(enabled: bool, phase: str) -> str:
    """把 urge 交接说明注入主叙事提示词（仅自动推进类相位）。对应上游 `urgeInstruction`。"""
    if not enabled or phase not in ("advance", "conversation-follow-up", "intent-due"):
        return ""
    return (
        '\nAfter writing the full script and its actions, optionally return urge:{value:0..1, pace:"normal"|"slow", '
        'suggestedDelayMinutes:number, basisQuote:"exact sentence from this script"}. Reflect the protagonist\'s '
        "present impulse and natural next opportunity to continue life; slow suits sleep or sustained focus. "
        "This is only a scheduling handoff, not speech, a future event, or a second contact decision. "
        "Continue using the existing authored actions and proactiveContact for any contact."
    )


__all__ = [
    "MINUTE",
    "UrgeConfig",
    "UrgeState",
    "ResolvedUrgeConfig",
    "resolve_urge_config",
    "normalize_urge_state",
    "urge_user_event",
    "urge_density",
    "commit_urge",
    "acknowledge_urge",
    "urge_burst_active",
    "plan_urge",
    "urge_instruction",
    "dt_ms",
]
