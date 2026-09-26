"""上游 `upstream/src/script/recall-navigation.ts`（70 行，v1.0.1-beta6-rebuild）的 Python 对应物。

只做本地导航：这些 span **永不替换不可变原文**
（上游注释：``Local navigation only: these spans never replace the immutable source.``）。
本模块负责「怎么在原文里定位与截窗」，不负责「取哪些条目」——那是 `episode_index.py` 的事。

四个导出：
- `recall_keys(text)`：召回用的词键（ASCII 词 + CJK 二元组），去重保序。
- `index_original(content)`：把原文切成不超过 700 字符的句子段（`RecallSpan`），
  句子边界让"条件"跟它所属的那句话待在一起，而不是被 token 切碎。
- `score_original(keys, spans)`：命中率打分，返回 `{'score', 'index'}`。
- `original_window(content, spans, index, budget, query_keys=[])`：**优先保留命中段，
  再补邻域**；单句过长时按查询键回退定位；最终 `end` 被夹到 `start + budget`，
  即"单条超长句子明确只是摘录，不是完整记录"。
- `recall_focus(message, topics, intent_summaries)`：有界的查询线索；
  计划只用来导航到证据，**绝不变成证据本身**。

形态与命名
----------
- `RecallSpan` 上游是 TS `interface`（`{start, end, keys: Set<string>}`）→ 这里用
  `TypedDict`，`keys` 为 `set[str]`。
- 返回对象字段 `camelCase → snake_case`（键名约定）：
  `RecallSpan` 内部字段本就是单词，无变化；`scoreOriginal` 返回
  `{'score', 'index'}`；`originalWindow` 返回 `{'content', 'start', 'end'}`。
- 长度一律用 `len()`。上游按 UTF-16 码元计长，Python 按码点计长：BMP 文本
  （含中日韩）完全一致，只有补充平面字符（emoji 等）会差 1。这里保留 Python 语义，
  让 `start`/`end` 能被 Python 侧直接切片（返回值 `content` 与 `start`/`end` 始终自洽）。
- 上游 `spans[index - 1]` / `spans[index + 1]` 在越界时是 `undefined`（JS 数组负下标
  不回溯）；Python 负下标会绕回数组尾部，**必须显式挡掉**，见 `_span_at`。
"""

from __future__ import annotations

import re
from typing import Any, Optional, TypedDict

__all__ = [
    'RecallSpan',
    'recall_keys',
    'index_original',
    'score_original',
    'original_window',
    'recall_focus',
]


class RecallSpan(TypedDict, total=False):
    """一处原文片段：`[start, end)` 以及它的召回词键。"""

    start: int
    end: int
    keys: set[str]


#: 上游 `/[a-z0-9]{2,}/g`（作用在 `toLowerCase()` 之后）。
_ASCII_KEY_RE = re.compile(r'[a-z0-9]{2,}')
#: 上游 `/[\u3400-\u9fff]{2,}/g`（CJK 统一表意文字扩展 A + 基本区）。
_CJK_RUN_RE = re.compile(r'[\u3400-\u9fff]{2,}')
#: 上游 `/[^。！？\n]+[。！？\n]*|[。！？\n]+/g`：句子（连同尾随标点）或纯标点段。
_SENTENCE_RE = re.compile(r'[^。！？\n]+[。！？\n]*|[。！？\n]+')
#: 上游硬编码的分段上限。
_MAX_SPAN_CHARS = 700
#: 上游 `message?.trim().slice(0, 400)`。
_FOCUS_MESSAGE_CHARS = 400
#: 上游 `cues.map(s => s.slice(0, 80))`。
_FOCUS_CUE_CHARS = 80
#: 上游 `[...cues].slice(0, 3)`。
_FOCUS_CUE_LIMIT = 3


def recall_keys(text: str) -> list[str]:
    """上游 `recallKeys(text)`：ASCII 词（≥2 字符）+ CJK 二元组，去重保序。"""
    normalized = text.lower()
    words: list[str] = _ASCII_KEY_RE.findall(normalized)
    for run in _CJK_RUN_RE.findall(normalized):
        for i in range(len(run) - 1):
            words.append(run[i:i + 2])
    # `[...new Set(words)]`：去重且保留首次出现顺序。
    return list(dict.fromkeys(words))


def index_original(content: str) -> list[RecallSpan]:
    """上游 `indexOriginal(content)`：按句子切段，段长上限 700。"""
    spans: list[RecallSpan] = []
    start = 0
    end = 0

    def push() -> None:
        nonlocal start
        if end > start:
            spans.append({
                'start': start,
                'end': end,
                'keys': set(recall_keys(content[start:end])),
            })
        start = end

    for match in _SENTENCE_RE.finditer(content):
        sentence = match.group(0)
        # 当前段再加上这句就超过 700 → 先把当前段收掉，句子整体落到下一段。
        if end > start and end - start + len(sentence) > _MAX_SPAN_CHARS:
            push()
        end = match.start() + len(sentence)
    push()
    return spans


def score_original(keys: list[str], spans: list[RecallSpan]) -> dict[str, int | float]:
    """上游 `scoreOriginal(keys, spans)`：返回覆盖度最高的 span 下标。"""
    score = 0.0
    index = 0
    if not keys:
        return {'score': score, 'index': index}
    for i, span in enumerate(spans):
        span_keys = span.get('keys') or set()
        value = sum(1 for key in keys if key in span_keys) / len(keys)
        if value > score:
            score = value
            index = i
    return {'score': score, 'index': index}


def _span_at(spans: list[RecallSpan], index: int) -> Optional[RecallSpan]:
    """越界返回 None（对齐 JS 数组越界为 `undefined`；Python 负下标会绕回，必须挡）。"""
    if index < 0 or index >= len(spans):
        return None
    return spans[index]


def original_window(
    content: str,
    spans: list[RecallSpan],
    index: int,
    budget: int,
    query_keys: Optional[list[str]] = None,
) -> dict[str, Any]:
    """上游 `originalWindow(content, spans, index, budget, queryKeys = [])`。

    返回 `{'content': 片段文本, 'start': 起, 'end': 止}`。
    **命中段优先**：先以 anchor span 为窗口；只有当这一段本身就超出预算、且给了查询键时，
    才把窗口起点退回"第一个查询键命中位置往前 1/3 预算"处（绝不越过 anchor.start）。
    随后依次尝试并入 `index-1` / `index+1` 两个邻居（只在合并后不超过预算时），
    最后把 `end` 夹到 `start + budget`。
    """
    keys = query_keys if query_keys is not None else []
    if len(content) <= budget:
        return {'content': content, 'start': 0, 'end': len(content)}
    anchor = _span_at(spans, index)
    if anchor is None:
        return {'content': content[:budget], 'start': 0, 'end': min(budget, len(content))}
    start = anchor['start']
    end = anchor['end']
    if end - start > budget and keys:
        lower = content.lower()
        offsets = []
        for key in keys:
            offset = lower.find(key, anchor['start'])
            if start <= offset < end:
                offsets.append(offset)
        if offsets:
            hit = min(offsets)
            start = max(start, min(hit - budget // 3, end - budget))
    for neighbor in (_span_at(spans, index - 1), _span_at(spans, index + 1)):
        if neighbor is None:
            continue
        if max(end, neighbor['end']) - min(start, neighbor['start']) <= budget:
            start = min(start, neighbor['start'])
            end = max(end, neighbor['end'])
    # 单条超长句子明确只是摘录，不是完整记录。
    end = min(end, start + budget)
    return {'content': content[start:end], 'start': start, 'end': end}


def recall_focus(
    message: Optional[str],
    topics: list[str],
    intent_summaries: list[str],
) -> str:
    """上游 `recallFocus(message, topics, intentSummaries)`：有界的查询线索。"""
    cues: list[str] = []
    seen: set[str] = set()
    for raw in [*intent_summaries, *topics]:
        trimmed = raw.strip() if isinstance(raw, str) else ''
        if not trimmed or trimmed in seen:
            continue
        seen.add(trimmed)
        cues.append(trimmed)
        if len(cues) >= _FOCUS_CUE_LIMIT:
            break
    parts: list[str] = []
    if isinstance(message, str):
        head = message.strip()[:_FOCUS_MESSAGE_CHARS]
        if head:
            parts.append(head)
    parts.extend(cue[:_FOCUS_CUE_CHARS] for cue in cues)
    return '\n'.join(parts)
