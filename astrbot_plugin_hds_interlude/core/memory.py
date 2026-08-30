"""记忆：场景压缩、长期事实与设定演化。

从上游 `src/service.ts` 中 Compaction/长期事实/StatePatch 的概念移植子集。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .model import ModelConfig, ChatMessage, chat_completion_json
from .types import (
    ContinuitySnapshot,
    NarrativeContext,
    NarrativeFact,
    StatePatchDraft,
    StorySetting,
)

_UTC = timezone.utc

_COMPACTION_INSTRUCTION = (
    "Compress completed scenes into concise continuity notes while preserving causality, "
    "promises, unresolved matters, and gradual character change. Respond with a JSON object "
    'with keys: "current" (string), "next" (array of strings), "recent" (array of strings), '
    '"salient" (array of strings).'
)


async def compact_scene(
    entries_with_content: list[tuple[str, str]],
    cfg: ModelConfig,
    *,
    astrobot_call=None,
    style_prompt: str = "Concise, factual, chronological, and concrete.",
) -> Optional[ContinuitySnapshot]:
    """把已完成的场景压缩为连续性快照。"""
    if not entries_with_content:
        return None
    lines = [f"[{stamp}] {content}" for stamp, content in entries_with_content[-80:]]
    user_text = "Recent script entries:\n" + "\n".join(lines)
    messages = [
        ChatMessage(role="system", content=_COMPACTION_INSTRUCTION),
        ChatMessage(role="user", content=user_text),
    ]
    data, err = await chat_completion_json(messages, cfg, astrobot_call=astrobot_call)
    if err is not None or data is None:
        return None
    return ContinuitySnapshot(
        current=str(data.get("current") or ""),
        next=[str(x) for x in data.get("next") or [] if isinstance(x, str)],
        recent=[str(x) for x in data.get("recent") or [] if isinstance(x, str)],
        salient=[str(x) for x in data.get("salient") or [] if isinstance(x, str)],
    )


_FACT_EXTRACTION_INSTRUCTION = (
    "From the following life script entries, extract durable, still-relevant long-term facts "
    "about the character, relationships, world, and unresolved threads. Respond with a JSON "
    'object with key "facts" which is an array of strings. Keep only facts the character still '
    "needs to remember. Skip trivial or transient detail."
)


async def extract_facts(
    entries_with_content: list[tuple[str, str]],
    cfg: ModelConfig,
    *,
    astrobot_call=None,
) -> list[str]:
    if not entries_with_content:
        return []
    lines = [f"[{stamp}] {content}" for stamp, content in entries_with_content]
    messages = [
        ChatMessage(role="system", content=_FACT_EXTRACTION_INSTRUCTION),
        ChatMessage(role="user", content="\n".join(lines)),
    ]
    data, err = await chat_completion_json(messages, cfg, astrobot_call=astrobot_call)
    if err is not None or data is None:
        return []
    facts = data.get("facts")
    if not isinstance(facts, list):
        return []
    return [str(f) for f in facts if isinstance(f, str) and f.strip()]


_STATE_PATCH_INSTRUCTION = (
    "Based on these entries, propose only well-supported changes to the character's stable "
    "setting. Respond with a JSON object with key 'patches' which is an array of objects "
    'with keys: "target" (character_profile|relationship|world|supporting_cast|location), '
    '"content" (string), "reason" (string), "confidence" (number 0..1), "patch_type" '
    '("minor"|"major"). Require strong evidence; do not propose changes for a single turn.'
)


async def propose_state_patches(
    setting: StorySetting,
    entries_with_content: list[tuple[str, str]],
    cfg: ModelConfig,
    *,
    astrobot_call=None,
) -> list[StatePatchDraft]:
    if not entries_with_content:
        return []
    lines = [f"[{stamp}] {content}" for stamp, content in entries_with_content]
    messages = [
        ChatMessage(role="system", content=_STATE_PATCH_INSTRUCTION),
        ChatMessage(role="user", content="\n".join(lines)),
    ]
    data, err = await chat_completion_json(messages, cfg, astrobot_call=astrobot_call)
    if err is not None or data is None:
        return []
    patches = data.get("patches")
    if not isinstance(patches, list):
        return []
    result: list[StatePatchDraft] = []
    for p in patches:
        if not isinstance(p, dict):
            continue
        target = str(p.get("target") or "")
        if target not in {"character_profile", "relationship", "world", "supporting_cast", "location"}:
            continue
        result.append(
            StatePatchDraft(
                target=target,
                content=str(p.get("content") or ""),
                reason=str(p.get("reason") or ""),
                confidence=float(p.get("confidence") or 0),
                patch_type=str(p.get("patch_type") or "minor"),
            )
        )
    return result


def should_compact(entry_count: int, char_count: int, threshold_entries: int = 16, threshold_chars: int = 10000) -> bool:
    """是否触发后台记忆整理。"""
    if entry_count >= threshold_entries:
        return True
    if char_count >= threshold_chars:
        return True
    return False
