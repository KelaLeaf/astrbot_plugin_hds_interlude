"""上游 `upstream/test/context-compiler.test.ts` 的逐条移植（stdlib `unittest`）。

两个用例都断言 `src/script/context-compiler.ts` 的行为：
1. 编译器只产出**一份**七段脚手架，且不重算共享事实（引用必须复用）；
2. 已经出现在 `recentScript` 里的场景证据不会被二次注入。

键名（`docs/PORT_PLAN.md` §2「⚠️ 键名法」）：`compile_narrative_context` 的**入参
payload 与返回值都是模型可见的 wire format**（结果由 `to_prompt_payload` 直接
`JSON.stringify` 给模型，上游 `systemPrompt` 的 FIELD MAP 就按这些名字指路），
所以断言里一律用上游原文 camelCase：`storyIdentity` / `relevantEstablishedEpisodes`
/ `currentSceneEvidence` / `ongoingThreads` / `availableNearFuture` / `incomingEvent`
/ `authoringWindow`，以及 `recentScript` / `currentEvent` / `timelinePlan`。

唯一的内部结构是 `SceneFrame`（`scene_frame.py` 物化、`types.py` 的领域对象，
按仓库约定是 snake_case：`present_people` / `open_motions` / `open_topics` /
`source_entry_ids`），因此第二个用例喂进去的是内部形状，断言仍是 wire 形状。

上游 payload 里显式写成 `undefined` 的键（如 `timelinePlan`）在 Python 侧等价于
**不写该键**；显式写成 `null` 的键在 Python 侧写 `None`（本项目统一约定 `None` ≡ `undefined`）。
"""

from __future__ import annotations

import unittest

from plugin.core.script.context_compiler import (
    compile_narrative_context,
    compiled_context_conflicts,
)


class ContextCompilerTest(unittest.TestCase):
    """`test_context_compiler.py` 对应用上游 `context-compiler.test.ts`。"""

    def test_creates_one_seven_part_scaffold_without_recomputing_shared_facts(self) -> None:
        """context compiler creates one seven-part scaffold without recomputing shared facts."""
        payload = {
            'setting': {'character': {'name': '水濑'}}, 'state': {}, 'currentParticipant': None, 'participants': [],
            'recentScript': [{'id': 1, 'content': '她还坐在窗边。'}], 'sceneContext': {'scene': None},
            'continuitySnapshot': None,
            'durableFacts': [], 'memories': [], 'overlayEvolution': [],
            'currentEvent': {'type': 'private-message-batch', 'content': '在吗'},
            'interval': {'from': 'a', 'now': 'b'}, 'activeConsequences': [], 'dueIntents': [], 'upcomingPlans': [],
            'phase': 'user-message', 'refreshContinuity': False, 'outputRecovery': False,
        }
        compiled = compile_narrative_context(payload, None, None)
        self.assertEqual(list(compiled.keys()), [
            'storyIdentity', 'relevantEstablishedEpisodes', 'currentSceneEvidence', 'ongoingThreads',
            'availableNearFuture', 'incomingEvent', 'authoringWindow',
        ])
        self.assertEqual(compiled_context_conflicts(payload, compiled), [])
        self.assertIs(compiled['relevantEstablishedEpisodes']['recentScript'], payload['recentScript'])
        self.assertIs(compiled['incomingEvent']['event'], payload['currentEvent'])

    def test_scene_evidence_already_visible_in_recent_script_is_not_injected_twice(self) -> None:
        """scene evidence already visible in recentScript is not injected twice."""
        payload = {
            'setting': {}, 'state': {}, 'recentScript': [{'id': 7, 'content': '她仍在书桌前。'}],
            'currentEvent': {}, 'interval': {},
        }
        # `SceneFrame` 是内部领域对象（snake_case），上游同名用例里的
        # `presentPeople` / `openMotions` / `sourceEntryIds` 对应下面这些键。
        compiled = compile_narrative_context(payload, {
            'id': 'frame', 'present_people': [], 'open_motions': [], 'open_topics': [],
            'source_entry_ids': [7], 'sources': {'place': [7]}, 'place': '书桌前', 'updated_at': 'now',
        }, None)
        self.assertIsNone(compiled['currentSceneEvidence'].get('place'))
        self.assertIsNone(compiled['currentSceneEvidence'].get('sceneId'))
        self.assertEqual(
            compiled['relevantEstablishedEpisodes']['recentScript'][0]['content'], '她仍在书桌前。',
        )

    def test_scene_evidence_projects_internal_snake_frame_onto_the_upstream_wire_names(self) -> None:
        """补充：内部 frame 的键是 snake_case，写出的证据字段必须是上游 wire 名。"""
        payload = {
            'setting': {}, 'state': {}, 'recentScript': [{'id': 7, 'content': '她仍在书桌前。'}],
            'currentEvent': {}, 'interval': {},
        }
        frame = {
            'id': 'frame', 'scene_id': 12, 'present_people': ['同学甲'], 'open_motions': ['等他回信'],
            'source_entry_ids': [7, 9], 'updated_at': 'now',
            'sources': {'presentPeople': [9], 'openMotions': [9]},
        }
        compiled = compile_narrative_context(payload, frame, None)
        evidence = compiled['currentSceneEvidence']
        self.assertEqual(evidence['sceneId'], 12)
        self.assertEqual(evidence['presentPeople'], {'value': ['同学甲'], 'sourceEntryIds': [9]})
        self.assertEqual(evidence['openLoops'], {'value': ['等他回信'], 'sourceEntryIds': [9]})
        self.assertEqual(evidence['sourceEntryIds'], [9])


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
