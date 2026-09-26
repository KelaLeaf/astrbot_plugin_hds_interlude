"""上游 `upstream/src/script/continuation.ts`（41 行，v1.0.1-beta6-rebuild）的 Python 对应物。

来源型 continuation bookmark
----------------------------
`continuation_bookmark` 产出的是一个**指向当前可见原文的指针**，不是又一份摘要，
也不是场景作者。上游注释写得很直白：

    A bookmark into the visible originals, not another summary or scene author.
    Communication references record occurrences, never infer an unanswered debt.

因此本模块只做三件事：
1. 从「当前可见条目」（`occurred_at <= now`）里挑出**末次已完成的剧本**
   （`kind == 'script'` 且 `occurred_at <= from_`）；
2. 记下最近 4 条真实通信条目的**引用**（id/kind/participant/时间），不复制台词；
3. 标出本批次的新用户事件 id。

它**不**生成场景摘要、**不**复制台词、**不**推断用户尚未回复（「他还没回我」这类
债务式结论必须由模型从原文自行判断，不能由 bookmark 凭空断言）。

键名（键名约定）
------------------------------------------
- **返回值是 wire format**：它由 `narrator_prompts.to_prompt_payload` 放进
  `authoringWindow.continuation` 直接发给模型，上游 `systemPrompt` 的
  `CONTINUATION BOOKMARK: authoringWindow.continuation locates the last completed
  passage and communications` 就是按这些名字指路。故输出键名**逐字保持上游
  camelCase**：``establishedThrough`` / ``lastScript`` / ``writingStart`` /
  ``originalEndpoint`` / ``newEventEntryIds`` / ``recentCommunications``，
  内层引用为 ``entryId`` / ``participantId`` / ``occurredAt``，
  端点偏移为 ``characterOffset``。
- **入参 `entries` 是内部领域对象**（`types.ScriptEntry`，snake_case）：
  读取一律走 `_get(entry, camel, snake)`，旧 Koishi camelCase 数据同样认。
- 上游参数 `from` 是 Python 保留字，按惯例改名为 `from_`（只改参数名，不改业务字段）。
- 时间：比较一律在 timezone-aware UTC `datetime` 上做（`..time.parse_dt`）；
  输出仍是 ISO 字符串（上游 `Date#toISOString()` 的形状，由 `..time.iso` 提供）。
- `content.length` → `len(content)`。上游量的是 UTF-16 码元，Python 量的是码点：
  对 BMP 文本（含中日韩）两者完全一致，只有补充平面字符（emoji 等）会相差 1。
  这里保留 Python 语义，好让 ``originalEndpoint['characterOffset']`` 能被 Python 侧
  直接用于切片。
"""

from __future__ import annotations

from typing import Any, Optional

from ..time import iso, parse_dt

__all__ = ['continuation_bookmark', 'prose_reuse_observation']

#: 被当作「真实通信」的条目类型（上游原样照抄，顺序不影响判定）。
_COMMUNICATION_KINDS = (
    'user-message',
    'character-message',
    'character-group-message',
    'character-platform-action',
)

#: `recentCommunications` 的条数上限（上游 `.slice(-4)`）。
_RECENT_COMMUNICATION_LIMIT = 4


def _get(mapping: Any, *keys: str, default: Any = None) -> Any:
    """按顺序读取第一个存在的键（兼容 snake_case / camelCase 两种拼写）。"""
    if isinstance(mapping, dict):
        for key in keys:
            if key in mapping:
                return mapping[key]
    return default


def _at(entry: dict[str, Any]) -> Optional[Any]:
    """条目的 occurredAt（UTC datetime）；无法解析时返回 None。

    上游对非法 `Date` 的比较恒为 false，因此解析失败等价于「不可见」。
    """
    return parse_dt(_get(entry, 'occurredAt', 'occurred_at')) if isinstance(entry, dict) else None


def _reference(entry: dict[str, Any]) -> dict[str, Any]:
    """上游闭包 `reference(entry)`：只记录引用，绝不带上 `content`。

    这是模型可见的 wire 形状，故用上游 camelCase。
    """
    return {
        'entryId': _get(entry, 'id'),
        'kind': _get(entry, 'kind'),
        'participantId': _get(entry, 'participantId', 'participant_id'),
        'occurredAt': iso(_get(entry, 'occurredAt', 'occurred_at')),
    }


def continuation_bookmark(
    entries: list[dict[str, Any]],
    from_: Any,
    now: Any,
) -> dict[str, Any]:
    """上游 `continuationBookmark(entries, from, now)`。

    返回一份「书签」：从哪儿继续写、上一段完成的剧本在哪、本批次新增了哪些用户事件、
    以及最近几条真实通信的引用。它指向原文，不重述原文。
    """
    from_dt = parse_dt(from_)
    now_dt = parse_dt(now)

    # `visible = entries.filter(entry => entry.occurredAt <= now)`
    visible: list[dict[str, Any]] = []
    for entry in entries:
        at = _at(entry)
        if at is None or now_dt is None:
            continue
        if at <= now_dt:
            visible.append(entry)

    # 末次已完成的剧本：可见条目里 kind === 'script' 且 occurredAt <= from 的最后一条。
    scripts = [
        entry for entry in visible
        if _get(entry, 'kind') == 'script'
        and from_dt is not None
        and (_at(entry) is not None and _at(entry) <= from_dt)
    ]
    last_script = scripts[-1] if scripts else None

    bookmark: dict[str, Any] = {
        'establishedThrough': iso(from_),
        'writingStart': 'after-last-completed-passage',
        'newEventEntryIds': [
            _get(entry, 'id') for entry in visible
            if _get(entry, 'kind') == 'user-message'
            and from_dt is not None
            and (_at(entry) is not None and _at(entry) > from_dt)
        ],
        # 这一批新事件不是最近对话的第二份副本。原文引用让短促的追问能
        # 保留它真正回应的那个异议/问题。
        'recentCommunications': [
            _reference(entry) for entry in visible
            if (_get(entry, 'kind') != 'user-message'
                or (from_dt is not None and _at(entry) is not None and _at(entry) <= from_dt))
            and _get(entry, 'kind') in _COMMUNICATION_KINDS
        ][-_RECENT_COMMUNICATION_LIMIT:],
    }
    if last_script is not None:
        bookmark['lastScript'] = _reference(last_script)
        bookmark['originalEndpoint'] = {
            'entryId': _get(last_script, 'id'),
            'characterOffset': len(_get(last_script, 'content') or ''),
        }
    return bookmark


def prose_reuse_observation(previous: str, next: str, width: int = 40) -> float:
    """上游 `proseReuseObservation(previous, next, width = 40)`。

    **仅用于诊断**：长段字面复用是"值得看一眼"的证据，不是文学层面的拒绝规则；
    完全相同的短问句绝不能被当成过错（因此长度不足 `width` 时直接返回 0）。

    返回被复用文本覆盖的比例（0..1）。
    """
    if len(previous) < width or len(next) < width:
        return 0
    spans = {previous[i:i + width] for i in range(len(previous) - width + 1)}
    covered = 0
    end = 0
    for i in range(len(next) - width + 1):
        if next[i:i + width] not in spans:
            continue
        covered += max(0, i + width - max(end, i))
        end = i + width
    return covered / len(next)
