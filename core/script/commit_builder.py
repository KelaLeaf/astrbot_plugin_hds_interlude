"""上游 `src/script/commit-builder.ts` 的 Python 对应物：beta10 → V2 的唯一桥。

它不调用模型、不发明叙事决策；它只给既有的单次回复补上稳定的 commit / event id，
并把「宿主已经决定要做的事」摊平成一条条可审计、可回链的剧本事件。

移植自 Koishi / TypeScript 上游快照 `upstream/src/script/commit-builder.ts`（248 行）。

关键对应：
- `decisionToScriptCommit` → `decision_to_script_commit`（上游测试里的
  `as legacyDecisionToScriptCommit` 只是调用点别名，本模块不额外导出第二个名字）。
- `findOutgoingScriptEvent` → `find_outgoing_script_event`
- `findGroupScriptEvent` → `find_group_script_event`
- `bindImmediateMessageAction` → `bind_immediate_message_action`
- `unboundImmediateMessageEvents` → `unbound_immediate_message_events`
- 私有辅助 `addMessageEvent` / `canonicalBubbleContent` / `splitBubbles` / `stableCommitId`
  → `_add_message_event` / `_canonical_bubble_content` / `_split_bubbles` / `_stable_commit_id`。

JS 默认参数只在实参为 `undefined` 时生效，而 Python 的 `None` 同时承担「未传」与
「显式空值」。本模块用 `_js_default()` 精确复刻该语义，避免 `splitReplyMessages=False`
被误当成「未传」而回落为 `True`。

`stableCommitId` 的哈希前像是**上游 camelCase 的 JSON 逐字节复刻**（紧凑分隔符、
不转义非 ASCII），这样同一份语义输入在两端得到同一个 commit id；字段名不参与
`PORT_PLAN.md` §2 的 snake_case 改名，因为是哈希前像而非数据协议。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Required, TypedDict

from ..time import iso
from ..types import NarrativeDecision, NarrativePhase
from .contract import (
    ScriptActionBinding,
    ScriptActionSpan,
    ScriptCommitDraft,
    ScriptDeliveryMode,
    ScriptEventDraft,
    is_outgoing_script_event,
)

try:  # 上游 `./authored-actions`；由并行的 `core/script/authored_actions.py` 移植任务落地。
    from .authored_actions import AuthoredAction, resolve_authored_actions
except ImportError:  # pragma: no cover - 仅在并行模块尚未落地时生效
    AuthoredAction = Any  # type: ignore[assignment,misc]

    def resolve_authored_actions(
        decision: NarrativeDecision,
        already_sent: bool = False,
        separator: str = '<sep/>',
    ) -> NarrativeDecision:
        """降级实现：与上游「整份剧本里没有任何 `<say>` 标签」时的行为一致。

        它**不是** `authored-actions.ts` 的移植（那份工作归属并行任务），
        只保证 `authored_actions.py` 落地前本模块仍可导入、可运行。
        """
        script = decision.get('script') if isinstance(decision, dict) else None
        if not isinstance(script, str):
            return decision
        return {**decision, 'script': script.strip(), 'authored_actions': []}


_DEFAULT_SEPARATOR = '<sep/>'


ScriptFirstDecisionInput = TypedDict('ScriptFirstDecisionInput', {
    'story_id': 'Required[str]',
    'participant_id': 'str',
    'phase': 'Required[NarrativePhase]',
    'from': 'Required[datetime]',
    'now': 'Required[datetime]',
    'decision': 'Required[NarrativeDecision]',
    'message_separator': 'str',
    'split_reply_messages': 'bool',
    # 群输出在进入本适配器之前已经由 service 归一化。
    'group_reply_content': 'str',
    'frame_id': 'str',
    'burst_id': 'str',
    'starts_after_event_id': 'str',
}, total=False)


def _js_default(value: Any, default: Any) -> Any:
    """复刻 JS 默认参数：`undefined` → 默认值。Python 里 `None` 即 `undefined`。"""
    return default if value is None else value


def _js_truthy(value: Any) -> bool:
    """JS 真值语义：`''` / `0` / `None` / 空容器均为假。"""
    if value is None or value is False:
        return False
    if isinstance(value, (str, list, tuple, dict, set)):
        return len(value) > 0
    if isinstance(value, (int, float)):
        return value != 0
    return True


def decision_to_script_commit(input: ScriptFirstDecisionInput) -> ScriptCommitDraft:
    """把一次叙事决策固化成一份宿主拥有的提交（唯一 beta10 → V2 桥）。"""
    decision = resolve_authored_actions(
        input['decision'],
        False,
        _js_default(input.get('message_separator'), _DEFAULT_SEPARATOR),
    )
    input = {**input, 'decision': decision}

    raw_participant = input.get('participant_id')
    participant_id = raw_participant.strip() if isinstance(raw_participant, str) else ''
    script = input['decision'].get('script')
    prose = script.strip() if isinstance(script, str) else ''
    commit_id = _stable_commit_id(input, participant_id, prose)
    events: list[ScriptEventDraft] = []

    def add(event: dict[str, Any]) -> ScriptEventDraft:
        result: ScriptEventDraft = {
            **event,
            'commit_id': commit_id,
            'event_id': f'{commit_id}:e{len(events) + 1}',
        }
        events.append(result)
        return result

    now_iso = iso(input['now'])
    interaction = input['decision'].get('interaction')
    perceived: ScriptEventDraft | None = None
    if input['phase'] == 'user-message' and (interaction or {}).get('seen') and participant_id:
        perceived = add({
            'kind': 'message-perceived', 'actor': 'protagonist', 'occurred_at': now_iso,
            'caused_by_event_ids': [], 'participant_id': participant_id,
        })
    narrative = add({
        'kind': 'narrative', 'actor': 'protagonist', 'occurred_at': now_iso,
        'caused_by_event_ids': [perceived['event_id']] if perceived else [], 'content': prose,
    })
    reply = (interaction or {}).get('reply') or {}
    if _js_truthy(reply.get('content')) and reply.get('mode') in ('immediate', 'delayed'):
        _add_message_event(add, {
            'participant_id': participant_id,
            'content': reply['content'],
            'mode': reply['mode'],
            'occurred_at': reply['send_at'] if reply['mode'] == 'delayed' and _js_truthy(reply.get('send_at')) else now_iso,
            'caused_by_event_ids': [perceived['event_id'] if perceived else narrative['event_id']],
            'separator': input.get('message_separator'),
            'split': input.get('split_reply_messages'),
            'prose': prose,
            'actions': input['decision'].get('authored_actions'),
        })
    for action in input['decision'].get('cross_conversation_actions') or []:
        if not _js_truthy(action.get('content')) or action.get('mode') not in ('immediate', 'delayed'):
            continue
        _add_message_event(add, {
            'participant_id': action['participant_id'],
            'content': action['content'],
            'mode': action['mode'],
            'occurred_at': action['send_at'] if action['mode'] == 'delayed' and _js_truthy(action.get('send_at')) else now_iso,
            'caused_by_event_ids': [narrative['event_id']],
            'separator': input.get('message_separator'),
            'split': input.get('split_reply_messages'),
            'prose': prose,
            'actions': input['decision'].get('authored_actions'),
        })
    if _js_truthy(input.get('group_reply_content')):
        bubbles = _split_bubbles(
            input['group_reply_content'], input.get('message_separator'), input.get('split_reply_messages'),
        )
        content = _canonical_bubble_content(
            input['group_reply_content'], bubbles, input.get('message_separator'), input.get('split_reply_messages'),
        )
        add({
            'kind': 'group-message', 'actor': 'protagonist', 'occurred_at': now_iso,
            'caused_by_event_ids': [narrative['event_id']], 'content': content,
            'bubbles': bubbles, 'delivery_mode': 'immediate',
            'script_binding': bind_immediate_message_action(
                prose, bubbles, input['decision'].get('authored_actions') or [],
            ),
        })
    if _js_truthy(input['decision'].get('follow_up_commitment')):
        commitment = input['decision']['follow_up_commitment']
        add({
            'kind': 'follow-up-promise', 'actor': 'protagonist', 'occurred_at': now_iso,
            'caused_by_event_ids': [narrative['event_id']], 'participant_id': participant_id,
            'content': commitment['summary'],
            'metadata': {'notBefore': commitment.get('not_before'), 'kind': commitment.get('kind')},
        })
    for intent in input['decision'].get('intents') or []:
        add({
            'kind': 'future-intent', 'actor': 'protagonist', 'occurred_at': now_iso,
            'caused_by_event_ids': [narrative['event_id']],
            'participant_id': intent.get('participant_id') or participant_id,
            'content': intent['summary'], 'metadata': {'type': intent.get('type'), 'notBefore': intent.get('not_before')},
        })
    for browser in input['decision'].get('browser_intents') or []:
        add({
            'kind': 'browser-intent', 'actor': 'protagonist', 'occurred_at': now_iso,
            'caused_by_event_ids': [narrative['event_id']],
            'participant_id': browser.get('participant_id') or participant_id,
            'content': browser['purpose'],
            'metadata': {'mode': browser.get('mode'), 'timing': _js_default(browser.get('timing'), 'deferred')},
        })
    local_media = input['decision'].get('local_media')
    native_face = input['decision'].get('native_face')
    message_reactions = input['decision'].get('message_reactions') or []
    if _js_truthy(local_media) or _js_truthy(native_face) or len(message_reactions):
        metadata: dict[str, Any] = {}
        if _js_truthy(local_media):
            metadata['localMedia'] = local_media
        if _js_truthy(native_face):
            metadata['nativeFace'] = native_face
        if len(message_reactions):
            metadata['messageReactions'] = message_reactions
        add({
            'kind': 'platform-action', 'actor': 'protagonist', 'occurred_at': now_iso,
            'caused_by_event_ids': [narrative['event_id']], 'participant_id': participant_id,
            'metadata': metadata,
        })

    scene_delta: dict[str, Any] = {
        'frame_id': _js_default(input.get('frame_id'), ''),
        'burst_id': _js_default(input.get('burst_id'), ''),
    }
    if _js_truthy(input.get('starts_after_event_id')):
        scene_delta['starts_after_event_id'] = input['starts_after_event_id']
    scene_delta['prose_append'] = prose
    scene_delta['event_ids'] = [event['event_id'] for event in events]

    return {
        'commit_id': commit_id, 'story_id': input['story_id'], 'participant_id': participant_id,
        'phase': input['phase'],
        'window': {'from': iso(input['from']), 'to': now_iso},
        'prose': prose, 'events': events,
        'scene_delta': scene_delta,
        'source_format': 'script-first-v1',
    }


def find_outgoing_script_event(
    commit: ScriptCommitDraft,
    participant_id: str,
    mode: ScriptDeliveryMode | None = None,
    content: str | None = None,
    separator: str = _DEFAULT_SEPARATOR,
) -> ScriptEventDraft | None:
    """在提交里定位那条唯一的出站私聊事件（`Array#find` 语义：取第一条匹配）。"""
    separator = _js_default(separator, _DEFAULT_SEPARATOR)
    for event in commit['events']:
        if not is_outgoing_script_event(event):
            continue
        if event['kind'] != 'outgoing-message':
            continue
        if event.get('participant_id') != participant_id:
            continue
        if mode and event.get('delivery_mode') != mode:
            continue
        if content is not None:
            if event.get('content') == content:
                return event
            if separator.join(event['bubbles']) == separator.join(_split_bubbles(content, separator, True)):
                return event
            continue
        return event
    return None


def find_group_script_event(commit: ScriptCommitDraft) -> ScriptEventDraft | None:
    """在提交里定位那条群消息事件。"""
    for event in commit['events']:
        if is_outgoing_script_event(event) and event['kind'] == 'group-message':
            return event
    return None


def bind_immediate_message_action(
    prose: str,
    bubbles: list[str],
    actions: list[AuthoredAction] | None = None,
) -> ScriptActionBinding:
    """把一条即时传输动作绑定到当前剧本里**唯一**的字面消息文本上，
    不解释散文，也不新增模型任务。歧义或缺失的文本只作诊断，绝不改写剧本。"""
    actions = actions if actions is not None else []
    matching = [
        action for action in actions
        if prose[action['start']:action['end']] == action['content']
        and all(bubble.strip() in action['content'] for bubble in bubbles)
    ]
    if len(matching) == 1:
        action = matching[0]
        binding = bind_immediate_message_action(action['content'], bubbles)
        if binding['status'] == 'bound':
            spans: list[ScriptActionSpan] = [
                {
                    **span,
                    'start': span['start'] + action['start'],
                    'end': span['end'] + action['start'],
                }
                for span in binding['spans']
            ]
            return {**binding, 'spans': spans}

    spans = []
    cursor = 0
    for bubble_index, raw_bubble in enumerate(bubbles):
        bubble = raw_bubble.strip()
        if not bubble:
            return {'status': 'unbound'}
        start = prose.find(bubble, cursor)
        if start < 0 or prose.find(bubble, start + 1) >= 0:
            return {'status': 'unbound'}
        end = start + len(bubble)
        spans.append({'bubble_index': bubble_index, 'start': start, 'end': end})
        cursor = end
    return {'status': 'bound', 'spans': spans}


def unbound_immediate_message_events(commit: ScriptCommitDraft) -> list[ScriptEventDraft]:
    """所有「声称即时、却没能绑定到字面消息」的出站事件（纯诊断）。"""
    return [
        event for event in commit['events']
        if is_outgoing_script_event(event)
        and event.get('delivery_mode') == 'immediate'
        and (event.get('script_binding') or {}).get('status') != 'bound'
    ]


def _add_message_event(
    add: Any,
    input: dict[str, Any],
) -> ScriptEventDraft:
    """上游私有 `addMessageEvent`：算好气泡与绑定后塞进事件列表。"""
    separator = _js_default(input.get('separator'), _DEFAULT_SEPARATOR)
    split = _js_default(input.get('split'), True)
    bubbles = _split_bubbles(input['content'], separator, split)
    return add({
        'kind': 'outgoing-message', 'actor': 'protagonist', 'occurred_at': input['occurred_at'],
        'caused_by_event_ids': input['caused_by_event_ids'], 'participant_id': input['participant_id'],
        'content': _canonical_bubble_content(input['content'], bubbles, separator, split),
        'bubbles': bubbles,
        'delivery_mode': input['mode'],
        'script_binding': {'status': 'future'} if input['mode'] == 'delayed'
        else bind_immediate_message_action(input['prose'], bubbles, input.get('actions') or []),
    })


def _canonical_bubble_content(
    content: str,
    bubbles: list[str],
    separator: str | None = _DEFAULT_SEPARATOR,
    enabled: bool | None = True,
) -> str:
    """上游私有 `canonicalBubbleContent`：开启分条时，内容以分隔符重排为准。"""
    enabled = _js_default(enabled, True)
    separator = _js_default(separator, _DEFAULT_SEPARATOR)
    return (separator or _DEFAULT_SEPARATOR).join(bubbles) if enabled else content


def _split_bubbles(
    content: str,
    separator: str | None = _DEFAULT_SEPARATOR,
    enabled: bool | None = True,
) -> list[str]:
    """上游私有 `splitBubbles`：按分隔符切分、去空白、丢空段；切不出就退回整条。"""
    enabled = _js_default(enabled, True)
    separator = _js_default(separator, _DEFAULT_SEPARATOR)
    if not enabled or not separator or separator not in content:
        return [content]
    bubbles = [item.strip() for item in content.split(separator)]
    bubbles = [item for item in bubbles if item]
    return bubbles if bubbles else [content]


def _stable_commit_id(input: ScriptFirstDecisionInput, participant_id: str, prose: str) -> str:
    """上游私有 `stableCommitId`：同一份语义输入恒得同一个 commit id。

    前像刻意保留上游 camelCase 键名与 `JSON.stringify` 的紧凑形态，使两端可复现同一摘要。
    """
    decision = input['decision']
    semantic_payload = {
        'participantId': participant_id,
        'prose': prose,
        'interaction': decision.get('interaction') or None,
        'groupReplyContent': _js_default(input.get('group_reply_content'), ''),
        'crossConversationActions': decision.get('cross_conversation_actions') or [],
        'intents': decision.get('intents') or [],
        'browserIntents': decision.get('browser_intents') or [],
        'followUpCommitment': decision.get('follow_up_commitment') or None,
        'messageReactions': decision.get('message_reactions') or [],
        'localMedia': decision.get('local_media') or None,
        'nativeFace': decision.get('native_face') or None,
        'frameId': _js_default(input.get('frame_id'), ''),
        'burstId': _js_default(input.get('burst_id'), ''),
        'startsAfterEventId': _js_default(input.get('starts_after_event_id'), ''),
    }
    preimage = '\u001f'.join([
        input['story_id'],
        input['phase'],
        iso(input['from']) or '',
        iso(input['now']) or '',
        json.dumps(semantic_payload, ensure_ascii=False, separators=(',', ':')),
    ])
    digest = hashlib.sha256(preimage.encode('utf-8')).hexdigest()[:20]
    return f'commit:{digest}'


# 上游测试里 `import { decisionToScriptCommit as legacyDecisionToScriptCommit }` 表达的是
# 「这是兼容旧 beta10 输出的唯一桥」的调用点语义，不是本模块的第二个导出名；
# 公开 API 与上游保持一一对应，故这里不额外提供别名。

__all__ = [
    'AuthoredAction',
    'ScriptFirstDecisionInput',
    'bind_immediate_message_action',
    'decision_to_script_commit',
    'find_group_script_event',
    'find_outgoing_script_event',
    'unbound_immediate_message_events',
]
