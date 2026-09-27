"""记忆维护纯逻辑的测试：相对时间锚定与单轮维护预算（v1.4.0）。"""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone

from plugin.core.memory_maintenance import (
    MaintenanceBudget,
    anchor_relative_times,
    exact_duplicate_groups,
    fact_similarity,
    fact_text_key,
    group_similar_facts,
    normalize_maintenance_decision,
    parse_cn_number,
)


#: 2026-09-27 是周日；用它当锚点，周内回退的方向能被看清。
ANCHOR = datetime(2026, 9, 27, 15, 30, tzinfo=timezone.utc)


class ParseChineseNumberTests(unittest.TestCase):
    def test_handles_units_and_teens(self) -> None:
        for text, expected in (('三', 3), ('十', 10), ('十五', 15), ('二十', 20),
                               ('二十三', 23), ('31', 31)):
            with self.subTest(text=text):
                self.assertEqual(parse_cn_number(text), expected)

    def test_rejects_unknown_text(self) -> None:
        for text in ('', 'x', '一百', '很多'):
            with self.subTest(text=text):
                self.assertIsNone(parse_cn_number(text))


class AnchorRelativeTimesTests(unittest.TestCase):
    def test_day_words_become_dates(self) -> None:
        cases = (
            ('昨天下午她去了面包店', '9月26日下午她去了面包店'),
            ('今日很热', '9月27日很热'),
            ('大前天他说过要还书', '9月24日他说过要还书'),
            ('后天是她的生日', '9月29日是她的生日'),
            ('明天见', '9月28日见'),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(anchor_relative_times(text, ANCHOR), expected)

    def test_counted_days_use_both_number_forms(self) -> None:
        self.assertEqual(anchor_relative_times('三天前下过雨', ANCHOR), '9月24日下过雨')
        self.assertEqual(anchor_relative_times('15天前下过雨', ANCHOR), '9月12日下过雨')
        self.assertEqual(anchor_relative_times('两天后交货', ANCHOR), '9月29日交货')

    def test_week_words_resolve_to_a_day(self) -> None:
        # 锚点是周日：上周三 = 9/16，本周三 = 9/23。
        self.assertEqual(anchor_relative_times('上周三他请了假', ANCHOR), '9月16日他请了假')
        self.assertEqual(anchor_relative_times('本周三他请了假', ANCHOR), '9月23日他请了假')

    def test_bare_last_week_becomes_a_range(self) -> None:
        self.assertEqual(
            anchor_relative_times('上周他们吵了一架', ANCHOR),
            '9月14日到9月20日他们吵了一架',
        )

    def test_month_words(self) -> None:
        self.assertEqual(anchor_relative_times('上个月十五号交房租', ANCHOR), '8月15日交房租')
        # 只写「上个月」不该凭空补出一个日子。
        self.assertEqual(anchor_relative_times('上个月她很忙', ANCHOR), '8月她很忙')
        self.assertEqual(anchor_relative_times('两个月前开始学吉他', ANCHOR), '7月27日开始学吉他')

    def test_year_words(self) -> None:
        self.assertEqual(anchor_relative_times('去年冬天的事', ANCHOR), '2025年冬天的事')
        self.assertEqual(anchor_relative_times('前年搬来这里', ANCHOR), '2024年搬来这里')

    def test_cross_year_dates_keep_the_year(self) -> None:
        # 锚点当年的日期只写月日（读起来自然），跨年才补年份；
        # 事实落库时另存 `knowledge.anchoredAt`，将来读回不靠猜。
        anchor = datetime(2027, 1, 2, 9, 0, tzinfo=timezone.utc)
        self.assertEqual(anchor_relative_times('昨天跨年了', anchor), '1月1日跨年了')
        self.assertEqual(anchor_relative_times('去年十二月', anchor), '2026年十二月')
        old_year = datetime(2027, 3, 1, 9, 0, tzinfo=timezone.utc)
        self.assertEqual(anchor_relative_times('去年冬天', old_year), '2026年冬天')

    def test_vague_words_are_left_alone(self) -> None:
        for text in ('最近她总失眠', '这几天很闷', '有空再说', ''):
            with self.subTest(text=text):
                self.assertEqual(anchor_relative_times(text, ANCHOR), text)

    def test_non_string_input_returns_as_is(self) -> None:
        self.assertEqual(anchor_relative_times(None, ANCHOR), None)  # type: ignore[arg-type]
        self.assertEqual(anchor_relative_times(123, ANCHOR), 123)  # type: ignore[arg-type]

    def test_each_relative_word_is_replaced_once(self) -> None:
        text = '昨天她说今天会来，三天前她还说过明天见'
        anchored = anchor_relative_times(text, ANCHOR)
        for token in ('昨天', '今天', '三天前', '明天'):
            self.assertNotIn(token, anchored)
        self.assertEqual(anchored, '9月26日她说9月27日会来，9月24日她还说过9月28日见')


class MaintenanceBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_call_cap_blocks_further_calls(self) -> None:
        budget = MaintenanceBudget(max_calls=2, min_call_interval_ms=0)
        self.assertEqual(await budget.acquire(), '')
        self.assertEqual(await budget.acquire(), '')
        self.assertIn('调用数', await budget.acquire())
        self.assertEqual(budget.calls, 2)

    async def test_runtime_cap_uses_elapsed_time(self) -> None:
        ticks = iter([0.0, 0.0, 0.0, 700.0])
        budget = MaintenanceBudget(
            max_calls=10, max_runtime_seconds=600, min_call_interval_ms=0,
            clock=lambda: next(ticks, 700.0),
        )
        self.assertEqual(await budget.acquire(), '')
        self.assertIn('时长', await budget.acquire())

    async def test_min_interval_sleeps_between_calls(self) -> None:
        waited: list[float] = []
        now = [0.0]

        async def fake_sleep(seconds: float) -> None:
            waited.append(seconds)

        budget = MaintenanceBudget(
            max_calls=5, min_call_interval_ms=500, sleep=fake_sleep,
            clock=lambda: now[0],
        )
        self.assertEqual(await budget.acquire(), '')
        now[0] = 0.2
        self.assertEqual(await budget.acquire(), '')
        self.assertEqual(len(waited), 1)
        self.assertAlmostEqual(waited[0], 0.3, places=6)

    async def test_zero_limits_are_normalized(self) -> None:
        budget = MaintenanceBudget(max_calls=-3, max_runtime_seconds=-1, min_call_interval_ms=-5)
        self.assertEqual(budget.max_calls, 0)
        self.assertEqual(budget.max_runtime_seconds, 0.0)
        self.assertEqual(budget.min_call_interval_ms, 0)
        self.assertIn('调用数', await budget.acquire())

        zero_runtime = MaintenanceBudget(max_calls=3, max_runtime_seconds=0, min_call_interval_ms=0)
        # 时长上限为 0 表示不限时长，只受调用数约束。
        self.assertEqual(await zero_runtime.acquire(), '')

    async def test_summary_reports_what_was_used(self) -> None:
        budget = MaintenanceBudget(max_calls=4, max_runtime_seconds=300, min_call_interval_ms=0)
        await budget.acquire()
        summary = budget.summary()
        self.assertEqual(summary['calls'], 1)
        self.assertEqual(summary['max_calls'], 4)
        self.assertEqual(summary['max_runtime_seconds'], 300.0)
        self.assertGreaterEqual(summary['elapsed_ms'], 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)


def _fact(fact_id: int, content: str, owner: str = 'a', evidence=None) -> dict:
    return {
        'id': fact_id, 'participantId': owner, 'content': content,
        'importance': 0.5, 'confidence': 0.5, 'unresolved': False,
        'sourceEntryIds': list(evidence or []),
    }


class FactGroupingTests(unittest.TestCase):
    def test_text_key_drops_punctuation_and_case(self) -> None:
        self.assertEqual(fact_text_key('主人 喜欢，冰美式。'), fact_text_key('主人喜欢冰美式'))
        self.assertEqual(fact_text_key('Ollama 版本'), 'ollama版本')

    def test_similarity_is_bounded_and_symmetric(self) -> None:
        self.assertEqual(fact_similarity('她养了一只猫', '她养了一只猫'), 1.0)
        self.assertEqual(fact_similarity('', '任何内容'), 0.0)
        left, right = '她昨天去了书店', '她今天去了书店'
        self.assertAlmostEqual(fact_similarity(left, right), fact_similarity(right, left))
        self.assertGreaterEqual(fact_similarity('她昨天去了书店', '她今天去了书店'), 0.5)

    def test_same_owner_and_shared_evidence_groups(self) -> None:
        groups = group_similar_facts([
            _fact(1, '他答应还书', evidence=[7]),
            _fact(2, '他答应过还书这件事', evidence=[7]),
        ])
        self.assertEqual([[fact['id'] for fact in group] for group in groups], [[1, 2]])

    def test_different_owners_never_group(self) -> None:
        rows = [
            _fact(1, '主人喜欢喝冰美式', owner='a', evidence=[3]),
            _fact(2, '主人喜欢喝冰美式。', owner='b', evidence=[3]),
        ]
        self.assertEqual(group_similar_facts(rows), [])
        self.assertEqual(exact_duplicate_groups(rows), [])

    def test_unrelated_facts_stay_apart(self) -> None:
        self.assertEqual(
            group_similar_facts([_fact(1, '她养了一只叫团子的猫'), _fact(2, '明天要交房租')]),
            [],
        )

    def test_exact_duplicates_only_need_normalization(self) -> None:
        groups = exact_duplicate_groups([
            _fact(1, '冰美式，不加糖。'), _fact(2, '冰美式不加糖'), _fact(3, '完全无关'),
        ])
        self.assertEqual([[fact['id'] for fact in group] for group in groups], [[1, 2]])


class MaintenanceDecisionTests(unittest.TestCase):
    def test_unknown_ids_and_actions_are_dropped(self) -> None:
        raw = {'groups': [
            {'ids': [1, 2, 99], 'action': 'merge', 'content': ' 合并后 ', 'keepId': 2, 'reason': ' 同一件事 '},
            {'ids': [1, 2], 'action': 'rewrite'},
            {'ids': [1], 'action': 'keep'},
            'not-a-group',
        ]}
        decisions = normalize_maintenance_decision(raw, {1, 2})
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]['ids'], [1, 2])
        self.assertEqual(decisions[0]['content'], '合并后')
        self.assertEqual(decisions[0]['reason'], '同一件事')
        self.assertEqual(decisions[0]['keepId'], 2)

    def test_keep_id_falls_back_to_the_first_id(self) -> None:
        decisions = normalize_maintenance_decision(
            {'groups': [{'ids': [5, 6], 'action': 'supersede', 'keepId': 77}]}, {5, 6},
        )
        self.assertEqual(decisions[0]['keepId'], 5)

    def test_bare_array_and_garbage_are_tolerated(self) -> None:
        decisions = normalize_maintenance_decision(
            [{'ids': [1, 2], 'action': 'keep'}], {1, 2},
        )
        self.assertEqual(len(decisions), 1)
        for garbage in (None, 'text', 42, {}, {'groups': 'nope'}):
            with self.subTest(garbage=garbage):
                self.assertEqual(normalize_maintenance_decision(garbage, {1, 2}), [])
