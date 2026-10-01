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
import hashlib
import os
import tempfile
import time
import unittest
import zlib
from typing import Any, Optional

from plugin.core.bubbles import VOICE_MARKER
from plugin.core.database import Database
from plugin.core.narrator import SilentStickerDescriber
from plugin.core.script.episode_index import episode_excerpt
from plugin.core.script.recall_navigation import (
    index_original,
    original_window,
    recall_focus,
    recall_keys,
    score_original,
)
from plugin.core.service.base import InterludeContext
from plugin.core.service import chunk2 as chunk2_module
from plugin.core.service import chunk3 as chunk3_module
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
    #: 自动收藏的节流时间戳（生产里由 `_warn_sticker_collect_unavailable` 首次写入）。
    service._sticker_collect_warn_at = 0
    #: 第二层判据（§45.7）的两处实例状态：能力缺失的 warn 时间戳，以及判定调用的滑动窗口。
    service._sticker_guess_warn_at = 0
    service._sticker_guess_calls = []
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
        #: 以语音投递的分段（正文 `<tts/>` 标记指定的那些）。
        self.voiced: list[str] = []
        self.fail_on: tuple[str, ...] = ()
        self.reacted: list[tuple[str, str]] = []
        self.face: tuple[str, str, bool] = ('', '', False)

    async def send_group(self, channel_id: str, content: str, reply_to: Optional[str] = None,
                         **kwargs: Any) -> dict[str, Any]:
        # 语音意图（v1.7.7）只在真要发语音时才作为关键字传下来 → 记进独立的那一列。
        self.group_calls.append((channel_id, content, reply_to))
        if kwargs.get('voice') is True:
            self.voiced.append(content)
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


class _ByteTransport:
    """只实现 `fetch_image` 的传输桩：按 URL 回字节，并记下被请求过哪些 URL。

    `payloads` 里没有的 URL 回 `None`（等价"下载失败"）；**绝不真实联网**。
    """

    def __init__(self, payloads: Optional[dict[str, bytes]] = None) -> None:
        self.payloads: dict[str, bytes] = dict(payloads or {})
        self.fetched: list[str] = []

    async def fetch_image(self, url: str) -> Optional[bytes]:
        self.fetched.append(url)
        return self.payloads.get(url)


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

    def test_sticker_placeholder_says_what_kind_it_is(self):
        """表情包 / 实拍照片不能在事实里写成同一个东西（受控偏离 §29）。"""
        host = self._host({
            'content': '',
            'sources': ['https://cdn/x.gif'],
            'media': [{'source': 'https://cdn/x.gif', 'kind': 'sticker',
                       'summary': '[动画表情]', 'label': '[动画表情]'}],
        })
        event = host.describe_user_event(host.story, {'content': ''})
        self.assertIn('用户发送了1 [动画表情]', event['content'])
        self.assertEqual(event['media'][0]['kind'], 'sticker')

    def test_captioned_image_keeps_a_media_fact_in_the_script(self):
        """图片带文字时也要进脚本，否则"他发了个表情包"这条事实随回合消失。"""
        host = self._host({
            'content': '看看',
            'sources': ['https://cdn/x.png'],
            'media': [{'source': 'https://cdn/x.png', 'kind': 'sticker',
                       'summary': '', 'label': '[表情包]'}],
        })
        event = host.describe_user_event(host.story, {'content': '看看'})
        self.assertTrue(event['content'].startswith('看看'))
        self.assertIn('[用户同时发送了1 [表情包]', event['content'])
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

    async def test_marked_group_segments_go_out_as_voice_in_order(self):
        """群聊里的 `<sep/>` + `<tts/>`：分段投递，带标记的那几段走语音，顺序不变。"""
        transport = _MemoryRecorderTransport()
        host = _host(transport=transport)
        story = {'id': 's', 'platform': 'onebot', 'selfId': 'bot'}

        result = await host.send_group_message(
            story, 'chan', '先打字<sep/>这段语音<tts/><sep/>再打字',
        )
        self.assertEqual([item['content'] for item in result['segment_outcomes']],
                         ['先打字', '这段语音', '再打字'])
        self.assertTrue(all(VOICE_MARKER not in item['content'] for item in result['segment_outcomes']))
        self.assertEqual(transport.voiced, ['这段语音'], '只有带标记的那一段走语音')

    async def test_voice_disabled_group_reply_is_plain_text_without_the_marker(self):
        host = _host(config={'model': {'audio': {'tts_enabled': False}}},
                     transport=_MemoryRecorderTransport())
        story = {'id': 's', 'platform': 'onebot', 'selfId': 'bot'}
        result = await host.send_group_message(story, 'chan', '甲' + VOICE_MARKER + '<sep/>乙')
        self.assertEqual([item['content'] for item in result['segment_outcomes']], ['甲', '乙'],
                         '关掉开关 = 退回发文字，内容一字不少')
        self.assertEqual(host.transport.voiced, [])

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

    async def test_a_turn_field_written_by_flush_is_readable_by_chunk2_guards(self):
        """回归（用户 2026-09-26 00:22 的日志）：在途请求号必须两种拼写都读得到。

        汐雨. 连发「行吧」「晚安」+ 一张晚安贴图；第 3 条到达时第 2 条那一回合正在跑模型，
        本该被判过时、与第 3 条合成**一个**回合，结果被拆成两回合——贴图那一回合她已经
        "睡着了、没看见"。根因是拼写：`bufferUserNarrative` 建的 turn 是 snake_case，而
        `in_flight_request_id` 是 `flushBufferedNarrative`（Chunk3）**第一次**写进去的，
        `_turn_set` 按"跟随已有拼写"的规则挑不到 snake 键 → 落在 camelCase；Chunk2 的两个
        守卫却直接 `turn.get('in_flight_request_id')` → 拿到 None → `shouldSupersede`
        永远为假。修复前的既有用例手工用 snake_case 造 turn，所以一直没抓到。
        """
        host = _host()
        participant = {'id': 'p1'}
        story = {'id': 's'}

        host.buffer_user_narrative(story, participant, {'content': '行吧'}, NOW, [])
        turn = host.buffered_narrative_turns['p1']
        # Chunk3 起跑时就是这么写的（`flush_buffered_narrative` 内的真实调用形态）。
        chunk3_module._turn_set(turn, 'inFlightRequestId', 'in_flight_request_id', 4)
        self.assertEqual(turn['in_flight_request_id'], 4, '两种拼写都要能读到同一个在途请求号')

        host.signal_incoming_interruption(story, participant)
        self.assertEqual(turn['obsolete_request_ids'], {4}, '在途请求未提交首条回复时必须作废')

        # 同一个守卫也住在 `bufferUserNarrative` 里：新消息进缓冲时顺手作废在途请求。
        turn['obsolete_request_ids'] = set()
        host.buffer_user_narrative(story, participant, {'content': '晚安'}, NOW, [])
        self.assertEqual(turn['obsolete_request_ids'], {4})
        self.assertEqual(len(turn['messages']), 2, '两条消息留在同一个回合里')

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
        # ⚠️ 夹具必须用**生产写入方**的写法（Chunk3 的 `_turn_set` 写两种拼写）：
        # 只改一种拼写会让"另一个拼写里的旧值"继续生效——这正是 2026-09-26 那个
        # "连发消息没被合并"的 bug 的同一类陷阱。
        chunk3_module._turn_set(turn, 'obsoleteRequestIds', 'obsolete_request_ids', set())
        chunk3_module._turn_set(
            turn, 'firstMessageCommittedRequestId', 'first_message_committed_request_id', 4,
        )
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

        async def send_outgoing_messages(
            story: Any, messages: Any, participant: Any, session: Any, **_kwargs: Any,
        ) -> list[Any]:
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

    async def test_scan_leaves_the_manual_description_alone(self):
        """手工描述压过自动描述：扫描不覆盖、不重调模型（受控偏离 §45.2）。"""
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
            described: list[Any] = []

            class _Describer:
                def available(self) -> bool:
                    return True

                async def describe_sticker(self, *_args: Any) -> dict[str, Any]:
                    described.append(_args)
                    return {'description': '模型写的', 'aliases': []}

            host.sticker_describer = _Describer()

            async def image_bytes_to_native(data: bytes, mime_type: str) -> dict[str, Any]:
                return {'mime_type': 'image/png', 'data_uri': 'data:image/png;base64,AA=='}

            host.image_bytes_to_native = image_bytes_to_native
            await host.scan_sticker_library()
            row = (await host.db_get('interlude_sticker', {}))[0]
            self.assertEqual(row['description'], '模型写的')

            # 用户手改成"她看到的其实是只猫"，然后重扫。
            await host.save_sticker_description(row['id'], '一只挥手的小猫')
            described.clear()
            await host.scan_sticker_library()
            after = (await host.db_get('interlude_sticker', {}))[0]
            self.assertEqual(after['description'], '一只挥手的小猫', '手改的描述不得被重扫覆盖')
            self.assertEqual(described, [], '标记了 manual 的素材不该再花一次模型调用')
            self.assertTrue(after['descriptionManual'])
            self.assertEqual(after['status'], 'active')
            # 目录（模型看到的那份）里也必须是新描述。
            self.assertEqual(
                [item['description'] for item in host.sticker_catalog], ['一只挥手的小猫'],
            )
            database.close()

    async def test_restore_sticker_description_hands_it_back_to_the_model(self):
        """摘掉手工标记 → 回到 `pending` + 空描述，下一轮扫描重新描述。"""
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
            row = (await host.db_get('interlude_sticker', {}))[0]
            await host.save_sticker_description(row['id'], '人工写的')
            await host.restore_sticker_description(row['id'])
            restored = (await host.db_get('interlude_sticker', {}))[0]
            self.assertEqual(restored['description'], '')
            self.assertFalse(restored['descriptionManual'])
            self.assertEqual(restored['status'], 'pending')
            self.assertEqual(host.sticker_catalog, [])
            database.close()


def _png(payload: bytes = b'x') -> bytes:
    """一张最小的"合法" PNG 字节串（魔数正确就够，内容不参与判据）。"""
    return b'\x89PNG\r\n\x1a\n' + payload * 32


class AutomaticStickerCollectionTests(unittest.IsolatedAsyncioTestCase):
    """自动收藏：**只收确认是表情包的**（受控偏离 §45.1，用户点名的红线）。

    每个用例都断言"库里到底有几行"——这条功能的全部风险都在误收。
    """

    def _collect_host(self, tmp: str, **stickers: Any) -> Any:
        database = Database(':memory:')
        database.register_tables()
        self.addCleanup(database.close)
        directory = stickers.pop('directory', 'stickers')
        os.makedirs(os.path.join(tmp, directory), exist_ok=True)
        return _host(
            ctx=InterludeContext(base_dir=tmp),
            config={'stickers': {'enabled': True, 'directory': directory, **stickers}},
            db=database,
            transport=_ByteTransport({'https://cdn.example.com/x.png': _png()}),
        )

    async def test_only_observed_sticker_kinds_are_collected(self):
        """`sticker` / `animated` / `market` 各收一张；`image` 与未知种类零入库。"""
        with tempfile.TemporaryDirectory() as tmp:
            host = self._collect_host(tmp)
            sources = ['https://cdn.example.com/%s' % name for name in
                       ('sticker', 'animated', 'market', 'photo', 'unknown', 'card')]
            media = [
                {'source': sources[0], 'kind': 'sticker', 'summary': '', 'label': '[表情包]'},
                {'source': sources[1], 'kind': 'animated', 'summary': '[动画表情]', 'label': '[动画表情]'},
                {'source': sources[2], 'kind': 'market', 'summary': '[QQ 商城表情]', 'label': '[QQ 商城表情]'},
                # ⚠️ 普通照片 / 截图：**绝不收藏**。
                {'source': sources[3], 'kind': 'image', 'summary': '[图片]', 'label': '[图片]'},
                # 种类缺失 / 未知：拿不准就不收（这一条是用户点名的红线）。
                {'source': sources[4], 'kind': '', 'summary': '', 'label': '[图片]'},
                {'source': sources[5], 'kind': 'card', 'summary': '', 'label': '[分享卡片]'},
            ]
            # 每张图内容不同，避免被 sha256 去重合并成一条。
            for index, source in enumerate(sources):
                host.transport.payloads[source] = _png(bytes([65 + index]))
            collected = await host.collect_incoming_stickers(media, sources)

            self.assertEqual(len(collected), 3, '只有三种表情包种类该入库')
            rows = await host.db_get('interlude_sticker', {})
            self.assertEqual(len(rows), 3)
            self.assertEqual({row['source'] for row in rows}, {'auto'})
            self.assertEqual({row['group'] for row in rows}, {'collected'})
            self.assertEqual({row['status'] for row in rows}, {'pending'})
            for row in rows:
                self.assertTrue(row['assetId'].startswith('sticker-'))
                self.assertEqual(row['mimeType'], 'image/png')
                self.assertEqual(len(row['hash']), 64, '内容 sha256 必须落库（去重的依据）')
                self.assertTrue(row['filePath'].startswith('collected/'))
                self.assertTrue(os.path.isfile(os.path.join(tmp, 'stickers', row['filePath'])))
            # 文件与库行一一对应（"收了但磁盘上没有"是最坏的形态）。
            self.assertEqual(
                sorted(os.listdir(os.path.join(tmp, 'stickers', 'collected'))),
                sorted(os.path.basename(row['filePath']) for row in rows),
            )

    async def test_plain_photos_and_unknown_kinds_never_enter_the_library(self):
        """单独钉死红线：`image` / 缺种类 / 未知种类 → 零入库、零网络请求。"""
        with tempfile.TemporaryDirectory() as tmp:
            host = self._collect_host(tmp)
            sources = ['https://cdn.example.com/%d.png' % index for index in range(4)]
            media = [
                {'source': sources[0], 'kind': 'image'},
                {'source': sources[1]},                       # 连 kind 键都没有
                {'source': sources[2], 'kind': 'photo'},      # 没人认识的种类
                {'source': sources[3], 'kind': None},         # 显式 None
            ]
            collected = await host.collect_incoming_stickers(media, sources)
            self.assertEqual(collected, [])
            self.assertEqual(await host.db_get('interlude_sticker', {}), [])
            self.assertEqual(host.transport.fetched, [], '不是表情包就不该去下载')
            self.assertEqual(host.sticker_catalog, [])

    async def test_missing_kind_metadata_skips_everything(self):
        """没有 media 元数据（拿不到种类）→ 一个都不收，哪怕来源是图片 URL。"""
        with tempfile.TemporaryDirectory() as tmp:
            host = self._collect_host(tmp)
            sources = ['https://cdn.example.com/a.png']
            host.transport.payloads[sources[0]] = _png()
            self.assertEqual(await host.collect_incoming_stickers(None, sources), [])
            self.assertEqual(await host.collect_incoming_stickers([], sources), [])
            self.assertEqual(host.transport.fetched, [])
            self.assertEqual(await host.db_get('interlude_sticker', {}), [])

    async def test_bytes_that_are_not_images_are_skipped(self):
        """种类对但字节不是图片（HTML 错误页 / 纯文本 / 空）→ 零入库 + 一条 warn。"""
        with tempfile.TemporaryDirectory() as tmp:
            host = self._collect_host(tmp)
            sources = ['https://cdn.example.com/not-image.png']
            host.transport.payloads[sources[0]] = b'<html>404 not found</html>'
            media = [{'source': sources[0], 'kind': 'sticker'}]
            self.assertEqual(await host.collect_incoming_stickers(media, sources), [])
            self.assertEqual(await host.db_get('interlude_sticker', {}), [])
            collected_dir = os.path.join(tmp, 'stickers', 'collected')
            self.assertEqual(
                sorted(os.listdir(collected_dir)) if os.path.isdir(collected_dir) else [], [],
                '不是图片就不该落盘',
            )

    async def test_same_content_is_collected_once(self):
        """重复内容只入库一次（内容 sha256 去重），两张不同的图各一条。"""
        with tempfile.TemporaryDirectory() as tmp:
            host = self._collect_host(tmp)
            sources = ['https://cdn.example.com/a.png', 'https://cdn.example.com/b.png']
            host.transport.payloads[sources[0]] = _png(b'same')
            host.transport.payloads[sources[1]] = _png(b'same')   # 同内容、不同 URL
            media = [{'source': source, 'kind': 'animated'} for source in sources]
            collected = await host.collect_incoming_stickers(media, sources)
            self.assertEqual(len(collected), 1)
            rows = await host.db_get('interlude_sticker', {})
            self.assertEqual(len(rows), 1)
            # 再收一遍（同一个表情再被发一次）仍然只有一条。
            again = await host.collect_incoming_stickers(media, sources)
            self.assertEqual(again, [])
            self.assertEqual(len(await host.db_get('interlude_sticker', {})), 1)

    async def test_fetch_failure_warns_once_without_raising(self):
        """拿不到字节（无本地路径 + fetch_image 失败）→ 跳过 + **一条节流 warn**。"""
        with tempfile.TemporaryDirectory() as tmp:
            database = Database(':memory:')
            database.register_tables()
            self.addCleanup(database.close)
            os.makedirs(os.path.join(tmp, 'stickers'))

            class _Broken:
                async def fetch_image(self, url: str) -> Any:
                    raise RuntimeError('network down')

            host = _host(
                ctx=InterludeContext(base_dir=tmp),
                config={'stickers': {'enabled': True, 'directory': 'stickers'}},
                db=database,
                transport=_Broken(),
            )
            sources = ['https://cdn.example.com/%d.png' % index for index in range(5)]
            media = [{'source': source, 'kind': 'sticker'} for source in sources]
            self.assertEqual(await host.collect_incoming_stickers(media, sources), [])
            warns = [
                entry for entry in host.reports
                if entry and entry[0] == 'warn' and '拿不到图片字节' in str(entry[1])
            ]
            self.assertEqual(len(warns), 1, '同一批只报一条 warn（节流）')
            self.assertEqual(await host.db_get('interlude_sticker', {}), [])
            self.assertEqual(host.sticker_catalog, [])

    async def test_local_file_sources_are_read_without_network(self):
        """适配器给的本地路径（`onebot-file:`）直接读盘，不走网络。"""
        with tempfile.TemporaryDirectory() as tmp:
            host = self._collect_host(tmp)
            local = os.path.join(tmp, 'inbound.png')
            with open(local, 'wb') as handle:
                handle.write(_png(b'local'))
            source = 'onebot-file:%s' % local
            media = [{'source': source, 'kind': 'sticker'}]
            collected = await host.collect_incoming_stickers(media, [source])
            self.assertEqual(len(collected), 1)
            self.assertEqual(host.transport.fetched, [], '本地文件不该走网络')
            self.assertTrue(os.path.isfile(
                os.path.join(tmp, 'stickers', collected[0]['filePath']),
            ))

    async def test_auto_collect_respects_the_master_switch_and_its_own(self):
        """`enabled=false`（总闸）与 `auto_collect=false` 都必须一个都不收。"""
        with tempfile.TemporaryDirectory() as tmp:
            host = self._collect_host(tmp)
            host.transport.payloads['https://cdn.example.com/a.png'] = _png()
            media = [{'source': 'https://cdn.example.com/a.png', 'kind': 'sticker'}]
            host.config = {'stickers': {'enabled': True, 'auto_collect': False}}
            host.cached_sticker_config = None
            self.assertEqual(
                await host.collect_incoming_stickers(media, list(host.transport.payloads)), [],
            )
            host.config = {'stickers': {'enabled': False}}
            host.cached_sticker_config = None
            self.assertEqual(await host.collect_incoming_stickers(media, ['https://cdn.example.com/a.png']), [])
            self.assertEqual(host.transport.fetched, [])

    async def test_collected_asset_gets_a_description_immediately(self):
        """入库后**立刻**描述 + 进目录（不等下一个完整扫描周期）。"""
        with tempfile.TemporaryDirectory() as tmp:
            host = self._collect_host(tmp)
            described: list[Any] = []

            class _Describer:
                def available(self) -> bool:
                    return True

                async def describe_sticker(self, data_uri: str, mime_type: str, file_path: str,
                                           animated: Any, response_format: Any, max_tokens: Any) -> dict[str, Any]:
                    described.append((data_uri, mime_type, file_path, animated))
                    return {'description': '一只挥手的猫', 'aliases': ['打招呼']}

            host.sticker_describer = _Describer()

            async def image_bytes_to_native(data: bytes, mime_type: str) -> dict[str, Any]:
                return {'mime_type': 'image/png', 'data_uri': 'data:image/png;base64,AA=='}

            host.image_bytes_to_native = image_bytes_to_native
            source = 'https://cdn.example.com/a.png'
            host.transport.payloads[source] = _png()
            collected = await host.collect_incoming_stickers(
                [{'source': source, 'kind': 'sticker'}], [source],
            )
            self.assertEqual(len(collected), 1)
            self.assertEqual(len(described), 1)
            self.assertEqual(described[0][1], 'image/png')
            self.assertTrue(described[0][2].startswith('collected/'))
            row = (await host.db_get('interlude_sticker', {}))[0]
            self.assertEqual(row['status'], 'active')
            self.assertEqual(row['description'], '一只挥手的猫')
            # 进目录 = 下一次 payload 里模型看得到它。
            self.assertEqual(
                [item['assetId'] for item in host.sticker_catalog], [row['assetId']],
            )
            self.assertEqual(host.sticker_catalog[0]['description'], '一只挥手的猫')
            self.assertEqual(host.sticker_catalog[0]['aliases'], ['打招呼'])

    async def test_buffered_turn_entry_point_uses_the_queued_message(self):
        """旁路入口从**已入队的**那条消息里回读来源与种类（不靠调用方另拼一份）。"""
        with tempfile.TemporaryDirectory() as tmp:
            host = self._collect_host(tmp)
            source = 'https://cdn.example.com/a.png'
            host.transport.payloads[source] = _png()
            host.buffered_narrative_turns['p1'] = {
                'messages': [{
                    'content': '', 'image_sources': [source],
                    'media': [{'source': source, 'kind': 'market'}],
                }],
            }
            collected = await host.collect_stickers_from_buffered_turn('p1', 0)
            self.assertEqual(len(collected), 1)
            self.assertEqual(collected[0]['source'], 'auto')
            # 索引对不上 / 参与者没有回合：安静返回，不抛。
            self.assertEqual(await host.collect_stickers_from_buffered_turn('p1', 5), [])
            self.assertEqual(await host.collect_stickers_from_buffered_turn('nobody', 0), [])
            self.assertEqual(await host.collect_stickers_from_buffered_turn('p1', -1), [])

    async def test_sticker_use_counter_increments_after_a_delivery(self):
        """`uses` 计的是投递次数（控制台按它排序）。"""
        with tempfile.TemporaryDirectory() as tmp:
            host = self._collect_host(tmp)
            source = 'https://cdn.example.com/a.png'
            host.transport.payloads[source] = _png()
            collected = await host.collect_incoming_stickers(
                [{'source': source, 'kind': 'sticker'}], [source],
            )
            asset_id = collected[0]['assetId']
            await host.record_sticker_use({'assetId': asset_id})
            await host.record_sticker_use({'assetId': asset_id})
            row = (await host.db_get('interlude_sticker', {}))[0]
            self.assertEqual(row['uses'], 2)
            # 不存在的素材只 warn，不抛。
            await host.record_sticker_use({'assetId': 'nope'})

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


class ModelGuessedStickerCollectionTests(unittest.IsolatedAsyncioTestCase):
    """第二层判据：**普通图片**（`kind == 'image'`）交给识图模型确认是不是表情包。

    受控偏离 `§45.7`。这一层的全部风险有两条，两类用例各钉一条：

    * **误收**——"拿不准"必须等于"不收"，任何失败（没模型 / 超时 / JSON 坏 / 置信度不够）
      都不许入库；
    * **烧 token**——不像表情包的图**一次模型调用都不该有**，第一层认的种类同样不许
      经过模型，开关关着时连候选都不产生。
    """

    def _guess_host(self, tmp: str, describer: Any = None, **stickers: Any) -> Any:
        """一个开着第二层判据的宿主（默认的 `auto_collect_guess` 由用例显式给）。"""
        database = Database(':memory:')
        database.register_tables()
        self.addCleanup(database.close)
        os.makedirs(os.path.join(tmp, 'stickers'), exist_ok=True)
        host = _host(
            ctx=InterludeContext(base_dir=tmp),
            config={'stickers': {
                'enabled': True, 'directory': 'stickers', 'auto_collect_guess': True, **stickers,
            }},
            db=database,
            transport=_ByteTransport(),
        )
        host.sticker_describer = describer if describer is not None else _GuessingDescriber()

        async def image_bytes_to_native(data: bytes, mime_type: str) -> dict[str, Any]:
            return {'mime_type': mime_type, 'data_uri': 'data:%s;base64,AA==' % mime_type}

        host.image_bytes_to_native = image_bytes_to_native
        return host

    async def test_a_small_square_image_the_model_calls_a_sticker_is_collected_with_its_description(self):
        """`image` + 近方形小图 + 模型说 is_sticker=true → 入库、guessed=true、用回执描述。"""
        with tempfile.TemporaryDirectory() as tmp:
            describer = _GuessingDescriber(verdict={
                'is_sticker': True, 'kind': 'meme', 'confidence': 0.91, 'description': '一只挥手的猫',
            })
            host = self._guess_host(tmp, describer)
            source = 'https://cdn.example.com/plain.png'
            host.transport.payloads[source] = _sized_png(120, 120)
            collected = await host.collect_incoming_stickers(
                [{'source': source, 'kind': 'image'}], [source],
            )

            self.assertEqual(len(collected), 1, '模型确认过的表情包该入库')
            self.assertEqual(len(describer.guessed), 1, '近方形小图问了一次模型')
            row = (await host.db_get('interlude_sticker', {}))[0]
            # SQLite 的 boolean 落在列里就是 0/1（坑 8：`is True` 会漏掉它），
            # 读外部这一列的口径在 `console_api._truthy_boolean`。
            self.assertEqual(row['guessed'], 1, '「模型猜的」要留痕')
            self.assertEqual(row['source'], 'auto', 'source 仍只有 auto / manual 两个取值')
            self.assertEqual(row['description'], '一只挥手的猫', '回执里的描述直接用')
            self.assertFalse(row['descriptionManual'], '模型写的描述不是「人写的」')
            self.assertEqual(row['status'], 'active', '有描述就该立刻可用')
            # ⚠️ 关键：**没有再跑一次描述调用**（不为同一张图付两次钱）。
            self.assertEqual(describer.described, [], '回执带了描述就不该再调描述模型')
            self.assertEqual(
                [item['assetId'] for item in host.sticker_catalog], [row['assetId']],
                '入库即可进目录',
            )
            self.assertEqual(host.sticker_catalog[0]['description'], '一只挥手的猫')

    async def test_receipt_without_a_description_falls_back_to_the_description_flow(self):
        """回执**没有**描述 → 照旧走描述流程（只有这一种情况才多一次调用）。"""
        with tempfile.TemporaryDirectory() as tmp:
            describer = _GuessingDescriber(verdict={
                'is_sticker': True, 'kind': 'reaction', 'confidence': 0.8, 'description': '   ',
            })
            host = self._guess_host(tmp, describer)
            source = 'https://cdn.example.com/plain.png'
            host.transport.payloads[source] = _sized_png(100, 100)
            collected = await host.collect_incoming_stickers(
                [{'source': source, 'kind': 'image'}], [source],
            )
            self.assertEqual(len(collected), 1)
            self.assertEqual(len(describer.described), 1, '没有描述就得走原来的描述流程')
            row = (await host.db_get('interlude_sticker', {}))[0]
            self.assertEqual(row['guessed'], 1)
            self.assertEqual(row['description'], '模型后来补的描述')
            self.assertEqual(row['status'], 'active')

    async def test_anything_short_of_a_confident_yes_never_enters_the_library(self):
        """判成照片 / 置信度不够 / JSON 坏 / 抛异常 → 一律不收（穷举）。"""
        cases: list[tuple[str, Any]] = [
            ('判成实拍照片', {'is_sticker': False, 'kind': 'photo', 'confidence': 0.99}),
            ('判成截图', {'is_sticker': False, 'kind': 'screenshot', 'confidence': 0.95}),
            ('置信度不够', {'is_sticker': True, 'kind': 'meme', 'confidence': 0.2}),
            ('缺 confidence', {'is_sticker': True, 'kind': 'meme'}),
            ('is_sticker 不是布尔', {'is_sticker': 'true', 'confidence': 0.99}),
            ('回执不是对象（JSON 解析后的原样文本）', '不是 JSON'),
            ('回执缺字段', {}),
        ]
        for label, verdict in cases:
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                describer = _GuessingDescriber(verdict=verdict)
                host = self._guess_host(tmp, describer)
                source = 'https://cdn.example.com/plain.png'
                host.transport.payloads[source] = _sized_png(64, 64)
                self.assertEqual(
                    await host.collect_incoming_stickers(
                        [{'source': source, 'kind': 'image'}], [source],
                    ),
                    [], label,
                )
                self.assertEqual(await host.db_get('interlude_sticker', {}), [], label)
                self.assertEqual(host.sticker_catalog, [], label)
                # 判定**被问过**（不是"没问就拒"），只是答案不达标。
                self.assertEqual(len(describer.guessed), 1, label)
                # 也没留下文件。
                collected_dir = os.path.join(tmp, 'stickers', 'collected')
                self.assertEqual(
                    sorted(os.listdir(collected_dir)) if os.path.isdir(collected_dir) else [], [], label,
                )

    async def test_a_failing_model_call_is_not_collected_and_never_raises(self):
        """模型调用抛异常 / 超时 → 按"不收"处理，异常不许冒到收藏任务之外。"""
        with tempfile.TemporaryDirectory() as tmp:
            describer = _GuessingDescriber(error=RuntimeError('network down'))
            host = self._guess_host(tmp, describer)
            source = 'https://cdn.example.com/plain.png'
            host.transport.payloads[source] = _sized_png(64, 64)
            self.assertEqual(
                await host.collect_incoming_stickers([{'source': source, 'kind': 'image'}], [source]), [],
            )
            self.assertEqual(await host.db_get('interlude_sticker', {}), [])

            # 超时：把软时限压到 1ms，让模拟的慢模型必然超时。
            hung = _GuessingDescriber(delay=0.05)
            host = self._guess_host(tmp, hung)
            host.transport.payloads[source] = _sized_png(64, 64)
            original = chunk2_module.STICKER_GUESS_TIMEOUT_SECONDS
            chunk2_module.STICKER_GUESS_TIMEOUT_SECONDS = 0.001
            self.addCleanup(setattr, chunk2_module, 'STICKER_GUESS_TIMEOUT_SECONDS', original)
            self.assertEqual(
                await host.collect_incoming_stickers([{'source': source, 'kind': 'image'}], [source]), [],
            )
            self.assertEqual(await host.db_get('interlude_sticker', {}), [])

    async def test_no_vision_model_warns_once_and_collects_nothing(self):
        """根本没配识图模型 → 零入库 + **一条节流 warn**（能力缺失，坑 25）。"""
        with tempfile.TemporaryDirectory() as tmp:
            host = self._guess_host(tmp, SilentStickerDescriber())
            sources = ['https://cdn.example.com/%d.png' % index for index in range(3)]
            media = [{'source': source, 'kind': 'image'} for source in sources]
            for index, source in enumerate(sources):
                host.transport.payloads[source] = _sized_png(80 + index, 80)
            self.assertEqual(await host.collect_incoming_stickers(media, sources), [])
            self.assertEqual(await host.db_get('interlude_sticker', {}), [])
            warns = [
                entry for entry in host.reports
                if entry and entry[0] == 'warn' and '没有可用的识图模型' in str(entry[1])
            ]
            self.assertEqual(len(warns), 1, '三张图只报一条 warn（节流）')

    async def test_photo_shaped_and_unparsable_images_never_cost_a_model_call(self):
        """大图 / 长宽比像照片 / 图片头解析不出 → **零模型调用**（预筛在调模型之前）。"""
        cases: dict[str, bytes] = {
            '2000×1500 的照片': _sized_png(2000, 1500),
            '800×200 的长条截图': _sized_png(800, 200),
            '500×200 的横幅': _sized_png(500, 200),
            '只有魔数的残缺 PNG': b'\x89PNG\r\n\x1a\n' + b'x' * 40,
        }
        for label, payload in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                describer = _GuessingDescriber()
                host = self._guess_host(tmp, describer)
                source = 'https://cdn.example.com/plain.png'
                host.transport.payloads[source] = payload
                self.assertEqual(
                    await host.collect_incoming_stickers(
                        [{'source': source, 'kind': 'image'}], [source],
                    ),
                    [], label,
                )
                self.assertEqual(describer.guessed, [], '%s：不该调模型' % label)
                self.assertEqual(await host.db_get('interlude_sticker', {}), [], label)
                self.assertEqual(host.transport.fetched, [source], '字节还是要下的（预筛读图片头）')
                # 没判定的原因要留在 debug 里（用户问"为什么这张没被判定"时靠它）。
                prefiltered = [
                    entry for entry in host.reports
                    if entry and '尺寸/形状预筛' in str(entry[2] if len(entry) > 2 else entry[1])
                ]
                self.assertEqual(len(prefiltered), 1, '%s：该留一条预筛 debug' % label)

    async def test_a_gif_and_a_transparent_png_are_candidates_even_when_large(self):
        """GIF 与带 alpha 的 PNG 一律算候选（聊天里这两种几乎只当表情用）。"""
        for label, payload in (
            ('GIF', _sized_gif(800, 600)),
            ('RGBA PNG', _sized_png(900, 900, color_type=6)),
            ('tRNS PNG', _sized_png(900, 900, color_type=2, trns=True)),
        ):
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                describer = _GuessingDescriber()
                host = self._guess_host(tmp, describer)
                source = 'https://cdn.example.com/plain.bin'
                host.transport.payloads[source] = payload
                collected = await host.collect_incoming_stickers(
                    [{'source': source, 'kind': 'image'}], [source],
                )
                self.assertEqual(len(describer.guessed), 1, '%s：该问模型' % label)
                self.assertEqual(len(collected), 1, label)
                self.assertEqual((await host.db_get('interlude_sticker', {}))[0]['guessed'], 1)

    async def test_the_first_layer_never_touches_the_model(self):
        """`sticker` / `animated` / `market` → 直接收，**零模型调用**（穷举）。"""
        with tempfile.TemporaryDirectory() as tmp:
            describer = _GuessingDescriber()
            host = self._guess_host(tmp, describer)
            kinds = ('sticker', 'animated', 'market')
            sources = ['https://cdn.example.com/%s' % kind for kind in kinds]
            media = [{'source': source, 'kind': kind} for source, kind in zip(sources, kinds)]
            for index, source in enumerate(sources):
                host.transport.payloads[source] = _sized_png(2000, 1500 + index, color_type=2 + 0)
            collected = await host.collect_incoming_stickers(media, sources)
            self.assertEqual(len(collected), 3, '第一层认的种类不受预筛影响')
            self.assertEqual(describer.guessed, [], '第一层永远不该走模型')
            rows = await host.db_get('interlude_sticker', {})
            self.assertEqual({row['guessed'] for row in rows}, {False}, '不是"猜的"')

    async def test_the_switch_off_means_zero_model_calls_and_zero_rows(self):
        """`auto_collect_guess=false`（默认）→ 普通图片零候选、零模型调用、零入库。"""
        with tempfile.TemporaryDirectory() as tmp:
            describer = _GuessingDescriber()
            host = self._guess_host(tmp, describer, auto_collect_guess=False)
            source = 'https://cdn.example.com/plain.png'
            host.transport.payloads[source] = _sized_png(64, 64)
            media = [{'source': source, 'kind': 'image'}]
            self.assertEqual(await host.collect_incoming_stickers(media, [source]), [])
            self.assertEqual(describer.guessed, [])
            self.assertEqual(await host.db_get('interlude_sticker', {}), [])
            # 连"要不要建任务"的同步预筛也说不：开关关着时与今天逐字一致。
            self.assertFalse(chunk2_module._has_collectible_media(media))
            self.assertTrue(chunk2_module._has_collectible_media(media, True))

    async def test_dedup_wins_over_the_model_and_over_the_second_layer(self):
        """同内容已经在库（`missing` 也算）→ 不调模型、不入库，只把行复活。"""
        with tempfile.TemporaryDirectory() as tmp:
            describer = _GuessingDescriber()
            host = self._guess_host(tmp, describer)
            source = 'https://cdn.example.com/plain.png'
            payload = _sized_png(64, 64)
            host.transport.payloads[source] = payload
            now = host.now()
            created = await host.db_create('interlude_sticker', {
                'assetId': 'sticker-old', 'filePath': 'collected/old.png', 'group': 'collected',
                'mimeType': 'image/png', 'animated': False, 'size': len(payload),
                'hash': hashlib.sha256(payload).hexdigest(), 'name': 'old', 'source': 'auto',
                'description': '早就描述过', 'descriptionManual': False, 'aliases': [],
                'status': 'missing', 'guessed': True, 'createdAt': now, 'updatedAt': now,
            })
            self.assertEqual(
                await host.collect_incoming_stickers([{'source': source, 'kind': 'image'}], [source]), [],
            )
            self.assertEqual(describer.guessed, [], '同内容已经在库里就不该再问模型')
            rows = await host.db_get('interlude_sticker', {})
            self.assertEqual(len(rows), 1, '不重复入库')
            # 文件曾被删、同内容又回来：只复活状态（不重花模型调用）。
            self.assertEqual(rows[0]['id'], created['id'])
            self.assertEqual(rows[0]['status'], 'active')
            self.assertEqual(rows[0]['description'], '早就描述过')

    async def test_per_message_and_per_minute_budgets_cap_the_model_calls(self):
        """一条消息多张候选 + 短时间大量图片：判定调用数被两个上限夹住。"""
        with tempfile.TemporaryDirectory() as tmp:
            describer = _GuessingDescriber()
            host = self._guess_host(tmp, describer)

            def batch(prefix: str, count: int) -> tuple[list[dict[str, Any]], list[str]]:
                sources = ['https://cdn.example.com/%s-%d.png' % (prefix, index) for index in range(count)]
                for index, source in enumerate(sources):
                    # 每张内容都不同（否则会被 sha256 去重合并成一条）。
                    host.transport.payloads[source] = _sized_png(
                        64, 64, extra=('%s-%d' % (prefix, index)).encode(),
                    )
                return [{'source': source, 'kind': 'image'} for source in sources], sources

            media, sources = batch('a', 5)
            await host.collect_incoming_stickers(media, sources)
            self.assertEqual(
                len(describer.guessed), chunk2_module.STICKER_GUESS_MAX_PER_MESSAGE,
                '一条消息最多问 %d 次' % chunk2_module.STICKER_GUESS_MAX_PER_MESSAGE,
            )

            # 再来两条消息：每条仍只花「每条消息上限」，三条累计正好触到每分钟窗口的上限。
            for prefix in ('b', 'c'):
                media, sources = batch(prefix, 5)
                await host.collect_incoming_stickers(media, sources)
            self.assertEqual(
                len(describer.guessed), chunk2_module.STICKER_GUESS_MAX_PER_MINUTE,
                '一分钟内的总量被窗口夹住',
            )

            media, sources = batch('d', 5)
            await host.collect_incoming_stickers(media, sources)
            self.assertEqual(
                len(describer.guessed), chunk2_module.STICKER_GUESS_MAX_PER_MINUTE,
                '超出窗口额度的调用一律丢弃（只记 debug）',
            )
            # 超限的那些**不入库**（拿不准就不收），但也不抛。
            self.assertEqual(
                len(await host.db_get('interlude_sticker', {})),
                chunk2_module.STICKER_GUESS_MAX_PER_MINUTE,
            )

    async def test_an_oversized_local_file_is_never_downloaded_nor_guessed(self):
        """来源自带体积（本地文件）超过上限 → 连字节都不读、不调模型。"""
        with tempfile.TemporaryDirectory() as tmp:
            describer = _GuessingDescriber()
            host = self._guess_host(tmp, describer, max_file_size_mb=1)
            local = os.path.join(tmp, 'big.png')
            with open(local, 'wb') as handle:
                handle.write(_sized_png(64, 64) + b'x' * (1024 * 1024))
            source = 'onebot-file:%s' % local
            self.assertEqual(
                await host.collect_incoming_stickers([{'source': source, 'kind': 'image'}], [source]), [],
            )
            self.assertEqual(describer.guessed, [], '不下载就不该有判定调用')
            self.assertEqual(host.transport.fetched, [])
            self.assertEqual(await host.db_get('interlude_sticker', {}), [])

    async def test_guessed_assets_get_their_embedding_indexed_like_described_ones(self):
        """回执描述进向量索引（否则"模型猜进来的"素材在语义过滤里永远缺一条）。"""
        with tempfile.TemporaryDirectory() as tmp:
            describer = _GuessingDescriber(verdict={
                'is_sticker': True, 'kind': 'meme', 'confidence': 0.9, 'description': '一只猫',
            })
            host = self._guess_host(tmp, describer)
            embedded: list[str] = []

            async def embed_text(value: str) -> list[float]:
                embedded.append(value)
                return [1.0, 0.0]

            host.embed_text = embed_text
            host.config = {'model': {'embedding': {'semanticStickerFilter': True}},
                           'stickers': {'enabled': True, 'directory': 'stickers', 'auto_collect_guess': True}}
            host.cached_sticker_config = None
            source = 'https://cdn.example.com/plain.png'
            host.transport.payloads[source] = _sized_png(64, 64)
            await host.collect_incoming_stickers([{'source': source, 'kind': 'image'}], [source])
            self.assertEqual(embedded, ['一只猫'])
            row = (await host.db_get('interlude_sticker', {}))[0]
            self.assertEqual(row['embedding'], [1.0, 0.0])


def _png_chunk(name: bytes, data: bytes) -> bytes:
    """一个结构合法的 PNG 块（长度 + 类型 + 数据 + CRC）。"""
    return (
        len(data).to_bytes(4, 'big') + name + data
        + zlib.crc32(name + data).to_bytes(4, 'big')
    )


def _sized_png(
    width: int, height: int, color_type: int = 2, trns: bool = False, extra: bytes = b'',
) -> bytes:
    """一张**图片头合法**的 PNG：宽高真的写在 `IHDR` 里（第二层的预筛要读它）。

    与 `_png()`（只保证魔数）刻意分开：第一层不关心尺寸，第二层全靠它。
    """
    ihdr = (
        width.to_bytes(4, 'big') + height.to_bytes(4, 'big')
        + bytes([8, color_type, 0, 0, 0])
    )
    body = _png_chunk(b'IHDR', ihdr)
    if trns:
        body += _png_chunk(b'tRNS', b'\x00' * 6)
    body += _png_chunk(b'IDAT', b'') + _png_chunk(b'IEND', b'')
    return b'\x89PNG\r\n\x1a\n' + body + extra


def _sized_gif(width: int, height: int) -> bytes:
    """一张 GIF：逻辑屏幕描述符里带宽高（小端 16 位）。"""
    return b'GIF89a' + width.to_bytes(2, 'little') + height.to_bytes(2, 'little') + b'\x00' * 8


class _GuessingDescriber:
    """第二层判据的模型桩：只实现 `StickerDescriber` 协议里这一层要用的两个方法。

    同时实现 `available()` / `describe_sticker()`，好把"描述调用"单独计数——
    "回执带描述时不许再调描述模型"那条断言全靠 `described`。
    """

    def __init__(
        self, verdict: Any = None, error: Optional[BaseException] = None, delay: float = 0.0,
    ) -> None:
        self.verdict = {'is_sticker': True, 'kind': 'meme', 'confidence': 0.9,
                        'description': '一只猫'} if verdict is None else verdict
        self.error = error
        self.delay = delay
        self.guessed: list[tuple[str, str, str]] = []
        self.described: list[Any] = []

    def available(self) -> bool:
        return True

    def guess_sticker_available(self) -> bool:
        return True

    async def guess_sticker(
        self, data_uri: str, mime_type: str, file_name: str = '',
        response_format: Any = 'json-object', max_tokens: Any = 256,
    ) -> Any:
        self.guessed.append((data_uri, mime_type, file_name))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.verdict

    async def describe_sticker(
        self, data_uri: str, mime_type: str, file_name: str, animated: Any,
        response_format: Any = 'json-object', max_tokens: Any = 768,
    ) -> Any:
        self.described.append((data_uri, mime_type, file_name, animated))
        return {'description': '模型后来补的描述', 'aliases': ['猫']}


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
            'enabled': False, 'auto_collect': True, 'auto_collect_guess': False,
            'directory': 'data/hds-interlude/stickers',
            'max_file_size_mb': 10, 'catalog_limit': 40,
            'description_max_tokens': 768, 'description_response_format': 'json-object',
        })
        configured = _host(config={'stickers': {
            'enabled': True, 'directory': '  my/stickers  ', 'maxFileSizeMB': 99,
            'catalogLimit': 999, 'descriptionMaxTokens': 10, 'descriptionResponseFormat': 'prompt-only',
        }})
        self.assertEqual(configured.sticker_config, {
            'enabled': True, 'auto_collect': True, 'auto_collect_guess': False,
            'directory': 'my/stickers', 'max_file_size_mb': 30.0,
            'catalog_limit': 80, 'description_max_tokens': 256,
            'description_response_format': 'prompt-only',
        })
        fallback = _host(config={'stickers': {'catalogLimit': 0, 'descriptionResponseFormat': 'json-object'}})
        self.assertEqual(fallback.sticker_config['catalog_limit'], 40)
        self.assertEqual(fallback.sticker_config['description_response_format'], 'json-object')
        # 自动收藏：默认开，**只有显式 false 才关**（camelCase 旧名也认）。
        self.assertIs(fallback.sticker_config['auto_collect'], True)
        self.assertIs(
            _host(config={'stickers': {'autoCollect': False}}).sticker_config['auto_collect'], False,
        )
        self.assertIs(
            _host(config={'stickers': {'auto_collect': False}}).sticker_config['auto_collect'], False,
        )
        # 第二层（模型判定普通图片，§45.7）：**默认关**，只有显式 true 才开
        # （`is True` 是刻意的：缺失 / NULL / 字符串一律当关——多花 token 的开关不许"意外打开"）。
        self.assertIs(fallback.sticker_config['auto_collect_guess'], False)
        self.assertIs(
            _host(config={'stickers': {'autoCollectGuess': True}}).sticker_config['auto_collect_guess'], True,
        )
        self.assertIs(
            _host(config={'stickers': {'auto_collect_guess': True}}).sticker_config['auto_collect_guess'], True,
        )
        for noise in (1, 'true', 'yes'):
            with self.subTest(noise=noise):
                self.assertIs(
                    _host(config={'stickers': {'auto_collect_guess': noise}})
                    .sticker_config['auto_collect_guess'],
                    False,
                    '非布尔真值不当成"打开"',
                )


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
