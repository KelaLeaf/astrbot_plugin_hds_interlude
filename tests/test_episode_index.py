"""`episode-index` 单元测试（不依赖 AstrBot SDK）。

移植自上游 `test/episode-index.test.ts`（node:test + node:assert/strict），
逐条对照断言。运行方式（在仓库根目录）：

    python3 -m unittest plugin.tests.test_episode_index -v

归属说明：上游该文件里还夹带了 `delivery-reality.ts` 与 `context-compiler.ts` 的用例。
两个模块都已落地，因此这三条用例不再 skip，断言按上游 wire 键名（camelCase）执行；
入参中的 metadata 仍是内部 snake_case（`commit_id` / `delivery_actions`）。
"""

import importlib
import json
import os
import sys
import unittest

# 把仓库根加入 sys.path，便于以插件包结构 import
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# 自动解析插件目录名（即 Python 包名），不硬编码
_PLUGIN_DIR = os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_episode_index = importlib.import_module(f'{_PLUGIN_DIR}.core.script.episode_index')
build_episode_index = _episode_index.build_episode_index
episode_excerpt = _episode_index.episode_excerpt


def row(content, participant_id='a', frame_id='scene:1'):
    """上游 `row(content, participantId = 'a', frameId = 'scene:1')`。

    `frameId: undefined` 的用例写成 `frame_id=None`（两者在 `||` 判定下等价）。
    """
    return {
        'content': content, 'participant_id': participant_id, 'frame_id': frame_id,
        'occurred_at': '2026-09-05T00:00:00Z', 'kind': 'script',
    }


class EpisodeIndexTest(unittest.TestCase):
    def test_episode_navigation_rebuilds_identically_and_never_groups_separate_relationships_or_scenes(self):
        rows = [[1, row('出发')], [2, row('抵达')], [3, row('秘密', 'b')], [4, row('新场景', 'a', 'scene:2')]]
        self.assertEqual(build_episode_index(rows), build_episode_index(json.loads(json.dumps(rows))))
        self.assertEqual(build_episode_index(rows).get(1), [1, 2])
        self.assertEqual(build_episode_index(rows).get(3), [3])

    def test_long_preceding_prose_cannot_displace_the_recall_hit_and_source_ids_reflect_included_text(self):
        rows = [[1, row('旧' * 5000)], [2, row('取餐码8914')], [3, row('取走奶茶')]]
        excerpt = episode_excerpt(rows, 2, 200)
        self.assertIsNotNone(excerpt)
        self.assertRegex(excerpt['content'], '取餐码8914')
        self.assertIn(2, excerpt['source_entry_ids'])
        self.assertIn(3, excerpt['source_entry_ids'])
        self.assertNotIn(1, excerpt['source_entry_ids'])
        self.assertEqual(len(rows[0][1]['content']), 5000)

    def test_delivery_reality_preserves_prose_and_distinguishes_unconfirmed_receipt_from_cancellation(self):
        # 上游用例测的是 `src/script/delivery-reality.ts` 的 `deliveryReality`。
        # 入参条目是内部领域对象（`metadata` 键 snake_case）；返回值是模型可见的
        # wire 结构，故断言用上游 camelCase 键名。
        delivery_reality = importlib.import_module(
            f'{_PLUGIN_DIR}.core.script.delivery_reality').delivery_reality
        entry = {
            'id': 1, 'kind': 'script', 'content': '她发了两句话。',
            'metadata': {'commit_id': 'c', 'delivery_actions': [{
                'commit_id': 'c', 'event_id': 'e', 'segments': [
                    {'kind': 'message', 'content': '第一句', 'status': 'delivered'},
                    {'kind': 'message', 'content': '第二句', 'status': 'pending'},
                ],
            }]},
        }
        before = json.dumps(entry, ensure_ascii=False)
        result = delivery_reality([entry])
        self.assertEqual(result[0]['sourceEntryId'], 1)
        self.assertEqual(result[0]['eventId'], 'e')
        self.assertEqual(result[0]['segments'][1]['outcome'], 'not-confirmed')
        self.assertEqual(json.dumps(entry, ensure_ascii=False), before)
        self.assertEqual(len(delivery_reality([{**entry, 'kind': 'user-message'}])), 0)
        silent = {'id': 2, 'kind': 'script', 'content': '她没说话。',
                  'metadata': {'commit_id': 'c1', 'delivery_actions': []}}
        self.assertEqual(delivery_reality([silent])[0]['communicationOutcome'],
                         'no-outgoing-action-recorded')

    def test_compiled_writing_context_excludes_legacy_rhythm_directives_and_carries_execution_evidence(self):
        # 上游用例测的是 `src/script/context-compiler.ts` 的 `compileNarrativeContext`。
        # payload 与返回值都是模型可见的 wire 结构 → 全部上游 camelCase。
        compile_narrative_context = importlib.import_module(
            f'{_PLUGIN_DIR}.core.script.context_compiler').compile_narrative_context
        result = compile_narrative_context(
            {'chatRhythm': {'drift': '强制三段'}, 'deliveryReality': [{'sourceEntryId': 1}]},
            None, None,
        )
        self.assertNotRegex(json.dumps(result, ensure_ascii=False), '强制三段|chatRhythm')
        self.assertEqual(result['ongoingThreads']['deliveryReality'], [{'sourceEntryId': 1}])

    def test_checkpoint_provenance_groups_legacy_original_entries_without_merging_relationships(self):
        rows = [
            [1, {**row('起点'), 'frame_id': None}],
            [2, {**row('结束'), 'frame_id': None,
                 'checkpoint': {'scene_id': 7, 'first_entry_id': 1, 'last_entry_id': 3}}],
            [3, {**row('另一关系', 'b'), 'frame_id': None}],
        ]
        self.assertEqual(build_episode_index(rows).get(1), [1, 2])
        self.assertEqual(build_episode_index(rows).get(3), [3])

    def test_cross_relationship_action_text_stays_out_of_delivery_reality_unless_sharing_is_enabled(self):
        # 上游用例测的是 `src/script/delivery-reality.ts` 的 `deliveryReality`。
        delivery_reality = importlib.import_module(
            f'{_PLUGIN_DIR}.core.script.delivery_reality').delivery_reality
        entry = {
            'id': 1, 'kind': 'script',
            'metadata': {'commit_id': 'c', 'delivery_actions': [{
                'commit_id': 'c', 'participant_id': 'b', 'event_id': 'e',
                'segments': [{'kind': 'message', 'content': '私密行动', 'status': 'pending'}],
            }]},
        }
        self.assertEqual(delivery_reality([entry], 'a', False), [])
        self.assertEqual(len(delivery_reality([entry], 'b', False)), 1)


if __name__ == '__main__':
    unittest.main()
