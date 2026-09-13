"""上游 `upstream/test/dialogue-burst.test.ts` 的逐条移植（stdlib `unittest`）。

两个用例都断言 `src/script/scene-frame.ts` 里 `resolveDialogueBurst` 的身份规则：
1. burst 身份跟随**场景身份**，而不是闲置时长或消息条数；
2. burst 只在一个显式事件边界、作用域变化或无关话题时改变。

两条 `new Date('...Z')` 都写成 timezone-aware UTC `datetime`。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from plugin.core.script.scene_frame import project_scene_frame, resolve_dialogue_burst
from plugin.core.types import empty_story_state

_UTC = timezone.utc


def _dt(text: str) -> datetime:
    """上游 `new Date('2026-09-04T08:00:00Z')`。"""
    return datetime.fromisoformat(text.replace('Z', '+00:00')).astimezone(_UTC)


class DialogueBurstTest(unittest.TestCase):
    """`test_dialogue_burst.py` 对应用上游 `dialogue-burst.test.ts`。"""

    def test_identity_follows_scene_identity_rather_than_inactivity_or_message_count(self) -> None:
        """DialogueBurst identity follows scene identity rather than inactivity or message count."""
        state = empty_story_state()
        frame = project_scene_frame({'story_id': 'story', 'now': _dt('2026-09-04T08:00:00Z'), 'scene': None, 'state': state})
        first = resolve_dialogue_burst(frame, None, _dt('2026-09-04T00:00:00Z'))
        much_later = resolve_dialogue_burst(frame, first, _dt('2026-09-05T00:00:00Z'))
        self.assertEqual(much_later['id'], first['id'])

        next_frame = project_scene_frame({
            'story_id': 'story', 'now': _dt('2026-09-05T00:00:00Z'),
            'scene': {
                'id': 2, 'story_id': 'story', 'status': 'active', 'started_at': _dt('2026-09-05T00:00:00Z'),
                'ended_at': None, 'hook': '', 'summary': '', 'entry_count': 0, 'last_entry_id': None,
                'created_at': _dt('2026-09-05T00:00:00Z'), 'updated_at': _dt('2026-09-05T00:00:00Z'),
            },
            'state': state,
        })
        self.assertNotEqual(
            resolve_dialogue_burst(next_frame, first, _dt('2026-09-05T00:00:00Z'))['id'], first['id'],
        )

    def test_changes_on_an_explicit_event_boundary_scope_change_or_unrelated_topic(self) -> None:
        """DialogueBurst changes on an explicit event boundary, scope change, or unrelated topic."""
        state = empty_story_state()
        frame = project_scene_frame({'story_id': 'story', 'now': _dt('2026-09-04T08:00:00Z'), 'scene': None, 'state': state})
        first = resolve_dialogue_burst(
            frame, None, _dt('2026-09-04T08:00:00Z'),
            {'scope': 'alice', 'topic_text': '昨天的奶茶拿到了吗'},
        )
        self.assertEqual(resolve_dialogue_burst(
            frame, first, _dt('2026-09-05T08:00:00Z'),
            {'scope': 'alice', 'topic_text': '那取餐码还记得吗'},
        )['id'], first['id'])
        self.assertNotEqual(resolve_dialogue_burst(
            frame, first, _dt('2026-09-04T08:01:00Z'),
            {'scope': 'bob', 'topic_text': '昨天的奶茶拿到了吗'},
        )['id'], first['id'])
        self.assertNotEqual(resolve_dialogue_burst(
            frame, first, _dt('2026-09-04T08:02:00Z'),
            {'scope': 'alice', 'topic_text': '新买的键盘怎么样'},
        )['id'], first['id'])
        self.assertNotEqual(resolve_dialogue_burst(
            frame, first, _dt('2026-09-04T08:03:00Z'),
            {'scope': 'alice', 'topic_text': '昨天的奶茶拿到了吗', 'boundary': True},
        )['id'], first['id'])


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
