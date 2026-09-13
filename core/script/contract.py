"""上游 `src/script/contract.ts` 的 Python 对应物：剧本提交契约。

这是 beta10 → V2 的**结构契约**：一次叙事回合被固化为「一份 commit + 若干 event」，
每个事件都带稳定的、由宿主拥有的 id。契约本身不做模型调用，也不发明叙事决策。

移植自 Koishi / TypeScript 上游快照 `upstream/src/script/contract.ts`（92 行）。
命名按 `docs/PORT_PLAN.md` §2：`camelCase` → `snake_case`；
`ScriptEventKind` / `ScriptDeliveryMode` / `ScriptActionBinding` / `ScriptEventDraft` /
`ScriptCommitDraft` / `ScriptMessageEventReference` 六个类型名保持上游原名。

两处语言映射细节：
- 上游 `ScriptCommitDraft.window` 的内联对象含字段 `from`（Python 保留字），
  故 `ScriptCommitWindow` 用**函数式 TypedDict 语法**，运行期键名原样保留为 `'from'`
  （与 `plugin/core/types.py` 中的同类处理一致）。
- 上游 `ScriptActionBinding.spans` 的内联对象被抽成 `ScriptActionSpan`。
"""

from __future__ import annotations

from typing import Any, Literal, Required, TypedDict

from ..types import NarrativePhase

ScriptEventKind = Literal[
    'narrative',
    'message-perceived',
    'outgoing-message',
    'group-message',
    'future-intent',
    'browser-intent',
    'follow-up-promise',
    'platform-action',
]

ScriptDeliveryMode = Literal['immediate', 'delayed']

ScriptActionBindingStatus = Literal['bound', 'unbound', 'future']


class ScriptActionSpan(TypedDict, total=False):
    """`ScriptActionBinding.spans` 的元素形状（上游为内联对象字面量）。"""

    bubble_index: Required[int]
    start: Required[int]
    end: Required[int]


class ScriptActionBinding(TypedDict, total=False):
    """即时动作绑定到权威散文里的字面消息片段；延迟动作在当前窗口内尚未发生。"""

    status: Required[ScriptActionBindingStatus]
    spans: list[ScriptActionSpan]


ScriptCommitWindow = TypedDict('ScriptCommitWindow', {
    'from': 'Required[str]',
    'to': 'Required[str]',
}, total=False)


class ScriptEventDraft(TypedDict, total=False):
    """一条待写入的剧本事件。"""

    event_id: Required[str]
    commit_id: Required[str]
    kind: Required[ScriptEventKind]
    actor: Required[Literal['protagonist', 'system']]
    occurred_at: Required[str]
    caused_by_event_ids: Required[list[str]]
    participant_id: str
    content: str
    bubbles: list[str]
    delivery_mode: ScriptDeliveryMode
    script_binding: ScriptActionBinding
    metadata: dict[str, Any]


class ScriptSceneDelta(TypedDict, total=False):
    """`ScriptCommitDraft.sceneDelta` 的内联对象形状。

    M4 append 身份：消息与散文始终是同一个场景增量。
    """

    frame_id: Required[str]
    burst_id: Required[str]
    starts_after_event_id: str
    prose_append: Required[str]
    event_ids: Required[list[str]]


class ScriptCommitDraft(TypedDict, total=False):
    """一次回合固化成的那一份宿主拥有的提交。"""

    commit_id: Required[str]
    story_id: Required[str]
    participant_id: Required[str]
    phase: Required[NarrativePhase]
    window: Required[ScriptCommitWindow]
    prose: Required[str]
    events: Required[list[ScriptEventDraft]]
    scene_delta: Required[ScriptSceneDelta]
    source_format: Required[Literal['beta10', 'script-first-v1']]


class ScriptMessageEventReference(TypedDict, total=False):
    """对一条出站消息事件的稳定回链。"""

    commit_id: Required[str]
    event_id: Required[str]
    # 含权威提交与投递账本的来源剧本行。
    script_entry_id: int
    event_kind: Required[Literal['outgoing-message', 'group-message']]
    caused_by_event_ids: Required[list[str]]
    full_content: Required[str]
    bubble_index: Required[int]
    bubble_count: Required[int]


def _is_safe_integer(value: Any) -> bool:
    """等价 JS `Number.isSafeInteger`（`bool` 是 `int` 子类，须排除）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return abs(value) <= 9_007_199_254_740_991


def is_outgoing_script_event(event: ScriptEventDraft) -> bool:
    """等价上游类型守卫 `isOutgoingScriptEvent`。

    上游用 `event is ScriptEventDraft & { ... }` 收窄类型；Python 没有等价的
    运行期收窄，因此这里只保留**判断语义**，调用方在真分支里按出站事件读取字段。
    """
    return (
        event.get('kind') in ('outgoing-message', 'group-message')
        and isinstance(event.get('content'), str)
        and isinstance(event.get('bubbles'), list)
        and event.get('delivery_mode') in ('immediate', 'delayed')
    )


def message_event_reference(
    event: ScriptEventDraft,
    bubble_index: int = 0,
    script_entry_id: int | None = None,
) -> ScriptMessageEventReference | None:
    """把一条出站剧本事件折成投递层的稳定引用；非出站事件返回 `None`。

    键顺序照抄上游展开运算符：`commitId, eventId, [scriptEntryId], eventKind,
    causedByEventIds, fullContent, bubbleIndex, bubbleCount`。
    """
    if not is_outgoing_script_event(event):
        return None
    reference: dict[str, Any] = {
        'commit_id': event['commit_id'],
        'event_id': event['event_id'],
    }
    if _is_safe_integer(script_entry_id):
        reference['script_entry_id'] = script_entry_id
    reference.update({
        'event_kind': event['kind'],
        'caused_by_event_ids': list(event['caused_by_event_ids']),
        'full_content': event['content'],
        'bubble_index': bubble_index,
        'bubble_count': len(event['bubbles']),
    })
    return reference
