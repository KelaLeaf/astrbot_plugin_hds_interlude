"""上游 `upstream/src/script/episode-index.ts`（75 行，v1.0.1-beta6-rebuild）的 Python 对应物。

对**已经过可见性过滤**的原文条目做「可重建」的导航索引。上游注释 ——

    Rebuildable navigation over already visibility-filtered original entries.
    A frame identifies a scene; neither time gaps nor message counts split it.
    Old entries without a frame remain singleton anchors.

四条不变量（本模块的全部意义）：
1. **可重建**：索引完全由输入行推导，同样的输入必须得到同样的分组
   （`buildEpisodeIndex` 可被 JSON 往返后的输入复现）。
2. **分组只认 frame / checkpoint 来源**：既不按时间间隔、也不按消息条数切场景；
   没有 frame 的老条目各自退化为单例锚点；不同关系（participantId）永不合并。
3. **原文回取优先保留命中段再补邻域**：`episode_excerpt` 先放 anchor 行本身，
   再按 ±1 / ±2 的邻近顺序补；**来源 ID 只描述结果里真的包含的文本**，
   绝不指向被省略的片段。
4. **标签是字面导航 span，不是生成的摘要或事实**：`grounded_episode_tags`
   只保留能在原文里字面找到的值；`episode_tag_score` 把"仅有散文相似度"的
   排序权重压到 0.8——它刻意不给更高权重，避免标签以语义联想凌驾于原文之上。

命名与形态
----------
- 上游 `EpisodeSource` / 内联 `checkpoint` → 本模块的 `EpisodeSource` TypedDict
  （`frameId → frame_id`，内层 `sceneId/firstEntryId/lastEntryId` → snake_case）。
- `RecallSpan` 从 `recall_navigation` 复用（上游也是跨文件 type import）。
- 返回对象字段 camelCase → snake_case：`{sourceEntryIds, content}` →
  `{'source_entry_ids', 'content'}`。
- `content.length` → `len(content)`：BMP 文本与上游 UTF-16 码元计数一致，
  补充平面字符（emoji）会差 1；这里保留 Python 语义，使 `content` 与
  `start`/`end`（见 `original_window`）自洽。
- `Map` → `dict`；`Array<[number, EpisodeSource]>` → `list[tuple[int, EpisodeSource]]`
  （也接受 JSON 往返后的 `list[list[...]]`，索引取值不依赖元组类型）。
"""

from __future__ import annotations

import json
from typing import Any, Optional, TypedDict, Union

from .recall_navigation import RecallSpan, index_original, original_window, score_original

__all__ = [
    'EpisodeSource',
    'EpisodeCheckpoint',
    'EpisodeRow',
    'build_episode_index',
    'episode_excerpt',
    'grounded_episode_tags',
    'episode_tag_score',
]


class EpisodeCheckpoint(TypedDict, total=False):
    """`EpisodeSource.checkpoint` 的内联对象形状。"""

    scene_id: int
    first_entry_id: int
    last_entry_id: int


class EpisodeSource(TypedDict, total=False):
    """一条可被导航的原文条目。"""

    content: str
    occurred_at: str
    participant_id: str
    kind: str
    frame_id: str
    tags: list[str]
    #: 记忆化字段：`episode_excerpt` 首次使用时会就地写入（上游 `row.spans ??=`）。
    spans: list[RecallSpan]
    checkpoint: EpisodeCheckpoint


#: `[id, source]`（list 也是合法输入，便于 JSON 往返后的复现测试）。
EpisodeRow = Union[tuple[int, EpisodeSource], list[Any]]

#: 上游 `Number.isSafeInteger` 的边界。
_MAX_SAFE_INTEGER = 2 ** 53 - 1

#: 上游 `episodeExcerpt` 的预算下限（`remaining < 40` 即停止）。
_MIN_REMAINING = 40
#: 上游 `originalWindow(..., Math.max(0, remaining - 180), ...)` 的头部保留量。
_WINDOW_RESERVE = 180
#: 上游"非锚点行要留 100 余量"的判断。
_NEIGHBOR_HEADROOM = 100
#: 上游 `Array<[number, EpisodeSource]>` 的默认预算。
_DEFAULT_EXCERPT_BUDGET = 4000
#: 上游 `groundedEpisodeTags` 的取值上限。
_TAG_LIMIT = 8
#: 上游 `groundedEpisodeTags` 检查的键（顺序即结果键的插入顺序）。
_TAG_KEYS = ('people', 'places', 'objects', 'topics', 'commitments', 'outcomes', 'dates')


def _is_safe_integer(value: Any) -> bool:
    """等价 `Number.isSafeInteger`：bool / float 一律不算。"""
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and -_MAX_SAFE_INTEGER <= value <= _MAX_SAFE_INTEGER
    )


def build_episode_index(rows: list[EpisodeRow]) -> dict[int, list[int]]:
    """上游 `buildEpisodeIndex(rows)`：条目 id → 同组条目 id 列表（组内保持行序）。

    分组键是 `JSON.stringify([participantId, 'scene:<id>' | frameId | 'entry:<id>'])`。
    checkpoint 来源优先于 frame：只要某个 checkpoint 的 `[firstEntryId, lastEntryId]`
    覆盖了该条目，就用 `scene:<sceneId>` 作组键（老条目因此能被来源化，
    而不是各自变成单例）。
    """
    checkpoints: list[EpisodeCheckpoint] = []
    for row in rows:
        candidate = row[1].get('checkpoint') if isinstance(row[1], dict) else None
        if not isinstance(candidate, dict):
            continue
        if _is_safe_integer(candidate.get('first_entry_id')) and _is_safe_integer(candidate.get('last_entry_id')):
            checkpoints.append(candidate)

    groups: dict[str, list[int]] = {}
    for entry_id, source in rows:
        checkpoint = None
        for candidate in checkpoints:
            if candidate['first_entry_id'] <= entry_id <= candidate['last_entry_id']:
                checkpoint = candidate
                break
        if checkpoint is not None:
            scope = f"scene:{checkpoint.get('scene_id')}"
        else:
            scope = source.get('frame_id') or f'entry:{entry_id}'
        key = _group_key(source.get('participant_id'), scope)
        groups.setdefault(key, []).append(entry_id)

    by_entry: dict[int, list[int]] = {}
    for ids in groups.values():
        for entry_id in ids:
            by_entry[entry_id] = ids
    return by_entry


def _group_key(participant_id: Any, scope: str) -> str:
    """`JSON.stringify([participantId, scope])` 的等价物（仅用于等值分组）。"""
    return json.dumps([participant_id, scope], ensure_ascii=False)


def _owner(kind: Any) -> str:
    """条目类型 → 面向 prompt 的发言归属标签（上游三元链逐字照抄）。"""
    if kind == 'user-message':
        return 'user-delivered-message'
    if kind == 'script':
        return 'protagonist-narrative'
    if kind == 'group-message':
        return 'group-member-message'
    return 'protagonist-delivered-message'


def episode_excerpt(
    rows: list[EpisodeRow],
    anchor_id: int,
    budget: int = _DEFAULT_EXCERPT_BUDGET,
    query_keys: Optional[list[str]] = None,
) -> Optional[dict[str, Any]]:
    """上游 `episodeExcerpt(rows, anchorId, budget = 4000, queryKeys = [])`。

    保留真正的命中行，再按"最近优先"补原始邻居；返回
    `{'source_entry_ids': [...], 'content': '...'}`，其中 `source_entry_ids`
    只列出**内容真的进了结果**的条目（与上游注释一致：
    ``Source ids always describe text included in the result, not an omitted part
    of the episode.``）。找不到 anchor 时返回 None（上游 `undefined`）。
    """
    keys = query_keys if query_keys is not None else []
    position = -1
    for i, row in enumerate(rows):
        if row[0] == anchor_id:
            position = i
            break
    if position < 0:
        return None

    selected: dict[int, str] = {}
    remaining = budget
    for index in (position, position + 1, position - 1, position + 2, position - 2):
        if index < 0 or index >= len(rows):
            continue
        row = rows[index]
        source = row[1]
        content = source.get('content') or ''
        if remaining < _MIN_REMAINING:
            continue
        if index != position and len(content) + _NEIGHBOR_HEADROOM > remaining:
            continue
        spans = source.get('spans')
        if spans is None:
            spans = index_original(content)
            source['spans'] = spans
        hit = score_original(keys, spans)
        window = original_window(content, spans, hit['index'], max(0, remaining - _WINDOW_RESERVE), keys)
        partial = window['start'] > 0 or window['end'] < len(content)
        mark = ''
        if partial:
            mark = (
                f"; source-view UTF-16 [{window['start']},{window['end']})/{len(content)}"
                ', not a complete event'
            )
        text = f"[{source.get('occurred_at')}; {_owner(source.get('kind'))}; entry:{row[0]}{mark}] {window['content']}"
        if index != position and len(text) > remaining:
            continue
        selected[row[0]] = text
        remaining -= len(text) + 1

    ordered = [(entry_id, source) for entry_id, source in rows if entry_id in selected]
    return {
        'source_entry_ids': [entry_id for entry_id, _ in ordered],
        'content': '\n'.join(selected[entry_id] for entry_id, _ in ordered),
    }


def grounded_episode_tags(content: str, draft: dict[str, Any]) -> dict[str, list[str]]:
    """上游 `groundedEpisodeTags(content, draft)`。

    标签是**字面导航 span**，绝不是生成的摘要或事实：只有满足
    「是字符串、trim 后 ≥2 字符、长度 ≤100、且在原文里字面出现」的值才留下，
    去重后每个键最多 8 条；空结果的键不写进返回对象。
    """
    result: dict[str, list[str]] = {}
    for key in _TAG_KEYS:
        values = draft.get(key)
        if not isinstance(values, list):
            continue
        grounded: list[str] = []
        seen: set[str] = set()
        for value in values:
            if not isinstance(value, str):
                continue
            if len(value.strip()) < 2 or len(value) > 100:
                continue
            if value not in content or value in seen:
                continue
            seen.add(value)
            grounded.append(value)
            if len(grounded) >= _TAG_LIMIT:
                break
        if grounded:
            result[key] = grounded
    return result


def episode_tag_score(query: str, tags: Optional[list[str]] = None) -> float:
    """上游 `episodeTagScore(query, tags = [])`：**仅凭散文相似度的排序权重**。

    标签命中给 0.8，未命中给 0——刻意低于"原文命中"的权重，
    以免标签以语义联想凌驾于原文之上。
    """
    for tag in (tags if tags is not None else []):
        if tag in query:
            return 0.8
    return 0
