# -*- coding: utf-8 -*-
"""异步动作「完成即回报」的回归面（v1.9.9，见 `docs/PORTING_NOTES.md` §87）。

用户点名的架构级要求：**凡异步发生的事（发消息 / 表情包 / 图片 / 语音、平台动作、
搜索 / 看网页、QZone 操作、定时任务）都必须在结果一落地就唤醒她并注入**，而不是等
下一个用户消息。这里钉的是那条链：

    异步动作完成
      → 三态 + 事实（判据一处：`script/completion_report.py` 从投递账本派生）
        → 节流（同因同会话只回报一次，日志说得出"因为 X 已回报过"）
          → 待回报事实（`interlude_intent` 的 `completion-report`）
            → **立即唤醒**（`schedule_due_intent_wake`，与浏览意图同一条通道）
              → 下一回合她可见的上下文（`dueIntents[].summary` + `deliveryReality[].fact`）

跑的是**真实实现**：真 `InterludeService`、真 `Database`、真投递漏斗
（`send_sticker` / `record_platform_delivery_outcome` / `record_outgoing_delivery_failure`）、
真平台动作留痕（`chunk12._record_platform_actions`）、真浏览（`Transport.search_web` /
`visit_web`）、真 `to_prompt_payload`。只把"她"（叙事模型）换成记录请求的替身。

⚠️ 库的生命周期照抄 `test_sticker_delivery_reality.py`（`addCleanup(database.close)`
之后注册停机清理，先取消计时器再关库）：少这一步，旁路任务会在 C 层撞上已关闭的
sqlite（全量 discover 段错误，坑 83）。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from plugin.core.database import Database
from plugin.core.delivery import attach_message_event, prepare_outgoing_delivery
from plugin.core.narrator_prompts import to_prompt_payload
from plugin.core.script.commit_builder import decision_to_script_commit, find_outgoing_script_event
from plugin.core.script.completion_report import (
    completion_state,
    completion_summary,
    ledger_completion,
    platform_action_state,
)
from plugin.core.script.delivery_ledger import (
    create_script_delivery_actions,
    platform_action_reference,
    update_script_delivery_actions,
)
from plugin.core.service import InterludeContext, InterludeService
from plugin.core.service import chunk5 as chunk5_module
from plugin.core.service.transport import NullTransport
from plugin.core.story_state import encode_story_state
from plugin.core.turn_persistence import script_entry_draft_for_commit
from plugin.core.types import empty_story_state

UTC = timezone.utc
NOW = datetime(2026, 9, 7, 4, 0, tzinfo=UTC)
FROM = NOW - timedelta(minutes=5)
SECOND = timedelta(seconds=1)

STORY_ID = 'character:test:1'
PARTICIPANT_ID = 'test:1:2'

#: 库里的真实 `assetId` 形状（真机那条就是 `sticker-<16 位 sha256>`）。
ASSET_ID = 'sticker-c5ad6fc27d316504'

#: 假页面正文：断言"取回的内容真的进了下一回合的 webContext"。
_PAGE_EXCERPT = '他妹妹叫小雨，今年刚上高一。'

_REPLY = {'seen': True, 'reply': {'mode': 'immediate', 'content': '唔，你还有个妹妹啊，搜着呢'}}

#: 媒体投递替身回的那条错误（真机原文）：既当失败原因，也当节流的"同因"。
_STICKER_ERROR = ("<ActionFailed status:'failed', retcode:1200, "
                  "message:'rich media transfer failed'>")


# =========================================================================== #
# 三态判据（纯逻辑，不起服务）
# =========================================================================== #

class CompletionStateTests(unittest.TestCase):
    """判据只有一处：结果词 → 三态；措辞只有一处：`delivery_reality` 的逐段 fact。"""

    def test_three_states_are_derived_from_the_ledger_outcome_vocabulary(self):
        # `delivery_reality._segment_outcome()` 的四种结果词。
        self.assertEqual(completion_state(['delivered']), 'delivered')
        self.assertEqual(completion_state(['not-confirmed']), 'unknown')
        self.assertEqual(completion_state(['delivery-not-confirmed-after-error']), 'failed')
        self.assertEqual(completion_state(['cancelled']), 'failed')
        # 混合：只要有"回执缺失"，整体就不是可结算的结论；全是终态则失败优先。
        self.assertEqual(completion_state(['delivered', 'not-confirmed']), 'unknown')
        self.assertEqual(completion_state(['delivered', 'delivery-not-confirmed-after-error']), 'failed')
        self.assertEqual(completion_state(['cancelled', 'pending']), 'unknown')
        # 执行侧自己的词（浏览观察 / 平台动作）走同一张表。
        self.assertEqual(completion_state(['success']), 'delivered')
        self.assertEqual(completion_state(['blocked']), 'unknown')

    def test_unrecognised_or_missing_outcome_is_unknown_and_never_success(self):
        """反向：认不出的结果词、空结果**绝不许**被猜成成功。"""
        for value in ([], None, ['whatever'], [''] , ['delivered', 'nonsense']):
            self.assertNotEqual(completion_state(value), 'delivered', value)
        self.assertEqual(completion_state([]), 'unknown')
        self.assertEqual(completion_state(['nonsense']), 'unknown')

    def test_summary_says_not_sent_for_failures_and_unsure_for_missing_receipts(self):
        failed = completion_summary('failed', ['未确认送达（出错）：sticker-x'])
        self.assertTrue(failed.startswith('没发出去'), failed)
        self.assertIn('sticker-x', failed, '失败那一行必须带原文，她才能决定补发还是作罢')
        unknown = completion_summary('unknown', ['未确认送达：晚安'])
        self.assertTrue(unknown.startswith('结果不确定'), unknown)
        for text in (failed, unknown):
            self.assertNotIn('已送达', text)
        # 成功不加状态词前缀：逐段事实本身就是结论。
        self.assertEqual(completion_summary('delivered', ['他妹妹叫小雨。']), '他妹妹叫小雨。')
        self.assertEqual(completion_summary('delivered', []), '已送达')

    def test_ledger_completion_reads_the_ledger_not_the_callers_status(self):
        """账本才是真相：迟到的失败不得把已送达说成没发出去。"""
        commit = _media_commit()
        actions = create_script_delivery_actions(commit)
        entry = {'id': 7, 'kind': 'script', 'metadata': {
            'commitId': commit['commit_id'], 'delivery_actions': actions,
        }}
        action = next(item for item in actions if item['event_kind'] == 'platform-action')
        media = next(seg for seg in action['segments'] if seg['kind'] == 'local-media')
        reference = {
            'commit_id': commit['commit_id'], 'event_id': action['event_id'],
            'script_entry_id': 7, 'segment_index': media['index'],
        }
        failed = update_script_delivery_actions(actions, reference, 'failed', NOW, 'rich media transfer failed')
        self.assertIsNotNone(failed)
        entry['metadata']['delivery_actions'] = failed
        report = ledger_completion(entry, commit['commit_id'], action['event_id'], PARTICIPANT_ID)
        self.assertEqual(report['state'], 'failed')
        self.assertEqual(len(report['facts']), 1)
        self.assertIn('未确认送达（出错）', report['facts'][0])
        self.assertIn(ASSET_ID, report['facts'][0])
        # 反向：全部送达的行动不进上下文，也就没有可回报的事实。
        delivered = update_script_delivery_actions(failed, reference, 'delivered', NOW)
        entry['metadata']['delivery_actions'] = delivered
        self.assertEqual(
            ledger_completion(entry, commit['commit_id'], action['event_id'], PARTICIPANT_ID),
            {'state': 'delivered', 'facts': [], 'outcomes': []},
        )

    def test_platform_action_state_never_calls_a_silent_failure_a_success(self):
        self.assertEqual(platform_action_state([{'action': 'a', 'ok': True}]), 'delivered')
        self.assertEqual(
            platform_action_state([{'action': 'a', 'ok': False, 'error': 'no-permission'}]), 'failed',
        )
        # 没给原因 = 不知道它到底有没有发生（回执缺失），不是成功。
        self.assertEqual(platform_action_state([{'action': 'a', 'ok': False, 'error': ''}]), 'unknown')
        self.assertEqual(platform_action_state([{'action': 'a', 'ok': True}, {
            'action': 'b', 'ok': False, 'error': 'x',
        }]), 'failed')


# =========================================================================== #
# 端到端：动作完成 → 无新用户消息 → 唤醒 → 下一回合上下文
# =========================================================================== #

class _FakeNarrator:
    """只记录请求并回一份最小合法决策的主叙事替身。"""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def decide(self, request: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(request)
        return {'script': '她低头看了一眼手机。', 'interaction': dict(_REPLY)}


class _Sink:
    """把分层日志收进内存（断言节流文案，避免输出噪音）。"""

    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def __call__(self, level: str, text: str) -> None:
        self.records.append((level, text))

    def text(self) -> str:
        return '\n'.join(text for _level, text in self.records)


class _StickerTransport(NullTransport):
    """媒体投递替身：按 `ok` 回成功/失败（形状照 `test_sticker_delivery_reality`）。"""

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[tuple[str, str, bool]] = []

    async def send_sticker(self, channel_id: str, file_path: str, is_group: bool = False) -> dict:
        self.calls.append((channel_id, file_path, is_group))
        if self.ok:
            return {'ok': True}
        return {'ok': False, 'error': _STICKER_ERROR}

    async def send_private(self, participant: Any, content: str, reply_to: Any = None,
                           voice: bool = False) -> dict:
        self.calls.append(('private', content, False))
        return {'ok': self.ok, 'error': None if self.ok else 'transport-unavailable'}

    async def send_session(self, session: Any, content: str, voice: bool = False) -> dict:
        self.calls.append(('session', content, False))
        return {'ok': self.ok, 'error': None if self.ok else 'transport-unavailable'}


class _BrowseTransport(NullTransport):
    """浏览替身：`page` 为假时"通道没有返回内容"（生产里的 failed 分支）。"""

    def __init__(self, page: bool = True) -> None:
        self.page = page
        self.calls: list[tuple[str, Any]] = []

    async def search_web(self, query: str, timeout_ms: int) -> list[dict[str, Any]]:
        self.calls.append(('search', query))
        return [{'url': 'https://example.com/a', 'title': '搜索结果', 'text': _PAGE_EXCERPT * 3}]

    async def visit_web(self, url: str, timeout_ms: int) -> Any:
        self.calls.append(('visit', url))
        if not self.page:
            return None
        return {'url': url, 'title': '页面标题', 'text': _PAGE_EXCERPT * 3}


def _service_config() -> dict[str, Any]:
    return {
        'runtime': {
            'maxScriptCharacters': 4_000, 'maxMessageCharacters': 3_000,
            'messageSeparator': '<sep/>', 'splitReplyMessages': True,
            'allowProactiveMessages': True, 'contextEntryLimit': 20,
            'contextTimeWindowMinutes': 60, 'minimumAdvanceMinutes': 5,
            'sweepIntervalMinutes': 5,
            # 输入状态引擎（chunk12）与本次回归无关：关掉，免得替身的日志混进来。
            'input_status': {'enabled': False},
        },
        'memory': {'enabled': False},
        'sharedStory': {'participantContextLimit': 4, 'maxCrossConversationActions': 2},
        'agency': {'enabled': False},
        'browser': {
            'enabled': True, 'allowSearch': True, 'allowVisit': True,
            'searchUrlTemplate': 'https://cn.bing.com/search?q={query}',
            'mode': 'deferred-only', 'maxObservationsInPrompt': 2,
        },
        'stickers': {'directory': 'stickers'},
        'alterSystem': {'enabled': False},
        'schedulePreplan': {'enabled': False},
        'timelineDirector': {'enabled': False},
        'logging': {'level': 'info'},
    }


def _media_commit(**overrides: Any) -> dict[str, Any]:
    decision: dict[str, Any] = {
        'script': '她把那张图甩过来，尾巴得意地翘着。',
        'interaction': {'seen': True, 'reply': {'mode': 'none'}},
        'local_media': {'assetId': ASSET_ID, 'placement': 'standalone', 'willingness': 0.72},
    }
    decision.update(overrides)
    return decision_to_script_commit({
        'story_id': STORY_ID, 'participant_id': PARTICIPANT_ID, 'phase': 'user-message',
        'from': FROM, 'now': NOW, 'message_separator': '<sep/>', 'split_reply_messages': True,
        'decision': decision,
    })


class AsyncCompletionReportTests(unittest.IsolatedAsyncioTestCase):
    """① 每一类一条端到端 + ②失败 + ③不可知 + ④成功不刷噪音 + ⑤节流。"""

    def setUp(self) -> None:
        from plugin.core import logging as interlude_logging

        self.sink = _Sink()
        interlude_logging.set_log_sink(self.sink)
        self.addCleanup(interlude_logging.set_log_sink, interlude_logging._default_sink)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.makedirs(os.path.join(self.tmp.name, 'stickers'), exist_ok=True)
        self.db = Database(':memory:')
        self.addCleanup(self.db.close)
        self.db.register_tables()
        self.ctx = InterludeContext(
            logger=None, database=self.db, base_dir=self.tmp.name, clock=lambda: NOW,
        )
        self.transport = _StickerTransport(ok=True)
        self.service = InterludeService(
            self.ctx, _service_config(), self.db, self.transport,
        )
        self.service.report = lambda *args, **kwargs: None
        self.addCleanup(self._stop_service, self.service)
        self.narrator = _FakeNarrator()
        self.service.narrator = self.narrator
        # 记下每一次 `ctx.set_timeout` 的回调：用例里要"手动让计时器到点"，而真计时器
        # （1 秒）在毫秒级用例里不该自己响。到点那一刻跑的必须是**真闭包**
        # （`schedule_due_intent_wake.wake`，含它收尾的补排），不是复刻品。
        self.timers: list[tuple[Any, Any]] = []
        real_set_timeout = self.ctx.set_timeout

        def recording_set_timeout(callback: Any, delay_ms: float) -> Any:
            handle = real_set_timeout(callback, delay_ms)
            self.timers.append((handle, callback))
            return handle

        self.ctx.set_timeout = recording_set_timeout
        self.db.insert('interlude_story', {
            'id': STORY_ID, 'platform': 'test', 'selfId': '1', 'userId': '1',
            'channelId': 'private:1', 'status': 'active',
            'setting': {'character': {'name': '凌梦', 'profile': ''},
                        'user': {'displayName': 'Kela', 'profile': ''},
                        'timezone': 'Asia/Shanghai'},
            'state': encode_story_state(empty_story_state()),
            'cursorAt': FROM, 'createdAt': FROM, 'updatedAt': FROM,
        })
        self.db.insert('interlude_participant', {
            'id': PARTICIPANT_ID, 'storyId': STORY_ID, 'platform': 'test', 'selfId': '1',
            'userId': '2', 'channelId': 'private:2', 'personId': 'person:2',
            'displayName': 'Kela', 'profile': '', 'relationship': '',
            'state': {'openThreads': [], 'relationshipNotes': []}, 'status': 'active',
            'createdAt': FROM, 'updatedAt': FROM,
        })

    async def _stop_service(self, service: Any) -> None:
        """先取消计时器、等旁路任务跑完，再轮到关库（见文件头与坑 83）。"""
        for name in ('_sweep_timer', '_compaction_timer', '_blind_mode_timer', '_sticker_scan_timer'):
            timer = getattr(service, name, None)
            if timer is not None:
                try:
                    timer.cancel()
                except Exception:  # pragma: no cover - 句柄已失效
                    pass
        for handle in list(service.due_intent_wake_timers.values()):
            cancel = handle.get('cancel') if isinstance(handle, dict) else None
            if callable(cancel):
                cancel()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        while loop.time() < deadline:
            pending = [task for task in asyncio.all_tasks()
                       if task is not asyncio.current_task() and not task.done()]
            if not pending:
                return
            await asyncio.sleep(0.01)
        self.fail('后台任务没有在预期时间内结束')

    # -- 夹具小工具 -------------------------------------------------------- #

    async def fire_wake(self, at: datetime) -> Any:
        """让到点的那一刻真的发生：跑 `schedule_due_intent_wake` 的 `wake` 闭包。

        真机上是 `ctx.setTimeout` 到点；这里把已排的句柄摘下来（真回调自己会 pop）、
        掐掉真计时器，然后调**同一个闭包**——所以 `sweep()`、`deliver_due_split_segments()`、
        以及收尾的补排都是真跑，`advance_story` 一个都没被绕过。
        """
        record = self.service.due_intent_wake_timers.pop(STORY_ID, None)
        self.assertIsNotNone(record, '没有排好的唤醒（"完成即唤醒"那一跳没发生？）')
        handle = record.cancel
        callback = next((cb for registered, cb in self.timers if registered is handle), None)
        self.assertIsNotNone(callback, '没拿到真正的唤醒回调')
        handle.cancel()
        self.ctx.clock = lambda: at
        before = set(asyncio.all_tasks())
        callback()
        # 只等这次回调自己派生的任务（`ensure_future(run())` 以及它派生的）。
        for _ in range(60):
            fresh = [task for task in asyncio.all_tasks() if task not in before and not task.done()]
            if not fresh:
                break
            await asyncio.gather(*fresh, return_exceptions=True)
        return self.narrator.requests[-1] if self.narrator.requests else None

    def reports(self, kind: Optional[str] = None) -> list[dict[str, Any]]:
        rows = [dict(row) for row in self.db.all(
            'interlude_intent', {'storyId': STORY_ID, 'type': 'completion-report'},
        )]
        if kind is None:
            return rows
        return [row for row in rows if (row.get('payload') or {}).get('kind') == kind]

    def wake(self) -> Any:
        return self.service.due_intent_wake_timers.get(STORY_ID)

    def visible(self, request: Any) -> dict[str, Any]:
        """她下一回合**真正看到**的那些字段（`narrator.py:1258` 交给提供者的就是它）。

        `to_prompt_payload` 把准备好的值分进几个语义分组（七段式），所以这里按分组取
        回字段，而不是在顶层瞎找——判据还是同一个函数。
        """
        payload = to_prompt_payload(request)
        episodes = payload.get('relevantEstablishedEpisodes') or {}
        threads = payload.get('ongoingThreads') or {}
        near = payload.get('availableNearFuture') or {}
        return {
            'deliveryReality': threads.get('deliveryReality') or [],
            'dueIntents': near.get('dueIntents') or [],
            'webContext': episodes.get('webContext') or [],
            'recentScript': episodes.get('recentScript') or [],
        }

    @staticmethod
    def _facts(payload: dict[str, Any]) -> list[str]:
        return [
            str(segment.get('fact') or '')
            for entry in (payload.get('deliveryReality') or [])
            for segment in (entry.get('segments') or [])
        ]

    @staticmethod
    def _due(payload: dict[str, Any], intent_type: str = 'completion-report') -> list[Any]:
        return [item for item in (payload.get('dueIntents') or []) if item.get('type') == intent_type]

    async def deliver_sticker(self, ok: bool) -> Any:
        """生产路径：`send_sticker` → `record_platform_delivery_outcome` → 投递账本。"""
        self.transport.ok = ok
        commit = _media_commit()
        entry = await self.service.append_entry(
            STORY_ID, script_entry_draft_for_commit(commit, {'seen': True, 'reply': {'mode': 'none'}}),
            NOW, PARTICIPANT_ID,
        )
        reference = platform_action_reference(commit, entry['id'], 'local-media', ASSET_ID)
        self.assertIsNotNone(reference, '平台行动片段没定位到 → 账本根本不会记这一笔')
        delivered = await self.service.send_sticker(
            {'id': STORY_ID}, {'platform': 'test'}, 'private:2',
            {'assetId': ASSET_ID, 'filePath': 'collected/x.png', 'description': '得意'},
            None, reference,
        )
        return delivered, reference

    # -- ① 媒体投递（② 失败）--------------------------------------------- #

    async def test_media_failure_wakes_her_and_the_fact_reaches_the_next_turn(self):
        delivered, _reference = await self.deliver_sticker(ok=False)
        self.assertFalse(delivered)

        # 完成即唤醒：计时器排在"结果一落地"的下一秒，而不是等下一次常规 sweep（默认 5 分钟）。
        wake = self.wake()
        self.assertIsNotNone(wake, '异步动作完成之后必须立刻排一次唤醒')
        self.assertEqual(wake['due_at'], (NOW + SECOND).timestamp() * 1000)

        # 待回报事实：三态 + 原文（措辞来自 delivery_reality，不在这里另写一份）。
        (report,) = self.reports('platform-delivery')
        self.assertEqual(report['type'], 'completion-report')
        self.assertEqual(report['status'], 'pending')
        self.assertEqual(report['participantId'], PARTICIPANT_ID)
        self.assertEqual(report['payload']['state'], 'failed')
        self.assertIn('没发出去', report['summary'])
        self.assertIn(ASSET_ID, report['summary'])
        self.assertTrue(report['payload']['facts'])

        # 下一回合（**没有新的用户消息**：走的就是唤醒那一跳）她看见的东西。
        request = await self.fire_wake(NOW + 2 * SECOND)
        self.assertIsNotNone(request, '唤醒必须真的换来一个回合')
        payload = self.visible(request)
        facts = self._facts(payload)
        self.assertTrue(
            any('未确认送达（出错）' in fact and ASSET_ID in fact for fact in facts), facts,
        )
        due = self._due(payload)
        self.assertEqual(len(due), 1, payload.get('dueIntents'))
        self.assertIn('没发出去', due[0]['summary'])
        self.assertIn(ASSET_ID, due[0]['summary'])
        self.assertEqual(due[0]['payload']['state'], 'failed')

    # -- ③ 不可知（回执缺失）--------------------------------------------- #

    async def test_a_missing_receipt_is_reported_as_unknown_not_as_success(self):
        """定时的分段气泡没有回执（重试已排期）：她说得出"不确定"，不是"发出去了"。"""
        commit = decision_to_script_commit({
            'story_id': STORY_ID, 'participant_id': PARTICIPANT_ID, 'phase': 'user-message',
            'from': FROM, 'now': NOW, 'message_separator': '<sep/>', 'split_reply_messages': True,
            'decision': {
                'script': '她回了半句，顿了一下。',
                'interaction': {'seen': True,
                                'reply': {'mode': 'immediate', 'content': '第一句<sep/>第二句'}},
            },
        })
        entry = await self.service.append_entry(
            STORY_ID, script_entry_draft_for_commit(commit, dict(_REPLY)), NOW, PARTICIPANT_ID,
        )
        event = find_outgoing_script_event(commit, PARTICIPANT_ID)
        message = prepare_outgoing_delivery(
            attach_message_event(
                {'participant_id': PARTICIPANT_ID, 'content': event['content'],
                 'user_initiated': True},
                event, entry['id'],
            ),
            event['bubbles'],
        )
        await self.service.confirm_outgoing_deliveries({'id': STORY_ID}, [message])
        split = await self._db_get_split()
        self.assertEqual(len(split), 1, '夹具没排出分段意图')
        # 第二段投不出去（传输当场失败）：账本 pending（重试已排期）——
        # 这就是"回执缺失"，不是成功。
        send_at = split[0]['notBefore']
        self.transport.ok = False
        await self.fire_wake(send_at + 2 * SECOND)

        reports = self.reports('due-intent')
        self.assertTrue(reports, '没有回执也必须回报一次')
        unknown = [row for row in reports if row['payload']['state'] == 'unknown']
        self.assertEqual(len(unknown), 1, reports)
        true_summary = unknown[0]['summary']
        # 总述必须是"不确定"打头——逐段事实里同时列出已经发出去的那半句是**故意的**
        # （她得知道哪半句已经出去了，别重发）。
        self.assertTrue(true_summary.startswith('结果不确定：'), true_summary)
        self.assertIn('未确认送达：第二句', true_summary, '带原文，她才能决定补发还是作罢')
        self.assertNotIn('成功', true_summary)
        self.assertEqual((await self._db_get_split())[0]['status'], 'pending', '重试仍要排期')
        # 唤醒收尾把"下一个到期意图"补上了：回报自己的唤醒（now+1s）而不是丢掉排期。
        self.assertIsNotNone(self.wake(), '回报之后必须还有一个到期的唤醒')
        # 回报那一跳：传输恢复，她的应答能真的发出去（于是不会再产生第二次回报），
        # 唤醒收尾要补的就是**重试那一跳**。
        retry_at = (await self._db_get_split())[0]['notBefore']
        self.transport.ok = True
        request = await self.fire_wake(send_at + 4 * SECOND)
        self.assertIsNotNone(request, '回报那一跳必须换来一个回合')
        self.assertIn('结果不确定', self._due(self.visible(request))[0]['summary'])
        rearmed = self.wake()
        self.assertIsNotNone(rearmed, '更晚的排期必须在唤醒收尾时补回来')
        self.assertEqual(rearmed['due_at'], retry_at.timestamp() * 1000)

    async def _db_get_split(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.db.all(
            'interlude_intent', {'storyId': STORY_ID, 'type': 'split-message'},
        )]

    # -- ① 平台动作 -------------------------------------------------------- #

    async def test_platform_action_failure_goes_through_the_same_channel(self):
        story = await self.service.get_story(STORY_ID)
        # 执行侧（chunk12）自己写留痕的那条路，一个字没改。
        await self.service._record_platform_actions(story, [
            {'action': 'set_group_add_request', 'ok': False, 'error': 'no-permission'},
        ], session={'participantId': PARTICIPANT_ID})

        entry = [dict(row) for row in self.db.all(
            'interlude_script_entry', {'storyId': STORY_ID, 'kind': 'life'},
        )]
        self.assertTrue(entry and entry[0]['content'].startswith('[平台动作]'), entry)
        (report,) = self.reports('platform-action')
        self.assertEqual(report['payload']['state'], 'failed')
        # 事实就是执行侧写下的那一句（不产生第二份措辞）。
        self.assertIn('[平台动作]', report['summary'])
        self.assertIn('no-permission', report['summary'])
        self.assertEqual(self.wake()['due_at'], (NOW + SECOND).timestamp() * 1000)

        request = await self.fire_wake(NOW + 2 * SECOND)
        payload = self.visible(request)
        due = self._due(payload)
        self.assertEqual(len(due), 1)
        self.assertIn('[平台动作]', due[0]['summary'])
        recent = [str(item.get('content') or '') for item in (payload.get('recentScript') or [])]
        self.assertTrue(any('[平台动作]' in text for text in recent), recent)

    # -- ① 浏览（成功与失败）--------------------------------------------- #

    async def test_a_completed_browse_reaches_the_next_turn_without_a_new_user_message(self):
        self.transport = _BrowseTransport(page=True)
        self.service.transport = self.transport
        await self.service.append_browser_intent(
            STORY_ID, {'mode': 'search', 'query': '妹妹', 'purpose': '想知道他妹妹是谁'},
            NOW, PARTICIPANT_ID,
        )
        # 到期那一跳：真的去查（§79 的唤醒），观察一落地就产生完成回报。
        await self.fire_wake(NOW + 2 * SECOND)
        observations = [dict(row) for row in self.db.all(
            'interlude_web_observation', {'storyId': STORY_ID},
        )]
        self.assertEqual([row['status'] for row in observations], ['success'])
        (report,) = self.reports('browse')
        self.assertEqual(report['payload']['state'], 'delivered', '浏览是她等的工作，成功也要回报')
        self.assertIn(_PAGE_EXCERPT, report['summary'])
        self.assertEqual(self.wake()['due_at'], (NOW + 3 * SECOND).timestamp() * 1000)

        request = await self.fire_wake(NOW + 4 * SECOND)
        payload = self.visible(request)
        web = payload.get('webContext') or []
        self.assertTrue(any(_PAGE_EXCERPT in str(item.get('excerpt') or '') for item in web), web)
        self.assertIn(_PAGE_EXCERPT, self._due(payload)[0]['summary'])

    async def test_a_failed_browse_is_reported_as_a_failure(self):
        self.transport = _BrowseTransport(page=False)
        self.service.transport = self.transport
        await self.service.append_browser_intent(
            STORY_ID, {'mode': 'visit', 'url': 'https://example.com/x', 'purpose': '看一眼'},
            NOW, PARTICIPANT_ID,
        )
        await self.fire_wake(NOW + 2 * SECOND)
        (report,) = self.reports('browse')
        self.assertEqual(report['payload']['state'], 'failed')
        self.assertIn('没发出去', report['summary'])
        self.assertIn('网页读取失败', report['summary'])
        request = await self.fire_wake(NOW + 4 * SECOND)
        self.assertIn('网页读取失败', self._due(self.visible(request))[0]['summary'])

    # -- ④ 全部成功：不刷噪音 --------------------------------------------- #

    async def test_all_successes_produce_no_report_and_no_extra_turn(self):
        delivered, _reference = await self.deliver_sticker(ok=True)
        self.assertTrue(delivered)
        story = await self.service.get_story(STORY_ID)
        await self.service._record_platform_actions(story, [
            {'action': 'set_group_add_request', 'ok': True},
        ], session={'participantId': PARTICIPANT_ID})
        self.assertEqual(self.reports(), [], '全部成功不许产生待回报事实')
        self.assertIsNone(self.wake(), '全部成功不许排额外的即时回合')
        # 成功之后照旧走一次唤醒那一跳：没有任何到期工作，不该冒出回合。
        self.ctx.clock = lambda: NOW + 3 * SECOND
        story = await self.service.get_story(STORY_ID)
        await self.service.advance_story(story, False)
        self.assertEqual(self.narrator.requests, [])

    # -- ⑤ 节流 ------------------------------------------------------------ #

    async def test_three_same_cause_failures_report_once_and_the_log_says_why(self):
        _delivered, reference = await self.deliver_sticker(ok=False)
        self.assertEqual(len(self.reports('platform-delivery')), 1)
        first = self.reports('platform-delivery')[0]['summary']
        outcome = []
        for _ in range(2):
            outcome.append(await self.service.report_ledger_completion(
                STORY_ID, reference, 'failed', _STICKER_ERROR, NOW,
                kind='platform-delivery',
            ))
        self.assertEqual(outcome, ['throttled', 'throttled'])
        self.assertEqual(len(self.reports('platform-delivery')), 1, '连续三次同因失败只许回报一次')
        self.assertEqual(self.reports('platform-delivery')[0]['summary'], first)
        text = self.sink.text()
        self.assertIn('完成回报被节流', text)
        self.assertIn('已回报过', text)
        self.assertIn('rich media transfer failed', text, '日志必须说得出"因为 X 已回报过"')
        # 反向：换一个原因就是新事实，不许被上一轮的节流吞掉。
        self.assertEqual(
            await self.service.report_ledger_completion(
                STORY_ID, reference, 'failed', 'sticker-path-missing', NOW,
                kind='platform-delivery',
            ),
            'reported',
        )
        self.assertEqual(len(self.reports('platform-delivery')), 2)

    # -- 反向：那一跳必须在通道里 ----------------------------------------- #

    async def test_the_wake_hop_is_what_makes_the_report_visible(self):
        """§87 的反向用例：待回报事实**到点之前**，后台扫描不会自己演出这一回合。

        这条钉的是"完成即回报"的最后一跳（`schedule_due_intent_wake`）本身：到点之前
        就算跑一次后台扫描也没有回合；到点之后（同一份数据、同一次扫描）才有——所以
        把那一跳拿掉，① 的断言会直接红。
        """
        _delivered, _reference = await self.deliver_sticker(ok=False)
        (report,) = self.reports('platform-delivery')
        self.assertEqual(report['status'], 'pending')
        self.ctx.clock = lambda: NOW + timedelta(milliseconds=500)
        story = await self.service.get_story(STORY_ID)
        await self.service.advance_story(story, False)
        self.assertEqual(self.narrator.requests, [], '到点之前不该出现即时回合')
        self.assertIsNotNone(self.wake(), '完成即唤醒必须自己排计时器')
        request = await self.fire_wake(NOW + 2 * SECOND)
        self.assertIsNotNone(request)
        self.assertIn('没发出去', self._due(self.visible(request))[0]['summary'])

    # -- 通道自身的闸门 ---------------------------------------------------- #

    def test_the_report_delay_is_positive_so_the_intent_row_is_accepted(self):
        """`append_intent` 只收未来时刻：回报延迟必须 > 0，否则整条通道静默失效。"""
        self.assertGreater(chunk5_module._COMPLETION_REPORT_DELAY_MS, 0)
        self.assertGreater(chunk5_module._COMPLETION_REPORT_THROTTLE_MS, 0)
        self.assertIn(
            'completion-report', chunk5_module._INTERNAL_INTENT_TYPES,
            '回报不是"她打算做的事"，不许混进 upcomingPlans',
        )


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
