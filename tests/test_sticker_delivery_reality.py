# -*- coding: utf-8 -*-
"""投递失败 → `deliveryReality` 的端到端链（真机：私聊表情包没发出去，剧本却写着"发出去"）。

真机现场：`send_sticker` 撞上 `<ActionFailed status:'failed', retcode:1200 … rich media
transfer failed>`，日志里只有一条 warn；而**下一回合**会不会仍然把这句话当成"已发出"，
取决于下面这条链有没有真的接上：

    send_sticker 失败
      → record_platform_delivery_outcome('failed', reason)
        → update_script_delivery_outcome()：写剧本行 metadata.delivery_actions 的对应片段
          → delivery_reality(下一回合的条目)：`outcome` + 一行 `fact`（状态词 + 原文）

这条链里任何一跳断掉，失败就会变成"未确认"甚至"看起来发了"——所以这里跑**真实实现**
（真 `InterludeService`、真 `Database`、真 `send_sticker`、真 `platform_action_reference`、
真的 `record/update_script_delivery_outcome`、真 `delivery_reality`），只把平台投递替身换成
"必失败 / 必成功"，其余副作用（日志一类）安静掉。

⚠️ **真库的生命周期（照抄 `test_service_chunk1.ServiceHarness`，别省）**

`InterludeService.__init__` 会 `call_soon(start_background_tasks)`，而 `stickers.enabled`
会让它用 `setTimeout(…, 0)` 起一次**没人 await** 的表情库扫描（`service/base.py:1662`），
那次扫描在 `asyncio.to_thread` 里读库。`IsolatedAsyncioTestCase` 的收尾顺序是
「`doCleanups()`（这里关库）→ `Runner.close()`（**这时才** join 线程池）」，
所以"关库清理"会撞上"线程里还在 `sqlite3_step`"——不是断言失败，是 sqlite 在 C 层
use-after-close **直接把整个 discover 段错误掉**（EXIT=139，实测约 1/5）。
因此：**关库之前必须先取消计时器、并把旁路任务推到跑完**（`_stop_service`，注册在
`addCleanup(database.close)` 之后 → LIFO 先跑）。`Database.close()` 自己也在
`_serialized` 名单里兜底（见 `core/database.py`），但测试不该靠兜底活着。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from typing import Any

from plugin.core.database import Database
from plugin.core.script.commit_builder import decision_to_script_commit
from plugin.core.script.delivery_ledger import platform_action_reference
from plugin.core.script.delivery_reality import delivery_reality
from plugin.core.service import InterludeService
from plugin.core.service.base import InterludeContext
from plugin.core.time import utc_now
from plugin.core.turn_persistence import script_entry_draft_for_commit

#: 库里的真实 `assetId` 形状（真机那条就是 `sticker-<16 位 sha256>`）。
ASSET_ID = 'sticker-c5ad6fc27d316504'


class _StickerTransport:
    """只实现 `send_sticker` 的替身：按 `ok` 回成功/失败，并记下平台调用参数。"""

    def __init__(self, ok: bool = True, error: str = '') -> None:
        self.ok = ok
        self.error = error or "<ActionFailed status:'failed', retcode:1200, message:'rich media transfer failed'>"
        self.sticker_calls: list[tuple[str, str, bool]] = []

    async def send_sticker(self, channel_id: str, file_path: str, is_group: bool = False) -> dict:
        self.sticker_calls.append((channel_id, file_path, is_group))
        if self.ok:
            return {'ok': True}
        return {'ok': False, 'error': self.error}


class StickerDeliveryRealityTests(unittest.IsolatedAsyncioTestCase):
    """`send_sticker` 的结局必须进得了投递账本，并出现在下一回合的 payload 里。"""

    def _service(self, tmp: str, transport: _StickerTransport) -> InterludeService:
        database = Database(':memory:')
        database.register_tables()
        self.addCleanup(database.close)
        os.makedirs(os.path.join(tmp, 'stickers'), exist_ok=True)
        service = InterludeService(
            InterludeContext(logger=None, database=database, base_dir=tmp, clock=utc_now),
            {'logging': {'level': 'silent'},
             'stickers': {'enabled': True, 'directory': 'stickers'}},
            database, transport,
        )
        # 只安静掉日志（与账本无关）。
        service.report = lambda *args, **kwargs: None
        service.report_operation = lambda *args, **kwargs: None
        service.report_standalone = lambda *args, **kwargs: None
        service.report_standalone_operation = lambda *args, **kwargs: None
        # ⚠️ 注册在 `database.close` **之后** → cleanup 是 LIFO，所以它先跑：把服务
        # `setTimeout(…, 0)` 起的那次表情库扫描（没人 await，见文件头）推到跑完，
        # 再关库。少这一步就是全量 discover 约 1/5 概率段错误（EXIT=139）。
        self.addCleanup(self._stop_service, service)
        return service

    @staticmethod
    def _background_tasks() -> list[Any]:
        """还挂着的旁路任务（`ensure_future` 出去、调用方不 await 的那些）。

        判据与 `test_service_chunk1.ServiceHarness._pending_sticker_tasks` 逐字同源。
        """
        current = asyncio.current_task()
        found: list[Any] = []
        for task in asyncio.all_tasks():
            if task is current or task.done():
                continue
            coro = task.get_coro() if hasattr(task, 'get_coro') else None
            if 'sticker' in str(getattr(coro, '__qualname__', '')).lower():
                found.append(task)
        return found

    async def _stop_service(self, service: Any) -> None:
        """取消计时器 + **等旁路任务跑完**；做完才轮到关库（见 `_service` 的注释）。"""
        for name in (
            '_sweep_timer', '_compaction_timer', '_blind_mode_timer', '_sticker_scan_timer',
        ):
            timer = getattr(service, name, None)
            if timer is not None:
                try:
                    timer.cancel()
                except Exception:  # pragma: no cover - 句柄已失效
                    pass
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        pending: list[Any] = []
        while loop.time() < deadline:
            pending = self._background_tasks()
            if not pending:
                return
            # 真实让出时间：线程池里那一跳要真的跑完，忙等 `sleep(0)` 不够。
            await asyncio.sleep(0.01)
        self.fail('后台任务没有在预期时间内结束：%r' % (pending,))

    async def _run_turn(self, transport: _StickerTransport):
        """一个完整的私聊回合：真实 commit → 真实剧本行 → 真实投递 → 真实记账。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        service = self._service(tmp.name, transport)
        commit = decision_to_script_commit({
            'story_id': 's',
            'participant_id': 'alice',
            'phase': 'user-message',
            'from': utc_now(),
            'now': utc_now(),
            'message_separator': '<sep/>',
            'split_reply_messages': True,
            'decision': {
                'script': '她把那张图甩过来，尾巴得意地翘着。',
                'interaction': {'seen': True, 'reply': {'mode': 'none'}},
                'local_media': {'assetId': ASSET_ID, 'placement': 'standalone', 'willingness': 0.72},
            },
        })
        draft = _script_entry_draft(commit)
        entry = await service.append_entry('s', draft, utc_now(), 'alice')
        reference = platform_action_reference(commit, entry['id'], 'local-media', ASSET_ID)
        self.assertIsNotNone(reference, '平台行动片段没定位到 → 账本根本不会记这一笔')
        delivered = await service.send_sticker(
            {'id': 's'}, {'platform': 'onebot'}, '1000008890',
            {'assetId': ASSET_ID, 'filePath': 'collected/x.png', 'description': '得意'},
            # 私聊回合：chunk3 传的 group_id 是 None（这正是不该落成群的会话类型）。
            None, reference,
        )
        rows = await service.db_get(
            'interlude_script_entry', {'storyId': 's'},
            {'limit': 20, 'sort': {'occurredAt': 'desc'}},
        )
        return service, delivered, rows, reference

    async def test_failed_sticker_delivery_marks_the_segment_failed_and_the_next_turn_says_so(self):
        transport = _StickerTransport(ok=False)
        _service_, delivered, rows, reference = await self._run_turn(transport)

        self.assertFalse(delivered)
        # 投递参数：私聊（`is_group=False`）+ 表情库里的相对路径。
        (channel, file_path, is_group) = transport.sticker_calls[0]
        self.assertEqual(channel, '1000008890')
        self.assertFalse(is_group, '私聊回合的媒体投递不许标成群')
        self.assertTrue(file_path.endswith(os.path.join('collected', 'x.png')), file_path)

        script_row = next(row for row in rows if row['kind'] == 'script')
        actions = script_row['metadata']['delivery_actions']
        action = next(item for item in actions if item['event_id'] == reference['event_id'])
        (segment,) = action['segments']
        self.assertEqual(segment['kind'], 'local-media')
        self.assertEqual(segment['content'], ASSET_ID)
        self.assertEqual(segment['status'], 'failed', '失败必须落成 failed，而不是停在 pending')
        self.assertIn('rich media transfer failed', segment['reason'])
        self.assertEqual(action['status'], 'failed')

        # 下一回合的 payload（`ongoingThreads.deliveryReality` 就是这个函数的返回值）。
        payload = delivery_reality(rows, 'alice')
        self.assertEqual(len(payload), 1, payload)
        self.assertEqual(payload[0]['eventId'], reference['event_id'])
        (seg,) = payload[0]['segments']
        self.assertEqual(seg['outcome'], 'delivery-not-confirmed-after-error')
        self.assertIn('未确认送达', seg['fact'])
        self.assertNotIn('已送达', seg['fact'], '没发出去的表情包不许被说成已送达')
        self.assertIn(ASSET_ID, seg['fact'], '那一行必须带原文，她才能决定补发还是作罢')

    async def test_delivered_sticker_leaves_no_open_question(self):
        transport = _StickerTransport(ok=True)
        _service_, delivered, rows, reference = await self._run_turn(transport)

        self.assertTrue(delivered)
        script_row = next(row for row in rows if row['kind'] == 'script')
        action = next(item for item in script_row['metadata']['delivery_actions']
                      if item['event_id'] == reference['event_id'])
        self.assertEqual(action['status'], 'delivered')
        self.assertEqual(action['segments'][0]['status'], 'delivered')
        # 全部已送达的行动不进上下文（上游口径：只报告还没确认的）。
        self.assertEqual(delivery_reality(rows, 'alice'), [])

    async def test_an_unresolvable_reference_records_nothing_and_never_claims_delivery(self):
        """反向：素材 id 对不上剧本片段时**不记这一笔**（但也不许说成已送达）。

        这条钉的是"没有记录"与"已送达"是两回事：片段停在 `pending`，下一回合照样报
        `未确认送达`。真实路径上 `assetId` 由 `chunk2.resolve_sticker()` 写回同一份草稿，
        两边逐字一致（`sticker-sticker-…` 那种双前缀正是让模型自己"顺手改拼写"的诱因）。
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        transport = _StickerTransport(ok=False)
        service = self._service(tmp.name, transport)
        commit = decision_to_script_commit({
            'story_id': 's', 'participant_id': 'alice', 'phase': 'user-message',
            'from': utc_now(), 'now': utc_now(), 'message_separator': '<sep/>',
            'split_reply_messages': True,
            'decision': {
                'script': '她把那张图甩过来。',
                'interaction': {'seen': True, 'reply': {'mode': 'none'}},
                'local_media': {'assetId': ASSET_ID, 'placement': 'standalone', 'willingness': 0.72},
            },
        })
        entry = await service.append_entry('s', _script_entry_draft(commit), utc_now(), 'alice')
        # 投递侧拿到的是另一个 id（模型"顺手改"了拼写）：定位不到片段。
        reference = platform_action_reference(commit, entry['id'], 'local-media', 'sticker-改过的id')
        self.assertIsNone(reference)
        delivered = await service.send_sticker(
            {'id': 's'}, {'platform': 'onebot'}, '1000008890',
            {'assetId': 'sticker-改过的id', 'filePath': 'collected/x.png'},
            None, reference,
        )
        self.assertFalse(delivered)
        rows = await service.db_get('interlude_script_entry', {'storyId': 's'}, {'limit': 20})
        script_row = next(row for row in rows if row['kind'] == 'script')
        action_event = next(item for item in commit['events'] if item['kind'] == 'platform-action')
        action = next(item for item in script_row['metadata']['delivery_actions']
                      if item['event_id'] == action_event['event_id'])
        self.assertEqual(action['segments'][0]['status'], 'pending', '定位不到就不能乱记')
        payload = delivery_reality(rows, 'alice')
        self.assertEqual(payload[0]['segments'][0]['outcome'], 'not-confirmed')
        self.assertNotIn('已送达', payload[0]['segments'][0]['fact'])


def _script_entry_draft(commit):
    """生产写入方的剧本行草稿（`turn_persistence.script_entry_draft_for_commit`）。"""
    return script_entry_draft_for_commit(commit, {'seen': True, 'reply': {'mode': 'none'}})


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
