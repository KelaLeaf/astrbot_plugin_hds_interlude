"""Token 统计：范围解析、账本增量、聚合与命中率（本移植版新增）。

纯函数层的用例（`core/token_stats.py`）；写入侧（`chunk11.record_token_usage`）
的服务级用例在 `test_service_chunk11.py` 里。
"""

from __future__ import annotations

import pathlib
import sys
import unittest
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from plugin.core.service.chunk11 import ServiceChunk11  # noqa: E402
from plugin.core.token_stats import (  # noqa: E402
    TOKEN_RANGES,
    day_key,
    merge_usage,
    normalize_range,
    normalize_usage_record,
    range_bounds,
    summarize_usage,
    usage_row_key,
)

ANCHOR = date(2026, 9, 30)


class RangeTests(unittest.TestCase):
    def test_presets_include_today_and_span_the_documented_window(self):
        self.assertEqual(range_bounds('day', ANCHOR), {'from': '2026-09-30', 'to': '2026-09-30'})
        self.assertEqual(range_bounds('week', ANCHOR), {'from': '2026-09-24', 'to': '2026-09-30'})
        self.assertEqual(range_bounds('month', ANCHOR), {'from': '2026-09-01', 'to': '2026-09-30'})
        self.assertEqual(TOKEN_RANGES, ('day', 'week', 'month', 'custom'))

    def test_custom_range_is_inclusive_swaps_reversed_bounds_and_caps_at_a_year(self):
        self.assertEqual(
            range_bounds('custom', ANCHOR, '2026-09-01', '2026-09-15'),
            {'from': '2026-09-01', 'to': '2026-09-15'},
        )
        # 反了就交换（用户手填日期时很容易反）。
        self.assertEqual(
            range_bounds('custom', ANCHOR, '2026-09-15', '2026-09-01'),
            {'from': '2026-09-01', 'to': '2026-09-15'},
        )
        # 只给一头：另一头补今天。
        self.assertEqual(range_bounds('custom', ANCHOR, '2026-09-01', ''), {'from': '2026-09-01', 'to': '2026-09-30'})
        # 上限 366 天：防止一次查询把整张表拉进内存。
        capped = range_bounds('custom', ANCHOR, '2000-01-01', '2026-09-30')
        self.assertEqual((date.fromisoformat(capped['to']) - date.fromisoformat(capped['from'])).days, 365)

    def test_unknown_range_falls_back_to_day(self):
        self.assertEqual(normalize_range('nonsense'), 'day')
        self.assertEqual(normalize_range(''), 'day')
        self.assertEqual(normalize_range(None), 'day')
        self.assertEqual(normalize_range('WEEK'), 'week')
        self.assertEqual(range_bounds('nonsense', ANCHOR), {'from': '2026-09-30', 'to': '2026-09-30'})

    def test_day_key_follows_server_local_time_but_honours_an_explicit_offset(self):
        stamp = datetime(2026, 9, 30, 23, 30, tzinfo=timezone.utc)
        # 不给偏移 → 服务器本地日期（这条断言不依赖跑测试的机器在哪个时区）。
        self.assertEqual(day_key(stamp), stamp.astimezone().date().isoformat())
        # 给了偏移 → 按那个时区切日（东八区此时已经是第二天）。
        self.assertEqual(day_key(stamp, timedelta(hours=8)), '2026-10-01')
        self.assertEqual(day_key(stamp, timedelta(hours=-5)), '2026-09-30')


class RecordTests(unittest.TestCase):
    def test_records_without_any_token_field_are_ignored(self):
        self.assertIsNone(normalize_usage_record(None))
        self.assertIsNone(normalize_usage_record({'task': 'main', 'model': 'm'}))
        self.assertIsNone(normalize_usage_record('junk'))

    def test_a_call_without_tokens_is_recorded_when_it_says_how_many_calls(self):
        """v1.9.4：网关 / 经宿主 Provider 的调用不回 token，但这一次调用要数上。

        真机症状：模型全部来自 AstrBot Provider 时「Token 统计」页连调用次数都是 0。
        """
        record = normalize_usage_record({'task': '主叙事', 'model': 'm', 'calls': 1})
        self.assertIsNotNone(record)
        self.assertEqual(
            (record['inputTokens'], record['outputTokens'], record['cachedTokens'], record['calls']),
            (0, 0, 0, 1),
        )
        # 只说"没 token"、又不说调了几次 → 仍然无效（上游那条纯身份记录不许凭空建行）。
        self.assertIsNone(normalize_usage_record({'task': '主叙事', 'model': 'm', 'calls': 0}))

    def test_a_record_always_counts_one_call_and_accepts_both_spellings(self):
        record = normalize_usage_record(
            {'task': '压缩', 'model': 'deepseek-chat', 'provider_label': '连接A',
             'inputTokens': 1000, 'output_tokens': 200, 'cached_input_tokens': 400},
            datetime(2026, 9, 30, tzinfo=timezone.utc), story_id='s1',
        )
        self.assertEqual(record['day'], '2026-09-30')
        self.assertEqual(record['storyId'], 's1')
        self.assertEqual(
            (record['inputTokens'], record['outputTokens'], record['cachedTokens'], record['calls']),
            (1000, 200, 400, 1),
        )

    def test_row_key_is_the_documented_aggregation(self):
        self.assertEqual(
            usage_row_key({'day': '2026-09-30', 'storyId': 's', 'task': 'main', 'model': 'm'}),
            ('2026-09-30', 's', 'main', 'm'),
        )

    def test_merge_accumulates_and_keeps_the_existing_row_identity(self):
        existing = {
            'id': 7, 'day': '2026-09-30', 'storyId': 's', 'task': 'main', 'model': 'm',
            'provider': '连接A', 'inputTokens': 100, 'outputTokens': 20, 'cachedTokens': 10, 'calls': 1,
        }
        delta = normalize_usage_record(
            {'task': 'main', 'model': 'm', 'input_tokens': 50, 'output_tokens': 5},
            datetime(2026, 9, 30, tzinfo=timezone.utc), story_id='s',
        )
        merged = merge_usage(existing, delta)
        self.assertEqual(merged['id'], 7)
        self.assertEqual(merged['inputTokens'], 150)
        self.assertEqual(merged['outputTokens'], 25)
        self.assertEqual(merged['cachedTokens'], 10)
        self.assertEqual(merged['calls'], 2)
        self.assertEqual(merged['provider'], '连接A')
        self.assertIn('updatedAt', merged)


class SummaryTests(unittest.TestCase):
    ROWS = [
        {'day': '2026-09-29', 'storyId': 's', 'task': 'main', 'model': 'deepseek-chat',
         'provider': 'A', 'inputTokens': 1000, 'outputTokens': 400, 'cachedTokens': 400, 'calls': 2},
        {'day': '2026-09-30', 'storyId': 's', 'task': '压缩', 'model': 'flash',
         'provider': 'B', 'inputTokens': 500, 'outputTokens': 100, 'cachedTokens': 0, 'calls': 1},
        {'day': '2026-08-01', 'storyId': 's', 'task': 'main', 'model': 'flash',
         'provider': 'B', 'inputTokens': 9999, 'outputTokens': 0, 'cachedTokens': 0, 'calls': 5},
    ]

    def test_bounds_filter_out_old_rows_and_fill_empty_days(self):
        summary = summarize_usage(self.ROWS, range_bounds('week', date(2026, 9, 30)))
        self.assertEqual(summary['totals']['inputTokens'], 1500, '8 月那行必须被范围挡掉')
        self.assertEqual(summary['totals']['calls'], 3)
        self.assertEqual(len(summary['series']), 7, '没有调用的日子要补 0，折线图才不断')
        self.assertEqual(summary['series'][0], {
            'day': '2026-09-24', 'inputTokens': 0, 'outputTokens': 0, 'cachedTokens': 0,
            'calls': 0, 'hitRate': 0.0,
        })
        self.assertEqual(summary['series'][-1]['day'], '2026-09-30')

    def test_totals_and_per_bucket_hit_rates(self):
        summary = summarize_usage(self.ROWS, range_bounds('week', date(2026, 9, 30)))
        self.assertAlmostEqual(summary['totals']['hitRate'], 400 / 1500)
        self.assertEqual(summary['totals']['totalTokens'], 2000)
        by_model = {item['model']: item for item in summary['byModel']}
        self.assertAlmostEqual(by_model['deepseek-chat']['hitRate'], 0.4)
        self.assertEqual(by_model['deepseek-chat']['provider'], 'A')
        by_task = {item['task']: item for item in summary['byTask']}
        self.assertEqual(by_task['压缩']['calls'], 1)
        self.assertEqual(by_task['压缩']['hitRate'], 0.0, '输入为 0 或没有缓存时命中率是 0，不是 100%')

    def test_rows_without_a_model_or_task_land_in_a_labelled_bucket(self):
        summary = summarize_usage([{'day': '2026-09-30', 'inputTokens': 10, 'outputTokens': 1, 'calls': 1}])
        self.assertEqual(summary['byModel'][0]['model'], '（未标注）')
        self.assertEqual(summary['byTask'][0]['task'], '（未标注）')

    def test_summary_of_nothing_is_all_zeroes(self):
        summary = summarize_usage([], range_bounds('day', ANCHOR))
        self.assertEqual(summary['totals']['inputTokens'], 0)
        self.assertEqual(summary['totals']['hitRate'], 0.0)
        self.assertEqual(summary['byModel'], [])
        self.assertEqual(summary['series'], [{
            'day': '2026-09-30', 'inputTokens': 0, 'outputTokens': 0, 'cachedTokens': 0,
            'calls': 0, 'hitRate': 0.0,
        }])


class RecordingTests(unittest.IsolatedAsyncioTestCase):
    """写入侧：`chunk11.record_token_usage` 先建行、再累加，失败只 warn。"""

    class _Host(ServiceChunk11):
        def __init__(self) -> None:
            self.rows: list[dict] = []
            self.reports: list[tuple[str, ...]] = []
            self._next = 0

        def now(self):
            return datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)

        async def db_get(self, table, query=None, options=None):
            return [
                dict(row) for row in self.rows
                if all(row.get(key) == value for key, value in (query or {}).items())
            ]

        async def db_create(self, table, row):
            self._next += 1
            stored = {**row, 'id': self._next}
            self.rows.append(stored)
            return stored

        async def db_set(self, table, query, patch):
            for row in self.rows:
                if row.get('id') == query.get('id'):
                    row.update(patch)

        def report_standalone(self, level, message, *args, **_kwargs):
            self.reports.append((level, message % args if args else message))

    async def test_a_record_creates_one_row_then_accumulates_into_it(self):
        host = self._Host()
        record = {'task': '主叙事', 'model': 'deepseek-chat', 'provider_label': '连接A',
                  'input_tokens': 100, 'output_tokens': 10, 'cached_input_tokens': 50}
        self.assertTrue(await host.record_token_usage(record, 's1'))
        self.assertTrue(await host.record_token_usage(record, 's1'))
        self.assertEqual(len(host.rows), 1, '同一 (day, storyId, task, model) 只该有一行')
        self.assertEqual(host.rows[0]['inputTokens'], 200)
        self.assertEqual(host.rows[0]['calls'], 2)

    async def test_a_record_without_tokens_is_skipped_and_failures_only_warn(self):
        host = self._Host()
        self.assertFalse(await host.record_token_usage({'task': '主叙事', 'model': 'm'}, 's1'))
        self.assertEqual(host.rows, [])

        class _Broken(self._Host):
            async def db_create(self, table, row):
                raise RuntimeError('磁盘满了')

        broken = _Broken()
        self.assertFalse(await broken.record_token_usage(
            {'task': '主叙事', 'model': 'm', 'input_tokens': 1}, 's1',
        ))
        self.assertTrue(any('记账失败' in message for _level, message in broken.reports), broken.reports)

    async def test_a_call_without_tokens_lands_in_the_ledger_with_its_count(self):
        """v1.9.4：没有 token 也要有"这一天这个任务调了几次"。"""
        host = self._Host()
        record = {'task': '主叙事', 'model': 'm', 'provider_label': 'AstrBot · p', 'calls': 1}
        self.assertTrue(await host.record_token_usage(record, 's1'))
        self.assertTrue(await host.record_token_usage(record, 's1'))
        self.assertEqual(len(host.rows), 1)
        self.assertEqual(host.rows[0]['calls'], 2)
        self.assertEqual(host.rows[0]['inputTokens'], 0)
        self.assertEqual(host.rows[0]['provider'], 'AstrBot · p')


if __name__ == '__main__':
    unittest.main()
