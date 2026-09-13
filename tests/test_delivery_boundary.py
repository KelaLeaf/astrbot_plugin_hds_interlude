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


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
