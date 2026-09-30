"""群聊意愿层：一个刻意保持极小的、不调用模型的群聊发言意愿判断。

移植自上游 `src/group-willingness.ts`（Koishi / TypeScript）。它受 YesImBot v3
的「本地分数 / 半衰减 / 概率」模式启发，但始终只作用于单个 HDSI 群，且绝不
影响私聊回合、Agency Window、Alter、prompt 或持久化剧本状态。

语义要点（与上游逐句对应）：
- 分数是纯内存态：每个群一份，不写数据库、不调模型。
- 半衰减：`0.5 ** (elapsedSeconds / decayHalfLifeSeconds)`，衰减后小于 0.001 归零。
- 边际递减：`1 - min(1, score / maxScore) ** 2`，分数越满增益越小。
- 阈值概率：分数必须超过 `threshold` 才会进入概率判定，概率 =
  `(score - threshold) * probabilityAmplifier`，再 clamp 到 [0, 1]。
- 成功发言成本：`consume_group_willingness()` 扣掉 `replyCost`（下限 0）。
- @ 机器人绕过概率门：`mentioned_bot` 直接 `forced-mention`，概率记 1。

随机性：上游用 `input.random ?? Math.random()`；这里同样是「入参 `random`
优先，否则调用随机源」。随机源可通过 `rng` 参数注入（默认 `random.random`），
默认行为与上游一致，且仅在真正需要掷骰时才消耗一次随机数。
"""

from __future__ import annotations

import random as _random
from typing import Callable, List, Literal, Mapping, Optional, TypedDict


class GroupWillingnessConfig(TypedDict):
    enabled: bool
    max_score: float
    threshold: float
    probability_amplifier: float
    decay_half_life_seconds: float
    reply_cost: float
    base_gain: float
    quote_gain: float
    keyword_gain: float
    keywords: List[str]


class GroupWillingnessState(TypedDict):
    score: float
    updated_at: int


GroupWillingnessReason = Literal['disabled', 'forced-mention', 'below-threshold', 'probability-roll']


class GroupWillingnessInput(TypedDict, total=False):
    now: int
    message_count: int
    content: str
    quoted_bot: bool
    mentioned_bot: bool
    random: float


class GroupWillingnessDecision(TypedDict):
    state: GroupWillingnessState
    should_call: bool
    probability: float
    reason: GroupWillingnessReason


DEFAULT_GROUP_WILLINGNESS: GroupWillingnessConfig = {
    'enabled': False,
    'max_score': 1,
    'threshold': 0.24,
    'probability_amplifier': 1.3,
    'decay_half_life_seconds': 180,
    'reply_cost': 0.55,
    'base_gain': 0.12,
    'quote_gain': 0.12,
    'keyword_gain': 0.18,
    'keywords': [],
}


#: 上游 1.0.1-rc23 `WILLINGNESS_TIERS`：五档预设，全部 `enabled: true`、`max_score: 2`。
#: `keywords` 是内容维度、与档位正交，始终从旧配置取（见 `resolve_willingness_preset`）。
WILLINGNESS_TIERS: dict[str, GroupWillingnessConfig] = {
    'quiet': {
        'enabled': True, 'max_score': 2, 'threshold': 0.80, 'base_gain': 0.12,
        'quote_gain': 0.08, 'keyword_gain': 0.12, 'probability_amplifier': 1.1,
        'decay_half_life_seconds': 150, 'reply_cost': 0.85, 'keywords': [],
    },
    'reserved': {
        'enabled': True, 'max_score': 2, 'threshold': 0.75, 'base_gain': 0.17,
        'quote_gain': 0.12, 'keyword_gain': 0.16, 'probability_amplifier': 1.25,
        'decay_half_life_seconds': 200, 'reply_cost': 0.85, 'keywords': [],
    },
    # normal 的标定：单条批次、零衰减下逐条概率 0.155 / 0.458 / 0.730 → 公平掷骰
    # 期望 4.43 条（固定掷骰 0.4 命中第 4 条），所以文档写"约每 4~5 条一次"。
    'normal': {
        'enabled': True, 'max_score': 2, 'threshold': 0.62, 'base_gain': 0.25,
        'quote_gain': 0.15, 'keyword_gain': 0.20, 'probability_amplifier': 1.4,
        'decay_half_life_seconds': 240, 'reply_cost': 0.80, 'keywords': [],
    },
    'active': {
        'enabled': True, 'max_score': 2, 'threshold': 0.30, 'base_gain': 0.34,
        'quote_gain': 0.20, 'keyword_gain': 0.25, 'probability_amplifier': 1.6,
        'decay_half_life_seconds': 300, 'reply_cost': 0.60, 'keywords': [],
    },
    'eager': {
        'enabled': True, 'max_score': 2, 'threshold': 0.10, 'base_gain': 0.45,
        'quote_gain': 0.28, 'keyword_gain': 0.30, 'probability_amplifier': 1.8,
        'decay_half_life_seconds': 360, 'reply_cost': 0.50, 'keywords': [],
    },
}

WILLINGNESS_PRESETS = ('off', 'quiet', 'reserved', 'normal', 'active', 'eager', 'auto', 'custom')
WILLINGNESS_TIER_ORDER = ('quiet', 'reserved', 'normal', 'active', 'eager')

#: 上游 `DEFAULT_AUTO_WILLINGNESS`：auto 档的三态默认映射。
DEFAULT_AUTO_WILLINGNESS = {'busy': 'quiet', 'idle': 'active', 'asleep': 'quiet'}

#: 上游 `LIFE_STATUS_STALE_MS`：生活状态超过 6 小时视为过期，回落 normal。
LIFE_STATUS_STALE_MS = 6 * 60 * 60 * 1000

#: 上游 `ASLEEP_PROBABILITY_MULTIPLIER`：睡眠态的概率乘数（**不可配**的安全余量）。
ASLEEP_PROBABILITY_MULTIPLIER = 0.2


def resolve_willingness_preset(
    preset: object, legacy: Optional[Mapping[str, object]] = None,
) -> tuple[str, GroupWillingnessConfig]:
    """上游 `resolveWillingnessPreset`：把档位名 + 旧数值门解析成实际配置。

    兼容判定**不看任何数值**，只看旧对象的 `enabled === true`：
    - 档位为空或 `off`、且旧门已启用 → `custom`（原样用旧对象，行为字节级不变）；
    - 五档之一 → 用档位参数，`keywords` 仍从旧配置取；
    - `auto` → 返回 auto 标记，真正的档位在 `evaluate_willingness_gate` 里按生活状态选；
    - `custom` → 原样用旧对象；
    - 未知/空 preset → `off`（不放行旧数值）。
    """
    raw = preset.strip() if isinstance(preset, str) else ''
    legacy_record = legacy if isinstance(legacy, Mapping) else {}
    legacy_enabled = legacy_record.get('enabled') is True
    if legacy_enabled and raw in ('', 'off'):
        return 'custom', resolve_group_willingness(legacy_record)
    if raw in WILLINGNESS_TIERS:
        config = dict(WILLINGNESS_TIERS[raw])  # type: ignore[arg-type]
        config['keywords'] = resolve_group_willingness(legacy_record)['keywords']
        return raw, config
    if raw == 'auto':
        return 'auto', dict(DEFAULT_GROUP_WILLINGNESS)
    if raw == 'custom':
        return 'custom', resolve_group_willingness(legacy_record)
    return 'off', {**resolve_group_willingness(legacy_record), 'enabled': False}


def resolve_auto_willingness(value: Optional[Mapping[str, object]]) -> dict[str, str]:
    """上游 `resolveAutoWillingness`：三态映射逐项校验，非法值逐个回落默认。"""
    resolved = dict(DEFAULT_AUTO_WILLINGNESS)
    if isinstance(value, Mapping):
        for status, fallback in DEFAULT_AUTO_WILLINGNESS.items():
            candidate = value.get(status, value.get(status.replace('_', '')))
            if isinstance(candidate, str) and candidate in WILLINGNESS_TIERS:
                resolved[status] = candidate
    return resolved


def resolve_life_status_tier(
    life_status: Optional[Mapping[str, object]], auto_map: Mapping[str, str],
    now_ms: int,
) -> tuple[str, bool, Optional[str]]:
    """上游 `resolveAutoTier`：返回 `(档位, 是否过期, 生活状态原文)`。

    状态缺失 → `normal`（`stale=False`）；状态不在三值内 / 时间解析不出 / 超过 6 小时
    → `normal` 且 `stale=True`。状态存在时按 `auto_map` 取档位。
    """
    if not isinstance(life_status, Mapping):
        return 'normal', False, None
    status = life_status.get('status')
    updated_at = life_status.get('updated_at', life_status.get('updatedAt'))
    if not isinstance(status, str) or status not in DEFAULT_AUTO_WILLINGNESS:
        return 'normal', True, None
    try:
        from datetime import datetime as _dt

        updated_ms = _dt.fromisoformat(str(updated_at).replace('Z', '+00:00')).timestamp() * 1000
    except (TypeError, ValueError):
        return 'normal', True, status
    if now_ms - updated_ms > LIFE_STATUS_STALE_MS:
        return 'normal', True, status
    return auto_map.get(status, 'normal'), False, status


def evaluate_willingness_gate(
    previous: Optional[GroupWillingnessState],
    preset: object,
    auto_map: Optional[Mapping[str, object]],
    life_status: Optional[Mapping[str, object]],
    legacy: Optional[Mapping[str, object]],
    input: GroupWillingnessInput,
    rng: Optional[Callable[[], float]] = None,
) -> dict[str, object]:
    """上游 1.0.1-rc23 `evaluateWillingnessGate`：档位解析层 + 核心打分。

    返回核心 decision 的副本，外加 `diagnosis`（`preset` / `tier` / `life_status` /
    `stale` / `asleep`），日志与测试都读它。核心数学**一字未改**（`evaluate_group_willingness`）。
    """
    resolution, config = resolve_willingness_preset(preset, legacy)
    now_ms = int(input.get('now') or 0)
    tier = 'normal'
    stale = False
    status: Optional[str] = None
    asleep = False
    if resolution == 'auto':
        mapping = resolve_auto_willingness(auto_map)
        tier, stale, status = resolve_life_status_tier(life_status, mapping, now_ms)
        config = dict(WILLINGNESS_TIERS[tier])  # type: ignore[arg-type]
        config['keywords'] = resolve_group_willingness(legacy)['keywords']
        asleep = status == 'asleep'
    elif resolution in WILLINGNESS_TIERS:
        tier = resolution

    if asleep:
        # 睡眠态：@ 不再直通，概率先算基础值再乘 0.2（独立于所配档位的安全余量）。
        base = evaluate_group_willingness(
            previous, config,
            {**input, 'mentioned_bot': False, 'random': 1},
            rng=rng,
        )
        probability = max(0.0, min(1.0, float(base['probability']) * ASLEEP_PROBABILITY_MULTIPLIER))
        roll = input.get('random')
        draw = float(roll) if isinstance(roll, (int, float)) else (rng or _random.random)()
        should_call = draw < probability
        decision: dict[str, object] = {
            'state': base['state'],
            'should_call': should_call,
            'probability': probability,
            'reason': 'probability-roll' if should_call else 'asleep',
        }
    else:
        decision = dict(evaluate_group_willingness(previous, config, input, rng=rng))

    decision['diagnosis'] = {
        'preset': resolution,
        'tier': tier,
        'life_status': status,
        'stale': stale,
        'asleep': asleep,
    }
    return decision


def resolve_group_willingness(
    config: Optional[Mapping[str, object]] = None,
) -> GroupWillingnessConfig:
    """合并默认配置；keywords 逐项转字符串、去空白、去空、最多保留 30 条。"""
    resolved = {**DEFAULT_GROUP_WILLINGNESS, **(config or {})}
    raw_keywords = (config or {}).get('keywords')
    if raw_keywords is None:
        raw_keywords = DEFAULT_GROUP_WILLINGNESS['keywords']
    resolved['keywords'] = [
        item for item in (str(keyword).strip() for keyword in raw_keywords) if item
    ][:30]
    return resolved  # type: ignore[return-value]


def evaluate_group_willingness(
    previous: Optional[GroupWillingnessState],
    config_input: Optional[Mapping[str, object]],
    input: GroupWillingnessInput,
    rng: Optional[Callable[[], float]] = None,
) -> GroupWillingnessDecision:
    """判定本次群消息是否值得发言（只读上游语义，不做任何 I/O）。

    `rng` 为可选随机源（无参、返回 [0, 1) 的浮点数），默认 `random.random`；
    仅当走到 `probability-roll` 且未显式传入 `input['random']` 时才会调用一次，
    调用顺序与上游一致。
    """
    config = resolve_group_willingness(config_input)
    state = _decay(previous, config, input['now'])
    if not config['enabled']:
        return {'state': state, 'should_call': True, 'probability': 1, 'reason': 'disabled'}

    keyword_hit = any(keyword in input['content'] for keyword in config['keywords'])
    raw_gain = (
        config['base_gain'] * max(1, min(3, input['message_count']))
        + (config['quote_gain'] if input.get('quoted_bot') else 0)
        + (config['keyword_gain'] if keyword_hit else 0)
    )
    marginal = 1 - min(1, state['score'] / config['max_score']) ** 2
    state['score'] = _clamp(
        state['score'] + raw_gain * max(0, marginal), 0, config['max_score']
    )

    if input.get('mentioned_bot'):
        return {'state': state, 'should_call': True, 'probability': 1, 'reason': 'forced-mention'}
    if state['score'] <= config['threshold']:
        return {'state': state, 'should_call': False, 'probability': 0, 'reason': 'below-threshold'}

    probability = _clamp(
        (state['score'] - config['threshold']) * config['probability_amplifier'], 0, 1
    )
    draw = input.get('random')
    if draw is None:
        draw = (rng or _random.random)()
    return {
        'state': state,
        'should_call': draw < probability,
        'probability': probability,
        'reason': 'probability-roll',
    }


def consume_group_willingness(
    previous: Optional[GroupWillingnessState],
    config_input: Optional[Mapping[str, object]],
    now: int,
) -> GroupWillingnessState:
    """一次成功发言的代价：衰减后扣掉 `reply_cost`，分数下限 0。"""
    config = resolve_group_willingness(config_input)
    state = _decay(previous, config, now)
    return {'score': max(0, state['score'] - config['reply_cost']), 'updated_at': now}


def consume_willingness_gate(
    previous: Optional[GroupWillingnessState],
    preset: object,
    auto_map: Optional[Mapping[str, object]],
    life_status: Optional[Mapping[str, object]],
    legacy: Optional[Mapping[str, object]],
    now_ms: int,
) -> GroupWillingnessState:
    """上游 `consumeWillingnessGate`：用**同一套档位解析**扣减，保证 replyCost 对齐档位。"""
    resolution, config = resolve_willingness_preset(preset, legacy)
    if resolution == 'auto':
        mapping = resolve_auto_willingness(auto_map)
        tier, _stale, _status = resolve_life_status_tier(life_status, mapping, now_ms)
        config = dict(WILLINGNESS_TIERS[tier])  # type: ignore[arg-type]
        config['keywords'] = resolve_group_willingness(legacy)['keywords']
    return consume_group_willingness(previous, config, now_ms)


def _decay(
    previous: Optional[GroupWillingnessState],
    config: GroupWillingnessConfig,
    now: int,
) -> GroupWillingnessState:
    """半衰期衰减；`previous` 缺省视为 (score=0, updated_at=now)。"""
    score = previous['score'] if previous else 0
    elapsed_seconds = max(0, now - (previous['updated_at'] if previous else now)) / 1_000
    factor = 0.5 ** (elapsed_seconds / max(1, config['decay_half_life_seconds']))
    decayed = score * factor
    return {'score': 0 if decayed < 0.001 else decayed, 'updated_at': now}


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))
