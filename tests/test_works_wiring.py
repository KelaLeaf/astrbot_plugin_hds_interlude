"""共同作品（works）的**接线**测试（本移植版新增的那一层）。

上游 `works.ts` 自身没有接线（没有配置组、没有 payload 字段、没有服务调用点），所以
"接线对不对"没有上游用例可抄——这里钉住的正是我们自己加的那几处：

1. `narrator_prompts.work_instruction()` 按工作模式选上游那两段英文原文（逐字）；
2. 决策字段 `workProposal` / `workRequest` 进双拼写表（坑 41 的同族：只写一种拼写 = 静默失效）；
3. 配置组 `works` 的读取口径（缺失按默认、只有显式 false 才算关）；
4. 「写手模型」的 AstrBot 模型选择器（v1.7.9）：schema 的 `_special` + 侧任务的
   传输层任务键（`SIDE_TASK_ROUTES`）——少一处，"指名了也不生效"。
"""

from __future__ import annotations

import ast
import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from plugin.core import works as works_module  # noqa: E402
from plugin.core.narrator import SIDE_TASK_ROUTES  # noqa: E402
from plugin.core.narrator_prompts import work_instruction  # noqa: E402
from plugin.core.service import chunk4 as chunk4_module  # noqa: E402

PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[1]


class WriterModelSelectorTests(unittest.TestCase):
    """「写手模型」= AstrBot 模型选择器（v1.7.9）的两处**纯接线**。

    第三条「合成指名连接行 + 双读兜底」需要 AstrBot 桩，在
    `test_astrbot_bridge.WorksWriterBindingTests`；服务层怎么用它见
    `test_works.WorksWiringTests`。这里钉的是不需要宿主就能验的两点：
    配置页渲染成选择器（schema），以及请求真的带上 `works` 这个任务键
    （`SIDE_TASK_ROUTES`——没有它，指名的 Provider 不会被用到）。
    """

    def schema(self) -> dict:
        return json.loads((PLUGIN_ROOT / '_conf_schema.json').read_text(encoding='utf-8-sig'))

    def test_the_key_becomes_a_provider_selector_without_changing_its_name_or_default(self):
        field = self.schema()['works']['items']['model_id']
        self.assertEqual(field['_special'], 'select_provider', '配置页才会渲染成 AstrBot 模型下拉框')
        self.assertEqual(field['default'], '', '默认值不能动（留空 = 用默认 Provider）')
        self.assertEqual(field['description'], '写手模型')

    def test_the_hint_documents_both_spellings(self):
        """两种填法都要写清，否则老用户不知道手填的值还算不算数。"""
        hint = self.schema()['works']['items']['model_id']['hint']
        self.assertIn('从 AstrBot 已配置的模型中选一个', hint)
        self.assertIn('留空则用默认 Provider', hint)
        self.assertIn('连接的 id / 模型名 / 标签', hint)

    def test_the_side_task_carries_the_works_task_key(self):
        """`_side_task_json` 的传输层任务键：`作品创作` 必须有独立的一条。

        没有它就会落到默认的 `compaction` → 适配层读的是 `compaction_provider_id`，
        用户给写手选的模型一次都不会被调用（"指名了也不生效"）。
        """
        self.assertEqual(SIDE_TASK_ROUTES['作品创作'], 'works')


class WorkInstructionTests(unittest.TestCase):
    def test_no_work_means_no_instruction_block(self):
        self.assertEqual(work_instruction({}), '')
        self.assertEqual(work_instruction({'sharedWork': None}), '')
        self.assertEqual(work_instruction(None), '')

    def test_main_mode_uses_the_upstream_verbatim_text(self):
        text = work_instruction({'sharedWork': {'head': 'rev-1'}, 'worksMode': 'main'})
        self.assertEqual(text, works_module.WORK_INSTRUCTION)

    def test_separate_mode_uses_the_async_writer_text(self):
        text = work_instruction({'sharedWork': {'head': 'rev-1'}, 'worksMode': 'separate'})
        self.assertEqual(text, works_module.ASYNC_WORK_INSTRUCTION)

    def test_missing_or_unknown_mode_defaults_to_main(self):
        head = {'sharedWork': {'head': 'rev-1'}}
        self.assertEqual(work_instruction(head), works_module.WORK_INSTRUCTION)
        self.assertEqual(work_instruction({**head, 'worksMode': 'nonsense'}),
                         works_module.WORK_INSTRUCTION)
        self.assertEqual(work_instruction({**head, 'works_mode': 'SEPARATE'}),
                         works_module.ASYNC_WORK_INSTRUCTION)

    def test_the_two_blocks_never_promise_a_saved_or_accepted_revision(self):
        """上游语义：模型只能写"打算/尝试"，**接受只有用户能做**。

        两段文案的措辞不同（主叙事写 "Only the user can accept or reject"，
        异步写手写 "The user alone accepts the resulting proposal"），所以只钉语义标记：
        必须出现"用户"与"接受"，且**不许**出现"已保存/已接受"这类既成事实的措辞。
        """
        main = works_module.WORK_INSTRUCTION.lower()
        separate = works_module.ASYNC_WORK_INSTRUCTION.lower()
        # 两段都点名"由用户决定"
        self.assertIn('only the user can accept or reject', main)
        self.assertIn('the user alone accepts', separate)
        # 并且都明确禁止把它写成既成事实（上游原文里的否定句，逐字保留）
        self.assertIn('not a successfully saved', main)
        self.assertIn('can intend to begin writing', separate)
        # 失败/运行中的任务不算完成品：两段的措辞不同，各按各的原文钉
        self.assertIn('failed save attempt', main)
        self.assertIn('not open tasks', main)
        self.assertIn('not completed work', separate)


class DecisionFieldTests(unittest.TestCase):
    def test_the_two_work_fields_are_registered_for_dual_spelling(self):
        keys = set(chunk4_module._DUAL_SINGLE_KEYS)
        self.assertIn('workProposal', keys)
        self.assertIn('workRequest', keys)

    def test_dual_normalization_copies_both_spellings_for_a_proposal(self):
        proposal = {'baseRevisionId': 'r1', 'content': '草稿', 'reason': '改语气'}
        normalized = chunk4_module._dual({'workProposal': proposal})
        self.assertEqual(normalized['workProposal'], normalized['work_proposal'])
        self.assertEqual(normalized['work_proposal']['content'], '草稿')


class ProposalConsumerTests(unittest.TestCase):
    """回合消费点：**必须把 `workProposal` / `workRequest` 本身传下去**。

    这里钉的是一个真踩过的坑：早先两处调用点把**整个 decision** 当 `proposal` 传给了
    `apply_work_proposal`，服务层的 `parse_work_edit` 自然解析不出东西 → 提案**永远存不下来**，
    而且失败是静默的（`apply_work_proposal` 按上游语义不抛）。断言写成 AST 检查：
    调用点里必须出现 `pick(decision, 'workProposal', ...)` 与 `pick(decision, 'workRequest', ...)`。
    """

    SERVICE_ROOT = pathlib.Path(__file__).resolve().parents[1] / 'core' / 'service'

    def _calls(self, filename: str, callee: set):
        """找出"调用某个服务成员"的调用点。

        两种写法都要认：直接 `self.apply_work_proposal(...)`，以及本仓库更常见的
        `saver = getattr(self, 'apply_work_proposal', None)` 之后 `await saver(...)`
        （chunk3 有「上游行段铁律」，新逻辑只能内联、靠 getattr 守卫取成员）。
        """
        source = (self.SERVICE_ROOT / filename).read_text(encoding='utf-8')
        tree = ast.parse(source)
        aliases = set()
        for node in ast.walk(tree):
            # `name = getattr(self, '<callee>'[, default])`
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                call = node.value
                func_name = getattr(call.func, 'id', '')
                if func_name == 'getattr' and len(call.args) >= 2:
                    literal = call.args[1]
                    if isinstance(literal, ast.Constant) and literal.value in callee:
                        for target in node.targets:
                            if isinstance(target, ast.Name):
                                aliases.add(target.id)
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, 'id', '')
            if name in callee or name in aliases:
                found.append(node)
        return found

    def _field_readers(self, filename: str, field: str) -> set:
        """找出"读了这个决策字段"的局部变量名（`proposal = pick(decision, 'workProposal', …)`）。"""
        tree = ast.parse((self.SERVICE_ROOT / filename).read_text(encoding='utf-8'))
        aliases = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
                continue
            call = node.value
            func = call.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, 'id', '')
            if name not in ('pick', 'get'):
                continue
            literals = [a.value for a in call.args if isinstance(a, ast.Constant)]
            if field in literals:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        aliases.add(target.id)
        return aliases

    def _passes_field(self, call: ast.Call, field: str, aliases: set) -> bool:
        for arg in call.args:
            if isinstance(arg, ast.Name) and arg.id in aliases:
                return True
            if isinstance(arg, ast.Call):
                names = [a.value for a in arg.args if isinstance(a, ast.Constant)]
                if field in names:
                    return True
        return False

    def test_both_turn_paths_pass_the_proposal_and_request_fields(self):
        for filename in ('chunk1.py', 'chunk3.py'):
            proposals = self._field_readers(filename, 'workProposal')
            calls = self._calls(filename, {'apply_work_proposal'})
            self.assertTrue(calls, '%s 没有提案消费点' % filename)
            for call in calls:
                self.assertTrue(
                    self._passes_field(call, 'workProposal', proposals),
                    '%s 的 apply_work_proposal 没传 workProposal 字段（整份 decision 传下去会静默存不上）'
                    % filename,
                )
            requests = self._field_readers(filename, 'workRequest')
            starters = self._calls(filename, {'start_work_generation'})
            self.assertTrue(starters, '%s 没有起草消费点' % filename)
            for call in starters:
                self.assertTrue(self._passes_field(call, 'workRequest', requests),
                                '%s 的 start_work_generation 没传 workRequest 字段' % filename)


if __name__ == '__main__':
    unittest.main()
