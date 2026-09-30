"""模型特化模块（`core/specialization.py`）的单元测试（不依赖 AstrBot SDK）。

对照上游 1.0.1-rc28 的 `test/specialization.test.ts`（node:test + node:assert/strict）
与 `src/specialization.ts` / `src/narrator.ts` / `src/script/lived-writing.ts`：

- 家族 / 档位真值表逐条对齐（含上游测试的 16 组模型名）；
- 长提示词不重打，只做**抽取守卫**：钉住关键子串与 U+2019 / em dash 这类容易被
  "顺手打对"或"顺手打错"的字符，确保 `core/specialization.py` 里的字符串是从上游
  逐字抽出来的，而不是凭记忆改写的；
- lite 档块列表按上游 `systemPrompt()` lite 分支的顺序与相位门控检查。

运行方式（在仓库根目录）：

    python3 -m unittest plugin.tests.test_specialization -v

包名与目录名解耦：按插件目录的实际名字动态 import，不硬编码。
"""

from __future__ import annotations

import ast
import importlib
import os
import pathlib
import sys
import unittest

# 把仓库根加入 sys.path，便于以插件包结构 import（两种仓库布局都能跑）。
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

#: 自动解析插件目录名（即 Python 包名）；发布仓布局下仓库根就是插件根。
_PLUGIN_DIR = os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_specialization = importlib.import_module(f"{_PLUGIN_DIR}.core.specialization")

CONTRACT_TIERS = _specialization.CONTRACT_TIERS
MODEL_FAMILIES = _specialization.MODEL_FAMILIES
auto_tier = _specialization.auto_tier
channels_block = _specialization.channels_block
detect_family = _specialization.detect_family
family_overrides = _specialization.family_overrides
infer_specialty = _specialization.infer_specialty
lite_blocks = _specialization.lite_blocks
repetition_guard_instruction = _specialization.repetition_guard_instruction
resolve_manual_specialty = _specialization.resolve_manual_specialty


#: 上游 `test/specialization.test.ts` 的真值表（model, tier, family）逐条照搬。
UPSTREAM_MODEL_CASES = (
    ('yunwu/gemini-3.7-flash', 'lite', 'gemini-flash'),
    ('gemini-3.8-flash', 'lite', 'gemini-flash'),
    ('gemini-3-pro-preview', 'standard', 'gemini'),
    ('claude-opus-5', 'full', 'claude'),
    ('claude-sonnet-4-6', 'full', 'claude'),
    ('gpt-5.5', 'full', 'gpt'),
    ('gpt-6-astra', 'full', 'gpt'),
    ('GLM-5.3', 'standard', 'glm'),
    ('zai-org/GLM-4.6', 'lite', 'glm'),
    ('kimi-k3', 'standard', 'kimi'),
    ('moonshotai/Kimi-K2.6', 'standard', 'kimi'),
    ('deepseek-ai/DeepSeek-V4-Flash', 'standard', 'deepseek'),
    ('deepseek-ai/DeepSeek-V3.2', 'standard', 'deepseek'),
    ('grok-4.5', 'lite', 'grok'),
    ('grok-4.20-beta', 'lite', 'grok'),
    ('qwen3-max', 'full', 'generic'),
)

#: 接线时要盯的常见模型名（含中转别名、大小写混写、`^o\d` 形态）。
NAMED_MODEL_CASES = (
    ('gemini-2.5-flash', 'lite', 'gemini-flash'),
    ('gemini-3-pro', 'standard', 'gemini'),
    ('claude-sonnet-4', 'full', 'claude'),
    ('gpt-5', 'full', 'gpt'),
    ('glm-4.6', 'lite', 'glm'),
    ('glm-5', 'standard', 'glm'),
    ('kimi-k2', 'standard', 'kimi'),
    ('deepseek-chat', 'standard', 'deepseek'),
    ('grok-4', 'lite', 'grok'),
    ('o3-mini', 'lite', 'gpt'),
    ('my-relay-alias', 'full', 'generic'),
    ('qwen3-max', 'full', 'generic'),
)

#: 顺序 / 大小写 / 边界：家族正则的先后与 `flash|lite|mini` 边界词。
EDGE_MODEL_CASES = (
    ('Flash', 'lite', 'generic'),
    ('some-LITE-relay', 'lite', 'generic'),
    ('deepseek-v4', 'standard', 'deepseek'),
    ('GPT-4o', 'full', 'gpt'),
    ('openai/gpt-4.1', 'full', 'gpt'),
    ('xai-grok-3', 'lite', 'grok'),
    ('chatglm4', 'lite', 'glm'),
    ('anthropic/claude-3-7-sonnet', 'full', 'claude'),
    ('gemini-flash', 'lite', 'gemini-flash'),
)

ALL_MODEL_CASES = UPSTREAM_MODEL_CASES + NAMED_MODEL_CASES + EDGE_MODEL_CASES


class FamilyAndTierTest(unittest.TestCase):
    """家族判定与档位推断（上游 `detectFamily` / `autoTier` / `inferSpecialty`）。"""

    def test_contract_tiers_and_model_families_are_the_upstream_enums(self) -> None:
        self.assertEqual(CONTRACT_TIERS, ('lite', 'standard', 'full'))
        self.assertEqual(
            MODEL_FAMILIES,
            ('gemini-flash', 'gemini', 'claude', 'gpt', 'glm', 'kimi', 'deepseek', 'grok', 'generic'),
        )

    def test_truth_table_for_every_model_name(self) -> None:
        for model, tier, family in ALL_MODEL_CASES:
            with self.subTest(model=model):
                self.assertEqual(detect_family(model), family, model)
                self.assertEqual(auto_tier(model), tier, model)
                self.assertEqual(
                    infer_specialty(model),
                    {'tier': tier, 'family': family, 'source': 'auto', 'probe': model[:80]},
                    model,
                )

    def test_manual_tier_keeps_family_and_marks_source_manual(self) -> None:
        for model, tier, family in ALL_MODEL_CASES:
            with self.subTest(model=model):
                # 显式档位：家族仍按模型名识别（上游 inferSpecialty 的 `mode !== 'auto'` 分支）。
                for manual in CONTRACT_TIERS:
                    profile = infer_specialty(model, manual)
                    self.assertEqual(profile['tier'], manual)
                    self.assertEqual(profile['family'], family)
                    self.assertEqual(profile['source'], 'manual')
                # Console 手动解析：family='auto' 时同样按模型名识别。
                self.assertEqual(
                    resolve_manual_specialty(model, tier, 'auto'),
                    {'tier': tier, 'family': family, 'source': 'manual', 'probe': model[:80]},
                )

    def test_off_is_the_rc12_baseline(self) -> None:
        # 上游测试：inferSpecialty('anything', 'off') 是 full+generic+manual。
        self.assertEqual(
            infer_specialty('anything', 'off'),
            {'tier': 'full', 'family': 'generic', 'source': 'manual', 'probe': 'anything'},
        )
        # 即使模型名可识别，off 也不识别家族（rc12 原样合约）。
        for model in ('gemini-3.8-flash', 'grok-4.5', 'claude-opus-5'):
            self.assertEqual(infer_specialty(model, 'off')['family'], 'generic', model)
        # Console 侧：mode 为空 / 'off' 一律 full+generic。
        for mode in ('', None, 'off'):
            with self.subTest(mode=mode):
                self.assertEqual(
                    resolve_manual_specialty('gemini-3.8-flash', mode, 'gemini-flash'),
                    {'tier': 'full', 'family': 'generic', 'source': 'manual',
                     'probe': 'gemini-3.8-flash'},
                )

    def test_manual_family_setting_overrides_detection(self) -> None:
        # 中转别名场景：档位 lite + 家族显式指定 gemini-flash。
        self.assertEqual(
            resolve_manual_specialty('mystery-relay-alias', 'lite', 'gemini-flash'),
            {'tier': 'lite', 'family': 'gemini-flash', 'source': 'manual',
             'probe': 'mystery-relay-alias'},
        )
        # 家族 generic：档位生效但不套家族块。
        self.assertEqual(resolve_manual_specialty('gemini-3.8-flash', 'lite', 'generic')['family'],
                         'generic')
        # family_setting 为空 / 'auto' 时回落到模型名识别。
        self.assertEqual(resolve_manual_specialty('kimi-k3', 'standard', '')['family'], 'kimi')
        self.assertEqual(resolve_manual_specialty('kimi-k3', 'standard', 'auto')['family'], 'kimi')

    def test_non_tier_mode_falls_back_to_full(self) -> None:
        """上游会把任意字符串当档位用；本移植版对非三档回落 full（受控偏离）。"""
        self.assertEqual(resolve_manual_specialty('gpt-5.5', 'bogus', 'auto')['tier'], 'full')
        self.assertEqual(infer_specialty('gpt-5.5', 'bogus')['tier'], 'full')
        self.assertIn(resolve_manual_specialty('gpt-5.5', 'bogus', 'auto')['tier'], CONTRACT_TIERS)

    def test_probe_is_truncated_to_80_chars(self) -> None:
        """probe 只用于日志；上游保留全长，本移植版截断到 80 字符（受控偏离）。"""
        long_name = 'relay/' + 'x' * 200
        self.assertEqual(infer_specialty(long_name)['probe'], long_name[:80])
        self.assertEqual(len(resolve_manual_specialty(long_name, 'lite', 'auto')['probe']), 80)

    def test_family_regex_order_and_deepseek_flash_exception(self) -> None:
        # gemini-flash 在 gemini 之前命中。
        self.assertEqual(detect_family('gemini-2.5-flash'), 'gemini-flash')
        # deepseek 的 flash 走 standard（V4-Flash 数据支持 standard）。
        self.assertEqual(auto_tier('deepseek-ai/DeepSeek-V4-Flash'), 'standard')
        # ^o\d 归 gpt，再由 mini 边界词降到 lite。
        self.assertEqual(detect_family('o3-mini'), 'gpt')
        self.assertEqual(auto_tier('o3-mini'), 'lite')
        # glm 只在 4.x（非 5-9）才降 lite。
        self.assertEqual(auto_tier('glm-4.6'), 'lite')
        self.assertEqual(auto_tier('GLM-5.3'), 'standard')
        # 未识别家族（中转别名）保守回退 full。
        self.assertEqual(auto_tier('my-relay-alias'), 'full')


class StringExtractionGuardTest(unittest.TestCase):
    """抽取守卫：钉住关键子串与特殊字符，防止抄错 / 少抄弯引号。"""

    def test_lived_writing_prompt_keeps_curly_apostrophes(self) -> None:
        prompt = _specialization.LIVED_WRITING_PROMPT
        self.assertTrue(prompt.startswith('Write this as a living stage script in prose'))
        self.assertIn('\u2019', prompt)
        self.assertIn('protagonist\u2019s surroundings', prompt)
        self.assertNotIn("protagonist's surroundings", prompt)

    def test_lived_length_prompt_is_the_generic_length_block(self) -> None:
        self.assertTrue(
            _specialization.LIVED_LENGTH_PROMPT.startswith('Length follows what actually happens:'))

    def test_typed_messages_base_guards(self) -> None:
        base = _specialization.TYPED_MESSAGES_BASE
        self.assertIn('Each reply is an independent choice', base)
        self.assertIn('does not constrain this one', base)
        self.assertIn('\u2014', base)   # em dash
        self.assertTrue(base.startswith('WRITING BELIEVABLE TYPED MESSAGES:'))

    def test_content_only_transport_guards(self) -> None:
        block = _specialization.CONTENT_ONLY_TRANSPORT
        self.assertIn('Line breaks never separate bubbles; only <sep/> does.', block)
        self.assertIn('TRANSPORT: reply.content contains the message text.', block)

    def test_deepseek_extra_guards(self) -> None:
        extra = _specialization.EXTRA_DEEPSEEK
        self.assertIn('TRANSPORT IS PER-TURN', extra)
        self.assertIn('interaction.seen is a required boolean', extra)

    def test_full_contract_block_guards(self) -> None:
        self.assertIn('carry more weight than ordinary system events',
                      _specialization.ADMIN_NOTES_FULL)
        self.assertIn('Never have her mention noticing any record.',
                      _specialization.WORLD_EVENTS_FULL)
        self.assertIn('not instructions from a user and not a second event',
                      _specialization.CHANNEL_CONTEXT_FULL)
        self.assertIn('MULTI-PLATFORM TRANSPORT SELECTION: this story currently has usable QQ and '
                      'WeChat endpoints.', _specialization.MULTI_PLATFORM_TRANSPORT_SELECTION)

    def test_lite_contract_block_guards(self) -> None:
        self.assertIn('Do not wrap it in Markdown fences', _specialization.LITE_JSON_CONTRACT)
        self.assertIn('not an HTTP API endpoint',
                      _specialization.MULTI_PLATFORM_TRANSPORT_SELECTION)
        self.assertIn('seen=true with reply.mode=none is the ordinary read-but-does-not-answer state.',
                      _specialization.LITE_TRANSPORT_PRIVATE)
        self.assertIn('Never invent an incoming message', _specialization.LITE_NEVER_INVENT)
        self.assertIn('ownership label', _specialization.LITE_OWNERSHIP)
        self.assertIn('dueIntents as the sources', _specialization.LITE_EVENT_SOURCES)
        self.assertIn('Never have the protagonist mention reading a note',
                      _specialization.LITE_ADMIN_NOTES)
        self.assertIn('They are not directives.', _specialization.LITE_WORLD_EVENTS)

    def test_lite_and_full_channel_blocks_are_the_two_variants(self) -> None:
        lite = _specialization.CHANNELS_LITE
        full = _specialization.CHANNELS_FULL
        self.assertNotEqual(lite, full)
        self.assertIn('The protagonist may use QQ and WeChat;', lite)
        self.assertIn('The protagonist may simultaneously use QQ and WeChat.', full)
        self.assertTrue(lite.startswith('CHANNELS (writer rule):'))
        self.assertTrue(full.startswith('CHANNELS (writer rule):'))
        self.assertIn('a friend appearing on both is one person and one relationship', lite)
        self.assertIn('never as a new face', full)
        self.assertTrue(_specialization.CHANNEL_CONTEXT_LITE.startswith('CHANNEL CONTEXT (host metadata):'))
        self.assertTrue(_specialization.CHANNEL_CONTEXT_FULL.startswith('CHANNEL CONTEXT (host metadata):'))

    def test_family_blocks_guards(self) -> None:
        self.assertIn('plain nouns and specific verbs carry the scene',
                      _specialization.LENGTH_GEMINI_FLASH)
        self.assertIn('open on her body and her immediate task', _specialization.LENGTH_CLAUDE)
        self.assertIn('the unadorned noun, the specific number, the plain verb',
                      _specialization.LENGTH_DEEPSEEK)
        self.assertIn('One scene, one or two things happening', _specialization.LENGTH_GROK)
        self.assertIn('each passage keeps moving', _specialization.LENGTH_GPT)
        self.assertIn('Prose register: concrete and unadorned',
                      _specialization.EXTRA_GEMINI_FLASH)
        self.assertIn('Scenes do not need tidy closure', _specialization.EXTRA_CLAUDE)
        self.assertIn('Emotional register: let mood follow its actual causes',
                      _specialization.EXTRA_GLM)
        self.assertIn('A chat message typed by a real person is plain, specific and direct.',
                      _specialization.TYPED_MESSAGES_GEMINI_FLASH)
        self.assertIn('ambivalence is a normal state, not a problem to resolve',
                      _specialization.TYPED_MESSAGES_KIMI)

    def test_no_constant_carries_stray_whitespace_or_escapes(self) -> None:
        """抽取损伤（多尾空格、真实换行、被转义的引号）一律当场报错。"""
        names = (
            'LIVED_WRITING_PROMPT', 'LIVED_LENGTH_PROMPT', 'TYPED_MESSAGES_BASE',
            'TYPED_MESSAGES_GEMINI_FLASH', 'TYPED_MESSAGES_KIMI', 'LENGTH_GEMINI_FLASH',
            'LENGTH_CLAUDE', 'LENGTH_DEEPSEEK', 'LENGTH_GROK', 'LENGTH_GPT',
            'EXTRA_GEMINI_FLASH', 'EXTRA_CLAUDE', 'EXTRA_GLM', 'EXTRA_DEEPSEEK',
            'CONTENT_ONLY_TRANSPORT', 'LITE_TRANSPORT_PRIVATE', 'LITE_TRANSPORT_GROUP',
            'LITE_TRANSPORT_ADVANCE', 'LITE_JSON_CONTRACT', 'LITE_INTERVAL', 'LITE_UNREAD',
            'LITE_NEVER_INVENT', 'LITE_OWNERSHIP', 'LITE_EVENT_SOURCES', 'LITE_ADMIN_NOTES',
            'LITE_WORLD_EVENTS', 'CHANNELS_LITE', 'CHANNEL_CONTEXT_LITE', 'CHANNELS_FULL',
            'CHANNEL_CONTEXT_FULL', 'ADMIN_NOTES_FULL', 'WORLD_EVENTS_FULL',
            'FORMAT_AND_REALITY_CONTRACT', 'MULTI_PLATFORM_TRANSPORT_SELECTION',
            'BUBBLE_AFFORDANCE', 'BUBBLE_AFFORDANCE_TEMPLATE', 'REPETITION_GUARD_TAIL',
            'REPETITION_GUARD_TEMPLATE',
        )
        for name in names:
            value = getattr(_specialization, name)
            with self.subTest(name=name):
                self.assertIsInstance(value, str)
                self.assertTrue(value)
                self.assertEqual(value, value.strip(), f'{name} 首尾有空白')
                self.assertNotIn('\n', value, f'{name} 里有真实换行')
                self.assertNotIn('\t', value, f'{name} 里有制表符')
                self.assertNotIn("\\'", value, f'{name} 里有未还原的转义引号')
                self.assertNotIn('\\u2019', value, f'{name} 里 Unicode 转义没还原')

    def test_lite_transport_trio_is_distinct_and_upstream_ordered(self) -> None:
        private = _specialization.LITE_TRANSPORT_PRIVATE
        group = _specialization.LITE_TRANSPORT_GROUP
        advance = _specialization.LITE_TRANSPORT_ADVANCE
        self.assertEqual(len({private, group, advance}), 3)
        self.assertIn('For this private turn', private)
        self.assertIn('For this group turn', group)
        self.assertIn('In this independent-life phase', advance)


class FamilyOverridesTest(unittest.TestCase):
    """家族偏移表（上游 `familyOverrides`）。"""

    def test_every_family_returns_all_four_keys(self) -> None:
        for family in MODEL_FAMILIES + ('unknown-relay', ''):
            with self.subTest(family=family):
                table = family_overrides(family)
                self.assertEqual(
                    sorted(table),
                    ['extra_after_length', 'extra_after_phase', 'length', 'typed'],
                )

    def test_deepseek_uses_length_and_extra_after_phase(self) -> None:
        table = family_overrides('deepseek')
        self.assertIs(table['extra_after_phase'], _specialization.EXTRA_DEEPSEEK)
        self.assertIs(table['length'], _specialization.LENGTH_DEEPSEEK)
        self.assertIsNone(table['typed'])
        self.assertIsNone(table['extra_after_length'])

    def test_gemini_and_generic_have_no_offsets(self) -> None:
        for family in ('gemini', 'generic', 'unknown-relay'):
            with self.subTest(family=family):
                table = family_overrides(family)
                self.assertEqual(list(table.values()), [None, None, None, None])

    def test_gemini_flash_replaces_length_and_typed_and_adds_one_line(self) -> None:
        table = family_overrides('gemini-flash')
        self.assertIs(table['length'], _specialization.LENGTH_GEMINI_FLASH)
        self.assertIs(table['typed'], _specialization.TYPED_MESSAGES_GEMINI_FLASH)
        self.assertIs(table['extra_after_length'], _specialization.EXTRA_GEMINI_FLASH)
        self.assertIsNone(table['extra_after_phase'])

    def test_other_families_match_the_upstream_table(self) -> None:
        self.assertIs(family_overrides('claude')['length'], _specialization.LENGTH_CLAUDE)
        self.assertIs(family_overrides('claude')['extra_after_phase'],
                      _specialization.EXTRA_CLAUDE)
        self.assertIs(family_overrides('glm')['extra_after_phase'], _specialization.EXTRA_GLM)
        self.assertIs(family_overrides('kimi')['typed'], _specialization.TYPED_MESSAGES_KIMI)
        self.assertIs(family_overrides('grok')['length'], _specialization.LENGTH_GROK)
        self.assertIs(family_overrides('gpt')['length'], _specialization.LENGTH_GPT)
        # 家族块是"最小偏移"：claude/glm/deepseek 都不换打字块。
        for family in ('claude', 'glm', 'deepseek', 'grok', 'gpt'):
            self.assertIsNone(family_overrides(family)['typed'], family)


class RepetitionGuardInstructionTest(unittest.TestCase):
    """重复气泡守卫（上游 `repetitionGuardInstruction`）。"""

    def test_guard_renders_both_numbers_and_ends_with_the_tail(self) -> None:
        text = repetition_guard_instruction({'bubbles': 2, 'consecutive': 3})
        self.assertIn('exactly 2 separate chat bubbles', text)
        self.assertIn('each of her last 3', text)
        self.assertIn('the same 2-bubble shape', text)
        self.assertTrue(text.endswith(_specialization.REPETITION_GUARD_TAIL), text)
        self.assertTrue(text.startswith('REPETITION GUARD (host observation about the recent script):'))

    def test_guard_stays_empty_below_thresholds(self) -> None:
        for repetition in (
            None,
            {},
            {'bubbles': 1, 'consecutive': 3},
            {'bubbles': 0, 'consecutive': 3},
            {'bubbles': 2, 'consecutive': 1},
            {'bubbles': 2, 'consecutive': 0},
            {'bubbles': 1, 'consecutive': 1},
        ):
            with self.subTest(repetition=repetition):
                self.assertEqual(repetition_guard_instruction(repetition), '')

    def test_guard_floor_is_two_and_two(self) -> None:
        self.assertTrue(repetition_guard_instruction({'bubbles': 2, 'consecutive': 2}))

    def test_guard_ignores_non_mapping_input(self) -> None:
        for repetition in ('2x3', 3, [2, 3], object()):
            with self.subTest(repetition=repetition):
                self.assertEqual(repetition_guard_instruction(repetition), '')

    def test_guard_renders_larger_runs(self) -> None:
        text = repetition_guard_instruction({'bubbles': 5, 'consecutive': 4})
        self.assertIn('exactly 5 separate chat bubbles', text)
        self.assertIn('each of her last 4', text)
        self.assertIn('\u2019', text)   # "A real person’s typing"


class LiteBlocksTest(unittest.TestCase):
    """lite 档块列表（上游 `systemPrompt()` 的 lite 分支）。"""

    def _joined(self, phase: str, group_turn: bool) -> str:
        return '\n'.join(lite_blocks(phase, group_turn))

    @staticmethod
    def _count_occurrences(blocks, needle: str) -> int:
        """传输块是 `CONTENT_ONLY_TRANSPORT` + 换行 + 相位传输句拼成的一块，按子串数。"""
        return sum(block.count(needle) for block in blocks)

    def test_lite_contract_has_no_full_only_blocks(self) -> None:
        joined = self._joined('user-message', False)
        self.assertNotIn('EVIDENCE AND EXPECTATION', joined)
        self.assertNotIn('CROSS-CHANNEL DELIVERY', joined)
        self.assertNotIn('SCRIPT-FIRST TRANSPORT MIRROR', joined)
        self.assertNotIn(_specialization.CHANNELS_FULL, joined)
        self.assertNotIn(_specialization.ADMIN_NOTES_FULL, joined)
        self.assertNotIn(_specialization.WORLD_EVENTS_FULL, joined)

    def test_private_phase_carries_exactly_the_private_transport(self) -> None:
        blocks = lite_blocks('user-message', False)
        self.assertEqual(self._count_occurrences(blocks, _specialization.LITE_TRANSPORT_PRIVATE), 1)
        self.assertEqual(self._count_occurrences(blocks, _specialization.LITE_TRANSPORT_GROUP), 0)
        self.assertEqual(self._count_occurrences(blocks, _specialization.LITE_TRANSPORT_ADVANCE), 0)
        # 上游把 content-only 协议句与相位传输句用换行拼成**一块**。
        self.assertIn(_specialization.CONTENT_ONLY_TRANSPORT + '\n'
                      + _specialization.LITE_TRANSPORT_PRIVATE, blocks)

    def test_group_phase_carries_exactly_the_group_transport(self) -> None:
        blocks = lite_blocks('user-message', True)
        self.assertEqual(self._count_occurrences(blocks, _specialization.LITE_TRANSPORT_GROUP), 1)
        self.assertEqual(self._count_occurrences(blocks, _specialization.LITE_TRANSPORT_PRIVATE), 0)
        self.assertEqual(self._count_occurrences(blocks, _specialization.LITE_TRANSPORT_ADVANCE), 0)
        self.assertIn(_specialization.PHASE_GROUP_POST, '\n'.join(blocks))
        self.assertNotIn(_specialization.PHASE_PRIVATE_SEND, '\n'.join(blocks))
        self.assertNotIn(_specialization.PHASE_INTERRUPTED_DRAFTS, '\n'.join(blocks))

    def test_advance_phase_carries_the_advance_transport(self) -> None:
        blocks = lite_blocks('advance', False)
        self.assertEqual(self._count_occurrences(blocks, _specialization.LITE_TRANSPORT_ADVANCE), 1)
        self.assertEqual(self._count_occurrences(blocks, _specialization.LITE_TRANSPORT_PRIVATE), 0)
        self.assertEqual(self._count_occurrences(blocks, _specialization.LITE_TRANSPORT_GROUP), 0)
        joined = '\n'.join(blocks)
        self.assertIn(_specialization.PHASE_ADVANCE_LIFE, joined)
        self.assertIn(_specialization.PHASE_ADVANCE_CROSS, joined)

    def test_other_phases_use_their_own_phase_instruction(self) -> None:
        for phase, expected in (('conversation-follow-up',
                                 _specialization.PHASE_CONVERSATION_FOLLOW_UP),
                                ('intent-due', _specialization.PHASE_INTENT_DUE)):
            with self.subTest(phase=phase):
                joined = self._joined(phase, False)
                self.assertIn(expected, joined)
                # 非 advance 相位仍是私聊传输形态（上游 liteTransport 的第二层判断）。
                self.assertIn(_specialization.LITE_TRANSPORT_PRIVATE, joined)

    def test_block_order_follows_the_upstream_lite_branch(self) -> None:
        blocks = lite_blocks('user-message', False)
        # 非可选块固定 16 块：12 块核心合约 + 相位指令 + 相位传输 + CHANNELS/CHANNEL CONTEXT。
        self.assertEqual(len(blocks), 16)
        self.assertIs(blocks[0], _specialization.LIVED_WRITING_PROMPT)
        self.assertIs(blocks[1], _specialization.LIVED_LENGTH_PROMPT)
        self.assertIs(blocks[2], _specialization.TYPED_MESSAGES_BASE)
        self.assertIs(blocks[3], _specialization.FORMAT_AND_REALITY_CONTRACT)
        self.assertIs(blocks[4], _specialization.LITE_JSON_CONTRACT)
        self.assertIs(blocks[5], _specialization.LITE_INTERVAL)
        self.assertIs(blocks[6], _specialization.LITE_UNREAD)
        self.assertTrue(blocks[7].startswith('CURRENT PHASE: USER MESSAGE.'))
        self.assertTrue(blocks[8].startswith('TRANSPORT: reply.content contains the message text.'))
        self.assertIs(blocks[9], _specialization.LITE_NEVER_INVENT)
        self.assertIs(blocks[10], _specialization.LITE_OWNERSHIP)
        self.assertIs(blocks[11], _specialization.LITE_EVENT_SOURCES)
        self.assertIs(blocks[12], _specialization.LITE_ADMIN_NOTES)
        self.assertIs(blocks[13], _specialization.LITE_WORLD_EVENTS)
        self.assertIs(blocks[14], _specialization.CHANNELS_LITE)
        self.assertIs(blocks[15], _specialization.CHANNEL_CONTEXT_LITE)

    def test_phase_and_group_turn_change_exactly_two_blocks(self) -> None:
        private = lite_blocks('user-message', False)
        group = lite_blocks('user-message', True)
        advance = lite_blocks('advance', False)
        self.assertEqual(len(private), len(group))
        self.assertEqual(len(private), len(advance))
        self.assertEqual([i for i in range(len(private)) if private[i] != group[i]], [7, 8])
        self.assertEqual([i for i in range(len(private)) if private[i] != advance[i]], [7, 8])

    def test_group_turn_wins_over_phase_like_upstream(self) -> None:
        """上游先判 `groupTurn` 再判 `phase === 'advance'`（相位描述里那两句是同一顺序）。"""
        blocks = lite_blocks('advance', True)
        self.assertEqual(self._count_occurrences(blocks, _specialization.LITE_TRANSPORT_GROUP), 1)
        self.assertEqual(self._count_occurrences(blocks, _specialization.LITE_TRANSPORT_ADVANCE), 0)


class ChannelsAndAffordanceTest(unittest.TestCase):
    """CHANNELS 块选择与默认气泡形态（上游 `channels_block` / `writingAffordances`）。"""

    def test_channels_block_switches_between_lite_and_full(self) -> None:
        self.assertEqual(channels_block(True),
                         [_specialization.CHANNELS_LITE, _specialization.CHANNEL_CONTEXT_LITE])
        self.assertEqual(channels_block(),
                         [_specialization.CHANNELS_FULL, _specialization.CHANNEL_CONTEXT_FULL])
        self.assertEqual(channels_block(False),
                         [_specialization.CHANNELS_FULL, _specialization.CHANNEL_CONTEXT_FULL])
        self.assertEqual(lite_blocks('user-message', False)[-2:], channels_block(True))

    def test_bubble_affordance_keeps_the_default_separator(self) -> None:
        affordance = _specialization.BUBBLE_AFFORDANCE
        self.assertIn('<sep/>', affordance)
        self.assertIn('"<sep/>"', affordance)   # 上游是 JSON.stringify(separator) 的产物
        self.assertIn('The plugin sends the first segment immediately and simulates typing',
                      affordance)
        self.assertNotIn('Message splitting is disabled', affordance)
        # 调用方换真实分隔符时用模板（插值占位保留原样）。
        self.assertIn('${JSON.stringify(separator)}',
                      _specialization.BUBBLE_AFFORDANCE_TEMPLATE)
        self.assertEqual(
            _specialization.BUBBLE_AFFORDANCE_TEMPLATE.replace('${JSON.stringify(separator)}',
                                                               '"<<>>"'),
            affordance.replace('"<sep/>"', '"<<>>"'),
        )


class ModulePurityTest(unittest.TestCase):
    """core 纯度与来源说明：不 import astrbot、不 import 本插件其它模块。"""

    def test_module_only_imports_stdlib(self) -> None:
        source = pathlib.Path(_specialization.__file__).read_text(encoding='utf-8')
        tree = ast.parse(source)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split('.')[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split('.')[0])
        self.assertNotIn('astrbot', imported)
        self.assertLessEqual(imported, {'__future__', 're', 'typing'}, sorted(imported))

    def test_docstring_records_provenance_and_the_no_rewrite_rule(self) -> None:
        doc = _specialization.__doc__ or ''
        for needle in ('upstream/src/specialization.ts', 'upstream/src/narrator.ts', '1.0.1-rc28',
                       '逐字抽取', '禁止改写', 'astrbot'):
            self.assertIn(needle, doc, needle)


if __name__ == '__main__':
    unittest.main()
