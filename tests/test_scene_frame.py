"""上游 `upstream/test/scene-frame.test.ts` 的逐条移植（stdlib `unittest`）。

两个用例都断言 `src/script/scene-frame.ts` 的行为：
1. SceneFrame 只投影**有来源**的值，且 frame 身份不随消息密度改变；
2. 一次成功提交只追加 SceneDelta，绝不替换 frame，散文也不回灌成场景事实。

上游测试同时 import 了 `src/script/commit-builder`（用于造出一份真实 commit）。
那是本模块的依赖，不是被测对象，故这里直接调用已移植的 `decision_to_script_commit`，
断言一字不改。上游 `emptyStoryState()` / `interludeScene` / `scriptEntry` 字面量
按 `docs/PORT_PLAN.md` §2 的字段名映射写成 Python dict。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from plugin.core.script.commit_builder import decision_to_script_commit
from plugin.core.script.scene_frame import (
    advance_scene_frame,
    project_scene_frame,
    resolve_dialogue_burst,
    scene_frame_provenance_errors,
)
from plugin.core.time import iso
from plugin.core.types import empty_story_state

_UTC = timezone.utc

now = datetime(2026, 9, 4, 8, 0, 0, tzinfo=_UTC)
scene = {
    'id': 12, 'story_id': 'story', 'status': 'active',
    'started_at': datetime(2026, 9, 4, 7, 0, 0, tzinfo=_UTC), 'ended_at': None,
    'hook': '她还坐在窗边整理录音。', 'summary': '窗边的录音整理仍在继续。',
    'entry_count': 4, 'last_entry_id': 40, 'created_at': now, 'updated_at': now,
}


def script(entry_id: int, content: str) -> dict:
    """上游 `function script(id, content): ScriptEntry`。"""
    return {
        'id': entry_id, 'story_id': 'story', 'participant_id': '', 'kind': 'script',
        'actor': 'narrator', 'content': content, 'occurred_at': now, 'metadata': {}, 'created_at': now,
    }


class SceneFrameTest(unittest.TestCase):
    """`test_scene_frame.py` 对应用上游 `scene-frame.test.ts`。"""

    def test_frame_projects_only_sourced_values_and_keeps_one_id_across_message_densities(self) -> None:
        """SceneFrame projects only sourced values and keeps one id across message densities."""
        state = {
            **empty_story_state(), 'active_scene_id': 12,
            'scene_presence': [{
                'name': '希绘', 'status': 'present', 'basis': '一起坐下',
                'source_entry_ids': [31], 'updated_at': iso(now),
            }],
            'working_details': [{
                'label': '录音', 'value': '还剩最后两段',
                'created_at': iso(now), 'source_entry_ids': [35],
            }],
            'agency_window': {
                'activity_load': 'occupied', 'privacy': 'shared', 'device_access': 'limited',
                'valid_until': '2026-09-04T09:00:00.000Z', 'basis': '正在整理',
                'source_entry_ids': [36], 'updated_at': iso(now),
            },
        }

        def project(count: int) -> dict:
            return project_scene_frame({
                'story_id': 'story', 'now': now, 'scene': scene, 'state': state,
                'recent_entries': [
                    script(31, '希绘一起坐下。'), script(35, '录音还剩两段。'),
                    *[script(41 + index, f'她继续整理第 {index + 1} 段录音。') for index in range(count)],
                ],
            })

        one = project(1)
        five = project(5)
        ten = project(10)
        self.assertEqual(one['id'], five['id'])
        self.assertEqual(five['id'], ten['id'])
        self.assertEqual(scene_frame_provenance_errors(ten), [])
        self.assertEqual(ten['present_people'], ['希绘'])
        self.assertEqual(ten['device_access'], 'limited')

    def test_a_successful_commit_appends_a_scene_delta_without_replacing_the_frame(self) -> None:
        """a successful commit appends a SceneDelta without replacing the frame."""
        frame = project_scene_frame({
            'story_id': 'story', 'now': now, 'scene': scene, 'state': empty_story_state(),
            'recent_entries': [script(41, '她仍坐在窗边。')],
        })
        burst = resolve_dialogue_burst(frame, None, scene['started_at'])
        commit = decision_to_script_commit({
            'story_id': 'story', 'participant_id': 'friend', 'phase': 'user-message',
            'from': now, 'now': now,
            'frame_id': frame['id'], 'burst_id': burst['id'],
            'decision': {
                'script': '屏幕亮起来，她低头看见消息，手指停在波形上。',
                'interaction': {'seen': True, 'reply': {'mode': 'none'}},
            },
        })
        advanced = advance_scene_frame(frame, burst, commit, 42, now)
        self.assertEqual(advanced['frame']['id'], frame['id'])
        self.assertEqual(advanced['burst']['id'], burst['id'])
        self.assertEqual(advanced['burst']['last_event_id'], commit['events'][-1]['event_id'])
        self.assertFalse(42 in advanced['frame']['source_entry_ids'], '新 prose 不能回灌为场景事实')
        self.assertTrue(42 in advanced['burst']['source_entry_ids'], 'burst 只保留提交链路来源')
        self.assertNotRegex(
            advanced['frame'].get('narrative_focus') or '', '屏幕亮起来',
            'private prose must not leak through the shared frame',
        )
        self.assertEqual(scene_frame_provenance_errors(advanced['frame']), [])


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
