"""M3 端点门控（上游 rc29）：**重启不恢复在线事实** + **fresh-start 例外** + 统一投递门控。

上游依据（`docs/UPSTREAM_SYNC.md` 第 28–31 行）：

| 上游 | 本文件 |
| --- | --- |
| `src/endpoints.ts:229/264/317/372`（`normalizeEndpointState` / `restoreEndpointState` / 两个判据） | `plugin.core.endpoints`（纯函数，`test_endpoints.py` 覆盖纯逻辑；这里覆盖**跨重启的落盘—回读**链路） |
| `src/database.ts:214`（`interlude_endpoint_state`） | `RestartTests`（真库 round-trip） |
| `src/service.ts:7547/7577`（`endpointGateReason` / `endpointForDelivery`）+ `:6204`（注册表不可用即拒） | `OutboundGateTests` |
| `src/service.ts:5013-5030`（Agency 侧接线） | `AgencyInitiateGateTests` |
| `src/service.ts:1001`（`desktopEndpointHealthSnapshot`） | `EndpointHealthProjectionTests` |
| `test/rc10-delivery.test.ts:180`（默认私聊端点先观察 live session 再门控） | `test_a_fresh_start_reply_still_goes_out_and_records_the_endpoint` |

## 为什么这些用例是**反向**用例

每个闸门都必须证明"关掉时真的拦住了"，否则"闸门恒真"的 bug（失败方向是**悄悄放行**）
在测试里不会响。所以：

* `is_endpoint_deliverable` 的 fresh-start 例外**只认那两个 note**——另一条 note
  （`transport-failed`）在线也必须拦住（`FreshStartExceptionTests`），
  删掉例外那两行，fresh-start 那几条必红；
* 投递门控被拒时断言**出站调用次数为 0** + 失败留痕的具体 reason（不是"有报错"）；
* 主动联系的 initiate 闸门过期时断言 reason 是 `endpoint-state-expired`，
  同时断言**没有 initiate 记录时不加闸**（否则 QQ/OneBot 的历史主动路径会被永久拦死）。
"""

from __future__ import annotations

import asyncio
import inspect
import pathlib
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from plugin.core.database import Database  # noqa: E402
from plugin.core.delivery import attach_message_event  # noqa: E402
from plugin.core.endpoints import fresh_endpoint_state  # noqa: E402
from plugin.core.script.commit_builder import (  # noqa: E402
    decision_to_script_commit,
    find_outgoing_script_event,
)
from plugin.core.service import InterludeContext, NullTransport  # noqa: E402
from plugin.core.service import chunk6 as chunk6_module  # noqa: E402
from plugin.core.service.base import ServiceBase, ServiceChunk0  # noqa: E402
from plugin.core.service.chunk2 import ServiceChunk2  # noqa: E402
from plugin.core.service.chunk11 import ServiceChunk11  # noqa: E402
from plugin.core.service.chunk4 import ServiceChunk4  # noqa: E402
from plugin.core.service.chunk5 import ServiceChunk5  # noqa: E402
from plugin.core.service.chunk6 import ServiceChunk6  # noqa: E402
from plugin.core.service.chunk7 import ServiceChunk7  # noqa: E402
from plugin.core.types import empty_participant_state, empty_story_setting, empty_story_state  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
NOW_MS = int(NOW.timestamp() * 1000)

STORY_ID = 'character:onebot:100'
PARTICIPANT_ID = 'onebot:100:200'


def _config() -> dict:
    return {
        'model': {}, 'runtime': {}, 'storyDefaults': {},
        'logging': {'level': 'debug', 'verbosity': 'diagnostic', 'format': 'layered'},
    }


class _RecordingTransport(NullTransport):
    """记录出站调用（默认全部成功）。"""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_private(self, participant, content, reply_to=None, **kwargs):
        self.sent.append({'kind': 'private', 'participant': participant, 'content': content})
        return {'ok': True, 'error': None}

    async def send_session(self, session, content, **kwargs):
        self.sent.append({'kind': 'session', 'session': session, 'content': content})
        return {'ok': True, 'error': None}

    async def send_group(self, channel_id, content, reply_to=None, **kwargs):
        self.sent.append({'kind': 'group', 'channel': channel_id, 'content': content})
        return {'ok': True, 'error': None}


class _GateHarness(ServiceChunk4, ServiceChunk6, ServiceChunk7, ServiceChunk11, ServiceChunk0):
    """真实实现 + 真库 + 记录型 Transport，只把**报告出口**换成内存列表。

    `ServiceBase.__init__` 显式调用：`ServiceChunk0.__init__` 会注册后台计时器，
    这些用例不需要（同 `test_service_chunk6._DbHarness` 的做法）。
    """

    def __init__(self, ctx, config, db=None, transport=None) -> None:
        ServiceBase.__init__(self, ctx, config, db, transport)
        self.reports: list[str] = []
        self.failures: list[tuple] = []

    # ---- 报告出口（只换 sink，不改判据） ----
    def report(self, level, story, phase, message, *args) -> None:
        self.reports.append(message % args if args else message)

    def report_operation(self, verbosity, level, story, phase, message, *args) -> None:
        self.reports.append(message % args if args else message)

    def report_standalone(self, level, message, *args, **kwargs) -> None:
        self.reports.append(message % args if args else message)

    def report_standalone_operation(self, verbosity, level, message, *args) -> None:
        self.reports.append(message % args if args else message)

    async def record_outgoing_delivery_failure(self, story, participant_id, message, reason) -> None:
        self.failures.append((participant_id, reason))


async def _flush_state_writes(service) -> None:
    """把 `persist_endpoint_state` 排下的写盘任务全部跑完（fire-and-forget 的护栏）。"""
    for _ in range(20):
        pending = list(getattr(service, '_endpoint_state_tasks', None) or ())
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


class _GateTestCase(unittest.IsolatedAsyncioTestCase):
    """真库夹具：一个 story + 一个 participant（都有 accountKey `onebot:100`）。"""

    async def asyncSetUp(self) -> None:
        self.db = Database(':memory:')
        self.addCleanup(self.db.close)
        self.db.register_tables()
        self.transport = _RecordingTransport()
        self.db.insert('interlude_story', {
            'id': STORY_ID, 'platform': 'onebot', 'selfId': '100', 'userId': '',
            'channelId': '', 'status': 'active', 'setting': empty_story_setting(),
            'state': empty_story_state(), 'cursorAt': NOW, 'createdAt': NOW, 'updatedAt': NOW,
        })
        self.db.insert('interlude_participant', {
            'id': PARTICIPANT_ID, 'storyId': STORY_ID, 'platform': 'onebot', 'selfId': '100',
            'userId': '200', 'channelId': 'private:200', 'personId': '200',
            'displayName': '主人', 'profile': '', 'relationship': '',
            'state': empty_participant_state(), 'status': 'active',
            'createdAt': NOW, 'updatedAt': NOW,
        })
        self.service = self._harness()

    def _harness(self) -> _GateHarness:
        ctx = InterludeContext(logger=None, database=self.db, clock=lambda: NOW)
        return _GateHarness(ctx, _config(), self.db, self.transport)

    def _story(self) -> dict:
        return {'id': STORY_ID, 'platform': 'onebot', 'selfId': '100'}

    def _participant(self) -> dict:
        return self.db.get('interlude_participant', {'id': PARTICIPANT_ID})

    @staticmethod
    def _endpoint_for(service, owner_kind: str, owner_id: str) -> str:
        for row in service.endpoint_rows:
            if row.get('ownerKind') == owner_kind and row.get('ownerId') == owner_id:
                return row['id']
        raise AssertionError('注册表里没有 %s:%s 的端点' % (owner_kind, owner_id))

    async def _ready(self, service=None):
        service = service or self.service
        await service.ensure_endpoint_registry()
        return service

    def _session(self, platform: str = 'onebot', self_id: str = '100'):
        return {'platform': platform, 'selfId': self_id, 'channelId': 'private:200'}


# =========================================================================== #
# 1. 重启：诊断快照留着，在线事实归零
# =========================================================================== #

class RestartTests(_GateTestCase):
    """上游 `test/endpoints.test.ts:93`（"never restores online truth across a restart"）。"""

    async def test_restart_keeps_the_delivery_snapshot_but_zeroes_online(self) -> None:
        service = await self._ready()
        endpoint_id = self._endpoint_for(service, 'participant-user', PARTICIPANT_ID)
        # 一次成功的出站 → deliverable.allowed 立起来（诊断事实）。
        service.note_endpoint_outbound(
            'participant-user', PARTICIPANT_ID, True, 'delivered',
            {'platform': 'onebot', 'selfId': '100'},
        )
        # 连接器报在线 → connection.online 立起来（**活**事实）。
        service.note_endpoint_connection('onebot:100', True)
        await _flush_state_writes(service)
        live = service.endpoint_states[endpoint_id]
        self.assertIs(live['connection']['online'], True)
        self.assertIs(live['deliverable']['allowed'], True)

        # 落盘的那份快照**确实**记着"在线"——所以下面的归零只可能来自 restore。
        row = self.db.get('interlude_endpoint_state', {'endpointId': endpoint_id})
        self.assertIsNotNone(row, '状态快照必须真的落盘（否则重启这条链路是假的）')
        self.assertIs(row['state']['connection']['online'], True)
        self.assertIs(row['state']['deliverable']['allowed'], True)

        # —— 重启：同一份库，全新的进程内状态 ——
        restarted = await self._ready(self._harness())
        restored = restarted.endpoint_states[endpoint_id]
        self.assertIs(restored['connection']['online'], False, '绝不跨重启恢复在线事实')
        self.assertEqual(restored['connection']['observed_at'], NOW_MS, '观测时刻记为本次启动')
        self.assertIs(restored['deliverable']['allowed'], True, 'deliverable 诊断快照保留')
        # 成功出站的 deliverable **不带** note（上游 `stateAfterOutbound` 只在失败时写），
        # 所以重启后由 restore 补上占位 note——"原 note 不被覆盖"由纯函数用例
        # （`test_endpoints.py`：note='last-ok' 的快照）钉住。
        self.assertEqual(restored['deliverable']['note'], 'restart-awaiting-connection')

    async def test_an_endpoint_without_a_snapshot_restarts_as_fresh_start(self) -> None:
        service = await self._ready()
        endpoint_id = self._endpoint_for(service, 'story-role', STORY_ID)
        await _flush_state_writes(service)
        restarted = await self._ready(self._harness())
        self.assertEqual(
            restarted.endpoint_states[endpoint_id]['deliverable']['note'], 'fresh-start',
        )
        self.assertIs(restarted.endpoint_states[endpoint_id]['connection']['online'], False)

    async def test_a_corrupt_snapshot_never_blocks_the_registry(self) -> None:
        """坏快照**不阻塞**注册表启动（上游 `normalizeEndpointState` 的防御性注释）。"""
        service = await self._ready()
        endpoint_id = self._endpoint_for(service, 'story-role', STORY_ID)
        self.db.update(
            'interlude_endpoint_state', {'endpointId': endpoint_id},
            {'state': {'connection': 'garbage', 'deliverable': None}, 'updatedAt': NOW},
        )
        restarted = await self._ready(self._harness())
        self.assertIs(restarted.endpoint_registry_ready, True)
        restored = restarted.endpoint_states[endpoint_id]
        self.assertIs(restored['connection']['online'], False)
        self.assertIs(restored['deliverable']['allowed'], False)


class FreshStartExceptionTests(_GateTestCase):
    """fresh-start / restart-awaiting-connection 例外（上游 `endpoints.ts:317-327`）。

    这条例外是上游自己踩出来的死锁：门控要求"上一次出站成功"才能投递，而第一次
    出站正是刷新那条记录的地方——没有例外，单平台第一条消息永远发不出去。
    """

    async def test_a_connected_fresh_endpoint_is_deliverable(self) -> None:
        service = await self._ready()
        endpoint_id = self._endpoint_for(service, 'participant-user', PARTICIPANT_ID)
        state = service.endpoint_states[endpoint_id]
        self.assertEqual(state['deliverable']['note'], 'fresh-start')
        self.assertEqual(
            service.endpoint_gate_reason(endpoint_id), 'endpoint-offline',
            '还没观测到连接时仍然是 offline（例外不越权）',
        )
        service.note_endpoint_connection('onebot:100', True)
        self.assertIsNone(
            service.endpoint_gate_reason(endpoint_id),
            '刚启动 + 连接器已报到 → 必须放行第一次投递（否则重启即全拦）',
        )

    async def test_restart_awaiting_connection_is_the_same_exception(self) -> None:
        service = await self._ready()
        endpoint_id = self._endpoint_for(service, 'participant-user', PARTICIPANT_ID)
        service.set_endpoint_state(endpoint_id, {
            'endpoint_id': endpoint_id,
            'connection': {'online': True, 'observed_at': NOW_MS},
            'deliverable': {'allowed': False, 'checked_at': NOW_MS,
                            'note': 'restart-awaiting-connection'},
        })
        self.assertIsNone(service.endpoint_gate_reason(endpoint_id))


# =========================================================================== #
# 2. 统一投递门控（私聊出站）
# =========================================================================== #

class OutboundGateTests(_GateTestCase):
    """上游 `service.ts:6268-6320`：门控在**投递之前**，被拒的理由必须点名。"""

    def _message(self, content: str = '收到', **extra) -> dict:
        return {'participant_id': PARTICIPANT_ID, 'content': content, **extra}

    async def test_a_fresh_start_reply_still_goes_out_and_records_the_endpoint(self) -> None:
        """上游 `test/rc10-delivery.test.ts:180` 逐条移植（默认端点先观察 live session）。

        断言三件事：真的发出去了、状态被这次 session 观测刷新成在线、
        解析出来的默认端点被记回草稿（M4 的前置）。
        """
        service = await self._ready()
        endpoint_id = self._endpoint_for(service, 'participant-user', PARTICIPANT_ID)
        message = self._message('收到', user_initiated=True)
        delivered = await service.send_outgoing_messages(
            self._story(), [message], self._participant(), self._session(),
        )
        self.assertEqual([item['content'] for item in delivered], ['收到'])
        self.assertEqual(len(self.transport.sent), 1)
        self.assertEqual(self.transport.sent[0]['kind'], 'session')
        self.assertEqual(delivered[0]['endpoint_id'], endpoint_id)
        self.assertIs(service.endpoint_states[endpoint_id]['connection']['online'], True)

    async def test_a_live_session_for_another_account_is_not_online_evidence(self) -> None:
        """**反向**：session 不是这个端点的（别的 selfId）→ 不许拿它当在线证据。"""
        service = await self._ready()
        endpoint_id = self._endpoint_for(service, 'participant-user', PARTICIPANT_ID)
        await service.send_outgoing_messages(
            self._story(), [self._message()], self._participant(), self._session(self_id='999'),
        )
        self.assertEqual(self.transport.sent, [], '没有在线事实就不许投递')
        self.assertEqual(service.failures, [(PARTICIPANT_ID, 'endpoint-offline')])
        self.assertIs(service.endpoint_states[endpoint_id]['connection']['online'], False)
        self.assertTrue(
            any('消息被端点门控阻止' in item and endpoint_id in item and 'endpoint-offline' in item
                for item in service.reports),
            '被拒时必须留下点名端点与理由的可见 warn：%s' % service.reports,
        )

    async def test_an_explicit_disabled_endpoint_blocks_the_send(self) -> None:
        """**反向**：显式指定的端点被停用 → 一条都不发，理由点名 `endpoint-disabled`。"""
        service = await self._ready()
        endpoint_id = self._endpoint_for(service, 'participant-user', PARTICIPANT_ID)
        self.db.update('interlude_endpoint', {'id': endpoint_id}, {'enabled': False})
        for row in service.endpoint_rows:
            if row['id'] == endpoint_id:
                row['enabled'] = False
        await service.send_outgoing_messages(
            self._story(), [self._message(endpoint_id=endpoint_id)],
            self._participant(), self._session(),
        )
        self.assertEqual(self.transport.sent, [])
        self.assertEqual(service.failures, [(PARTICIPANT_ID, 'endpoint-disabled')])
        self.assertTrue(any(
            '消息被端点门控阻止' in item and endpoint_id in item and 'endpoint-disabled' in item
            for item in service.reports
        ), service.reports)

    async def test_an_explicit_endpoint_belonging_to_someone_else_is_not_found(self) -> None:
        """**反向**：别人（另一个参与者）的端点不是合法硬路由（上游 `endpoint-not-found`）。"""
        self.db.insert('interlude_participant', {
            'id': 'onebot:100:999', 'storyId': STORY_ID, 'platform': 'onebot', 'selfId': '100',
            'userId': '999', 'channelId': 'private:999', 'personId': '999',
            'displayName': '别人', 'profile': '', 'relationship': '',
            'state': empty_participant_state(), 'status': 'active',
            'createdAt': NOW, 'updatedAt': NOW,
        })
        service = await self._ready()
        other = self._endpoint_for(service, 'participant-user', 'onebot:100:999')
        await service.send_outgoing_messages(
            self._story(), [self._message(endpoint_id=other)],
            self._participant(), self._session(),
        )
        self.assertEqual(self.transport.sent, [])
        self.assertEqual(service.failures, [(PARTICIPANT_ID, 'endpoint-not-found')])

    async def test_an_unavailable_registry_blocks_the_whole_batch(self) -> None:
        """上游 `:6204`：注册表不可用时**整批**拒绝并逐条留痕（不退化成"看起来发了"）。"""
        service = await self._ready()
        calls: list = []

        async def _broken() -> None:
            raise RuntimeError('端点表读不了')

        service.ensure_endpoint_registry = _broken  # type: ignore[assignment]
        service.endpoint_registry_ready = False
        delivered = await service.send_outgoing_messages(
            self._story(), [self._message('a'), self._message('b')], self._participant(),
        )
        self.assertEqual(delivered, [])
        self.assertEqual(self.transport.sent, [])
        self.assertEqual(service.failures, [
            (PARTICIPANT_ID, 'endpoint-registry-unavailable'),
            (PARTICIPANT_ID, 'endpoint-registry-unavailable'),
        ])
        self.assertTrue(any('出站端点注册表不可用' in item for item in service.reports))

    async def test_a_failed_outbound_lands_in_cooldown_and_blocks_the_retry(self) -> None:
        """**反向**：显式失败进 5 分钟冷却，冷却期内重投被门控拦住。"""
        service = await self._ready()
        endpoint_id = self._endpoint_for(service, 'participant-user', PARTICIPANT_ID)
        service.note_endpoint_connection('onebot:100', True)
        service.note_endpoint_outbound(
            'participant-user', PARTICIPANT_ID, False, 'transport-error: 风控',
            {'platform': 'onebot', 'selfId': '100'},
        )
        self.assertEqual(service.endpoint_gate_reason(endpoint_id), 'endpoint-cooldown')
        await service.send_outgoing_messages(
            self._story(), [self._message('重试')], self._participant(), self._session(),
        )
        self.assertEqual(self.transport.sent, [])
        self.assertEqual(service.failures, [(PARTICIPANT_ID, 'endpoint-cooldown')])


class InitiateGateTests(_GateTestCase):
    """上游 `endpoints.ts:372` / `service.ts:7568`：可选的主动联系闸门。"""

    async def test_expired_or_forbidden_initiate_is_a_hard_stop(self) -> None:
        service = await self._ready()
        endpoint_id = self._endpoint_for(service, 'participant-user', PARTICIPANT_ID)
        service.note_endpoint_connection('onebot:100', True)
        service.endpoint_states[endpoint_id]['initiate'] = {
            'allowed': True, 'observed_at': NOW_MS, 'expires_at': NOW_MS - 1,
        }
        self.assertEqual(
            service.endpoint_initiate_gate_reason(endpoint_id), 'endpoint-state-expired',
        )
        service.endpoint_states[endpoint_id]['initiate'] = {'allowed': False, 'observed_at': NOW_MS}
        self.assertEqual(
            service.endpoint_initiate_gate_reason(endpoint_id), 'endpoint-initiate-forbidden',
        )

    async def test_a_missing_initiate_record_adds_no_extra_gate(self) -> None:
        """**反向的一半**：没有 initiate 记录 = 不额外加闸（不是永久禁止）。"""
        service = await self._ready()
        endpoint_id = self._endpoint_for(service, 'participant-user', PARTICIPANT_ID)
        service.note_endpoint_connection('onebot:100', True)
        self.assertNotIn('initiate', service.endpoint_states[endpoint_id])
        self.assertIsNone(service.endpoint_initiate_gate_reason(endpoint_id))

    async def test_a_background_message_still_passes_an_allowed_initiate(self) -> None:
        """非用户触发的后台消息在有有效 initiate 时照常投递（闸门不是"后台一律禁"）。"""
        service = await self._ready()
        endpoint_id = self._endpoint_for(service, 'participant-user', PARTICIPANT_ID)
        service.note_endpoint_connection('onebot:100', True)
        service.endpoint_states[endpoint_id]['initiate'] = {
            'allowed': True, 'observed_at': NOW_MS, 'expires_at': NOW_MS + 60_000,
        }
        await service.send_outgoing_messages(
            self._story(), [self._message()], self._participant(), self._session(),
        )
        self.assertEqual(len(self.transport.sent), 1)

    async def test_a_background_message_is_blocked_by_an_expired_initiate(self) -> None:
        """**反向**：同样一条后台消息，initiate 过期 → 一条都不发 + 理由可见。"""
        service = await self._ready()
        endpoint_id = self._endpoint_for(service, 'participant-user', PARTICIPANT_ID)
        service.note_endpoint_connection('onebot:100', True)
        service.endpoint_states[endpoint_id]['initiate'] = {
            'allowed': True, 'observed_at': NOW_MS, 'expires_at': NOW_MS - 1,
        }
        await service.send_outgoing_messages(
            self._story(), [self._message()], self._participant(), self._session(),
        )
        self.assertEqual(self.transport.sent, [])
        self.assertEqual(service.failures, [(PARTICIPANT_ID, 'endpoint-state-expired')])
        self.assertTrue(any('消息被主动联系端点门控阻止' in item for item in service.reports))

    def _message(self, content: str = '后台消息', **extra) -> dict:
        return {'participant_id': PARTICIPANT_ID, 'content': content, **extra}


class GroupDeliveryGateTests(_GateTestCase):
    """群路的门控（上游 `service.ts:2761-2825`）。

    `chunk2.send_group_message` 的接线段由 Lead 落补丁（本次不碰那个文件），
    所以这里钉的是**它要调用的那个方法**：`chunk11.group_delivery_gate`。
    """

    GROUP_CHANNEL = 'group:123'

    def _group_session(self, channel: str = 'group:123', self_id: str = '100'):
        return {'platform': 'onebot', 'selfId': self_id, 'channelId': channel, 'guildId': channel}

    async def test_a_fresh_group_session_lets_the_first_group_message_through(self) -> None:
        service = self._harness()
        role = self._endpoint_for(await self._ready(service), 'story-role', STORY_ID)
        self.assertIsNone(
            await service.group_delivery_gate(self._story(), self.GROUP_CHANNEL, self._group_session()),
        )
        self.assertIs(service.endpoint_states[role]['connection']['online'], True)

    async def test_a_group_message_without_a_session_stays_blocked(self) -> None:
        """**反向**：没有 live session 就没有在线证据 → 群消息被拦（理由可见）。"""
        service = await self._ready()
        self.assertEqual(
            await service.group_delivery_gate(self._story(), self.GROUP_CHANNEL, None),
            'endpoint-offline',
        )

    async def test_a_session_from_another_group_is_not_online_evidence(self) -> None:
        """**反向**：私聊回合发起的跨群动作不是这个群的有效传输会话。"""
        service = self._harness()
        await self._ready(service)
        self.assertEqual(
            await service.group_delivery_gate(
                self._story(), self.GROUP_CHANNEL, self._group_session('group:999'),
            ),
            'endpoint-offline',
        )

    async def test_a_disabled_role_endpoint_blocks_the_group_message(self) -> None:
        service = await self._ready()
        role = self._endpoint_for(service, 'story-role', STORY_ID)
        for row in service.endpoint_rows:
            if row['id'] == role:
                row['enabled'] = False
        # 停用后 `endpointAddressSync` 不再命中（它只挑启用行）→ 默认路由回落到旧字段，
        # 这正是上游单平台零影响的形状：门控只在注册表真的选出端点时才生效。
        self.assertIsNone(
            await service.group_delivery_gate(self._story(), self.GROUP_CHANNEL, self._group_session()),
        )

    async def test_an_unavailable_registry_blocks_the_group_message(self) -> None:
        service = self._harness()

        async def _broken() -> None:
            raise RuntimeError('端点表读不了')

        service.ensure_endpoint_registry = _broken  # type: ignore[assignment]
        reason = await service.group_delivery_gate(
            self._story(), self.GROUP_CHANNEL, self._group_session(),
        )
        self.assertEqual(reason, 'endpoint-registry-unavailable')
        self.assertTrue(any('群消息端点注册表不可用' in item for item in service.reports))


class _GroupSendHarness(ServiceChunk2, _GateHarness):
    """`_GateHarness` 再加 `ServiceChunk2`：群路出站 `send_group_message` 在那一支。

    排在最左边只影响这一组用例（方法名与其它 chunk 无重叠），跑的是**真实**群发实现
    + 真实 `chunk11` 判据 + 真库，不是替身。
    """


# =========================================================================== #
# 3b. M4：群路显式 `endpointId`（硬路由）+ 默认路由
# =========================================================================== #
class GroupExplicitEndpointTests(_GateTestCase):
    """群路的显式端点（上游 `service.ts:2787-2801`，清单第 89/91 行）。

    | 上游 | 本文件 |
    | --- | --- |
    | `:2787-2801`（显式端点 = 硬路由，选不中即 `endpoint-not-found`） | `test_an_explicit_endpoint_from_another_story_is_rejected` / `..._in_another_group_...` |
    | `:6297`（门控**之前**先观察 live session） | `test_an_explicit_group_endpoint_observes_the_live_session_before_gating` |
    | `:6366`（选中后永不回落） | `test_a_missing_explicit_endpoint_never_falls_back_to_the_default_route` |
    | `test/rc10-delivery.test.ts:249`（默认群端点同理） | `test_the_default_group_route_observes_the_session_before_gating` |

    每条都带反向：session 不是这个端点的 / 没有 session / 端点不属于本群或本故事 /
    注册表不可用 —— 全部断言**出站调用次数为 0** 且理由可见，不许"看起来发了"。
    """

    GROUP_CHANNEL = 'group:123'

    def _harness(self) -> _GroupSendHarness:  # type: ignore[override]
        ctx = InterludeContext(logger=None, database=self.db, clock=lambda: NOW)
        return _GroupSendHarness(ctx, _config(), self.db, self.transport)

    def _group_session(self, channel: str = 'group:123', self_id: str = '100'):
        return {'platform': 'onebot', 'selfId': self_id, 'channelId': channel, 'guildId': channel}

    def _inject_group_endpoint(self, service, endpoint_id: str = 'ep-group',
                               group: str = '123', owner_story: str = '') -> str:
        """往注册表塞一条群端点（上游测试直接摆 `endpointRows` 的等价做法）。"""
        owner_kind = 'story-role' if owner_story else 'group'
        row = {
            'id': endpoint_id, 'ownerKind': owner_kind,
            'ownerId': owner_story or group,
            'channelKind': 'qq', 'platform': 'onebot', 'accountKey': 'onebot:100',
            'selfId': '100', 'conversationKind': 'group' if not owner_story else 'unknown',
            'enabled': True, 'createdAt': NOW, 'updatedAt': NOW,
        }
        if not owner_story:
            row['groupId'] = group
        service.endpoint_rows = [*service.endpoint_rows, row]
        service.endpoint_states[endpoint_id] = fresh_endpoint_state(endpoint_id, NOW_MS)
        return endpoint_id

    def _group_sends(self, service) -> list:
        return [item for item in self.transport.sent if item['kind'] == 'group']

    # ---- ① 显式端点：门控前观测 live session，观测到位才发 ----
    async def test_an_explicit_group_endpoint_observes_the_live_session_before_gating(self) -> None:
        service = await self._ready()
        self._inject_group_endpoint(service)
        result = await service.send_group_message(
            self._story(), self.GROUP_CHANNEL, '群里收到', None,
            self._group_session(), 'ep-group',
        )
        self.assertTrue(result['complete'])
        self.assertEqual(self._group_sends(service)[0]['channel'], self.GROUP_CHANNEL)
        self.assertIs(
            service.endpoint_states['ep-group']['connection']['online'], True,
            'live 群会话必须在门控**之前**被记成在线证据',
        )

    async def test_an_explicit_group_endpoint_without_a_matching_session_is_blocked(self) -> None:
        """**反向**：同一支显式端点，session 的 selfId 不是这个端点 → 没有在线证据 → 一条不发。"""
        service = await self._ready()
        self._inject_group_endpoint(service)
        result = await service.send_group_message(
            self._story(), self.GROUP_CHANNEL, '群里收到', None,
            self._group_session(self_id='999'), 'ep-group',
        )
        self.assertFalse(result['complete'])
        self.assertEqual(self._group_sends(service), [])
        self.assertEqual(result['segment_outcomes'][0]['reason'], 'endpoint-offline')
        self.assertTrue(any('群消息被端点门控阻止' in item and 'endpoint-offline' in item
                            for item in service.reports))

    async def test_an_explicit_group_endpoint_without_any_session_is_blocked(self) -> None:
        """**反向**（上游 `:2802`）：拿不到 session 就没有在线证据 → 理由可见、不静默。"""
        service = await self._ready()
        self._inject_group_endpoint(service)
        result = await service.send_group_message(
            self._story(), self.GROUP_CHANNEL, '群里收到', None, None, 'ep-group',
        )
        self.assertEqual(self._group_sends(service), [])
        self.assertEqual(result['segment_outcomes'][0]['reason'], 'endpoint-offline')
        self.assertTrue(any('群消息被端点门控阻止' in item and 'endpoint-offline' in item
                            for item in service.reports))

    # ---- ③ 选不中 = 硬失败；④ 他故事 / 他群的端点被拒 ----
    async def test_an_explicit_endpoint_from_another_story_is_rejected(self) -> None:
        """上游 `test/rc10-delivery.test.ts:85` 逐条搬：他故事的端点 → `endpoint-not-found`、sends=0。"""
        service = await self._ready()
        self._inject_group_endpoint(service, 'ep-other', owner_story='other-story')
        result = await service.send_group_message(
            self._story(), self.GROUP_CHANNEL, '群里收到', None,
            self._group_session(), 'ep-other',
        )
        self.assertFalse(result['complete'])
        self.assertEqual(result['segment_outcomes'][0]['reason'], 'endpoint-not-found')
        self.assertEqual(self._group_sends(service), [])
        self.assertTrue(any('群消息指定端点无效或不属于目标群' in item for item in service.reports))

    async def test_an_explicit_endpoint_registered_for_another_group_is_rejected(self) -> None:
        """**反向**：群端点只对**它自己那个群**是合法硬路由（`normalizeGroupId` 等价形态）。"""
        service = await self._ready()
        self._inject_group_endpoint(service, 'ep-group-999', group='999')
        result = await service.send_group_message(
            self._story(), self.GROUP_CHANNEL, '群里收到', None,
            self._group_session(), 'ep-group-999',
        )
        self.assertEqual(result['segment_outcomes'][0]['reason'], 'endpoint-not-found')
        self.assertEqual(self._group_sends(service), [])

    async def test_a_missing_explicit_endpoint_never_falls_back_to_the_default_route(self) -> None:
        """**反向（回退）**：默认路由本来是通的，显式端点选不中时仍然**一条都不发**。

        上游 `:6366`："Once a caller/model has selected an endpoint … must never fall back"。
        先跑一次不带 `endpoint_id` 的对照（证明默认路由此刻真的能发），再用一个不存在的
        显式端点跑同一条件——若实现偷偷回落，第二次会照发，本用例立刻红。
        """
        service = await self._ready()
        control = await service.send_group_message(
            self._story(), self.GROUP_CHANNEL, '默认路由', None, self._group_session(),
        )
        self.assertTrue(control['complete'], '前提：默认路由此刻是通的')
        self.assertEqual(len(self._group_sends(service)), 1)

        result = await service.send_group_message(
            self._story(), self.GROUP_CHANNEL, '显式路由', None,
            self._group_session(), 'ep-ghost',
        )
        self.assertFalse(result['complete'])
        self.assertEqual(result['segment_outcomes'][0]['reason'], 'endpoint-not-found')
        self.assertEqual(len(self._group_sends(service)), 1, '硬失败：不许回落到默认路由再发一条')

    async def test_an_explicit_disabled_group_endpoint_is_blocked(self) -> None:
        """**反向**：端点存在且属于本群，但被停用 → 硬失败（理由 `endpoint-disabled`）。"""
        service = await self._ready()
        self._inject_group_endpoint(service)
        for row in service.endpoint_rows:
            if row['id'] == 'ep-group':
                row['enabled'] = False
        result = await service.send_group_message(
            self._story(), self.GROUP_CHANNEL, '群里收到', None,
            self._group_session(), 'ep-group',
        )
        self.assertFalse(result['complete'])
        self.assertEqual(result['segment_outcomes'][0]['reason'], 'endpoint-disabled')
        self.assertEqual(self._group_sends(service), [])

    # ---- ⑤ 显式端点成功 → 端点被记回草稿（与私聊同形） ----
    async def test_a_delivered_explicit_group_endpoint_is_recorded_back_onto_the_draft(self) -> None:
        service = await self._ready()
        self._inject_group_endpoint(service)
        draft: dict = {}
        result = await service.send_group_message(
            self._story(), self.GROUP_CHANNEL, '群里收到', None,
            self._group_session(), 'ep-group', draft=draft,
        )
        self.assertTrue(result['complete'])
        self.assertEqual(draft.get('endpoint_id'), 'ep-group')

    async def test_a_blocked_group_message_leaves_the_draft_untouched(self) -> None:
        """**反向**：被门控拦下时草稿不许被写上一条"其实走了"的端点。"""
        service = await self._ready()
        self._inject_group_endpoint(service)
        draft: dict = {}
        result = await service.send_group_message(
            self._story(), self.GROUP_CHANNEL, '群里收到', None, None, 'ep-group', draft=draft,
        )
        self.assertFalse(result['complete'])
        self.assertEqual(draft, {})

    # ---- ② 默认路由（端到端）：与 M3 补丁的语义一致 ----
    async def test_the_default_group_route_observes_the_session_before_gating(self) -> None:
        """上游 `test/rc10-delivery.test.ts:249`：默认群端点先观察再判，立即回复不被误判离线。"""
        service = self._harness()
        role = self._endpoint_for(await self._ready(service), 'story-role', STORY_ID)
        result = await service.send_group_message(
            self._story(), self.GROUP_CHANNEL, '群里收到', None, self._group_session(),
        )
        self.assertTrue(result['complete'])
        self.assertEqual(self._group_sends(service)[0]['channel'], self.GROUP_CHANNEL)
        self.assertIs(service.endpoint_states[role]['connection']['online'], True)

    async def test_the_default_group_route_without_a_session_stays_blocked_and_visible(self) -> None:
        """**反向**：全新进程、没有 live session → 一条不发，理由可见（§92.5 的同一口径）。"""
        service = await self._ready()
        result = await service.send_group_message(
            self._story(), self.GROUP_CHANNEL, '群里收到', None, None,
        )
        self.assertEqual(self._group_sends(service), [])
        self.assertEqual(result['segment_outcomes'][0]['reason'], 'endpoint-offline')
        self.assertTrue(any('群消息被端点门控阻止' in item and 'endpoint-offline' in item
                            for item in service.reports))

    async def test_the_default_group_route_without_a_registry_still_reports_visibly(self) -> None:
        """**反向**（不许静默）：注册表不可用 → 端到端一条不发 + 端点层带详情留 warn。"""
        service = self._harness()

        async def _broken() -> None:
            raise RuntimeError('端点表读不了')

        service.ensure_endpoint_registry = _broken  # type: ignore[assignment]
        result = await service.send_group_message(
            self._story(), self.GROUP_CHANNEL, '群里收到', None, self._group_session(),
        )
        self.assertEqual(self._group_sends(service), [])
        self.assertEqual(result['segment_outcomes'][0]['reason'], 'endpoint-registry-unavailable')
        self.assertTrue(any('群消息端点注册表不可用' in item and '端点表读不了' in item
                            for item in service.reports),
                        '必须留下"哪一层缺、错误是什么"的可见 warn：%s' % service.reports)

    async def test_a_default_route_never_writes_an_endpoint_onto_the_draft_when_none_is_registered(self) -> None:
        """**反向**：没有注册端点（`route == {}`）时草稿不许凭空多出一个端点 id。"""
        service = await self._ready()
        service.endpoint_rows = []
        service.endpoint_states = {}
        draft: dict = {}
        await service.send_group_message(
            self._story(), self.GROUP_CHANNEL, '群里收到', None, self._group_session(), draft=draft,
        )
        self.assertEqual(draft, {})
        self.assertEqual(len(self._group_sends(service)), 1, '没有注册表 = 旧路径照发')


# =========================================================================== #
# 3c. U17：跨群出站（上游 `service.ts:6162`，rc28 `:5790`）
# =========================================================================== #

def _group_dispatch_wired() -> bool:
    """`chunk6.send_outgoing_messages` 的**群目标分支**是否已落地。

    U17 的第二跳（上游 `:6223`）必须插在私聊参与者查表**之前**，而 `chunk6.py` 是
    M3/M4 已收口的文件（只读；改动以逐字补丁交给 Lead）。补丁落地前这里如实 skip，
    落地后同一批用例立刻转成真跑——两个方向都不会假装通过。
    """
    try:
        source = inspect.getsource(chunk6_module)
    except OSError:  # pragma: no cover - 源码不可读时不假装通过
        return False
    return 'send_cross_group_message' in source


_GROUP_DISPATCH_WIRED = _group_dispatch_wired()


class _FailingGroupTransport(_RecordingTransport):
    """群路出站**试过但失败**的 transport（用来钉住失败也要记回端点状态）。"""

    async def send_group(self, channel_id, content, reply_to=None, **kwargs):
        await _RecordingTransport.send_group(self, channel_id, content, reply_to, **kwargs)
        return {'ok': False, 'error': '风控拦截'}


class _VoiceRecordingTransport(_RecordingTransport):
    """额外记住每段是不是语音（正文 `<tts/>` 意图的落点）。"""

    async def send_group(self, channel_id, content, reply_to=None, **kwargs):
        result = await _RecordingTransport.send_group(self, channel_id, content, reply_to, **kwargs)
        self.sent[-1]['voice'] = kwargs.get('voice')
        return result


class _CrossGroupHarness(ServiceChunk5, ServiceChunk2, _GateHarness):
    """跨群出站要的那几个 chunk：`send_outgoing_messages`(6) + `append_entry`(5) + 群发(2)。"""


class CrossGroupMessageTests(_GateTestCase):
    """她主动去**另一个群**说话：群路出站 + 显式端点 + 记回端点状态（上游 `sendCrossGroupMessage`）。

    | 上游 | 本文件 |
    | --- | --- |
    | `:6164`（`groupChats` 是群的唯一准入） | `test_a_group_outside_the_rule_list_is_refused_visibly` |
    | `:6166`（显式端点透传给 `sendGroupMessage` = 硬路由） | `test_an_undeliverable_explicit_endpoint_never_falls_back` |
    | `:6170-6174`（逐段结局写回剧本事件，含理由） | `test_a_blocked_group_message_leaves_per_segment_reasons` |
    | `:2862`（成功 `noteEndpointOutbound(..., 'group-delivered')`） | `test_a_delivered_group_message_records_the_endpoint_state` |
    | `:2872`（失败同样记回，带错误串 + 冷却） | `test_a_failed_group_send_lands_in_the_endpoint_state_with_the_reason` |
    | `:6223`（群目标在私聊出站里单独一支） | `CrossGroupDispatchTests`（补丁落地后真跑） |

    每条都带反向：选不中 → 一条都不发（即使默认路由此刻是通的）；名单外 → 发不出去且
    理由可见；出站成功后端点状态必须变，而且只变**实际走的那条**；没接端点层 / 被门控
    拒绝 → 一个字都不写（不许把"没试过"记成"试过并失败"）。
    """

    GROUP_CHANNEL = 'group:123'

    # ---- 夹具：群 123 在群聊名单里（群端点由注册表派生） ----
    def _config_with_groups(self) -> dict:
        config = _config()
        config['onebot'] = {'groupChats': [{'groupId': '123', 'enabled': True}]}
        return config

    def _harness(self) -> _CrossGroupHarness:  # type: ignore[override]
        ctx = InterludeContext(logger=None, database=self.db, clock=lambda: NOW)
        return _CrossGroupHarness(ctx, self._config_with_groups(), self.db, self.transport)

    def _group_endpoint(self, service, group: str = '123') -> str:
        for row in service.endpoint_rows:
            if row['ownerKind'] == 'group' and str(row.get('groupId')) == group:
                return row['id']
        raise AssertionError('注册表里没有群 %s 的端点：%s' % (group, service.endpoint_rows))

    def _inject_group_endpoint(self, service, endpoint_id: str = 'ep-extra',
                               group: str = '123') -> str:
        """再塞一条同群的群端点（上游测试直接摆 `endpointRows` 的等价做法）。"""
        service.endpoint_rows = [*service.endpoint_rows, {
            'id': endpoint_id, 'ownerKind': 'group', 'ownerId': group, 'groupId': group,
            'channelKind': 'qq', 'platform': 'onebot', 'accountKey': 'onebot:100',
            'selfId': '100', 'conversationKind': 'group', 'enabled': True,
            'createdAt': NOW, 'updatedAt': NOW,
        }]
        service.endpoint_states[endpoint_id] = fresh_endpoint_state(endpoint_id, NOW_MS)
        return endpoint_id

    def _message(self, content: str = '我在另一个群说一句', endpoint_id: str = '') -> dict:
        message: dict = {'participant_id': self.GROUP_CHANNEL, 'content': content}
        if endpoint_id:
            message['endpoint_id'] = endpoint_id
        return message

    def _group_sends(self, service=None) -> list:
        return [item for item in self.transport.sent if item['kind'] == 'group']

    # ---- ① 真的发到那个端点 / 那个群 ----
    async def test_she_delivers_into_the_other_group_on_the_explicit_endpoint(self) -> None:
        """端到端：显式群端点 + 群在名单里 → 真的发到那个群，且只记回**那条**端点。"""
        service = self._harness()
        await self._ready(service)
        service.note_endpoint_connection('onebot:100', True)
        group_endpoint = self._group_endpoint(service)
        message = self._message('我在另一个群说一句', group_endpoint)

        outcome = await service.send_cross_group_message(self._story(), message)

        self.assertTrue(outcome['complete'])
        self.assertEqual([item['channel'] for item in self._group_sends(service)], ['123'])
        self.assertEqual(self._group_sends(service)[0]['content'], '我在另一个群说一句')
        self.assertEqual(message['endpoint_id'], group_endpoint, '实际走的端点被记回草稿')
        # 剧本条目按上游形状落库（群号/频道/分段索引/部分投递）。
        entries = self.db.all('interlude_script_entry', {'storyId': STORY_ID})
        group_entries = [row for row in entries if row['kind'] == 'character-group-message']
        self.assertEqual(len(group_entries), 1)
        self.assertEqual(group_entries[0]['content'], '我在另一个群说一句')
        self.assertEqual(group_entries[0]['metadata']['groupId'], '123')
        self.assertEqual(group_entries[0]['metadata']['channelId'], '123')
        self.assertEqual(group_entries[0]['metadata']['deliverySegmentIndexes'], [0])
        self.assertIs(group_entries[0]['metadata']['partialDelivery'], False)
        # **只记回实际走的那条**：默认（角色）端点不许被这次投递刷成"用过了"。
        role = self._endpoint_for(service, 'story-role', STORY_ID)
        self.assertIs(service.endpoint_states[group_endpoint]['deliverable']['allowed'], True)
        self.assertIs(
            service.endpoint_states[role]['deliverable']['allowed'], False,
            '默认端点没被走过，不许被记成用过',
        )

    # ---- ② 选不中 → 一条都不发（反向：允许回落 → 红） ----
    async def test_an_undeliverable_explicit_endpoint_never_falls_back(self) -> None:
        """**反向（回落）**：显式端点选不中时，即使默认路由是通的也**一条都不发**。

        对照组先证明默认路由此刻真的能发；随后走两支硬失败——①**存在、属于本群、但没
        观测到连接**的端点（`endpoint-offline`）；②**根本不存在**的端点
        （`endpoint-not-found`）。任何一支偷偷回落，出站计数都会变成 2，本用例立刻红。
        """
        service = self._harness()
        await self._ready(service)
        service.note_endpoint_connection('onebot:100', True)
        control = await service.send_cross_group_message(self._story(), self._message('默认路由'))
        self.assertTrue(control['complete'], '前提：默认路由此刻真的能发')
        self.assertEqual(len(self._group_sends(service)), 1)

        offline = self._inject_group_endpoint(service)
        result = await service.send_cross_group_message(
            self._story(), self._message('显式路由', offline),
        )
        self.assertFalse(result['complete'])
        self.assertEqual(result['segment_outcomes'][0]['reason'], 'endpoint-offline')
        self.assertEqual(len(self._group_sends(service)), 1, '硬失败：不许回落到默认路由再发一条')
        self.assertTrue(any('群消息被端点门控阻止' in item and 'endpoint-offline' in item
                            for item in service.reports), service.reports)

        ghost = await service.send_cross_group_message(
            self._story(), self._message('幽灵端点', 'ep-ghost'),
        )
        self.assertFalse(ghost['complete'])
        self.assertEqual(ghost['segment_outcomes'][0]['reason'], 'endpoint-not-found')
        self.assertEqual(len(self._group_sends(service)), 1, '选不中就是选不中，不许换一条发')
        self.assertTrue(any('群消息指定端点无效或不属于目标群' in item
                            for item in service.reports), service.reports)

    # ---- ③ 群路出站成功 → 端点状态被记回 ----
    async def test_a_delivered_group_message_records_the_endpoint_state(self) -> None:
        """上游 `:2862`：群路出站成功必须记回端点状态（deliverable 维）。"""
        service = self._harness()
        await self._ready(service)
        service.note_endpoint_connection('onebot:100', True)
        group_endpoint = self._group_endpoint(service)
        before = service.endpoint_states[group_endpoint]['deliverable']['allowed']
        self.assertIs(before, False, '前提：fresh-start 还没被出站刷新过')

        await service.send_cross_group_message(
            self._story(), self._message('群里见', group_endpoint),
        )

        state = service.endpoint_states[group_endpoint]['deliverable']
        self.assertIs(state['allowed'], True)
        self.assertIsNone(service.endpoint_gate_reason(group_endpoint), '记回之后是可投递的')

    async def test_a_failed_group_send_lands_in_the_endpoint_state_with_the_reason(self) -> None:
        """**反向（失败也要记）**：出站真的试过但失败 → 端点进冷却，note 带错误串。"""
        service = self._harness()
        await self._ready(service)
        service.note_endpoint_connection('onebot:100', True)
        group_endpoint = self._group_endpoint(service)
        failing = _FailingGroupTransport()
        service.transport = failing

        outcome = await service.send_cross_group_message(
            self._story(), self._message('群里见', group_endpoint),
        )

        self.assertFalse(outcome['complete'])
        self.assertEqual(len([item for item in failing.sent if item['kind'] == 'group']), 1,
                         '失败也是"试过了"')
        state = service.endpoint_states[group_endpoint]['deliverable']
        self.assertIs(state['allowed'], False)
        self.assertIn('风控拦截', state['note'])
        self.assertEqual(service.endpoint_gate_reason(group_endpoint), 'endpoint-cooldown')

    async def test_a_gate_refusal_never_poisons_the_endpoint_state(self) -> None:
        """**反向（不许把"没试过"记成"试过并失败"）**：被门控拒绝 → 端点状态不写冷却。"""
        service = self._harness()
        await self._ready(service)
        group_endpoint = self._group_endpoint(service)  # fresh-start：没有在线证据
        outcome = await service.send_cross_group_message(
            self._story(), self._message('群里见', group_endpoint),
        )
        self.assertFalse(outcome['complete'])
        self.assertEqual(outcome['segment_outcomes'][0]['reason'], 'endpoint-offline')
        state = service.endpoint_states[group_endpoint]['deliverable']
        self.assertNotIn('cooldown_until', state)
        self.assertEqual(state.get('note'), 'fresh-start', '门控拒绝不动 deliverable 记录')

    # ---- ④ 注册表不可用 → 硬失败 + 逐段留痕 + 可见理由 ----
    async def test_a_blocked_group_message_leaves_per_segment_reasons(self) -> None:
        """上游 `:6170-6174`：注册表不可用时整条硬失败，**每一段**都留可见理由。"""
        service = self._harness()
        await self._ready(service)

        async def _broken() -> None:
            raise RuntimeError('端点表读不了')

        service.ensure_endpoint_registry = _broken  # type: ignore[assignment]
        service.endpoint_registry_ready = False
        recorded: list = []

        async def _record(story_id, reference, status, at, reason=None):
            recorded.append((reference.get('segment_index'), status, reason))

        service.update_script_delivery_outcome = _record  # type: ignore[assignment]
        message = self._message('第一段<sep/>第二段')
        message['script_event'] = {'commit_id': 'commit:u17', 'event_id': 'commit:u17:e1'}

        outcome = await service.send_cross_group_message(self._story(), message)

        self.assertFalse(outcome['complete'])
        self.assertEqual(self._group_sends(service), [])
        self.assertEqual(
            [item['reason'] for item in outcome['segment_outcomes']],
            ['endpoint-registry-unavailable', 'endpoint-registry-unavailable'],
        )
        self.assertEqual(recorded, [
            (0, 'failed', 'endpoint-registry-unavailable'),
            (1, 'failed', 'endpoint-registry-unavailable'),
        ], '逐段留痕（不是只报一句"失败了"）')
        self.assertTrue(any('群消息端点注册表不可用' in item for item in service.reports))
        self.assertTrue(any('跨群消息投递未完成（不自动重发）' in item for item in service.reports))

    async def test_a_group_outside_the_rule_list_is_refused_visibly(self) -> None:
        """上游 `:6164`：群聊名单是群的唯一准入 → 逐段 `group-not-allowed` + 可见理由。"""
        service = self._harness()
        await self._ready(service)
        service.note_endpoint_connection('onebot:100', True)
        message = {'participant_id': 'group:999', 'content': '另一个群'}

        outcome = await service.send_cross_group_message(self._story(), message)

        self.assertFalse(outcome['complete'])
        self.assertEqual(self._group_sends(service), [])
        self.assertEqual(outcome['segment_outcomes'][0]['reason'], 'group-not-allowed')
        self.assertTrue(
            any('跨群消息被群聊名单阻止' in item and 'group-not-allowed' in item
                for item in service.reports),
            '被名单挡下必须看得见（否则真机上只是"她没说话"）：%s' % service.reports,
        )

    # ---- ①b 多气泡 / 语音：整条都发出去，一段都不许静默丢 ----
    async def test_a_pre_split_cross_group_draft_loses_no_bubble_and_keeps_voice(self) -> None:
        """**反向（丢内容）**：`chunk4` 预拆过的群草稿（首段 + `later_segments`）必须整条发出。

        `chunk4` 的即时动作**一律**过 `prepare_outgoing_delivery`（上游 `:5201` 明确把
        群目标排除在外），所以跨群草稿到这里时可能已经是"首段 + `later_segments` + 每段
        语音意图"。只发 `content` 会让后面的气泡**静默消失**、语音意图也会丢——本用例
        把"一段不少、语音照旧"钉住。
        """
        service = self._harness()
        await self._ready(service)
        service.note_endpoint_connection('onebot:100', True)
        group_endpoint = self._group_endpoint(service)
        voice_transport = _VoiceRecordingTransport()
        service.transport = voice_transport
        message = {
            'participant_id': self.GROUP_CHANNEL, 'content': '第一句',
            'later_segments': ['第二句', '第三句'],
            'later_segments_voice': [False, True],
            'endpoint_id': group_endpoint,
        }

        outcome = await service.send_cross_group_message(self._story(), message)

        self.assertTrue(outcome['complete'])
        self.assertEqual(
            [(item['content'], item.get('voice')) for item in voice_transport.sent
             if item['kind'] == 'group'],
            [('第一句', None), ('第二句', None), ('第三句', True)],
            '三段都要发出去，且第三段的语音意图不许丢',
        )

    # ---- ⑤ 等价（不涉及跨群动作时，私聊那条路一个字不变） ----
    async def test_a_private_message_never_enters_the_cross_group_path(self) -> None:
        """**反向（等价）**：私聊出站不碰跨群那一跳，出站序列与投递结果键集全等。"""
        service = await self._ready()

        async def _explode(*_args, **_kwargs):
            raise AssertionError('私聊消息不许进跨群出站')

        service.send_cross_group_message = _explode  # type: ignore[assignment]
        message = {'participant_id': PARTICIPANT_ID, 'content': '在呢'}
        delivered = await service.send_outgoing_messages(
            self._story(), [message], self._participant(), self._session('onebot', '100'),
        )
        self.assertEqual(
            [(item['kind'], item['content']) for item in self.transport.sent],
            [('session', '在呢')],
        )
        self.assertEqual(len(delivered), 1)
        self.assertEqual(
            set(delivered[0]), {'participant_id', 'content', 'endpoint_id'},
            '私聊路径的交付键集不许因为跨群补丁多出任何键',
        )

    # ---- ⑥ 反向（变异）：去掉"把 endpointId 传给出站"那一跳 → ① 必红 ----
    async def test_the_endpoint_plumbing_is_what_puts_the_message_on_the_explicit_endpoint(self) -> None:
        """走**生产**管道：`decision_to_script_commit` → `attach_message_event` → 跨群出站。

        删掉 `commit_builder.py` 里给跨会话事件写 `endpoint_id` 那一跳，或删掉
        `delivery.attach_message_event` 把它提到草稿顶层那一跳，`message` 就没有
        `endpoint_id` → 这一条会落到默认（角色）端点，下面的断言立刻红。
        """
        service = self._harness()
        await self._ready(service)
        service.note_endpoint_connection('onebot:100', True)
        group_endpoint = self._group_endpoint(service)
        content = '我去那个群说句话'
        commit = decision_to_script_commit({
            'story_id': STORY_ID, 'participant_id': PARTICIPANT_ID, 'phase': 'user-message',
            'from': NOW, 'now': NOW,
            'decision': {
                'script': '她把话头转向了另一个群。',
                'interaction': {'seen': True, 'reply': {'mode': 'none'}},
                'cross_conversation_actions': [{
                    'participant_id': self.GROUP_CHANNEL, 'mode': 'immediate',
                    'content': content, 'endpoint_id': group_endpoint,
                }],
            },
            'message_separator': '<sep/>', 'split_reply_messages': True,
        })
        event = find_outgoing_script_event(commit, self.GROUP_CHANNEL, 'immediate', content)
        self.assertIsNotNone(event, '跨会话动作必须真的落成出站事件')
        message = attach_message_event(
            {'participant_id': self.GROUP_CHANNEL, 'content': content}, event,
        )
        self.assertEqual(message.get('endpoint_id'), group_endpoint)

        await service.send_cross_group_message(self._story(), message)

        self.assertEqual([item['channel'] for item in self._group_sends(service)], ['123'])
        self.assertIs(
            service.endpoint_states[group_endpoint]['deliverable']['allowed'], True,
            '显式端点必须是被实际走的那条（端点管道断掉就会落到默认端点）',
        )

    def test_the_cross_action_loop_attaches_the_script_event_to_the_draft(self) -> None:
        """结构性兜底：`chunk4` 的跨会话动作那一跳必须把剧本事件挂到出站草稿上。

        端点就是这样一路到出站层的：模型 → `commit_builder`（事件带 `endpoint_id`）→
        `chunk4` 的 `attach_message_event`（提到草稿顶层）→ `send_outgoing_messages` →
        `send_cross_group_message`。少了中间这跳，补丁一的端点传递就是空转。
        """
        source = inspect.getsource(ServiceChunk4.persist_decision)
        self.assertIn('messages.append(attach_message_event({', source)
        self.assertIn("action.get('participantId')", source)
        self.assertLess(
            source.index("action.get('participantId')"),
            source.index('messages.append(attach_message_event({'),
            '跨会话动作那一支才是挂事件的地方',
        )


@unittest.skipUnless(
    _GROUP_DISPATCH_WIRED,
    'chunk6 的群目标分发分支待应用 U17 逐字补丁（chunk6 属 M3/M4 冻结文件）',
)
class CrossGroupDispatchTests(_GateTestCase):
    """上游 `:6223`：`sendOutgoingMessages` 的群目标分支（生产调用方）。"""

    GROUP_CHANNEL = 'group:123'

    def _config_with_groups(self) -> dict:
        config = _config()
        config['onebot'] = {'groupChats': [{'groupId': '123', 'enabled': True}]}
        return config

    def _harness(self) -> _CrossGroupHarness:  # type: ignore[override]
        ctx = InterludeContext(logger=None, database=self.db, clock=lambda: NOW)
        return _CrossGroupHarness(ctx, self._config_with_groups(), self.db, self.transport)

    async def test_send_outgoing_messages_routes_group_targets_to_the_cross_group_path(self) -> None:
        service = self._harness()
        await self._ready(service)
        service.note_endpoint_connection('onebot:100', True)
        group_endpoint = next(
            row['id'] for row in service.endpoint_rows
            if row['ownerKind'] == 'group' and str(row.get('groupId')) == '123'
        )
        calls: list = []
        original = service.send_cross_group_message

        async def spy(story, message, session=None):
            calls.append((story.get('id'), message.get('participant_id'),
                          message.get('endpoint_id'), session))
            return await original(story, message, session)

        service.send_cross_group_message = spy  # type: ignore[assignment]
        delivered = await service.send_outgoing_messages(
            self._story(),
            [{'participant_id': 'group:123', 'content': '我在另一个群说一句',
              'endpoint_id': group_endpoint}],
            None, None,
        )

        self.assertEqual(len(calls), 1, '群目标必须走 sendCrossGroupMessage，而不是私聊查表')
        self.assertEqual(calls[0][1], 'group:123')
        self.assertEqual(calls[0][2], group_endpoint)
        self.assertEqual([item['channel'] for item in self.transport.sent
                          if item['kind'] == 'group'], ['123'])
        self.assertEqual(delivered, [], '群目标不进私聊 delivered（上游同样 continue）')
        self.assertFalse(
            any('无法投递消息：参与者不存在' in item for item in service.reports),
            '群目标不许被当成"参与者不存在"丢掉：%s' % service.reports,
        )


# =========================================================================== #
# 4. Agency 侧接线（上游 `:5013-5030`）
# =========================================================================== #
class AgencyInitiateGateTests(_GateTestCase):
    """主动联系在**决策边界**就要过端点门控，理由必须显式分类且可见。"""

    async def test_an_expired_initiate_stops_agency_proactive_contact(self) -> None:
        service = await self._ready()
        endpoint_id = self._endpoint_for(service, 'participant-user', PARTICIPANT_ID)
        service.note_endpoint_connection('onebot:100', True)
        service.endpoint_states[endpoint_id]['initiate'] = {
            'allowed': True, 'observed_at': NOW_MS, 'expires_at': NOW_MS - 1,
        }
        reason = await service.agency_endpoint_gate(
            {'crossConversationActions': []}, {'participant_id': PARTICIPANT_ID},
            self._story(), 'advance',
        )
        self.assertEqual(reason, 'endpoint-state-expired')
        self.assertTrue(
            any('Agency 端点门控阻止主动联系' in item and 'endpoint-state-expired' in item
                for item in service.reports),
            '理由必须写进可见 warn（不能被当成意愿/容量拒绝）：%s' % service.reports,
        )

    async def test_without_an_initiate_record_agency_keeps_the_legacy_path(self) -> None:
        """**反向的一半**：QQ/OneBot 没有 initiate 记录 → 历史主动联系路径不受影响。"""
        service = await self._ready()
        service.note_endpoint_connection('onebot:100', True)
        self.assertIsNone(await service.agency_endpoint_gate(
            {'crossConversationActions': []}, {'participant_id': PARTICIPANT_ID},
            self._story(), 'advance',
        ))

    async def test_an_offline_endpoint_stops_agency_proactive_contact(self) -> None:
        """普通投递门控同样属于 Agency 决策边界（不只是 initiate）。"""
        service = await self._ready()
        reason = await service.agency_endpoint_gate(
            {'crossConversationActions': []}, {'participant_id': PARTICIPANT_ID},
            self._story(), 'advance',
        )
        self.assertEqual(reason, 'endpoint-offline')

    async def test_an_unavailable_registry_blocks_proactive_contact(self) -> None:
        service = await self._ready()

        async def _broken() -> None:
            raise RuntimeError('端点表读不了')

        service.ensure_endpoint_registry = _broken  # type: ignore[assignment]
        service.endpoint_registry_ready = False
        reason = await service.agency_endpoint_gate(
            {'crossConversationActions': []}, {'participant_id': PARTICIPANT_ID},
            self._story(), 'advance',
        )
        self.assertEqual(reason, 'endpoint-registry-unavailable')
        self.assertTrue(any('Agency 端点注册表不可用' in item for item in service.reports))

    def test_the_decision_boundary_actually_consumes_the_gate(self) -> None:
        """接线断言（上游 `test/rc10-delivery.test.ts:288` 同风格的结构化断言）。

        行为部分由上面四条钉住；这里再钉住 `advance_unlocked` **真的**把它折进了
        `agency_allows_send`——否则闸门会变成又一处"有实现、没消费者"的死代码。
        """
        source = inspect.getsource(ServiceChunk4.persist_decision)
        self.assertIn('agency_endpoint_gate', source)
        self.assertIn('agency_policy_allows', source)
        self.assertIn('and agency_policy_allows', source)
        self.assertLess(
            source.index('agency_endpoint_gate'), source.index('and agency_policy_allows'),
            '先过闸门，再决定是否发送',
        )


# =========================================================================== #
# 5. endpoint-health 投影
# =========================================================================== #

class EndpointHealthProjectionTests(_GateTestCase):
    """上游 `service.ts:1001`：桌面端看到的可用性必须与投递用同一套判据。"""

    async def test_the_projection_reports_the_full_shape(self) -> None:
        service = await self._ready()
        user = self._endpoint_for(service, 'participant-user', PARTICIPANT_ID)
        role = self._endpoint_for(service, 'story-role', STORY_ID)
        # 只给用户端点写状态：角色端点保持 fresh-start（离线），于是两种"不可投递"
        # 的理由（离线 / 冷却）在同一次投影里各出现一次。
        service.endpoint_states[user] = {
            'endpoint_id': user,
            'connection': {'online': True, 'observed_at': NOW_MS},
            'deliverable': {'allowed': False, 'checked_at': NOW_MS,
                            'cooldown_until': NOW_MS + 300_000, 'note': 'transport-error: 风控'},
        }
        snapshot = await service.desktop_endpoint_health_snapshot()
        self.assertEqual(snapshot['protocol'], 1)
        self.assertEqual(snapshot['generatedAt'], '2026-09-30T12:00:00.000Z')
        entries = {item['endpointId']: item for item in snapshot['endpoints']}
        self.assertEqual(set(entries), {user, role})
        self.assertEqual(entries[user], {
            'endpointId': user, 'ownerKind': 'participant-user', 'ownerId': PARTICIPANT_ID,
            'platform': 'onebot', 'channelKind': 'qq', 'enabled': True,
            'online': True, 'observedAt': '2026-09-30T12:00:00.000Z',
            'deliverable': False, 'initiateAllowed': False,
            'cooldownUntil': '2026-09-30T12:05:00.000Z',
            'lastError': 'transport-error: 风控', 'note': 'transport-error: 风控',
        })
        self.assertEqual(entries[role], {
            'endpointId': role, 'ownerKind': 'story-role', 'ownerId': STORY_ID,
            'platform': 'onebot', 'channelKind': 'qq', 'enabled': True,
            'online': False, 'observedAt': '2026-09-30T12:00:00.000Z',
            'deliverable': False, 'initiateAllowed': False,
            'lastError': 'fresh-start', 'note': 'fresh-start',
        })

    async def test_a_deliverable_endpoint_reports_no_last_error(self) -> None:
        service = await self._ready()
        user = self._endpoint_for(service, 'participant-user', PARTICIPANT_ID)
        service.note_endpoint_connection('onebot:100', True)
        snapshot = await service.desktop_endpoint_health_snapshot()
        entry = next(item for item in snapshot['endpoints'] if item['endpointId'] == user)
        self.assertIs(entry['online'], True)
        self.assertIs(entry['deliverable'], True, 'fresh-start 例外在投影里同样生效')
        self.assertIs(entry['initiateAllowed'], True, 'initiate 缺失 = 不额外加闸')
        self.assertNotIn('lastError', entry)
        self.assertNotIn('cooldownUntil', entry)
        self.assertEqual(entry['note'], 'fresh-start')

    async def test_a_disabled_endpoint_is_never_reported_as_deliverable(self) -> None:
        service = await self._ready()
        user = self._endpoint_for(service, 'participant-user', PARTICIPANT_ID)
        service.note_endpoint_connection('onebot:100', True)
        for row in service.endpoint_rows:
            if row['id'] == user:
                row['enabled'] = False
        snapshot = await service.desktop_endpoint_health_snapshot()
        entry = next(item for item in snapshot['endpoints'] if item['endpointId'] == user)
        self.assertIs(entry['enabled'], False)
        self.assertIs(entry['deliverable'], False)
        self.assertIs(entry['initiateAllowed'], False)


if __name__ == '__main__':
    unittest.main()
