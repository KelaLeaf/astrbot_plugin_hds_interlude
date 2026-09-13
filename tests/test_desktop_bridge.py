"""`plugin/core/service/desktop.py`（`upstream/src/desktop-bridge.ts`）的单元测试。

覆盖三类契约：

1. **参数校验**：`isPhase` / `isRequestId` / `isInboundEvent` /
   `isTimelineRangeRequest` 的每一个判据（含边界值）。
2. **命令返回结构**：8 条 `hdsi-desktop` 命令的成功 payload 与上游 `handle()`
   catch 分支的失败信封（`{requestId, accepted: false, error}` +
   `replay-inbox` 额外的 `results: []` + 一条 `error` 事件）。
3. **安全降级**：没有 sink 时事件只进缓冲区、`console-port` 在本移植版恒不上报、
   没有计时器时心跳不启动、`stop()` 拒绝全部挂起投递。

运行：`python3 -m unittest plugin.tests.test_desktop_bridge -v`
"""

from __future__ import annotations

import asyncio
import os
import unittest
from typing import Any
from unittest import mock

from plugin.core.service.desktop import (
    DESKTOP_BRIDGE_ENV,
    DESKTOP_PHASE_ENV,
    DesktopBridge,
    desktop_session,
    escape_attribute,
    install_desktop_bridge,
    is_inbound_event,
    is_phase,
    is_request_id,
    is_timeline_range_request,
)
from plugin.core.time import parse_dt

REQUEST_ID = 'request-0001'


# =========================================================================== #
# 假服务：只实现 DesktopBridge 真正调用的那一组方法
# =========================================================================== #

class _FakeTimer:
    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class _FakeCtx:
    """只提供 `ctx.set_interval`（上游 `setInterval` 的等价物）。"""

    def __init__(self, port: Any = None) -> None:
        self.intervals: list[tuple[Any, int]] = []
        self.timers: list[_FakeTimer] = []
        if port is not None:
            self.server = type('_Server', (), {'port': port})()

    def set_interval(self, callback: Any, delay_ms: int) -> _FakeTimer:
        self.intervals.append((callback, delay_ms))
        timer = _FakeTimer()
        self.timers.append(timer)
        return timer


class _FakeService:
    """记录全部调用的假服务。"""

    def __init__(self, *, phase: str = 'running', ctx: Any = None) -> None:
        self.ctx = ctx if ctx is not None else _FakeCtx()
        self._phase = phase
        self.event_sink: Any = None
        self.delivery_handler: Any = None
        self.phase_calls: list[str] = []
        self.received: list[tuple[Any, Any]] = []
        self.receive_result: Any = True
        self.receive_error: Exception | None = None
        self.cursor_calls: list[Any] = []
        self.snapshots = 0
        self.range_queries: list[Any] = []
        self.purge_calls: list[tuple[Any, Any]] = []
        self.phase_error: Exception | None = None

    # ---- 桥接面 ----

    def set_desktop_event_sink(self, sink: Any) -> None:
        self.event_sink = sink

    def set_desktop_delivery_handler(self, handler: Any) -> None:
        self.delivery_handler = handler

    def get_desktop_runtime_phase(self) -> str:
        return self._phase

    async def set_desktop_runtime_phase(self, phase: str) -> None:
        if self.phase_error is not None:
            raise self.phase_error
        self.phase_calls.append(phase)
        self._phase = phase

    async def receive_desktop_event(self, event: Any, session: Any) -> Any:
        self.received.append((event, session))
        if self.receive_error is not None:
            raise self.receive_error
        return self.receive_result

    async def set_desktop_cursor_at(self, cursor_at: Any) -> None:
        self.cursor_calls.append(cursor_at)

    async def desktop_timeline_snapshot(self) -> dict[str, Any]:
        self.snapshots += 1
        return {'storyId': 'story-1', 'entries': []}

    async def desktop_timeline_range(self, query: Any = None) -> dict[str, Any]:
        self.range_queries.append(query)
        return {'protocol': 4, 'storyId': 'story-1', 'entries': []}

    async def desktop_purge_range(self, from_value: Any, to_value: Any) -> dict[str, Any]:
        self.purge_calls.append((from_value, to_value))
        return {'storyId': 'story-1'}


def inbound_event(**overrides: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        'transport': 'sandbox',
        'accountKey': 'acct-1',
        'platform': 'onebot',
        'selfId': '10001',
        'senderId': '20002',
        'senderName': '主人',
        'kind': 'private',
        'content': '在吗',
        'occurredAt': '2024-05-06T07:08:09.000Z',
        'rawMessageId': 'msg-1',
    }
    event.update(overrides)
    return event


def command(name: str, value: Any = None) -> dict[str, Any]:
    return {'type': 'hdsi-desktop', 'command': name, 'value': value}


def events_of(result: dict[str, Any], name: str) -> list[dict[str, Any]]:
    return [item for item in result['events'] if item['event'] == name]


# =========================================================================== #
# 1. 参数校验（上游 `desktop-bridge.ts:63-88`）
# =========================================================================== #

class PhaseGuardTests(unittest.TestCase):
    def test_accepts_the_three_runtime_phases(self) -> None:
        for value in ('running', 'muted', 'paused'):
            self.assertTrue(is_phase(value), value)

    def test_rejects_anything_else(self) -> None:
        for value in ('', 'RUNNING', 'idle', None, 1, True):
            self.assertFalse(is_phase(value), value)


class RequestIdGuardTests(unittest.TestCase):
    def test_length_bounds_are_inclusive(self) -> None:
        self.assertTrue(is_request_id('a' * 8))
        self.assertTrue(is_request_id('a' * 128))
        self.assertFalse(is_request_id('a' * 7))
        self.assertFalse(is_request_id('a' * 129))

    def test_non_strings_are_rejected(self) -> None:
        for value in (None, 12345678, True, ['12345678']):
            self.assertFalse(is_request_id(value), value)


class InboundEventGuardTests(unittest.TestCase):
    def test_minimal_private_event_is_valid(self) -> None:
        self.assertTrue(is_inbound_event(inbound_event()))

    def test_blank_content_is_valid(self) -> None:
        self.assertTrue(is_inbound_event(inbound_event(content='')))

    def test_content_longer_than_the_cap_is_rejected(self) -> None:
        self.assertTrue(is_inbound_event(inbound_event(content='x' * 128_000)))
        self.assertFalse(is_inbound_event(inbound_event(content='x' * 128_001)))

    def test_required_string_fields(self) -> None:
        for key in ('accountKey', 'platform', 'selfId', 'senderId'):
            event = inbound_event()
            event[key] = 42
            self.assertFalse(is_inbound_event(event), key)

    def test_kind_must_be_private_or_group(self) -> None:
        self.assertFalse(is_inbound_event(inbound_event(kind='channel')))
        self.assertFalse(is_inbound_event(inbound_event(kind=None)))

    def test_group_events_require_a_channel_id(self) -> None:
        self.assertFalse(is_inbound_event(inbound_event(kind='group')))
        self.assertTrue(is_inbound_event(inbound_event(kind='group', channelId='room-1')))
        self.assertFalse(is_inbound_event(inbound_event(kind='group', channelId=7)))

    def test_occurred_at_must_be_a_parseable_string(self) -> None:
        self.assertFalse(is_inbound_event(inbound_event(occurredAt='')))
        self.assertFalse(is_inbound_event(inbound_event(occurredAt='not a date')))
        # 上游 `Date.parse` 对非字符串一律 NaN。
        self.assertFalse(is_inbound_event(inbound_event(occurredAt=1714970889000)))
        self.assertFalse(is_inbound_event(inbound_event(occurredAt=None)))

    def test_non_dict_is_rejected(self) -> None:
        for value in (None, 'x', 1, []):
            self.assertFalse(is_inbound_event(value), value)

    def test_snake_case_spellings_are_accepted(self) -> None:
        """键名法：外部输入双读，snake_case 也认。"""
        self.assertTrue(is_inbound_event({
            'account_key': 'a', 'platform': 'onebot', 'self_id': '1', 'sender_id': '2',
            'kind': 'private', 'content': 'hi', 'occurred_at': '2024-05-06T07:08:09Z',
        }))


class TimelineRangeGuardTests(unittest.TestCase):
    def test_none_is_valid(self) -> None:
        self.assertTrue(is_timeline_range_request(None))

    def test_non_dict_is_rejected(self) -> None:
        for value in ('x', 1, []):
            self.assertFalse(is_timeline_range_request(value), value)

    def test_empty_object_is_valid(self) -> None:
        self.assertTrue(is_timeline_range_request({}))

    def test_from_and_to_must_parse(self) -> None:
        self.assertTrue(is_timeline_range_request({'from': '2024-05-06T07:08:09Z'}))
        self.assertFalse(is_timeline_range_request({'from': 'nope'}))
        self.assertFalse(is_timeline_range_request({'to': 1714970889000}))

    def test_tracks_bounds(self) -> None:
        self.assertTrue(is_timeline_range_request({'tracks': ['entries'] * 8}))
        self.assertFalse(is_timeline_range_request({'tracks': ['entries'] * 9}))
        self.assertFalse(is_timeline_range_request({'tracks': 'entries'}))
        self.assertFalse(is_timeline_range_request({'tracks': ['entries', 7]}))

    def test_cursor_length_bound(self) -> None:
        self.assertTrue(is_timeline_range_request({'cursor': 'c' * 80}))
        self.assertFalse(is_timeline_range_request({'cursor': 'c' * 81}))
        self.assertFalse(is_timeline_range_request({'cursor': 7}))

    def test_limit_bounds(self) -> None:
        self.assertTrue(is_timeline_range_request({'limit': 1}))
        self.assertTrue(is_timeline_range_request({'limit': 500}))
        self.assertFalse(is_timeline_range_request({'limit': 0}))
        self.assertFalse(is_timeline_range_request({'limit': 501}))
        self.assertFalse(is_timeline_range_request({'limit': float('nan')}))
        self.assertFalse(is_timeline_range_request({'limit': float('inf')}))
        self.assertFalse(is_timeline_range_request({'limit': True}))
        self.assertFalse(is_timeline_range_request({'limit': '10'}))

    def test_detail_level_enum(self) -> None:
        self.assertTrue(is_timeline_range_request({'detailLevel': 'summary'}))
        self.assertTrue(is_timeline_range_request({'detailLevel': 'full'}))
        self.assertFalse(is_timeline_range_request({'detailLevel': 'verbose'}))
        self.assertTrue(is_timeline_range_request({'detail_level': 'full'}))


class EscapeAttributeTests(unittest.TestCase):
    def test_escapes_the_five_entities(self) -> None:
        self.assertEqual(escape_attribute('a&b<c>d"e\'f'), 'a&amp;b&lt;c&gt;d&quot;e&#39;f')

    def test_leaves_plain_text_untouched(self) -> None:
        self.assertEqual(escape_attribute('https://x/y.png?q=1'), 'https://x/y.png?q=1')


# =========================================================================== #
# 2. 入站事件 → SessionView（上游 `desktopSession`）
# =========================================================================== #

class DesktopSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_channel_defaults_to_private_sender(self) -> None:
        session = desktop_session(inbound_event(), None)
        self.assertEqual(session.channel_id, 'private:20002')
        self.assertEqual(session.guild_id, '')
        self.assertTrue(session.is_direct)
        self.assertEqual(session.user_id, '20002')
        self.assertEqual(session.username, '主人')
        self.assertEqual(session.message_id, 'msg-1')

    async def test_group_channel_uses_channel_id_as_guild(self) -> None:
        session = desktop_session(inbound_event(kind='group', channelId='room-9'), None)
        self.assertEqual(session.channel_id, 'room-9')
        self.assertEqual(session.guild_id, 'room-9')
        self.assertFalse(session.is_direct)

    async def test_content_appends_escaped_image_elements(self) -> None:
        session = desktop_session(
            inbound_event(content='看图', imageSources=['https://x/a.png?b=1&c=2', 'https://x/b.png']),
            None,
        )
        self.assertEqual(
            session.content,
            '看图<img src="https://x/a.png?b=1&amp;c=2"><img src="https://x/b.png">',
        )

    async def test_empty_content_is_dropped_like_js_filter_boolean(self) -> None:
        session = desktop_session(inbound_event(content='', imageSources=['https://x/a.png']), None)
        self.assertEqual(session.content, '<img src="https://x/a.png">')

    async def test_send_routes_through_request_delivery(self) -> None:
        payloads: list[dict[str, Any]] = []

        async def request_delivery(payload: dict[str, Any]) -> list[str]:
            payloads.append(payload)
            return ['msg-out']

        session = desktop_session(inbound_event(), request_delivery)
        self.assertEqual(await session.send('好的'), ['msg-out'])
        self.assertEqual(payloads[0]['accountKey'], 'acct-1')
        self.assertEqual(payloads[0]['channelId'], 'private:20002')
        self.assertEqual(payloads[0]['kind'], 'private')
        self.assertEqual(payloads[0]['replyTo'], 'msg-1')
        self.assertEqual(payloads[0]['content'], '好的')


# =========================================================================== #
# 3. 命令分发与返回结构
# =========================================================================== #

class DesktopBridgeCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.service = _FakeService()
        self.bridge = DesktopBridge(self.service, None, enabled=True)

    # ---- 报文入口 ----

    async def test_non_bridge_message_is_ignored(self) -> None:
        for message in (None, {}, {'type': 'other'}, 'x', 1):
            self.assertIsNone(await self.bridge.handle(message), message)

    async def test_unknown_command_is_silently_ignored(self) -> None:
        result = await self.bridge.handle(command('nope', {'requestId': REQUEST_ID}))
        self.assertEqual(result, {'command': 'nope', 'events': []})

    async def test_delivery_result_returns_no_response(self) -> None:
        self.assertIsNone(await self.bridge.handle(command('delivery-result', {'deliveryId': 'x'})))

    # ---- phase ----

    async def test_phase_success_payload(self) -> None:
        result = await self.bridge.handle(command('phase', {'requestId': REQUEST_ID, 'phase': 'paused'}))
        payload = events_of(result, 'phase-result')[0]['payload']
        self.assertEqual(payload, {'requestId': REQUEST_ID, 'accepted': True, 'phase': 'paused'})
        self.assertEqual(self.service.phase_calls, ['paused'])

    async def test_phase_rejects_bad_request_id(self) -> None:
        result = await self.bridge.handle(command('phase', {'requestId': 'short', 'phase': 'running'}))
        payload = events_of(result, 'phase-result')[0]['payload']
        self.assertEqual(payload, {'requestId': 'short', 'accepted': False, 'error': '无效 typ-0 运行状态请求。'})
        self.assertEqual(self.service.phase_calls, [])

    async def test_phase_rejects_bad_phase(self) -> None:
        result = await self.bridge.handle(command('phase', {'requestId': REQUEST_ID, 'phase': 'idle'}))
        payload = events_of(result, 'phase-result')[0]['payload']
        self.assertEqual(payload['accepted'], False)
        self.assertEqual(payload['error'], '无效 typ-0 运行状态请求。')

    async def test_phase_failure_also_emits_a_generic_error_event(self) -> None:
        result = await self.bridge.handle(command('phase', {'requestId': 'short', 'phase': 'idle'}))
        error = events_of(result, 'error')
        self.assertEqual(len(error), 1)
        self.assertEqual(error[0]['payload']['command'], 'phase')
        self.assertEqual(error[0]['payload']['requestId'], 'short')

    # ---- inbound ----

    async def test_inbound_accepted_while_running(self) -> None:
        result = await self.bridge.handle(command('inbound', {'requestId': REQUEST_ID, 'event': inbound_event()}))
        payload = events_of(result, 'inbound-result')[0]['payload']
        self.assertEqual(payload, {'requestId': REQUEST_ID, 'accepted': True})
        self.assertEqual(len(self.service.received), 1)
        event, session = self.service.received[0]
        self.assertEqual(event['senderId'], '20002')
        self.assertEqual(session.channel_id, 'private:20002')

    async def test_inbound_rejected_when_not_running(self) -> None:
        self.service._phase = 'muted'
        result = await self.bridge.handle(command('inbound', {'requestId': REQUEST_ID, 'event': inbound_event()}))
        payload = events_of(result, 'inbound-result')[0]['payload']
        self.assertEqual(payload['accepted'], False)
        self.assertEqual(payload['error'], '当前剧本未接收该入站事件。')
        self.assertEqual(self.service.received, [], '非 running 时不得调用 receiveDesktopEvent')

    async def test_inbound_rejected_when_service_declines(self) -> None:
        self.service.receive_result = False
        result = await self.bridge.handle(command('inbound', {'requestId': REQUEST_ID, 'event': inbound_event()}))
        payload = events_of(result, 'inbound-result')[0]['payload']
        self.assertEqual(payload['accepted'], False)
        self.assertEqual(payload['error'], '当前剧本未接收该入站事件。')

    async def test_inbound_rejects_invalid_event(self) -> None:
        result = await self.bridge.handle(command('inbound', {'requestId': REQUEST_ID, 'event': {'kind': 'private'}}))
        payload = events_of(result, 'inbound-result')[0]['payload']
        self.assertEqual(payload['error'], '无效 typ-0 入站事件。')

    # ---- cursor-set ----

    async def test_cursor_set_success(self) -> None:
        result = await self.bridge.handle(command('cursor-set', {
            'requestId': REQUEST_ID, 'cursorAt': '2024-05-06T07:08:09.000Z',
        }))
        payload = events_of(result, 'cursor-set-result')[0]['payload']
        self.assertEqual(payload['requestId'], REQUEST_ID)
        self.assertTrue(payload['accepted'])
        self.assertEqual(parse_dt(payload['cursorAt']), parse_dt('2024-05-06T07:08:09.000Z'))
        self.assertEqual(self.service.cursor_calls, [parse_dt('2024-05-06T07:08:09.000Z')])

    async def test_cursor_set_failures_use_the_generic_error_event(self) -> None:
        """上游 `handle()` 的 catch 分支里 `cursor-set` **不在**三元链上。

        因此它校验失败时发出的两条事件都叫 `error`（先失败信封、再通用错误），
        而不是 `cursor-set-result`。这里逐字保留上游这个（略怪的）行为。
        """
        result = await self.bridge.handle(command('cursor-set', {'requestId': REQUEST_ID, 'cursorAt': 1714970889000}))
        self.assertEqual([item['event'] for item in result['events']], ['error', 'error'])
        self.assertEqual(result['events'][0]['payload'], {
            'requestId': REQUEST_ID, 'accepted': False, 'error': '无效 typ-0 游标设置请求。',
        })
        self.assertEqual(result['events'][1]['payload'], {
            'command': 'cursor-set', 'requestId': REQUEST_ID, 'message': '无效 typ-0 游标设置请求。',
        })

    async def test_cursor_set_rejects_unparseable_time(self) -> None:
        result = await self.bridge.handle(command('cursor-set', {'requestId': REQUEST_ID, 'cursorAt': 'nope'}))
        payload = events_of(result, 'error')[0]['payload']
        self.assertEqual(payload['error'], '游标时间无法解析。')

    # ---- replay-inbox ----

    async def test_replay_inbox_mixed_records(self) -> None:
        result = await self.bridge.handle(command('replay-inbox', {
            'requestId': REQUEST_ID,
            'records': [
                {'id': 'r1', 'event': inbound_event()},
                {'id': 'r2', 'event': {'bad': True}},
                {'id': '', 'event': inbound_event()},
                'not-a-record',
            ],
        }))
        payload = events_of(result, 'replay-result')[0]['payload']
        self.assertEqual(payload['requestId'], REQUEST_ID)
        self.assertEqual(payload['results'], [
            {'id': 'r1', 'accepted': True},
            {'id': 'r2', 'accepted': False, 'error': '无效收件箱记录。'},
            {'id': '', 'accepted': False, 'error': '无效收件箱记录。'},
            {'id': '', 'accepted': False, 'error': '无效收件箱记录。'},
        ])

    async def test_replay_inbox_marks_service_declines(self) -> None:
        self.service.receive_result = False
        result = await self.bridge.handle(command('replay-inbox', {
            'requestId': REQUEST_ID, 'records': [{'id': 'r1', 'event': inbound_event()}],
        }))
        payload = events_of(result, 'replay-result')[0]['payload']
        self.assertEqual(payload['results'], [
            {'id': 'r1', 'accepted': False, 'error': '当前剧本未接收该入站事件。'},
        ])

    async def test_replay_inbox_isolates_per_record_failures(self) -> None:
        self.service.receive_error = RuntimeError('这条记录炸了')
        result = await self.bridge.handle(command('replay-inbox', {
            'requestId': REQUEST_ID,
            'records': [{'id': 'r1', 'event': inbound_event()}, {'id': 'r2', 'event': inbound_event()}],
        }))
        payload = events_of(result, 'replay-result')[0]['payload']
        self.assertEqual([item['error'] for item in payload['results']], ['这条记录炸了', '这条记录炸了'])

    async def test_replay_inbox_requires_request_id(self) -> None:
        result = await self.bridge.handle(command('replay-inbox', {'records': []}))
        payload = events_of(result, 'replay-result')[0]['payload']
        self.assertEqual(payload, {
            'requestId': None, 'accepted': False, 'error': '回放请求缺少 requestId。', 'results': [],
        })

    # ---- snapshot ----

    async def test_snapshot_success(self) -> None:
        result = await self.bridge.handle(command('snapshot', {'requestId': REQUEST_ID}))
        payload = events_of(result, 'snapshot-result')[0]['payload']
        self.assertEqual(payload['requestId'], REQUEST_ID)
        self.assertEqual(payload['snapshot'], {'storyId': 'story-1', 'entries': []})
        self.assertEqual(self.service.snapshots, 1)

    async def test_snapshot_requires_request_id(self) -> None:
        result = await self.bridge.handle(command('snapshot', {}))
        payload = events_of(result, 'snapshot-result')[0]['payload']
        self.assertEqual(payload['error'], '快照请求缺少 requestId。')
        self.assertNotIn('results', payload)

    # ---- timeline-range ----

    async def test_timeline_range_accepts_missing_query(self) -> None:
        result = await self.bridge.handle(command('timeline-range', {'requestId': REQUEST_ID}))
        payload = events_of(result, 'timeline-range-result')[0]['payload']
        self.assertEqual(payload['requestId'], REQUEST_ID)
        self.assertEqual(payload['projection']['protocol'], 4)
        self.assertEqual(self.service.range_queries, [None])

    async def test_timeline_range_passes_the_query_through_verbatim(self) -> None:
        query = {'from': '2024-05-06T00:00:00Z', 'tracks': ['entries'], 'limit': 10}
        result = await self.bridge.handle(command('timeline-range', {'requestId': REQUEST_ID, 'query': query}))
        self.assertTrue(events_of(result, 'timeline-range-result'))
        self.assertEqual(self.service.range_queries, [query])

    async def test_timeline_range_rejects_bad_query(self) -> None:
        result = await self.bridge.handle(command('timeline-range', {
            'requestId': REQUEST_ID, 'query': {'limit': 501},
        }))
        payload = events_of(result, 'timeline-range-result')[0]['payload']
        self.assertEqual(payload['error'], '无效时间线范围请求。')
        self.assertEqual(self.service.range_queries, [])

    # ---- purge-range ----

    async def test_purge_range_success(self) -> None:
        result = await self.bridge.handle(command('purge-range', {
            'requestId': REQUEST_ID, 'from': '2024-05-06T00:00:00Z', 'to': '2024-05-07T00:00:00Z',
        }))
        payload = events_of(result, 'purge-range-result')[0]['payload']
        self.assertEqual(payload, {'requestId': REQUEST_ID, 'accepted': True, 'storyId': 'story-1'})
        self.assertEqual(len(self.service.purge_calls), 1)

    async def test_purge_range_requires_request_id(self) -> None:
        result = await self.bridge.handle(command('purge-range', {
            'from': '2024-05-06T00:00:00Z', 'to': '2024-05-07T00:00:00Z',
        }))
        payload = events_of(result, 'purge-range-result')[0]['payload']
        self.assertEqual(payload['error'], '选区删除请求缺少 requestId。')

    async def test_purge_range_rejects_unparseable_bounds(self) -> None:
        result = await self.bridge.handle(command('purge-range', {'requestId': REQUEST_ID, 'from': '', 'to': 'x'}))
        payload = events_of(result, 'purge-range-result')[0]['payload']
        self.assertEqual(payload['error'], '选区删除时间范围无效。')
        self.assertEqual(self.service.purge_calls, [])

    async def test_purge_range_rejects_reversed_bounds(self) -> None:
        result = await self.bridge.handle(command('purge-range', {
            'requestId': REQUEST_ID, 'from': '2024-05-07T00:00:00Z', 'to': '2024-05-06T00:00:00Z',
        }))
        payload = events_of(result, 'purge-range-result')[0]['payload']
        self.assertEqual(payload['error'], '选区删除时间范围无效。')

    # ---- 服务异常 ----

    async def test_service_exception_becomes_a_failure_envelope(self) -> None:
        self.service.phase_error = RuntimeError('数据库挂了')
        result = await self.bridge.handle(command('phase', {'requestId': REQUEST_ID, 'phase': 'running'}))
        payload = events_of(result, 'phase-result')[0]['payload']
        self.assertEqual(payload, {'requestId': REQUEST_ID, 'accepted': False, 'error': '数据库挂了'})
        self.assertEqual(events_of(result, 'error')[0]['payload']['message'], '数据库挂了')


# =========================================================================== #
# 4. 事件出口与投递通道
# =========================================================================== #

class EventSinkTests(unittest.TestCase):
    def test_emit_records_and_calls_the_sink(self) -> None:
        seen: list[tuple[str, Any]] = []
        bridge = DesktopBridge(_FakeService(), lambda event, payload: seen.append((event, payload)), enabled=True)
        envelope = bridge.emit('heartbeat', {'phase': 'running'})
        self.assertEqual(envelope, {
            'type': 'hdsi-desktop', 'event': 'heartbeat', 'payload': {'phase': 'running'},
        })
        self.assertEqual(seen, [('heartbeat', {'phase': 'running'})])
        self.assertEqual(bridge.drain_events(), [envelope])
        self.assertEqual(bridge.drain_events(), [])

    def test_emit_without_sink_still_buffers(self) -> None:
        bridge = DesktopBridge(_FakeService(), None, enabled=True)
        bridge.emit('heartbeat', {})
        self.assertEqual(len(bridge.drain_events()), 1)

    def test_sink_exceptions_are_swallowed(self) -> None:
        def exploding(_event: str, _payload: Any) -> None:
            raise RuntimeError('宿主没了')

        bridge = DesktopBridge(_FakeService(), exploding, enabled=True)
        bridge.emit('heartbeat', {})
        self.assertEqual(len(bridge._events), 1)

    def test_event_buffer_is_bounded(self) -> None:
        bridge = DesktopBridge(_FakeService(), None, enabled=True)
        for index in range(DesktopBridge.EVENT_BUFFER_LIMIT + 50):
            bridge.emit('heartbeat', {'index': index})
        events = bridge.drain_events()
        self.assertEqual(len(events), DesktopBridge.EVENT_BUFFER_LIMIT)


class DeliveryChannelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.service = _FakeService()
        self.bridge = DesktopBridge(self.service, None, enabled=True, delivery_timeout_ms=60_000)

    async def test_request_delivery_resolves_on_a_sent_receipt(self) -> None:
        pending = asyncio.ensure_future(self.bridge.request_delivery({'content': 'hi'}))
        await asyncio.sleep(0)
        delivery = self.bridge.drain_events()[0]['payload']
        self.assertEqual(delivery['content'], 'hi')
        self.assertTrue(self.bridge.settle_delivery({
            'deliveryId': delivery['deliveryId'], 'status': 'sent', 'messageIds': ['m1', 7, 'm2'],
        }))
        self.assertEqual(await pending, ['m1', 'm2'])

    async def test_failure_receipt_carries_the_upstream_error(self) -> None:
        pending = asyncio.ensure_future(self.bridge.request_delivery({'content': 'hi'}))
        await asyncio.sleep(0)
        delivery_id = self.bridge.drain_events()[0]['payload']['deliveryId']
        self.assertTrue(self.bridge.settle_delivery({
            'deliveryId': delivery_id, 'status': 'retryable-failed', 'error': '渠道超时',
        }))
        with self.assertRaises(RuntimeError) as caught:
            await pending
        self.assertEqual(str(caught.exception), '渠道超时')

    async def test_failure_receipt_without_error_uses_the_status_text(self) -> None:
        pending = asyncio.ensure_future(self.bridge.request_delivery({'content': 'hi'}))
        await asyncio.sleep(0)
        delivery_id = self.bridge.drain_events()[0]['payload']['deliveryId']
        self.bridge.settle_delivery({'deliveryId': delivery_id, 'status': 'permanent-failed'})
        with self.assertRaises(RuntimeError) as caught:
            await pending
        self.assertEqual(str(caught.exception), '渠道投递失败：permanent-failed')

    async def test_unknown_or_malformed_receipts_return_false(self) -> None:
        for value in (None, {}, {'deliveryId': 1}, {'deliveryId': 'nope', 'status': 'sent'}, 'x'):
            self.assertFalse(self.bridge.settle_delivery(value), value)

    async def test_receipts_are_single_use(self) -> None:
        pending = asyncio.ensure_future(self.bridge.request_delivery({'content': 'hi'}))
        await asyncio.sleep(0)
        delivery_id = self.bridge.drain_events()[0]['payload']['deliveryId']
        receipt = {'deliveryId': delivery_id, 'status': 'sent', 'messageIds': []}
        self.assertTrue(self.bridge.settle_delivery(receipt))
        self.assertFalse(self.bridge.settle_delivery(receipt))
        self.assertEqual(await pending, [])

    async def test_timeout_rejects_with_the_upstream_message(self) -> None:
        bridge = DesktopBridge(self.service, None, enabled=True, delivery_timeout_ms=20)
        pending = asyncio.ensure_future(bridge.request_delivery({'content': 'hi'}))
        with self.assertRaises(RuntimeError) as caught:
            await asyncio.wait_for(pending, timeout=2)
        self.assertEqual(str(caught.exception), '等待 typ-0 渠道投递确认超时。')
        self.assertEqual(bridge._pending_deliveries, {})

    async def test_background_delivery_wraps_success(self) -> None:
        handler = self.bridge._background_delivery
        task = asyncio.ensure_future(handler({
            'platform': 'onebot', 'self_id': '10001', 'channel_id': 'room-1', 'kind': 'group',
            'content': '夜里的一句', 'quote_message_id': 'q-1',
        }))
        await asyncio.sleep(0)
        sent = self.bridge.drain_events()[0]['payload']
        self.assertEqual(sent['accountKey'], 'desktop:10001')
        self.assertEqual(sent['transport'], 'onebot-external')
        self.assertEqual(sent['replyTo'], 'q-1')
        self.bridge.settle_delivery({'deliveryId': sent['deliveryId'], 'status': 'sent', 'messageIds': ['m']})
        self.assertEqual(await task, {'ok': True, 'messageIds': ['m']})

    async def test_background_delivery_wraps_failure(self) -> None:
        task = asyncio.ensure_future(self.bridge._background_delivery({'self_id': '10001'}))
        await asyncio.sleep(0)
        delivery_id = self.bridge.drain_events()[0]['payload']['deliveryId']
        self.bridge.settle_delivery({'deliveryId': delivery_id, 'status': 'permanent-failed', 'error': '没了'})
        self.assertEqual(await task, {'ok': False, 'error': '没了'})


# =========================================================================== #
# 5. 生命周期与安全降级
# =========================================================================== #

class BridgeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.service = _FakeService()

    async def test_install_is_gated_by_the_env_flag(self) -> None:
        from plugin.core import logging as interlude_logging

        interlude_logging.set_log_sink(lambda _level, _text: None)
        self.addCleanup(interlude_logging.set_log_sink, interlude_logging._default_sink)
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(await install_desktop_bridge(self.service))
            self.assertIsNone(self.service.event_sink, '闸门关闭时不得挂任何东西')
        with mock.patch.dict(os.environ, {DESKTOP_BRIDGE_ENV: '1'}, clear=True):
            bridge = await install_desktop_bridge(self.service, heartbeat_interval_ms=0)
        self.assertIsNotNone(bridge)
        self.assertEqual(self.service.event_sink, bridge._emit_to_sink)
        self.assertIsNotNone(self.service.delivery_handler)
        bridge.stop()
        self.assertIsNone(self.service.event_sink)

    async def test_bridge_ready_reports_protocol_and_phase(self) -> None:
        bridge = DesktopBridge(self.service, None, enabled=True, heartbeat_interval_ms=0)
        await bridge.start()
        events = bridge.drain_events()
        self.assertEqual(events, [{
            'type': 'hdsi-desktop', 'event': 'bridge-ready',
            'payload': {'protocol': 4, 'phase': 'running'},
        }])
        self.assertEqual(self.service.phase_calls, ['running'])

    async def test_initial_phase_comes_from_the_environment(self) -> None:
        with mock.patch.dict(os.environ, {DESKTOP_PHASE_ENV: 'muted'}):
            bridge = DesktopBridge(self.service, None, enabled=True, heartbeat_interval_ms=0)
            await bridge.start()
        self.assertEqual(self.service.phase_calls, ['muted'])
        self.assertEqual(bridge.drain_events()[-1]['payload']['phase'], 'muted')

    async def test_invalid_initial_phase_falls_back_to_running(self) -> None:
        with mock.patch.dict(os.environ, {DESKTOP_PHASE_ENV: 'idle'}):
            bridge = DesktopBridge(self.service, None, enabled=True, heartbeat_interval_ms=0)
            await bridge.start()
        self.assertEqual(self.service.phase_calls, ['running'])

    async def test_bridge_ready_is_emitted_even_when_the_phase_call_fails(self) -> None:
        self.service.phase_error = RuntimeError('阶段设置失败')
        bridge = DesktopBridge(self.service, None, enabled=True, heartbeat_interval_ms=0)
        await bridge.start()
        events = bridge.drain_events()
        self.assertEqual(events[-1]['event'], 'bridge-ready')
        self.assertTrue(any(
            item['event'] == 'error' and item['payload']['message'] == '阶段设置失败' for item in events
        ))

    async def test_heartbeat_is_scheduled_and_reports_the_phase(self) -> None:
        ctx = _FakeCtx()
        bridge = DesktopBridge(_FakeService(ctx=ctx), None, enabled=True)
        await bridge.start()
        self.assertEqual(len(ctx.intervals), 1)
        self.assertEqual(ctx.intervals[0][1], DesktopBridge.HEARTBEAT_INTERVAL_MS)
        bridge.drain_events()
        ctx.intervals[0][0]()  # 触发一次心跳（等价上游 setInterval 回调）
        heartbeat = bridge.drain_events()[0]
        self.assertEqual(heartbeat['event'], 'heartbeat')
        self.assertEqual(heartbeat['payload']['phase'], 'running')
        self.assertIsNotNone(parse_dt(heartbeat['payload']['at']))

    async def test_heartbeat_is_skipped_without_a_timer_facility(self) -> None:
        service = _FakeService(ctx=object())
        bridge = DesktopBridge(service, None, enabled=True)
        await bridge.start()
        self.assertIsNone(bridge._heartbeat)

    async def test_console_port_is_not_reported_without_a_loopback_server(self) -> None:
        """降级分支：AstrBot 没有 Koishi Console / loopback server。"""
        bridge = DesktopBridge(_FakeService(), None, enabled=True)
        self.assertIsNone(bridge._read_server_port())
        self.assertFalse(bridge.report_console_port())
        self.assertEqual(bridge.drain_events(), [])

    async def test_console_port_is_reported_when_the_host_exposes_one(self) -> None:
        bridge = DesktopBridge(_FakeService(ctx=_FakeCtx(port=6185)), None, enabled=True)
        self.assertTrue(bridge.report_console_port())
        self.assertEqual(bridge.drain_events()[0], {
            'type': 'hdsi-desktop', 'event': 'console-port',
            'payload': {'port': 6185, 'uiPath': '/console/'},
        })

    async def test_stop_clears_every_registration_and_rejects_pending(self) -> None:
        bridge = DesktopBridge(self.service, None, enabled=True, heartbeat_interval_ms=60_000)
        await bridge.start()
        pending = asyncio.ensure_future(bridge.request_delivery({'content': 'hi'}))
        await asyncio.sleep(0)
        bridge.stop()
        with self.assertRaises(RuntimeError) as caught:
            await pending
        self.assertEqual(str(caught.exception), 'typ-0 bridge 已关闭。')
        self.assertIsNone(self.service.event_sink)
        self.assertIsNone(self.service.delivery_handler)
        self.assertFalse(bridge.started)

    async def test_stop_is_idempotent_and_usable_as_a_context_manager(self) -> None:
        with DesktopBridge(self.service, None, enabled=True, heartbeat_interval_ms=0) as bridge:
            await bridge.start()
        bridge.stop()
        self.assertFalse(bridge.started)

    async def test_enabled_defaults_to_the_environment_flag(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(DesktopBridge(self.service).enabled)
            os.environ[DESKTOP_BRIDGE_ENV] = '1'
            self.assertTrue(DesktopBridge(self.service).enabled)


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
