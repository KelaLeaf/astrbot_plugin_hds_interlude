"""上游 `test/alter-system.test.ts` 的逐条移植（stdlib `unittest`，零依赖）。

运行：
    cd /home/kela/文档/harness/hds-interlude && python3 -m unittest plugin.tests.test_alter_system -v

对应关系
--------
* 上游 6 条用例全部只测 `src/alter.ts` 导出的函数（无 `service.ts` / `narrator.ts`
  归属），因此本文件 **没有 `SkipTest`**：`AlterSystemTests` 一一对应。
* `AlterCoverageTests` 是**移植版补充**（上游测试未覆盖的导出：
  `resolveAlterSystemConfig` / `alterAnalysisCoolingDown` / `alterScopeCoolingDown` /
  `markAlterScopeAnalysisAttempt` / 偏移过期 / `pendingScopes` 合并与上限）。
  每条断言都能在上游 `src/alter.ts` 里逐行找到依据。

字段拼写
--------
上游测试第 5 条专门喂 **camelCase 遗留 JSON**（`alterValue` / `lastTriggerAlter` /
`emotionalOffset` / `lastUpdatedAt`），故此处原样保留 camelCase 输入 —— 这正是
`normalize_alter_system_state` 需要兼容的历史数据形态。其余用例统一用移植版的
canonical snake_case。
"""

from __future__ import annotations

import importlib
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

# 把仓库根加入 sys.path，便于以插件包结构 import（与 tests/test_core.py / test_urge.py 同法）。
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
_PLUGIN_DIR = os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

htime = importlib.import_module(f"{_PLUGIN_DIR}.core.time")
alter = importlib.import_module(f"{_PLUGIN_DIR}.core.alter")

DEFAULT_ALTER_SYSTEM_CONFIG = alter.DEFAULT_ALTER_SYSTEM_CONFIG
resolve_alter_system_config = alter.resolve_alter_system_config
normalize_alter_value = alter.normalize_alter_value
create_alter_system_state = alter.create_alter_system_state
normalize_alter_system_state = alter.normalize_alter_system_state
calculate_alter_threshold = alter.calculate_alter_threshold
adjust_alter_weight = alter.adjust_alter_weight
advance_alter_system = alter.advance_alter_system
complete_alter_analysis = alter.complete_alter_analysis
emotional_offset_for_prompt = alter.emotional_offset_for_prompt
alter_analysis_cooling_down = alter.alter_analysis_cooling_down
alter_scope_cooling_down = alter.alter_scope_cooling_down
mark_alter_scope_analysis_attempt = alter.mark_alter_scope_analysis_attempt
alter_scope_value = alter.alter_scope_value
alter_history_for_scope = alter.alter_history_for_scope

# 上游 `const config: AlterSystemConfig = {...}`
CONFIG = {
    'enabled': True,
    'base_threshold': 10,
    'density_factor': 0.3,
    'same_direction_boost': 0.05,
    'opposite_decay': 0.15,
    'min_weight': 0.2,
    'max_intensity': 2,
}

# 上游测试里的两个时间锚点。
NOW_THRESHOLD = datetime(2026, 8, 22, 12, 0, 0, tzinfo=timezone.utc)
NOW_SCOPE = datetime(2026, 9, 6, 12, 0, 0, tzinfo=timezone.utc)


class AlterSystemTests(unittest.TestCase):
    """上游 `alter-system.test.ts` 的 6 条用例，逐条对应。"""

    # 上游 `test('model Alter values are bounded integers and invalid values are ignored')`
    def test_model_alter_values_are_bounded_integers_and_invalid_values_are_ignored(self):
        self.assertEqual(normalize_alter_value(3.4), 3)
        self.assertEqual(normalize_alter_value(20), 5)
        self.assertEqual(normalize_alter_value(-20), -5)
        self.assertIsNone(normalize_alter_value(float('nan')))
        self.assertIsNone(normalize_alter_value('3'))

    # 上游 `test('dynamic threshold moves gradually from ten to seven under dense narration')`
    def test_dynamic_threshold_moves_gradually_from_ten_to_seven_under_dense_narration(self):
        now = NOW_THRESHOLD

        def entry(turn: int):
            """上游 `const entry = (turn) => ({...})`。"""
            return {
                'turn': turn,
                'phase': 'user-message',
                'alter': 1,
                'alter_value': turn,
                'timestamp': htime.iso(now - timedelta(seconds=turn)),
            }

        self.assertEqual(calculate_alter_threshold([], CONFIG, now), 10)
        self.assertEqual(
            calculate_alter_threshold([entry(i + 1) for i in range(5)], CONFIG, now),
            8.5,
        )
        self.assertEqual(
            calculate_alter_threshold([entry(i + 1) for i in range(20)], CONFIG, now),
            7,
        )

    # 上游 `test('same-direction movement strengthens while opposite movement decays')`
    def test_same_direction_movement_strengthens_while_opposite_movement_decays(self):
        self.assertEqual(adjust_alter_weight(0.6, True, 2, CONFIG), 0.7)
        self.assertLess(abs(adjust_alter_weight(0.6, False, 2, CONFIG) - 0.3), 1e-9)
        self.assertEqual(adjust_alter_weight(0.95, True, 5, CONFIG), 1)
        self.assertEqual(adjust_alter_weight(0.2, False, 5, CONFIG), 0)

    # 上游 `test('a trigger is completed only after a valid side-model description')`
    def test_a_trigger_is_completed_only_after_a_valid_side_model_description(self):
        now = NOW_THRESHOLD
        state = create_alter_system_state(now)
        state['alter_value'] = 8
        turn = advance_alter_system(state, 3, 'user-message', now, CONFIG)
        self.assertEqual(turn['threshold_reached'], True)
        self.assertEqual(turn['state']['alter_value'], 11)

        completed = complete_alter_analysis(
            turn['state'], '氛围开始转向更谨慎、私密的交流。', turn['threshold'], now, CONFIG,
        )
        self.assertEqual(completed['alter_value'], 0)
        self.assertEqual(completed['alter_weight'], 1)
        self.assertEqual(completed['last_trigger_direction'], 1)
        self.assertEqual(completed['emotional_offset']['direction'], 'serious')
        self.assertGreaterEqual(completed['emotional_offset']['intensity'], 1)

    # 上游 `test('legacy persisted Alter state is normalized without exposing duplicate weight')`
    def test_legacy_persisted_alter_state_is_normalized_without_exposing_duplicate_weight(self):
        # 这段输入是**上游遗留 JSON 的原样形状**（camelCase），刻意不改写。
        normalized = normalize_alter_system_state({
            'alterValue': -4,
            'alterWeight': 0.75,
            'lastTriggerAlter': -12,
            'emotionalOffset': {
                'direction': 'relaxed', 'description': '较轻松', 'intensity': 1.2,
                'generatedAt': 1787400000000, 'weight': 0.1,
            },
            'history': [],
            'lastUpdatedAt': 1787400000000,
        })
        self.assertEqual(normalized['last_trigger_direction'], -1)
        self.assertEqual(normalized['alter_weight'], 0.75)
        prompt = emotional_offset_for_prompt(normalized, CONFIG)
        self.assertEqual(prompt['weight'], 0.75)
        self.assertNotIn('weight', normalized['emotional_offset'] or {})

    # 上游 `test('Alter keeps relationship-local accumulation and analysis evidence in the same source bucket')`
    def test_alter_keeps_relationship_local_accumulation_and_analysis_evidence_in_the_same_source_bucket(self):
        now = NOW_SCOPE
        state = create_alter_system_state(now)
        alice_first = advance_alter_system(state, 6, 'user-message', now, CONFIG, 'alice')
        state = alice_first['state']
        bob_first = advance_alter_system(
            state, 6, 'user-message', now + timedelta(seconds=1), CONFIG, 'bob',
        )
        state = bob_first['state']

        # 故事级诊断仍然看到十二点，但两段私密关系谁也不能借用对方的证据触发分析。
        self.assertEqual(state['alter_value'], 12)
        self.assertEqual(alice_first['threshold_reached'], False)
        self.assertEqual(bob_first['threshold_reached'], False)
        self.assertEqual(alter_scope_value(state, 'alice'), 6)
        self.assertEqual(alter_scope_value(state, 'bob'), 6)

        alice_trigger = advance_alter_system(
            state, 4, 'conversation-follow-up', now + timedelta(seconds=2), CONFIG, 'alice',
        )
        self.assertEqual(alice_trigger['threshold_reached'], True)
        self.assertEqual(alice_trigger['source_participant_id'], 'alice')
        self.assertEqual(alice_trigger['trigger_value'], 10)
        self.assertEqual(len(alter_history_for_scope(alice_trigger['state']['history'], 'alice')), 2)
        self.assertEqual(len(alter_history_for_scope(alice_trigger['state']['history'], 'bob')), 1)

        completed = complete_alter_analysis(
            alice_trigger['state'], '她在这段关系里变得更谨慎。', alice_trigger['threshold'],
            now, CONFIG, 'alice',
        )
        self.assertEqual(alter_scope_value(completed, 'alice'), 0)
        self.assertEqual(alter_scope_value(completed, 'bob'), 6)
        self.assertEqual(completed['alter_value'], 6)


class AlterCoverageTests(unittest.TestCase):
    """移植版补充：上游测试未覆盖、但 `src/alter.ts` 明确导出的行为。"""

    def test_resolve_config_keeps_defaults_and_accepts_both_spellings(self):
        self.assertDictEqual(resolve_alter_system_config(), dict(DEFAULT_ALTER_SYSTEM_CONFIG))
        resolved = resolve_alter_system_config({
            'enabled': True, 'baseThreshold': 12, 'min_weight': 0.5,
        })
        self.assertEqual(resolved['enabled'], True)
        # 上游 Console 的 camelCase 键名被归一为 snake_case（写出一律 snake_case）。
        self.assertEqual(resolved['base_threshold'], 12)
        self.assertEqual(resolved['min_weight'], 0.5)
        # 未显式给出的项保留默认值。
        self.assertEqual(resolved['max_intensity'], 2)
        self.assertEqual(resolved['timeout'], 30_000)

    def test_alter_value_rounds_half_up_like_js_math_round(self):
        # 上游 `Math.round` 半值向 +∞；Python 内建 `round` 是银行家舍入，这里必须不同。
        self.assertEqual(normalize_alter_value(2.5), 3)
        self.assertEqual(normalize_alter_value(-2.5), -2)
        self.assertIsNone(normalize_alter_value(True))  # JS `typeof true !== 'number'`

    def test_emotional_offset_for_prompt_gates_on_enabled_and_weight(self):
        state = create_alter_system_state(NOW_SCOPE)
        state['emotional_offset'] = {
            'direction': 'serious', 'description': '氛围收紧', 'intensity': 1,
            'generated_at': htime.iso(NOW_SCOPE),
        }
        state['alter_weight'] = 0.2  # 恰好等于 minWeight
        self.assertIsNotNone(emotional_offset_for_prompt(state, CONFIG))
        self.assertEqual(emotional_offset_for_prompt(state, CONFIG)['weight'], 0.2)

        disabled = dict(CONFIG, enabled=False)
        self.assertIsNone(emotional_offset_for_prompt(state, disabled))

        state['alter_weight'] = 0.1999
        self.assertIsNone(emotional_offset_for_prompt(state, CONFIG))

    def test_offset_expires_when_opposite_movement_drops_weight_below_minimum(self):
        state = create_alter_system_state(NOW_SCOPE)
        state['emotional_offset'] = {
            'direction': 'serious', 'description': '氛围收紧', 'intensity': 1,
            'generated_at': htime.iso(NOW_SCOPE),
        }
        state['alter_weight'] = 0.2
        state['last_trigger_direction'] = 1
        turn = advance_alter_system(state, -1, 'user-message', NOW_SCOPE, CONFIG, 'alice')
        self.assertEqual(turn['offset_expired'], True)
        self.assertIsNone(turn['state']['emotional_offset'])
        self.assertEqual(turn['state']['alter_weight'], 0)
        # 位移本身仍然入桶（过期只清掉可见偏移，不清掉账）。
        self.assertEqual(alter_scope_value(turn['state'], 'alice'), -1)

    def test_analysis_cooldown_uses_scope_gate_and_falls_back_to_state_gate(self):
        state = create_alter_system_state(NOW_SCOPE)
        self.assertEqual(alter_analysis_cooling_down(state, NOW_SCOPE), False)
        self.assertEqual(alter_scope_cooling_down(state, 'alice', NOW_SCOPE), False)

        marked = mark_alter_scope_analysis_attempt(state, 'alice', NOW_SCOPE)
        self.assertEqual(alter_scope_cooling_down(marked, 'alice', NOW_SCOPE + timedelta(minutes=4)), True)
        self.assertEqual(alter_scope_cooling_down(marked, 'alice', NOW_SCOPE + timedelta(minutes=5)), False)
        # 独立重试闸门：另一段关系不被 alice 的失败压住（回落到状态级、且状态级为空）。
        self.assertEqual(alter_scope_cooling_down(marked, 'bob', NOW_SCOPE), False)
        self.assertEqual(alter_analysis_cooling_down(marked, NOW_SCOPE), False)

        state_level = dict(state)
        state_level['last_analysis_attempt_at'] = htime.iso(NOW_SCOPE)
        self.assertEqual(alter_analysis_cooling_down(state_level, NOW_SCOPE + timedelta(minutes=4)), True)
        self.assertEqual(alter_analysis_cooling_down(state_level, NOW_SCOPE + timedelta(minutes=5)), False)
        # 没有自己的桶时，作用域冷却回落到状态级时间戳。
        self.assertEqual(alter_scope_cooling_down(state_level, 'bob', NOW_SCOPE), True)

    def test_pending_scopes_merge_by_participant_and_keep_first_position(self):
        normalized = normalize_alter_system_state({
            'pendingScopes': [
                {'participantId': '  alice  ', 'alterValue': 2},
                {'participantId': 'alice', 'alterValue': 3},
                {'participantId': 'bob', 'alterValue': 1},
                {'participantId': 42, 'alterValue': 9},  # 非法 id 归一为 ''
            ],
        })
        self.assertEqual(
            [scope['participant_id'] for scope in normalized['pending_scopes']],
            ['alice', 'bob', ''],
        )
        self.assertEqual(normalized['pending_scopes'][0]['alter_value'], 5)
        self.assertEqual(normalized['pending_scopes'][1]['alter_value'], 1)
        # 分桶存在时，故事级 alterValue 不再被重复计入桶。
        self.assertEqual(normalized['alter_value'], 0)

    def test_history_is_capped_at_fifty_and_turn_numbering_continues(self):
        state = create_alter_system_state(NOW_SCOPE)
        for _ in range(60):
            state = advance_alter_system(state, 0, 'user-message', NOW_SCOPE, CONFIG, 'alice')['state']
        self.assertEqual(len(state['history']), 50)
        self.assertEqual(state['history'][-1]['turn'], 60)
        self.assertEqual(len(alter_history_for_scope(state['history'], 'alice')), 50)


if __name__ == '__main__':
    unittest.main()
