"""Alter System（氛围位移系统）—— 上游 `src/alter.ts` 的逐句移植。

上游：Koishi / TypeScript，`upstream/src/alter.ts`（302 行，v1.0.1-beta6-rebuild）。
本模块与上游 **一一对应**，公开 API 与上游 export 完全同名单数：

===============================  ==================================================
上游                            本模块
===============================  ==================================================
``DEFAULT_ALTER_SYSTEM_CONFIG``  ``DEFAULT_ALTER_SYSTEM_CONFIG``
``resolveAlterSystemConfig``     ``resolve_alter_system_config``
``normalizeAlterValue``          ``normalize_alter_value``
``createAlterSystemState``       ``create_alter_system_state``
``normalizeAlterSystemState``    ``normalize_alter_system_state``
``calculateAlterThreshold``      ``calculate_alter_threshold``
``adjustAlterWeight``            ``adjust_alter_weight``
``advanceAlterSystem``           ``advance_alter_system``
``completeAlterAnalysis``        ``complete_alter_analysis``
``emotionalOffsetForPrompt``     ``emotional_offset_for_prompt``
``alterAnalysisCoolingDown``     ``alter_analysis_cooling_down``
``alterScopeCoolingDown``        ``alter_scope_cooling_down``
``markAlterScopeAnalysisAttempt````mark_alter_scope_analysis_attempt``
``alterScopeValue``              ``alter_scope_value``
``alterHistoryForScope``         ``alter_history_for_scope``
``AlterTurnResult``              ``AlterTurnResult``
===============================  ==================================================

语言映射（见键名约定）
------------------------------------
* 领域对象一律 `dict`；字段名 ``camelCase`` → ``snake_case``（``alter_value`` /
  ``alter_weight`` / ``last_trigger_direction`` / ``emotional_offset`` /
  ``pending_scopes`` / ``last_analysis_attempt_at`` / ``participant_id`` …）。
* 时间统一 timezone-aware UTC ``datetime``；ISO 输出走 `core/time.py` 的 ``iso()``
  （与上游 ``Date#toISOString()`` 同形：UTC、毫秒三位、``Z`` 结尾）。状态里存的
  仍是 ISO **字符串**（上游就是这么持久化的，见时间约定最后一条）。
* ``Math.round`` → :func:`_js_round`（**半值向上**，不是 Python 的银行家舍入）。
* ``?? fallback`` → ``_pick()`` 只认「键存在且值为 None 以外」；``undefined`` 位置
  一律由 :func:`_finite_number` 的 fallback 承担。

歧义点与处理（并行 Agent 请照此协作）
------------------------------------
1. **读写拼写**：写出侧只写 ``snake_case``；读取侧 **两种拼写都接受**
   （``_pick(obj, 'alter_value', 'alterValue')``）。理由：上游测试与历史数据里
   的旧 JSON 就是 camelCase（``alter-system.test.ts`` 的第 5 条用例专门喂
   ``lastTriggerAlter`` / ``emotionalOffset`` / ``lastUpdatedAt`` 做「遗留状态归一」），
   而 键名约定 要求新写入的字段名是 snake_case。二者不冲突。
2. **缺失的配置键**：上游直接读 ``config.maxIntensity`` 这类必填字段，缺键会得到
   ``NaN``；本移植版回落到 ``DEFAULT_ALTER_SYSTEM_CONFIG`` 里的同名默认值。
   正常调用点都会先过 ``resolve_alter_system_config()``，故行为一致。
3. **``scope.lastAnalysisAttemptAt = undefined``**：JS 里是「写入 undefined」，
   序列化后等于键消失；Python 里改为 **删除该键**。
4. **``now`` 参数**：上游要求 ``Date``；本模块统一接受 ``datetime`` / ISO 字符串 /
   毫秒数（``core/time.py`` 的 ``parse_dt``），无法解析时退回 ``utc_now()``。
5. **``completeAlterAnalysis`` 的方向**：上游写 ``Math.sign(triggerValue) as -1 | 1``，
   类型断言不改变运行期 ``0`` 的可能值；本移植版保留 ``-1 | 0 | 1``。
"""

from __future__ import annotations

import math
from typing import Any, Optional, TypedDict

from .time import dt_ms, iso, parse_dt, utc_now
from .types import (
    AlterHistoryEntry,
    AlterPendingScope,
    AlterSystemConfig,
    AlterSystemState,
    EmotionalOffsetPrompt,
    NarrativePhase,
)

__all__ = [
    'DEFAULT_ALTER_SYSTEM_CONFIG',
    'AlterTurnResult',
    'resolve_alter_system_config',
    'normalize_alter_value',
    'create_alter_system_state',
    'normalize_alter_system_state',
    'calculate_alter_threshold',
    'adjust_alter_weight',
    'advance_alter_system',
    'complete_alter_analysis',
    'emotional_offset_for_prompt',
    'alter_analysis_cooling_down',
    'alter_scope_cooling_down',
    'mark_alter_scope_analysis_attempt',
    'alter_scope_value',
    'alter_history_for_scope',
]

# 上游 `const HOUR = 60 * 60 * 1000`
HOUR_MS = 60 * 60 * 1000
# 上游 `const HISTORY_LIMIT = 50`
HISTORY_LIMIT = 50
# 上游 `normalizePendingScopes` 末尾的 `.slice(0, 32)`
PENDING_SCOPE_LIMIT = 32
# 上游 `alterAnalysisCoolingDown` / `alterScopeCoolingDown` 的默认冷却窗口 5 分钟
DEFAULT_COOLDOWN_MS = 5 * 60 * 1000

_ALERT_VALUE_LIMIT = 1_000
_PHASES = ('advance', 'conversation-follow-up', 'user-message', 'intent-due')
# 上游 `new Date(0).toISOString()`
_EPOCH_ISO = '1970-01-01T00:00:00.000Z'


# ======================================================================================
# 配置
# ======================================================================================

# 上游 `export const DEFAULT_ALTER_SYSTEM_CONFIG`：默认值逐字照抄。
DEFAULT_ALTER_SYSTEM_CONFIG: AlterSystemConfig = {
    'enabled': False,
    'base_threshold': 10,
    'density_factor': 0.3,
    'same_direction_boost': 0.05,
    'opposite_decay': 0.15,
    'min_weight': 0.2,
    'max_intensity': 2,
    'model_id': '',
    'provider_id': '',
    'model': '',
    'temperature': 0.3,
    'top_p': 1,
    'max_tokens': 400,
    'timeout': 30_000,
    'prompt': '',
}

# 上游 Console 配置项是 camelCase；端口配置（`_conf_schema.json`）是 snake_case。
# 为了让两种来源都能被 `resolve_alter_system_config` / `_cfg_number` 正确读取，这里
# 给出规范映射：本模块一律接受两种拼写，写出只写 snake_case。
_CONFIG_ALIASES: dict[str, str] = {
    'base_threshold': 'baseThreshold',
    'density_factor': 'densityFactor',
    'same_direction_boost': 'sameDirectionBoost',
    'opposite_decay': 'oppositeDecay',
    'min_weight': 'minWeight',
    'max_intensity': 'maxIntensity',
    'model_id': 'modelId',
    'provider_id': 'providerId',
    'max_tokens': 'maxTokens',
    'top_p': 'topP',
}
_CONFIG_SNAKE: dict[str, str] = {camel: snake for snake, camel in _CONFIG_ALIASES.items()}


class AlterTurnResult(TypedDict, total=False):
    """上游 `interface AlterTurnResult`：一次 ``advanceAlterSystem`` 的结果。"""

    state: AlterSystemState
    threshold: float
    offset_expired: bool
    threshold_reached: bool
    source_participant_id: str
    trigger_value: float


def resolve_alter_system_config(value: Optional[dict[str, Any]] = None) -> AlterSystemConfig:
    """上游 `resolveAlterSystemConfig`：``{ ...DEFAULT, ...value }``。

    额外做一件上游不需要做的事：把 camelCase 键名归一为 snake_case（上游两种写法
    不会同时出现，本移植版可能同时收到 Console 旧配置与 AstrBot 新配置）。
    """
    resolved: dict[str, Any] = dict(DEFAULT_ALTER_SYSTEM_CONFIG)
    if isinstance(value, dict):
        for key, item in value.items():
            resolved[_CONFIG_SNAKE.get(key, key)] = item
    return resolved  # type: ignore[return-value]


# ======================================================================================
# 防御读取原语（对应上游文件末尾的 clamp / finiteNumber / dateValue / isRecord）
# ======================================================================================


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


def _finite_number(value: Any, fallback: float) -> float:
    """上游 `finiteNumber(value, fallback)`。"""
    return value if _is_finite_number(value) else fallback


def _js_round(value: float) -> int:
    """等价于 JS `Math.round`：**半值向 +∞ 取整**（``-2.5 → -2``，``2.5 → 3``）。"""
    return int(math.floor(value + 0.5))


def _sign(value: float) -> int:
    """等价于 JS `Math.sign`（输入恒为有限数，故不处理 NaN）。"""
    if value == 0:
        return 0
    return 1 if value > 0 else -1


def _clamp(value: float, minimum: float, maximum: float) -> float:
    """上游 `clamp(value, min, max)`。"""
    return max(minimum, min(maximum, value))


def _is_record(value: Any) -> bool:
    """等价于上游 `isRecord`：非 null 的对象且不是数组。"""
    return isinstance(value, dict)


def _pick(source: Any, *names: str) -> Any:
    """按顺序取第一个「键存在且值不为 None」的键。

    用于同时兼容 snake_case（本移植版写出）与 camelCase（上游旧 JSON）。
    上游的 ``?? fallback`` 语义由调用点的 :func:`_finite_number` 等 fallback 承担。
    """
    if not isinstance(source, dict):
        return None
    for name in names:
        if name in source and source[name] is not None:
            return source[name]
    return None


# 状态字段的 camelCase → snake_case 映射：只做键名归一，**不改任何数值语义**
# （不夹取、不重解析时间），等价于上游的浅展开 `{ ...state }`。
_STATE_KEY_ALIASES: dict[str, str] = {
    'alterValue': 'alter_value',
    'alterWeight': 'alter_weight',
    'lastTriggerDirection': 'last_trigger_direction',
    'emotionalOffset': 'emotional_offset',
    'pendingScopes': 'pending_scopes',
    'lastUpdatedAt': 'last_updated_at',
    'lastAnalysisAttemptAt': 'last_analysis_attempt_at',
}


def _canonical_state(value: Any) -> dict[str, Any]:
    """浅拷贝领域对象，并把遗留 camelCase 键名归一为 snake_case。

    上游只有一种拼写，本移植版要同时吃下 AstrBot 侧的新状态与 DB 里的旧 JSON，
    因此在**写入路径**上也做一次键名归一，避免同一字段以两种键名共存。
    """
    if not isinstance(value, dict):
        return {}
    return {_STATE_KEY_ALIASES.get(key, key): item for key, item in value.items()}


def _cfg_number(config: Any, key: str) -> float:
    """读取必填数值配置：缺键时回落到 ``DEFAULT_ALTER_SYSTEM_CONFIG``（上游会得到 NaN）。"""
    alias = _CONFIG_ALIASES.get(key)
    fallback = DEFAULT_ALTER_SYSTEM_CONFIG.get(key)  # type: ignore[arg-type]
    return _finite_number(_pick(config, key, alias) if alias else _pick(config, key), fallback)


def _to_dt(value: Any):
    """把 ``now`` 归一为 timezone-aware UTC ``datetime``；无法解析时用当前时刻。"""
    parsed = parse_dt(value)
    return parsed if parsed is not None else utc_now()


def _normalized_iso(value: Any) -> Optional[str]:
    """上游 `normalizedIso`：能解析则返回 ``toISOString()``，否则 ``undefined``。"""
    return iso(value)


def _normalize_phase(value: Any) -> NarrativePhase:
    """上游 `normalizePhase`：非法值回落 ``'user-message'``。"""
    if isinstance(value, str) and value in _PHASES:
        return value  # type: ignore[return-value]
    return 'user-message'


# ======================================================================================
# 值 / 状态
# ======================================================================================


def normalize_alter_value(value: Any) -> Optional[int]:
    """上游 `normalizeAlterValue`：模型给出的氛围位移是有界整数，非法值忽略。"""
    if not _is_finite_number(value):
        return None
    # 上游 `Math.max(-5, Math.min(5, Math.round(value)))`
    return int(_clamp(_js_round(value), -5, 5))


def create_alter_system_state(now: Any = None) -> AlterSystemState:
    """上游 `createAlterSystemState`：干净的新 Alter 状态（**不含** ``pendingScopes``）。"""
    return {
        'alter_value': 0,
        'alter_weight': 0,
        'last_trigger_direction': 0,
        'emotional_offset': None,
        'history': [],
        'last_updated_at': iso(_to_dt(now)),
    }


def normalize_alter_system_state(value: Any) -> Optional[AlterSystemState]:
    """上游 `normalizeAlterSystemState`：对 ``unknown`` 做防御性归一。

    兼容遗留 JSON：``lastTriggerAlter``（更早的字段）在没有
    ``lastTriggerDirection`` 时提供方向；只有 ``alterValue`` 没有 ``pendingScopes``
    的旧状态会被解读为「主角/全局生活流」的那一桶。
    """
    if not _is_record(value):
        return None

    history: list[AlterHistoryEntry] = []
    raw_history = value.get('history')
    if isinstance(raw_history, list):
        for index, entry in enumerate(raw_history):
            if not _is_record(entry):
                continue
            item: AlterHistoryEntry = {
                'turn': max(1, math.floor(_finite_number(_pick(entry, 'turn'), index + 1))),
                'phase': _normalize_phase(_pick(entry, 'phase')),
                'alter': normalize_alter_value(_pick(entry, 'alter')) or 0,
                'alter_value': _clamp(
                    _finite_number(_pick(entry, 'alter_value', 'alterValue'), 0),
                    -_ALERT_VALUE_LIMIT, _ALERT_VALUE_LIMIT,
                ),
                'timestamp': _normalized_iso(_pick(entry, 'timestamp')) or _EPOCH_ISO,
            }
            participant_id = _pick(entry, 'participant_id', 'participantId')
            if isinstance(participant_id, str) and participant_id.strip():
                item['participant_id'] = participant_id.strip()[:255]
            history.append(item)
        history = history[-HISTORY_LIMIT:]

    emotional_offset = None
    raw_offset = _pick(value, 'emotional_offset', 'emotionalOffset')
    if _is_record(raw_offset) and isinstance(_pick(raw_offset, 'description'), str):
        emotional_offset = {
            'direction': 'relaxed' if _pick(raw_offset, 'direction') == 'relaxed' else 'serious',
            'description': _pick(raw_offset, 'description').strip()[:800],
            'intensity': _clamp(_finite_number(_pick(raw_offset, 'intensity'), 1), 0, 3),
            'generated_at': _normalized_iso(
                _pick(raw_offset, 'generated_at', 'generatedAt'),
            ) or _EPOCH_ISO,
        }

    legacy_direction = _sign(_finite_number(_pick(value, 'last_trigger_alter', 'lastTriggerAlter'), 0))
    direction = _sign(
        _finite_number(_pick(value, 'last_trigger_direction', 'lastTriggerDirection'), legacy_direction),
    )

    pending_scopes = _normalize_pending_scopes(_pick(value, 'pending_scopes', 'pendingScopes'))
    alter_value = _clamp(
        _finite_number(_pick(value, 'alter_value', 'alterValue'), 0),
        -_ALERT_VALUE_LIMIT, _ALERT_VALUE_LIMIT,
    )
    # 只有旧状态（作用域分桶之前）需要这次「归属主角/全局流」的迁移。
    if not pending_scopes and abs(alter_value) > 0:
        pending_scopes.append({'participant_id': '', 'alter_value': alter_value})

    return {
        'alter_value': alter_value,
        'alter_weight': _clamp(_finite_number(_pick(value, 'alter_weight', 'alterWeight'), 0), 0, 1),
        'last_trigger_direction': direction,  # type: ignore[typeddict-item]
        'emotional_offset': emotional_offset,
        'history': history,
        'pending_scopes': pending_scopes,
        'last_updated_at': _normalized_iso(
            _pick(value, 'last_updated_at', 'lastUpdatedAt'),
        ) or _EPOCH_ISO,
        'last_analysis_attempt_at': _normalized_iso(
            _pick(value, 'last_analysis_attempt_at', 'lastAnalysisAttemptAt'),
        ),
    }


# ======================================================================================
# 阈值 / 权重
# ======================================================================================


def calculate_alter_threshold(
    history: list[AlterHistoryEntry],
    config: AlterSystemConfig,
    now: Any = None,
) -> float:
    """上游 `calculateAlterThreshold`：密度越高阈值越低，但绝不低于基准的一半。

    密度只统计最近一小时内的条目（``HOUR_MS``）；10 条即达到满密度。
    """
    now_ms = dt_ms(_to_dt(now))
    one_hour_ago = now_ms - HOUR_MS
    turns = 0
    for entry in history or []:
        if not _is_record(entry):
            continue
        # 上游 `dateValue(entry.timestamp)?.getTime() ?? 0`：无法解析的时间戳按 0 计。
        if dt_ms(_pick(entry, 'timestamp')) >= one_hour_ago:
            turns += 1
    density = min(turns / 10, 1)
    base = max(1, _finite_number(_pick(config, 'base_threshold', 'baseThreshold'), 10))
    factor = _clamp(_finite_number(_pick(config, 'density_factor', 'densityFactor'), 0.3), 0, 1)
    return max(base * 0.5, base * (1 - density * factor))


def adjust_alter_weight(
    weight: float,
    same_direction: bool,
    magnitude: float,
    config: AlterSystemConfig,
) -> float:
    """上游 `adjustAlterWeight`：同向增益、反向衰减，权重恒在 ``[0, 1]``。"""
    rate = _cfg_number(config, 'same_direction_boost') if same_direction \
        else -_cfg_number(config, 'opposite_decay')
    return _clamp(weight + max(0, magnitude) * _finite_number(rate, 0), 0, 1)


# ======================================================================================
# 推进 / 结算
# ======================================================================================


def advance_alter_system(
    current: Optional[AlterSystemState],
    alter: float,
    phase: NarrativePhase,
    now: Any,
    config: AlterSystemConfig,
    participant_id: str = '',
) -> AlterTurnResult:
    """上游 `advanceAlterSystem`：把一次模型位移累进它的**来源桶**并给出阈值判定。

    关键点（逐条照抄上游）：
    * 位移按 ``participant_id`` 分桶；故事的 ``alter_value`` 恒为所有桶之和（±1000）。
    * ``threshold_reached`` 只对比 **本次来源桶** 的值与阈值，绝不借用其它关系的证据。
    * 已有情绪偏移时，权重按同向/反向调整；跌破 ``min_weight`` 就立即过期并清零。
    """
    moment = _to_dt(now)
    if isinstance(current, dict):
        state: AlterSystemState = _canonical_state(current)  # type: ignore[assignment]
        state['history'] = list(current.get('history') or [])
        state['pending_scopes'] = _normalize_pending_scopes(state.get('pending_scopes'))
    else:
        state = create_alter_system_state(moment)
    _materialize_legacy_pending_value(state)

    source_participant_id = _normalize_participant_id(participant_id)
    scope = _ensure_pending_scope(state, source_participant_id)
    magnitude = _finite_number(alter, 0)
    scope['alter_value'] = _clamp(
        _finite_number(scope.get('alter_value'), 0) + magnitude,
        -_ALERT_VALUE_LIMIT, _ALERT_VALUE_LIMIT,
    )
    state['alter_value'] = _total_pending_alter(state.get('pending_scopes'))
    direction = _sign(magnitude)

    offset_expired = False
    if state.get('emotional_offset') and direction:
        state['alter_weight'] = adjust_alter_weight(
            _finite_number(state.get('alter_weight'), 0),
            direction == _finite_number(state.get('last_trigger_direction'), 0),
            abs(magnitude),
            config,
        )
        if state['alter_weight'] < _cfg_number(config, 'min_weight'):
            state['emotional_offset'] = None
            state['alter_weight'] = 0
            offset_expired = True

    history = state.get('history') or []
    last_turn = 0
    if history and _is_record(history[-1]):
        last_turn = _finite_number(_pick(history[-1], 'turn'), 0)
    entry: AlterHistoryEntry = {
        'turn': last_turn + 1,
        'phase': phase,
        'alter': magnitude,
        'alter_value': state['alter_value'],
        'timestamp': iso(moment),
    }
    if source_participant_id:
        entry['participant_id'] = source_participant_id
    history.append(entry)
    state['history'] = history[-HISTORY_LIMIT:]
    state['last_updated_at'] = iso(moment)

    scope_history = alter_history_for_scope(state['history'], source_participant_id)
    threshold = calculate_alter_threshold(scope_history, config, moment)
    return {
        'state': state,
        'threshold': threshold,
        'offset_expired': offset_expired,
        'threshold_reached': abs(_finite_number(scope.get('alter_value'), 0)) >= threshold,
        'source_participant_id': source_participant_id,
        'trigger_value': scope.get('alter_value'),
    }


def complete_alter_analysis(
    state: AlterSystemState,
    description: str,
    threshold: float,
    now: Any,
    config: AlterSystemConfig,
    participant_id: str = '',
) -> AlterSystemState:
    """上游 `completeAlterAnalysis`：旁路分析成功后才兑现一次触发。

    来源桶清零（其它桶原样保留）、权重复位为 1、方向与偏移一并写下。
    """
    moment = _to_dt(now)
    source_participant_id = _normalize_participant_id(participant_id)
    scopes = _normalize_pending_scopes(
        _pick(state, 'pending_scopes', 'pendingScopes') if isinstance(state, dict) else None,
    )
    if not scopes:
        legacy_value = _finite_number(
            _pick(state, 'alter_value', 'alterValue') if isinstance(state, dict) else None,
            0,
        )
        if abs(legacy_value) > 0:
            scopes.append({'participant_id': '', 'alter_value': legacy_value})

    scope = _ensure_pending_scope({'pending_scopes': scopes}, source_participant_id)
    trigger_value = _finite_number(scope.get('alter_value'), 0)
    direction = _sign(trigger_value)
    scope['alter_value'] = 0
    # 上游 `scope.lastAnalysisAttemptAt = undefined`：等价于键消失。
    scope.pop('last_analysis_attempt_at', None)

    text = description if isinstance(description, str) else ''
    result: AlterSystemState = _canonical_state(state)  # type: ignore[assignment]
    result.update({
        'alter_value': _total_pending_alter(scopes),
        'pending_scopes': scopes,
        'alter_weight': 1,
        'last_trigger_direction': direction,  # type: ignore[typeddict-item]
        'emotional_offset': {
            'direction': 'serious' if direction > 0 else 'relaxed',
            'description': text.strip()[:800],
            'intensity': min(
                abs(trigger_value) / max(1, _finite_number(threshold, 0)),
                _cfg_number(config, 'max_intensity'),
            ),
            'generated_at': iso(moment),
        },
        'last_updated_at': iso(moment),
    })
    return result


def emotional_offset_for_prompt(
    state: Optional[AlterSystemState],
    config: AlterSystemConfig,
) -> Optional[EmotionalOffsetPrompt]:
    """上游 `emotionalOffsetForPrompt`：把当前偏移（带权重）交给 prompt；否则 ``None``。"""
    if not _pick(config, 'enabled') or not isinstance(state, dict) or not state.get('emotional_offset'):
        return None
    alter_weight = _finite_number(state.get('alter_weight'), 0)
    if alter_weight < _cfg_number(config, 'min_weight'):
        return None
    offset: EmotionalOffsetPrompt = dict(state['emotional_offset'])  # type: ignore[arg-type]
    offset['weight'] = alter_weight
    return offset


# ======================================================================================
# 冷却与作用域
# ======================================================================================


def alter_analysis_cooling_down(
    state: AlterSystemState,
    now: Any = None,
    cooldown_ms: int = DEFAULT_COOLDOWN_MS,
) -> bool:
    """上游 `alterAnalysisCoolingDown`：故事级分析尝试的冷却闸门。"""
    last_attempt = parse_dt(
        _pick(state, 'last_analysis_attempt_at', 'lastAnalysisAttemptAt') if isinstance(state, dict) else None,
    )
    if last_attempt is None:
        return False
    return dt_ms(_to_dt(now)) - dt_ms(last_attempt) < cooldown_ms


def alter_scope_cooling_down(
    state: AlterSystemState,
    participant_id: str = '',
    now: Any = None,
    cooldown_ms: int = DEFAULT_COOLDOWN_MS,
) -> bool:
    """上游 `alterScopeCoolingDown`：当前作用域 **有独立的重试闸门**。

    失败的关系本地分析不会顺带压住另一段互不相干的生活。
    """
    scopes = _pick(state, 'pending_scopes', 'pendingScopes') if isinstance(state, dict) else None
    scope = _find_pending_scope(scopes, _normalize_participant_id(participant_id))
    source = scope if scope is not None else (state if isinstance(state, dict) else {})
    last_attempt = parse_dt(_pick(source, 'last_analysis_attempt_at', 'lastAnalysisAttemptAt'))
    if last_attempt is None:
        return False
    return dt_ms(_to_dt(now)) - dt_ms(last_attempt) < cooldown_ms


def mark_alter_scope_analysis_attempt(
    state: AlterSystemState,
    participant_id: str = '',
    now: Any = None,
) -> AlterSystemState:
    """上游 `markAlterScopeAnalysisAttempt`：只给本次来源桶打上尝试时间戳。"""
    moment = _to_dt(now)
    scopes = _normalize_pending_scopes(
        _pick(state, 'pending_scopes', 'pendingScopes') if isinstance(state, dict) else None,
    )
    scope = _ensure_pending_scope({'pending_scopes': scopes}, _normalize_participant_id(participant_id))
    scope['last_analysis_attempt_at'] = iso(moment)
    result: AlterSystemState = _canonical_state(state)  # type: ignore[assignment]
    result['pending_scopes'] = scopes
    return result


def alter_scope_value(state: AlterSystemState, participant_id: str = '') -> float:
    """上游 `alterScopeValue`：某个来源桶的待处理位移（不存在即 0）。"""
    scope = _find_pending_scope(
        _pick(state, 'pending_scopes', 'pendingScopes') if isinstance(state, dict) else None,
        _normalize_participant_id(participant_id),
    )
    if scope is None:
        return 0
    return _finite_number(_pick(scope, 'alter_value', 'alterValue'), 0)


def alter_history_for_scope(
    history: list[AlterHistoryEntry],
    participant_id: str = '',
) -> list[AlterHistoryEntry]:
    """上游 `alterHistoryForScope`：只保留同一来源桶的历史证据。"""
    source_participant_id = _normalize_participant_id(participant_id)
    result: list[AlterHistoryEntry] = []
    for entry in history or []:
        if not _is_record(entry):
            continue
        if _normalize_participant_id(_pick(entry, 'participant_id', 'participantId') or '') == source_participant_id:
            result.append(entry)
    return result


# ======================================================================================
# 私有助手（上游文件末尾的未导出函数）
# ======================================================================================


def _normalize_pending_scopes(value: Any) -> list[AlterPendingScope]:
    """上游 `normalizePendingScopes`：按 ``participantId`` 合并分桶，最多 32 个。"""
    if not isinstance(value, list):
        return []
    by_participant: dict[str, AlterPendingScope] = {}
    for item in value:
        if not _is_record(item):
            continue
        participant_id = _normalize_participant_id(_pick(item, 'participant_id', 'participantId'))
        existing = by_participant.get(participant_id)
        alter_value = _clamp(
            _finite_number(_pick(item, 'alter_value', 'alterValue'), 0),
            -_ALERT_VALUE_LIMIT, _ALERT_VALUE_LIMIT,
        )
        scope: AlterPendingScope = {
            'participant_id': participant_id,
            'alter_value': _clamp(
                _finite_number(existing.get('alter_value') if existing else 0, 0) + alter_value,
                -_ALERT_VALUE_LIMIT, _ALERT_VALUE_LIMIT,
            ),
        }
        attempt = _normalized_iso(_pick(item, 'last_analysis_attempt_at', 'lastAnalysisAttemptAt'))
        if attempt:
            scope['last_analysis_attempt_at'] = attempt
        # JS `Map.set` 覆盖已有键时保持首次插入位置；Python dict 语义相同。
        by_participant[participant_id] = scope
    return list(by_participant.values())[:PENDING_SCOPE_LIMIT]


def _normalize_participant_id(value: Any) -> str:
    """上游 `normalizeParticipantId`：字符串则 trim + 截断 255，否则空串。"""
    return value.strip()[:255] if isinstance(value, str) else ''


def _find_pending_scope(
    scopes: Any,
    participant_id: str,
) -> Optional[AlterPendingScope]:
    """上游 `findPendingScope`。"""
    if not isinstance(scopes, list):
        return None
    for scope in scopes:
        if not _is_record(scope):
            continue
        if _pick(scope, 'participant_id', 'participantId') == participant_id:
            return scope  # type: ignore[return-value]
    return None


def _ensure_pending_scope(state: dict[str, Any], participant_id: str) -> AlterPendingScope:
    """上游 `ensurePendingScope`：取不到就地创建（会写回 ``state['pending_scopes']``）。"""
    scopes = state.get('pending_scopes')
    if not isinstance(scopes, list):
        scopes = []
        state['pending_scopes'] = scopes
    scope = _find_pending_scope(scopes, participant_id)
    if scope is None:
        scope = {'participant_id': participant_id, 'alter_value': 0}
        scopes.append(scope)
    return scope


def _total_pending_alter(scopes: Any) -> float:
    """上游 `totalPendingAlter`：所有桶之和，夹取 ±1000。"""
    total = 0
    for scope in scopes or []:
        if _is_record(scope):
            total += _finite_number(_pick(scope, 'alter_value', 'alterValue'), 0)
    return _clamp(total, -_ALERT_VALUE_LIMIT, _ALERT_VALUE_LIMIT)


def _materialize_legacy_pending_value(state: AlterSystemState) -> None:
    """上游 `materializeLegacyPendingValue`：把遗留的裸 ``alterValue`` 落进空桶。"""
    if state.get('pending_scopes'):
        return
    alter_value = _finite_number(_pick(state, 'alter_value', 'alterValue'), 0)
    if abs(alter_value) > 0:
        state['pending_scopes'] = [{'participant_id': '', 'alter_value': alter_value}]
