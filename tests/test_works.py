"""`plugin/core/works.py` + `plugin/core/service/chunk14.py` 的单元测试（stdlib `unittest`）。

上游测试**逐条移植**（每条用例名后标注上游文件与用例名，断言一一对应）：

| 本文件 | 上游 |
| --- | --- |
| `WorksUpstreamTests.*` | `upstream/test/works.test.ts` 的 9 条 `test(...)` |
| `WorksReviewTests.*` | `upstream/test/works-review.test.ts` 的 7 条 `test(...)` |
| `WorksSqliteTests.*` | `upstream/test/works-sqlite.test.ts` 的唯一一条（真 SQLite，走本移植版的 `interlude_work`） |

上游没测、但本移植版必须钉住的（任务书点名）：

1. `decode` 的**每一条**坏行分支都不许当成"空文档"（`DecodeGuardTests`）；
2. 剪枝的 8 / 32 / 64 边界（`PruneBoundaryTests`）；
3. `workKey` 的形状与**逐字节**序列化（`WorkKeyTests`：拿 Node 算出来的常量钉死）；
4. `splitDumpParts` 分段与 `works_dump` 的拼接还原（`WorksReviewTests` + `WorksWiringTests`）；
5. CAS 冲突不覆盖（`works.test.ts` 并发提案 + `WorksSqliteTests` 的过期 generation
   + `chunk14` 的服务层出口）；
6. 提案状态机、`lastFailure` 记录、配置门（`WorksWiringTests`）。

## 夹具为什么必须"每个存储操作都让步一次"

`works.test.ts` 的并发用例（`Promise.allSettled` 两个 `propose`）能测出 CAS，靠的是 JS 的
`await` **总是**让出一个微任务：A 读到 generation 0 → B 也读到 0 → A 写成功 → B 写失败。
Python 里 `await` 一个内部没有 `await` 的协程**不让步**，两个 `propose` 会一前一后跑完、
双双成功（假绿）。所以 `_MemoryStore` 的每个方法都 `await asyncio.sleep(0)`——
这不是"为了让测试过"，而是复现上游测试所依赖的调度语义。

运行：
    cd <仓库根目录> && python3 -m unittest plugin.tests.test_works -v
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
import unittest
from typing import Any, Optional

from plugin.core import logging as interlude_logging
from plugin.core.database import Database
from plugin.core.time import parse_dt
from plugin.core.works import (
    ASYNC_WORK_INSTRUCTION,
    KEEP_TERMINAL,
    MAX_PROPOSALS_AND_JOBS,
    MAX_REVISIONS,
    WORK_INSTRUCTION,
    SharedWorks,
    WorkStore,
    WorksError,
    bounded_text,
    decode_work_row,
    parse_work_edit,
    parse_work_generation_request,
    prune_revisions,
    prune_terminal_jobs,
    prune_terminal_proposals,
    split_dump_parts,
    work_key,
)

try:  # pragma: no cover - 取决于并行分块的落地顺序
    from plugin.core.service.base import InterludeContext
    from plugin.core.service.chunk14 import ServiceChunk14
    from plugin.core.service.transport import NullTransport
    _SERVICE_IMPORT_ERROR: Optional[BaseException] = None
except Exception as exc:  # pragma: no cover
    InterludeContext = None  # type: ignore[assignment]
    ServiceChunk14 = None  # type: ignore[assignment]
    NullTransport = None  # type: ignore[assignment]
    _SERVICE_IMPORT_ERROR = exc

#: 上游快照（发布仓里没有 `upstream/`，那条逐字比对用例会整体跳过）。
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_UPSTREAM_WORKS = os.path.join(_REPO_ROOT, 'upstream', 'src', 'works.ts')

#: Node 实测的 sha256(`["s","p"]`) 等——钉死 `workKey` 的序列化形状（含非 ASCII）。
NODE_WORK_KEYS = {
    ('s', 'p'): '2697ee60816aa3d29dbfdf24dcf975c25900a97849e4e227353077540a6872ef',
    ('s', 'alice'): '771461607c65f52df12d8e2babbb203fe527789f39e93d8f381db953b0d9096b',
    ('story-1', 'participant-1'): '27aaa8c0e3e128811d308d0cd61eed179237027c195aa9816802315a599d631c',
    ('故事', '参与者'): 'cd4e1470f502d700a06f2edd9f168f6fe1c43f7e69ce817d5edcda10c44aa276',
}


def needs_service(*names: str) -> Any:
    """`unittest.skipUnless`：服务层可导入且被测成员存在才运行。"""
    return unittest.skipUnless(
        _SERVICE_IMPORT_ERROR is None and all(hasattr(ServiceChunk14, name) for name in names),
        '依赖 core/service 其它分块：%s' % (_SERVICE_IMPORT_ERROR,),
    )


async def settle(rounds: int = 120) -> None:
    """让后台任务把该跑的几步跑完（上游 `settle = () => new Promise(setImmediate)`）。"""
    for _ in range(rounds):
        await asyncio.sleep(0)


def seeded_job(index: int, status: str = 'failed', **overrides: Any) -> dict[str, Any]:
    job = {
        'baseRevisionId': 'r0',
        'brief': 'b',
        'id': 'j%d' % index,
        'operationKey': 'op%d' % index,
        'sourceEntryId': index,
        'modelId': 'm',
        'status': status,
        'createdAt': '2026-01-01T00:00:00.000Z',
    }
    job.update(overrides)
    return job


def seeded_proposal(index: int, status: str = 'rejected') -> dict[str, Any]:
    return {
        'id': 'done%d' % index,
        'baseRevisionId': 'r0',
        'content': '稿%d' % index,
        'reason': 'r',
        'status': status,
        'author': 'user',
        'operationKey': 'k%d' % index,
        'createdAt': '2026-01-01T00:00:00.000Z',
    }


# =========================================================================== #
# 夹具
# =========================================================================== #

class _MemoryStore(WorkStore):
    """`works.test.ts` / `works-review.test.ts` 两个内存 fixture 的合体。

    每个方法都 `await asyncio.sleep(0)`（见模块 docstring）；`get` / `create` 深拷贝，
    这样"存储里的对象"与"调用方手里的对象"互不干扰（上游 `structuredClone` 同义）。
    """

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.removed: list[dict[str, Any]] = []
        self.yield_once = True

    async def _tick(self) -> None:
        if self.yield_once:
            await asyncio.sleep(0)

    async def get(self, id: str) -> Optional[dict[str, Any]]:
        await self._tick()
        row = self.rows.get(id)
        return copy.deepcopy(row) if row is not None else None

    async def create(self, row: dict[str, Any]) -> None:
        await self._tick()
        if row['id'] in self.rows:
            raise RuntimeError('duplicate')
        self.rows[row['id']] = copy.deepcopy(row)

    async def replace(self, row: dict[str, Any], generation: int) -> bool:
        await self._tick()
        current = self.rows.get(row['id'])
        if current is None or current.get('generation') != generation:
            return False
        self.rows[row['id']] = copy.deepcopy(row)
        return True

    async def list(self) -> list[dict[str, Any]]:
        await self._tick()
        return [copy.deepcopy(row) for row in self.rows.values()]

    async def remove(self, query: dict[str, Any]) -> None:
        await self._tick()
        self.removed.append(dict(query))
        for id_, row in list(self.rows.items()):
            if row.get('storyId') == query.get('storyId') and row.get('participantId') == query.get('participantId'):
                self.rows.pop(id_, None)


class _Fixture:
    """`fixture(seed?)`（上游两个测试文件各有一份，这里合成一个）。"""

    def __init__(self, seed: Any = None) -> None:
        self.store = _MemoryStore()
        if seed is not None:
            self.seed_row(seed)
        self.works = SharedWorks(self.store)

    def seed_row(self, mutate: Any) -> dict[str, Any]:
        row = {
            'id': work_key('s', 'p'),
            'storyId': 's',
            'participantId': 'p',
            'generation': 0,
            'state': {
                'schemaVersion': 1,
                'title': '标题',
                'head': 'r0',
                'revisions': [{
                    'id': 'r0',
                    'parentId': None,
                    'content': '底稿',
                    'author': 'user',
                    'createdAt': '2026-01-01T00:00:00.000Z',
                }],
                'proposals': [],
                'jobs': [],
            },
        }
        mutate(row)
        self.store.rows[row['id']] = row
        return row

    @property
    def rows(self) -> dict[str, dict[str, Any]]:
        return self.store.rows

    @property
    def removed(self) -> list[dict[str, Any]]:
        return self.store.removed

    def key(self) -> str:
        return work_key('s', 'p')

    def state(self) -> dict[str, Any]:
        return self.rows[self.key()]['state']


def fixture(seed: Any = None) -> _Fixture:
    return _Fixture(seed)


# =========================================================================== #
# 1. upstream/test/works.test.ts（9 条）
# =========================================================================== #

class WorksUpstreamTests(unittest.IsolatedAsyncioTestCase):
    """`upstream/test/works.test.ts` 逐条移植。"""

    async def test_cev_separate_generation_returns_before_inference_persists_status_deduplicates_and_needs_acceptance(self) -> None:
        f = fixture()
        row = await f.works.create('s', 'p', '短篇', '原稿')
        finish: asyncio.Future = asyncio.get_running_loop().create_future()
        calls: list[dict[str, Any]] = []

        async def generate(material: dict[str, Any]) -> str:
            calls.append(material)
            self.assertEqual(material['content'], '原稿')
            return await finish

        request = {'baseRevisionId': row['state']['head'], 'brief': '从窗外雨声开始'}
        job = await f.works.generate('s', 'p', request, 'script:1', 1, 'writer', generate)
        self.assertEqual(len(calls), 1)
        self.assertEqual((await f.works.context('s', 'p', 'separate'))['generationJobs'][0]['status'], 'running')
        again = await f.works.generate('s', 'p', request, 'script:1', 1, 'writer', generate)
        self.assertEqual(again['id'], job['id'])
        self.assertEqual(len(calls), 1)
        finish.set_result('雨滴敲在玻璃上。')
        await settle()
        result = await f.works.read('s', 'p')
        self.assertEqual(result['state']['head'], row['state']['head'])
        self.assertEqual(result['state']['jobs'][0]['status'], 'completed')
        await f.works.resolve('s', 'p', result['state']['proposals'][0]['id'], True)
        self.assertEqual((await f.works.context('s', 'p'))['content'], '雨滴敲在玻璃上。')

    async def test_cev_asynchronous_stale_result_is_retained_as_a_proposal_never_overwrites_a_newer_revision(self) -> None:
        f = fixture()
        row = await f.works.create('s', 'p', '标题', '原稿')
        finish: asyncio.Future = asyncio.get_running_loop().create_future()

        async def slow(_material: dict[str, Any]) -> str:
            return await finish

        await f.works.generate('s', 'p', {'baseRevisionId': row['state']['head'], 'brief': '改写'}, 'script:2', 2, 'writer', slow)
        edit = await f.works.propose(
            's', 'p', {'baseRevisionId': row['state']['head'], 'content': '用户新稿', 'reason': '新选择'}, 'user', 'user:3',
        )
        await f.works.resolve('s', 'p', edit['id'], True)
        finish.set_result('较早基础的独立模型草稿')
        await settle()
        current = await f.works.read('s', 'p')
        generated = next(item for item in current['state']['proposals'] if item['operationKey'] == 'script:2')
        self.assertTrue(generated)
        with self.assertRaisesRegex(WorksError, '旧版本'):
            await f.works.resolve('s', 'p', generated['id'], True)
        self.assertEqual((await f.works.context('s', 'p'))['content'], '用户新稿')

    async def test_cev_failure_has_no_retry_old_rows_interruption_and_cancellation_remain_explicit(self) -> None:
        f = fixture()
        row = await f.works.create('s', 'p', '标题', '原稿')
        self.assertEqual(await f.works.generation_status('s', 'p'), [])
        calls = 0
        input_ = {'baseRevisionId': row['state']['head'], 'brief': '改写'}

        async def failing(_material: dict[str, Any]) -> str:
            nonlocal calls
            calls += 1
            raise RuntimeError('request failed')

        await f.works.generate('s', 'p', input_, 'script:1', 1, 'writer', failing)
        await settle()
        self.assertEqual(calls, 1)
        self.assertEqual((await f.works.generation_status('s', 'p'))[0]['status'], 'failed')
        self.assertEqual((await f.works.read('s', 'p'))['state']['proposals'], [])
        finish: asyncio.Future = asyncio.get_running_loop().create_future()

        async def slow(_material: dict[str, Any]) -> str:
            return await finish

        job = await f.works.generate('s', 'p', input_, 'script:2', 2, 'writer', slow)
        reopened = SharedWorks(f.store)
        self.assertEqual((await reopened.generation_status('s', 'p'))[1]['status'], 'interrupted')
        with self.assertRaisesRegex(WorksError, '中断'):

            async def no(_material: dict[str, Any]) -> str:
                return 'no'

            await reopened.generate('s', 'p', input_, 'script:3', 3, 'writer', no)
        await reopened.cancel_generation('s', 'p', job['id'])
        finish.set_result('不应写入')
        await settle()
        self.assertEqual((await f.works.read('s', 'p'))['state']['proposals'], [])

    async def test_cev_deletion_recreation_and_disposal_invalidate_pending_results_concurrency_is_bounded(self) -> None:
        f = fixture()
        row = await f.works.create('s', 'p', '标题', '原稿')
        second = await f.works.create('s', 'other', '另一作品', '另一原稿')
        finish: asyncio.Future = asyncio.get_running_loop().create_future()

        async def slow(_material: dict[str, Any]) -> str:
            return await finish

        await f.works.generate('s', 'p', {'baseRevisionId': row['state']['head'], 'brief': '改写'}, 'script:1', 1, 'writer', slow)

        async def no(_material: dict[str, Any]) -> str:
            return 'no'

        with self.assertRaisesRegex(WorksError, '进行中'):
            await f.works.generate(
                's', 'other', {'baseRevisionId': second['state']['head'], 'brief': '改写'}, 'script:2', 2, 'writer', no,
            )
        f.rows.pop(row['id'])
        await f.works.create('s', 'p', '重建作品', '全新原文')
        finish.set_result('旧任务输出')
        await settle()
        self.assertEqual((await f.works.read('s', 'p'))['state']['proposals'], [])
        finish2: asyncio.Future = asyncio.get_running_loop().create_future()

        async def slow2(_material: dict[str, Any]) -> str:
            return await finish2

        await f.works.generate(
            's', 'other', {'baseRevisionId': second['state']['head'], 'brief': '改写'}, 'script:3', 3, 'writer', slow2,
        )
        f.works.stop()
        finish2.set_result('关闭后的输出')
        await settle()
        self.assertEqual((await SharedWorks(f.store).generation_status('s', 'other'))[0]['status'], 'interrupted')

    async def test_cev_create_proposal_accept_keeps_immutable_versions_and_survives_reopening(self) -> None:
        f = fixture()
        initial = await f.works.create('s', 'alice', '共同短篇', '旧正文')
        proposal = await f.works.propose(
            's', 'alice', {'baseRevisionId': initial['state']['head'], 'content': '新正文', 'reason': '保留留白'},
            'protagonist', 'script:1', 1,
        )
        self.assertEqual((await f.works.read('s', 'alice'))['state']['head'], initial['state']['head'])
        accepted = await f.works.resolve('s', 'alice', proposal['id'], True)
        self.assertEqual(accepted['state']['revisions'][0]['content'], '旧正文')
        self.assertEqual(accepted['state']['revisions'][1]['parentId'], initial['state']['head'])
        self.assertEqual(accepted['state']['revisions'][1]['content'], '新正文')
        self.assertEqual(await SharedWorks(f.store).read('s', 'alice'), accepted)
        self.assertEqual(await f.works.resolve('s', 'alice', proposal['id'], True), accepted)

    async def test_cev_rejection_does_not_modify_head_other_user_and_other_story_cannot_access_it(self) -> None:
        f = fixture()
        row = await f.works.create('s', 'alice', '标题', '正文')
        proposal = await f.works.propose(
            's', 'alice', {'baseRevisionId': row['state']['head'], 'content': '另一稿', 'reason': '提案'}, 'user', '1',
        )
        rejected = await f.works.resolve('s', 'alice', proposal['id'], False)
        self.assertEqual(rejected['state']['head'], row['state']['head'])
        self.assertEqual(rejected['state']['proposals'][0]['status'], 'rejected')
        self.assertIsNone(await f.works.read('s', 'bob'))
        self.assertIsNone(await f.works.context('other', 'alice'))
        with self.assertRaises(WorksError):
            await f.works.resolve('s', 'bob', proposal['id'], True)
        with self.assertRaisesRegex(WorksError, '已经处理'):
            await f.works.resolve('s', 'alice', proposal['id'], True)

    async def test_cev_stale_proposal_stays_pending_instead_of_overwriting_a_newer_revision(self) -> None:
        f = fixture()
        row = await f.works.create('s', 'p', '标题', '正文')
        edit = {'baseRevisionId': row['state']['head'], 'content': 'A', 'reason': '修改'}
        first = await f.works.propose('s', 'p', edit, 'user', 'a')
        second = await f.works.propose('s', 'p', {**edit, 'content': 'B'}, 'protagonist', 'b')
        await f.works.resolve('s', 'p', first['id'], True)
        with self.assertRaisesRegex(WorksError, '旧版本'):
            await f.works.resolve('s', 'p', second['id'], True)
        self.assertEqual((await f.works.read('s', 'p'))['state']['proposals'][1]['status'], 'pending')

    async def test_cev_concurrent_edits_use_compare_and_swap_not_last_writer_overwrite(self) -> None:
        f = fixture()
        row = await f.works.create('s', 'p', '标题', '正文')
        results = await asyncio.gather(
            *[
                f.works.propose(
                    's', 'p', {'baseRevisionId': row['state']['head'], 'content': key, 'reason': '修改'}, 'user', key,
                )
                for key in ('a', 'b')
            ],
            return_exceptions=True,
        )
        self.assertEqual(len([item for item in results if not isinstance(item, BaseException)]), 1)
        self.assertEqual(len((await f.works.read('s', 'p'))['state']['proposals']), 1)

    async def test_cev_operation_idempotence_failure_feedback_and_corrupt_legacy_rows_are_safe(self) -> None:
        f = fixture()
        row = await f.works.create('s', 'p', '标题', '正文')
        await f.works.record_failure('s', 'p', 5)
        self.assertEqual((await f.works.context('s', 'p'))['lastFailure']['status'], 'proposal-not-saved')
        edit = {'baseRevisionId': row['state']['head'], 'content': 'A', 'reason': '修改'}
        first = await f.works.propose('s', 'p', edit, 'protagonist', 'script:6', 6)
        self.assertEqual(await f.works.propose('s', 'p', edit, 'protagonist', 'script:6', 6), first)
        self.assertNotIn('lastFailure', await f.works.context('s', 'p'))
        with self.assertRaisesRegex(WorksError, '已有共同作品'):
            await f.works.create('s', 'p', '替换', '覆盖')
        f.rows[row['id']]['state'] = {}
        with self.assertRaisesRegex(WorksError, '形状不兼容'):
            await f.works.read('s', 'p')
        self.assertEqual(f.rows[row['id']]['state'], {})

    async def test_cev_bounded_full_text_proposals_preserve_literal_source_and_reject_oversized_drafts(self) -> None:
        content = '  原文\n\n保留标点。  '
        self.assertEqual(parse_work_edit({'content': content, 'baseRevisionId': 'r', 'reason': '保留'})['content'], content)
        for bad in (None, {}, {'content': 'x' * 8001, 'baseRevisionId': 'r', 'reason': 'x'}):
            with self.assertRaises(WorksError):
                parse_work_edit(bad)


# =========================================================================== #
# 2. upstream/test/works-review.test.ts（7 条）
# =========================================================================== #

class WorksReviewTests(unittest.IsolatedAsyncioTestCase):
    """`upstream/test/works-review.test.ts` 逐条移植。"""

    async def test_review1_resolved_proposals_are_pruned_at_propose_time_pending_cap_still_enforced(self) -> None:
        def seed(row: dict[str, Any]) -> None:
            for index in range(30):
                row['state']['proposals'].append(seeded_proposal(index, 'accepted' if index % 2 else 'rejected'))

        f = fixture(seed)
        before = len(f.rows[f.key()]['state']['proposals'])
        await f.works.propose('s', 'p', {'baseRevisionId': 'r0', 'content': '新稿', 'reason': '测试'}, 'user', 'cmd:1')
        state = f.state()
        self.assertEqual(before, 30)
        # 终态只保留最近 8 条 + 1 条新 pending
        terminal = [item for item in state['proposals'] if item['status'] != 'pending']
        self.assertEqual(len(terminal), 8)
        self.assertTrue(any(item['status'] == 'pending' for item in state['proposals']))
        # pending 打满后仍然拒绝（需用户处理，而不是锁死历史）
        for index in range(40):
            try:
                await f.works.propose(
                    's', 'p', {'baseRevisionId': f.state()['head'], 'content': '稿%d' % index, 'reason': 'x'},
                    'user', 'cmd:1%d' % index,
                )
            except WorksError:
                break
        final_state = f.state()
        self.assertEqual(len(final_state['proposals']), MAX_PROPOSALS_AND_JOBS, '总量精确停在 decode 上限（剪除为待决腾出空间）')
        with self.assertRaisesRegex(WorksError, '提案已达 32 条上限'):
            await f.works.propose(
                's', 'p', {'baseRevisionId': f.state()['head'], 'content': '超', 'reason': 'x'}, 'user', 'cmd:overflow',
            )

    async def test_review1_revisions_roll_instead_of_bricking_at_64(self) -> None:
        f = fixture()
        await f.works.create('s', 'p', '标题', '底稿')
        for index in range(70):
            proposal = await f.works.propose(
                's', 'p', {'baseRevisionId': f.state()['head'], 'content': '第%d版' % index, 'reason': '滚动'},
                'user', 'k%d' % index,
            )
            await f.works.resolve('s', 'p', proposal['id'], True)
        state = f.state()
        self.assertEqual(len(state['revisions']), MAX_REVISIONS, '滚动窗口封顶 64，不再抛错锁死')
        self.assertTrue(all(isinstance(item['content'], str) for item in state['revisions']))
        # 剪掉最旧后 decode 兼容（读回不炸）
        self.assertIsNotNone(await f.works.read('s', 'p'))

    async def test_review1_terminal_jobs_pruned_before_the_generate_cap_check(self) -> None:
        def seed(row: dict[str, Any]) -> None:
            row['state']['jobs'] = [
                seeded_job(index, 'failed' if index < 25 else 'completed') for index in range(30)
            ]

        f = fixture(seed)

        async def writer(_material: dict[str, Any]) -> str:
            return '生成稿'

        job = await f.works.generate('s', 'p', {'baseRevisionId': 'r0', 'brief': '新任务'}, 'op-new', 1, 'm', writer)
        jobs = f.state()['jobs']
        self.assertEqual(job['status'], 'running')
        self.assertLessEqual(len(jobs), MAX_PROPOSALS_AND_JOBS, '任务滚动窗口不超过 decode 上限')
        self.assertEqual(len([item for item in jobs if item['status'] != 'running']), KEEP_TERMINAL, '终态任务保留最近 8 条')
        await asyncio.sleep(0.02)

    async def test_review2_startup_recovery_marks_stale_running_jobs_failed_and_frees_may_propose(self) -> None:
        def seed(row: dict[str, Any]) -> None:
            row['state']['jobs'] = [seeded_job(1, 'running', operationKey='op1', id='j1')]

        f = fixture(seed)
        ctx0 = await f.works.context('s', 'p', 'separate')
        self.assertFalse(ctx0['mayPropose'], 'DB running 任务压住 mayPropose')
        await f.works.startup_recover()
        self.assertEqual(f.state()['jobs'][0]['status'], 'failed')
        ctx1 = await f.works.context('s', 'p', 'separate')
        self.assertTrue(ctx1['mayPropose'], '清扫后 mayPropose 解锁')

    async def test_review3_delete_all_goes_through_the_store_abstraction(self) -> None:
        f = fixture()
        await f.works.create('s', 'p', '标题', '正文')
        await f.works.delete_all('s', 'p')
        self.assertEqual(f.removed, [{'storyId': 's', 'participantId': 'p'}])
        self.assertEqual(len(f.rows), 0)

    async def test_review4_split_dump_parts_chunks_oversized_dumps_losslessly(self) -> None:
        text = json.dumps({'a': 'x' * 6000})
        parts = split_dump_parts(text, 2400)
        self.assertGreaterEqual(len(parts), 3)
        self.assertEqual(''.join(parts), text, '分段无损可拼接')
        self.assertEqual(split_dump_parts('short'), ['short'])

    async def test_review11_last_failure_carries_a_timestamp_the_model_can_judge_recency_by(self) -> None:
        f = fixture()
        await f.works.create('s', 'p', '标题', '底稿')
        await f.works.record_failure('s', 'p', 7)
        failure = f.state()['lastFailure']
        self.assertEqual(failure['sourceEntryId'], 7)
        self.assertTrue(failure.get('at') and parse_dt(failure['at']) is not None, 'at 是可解析时间戳')
        ctx = await f.works.context('s', 'p')
        self.assertTrue(ctx['lastFailure']['at'])

    async def test_review5_instructions_state_the_may_propose_semantics_consistently(self) -> None:
        self.assertRegex(WORK_INSTRUCTION, r'mayPropose shows whether a new request is wanted')
        self.assertRegex(WORK_INSTRUCTION, r'allowed regardless of running jobs')
        self.assertRegex(ASYNC_WORK_INSTRUCTION, r'lastFailure\.at timestamps the most recent failure')
        self.assertRegex(WORK_INSTRUCTION, r'lastFailure\.at timestamps a failed save attempt')

    @unittest.skipUnless(os.path.isfile(_UPSTREAM_WORKS), '工作区才有 upstream/ 快照')
    async def test_instructions_are_verbatim_upstream_text(self) -> None:
        """两个提示词是**逐字**移植：与上游 TS 源里的模板串整串相等（英文一字不改）。"""
        with open(_UPSTREAM_WORKS, encoding='utf-8') as handle:
            source = handle.read()
        for name, value in (('WORK_INSTRUCTION', WORK_INSTRUCTION), ('ASYNC_WORK_INSTRUCTION', ASYNC_WORK_INSTRUCTION)):
            match = re.search(r'export const %s = `(.*?)`\n' % name, source, re.S)
            self.assertIsNotNone(match, name)
            self.assertEqual(match.group(1), value, name)


# =========================================================================== #
# 3. upstream/test/works-sqlite.test.ts（1 条，走真 SQLite）
# =========================================================================== #

@needs_service('shared_works')
class WorksSqliteTests(unittest.IsolatedAsyncioTestCase):
    """`upstream/test/works-sqlite.test.ts`：真数据库上的原子保存与过期 generation 拒绝。"""

    def setUp(self) -> None:
        self.db = Database(':memory:')
        self.db.register_tables()
        self.addCleanup(self.db.close)
        ctx = InterludeContext(logger=None, database=self.db)
        self.service = ServiceChunk14(ctx, {'works': {'enabled': True}}, self.db, NullTransport())
        self.store = self.service.shared_works().store

    async def test_cev_real_sqlite_atomically_saves_versions_and_rejects_stale_generations(self) -> None:
        works = SharedWorks(self.store)
        row = await works.create('s', 'p', 'SQLite 作品', '初稿')
        proposal = await works.propose(
            's', 'p', {'baseRevisionId': row['state']['head'], 'content': '修订稿', 'reason': '用户反馈'},
            'protagonist', 'script:1', 1,
        )
        saved = await works.resolve('s', 'p', proposal['id'], True)
        self.assertEqual(len(saved['state']['revisions']), 2)
        # 过期 generation：读回来比对不匹配 → 直接 False，绝不覆盖
        self.assertFalse(await self.store.replace(copy.deepcopy(row), 0))
        self.assertEqual((await works.read('s', 'p'))['state']['head'], saved['state']['head'])
        # 条件写本身也是原子的：where 里带旧 generation，受影响行数必须是 0
        matched = await self.service.db_set(
            'interlude_work', {'id': row['id'], 'generation': 0}, {'state': row['state']},
        )
        self.assertEqual(int(matched or 0), 0)
        self.assertEqual(len(await self.service.db_get('interlude_work', {})), 1)
        await self.service.db_remove('interlude_work', {'storyId': 's'})
        self.assertIsNone(await works.read('s', 'p'))

    async def test_corrupt_row_in_database_is_an_error_and_never_overwritten(self) -> None:
        """坏行落库后：`read` 报错、`propose` 被拒，而**库里那一行原样不动**。"""
        works = SharedWorks(self.store)
        row = await works.create('s', 'p', '标题', '正文')
        await self.service.db_set('interlude_work', {'id': row['id']}, {'state': {'schemaVersion': 7}})
        with self.assertRaisesRegex(WorksError, '形状不兼容'):
            await works.read('s', 'p')
        with self.assertRaisesRegex(WorksError, '形状不兼容'):
            await works.propose('s', 'p', {'baseRevisionId': 'x', 'content': 'y', 'reason': 'z'}, 'user', 'k')
        stored = await self.service.db_get('interlude_work', {'id': row['id']})
        self.assertEqual(stored[0]['state'], {'schemaVersion': 7})
        self.assertEqual(int(stored[0]['generation']), 0)

    async def test_startup_recovery_and_delete_use_the_real_table(self) -> None:
        works = SharedWorks(self.store)
        row = await works.create('s', 'p', '标题', '正文')

        async def slow(_material: dict[str, Any]) -> str:
            await asyncio.sleep(30)
            return '不会写进去'

        await works.generate('s', 'p', {'baseRevisionId': row['state']['head'], 'brief': '改写'}, 'script:1', 1, 'm', slow)
        # 模拟进程重载：新的 SharedWorks（内存 active 为空）+ 启动清扫
        fresh = SharedWorks(self.store)
        self.assertEqual((await fresh.generation_status('s', 'p'))[0]['status'], 'interrupted')
        self.assertEqual(await fresh.startup_recover(), 1)
        self.assertEqual((await fresh.generation_status('s', 'p'))[0]['status'], 'failed')
        await fresh.delete_all('s', 'p')
        self.assertIsNone(await fresh.read('s', 'p'))
        self.assertEqual(await self.service.db_get('interlude_work', {}), [])


# =========================================================================== #
# 4. 本移植版必须钉住的边界（任务书点名）
# =========================================================================== #

class WorkKeyTests(unittest.TestCase):
    """`workKey` 的形状与**逐字节**序列化。

    主键形状是持久化格式：序列化形状一变（比如 `json.dumps` 默认的 `", "` 分隔），
    同一个 (story, participant) 就算出另一个 id，等于整行数据凭空消失。所以这里拿
    Node 的 `createHash('sha256').update(JSON.stringify([a, b]))` 实算值钉死，
    并额外断言"不是 `json.dumps` 默认分隔符的那个值"。
    """

    def test_work_key_matches_node_sha256_of_json_stringify(self) -> None:
        for (story, participant), expected in NODE_WORK_KEYS.items():
            self.assertEqual(work_key(story, participant), expected, (story, participant))

    def test_work_key_shape_is_lowercase_sha256_hex(self) -> None:
        key = work_key('s', 'p')
        self.assertEqual(len(key), 64)
        self.assertRegex(key, r'^[0-9a-f]{64}$')
        self.assertEqual(key, work_key('s', 'p'), '同一对输入必须稳定')

    def test_work_key_distinguishes_order_and_values(self) -> None:
        self.assertNotEqual(work_key('s', 'p'), work_key('p', 's'))
        self.assertNotEqual(work_key('s', 'p'), work_key('s', 'p2'))
        # 字符串拼接会撞（`('ab','c')` 与 `('a','bc')`）：JSON 数组形状不会
        self.assertNotEqual(work_key('ab', 'c'), work_key('a', 'bc'))

    def test_default_json_separators_would_have_been_wrong(self) -> None:
        naive = hashlib.sha256(json.dumps(['s', 'p']).encode('utf-8')).hexdigest()
        self.assertNotEqual(naive, work_key('s', 'p'))
        exact = hashlib.sha256(b'["s","p"]').hexdigest()
        self.assertEqual(exact, work_key('s', 'p'))


class BoundedTextTests(unittest.TestCase):
    """`boundedText`：1～max 个字符，原样返回，越界/空/非文本一律报错。"""

    def test_preserves_literal_text(self) -> None:
        for value in ('x', '  前后留白  ', '第一行\n\n第二行', '🙂'):
            self.assertEqual(bounded_text(value, '作品正文', 8000), value)

    def test_rejects_empty_non_text_and_oversized(self) -> None:
        for bad in ('', '   ', '\n\t', None, 5, ['x'], True):
            with self.assertRaises(WorksError):
                bounded_text(bad, '标题', 120)
        with self.assertRaises(WorksError):
            bounded_text('x' * 121, '标题', 120)
        self.assertEqual(bounded_text('x' * 120, '标题', 120), 'x' * 120)

    def test_length_is_measured_in_js_utf16_units(self) -> None:
        """JS 的 `String#length` 把星号平面字符算 2（坑 11 的同一类语义差）。"""
        emoji = '🙂' * 60          # 60 个字符，JS 长度 120
        self.assertEqual(len(emoji), 60)
        self.assertIsNotNone(bounded_text(emoji, '标题', 120))
        with self.assertRaises(WorksError):
            bounded_text(emoji + '🙂', '标题', 120)

    def test_parse_work_generation_request_bounds_and_spellings(self) -> None:
        parsed = parse_work_generation_request({'baseRevisionId': 'r', 'brief': '来一段雨'})
        self.assertEqual(parsed, {'baseRevisionId': 'r', 'brief': '来一段雨'})
        # 键名法：读外部输入两种拼写都认
        self.assertEqual(
            parse_work_generation_request({'base_revision_id': 'r', 'brief': 'x'})['baseRevisionId'], 'r',
        )
        for bad in (None, 'x', [], {'baseRevisionId': 'r'}, {'brief': 'x'}, {'baseRevisionId': 'r', 'brief': 'x' * 2001}):
            with self.assertRaises(WorksError):
                parse_work_generation_request(bad)


class DecodeGuardTests(unittest.TestCase):
    """`decode`：坏行是错误，**绝不是**可以覆盖的空文档。

    每个分支各来一条：任何"看着像空作品"的形状都必须报错，让上层保留原数据。
    """

    def base_row(self) -> dict[str, Any]:
        return {
            'id': 'k',
            'storyId': 's',
            'participantId': 'p',
            'generation': 0,
            'state': {
                'schemaVersion': 1,
                'title': '标题',
                'head': 'r0',
                'revisions': [{'id': 'r0', 'parentId': None, 'content': '正文', 'author': 'user'}],
                'proposals': [],
            },
        }

    def test_accepts_a_minimal_valid_row_and_returns_a_deep_copy(self) -> None:
        row = self.base_row()
        decoded = decode_work_row(row)
        self.assertEqual(decoded, row)
        decoded['state']['title'] = '被改坏'
        self.assertEqual(row['state']['title'], '标题', 'decode 必须深拷贝（上游 structuredClone）')

    def test_rejects_every_bad_shape(self) -> None:
        cases: list[tuple[str, Any]] = [
            ('不是对象', None),
            ('不是对象', 'x'),
            ('没有 state', {'generation': 0}),
            ('state 是空对象', {'generation': 0, 'state': {}}),
            ('schemaVersion 不是 1', {**self.base_row(), 'state': {**self.base_row()['state'], 'schemaVersion': 2}}),
            ('schemaVersion 是 true', {**self.base_row(), 'state': {**self.base_row()['state'], 'schemaVersion': True}}),
            ('revisions 不是数组', {**self.base_row(), 'state': {**self.base_row()['state'], 'revisions': {}}}),
            ('revisions 为空', {**self.base_row(), 'state': {**self.base_row()['state'], 'revisions': []}}),
            ('head 不命中任何版本', {**self.base_row(), 'state': {**self.base_row()['state'], 'head': 'nope'}}),
            ('版本缺 content', {
                **self.base_row(),
                'state': {**self.base_row()['state'], 'revisions': [{'id': 'r0'}]},
            }),
            ('版本元素是 null', {
                **self.base_row(),
                'state': {**self.base_row()['state'], 'revisions': [None]},
            }),
            ('提案状态非法', {
                **self.base_row(),
                'state': {**self.base_row()['state'], 'proposals': [{'id': 'p', 'content': 'x', 'status': 'maybe'}]},
            }),
            ('generation 不是整数', {**self.base_row(), 'generation': '0'}),
            ('generation 是 bool', {**self.base_row(), 'generation': True}),
            ('generation 是浮点', {**self.base_row(), 'generation': 1.5}),
            ('generation 超出安全整数', {**self.base_row(), 'generation': 2 ** 53}),
        ]
        for label, row in cases:
            with self.assertRaisesRegex(WorksError, '形状不兼容', msg=label):
                decode_work_row(row)

    def test_rejects_oversized_windows(self) -> None:
        row = self.base_row()
        row['state']['revisions'] = [{'id': 'r%d' % i, 'content': 'x'} for i in range(MAX_REVISIONS + 1)]
        row['state']['head'] = 'r0'
        with self.assertRaisesRegex(WorksError, '形状不兼容'):
            decode_work_row(row)
        row = self.base_row()
        row['state']['revisions'] = [{'id': 'r%d' % i, 'content': 'x'} for i in range(MAX_REVISIONS)]
        row['state']['head'] = 'r0'
        self.assertIsNotNone(decode_work_row(row), '恰好 64 条必须合法')
        row = self.base_row()
        row['state']['proposals'] = [
            {'id': 'p%d' % i, 'content': 'x', 'status': 'pending'} for i in range(MAX_PROPOSALS_AND_JOBS + 1)
        ]
        with self.assertRaisesRegex(WorksError, '形状不兼容'):
            decode_work_row(row)

    def test_rejects_bad_jobs_shape(self) -> None:
        bad_jobs: list[Any] = [
            'x',
            {},
            [{'id': 'j', 'brief': 'b', 'baseRevisionId': 'r', 'status': 'whatever'}],
            [{'id': 'j', 'brief': 'b', 'status': 'running'}],
            [None],
        ]
        for jobs in bad_jobs:
            row = self.base_row()
            row['state']['jobs'] = jobs
            with self.assertRaisesRegex(WorksError, '任务数据不兼容'):
                decode_work_row(row)
        # 显式 null（不是"键不存在"）同样是坏行
        row = self.base_row()
        row['state']['jobs'] = None
        with self.assertRaisesRegex(WorksError, '任务数据不兼容'):
            decode_work_row(row)
        # 键不存在 / 空数组都合法
        for jobs in ('absent', []):
            row = self.base_row()
            if jobs != 'absent':
                row['state']['jobs'] = jobs
            self.assertIsNotNone(decode_work_row(row))
        # 超过 32 条任务也是坏行
        row = self.base_row()
        row['state']['jobs'] = [
            {'id': 'j%d' % i, 'brief': 'b', 'baseRevisionId': 'r', 'status': 'completed'}
            for i in range(MAX_PROPOSALS_AND_JOBS + 1)
        ]
        with self.assertRaisesRegex(WorksError, '任务数据不兼容'):
            decode_work_row(row)

    def test_read_rejects_a_row_that_belongs_to_another_owner(self) -> None:
        class _Store(WorkStore):
            async def get(self, id: str) -> dict[str, Any]:
                return {'id': id, 'storyId': 'other', 'participantId': 'p', 'generation': 0, 'state': {}}

            async def create(self, row: dict[str, Any]) -> None:
                raise AssertionError('不该走到这里')

            async def replace(self, row: dict[str, Any], generation: int) -> bool:
                raise AssertionError('不该走到这里')

        works = SharedWorks(_Store())
        with self.assertRaisesRegex(WorksError, '归属不一致'):
            asyncio.run(works.read('s', 'p'))


class PruneBoundaryTests(unittest.TestCase):
    """剪枝的 8 / 32 / 64 边界（上游 review#1 的三条修法）。"""

    def test_terminal_proposals_keep_the_newest_eight(self) -> None:
        for count, expected in ((KEEP_TERMINAL - 1, KEEP_TERMINAL - 1), (KEEP_TERMINAL, KEEP_TERMINAL), (KEEP_TERMINAL + 1, KEEP_TERMINAL), (30, KEEP_TERMINAL)):
            state = {'proposals': [seeded_proposal(i, 'accepted' if i % 2 else 'rejected') for i in range(count)]}
            prune_terminal_proposals(state)
            self.assertEqual(len(state['proposals']), expected, count)
        state = {'proposals': [seeded_proposal(i, 'accepted' if i % 2 else 'rejected') for i in range(30)]}
        state['proposals'].append(seeded_proposal(99, 'pending'))
        prune_terminal_proposals(state)
        kept = [item['id'] for item in state['proposals']]
        self.assertEqual(len(state['proposals']), KEEP_TERMINAL + 1)
        self.assertIn('done99', kept, '待决提案永不被剪')
        self.assertNotIn('done0', kept)
        self.assertIn('done29', kept, '保留的是最近的终态')

    def test_terminal_jobs_keep_the_newest_eight_and_never_drop_running(self) -> None:
        state = {'jobs': [seeded_job(i, 'failed' if i < 25 else 'completed') for i in range(30)]}
        prune_terminal_jobs(state)
        self.assertEqual(len(state['jobs']), KEEP_TERMINAL)
        self.assertEqual([item['id'] for item in state['jobs']], ['j%d' % i for i in range(22, 30)])
        state = {'jobs': [seeded_job(i, 'failed') for i in range(30)] + [seeded_job(99, 'running')]}
        prune_terminal_jobs(state)
        self.assertIn('j99', [item['id'] for item in state['jobs']], 'running 任务永不被剪')
        self.assertEqual(len([item for item in state['jobs'] if item['status'] != 'running']), KEEP_TERMINAL)
        empty: dict[str, Any] = {}
        prune_terminal_jobs(empty)
        self.assertNotIn('jobs', empty, '没有 jobs 键时什么都不做')
        state = {'jobs': []}
        prune_terminal_jobs(state)
        self.assertEqual(state['jobs'], [])

    def test_revisions_roll_at_sixty_four(self) -> None:
        state = {'revisions': [{'id': 'r%d' % i, 'content': 'x'} for i in range(MAX_REVISIONS)]}
        prune_revisions(state)
        self.assertEqual(len(state['revisions']), MAX_REVISIONS, '恰好 64 条不动')
        state['revisions'].append({'id': 'r-new', 'content': 'y'})
        prune_revisions(state)
        self.assertEqual(len(state['revisions']), MAX_REVISIONS)
        self.assertEqual(state['revisions'][0]['id'], 'r1', '丢最旧')
        self.assertEqual(state['revisions'][-1]['id'], 'r-new', '新版本一定在')

    def test_cap_constant_is_the_decode_limit(self) -> None:
        self.assertEqual(KEEP_TERMINAL, 8)
        self.assertEqual(MAX_REVISIONS, 64)
        self.assertEqual(MAX_PROPOSALS_AND_JOBS, 32)


class SplitDumpPartsTests(unittest.TestCase):
    """`splitDumpParts` 的边界（含上游没覆盖的非法参数）。"""

    def test_short_text_is_one_part_and_long_text_round_trips(self) -> None:
        self.assertEqual(split_dump_parts(''), [''])
        self.assertEqual(split_dump_parts('短'), ['短'])
        self.assertEqual(split_dump_parts('x' * 2400), ['x' * 2400])
        text = json.dumps({'a': 'x' * 6000}, ensure_ascii=False)
        parts = split_dump_parts(text)
        self.assertEqual(len(parts), 3)
        self.assertTrue(all(len(part) <= 2400 for part in parts))
        self.assertEqual(''.join(parts), text)

    def test_large_dump_survives_many_parts(self) -> None:
        text = json.dumps({'a': '汉' * 20000}, ensure_ascii=False)
        parts = split_dump_parts(text, 1000)
        self.assertEqual(''.join(parts), text)

    def test_rejects_non_positive_max_length(self) -> None:
        for bad in (0, -1):
            with self.assertRaises(WorksError):
                split_dump_parts('x', bad)


# =========================================================================== #
# 5. 服务层接线（本移植版新增）
# =========================================================================== #

class _Sink:
    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def __call__(self, level: str, text: str) -> None:
        self.records.append((level, text))

    def text(self) -> str:
        return '\n'.join(text for _level, text in self.records)

    def levels(self) -> list[str]:
        return [level for level, _text in self.records]


class _FakeNarrator:
    """只实现 chunk14 用到的那两个口（`_assigned_providers` / `_side_task_json`）。"""

    def __init__(self, draft: str = '雨滴敲在玻璃上。') -> None:
        self.draft = draft
        self.calls: list[dict[str, Any]] = []

    def _assigned_providers(self, task: str) -> list[dict[str, Any]]:
        if task != 'main':
            return []
        return [{'id': 'main-conn', 'model': 'main-model', 'enabled': True, 'endpoint': 'http://192.168.1.9/v1/chat/completions'}]

    async def _side_task_json(self, provider: Any, model: str, task: str, timeout: Any, build_body: Any, parse: Any) -> Any:
        body = build_body(True)
        self.calls.append({'provider': provider, 'model': model, 'task': task, 'body': body})
        return parse(self.draft)


def works_config(**works_section: Any) -> dict[str, Any]:
    section = {'enabled': True}
    section.update(works_section)
    return {
        'works': section,
        'model': {
            'providers': [
                {
                    'id': 'main-conn',
                    'model': 'main-model',
                    'enabled': True,
                    'endpoint': 'http://192.168.1.9/v1/chat/completions',
                },
                {
                    'id': 'writer-conn',
                    'model': 'writer-model',
                    'enabled': True,
                    'endpoint': 'http://192.168.1.9/v1/chat/completions',
                },
            ],
        },
        'logging': {'level': 'debug', 'verbosity': 'diagnostic', 'format': 'layered'},
    }


@needs_service('shared_work_state', 'apply_work_proposal', 'start_work_generation', 'works_dump')
class WorksWiringTests(unittest.IsolatedAsyncioTestCase):
    """chunk14 的接线：配置门、payload 投影、提案保存、手改、接受/拒绝、导出、写手任务。"""

    def setUp(self) -> None:
        self.sink = _Sink()
        interlude_logging.set_log_sink(self.sink)
        self.addCleanup(interlude_logging.set_log_sink, interlude_logging._default_sink)
        self.db = Database(':memory:')
        self.db.register_tables()
        self.addCleanup(self.db.close)

    def make_service(self, config: Any = None) -> Any:
        ctx = InterludeContext(logger=None, database=self.db)
        service = ServiceChunk14(ctx, config if config is not None else works_config(), self.db, NullTransport())
        self.addCleanup(service.stop_works)
        return service

    async def head_of(self, service: Any, story: Any = 's', participant: Any = 'p') -> str:
        row = await service.shared_works().read(
            service._work_keys(story, participant)[0], service._work_keys(story, participant)[1],
        )
        return row['state']['head']

    # ---- 配置门 ---- #

    def test_config_defaults_and_the_explicit_false_rule(self) -> None:
        service = self.make_service({'works': {}})
        self.assertEqual(
            service.works_config(),
            {'enabled': False, 'generation_mode': 'main', 'model_id': ''},
            '缺键按 schema 默认（默认关闭 / main / 空模型）',
        )
        self.assertIn('未启用', service.explain_works_state())
        # 只有显式 false 才算"被关掉"；其他存在的值（含 0 / ''）都不是关闭（坑 36）
        self.assertFalse(self.make_service({'works': {'enabled': False}}).works_enabled())
        self.assertTrue(self.make_service({'works': {'enabled': True}}).works_enabled())
        self.assertTrue(self.make_service({'works': {'enabled': 0}}).works_enabled())
        self.assertTrue(self.make_service({'works': {'enabled': ''}}).works_enabled())
        # 段位完全缺失 = 按默认
        self.assertFalse(self.make_service({}).works_enabled())

    def test_config_reads_both_spellings_and_normalizes_mode(self) -> None:
        service = self.make_service({'works': {'enabled': True, 'generationMode': 'SEPARATE', 'modelId': 'writer-conn'}})
        self.assertEqual(service.works_config()['generation_mode'], 'separate')
        self.assertEqual(service.works_config()['model_id'], 'writer-conn')
        snake = self.make_service({'works': {'enabled': True, 'generation_mode': 'separate', 'model_id': 'x'}})
        self.assertEqual(snake.works_config()['generation_mode'], 'separate')
        self.assertEqual(snake.works_config()['model_id'], 'x')
        # 认不出的模式回落 main（不静默变成别的）
        weird = self.make_service({'works': {'enabled': True, 'generationMode': 'nonsense'}})
        self.assertEqual(weird.works_config()['generation_mode'], 'main')
        self.assertIn('独立写手', self.make_service(works_config(generation_mode='separate')).explain_works_state())

    def test_config_reads_via_bridge_section_when_available(self) -> None:
        service = self.make_service({'works': {'enabled': False}})

        class _WithSection:
            def section(self, name: str) -> dict[str, Any]:
                return {'enabled': True, 'model_id': 'writer-model'} if name == 'works' else {}

        service.section = _WithSection().section
        self.assertTrue(service.works_enabled())
        self.assertEqual(service.works_config()['model_id'], 'writer-model')

    # ---- payload 投影 ---- #

    async def test_shared_work_state_is_none_when_disabled_and_camel_case_when_enabled(self) -> None:
        disabled = self.make_service({'works': {'enabled': False}})
        self.assertIsNone(await disabled.shared_work_state('s', 'p'))
        self.assertIn('共同作品未启用', self.sink.text())

        service = self.make_service()
        self.assertIsNone(await service.shared_work_state('s', 'p'), '没有作品时什么都不注入')
        await service.create_work('s', 'p', '短篇', '原稿')
        state = await service.shared_work_state({'id': 's'}, {'id': 'p'})
        self.assertEqual(
            set(state), {'workId', 'title', 'head', 'content', 'proposals', 'generationMode', 'generationJobs', 'mayPropose'},
        )
        self.assertNotIn('pendingDraft', state, '没有待决草稿时不留键（上游 undefined 被 compactObject 丢掉）')
        self.assertNotIn('lastFailure', state)
        self.assertEqual(state['content'], '原稿')
        self.assertEqual(state['generationMode'], 'main')
        self.assertTrue(state['mayPropose'])
        self.assertEqual(state['workId'], work_key('s', 'p'))

    async def test_shared_work_state_reports_bad_rows_without_overwriting(self) -> None:
        service = self.make_service()
        created = await service.create_work('s', 'p', '短篇', '原稿')
        self.db.update('interlude_work', {'id': created['workId']}, {'state': {'schemaVersion': 3}})
        self.assertIsNone(await service.shared_work_state('s', 'p'))
        self.assertIn('共同作品读取失败', self.sink.text())
        stored = self.db.get('interlude_work', {'id': created['workId']})
        self.assertEqual(stored['state'], {'schemaVersion': 3}, '坏行必须原样保留')

    # ---- 提案保存与 lastFailure ---- #

    async def test_apply_work_proposal_saves_and_is_idempotent(self) -> None:
        service = self.make_service()
        await service.create_work('s', 'p', '短篇', '原稿')
        head = await self.head_of(service)
        edit = {'baseRevisionId': head, 'content': '新正文', 'reason': '保留留白'}
        saved = await service.apply_work_proposal('s', 'p', edit, 42)
        self.assertEqual(saved['status'], 'pending')
        self.assertEqual(saved['author'], 'protagonist')
        self.assertEqual(saved['sourceEntryId'], 42)
        again = await service.apply_work_proposal('s', 'p', edit, 42)
        self.assertEqual(again['id'], saved['id'], '同一个来源条目幂等')
        snapshot = await service.works_snapshot('s', 'p')
        self.assertEqual(len(snapshot['proposals']), 1)
        self.assertEqual((await service.shared_work_state('s', 'p'))['pendingDraft']['id'], saved['id'])
        self.assertIn('共同作品提案已保存', self.sink.text())

    async def test_apply_work_proposal_failure_records_last_failure_and_never_raises(self) -> None:
        service = self.make_service()
        await service.create_work('s', 'p', '短篇', '原稿')
        stale = {'baseRevisionId': 'r-old', 'content': '新正文', 'reason': '过期基础'}
        self.assertIsNone(await service.apply_work_proposal('s', 'p', stale, 9))
        snapshot = await service.works_snapshot('s', 'p')
        self.assertEqual(snapshot['proposals'], [], '失败不写提案')
        self.assertEqual(snapshot['lastFailure']['status'], 'proposal-not-saved')
        self.assertEqual(snapshot['lastFailure']['sourceEntryId'], 9)
        self.assertIsNotNone(parse_dt(snapshot['lastFailure']['at']))
        self.assertIn('共同作品保存提案失败', self.sink.text())
        self.assertIn('warn', self.sink.levels())
        # 坏输入也不抛
        self.assertIsNone(await service.apply_work_proposal('s', 'p', {'content': ''}, 10))
        # 没有归属也不抛
        self.assertIsNone(await service.apply_work_proposal('', '', stale, 11))

    async def test_apply_work_proposal_without_a_work_row_never_raises(self) -> None:
        service = self.make_service()
        self.assertIsNone(await service.apply_work_proposal('s', 'p', {'baseRevisionId': 'r', 'content': 'x', 'reason': 'y'}, 3))
        self.assertIn('请先创建共同作品', self.sink.text())
        self.assertFalse(await service.record_work_failure('s', 'p', 3), '没有作品时留痕也只能是 False')

    async def test_record_work_failure_writes_last_failure_once(self) -> None:
        service = self.make_service()
        await service.create_work('s', 'p', '短篇', '原稿')
        self.assertTrue(await service.record_work_failure('s', 'p', 5))
        snapshot = await service.works_snapshot('s', 'p')
        self.assertEqual(snapshot['lastFailure']['sourceEntryId'], 5)
        # 已经有该条目的提案时不再留痕
        head = await self.head_of(service)
        await service.apply_work_proposal('s', 'p', {'baseRevisionId': head, 'content': 'x', 'reason': 'y'}, 5)
        snapshot = await service.works_snapshot('s', 'p')
        self.assertIsNone(snapshot['lastFailure'], '成功保存后 lastFailure 被清掉')

    async def test_disabled_feature_skips_model_side_entries_with_a_visible_note(self) -> None:
        service = self.make_service({'works': {'enabled': False}, 'model': works_config()['model']})
        self.assertIsNone(await service.apply_work_proposal('s', 'p', {'baseRevisionId': 'r', 'content': 'x', 'reason': 'y'}, 1))
        started = await service.start_work_generation('s', 'p', {'baseRevisionId': 'r', 'brief': 'b'}, 1)
        self.assertFalse(started['ok'])
        self.assertIn('共同作品未启用', self.sink.text())

    # ---- 用户侧出口 ---- #

    async def test_create_edit_accept_reject_and_delete(self) -> None:
        service = self.make_service()
        created = await service.create_work('s', 'p', '短篇', '原稿')
        self.assertTrue(created['ok'])
        duplicate = await service.create_work('s', 'p', '换个标题', '覆盖它')
        self.assertFalse(duplicate['ok'])
        self.assertIn('已有共同作品', duplicate['error'])
        # 模型提一条，用户接受 → 新 revision
        head = await self.head_of(service)
        proposal = await service.apply_work_proposal('s', 'p', {'baseRevisionId': head, 'content': '采纳稿', 'reason': 'r'}, 7)
        accepted = await service.accept_work_proposal(created['workId'], proposal['id'])
        self.assertTrue(accepted['ok'])
        self.assertEqual(accepted['revisions'], 2)
        # 用户手改 → 又是一条新 revision
        head = await self.head_of(service)
        edited = await service.edit_work('s', 'p', {'baseRevisionId': head, 'content': '手改稿', 'reason': '我自己改'})
        self.assertTrue(edited['ok'])
        self.assertEqual(edited['revision']['content'], '手改稿')
        self.assertEqual(edited['revision']['author'], 'user')
        self.assertEqual(edited['revision']['parentId'], accepted['head'])
        # 再提一条并拒绝 → head 不动
        head = await self.head_of(service)
        proposal2 = await service.apply_work_proposal('s', 'p', {'baseRevisionId': head, 'content': '不采纳', 'reason': 'r'}, 8)
        rejected = await service.reject_work_proposal(created['workId'], proposal2['id'])
        self.assertTrue(rejected['ok'])
        self.assertEqual(rejected['head'], head)
        self.assertEqual((await service.works_snapshot('s', 'p'))['head'], head)
        self.assertIn('共同作品提案已接受', self.sink.text())
        self.assertIn('共同作品已手改为新版本', self.sink.text())
        # 删除
        self.assertTrue((await service.delete_work('s', 'p'))['ok'])
        self.assertIsNone(await service.works_snapshot('s', 'p'))

    async def test_accept_reject_failures_are_reported_not_raised(self) -> None:
        service = self.make_service()
        missing = await service.accept_work_proposal('nope', 'p1')
        self.assertFalse(missing['ok'])
        self.assertIn('找不到这件共同作品', missing['error'])
        self.assertIn('找不到这件作品', self.sink.text())
        created = await service.create_work('s', 'p', '短篇', '原稿')
        bad = await service.resolve_work_proposal('s', 'p', 'not-a-proposal', True)
        self.assertFalse(bad['ok'])
        self.assertIn('提案不存在', bad['error'])
        self.assertTrue(created['ok'])

    async def test_edit_work_rejects_a_stale_base_and_keeps_the_newer_revision(self) -> None:
        service = self.make_service()
        created = await service.create_work('s', 'p', '短篇', '原稿')
        head = await self.head_of(service)
        self.assertTrue((await service.edit_work('s', 'p', {'baseRevisionId': head, 'content': 'A', 'reason': 'x'}))['ok'])
        stale = await service.edit_work('s', 'p', {'baseRevisionId': head, 'content': 'B', 'reason': 'x'})
        self.assertFalse(stale['ok'])
        self.assertIn('基础版本已过期', stale['error'])
        snapshot = await service.works_snapshot('s', 'p')
        self.assertEqual(snapshot['revisions'][-1]['content'], 'A', '过期手改绝不覆盖新版本')
        self.assertEqual(len(snapshot['revisions']), 2)

    async def test_works_dump_parts_join_back_to_the_whole_json(self) -> None:
        service = self.make_service()
        self.assertEqual(await service.works_dump('s', 'p'), [], '没有作品时没有可导出的东西')
        await service.create_work('s', 'p', '短篇', '原' + '稿' * 6000)
        parts = await service.works_dump('s', 'p', 2400)
        self.assertGreaterEqual(len(parts), 3)
        self.assertTrue(all(len(part) <= 2400 for part in parts))
        restored = json.loads(''.join(parts))
        snapshot = await service.works_snapshot('s', 'p')
        self.assertEqual(restored['id'], snapshot['workId'])
        self.assertEqual(restored['state']['head'], snapshot['head'])
        self.assertIn('共同作品已导出', self.sink.text())

    async def test_works_snapshot_is_none_when_missing_and_reports_the_active_job_status(self) -> None:
        service = self.make_service()
        self.assertIsNone(await service.works_snapshot('s', 'p'))
        self.assertIsNone(await service.works_snapshot('', ''))
        await service.create_work('s', 'p', '短篇', '原稿')
        snapshot = await service.works_snapshot('s', 'p')
        self.assertEqual(snapshot['revisionLimit'], MAX_REVISIONS)
        self.assertEqual(snapshot['jobs'], [])

    # ---- 写手任务 ---- #

    async def test_start_work_generation_runs_a_separate_writer_and_returns_a_proposal(self) -> None:
        service = self.make_service(works_config(generation_mode='separate'))
        service.narrator = _FakeNarrator()
        await service.create_work('s', 'p', '短篇', '原稿')
        head = await self.head_of(service)
        started = await service.start_work_generation('s', 'p', {'baseRevisionId': head, 'brief': '从窗外雨声开始'}, 1)
        self.assertTrue(started['ok'], started)
        self.assertEqual(started['modelId'], 'main-model', '未指名 → 跟随主叙事连接')
        self.assertEqual(started['generationMode'], 'separate')
        self.assertEqual(service.narrator.calls[0]['task'], '作品创作')
        self.assertEqual(service.narrator.calls[0]['model'], 'main-model')
        writer_system = service.narrator.calls[0]['body']['messages'][0]['content']
        self.assertIn('ONLY', writer_system, '写手提示词只要求成品正文')
        for word in ('saved', 'accepted', 'shared'):
            self.assertIn(word, writer_system)
        # 「不承诺已保存/已接受」是**否定句**：必须在 never / only 之后出现
        self.assertRegex(writer_system, r'never claim that it was saved, accepted or shared')
        self.assertEqual(service.narrator.calls[0]['body']['messages'][1]['content'].count('原稿'), 1)
        snapshot = await service.works_snapshot('s', 'p')
        self.assertEqual(len(snapshot['jobs']), 1)
        job_id = snapshot['jobs'][0]['id']
        for _ in range(400):
            snapshot = await service.works_snapshot('s', 'p')
            if snapshot['proposals']:
                break
            await asyncio.sleep(0.005)
        self.assertEqual(snapshot['jobs'][0]['status'], 'completed')
        self.assertEqual(snapshot['jobs'][0]['proposalId'], snapshot['proposals'][0]['id'])
        self.assertEqual(snapshot['proposals'][0]['content'], '雨滴敲在玻璃上。')
        self.assertEqual(snapshot['proposals'][0]['status'], 'pending')
        self.assertEqual(snapshot['proposals'][0]['author'], 'protagonist')
        self.assertEqual(snapshot['proposals'][0]['operationKey'], 'entry:1')
        self.assertEqual(snapshot['head'], head, '写手不改 head：草案要等用户接受')
        self.assertIn('共同作品写手任务已开始', self.sink.text())
        # 幂等：同一个 operationKey 不再起第二次
        again = await service.start_work_generation('s', 'p', {'baseRevisionId': head, 'brief': '再来'}, 1)
        self.assertEqual(again['job']['id'], job_id)
        self.assertEqual(len(service.narrator.calls), 1)

    async def test_start_work_generation_never_falls_back_when_the_named_model_is_missing(self) -> None:
        service = self.make_service(works_config(generation_mode='separate', model_id='nope'))
        service.narrator = _FakeNarrator()
        await service.create_work('s', 'p', '短篇', '原稿')
        head = await self.head_of(service)
        started = await service.start_work_generation('s', 'p', {'baseRevisionId': head, 'brief': 'b'}, 2)
        self.assertFalse(started['ok'])
        self.assertIn('nope', started['error'])
        self.assertEqual(service.narrator.calls, [], '指名找不到时绝不用别的模型顶上')
        self.assertIn('warn', self.sink.levels())

    async def test_start_work_generation_uses_the_named_connection(self) -> None:
        service = self.make_service(works_config(generation_mode='separate', model_id='writer-conn'))
        service.narrator = _FakeNarrator()
        await service.create_work('s', 'p', '短篇', '原稿')
        head = await self.head_of(service)
        started = await service.start_work_generation('s', 'p', {'baseRevisionId': head, 'brief': 'b'}, 3)
        self.assertTrue(started['ok'], started)
        self.assertEqual(started['modelId'], 'writer-model')
        self.assertEqual(service.narrator.calls[0]['provider']['id'], 'writer-conn')

    async def test_work_generation_status_and_cancel(self) -> None:
        service = self.make_service(works_config(generation_mode='separate'))
        blocked = asyncio.get_running_loop().create_future()

        class _BlockingNarrator(_FakeNarrator):
            async def _side_task_json(self, provider: Any, model: str, task: str, timeout: Any, build_body: Any, parse: Any) -> Any:
                self.calls.append({'provider': provider, 'model': model, 'task': task, 'body': build_body(True)})
                return await blocked

        service.narrator = _BlockingNarrator()
        await service.create_work('s', 'p', '短篇', '原稿')
        head = await self.head_of(service)
        started = await service.start_work_generation('s', 'p', {'baseRevisionId': head, 'brief': 'b'}, 4)
        self.assertTrue(started['ok'])
        status = await service.work_generation_status('s', 'p')
        self.assertEqual(status['jobs'][0]['status'], 'running')
        self.assertTrue((await service.cancel_work_generation('s', 'p', started['job']['id']))['ok'])
        self.assertIn('共同作品写手任务已取消', self.sink.text())
        blocked.set_result('迟到的输出')
        await settle()
        snapshot = await service.works_snapshot('s', 'p')
        self.assertEqual(snapshot['proposals'], [], '取消后的结果必须被丢弃')
        self.assertEqual(snapshot['jobs'][0]['status'], 'cancelled')
        missing = await service.cancel_work_generation('s', 'p', 'nope')
        self.assertFalse(missing['ok'])

    async def test_missing_work_rows_and_bad_keys_are_reported_not_raised(self) -> None:
        service = self.make_service()
        started = await service.start_work_generation('s', 'p', {'baseRevisionId': 'r', 'brief': 'b'}, 1)
        self.assertFalse(started['ok'])
        self.assertIn('请先创建共同作品', started['error'])
        self.assertFalse((await service.edit_work('s', 'p', {'baseRevisionId': 'r', 'content': 'x', 'reason': 'y'}))['ok'])
        self.assertFalse((await service.delete_work('s', 'p'))['ok'])
        self.assertFalse((await service.startup_recover_works()) < 0)

    async def test_startup_recover_works_logs_and_frees_the_gate(self) -> None:
        service = self.make_service()
        await service.create_work('s', 'p', '短篇', '原稿')
        head = await self.head_of(service)
        blocked = asyncio.get_running_loop().create_future()

        class _BlockingNarrator(_FakeNarrator):
            async def _side_task_json(self, provider: Any, model: str, task: str, timeout: Any, build_body: Any, parse: Any) -> Any:
                return await blocked

        service.narrator = _BlockingNarrator()
        await service.start_work_generation('s', 'p', {'baseRevisionId': head, 'brief': 'b'}, 5)
        # 模拟进程重载：新的服务实例（内存 active 为空）看到遗留的 running
        fresh = self.make_service()
        snapshot = await fresh.works_snapshot('s', 'p')
        self.assertEqual(snapshot['jobs'][0]['status'], 'interrupted')
        self.assertFalse((await fresh.shared_work_state('s', 'p'))['mayPropose'])
        self.assertEqual(await fresh.startup_recover_works(), 1)
        self.assertIn('共同作品启动清扫', self.sink.text())
        self.assertTrue((await fresh.shared_work_state('s', 'p'))['mayPropose'])
        blocked.cancel()

    async def test_startup_recover_reports_unreadable_rows_without_touching_them(self) -> None:
        service = self.make_service()
        created = await service.create_work('s', 'p', '短篇', '原稿')
        self.db.update('interlude_work', {'id': created['workId']}, {'state': {'bad': True}})
        self.assertEqual(await service.startup_recover_works(), 0)
        self.assertIn('数据形状不兼容', self.sink.text())
        stored = self.db.get('interlude_work', {'id': created['workId']})
        self.assertEqual(stored['state'], {'bad': True})

    async def test_stop_works_discards_late_results(self) -> None:
        service = self.make_service(works_config(generation_mode='separate'))
        blocked = asyncio.get_running_loop().create_future()

        class _BlockingNarrator(_FakeNarrator):
            async def _side_task_json(self, provider: Any, model: str, task: str, timeout: Any, build_body: Any, parse: Any) -> Any:
                return await blocked

        service.narrator = _BlockingNarrator()
        await service.create_work('s', 'p', '短篇', '原稿')
        head = await self.head_of(service)
        await service.start_work_generation('s', 'p', {'baseRevisionId': head, 'brief': 'b'}, 6)
        service.stop_works()
        blocked.set_result('停止后的输出')
        await settle()
        snapshot = await service.works_snapshot('s', 'p')
        self.assertEqual(snapshot['proposals'], [], '停止后迟到的结果绝不落库')
        self.assertEqual(
            snapshot['jobs'][0]['status'], 'interrupted',
            '停止后服务不再持有实例：库里遗留的 running 读出来就是 interrupted（重启清扫时才标 failed）',
        )

    async def test_store_replace_is_compare_and_swap(self) -> None:
        service = self.make_service()
        created = await service.create_work('s', 'p', '短篇', '原稿')
        store = service.shared_works().store
        row = await store.get(created['workId'])
        # 别处先改了一代
        self.db.update('interlude_work', {'id': created['workId']}, {'generation': 5})
        stale = copy.deepcopy(row)
        stale['generation'] = 0
        stale['state']['title'] = '想覆盖别人'
        self.assertFalse(await store.replace(stale, 0), 'generation 不匹配 → False，绝不覆盖')
        stored = self.db.get('interlude_work', {'id': created['workId']})
        self.assertEqual(stored['state']['title'], '短篇')
        self.assertEqual(int(stored['generation']), 5)
        # 代号对上就成功：写入的代号就是 `row` 里那一代（上游契约：调用方先自增）
        current = await store.get(created['workId'])
        current['generation'] = 6
        self.assertTrue(await store.replace(current, 5))
        stored = self.db.get('interlude_work', {'id': created['workId']})
        self.assertEqual(int(stored['generation']), 6)
        self.assertEqual(stored['state']['title'], '短篇')


class WorkStoreContractTests(unittest.IsolatedAsyncioTestCase):
    """`WorkStore` 的默认成员形状（上游 `list?` / `remove?` 可选成员）。"""

    async def test_optional_members_default_to_none(self) -> None:
        class _Minimal(WorkStore):
            pass

        store = _Minimal()
        self.assertIsNone(store.list)
        self.assertIsNone(store.remove)
        works = SharedWorks(store)
        self.assertEqual(await works.startup_recover(), 0, '没有 list 就什么都不扫')
        with self.assertRaisesRegex(WorksError, '当前存储不支持删除'):
            await works.delete_all('s', 'p')
        for call in (store.get('x'), store.create({}), store.replace({}, 0)):
            with self.assertRaises(NotImplementedError):
                await call


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
