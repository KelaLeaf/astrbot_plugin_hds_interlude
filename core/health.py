"""运行期滚动健康指标（上游 `src/health.ts`，P3 的健康面板数据源）。

**纯内存、不持久化**：插件重载即归零——这正是"自上次重载以来"这个视图的语义。
每个故事一份状态；`snapshot()` 额外算出五个派生比率（成功率 / 结构化缺失率 /
缓存命中率 / 主动联系率 / 延迟中位数）。

键名约定：本模块产出的是**控制台 API 的 wire 形状**，因此键名逐字 camelCase
（`narrativeTotal` / `replyModes` …）；Python 内部标识符保持 snake_case。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Optional

__all__ = ['HealthMonitor', 'MAX_LATENCIES']

#: 上游 `MAX_LATENCIES`：延迟样本的滚动窗口（超过就丢最旧的）。
MAX_LATENCIES = 200

_REPLY_MODES = ('immediate', 'none', 'delayed', 'noDelivery')


def _new_state() -> dict[str, Any]:
    return {
        'narrativeTotal': 0,
        'narrativeFailed': 0,
        'structureMissing': 0,
        'recoverySaved': 0,
        'replyModes': {mode: 0 for mode in _REPLY_MODES},
        'sideTaskTotal': 0,
        'sideTaskFailed': 0,
        'proactiveTotal': 0,
        'proactiveSent': 0,
        'inputTokens': 0,
        'cachedTokens': 0,
        'latenciesMs': [],
        'startedAt': datetime.now(tz=timezone.utc),
    }


class HealthMonitor:
    """上游 `HealthMonitor`：7 个记录点 + 快照（含 5 个派生比率）。"""

    def __init__(self) -> None:
        self.stories: dict[str, dict[str, Any]] = {}

    def _state(self, story_id: str) -> dict[str, Any]:
        state = self.stories.get(story_id)
        if state is None:
            state = _new_state()
            self.stories[story_id] = state
        return state

    def record_narrative_complete(self, story_id: str, latency_ms: float, reply_mode: str) -> None:
        """一次主叙事回合完成：计数 + 延迟样本 + 回复模式分桶。

        ⚠️ 上游的分桶是 **else 分支落到 `noDelivery`**——未知/缺失的 `replyMode`
        计为"没有投递"（而不是"立即回复"）。这条语义别顺手"修好"：
        健康面板的"投递率"就是靠它把协议异常暴露出来的。
        """
        state = self._state(story_id)
        state['narrativeTotal'] += 1
        latencies = state['latenciesMs']
        latencies.append(float(latency_ms))
        if len(latencies) > MAX_LATENCIES:
            latencies.pop(0)
        bucket = reply_mode if reply_mode in _REPLY_MODES and reply_mode != 'noDelivery' else 'noDelivery'
        if reply_mode == 'immediate':
            bucket = 'immediate'
        elif reply_mode == 'none':
            bucket = 'none'
        elif reply_mode == 'delayed':
            bucket = 'delayed'
        state['replyModes'][bucket] += 1

    def record_narrative_failed(self, story_id: str) -> None:
        """一次主叙事失败（重试耗尽后仍失败）。"""
        self._state(story_id)['narrativeFailed'] += 1

    def record_structure_missing(self, story_id: str) -> None:
        """结构化可见回复缺失（触发重写的那一类）。"""
        self._state(story_id)['structureMissing'] += 1

    def record_recovery_saved(self, story_id: str) -> None:
        """重写/兜底救回来的一次（分子意义上是"本该丢、但没丢"）。"""
        self._state(story_id)['recoverySaved'] += 1

    def record_side_task(self, story_id: str, ok: bool) -> None:
        """侧端任务（压缩 / 时间导演 / 日程预排 / 世界播种 …）一次结果。"""
        state = self._state(story_id)
        state['sideTaskTotal'] += 1
        if not ok:
            state['sideTaskFailed'] += 1

    def record_proactive(self, story_id: str, sent: bool) -> None:
        """主动联系候选一次（`sent` = 真的发出去了）。"""
        state = self._state(story_id)
        state['proactiveTotal'] += 1
        if sent:
            state['proactiveSent'] += 1

    def record_tokens(self, story_id: str, input_tokens: float, cached_tokens: float) -> None:
        """Token 用量（输入总量与其中的缓存命中量）。"""
        state = self._state(story_id)
        state['inputTokens'] += int(input_tokens or 0)
        state['cachedTokens'] += int(cached_tokens or 0)

    def snapshot(self, story_id: str) -> dict[str, Any]:
        """上游 `snapshot`：原始计数 + 五个派生比率（无样本时的保守取值照抄上游）。"""
        state = self._state(story_id)
        total = state['narrativeTotal'] + state['narrativeFailed']
        latencies = sorted(state['latenciesMs'])
        median = latencies[len(latencies) // 2] if latencies else 0
        return {
            'narrativeTotal': state['narrativeTotal'],
            'narrativeFailed': state['narrativeFailed'],
            'structureMissing': state['structureMissing'],
            'recoverySaved': state['recoverySaved'],
            'replyModes': dict(state['replyModes']),
            'sideTaskTotal': state['sideTaskTotal'],
            'sideTaskFailed': state['sideTaskFailed'],
            'proactiveTotal': state['proactiveTotal'],
            'proactiveSent': state['proactiveSent'],
            'inputTokens': state['inputTokens'],
            'cachedTokens': state['cachedTokens'],
            'latenciesMs': list(state['latenciesMs']),
            'sinceAt': state['startedAt'].isoformat(),
            'successRate': (state['narrativeTotal'] / total) if total else 1,
            'structureMissingRate': (
                state['structureMissing'] / state['narrativeTotal']
            ) if state['narrativeTotal'] else 0,
            'cacheHitRate': (
                state['cachedTokens'] / state['inputTokens']
            ) if state['inputTokens'] else 0,
            'proactiveRate': (
                state['proactiveSent'] / state['proactiveTotal']
            ) if state['proactiveTotal'] else 0,
            'medianLatencyMs': median,
        }

    def all(self) -> dict[str, dict[str, Any]]:
        """上游 `all()`：每个故事一份快照。"""
        return {story_id: self.snapshot(story_id) for story_id in self.stories}


def format_health_lines(snapshot: Mapping[str, Any]) -> list[str]:
    """把快照渲染成 `hdsi_status` 的中文健康段（6 行）。

    这是**本移植版新增的表述**（上游只把数据交给 Console 面板）：命令行长文本
    在 QQ 上不好读，所以给一份人话摘要，数值与面板同源。
    """
    if not isinstance(snapshot, Mapping):
        return []
    modes = snapshot.get('replyModes') or {}
    total = int(snapshot.get('narrativeTotal') or 0) + int(snapshot.get('narrativeFailed') or 0)
    return [
        '回合 %d 次（失败 %d 次，成功率 %.0f%%）'
        % (total, int(snapshot.get('narrativeFailed') or 0), float(snapshot.get('successRate') or 0) * 100),
        '回复模式：立即 %d / 不回 %d / 延迟 %d / 未投递 %d'
        % (
            int(modes.get('immediate') or 0), int(modes.get('none') or 0),
            int(modes.get('delayed') or 0), int(modes.get('noDelivery') or 0),
        ),
        '结构化缺失 %d 次（%.0f%%），挽回 %d 次'
        % (
            int(snapshot.get('structureMissing') or 0),
            float(snapshot.get('structureMissingRate') or 0) * 100,
            int(snapshot.get('recoverySaved') or 0),
        ),
        '侧端任务 %d 次（失败 %d 次）'
        % (int(snapshot.get('sideTaskTotal') or 0), int(snapshot.get('sideTaskFailed') or 0)),
        '主动联系 %d 次（送出 %d 次）'
        % (int(snapshot.get('proactiveTotal') or 0), int(snapshot.get('proactiveSent') or 0)),
        '缓存命中 %.0f%%，中位延迟 %.0fms'
        % (float(snapshot.get('cacheHitRate') or 0) * 100, float(snapshot.get('medianLatencyMs') or 0)),
    ]
