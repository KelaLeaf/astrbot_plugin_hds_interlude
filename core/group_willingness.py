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
