"""上游 `src/script/context-compiler.ts` 的 Python 对应物：七段式上下文编译。

M4 桥：把 beta10 那些被验证过的字段编译成**正向续写脚手架**。每个已备好的值
只被搬进一个语义分组，绝不在面向模型的载荷里重算或重复。

移植自 Koishi / TypeScript 上游快照 `upstream/src/script/context-compiler.ts`（127 行）。

七段顺序（一字不差，`Object.keys` 顺序即序列化顺序）：

1. `storyIdentity`                —— setting
2. `relevantEstablishedEpisodes`  —— 更早但已确立的剧集证据
3. `currentSceneEvidence`         —— 当前场景的有源导航
4. `ongoingThreads`               —— 仍在进行中的线索
5. `availableNearFuture`          —— 有界的近期未来
6. `incomingEvent`                —— 触发本回合的入站事件
7. `authoringWindow`              —— 本回合的写作窗口与能力开关

键名（`docs/PORT_PLAN.md` §2「⚠️ 键名法」）——本模块**输入输出全是 wire format**
--------------------------------------------------------------------------------
`compile_narrative_context` 的产物由 `narrator_prompts.to_prompt_payload` 直接
`JSON.stringify` 进发给模型的 payload；上游 `systemPrompt` 里的 FIELD MAP 明文按
`recentScript and recalledScript are inside relevantEstablishedEpisodes`、
`currentEvent means incomingEvent.event`、`interval means authoringWindow.interval`
指路。因此：

- **七个分区名与分区内所有键一律上游 camelCase**（`storyIdentity` /
  `relevantEstablishedEpisodes` / `currentSceneEvidence` / `ongoingThreads` /
  `availableNearFuture` / `incomingEvent` / `authoringWindow`，以及
  `recentScript` / `continuitySnapshotAgeMinutes` / `recalledScript` /
  `currentParticipant` / `followUpCommitments` / `workingDetails` /
  `interruptedOutgoingDrafts` / `recentExchange` / `timelinePlan` / `timelineCarry` /
  `schedulePreplan` / `dueIntents` / `upcomingPlans` / `groupContext` /
  `chatCapabilities` / `stickerCatalog` / `liveTimeBoundary` /
  `refreshContinuity` / `outputRecovery` / `emotionalOffset` / `agencyWindow`）。
  改成 snake_case 就等于把模型读到的结构与提示词里写的名字错开。
- **入参 payload 用的是上游 `toPromptPayload` 的那套扁平键名**（`recentScript` /
  `recalledHistory` / `currentEvent` …）。读取一律走
  `_field(payload, camel, snake)`：优先上游 camelCase，同时容忍 snake_case，
  这样旧调用点与单元测试两种拼写都能命中（§2「从外部读入的不可信输入」）。
  注意 `recalledHistory`（payload 名）→ `recalledScript`（wire 名）是上游本身
  就存在的改名，不是本移植版的发明。
- **唯一的内部结构是 `SceneFrame`**：它由 `scene_frame.py` 物化、键名按仓库约定是
  snake_case（`types.py` 的领域对象）。`_sourced()` 因此对每个字段同时接受
  `presentPeople` 与 `present_people`（优先上游 camelCase），但**写出时一律用
  上游 wire 名**（`presentPeople` / `openLoops` / `sourceEntryIds` / `sceneId`）。
- 冲突诊断里的标签（`recentScript was recomputed` 等）保留上游 camelCase 字面量，
  便于与上游日志逐字比对。

`compile_narrative_context` 的第三个参数上游命名为 `_burst`：DialogueBurst 是宿主
拥有的提交身份，**刻意不序列化**进模型载荷，因此该参数在本函数里不被读取。

`undefined` 的两种写法（务必区分，决定键**是否存在**于 wire 上）
------------------------------------------------------------------
上游 `compactObject` 只丢 `undefined`，**保留显式 `null`**。本移植版因此有两种
「无值」：

- `None`   ≡ 上游 `null`      —— **保留**（例如 `continuitySnapshot: null`、
  `currentParticipant: null`、`emotionalOffset: null`）；
- `undefined` —— **丢掉**。它有两种载体：本模块内部的哨兵 `_OMIT`（键在调用方
  字典里**不存在**时用它表示「上游读到的是 undefined」），以及调用方
  `narrator_prompts` 为了把 `null` 与 `undefined` 分开而设的哨兵 `_UNDEFINED`
  （它会直接出现在扁平 payload 的值里）。

`narrator_prompts` 是本模块的调用方（`to_prompt_payload` → `compile_narrative_context`），
所以这里不能 import 它（会成环）；`_is_undefined()` 按哨兵的公开形状识别它。
"""

from __future__ import annotations

import re
from typing import Any, Required, TypedDict

from ..types import DialogueBurstState, SceneFrame


class CompiledNarrativeContext(TypedDict, total=False):
    """七段式编译结果（上游 `interface CompiledNarrativeContext`）。

    这是**模型可见的 wire 结构**，故字段名逐字保持上游 camelCase；每个分区都是
    已经过 `compactObject` 去 `undefined` 的字典。
    """

    storyIdentity: Required[dict[str, Any]]
    relevantEstablishedEpisodes: Required[dict[str, Any]]
    currentSceneEvidence: Required[dict[str, Any]]
    ongoingThreads: Required[dict[str, Any]]
    availableNearFuture: Required[dict[str, Any]]
    incomingEvent: Required[dict[str, Any]]
    authoringWindow: Required[dict[str, Any]]


#: 本模块内部表达上游 `undefined` 的哨兵：**键不存在**时用它，`compactObject` 丢掉它。
_OMIT = object()


def _field(mapping: Any, *keys: str) -> Any:
    """JS 属性读取（`mapping[key]` / 可选链）：按顺序取第一个**存在**的键。

    键完全不存在 → `_OMIT`（上游 `undefined`，会被丢掉）；
    键存在但值为 `None` → 原样返回（上游显式 `null`，会被保留）。
    多种拼写都认：优先上游 camelCase，其次 snake_case / 上游 payload 名。
    """
    if isinstance(mapping, dict):
        for key in keys:
            if key in mapping:
                return mapping[key]
    return _OMIT


def _is_undefined(value: Any) -> bool:
    """本模块要丢掉的 `undefined`：内部哨兵 `_OMIT`，或 `narrator_prompts._UNDEFINED`。

    `repr` 为 `undefined`、真值为假的无槽位单例即那个哨兵。
    """
    if value is _OMIT:
        return True
    return type(value).__name__ == '_UndefinedType' and repr(value) == 'undefined'


def _compact_object(value: dict[str, Any]) -> dict[str, Any]:
    """等价上游 `compactObject`：丢掉值为 `undefined` 的键（保序、保留 `null`）。"""
    return {key: item for key, item in value.items() if not _is_undefined(item)}


def _snake_key(key: str) -> str:
    """`presentPeople` → `present_people`（内部 `SceneFrame` 的字段名拼写）。"""
    return re.sub(r'([a-z0-9])([A-Z])', lambda match: f'{match.group(1)}_{match.group(2).lower()}', key)


def _as_list(value: Any) -> list[Any]:
    """上游 `x ?? []` 之后还要 `.map()`：非列表一律当空列表。"""
    return value if isinstance(value, list) else []


def compile_narrative_context(
    payload: dict[str, Any],
    frame: SceneFrame | None,
    _burst: DialogueBurstState | None,
) -> CompiledNarrativeContext:
    """把一个扁平 payload 编译成七段式脚手架（顺序与字段照抄上游）。"""
    return {
        # 上游这里没有 compactObject，靠 `JSON.stringify` 丢掉 undefined 的键；
        # Python 侧必须显式过滤，否则内部哨兵会漏进 json.dumps。
        'storyIdentity': _compact_object({
            'setting': _field(payload, 'setting'),
        }),
        'relevantEstablishedEpisodes': _compact_object({
            'recentScript': _field(payload, 'recentScript', 'recent_script'),
            'sceneContext': _field(payload, 'sceneContext', 'scene_context'),
            'continuitySnapshot': _field(payload, 'continuitySnapshot', 'continuity_snapshot'),
            'continuitySnapshotAgeMinutes': _field(
                payload, 'continuitySnapshotAgeMinutes', 'continuity_snapshot_age_minutes'),
            'durableFacts': _field(payload, 'durableFacts', 'durable_facts'),
            'memories': _field(payload, 'memories'),
            'overlayEvolution': _field(payload, 'overlayEvolution', 'overlay_evolution'),
            # M4.1：一次召回命中指回原始剧本邻域。面向模型的名字要让人看清
            # 这是散文证据，而不是第二份抽象记忆摘要。上游扁平 payload 里的键叫
            # `recalledHistory`，写出的 wire 名是 `recalledScript`（上游源码如此）。
            'recalledScript': _field(payload, 'recalledScript', 'recalledHistory', 'recalled_history'),
            'webContext': _field(payload, 'webContext', 'web_context'),
            'recentExchange': _field(payload, 'recentExchange', 'recent_exchange'),
        }),
        'currentSceneEvidence': _project_scene_evidence(
            frame,
            {
                entry.get('id') for entry in _as_list(_field(payload, 'recentScript', 'recent_script'))
                if isinstance(entry, dict) and _is_safe_integer(entry.get('id'))
            },
        ),
        'ongoingThreads': _compact_object({
            'state': _field(payload, 'state'),
            'currentParticipant': _field(payload, 'currentParticipant', 'current_participant'),
            'participants': _field(payload, 'participants'),
            'activeConsequences': _field(payload, 'activeConsequences', 'active_consequences'),
            'followUpCommitments': _field(payload, 'followUpCommitments', 'follow_up_commitments'),
            'contactThreads': _field(payload, 'contactThreads', 'contact_threads'),
            'workingDetails': _field(payload, 'workingDetails', 'working_details'),
            'interruptedOutgoingDrafts': _field(
                payload, 'interruptedOutgoingDrafts', 'interrupted_outgoing_drafts'),
            'supersededDelayedReplies': _field(
                payload, 'supersededDelayedReplies', 'superseded_delayed_replies'),
            'automaticDeliverySummaries': _field(
                payload, 'automaticDeliverySummaries', 'automatic_delivery_summaries'),
            'deliveryReality': _field(payload, 'deliveryReality', 'delivery_reality'),
            'developmentTendencies': _field(payload, 'developmentTendencies', 'development_tendencies'),
        }),
        'availableNearFuture': _compact_object({
            'timelinePlan': _field(payload, 'timelinePlan', 'timeline_plan'),
            'timelineCarry': _field(payload, 'timelineCarry', 'timeline_carry'),
            'schedulePreplan': _field(payload, 'schedulePreplan', 'schedule_preplan'),
            'dueIntents': _field(payload, 'dueIntents', 'due_intents'),
            'upcomingPlans': _field(payload, 'upcomingPlans', 'upcoming_plans'),
        }),
        'incomingEvent': _compact_object({
            'event': _field(payload, 'currentEvent', 'current_event'),
            'groupContext': _field(payload, 'groupContext', 'group_context'),
            'chatCapabilities': _field(payload, 'chatCapabilities', 'chat_capabilities'),
            'stickerCatalog': _field(payload, 'stickerCatalog', 'sticker_catalog'),
        }),
        'authoringWindow': _compact_object({
            'phase': _field(payload, 'phase'),
            'interval': _field(payload, 'interval'),
            'continuation': _field(payload, 'continuation'),
            'liveTimeBoundary': _field(payload, 'liveTimeBoundary', 'live_time_boundary'),
            'refreshContinuity': _field(payload, 'refreshContinuity', 'refresh_continuity'),
            'outputRecovery': _field(payload, 'outputRecovery', 'output_recovery'),
            'emotionalOffset': _field(payload, 'emotionalOffset', 'emotional_offset'),
            'agencyWindow': _field(payload, 'agencyWindow', 'agency_window'),
        }),
    }


def compiled_context_conflicts(
    payload: dict[str, Any],
    compiled: CompiledNarrativeContext,
) -> list[str]:
    """校验编译结果**复用**了扁平 payload 的同一份引用，而不是重算了一遍。

    上游用 `legacy !== next` 做引用比较，Python 侧对应 `is not`。
    诊断标签沿用上游字面量。

    两侧都用 `_OMIT` 兜底「键不存在」：上游此时两边都是 `undefined`，
    `undefined !== undefined` 为假，不算冲突。
    """
    pairs: list[tuple[Any, Any, str]] = [
        (_field(payload, 'setting'), (compiled.get('storyIdentity') or {}).get('setting', _OMIT), 'setting'),
        (_field(payload, 'state'), (compiled.get('ongoingThreads') or {}).get('state', _OMIT), 'state'),
        (_field(payload, 'recentScript', 'recent_script'),
         (compiled.get('relevantEstablishedEpisodes') or {}).get('recentScript', _OMIT), 'recentScript'),
        (_field(payload, 'currentEvent', 'current_event'),
         (compiled.get('incomingEvent') or {}).get('event', _OMIT), 'currentEvent'),
        (_field(payload, 'interval'), (compiled.get('authoringWindow') or {}).get('interval', _OMIT), 'interval'),
        (_field(payload, 'timelinePlan', 'timeline_plan'),
         (compiled.get('availableNearFuture') or {}).get('timelinePlan', _OMIT), 'timelinePlan'),
    ]
    return [f'{label} was recomputed' for legacy, nxt, label in pairs if legacy is not nxt]


def _project_scene_evidence(frame: SceneFrame | None, recent_entry_ids: set[int]) -> dict[str, Any]:
    """场景状态是一份很小的、带来源的导航辅助。

    DialogueBurst 是宿主拥有的提交身份，形如散文的字段刻意不被序列化。
    入参 `frame` 是内部 snake_case 领域对象，返回的是**模型可见的 wire 结构**。
    """
    if not frame:
        return {}
    outside_source_entry_ids = [
        entry_id for entry_id in _as_list(_field(frame, 'sourceEntryIds', 'source_entry_ids'))
        if entry_id not in recent_entry_ids
    ]
    return _compact_object({
        'sceneId': _field(frame, 'sceneId', 'scene_id'),
        'place': _sourced(frame, 'place', recent_entry_ids),
        'presentPeople': _sourced(frame, 'presentPeople', recent_entry_ids),
        'ongoingActivity': _sourced(frame, 'ongoingActivity', recent_entry_ids),
        'attention': _sourced(frame, 'attention', recent_entry_ids),
        'deviceAccess': _sourced(frame, 'deviceAccess', recent_entry_ids),
        'privacy': _sourced(frame, 'privacy', recent_entry_ids),
        'openLoops': _sourced(frame, 'openMotions', recent_entry_ids),
        'sourceEntryIds': outside_source_entry_ids if outside_source_entry_ids else _OMIT,
    })


def _sourced(frame: SceneFrame, field: str, recent_entry_ids: set[int]) -> Any:
    """只有「已填充、有来源、且来源不在本回合 recentScript 里」的字段才被注入。

    `field` 是上游 camelCase 字段名（如 `presentPeople` / `openMotions`）；内部
    `SceneFrame` 的键是 snake_case，两种拼写都读，写出用 wire 名 `sourceEntryIds`。
    不注入时返回 `_OMIT`（上游 `undefined` → `compactObject` 丢掉）。
    """
    value = _field(frame, field, _snake_key(field))
    sources = _field(frame, 'sources')
    source_entry_ids = _field(sources, field, _snake_key(field))
    populated = len(value) > 0 if isinstance(value, list) else (
        isinstance(value, str) and bool(value.strip())
    )
    if populated and isinstance(source_entry_ids, list) and source_entry_ids and any(
        entry_id not in recent_entry_ids for entry_id in source_entry_ids
    ):
        return {'value': value, 'sourceEntryIds': source_entry_ids}
    return _OMIT


def _is_safe_integer(value: Any) -> bool:
    """等价 JS `Number.isSafeInteger`（`bool` 是 `int` 子类，须排除）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return abs(value) <= 9_007_199_254_740_991
