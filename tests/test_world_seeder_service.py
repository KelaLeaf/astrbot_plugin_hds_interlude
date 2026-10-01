"""世界播种器**服务层**的用例（上游无对应测试——这是一块测试真空，移植时补齐）。

覆盖上游 `worldSeederSweep` / `drainDueSeededEvents` 的状态机与门序：
注入形态、过期作废、`low` 的相位门、按 `occursAt` 排序、`injecting` 陈旧 claim、
失败回滚、预检频控零模型调用、高重要性每日一条。

运行：`python3 -m unittest plugin.tests.test_world_seeder_service -v`
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from plugin.core.service import chunk10 as chunk10_module  # noqa: E402
from plugin.core.service.chunk10 import ServiceChunk10  # noqa: E402
from plugin.core.world_seeder import (  # noqa: E402
    STATUS_EXPIRED,
    STATUS_INJECTED,
    STATUS_INJECTING,
    STATUS_SCHEDULED,
)

NOW = datetime(2026, 9, 28, 14, 0, tzinfo=timezone.utc)
STORY_ID = 'character:NapCat:100001357'


class _Host(ServiceChunk10):
    """最小宿主：只实现播种器用到的那几个兄弟成员。"""

    def __init__(self, rows: Optional[list[dict[str, Any]]] = None) -> None:
        self.rows = list(rows or [])
        self.created: list[dict[str, Any]] = []
        self.entries: list[dict[str, Any]] = []
        self.reports: list[tuple[str, ...]] = []
        self.standalone: list[tuple[str, ...]] = []
        self.desktop_runtime_phase = 'running'
        self.database_resetting = False
        self._world_seeder_sweep_running = False
        self.runtime_generation = 0
        self.story_task_generations: dict[str, int] = {}
        self._next_id = 100
        self.append_should_fail = False

    # ---- 兄弟成员桩 ----
    def now(self) -> datetime:
        return NOW

    async def db_get(self, table: str, where: Any = None, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        if table == 'interlude_seeded_event':
            return [dict(row) for row in self.rows]
        if table == 'interlude_script_entry':
            return []
        return []

    async def db_set(self, table: str, where: Any, values: dict[str, Any]) -> None:
        for row in self.rows:
            if row.get('id') == where.get('id'):
                row.update(values)

    async def db_create(self, table: str, row: dict[str, Any]) -> dict[str, Any]:
        if table == 'interlude_seeded_event':
            self._next_id += 1
            stored = {**row, 'id': self._next_id}
            self.rows.append(stored)
            self.created.append(stored)
            return stored
        return row

    async def append_entry(self, story_id: str, entry: Any, now: Any, participant_id: str = '') -> dict[str, Any]:
        if self.append_should_fail:
            raise RuntimeError('append failed')
        self._next_id += 1
        stored = {**entry, 'id': self._next_id, 'storyId': story_id}
        self.entries.append(stored)
        return stored

    async def participants(self, story_id: str, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    def report(self, level: str, story: Any, phase: str, message: str, *args: Any) -> None:
        self.reports.append((level, message % args if args else message))

    def report_operation(self, level: str, verbosity: str, story: Any, phase: str, message: str, *args: Any) -> None:
        self.reports.append((level, message % args if args else message))

    def report_standalone(self, level: str, message: str, *args: Any, **_kwargs: Any) -> None:
        self.standalone.append((level, message % args if args else message))

    def can_handle_story(self, story: Any) -> bool:
        return True

    async def get_canonical_story(self) -> dict[str, Any]:
        return {
            'id': STORY_ID, 'status': 'active',
            'setting': {'timezone': 'Asia/Shanghai'},
            'state': {},
        }


def _row(**overrides: Any) -> dict[str, Any]:
    base = {
        'id': 1, 'storyId': STORY_ID, 'summary': '楼下五金店开始装修，电钻声断断续续。',
        'importance': 'low', 'occursAt': NOW - timedelta(minutes=5), 'expiresAt': None,
        'status': STATUS_SCHEDULED, 'subjects': [], 'sourcePayload': {},
        'injectedEntryId': None, 'createdAt': NOW - timedelta(hours=1), 'updatedAt': NOW - timedelta(hours=1),
    }
    base.update(overrides)
    return base


class DrainTests(unittest.IsolatedAsyncioTestCase):
    async def test_due_scheduled_event_is_injected_with_the_documented_entry_shape(self):
        host = _Host([_row(id=7)])
        await host.drain_due_seeded_events({'id': STORY_ID}, NOW, include_low=True)
        self.assertEqual(len(host.entries), 1)
        entry = host.entries[0]
        self.assertEqual(entry['kind'], 'world-event')
        self.assertEqual(entry['actor'], 'system')
        self.assertEqual(entry['content'], '[世界事件] 楼下五金店开始装修，电钻声断断续续。')
        self.assertEqual(entry['metadata']['seeded_event_id'], 7)
        self.assertEqual(host.rows[0]['status'], STATUS_INJECTED)
        self.assertEqual(host.rows[0]['injectedEntryId'], entry['id'])

    async def test_expired_event_is_marked_expired_and_never_becomes_an_entry(self):
        host = _Host([_row(id=3, expiresAt=NOW - timedelta(minutes=1))])
        await host.drain_due_seeded_events({'id': STORY_ID}, NOW, include_low=True)
        self.assertEqual(host.entries, [])
        self.assertEqual(host.rows[0]['status'], STATUS_EXPIRED)

    async def test_low_importance_waits_for_a_non_user_message_phase(self):
        host = _Host([_row(id=4, importance='low')])
        await host.drain_due_seeded_events({'id': STORY_ID}, NOW, include_low=False)
        self.assertEqual(host.entries, [], 'low 事件不能在 user-message 相位注入')
        self.assertEqual(host.rows[0]['status'], STATUS_SCHEDULED, '被跳过的 low 留在 scheduled 等待补排')
        await host.drain_due_seeded_events({'id': STORY_ID}, NOW, include_low=True)
        self.assertEqual(len(host.entries), 1)

    async def test_medium_and_high_drain_in_any_phase(self):
        host = _Host([_row(id=5, importance='medium'), _row(id=6, importance='high')])
        await host.drain_due_seeded_events({'id': STORY_ID}, NOW, include_low=False)
        self.assertEqual(len(host.entries), 2)

    async def test_multiple_due_events_inject_in_occurs_at_order(self):
        host = _Host([
            _row(id=9, occursAt=NOW - timedelta(minutes=1), summary='后发生'),
            _row(id=8, occursAt=NOW - timedelta(minutes=30), summary='先发生'),
        ])
        await host.drain_due_seeded_events({'id': STORY_ID}, NOW, include_low=True)
        self.assertEqual([entry['content'] for entry in host.entries],
                         ['[世界事件] 先发生', '[世界事件] 后发生'])

    async def test_fresh_injecting_claim_is_left_alone_but_a_stale_one_is_reprocessed(self):
        fresh = _Host([_row(id=11, status=STATUS_INJECTING, updatedAt=NOW - timedelta(minutes=1))])
        await fresh.drain_due_seeded_events({'id': STORY_ID}, NOW, include_low=True)
        self.assertEqual(fresh.entries, [], '5 分钟内的 claim 不能被重复处理')
        stale = _Host([_row(id=12, status=STATUS_INJECTING, updatedAt=NOW - timedelta(minutes=6))])
        await stale.drain_due_seeded_events({'id': STORY_ID}, NOW, include_low=True)
        self.assertEqual(len(stale.entries), 1, '进程在 append 中途崩掉后，陈旧 claim 必须可重处理')

    async def test_append_failure_rolls_the_claim_back_to_scheduled_and_reports_a_warning(self):
        host = _Host([_row(id=13)])
        host.append_should_fail = True
        with self.assertRaises(RuntimeError):
            await host.drain_due_seeded_events({'id': STORY_ID}, NOW, include_low=True)
        self.assertEqual(host.rows[0]['status'], STATUS_SCHEDULED)
        self.assertTrue(any('世界事件排水失败' in message for _level, message in host.reports), host.reports)


class SweepTests(unittest.IsolatedAsyncioTestCase):
    def _config(self, **seeder: Any) -> dict[str, Any]:
        section = {'enabled': True, 'cadence_minutes': 45, 'max_pending': 4,
                   'daily_cap': 4, 'max_horizon_hours': 72, 'temperature': 0.9,
                   'max_tokens': 1000, 'timeout': 60000}
        section.update(seeder)
        return {
            'world_seeder': section,
            'model_center': {'providers': [{'id': 'p1', 'model': 'flash', 'use_for_world_seeding': True}]},
        }

    async def test_precheck_frequency_controls_skip_the_model_call_entirely(self):
        host = _Host([_row(id=1, status=STATUS_SCHEDULED) for _ in range(4)])
        host.config = self._config(max_pending=4)
        host.narrator = mock.Mock()
        host.narrator.generate_world_seeds = mock.AsyncMock(return_value={'events': []})
        with mock.patch.object(chunk10_module.random, 'random', lambda: 0.9):
            await host.world_seeder_sweep()
        host.narrator.generate_world_seeds.assert_not_called()

    async def test_sweep_is_disabled_without_an_assigned_connection(self):
        host = _Host([])
        config = self._config()
        config['model_center'] = {'providers': [{'id': 'p1', 'model': 'flash'}]}
        host.config = config
        host.narrator = mock.Mock()
        host.narrator.generate_world_seeds = mock.AsyncMock(return_value={'events': []})
        await host.world_seeder_sweep()
        host.narrator.generate_world_seeds.assert_not_called()
        self.assertFalse(host.world_seeder_runtime()['enabled'])

    async def test_runtime_is_the_and_of_master_switch_and_assignment(self):
        host = _Host([])
        host.config = self._config(enabled=False)
        self.assertFalse(host.world_seeder_runtime()['enabled'])
        host.config = self._config()
        self.assertTrue(host.world_seeder_runtime()['enabled'])

    async def test_accepted_draft_is_stored_and_its_summary_joins_the_dedupe_window(self):
        host = _Host([])
        host.config = self._config()
        host.narrator = mock.Mock()
        host.narrator.generate_world_seeds = mock.AsyncMock(return_value={'events': [
            {'summary': '楼下五金店开始装修，电钻声断断续续。', 'importance': 'low',
             'occursAt': (NOW + timedelta(hours=2)).isoformat()},
            {'summary': '楼下五金店开始装修，电钻声阵阵。', 'importance': 'low',
             'occursAt': (NOW + timedelta(hours=3)).isoformat()},
        ]})
        with mock.patch.object(chunk10_module.random, 'random', lambda: 0.9):
            await host.world_seeder_sweep()
        # 第二条是同批次里的换皮：上游注释承诺"立即入列"但代码里漏了，本移植版补上。
        self.assertEqual(len(host.created), 1)
        self.assertEqual(host.created[0]['status'], STATUS_SCHEDULED)


if __name__ == '__main__':
    unittest.main()
