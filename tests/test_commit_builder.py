"""M4 显式端点：`core/script/commit_builder.py` 的落点（上游 `src/script/commit-builder.ts:72,192`）。

上游只在**跨会话发言**（`crossConversationActions`）上把模型选的端点带进剧本事件：
`addMessageEvent({ ..., endpointId: action.endpointId, ... })`，而它内部是
`...(input.endpointId ? { endpointId } : {})`——**缺省不建这个键**。立即回复与群消息
事件都没有这个字段（它们各自的端点由投递层决定），这里逐字对账、不扩散。
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from plugin.core.script.commit_builder import (
    decision_to_script_commit,
    find_group_script_event,
    find_outgoing_script_event,
)

_UTC = timezone.utc


def _input(action: dict) -> dict:
    return {
        'story_id': 'story-ep', 'participant_id': 'alice', 'phase': 'user-message',
        'from': datetime(2026, 10, 5, 0, 0, 0, tzinfo=_UTC),
        'now': datetime(2026, 10, 5, 0, 1, 0, tzinfo=_UTC),
        'message_separator': '<sep/>', 'split_reply_messages': True,
        'frame_id': 'frame:ep', 'burst_id': 'burst:ep',
        'group_reply_content': '群里回一句',
        'decision': {
            'script': '她回了私聊，又在群里说了一句。',
            'interaction': {'seen': True, 'reply': {'mode': 'immediate', 'content': '私聊回复'}},
            'cross_conversation_actions': [action],
        },
    }


def _action(**overrides: object) -> dict:
    action = {
        'participant_id': 'bob', 'mode': 'immediate', 'content': '换个端点说',
    }
    action.update(overrides)
    return action


class CommitBuilderEndpointIdTest(unittest.TestCase):
    def test_the_chosen_endpoint_lands_on_the_authored_event_verbatim(self) -> None:
        commit = decision_to_script_commit(_input(_action(endpoint_id='ep-explicit')))
        event = find_outgoing_script_event(commit, 'bob')
        self.assertIsNotNone(event)
        self.assertEqual(event['endpoint_id'], 'ep-explicit')

    def test_no_endpoint_means_no_key_not_an_empty_string(self) -> None:
        commit = decision_to_script_commit(_input(_action()))
        event = find_outgoing_script_event(commit, 'bob')
        self.assertNotIn('endpoint_id', event)
        self.assertNotIn('endpointId', event)
        self.assertNotIn('endpoint_id', json.dumps(commit, ensure_ascii=False))

    def test_both_spellings_are_accepted_and_camel_case_wins(self) -> None:
        snake = find_outgoing_script_event(
            decision_to_script_commit(_input(_action(endpoint_id='ep-snake'))), 'bob',
        )
        camel = find_outgoing_script_event(
            decision_to_script_commit(_input(_action(endpointId='ep-camel'))), 'bob',
        )
        both = find_outgoing_script_event(
            decision_to_script_commit(_input(_action(endpointId='ep-camel', endpoint_id='ep-snake'))), 'bob',
        )
        self.assertEqual(snake['endpoint_id'], 'ep-snake')
        self.assertEqual(camel['endpoint_id'], 'ep-camel')
        self.assertEqual(both['endpoint_id'], 'ep-camel')
        # 空串 / None 不算选了端点：缺省口径与"根本没写这个字段"逐字一致。
        for empty in ('', None):
            event = find_outgoing_script_event(
                decision_to_script_commit(_input(_action(endpoint_id=empty))), 'bob',
            )
            self.assertNotIn('endpoint_id', event)

    def test_the_field_does_not_spread_to_the_reply_or_group_events(self) -> None:
        """上游只给跨会话发言带 endpointId：本回合的立即回复与群消息事件不许凭空多键。"""
        commit = decision_to_script_commit(_input(_action(endpoint_id='ep-explicit')))
        reply = find_outgoing_script_event(commit, 'alice')
        group = find_group_script_event(commit)
        self.assertNotIn('endpoint_id', reply)
        self.assertNotIn('endpoint_id', group)

    def test_the_default_path_stays_byte_identical(self) -> None:
        """反向：没有端点选择的回合，事件键集合与本改动前逐字一致。"""
        commit = decision_to_script_commit(_input(_action()))
        outgoing_keys = [
            'kind', 'actor', 'occurred_at', 'caused_by_event_ids', 'participant_id',
            'content', 'bubbles', 'delivery_mode', 'script_binding',
            'commit_id', 'event_id',
        ]
        group_keys = [
            'kind', 'actor', 'occurred_at', 'caused_by_event_ids',
            'content', 'bubbles', 'delivery_mode', 'script_binding',
            'commit_id', 'event_id',
        ]
        for event in commit['events']:
            if event['kind'] == 'outgoing-message':
                self.assertEqual(list(event.keys()), outgoing_keys)
            elif event['kind'] == 'group-message':
                self.assertEqual(list(event.keys()), group_keys)


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
