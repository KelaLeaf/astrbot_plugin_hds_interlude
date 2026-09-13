"""上游 `src/script/authored-actions.ts` 的 Python 对应物：剧本里的「她说的话」。

**只做语法解析，绝不从散文里猜动作。** 重复 id 不是可执行引用；原文照旧存活。

解析出的 `AuthoredAction` 会被下游运输层使用，因此这里保持与上游一致的
「同一份 actions 数组对象」语义（见下方 WeakSet 说明）。

移植约定（见 `docs/PORT_PLAN.md`）：
- 字段名 `camelCase` → `snake_case`（`authoredActions` → `authored_actions`、
  `actionId` → `action_id`、`groupReply` → `group_reply`、
  `crossConversationActions` → `cross_conversation_actions`）。
- `start` / `end` 是**散文串的下标**，全程用 Python 码点；同一份代码里的
  `prose[start:end] == content` 比较也按码点，故自洽。
"""

from __future__ import annotations

import re
from collections import OrderedDict
from typing import Any, Optional, Required, TypedDict

from ..types import NarrativeDecision

# 上游 `/<say id="([\w-]{1,64})">([\s\S]*?)<\/say>/g`。
# ⚠️ JS 的 `\w` 只匹配 `[A-Za-z0-9_]`（ASCII），Python 的 `\w` 默认还匹配汉字，
# 故这里显式写成 `[A-Za-z0-9_-]`，避免接受上游不会接受的 id。
_SAY_PATTERN = re.compile(r'<say id="([A-Za-z0-9_-]{1,64})">([\s\S]*?)</say>')
# 上游 `completeLegacyBubbleBlock` 里的分段与校验正则（`\\n` = 字面量反斜杠 + n）。
_PARAGRAPH_BREAK = re.compile(r'\r?\n\s*\r?\n|\\n\\n')
_BAD_BUBBLE_CHAR = re.compile(r'[\r\n<>]|\\n')


class AuthoredAction(TypedDict, total=False):
    """上游 `interface AuthoredAction`：剧本里一段被显式授权的发言。"""

    id: Required[str]
    start: Required[int]
    end: Required[int]
    content: Required[str]


class ReadAuthoredActionsResult(TypedDict, total=False):
    """上游 `readAuthoredActions` 的返回形状（上游未命名）。"""

    prose: Required[str]
    actions: Required[list[AuthoredAction]]


# --------------------------------------------------------------------------------------
# WeakSet<AuthoredAction[]> 的等价物（本模块唯一的实现性偏离）
#
# 上游用两个模块级 `WeakSet<AuthoredAction[]>` 记住「这个 actions 数组已经被 resolve
# 过 / 已经提前投递过」。WeakSet 按**对象身份**判定，因此关键在于：判定必须落在
# 同一个 list 对象上，而不是内容相同的新 list（伪造的 authoredActions 必须被忽略）。
#
# Python 的 list 不支持弱引用（`weakref.ref([])` 直接 TypeError），按
# `docs/PORT_PLAN.md` §2 的约定改用 `set[int]` 保存 `id(list)`，并用显式辅助函数
# 集中读写标记，业务代码里绝不裸操作集合。
#
# ⚠️ 纯 `set[int]` 会破坏「行为一致」：被标记的 list 一旦被 GC，它的 `id` 会立刻被
# 下一条新 list 复用，集合里就留下一个**假阳性**标记。实测（本文件用例
# `test_bad_explicit_ids_...`）：一次 resolve 造出的空 actions 数组被回收后，紧接着
# 新建的 `authored_actions` 就被误判成「已解析」，伪造继承因此被放行——上游不会。
# 因此在 `set[int]` 之外补一份**同 id 的强引用**（`_pins`），标记与引用同生共死：
#   * `_ids` 里的每个 id 都对应一个仍然活着的对象 ⇒ 绝不会有失效 id、绝无假阳性；
#   * `_pins` 有上限（4096），淘汰时**同时**从 `_ids` 移除，所以也不会留下失效 id；
#     代价只是「极老的、仍被外部持有的 actions 数组会丢标记」——而上游那种数组早已
#     被 GC 掉，实际语义等价。
# 若允许偏离 PORT_PLAN 指定的 `set[int]`，用带 `_resolved` 标记位的 list 子类可以
# 零成本做到完全一致（list 子类支持弱引用），此处按约定不加。
# --------------------------------------------------------------------------------------
class _ActionMarkRegistry:
    """`WeakSet<AuthoredAction[]>` 的等价物：按对象身份标记，且绝不残留失效 id。"""

    __slots__ = ('_ids', '_pins')
    _MAX_PINS = 4096

    def __init__(self) -> None:
        self._ids: set[int] = set()
        self._pins: 'OrderedDict[int, Any]' = OrderedDict()

    def has(self, value: Any) -> bool:
        return isinstance(value, list) and id(value) in self._ids

    def add(self, value: Any) -> None:
        if not isinstance(value, list):
            return
        key = id(value)
        if key in self._ids:
            self._pins.move_to_end(key)
            return
        self._ids.add(key)
        self._pins[key] = value
        while len(self._pins) > self._MAX_PINS:
            stale, _ = self._pins.popitem(last=False)
            self._ids.discard(stale)

    def clear(self) -> None:
        self._ids.clear()
        self._pins.clear()


_resolved_actions = _ActionMarkRegistry()
_delivered_actions = _ActionMarkRegistry()


def _is_resolved(value: Any) -> bool:
    """上游 `resolvedActions.has(value)`。"""
    return _resolved_actions.has(value)


def _is_delivered(value: Any) -> bool:
    """上游 `deliveredActions.has(value)`。"""
    return _delivered_actions.has(value)


def _mark_resolved(value: Any) -> None:
    """上游 `resolvedActions.add(value)`。"""
    _resolved_actions.add(value)


def _mark_delivered(value: Any) -> None:
    """上游 `deliveredActions.add(value)`。"""
    _delivered_actions.add(value)


def read_authored_actions(script: str) -> ReadAuthoredActionsResult:
    """把 `<say id="...">...</say>` 拆成「散文 + 可执行发言」。

    重复 id 会被整组剔除：同一个 id 出现两次时无法确定引用的是哪一段，
    因此它们都不是可执行引用。
    """
    actions: list[AuthoredAction] = []
    prose = ''
    cursor = 0
    for match in _SAY_PATTERN.finditer(script):
        prose += script[cursor:match.start()]
        start = len(prose)
        prose += match.group(2)
        actions.append({'id': match.group(1), 'start': start, 'end': len(prose), 'content': match.group(2)})
        cursor = match.end()
    prose += script[cursor:]
    return {
        'prose': prose,
        'actions': [action for action in actions
                    if sum(1 for other in actions if other['id'] == action['id']) == 1],
    }


def _action_in_prose(action: Any, prose: str) -> bool:
    """上游 filter：`action.start >= 0 && action.end <= prose.length && prose.slice(...) === action.content`。

    非数值下标在 JS 里比较恒为 false（NaN），这里显式复现，避免 Python 抛异常。
    """
    if not isinstance(action, dict):
        return False
    start = action.get('start')
    end = action.get('end')
    content = action.get('content')
    if not (isinstance(start, int) and not isinstance(start, bool)
            and isinstance(end, int) and not isinstance(end, bool)):
        return False
    return 0 <= start and end <= len(prose) and prose[start:end] == content


def _split_limit(value: Any, prose: str) -> bool:
    """上游 `prose.slice(item.start, item.end) === item.content && item.content.trim()`。"""
    if not isinstance(value, dict) or not isinstance(value.get('content'), str):
        return False
    start = value.get('start')
    end = value.get('end')
    content = value['content']
    if not (isinstance(start, int) and not isinstance(start, bool)
            and isinstance(end, int) and not isinstance(end, bool)):
        return False
    return prose[start:end] == content and bool(content.strip())


def complete_legacy_bubble_block(prose: str, content: str, separator: str) -> Optional[str]:
    """从 legacy 镜像末尾那段**显式运输块**里补回丢失的气泡。

    只在最后一段以「已有内容 + 分隔符」开头时成立；普通叙述、别的接收者的动作、
    含换行/尖括号/字面量 `\\n` 的段一律不认。
    """
    if not separator or not (isinstance(content, str) and content.strip()):
        return None
    paragraphs = _PARAGRAPH_BREAK.split(prose.strip())
    # 上游 `.at(-1)?.trim() ?? ''`；split 恒返回至少一个元素。
    tail = paragraphs[-1].strip() if paragraphs else ''
    if not tail.startswith(content.strip() + separator) or len(tail) > 4000:
        return None
    parts = tail.split(separator)
    if len(parts) < 2 or any((not part.strip()) or _BAD_BUBBLE_CHAR.search(part) for part in parts):
        return None
    return tail


def resolve_authored_actions(
    decision: NarrativeDecision,
    already_sent: bool = False,
    separator: str = '<sep/>',
) -> NarrativeDecision:
    """在任何运输层归一化之前，先把同一份授权原文解析出来。

    legacy 内容保持兼容；一个破损的显式引用**不能**去发另一段独立写就的答复；
    延迟草稿仍走它原有的路径。
    """
    if not isinstance(decision, dict) or not isinstance(decision.get('script'), str):
        return decision
    parsed = read_authored_actions(decision['script'])
    raw_prose = parsed['prose']
    leading = len(raw_prose) - len(raw_prose.lstrip())
    prose = raw_prose.strip()

    inherited = decision.get('authored_actions')
    if not _is_resolved(inherited):
        # 只有「本模块此前解析出来的同一个数组」才允许被继承。
        inherited = []
    if not already_sent and _is_delivered(inherited):
        already_sent = True

    source = parsed['actions'] if parsed['actions'] else inherited
    actions: list[AuthoredAction] = [
        {**action, 'start': action['start'] - leading, 'end': action['end'] - leading}
        for action in source
    ]
    actions = [action for action in actions if _action_in_prose(action, prose)]

    interaction = decision.get('interaction')
    private_reply = interaction.get('reply') if isinstance(interaction, dict) else None
    group_reply = decision.get('group_reply')
    cross_actions = decision.get('cross_conversation_actions')
    one_private_recipient = (
        (not group_reply or (group_reply.get('mode') if isinstance(group_reply, dict) else None) == 'none')
        and not (len(cross_actions) if isinstance(cross_actions, (list, str)) else 0)
    )

    if (not already_sent and one_private_recipient and len(actions) == 1
            and isinstance(private_reply, dict) and private_reply.get('mode') == 'immediate'
            and private_reply.get('action_id') == actions[0]['id']):
        tail = complete_legacy_bubble_block(prose, actions[0]['content'], separator)
        if tail and actions[0]['start'] == len(prose) - len(tail):
            actions[0] = {**actions[0], 'content': tail, 'end': len(prose)}

    def resolve(reply: Any) -> Any:
        """把 `actionId` 解析成真实原话；失配时保守地退成 `mode:'none'`。"""
        if not isinstance(reply, dict):
            return reply
        if already_sent:
            # 已经提前流式发出的回合：引用作废，但内容（若已有）保持原样。
            return {**reply, 'action_id': None}
        if not reply.get('action_id') or reply.get('mode') != 'immediate':
            return reply
        action = next((item for item in actions if item['id'] == reply['action_id']
                       and _split_limit(item, prose)), None)
        if action is not None:
            return {**reply, 'content': action['content']}
        return {**reply, 'mode': 'none', 'content': None}

    # 引用失配的保守兜底：整份剧本只有一个已授权 say 行动、本回合只有一个私聊
    # 接收者、且回复没有可用 content 时，该行动就是这条回复的本体——模型常照抄
    # 协议示例里的 id 字面量导致引用对不上。零行动、重复 id、伪造继承与已提前
    # 流式发送的情况都不适用，保持原有的 none 语义。
    def sole_action_reply(reply: Any) -> Any:
        if (already_sent or not one_private_recipient or not isinstance(reply, dict)
                or reply.get('mode') != 'immediate' or reply.get('content') or len(actions) != 1):
            return reply
        only = actions[0]
        if not _split_limit(only, prose):
            return reply
        return {**reply, 'action_id': only['id'], 'content': only['content']}

    # 只从 legacy 镜像显式的末尾运输块里补回丢失的气泡，绝不从普通叙述或
    # 另一个接收者的动作里补。
    legacy_tail = (
        complete_legacy_bubble_block(prose, private_reply['content'], separator)
        if (not already_sent and one_private_recipient and not actions
            and isinstance(private_reply, dict) and private_reply.get('mode') == 'immediate'
            and not private_reply.get('action_id') and private_reply.get('content'))
        else None
    )

    _mark_resolved(actions)
    if already_sent:
        _mark_delivered(actions)

    result: NarrativeDecision = {**decision, 'script': prose, 'authored_actions': actions}
    if decision.get('interaction'):
        reply = private_reply
        if legacy_tail:
            reply = {**private_reply, 'content': legacy_tail}
        else:
            reply = sole_action_reply(private_reply)
        result['interaction'] = {**decision['interaction'], 'reply': resolve(reply)}
    if decision.get('group_reply'):
        result['group_reply'] = resolve(decision['group_reply'])
    if isinstance(decision.get('cross_conversation_actions'), list):
        result['cross_conversation_actions'] = [resolve(item) for item in decision['cross_conversation_actions']]
    return result
