"""M4 显式端点：`core/turn_persistence.py` 的落点（上游 `src/turn-persistence.ts`）。

上游 `scriptEntryDraftForCommit()` 把整份 `commit.events` 原样存进 metadata 的
``scriptEvents``——**显式端点就藏在那份事件列表里**（每个事件自带 ``endpointId``），
上游**没有**顶层端点字段（一次提交可以有多条发往不同端点的发言，顶层标量会撒谎）。
本移植版逐字对账：`script_events[i].endpoint_id` 只在确实选了端点的事件上出现。
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from plugin.core.long_arc import resolve_conversation_weight
from plugin.core.script.commit_builder import decision_to_script_commit
from plugin.core.script.contract import message_event_reference
from plugin.core.turn_persistence import script_entry_draft_for_commit

_UTC = timezone.utc
_FROM = datetime(2026, 10, 5, 0, 0, 0, tzinfo=_UTC)
_NOW = datetime(2026, 10, 5, 0, 1, 0, tzinfo=_UTC)


def _commit(action: dict) -> dict:
    return decision_to_script_commit({
        'story_id': 'story-persist', 'participant_id': 'alice', 'phase': 'user-message',
        'from': datetime(2026, 10, 5, 0, 0, 0, tzinfo=_UTC),
        'now': datetime(2026, 10, 5, 0, 1, 0, tzinfo=_UTC),
        'message_separator': '<sep/>', 'split_reply_messages': True,
        'frame_id': 'frame:p', 'burst_id': 'burst:p',
        'decision': {
            'script': '她回了私聊。',
            'interaction': {'seen': True, 'reply': {'mode': 'delayed', 'content': '稍后回',
                                                    'send_at': '2026-10-05T00:05:00.000Z'}},
            'cross_conversation_actions': [action],
        },
    })


def _action(**overrides: object) -> dict:
    action = {'participant_id': 'bob', 'mode': 'delayed', 'content': '换个端点说',
              'send_at': '2026-10-05T00:09:00.000Z'}
    action.update(overrides)
    return action


def _event_of(draft: dict, participant_id: str) -> dict:
    return next(
        event for event in draft['metadata']['script_events']
        if event.get('participant_id') == participant_id and event['kind'] == 'outgoing-message'
    )


class TurnPersistenceEndpointIdTest(unittest.TestCase):
    def test_the_selected_endpoint_is_persisted_verbatim_inside_metadata(self) -> None:
        commit = _commit(_action(endpoint_id='ep-explicit'))
        draft = script_entry_draft_for_commit(commit, None)
        event = _event_of(draft, 'bob')
        self.assertEqual(event['endpoint_id'], 'ep-explicit')
        # 落库的那一份就是 commit 里的那一份（不复制、不改写、不改名）。
        self.assertEqual(draft['metadata']['script_events'], commit['events'])
        # 同一次提交里没选端点的事件照旧没有这个键。
        self.assertNotIn('endpoint_id', _event_of(draft, 'alice'))

    def test_without_a_selected_endpoint_metadata_has_no_endpoint_key_at_all(self) -> None:
        draft = script_entry_draft_for_commit(_commit(_action()), None)
        serialized = json.dumps(draft['metadata'], ensure_ascii=False)
        self.assertNotIn('endpoint_id', serialized)
        self.assertNotIn('endpointId', serialized)
        self.assertNotIn('endpoint', serialized)

    def test_the_persisted_event_reads_back_as_the_same_reference_endpoint(self) -> None:
        """从**落库的 metadata** 读回来：同一条端点上事件 / 引用 / 恢复值三处一致。"""
        commit = _commit(_action(endpoint_id='ep-explicit'))
        draft = script_entry_draft_for_commit(commit, None)
        event = _event_of(draft, 'bob')
        self.assertEqual(message_event_reference(event, 0, 7)['endpoint_id'], 'ep-explicit')
        legacy = script_entry_draft_for_commit(_commit(_action()), None)
        legacy_reference = message_event_reference(_event_of(legacy, 'bob'), 0, 7)
        self.assertNotIn('endpoint_id', legacy_reference)


# =========================================================================== #
# 缺口 A：`metadata.conversation_kind` 的**落库侧**
# 上游 `src/turn-persistence.ts:14-28`（`scriptEntryDraftForCommit` 的
# `hasGroupEvent` / `hasPrivateEvent` → `conversationKind`）。
#
# 为什么必须先有落库侧：长线叙事评分（`core/long_arc.py:resolve_conversation_kind`）
# 读这个键给群聊按 0.5、私聊按 1.0 计权。没有写入方时，群回合只能靠"事件账本反推"
# 或退化成旧剧本行的"约定私聊"，两条路都不稳。
# =========================================================================== #

def _commit_of(decision: dict, group_reply_content: str = '') -> dict:
    """按**生产写入方**（`chunk4.persist_decision` → `decision_to_script_commit`）的写法造提交。

    `group_reply_content` 是 `decision_to_script_commit` **入参**（不是 decision 里的键）
    ——与 `chunk4.py` 的同名入参逐字同形，别在夹具里另创写法（坑 39/66）。
    """
    return decision_to_script_commit({
        'story_id': 'story-persist', 'participant_id': 'alice', 'phase': 'user-message',
        'from': _FROM, 'now': _NOW,
        'message_separator': '<sep/>', 'split_reply_messages': True,
        'frame_id': 'frame:p', 'burst_id': 'burst:p',
        'group_reply_content': group_reply_content,
        'decision': decision,
    })


_PRIVATE_REPLY = {'seen': True, 'reply': {'mode': 'immediate', 'content': '私聊回一句'}}


class ConversationKindPersistTests(unittest.TestCase):
    """群聊落 `group`、私聊落 `private`（断言**具体值**）。"""

    def test_a_group_commit_persists_conversation_kind_group(self) -> None:
        commit = _commit_of({'script': '她在群里说了句话。'}, '群里回一句')
        self.assertIn('group-message', [event['kind'] for event in commit['events']])
        self.assertEqual(
            script_entry_draft_for_commit(commit, None)['metadata']['conversation_kind'], 'group',
        )

    def test_a_private_commit_persists_conversation_kind_private(self) -> None:
        commit = _commit_of({'script': '她私下回了句。', 'interaction': dict(_PRIVATE_REPLY)})
        self.assertIn('outgoing-message', [event['kind'] for event in commit['events']])
        self.assertEqual(
            script_entry_draft_for_commit(commit, None)['metadata']['conversation_kind'], 'private',
        )

    def test_the_persisted_key_is_snake_case_only(self) -> None:
        """metadata 键名法：写出侧只写 `conversation_kind`，不写上游 camelCase。

        反向：把写入键改成 `conversationKind` → 本用例当场红（读侧虽兼容，但落库那一份
        必须守本仓的 metadata snake_case 约定）。
        """
        metadata = script_entry_draft_for_commit(_commit_of({'script': 'x'}, '群里回'), None)['metadata']
        self.assertEqual(metadata['conversation_kind'], 'group')
        self.assertNotIn('conversationKind', metadata)

    def test_a_commit_spanning_both_conversations_persists_unknown(self) -> None:
        """上游 `:16`：两种事件都在 → 显式 `unknown`（不许猜成其中一种）。

        反向：删掉这条分支（退回"先看群消息就算 group"或"有私聊回复就算 private"）→ 红。
        """
        commit = _commit_of(
            {'script': '两个会话都说了话。', 'interaction': dict(_PRIVATE_REPLY)}, '群里也回一句',
        )
        kinds = {event['kind'] for event in commit['events']}
        self.assertEqual(kinds & {'group-message', 'outgoing-message'}, {'group-message', 'outgoing-message'})
        self.assertEqual(
            script_entry_draft_for_commit(commit, None)['metadata']['conversation_kind'], 'unknown',
        )


class ConversationKindScoringTests(unittest.TestCase):
    """写入的那个值就是评分读到的那个值（跨模块接线，不是自说自话）。"""

    def test_group_entries_are_worth_half_and_private_entries_full(self) -> None:
        group = script_entry_draft_for_commit(_commit_of({'script': 'x'}, '群里回一句'), None)
        private = script_entry_draft_for_commit(
            _commit_of({'script': 'x', 'interaction': dict(_PRIVATE_REPLY)}), None,
        )
        self.assertEqual(resolve_conversation_weight(group, None), 0.5)
        self.assertEqual(resolve_conversation_weight(private, None), 1.0)

    def test_a_group_entry_straddling_both_conversations_is_never_scored_as_private(self) -> None:
        """③ 反向：拿不准时群聊那一半**不会**被按私聊全额（1.0）计分。

        这条提交里有 `group-message` 事件，若写入侧把它猜成 `private`（或干脆不写、
        让读侧按事件账本之外的口径兜底），权重就会变成 1.0。
        """
        mixed = script_entry_draft_for_commit(
            _commit_of(
                {'script': 'x', 'interaction': dict(_PRIVATE_REPLY)}, '群里也回一句',
            ), None,
        )
        self.assertEqual(resolve_conversation_weight(mixed, None), 0.0)
        self.assertNotEqual(resolve_conversation_weight(mixed, None), 1.0)


class ConversationKindUnknownTests(unittest.TestCase):
    """③ 会话类型不可知 → **不写键**（不许缺省成 private）。"""

    def test_a_commit_without_any_delivery_event_writes_no_key(self) -> None:
        """只有 narrative 事件（拿不到会话类型）→ 两种拼写都不落。

        反向：写入侧缺省猜成 `private` → `assertNotIn` 立刻红。猜成 private 的后果是
        一个真正的群回合会被 `resolve_conversation_weight` 按 1.0 全额计分。
        """
        commit = _commit_of({'script': '她一个人待着。'})
        self.assertEqual([event['kind'] for event in commit['events']], ['narrative'])
        metadata = script_entry_draft_for_commit(commit, None)['metadata']
        self.assertNotIn('conversation_kind', metadata)
        self.assertNotIn('conversationKind', metadata)
        # 落库那一份序列化之后也不能出现这个键（任何拼写）。
        serialized = json.dumps(metadata, ensure_ascii=False)
        self.assertNotIn('conversation_kind', serialized)
        self.assertNotIn('conversationKind', serialized)

    def test_an_empty_event_list_writes_no_key_either(self) -> None:
        """空事件列表是同一支（上游 `events.some(...)` 恒 false）。"""
        draft = script_entry_draft_for_commit({'prose': 'x', 'events': [], 'window': {}}, None)
        self.assertNotIn('conversation_kind', draft['metadata'])


if __name__ == '__main__':  # pragma: no cover
    unittest.main()