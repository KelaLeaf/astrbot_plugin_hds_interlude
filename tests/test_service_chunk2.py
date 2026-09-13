"""`plugin/core/service/chunk2.py` 的测试（上游 `src/service.ts:1929-2572`）。

逐条移植的上游用例
------------------

======================  =========================================================
上游测试文件             移植到本文件的用例
======================  =========================================================
`p1-memory-navigation`   cold-load 单飞 / automatic recall 原文与公开性 / 模型不兼容
                        向量不进语义排序 / 超长原文的条件句存活 / 6000 条召回基准
`history-recall-guards`  私密分支与群记录不外泄 / 贴纸过滤只在超规模活跃库上要向量 /
                        中文词法召回车道 / 词法锚点返回连续原文邻域
`sticker-vision-helpers` `rankStickerCatalog` 直通与余弦排序 / 语义限额 12 /
                        `shouldDownscaleImage` 门槛 / `stableStickerAssetId` 唯一性
`voice-transcription`    OneBot 语音识别 / file token 解析 / QQ 音频文件入通道 /
                        文件事实 / `guessAudioFormat` 魔数嗅探边界 / 可见回复污染清除 /
                        群附件占位事实
======================  =========================================================

`p1-memory-navigation.test.ts` 里断言**块外成员**的用例没有移植（它们属于 Chunk3）：
`backfillHistoryEmbeddings` 的「归档游标 / 单飞与退避 / 批大小 1 与模型切换」三条 ——
`backfill_history_embeddings` 的声明起始行是 2590，不在 [1929, 2572) 内。
同理 `token-usage.test.ts` 与 `sidecar-vision.test.ts` 断言的全部是 `narrator` 模块
（`parseTokenUsage` / `describeImages` / `toPromptPayload`），没有任何一条断言本范围
成员，因此不在本文件重复。

宿主构造
--------

上游测试用 `InterludeService.prototype.method.call(host, ...)` 把成员方法挂在**部分
host 对象**上跑。本移植版用 `ServiceChunk2.__new__(ServiceChunk2)` + 逐字段赋值复现
同一手法：绕开 `__init__` 的模型装配，但跑的是**真实实现**（不是 mock 出的行为）。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
import unittest
from typing import Any, Optional

from plugin.core.database import Database
from plugin.core.script.episode_index import episode_excerpt
from plugin.core.script.recall_navigation import (
    index_original,
    original_window,
    recall_focus,
    recall_keys,
    score_original,
)
from plugin.core.service.base import InterludeContext
from plugin.core.service.chunk2 import (
    ServiceChunk2,
    group_message_ref,
    sticker_mime,
    targetable_message_id,
)
from plugin.core.service.config import (
    is_history_entry_visible_to_participant,
    should_request_turn_embedding,
)
from plugin.core.service.helpers import (
    SEMANTIC_STICKER_LIMIT,
    describe_group_attachments,
    extract_session_audio_sources,
    extract_session_file_facts,
    extract_session_voice_count,
    guess_audio_format,
    history_lexical_score,
    normalize_interaction,
    rank_sticker_catalog,
    should_downscale_image,
    stable_sticker_asset_id,
)
from plugin.core.service.transport import NullTransport
from plugin.core.time import iso, utc_now

#: `p1-memory-navigation.test.ts` 的时间基准（`new Date('2026-09-06T12:00:00Z')`）。
NOW_ISO = '2026-09-06T12:00:00.000Z'
NOW = utc_now().replace(microsecond=0)

#: 6000 条召回基准的上游测试是**基准**而不是 SLA；默认只跑缩小版，设该环境变量跑全量。
RUN_FULL_BENCHMARK = os.environ.get('HDSI_FULL_RECALL_BENCHMARK') == '1'


# --------------------------------------------------------------------------- #
# 宿主构造
# --------------------------------------------------------------------------- #

def _host(**overrides: Any) -> Any:
    """构造一个能调用 Chunk2 成员的宿主（绕开 `ServiceBase.__init__` 的模型装配）。"""
    service = ServiceChunk2.__new__(ServiceChunk2)
    service.ctx = overrides.pop('ctx', InterludeContext())
    service.config = overrides.pop('config', {})
    service.db = overrides.pop('db', None)
    service.transport = overrides.pop('transport', NullTransport())
    service.service_logger = None
    service.blind_mode_health_issue = False
    service.cached_blind_mode_config = None
    service.cached_audio_config = None
    service.cached_sticker_config = None
    service.embedder = None
    service.sticker_catalog = []
    service.sticker_by_id = {}
    service.sticker_scan_running = False
    service.sticker_describer = None
    service.vision_describer = None
    service.history_vectors = {}
    service.history_vectors_ready = set()
    service.history_vector_loads = {}
    service.automatic_recall_cache = {}
    service.buffered_narrative_turns = {}
    service.interrupted_typing_participants = set()
    service.queues = {}
    service._db_write_lock = asyncio.Lock()
    #: `report` / `reportStandalone*` 是 Chunk9 的成员（上游 `src/service.ts:6724+`）：
    #: MRO 里能解析到，但部分 host 上没有，测试里记下来做断言（同时让输出安静）。
    service.reports = []
    service.report = lambda *args, **kwargs: service.reports.append(args)
    service.report_standalone = lambda *args, **kwargs: service.reports.append(args)
    service.report_standalone_operation = lambda *args, **kwargs: service.reports.append(args)
    for key, value in overrides.items():
        setattr(service, key, value)
    return service


def _cache_entry(
    entry_id: int,
    content: str,
    participant_id: str = 'alice',
    occurred_at: str = NOW_ISO,
    kind: str = 'user-message',
    **extra: Any,
) -> dict[str, Any]:
    """上游测试 `entry()` 的召回缓存版（内部结构 → snake_case）。"""
    entry: dict[str, Any] = {
        'id': entry_id,
        'kind': kind,
        'content': content,
        'participant_id': participant_id,
        'occurred_at': occurred_at,
    }
    entry.update(extra)
    return entry


def _script_row(entry_id: int, content: Optional[str] = None, metadata: Any = None) -> dict[str, Any]:
    """一条 `interlude_script_entry` 数据库行（列名保持上游 camelCase）。"""
    return {
        'id': entry_id,
        'storyId': 's',
        'participantId': 'alice',
        'kind': 'user-message',
        'actor': 'user',
        'content': content if content is not None else '原始记录 %d' % entry_id,
        'occurredAt': NOW,
        'metadata': metadata if metadata is not None else {},
        'createdAt': NOW,
    }


class _MemoryRecorderTransport:
    """记录出站动作并允许按段成功/失败的最小 Transport 替身。

    刻意**不**继承 `NullTransport`：真实适配器直接实现 `Transport` 协议，而
    `NullTransport` 在 Chunk2 里是「当前没有平台连接器」的判定依据。
    """

    def __init__(self) -> None:
        self.group_calls: list[tuple[str, str, Optional[str]]] = []
        self.fail_on: tuple[str, ...] = ()
        self.reacted: list[tuple[str, str]] = []
        self.face: tuple[str, str, bool] = ('', '', False)

    async def send_group(self, channel_id: str, content: str, reply_to: Optional[str] = None) -> dict[str, Any]:
        self.group_calls.append((channel_id, content, reply_to))
        if content in self.fail_on:
            return {'ok': False, 'error': 'boom'}
        return {'ok': True, 'message_ids': ['m-%d' % len(self.group_calls)]}

    async def send_native_face(self, channel_id: str, face_id: str, is_group: bool = False) -> dict[str, Any]:
        self.face = (channel_id, face_id, is_group)
        return {'ok': True}

    async def react(self, message_ref: str, reaction: str) -> bool:
        self.reacted.append((message_ref, reaction))
        return True


class _BareTransport:
    """没有任何可选能力的 Transport：用来验证 Chunk2 的安全降级路径。"""


async def _noop_ensure_history_vectors(story_id: str) -> None:
    """上游测试里 `ensureHistoryVectors: async () => {}` 的等价 stub。"""


# =========================================================================== #
# p1-memory-navigation.test.ts
# =========================================================================== #

class ColdLoadTests(unittest.IsolatedAsyncioTestCase):
    async def test_p1_cold_load_requests_join_one_promise_and_do_not_overwrite_a_concurrent_live_cache_entry(self):
        """上游 `P1 cold-load requests join one promise…`。"""
        host = _host()
        calls = {'n': 0}
        pending: dict[str, Any] = {}

        async def db_get(table: str, query: Any, options: Any = None) -> list[Any]:
            calls['n'] += 1
            future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
            pending['future'] = future
            return await future

        host.db_get = db_get
        first = asyncio.ensure_future(host.ensure_history_vectors('s'))
        await asyncio.sleep(0)
        second = asyncio.ensure_future(host.ensure_history_vectors('s'))
        await asyncio.sleep(0)
        self.assertEqual(calls['n'], 1)
        self.assertFalse('s' in host.history_vectors_ready)

        host.history_vectors['s'][1] = {'content': 'new live value'}
        pending['future'].set_result([_script_row(1), _script_row(2)])
        await asyncio.gather(first, second)

        self.assertEqual(host.history_vectors['s'][1]['content'], 'new live value')
        self.assertEqual(len(host.history_vectors['s']), 2)
        self.assertIn('s', host.history_vectors_ready)
        self.assertEqual(host.history_vector_loads, {})

    async def test_ensure_history_vectors_indexes_only_recallable_kinds_and_keeps_metadata(self):
        """`ensureHistoryVectors` 的整表载入语义（本范围成员）。"""
        host = _host()
        rows = [
            _script_row(1, '她在奶茶店报出取餐码 8914。', {
                'frameId': 'frame-1',
                'sceneCheckpoint': {'sceneId': 7},
                'episodeTags': {'objects': ['取餐码'], 'topics': ['不存在的词']},
                'embeddingIdentity': 'model-a',
            }),
            _script_row(2, '不该进入召回的场景摘要'),
            _script_row(3, '主角的散文。'),
        ]
        rows[1]['kind'] = 'scene'
        rows[2]['kind'] = 'script'
        rows[2]['actor'] = 'narrator'
        rows[2]['embedding'] = [0.5, 0.5]
        host.db_get = lambda *args, **kwargs: _resolved(rows)

        await host.ensure_history_vectors('s')
        cache = host.history_vectors['s']
        self.assertEqual(sorted(cache), [1, 3])
        self.assertEqual(cache[1]['tags'], ['取餐码'])
        self.assertEqual(cache[1]['frame_id'], 'frame-1')
        self.assertEqual(cache[1]['checkpoint'], {'sceneId': 7})
        self.assertEqual(cache[1]['embedding_identity'], 'model-a')
        self.assertNotIn('vector', cache[1])
        self.assertEqual(cache[3]['vector'], [0.5, 0.5])
        self.assertIsNone(cache[3]['frame_id'])
        self.assertEqual(cache[3]['occurred_at'], iso(NOW))

        # 第二次调用命中 ready 集合，不再查库。
        calls = {'n': 0}

        async def db_get(*args: Any, **kwargs: Any) -> list[Any]:
            calls['n'] += 1
            return rows

        host.db_get = db_get
        await host.ensure_history_vectors('s')
        self.assertEqual(calls['n'], 0)


class RecallHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_p1_automatic_recall_is_original_only_public_only_and_cached_without_model_calls(self):
        """上游 `P1 automatic recall is original-only, public-only…`。"""
        rows = {
            1: _cache_entry(1, '尚未完成取餐，取餐码8914', ''),
            2: _cache_entry(2, '取餐的私聊秘密', 'alice'),
        }
        host = _host(
            config={'sharedStory': {'shareParticipantDetails': True}},
            history_vectors={'s': rows},
            ensure_history_vectors=_noop_ensure_history_vectors,
        )
        focus = recall_focus(None, ['取餐'], ['取餐'])
        self.assertEqual(focus, '取餐')

        first = await host.recall_history('s', '', focus, [], set(), set(), 1)
        self.assertEqual(len(first), 1)
        self.assertNotIn('私聊秘密', repr(first))
        self.assertEqual(first[0]['source_entry_ids'], [1])

        second = await host.recall_history('s', '', focus, [], set(), set(), 1)
        self.assertEqual(first, second)
        # 缓存副本不得与内部缓存共享 source_entry_ids 列表。
        self.assertIsNot(first[0]['source_entry_ids'], second[0]['source_entry_ids'])

        rows[3] = _cache_entry(3, '取餐已经完成', '')
        third = await host.recall_history('s', '', focus, [], set(), set(), 1)
        self.assertIn('已经完成', repr(third))

    async def test_p1_incompatible_known_model_vectors_do_not_enter_semantic_ranking(self):
        """上游 `P1 incompatible known model vectors…`。"""
        host = _host(
            config={'sharedStory': {'shareParticipantDetails': False}},
            history_vectors={'s': {1: _cache_entry(
                1, '完全无关的内容', 'alice', vector=[1, 0], embedding_identity='old',
            )}},
            ensure_history_vectors=_noop_ensure_history_vectors,
        )
        host.embedder = _Identity('new')
        result = await host.recall_history('s', 'alice', '取餐码', [1, 0], set())
        self.assertEqual(result, [])

    async def test_a_lexical_anchor_returns_its_contiguous_original_script_neighborhood(self):
        """上游 `history-recall-guards.test.ts` 第 4 条。"""
        rows = {
            1: _cache_entry(1, '她从教学楼出来。', 'alice', '2026-09-03T09:00:00.000Z', kind='script'),
            2: _cache_entry(2, '她在奶茶店报出取餐码 8914。', 'alice', '2026-09-03T09:03:00.000Z', kind='script'),
            3: _cache_entry(3, '杯子拿到手时还是冰的。', 'alice', '2026-09-03T09:04:00.000Z', kind='script'),
        }
        host = _host(
            config={'sharedStory': {'shareParticipantDetails': False}},
            history_vectors={'s': rows},
            ensure_history_vectors=_noop_ensure_history_vectors,
        )
        recalled = await host.recall_history('s', 'alice', '昨天奶茶的取餐码', [], set())
        self.assertEqual(recalled[0]['source_entry_ids'], [1, 2, 3])
        self.assertRegex(recalled[0]['content'], r'教学楼出来[\s\S]*8914[\s\S]*还是冰的')

    async def test_recall_history_skips_other_participants_and_excluded_ids(self):
        """`recallHistory` 的可见性 / 排除 / 优选来源三条车道（本范围成员）。"""
        rows = {
            1: _cache_entry(1, '取餐码 8914 已经用掉了', 'alice'),
            2: _cache_entry(2, '取餐码 8914 的秘密副本', 'bob'),
            3: _cache_entry(3, '取餐码 8914 被排除', 'alice'),
            4: _cache_entry(4, '取餐码 8914 只是优选', 'alice'),
        }
        host = _host(
            config={'sharedStory': {'shareParticipantDetails': False}},
            history_vectors={'s': rows},
            ensure_history_vectors=_noop_ensure_history_vectors,
        )
        recalled = await host.recall_history(
            's', 'alice', '取餐码 8914', [], {3}, {4}, 3,
        )
        ids = [item['id'] for item in recalled]
        # 优选来源（source）权重最高，它作为锚点，并把同关系分支的邻域条目一起带出；
        # 被排除的 3 与另一条关系分支的 2 绝不出现。
        self.assertEqual(ids, [4])
        self.assertEqual(recalled[0]['source_entry_ids'], [1, 4])
        self.assertNotIn(2, [item['id'] for item in recalled])

    async def test_recall_history_returns_empty_without_a_cache(self):
        """上游 `if (!cache?.size) return []`。"""
        host = _host(ensure_history_vectors=_noop_ensure_history_vectors)
        self.assertEqual(await host.recall_history('s', 'alice', '取餐码', [], set()), [])
        host.history_vectors['s'] = {}
        self.assertEqual(await host.recall_history('s', 'alice', '取餐码', [], set()), [])

    async def test_p1_long_original_tail_and_its_conditional_sentence_survive_bounded_exact_span_recall(self):
        """上游 `P1 long original tail…`：召回用的原文窗口与摘录预算。"""
        content = '她整理了一页普通笔记。' * 600 + '\n如果电影好看，我想再和你一起看一次。取餐码是 8914。\n她收起手机，继续手边的事。'
        spans = index_original(content)
        hit = score_original(recall_keys('电影 8914'), spans)
        self.assertGreater(hit['score'], 0)
        window = original_window(content, spans, hit['index'], 2200)
        self.assertEqual(window['content'], content[window['start']:window['end']])
        self.assertRegex(window['content'], r'如果电影好看，我想再和你一起看一次')
        excerpt = episode_excerpt(
            [[1, {'content': content, 'kind': 'script', 'participant_id': 'alice',
                  'occurred_at': NOW_ISO, 'spans': spans}]],
            1, 2400, recall_keys('电影 8914'),
        )
        self.assertLessEqual(len(excerpt['content']), 2400)
        self.assertIn('8914', excerpt['content'])
        self.assertIn('not a complete event', excerpt['content'])
        self.assertEqual(excerpt['source_entry_ids'], [1])
        unpunctuated = '普通笔记' * 2000 + '取餐码8914'
        long_line = episode_excerpt(
            [[2, {'content': unpunctuated, 'kind': 'script', 'participant_id': 'alice',
                  'occurred_at': NOW_ISO}]],
            2, 2400, recall_keys('取餐码8914'),
        )
        self.assertIn('取餐码8914', long_line['content'])
        self.assertLessEqual(len(long_line['content']), 2400)

        # 同一条超长原文经由本范围成员召回时同样只带出有界摘录。
        host = _host(
            history_vectors={'s': {1: _cache_entry(1, content, 'alice', NOW_ISO, kind='script')}},
            ensure_history_vectors=_noop_ensure_history_vectors,
        )
        recalled = await host.recall_history('s', 'alice', '电影 8914', [], set(), set(), 1)
        self.assertEqual(len(recalled), 1)
        self.assertIn('8914', recalled[0]['content'])
        self.assertLessEqual(len(recalled[0]['content']), 2400)

    @unittest.skipUnless(RUN_FULL_BENCHMARK, '设 HDSI_FULL_RECALL_BENCHMARK=1 跑上游 6000 条基准')
    async def test_p1_6000_entry_recall_benchmark_synthetic_no_external_requests(self):
        """上游 `P1 6000-entry recall benchmark`（基准，非 SLA）。"""
        size = 6000
        rows = {
            i + 1: _cache_entry(
                i + 1,
                '她继续整理普通笔记。' * 40 + '编号%d。' % i + ('取餐码8914。' if i == 3000 else ''),
                'alice',
                NOW_ISO,
                frame_id='frame-%d' % (i // 20),
            )
            for i in range(size)
        }
        host = _host(
            config={'sharedStory': {'shareParticipantDetails': False}},
            history_vectors={'s': rows},
            ensure_history_vectors=_noop_ensure_history_vectors,
        )
        cold = time.perf_counter()
        result = await host.recall_history('s', 'alice', '取餐码8914', [], set())
        cold_ms = (time.perf_counter() - cold) * 1000
        warm = time.perf_counter()
        await host.recall_history('s', 'alice', '取餐码8914', [], set())
        warm_ms = (time.perf_counter() - warm) * 1000
        vector = [1.0 if index == 0 else 0.01 for index in range(1536)]
        for row in rows.values():
            row['vector'] = vector
        semantic = time.perf_counter()
        await host.recall_history('s', 'alice', '取餐码8914', vector, set())
        semantic_ms = (time.perf_counter() - semantic) * 1000
        self.assertIn('取餐码8914', result[0]['content'])
        self.assertLess(warm_ms, 30_000, '宽松的回归上限（%d 条）' % size)
        print('\n[benchmark] cold=%.0fms warm=%.0fms semantic1536=%.0fms'
              % (cold_ms, warm_ms, semantic_ms))

    async def test_recall_benchmark_scaled_down_runs_by_default(self):
        """上游基准的常跑缩小版：600 条下词法锚点仍然被召回。"""
        size = 600
        rows = {
            i + 1: _cache_entry(
                i + 1,
                '她继续整理普通笔记。' * 10 + '编号%d。' % i + ('取餐码8914。' if i == 300 else ''),
                'alice',
                NOW_ISO,
            )
            for i in range(size)
        }
        host = _host(
            history_vectors={'s': rows},
            ensure_history_vectors=_noop_ensure_history_vectors,
        )
        result = await host.recall_history('s', 'alice', '取餐码8914', [], set())
        self.assertIn('取餐码8914', result[0]['content'])


# =========================================================================== #
# history-recall-guards.test.ts
# =========================================================================== #

class RecallGuardTests(unittest.TestCase):
    def test_semantic_history_recall_keeps_private_branches_and_group_transcripts_out_of_another_participant_prompt(self):
        """上游 `semantic history recall keeps private branches…`。"""
        self.assertTrue(is_history_entry_visible_to_participant(
            {'participantId': 'a', 'kind': 'user-message'}, 'a', False))
        self.assertFalse(is_history_entry_visible_to_participant(
            {'participantId': 'a', 'kind': 'character-message'}, 'b', False))
        self.assertTrue(is_history_entry_visible_to_participant(
            {'participantId': '', 'kind': 'script'}, 'b', False))
        self.assertFalse(is_history_entry_visible_to_participant(
            {'participantId': '', 'kind': 'group-message'}, 'b', False))
        self.assertTrue(is_history_entry_visible_to_participant(
            {'participantId': 'a', 'kind': 'user-message'}, 'b', True))

    def test_sticker_filtering_requests_a_live_vector_only_for_an_active_oversized_library(self):
        """上游 `sticker filtering requests a live vector…`。"""
        base = {'enabled': True, 'liveQuery': False, 'semanticHistory': False, 'semanticStickerFilter': True}
        self.assertFalse(should_request_turn_embedding(base, False, 100))
        self.assertFalse(should_request_turn_embedding(base, True, 12))
        self.assertTrue(should_request_turn_embedding(base, True, 13))
        self.assertTrue(should_request_turn_embedding({**base, 'semanticHistory': True}, False, 0))
        self.assertFalse(should_request_turn_embedding({**base, 'enabled': False}, True, 100))

    def test_semantic_turn_embedding_enabled_reads_config_and_catalog_size(self):
        """`semanticTurnEmbeddingEnabled()`（本范围成员，`:2429`）。"""
        config = {'model': {'embedding': {
            'enabled': True, 'liveQuery': False, 'semanticHistory': False, 'semanticStickerFilter': True,
        }}, 'stickers': {'enabled': True}}
        host = _host(config=config)
        host.sticker_catalog = [{'assetId': str(i)} for i in range(SEMANTIC_STICKER_LIMIT)]
        self.assertFalse(host.semantic_turn_embedding_enabled())
        host.sticker_catalog.append({'assetId': 'extra'})
        self.assertTrue(host.semantic_turn_embedding_enabled())
        host.config = {}
        self.assertFalse(host.semantic_turn_embedding_enabled())

    def test_raw_history_has_a_chinese_lexical_recall_lane_without_embeddings(self):
        """上游 `raw history has a Chinese lexical recall lane…`。"""
        self.assertGreaterEqual(
            history_lexical_score('昨天那杯奶茶拿到了吗', '昨天傍晚，她取走了奶茶，取餐码是 8914。'), 0.12)
        self.assertEqual(history_lexical_score('昨天那杯奶茶', '她正在整理完全无关的课程笔记。'), 0)


# =========================================================================== #
# sticker-vision-helpers.test.ts
# =========================================================================== #

class StickerVisionHelperTests(unittest.TestCase):
    def test_rank_sticker_catalog_passes_through_when_below_the_limit_or_without_a_query_vector(self):
        assets = [{'id': 1}, {'id': 2}, {'id': 3}]
        self.assertEqual(rank_sticker_catalog(assets, [1, 0], 12), assets)
        self.assertEqual(rank_sticker_catalog(assets, [], 2), assets)

    def test_rank_sticker_catalog_orders_by_cosine_similarity_and_fills_leftover_slots(self):
        assets = [
            {'id': 1, 'embedding': [0, 1]},
            {'id': 2, 'embedding': [1, 0]},
            {'id': 3},
            {'id': 4, 'embedding': [0.9, 0.1]},
        ]
        ranked = rank_sticker_catalog(assets, [1, 0], 3)
        self.assertEqual([item['id'] for item in ranked], [2, 4, 1])

    def test_semantic_sticker_limit_is_twelve(self):
        self.assertEqual(SEMANTIC_STICKER_LIMIT, 12)

    def test_should_downscale_image_gates_mime_types_and_small_payloads(self):
        big = 'A' * 220_000
        small = 'A' * 1_000
        self.assertTrue(should_downscale_image('image/jpeg', 'data:image/jpeg;base64,' + big))
        self.assertTrue(should_downscale_image('image/png', 'data:image/png;base64,' + big))
        self.assertTrue(should_downscale_image('image/webp', 'data:image/webp;base64,' + big))
        self.assertFalse(should_downscale_image('image/gif', 'data:image/gif;base64,' + big))
        self.assertFalse(should_downscale_image('image/svg+xml', 'data:image/svg+xml;base64,' + big))
        self.assertFalse(should_downscale_image('image/jpeg', 'data:image/jpeg;base64,' + small))
        self.assertFalse(should_downscale_image('image/jpeg', 'data:image/jpeg;base64,'))

    def test_stable_sticker_asset_id_keeps_punctuation_colliding_filenames_globally_distinct(self):
        hash_a = 'a' * 64
        hash_b = 'b' * 64
        self.assertEqual(stable_sticker_asset_id('bq (6).png', hash_a), stable_sticker_asset_id('bq (6).png', hash_a))
        self.assertNotEqual(stable_sticker_asset_id('bq (6).png', hash_a), stable_sticker_asset_id('bq [6].png', hash_b))
        self.assertRegex(stable_sticker_asset_id('bq (6).png', hash_a), r'a{16}$')

    def test_sticker_mime_and_sticker_path_helpers(self):
        """`stickerMime` / `targetableMessageId` / `groupMessageRef`（模块级，`:7300-7325`）。"""
        self.assertEqual(sticker_mime('a/b/c.GIF'), 'image/gif')
        self.assertEqual(sticker_mime('c.webp'), 'image/webp')
        self.assertEqual(sticker_mime('c.JPEG'), 'image/jpeg')
        self.assertEqual(sticker_mime('c.jpg'), 'image/jpeg')
        self.assertEqual(sticker_mime('c.unknown'), 'image/png')
        self.assertEqual(targetable_message_id(' 42 '), '42')
        self.assertEqual(targetable_message_id('-7'), '-7')
        self.assertIsNone(targetable_message_id('0'))
        self.assertIsNone(targetable_message_id('abc'))
        self.assertIsNone(targetable_message_id(None))
        self.assertEqual(group_message_ref(12), 'msg-12')
        self.assertEqual(group_message_ref(-3), 'msg-0')

    def test_rank_sticker_assets_narrows_only_with_a_query_vector_and_an_oversized_catalog(self):
        """`rankStickerAssets()`（本范围成员，`:2422`）。"""
        catalog = [{'assetId': str(i), 'embedding': [1, 0] if i % 2 else [0, 1]} for i in range(20)]
        host = _host(config={'stickers': {'enabled': True, 'catalogLimit': 40}})
        host.sticker_catalog = catalog

        async def run() -> list[Any]:
            full = await host.rank_sticker_assets()
            narrowed = await host.rank_sticker_assets([1, 0])
            return [full, narrowed]

        full, narrowed = asyncio.run(run())
        self.assertEqual(len(full), 20)
        self.assertEqual(len(narrowed), 20, '语义过滤关闭时即便给了查询向量也必须直通')

        host.config = {'model': {'embedding': {'semanticStickerFilter': True}}}
        full, narrowed = asyncio.run(run())
        self.assertEqual(len(full), 20, '没有查询向量时不得收窄')
        self.assertEqual(len(narrowed), SEMANTIC_STICKER_LIMIT)
        self.assertEqual(narrowed[0]['embedding'], [1, 0])

    def test_sticker_catalog_for_session_keeps_the_model_facing_camel_case_keys(self):
        """`stickerCatalogForSession()`（本范围成员，`:2410`）：payload 键名逐字 camelCase。"""
        host = _host(config={'stickers': {'enabled': True}})
        host.sticker_catalog = [{
            'assetId': 'asset-1', 'group': 'default', 'description': '一只挥手的猫',
            'aliases': ['打招呼'], 'animated': False,
        }]
        session = {'platform': 'onebot', 'channelId': 'c'}
        entries = asyncio.run(host.sticker_catalog_for_session(session))
        self.assertEqual(list(entries[0]), ['assetId', 'group', 'description', 'aliases', 'animated'])
        self.assertEqual(entries[0]['assetId'], 'asset-1')
        self.assertEqual(asyncio.run(host.sticker_catalog_for_session({'platform': 'telegram'})), [])
        disabled = _host()
        disabled.sticker_catalog = host.sticker_catalog
        self.assertEqual(asyncio.run(disabled.sticker_catalog_for_session(session)), [])

    def test_resolve_sticker_requires_catalog_membership_and_willingness_threshold(self):
        """`resolveSticker()`（本范围成员，`:2035`）+ `expressionThreshold` getter。"""
        host = _host(config={'chatActions': {'expressionThreshold': 0.7}})
        asset = {'assetId': 'a', 'description': '猫'}
        host.sticker_by_id = {'a': asset}
        catalog = [{'assetId': 'a'}]
        self.assertEqual(host.expression_threshold, 0.7)
        self.assertEqual(host.resolve_sticker({'assetId': 'a', 'willingness': 0.7}, catalog), asset)
        self.assertIsNone(host.resolve_sticker({'assetId': 'a', 'willingness': 0.69}, catalog))
        self.assertIsNone(host.resolve_sticker({'assetId': 'b', 'willingness': 0.9}, catalog))
        self.assertIsNone(host.resolve_sticker({'assetId': 'a', 'willingness': '0.9'}, catalog))
        self.assertIsNone(host.resolve_sticker({'assetId': 'a'}, catalog))
        self.assertIsNone(host.resolve_sticker(None, catalog))
        host.config = {}
        self.assertEqual(host.expression_threshold, 0.7, '配置缺失时回落 0.7')

    def test_resolve_native_face_needs_declared_willingness_and_visible_text(self):
        """`resolveNativeFace()`（本范围成员，`:2045`）。"""
        host = _host(config={'chatActions': {'expressionThreshold': 0.7}})
        capabilities = {'native_faces': ['smile', 'laugh'], 'expression_threshold': 0.7}
        decision = {'group_reply': {'content': '好耶，谢谢你！'},
                    'native_face': {'semantic': 'smile', 'willingness': 1.0}}
        self.assertEqual(host.resolve_native_face(decision, capabilities), 'smile')
        # 未声明的语义 / 空文本 / 无能力表都不得放行。
        self.assertIsNone(host.resolve_native_face(
            {'group_reply': {'content': '好耶'}, 'native_face': {'semantic': 'angry', 'willingness': 1.0}},
            capabilities))
        self.assertIsNone(host.resolve_native_face(
            {'group_reply': {'content': ''}, 'native_face': {'semantic': 'smile', 'willingness': 1.0}},
            capabilities))
        self.assertIsNone(host.resolve_native_face(decision, None))
        # 阈值高于 0.9 时校准上限让原生表情事实上近乎禁用。
        self.assertIsNone(host.resolve_native_face(
            decision, {'native_faces': ['smile'], 'expression_threshold': 0.95}))


# =========================================================================== #
# voice-transcription.test.ts（本范围成员依赖的会话解析 + describeUserEvent）
# =========================================================================== #

class VoiceAndAttachmentTests(unittest.TestCase):
    def test_onebot_record_cq_segments_are_recognized_as_incoming_voice(self):
        self.assertEqual(extract_session_voice_count({'content': '[CQ:record,file=voice.amr]'}), 1)
        self.assertEqual(extract_session_voice_count({'content': '普通文字'}), 0)

    def test_voice_records_resolve_to_onebot_file_tokens_for_the_native_audio_channel(self):
        self.assertEqual(
            extract_session_audio_sources({'content': '[CQ:record,file=ABC.silk,url=https://example.com/v.silk]'}),
            ['onebot-file:ABC.silk'],
        )
        self.assertEqual(
            extract_session_audio_sources({'content': '<audio file="xyz.amr"/>'}),
            ['onebot-file:xyz.amr'],
        )
        # 裸 URL 无法在服务端转码，因此不进原生音频通道。
        self.assertEqual(extract_session_audio_sources({'content': '<record url="https://example.com/raw.silk"/>'}), [])
        self.assertEqual(
            extract_session_audio_sources({'content': '<audio src="data:audio/mp3;base64,AAAA"/>'}),
            ['data:audio/mp3;base64,AAAA'],
        )
        self.assertEqual(extract_session_audio_sources({'content': '普通文字'}), [])

    def test_qq_audio_files_enter_the_native_audio_channel(self):
        session = {'content': (
            '<file src="http://223.109.208.77:80/asn.com/qqdownloadftnv5?ver=2&rkey=abc"'
            ' file="Mirai Post - ISKL（original mix）.mp3" file-id="fid-1" file-size="3492875"'
            ' name="Mirai Post - ISKL（original mix）.mp3" size="3492875" id="fid-1"/>'
        )}
        sources = extract_session_audio_sources(session)
        self.assertEqual(len(sources), 1)
        self.assertRegex(sources[0], r'^file-url:http://223\.109\.208\.77:80/asn\.com/qqdownloadftnv5\?ver=2&rkey=abc#')
        self.assertTrue(sources[0].endswith(':3492875'), '体积随源携带')
        self.assertIn('Mirai%20Post%20-%20ISKL', sources[0], '文件名随源携带')
        self.assertEqual(
            extract_session_audio_sources({'content': '<file src="https://cdn.example.com/dl?k=1" name="报告.pdf" size="1200"/>'}),
            [],
        )

    def test_extract_session_file_facts_reports_names_sizes_and_audio_flags_without_url_soup(self):
        facts = extract_session_file_facts(
            {'content': '听听<file src="https://cdn.example.com/dl?k=1" name="demo.mp3" size="4096"/>'})
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]['name'], 'demo.mp3')
        self.assertTrue(facts[0]['audio'])
        self.assertEqual(facts[0]['size'], 4096)
        plain = extract_session_file_facts(
            {'content': '<file src="https://cdn.example.com/dl?k=2" name="笔记.zip" size="99"/>'})
        self.assertFalse(plain[0]['audio'])
        fallback = extract_session_file_facts(
            {'content': '<file src="https://x/y.mp3" name="y.mp3" size="1"/>'})
        self.assertGreaterEqual(len(fallback), 1)
        self.assertEqual(extract_session_file_facts({'content': '普通文字'}), [])

    def test_guess_audio_format_sniffs_model_readable_containers_from_magic_bytes_and_filename_hints(self):
        self.assertEqual(guess_audio_format(b'', 'song.MP3'), 'mp3')
        self.assertEqual(guess_audio_format(bytes([0x49, 0x44, 0x33, 0x04, 0, 0, 0, 0]), 'noext'), 'mp3')
        self.assertEqual(guess_audio_format(bytes([0xFF, 0xFB, 0x90, 0x00]), 'noext'), 'mp3')
        self.assertEqual(guess_audio_format(b'RIFF' + bytes(4) + b'WAVE', 'noext'), 'wav')
        self.assertEqual(guess_audio_format(b'OggS\x00\x02', 'noext'), 'ogg')
        self.assertEqual(guess_audio_format(bytes(4) + b'ftypM4A ', 'noext'), 'm4a')
        self.assertEqual(guess_audio_format(b'fLaC', 'noext'), 'flac')
        self.assertEqual(guess_audio_format(b'#!AMR\n', 'noext'), 'amr')
        self.assertEqual(guess_audio_format(b'????'), '')
        self.assertEqual(guess_audio_format(b'', ''), '')

    def test_visible_replies_never_materialize_echoed_attachment_markup_into_real_sends(self):
        runtime = {
            'maxMessageCharacters': 500, 'messageSeparator': '<sep/>',
            'minimumDelayedReplySeconds': 10, 'maximumDelayedReplyMinutes': 120,
        }
        interaction = normalize_interaction({
            'seen': True,
            'reply': {'mode': 'immediate', 'content': (
                '给你听听这个<file src="http://223.109.208.77/asn.com/qqdownloadftnv5?rkey=xyz"'
                ' name="音频.mp3"/>还有图<img src="https://cdn/x.jpg"/>[CQ:record,file=a.mp3]'
            )},
        }, utc_now(), runtime)
        self.assertEqual(interaction['reply']['content'], '给你听听这个还有图')

    def test_group_inbound_attachments_become_fact_placeholders_instead_of_url_soup(self):
        self.assertEqual(
            describe_group_attachments(
                '看看这个<img src="https:// multimedia.qq.com.cn/download?appid=1400&rkey=SECRET"/>'
                '和<file src="http://223.109.208.77/asn.com/qqdownloadftnv5?rkey=xyz" name="demo.mp3" size="4096"/>'),
            '看看这个[图片]和[文件：demo.mp3]',
        )
        self.assertEqual(describe_group_attachments('听这个[CQ:record,file=abc.silk,url=https://x/y.silk]'), '听这个[语音]')
        self.assertEqual(describe_group_attachments('正常文字和[微笑]表情'), '正常文字和[微笑]表情')


class DescribeUserEventTests(unittest.TestCase):
    """`describeUserEvent()`（本范围成员，`:2276`）：一个用户事件折成一条事实。"""

    def _host(self, visual: dict[str, Any], story_name: str = '凌') -> Any:
        host = _host()
        host.describe_vision_event = lambda session: visual
        host.story = {'setting': {'character': {'name': story_name}}}
        return host

    def test_text_only_message_keeps_verbatim_content(self):
        host = self._host({'content': '看看这张图', 'sources': []})
        event = host.describe_user_event(host.story, {'content': '看看这张图'})
        self.assertEqual(event['content'], '看看这张图')
        self.assertEqual(event['sources'], [])
        self.assertEqual(event['audio_sources'], [])
        self.assertIsNone(event['quote'])

    def test_image_attachment_becomes_a_placeholder_fact(self):
        host = self._host({'content': '', 'sources': ['https://cdn/x.jpg']})
        event = host.describe_user_event(host.story, {'content': ''})
        self.assertEqual(event['sources'], ['https://cdn/x.jpg'])
        self.assertIn('用户发送了图片', event['content'])
        self.assertIn('保持未知', event['content'])

    def test_audio_and_file_attachments_get_distinct_placeholder_facts(self):
        host = self._host({'content': '', 'sources': []})
        voice = host.describe_user_event(host.story, {'content': '[CQ:record,file=ABC.silk]'})
        self.assertEqual(voice['audio_sources'], ['onebot-file:ABC.silk'])
        self.assertIn('用户发送了一条语音', voice['content'])

        audio_file = host.describe_user_event(host.story, {
            'content': '<file src="https://cdn/dl?k=1" name="demo.mp3" size="4096"/>',
        })
        self.assertEqual(audio_file['audio_sources'], ['file-url:https://cdn/dl?k=1#demo.mp3:4096'])
        self.assertIn('音频文件：demo.mp3', audio_file['content'])

        plain = host.describe_user_event(host.story, {
            'content': '<file src="https://cdn/dl?k=1" name="报告.pdf" size="1200"/>',
        })
        self.assertEqual(plain['audio_sources'], [])
        self.assertIn('用户发送了文件：报告.pdf', plain['content'])

        # `describeVisionEvent` 已经把附件标记剥掉并留下文本，因此这时走「同时发送」分支。
        mixed_host = self._host({'content': '给你笔记', 'sources': []})
        mixed = mixed_host.describe_user_event(mixed_host.story, {
            'content': '给你笔记<file src="https://cdn/dl?k=1" name="报告.pdf" size="1200"/>',
        })
        self.assertTrue(mixed['content'].startswith('给你笔记'))
        self.assertIn('用户同时发送了文件：报告.pdf', mixed['content'])

    def test_quote_snapshot_is_the_model_facing_camel_case_shape(self):
        host = self._host({'content': '在', 'sources': []}, '凌')
        session = {
            'content': '在吗',
            'selfId': 'bot',
            'quote': {'user': {'id': '42', 'name': 'Kela'}, 'content': '<img src="x"/>在的'},
        }
        event = host.describe_user_event(host.story, session)
        self.assertEqual(event['quote']['senderId'], '42')
        self.assertEqual(event['quote']['senderName'], 'Kela')
        self.assertEqual(event['quote']['content'], '[图片]在的')


# =========================================================================== #
# 本范围成员的自有用例（纯逻辑 / 真实数据库）
# =========================================================================== #

class GroupMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_messages_filters_kind_and_group_then_returns_oldest_first(self):
        """`groupMessages()`（本范围成员，`:1929`）。"""
        host = _host()
        rows = [
            {'id': 5, 'kind': 'group-message', 'actor': 'user', 'content': '别的群', 'occurredAt': NOW,
             'metadata': {'groupId': '999', 'senderId': '7', 'senderName': '甲', 'messageId': '900'}},
            {'id': 4, 'kind': 'group-message', 'actor': 'user', 'content': '四', 'occurredAt': NOW,
             'metadata': {'groupId': 'private:123', 'senderId': '7', 'senderName': '甲', 'messageId': '0'}},
            {'id': 3, 'kind': 'script', 'actor': 'narrator', 'content': '散文', 'occurredAt': NOW, 'metadata': {}},
            {'id': 2, 'kind': 'group-message', 'actor': 'user', 'content': '二', 'occurredAt': NOW,
             'metadata': {'groupId': '123', 'senderId': '7', 'senderName': '甲', 'messageId': '900',
                          'quote': {'senderId': '9', 'senderName': '乙', 'speaker': '消息发送者「乙」（ID：9）',
                                    'content': '被引用的内容'}}},
            {'id': 1, 'kind': 'character-group-message', 'actor': 'character', 'content': '一',
             'occurredAt': NOW, 'metadata': {'groupId': '123'}},
        ]
        captured: dict[str, Any] = {}

        async def db_get(table: str, query: Any, options: Any = None) -> list[Any]:
            captured['table'] = table
            captured['options'] = options
            return rows

        host.db_get = db_get
        messages = await host.group_messages('s', '123', 5)

        self.assertEqual(captured['table'], 'interlude_script_entry')
        self.assertEqual(captured['options']['limit'], 40, 'limit * 8 且下限 20')
        self.assertEqual([m['content'] for m in messages], ['一', '二'])
        character, member = messages
        self.assertEqual(character['sender_id'], 'character')
        self.assertEqual(character['sender_name'], '主角')
        self.assertEqual(character['speaker'], '群成员「主角」（QQ：character）')
        self.assertNotIn('message_id', character)
        self.assertEqual(character['direction'], 'character')
        self.assertEqual(member['sender_id'], '7')
        self.assertEqual(member['sender_name'], '甲')
        self.assertEqual(member['message_ref'], 'msg-2')
        self.assertEqual(member['message_id'], '900')
        self.assertEqual(member['direction'], 'user')
        self.assertEqual(member['quote']['content'], '被引用的内容')

    async def test_group_messages_uses_the_fallback_name_chain(self):
        """`senderName ?? (character ? '主角' : senderId ?? '群成员')` 的逐字等价物。"""
        host = _host()
        rows = [
            {'id': 2, 'kind': 'group-message', 'actor': 'user', 'content': 'x',
             'occurredAt': NOW, 'metadata': {'groupId': '1', 'senderId': '77'}},
            {'id': 1, 'kind': 'group-message', 'actor': 'user', 'content': 'y',
             'occurredAt': NOW, 'metadata': {'groupId': '1'}},
        ]
        host.db_get = lambda *args, **kwargs: _resolved(rows)
        messages = await host.group_messages('s', '1', 10)
        self.assertEqual(messages[0]['sender_id'], 'unknown')
        self.assertEqual(messages[0]['sender_name'], '群成员')
        self.assertEqual(messages[1]['sender_id'], '77')
        self.assertEqual(messages[1]['sender_name'], '77')
        self.assertEqual(messages[1]['occurred_at'], NOW)

    async def test_group_cooldown_active_checks_kind_group_and_window(self):
        """`groupCooldownActive()`（本范围成员，`:1953`）。"""
        host = _host()
        recent = utc_now().replace(microsecond=0)
        rows = [
            {'id': 4, 'kind': 'group-message', 'actor': 'user', 'occurredAt': recent,
             'metadata': {'groupId': '123'}},
            {'id': 3, 'kind': 'character-group-message', 'actor': 'character', 'occurredAt': recent,
             'metadata': {'groupId': '999'}},
            {'id': 2, 'kind': 'character-platform-action', 'actor': 'character', 'occurredAt': recent,
             'metadata': {'groupId': '123'}},
        ]
        host.db_get = lambda *args, **kwargs: _resolved(rows)
        self.assertTrue(await host.group_cooldown_active('s', '123', 30))
        self.assertFalse(await host.group_cooldown_active('s', '123', 0), '冷却为 0 直接放行')
        self.assertFalse(await host.group_cooldown_active('s', '123', -5))
        host.db_get = lambda *args, **kwargs: _resolved([])
        self.assertFalse(await host.group_cooldown_active('s', '123', 30))
        # 只有用户消息时不计入主角冷却。
        rows = [row for row in rows if row['kind'] == 'group-message']
        host.db_get = lambda *args, **kwargs: _resolved(rows)
        self.assertFalse(await host.group_cooldown_active('s', '123', 30))


class ChatCapabilityTests(unittest.TestCase):
    def test_group_chat_capabilities_requires_config_platform_and_a_targetable_message(self):
        """`groupChatCapabilities()`（本范围成员，`:1963`）。"""
        session = {'platform': 'onebot', 'bot': {'internal': {}}}
        config = {'chatActions': {
            'enabled': True, 'platforms': ['qq'], 'quoteReply': True,
            'messageReactions': True, 'allowedReactions': ['like', 'bogus'],
            'nativeFaces': True, 'allowedNativeFaces': ['smile', 'bogus'],
            'expressionThreshold': 0.6,
        }}
        host = _host(config=config, transport=_MemoryRecorderTransport())
        messages = [{'message_ref': 'msg-1', 'message_id': '9'}]
        capabilities = host.group_chat_capabilities(session, messages)
        self.assertEqual(capabilities, {
            'platform': 'qq', 'quote_reply': True, 'reactions': ['like'],
            'native_faces': ['smile'], 'expression_threshold': 0.6,
        })
        self.assertIsNone(host.group_chat_capabilities(session, [{'message_ref': 'msg-1'}]))
        self.assertIsNone(host.group_chat_capabilities(None, messages))
        self.assertIsNone(host.group_chat_capabilities({'platform': 'telegram'}, messages))
        host.config = {'chatActions': {'enabled': True, 'platforms': ['wechat']}}
        self.assertIsNone(host.group_chat_capabilities(session, messages))
        host.config = {'chatActions': {'enabled': True, 'platforms': ['qq']}}
        self.assertIsNone(host.group_chat_capabilities(session, messages), '没有任何能力时不声明')

    def test_reaction_capability_is_withheld_when_no_platform_connector_exists(self):
        """`typeof internal?.setMsgEmojiLike === 'function'` 的等价探测。"""
        config = {'chatActions': {
            'enabled': True, 'platforms': ['qq'], 'quoteReply': False,
            'messageReactions': True, 'allowedReactions': ['like'],
        }}
        messages = [{'message_ref': 'msg-1', 'message_id': '9'}]
        host = _host(config=config, transport=NullTransport())
        self.assertIsNone(host.group_chat_capabilities({'platform': 'onebot'}, messages))
        host.transport = _MemoryRecorderTransport()
        capabilities = host.group_chat_capabilities({'platform': 'onebot'}, messages)
        self.assertEqual(capabilities['reactions'], ['like'])

    def test_private_chat_capabilities_only_exposes_native_faces(self):
        """`privateChatCapabilities()`（本范围成员，`:1977`）。"""
        host = _host(config={'chatActions': {
            'enabled': True, 'platforms': ['qq'], 'nativeFaces': True,
            'allowedNativeFaces': ['heart'], 'expressionThreshold': 2,
        }})
        session = {'platform': 'onebot', 'is_direct': True}
        capabilities = host.private_chat_capabilities(session)
        self.assertEqual(capabilities, {
            'platform': 'qq', 'quote_reply': False, 'reactions': [],
            'native_faces': ['heart'], 'expression_threshold': 1.0,
        })
        host.config = {'chatActions': {'enabled': True, 'platforms': ['qq'], 'nativeFaces': True,
                                       'allowedNativeFaces': []}}
        self.assertIsNone(host.private_chat_capabilities(session))


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_group_message_reports_per_segment_outcomes(self):
        """`sendGroupMessage()`（本范围成员，`:2147`）。"""
        transport = _MemoryRecorderTransport()
        transport.fail_on = ('第二段',)
        host = _host(transport=transport)
        host.split_outgoing_message = lambda content: content.split('<sep/>')
        story = {'id': 's', 'platform': 'onebot', 'selfId': 'bot'}

        result = await host.send_group_message(story, 'chan', '第一段<sep/>第二段', 'reply-9')
        self.assertEqual(result['delivered_segments'], ['第一段'])
        self.assertFalse(result['complete'])
        self.assertEqual(result['segment_outcomes'][0]['status'], 'delivered')
        self.assertEqual(result['segment_outcomes'][1]['status'], 'failed')
        self.assertIn('boom', result['segment_outcomes'][1]['reason'])
        self.assertEqual(transport.group_calls[0], ('chan', '第一段', 'reply-9'))
        self.assertEqual(transport.group_calls[1], ('chan', '第二段', None), '只有第一段带引用')

    async def test_send_group_message_degrades_when_no_transport_is_installed(self):
        """「没有可用机器人账号」的等价降级：整段失败而不是抛异常。"""
        host = _host(transport=_BareTransport())
        host.split_outgoing_message = lambda content: content.split('<sep/>')
        result = await host.send_group_message({'id': 's'}, 'chan', '甲<sep/>乙')
        self.assertEqual(result['delivered_segments'], [])
        self.assertFalse(result['complete'])
        self.assertEqual([item['reason'] for item in result['segment_outcomes']],
                         ['transport-unavailable', 'transport-unavailable'])
        self.assertTrue(host.reports)

    async def test_send_sticker_refuses_paths_outside_the_library_and_records_cancellation(self):
        """`sendSticker()`（本范围成员，`:2059`）的越界检查与账本记账。"""
        recorded: list[tuple[Any, ...]] = []

        async def record(story_id: Any, reference: Any, status: str, reason: Any = None) -> None:
            recorded.append((story_id, reference, status, reason))

        host = _host(config={'stickers': {'enabled': True, 'directory': 'data/stickers'}})
        host.ctx = InterludeContext(base_dir='/base')
        host.record_platform_delivery_outcome = record
        reference = {'commit_id': 'c', 'event_id': 'e', 'script_entry_id': 1, 'segment_index': 0}

        sent = await host.send_sticker(
            {'id': 's'}, {'platform': 'onebot'}, 'chan', {'assetId': 'a', 'filePath': '../evil.png'}, None, reference,
        )
        self.assertFalse(sent)
        self.assertEqual(recorded, [('s', reference, 'cancelled', 'invalid-sticker-path')])

    async def test_send_native_face_records_a_delivered_platform_action(self):
        """`sendNativeFace()`（本范围成员，`:2107`）。"""
        appended: list[dict[str, Any]] = []

        class _Faces(_MemoryRecorderTransport):
            async def send_native_face(self, channel_id: str, face_id: str, is_group: bool = False) -> dict[str, Any]:
                self.face = (channel_id, face_id, is_group)
                return {'ok': True}

        transport = _Faces()
        host = _host(transport=transport)

        async def append_entry(story_id: str, entry: dict[str, Any], now: Any, participant_id: str = '') -> None:
            appended.append(entry)

        host.append_entry = append_entry
        host.update_script_delivery_outcome = _noop_async
        delivered = await host.send_native_face(
            {'id': 's'}, {'platform': 'onebot'}, 'chan', 'smile', 'group-1',
            {'commit_id': 'c', 'event_id': 'e', 'script_entry_id': 1, 'segment_index': 0},
        )
        self.assertTrue(delivered)
        self.assertEqual(transport.face, ('chan', '14', True))
        self.assertEqual(appended[0]['kind'], 'character-platform-action')
        self.assertEqual(appended[0]['content'], '主角发送了 smile 原生表情。')
        self.assertEqual(appended[0]['metadata']['deliverySegmentIndex'], 0)

    async def test_execute_group_reactions_runs_at_most_one_reaction(self):
        """`executeGroupReactions()`（本范围成员，`:1985`）：最多执行一条表态。"""
        class _Reactions(_MemoryRecorderTransport):
            def __init__(self) -> None:
                super().__init__()
                self.reacted: list[tuple[str, str]] = []

            async def react(self, message_ref: str, reaction: str) -> bool:
                self.reacted.append((message_ref, reaction))
                return True

        transport = _Reactions()
        host = _host(transport=transport)
        appended: list[dict[str, Any]] = []

        async def append_entry(story_id: str, entry: dict[str, Any], now: Any, participant_id: str = '') -> None:
            appended.append(entry)

        host.append_entry = append_entry
        host.update_script_delivery_outcome = _noop_async
        completed = await host.execute_group_reactions(
            {'id': 's'}, {'platform': 'onebot'}, '123',
            [{'message_ref': 'msg-1', 'reaction': 'like'}, {'message_ref': 'msg-2', 'reaction': 'heart'}],
        )
        self.assertEqual(completed, 1)
        self.assertEqual(transport.reacted, [('msg-1', 'like')])
        self.assertEqual(appended[0]['content'], '主角给群消息 msg-1 添加了 like 表情回应。')

        # 没有平台连接器时记 cancelled 而不是失败。
        cancelled: list[tuple[Any, ...]] = []
        host.transport = _BareTransport()

        async def record(story_id: Any, reference: Any, status: str, reason: Any = None) -> None:
            cancelled.append((status, reason))

        host.record_platform_delivery_outcome = record
        reference = {'commit_id': 'c', 'event_id': 'e', 'script_entry_id': 1, 'segment_index': 2}
        self.assertEqual(await host.execute_group_reactions(
            {'id': 's'}, {'platform': 'onebot'}, '123',
            [{'message_ref': 'msg-1', 'reaction': 'like'}], lambda reaction: reference,
        ), 0)
        self.assertEqual(cancelled, [('cancelled', 'reaction-api-unavailable')])


class BufferTurnTests(unittest.IsolatedAsyncioTestCase):
    async def test_buffer_user_narrative_merges_messages_and_schedules_one_flush(self):
        """`bufferUserNarrative()`（本范围成员，`:2189`）：短时消息合并。"""
        host = _host(config={'runtime': {'userMessageDebounceSeconds': 3}})
        flushes: list[tuple[Any, Any]] = []

        async def flush_buffered_narrative(key: str, revision: int) -> None:
            flushes.append((key, revision))

        host.flush_buffered_narrative = flush_buffered_narrative
        participant = {'id': 'p1'}
        story = {'id': 's'}

        host.buffer_user_narrative(story, participant, {'content': '在吗'}, NOW, [])
        turn = host.buffered_narrative_turns['p1']
        self.assertEqual(turn['story_id'], 's')
        self.assertEqual(turn['participant_id'], 'p1')
        self.assertEqual(len(turn['messages']), 1)
        self.assertEqual(turn['messages'][0]['content'], '在吗')
        self.assertEqual(turn['messages'][0]['image_sources'], [])
        self.assertNotIn('quote', turn['messages'][0])

        first_timer = turn['timer']
        host.buffer_user_narrative(
            story, participant, {'content': '我有件事想问'}, NOW, [], None, ['img'], ['aud'], {'content': 'x'},
        )
        self.assertEqual(len(turn['messages']), 2)
        self.assertEqual(turn['next_revision'], 2)
        self.assertEqual(turn['messages'][1]['image_sources'], ['img'])
        self.assertEqual(turn['messages'][1]['audio_sources'], ['aud'])
        self.assertEqual(turn['messages'][1]['quote'], {'content': 'x'})
        self.assertIsNot(turn['timer'], first_timer, '每次入队都会重排计时器')

        # `turn.timer()` 是**取消**（上游 `if (turn.timer) turn.timer()`）：3 秒的
        # 防抖窗口内不会 flush。
        turn['timer']()
        await asyncio.sleep(0.05)
        self.assertEqual(flushes, [])

        # 明确的 content 参数优先于 session.content。
        host.buffer_user_narrative(story, participant, {'content': 'ignored'}, NOW, [], 'explicit')
        self.assertEqual(turn['messages'][2]['content'], 'explicit')

        # 防抖为 0 时计时器立即触发，并把当时的 revision 交给 flush。
        immediate = _host(ensure_history_vectors=_noop_ensure_history_vectors)
        immediate.config = {'runtime': {'userMessageDebounceSeconds': 0}}
        fired: list[tuple[Any, Any]] = []

        async def flush_immediately(key: str, revision: int) -> None:
            fired.append((key, revision))

        immediate.flush_buffered_narrative = flush_immediately
        immediate.buffer_user_narrative(story, participant, {'content': '在吗'}, NOW, [])
        immediate.buffer_user_narrative(story, participant, {'content': '还在吗'}, NOW, [])
        await asyncio.sleep(0.05)
        self.assertEqual(fired, [('p1', 2)], '只有最后一次排期进入 flush')

    async def test_signal_incoming_interruption_marks_the_in_flight_request_obsolete(self):
        """`signalIncomingInterruption()`（本范围成员，`:2209`）。"""
        host = _host()
        turn = {
            'story_id': 's', 'participant_id': 'p1', 'messages': [], 'next_revision': 1,
            'in_flight_request_id': 4, 'first_message_committed_request_id': None,
            'obsolete_request_ids': set(),
        }
        host.buffered_narrative_turns['p1'] = turn
        host.signal_incoming_interruption({'id': 's'}, {'id': 'p1'})
        self.assertEqual(turn['obsolete_request_ids'], {4})
        self.assertIn('p1', host.interrupted_typing_participants)
        # 已经提交过该请求时不再重复标记。
        turn['obsolete_request_ids'] = set()
        turn['first_message_committed_request_id'] = 4
        host.signal_incoming_interruption({'id': 's'}, {'id': 'p1'})
        self.assertEqual(turn['obsolete_request_ids'], set())
        # 没有在途请求的参与者也要进中断集合。
        host.signal_incoming_interruption({'id': 's'}, {'id': 'p2'})
        self.assertIn('p2', host.interrupted_typing_participants)

    async def test_deliver_early_private_reply_guards_on_revision_and_commits_boundary(self):
        """`deliverEarlyPrivateReply()`（本范围成员，`:2221`）。"""
        host = _host(config={'runtime': {'maxMessageCharacters': 500, 'messageSeparator': '<sep/>'}})
        host.can_handle_participant = lambda participant: True
        host.split_outgoing_message = lambda content: [content]
        drafts: list[Any] = []

        async def send_outgoing_messages(story: Any, messages: Any, participant: Any, session: Any) -> list[Any]:
            drafts.extend(messages)
            return [{'participant_id': 'p1', 'content': messages[0]['content']}]

        async def confirm_outgoing_deliveries(story: Any, delivered: Any) -> list[Any]:
            return delivered

        host.send_outgoing_messages = send_outgoing_messages
        host.confirm_outgoing_deliveries = confirm_outgoing_deliveries
        turn = {'next_revision': 2, 'obsolete_request_ids': set(), 'first_message_committed_request_id': None}
        reply = {'kind': 'private', 'interaction': {'reply': {'mode': 'immediate', 'content': ' 好呀，我在 '}}}

        self.assertEqual(
            await host.deliver_early_private_reply({'id': 's'}, {'id': 'p1'}, {}, turn, 2, reply),
            {'participant_id': 'p1', 'content': '好呀，我在'},
        )
        self.assertEqual(turn['first_message_committed_request_id'], 2)
        self.assertEqual(drafts[0]['user_initiated'], True)
        self.assertEqual(drafts[0]['participant_id'], 'p1')
        self.assertEqual(drafts[0]['interaction'], reply['interaction'])

        # revision 已过期 / kind 不是私聊 / mode 不是 immediate 都不得早发。
        stale = {'next_revision': 3, 'obsolete_request_ids': set(), 'first_message_committed_request_id': None}
        self.assertIs(await host.deliver_early_private_reply({'id': 's'}, {'id': 'p1'}, {}, stale, 2, reply), False)
        self.assertIs(await host.deliver_early_private_reply(
            {'id': 's'}, {'id': 'p1'}, {}, dict(turn), 2, {'kind': 'group'}), False)
        self.assertIs(await host.deliver_early_private_reply(
            {'id': 's'}, {'id': 'p1'}, {}, dict(turn), 2,
            {'kind': 'private', 'interaction': {'reply': {'mode': 'none', 'content': 'x'}}}), False)
        # 早发内容必须是**单段**：含分隔符的可见回复不能走流式通道。
        multi = {'next_revision': 2, 'obsolete_request_ids': set(),
                 'first_message_committed_request_id': None}
        host.split_outgoing_message = lambda content: content.split('<sep/>')
        self.assertIs(await host.deliver_early_private_reply(
            {'id': 's'}, {'id': 'p1'}, {}, multi, 2,
            {'kind': 'private', 'interaction': {'reply': {'mode': 'immediate', 'content': '甲<sep/>乙'}}}), False)


class StickerLibraryTests(unittest.IsolatedAsyncioTestCase):
    async def test_scan_sticker_library_registers_new_assets_and_marks_missing_ones(self):
        """`scanStickerLibrary()`（本范围成员，`:2303`）走真实 sqlite + 真实临时目录。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, 'data', 'stickers')
            os.makedirs(os.path.join(root, 'grp'))
            first = os.path.join(root, 'grp', 'a.png')
            second = os.path.join(root, 'b.jpg')
            with open(first, 'wb') as handle:
                handle.write(b'\x89PNG\r\n\x1a\n' + b'x' * 64)
            with open(second, 'wb') as handle:
                handle.write(b'\xff\xd8\xff' + b'y' * 64)

            database = Database(':memory:')
            database.register_tables()
            host = _host(
                ctx=InterludeContext(base_dir=tmp),
                config={'stickers': {'enabled': True, 'directory': 'data/stickers',
                                     'maxFileSizeMB': 10, 'catalogLimit': 40}},
                db=database,
                transport=_BareTransport(),
            )
            await host.scan_sticker_library()

            rows = {row['filePath']: row for row in await host.db_get('interlude_sticker', {})}
            self.assertEqual(sorted(rows), ['b.jpg', 'grp/a.png'])
            self.assertEqual(rows['grp/a.png']['mimeType'], 'image/png')
            self.assertEqual(rows['b.jpg']['mimeType'], 'image/jpeg')
            self.assertEqual(rows['grp/a.png']['group'], 'grp')
            self.assertEqual(rows['b.jpg']['group'], 'default')
            self.assertEqual(rows['grp/a.png']['status'], 'pending')
            self.assertEqual(rows['grp/a.png']['aliases'], [])
            self.assertFalse(rows['grp/a.png']['animated'])
            self.assertEqual(len(rows['grp/a.png']['hash']), 64)
            self.assertFalse(host.sticker_scan_running, '扫描结束必须复位单飞标志')
            self.assertEqual(host.sticker_catalog, [], '尚无 active 资产')

            # 描述完成后转 active；删掉另一张图后必须被标成 missing。
            active_path = 'grp/a.png'
            await host.db_set('interlude_sticker', {'id': rows[active_path]['id']},
                              {'status': 'active', 'description': '一只挥手的猫', 'aliases': ['打招呼']})
            os.remove(second)
            await host.scan_sticker_library()
            after = {row['filePath']: row for row in await host.db_get('interlude_sticker', {})}
            self.assertEqual(after[active_path]['status'], 'active', '未变化的 active 资产不得重扫')
            self.assertEqual(after['b.jpg']['status'], 'missing')
            self.assertEqual([row['assetId'] for row in host.sticker_catalog],
                             [after[active_path]['assetId']])
            self.assertIn(after[active_path]['assetId'], host.sticker_by_id)
            database.close()

    async def test_scan_sticker_library_describes_new_assets_and_indexes_them(self):
        """`scanStickerLibrary` 的描述 + 立即向量化路径（`:2349-2378`）。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, 'stickers')
            os.makedirs(root)
            with open(os.path.join(root, 'a.png'), 'wb') as handle:
                handle.write(b'\x89PNG\r\n\x1a\n' + b'x' * 32)
            database = Database(':memory:')
            database.register_tables()
            host = _host(
                ctx=InterludeContext(base_dir=tmp),
                config={
                    'stickers': {'enabled': True, 'directory': 'stickers',
                                 'descriptionResponseFormat': 'prompt-only',
                                 'descriptionMaxTokens': 512},
                    'model': {'embedding': {'semanticStickerFilter': True}},
                },
                db=database,
                transport=_BareTransport(),
            )
            described: list[tuple[Any, ...]] = []
            native: list[tuple[Any, ...]] = []
            embedded: list[str] = []

            class _Describer:
                def available(self) -> bool:
                    return True

                async def describe_sticker(self, data_uri, mime_type, file_path, animated,
                                           response_format, max_tokens):
                    described.append((data_uri, mime_type, file_path, animated, response_format, max_tokens))
                    return {'description': '一只挥手的猫', 'aliases': ['打招呼']}

            async def image_bytes_to_native(data: bytes, mime_type: str) -> dict[str, Any]:
                native.append((len(data), mime_type))
                # `imageBytesToNative` 的移植版输出 snake_case（`types.NarrativeImage`）。
                return {'mime_type': 'image/png', 'data_uri': 'data:image/png;base64,AA=='}

            async def embed_text(value: str) -> list[float]:
                embedded.append(value)
                return [1.0, 0.0]

            host.sticker_describer = _Describer()
            host.image_bytes_to_native = image_bytes_to_native
            host.embed_text = embed_text
            await host.scan_sticker_library()

            self.assertEqual(native, [(40, 'image/png')])
            self.assertEqual(described, [
                ('data:image/png;base64,AA==', 'image/png', 'a.png', False, 'prompt-only', 512),
            ])
            self.assertEqual(embedded, ['一只挥手的猫 打招呼'])
            rows = await host.db_get('interlude_sticker', {})
            self.assertEqual(rows[0]['status'], 'active')
            self.assertEqual(rows[0]['description'], '一只挥手的猫')
            self.assertEqual(rows[0]['aliases'], ['打招呼'])
            self.assertEqual(rows[0]['embedding'], [1.0, 0.0])
            self.assertEqual([row['assetId'] for row in host.sticker_catalog], [rows[0]['assetId']])
            self.assertEqual(host.sticker_catalog[0]['embedding'], [1.0, 0.0])

            # 已描述且已索引的素材不会再被描述或重复索引。
            described.clear()
            await host.scan_sticker_library()
            self.assertEqual(described, [])
            self.assertEqual(embedded, ['一只挥手的猫 打招呼'])
            database.close()

    async def test_scan_sticker_library_keeps_pending_assets_in_retry_cooldown(self):
        """`STICKER_DESCRIPTION_RETRY_COOLDOWN`：未描述的素材不得每个周期重写。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, 'stickers')
            os.makedirs(root)
            with open(os.path.join(root, 'a.png'), 'wb') as handle:
                handle.write(b'\x89PNG\r\n\x1a\n' + b'x' * 32)
            database = Database(':memory:')
            database.register_tables()
            host = _host(
                ctx=InterludeContext(base_dir=tmp),
                config={'stickers': {'enabled': True, 'directory': 'stickers'}},
                db=database,
                transport=_BareTransport(),
            )
            await host.scan_sticker_library()
            rows = await host.db_get('interlude_sticker', {})
            before = rows[0]['updatedAt']
            await host.scan_sticker_library()
            rows = await host.db_get('interlude_sticker', {})
            self.assertEqual(rows[0]['updatedAt'], before, '冷却期内不重写 pending 资产')
            database.close()

    async def test_scan_sticker_library_is_disabled_and_single_flight(self):
        """`!config.enabled || this.stickerScanRunning` 的两道闸门。"""
        host = _host(config={})
        host.sticker_scan_running = True
        await host.scan_sticker_library()  # 关闭时不得翻动标志
        self.assertTrue(host.sticker_scan_running)
        host.config = {'stickers': {'enabled': True}}
        host.sticker_scan_running = True
        await host.scan_sticker_library()
        self.assertTrue(host.sticker_scan_running, '已在运行时直接返回')

    async def test_refresh_sticker_catalog_indexes_active_assets_by_asset_id(self):
        """`refreshStickerCatalog()`（本范围成员，`:2386`）。"""
        host = _host()
        rows = [
            {'id': 1, 'assetId': 'b', 'status': 'active'},
            {'id': 2, 'assetId': 'a', 'status': 'active'},
        ]
        captured: dict[str, Any] = {}

        async def db_get(table: str, query: Any, options: Any = None) -> list[Any]:
            captured['query'] = query
            captured['options'] = options
            return rows

        host.db_get = db_get
        await host.refresh_sticker_catalog()
        self.assertEqual(captured['query'], {'status': 'active'})
        self.assertEqual(captured['options'], {'sort': {'updatedAt': 'desc'}})
        self.assertEqual(host.sticker_catalog, rows)
        self.assertEqual(sorted(host.sticker_by_id), ['a', 'b'])
        self.assertIs(host.sticker_by_id['a'], rows[1])

    async def test_semantic_sticker_embedding_enabled_reads_the_model_section(self):
        """`semanticStickerEmbeddingEnabled()`（本范围成员，`:2392`）。"""
        host = _host()
        self.assertFalse(host.semantic_sticker_embedding_enabled())
        host.config = {'model': {'embedding': {'semanticStickerFilter': True}}}
        self.assertTrue(host.semantic_sticker_embedding_enabled())
        host.config = {'model': {'embedding': {'semantic_sticker_filter': True}}}
        self.assertTrue(host.semantic_sticker_embedding_enabled(), '配置层归一化后是 snake_case')

    async def test_backfill_sticker_embeddings_only_indexes_described_assets_in_batches_of_eight(self):
        """`backfillStickerEmbeddings()`（本范围成员，`:2398`）。"""
        embedded: list[str] = []
        writes: list[tuple[Any, Any]] = []

        async def embed_text(value: str) -> list[float]:
            embedded.append(value)
            return [1.0, 0.0]

        async def db_set(table: str, query: Any, data: Any) -> int:
            writes.append((query, data))
            return 1

        host = _host(config={'model': {'embedding': {'semanticStickerFilter': True}}})
        host.embed_text = embed_text
        host.db_set = db_set
        host.sticker_catalog = (
            [{'id': i, 'assetId': str(i), 'description': '猫 %d' % i, 'aliases': ['喵']} for i in range(10)]
            + [{'id': 99, 'assetId': 'no-desc', 'description': '', 'aliases': []},
               {'id': 98, 'assetId': 'indexed', 'description': '已有', 'embedding': [1.0]}]
        )
        await host.backfill_sticker_embeddings()
        self.assertEqual(len(embedded), 8, '一批最多 8 条')
        self.assertEqual(embedded[0], '猫 0 喵')
        self.assertEqual(writes[0][0], {'id': 0})
        self.assertEqual(writes[0][1]['embedding'], [1.0, 0.0])
        self.assertIn('updatedAt', writes[0][1])

        # 语义过滤关闭时直接返回，一次模型调用都不发。
        embedded.clear()
        host.config = {}
        await host.backfill_sticker_embeddings()
        self.assertEqual(embedded, [])


class ConfigGetterTests(unittest.TestCase):
    def test_audio_config_defaults_and_clamps(self):
        """`get audioConfig()`（本范围成员，`:2247`）。"""
        host = _host()
        self.assertEqual(host.audio_config, {
            'enabled': False, 'out_format': 'mp3', 'max_file_size_mb': 10, 'max_per_message': 1,
        })
        configured = _host(config={'model': {'audio': {
            'enabled': True, 'outFormat': 'wav', 'maxFileSizeMB': 99, 'maxPerMessage': 9,
        }}})
        self.assertEqual(configured.audio_config, {
            'enabled': True, 'out_format': 'wav', 'max_file_size_mb': 25, 'max_per_message': 3,
        })
        self.assertIs(configured.audio_config, configured.audio_config, '实例级缓存')
        host = _host(config={'model': {'audio': {'outFormat': 'aac', 'maxFileSizeMB': 0}}})
        self.assertEqual(host.audio_config['out_format'], 'mp3', '白名单外的格式回落 mp3')
        self.assertEqual(host.audio_config['max_file_size_mb'], 10, '0 视为未配置')

    def test_sticker_config_defaults_and_clamps(self):
        """`get stickerConfig()`（本范围成员，`:2260`）。"""
        host = _host()
        self.assertEqual(host.sticker_config, {
            'enabled': False, 'directory': 'data/hds-interlude/stickers',
            'max_file_size_mb': 10, 'catalog_limit': 40,
            'description_max_tokens': 768, 'description_response_format': 'json-object',
        })
        configured = _host(config={'stickers': {
            'enabled': True, 'directory': '  my/stickers  ', 'maxFileSizeMB': 99,
            'catalogLimit': 999, 'descriptionMaxTokens': 10, 'descriptionResponseFormat': 'prompt-only',
        }})
        self.assertEqual(configured.sticker_config, {
            'enabled': True, 'directory': 'my/stickers', 'max_file_size_mb': 30.0,
            'catalog_limit': 80, 'description_max_tokens': 256,
            'description_response_format': 'prompt-only',
        })
        fallback = _host(config={'stickers': {'catalogLimit': 0, 'descriptionResponseFormat': 'json-object'}})
        self.assertEqual(fallback.sticker_config['catalog_limit'], 40)
        self.assertEqual(fallback.sticker_config['description_response_format'], 'json-object')


class WorkingDetailTests(unittest.TestCase):
    def test_prune_working_details_drops_expired_entries_and_keeps_the_last_ten(self):
        """`pruneWorkingDetails()`（本范围成员，`:2457`）。"""
        now = utc_now()
        earlier = (now.replace(microsecond=0) - __import__('datetime').timedelta(hours=1)).isoformat()
        later = (now + __import__('datetime').timedelta(hours=1)).isoformat()

        self.assertIsNone(_host().prune_working_details(None, now))
        self.assertIsNone(_host().prune_working_details([], now))
        self.assertIsNone(_host().prune_working_details(
            [{'label': 'a', 'expires_at': earlier}], now), '全部过期 → None')
        host = _host()
        live = host.prune_working_details([
            {'label': 'past', 'expires_at': earlier},
            {'label': 'future', 'expires_at': later},
            {'label': 'no-expiry'},
            {'label': 'unparsable', 'expires_at': '不是时间'},
            {'label': 'camel', 'expiresAt': later},
        ], now)
        self.assertEqual([item['label'] for item in live], ['future', 'no-expiry', 'camel'])

        many = [{'label': 'd%d' % i, 'created_at': NOW_ISO} for i in range(14)]
        kept = host.prune_working_details(many, now)
        self.assertEqual(len(kept), 10)
        self.assertEqual([item['label'] for item in kept],
                         ['d%d' % i for i in range(4, 14)], '保留最后 10 条')


class PreviousSceneSummaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_previous_scene_summaries_respects_memory_config_and_bounds_summaries(self):
        """`previousSceneSummaries()`（本范围成员，`:2440`）。"""
        from datetime import datetime, timezone

        started = datetime(2026, 9, 5, 10, 0, tzinfo=timezone.utc)
        ended = datetime(2026, 9, 5, 11, 0, tzinfo=timezone.utc)
        host = _host(config={'memory': {'enabled': True, 'previousSceneSummaries': 2}})
        captured: dict[str, Any] = {}

        async def db_get(table: str, query: Any, options: Any = None) -> list[Any]:
            captured['table'] = table
            captured['query'] = query
            captured['options'] = options
            return [
                {'id': 1, 'startedAt': started, 'endedAt': ended, 'summary': '  生活继续  '},
                {'id': 2, 'startedAt': started, 'endedAt': None, 'summary': '未结束'},
                {'id': 3, 'startedAt': started, 'endedAt': ended, 'summary': '   '},
                {'id': 4, 'startedAt': started, 'endedAt': ended, 'summary': 'x' * 2500},
            ]

        host.db_get = db_get
        summaries = await host.previous_scene_summaries('s')
        self.assertEqual(captured['table'], 'interlude_scene')
        self.assertEqual(captured['query'], {'storyId': 's', 'status': 'closed'})
        self.assertEqual(captured['options'], {'limit': 2, 'sort': {'endedAt': 'desc'}})
        self.assertEqual(len(summaries), 2)
        self.assertEqual(summaries[0], {
            'started_at': '2026-09-05T10:00:00.000Z',
            'ended_at': '2026-09-05T11:00:00.000Z',
            'summary': '  生活继续  ',
        })
        self.assertEqual(len(summaries[1]['summary']), 2000)

        no_limit = _host(config={'memory': {'enabled': True}})
        no_limit.db_get = db_get
        self.assertEqual(await no_limit.previous_scene_summaries('s'), [])
        disabled = _host(config={'memory': {'enabled': False, 'previousSceneSummaries': 2}})
        disabled.db_get = db_get
        self.assertEqual(await disabled.previous_scene_summaries('s'), [])


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #

class _Identity:
    """`this.embedder?.identity?.()` 的最小替身。"""

    def __init__(self, value: str) -> None:
        self._value = value

    def identity(self) -> str:
        return self._value


async def _resolved(value: Any) -> Any:
    return value


async def _noop_async(*args: Any, **kwargs: Any) -> None:
    """接受任意签名的 async 空实现（投递账本一类只记账的成员）。"""
    return None


if __name__ == '__main__':
    unittest.main()
