# -*- coding: utf-8 -*-
"""氛围位移（Alter System）**触发链**的回归面（2026-10-05 真机暴露的两个缺陷）。

现场：用户在控制台看到桶值 `-43`（阈值按默认配置只有 ~5–10）却写着「上次分析：从未」、
「内心天气」永远空着，同时「触发配置」四栏全是「—」。根因两条，都是**判据与消费端对不上**：

1. `advance_alter_system()` 返回的是 **snake_case**（`threshold_reached`），而
   `chunk4` 的触发判定只读 **camelCase**（`thresholdReached`）→ 恒为 `None`
   → **触发后的侧端氛围分析从来没被排过**（功能整条死掉，测试还全绿）。
2. 控制台「触发配置」按**上游旧键名**读（`threshold` / `decay` / `weight_step` /
   `cooldown_minutes`），而本移植版 schema 是 `base_threshold` / `opposite_decay` /
   `same_direction_boost`，冷却更是代码常量 → 四栏显示「—」。

这里的用例都走**真实实现**（真 `chunk4` 方法、真 `ConsoleApi` 投影），
并各配一条反向：把双拼写读回 camelCase-only 就必须红。
"""

from __future__ import annotations

import unittest
from typing import Any

# astrbot 桩由这条 import 装上（既有控制台测试的同款做法）：不先 import 它，
# `plugin.adapters` 会在 import 期就因缺 astrbot 而失败。
from plugin.tests import test_astrbot_bridge  # noqa: F401
from plugin.adapters.console_api import ConsoleApi
from plugin.core.alter import DEFAULT_COOLDOWN_MS
from plugin.core.service.chunk4 import ServiceChunk4


class _SchedulingStub:
    """只负责记录"有没有排过一次分析"的最小替身。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def schedule_alter_analysis(self, story_id: str, phase: str, participant_id: str = '') -> None:
        self.calls.append((story_id, phase, participant_id))


class AlterTriggerGateTests(unittest.TestCase):
    """`advance_alter_system` 的结果必须真的排得起一次侧端分析。"""

    def _gate(self, alter_turn: Any) -> tuple[bool, list[tuple[str, str, str]]]:
        stub = _SchedulingStub()
        scheduled = ServiceChunk4._maybe_schedule_alter_analysis(
            stub, alter_turn, 'story-1', 'user-message',
        )
        return bool(scheduled), stub.calls

    def test_the_real_snake_case_result_schedules_the_analysis(self) -> None:
        """**真实现场**：`advance_alter_system()` 返回的就是 snake_case，必须排得动。"""
        from plugin.core.alter import advance_alter_system, resolve_alter_system_config

        config = resolve_alter_system_config({})
        turn = advance_alter_system(
            None, 10, 'user-message', '2026-10-05T16:00:00.000Z', config, 'participant-1',
        )
        self.assertTrue(turn['threshold_reached'], '夹具要先真的到阈值')
        scheduled, calls = self._gate(turn)
        self.assertTrue(scheduled, 'snake_case 的 threshold_reached 必须触发排期')
        self.assertEqual(calls, [('story-1', 'user-message', 'participant-1')])

    def test_the_camel_case_spelling_also_schedules(self) -> None:
        """另一份拼写（历史/上游 wire）同样认——跨 chunk 传参一律双读。"""
        scheduled, calls = self._gate(
            {'thresholdReached': True, 'sourceParticipantId': 'participant-9'},
        )
        self.assertTrue(scheduled)
        self.assertEqual(calls, [('story-1', 'user-message', 'participant-9')])

    def test_below_the_threshold_schedules_nothing(self) -> None:
        """反向：没到阈值不许排（否则每次说话都白跑一次侧端模型）。"""
        for payload in ({'threshold_reached': False, 'thresholdReached': False}, {}, None):
            scheduled, calls = self._gate(payload)
            self.assertFalse(scheduled, payload)
            self.assertEqual(calls, [], payload)

    def test_a_missing_source_falls_back_to_the_global_bucket(self) -> None:
        """没给来源桶时排**全局**那一桶（空串 = 主角自身/全局）。"""
        scheduled, calls = self._gate({'threshold_reached': True})
        self.assertTrue(scheduled)
        self.assertEqual(calls, [('story-1', 'user-message', '')])


class AlterConfigProjectionTests(unittest.TestCase):
    """控制台「触发配置」必须显示**本移植版真实 schema** 的值。"""

    def _config(self, section: dict[str, Any]) -> dict[str, Any]:
        stub = type('Stub', (), {})()
        stub.bridge = type('Bridge', (), {'section': staticmethod(lambda name: section)})()
        return ConsoleApi._alter_config(stub)

    def test_real_schema_keys_reach_the_panel(self) -> None:
        section = {
            'enabled': True,
            'base_threshold': 10.0,
            'density_factor': 0.3,
            'max_intensity': 2.0,
            'opposite_decay': 0.15,
            'same_direction_boost': 0.05,
            'min_weight': 0.2,
        }
        config = self._config(section)
        self.assertEqual(config['threshold'], 10.0)
        self.assertEqual(config['max_intensity'], 2.0)
        self.assertEqual(config['decay'], 0.15)
        self.assertEqual(config['weight_step'], 0.05)
        self.assertEqual(config['cooldown_minutes'], max(1, int(DEFAULT_COOLDOWN_MS // 60_000)))
        self.assertEqual(config['density_factor'], 0.3)
        self.assertEqual(config['min_weight'], 0.2)

    def test_the_old_upstream_names_still_work_if_present(self) -> None:
        """反向的一半：万一配置里还留着旧名（手改/老导出），也读得到。"""
        config = self._config({
            'threshold': 7, 'decay': 0.2, 'weight_step': 0.1, 'cooldown_minutes': 3,
        })
        self.assertEqual(config['threshold'], 7)
        self.assertEqual(config['decay'], 0.2)
        self.assertEqual(config['weight_step'], 0.1)
        self.assertEqual(config['cooldown_minutes'], 3)

    def test_an_empty_section_does_not_invent_numbers(self) -> None:
        """空配置：阈值/衰减/步长给 `None`（面板显示「—」），冷却给有效常量。"""
        config = self._config({'enabled': True})
        self.assertIsNone(config['threshold'])
        self.assertIsNone(config['decay'])
        self.assertIsNone(config['weight_step'])
        self.assertEqual(config['cooldown_minutes'], max(1, int(DEFAULT_COOLDOWN_MS // 60_000)))


if __name__ == '__main__':  # pragma: no cover
    unittest.main()

class AlterCopyMatchesTheJudgementSourceTests(unittest.TestCase):
    """控制台文案必须与**判据源头**一致（文案即规格；2026-10-05 用户看出图例是反的）。

    源头两处：`narrator_prompts.py` 给模型的定义（positive = more serious…），
    以及 `core/alter.py` 的 `'serious' if direction > 0 else 'relaxed'`。
    前端 `Alter.tsx` 的方向标签与曲线图例原先**正好写反**（+1 标 relaxed、-1 标 serious，
    图例「正=放松，负=紧绷」）。
    """

    def _read(self, rel: str) -> str:
        import os

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, rel), encoding='utf-8') as fh:
            return fh.read()

    def test_the_prompt_defines_positive_as_serious(self) -> None:
        source = self._read('core/narrator_prompts.py')
        self.assertIn('positive means more serious', source)
        self.assertIn('negative means more relaxed', source)

    def test_the_settlement_maps_positive_to_serious(self) -> None:
        source = self._read('core/alter.py')
        self.assertIn("'serious' if direction > 0 else 'relaxed'", source)

    def test_the_panel_labels_follow_the_same_direction(self) -> None:
        source = self._read('frontend/src/panels/Alter.tsx')
        plus = source.split('[-1]:')[0].split('1:')[-1]
        minus = source.split('[-1]:')[1].split('0:')[0]
        self.assertIn('严肃', plus, '正方向必须标"严肃/沉重"')
        self.assertNotIn('放松', plus, '正方向不许标成放松（旧的写反处）')
        self.assertIn('放松', minus, '负方向必须标"放松/活跃"')

    def test_the_chart_legend_is_not_inverted(self) -> None:
        source = self._read('frontend/src/panels/Alter.tsx')
        self.assertIn('正=严肃/沉重，负=放松/活跃', source)
        self.assertNotIn('正=放松，负=紧绷', source)
    def test_the_table_colors_follow_the_same_direction(self) -> None:
        """数值配色必须与方向语义一致：**正（严肃/沉重）= 琥珀，负（放松/活跃）= 绿**。

        原先两处表格写的是 `value >= 0 ? 'text-ok' : 'text-warn'`——正值给绿、负值给琥珀，
        与面板自己的内心天气徽章（松弛=绿 / 严肃=琥珀）和修好后的图例**正好相反**。
        """
        source = self._read('frontend/src/panels/Alter.tsx')
        self.assertIn("row.value > 0 ? 'text-warn'", source, '按来源分桶：正值必须琥珀')
        self.assertIn("row.alter > 0 ? 'text-warn'", source, '本论增量：正值必须琥珀')
        self.assertNotIn("row.value >= 0 ? 'text-ok'", source, '旧的写反配色不许回来')
        self.assertNotIn("row.alter >= 0 ? 'text-ok'", source, '旧的写反配色不许回来')

    def test_the_never_rendered_tone_field_is_gone(self) -> None:
        """`DIRECTION` 的 `tone` 字段从来没被渲染过（死代码，且它自己也写反过）——已删。"""
        source = self._read('frontend/src/panels/Alter.tsx')
        self.assertNotIn("{ label: string; tone:", source)
    def test_the_card_value_and_meter_follow_direction_too(self) -> None:
        """整屏"同语义同色"：氛围位移**数值**与内心天气**强度条**也必须按方向给色。

        原先数值用的是 `Math.abs(value) > 0.5 ? 'warn' : 'neutral'`（只看大小、不看方向）：
        放松态（负）也显示琥珀，与徽章、两张表的"负=绿"自相矛盾；强度条则根本没传 tone
        （默认蓝），出现"绿徽章配蓝条"。
        """
        source = self._read('frontend/src/panels/Alter.tsx')
        self.assertIn('function directionTone(value: number)', source, '方向→色调必须是唯一判据')
        self.assertIn("tone={directionTone(state?.value ?? 0)}", source, '数值必须走方向判据')
        self.assertIn("tone={offset.direction === 'relaxed' ? 'ok' : 'warn'}", source,
                      '强度条必须跟方向一致')
        self.assertNotIn("Math.abs(state?.value ?? 0) > 0.5 ? 'warn'", source, '旧的只看大小的写法不许回来')
