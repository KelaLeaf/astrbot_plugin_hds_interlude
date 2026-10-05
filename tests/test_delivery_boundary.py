"""投递边界测试（移植自上游 `upstream/test/delivery-boundary.test.ts`）。

上游：Koishi / TypeScript，v1.0.1-beta6-rebuild；用例逐条对照，断言全部保留。

覆盖：所有气泡在「投递意图 → 平台回执」全链路上共享同一个剧本事件身份
（``commitId`` / ``eventId``），只是 ``bubbleIndex`` 不同。
"""

from __future__ import annotations

import unittest

from plugin.core.delivery import (
    attach_message_event,
    delivery_entry_metadata,
    prepare_outgoing_delivery,
    restore_message_event,
    script_event_payload,
)
from plugin.core.script.contract import message_event_reference


class DeliveryBoundaryTest(unittest.TestCase):
    def test_all_bubbles_retain_the_script_event_identity_across_delivery_intents_and_receipts(self) -> None:
        # 上游用 camelCase 字面量构造事件；按 docs/PORT_PLAN.md §2 转 snake_case
        # （`plugin/core/script/contract.py` 的 `ScriptEventDraft` 即 snake_case）。
        event = {
            'commit_id': 'commit:x', 'event_id': 'commit:x:e2', 'kind': 'outgoing-message',
            'actor': 'protagonist', 'occurred_at': '2026-09-04T00:00:00.000Z',
            'caused_by_event_ids': ['commit:x:e1'], 'participant_id': 'alice',
            'content': '第一句<sep/>第二句', 'bubbles': ['第一句', '第二句'],
            'delivery_mode': 'immediate',
        }
        attached = attach_message_event({'participant_id': 'alice', 'content': event['content']}, event)
        prepared = prepare_outgoing_delivery(attached, event['bubbles'])
        self.assertIsNotNone(prepared)
        self.assertEqual(prepared['content'], '第一句')
        self.assertEqual(prepared['later_segments'], ['第二句'])
        self.assertEqual(prepared['script_event']['bubble_count'], 2)

        payload = {'content': '第二句', **script_event_payload(prepared, 1)}
        restored = restore_message_event(payload, '第二句')
        self.assertIsNotNone(restored)
        self.assertEqual(restored['event_id'], event['event_id'])
        self.assertEqual(restored['bubble_index'], 1)
        self.assertEqual(restored['full_content'], event['content'])
        self.assertEqual(
            delivery_entry_metadata({'participant_id': 'alice', 'content': '第二句',
                                     'script_event': restored})['event_id'],
            event['event_id'],
        )


class DeliveryEndpointIdTest(unittest.TestCase):
    """M4 显式端点：上游 `src/delivery.ts:6,42`（`attachMessageEvent` / `restoreMessageEvent`）。

    上游 `rc10-delivery.test.ts:277`「分段消息恢复显式 endpointId」——
    `deliverDueSplitSegments` 把 `scriptEvent.endpointId` 恢复到出站草稿上，
    到期分段的草稿因此带着同一个端点（我们 chunk6 的那处接线见交付说明）。
    """

    def _event(self, **overrides: object) -> dict:
        event = {
            'commit_id': 'commit:ep', 'event_id': 'commit:ep:e2', 'kind': 'outgoing-message',
            'actor': 'protagonist', 'occurred_at': '2026-10-05T00:00:00.000Z',
            'caused_by_event_ids': ['commit:ep:e1'], 'participant_id': 'alice',
            'content': '显式端点分段', 'bubbles': ['显式端点分段'],
            'delivery_mode': 'immediate',
        }
        event.update(overrides)
        return event

    def test_attach_brings_the_event_endpoint_onto_the_outgoing_draft(self) -> None:
        attached = attach_message_event(
            {'participant_id': 'alice', 'content': '显式端点分段'},
            self._event(endpoint_id='ep-explicit'), 7,
        )
        self.assertEqual(attached['endpoint_id'], 'ep-explicit')
        self.assertEqual(attached['script_event']['endpoint_id'], 'ep-explicit')
        # 草稿自己已经带了端点时不覆盖（上游 `!message.endpointId` 判定）。
        own = attach_message_event(
            {'participant_id': 'alice', 'content': '显式端点分段', 'endpoint_id': 'ep-own'},
            self._event(endpoint_id='ep-explicit'), 7,
        )
        self.assertEqual(own['endpoint_id'], 'ep-own')
        # 没选端点时草稿一个键都不多。
        bare = attach_message_event({'participant_id': 'alice', 'content': '显式端点分段'},
                                    self._event(), 7)
        self.assertNotIn('endpoint_id', bare)
        self.assertNotIn('endpoint_id', bare['script_event'])

    def test_a_due_split_segment_restores_the_same_endpoint_onto_its_draft(self) -> None:
        """上游 `rc10-delivery.test.ts:277`：到期分段的出站草稿带同一个 endpointId。"""
        attached = attach_message_event(
            {'participant_id': 'alice', 'content': '显式端点分段'},
            self._event(endpoint_id='ep-explicit'), 7,
        )
        prepared = prepare_outgoing_delivery(attached, ['显式端点分段'])
        payload = {'content': '显式端点分段', **script_event_payload(prepared, 1)}
        restored = restore_message_event(payload, '显式端点分段')
        self.assertEqual(restored['endpoint_id'], 'ep-explicit')
        draft = {
            'participant_id': 'alice', 'content': '显式端点分段',
            'script_event': restored,
        }
        if restored['endpoint_id']:
            draft['endpoint_id'] = restored['endpoint_id']
        self.assertEqual(draft['endpoint_id'], 'ep-explicit')
        self.assertEqual(draft['script_event']['endpoint_id'], 'ep-explicit')

    def test_metadata_carries_the_endpoint_only_when_one_was_selected(self) -> None:
        selected = delivery_entry_metadata({
            'participant_id': 'alice', 'content': '第二句',
            'script_event': restore_message_event(
                {'script_event': message_event_reference(self._event(endpoint_id='ep-explicit'))},
                '第二句',
            ),
        })
        self.assertEqual(selected['endpoint_id'], 'ep-explicit')
        legacy = delivery_entry_metadata({
            'participant_id': 'alice', 'content': '第二句',
            'script_event': message_event_reference(self._event()),
        })
        self.assertNotIn('endpoint_id', legacy)
        self.assertNotIn('endpointId', legacy)

    def test_a_legacy_payload_restores_byte_identically(self) -> None:
        """反向：历史 payload（没选端点）的恢复结果与本改动前逐字一致。"""
        legacy = restore_message_event(
            {'script_event': message_event_reference(self._event())}, '第二句',
        )
        self.assertEqual(list(legacy.keys()), [
            'commit_id', 'event_id', 'event_kind', 'caused_by_event_ids',
            'full_content', 'bubble_index', 'bubble_count',
        ])
        # 空串（旧的"选了但没值"写法）同样不落键。
        self.assertNotIn('endpoint_id', restore_message_event(
            {'script_event': {**message_event_reference(self._event()), 'endpoint_id': ''}},
            '第二句',
        ))
        # camelCase（上游 era / 旧 JSON）也认，值照抄。
        self.assertEqual(
            restore_message_event(
                {'script_event': {**message_event_reference(self._event()), 'endpointId': 'ep-camel'}},
                '第二句',
            )['endpoint_id'],
            'ep-camel',
        )


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
