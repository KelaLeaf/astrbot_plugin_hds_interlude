"""上游 `src/script/life-handoff.ts` 的 Python 对应物：生活交接指针。

这些只是**指向这一份已提交原文的小指针**，不是第二份场景摘要。引用用来建立
出处，不用来证明语义；推断仍然留在文学层。

键名（`docs/PORT_PLAN.md` §2「⚠️ 键名法」）——本模块同时有内部与 wire 两个出口
--------------------------------------------------------------------------------
- **内部 / metadata 出口（snake_case）**：
  `normalize_life_handoff()` 的结果由 `turn_persistence.py` 写进
  `script_entry.metadata['life_handoff']`，并由 `scene_frame.py` 的
  `_apply_life_handoff()` 读取 `resolved_details`。按仓库既有约定，写进 metadata
  的键一律 snake_case（`resolved_details` / `name` / `quote` / `value`），
  `entry_life_handoff()` 返回的也是这份内部形状，`types.py` 的 `metadata.life_handoff`
  就标注为 `LifeHandoff`（同样是 snake_case）。
- **prompt / wire 出口（上游 camelCase）**：`narrative_evidence()` 的结果由
  `narrator_prompts` 放进剧本条目（`lifeHandoff` / `proposedTimeline` /
  `timelineEvidence` / `communicationOutcome` / `narrativeAuthority`）直接发给模型，
  上游 `systemPrompt` 也按 `resolvedDetails` 告诉模型怎么产出。因此这里**另设一份
  投影** `_life_handoff_wire()`：同一个函数既要写 metadata 又要进 payload，就拆成
  两个出口，绝不混用（内部用 `resolved_details`，wire 用 `resolvedDetails`）。
- 读入侧（模型输出 / 旧 Koishi 数据 / 被 `chunk7._readable_metadata()` 补过
  camelCase 副本的 metadata）两种拼写都认，优先上游 camelCase。
- 字符串长度按 Python 码点计算（与 `knowledge_evidence.py` 同一口径，见该文件说明）。
"""

from __future__ import annotations

from typing import Any, Optional, Required, TypedDict

from ..types import ScriptEntry


def _pick(mapping: Any, camel: str, snake: Optional[str] = None) -> Any:
    """读取外部/内部输入：camelCase 与 snake_case 都认，优先上游 camelCase。"""
    if not isinstance(mapping, dict):
        return None
    if camel in mapping:
        return mapping[camel]
    if snake is not None:
        return mapping.get(snake)
    return None


class QuotedLifeValue(TypedDict, total=False):
    """上游 `interface QuotedLifeValue`。"""

    value: Required[str]
    quote: Required[str]


class LifeHandoffPresence(TypedDict, total=False):
    """上游 `presence?: { names: string[]; quote: string }`。

    这是**完整的本地名单**，包括「明确有证据的空名单」。
    """

    names: Required[list[str]]
    quote: Required[str]


class LifeHandoffTransition(TypedDict, total=False):
    """上游 `transition?: { quote: string }`。"""

    quote: Required[str]


class LifeHandoffResolvedDetail(TypedDict, total=False):
    """`LifeHandoff['resolvedDetails']` 的元素（上游为内联对象字面量）。"""

    label: Required[str]
    quote: Required[str]


class LifeHandoff(TypedDict, total=False):
    """上游 `interface LifeHandoff`，但**按本仓库的 metadata 约定存 snake_case**。

    这是内部出口（`entry.metadata['life_handoff']`）的形状；发给模型的那一份由
    `_life_handoff_wire()` 译成上游 camelCase（`resolvedDetails`）。
    """

    place: QuotedLifeValue
    activity: QuotedLifeValue
    presence: LifeHandoffPresence
    transition: LifeHandoffTransition
    resolved_details: list[LifeHandoffResolvedDetail]


class NarrativeEvidence(TypedDict, total=False):
    """上游 `narrativeEvidence` 的返回形状（上游写作内联对象类型）。

    这是**模型可见的 wire 结构**，字段一律上游 camelCase。
    """

    narrativeAuthority: str
    lifeHandoff: Optional[dict[str, Any]]
    proposedTimeline: Any
    timelineEvidence: Any
    communicationOutcome: str


def _unique(values: list[Any]) -> list[str]:
    """等价 `[...new Set(values)]`：按首次出现顺序去重。"""
    result: list[Any] = []
    for value in values:
        if not any(value == existing for existing in result):
            result.append(value)
    return result


def normalize_life_handoff(raw: Any, prose: str) -> Optional[LifeHandoff]:
    """把模型给的生活交接草稿裁成**有原文出处**的指针；没有任何可用字段时返回 None。

    入参 `raw` 既是模型刚吐出的草稿（上游 camelCase `resolvedDetails`），也可能是
    库里已存的 metadata（本仓库 snake_case `resolved_details`），两种拼写都认。
    返回的是**内部/metadata 形状**（snake_case）。

    每一条 quote 都必须真实出现在本次提交的散文里：
    - quote 去空白后长度 2–500，且被 `prose` 包含；
    - `place` / `activity` 的 value 去空白后非空、长度 ≤160；
    - `presence.names` 每一项都必须非空、且被 `presence.quote` 包含（上限 8 项）；
    - `resolvedDetails` 的 label ≤80（不去空白）、quote 合规，过滤后取前 10 条。
    """
    if not isinstance(raw, dict) or not isinstance(prose, str):
        return None
    value: dict[str, Any] = raw

    def quoted(quote: Any) -> bool:
        """上游 `quoted`：非空、长度 2–500、且确实出现在散文里。"""
        return (isinstance(quote, str) and len(quote.strip()) >= 2
                and len(quote) <= 500 and quote in prose)

    result: LifeHandoff = {}
    for key in ('place', 'activity'):
        item = value.get(key)
        if not isinstance(item, dict):
            continue
        item_value = item.get('value')
        if (isinstance(item_value, str) and item_value.strip()
                and len(item_value) <= 160 and quoted(item.get('quote'))):
            result[key] = {'value': item_value.strip(), 'quote': item['quote']}
    presence = value.get('presence')
    if isinstance(presence, dict):
        names = presence.get('names')
        quote = presence.get('quote')
        if (isinstance(names, list) and quoted(quote)
                and all(isinstance(name, str) and name.strip() and name in quote for name in names)):
            result['presence'] = {'names': _unique(names)[:8], 'quote': quote}
    transition = value.get('transition')
    if isinstance(transition, dict) and quoted(transition.get('quote')):
        result['transition'] = {'quote': transition['quote']}
    # 模型按上游提示词给的是 `resolvedDetails`；库里已存的是 `resolved_details`。
    resolved = _pick(value, 'resolvedDetails', 'resolved_details')
    if isinstance(resolved, list):
        kept = [item for item in resolved
                if isinstance(item, dict) and isinstance(item.get('label'), str)
                and len(item['label']) <= 80 and quoted(item.get('quote'))]
        result['resolved_details'] = [
            {'label': item['label'], 'quote': item['quote']} for item in kept[:10]
        ]
    return result if result else None


def entry_life_handoff(entry: ScriptEntry) -> Optional[LifeHandoff]:
    """只有 `script` 条目才有生活交接（它是那次提交的原文）。

    返回**内部/metadata 形状**（snake_case）：`scene_frame.py` 与
    `chunk7` 都按这个形状消费。发给模型的那一份见 `_life_handoff_wire()`。
    """
    if entry.get('kind') != 'script':
        return None
    metadata = entry.get('metadata')
    raw = _pick(metadata, 'lifeHandoff', 'life_handoff') if isinstance(metadata, dict) else None
    return normalize_life_handoff(raw, entry.get('content'))


def _life_handoff_wire(handoff: Optional[LifeHandoff]) -> Optional[dict[str, Any]]:
    """把内部/metadata 形状的生活交接译成上游 wire 形状（`resolvedDetails`）。

    键序与上游 `normalizeLifeHandoff()` 的赋值顺序一致：
    place → activity → presence → transition → resolvedDetails。
    """
    if handoff is None:
        return None
    wire: dict[str, Any] = {}
    for key in ('place', 'activity'):
        item = handoff.get(key)
        if isinstance(item, dict):
            wire[key] = {'value': item.get('value'), 'quote': item.get('quote')}
    presence = handoff.get('presence')
    if isinstance(presence, dict):
        wire['presence'] = {'names': presence.get('names'), 'quote': presence.get('quote')}
    transition = handoff.get('transition')
    if isinstance(transition, dict):
        wire['transition'] = {'quote': transition.get('quote')}
    resolved = handoff.get('resolved_details')
    if isinstance(resolved, list):
        wire['resolvedDetails'] = [
            {'label': item.get('label'), 'quote': item.get('quote')} for item in resolved
        ]
    return wire


def narrative_evidence(entry: ScriptEntry) -> NarrativeEvidence:
    """新的原文才是完成记录；它的导演计划仍然只是计划。

    旧的账本保持**可区分**，不会被就地重新解释。

    返回的是**模型可见的 wire 结构**（上游 camelCase），其中 `lifeHandoff` 由
    `_life_handoff_wire()` 从 metadata 的 snake_case 形状投影而来。
    """
    metadata = entry.get('metadata')
    meta: dict[str, Any] = metadata if isinstance(metadata, dict) else {}
    result: NarrativeEvidence = {}
    delivery_actions = _pick(meta, 'deliveryActions', 'delivery_actions')
    # 有 commitId 却没有任何投递动作 → 明确记为「没有出站动作」，避免被误读成已发送。
    if _pick(meta, 'commitId', 'commit_id') and isinstance(delivery_actions, list) and not delivery_actions:
        result['communicationOutcome'] = 'no-outgoing-action-recorded'
    timeline_plan = _pick(meta, 'timelinePlan', 'timeline_plan')
    if _pick(meta, 'narrativeAuthority', 'narrative_authority') == 'original-v2':
        result['narrativeAuthority'] = 'original-v2'
        # 上游此处无条件写入该键（值可能是 undefined）；Python 用 None 保持键存在。
        result['lifeHandoff'] = _life_handoff_wire(entry_life_handoff(entry))
        if timeline_plan:
            result['proposedTimeline'] = timeline_plan
    elif timeline_plan:
        result['timelineEvidence'] = timeline_plan
    return result
