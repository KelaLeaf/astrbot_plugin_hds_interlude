"""`continuity-checkpoint` 单元测试（不依赖 AstrBot SDK）。

移植自上游 `test/continuity-checkpoint.test.ts`（node:test + node:assert/strict），
逐条对照断言。运行方式（在仓库根目录）：

    python3 -m unittest plugin.tests.test_continuity_checkpoint -v

归属说明：上游该文件里还有 3 个用例打的是 `InterludeService`（`src/service.ts`）的
`persistCompaction` / `describeUserEvent`。按移植约定，这些用例的断言**原样保留**，
但用 `unittest.SkipTest` 标出归属，等 `core/service.py` 落地后取消 skip 即可直接跑。
"""

import asyncio
import importlib
import os
import sys
import unittest
from datetime import datetime, timezone

# 把仓库根加入 sys.path，便于以插件包结构 import
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# 自动解析插件目录名（即 Python 包名），不硬编码
_PLUGIN_DIR = os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_checkpoint_module = importlib.import_module(f'{_PLUGIN_DIR}.core.script.continuity_checkpoint')
assert_continuity_review = _checkpoint_module.assert_continuity_review
compaction_prefix = _checkpoint_module.compaction_prefix

#: 上游 `const date = new Date('2026-09-05T00:00:00Z')`。
DATE = datetime(2026, 9, 5, tzinfo=timezone.utc)

_SERVICE_SKIP = '归属 core/service.py 或 core/narrator.py 移植任务'


def make_entries():
    """上游 `entries`：3 条各 80 字符的 `script` 条目。"""
    return [
        {
            'id': entry_id, 'story_id': 's', 'participant_id': '', 'kind': 'script',
            'actor': 'narrator', 'content': str(entry_id) * 80,
            'occurred_at': DATE, 'created_at': DATE, 'metadata': {},
        }
        for entry_id in (1, 2, 3)
    ]


class ContinuityCheckpointTest(unittest.TestCase):
    def test_incremental_review_exhausts_an_oversized_backlog_without_skipping_any_original(self):
        entries = make_entries()
        processed = []
        # 上游是 `while (processed.length < entries.length)`；这里额外加一个迭代上限，
        # 免得实现退化时把测试挂死（不改变断言语义）。
        for _ in range(50):
            if len(processed) >= len(entries):
                break
            last = processed[-1] if processed else 0
            batch = compaction_prefix([entry for entry in entries if entry['id'] > last], 100)
            processed.extend(entry['id'] for entry in batch)
        self.assertEqual(processed, [1, 2, 3])
        self.assertEqual(compaction_prefix(entries, 10)[0]['content'], entries[0]['content'])

    def test_empty_or_partial_continuity_output_cannot_acknowledge_scene_evidence(self):
        with self.assertRaises(ValueError):
            assert_continuity_review({})
        with self.assertRaises(ValueError):
            assert_continuity_review({'scene': {'summary': '已下课'}})
        assert_continuity_review({'scene': {'summary': '已下课'}, 'arc': {'summary': '约定尚未履行'}})

    def test_closing_a_reviewed_scene_retains_the_processed_frontier_despite_later_message_arrival(self):
        # 上游用例测的是 `InterludeService.prototype.persistCompaction`（src/service.ts）。
        raise unittest.SkipTest(_SERVICE_SKIP)

        # ---- 以下为上游用例译文（断言逐条保留，待 core/service.py 落地后删除上面的 skip）----
        entries = make_entries()
        writes = []
        observed = {}

        async def active_arc():
            return {'id': 7, 'title': '旧弧'}

        async def active_scene():
            return {'id': 9}

        async def db_get():
            return []

        async def db_set(table, query, patch):
            writes.append({'table': table, 'query': query, 'patch': patch})

        async def ensure_continuity(_story, at):
            observed['next_at'] = at

        service = {
            'memory_config': {'arc_summary_characters': 1000, 'scene_hook_characters': 100,
                              'scene_summary_characters': 1000},
            'active_arc': active_arc, 'active_scene': active_scene,
            'db_get': db_get, 'db_set': db_set, 'ensure_continuity': ensure_continuity,
        }
        service_module = importlib.import_module(f'{_PLUGIN_DIR}.core.service')
        asyncio.run(service_module.InterludeService.persist_compaction(
            service,
            {'id': 's'}, {'id': 8, 'entry_count': 0, 'started_at': DATE},
            {'scene': {'summary': '下课离开', 'close': True,
                       'boundary': {'reason': '已离开教室', 'source_entry_ids': [3]}},
             'arc': {'summary': '约定延续'}},
            entries, datetime.fromtimestamp(DATE.timestamp() + 60, tz=timezone.utc),
        ))
        self.assertEqual(observed['next_at'], DATE)
        self.assertEqual(writes[0]['table'], 'interlude_arc')
        self.assertTrue(any(write['table'] == 'interlude_scene' and write['query']['id'] == 9
                            and write['patch']['last_entry_id'] == 3 for write in writes))

    def test_failed_arc_persistence_leaves_the_scene_checkpoint_untouched(self):
        # 上游用例同样打的是 `InterludeService.prototype.persistCompaction`。
        raise unittest.SkipTest(_SERVICE_SKIP)

        # ---- 以下为上游用例译文（断言逐条保留）----
        entries = make_entries()
        writes = []

        async def active_arc():
            return {'id': 7, 'title': '旧弧'}

        async def db_set(table):
            writes.append(table)
            raise RuntimeError('database unavailable')

        service = {'memory_config': {'arc_summary_characters': 1000},
                   'active_arc': active_arc, 'db_set': db_set}
        service_module = importlib.import_module(f'{_PLUGIN_DIR}.core.service')
        with self.assertRaises(RuntimeError):
            asyncio.run(service_module.InterludeService.persist_compaction(
                service, {'id': 's'}, {'id': 8},
                {'scene': {'summary': '下课'}, 'arc': {'summary': '约定'}}, entries, DATE,
            ))
        self.assertEqual(writes, ['interlude_arc'])

    def test_image_only_input_remains_an_observed_media_event_and_never_gains_invented_words(self):
        # 上游用例测的是 `InterludeService.prototype.describeUserEvent`（src/service.ts）。
        raise unittest.SkipTest(_SERVICE_SKIP)

        # ---- 以下为上游用例译文（断言逐条保留）----
        async def transcribe_voice_event():
            return {'detected': 0, 'transcripts': [], 'provider': ''}

        service = {
            'describe_vision_event': lambda: {'content': '', 'sources': ['image-source']},
            'transcribe_voice_event': transcribe_voice_event,
        }
        service_module = importlib.import_module(f'{_PLUGIN_DIR}.core.service')
        result = asyncio.run(service_module.InterludeService.describe_user_event(
            service,
            {'setting': {'character': {'name': '测试角色'}}},
            {'content': '<img src="image-source"/>'},
        ))
        self.assertEqual(result['sources'], ['image-source'])
        self.assertRegex(result['content'], '用户发送了图片')
        self.assertRegex(result['content'], '未知')
        self.assertNotRegex(result['content'], '非文本消息')


if __name__ == '__main__':
    unittest.main()
