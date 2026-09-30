"""运行期健康指标（上游 `src/health.ts`，rc28 的 P3 面板数据源）。

纯内存、重载归零；这里钉住三件事：分桶语义（未知回复模式 → `noDelivery`）、
五个派生比率的零样本取值、延迟窗口的滚动上限。
"""

from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from plugin.core.health import MAX_LATENCIES, HealthMonitor, format_health_lines  # noqa: E402


class HealthMonitorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.monitor = HealthMonitor()

    def test_reply_mode_buckets_put_anything_unmatched_into_no_delivery(self):
        self.monitor.record_narrative_complete('s', 100, 'immediate')
        self.monitor.record_narrative_complete('s', 120, 'none')
        self.monitor.record_narrative_complete('s', 140, 'delayed')
        # 上游是 else 分支：未知/缺失的模式计为"没有投递"。
        self.monitor.record_narrative_complete('s', 160, 'bogus')
        self.monitor.record_narrative_complete('s', 180, None)
        modes = self.monitor.snapshot('s')['replyModes']
        self.assertEqual(modes, {'immediate': 1, 'none': 1, 'delayed': 1, 'noDelivery': 2})

    def test_success_rate_counts_failures_and_zero_samples_is_optimistic(self):
        self.assertEqual(self.monitor.snapshot('empty')['successRate'], 1, '没有样本时上游给 1')
        self.monitor.record_narrative_complete('s', 10, 'immediate')
        self.monitor.record_narrative_failed('s')
        self.monitor.record_narrative_failed('s')
        self.assertAlmostEqual(self.monitor.snapshot('s')['successRate'], 1 / 3)

    def test_rates_and_median_latency(self):
        self.monitor.record_tokens('s', 1_000, 250)
        for index, latency in enumerate((10, 30, 20)):
            self.monitor.record_narrative_complete('s', latency, 'immediate')
        self.monitor.record_structure_missing('s')
        self.monitor.record_recovery_saved('s')
        self.monitor.record_side_task('s', True)
        self.monitor.record_side_task('s', False)
        self.monitor.record_proactive('s', False)
        self.monitor.record_proactive('s', True)
        snapshot = self.monitor.snapshot('s')
        self.assertAlmostEqual(snapshot['cacheHitRate'], 0.25)
        self.assertAlmostEqual(snapshot['structureMissingRate'], 1 / 3)
        self.assertAlmostEqual(snapshot['proactiveRate'], 0.5)
        self.assertEqual(snapshot['medianLatencyMs'], 20, '中位数取排序后的中间项')
        self.assertEqual(snapshot['sideTaskFailed'], 1)
        self.assertEqual(snapshot['recoverySaved'], 1)

    def test_latency_window_is_bounded_and_drops_the_oldest(self):
        for index in range(MAX_LATENCIES + 5):
            self.monitor.record_narrative_complete('s', index, 'immediate')
        latencies = self.monitor.snapshot('s')['latenciesMs']
        self.assertEqual(len(latencies), MAX_LATENCIES)
        self.assertEqual(latencies[0], 5, '最旧的样本被丢掉')

    def test_all_returns_one_snapshot_per_story(self):
        self.monitor.record_narrative_complete('a', 1, 'immediate')
        self.monitor.record_narrative_complete('b', 2, 'none')
        self.assertEqual(set(self.monitor.all()), {'a', 'b'})

    def test_command_summary_renders_six_chinese_lines(self):
        self.monitor.record_narrative_complete('s', 50, 'immediate')
        lines = format_health_lines(self.monitor.snapshot('s'))
        self.assertEqual(len(lines), 6)
        self.assertTrue(lines[0].startswith('回合 1 次'), lines[0])
        self.assertIn('中位延迟 50ms', lines[5])


if __name__ == '__main__':
    unittest.main()
