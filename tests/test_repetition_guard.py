"""条数锚定守卫的检测层（上游 `test/repetition-guard.test.ts` 的检测用例，1.0.1-rc18）。

渲染层（`REPETITION GUARD` 段落）由 `test_narrative_prompts.py` 与
`test_specialization.py` 覆盖；这里只测 `detect_message_repetition` 的归批算法。
"""

from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from plugin.core.service.helpers import detect_message_repetition  # noqa: E402


def character(index: int, count: int = 0) -> dict:
    """一条 `character-message` 条目；`count` 非 0 时带批次首领投递元数据。"""
    metadata: dict = {}
    if count:
        metadata = {'bubbleIndex': 0, 'bubbleCount': count}
    return {'kind': 'character-message', 'actor': 'character', 'metadata': metadata, 'content': '嗯'}


def character_flat(index: int, count: int) -> dict:
    """本移植版把气泡元数据**平铺**在 metadata 里，这一形态也必须被认。"""
    return {
        'kind': 'character-message', 'actor': 'character', 'content': '嗯',
        'metadata': {'bubble_index': index, 'bubble_count': count},
    }


class DetectMessageRepetitionTests(unittest.TestCase):
    def test_triggers_on_a_fixed_two_bubble_run_with_delivery_metadata(self):
        entries = [character(0, 2), character(0, 2)]
        self.assertEqual(detect_message_repetition(entries), {'bubbles': 2, 'consecutive': 2})

    def test_a_trailing_batch_whose_later_bubbles_are_still_typing_counts_by_its_leader(self):
        # 尾部只有首领已落库（后续气泡还在打字）时，仍按首领的 bubbleCount 计一整批。
        entries = [character(0, 2), character(0, 2), character(0, 2)]
        self.assertEqual(detect_message_repetition(entries), {'bubbles': 2, 'consecutive': 3})

    def test_contiguous_character_message_runs_without_metadata_still_form_batches(self):
        # 无元数据时连续的 character-message 归成一批：两批各 2 条（中间有旁白分断）。
        entries = [
            character(0), character(0),
            {'kind': 'script', 'content': '她放下杯子'},
            character(0), character(0),
        ]
        self.assertEqual(detect_message_repetition(entries), {'bubbles': 2, 'consecutive': 2})
        # 四连发在没有分断时是**一批 4 条**（不是两批 2 条）→ 不触发。
        self.assertIsNone(detect_message_repetition([character(0)] * 4))

    def test_flat_metadata_spelling_is_accepted(self):
        entries = [character_flat(0, 3), character_flat(0, 3)]
        self.assertEqual(detect_message_repetition(entries), {'bubbles': 3, 'consecutive': 2})

    def test_a_broken_run_or_a_single_bubble_habit_does_not_trigger_the_guard(self):
        self.assertIsNone(detect_message_repetition([character(0, 2), character(0, 3)]))
        self.assertIsNone(detect_message_repetition([character(0, 1), character(0, 1)]))
        self.assertIsNone(detect_message_repetition([character(0, 2)]))

    def test_non_character_entries_break_fallback_batches_but_not_leader_batches(self):
        # 回退归批会被任何非 character-message 条目截断：尾部只有 1 条 → 不成批。
        self.assertIsNone(detect_message_repetition([
            character(0), character(0),
            {'kind': 'script', 'content': '她放下杯子'},
            character(0),
        ]))
        # 带首领元数据的批次自成一格，旁白与用户消息都不会把它们并成一批。
        for breaker in ({'kind': 'script', 'content': '旁白'}, {'kind': 'user-message', 'content': '在？'}):
            with self.subTest(breaker=breaker['kind']):
                self.assertEqual(
                    detect_message_repetition([character(0, 2), breaker, character(0, 2)]),
                    {'bubbles': 2, 'consecutive': 2},
                )

    def test_malformed_input_is_ignored(self):
        self.assertIsNone(detect_message_repetition(None))
        self.assertIsNone(detect_message_repetition('junk'))
        self.assertIsNone(detect_message_repetition([]))


if __name__ == '__main__':
    unittest.main()
