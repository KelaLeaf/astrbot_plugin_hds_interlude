"""`plugin/core/service/chunk5.py` 的单元测试（stdlib `unittest`）。

对应上游 `src/service.ts:4186-4813`（Chunk5）。测试分两层：

1. **上游用例移植**（`ServiceChunk5UpstreamTests`）：上游测试里**断言本范围成员**的用例逐条移植。
   - `beta6-handoff.test.ts` → 「scene anchor no longer overwrites scene/arc summary with
     director prose」断言 `persistTimelineSceneAnchor`；
   - `evidence-repair.test.ts` → 「condition chains keep whole originals and do not leak
     branches」断言 `contactThreads`（`tests/test_script_evidence.py` 显式退回
     「归属 core/service.py 移植任务」，因此在 Chunk5 落地）；
   - `m10-cooperation.test.ts` → 「mixed due work does not starve committed typing or start
     background narration during a live turn」的 `dueIntents` / `deliverDueSplitSegments`
     边界：需要 Chunk6/Chunk7 才能执行，用 `@unittest.skipUnless(...)` **真能力守门**。
   上游指定的另外两个文件（`follow-up-commitment.test.ts` / `conversation-interruption.test.ts`）
   断言的是 `narrator.toPromptPayload` / `helpers.shouldSupersedeNarrativeRequest` 等本范围之外的
   成员，故不在本文件重复移植（避免与其它模块的测试文件抢同一批断言）。

2. **本范围行为用例**（`ServiceChunk5Tests`）：用**真实** `Database` + 真实
   `InterludeService`（Chunk0+Chunk5，其余 chunk 用实例级替身）验证可独立验证的行为：
   裁剪与边界、剧情余波生命周期、私密隔离、到期意图视图、网页观察缓存与公开性校验、
   浏览器并发闸门、重试与到期唤醒、Alter 旁路分析。

运行：
    cd /home/kela/文档/harness/hds-interlude && python3 -m unittest plugin.tests.test_service_chunk5 -v

时间约定：上游 `new Date('...Z')` → timezone-aware `datetime`；时间源用
`InterludeContext(clock=...)` 注入，不依赖真实时钟。
数据库文件建在 `tempfile.TemporaryDirectory()` 里（**不放 /tmp**）。
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from plugin.core.database import Database
from plugin.core.service import InterludeContext, InterludeService
from plugin.core.service import chunk5 as chunk5_module
from plugin.core.story_state import decode_story_state, encode_story_state
from plugin.core.time import iso

UTC = timezone.utc
NOW = datetime(2026, 9, 7, 4, 0, tzinfo=UTC)

#: 上游 `models.Time.second`。
SECOND = timedelta(seconds=1)


def _has_member(name: str) -> bool:
    """该成员是否已由 chunk5 落地（`skipUnless` 的能力守门，不掩盖失败）。"""
    return callable(getattr(InterludeService, name, None))


async def _resolved(value: Any = None) -> Any:
    """`async () => value`（替身用）。"""
    return value


async def _noop_async(*args: Any, **kwargs: Any) -> None:
    """`async () => void`（替身用）。"""
    return None


async def _wait_until(predicate: Any, timeout: float = 2.0) -> bool:
    """等到 `predicate()` 为真（后台计时器回调是 `call_soon` + 任务，必须等它跑完）。

    直接 `sleep(0.05)` 在高负载下会留下仍在 `asyncio.to_thread` 里的数据库读取线程，
    测试结束关库后它才回来，就会变成跨用例的偶发 `sqlite3.InterfaceError`。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate() and loop.time() < deadline:
        await asyncio.sleep(0.01)
    return bool(predicate())


def _intent(intent_id: int, intent_type: str, participant_id: str = 'alice') -> dict[str, Any]:
    """上游 `m10-cooperation.test.ts` 的 `intent()` 助手（领域形状 snake_case）。"""
    return {
        'id': intent_id, 'type': intent_type, 'participant_id': participant_id, 'story_id': 's',
        'summary': 'task %d' % intent_id, 'status': 'pending', 'not_before': NOW,
        'created_at': NOW, 'updated_at': NOW, 'payload': {},
    }


class _SceneAnchorHost:
    """上游 `beta6-handoff.test.ts` 用例里的 `host` 字面量。

    只提供 `persistTimelineSceneAnchor` 用到的三个成员，其余一律不装。
    """

    def __init__(self, scene: Any):
        self._scene = scene
        self.memory_config = {'sceneHookCharacters': 300}
        self.writes: list[dict[str, Any]] = []

    async def active_scene(self, story_id: str) -> Any:
        return self._scene

    async def db_set(self, table: str, query: Any, patch: Any) -> None:
        self.writes.append({'table': table, 'query': query, 'patch': patch})


# =========================================================================== #
# 上游用例移植
# =========================================================================== #

class ServiceChunk5UpstreamTests(unittest.TestCase):
    """上游用例逐条移植（断言原样保留）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(os.path.join(self._tmp.name, 'hds-upstream.sqlite3'))
        self.db.register_tables()
        self.addCleanup(self.db.close)
        self.service = self._service()

    def _service(self, **sections: Any) -> InterludeService:
        config: dict[str, Any] = {'logging': {'level': 'silent'}}
        config.update(sections)
        service = InterludeService(
            InterludeContext(logger=None, database=self.db, clock=lambda: NOW), config,
        )
        # `report` 由 Chunk9 提供；测试里静音，避免输出被日志淹没。
        service.report = lambda *args, **kwargs: None
        return service

    # -- `upstream/test/beta6-handoff.test.ts` --------------------------------- #

    @unittest.skipUnless(
        _has_member('persist_timeline_scene_anchor'), 'chunk5 未组装（ServiceChunk5 缺失）',
    )
    def test_scene_anchor_no_longer_overwrites_scene_arc_summary_with_director_prose(self):
        """上游：`scene anchor no longer overwrites scene/arc summary with director prose`。"""
        host = _SceneAnchorHost({'id': 9})

        async def scenario() -> None:
            await InterludeService.persist_timeline_scene_anchor(
                host, 's',
                {'activity': {'value': '看书', 'quote': '她回到床边看书。'}},
                15448, NOW,
            )

        asyncio.run(scenario())
        self.assertEqual(len(host.writes), 1)
        written = host.writes[0]
        self.assertEqual(written['table'], 'interlude_scene')
        self.assertEqual(written['query'], {'id': 9})
        # `assert.match(writes[0].hook, /15448/)`
        self.assertRegex(written['patch']['hook'], r'15448')
        # 场景 / 弧摘要与检查点归后台编辑器所有：这里绝不能覆盖它们。
        self.assertNotIn('summary', written['patch'])
        self.assertNotIn('lastEntryId', written['patch'])
        self.assertEqual(written['patch']['hook'], 'Original #15448: 她回到床边看书。')
        self.assertEqual(written['patch']['updatedAt'], NOW)

    # -- `upstream/test/evidence-repair.test.ts`（`contactThreads` 部分） -------- #

    def test_condition_chains_retain_whole_originals_and_do_not_leak_branches(self):
        """上游：`condition chains keep whole originals and do not leak branches`。

        `tests/test_script_evidence.py` 把这半条断言明确退回「归属 core/service.py 移植任务」
        （`contactThreads` 由 Chunk5 提供），因此在这里落地并真正执行。
        """
        if not _has_member('contact_threads'):
            self.skipTest('chunk5 未组装（ServiceChunk5 缺失）')

        def add_entry(entry_id: int, kind: str, content: str, participant_id: str = 'alice') -> None:
            self.db.insert('interlude_script_entry', {
                'id': entry_id, 'storyId': 'story', 'participantId': participant_id, 'kind': kind,
                'actor': 'user' if kind == 'user-message' else 'character', 'content': content,
                'occurredAt': NOW, 'metadata': {}, 'createdAt': NOW,
            })

        def add_fact(fact_id: int, content: str, source_ids: list[int], participant_id: str = 'alice',
                     knowledge: Any = None) -> dict[str, Any]:
            row: dict[str, Any] = {
                'id': fact_id, 'storyId': 'story', 'participantId': participant_id, 'scope': 'promise',
                'content': content, 'sourceEntryIds': source_ids, 'importance': 0.5, 'confidence': 0.6,
                'unresolved': True, 'status': 'active', 'lastSeenAt': NOW, 'createdAt': NOW, 'updatedAt': NOW,
            }
            if knowledge is not None:
                row['knowledge'] = knowledge
            return self.db.insert('interlude_fact', row)

        add_entry(100, 'character-message', '至少连续一周再说')
        add_entry(101, 'user-message', '好吧')
        add_entry(200, 'user-message', '如果好看，我想再跟你看一次')
        add_entry(201, 'character-message', '別急')
        add_entry(202, 'user-message', '私密', 'bob')
        condition = add_fact(10, '早睡一周才看电影', [100])
        proposal = add_fact(11, '如果好看，想再看一次', [200], knowledge={
            'mode': 'conditional', 'clauses': [], 'related_fact_ids': [condition['id']],
        })
        add_fact(12, '另一个人的电影', [202], 'bob')

        async def scenario() -> tuple[list[Any], list[Any]]:
            selected = [self.db.get('interlude_fact', {'id': proposal['id']})]
            return (
                await self.service.contact_threads('story', selected, 'alice'),
                await self.service.contact_threads('story', selected, None),
            )

        chain, public_chain = asyncio.run(scenario())
        self.assertTrue(
            any(any(item['content'] == '至少连续一周再说' for item in link['originals']) for link in chain),
        )
        self.assertNotIn('私密', json.dumps(chain, ensure_ascii=False, default=str))
        self.assertEqual(public_chain, [])

    # -- `upstream/test/m10-cooperation.test.ts` -------------------------------- #

    @unittest.skipUnless(
        _has_member('deliver_due_split_segments') and _has_member('sweep'),
        'Chunk6/Chunk7 未落地（deliverDueSplitSegments / sweep）',
    )
    def test_mixed_due_work_does_not_starve_committed_typing_or_start_background_narration(self):
        """上游：`mixed due work does not starve committed typing or start background narration during a live turn`。"""
        counters = {'deliveries': 0, 'advances': 0}

        async def due_intents(story_id: str, now: Any) -> list[Any]:
            return [
                _intent(1, 'split-message'), _intent(2, 'follow-up-commitment'), _intent(3, 'browser-research'),
            ]

        async def deliver_due_split_segments(story_id: str) -> None:
            counters['deliveries'] += 1

        async def advance_story(*args: Any, **kwargs: Any) -> list[Any]:
            counters['advances'] += 1
            return []

        service = SimpleNamespace(
            desktop_runtime_phase='running', database_resetting=False, sweep_running=False,
            get_canonical_story=lambda: _resolved({'id': 's'}),
            can_handle_story=lambda story: True,
            has_pending_narrative=lambda story_id: True,
            now_ms=lambda: int(NOW.timestamp() * 1000),
            now=lambda: NOW,
            due_intents=due_intents,
            deliver_due_split_segments=deliver_due_split_segments,
            advance_story=advance_story,
            report_operation=lambda *args, **kwargs: None,
            report_standalone_operation=lambda *args, **kwargs: None,
        )

        asyncio.run(InterludeService.sweep(service))
        self.assertEqual(counters['deliveries'], 1)
        self.assertEqual(counters['advances'], 0)
        self.assertFalse(service.sweep_running)


# =========================================================================== #
# 本范围行为用例
# =========================================================================== #

class ServiceChunk5Tests(unittest.TestCase):
    """真实 `Database` + 真实 `InterludeService` 下的 Chunk5 行为。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(os.path.join(self._tmp.name, 'hds-chunk5.sqlite3'))
        self.db.register_tables()
        self.addCleanup(self.db.close)
        self.service = self._service()

    def _service(self, **sections: Any) -> InterludeService:
        config: dict[str, Any] = {'logging': {'level': 'silent'}}
        config.update(sections)
        service = InterludeService(
            InterludeContext(logger=None, database=self.db, clock=lambda: NOW), config,
        )
        service.report = lambda *args, **kwargs: None
        return service

    def _add_intent(self, **row: Any) -> dict[str, Any]:
        """插一行意图（库列名保持上游 camelCase）。"""
        payload: dict[str, Any] = {
            'storyId': 's', 'participantId': '', 'type': 'follow-up', 'summary': 'x',
            'notBefore': NOW, 'status': 'pending', 'payload': {}, 'createdAt': NOW, 'updatedAt': NOW,
        }
        payload.update(row)
        return self.db.insert('interlude_intent', payload)

    # ------------------------------------------------------------------ #
    # 条目 / 记忆 / 意图落库
    # ------------------------------------------------------------------ #

    def test_append_entry_keeps_script_whole_and_refreshes_recall_cache(self):
        if not _has_member('append_entry'):
            self.skipTest('chunk5 未组装')

        async def scenario() -> dict[str, Any]:
            service = self.service
            service.history_vectors['s'] = {}
            long_text = '她' * 13_000
            return {
                'script': await service.append_entry('s', {
                    'kind': 'script', 'actor': 'narrator', 'content': long_text, 'metadata': {},
                }, NOW),
                'message': await service.append_entry('s', {
                    'kind': 'user-message', 'content': long_text, 'occurredAt': NOW,
                }, NOW),
                'cached_source': await service.append_entry('s', {
                    'kind': 'character-message', 'actor': 'character',
                    'content': '[微笑]我去了书店，看到那本旧书。', 'occurredAt': NOW,
                    'metadata': {'episodeTags': {'places': ['书店'], 'people': ['不存在的人']}},
                }, NOW, 'p1'),
                'life': await service.append_entry('s', {
                    'kind': 'life', 'content': 'x', 'occurredAt': NOW,
                }, NOW),
            }

        rows = asyncio.run(scenario())
        # 剧本原文整条保留；其它类型裁到 12_000。
        self.assertEqual(rows['script']['kind'], 'script')
        self.assertEqual(rows['script']['actor'], 'narrator')
        self.assertEqual(len(rows['script']['content']), 13_000)
        self.assertEqual(len(rows['message']['content']), 12_000)
        # 缺省 actor 回落 'character'；缺省 occurredAt 回落 now。
        self.assertEqual(rows['message']['actor'], 'character')
        self.assertEqual(rows['script']['occurredAt'], NOW)

        cache = self.service.history_vectors['s']
        cached = cache[rows['cached_source']['id']]
        self.assertEqual(sorted(cached), [
            'checkpoint', 'content', 'frame_id', 'kind', 'occurred_at', 'participant_id', 'tags',
        ])
        self.assertEqual(cached['kind'], 'character-message')
        self.assertEqual(cached['participant_id'], 'p1')
        self.assertEqual(cached['occurred_at'], iso(NOW))
        self.assertIsNone(cached['frame_id'])
        self.assertIsNone(cached['checkpoint'])
        # `groundedEpisodeTags`：只有字面出现在原文里的标签才留下。
        self.assertEqual(cached['tags'], ['书店'])
        # `promptVisibleMessageContent`：主角自己发出的消息里的表情标记被投影成语义。
        self.assertEqual(cached['content'], '〈附带微笑表情〉我去了书店，看到那本旧书。')
        # 非召回类型不进召回缓存。
        self.assertNotIn(rows['life']['id'], cache)
        self.assertIn(rows['message']['id'], cache)

    def test_append_memory_clamps_and_append_intent_bounds_active_consequences(self):
        if not _has_member('append_intent'):
            self.skipTest('chunk5 未组装')

        async def scenario() -> None:
            service = self.service
            await service.append_memory(
                's', {'category': 'c' * 40, 'content': 'm' * 5_000, 'importance': 5},
                NOW, 'p1', 7,
            )
            await service.append_memory('s', {'content': 'bare'}, NOW)
            # 过去的普通计划被丢弃（上游 `notBefore <= now`）。
            await service.append_intent('s', {
                'type': 'follow-up', 'summary': 'past', 'notBefore': iso(NOW - timedelta(minutes=1)),
            }, NOW)
            # 未来的普通计划保留。
            await service.append_intent('s', {
                'type': 'follow-up', 'summary': 'future', 'notBefore': iso(NOW + timedelta(minutes=30)),
            }, NOW)
            # 没有 expiresAt 的余波不是有效余波。
            await service.append_intent('s', {
                'type': 'active-consequence', 'summary': 'no-expiry', 'notBefore': iso(NOW),
                'payload': {'lifecycle': 'active'},
            }, NOW)
            # 余波：允许从 now 开始；寿命被 activeConsequenceMaxDays 截断。
            await service.append_intent('s', {
                'type': 'active-consequence', 'summary': 'live', 'notBefore': iso(NOW),
                'payload': {'lifecycle': 'active', 'expiresAt': iso(NOW + timedelta(days=90))},
            }, NOW, 'p1')

        asyncio.run(scenario())
        memories = self.db.all('interlude_memory', {'storyId': 's'})
        self.assertEqual(len(memories[0]['category']), 32)
        self.assertEqual(len(memories[0]['content']), 4_000)
        self.assertEqual(memories[0]['importance'], 1.0)
        self.assertEqual(memories[0]['sourceEntryId'], 7)
        self.assertEqual(memories[1]['category'], 'fact')
        self.assertIsNone(memories[1]['sourceEntryId'])

        by_summary = {row['summary']: row for row in self.db.all('interlude_intent', {'storyId': 's'})}
        self.assertNotIn('past', by_summary)
        self.assertIn('future', by_summary)
        self.assertNotIn('no-expiry', by_summary)
        live = by_summary['live']
        self.assertEqual(live['participantId'], 'p1')
        self.assertEqual(live['status'], 'pending')
        self.assertEqual(live['payload']['strength'], 0.55)
        self.assertEqual(live['payload']['expiresAt'], iso(NOW + timedelta(days=7)),
                         '余波寿命必须被 activeConsequenceMaxDays 截断')

    def test_active_consequences_expire_and_updates_stay_branch_local(self):
        if not _has_member('apply_intent_updates'):
            self.skipTest('chunk5 未组装')
        expired = self._add_intent(
            participantId='', type='active-consequence', summary='已过期',
            payload={'lifecycle': 'active', 'expiresAt': iso(NOW - timedelta(minutes=1))},
        )
        live = self._add_intent(
            participantId='p1', type='active-consequence', summary='生效中',
            payload={'lifecycle': 'active', 'strength': 0.8, 'expiresAt': iso(NOW + timedelta(hours=1))},
        )
        other_branch = self._add_intent(
            participantId='p2', type='active-consequence', summary='别人的余波',
            payload={'lifecycle': 'active', 'strength': 0.9, 'expiresAt': iso(NOW + timedelta(hours=1))},
        )
        not_started = self._add_intent(
            participantId='p1', type='active-consequence', summary='还没开始',
            notBefore=NOW + timedelta(hours=1),
            payload={'lifecycle': 'active', 'expiresAt': iso(NOW + timedelta(hours=2))},
        )
        scheduled = self._add_intent(
            participantId='p1', type='follow-up', summary='排期计划', notBefore=NOW,
        )
        # 形状不完整的旧行（没有 lifecycle）不会被当成余波。
        legacy_shape = self._add_intent(
            participantId='p1', type='active-consequence', summary='旧形状',
            payload={'expiresAt': iso(NOW + timedelta(hours=1))},
        )

        async def scenario() -> tuple[list[Any], bool, bool]:
            service = self.service
            visible = await service.active_consequences_and_expire('s', NOW, 'p1')
            changed = await service.apply_intent_updates(
                's', [
                    {'id': live['id'], 'status': 'completed', 'resolution': '已经说开了'},
                    {'id': other_branch['id'], 'status': 'completed'},
                    {'id': scheduled['id'], 'status': 'completed'},
                    {'id': legacy_shape['id'], 'status': 'completed'},
                    {'id': 999_999, 'status': 'cancelled'},
                ],
                NOW, 'p1',
            )
            # 第二次调用：目标已经不是 pending，什么都不改。
            again = await service.apply_intent_updates(
                's', [{'id': live['id'], 'status': 'cancelled'}], NOW, 'p1',
            )
            return visible, changed, again

        visible, changed, again = asyncio.run(scenario())
        self.assertEqual([row['summary'] for row in visible], ['生效中'])
        self.assertTrue(changed)
        self.assertFalse(again)
        self.assertEqual(self.db.get('interlude_intent', {'id': expired['id']})['status'], 'completed')
        updated = self.db.get('interlude_intent', {'id': live['id']})
        self.assertEqual(updated['status'], 'completed')
        self.assertEqual(updated['payload']['resolution'], '已经说开了')
        # 隐私边界：别的分支的余波、普通排期计划、形状不完整的旧行都不能被碰到。
        self.assertEqual(self.db.get('interlude_intent', {'id': other_branch['id']})['status'], 'pending')
        self.assertEqual(self.db.get('interlude_intent', {'id': scheduled['id']})['status'], 'pending')
        self.assertEqual(self.db.get('interlude_intent', {'id': legacy_shape['id']})['status'], 'pending')
        # 尚未开始的余波既不算"生效中"，也不该被过期清理。
        self.assertEqual(self.db.get('interlude_intent', {'id': not_started['id']})['status'], 'pending')

    # ------------------------------------------------------------------ #
    # 意图调度视图
    # ------------------------------------------------------------------ #

    def test_due_intents_cancels_expired_proactive_checks_and_upcoming_hides_internal_types(self):
        if not _has_member('due_intents'):
            self.skipTest('chunk5 未组装')
        # 先插"晚到期"的、再插"早到期"的：断言必须证明是按时间而非按 id 排序。
        late_due = self._add_intent(
            participantId='p1', type='follow-up', summary='稍晚到期',
            notBefore=NOW - timedelta(seconds=3),
        )
        early_due = self._add_intent(
            participantId='p1', type='split-message', summary='第一段',
            notBefore=NOW - timedelta(seconds=5),
        )
        due_proactive = self._add_intent(
            participantId='p1', type='proactive-check', summary='过期的主动检查',
            notBefore=NOW - timedelta(seconds=1),
            payload={'expiresAt': iso(NOW - timedelta(seconds=1))},
        )
        due_consequence = self._add_intent(
            participantId='p1', type='active-consequence', summary='余波不是调度工作',
            notBefore=NOW - timedelta(seconds=1),
            payload={'lifecycle': 'active', 'expiresAt': iso(NOW + timedelta(hours=1))},
        )
        future_plan = self._add_intent(
            participantId='p1', type='follow-up-commitment', summary='稍后回访',
            notBefore=NOW + timedelta(minutes=5),
        )
        future_split = self._add_intent(
            participantId='p1', type='split-message', summary='下一段',
            notBefore=NOW + timedelta(minutes=1),
        )
        far_plans = [
            self._add_intent(
                participantId='p1', type='follow-up-commitment', summary='计划%d' % index,
                notBefore=NOW + timedelta(minutes=10 + index),
            )
            for index in range(10)
        ]

        async def scenario() -> tuple[list[Any], list[Any]]:
            service = self.service
            return (
                await service.due_intents('s', NOW),
                await service.upcoming_narrative_intents('s', NOW),
            )

        due, upcoming = asyncio.run(scenario())
        due_ids = [row['id'] for row in due]
        self.assertEqual(due_ids, [early_due['id'], late_due['id']], '按 notBefore 升序（上游 sort asc）')
        self.assertEqual(due[0]['notBefore'], NOW - timedelta(seconds=5))
        # chunk4 消费的是**库行**（camelCase 列名）。
        self.assertIn('participantId', due[0])
        self.assertNotIn(due_proactive['id'], due_ids, '过期的 proactive-check 必须先取消再排除')
        self.assertNotIn(due_consequence['id'], due_ids, '余波永远不是调度工作')
        self.assertEqual(self.db.get('interlude_intent', {'id': due_proactive['id']})['status'], 'cancelled')
        self.assertEqual(self.db.get('interlude_intent', {'id': due_consequence['id']})['status'], 'pending')

        upcoming_ids = [row['id'] for row in upcoming]
        self.assertEqual(len(upcoming), 8, '上游 `.slice(0, 8)`')
        self.assertEqual(upcoming_ids[0], future_plan['id'])
        self.assertEqual(upcoming_ids[1:], [row['id'] for row in far_plans[:7]])
        self.assertNotIn(future_split['id'], upcoming_ids, '内部意图类型不送给主叙事')
        self.assertNotIn(early_due['id'], upcoming_ids, '已到期的不算 upcoming')

    # ------------------------------------------------------------------ #
    # 事实与网页观察读取
    # ------------------------------------------------------------------ #

    def test_facts_visibility_lanes_and_web_observation_branch_switch(self):
        if not _has_member('facts'):
            self.skipTest('chunk5 未组装')

        def add_fact(scope: str, content: str, importance: float, participant_id: str,
                     unresolved: bool = False) -> dict[str, Any]:
            return self.db.insert('interlude_fact', {
                'storyId': 's', 'participantId': participant_id, 'scope': scope, 'content': content,
                'importance': importance, 'confidence': 0.5, 'unresolved': unresolved, 'status': 'active',
                'sourceEntryIds': [], 'lastSeenAt': NOW, 'createdAt': NOW, 'updatedAt': NOW,
            })

        low_private = add_fact('relationship', '关系内的普通细节', 0.1, 'p1')
        promise = add_fact('promise', '答应回电话', 0.05, 'p1', unresolved=True)
        self.db.insert('interlude_fact', {
            'storyId': 's', 'participantId': 'p1', 'scope': 'event', 'content': '最近已经了结的事件',
            'importance': 0.3, 'confidence': 0.5, 'unresolved': False, 'status': 'active',
            'sourceEntryIds': [], 'lastSeenAt': NOW, 'createdAt': NOW, 'updatedAt': NOW,
        })
        other_branch = add_fact('event', '别人的事', 0.99, 'p2')
        world_fact = add_fact('world', '世界级小事', 0.2, '')

        service = self._service(
            memory={
                'factLimit': 4, 'maxFactsPerStory': 200, 'factImportanceWeight': 1.0,
                'factConfidenceWeight': 1.0, 'factRecencyWeight': 1.0, 'semanticWeight': 1.0,
                'unresolvedWeight': 1.0,
            },
            browser={'enabled': True, 'maxObservationsInPrompt': 4},
        )
        self.service = service

        def add_observation(participant_id: str, status: str, url: str, index: int) -> None:
            self.db.insert('interlude_web_observation', {
                'storyId': 's', 'participantId': participant_id, 'intentId': None, 'mode': 'visit',
                'query': '', 'url': url, 'title': url, 'excerpt': 'e', 'summary': 's', 'status': status,
                'accessedAt': NOW + timedelta(seconds=index), 'createdAt': NOW,
            })

        add_observation('p1', 'success', 'https://example.com/1', 1)
        add_observation('p1', 'failed', 'https://example.com/2', 2)
        add_observation('p1', 'success', 'https://example.com/3', 3)
        add_observation('p2', 'success', 'https://example.com/other', 4)
        add_observation('', 'success', 'https://example.com/world', 5)

        async def scenario() -> dict[str, Any]:
            return {
                'private': await service.facts('s', 4, '', 'p1'),
                'all_branches': await service.facts('s', 10, '', None),
                'observations': await service.web_observations('s', 'p1'),
                'disabled': await self._service().web_observations('s', 'p1'),
            }

        result = asyncio.run(scenario())
        private_ids = [row['id'] for row in result['private']]
        # 通道保留：最近了结的事件与未结承诺各占一席（即使打分很低）。
        self.assertIn(promise['id'], private_ids)
        self.assertIn(low_private['id'], private_ids)
        self.assertIn(world_fact['id'], private_ids, '世界级事实对所有分支可见')
        self.assertNotIn(other_branch['id'], private_ids, '其它关系分支的事实不可见')
        self.assertEqual(len(result['private']), 4)
        self.assertIn(other_branch['id'], [row['id'] for row in result['all_branches']])
        # 浏览器默认关闭：连一次库读都不做。
        self.assertEqual(result['disabled'], [])
        # 开启后：只留成功观察；世界级观察对所有分支可见，别的私聊分支不可见；
        # 返回时按 accessedAt 升序（最新 N 条再 `reverse()`）。
        self.assertEqual(
            [row['url'] for row in result['observations']],
            ['https://example.com/1', 'https://example.com/3', 'https://example.com/world'],
        )

    def test_web_observation_reads_respect_status_limit_and_branch_switch(self):
        if not _has_member('web_observations'):
            self.skipTest('chunk5 未组装')

        def add_observation(participant_id: str, status: str, index: int) -> None:
            self.db.insert('interlude_web_observation', {
                'storyId': 's', 'participantId': participant_id, 'intentId': None, 'mode': 'visit',
                'query': '', 'url': 'https://example.com/%d' % index, 'title': 't%d' % index,
                'excerpt': 'e', 'summary': 's', 'status': status,
                'accessedAt': NOW + timedelta(seconds=index), 'createdAt': NOW,
            })

        for index in range(6):
            add_observation('p1', 'success', index)
        add_observation('p1', 'blocked', 10)
        add_observation('p1', 'failed', 11)
        add_observation('p2', 'success', 12)
        add_observation('', 'success', 13)

        service = self._service(browser={'enabled': True, 'maxObservationsInPrompt': 4})
        self.service = service

        async def scenario() -> tuple[list[Any], list[Any], list[Any]]:
            return (
                await service.web_observations('s', 'p1'),
                await service.web_observations('s', 'p2'),
                await service.web_observations('s', None),
            )

        p1, p2, world = asyncio.run(scenario())
        self.assertEqual(
            [row['url'] for row in p1],
            ['https://example.com/3', 'https://example.com/4', 'https://example.com/5',
             'https://example.com/13'],
            '取最近 4 条可用观察（世界级观察对所有分支可见）后按 accessedAt 升序',
        )
        self.assertEqual([row['url'] for row in p2],
                         ['https://example.com/12', 'https://example.com/13'])
        self.assertEqual([row['url'] for row in world], ['https://example.com/13'],
                         '无参与者的回合只看世界级观察')

    # ------------------------------------------------------------------ #
    # 网页浏览：目标校验、缓存、并发闸门
    # ------------------------------------------------------------------ #

    def test_browser_observation_public_policy_cache_and_no_persist(self):
        if not _has_member('collect_web_observation'):
            self.skipTest('chunk5 未组装')

        class RecordingTransport:
            def __init__(self) -> None:
                self.calls: list[tuple[str, Any]] = []

            async def search_web(self, query: str, timeout_ms: int) -> list[dict[str, Any]]:
                self.calls.append(('search', query))
                return [{
                    'url': 'https://example.com/search?q=%s' % query, 'title': '搜索标题', 'text': '正文' * 100,
                }]

            async def visit_web(self, url: str, timeout_ms: int) -> dict[str, Any]:
                self.calls.append(('visit', url))
                return {'url': url, 'title': '页面标题', 'text': '页面正文 ' * 100}

        service = self._service(browser={'enabled': True, 'cacheMinutes': 30})
        transport = RecordingTransport()
        service.transport = transport
        self.service = service

        async def scenario() -> dict[str, Any]:
            story = {'id': 's'}
            draft = {'mode': 'visit', 'url': 'https://example.com/x', 'purpose': 'p'}
            visit = await service.collect_web_observation(story, draft, 'p1', 11, NOW)
            entries_after_visit = self.db.count('interlude_script_entry', {'storyId': 's'})
            cached = await service.collect_web_observation(story, draft, 'p1', 12, NOW)
            entries_after_cached = self.db.count('interlude_script_entry', {'storyId': 's'})
            non_persisted = await service.collect_web_observation(story, draft, 'p1', 13, NOW, False)
            blocked = await service.collect_web_observation(
                story, {'mode': 'visit', 'url': 'http://127.0.0.1/x', 'purpose': 'p'}, 'p1', 14, NOW,
            )
            search = await service.collect_web_observation(
                story, {'mode': 'search', 'query': '猫', 'purpose': 'p'}, 'p1', 15, NOW,
            )
            await service.append_browser_intent(
                's', {'mode': 'visit', 'url': 'https://example.com/y', 'purpose': '去看一眼'}, NOW,
            )
            return {
                'visit': visit, 'cached': cached, 'non_persisted': non_persisted,
                'blocked': blocked, 'search': search,
                'entries_after_visit': entries_after_visit,
                'entries_after_cached': entries_after_cached,
            }

        result = asyncio.run(scenario())
        self.assertEqual(result['visit']['status'], 'success')
        self.assertEqual(result['visit']['title'], '页面标题')
        self.assertGreater(result['visit']['id'], 0)
        self.assertEqual(result['visit']['intentId'], 11)
        self.assertEqual(
            self.db.count('interlude_web_observation', {'storyId': 's', 'status': 'success'}), 2,
            '两次访问各有一次成功观察（第二次是同一条命中缓存后的搜索）',
        )
        # 缓存命中：复用同一条观察并补一条"重温"剧本条目。
        self.assertEqual(result['cached']['id'], result['visit']['id'])
        self.assertEqual(result['entries_after_cached'], result['entries_after_visit'] + 1)
        revisit = self.db.all('interlude_script_entry', {'storyId': 's', 'kind': 'web-observation'})
        self.assertIn('revisited', revisit[1]['content'])
        # persist=False：只留在内存里，主键归零。
        self.assertEqual(result['non_persisted']['id'], 0)
        self.assertEqual(result['non_persisted']['intentId'], 13)
        self.assertEqual(
            self.db.count('interlude_web_observation', {'storyId': 's', 'status': 'success'}), 2,
        )
        # 私网地址在发请求之前就被拦截。
        self.assertEqual(result['blocked']['status'], 'blocked')
        self.assertIn('安全校验', result['blocked']['summary'])
        self.assertNotIn(('visit', 'http://127.0.0.1/x'), transport.calls)
        self.assertEqual(result['search']['status'], 'success')
        self.assertIn(('search', '猫'), transport.calls)
        # `append_browser_intent` 落的是延迟的 browser-research 意图（`notBefore = now + 1s`）。
        pending = self.db.all('interlude_intent', {'storyId': 's', 'type': 'browser-research'})
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['payload']['purpose'], '去看一眼')
        self.assertEqual(pending[0]['summary'], '去看一眼')
        self.assertEqual(pending[0]['notBefore'], NOW + SECOND)

        # `allowSearch = false`：搜索意图走 blocked 分支，且不调用浏览通道。
        strict = self._service(browser={'enabled': True, 'allowSearch': False})
        strict_transport = RecordingTransport()
        strict.transport = strict_transport

        async def strict_scenario() -> dict[str, Any]:
            return await strict.collect_web_observation(
                {'id': 's2'}, {'mode': 'search', 'query': '猫', 'purpose': 'p'}, 'p1', 16, NOW,
            )

        blocked_search = asyncio.run(strict_scenario())
        self.assertEqual(blocked_search['status'], 'blocked')
        self.assertEqual(strict_transport.calls, [])

    def test_public_url_policy_matrix_and_browser_slot_serialization(self):
        if not _has_member('with_browser_slot'):
            self.skipTest('chunk5 未组装')
        config: dict[str, Any] = {'blockedDomains': [], 'allowedDomains': []}
        allowed = chunk5_module.is_safe_public_web_url
        self.assertTrue(allowed('https://example.com/a', config))
        self.assertTrue(allowed('http://example.com:8080/a?b=1', config))
        for blocked in (
            'http://localhost/x', 'https://127.0.0.1/x', 'https://10.0.0.5/x', 'https://0.0.0.0/x',
            'https://169.254.1.1/x', 'https://172.20.3.4/x', 'https://192.168.1.1/x',
            'https://[::1]/x', 'https://user:password@example.com/x', 'ftp://example.com/x',
            'example.com/a', 'https:///x',
        ):
            self.assertFalse(allowed(blocked, config), blocked)
        self.assertFalse(allowed('https://sub.example.com/x', {'blockedDomains': ['example.com']}))
        self.assertFalse(allowed('https://other.com/x', {'allowedDomains': ['example.com']}))
        self.assertTrue(allowed('https://example.com/x', {'allowedDomains': ['example.com']}))
        # 搜索模板必须含 `{query}`，且编码后的目标仍要过安全校验。
        self.assertIsNone(chunk5_module.resolve_browser_target(
            {'mode': 'search', 'query': '猫'}, {'searchUrlTemplate': 'https://example.com/search'},
        ))
        self.assertEqual(
            chunk5_module.resolve_browser_target(
                {'mode': 'search', 'query': '猫 和 狗'},
                {'searchUrlTemplate': 'https://example.com/s?q={query}'},
            ),
            'https://example.com/s?q=%E7%8C%AB%20%E5%92%8C%20%E7%8B%97',
        )
        self.assertIsNone(chunk5_module.resolve_browser_target(
            {'mode': 'visit', 'url': 'http://localhost/x'}, config,
        ))
        self.assertIsNone(chunk5_module.normalize_browser_intent_draft(
            {'mode': 'visit', 'purpose': 'p'}, config,
        ), 'visit 必须有 url')
        self.assertIsNone(chunk5_module.normalize_browser_intent_draft(
            {'mode': 'search', 'purpose': 'p'}, config,
        ), 'search 必须有 query')

        service = self._service(browser={'enabled': True, 'maxConcurrentPages': 1})
        self.service = service
        order: list[str] = []

        async def job(tag: str, delay: float) -> str:
            order.append('start-%s' % tag)
            await asyncio.sleep(delay)
            order.append('end-%s' % tag)
            return tag

        async def scenario() -> list[Any]:
            return await asyncio.gather(
                service.with_browser_slot(lambda: job('a', 0.05)),
                service.with_browser_slot(lambda: job('b', 0.01)),
            )

        self.assertEqual(asyncio.run(scenario()), ['a', 'b'])
        self.assertEqual(order, ['start-a', 'end-a', 'start-b', 'end-b'])
        self.assertEqual(service.browser_active, 0)
        self.assertEqual(service.browser_waiters, [])

    # ------------------------------------------------------------------ #
    # 重试与到期唤醒
    # ------------------------------------------------------------------ #

    def test_narrative_retry_replaces_previous_and_stops_at_cap(self):
        if not _has_member('schedule_narrative_retry'):
            self.skipTest('chunk5 未组装')
        self._add_intent(participantId='p1', type='narrative-retry', summary='旧重试',
                         notBefore=NOW + timedelta(seconds=20))

        async def scenario(previous: int, participant_id: str = 'p1') -> bool:
            return await self.service.schedule_narrative_retry('s', participant_id, NOW, previous)

        self.assertTrue(asyncio.run(scenario(0)))
        retries = self.db.all('interlude_intent', {'storyId': 's', 'type': 'narrative-retry'})
        self.assertEqual(len(retries), 2)
        self.assertEqual(sorted(row['status'] for row in retries), ['cancelled', 'pending'])
        fresh = [row for row in retries if row['status'] == 'pending'][0]
        self.assertEqual(fresh['payload'], {'narrativeRetry': True, 'userInitiated': True, 'attempt': 1})
        self.assertEqual(fresh['participantId'], 'p1')
        self.assertEqual(fresh['notBefore'], NOW + timedelta(seconds=60))
        self.assertIn('attempt 1/6', fresh['summary'])
        # 达到上限：已有的待重试被取消，且不再新增。
        self.assertFalse(asyncio.run(scenario(6)))
        self.assertEqual(self.db.count('interlude_intent', {'storyId': 's', 'type': 'narrative-retry'}), 2)
        self.assertEqual(
            self.db.count('interlude_intent', {'storyId': 's', 'type': 'narrative-retry', 'status': 'pending'}),
            0,
        )
        # 没有参与者就没有可重试的回合。
        self.assertFalse(asyncio.run(scenario(0, '')))
        # 延迟下限 5 秒（配置写 1 也按 5 算）。
        service = self._service(runtime={'narrativeRetryDelaySeconds': 1})
        self.service = service
        self.assertTrue(asyncio.run(scenario(0)))
        newest = [
            row for row in self.db.all('interlude_intent', {'storyId': 's', 'type': 'narrative-retry'})
            if row['status'] == 'pending'
        ][0]
        self.assertEqual(newest['notBefore'], NOW + timedelta(seconds=5))

    def test_due_intent_wake_keeps_earliest_and_delivers_split_before_sweep(self):
        if not _has_member('schedule_due_intent_wake'):
            self.skipTest('chunk5 未组装')
        service = self._service()
        service.has_pending_narrative = lambda story_id: False
        self.service = service
        counters = {'deliveries': 0, 'sweeps': 0}

        async def deliver_due_split_segments(story_id: str) -> None:
            counters['deliveries'] += 1

        async def sweep() -> None:
            counters['sweeps'] += 1

        service.deliver_due_split_segments = deliver_due_split_segments
        service.sweep = sweep

        async def scenario_ordering() -> None:
            service.schedule_due_intent_wake('s', NOW + timedelta(seconds=60))
            first = service.due_intent_wake_timers['s']
            # 更晚的唤醒不能覆盖更早的那次。
            service.schedule_due_intent_wake('s', NOW + timedelta(seconds=120))
            self.assertIs(service.due_intent_wake_timers['s'], first)
            # 更早的唤醒替换旧句柄（并取消它）。
            service.schedule_due_intent_wake('s', NOW + timedelta(seconds=10))
            replaced = service.due_intent_wake_timers['s']
            self.assertIsNot(replaced, first)
            self.assertEqual(replaced['due_at'], (NOW + timedelta(seconds=10)).timestamp() * 1000)
            self.assertTrue(callable(replaced['cancel']))
            self.assertTrue(callable(replaced.cancel), 'base.py 的暂停路径按属性调用 `.cancel`')
            service.due_intent_wake_timers.pop('s').cancel()

        asyncio.run(scenario_ordering())
        self.assertEqual(counters, {'deliveries': 0, 'sweeps': 0})

        # 到期的拆分段：先投递；若还有别的到期工作，再走一次 sweep。
        async def scenario_due() -> None:
            self._add_intent(participantId='p1', type='split-message', summary='第一段',
                             notBefore=NOW - timedelta(seconds=1))
            self._add_intent(participantId='p1', type='follow-up-commitment', summary='回访',
                             notBefore=NOW - timedelta(seconds=1))
            service.schedule_due_intent_wake('s', NOW - timedelta(seconds=1))
            await _wait_until(lambda: counters['deliveries'] == 1 and counters['sweeps'] == 1)

        asyncio.run(scenario_due())
        self.assertEqual(counters['deliveries'], 1)
        self.assertEqual(counters['sweeps'], 1)
        self.assertNotIn('s', service.due_intent_wake_timers)

        # 只有拆分段时：投递之后直接返回，不另起一次 sweep。
        counters['deliveries'] = 0
        counters['sweeps'] = 0
        self.db.update('interlude_intent', {'type': 'follow-up-commitment'}, {'status': 'completed'})

        async def scenario_only_split() -> None:
            due = await service.due_intents('s', NOW)
            self.assertTrue(due and all(row['type'] == 'split-message' for row in due))
            service.schedule_due_intent_wake('s', NOW - timedelta(seconds=1))
            await _wait_until(lambda: counters['deliveries'] == 1)

        asyncio.run(scenario_only_split())
        self.assertEqual(counters['deliveries'], 1)
        self.assertEqual(counters['sweeps'], 0)

        # `scheduleNextSplitWake` 只为最早的拆分段设一次唤醒。
        self.db.update('interlude_intent', {'type': 'split-message'}, {'status': 'pending'})
        self._add_intent(participantId='p1', type='split-message', summary='再晚一点',
                         notBefore=NOW + timedelta(minutes=30))

        async def scenario_next() -> Any:
            await service.schedule_next_split_wake('s')
            return service.due_intent_wake_timers['s']

        wake = asyncio.run(scenario_next())
        self.assertEqual(wake['due_at'], (NOW - timedelta(seconds=1)).timestamp() * 1000)

    # ------------------------------------------------------------------ #
    # Alter System
    # ------------------------------------------------------------------ #

    def test_alter_system_update_bypass_analysis_and_prompt_offset(self):
        if not _has_member('update_alter_system'):
            self.skipTest('chunk5 未组装')
        service = self._service(alterSystem={'enabled': True})
        self.service = service
        story = {'id': 's'}
        self.assertIsNone(service.update_alter_system(story, None, None, 'user-message', NOW))
        disabled = self._service(alterSystem={'enabled': False})
        self.assertIsNone(disabled.update_alter_system(story, None, 40, 'user-message', NOW))
        # `0` 是合法位移（只有 `undefined` 才跳过）。
        self.assertIsNotNone(service.update_alter_system(story, None, 0, 'user-message', NOW))
        turn = service.update_alter_system(story, None, 40, 'user-message', NOW, 'p1')
        self.assertEqual(turn['trigger_value'], 40)
        self.assertTrue(turn['threshold_reached'])
        self.assertEqual(turn['source_participant_id'], 'p1')
        self.assertGreater(turn['threshold'], 0)

        self.db.insert('interlude_story', {
            'id': 's', 'platform': 'onebot', 'selfId': 'bot', 'userId': '', 'channelId': '',
            'status': 'active', 'setting': {'character': {'name': '凌梦'}},
            'state': encode_story_state({}), 'cursorAt': NOW, 'createdAt': NOW, 'updatedAt': NOW,
        })
        self.db.insert('interlude_script_entry', {
            'storyId': 's', 'participantId': 'p1', 'kind': 'script', 'actor': 'narrator',
            'content': '她想起刚才那句话。', 'occurredAt': NOW, 'metadata': {}, 'createdAt': NOW,
        })
        calls: list[dict[str, Any]] = []

        class Narrator:
            async def analyze_alter(self, request: Any, config: Any) -> dict[str, Any]:
                calls.append(request)
                return {'description': '  她心里有点发紧。  '}

        service.narrator = Narrator()

        # 没有 alter 系统的状态：直接返回，不调用侧端模型。
        asyncio.run(service.analyze_alter_system('s', 'user-message', 'p1'))
        self.assertEqual(calls, [])

        # 写入达标状态后触发分析。
        state = service.update_alter_system({'id': 's'}, None, 40, 'user-message', NOW, 'p1')['state']
        story_row = self.db.get('interlude_story', {'id': 's'})
        self.db.update('interlude_story', {'id': 's'}, {
            'state': encode_story_state({**decode_story_state(story_row['state']), 'alter_system': state}),
        })
        asyncio.run(service.analyze_alter_system('s', 'user-message', 'p1'))
        self.assertEqual(len(calls), 1)
        request = calls[0]
        self.assertEqual(sorted(request), [
            'character_name', 'current_offset', 'direction', 'history', 'recent_scripts',
            'setting_overlay', 'threshold', 'trigger_value',
        ])
        self.assertEqual(request['direction'], 'serious')
        self.assertEqual(request['trigger_value'], 40)
        self.assertEqual(request['character_name'], '凌梦')
        self.assertEqual(request['recent_scripts'][0]['content'], '她想起刚才那句话。')
        self.assertIsNone(request['current_offset'])
        # 描述被 trim 后写进情绪偏移，权重复位为 1。
        offset = service.emotional_offset_for_prompt(self.db.get('interlude_story', {'id': 's'}))
        self.assertEqual(offset['description'], '她心里有点发紧。')
        self.assertEqual(offset['direction'], 'serious')
        self.assertEqual(offset['weight'], 1.0)
        # 冷却：立刻再分析一次不会重复调用侧端模型。
        asyncio.run(service.analyze_alter_system('s', 'user-message', 'p1'))
        self.assertEqual(len(calls), 1)
        # 关闭 Alter 时 prompt 不再带偏移。
        self.assertEqual(
            self._service(alterSystem={'enabled': False}).emotional_offset_for_prompt(
                self.db.get('interlude_story', {'id': 's'}),
            ),
            None,
        )

        # 后台排队的去重键与清理（上游 `scheduledAlterAnalyses`）。
        service.analyze_alter_system = _noop_async

        async def scenario() -> tuple[set[str], set[str]]:
            service.schedule_alter_analysis('s', 'advance', 'p1')
            service.schedule_alter_analysis('s', 'advance', 'p1')
            scheduled = set(service.scheduled_alter_analyses)
            await _wait_until(lambda: not service.scheduled_alter_analyses)
            return scheduled, set(service.scheduled_alter_analyses)

        scheduled, after = asyncio.run(scenario())
        self.assertEqual(scheduled, {'s\u0000p1'})
        self.assertEqual(after, set())

    # ------------------------------------------------------------------ #
    # 日程预排管理入口
    # ------------------------------------------------------------------ #

    def test_schedule_preplan_admin_entries(self):
        if not _has_member('admin_schedule_preplan'):
            self.skipTest('chunk5 未组装')
        service = self.service

        async def no_preplan(story_id: str) -> Any:
            return None

        async def has_preplan(story_id: str) -> Any:
            return {'storyId': story_id, 'revision': 1}

        service.get_schedule_preplan = no_preplan
        self.assertIsNone(asyncio.run(service.admin_schedule_preplan('s')))
        self.assertFalse(asyncio.run(service.request_schedule_preplan_rebuild('s')))

        service.get_schedule_preplan = has_preplan
        compactions: list[str] = []
        service.schedule_compaction = lambda story_id: compactions.append(story_id)
        self.db.insert('interlude_schedule_preplan', {
            'storyId': 's', 'revision': 1, 'timezone': 'Asia/Shanghai', 'validFrom': '2026-09-07',
            'validThrough': '2026-10-07', 'lastReviewedLocalDate': '2026-09-07',
            'lastEvidenceEntryId': 3, 'reviewReason': '', 'regimes': {}, 'exceptions': [],
            'materializedDays': 3, 'createdAt': NOW, 'updatedAt': NOW,
        })
        self.assertTrue(asyncio.run(service.request_schedule_preplan_rebuild('s')))
        self.assertEqual(compactions, ['s'])
        row = self.db.get('interlude_schedule_preplan', {'storyId': 's'})
        self.assertEqual(row['lastReviewedLocalDate'], '')
        self.assertEqual(row['validThrough'], '1970-01-01')
        self.assertEqual(row['reviewReason'], 'Administrator requested a rebuild.')
        self.assertEqual(row['updatedAt'], NOW)


if __name__ == '__main__':  # pragma: no cover
    unittest.main(verbosity=2)
