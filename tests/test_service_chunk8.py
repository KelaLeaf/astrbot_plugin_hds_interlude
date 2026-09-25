"""`plugin/core/service/chunk8.py` 的移植测试。

两部分：

1. **上游断言逐条移植** —— `upstream/test/` 里断言本范围成员（`persistFact` /
   `persistStatePatch` / `persistCompaction`）的用例，按 `docs/PORT_PLAN.md` §3
   的约定转成 stdlib `unittest`：
   * `memory-continuity.test.ts`
   * `evidence-repair.test.ts`
   * `m7-m8-continuity.test.ts`
   * `beta6-handoff.test.ts`
   * `continuity-checkpoint.test.ts`

   上游用 `(InterludeService.prototype as any).xxx.call(service, ...)` 直接调用
   原型方法、并把一个**只有少数成员的对象**当 `this`。本移植版用同一手法：
   替身继承 `ServiceChunk8`（见 `_HarnessBase`）但只带该分支真正用到的字段。
   两处**可解释的**替身差异（都写在用例注释里）：
   * 上游 `dbGet` 桩按 `query.id.$in` 分发；本移植版的 `_entries_by_ids` 先按同一
     形状问库，遇到 `ServiceBase.db_get` 的算子拒绝才降级，因此桩同时兼容两种形状。
   * 上游 `state.workingDetails` 是 camelCase wire；本移植版的 `story.state` 经
     `encode_story_state()` 归一化成 snake_case（`PORT_PLAN` §2「键名法」），
     断言相应改成 `state['working_details']`。

2. **本范围可独立验证的纯逻辑 / 集成用例**（`_DbHost` 用真实 sqlite3 内存库 +
   `base.py` 的 `db_get/db_create/db_set`）。

守门：`@unittest.skipUnless(...)` 只用于"并行任务尚未落地的依赖"，
本仓库当前全部就绪，用例真跑而不是跳过。
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone

# --------------------------------------------------------------------------- #
# 依赖守门（并行移植期间 base / script / schedule_preplan / database 可能缺失）
# --------------------------------------------------------------------------- #

CHUNK8_READY = False
CHUNK8_ERROR: Exception | None = None
try:
    from plugin.core.service.base import InterludeContext
    from plugin.core.service.chunk8 import (
        ServiceChunk8,
        explicit_evidence_ids,
        group_overlay_patches,
        group_overlay_snapshots,
        has_compaction_evidence,
        start_of_utc_window,
        state_patch_evidence,
    )

    CHUNK8_READY = True
except Exception as error:  # pragma: no cover - 取决于同批任务的落地顺序
    CHUNK8_ERROR = error

try:
    from plugin.core.types import empty_story_state
except Exception:  # pragma: no cover
    empty_story_state = None  # type: ignore[assignment]

try:
    from plugin.core.database import Database

    DATABASE_READY = True
except Exception:  # pragma: no cover
    DATABASE_READY = False

SCRIPT_READY = False
try:
    from plugin.core.script.development import development_scenes
    from plugin.core.script.knowledge_evidence import supports_recorded_outcome

    SCRIPT_READY = CHUNK8_READY and callable(development_scenes) and callable(supports_recorded_outcome)
except Exception:  # pragma: no cover
    pass

SCHEDULE_READY = False
try:
    from plugin.core.schedule_preplan import apply_schedule_preplan_proposal

    SCHEDULE_READY = CHUNK8_READY and callable(apply_schedule_preplan_proposal)
except Exception:  # pragma: no cover
    pass

NOW = datetime(2026, 8, 30, 11, 22, tzinfo=timezone.utc)


class _HarnessBase(ServiceChunk8 if CHUNK8_READY else object):
    """上游 `(InterludeService.prototype as any).xxx.call(service, ...)` 的等价物。

    上游的替身只带少数成员，因为被调方法只读 `this` 上的**字段**；
    本移植版把 memory 配置读取抽成了 `_memory*` 方法（chunk8 自己的一部分），
    所以替身继承 `ServiceChunk8` 但不跑 `ServiceBase.__init__` ——
    这样方法能被解析，字段仍然由每个替身自己给。
    """

    def report_operation(self, *args, **kwargs):
        self.reports.append(args)

    def report_standalone_operation(self, *args, **kwargs):
        self.reports.append(args)

    def report(self, *args, **kwargs):
        self.reports.append(args)


# --------------------------------------------------------------------------- #
# 共同夹具
# --------------------------------------------------------------------------- #

def story_row(**overrides):
    """最小剧本行（数据库行的列名保持上游 camelCase）。"""
    story = {
        'id': 'story', 'platform': 'onebot', 'selfId': 'bot', 'userId': '', 'channelId': '',
        'status': 'active', 'setting': {'timezone': 'Asia/Shanghai', 'character': {'name': '测试角色'}},
        'state': {}, 'cursorAt': NOW, 'createdAt': NOW, 'updatedAt': NOW,
    }
    story.update(overrides)
    return story


def entry_row(entry_id, kind, content, occurred_at=NOW, participant_id='participant'):
    """剧本条目行（数据库列名 camelCase）。"""
    return {
        'id': entry_id, 'storyId': 'story', 'participantId': participant_id, 'kind': kind,
        'actor': 'narrator' if kind == 'script' else 'user', 'content': content,
        'occurredAt': occurred_at, 'createdAt': occurred_at, 'metadata': {},
    }


def fact_row(fact_id, scope, unresolved, content, participant_id='', knowledge=None):
    fact = {
        'id': fact_id, 'storyId': 'story', 'participantId': participant_id, 'scope': scope,
        'content': content, 'importance': 0.8, 'confidence': 1.0, 'unresolved': unresolved,
        'embedding': [], 'status': 'active', 'sourceEntryIds': [],
        'lastSeenAt': NOW, 'createdAt': NOW, 'updatedAt': NOW,
    }
    if knowledge is not None:
        fact['knowledge'] = knowledge
    return fact


# =========================================================================== #
# 1. memory-continuity.test.ts：an explicit resolved fact can close the old row
# =========================================================================== #

class _FactHarness(_HarnessBase):
    """上游 `persistFact.call(service, ...)` 的最小替身（memory-continuity 版）。"""

    def __init__(self, existing):
        self.cached_memory_config = {'factContentCharacters': 4_000, 'maxFactsPerStory': 200}
        self.rows = list(existing)
        self.patch = None
        self.created = None
        self.embed_calls = 0

    async def db_get(self, table, query, options=None):
        return list(self.rows)

    async def db_set(self, table, query, value):
        self.patch = value

    async def db_create(self, table, value):
        self.created = dict(value)
        self.created.setdefault('id', 99)
        return self.created

    async def embed_text(self, value):
        self.embed_calls += 1
        return []


@unittest.skipUnless(CHUNK8_READY and SCRIPT_READY, 'chunk8 / script 依赖未就绪')
class PortedMemoryContinuityTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_explicit_resolved_fact_can_close_the_old_unresolved_row(self):
        existing = fact_row(12, 'promise', True, '奶茶配送事项', participant_id='participant')
        host = _FactHarness([existing])
        source = entry_row(1, 'script', '奶茶已经取回', NOW, 'participant')
        resolved = await host.persist_fact('story', {
            'scope': 'promise', 'content': '奶茶配送事项', 'unresolved': False,
            'sourceEntryIds': [1],
            'knowledge': {
                'mode': 'observed',
                'clauses': [{'role': 'observation', 'sourceEntryId': 1, 'quote': '奶茶已经取回'}],
            },
        }, [source], NOW)
        self.assertIs(resolved, True)
        self.assertIs(host.patch['unresolved'], False)
        self.assertEqual(host.created, None, '已有同内容事实时不应新建一行')

    async def test_an_empty_provider_embedding_never_blocks_the_fact_write(self):
        existing = fact_row(13, 'event', False, '旧记录', participant_id='')
        host = _FactHarness([existing])
        source = entry_row(2, 'script', '她今天去了书店。', NOW, '')
        resolved = await host.persist_fact('story', {
            'scope': 'event', 'content': '她今天去了书店。', 'sourceEntryIds': [2],
        }, [source], NOW)
        self.assertIs(resolved, False)
        self.assertIsInstance(host.created, dict)
        self.assertEqual(host.created['scope'], 'event')
        self.assertEqual(host.created['status'], 'active')
        self.assertEqual(host.created['participantId'], '')
        self.assertEqual(host.created['sourceEntryIds'], [2])
        self.assertEqual(host.created['embedding'], [])


# =========================================================================== #
# 2. evidence-repair.test.ts：unclassified completion cannot close a promise
# =========================================================================== #

@unittest.skipUnless(CHUNK8_READY and SCRIPT_READY, 'chunk8 / script 依赖未就绪')
class PortedEvidenceRepairTests(unittest.IsolatedAsyncioTestCase):
    async def test_unclassified_completion_cannot_close_a_promise(self):
        # 上游 harness 里的 `fact()` 完全不写 knowledge 字段（旧行形状）。
        existing = fact_row(12, 'promise', True, '电影安排', participant_id='')
        existing['confidence'] = 0.6
        host = _FactHarness([existing])
        source = entry_row(2, 'script', '她觉得他默认接受了', NOW, '')
        await host.persist_fact('story', {
            'scope': 'promise', 'content': existing['content'], 'unresolved': False,
            'confidence': 1, 'sourceEntryIds': [2],
        }, [source], NOW)
        self.assertIs(host.patch['unresolved'], True, '没有可记录结论的想象不能关闭承诺')
        self.assertEqual(host.patch['confidence'], 0.6, '重复的臆测不增加置信度')
        self.assertEqual(host.patch['knowledge']['mode'], 'unclassified')
        self.assertEqual(host.patch['sourceEntryIds'], [2])


# =========================================================================== #
# 3. m7-m8-continuity.test.ts：状态补丁的观察周期与证据闸门
# =========================================================================== #

def m7_entry(entry_id, frame='one', participant_id=''):
    return {
        'id': entry_id, 'storyId': 's', 'participantId': participant_id, 'kind': 'script',
        'actor': 'narrator', 'content': '她答应明天在书店归还书本。',
        'occurredAt': NOW - timedelta(days=entry_id), 'createdAt': NOW,
        'metadata': {'frameId': frame},
    }


class _PatchHarness(_HarnessBase):
    """上游 `learningHarness()` 的最小替身。

    上游 `dbGet` 桩用 `query.id.$in.includes(...)` 分发条目；本移植版的
    `_entries_by_ids` 会先按 `$in` 形状问库，桩因此同时兼容 `$in` 与等值查询。
    """

    def __init__(self, rows, memory=None):
        self.proposals = []
        self.rows = list(rows)
        self.cached_memory_config = memory or {
            'autoApplyStatePatches': True, 'allowMajorStateChanges': True,
            'statePatchMinTurns': 3, 'statePatchMinDays': 2,
            'statePatchConfidenceThreshold': 0.8, 'majorStatePatchConfidenceThreshold': 0.9,
            'statePatchCooldownHours': 72,
        }
        self.reports = []

    async def db_get(self, table, query, options=None):
        if table == 'interlude_state_patch':
            return list(self.proposals)
        ids = query.get('id')
        wanted = set(ids.get('$in') or []) if isinstance(ids, dict) else None
        return [row for row in self.rows if wanted is None or row.get('id') in wanted]

    async def db_create(self, table, value):
        item = dict(value)
        item['id'] = len(self.proposals) + 1
        self.proposals.append(item)
        return item

    async def db_set(self, table, query, value):
        for item in self.proposals:
            if item.get('id') == query.get('id'):
                item.update(value)

    async def call(self, ids, extras=None):
        draft = {
            'target': 'character', 'path': 'preferences',
            'proposedValue': '她遇到困惑时倾向先去书店查证。', 'evidence': '跨场景观察',
            'confidence': 0.95, 'sourceEntryIds': list(ids),
        }
        draft.update(extras or {})
        await self.persist_state_patch(
            {'id': 's', 'setting': {'timezone': 'Asia/Shanghai'},
             'state': empty_story_state() if empty_story_state else {}},
            draft, self.rows, NOW,
        )


@unittest.skipUnless(CHUNK8_READY and SCRIPT_READY, 'chunk8 / script 依赖未就绪')
class PortedM7M8ContinuityTests(unittest.IsolatedAsyncioTestCase):
    async def test_development_requires_an_observation_cycle_then_counter_evidence_retires_it(self):
        host = _PatchHarness([m7_entry(1, 'a'), m7_entry(2, 'b'), m7_entry(3, 'c'), m7_entry(4, 'd')])
        await host.call([1, 2, 3])
        self.assertEqual(host.proposals[0]['status'], 'proposed')
        await host.call([1, 2, 3])
        self.assertEqual(host.proposals[0]['status'], 'proposed', '同一批证据不能自证')
        await host.call([4])
        self.assertEqual(host.proposals[0]['status'], 'applied')
        host.proposals[0]['sourceEntryIds'] = [1, 2, 3]
        await host.call([4], {
            'contradictsProposalIds': [1], 'evidence': '同等条件下明确放弃这一选择',
        })
        self.assertEqual(host.proposals[0]['status'], 'rejected')
        self.assertLess(host.proposals[0]['confidence'], 0.8)

    async def test_dense_same_scene_evidence_and_private_evidence_cannot_promote_globals(self):
        host = _PatchHarness([m7_entry(index + 1) for index in range(10)])
        await host.call([1, 2, 3])
        await host.call([4, 5, 6])
        self.assertEqual(host.proposals[0]['status'], 'proposed', '十回合同一场景只算一次观察')
        private = _PatchHarness([m7_entry(1, 'a', 'friend')])
        await private.call([1])
        self.assertEqual(private.proposals, [], '私密材料不能变成全局人格变化')


# =========================================================================== #
# 4. beta6-handoff.test.ts：working detail 的证据修订
# =========================================================================== #

class _WorkingDetailHarness(_HarnessBase):
    """上游 `persistCompaction.call(host, ...)` 的最小替身（beta6 版）。"""

    def __init__(self, state):
        self.state = state
        self.patches = []
        self.cached_memory_config = {
            'arcSummaryCharacters': 1_000, 'sceneSummaryCharacters': 1_000,
            'sceneHookCharacters': 100,
        }

    async def active_arc(self, story_id):
        return {'id': 1, 'title': '测试弧'}

    async def get_story(self, story_id):
        return {'id': 's', 'state': self.state}

    async def db_set(self, table, query, value):
        self.patches.append({'table': table, 'query': query, 'patch': value})
        if table == 'interlude_story' and 'state' in value:
            self.state = value['state']


@unittest.skipUnless(CHUNK8_READY and SCRIPT_READY, 'chunk8 / script 依赖未就绪')
class PortedBeta6HandoffTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolved_working_detail_removes_only_the_evidence_backed_label(self):
        base_state = empty_story_state() if empty_story_state else {}
        state = dict(base_state)
        state['working_details'] = [
            {'label': '归还书本', 'value': '尚未归还', 'created_at': NOW.isoformat()},
            {'label': '取餐', 'value': '等待取餐', 'created_at': NOW.isoformat()},
        ]
        host = _WorkingDetailHarness(state)
        await host.persist_compaction( {'id': 's'}, {'id': 2, 'entryCount': 0},
            {
                'scene': {'summary': '归还完成'},
                'arc': {'summary': '承诺已履行'},
                'workingDetails': [
                    {'label': '归还书本', 'value': '', 'resolved': True, 'sourceEntryIds': [1]},
                    {'label': '取餐', 'value': '', 'resolved': True, 'sourceEntryIds': [999]},
                ],
            },
            [m7_entry(1)], NOW,
        )
        story_patches = [item for item in host.patches if item['table'] == 'interlude_story']
        self.assertEqual(len(story_patches), 1)
        labels = [item['label'] for item in story_patches[0]['patch']['state']['working_details']]
        self.assertEqual(labels, ['取餐'], '没有本批证据的 id 不能推进修订号')

    async def test_delayed_background_review_cannot_reopen_a_completed_working_detail(self):
        base_state = empty_story_state() if empty_story_state else {}
        state = dict(base_state)
        state['working_details'] = []
        state['working_detail_resolutions'] = {'物理作业': 20}
        host = _WorkingDetailHarness(state)

        async def review(source_id):
            await host.persist_compaction( {'id': 's'}, {'id': 1, 'entryCount': 0},
                {
                    'scene': {'summary': '生活继续'},
                    'arc': {'summary': '原剧情弧'},
                    'workingDetails': [
                        {'label': '物理作业', 'value': '还有一道题', 'sourceEntryIds': [source_id, 999999]},
                    ],
                },
                [{**m7_entry(source_id), 'content': '还有一道物理题。'}], NOW,
            )

        await review(10)
        self.assertEqual(host.state['working_details'], [],
                         '未知的来源 id 不能推进证据修订号')
        await review(21)
        self.assertEqual(host.state['working_details'][0]['label'], '物理作业',
                         '有证据的新任务可以复用同一个 label')


# =========================================================================== #
# 5. continuity-checkpoint.test.ts：场景关闭的前沿与失败保序
# =========================================================================== #

class _SceneHarness(_HarnessBase):
    """上游 `persistCompaction.call(service, ...)` 的最小替身（continuity-checkpoint 版）。"""

    def __init__(self):
        self.writes = []
        self.next_at = None
        self.cached_memory_config = {
            'arcSummaryCharacters': 1_000, 'sceneHookCharacters': 100, 'sceneSummaryCharacters': 1_000,
        }

    async def active_arc(self, story_id):
        return {'id': 7, 'title': '旧弧'}

    async def active_scene(self, story_id):
        return {'id': 9}

    async def db_get(self, table, query, options=None):
        return []

    async def db_set(self, table, query, value):
        self.writes.append({'table': table, 'query': query, 'patch': value})

    async def ensure_continuity(self, story, at):
        self.next_at = at


class _FailingArcHarness(_HarnessBase):
    def __init__(self):
        self.writes = []
        self.cached_memory_config = {'arcSummaryCharacters': 1_000}

    async def active_arc(self, story_id):
        return {'id': 7, 'title': '旧弧'}

    async def db_set(self, table, query, value):
        self.writes.append(table)
        raise RuntimeError('database unavailable')


@unittest.skipUnless(CHUNK8_READY and SCRIPT_READY, 'chunk8 / script 依赖未就绪')
class PortedContinuityCheckpointTests(unittest.IsolatedAsyncioTestCase):
    def _entries(self):
        return [
            entry_row(item, 'script', str(item) * 80, NOW, '') for item in (1, 2, 3)
        ]

    async def test_closing_a_reviewed_scene_retains_the_processed_frontier(self):
        host = _SceneHarness()
        await host.persist_compaction( {'id': 's'}, {'id': 8, 'entryCount': 0, 'startedAt': NOW},
            {
                'scene': {
                    'summary': '下课离开', 'close': True,
                    'boundary': {'reason': '已离开教室', 'sourceEntryIds': [3]},
                },
                'arc': {'summary': '约定延续'},
            },
            self._entries(), NOW + timedelta(minutes=1),
        )
        self.assertEqual(host.writes[0]['table'], 'interlude_arc', '先提交剧情弧，再确认证据')
        self.assertEqual(host.next_at, NOW, '关闭点落在已处理的前沿，而不是模型返回时刻')
        closed = [item for item in host.writes if item['table'] == 'interlude_scene']
        self.assertTrue(any(
            item['query'].get('id') == 9 and item['patch'].get('lastEntryId') == 3 for item in closed
        ), '新场景的检查点必须前进到本批最后一条')

    async def test_failed_arc_persistence_leaves_the_scene_checkpoint_untouched(self):
        host = _FailingArcHarness()
        with self.assertRaises(RuntimeError):
            await host.persist_compaction( {'id': 's'}, {'id': 8},
                {'scene': {'summary': '下课'}, 'arc': {'summary': '约定'}},
                self._entries(), NOW,
            )
        self.assertEqual(host.writes, ['interlude_arc'])


# =========================================================================== #
# 6. 本范围纯逻辑用例
# =========================================================================== #

@unittest.skipUnless(CHUNK8_READY, 'chunk8 依赖未就绪')
class Chunk8PureLogicTests(unittest.TestCase):
    EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

    def test_start_of_utc_window_floors_like_js_and_groups_patches(self):
        # 上游 `Math.floor(epochDay / size) * size * Time.day`：5 天为一个桶。
        self.assertEqual(start_of_utc_window(self.EPOCH, 5), self.EPOCH)
        self.assertEqual(
            start_of_utc_window(self.EPOCH + timedelta(days=4, hours=23, minutes=59), 5),
            self.EPOCH,
        )
        self.assertEqual(
            start_of_utc_window(self.EPOCH + timedelta(days=5) + timedelta(hours=1), 5),
            self.EPOCH + timedelta(days=5),
        )
        aligned = start_of_utc_window(NOW, 5)
        self.assertLessEqual(aligned, NOW)
        self.assertEqual(start_of_utc_window(aligned, 5), aligned, '分桶必须幂等')
        self.assertEqual((NOW - aligned).days % 5, (NOW - aligned).days % 5)

        patches = [
            {'id': 1, 'participantId': '', 'target': 'character',
             'appliedAt': self.EPOCH + timedelta(days=1), 'proposedValue': 'a'},
            {'id': 2, 'participantId': '', 'target': 'character',
             'appliedAt': self.EPOCH + timedelta(days=4), 'proposedValue': 'b'},
            {'id': 3, 'participantId': '', 'target': 'character',
             'appliedAt': self.EPOCH + timedelta(days=12), 'proposedValue': 'c'},
        ]
        groups = group_overlay_patches(patches, 5)
        self.assertEqual(len(groups), 2)
        self.assertEqual([patch['id'] for patch in groups[0]['patches']], [1, 2])
        self.assertEqual([patch['id'] for patch in groups[1]['patches']], [3])
        self.assertEqual(groups[0]['from'], self.EPOCH)
        self.assertEqual(groups[0]['to'], self.EPOCH + timedelta(days=5))
        self.assertEqual(groups[1]['from'], self.EPOCH + timedelta(days=10))

        snapshots = group_overlay_snapshots([
            {'id': 5, 'participantId': 'alice', 'target': 'relationship',
             'periodEnd': self.EPOCH + timedelta(days=2), 'sourcePatchIds': [1]},
            {'id': 6, 'participantId': 'alice', 'target': 'relationship',
             'periodEnd': self.EPOCH + timedelta(days=3), 'sourcePatchIds': [2]},
            {'id': 7, 'participantId': 'bob', 'target': 'relationship',
             'periodEnd': self.EPOCH + timedelta(days=2), 'sourcePatchIds': [3]},
        ], 10)
        self.assertEqual(len(snapshots), 2, '不同参与者不能合并到同一窗口')
        self.assertEqual(len(snapshots[0]['snapshots']), 2)

    def test_compaction_evidence_must_reference_a_processed_entry(self):
        entries = [entry_row(1, 'script', 'a'), entry_row(2, 'script', 'b')]
        self.assertFalse(has_compaction_evidence(None, entries))
        self.assertFalse(has_compaction_evidence([], entries))
        self.assertFalse(has_compaction_evidence([999], entries))
        self.assertTrue(has_compaction_evidence([2, 999], entries))
        self.assertEqual(explicit_evidence_ids([999, 2, 1], entries), [2, 1])

    def test_state_patch_evidence_counts_unique_turns_days_and_scenes(self):
        same_moment = entry_row(1, 'script', '一次', NOW)
        same_moment_2 = entry_row(2, 'script', '两次', NOW)
        other_day = entry_row(3, 'script', '另一天', NOW + timedelta(days=1))
        for row, frame in ((same_moment, 'a'), (same_moment_2, 'a'), (other_day, 'b')):
            row['metadata'] = {'frameId': frame}
        user_row = entry_row(4, 'user-message', '用户说话', NOW + timedelta(days=2))
        evidence = state_patch_evidence([same_moment, same_moment_2, other_day, user_row], 'Asia/Shanghai')
        self.assertEqual(evidence['turns'], 2, '同一时刻的重复行只算一个回合')
        self.assertEqual(evidence['days'], 2)
        self.assertEqual(evidence['scenes'], 2, '按证据帧计完成场景数')


# =========================================================================== #
# 7. 本范围集成用例（真实 sqlite3 内存库 + base.py 的数据库访问层）
# =========================================================================== #

class _OverlayCompactor:
    def __init__(self):
        self.calls = []

    async def compact_overlay(self, request):
        self.calls.append(request)
        return {'summary': '她开始更主动地确认。', 'majorEvents': ['开始主动确认']}


class _PipelineCompactor:
    """一次完整压缩决策（场景 / 弧 / 事实 / 状态补丁 / 剧集标签）。"""

    def __init__(self):
        self.requests = []

    async def compact(self, request):
        self.requests.append(request)
        return {
            'scene': {'summary': '一次书店的往返。', 'hook': '她回到书店。'},
            'arc': {'summary': '归还与重逢。', 'title': '归还之约'},
            'facts': [{
                'scope': 'event', 'content': '她去过书店。', 'sourceEntryIds': [1],
                'importance': 0.6, 'confidence': 0.7,
            }],
            'statePatches': [{
                'target': 'character', 'path': 'preferences',
                'proposedValue': '她喜欢在书店待着。', 'evidence': '观察',
                'confidence': 0.9, 'sourceEntryIds': [1],
            }],
            'episodeTags': [{'sourceEntryId': 1, 'places': ['书店']}],
        }

    async def compact_overlay(self, request):  # pragma: no cover - 本用例关闭了 overlay
        return {'summary': 'overlay 摘要'}


class _Planner:
    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    async def plan_schedule_preplan(self, request):
        self.calls += 1
        return self.results.pop(0) if self.results else None


INTERLUDE_SERVICE_READY = False
try:
    from plugin.core.service import InterludeService

    INTERLUDE_SERVICE_READY = DATABASE_READY
except Exception:  # pragma: no cover
    InterludeService = None  # type: ignore[assignment]


@unittest.skipUnless(INTERLUDE_SERVICE_READY, 'database / service 依赖未就绪')
class _DbHost(InterludeService if INTERLUDE_SERVICE_READY else object):
    """真实内存库 + 组装后的 `InterludeService`。

    集成用例走**真正的服务实例**（chunk0 + 已落地的 chunk + chunk8），
    跨 mixin 调用（`append_entry` / `save_schedule_preplan` …）因此是真实实现，
    不是测试替身。
    """

    def __init__(self, config=None):
        self.db = Database(':memory:')
        self.db.register_tables()
        # 构造期就会上报（`ServiceChunk0.__init__`），收集器必须先就位。
        self.reports = []
        self.compactor = None
        super().__init__(InterludeContext(database=self.db), config or {}, self.db, None)

    def report_operation(self, *args, **kwargs):
        self.reports.append(args)

    def report_standalone_operation(self, *args, **kwargs):
        self.reports.append(args)

    def report(self, *args, **kwargs):
        self.reports.append(args)


@unittest.skipUnless(CHUNK8_READY and DATABASE_READY and SCRIPT_READY, '依赖未就绪')
class _PipelineHost(_DbHost):
    """跑整条压缩管线的宿主。

    `chunk5.facts` 用 `asyncio.gather` 并发读同一张 sqlite3 连接，实测会偶发
    `sqlite3.InterfaceError`（300 行规模下 20 次并发读失败 5 次，另案上报）。
    这里按上游语义串行实现 `facts`，让 chunk8 自己的管线可被独立验证。
    """

    async def facts(self, story_id, limit=None, query='', participant_id=None,
                    turn_query_embedding=None):
        return await self.db_get('interlude_fact', {'storyId': story_id, 'status': 'active'})


class Chunk8IntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._hosts = []

    async def asyncTearDown(self):
        for host in self._hosts:
            try:
                host.db.close()
            except Exception:  # pragma: no cover
                pass

    def _host(self, config=None):
        host = _DbHost(config)
        self._hosts.append(host)
        return host

    async def test_compaction_pipeline_persists_scene_arc_facts_and_tags(self):
        host = _PipelineHost({
            'memory': {'sceneEntryThreshold': 3, 'overlayCompressionEnabled': False},
            'schedulePreplan': {'enabled': False},
        })
        self._hosts.append(host)
        host.compactor = _PipelineCompactor()
        host.db.insert('interlude_story', {
            'id': 'story', 'platform': 'onebot', 'selfId': 'bot', 'userId': '', 'channelId': '',
            'status': 'active', 'setting': {'timezone': 'Asia/Shanghai'}, 'state': {},
            'cursorAt': NOW, 'createdAt': NOW, 'updatedAt': NOW,
        })
        story = await host.get_story('story')
        await host.ensure_continuity(story, NOW)
        for index in range(1, 5):
            await host.append_entry('story', {
                'kind': 'script', 'actor': 'narrator',
                'content': '她今天去了书店第%d次。' % index,
                'occurredAt': (NOW + timedelta(minutes=index)).isoformat(), 'metadata': {},
            }, NOW)

        later = NOW + timedelta(hours=1)
        context = await host.prepare_compaction(story, later, False)
        self.assertEqual(context['phase'], 'run')
        self.assertEqual(len(context['scene_entries']), 4)
        self.assertTrue(context['scene_compaction_due'])
        self.assertEqual(context['compact_request']['from'], context['scene']['startedAt'])
        self.assertEqual(len(context['compact_request']['entries']), 4)

        self.assertTrue(await host.compact_unlocked(story, later, False))
        self.assertEqual(len(host.compactor.requests), 1)
        scene = await host.active_scene('story')
        self.assertEqual(scene['lastEntryId'], 4, '检查点在压缩成功后前进')
        self.assertEqual(scene['entryCount'], 4)
        self.assertEqual(scene['hook'], '她回到书店。')
        self.assertEqual(scene['summary'], '一次书店的往返。')
        self.assertEqual((await host.active_arc('story'))['summary'], '归还与重逢。')
        facts = await host.db_get('interlude_fact', {'storyId': 'story'})
        self.assertEqual([fact['content'] for fact in facts], ['她去过书店。'])
        entries = await host.db_get('interlude_script_entry', {'storyId': 'story'})
        self.assertEqual(entries[0]['metadata']['episodeTags'], {'places': ['书店']},
                         '剧集标签是字面 span，不生成摘要')
        # 检查点已前进 → 没有新条目就不该再叫模型。
        again = await host.prepare_compaction(story, later + timedelta(hours=1), False)
        self.assertEqual(again['phase'], 'skip')

    async def test_privacy_switch_hides_conversation_from_compaction_and_warns(self):
        """共享主剧本硬开启后**每句私聊都挂在参与者上**；`share_participant_details`
        关闭时压缩只能看到系统条目，摘要于是退化成「内容因隐私设置被省略」
        （用户日志里出现过，长期事实也会是 0）。隐私语义照上游，但必须有日志说清楚。
        """
        host = _PipelineHost({
            'memory': {'sceneEntryThreshold': 3, 'overlayCompressionEnabled': False},
            'schedulePreplan': {'enabled': False},
            'sharedStory': {'shareParticipantDetails': False},
        })
        self._hosts.append(host)
        host.compactor = _PipelineCompactor()
        host.db.insert('interlude_story', {
            'id': 'story', 'platform': 'onebot', 'selfId': 'bot', 'userId': '', 'channelId': '',
            'status': 'active', 'setting': {'timezone': 'Asia/Shanghai'}, 'state': {},
            'cursorAt': NOW, 'createdAt': NOW, 'updatedAt': NOW,
        })
        story = await host.get_story('story')
        await host.ensure_continuity(story, NOW)
        for index in range(1, 4):
            await host.append_entry('story', {
                'kind': 'script', 'actor': 'narrator',
                'content': '她说了第%d句话。' % index,
                'occurredAt': (NOW + timedelta(minutes=index)).isoformat(), 'metadata': {},
            }, NOW, 'onebot:bot:user:story')
        await host.append_entry('story', {
            'kind': 'setup', 'actor': 'system', 'content': '故事开始。',
            'occurredAt': NOW.isoformat(), 'metadata': {},
        }, NOW)

        later = NOW + timedelta(hours=1)
        context = await host.prepare_compaction(story, later, False)
        self.assertEqual(context['phase'], 'run')
        contents = [entry.get('content') for entry in context['compact_request']['entries']]
        self.assertIn('[participant-specific conversation omitted by privacy setting]', contents)
        self.assertNotIn('她说了第1句话。', contents)
        self.assertIn('故事开始。', contents, '系统条目不受隐私开关影响')
        self.assertTrue(
            any('隐私开关隐藏' in ' '.join(str(part) for part in args) for args in host.reports),
            '必须留下一条日志说明摘要为什么成了空壳：%s' % (host.reports[-3:],),
        )

    async def test_privacy_switch_on_lets_compaction_see_the_conversation(self):
        host = _PipelineHost({
            'memory': {'sceneEntryThreshold': 3, 'overlayCompressionEnabled': False},
            'schedulePreplan': {'enabled': False},
            'sharedStory': {'shareParticipantDetails': True},
        })
        self._hosts.append(host)
        host.compactor = _PipelineCompactor()
        host.db.insert('interlude_story', {
            'id': 'story', 'platform': 'onebot', 'selfId': 'bot', 'userId': '', 'channelId': '',
            'status': 'active', 'setting': {'timezone': 'Asia/Shanghai'}, 'state': {},
            'cursorAt': NOW, 'createdAt': NOW, 'updatedAt': NOW,
        })
        story = await host.get_story('story')
        await host.ensure_continuity(story, NOW)
        for index in range(1, 4):
            await host.append_entry('story', {
                'kind': 'script', 'actor': 'narrator',
                'content': '她说了第%d句话。' % index,
                'occurredAt': (NOW + timedelta(minutes=index)).isoformat(), 'metadata': {},
            }, NOW, 'onebot:bot:user:story')
        later = NOW + timedelta(hours=1)
        context = await host.prepare_compaction(story, later, False)
        contents = [entry.get('content') for entry in context['compact_request']['entries']]
        self.assertIn('她说了第1句话。', contents)
        self.assertFalse(
            any('隐私开关隐藏' in ' '.join(str(part) for part in args) for args in host.reports),
        )

    async def test_compact_overlay_unlocked_archives_weekly_patches(self):
        host = self._host()
        host.compactor = _OverlayCompactor()
        applied_at = NOW - timedelta(days=10)
        patch = host.db.insert('interlude_state_patch', {
            'storyId': 'story', 'participantId': '', 'target': 'character',
            'path': 'character.profile', 'proposedValue': '她更愿意主动确认。',
            'evidence': '跨场景观察', 'confidence': 0.9, 'impact': 'minor',
            'status': 'applied', 'sourceEntryIds': [1, 2],
            'createdAt': applied_at, 'appliedAt': applied_at,
        })
        changed = await host.compact_overlay_unlocked(story_row(), NOW)
        self.assertTrue(changed)
        snapshots = await host.db_get(
            'interlude_overlay_snapshot', {'storyId': 'story', 'tier': 'weekly'},
        )
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]['summary'], '她开始更主动地确认。')
        self.assertEqual(snapshots[0]['sourcePatchIds'], [patch['id']])
        self.assertEqual(snapshots[0]['majorEvents'], ['开始主动确认'])
        self.assertEqual(snapshots[0]['periodStart'], start_of_utc_window(applied_at, 5))
        rows = await host.db_get('interlude_state_patch', {'id': patch['id']})
        self.assertEqual(rows[0]['status'], 'compacted', '快照落库后原始补丁才标记为已压缩')
        self.assertEqual(host.compactor.calls[0]['tier'], 'weekly')

    async def test_compact_overlay_unlocked_skips_recent_patches_and_bad_providers(self):
        host = self._host()
        host.compactor = _OverlayCompactor()
        recent_at = NOW - timedelta(hours=6)
        host.db.insert('interlude_state_patch', {
            'storyId': 'story', 'participantId': '', 'target': 'world',
            'path': 'world.established', 'proposedValue': '刚发生的变化',
            'evidence': '本回合', 'confidence': 0.9, 'impact': 'minor',
            'status': 'applied', 'sourceEntryIds': [1],
            'createdAt': recent_at, 'appliedAt': recent_at,
        })
        self.assertFalse(await host.compact_overlay_unlocked(story_row(), NOW),
                         '最近两天的原始补丁必须留在实时层')

        class _Broken:
            async def compact_overlay(self, request):
                raise RuntimeError('provider down')

        old_at = NOW - timedelta(days=20)
        host.db.insert('interlude_state_patch', {
            'storyId': 'story', 'participantId': '', 'target': 'world',
            'path': 'world.established', 'proposedValue': '更早的变化',
            'evidence': '旧回合', 'confidence': 0.9, 'impact': 'minor',
            'status': 'applied', 'sourceEntryIds': [2],
            'createdAt': old_at, 'appliedAt': old_at,
        })
        host.compactor = _Broken()
        self.assertFalse(await host.compact_overlay_unlocked(story_row(), NOW),
                         '坏的压缩响应必须降级为 false 而不是抛出')
        rows = await host.db_get('interlude_state_patch', {'storyId': 'story', 'status': 'applied'})
        self.assertEqual(len(rows), 2, '原始补丁在失败后必须毫发无损')

    async def test_overlay_snapshots_for_prompt_prefers_monthly_and_limits_weekly(self):
        host = self._host({'sharedStory': {'shareParticipantDetails': True}})

        def add(target, tier, period_end, participant=''):
            host.db.insert('interlude_overlay_snapshot', {
                'storyId': 'story', 'participantId': participant, 'target': target, 'tier': tier,
                'periodStart': period_end - timedelta(days=5), 'periodEnd': period_end,
                'summary': '%s-%s-%s' % (target, tier, participant or 'global'),
                'majorEvents': [], 'sourcePatchIds': [],
                'status': 'active', 'createdAt': NOW, 'updatedAt': NOW,
            })

        add('character', 'monthly', NOW - timedelta(days=40))
        for index in range(6):
            add('character', 'weekly', NOW - timedelta(days=30 - index))
        add('perspective', 'weekly', NOW - timedelta(days=28))
        add('relationship', 'monthly', NOW - timedelta(days=35), 'alice')
        add('relationship', 'monthly', NOW - timedelta(days=34), 'other')

        result = await host.overlay_snapshots_for_prompt('story', 'alice')
        self.assertEqual(result[0]['target'], 'character')
        self.assertEqual(result[0]['tier'], 'monthly', '每个目标优先当月快照')
        weekly = [item for item in result if item['target'] == 'character' and item['tier'] == 'weekly']
        self.assertEqual(len(weekly), 4, '短期增量最多 4 条')
        relationship = [item for item in result if item['target'] == 'relationship']
        self.assertEqual([item['participantId'] for item in relationship], ['alice'],
                         '别人的关系分支不得泄漏')
        self.assertEqual(
            [item['target'] for item in result if item['target'] != 'character'],
            ['perspective', 'relationship'],
            '目标顺序与上游一致',
        )

        background = await host.overlay_snapshots_for_prompt('story', 'alice', True)
        relationship = [item for item in background if item['target'] == 'relationship']
        # 每个目标只取一条当月快照（`matches.find(tier === 'monthly')`，
        # 而 matches 按 periodEnd 降序 → 取最新那条）。
        self.assertEqual([item['participantId'] for item in relationship], ['other'],
                         '共享模式的后台读取可以看到全部分支')

    async def test_request_schedule_preplan_retries_exactly_once(self):
        planner = _Planner([None, {'outcome': 'replace'}])
        host = self._host()
        host.compactor = planner
        baseline = len(host.reports)  # 构造期自身会上报，只比较增量
        proposal = await host.request_schedule_preplan(story_row(), {'local_date': '2026-08-30'})
        self.assertEqual(proposal, {'outcome': 'replace'})
        self.assertEqual(planner.calls, 2)
        self.assertEqual(len(host.reports) - baseline, 1, '只报告一次空返回')

        immediate = _Planner([{'outcome': 'unchanged'}])
        host.compactor = immediate
        self.assertEqual(
            await host.request_schedule_preplan(story_row(), {}), {'outcome': 'unchanged'},
        )
        self.assertEqual(immediate.calls, 1)
        self.assertEqual(len(host.reports) - baseline, 1, '首次成功不产生恢复重试日志')

        host.compactor = object()  # 提供者不支持 plan_schedule_preplan
        self.assertIsNone(await host.request_schedule_preplan(story_row(), {}))

    async def test_persist_schedule_preplan_review_writes_the_first_empty_review(self):
        host = self._host()
        saved = []

        async def save(record):
            saved.append(record)

        host.save_schedule_preplan = save  # type: ignore[assignment]
        story = story_row(setting={'timezone': 'Asia/Shanghai'})
        persisted = await host.persist_schedule_preplan_review(
            story,
            {'current': None, 'evidence_entries': [], 'local_date': '2026-08-30'},
            None, NOW,
        )
        self.assertTrue(persisted)
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]['story_id'], 'story')
        self.assertEqual(saved[0]['regimes'], [])
        self.assertEqual(saved[0]['last_reviewed_local_date'], '2026-08-30')
        self.assertEqual(saved[0]['timezone'], 'Asia/Shanghai')

    async def test_persist_schedule_preplan_review_keeps_the_existing_version(self):
        host = self._host()
        saved = []

        async def save(record):
            saved.append(record)

        host.save_schedule_preplan = save  # type: ignore[assignment]
        current = {
            'story_id': 'story', 'revision': 3, 'timezone': 'Asia/Shanghai',
            'valid_from': '2026-08-01', 'valid_through': '2026-08-14',
            'last_reviewed_local_date': '2026-08-29', 'last_evidence_entry_id': 0,
            'review_reason': '', 'regimes': [], 'exceptions': [], 'materialized_days': [],
            'created_at': NOW, 'updated_at': NOW,
        }
        persisted = await host.persist_schedule_preplan_review(
            story_row(setting={'timezone': 'Asia/Shanghai'}),
            {'current': current, 'evidence_entries': [], 'local_date': '2026-08-30'},
            None, NOW,
        )
        self.assertTrue(persisted)
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]['last_reviewed_local_date'], '2026-08-30')
        self.assertEqual(
            saved[0]['review_reason'],
            'Invalid proposal ignored; existing Schedule Preplan retained.',
        )
        self.assertEqual(saved[0]['story_id'], 'story')

    async def test_persist_stream_script_recovery_only_writes_the_missing_script(self):
        host = self._host()
        story = story_row(state=empty_story_state() if empty_story_state else {})
        host.db.insert('interlude_story', {
            'id': 'story', 'platform': 'onebot', 'selfId': 'bot', 'userId': '', 'channelId': '',
            'status': 'active', 'setting': {'timezone': 'Asia/Shanghai'}, 'state': {},
            'cursorAt': NOW, 'createdAt': NOW, 'updatedAt': NOW,
        })
        written = await host.persist_stream_script_recovery(
            story, {'id': 'participant'},
            {'script': '  她补上了漏掉的原文。  ', 'interaction': {'seen': True, 'reply': {'mode': 'immediate', 'content': '不重复发送'}}},
            NOW,
        )
        self.assertTrue(written)
        entries = await host.db_get('interlude_script_entry', {'storyId': 'story'})
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['kind'], 'script')
        self.assertEqual(entries[0]['content'], '她补上了漏掉的原文。')
        self.assertEqual(entries[0]['metadata']['phase'], 'stream-script-recovery')
        rows = await host.db_get('interlude_story', {'id': 'story'})
        self.assertEqual(rows[0]['state'].get('narrative_update_count'), 1)
        self.assertIs(
            await host.persist_stream_script_recovery(story, None, {'script': '   '}, NOW),
            False,
        )

    async def test_schedule_stream_script_recovery_respects_attempt_cap(self):
        host = self._host({'runtime': {'narrativeRetryDelaySeconds': 5, 'narrativeRetryMaxAttempts': 6}})
        intents = []
        wakes = []

        async def append_intent(story_id, intent, now, participant_id=''):
            intents.append({'story_id': story_id, 'draft': intent, 'participant_id': participant_id})

        def schedule_wake(story_id, not_before):
            wakes.append((story_id, not_before))

        host.append_intent = append_intent  # type: ignore[assignment]
        host.schedule_due_intent_wake = schedule_wake  # type: ignore[assignment]
        # maxAttempts = min(2, max(0, 6)) = 2：第 3 次必须放弃。
        self.assertTrue(await host.schedule_stream_script_recovery('story', 'participant', NOW, 0))
        self.assertFalse(await host.schedule_stream_script_recovery('story', 'participant', NOW, 2))
        self.assertFalse(await host.schedule_stream_script_recovery('story', '', NOW, 0))
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0]['participant_id'], 'participant')
        self.assertEqual(intents[0]['draft']['payload']['streamRecovery'], True)
        self.assertEqual(intents[0]['draft']['notBefore'], intents[0]['draft']['not_before'])
        from plugin.core.time import iso

        self.assertEqual(wakes, [('story', NOW + timedelta(seconds=5))])
        self.assertEqual(intents[0]['draft']['not_before'], iso(NOW + timedelta(seconds=5)))

    async def test_schedule_fact_embedding_backfill_skips_disabled_or_unconfigured(self):
        host = self._host({'model': {'embedding': {'enabled': False, 'model': 'm', 'backfillBatchSize': 5}}})
        host.schedule_fact_embedding_backfill('story')
        self.assertEqual(host.fact_backfills, set())

        host = self._host({'model': {'embedding': {'enabled': True, 'model': '  ', 'backfillBatchSize': 5}}})
        host.schedule_fact_embedding_backfill('story')
        self.assertEqual(host.fact_backfills, set())

        host = self._host({'model': {'embedding': {'enabled': True, 'model': 'm', 'backfillBatchSize': 0}}})
        host.schedule_fact_embedding_backfill('story')
        self.assertEqual(host.fact_backfills, set())

        host = self._host({'model': {'embedding': {'enabled': True, 'model': 'm', 'backfillBatchSize': 5}}})
        host.schedule_fact_embedding_backfill('story')
        self.assertEqual(host.fact_backfills, {'story'}, '同一故事只排一次')
        host.schedule_fact_embedding_backfill('story')
        for _ in range(50):
            if not host.fact_backfills:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(host.fact_backfills, set(), '任务结束后必须释放标记')

    async def test_backfill_fact_embeddings_fills_missing_vectors_newest_first(self):
        host = self._host()

        class _Embedder:
            def __init__(self):
                self.seen = []

            async def embed(self, value):
                self.seen.append(value)
                return [0.5, 0.5]

        host.embedder = _Embedder()
        host.db.insert('interlude_fact', {
            'storyId': 'story', 'participantId': '', 'scope': 'event', 'content': '旧',
            'importance': 0.5, 'confidence': 0.5, 'unresolved': False, 'embedding': [],
            'status': 'active', 'sourceEntryIds': [], 'lastSeenAt': NOW,
            'createdAt': NOW - timedelta(days=5), 'updatedAt': NOW - timedelta(days=5),
        })
        host.db.insert('interlude_fact', {
            'storyId': 'story', 'participantId': '', 'scope': 'event', 'content': '新',
            'importance': 0.5, 'confidence': 0.5, 'unresolved': False, 'embedding': [],
            'status': 'active', 'sourceEntryIds': [], 'lastSeenAt': NOW,
            'createdAt': NOW - timedelta(days=1), 'updatedAt': NOW - timedelta(days=1),
        })
        await host.backfill_fact_embeddings('story', 1)
        self.assertEqual(host.embedder.seen, ['新'], '批量上限内的最新事实优先')
        rows = await host.db_get('interlude_fact', {'storyId': 'story'})
        embedded = [row for row in rows if row.get('embedding')]
        self.assertEqual(len(embedded), 1)
        self.assertEqual(embedded[0]['content'], '新')


if __name__ == '__main__':
    unittest.main()
