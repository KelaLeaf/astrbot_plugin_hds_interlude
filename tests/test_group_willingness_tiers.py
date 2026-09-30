"""群聊意愿档位（上游 `test/group-willingness-tiers.test.ts` 逐条移植，1.0.1-rc23）。

核心打分数学由 `test_group_willingness.py` 覆盖；这里只测**档位解析层**：
五档标定、auto 三态、睡眠态两个硬行为（@ 不通 + 概率 ×0.2）、过期回落、旧数值门兼容。
"""

from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from plugin.core.group_willingness import (  # noqa: E402
    ASLEEP_PROBABILITY_MULTIPLIER,
    LIFE_STATUS_STALE_MS,
    WILLINGNESS_TIERS,
    evaluate_willingness_gate,
)

# `normalize_life_status` 在本移植版住在 `story_state.py`（状态信封的归一化器都在那里）；
# 上游把它放在 group-willingness.ts。语义一致。
from plugin.core.story_state import normalize_life_status  # noqa: E402

#: 固定基准时刻：2026-09-26T10:00:00+08:00。
NOW = 1_790_493_600_000


def first_call_at(preset, *, legacy=None, auto_map=None, life_status=None, roll=0.4, limit=24):
    """连续单条消息（零衰减）下第几条触发；返回 None 表示始终不触发。"""
    state = None
    for index in range(1, limit + 1):
        decision = evaluate_willingness_gate(
            state, preset, auto_map, life_status, legacy,
            {
                'now': NOW, 'message_count': 1, 'content': '普通消息',
                'mentioned_bot': False, 'quoted_bot': False, 'random': roll,
            },
        )
        state = decision['state']
        if decision['should_call']:
            return index
    return None


def life(status, at=NOW, offset=0):
    from datetime import datetime, timezone

    stamp = datetime.fromtimestamp((at + offset) / 1000, tz=timezone.utc).isoformat()
    return {'status': status, 'updated_at': stamp}


class TierCalibrationTests(unittest.TestCase):
    def test_normal_first_call_lands_on_message_4_to_5_and_neighbours_scale_around_it(self):
        # 上游标定：固定掷骰 0.4 下 normal 命中第 4 条；五档首发序号严格递增。
        self.assertEqual(first_call_at('eager'), 1)
        self.assertEqual(first_call_at('active'), 2)
        self.assertIn(first_call_at('normal'), (4, 5))
        self.assertIn(first_call_at('reserved'), range(6, 9))
        self.assertIn(first_call_at('quiet'), range(9, 14))

    def test_all_five_tiers_are_distinct_and_ordered_by_invocation_frequency(self):
        order = [first_call_at(tier) for tier in ('eager', 'active', 'normal', 'reserved', 'quiet')]
        self.assertEqual(order, sorted(order), order)
        self.assertEqual(len(set(order)), 5, order)
        self.assertEqual(set(WILLINGNESS_TIERS), {'quiet', 'reserved', 'normal', 'active', 'eager'})

    def test_off_disables_the_gate_entirely_and_ignores_legacy_numbers(self):
        state = None
        decision = evaluate_willingness_gate(
            state, 'off', None, None, {'enabled': False, 'threshold': 99},
            {'now': NOW, 'message_count': 1, 'content': 'x', 'random': 0.99},
        )
        self.assertTrue(decision['should_call'])
        self.assertEqual(decision['reason'], 'disabled')
        self.assertEqual(decision['diagnosis']['preset'], 'off')

    def test_legacy_numeric_gate_with_preset_off_is_treated_as_custom(self):
        # 旧门启用 + preset 空/off → 按 custom 原样使用，数值语义不迁移。
        self.assertIsNone(first_call_at('off', legacy={'enabled': True, 'threshold': 2, 'base_gain': 0.1}))
        decision = evaluate_willingness_gate(
            None, 'off', None, None, {'enabled': True, 'threshold': 2, 'base_gain': 0.1},
            {'now': NOW, 'message_count': 1, 'content': 'x', 'mentioned_bot': True},
        )
        self.assertEqual(decision['diagnosis']['preset'], 'custom')
        self.assertEqual(decision['reason'], 'forced-mention')


class AutoTierTests(unittest.TestCase):
    def test_auto_maps_each_life_status_to_its_configured_tier(self):
        self.assertEqual(first_call_at('auto', life_status=life('idle')), first_call_at('active'))
        self.assertEqual(first_call_at('auto', life_status=life('busy')), first_call_at('quiet'))
        custom = {'busy': 'normal', 'idle': 'eager', 'asleep': 'quiet'}
        self.assertEqual(
            first_call_at('auto', life_status=life('idle'), auto_map=custom), first_call_at('eager'),
        )
        self.assertEqual(
            first_call_at('auto', life_status=life('busy'), auto_map=custom), first_call_at('normal'),
        )

    def test_asleep_blocks_the_mention_bypass_and_multiplies_probability(self):
        asleep = life('asleep')
        blocked = evaluate_willingness_gate(
            None, 'auto', {'asleep': 'eager'}, asleep, None,
            {'now': NOW, 'message_count': 1, 'content': 'x', 'mentioned_bot': True, 'random': 0.5},
        )
        self.assertFalse(blocked['should_call'], '@ 在睡眠态不再直通')
        self.assertEqual(blocked['reason'], 'asleep')
        self.assertLessEqual(blocked['probability'], ASLEEP_PROBABILITY_MULTIPLIER + 1e-9)
        allowed = evaluate_willingness_gate(
            None, 'auto', {'asleep': 'eager'}, asleep, None,
            {'now': NOW, 'message_count': 1, 'content': 'x', 'mentioned_bot': True, 'random': 0.05},
        )
        self.assertTrue(allowed['should_call'])
        self.assertTrue(allowed['diagnosis']['asleep'])

    def test_stale_or_missing_life_status_falls_back_to_the_normal_tier(self):
        stale = life('idle', offset=-(LIFE_STATUS_STALE_MS + 1000))
        self.assertEqual(first_call_at('auto', life_status=stale), first_call_at('normal'))
        missing = first_call_at('auto', life_status=None)
        self.assertEqual(missing, first_call_at('normal'))

    def test_normalize_life_status_accepts_exactly_the_three_documented_values(self):
        for value in ('busy', 'asleep', 'idle'):
            with self.subTest(value=value):
                self.assertEqual(normalize_life_status({'status': value, 'updatedAt': '2026-09-26T02:00:00Z'})['status'], value)
        for bad in ('sleeping', None, 42, {'status': 'busy'}, {'status': 'busy', 'updatedAt': 'nope'}):
            with self.subTest(bad=bad):
                self.assertIsNone(normalize_life_status(bad))


if __name__ == '__main__':
    unittest.main()
