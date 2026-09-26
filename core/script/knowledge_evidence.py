"""上游 `src/script/knowledge-evidence.ts` 的 Python 对应物：认知证据与联系线索。

证据只是**指向原文的指针**（谁、在哪一条条目里、原话是哪一段），永远不是叙事的替身。
本模块全部是纯函数：不 import `astrbot`，也不碰数据库。

移植约定（见键名约定）：
- Python 标识符一律 snake_case；**内部结构**（`KnowledgeEvidence`、落库的事实行、
  `ScriptEntry.metadata`）同样 snake_case（`related_fact_ids` / `source_entry_id` /
  `life_handoff`），与 `plugin/core/types.py` 的 TypedDict 一致。
- **模型可见的投影**逐字保持上游 camelCase：`fact_evidence_for_prompt`
  （`participantId` / `sourceEntryIds` / `knowledge.relatedFactIds` /
  `knowledge.clauses[].sourceEntryId`，`authority` 是上游原字面量）与
  `contact_evidence_threads`（`originalEntryIds` / `missingSourceEntryIds` /
  `originals[].participantId` / `occurredAt`）。二者的结果由
  `narrator_prompts.to_prompt_payload` 直接放进 `durableFacts` / `existingFacts` /
  `contactThreads` 发给模型，改名就等于把模型读到的东西和压缩提示词里写的字段错开。
- 上游未命名的内联对象字面量被抽成本模块的 TypedDict（`KnowledgeClause`、
  `FactEvidenceForPrompt`、`ContactEvidenceOriginal`）。
- 读入侧（模型输出 / 旧 Koishi 数据）两种拼写都认，优先上游 camelCase。
- 字符串长度与截断一律按 Python 码点计算。上游 `String#length` 数的是 UTF-16 码元，
  只有串里含星平面字符（emoji 等）且正好卡在上限时才会差 1；上游同一段代码里的
  下标运算本来就必须按码点理解，故本移植版统一码点口径。
- 上游 `Array.isArray` 判断 → `isinstance(x, list)`；`typeof x === 'object'` →
  `isinstance(x, dict)`（JS 里数组也算 object，但上游真正用到的都还会再判数组）。
- JS 的 `undefined`/`NaN` 参与比较时恒为 false，本模块用 `_js_number` / `_js_greater`
  显式复现该语义，避免 Python 直接抛 `TypeError`。
"""

from __future__ import annotations

import re
from typing import Any, Literal, Optional, Required, TypedDict

from ..time import iso
from ..types import NarrativeFact, ScriptEntry

# 上游 `const modes` / `const roles`（未 export，按约定加 `_` 前缀）。
_MODES = ('observed', 'reported', 'belief', 'proposal', 'conditional', 'confirmed', 'unclassified')
_ROLES = ('observation', 'interpretation', 'proposal', 'condition', 'confirmation')
# 上游内联数组：算作「已投递」的条目类型。
_DELIVERED_KINDS = ('user-message', 'character-message', 'group-message', 'character-group-message')
# 上游内联数组：能构成「提议↔确认」往返的条目类型。
_EXCHANGE_KINDS = ('user-message', 'character-message')
# 上游内联数组：能作为联系线索原文的条目类型。
_THREAD_KINDS = ('script', 'user-message', 'character-message', 'group-message', 'character-group-message')
# 上游 `['observed', 'reported', 'confirmed'].includes(knowledge.mode)`。
_SUPPORTING_MODES = ('observed', 'reported', 'confirmed')
# 上游 `/前置|前提|门槛|至少|除非/.test(content)`。
_LEGACY_CONDITION_CUE = re.compile(r'前置|前提|门槛|至少|除非')

KnowledgeMode = Literal['observed', 'reported', 'belief', 'proposal', 'conditional', 'confirmed', 'unclassified']
KnowledgeClauseRole = Literal['observation', 'interpretation', 'proposal', 'condition', 'confirmation']


class KnowledgeClause(TypedDict, total=False):
    """`KnowledgeEvidence['clauses']` 的元素（上游写作内联对象字面量）。"""

    role: Required[KnowledgeClauseRole]
    source_entry_id: Required[int]
    quote: Required[str]


class KnowledgeEvidence(TypedDict, total=False):
    """上游 `interface KnowledgeEvidence`：指向原文的认知指针。

    `mode` 是证据的**认识论身份**：观察到 / 被告知 / 主观相信 / 提议 / 有条件 /
    已确认 / 无法归类。`holder` 是「这是谁的观点」，`topic` 只在被某条引用原话
    包含时才算有据。
    """

    mode: Required[KnowledgeMode]
    holder: str
    topic: str
    clauses: Required[list[KnowledgeClause]]
    related_fact_ids: Required[list[int]]


class ContactEvidenceOriginal(TypedDict, total=False):
    """`ContactEvidenceThread['originals']` 的元素（上游为内联对象字面量）。

    这是**模型可见的 wire 结构**（`contactThreads` 直接进 payload），故用上游 camelCase。
    """

    id: Required[int]
    kind: Required[str]
    participantId: Required[str]
    content: Required[str]
    # 上游 `occurredAt: string`，由 `Date#toISOString()` 产出；`iso()` 无法解析时为 None。
    occurredAt: Required[str]


class FactEvidenceForPrompt(TypedDict, total=False):
    """上游 `factEvidenceForPrompt` 的返回形状（上游未命名）。

    这是**模型可见的 wire 结构**（`durableFacts` / `existingFacts` / `contactThreads.fact`
    都直接进 payload，且上游 `toCompactionPayload` 的提示词按 `relatedFactIds` /
    `sourceEntryId` 告诉模型怎么标注证据），故字段一律上游 camelCase。
    `knowledge` 是**投影**：库里的 `KnowledgeEvidence` 是内部 snake_case
    （`related_fact_ids` / `source_entry_id`），这里逐层译成 wire 名。
    """

    id: Required[int]
    participantId: Required[str]
    scope: Required[str]
    content: Required[str]
    unresolved: bool
    status: Required[str]
    sourceEntryIds: Required[list[int]]
    authority: Required[Literal['attributed-belief', 'derived-record']]
    knowledge: Required[dict[str, Any]]


class ContactEvidenceThread(TypedDict, total=False):
    """上游 `interface ContactEvidenceThread`（模型可见的 wire 结构）。"""

    fact: Required[FactEvidenceForPrompt]
    originals: Required[list[ContactEvidenceOriginal]]
    originalEntryIds: Required[list[int]]
    missingSourceEntryIds: Required[list[int]]


def _is_number(value: Any) -> bool:
    """等价 JS `typeof value === 'number' && !Number.isNaN(value)`。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _pick(mapping: Any, camel: str, snake: Optional[str] = None) -> Any:
    """读取外部/内部输入：camelCase 与 snake_case 都认，优先上游 camelCase。"""
    if not isinstance(mapping, dict):
        return None
    if camel in mapping:
        return mapping[camel]
    if snake is not None:
        return mapping.get(snake)
    return None


def _js_greater(left: Any, right: Any) -> bool:
    """等价 JS `left > right`：任一侧不是数值（undefined/NaN）时恒为 False。"""
    return _is_number(left) and _is_number(right) and left > right


def _unique(values: Any) -> list[Any]:
    """等价 `[...new Set(values)]`：按首次出现顺序去重。

    用 `==` 顺序比较而不是 `dict.fromkeys`，这样即使元素不可哈希（上游 Set 允许）
    也不会抛 `TypeError`。集合很小（引用上限 12 条），O(n²) 可接受。
    """
    result: list[Any] = []
    for value in values:
        if not any(value == existing for existing in result):
            result.append(value)
    return result


def normalize_knowledge_evidence(
    raw: Any,
    entries: list[ScriptEntry],
    source_entry_ids: list[int],
    related_fact_ids: Optional[list[int]] = None,
) -> KnowledgeEvidence:
    """校验**引用**，而不是校验自然语言的含义。

    一条引用是它作者的原话；叙述性引用不能替另一个人确认某个行为。因此：
    `confirmation` 只有在「提议与确认来自同一 participantId 的相反方向
    user/character 消息、且确认条目 id 更大」时才成立，否则降级为
    `conditional` / `unclassified`。
    """
    value: dict[str, Any] = raw if isinstance(raw, dict) else {}
    by_id: dict[Any, ScriptEntry] = {}
    for entry in entries:
        entry_id = entry.get('id')
        if entry_id in source_entry_ids:
            by_id[entry_id] = entry

    clauses: list[KnowledgeClause] = []
    raw_clauses = value.get('clauses')
    # 上游 `value.clauses.slice(0, 12)`：上限 12 条，超出直接丢弃。
    for clause in (raw_clauses[:12] if isinstance(raw_clauses, list) else []):
        if not isinstance(clause, dict):
            continue
        entry = by_id.get(clause.get('source_entry_id'))
        role = clause.get('role')
        quote = clause.get('quote')
        # 逐条照抄上游校验：条目必须来自 sourceEntryIds、role 必须在白名单、
        # quote 必须是非空字符串、≤800、且确实被 entry.content 包含。
        if (entry is None or role not in _ROLES or not isinstance(quote, str)
                or not quote.strip() or len(quote) > 800
                or not isinstance(entry.get('content'), str) or quote not in entry['content']):
            continue
        delivered = entry.get('kind') in _DELIVERED_KINDS
        # 保留原话与它真实的证据角色，而不是直接否掉这段散文：
        # 非投递条目上的 `confirmation` 降级为 `interpretation`。
        clauses.append({
            'role': 'interpretation' if role == 'confirmation' and not delivered else role,
            'source_entry_id': entry['id'],
            'quote': quote,
        })

    mode: Any = value.get('mode') or ''
    mode = mode if mode in _MODES else 'unclassified'
    if not clauses:
        mode = 'unclassified'
    if mode == 'confirmed':
        proposed = [clause for clause in clauses if clause['role'] == 'proposal']
        confirmed = [clause for clause in clauses if clause['role'] == 'confirmation']
        exchanged = False
        for proposal in proposed:
            first = by_id.get(proposal['source_entry_id'])
            for confirmation in confirmed:
                second = by_id.get(confirmation['source_entry_id'])
                if (first is not None and second is not None
                        and first.get('kind') in _EXCHANGE_KINDS
                        and second.get('kind') in _EXCHANGE_KINDS
                        and first.get('kind') != second.get('kind')
                        and first.get('participant_id') == second.get('participant_id')
                        and _js_greater(second.get('id'), first.get('id'))):
                    exchanged = True
                    break
            if exchanged:
                break
        if not exchanged:
            mode = 'conditional' if any(clause['role'] == 'condition' for clause in clauses) else 'unclassified'
    if mode == 'observed' and any(clause['role'] == 'interpretation' for clause in clauses):
        mode = 'belief'

    result: KnowledgeEvidence = {'mode': mode}
    holder = value.get('holder')
    if isinstance(holder, str) and holder.strip():
        # 上游 `value.holder.trim().slice(0, 127)`。
        result['holder'] = holder.strip()[:127]
    elif mode == 'belief':
        # 主观相信没有明说持有者时，默认属于主角。
        result['holder'] = 'protagonist'
    topic = value.get('topic')
    # topic 必须 ≤80 且被某条保留下来的引用原话包含，否则一律丢弃。
    if isinstance(topic, str) and len(topic) <= 80 and any(topic in clause['quote'] for clause in clauses):
        result['topic'] = topic
    result['clauses'] = clauses
    result['related_fact_ids'] = _unique(related_fact_ids if related_fact_ids is not None else [])[:12]
    return result


def knowledge_clauses(knowledge: Any) -> list[KnowledgeClause]:
    """在证据字段出现之前落库的行可能存着 `{}` 或半截 knowledge 对象，
    因此每一次读取都做防御性强制转换。"""
    if isinstance(knowledge, dict) and isinstance(knowledge.get('clauses'), list):
        return knowledge['clauses']
    return []


def knowledge_related_ids(knowledge: Any) -> list[int]:
    """上游 `knowledgeRelatedIds`：读取关联事实 id，形状不对时返回空列表。

    库里的证据是内部 snake_case（`related_fact_ids`）；同时接受上游 camelCase
    （`relatedFactIds`），以便读回旧 Koishi 数据与模型输出。
    """
    if isinstance(knowledge, dict):
        for key in ('related_fact_ids', 'relatedFactIds'):
            if isinstance(knowledge.get(key), list):
                return knowledge[key]
    return []


def _knowledge_wire(knowledge: KnowledgeEvidence) -> dict[str, Any]:
    """把库里的 `KnowledgeEvidence`（内部 snake_case）译成上游 wire 形状。

    模型在压缩提示词里被明确要求产出 `clauses:[{... sourceEntryId ...}]` 与
    `relatedFactIds`，所以发给模型的证据投影必须用同样的拼写；而库里按仓库约定
    存的是 `source_entry_id` / `related_fact_ids`。两者在这里交界。
    """
    wire: dict[str, Any] = {'mode': knowledge.get('mode')}
    if 'holder' in knowledge:
        wire['holder'] = knowledge.get('holder')
    if 'topic' in knowledge:
        wire['topic'] = knowledge.get('topic')
    wire['clauses'] = [
        {
            'role': clause.get('role'),
            'sourceEntryId': _pick(clause, 'sourceEntryId', 'source_entry_id'),
            'quote': clause.get('quote'),
        }
        for clause in knowledge_clauses(knowledge) if isinstance(clause, dict)
    ]
    wire['relatedFactIds'] = knowledge_related_ids(knowledge)
    return wire


def fact_evidence_for_prompt(fact: NarrativeFact) -> FactEvidenceForPrompt:
    """给 prompt 用的事实证据投影：只有形状完整的 knowledge 才算证据。

    返回**模型可见的 wire 结构**（上游 camelCase），不返回库里的内部形状。
    """
    raw_knowledge = fact.get('knowledge')
    knowledge: Optional[KnowledgeEvidence] = None
    if isinstance(raw_knowledge, dict):
        if isinstance(raw_knowledge.get('clauses'), list):
            knowledge = raw_knowledge
        elif (isinstance(raw_knowledge.get('related_fact_ids'), list)
              or isinstance(raw_knowledge.get('relatedFactIds'), list)):
            # 只有关联 id 的旧形状：保留关联关系，但明确表示「没有引用原话」。
            knowledge = {**raw_knowledge, 'clauses': []}
    return {
        'id': fact.get('id'),
        'participantId': fact.get('participant_id'),
        'scope': fact.get('scope'),
        'content': fact.get('content'),
        'unresolved': fact.get('unresolved'),
        'status': fact.get('status'),
        'sourceEntryIds': fact.get('source_entry_ids'),
        # 主角自己相信的事只能以「归属信念」出现，绝不冒充派生记录。
        'authority': 'attributed-belief' if (knowledge or {}).get('mode') == 'belief' else 'derived-record',
        'knowledge': _knowledge_wire(knowledge) if knowledge is not None else {
            'mode': 'unclassified', 'clauses': [], 'relatedFactIds': [],
        },
    }


def supports_recorded_outcome(knowledge: Any) -> bool:
    """这条证据能不能支撑「已经发生的结果」。

    上游此处对 `undefined` 会抛 TypeError；本移植版按全仓统一的防御性读取口径
    视为「无证据」。
    """
    if not isinstance(knowledge, dict):
        return False
    if knowledge.get('mode') not in _SUPPORTING_MODES:
        return False
    clauses = [clause for clause in knowledge_clauses(knowledge) if isinstance(clause, dict)]
    return (any(clause.get('role') in ('observation', 'confirmation') for clause in clauses)
            and not any(clause.get('role') == 'interpretation' for clause in clauses))


def legacy_condition_cue(content: Any) -> bool:
    """只用于收纳证据出现之前的旧记录：字面上的条件措辞换来一条检索通道，
    它绝不表示某个条件已经成立或已经兑现。"""
    return bool(_LEGACY_CONDITION_CUE.search(content if isinstance(content, str) else ''))


def _near_id(candidate: Any, entry_id: Any) -> bool:
    """上游 `Math.abs(id - entry.id) <= 2`（非数值时为 false）。"""
    return _is_number(candidate) and _is_number(entry_id) and abs(candidate - entry_id) <= 2


def contact_evidence_threads(facts: list[NarrativeFact], entries: list[ScriptEntry]) -> list[ContactEvidenceThread]:
    """把每条事实的原话条件与邻近回复收进一条线索。

    作用域在这里**再查一次**，即使调用者已经过滤过候选事实与来源行。
    """
    emitted: set[Any] = set()
    threads: list[ContactEvidenceThread] = []
    for fact in facts:
        # 上游 `new Set([...fact.sourceEntryIds, ...clauses.map(c => c.sourceEntryId)])`：
        # JS Set 保持插入顺序，这里用 `_unique` 复现同样的顺序与去重。
        raw_ids: list[Any] = list(fact.get('source_entry_ids') or [])
        for clause in knowledge_clauses(fact.get('knowledge')):
            if isinstance(clause, dict) and clause.get('source_entry_id') is not None:
                raw_ids.append(clause['source_entry_id'])
        ids = _unique(raw_ids)

        selected: list[ScriptEntry] = []
        for entry in entries:
            if entry.get('kind') not in _THREAD_KINDS:
                continue
            participant_id = entry.get('participant_id')
            # 世界级条目（无 participantId）总是可见；其余必须与本事实同分支。
            if participant_id and participant_id != fact.get('participant_id'):
                continue
            entry_id = entry.get('id')
            included = entry_id in ids or (
                entry.get('kind') in _EXCHANGE_KINDS
                and any(_near_id(candidate, entry_id) for candidate in ids))
            if not included:
                continue
            selected.append(entry)
        # 上游 `.sort((a, b) => a.id - b.id)`：按条目 id 升序。
        selected.sort(key=lambda item: item.get('id') if _is_number(item.get('id')) else 0)
        originals: list[ContactEvidenceOriginal] = [{
            'id': entry.get('id'),
            'kind': entry.get('kind'),
            'participantId': entry.get('participant_id'),
            'content': entry.get('content'),
            'occurredAt': iso(entry.get('occurred_at')),
        } for entry in selected]

        deduped: list[ContactEvidenceOriginal] = []
        for original in originals:
            if original['id'] in emitted:
                continue
            emitted.add(original['id'])
            deduped.append(original)

        threads.append({
            'fact': fact_evidence_for_prompt(fact),
            'originalEntryIds': [original['id'] for original in originals],
            'originals': deduped,
            'missingSourceEntryIds': [
                source_id for source_id in ids
                if not any(original['id'] == source_id for original in originals)
            ],
        })
    return threads


# 上游逐字照抄的英文长字符串（写作框架提示词，不翻译）。
KNOWLEDGE_WRITING_FRAME = 'EVIDENCE AND EXPECTATION: The original remains the life script. Within it, her belief, wish and imagined explanation belong to her perspective; an observed action belongs to the actor who performed it. Derived records retain these roles and their original conditions. contactThreads supplies original proposals, conditions and replies, not a second plot. Let unfinished contact motivate another question or private anticipation while its confirmation and timing remain open. Elapsed silence can change her feelings without changing what the other person promised. A confirmed exchange still carries its conditions; platform delivery alone establishes neither reading nor agreement.'
