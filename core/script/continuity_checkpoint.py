"""上游 `upstream/src/script/continuity-checkpoint.ts`（21 行，v1.0.1-beta6-rebuild）的 Python 对应物。

两个不变量，都写在注释里了：

1. **增量整理按完整前缀推进**（`compaction_prefix`）：
   上游注释 ——
   ``A checkpoint consumes a contiguous prefix. Keeping the newest suffix here
   would permanently skip the older evidence when lastEntryId advances.``
   所以这里从头往后连续取，直到预算用尽就**停**，绝不"跳过中间、抓最新几条"。
   第一条永远入选（即使它自己就超预算），保证前缀至少前进一格，不会死循环。

2. **空 scene / arc 结果不确认检查点**（`assert_continuity_review`）：
   上游注释 ——
   ``An empty/silent provider response must leave the evidence available for retry.``
   scene 与 arc 的 summary 缺一即抛错，检查点保留待重试。

命名：函数 `camelCase → snake_case`；本模块的入参 `decision` 是
`core.types.CompactionDecision`（dict，字段保持 types.py 里的 snake_case 形状）。
"""

from __future__ import annotations

from typing import Any

from ..types import CompactionDecision, ScriptEntry

__all__ = ['compaction_prefix', 'assert_continuity_review']

#: 上游 `assertContinuityReview` 抛出的错误消息（逐字照抄，便于对日志定位）。
CONTINUITY_REVIEW_ERROR = (
    'Continuity review needs both scene and arc summaries; checkpoint retained for retry'
)


def compaction_prefix(entries: list[ScriptEntry], budget: int) -> list[ScriptEntry]:
    """上游 `compactionPrefix(entries, budget)`。

    从头部起连续取条目，累计 `len(content)` 不超过 `budget`；一旦下一条会超预算就
    停止（保留完整前缀）。返回的是原条目对象本身，不做拷贝。
    """
    selected: list[ScriptEntry] = []
    used = 0
    for entry in entries:
        content = entry.get('content') or ''
        if selected and used + len(content) > budget:
            break
        selected.append(entry)
        used += len(content)
    return selected


def _trimmed(value: Any) -> str:
    """等价上游 `value?.trim()`：非字符串（含 None）视作空。"""
    return value.strip() if isinstance(value, str) else ''


def assert_continuity_review(decision: CompactionDecision) -> None:
    """上游 `assertContinuityReview(decision)`：scene 与 arc 的 summary 都必须非空。

    校验失败抛 `ValueError`（对应上游 `throw new Error(...)`），
    调用方据此**不推进检查点**，把证据留给下一轮重试。
    """
    scene = decision.get('scene') if isinstance(decision, dict) else None
    arc = decision.get('arc') if isinstance(decision, dict) else None
    scene_summary = scene.get('summary') if isinstance(scene, dict) else None
    arc_summary = arc.get('summary') if isinstance(arc, dict) else None
    if not _trimmed(scene_summary) or not _trimmed(arc_summary):
        raise ValueError(CONTINUITY_REVIEW_ERROR)
