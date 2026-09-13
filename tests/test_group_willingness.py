"""群聊意愿层单元测试（不依赖 AstrBot SDK）。

移植自上游 `test/group-willingness.test.ts`（node:test + node:assert/strict），
逐条对照断言。运行方式（在仓库根目录）：

    python3 -m unittest plugin.tests.test_group_willingness -v

包名与目录名解耦：按插件目录的实际名字动态 import，不硬编码。
"""

import importlib
import os
import sys
import unittest

import random as _random

# 把插件根目录的父目录（repo 根）加入 sys.path，便于以插件包结构 import
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# 自动解析插件目录名（即 Python 包名）
_PLUGIN_DIR = os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_group_willingness = importlib.import_module(f"{_PLUGIN_DIR}.core.group_willingness")
DEFAULT_GROUP_WILLINGNESS = _group_willingness.DEFAULT_GROUP_WILLINGNESS
consume_group_willingness = _group_willingness.consume_group_willingness
evaluate_group_willingness = _group_willingness.evaluate_group_willingness
resolve_group_willingness = _group_willingness.resolve_group_willingness

CONFIG = {
    'enabled': True, 'max_score': 1, 'threshold': 0.24, 'probability_amplifier': 1.3,
    'decay_half_life_seconds': 180, 'reply_cost': 0.55, 'base_gain': 0.12,
    'quote_gain': 0.12, 'keyword_gain': 0.18, 'keywords': ['水濑'],
}


class GroupWillingnessTest(unittest.TestCase):
    def test_accumulates_locally_respects_threshold_and_uses_bounded_probability(self):
        first = evaluate_group_willingness(None, CONFIG, {
            'now': 0, 'message_count': 1, 'content': '大家晚上好',
            'mentioned_bot': False, 'quoted_bot': False, 'random': 0,
        })
        self.assertEqual(first['should_call'], False)
        self.assertEqual(first['reason'], 'below-threshold')

        second = evaluate_group_willingness(first['state'], CONFIG, {
            'now': 1_000, 'message_count': 2, 'content': '水濑你怎么看',
            'mentioned_bot': False, 'quoted_bot': False, 'random': 0,
        })
        self.assertEqual(second['reason'], 'probability-roll')
        self.assertTrue(second['probability'] > 0 and second['probability'] <= 1)
        self.assertEqual(second['should_call'], True)

    def test_mentions_bypass_willingness_while_a_sent_group_reply_consumes_score(self):
        forced = evaluate_group_willingness(None, CONFIG, {
            'now': 0, 'message_count': 1, 'content': '在吗',
            'mentioned_bot': True, 'quoted_bot': False, 'random': 0.99,
        })
        self.assertEqual(forced['should_call'], True)
        self.assertEqual(forced['reason'], 'forced-mention')
        after_reply = consume_group_willingness(forced['state'], CONFIG, 1_000)
        self.assertTrue(after_reply['score'] < forced['state']['score'])

    def test_disabled_willingness_preserves_existing_always_trigger_behavior(self):
        decision = evaluate_group_willingness(None, {'enabled': False}, {
            'now': 0, 'message_count': 1, 'content': '普通消息',
            'mentioned_bot': False, 'quoted_bot': False,
        })
        self.assertEqual(decision['should_call'], True)
        self.assertEqual(decision['reason'], 'disabled')

    def test_injected_rng_decides_the_probability_roll(self):
        # 分数高于阈值时进入 probability-roll；random 未传入 → 消耗注入的随机源
        previous = {'score': 0.5, 'updated_at': 1_000}
        input_ = {
            'now': 1_000, 'message_count': 1, 'content': '随便聊聊',
            'mentioned_bot': False, 'quoted_bot': False,
        }

        calls = []

        def rng_low():
            calls.append(1)
            return 0.0

        rolled_in = evaluate_group_willingness(previous, CONFIG, dict(input_), rng=rng_low)
        self.assertEqual(rolled_in['reason'], 'probability-roll')
        self.assertEqual(rolled_in['should_call'], True)
        self.assertEqual(len(calls), 1)

        def rng_high():
            calls.append(1)
            return 0.999

        calls.clear()
        rolled_out = evaluate_group_willingness(previous, CONFIG, dict(input_), rng=rng_high)
        self.assertEqual(rolled_out['reason'], 'probability-roll')
        self.assertEqual(rolled_out['should_call'], False)
        self.assertEqual(len(calls), 1)

    def test_random_source_is_untouched_on_predictable_paths(self):
        def rng_forbidden():  # pragma: no cover - 一旦被调用就说明路径错了
            raise AssertionError('随机源不应在可预测路径上被调用')

        disabled = evaluate_group_willingness(None, {**CONFIG, 'enabled': False}, {
            'now': 0, 'message_count': 1, 'content': '普通消息',
            'mentioned_bot': False, 'quoted_bot': False,
        }, rng=rng_forbidden)
        self.assertEqual(disabled['reason'], 'disabled')

        below = evaluate_group_willingness(None, CONFIG, {
            'now': 0, 'message_count': 1, 'content': '普通消息',
            'mentioned_bot': False, 'quoted_bot': False,
        }, rng=rng_forbidden)
        self.assertEqual(below['reason'], 'below-threshold')

        forced = evaluate_group_willingness(None, CONFIG, {
            'now': 0, 'message_count': 1, 'content': '在吗',
            'mentioned_bot': True, 'quoted_bot': False,
        }, rng=rng_forbidden)
        self.assertEqual(forced['reason'], 'forced-mention')

    def test_default_random_source_matches_upstream_signature(self):
        # 不注入 rng、不传 input.random 时，退回 random.random（与上游 Math.random 同位）
        decision = evaluate_group_willingness({'score': 0.5, 'updated_at': 1_000}, CONFIG, {
            'now': 1_000, 'message_count': 1, 'content': '随便聊聊',
            'mentioned_bot': False, 'quoted_bot': False,
        })
        self.assertEqual(decision['reason'], 'probability-roll')
        self.assertIsInstance(decision['should_call'], bool)
        self.assertIs(_group_willingness._random.random, _random.random)

    def test_resolve_normalizes_keywords_like_upstream(self):
        resolved = resolve_group_willingness({'keywords': ['  水濑  ', '', '   ', '幕间']})
        self.assertEqual(resolved['keywords'], ['水濑', '幕间'])
        # 缺省 keywords 时回落到默认空数组
        self.assertEqual(resolve_group_willingness({'enabled': True})['keywords'], [])
        self.assertEqual(resolve_group_willingness(None)['keywords'], [])
        # 归一化不会污染默认配置
        self.assertEqual(DEFAULT_GROUP_WILLINGNESS['keywords'], [])


if __name__ == '__main__':
    unittest.main()
