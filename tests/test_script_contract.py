"""M4 显式端点：`core/script/contract.py` 的落点（上游 `src/script/contract.ts:30,65,91`）。

上游把「选中的投递端点」当作**作者化事件**的一部分：`ScriptEventDraft.endpointId`
落进提交，`messageEventReference()` 再把它折进 `ScriptMessageEventReference`
（`...(event.endpointId ? { endpointId } : {})`），因此**没选端点时那个键根本不存在**
（不是空串）。

键名法：上游 wire / 模型可见的 JSON 写 camelCase ``endpointId``，本移植版内部 dict
写 snake_case ``endpoint_id``；读入两种拼写都认、优先 camelCase（`read_endpoint_id`）。
"""

from __future__ import annotations

import unittest

from plugin.core.script.contract import message_event_reference, read_endpoint_id

_REFERENCE_KEYS_WITHOUT_ENDPOINT = [
    'commit_id', 'event_id', 'script_entry_id', 'event_kind',
    'caused_by_event_ids', 'full_content', 'bubble_index', 'bubble_count',
]
_REFERENCE_KEYS_WITH_ENDPOINT = [
    'commit_id', 'event_id', 'script_entry_id', 'event_kind',
    'caused_by_event_ids', 'endpoint_id', 'full_content', 'bubble_index', 'bubble_count',
]


def _event(**overrides: object) -> dict:
    event = {
        'commit_id': 'commit:x', 'event_id': 'commit:x:e2', 'kind': 'outgoing-message',
        'actor': 'protagonist', 'occurred_at': '2026-10-05T00:00:00.000Z',
        'caused_by_event_ids': ['commit:x:e1'], 'participant_id': 'alice',
        'content': '第一句', 'bubbles': ['第一句'], 'delivery_mode': 'immediate',
    }
    event.update(overrides)
    return event


class ScriptContractEndpointIdTest(unittest.TestCase):
    def test_the_selected_endpoint_rides_the_reference_with_the_upstream_key_order(self) -> None:
        reference = message_event_reference(_event(endpoint_id='ep-explicit'), 0, 7)
        self.assertEqual(reference['endpoint_id'], 'ep-explicit')
        # 上游展开顺序：endpointId 夹在 causedByEventIds 与 fullContent 之间。
        self.assertEqual(list(reference.keys()), _REFERENCE_KEYS_WITH_ENDPOINT)

    def test_an_event_without_a_selected_endpoint_adds_no_key_at_all(self) -> None:
        reference = message_event_reference(_event(), 0, 7)
        self.assertNotIn('endpoint_id', reference)
        self.assertNotIn('endpointId', reference)
        self.assertEqual(list(reference.keys()), _REFERENCE_KEYS_WITHOUT_ENDPOINT)
        # 空串在上游真值判定里等于「没选端点」，不许落成"选了空端点"。
        self.assertNotIn('endpoint_id', message_event_reference(_event(endpoint_id='')))
        self.assertNotIn('endpoint_id', message_event_reference(_event(endpointId='')))

    def test_both_spellings_are_accepted_and_camel_case_wins(self) -> None:
        self.assertEqual(read_endpoint_id({'endpoint_id': 'ep-snake'}), 'ep-snake')
        self.assertEqual(read_endpoint_id({'endpointId': 'ep-camel'}), 'ep-camel')
        self.assertEqual(
            read_endpoint_id({'endpointId': 'ep-camel', 'endpoint_id': 'ep-snake'}), 'ep-camel',
        )
        # 只认非空字符串：缺键 / 空串 / None / 数字一律当"没选端点"。
        self.assertEqual(read_endpoint_id({}), '')
        self.assertEqual(read_endpoint_id({'endpoint_id': ''}), '')
        self.assertEqual(read_endpoint_id({'endpoint_id': 7}), '')
        self.assertEqual(read_endpoint_id({'endpointId': None, 'endpoint_id': 'ep-snake'}), 'ep-snake')
        self.assertEqual(read_endpoint_id(None), '')
        # camelCase 事件（旧 JSON / 兄弟模块）也照原值折进内部 snake_case 引用。
        self.assertEqual(
            message_event_reference(_event(endpointId='ep-camel'))['endpoint_id'], 'ep-camel',
        )


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
