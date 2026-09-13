"""意图生命周期（上游 `src/script/intent-lifecycle.ts` 的 Python 对应物）。

上游：Koishi / TypeScript，v1.0.1-beta6-rebuild。

这些任务**有自己的执行器**，绝不由实时叙事回合顺手结清；把它们排除在「实时回合
消费的意图」之外，延迟的/后台的意图才不会被一次普通对话误标为已完成。

命名约定（docs/PORT_PLAN.md §2）：camelCase 转 snake_case；读取侧两种拼写都接受。
"""

from __future__ import annotations

from typing import Any

# 上游 `['split-message', 'browser-research', 'proactive-check', 'active-consequence']`
_SELF_EXECUTED_INTENT_TYPES = (
    'split-message',
    'browser-research',
    'proactive-check',
    'active-consequence',
)


def _get(mapping: Any, *keys: str, default: Any = None) -> Any:
    """按顺序读取第一个存在的键（兼容 snake_case / camelCase 两种拼写）。"""
    if isinstance(mapping, dict):
        for key in keys:
            if key in mapping:
                return mapping[key]
    return default


def live_narrative_intents(intents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """上游 `liveNarrativeIntents()`：过滤出实时叙事回合真正消费的意图。"""
    return [intent for intent in intents
            if _get(intent, 'type') not in _SELF_EXECUTED_INTENT_TYPES]


def consumed_live_intent_ids(intents: list[dict[str, Any]]) -> list[Any]:
    """上游 `consumedLiveIntentIds()`：本回合真正被消费的意图 id。

    ``follow-up-commitment`` 由投递回执结清，因此不算被实时回合消费。
    """
    return [_get(intent, 'id') for intent in live_narrative_intents(intents)
            if _get(intent, 'type') != 'follow-up-commitment']
