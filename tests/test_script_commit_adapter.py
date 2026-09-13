"""上游 `upstream/test/script-commit-adapter.test.ts` 的逐条移植（stdlib `unittest`）。

上游文件只有两个用例，主体都属于 `src/script/commit-builder.ts`（本文件负责）：

* `decisionToScriptCommit` 的确定性（同一输入 → 同一 commitId）、事件因果链、
  即时气泡绑定、延迟动作的 `future` 绑定、群消息事件；
* 缺失/歧义散文动作时保持「诊断可见、兼容投递不变」。

上游还 import 了 `src/script/validator.ts` 的 `validateScriptCommit`，在两次断言里
用它做结构校验。`validator.ts` → `plugin/core/script/validator.py` **不在本任务的
文件清单内**（由并行移植任务负责），所以这里把它拆成两个独立的用例：
`validator.py` 未落地时显式 `SkipTest`，**断言逐字保留、不改弱、不删**，
落地后自动生效。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from plugin.core.script.commit_builder import (
    decision_to_script_commit,
    find_group_script_event,
    find_outgoing_script_event,
    unbound_immediate_message_events,
)
from plugin.core.script.contract import message_event_reference

try:  # 归属 `core/script/validator.py` 移植任务（并行进行）。
    from plugin.core.script.validator import validate_script_commit
except ImportError:  # pragma: no cover - 仅在并行模块尚未落地时生效
    validate_script_commit = None  # type: ignore[assignment]

_VALIDATOR_SKIP = '归属 core/script/validator.py 移植任务'

_UTC = timezone.utc


def _first_input() -> dict:
    """上游第一个用例的 `input` 字面量。"""
    return {
        'story_id': 'story-1', 'participant_id': 'alice', 'phase': 'user-message',
        'from': datetime(2026, 9, 4, 0, 0, 0, tzinfo=_UTC), 'now': datetime(2026, 9, 4, 0, 3, 0, tzinfo=_UTC),
        'message_separator': '<sep/>', 'split_reply_messages': True,
        'frame_id': 'frame:one', 'burst_id': 'burst:one',
        'group_reply_content': '群里说一句',
        'decision': {
            'script': '她停下手里的事，发出“我看见了”，又接着发出“让我想一下”；随后在群里写下“群里说一句”。',
            'interaction': {'seen': True, 'reply': {'mode': 'immediate', 'content': '我看见了<sep/>让我想一下'}},
            'cross_conversation_actions': [{
                'participant_id': 'bob', 'mode': 'delayed', 'content': '晚点说',
                'send_at': '2026-09-04T00:10:00.000Z',
            }],
        },
    }


def _second_input() -> dict:
    """上游第二个用例的 `decisionToScriptCommit` 入参。"""
    return {
        'story_id': 'story-2', 'participant_id': 'alice', 'phase': 'user-message',
        'from': datetime(2026, 9, 4, 0, 0, 0, tzinfo=_UTC), 'now': datetime(2026, 9, 4, 0, 1, 0, tzinfo=_UTC),
        'frame_id': 'frame:two', 'burst_id': 'burst:two',
        'decision': {
            'script': '她看完以后立即回了过去，但这段旧式输出没有写出消息原文。',
            'interaction': {'seen': True, 'reply': {'mode': 'immediate', 'content': '知道了'}},
        },
    }


class ScriptCommitAdapterTest(unittest.TestCase):
    """`test_script_commit_adapter.py` 对应用上游 `script-commit-adapter.test.ts`。"""

    def test_script_first_output_becomes_one_deterministic_host_owned_commit(self) -> None:
        """script-first output becomes one deterministic host-owned commit with causal message events."""
        input = _first_input()
        first = decision_to_script_commit(input)
        second = decision_to_script_commit(input)
        self.assertEqual(first['commit_id'], second['commit_id'])
        self.assertEqual(first['source_format'], 'script-first-v1')
        self.assertEqual(first['events'], second['events'])
        # 上游此处还有 `assert.deepEqual(validateScriptCommit(first), { valid: true, errors: [] })`。
        # 该断言依赖 `core/script/validator.py`（并行任务），已原样搬到
        # `test_script_first_commit_passes_structural_validation`，不删不改。
        self.assertNotEqual(decision_to_script_commit({
            **input,
            'decision': {**input['decision'], 'interaction': {'seen': True, 'reply': {'mode': 'immediate', 'content': '不同的实际消息'}}},
        })['commit_id'], first['commit_id'])

        outgoing = find_outgoing_script_event(first, 'alice', 'immediate', '我看见了<sep/>让我想一下')
        self.assertEqual(outgoing['bubbles'], ['我看见了', '让我想一下'])
        self.assertEqual(len(outgoing['caused_by_event_ids']), 1)
        self.assertEqual(outgoing['script_binding']['status'], 'bound')
        self.assertEqual(len(outgoing['script_binding']['spans']), 2)
        self.assertEqual(message_event_reference(outgoing)['commit_id'], first['commit_id'])
        self.assertEqual(find_group_script_event(first)['content'], '群里说一句')
        self.assertEqual(find_group_script_event(first)['script_binding']['status'], 'bound')
        self.assertEqual(
            next(
                event for event in first['events']
                if event['kind'] == 'outgoing-message' and event.get('delivery_mode') == 'delayed'
            )['script_binding']['status'],
            'future',
        )
        self.assertEqual(unbound_immediate_message_events(first), [])

    def test_an_absent_or_ambiguous_prose_action_is_diagnostic_and_keeps_compatibility_delivery_intact(self) -> None:
        """an absent or ambiguous prose action is diagnostic and keeps compatibility delivery intact."""
        commit = decision_to_script_commit(_second_input())
        unbound = unbound_immediate_message_events(commit)
        self.assertEqual(len(unbound), 1)
        self.assertEqual(unbound[0]['script_binding']['status'], 'unbound')
        self.assertEqual(find_outgoing_script_event(commit, 'alice')['content'], '知道了')
        # 上游此处还有 `assert.deepEqual(validateScriptCommit(commit), { valid: true, errors: [] })`。
        # 见 `test_second_commit_passes_structural_validation`。

    def test_script_first_commit_passes_structural_validation(self) -> None:
        """上游用例一的 `validateScriptCommit(first)` 断言（归属 `core/script/validator.py`）。"""
        if validate_script_commit is None:
            raise unittest.SkipTest(_VALIDATOR_SKIP)
        first = decision_to_script_commit(_first_input())
        self.assertEqual(validate_script_commit(first), {'valid': True, 'errors': []})

    def test_second_commit_passes_structural_validation(self) -> None:
        """上游用例二的 `validateScriptCommit(commit)` 断言（归属 `core/script/validator.py`）。"""
        if validate_script_commit is None:
            raise unittest.SkipTest(_VALIDATOR_SKIP)
        commit = decision_to_script_commit(_second_input())
        self.assertEqual(validate_script_commit(commit), {'valid': True, 'errors': []})


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
