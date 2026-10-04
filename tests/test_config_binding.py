# -*- coding: utf-8 -*-
"""配置**绑定**回归：schema 里的键，运行期真的读到了吗？

本文件不测"某个功能对不对"，只测一件被踩过很多次的事——**用户改的那个键，
运行期读的是同一份值**。三类历史事故：

1. **拼写单读**（`memory.*` / `logging.color_theme`）：`_conf_schema.json` 落盘
   snake_case（`scene_entry_threshold`），读取点却只查 camelCase
   （`sceneEntryThreshold`）→ 用户改了配置、运行期照旧用内置默认值。
   夹具要是也写 camelCase，两边都错却全绿（AGENTS 坑 39/46/66）。
2. **两处判据**（`browser.max_text_characters`）：适配层的 `visit_web()` 写死
   12_000/3_000，core 再按配置 `clip()` 一次 —— 先发生的那道截断让"调大上限"
   失效。
3. **两处默认值**（`runtime.context_entry_limit` 等）：`CONFIG_DEFAULTS` 与
   schema 各写一份，走哪条路取决于配置从哪来。

每条都有**反向用例**：把修复回退，用例必须红。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(HERE)

from plugin.core.service.base import ServiceChunk0, _camel_to_snake  # noqa: E402
from plugin.core.service.chunk5 import _cfg  # noqa: E402
from plugin.core.service.chunk8 import ServiceChunk8  # noqa: E402
from plugin.core.service.chunk9 import ServiceChunk9  # noqa: E402
from plugin.core.service.config import CONFIG_DEFAULTS, normalize_config  # noqa: E402
from plugin.core.service.session import SessionView  # noqa: E402


def _schema() -> dict:
    with open(os.path.join(PLUGIN_ROOT, '_conf_schema.json'), encoding='utf-8-sig') as handle:
        return json.load(handle)


# =========================================================================== #
# 1. memory.*：snake_case 必须读得到（chunk8 的 `_memory_int('camel')` 路径）
# =========================================================================== #

class _MemoryHost(ServiceChunk8):
    """只带 `config` 的最小宿主：`_memory_section()` 会回落到 `_config_section`。"""

    def __init__(self, memory: dict) -> None:
        self.config = {'memory': memory}


class MemorySpellingBindingTests(unittest.TestCase):
    """`memory.*` 的 snake_case（schema 形状）必须真的生效。"""

    #: (camelCase 读取名, snake_case 配置键, schema 默认值, 用户值)
    CASES = (
        ('sceneEntryThreshold', 'scene_entry_threshold', 16, 3),
        ('sceneCharacterThreshold', 'scene_character_threshold', 10_000, 1_500),
        ('compactionEntryLimit', 'compaction_entry_limit', 80, 20),
        ('compactionCharacterLimit', 'compaction_character_limit', 32_000, 9_000),
        ('sceneHookCharacters', 'scene_hook_characters', 2_000, 700),
        ('sceneSummaryCharacters', 'scene_summary_characters', 8_000, 2_400),
        ('arcSummaryCharacters', 'arc_summary_characters', 12_000, 3_000),
        ('factContentCharacters', 'fact_content_characters', 4_000, 900),
        ('maxFactsPerStory', 'max_facts_per_story', 200, 42),
        ('overlayRecentDays', 'overlay_recent_days', 2, 7),
        ('overlayMonthlyAfterDays', 'overlay_monthly_after_days', 10, 30),
        ('overlayWeeklyWindowDays', 'overlay_weekly_window_days', 5, 14),
        ('overlayMonthlyWindowDays', 'overlay_monthly_window_days', 10, 21),
        ('overlayWeeklySummaryCharacters', 'overlay_weekly_summary_characters', 1_600, 500),
        ('overlayMonthlySummaryCharacters', 'overlay_monthly_summary_characters', 2_400, 800),
        ('maintenanceMaxLlmCalls', 'maintenance_max_llm_calls', 12, 3),
        ('maintenanceMaxRuntimeMinutes', 'maintenance_max_runtime_minutes', 10, 2),
        ('statePatchMinDays', 'state_patch_min_days', 2, 9),
        ('statePatchCooldownHours', 'state_patch_cooldown_hours', 72, 6),
    )

    def test_ints_read_from_the_schema_spelling(self) -> None:
        for camel, snake, default, value in self.CASES:
            with self.subTest(key=snake):
                host = _MemoryHost({snake: value})
                self.assertEqual(host._memory_int(camel, default=default), value)

    def test_floats_and_bools_read_from_the_schema_spelling(self) -> None:
        host = _MemoryHost({
            'state_patch_confidence_threshold': 0.5,
            'major_state_patch_confidence_threshold': 0.6,
            'forgetting_threshold': 0.9,
            'forgetting_half_life_days': 5,
            'forgetting_retention_days': 30,
            'forgetting_enabled': True,
            'facts_dedupe_enabled': False,
            'facts_contradiction_enabled': False,
            'temporal_anchor_enabled': False,
            'auto_apply_state_patches': False,
            'allow_major_state_changes': False,
            'overlay_compression_enabled': False,
            'context_metrics_enabled': False,
        })
        self.assertAlmostEqual(host._memory('statePatchConfidenceThreshold', default=0.82), 0.5)
        self.assertAlmostEqual(
            host._memory('majorStatePatchConfidenceThreshold', default=0.95), 0.6,
        )
        self.assertAlmostEqual(host._memory_float('forgettingThreshold', default=0.25), 0.9)
        self.assertEqual(host._memory_int('forgettingHalfLifeDays', default=30), 5)
        self.assertEqual(host._memory_int('forgettingRetentionDays', default=14), 30)
        self.assertIs(host._memory_bool('forgettingEnabled', default=False), True)
        self.assertIs(host._memory_bool('factsDedupeEnabled', default=True), False)
        self.assertIs(host._memory_bool('factsContradictionEnabled', default=True), False)
        self.assertIs(host._memory_bool('temporalAnchorEnabled', default=True), False)
        self.assertIs(host._memory_bool('autoApplyStatePatches', default=True), False)
        self.assertIs(host._memory_bool('allowMajorStateChanges', default=True), False)
        self.assertIs(host._memory_bool('overlayCompressionEnabled', default=True), False)
        self.assertIs(host._memory_bool('contextMetricsEnabled', default=True), False)

    def test_camel_case_still_wins_when_both_spellings_are_present(self) -> None:
        """旧 Koishi 配置（camelCase）不能因为新增 snake 兜底就被顶掉。"""
        host = _MemoryHost({'sceneEntryThreshold': 5, 'scene_entry_threshold': 9})
        self.assertEqual(host._memory_int('sceneEntryThreshold', default=16), 5)

    def test_missing_key_still_falls_back_to_the_upstream_default(self) -> None:
        host = _MemoryHost({})
        self.assertEqual(host._memory_int('sceneEntryThreshold', default=16), 16)
        self.assertEqual(host._memory_int('overlayMonthlySummaryCharacters', default=0), 2_400)

    def test_the_same_key_reads_the_same_number_in_chunk5_and_chunk8(self) -> None:
        """一个键两处判据：`chunk5._cfg`（一直双读）与 `chunk8._memory_int` 必须同值。"""
        memory = {'max_facts_per_story': 42, 'fact_limit': 7}
        host = _MemoryHost(memory)
        self.assertEqual(_cfg(memory, 'maxFactsPerStory', 200), 42)
        self.assertEqual(host._memory_int('maxFactsPerStory', default=200), 42)
        self.assertEqual(_cfg(memory, 'factLimit', 20), 7)

    def test_every_schema_memory_key_is_reachable_in_both_spellings(self) -> None:
        """schema 里 memory 的每个标量键：用它的 snake 形状写进去，必须被读到。"""
        section = _schema()['memory']['items']
        for key, spec in section.items():
            if spec.get('type') == 'object' or 'default' not in spec:
                continue
            with self.subTest(key=key):
                value = spec['default']
                probe = _probe_value(spec, value)
                host = _MemoryHost({key: probe})
                camel = _to_camel(key)
                snake = key

                def read() -> object:
                    return ServiceChunk8._memory_raw(host, camel, snake)

                self.assertEqual(read(), probe, 'schema 键 %s 没被读出来' % key)


def _to_camel(name: str) -> str:
    head, *rest = name.split('_')
    return head + ''.join(part[:1].upper() + part[1:] for part in rest)


def _probe_value(spec: dict, default: object) -> object:
    """造一个**跟默认值不同**的探针值（bool 取反、数字 +1、字符串换一个）。"""
    kind = spec.get('type')
    if kind == 'bool':
        return not default
    if kind == 'int':
        return int(default) + 1
    if kind == 'float':
        return float(default) + 0.25
    return 'probe'


# =========================================================================== #
# 2. logging.color_theme：配色主题必须真的改变渲染
# =========================================================================== #

class _LoggingHost(ServiceChunk9):
    def __init__(self, logging_section: dict) -> None:
        self.config = {'logging': logging_section}
        self.emitted: list = []

    @property
    def blind_mode_config(self) -> dict:
        return {}

    def emit_log(self, level: str, text: str) -> None:  # noqa: D102 - 测试桩
        self.emitted.append((level, text))


class LoggingThemeBindingTests(unittest.TestCase):
    def _render(self, section: dict) -> dict:
        host = _LoggingHost(section)
        seen: dict = {}

        def capture(data):  # noqa: ANN001 - 测试替身
            seen.update(data)
            return 'rendered'

        with mock.patch('plugin.core.service.chunk9.format_layered_log', capture):
            host.report('info', {'id': 's1', 'setting': {}}, 'user-message', '提醒到了')
        return seen

    def test_snake_case_colour_theme_reaches_the_renderer(self) -> None:
        seen = self._render({'format': 'layered', 'color_theme': 'light'})
        self.assertEqual(seen.get('color_theme'), 'light')

    def test_camel_case_colour_theme_still_works(self) -> None:
        seen = self._render({'format': 'layered', 'colorTheme': 'light'})
        self.assertEqual(seen.get('color_theme'), 'light')

    def test_missing_theme_falls_back_to_dark(self) -> None:
        seen = self._render({'format': 'layered'})
        self.assertEqual(seen.get('color_theme'), 'dark')

    def test_standalone_report_path_also_reads_the_theme(self) -> None:
        """第二处渲染点（`report_standalone`）不能漏。"""
        host = _LoggingHost({'format': 'layered', 'color_theme': 'light'})
        seen: dict = {}
        with mock.patch(
            'plugin.core.service.chunk9.format_layered_log',
            lambda data: seen.update(data) or 'rendered',
        ):
            host.report_standalone('info', '独立日志')
        self.assertEqual(seen.get('color_theme'), 'light')


# =========================================================================== #
# 3. browser 字符上限：适配层那道截断必须跟着配置走
# =========================================================================== #

def _install_astrbot_stub() -> None:
    """桥依赖 AstrBot 模块；复用 `test_platform_transport` 已经装好的最小桩。"""
    for name in ('plugin.tests.test_platform_transport', 'tests.test_platform_transport'):
        try:
            importlib.import_module(name)
            return
        except Exception:  # noqa: BLE001 - 布局不同就换下一个包名
            continue


class _FakeBridgeForWeb:
    def __init__(self, browser: dict, html: str) -> None:
        self.config = {'browser': browser}
        self.html = html

    def section(self, name: str) -> dict:
        section = self.config.get(name)
        return section if isinstance(section, dict) else {}

    async def http_get_text(self, url: str, timeout_ms: int):  # noqa: D102 - 测试桩
        return self.html

    def remember_outbound_message_ids(self, *args, **kwargs) -> None:  # pragma: no cover
        return None


class BrowserTextLimitBindingTests(unittest.TestCase):
    """`visit_web()` 的截断上限 = `browser.max_text_characters` / `max_excerpt_characters`。"""

    @classmethod
    def setUpClass(cls) -> None:
        _install_astrbot_stub()
        cls.bridge_module = importlib.import_module('plugin.adapters.astrbot_bridge')

    def _visit(self, browser: dict, html: str) -> dict:
        transport = self.bridge_module.AstrbotTransport(_FakeBridgeForWeb(browser, html))
        return asyncio.run(transport.visit_web('https://example.com/page', 5_000))

    def test_raised_limits_are_honoured(self) -> None:
        html = '<html><body>' + ('字' * 20_000) + '</body></html>'
        page = self._visit(
            {'max_text_characters': 20_000, 'max_excerpt_characters': 5_000}, html,
        )
        self.assertEqual(len(page['text']), 20_000)
        self.assertEqual(len(page['excerpt']), 5_000)

    def test_defaults_are_the_schema_defaults(self) -> None:
        html = '<html><body>' + ('字' * 20_000) + '</body></html>'
        page = self._visit({}, html)
        self.assertEqual(len(page['text']), 12_000)
        self.assertEqual(len(page['excerpt']), 3_000)

    def test_camel_case_config_is_also_read(self) -> None:
        html = '<html><body>' + ('字' * 20_000) + '</body></html>'
        page = self._visit({'maxTextCharacters': 20_000, 'maxExcerptCharacters': 4_000}, html)
        self.assertEqual(len(page['text']), 20_000)
        self.assertEqual(len(page['excerpt']), 4_000)

    def test_nonsense_limits_fall_back_instead_of_truncating_to_zero(self) -> None:
        page = self._visit(
            {'max_text_characters': 0, 'max_excerpt_characters': 'x'}, '<html><body>abc</body></html>',
        )
        self.assertEqual(page['text'], 'abc')
        self.assertEqual(page['excerpt'], 'abc')


# =========================================================================== #
# 4. 默认值对账：schema（用户看到的）与 CONFIG_DEFAULTS（core 用的）必须同值
# =========================================================================== #

#: 逐键对账的**已知例外**（各有理由，见注释）：
#: * `model_center.providers` 是列表，两边形状不同（core 默认空列表 = 不预置连接行）。
#:
#: v1.9.5：`story_defaults.style` 的例外**取消** —— core（`CONFIG_DEFAULTS` 与
#: `types.empty_story_setting`）已对齐到 schema 的中文默认值，由本文件钉死。
_DEFAULT_DIFF_ALLOWLIST = {
    ('model_center', 'providers'),
}

_GROUP_ALIASES = {'model_center': 'model', 'qq_access': 'onebot'}


class ConfigDefaultsReconciliationTests(unittest.TestCase):
    """同一个键两份默认值 = 走哪条路取决于配置从哪来（踩过的"改了没反应"）。"""

    def _diff(self) -> list:
        schema = _schema()
        diffs: list = []

        def walk(path: str, spec: dict, core: object) -> None:
            if not isinstance(spec, dict) or not isinstance(core, dict):
                return
            for key, value in spec.items():
                if not isinstance(value, dict):
                    continue
                here = (path, key)
                if value.get('type') == 'object':
                    walk('%s.%s' % (path, key), value.get('items') or {}, core.get(key))
                elif 'default' in value and key in core and core[key] != value['default']:
                    diffs.append(('%s.%s' % (path, key), value['default'], core[key]))

        for group, spec in schema.items():
            if not isinstance(spec, dict) or spec.get('type') != 'object':
                continue
            core = CONFIG_DEFAULTS.get(_GROUP_ALIASES.get(group, group))
            walk(group, spec.get('items') or {}, core)
        return diffs

    def test_every_schema_default_matches_the_core_default(self) -> None:
        leftovers = []
        for path, schema_default, core_default in self._diff():
            group, _, key = path.partition('.')
            top = path.split('.')[0]
            if (top, key) in _DEFAULT_DIFF_ALLOWLIST or (top, path.split('.')[-1]) in {
                ('model_center', 'providers'),
            }:
                continue
            if (group, path.rsplit('.', 1)[-1]) in _DEFAULT_DIFF_ALLOWLIST:
                continue
            leftovers.append((path, schema_default, core_default))
        self.assertEqual(leftovers, [], 'schema 与 CONFIG_DEFAULTS 的默认值不一致：%s' % leftovers)

    def test_the_four_historical_drifts_are_pinned(self) -> None:
        """这四个是本次修掉的漂移（回退任意一个，本用例红）。"""
        schema = _schema()
        core = CONFIG_DEFAULTS
        for path, expected in (
            ('runtime.context_entry_limit', 35),
            ('runtime.context_time_window_minutes', 45),
            ('memory.overlay_monthly_summary_characters', 2_400),
            ('model_center.main_payload_order', 'cache-first'),
        ):
            with self.subTest(path=path):
                group, _, key = path.partition('.')
                self.assertEqual(schema[group]['items'][key]['default'], expected)
                core_group = _GROUP_ALIASES.get(group, group)
                self.assertEqual(core[core_group][key], expected)

    def test_the_story_style_default_is_the_schema_one(self) -> None:
        """v1.9.5：`story_defaults.style` 只有一句话（schema 的中文默认值）。

        反向用例：把 core 改回上游英文（`CONFIG_DEFAULTS` **或** `types.empty_story_setting`
        任意一处）→ 本用例红；把 schema 改成英文 → 也红。两个 core 副本都钉住，
        因为 `initial_story_setting()` 在配置为空时会用 `empty_story_setting()` 兜底。
        """
        from plugin.core.types import empty_story_setting  # noqa: PLC0415

        schema = _schema()
        expected = schema['story_defaults']['items']['style']['default']
        self.assertEqual(expected, '现实主义日常叙事，情绪克制，关系变化缓慢而具体。')
        self.assertEqual(CONFIG_DEFAULTS['story_defaults']['style'], expected,
                         'CONFIG_DEFAULTS 与 schema 的中文默认值必须同句')
        self.assertEqual(empty_story_setting()['style'], expected,
                         '空剧本设定的兜底也必须同句（否则清空文风会掉回英文）')

    def test_normalize_config_fills_the_schema_defaults(self) -> None:
        normalized = normalize_config({})
        self.assertEqual(normalized['runtime']['context_entry_limit'], 35)
        self.assertEqual(normalized['runtime']['context_time_window_minutes'], 45)
        self.assertEqual(normalized['memory']['overlay_monthly_summary_characters'], 2_400)
        self.assertEqual(normalized['model']['main_payload_order'], 'cache-first')


# =========================================================================== #
# 5. 同一把闸两条路：ignore_self_messages 的缺省
# =========================================================================== #

class _AccessHost(ServiceChunk0):
    """只带 `config` 的接入判定宿主（`_access_config()` 读 `onebot` 段）。"""

    def __init__(self, access: dict) -> None:
        self.config = {'onebot': access}


def _private_session(self_id: str, user_id: str) -> SessionView:
    return SessionView(platform='onebot', self_id=self_id, user_id=user_id)


def _group_session(self_id: str, user_id: str, group_id: str) -> SessionView:
    return SessionView(platform='onebot', self_id=self_id, user_id=user_id, channel_id=group_id)


class IgnoreSelfMessagesDefaultTests(unittest.TestCase):
    def test_private_path_defaults_to_ignoring(self) -> None:
        host = _AccessHost({})
        allowed, reason = host.explain_session_access(_private_session('1', '1'))
        self.assertFalse(allowed)
        self.assertIn('ignore_self_messages', reason)

    def test_group_path_defaults_to_ignoring(self) -> None:
        host = _AccessHost({})
        allowed, _reason = host.explain_group_access(_group_session('1', '1', '9'))
        self.assertFalse(allowed)

    def test_explicit_false_lets_self_messages_through_on_both_paths(self) -> None:
        host = _AccessHost({'ignore_self_messages': False})
        self.assertTrue(host.explain_session_access(_private_session('1', '1'))[0])
        self.assertTrue(host.explain_group_access(_group_session('1', '1', '9'))[0])

    def test_other_users_are_unaffected(self) -> None:
        host = _AccessHost({})
        self.assertTrue(host.explain_session_access(_private_session('1', '2'))[0])


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
