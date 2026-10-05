"""端点注册表 / 剧本别名 / 通道标注（上游 `test/endpoints.test.ts` 的纯函数用例，rc28）。

上游那 18 个用例里有一半带 IO（service 侧迁移、投递、健康面板）——那部分由
`test_service_endpoints.py` 覆盖；这里逐条对应**纯函数**那 7 条：

| 上游用例 | 本文件 |
| --- | --- |
| derive functions project legacy single-endpoint shapes | `DeriveTests` |
| endpointUniqueKey separates the three owner kinds | `UniqueKeyTests` |
| normalizeEndpointRow drops malformed rows | `NormalizeTests` |
| resolveInboundEndpoint follows the v3 boundary rules | `InboundTests` |
| channelContextMetadata carries the full structure | `ChannelContextTests` |
| EndpointState is conservative | `EndpointStateTests` |
| normalizeEndpointState / restoreEndpointState（rc29 M3） | `NormalizeEndpointStateTests` / `RestoreEndpointStateTests` / `EndpointInitiateGateTests` |
| M2-1.4 channelKindForAccount | `ChannelKindTests` |
| M1b 别名解析（含链式） | `StoryAliasTests` |
| M4 projectChannelContext 五规则 | `ChannelAnnotationTests` |
"""

from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from plugin.core.endpoints import (  # noqa: E402
    ENDPOINT_DELIVERABLE_TTL_MS,
    channel_context_metadata,
    channel_context_payload,
    channel_kind_for_account,
    derive_group_endpoint,
    derive_participant_user_endpoint,
    derive_story_role_endpoint,
    endpoint_account_key,
    endpoint_unique_key,
    fresh_endpoint_state,
    is_endpoint_deliverable,
    is_endpoint_initiate_allowed,
    normalize_endpoint_row,
    normalize_endpoint_state,
    normalize_story_alias_row,
    resolve_inbound_endpoint,
    resolve_story_alias,
    restore_endpoint_state,
    session_matches_endpoint,
    state_after_connection,
    state_after_inbound,
    state_after_outbound,
)
from plugin.core.script.context_compiler import project_channel_context  # noqa: E402

STORY = {'id': 'character:onebot:1', 'platform': 'onebot', 'selfId': '1'}


class DeriveTests(unittest.TestCase):
    def test_derive_functions_project_legacy_shapes_without_inventing_ids(self):
        role = derive_story_role_endpoint(STORY)
        self.assertEqual(role['id'], '', '派生函数不造主键')
        self.assertEqual(role['ownerKind'], 'story-role')
        self.assertEqual(role['ownerId'], 'character:onebot:1')
        self.assertEqual(role['accountKey'], 'onebot:1')
        self.assertEqual(role['channelKind'], 'qq')
        self.assertTrue(role['enabled'])
        self.assertNotIn('conversationKind', role, '角色端点没有会话类型')

        user = derive_participant_user_endpoint(
            {'id': 'p1', 'platform': 'onebot', 'selfId': '1', 'userId': '9'},
        )
        self.assertEqual(user['ownerKind'], 'participant-user')
        self.assertEqual(user['conversationKind'], 'private')
        self.assertEqual(user['userId'], '9')

        group = derive_group_endpoint(STORY, {'groupId': 'group:123'})
        self.assertEqual(group['ownerKind'], 'group')
        self.assertEqual(group['groupId'], '123', 'group:/guild: 前缀归一化掉')
        self.assertEqual(group['ownerId'], '123')
        self.assertEqual(group['conversationKind'], 'group')


class UniqueKeyTests(unittest.TestCase):
    def test_unique_key_separates_the_three_owner_kinds(self):
        self.assertEqual(endpoint_unique_key({'ownerKind': 'story-role', 'accountKey': 'onebot:1'}), 'role:onebot:1')
        self.assertEqual(
            endpoint_unique_key({
                'ownerKind': 'participant-user', 'ownerId': 'p1', 'accountKey': 'onebot:1', 'userId': '9',
            }),
            'user:p1:onebot:1:9',
        )
        self.assertEqual(
            endpoint_unique_key({
                'ownerKind': 'group', 'accountKey': 'onebot:1', 'channelId': '', 'groupId': '123',
            }),
            'group:onebot:1::123',
        )
        # 不同平台同 selfId 不碰撞（平台隔离键）。
        self.assertNotEqual(endpoint_account_key('onebot', '1'), endpoint_account_key('wechat', '1'))


class NormalizeTests(unittest.TestCase):
    def test_normalize_drops_malformed_rows_and_coerces_optional_fields(self):
        self.assertIsNone(normalize_endpoint_row(None))
        self.assertIsNone(normalize_endpoint_row('junk'))
        self.assertIsNone(normalize_endpoint_row({'ownerKind': 'bogus', 'id': 'x', 'accountKey': 'a', 'ownerId': 'o'}))
        self.assertIsNone(normalize_endpoint_row({'ownerKind': 'group', 'id': '', 'accountKey': 'a', 'ownerId': 'o'}))
        row = normalize_endpoint_row({
            'id': 'ep1', 'ownerKind': 'participant-user', 'ownerId': 'p1', 'accountKey': 'onebot:1',
            'userId': '9', 'channelId': '', 'enable': False,
        })
        self.assertEqual(row['channelKind'], 'qq', '未知通道类型回落 qq')
        self.assertEqual(row['conversationKind'], 'private')
        self.assertNotIn('channelId', row, '空的可选字段不落键')
        self.assertTrue(row['enabled'], '`enable` 不是有效键 → enabled 保持默认真')
        disabled = normalize_endpoint_row({'id': 'e', 'ownerKind': 'group', 'ownerId': '1', 'accountKey': 'a', 'enabled': False})
        self.assertFalse(disabled['enabled'])


class InboundTests(unittest.TestCase):
    def _rows(self):
        role = {**derive_story_role_endpoint(STORY), 'id': 'ep-role'}
        user = {**derive_participant_user_endpoint({'id': 'p1', 'platform': 'onebot', 'selfId': '1', 'userId': '9'}), 'id': 'ep-user'}
        group = {**derive_group_endpoint(STORY, {'groupId': '123'}), 'id': 'ep-group'}
        return [role, user, group]

    def test_inbound_follows_the_v3_boundary_rules(self):
        rows = self._rows()
        # 未注册账号 → undefined（调用方回落旧路径，绝不自动挂载）。
        self.assertIsNone(resolve_inbound_endpoint(rows, {'platform': 'onebot', 'selfId': '404'}))
        # 陌生 userId：只给角色端点，用户端点缺省。
        stranger = resolve_inbound_endpoint(rows, {'platform': 'onebot', 'selfId': '1', 'userId': '404'})
        self.assertEqual(stranger['role_endpoint']['id'], 'ep-role')
        self.assertNotIn('user_endpoint', stranger)
        # 已注册用户端点。
        known = resolve_inbound_endpoint(rows, {'platform': 'onebot', 'selfId': '1', 'userId': '9'})
        self.assertEqual(known['user_endpoint']['id'], 'ep-user')
        # 群消息走群端点（群号形态差异也能命中）。
        group = resolve_inbound_endpoint(rows, {'platform': 'napcat', 'selfId': '1', 'groupId': 'group:123'})
        self.assertEqual(group['group_endpoint']['id'], 'ep-group')
        # 未注册的群 → 仍给角色端点（调用方走既有群规则路径）。
        other = resolve_inbound_endpoint(rows, {'platform': 'onebot', 'selfId': '1', 'groupId': '999'})
        self.assertNotIn('group_endpoint', other)
        # 关闭的端点不参与解析。
        rows[0]['enabled'] = False
        self.assertIsNone(resolve_inbound_endpoint(rows, {'platform': 'onebot', 'selfId': '1'}))

    def test_duplicate_role_endpoints_are_reported_not_silently_picked(self):
        rows = self._rows()
        duplicate = {**rows[0], 'id': 'ep-role-2'}
        resolution = resolve_inbound_endpoint(rows + [duplicate], {'platform': 'onebot', 'selfId': '1'})
        self.assertEqual(resolution['role_endpoint']['id'], 'ep-role', '首行生效')
        self.assertEqual(resolution['duplicate_role_account_keys'], ['onebot:1'], '重复行必须暴露给调用方告警')


class ChannelContextTests(unittest.TestCase):
    def test_channel_context_carries_the_full_disambiguated_structure(self):
        endpoint = {**derive_participant_user_endpoint(
            {'id': 'p1', 'platform': 'onebot', 'selfId': '1', 'userId': '9'},
        ), 'id': 'ep1'}
        metadata = channel_context_metadata(endpoint, {'channelId': 'c1'})
        self.assertEqual(metadata, {
            'endpoint_id': 'ep1', 'channel_kind': 'qq', 'platform': 'onebot',
            'account_key': 'onebot:1', 'self_id': '1', 'conversation_kind': 'private',
            'channel_id': 'c1',
        })
        # wire 形态转 camelCase（发给模型）。
        self.assertEqual(channel_context_payload(metadata), {
            'endpointId': 'ep1', 'channelKind': 'qq', 'platform': 'onebot',
            'accountKey': 'onebot:1', 'selfId': '1', 'conversationKind': 'private',
            'channelId': 'c1',
        })


class EndpointStateTests(unittest.TestCase):
    """`EndpointState` 三维状态机（v3 §四）：过期即保守、重启归零。

    v1.9.10（rc29 第 28–29 条）起这两个判据**不再是死代码**：`is_endpoint_deliverable`
    由出站路径消费（`chunk6.send_outgoing_messages`）+ Agency 决策边界
    （`chunk4.agency_endpoint_gate`）+ 桌面健康投影，`is_endpoint_initiate_allowed`
    由可选的主动联系闸门消费（`chunk11.endpoint_initiate_gate_reason`）。
    端到端接线见 `test_endpoint_gate.py`；这里逐条对应上游
    `test/endpoints.test.ts:93`（restore）与 `:112`（保守性）。
    """

    def test_fresh_state_is_conservative(self):
        fresh = fresh_endpoint_state('ep1', 1_000)
        self.assertEqual(fresh['endpoint_id'], 'ep1')
        self.assertEqual(fresh['connection'], {'online': False, 'observed_at': 1_000})
        self.assertEqual(fresh['deliverable'], {
            'allowed': False, 'checked_at': 1_000, 'note': 'fresh-start',
        })
        # 离线一票否决：fresh-start 例外也不越过"还没观测到连接"。
        self.assertFalse(is_endpoint_deliverable(fresh, 1_000))
        self.assertFalse(is_endpoint_initiate_allowed(fresh, 1_000))

    def test_state_is_refreshed_by_evidence(self):
        fresh = fresh_endpoint_state('ep1', 1_000)
        online = state_after_connection(fresh, True, 2_000)
        self.assertEqual(online['connection'], {'online': True, 'observed_at': 2_000})
        self.assertIs(online['deliverable']['allowed'], False, '只上线还不算可投递')
        self.assertTrue(
            is_endpoint_deliverable(online, 2_000),
            'fresh-start 例外：连接器已报到 → 第一次投递必须放行（否则重启即全拦）',
        )

        inbound = state_after_inbound(online, 3_000)
        self.assertIs(inbound['connection']['online'], True)
        self.assertEqual(inbound['deliverable'], {'allowed': True, 'checked_at': 3_000},
                         '入站即证明可投递')
        self.assertTrue(is_endpoint_deliverable(inbound, 4_000))

        failed = state_after_outbound(inbound, False, 'send-failed', 60_000, 4_000)
        self.assertIs(failed['deliverable']['allowed'], False)
        self.assertEqual(failed['deliverable']['cooldown_until'], 64_000, '失败落冷却')
        self.assertEqual(failed['deliverable']['note'], 'send-failed')
        self.assertFalse(is_endpoint_deliverable(failed, 4_000 + 26_000), '冷却期内保守')
        self.assertTrue(is_endpoint_deliverable(failed, 64_001), '冷却结束允许重试探测')

        recovered = state_after_outbound(failed, True, 'delivered', 60_000, 5_000)
        self.assertEqual(recovered['deliverable'], {'allowed': True, 'checked_at': 5_000},
                         '成功即刷新（冷却不再留着）')

    def test_inbound_also_refreshes_initiate_when_a_token_exists(self):
        """**反向**：没有 `initiate` 记录时不许凭空造一个出来。"""
        state = {'initiate': {'allowed': False, 'observed_at': 1}}
        refreshed = state_after_inbound(state, 3_000)
        self.assertEqual(refreshed['initiate'], {'allowed': True, 'observed_at': 3_000})
        self.assertNotIn('initiate', state_after_inbound({'connection': {}}, 3_000))

    def test_deliverable_truth_expires_after_the_ttl(self):
        """陈旧的"可投递"不是事实（上游 P2-8：24 小时保质期）。`allowed` 过期按未知保守。"""
        state = {
            'connection': {'online': True, 'observed_at': 0},
            'deliverable': {'allowed': True, 'checked_at': 0},
        }
        self.assertTrue(is_endpoint_deliverable(state, ENDPOINT_DELIVERABLE_TTL_MS))
        self.assertFalse(is_endpoint_deliverable(state, ENDPOINT_DELIVERABLE_TTL_MS + 1))

    def test_the_fresh_start_exception_is_limited_to_its_two_notes(self):
        """**反向**：例外不是"note 在就放行"——别的显式失败照旧拦住。"""
        for note in ('fresh-start', 'restart-awaiting-connection'):
            with self.subTest(note=note):
                self.assertTrue(is_endpoint_deliverable({
                    'connection': {'online': True, 'observed_at': 0},
                    'deliverable': {'allowed': False, 'checked_at': 0, 'note': note},
                }, 1_000))
        for note in ('transport-failed', 'send-failed', 'endpoint-not-deliverable'):
            with self.subTest(note=note):
                self.assertFalse(is_endpoint_deliverable({
                    'connection': {'online': True, 'observed_at': 0},
                    'deliverable': {'allowed': False, 'checked_at': 0, 'note': note},
                }, 1_000))

    def test_unknown_or_malformed_state_is_never_deliverable(self):
        self.assertFalse(is_endpoint_deliverable(None))
        self.assertFalse(is_endpoint_deliverable({}))
        self.assertFalse(is_endpoint_deliverable({'connection': {'online': True}}))
        self.assertFalse(is_endpoint_initiate_allowed(None))
        self.assertFalse(is_endpoint_initiate_allowed({}))


class NormalizeEndpointStateTests(unittest.TestCase):
    """上游 `normalizeEndpointState`（`endpoints.ts:229`）：防御性读取持久快照。"""

    def test_malformed_rows_are_dropped_instead_of_raising(self):
        self.assertIsNone(normalize_endpoint_state(None))
        self.assertIsNone(normalize_endpoint_state('junk'))
        self.assertIsNone(normalize_endpoint_state([]))
        self.assertIsNone(normalize_endpoint_state({'state': {'connection': {}}}), '没有 id 就没有行')

    def test_a_full_row_is_normalized_to_the_internal_shape(self):
        state = normalize_endpoint_state({
            'endpointId': 'ep1',
            'state': {
                'connection': {'online': True, 'observedAt': 1_000},
                'deliverable': {'allowed': True, 'checkedAt': 2_000, 'cooldownUntil': 3_000,
                                'note': 'x' * 600},
                'initiate': {'allowed': True, 'observedAt': 4_000, 'expiresAt': 5_000,
                             'reason': 'token'},
            },
            'updatedAt': '2026-09-30T00:00:00.000Z',
        })
        self.assertEqual(state['endpoint_id'], 'ep1')
        self.assertEqual(state['connection'], {'online': True, 'observed_at': 1_000})
        self.assertEqual(state['deliverable']['allowed'], True)
        self.assertEqual(state['deliverable']['checked_at'], 2_000)
        self.assertEqual(state['deliverable']['cooldown_until'], 3_000)
        self.assertEqual(len(state['deliverable']['note']), 500, 'note 截到 500（上游 slice）')
        self.assertEqual(state['initiate'], {
            'allowed': True, 'observed_at': 4_000, 'expires_at': 5_000, 'reason': 'token',
        })

    def test_a_bare_state_object_is_accepted_and_bad_fields_fall_back(self):
        """裸状态对象（没有外层行）也要认；坏时间戳回落"现在"而不是抛错。"""
        state = normalize_endpoint_state({
            'endpointId': 'ep2',
            'connection': {'online': 'yes', 'observedAt': 'garbage'},
            'deliverable': 'not-a-record',
            'initiate': None,
        })
        self.assertEqual(state['endpoint_id'], 'ep2')
        self.assertIs(state['connection']['online'], False, '只认严格 True（上游 === true）')
        self.assertIsInstance(state['connection']['observed_at'], int)
        self.assertEqual(state['deliverable']['allowed'], False)
        self.assertNotIn('initiate', state, 'initiate 不是 record = 没有这条闸门')

    def test_the_internal_snake_case_spelling_round_trips(self):
        """我方自己落盘的快照（内部 snake_case）必须能原样读回来。"""
        original = {
            'endpoint_id': 'ep3',
            'connection': {'online': True, 'observed_at': 7},
            'deliverable': {'allowed': True, 'checked_at': 8, 'note': 'delivered'},
        }
        self.assertEqual(normalize_endpoint_state(original), original)


class RestoreEndpointStateTests(unittest.TestCase):
    """上游 `restoreEndpointState`（`endpoints.ts:264`）：重启恢复。"""

    def test_restore_never_restores_online_truth_across_a_restart(self):
        """上游 `test/endpoints.test.ts:93` 逐条移植。"""
        now = 2_000_000
        restored = restore_endpoint_state('ep-restart', {
            'endpointId': 'ep-restart',
            'connection': {'online': True, 'observedAt': now - 10_000},
            'deliverable': {'allowed': True, 'checkedAt': now - 10_000, 'note': 'last-ok'},
            'initiate': {'allowed': True, 'observedAt': now - 10_000, 'expiresAt': now + 10_000},
        }, now)
        self.assertIs(restored['connection']['online'], False)
        self.assertEqual(restored['connection']['observed_at'], now)
        self.assertIs(restored['deliverable']['allowed'], True, '诊断投递快照保留')
        self.assertEqual(restored['deliverable']['note'], 'last-ok', '原 note 不被覆盖')
        self.assertIs(restored['initiate']['allowed'], True)

    def test_restore_without_a_snapshot_is_a_fresh_start(self):
        restored = restore_endpoint_state('ep-new', None, 1_000)
        self.assertEqual(restored, fresh_endpoint_state('ep-new', 1_000))
        # 坏快照同样退化成 fresh-start（不抛、不半途而废）。
        self.assertEqual(restore_endpoint_state('ep-new', 'junk', 1_000),
                         fresh_endpoint_state('ep-new', 1_000))

    def test_a_missing_note_becomes_restart_awaiting_connection(self):
        """没 note 的快照补占位 note：这是连接器报到之后首次出站的凭据。"""
        restored = restore_endpoint_state('ep1', {
            'endpointId': 'ep1',
            'connection': {'online': True, 'observedAt': 5},
            'deliverable': {'allowed': True, 'checkedAt': 6},
        }, 1_000)
        self.assertEqual(restored['deliverable']['note'], 'restart-awaiting-connection')
        reconnected = state_after_connection(restored, True, 2_000)
        self.assertTrue(is_endpoint_deliverable(reconnected, 2_000))


class EndpointInitiateGateTests(unittest.TestCase):
    """上游 `isEndpointInitiateAllowed`（`endpoints.ts:372`）。"""

    def test_a_missing_initiate_record_means_no_extra_gate(self):
        self.assertFalse(is_endpoint_initiate_allowed({'connection': {}}, 1_000))

    def test_expiry_and_forbidden_are_hard_stops(self):
        base = {'initiate': {'allowed': True, 'observed_at': 0, 'expires_at': 500}}
        self.assertTrue(is_endpoint_initiate_allowed(base, 499))
        self.assertFalse(is_endpoint_initiate_allowed(base, 500), '到期即不允许')
        self.assertFalse(is_endpoint_initiate_allowed({'initiate': {'allowed': False}}, 1))
        self.assertTrue(is_endpoint_initiate_allowed({'initiate': {'allowed': True}}, 999_999_999),
                        '没有 expiresAt = 不过期')



class ChannelKindTests(unittest.TestCase):
    def test_channel_kind_lookup_defaults_to_qq_and_ignores_disabled_rows(self):
        rows = [{'accountKey': 'onebot:1', 'channelKind': 'wechat', 'enabled': True}]
        self.assertEqual(channel_kind_for_account(rows, 'onebot:1'), 'wechat')
        self.assertEqual(channel_kind_for_account(rows, 'onebot:404'), 'qq', '未注册默认 qq')
        self.assertEqual(channel_kind_for_account([{**rows[0], 'enabled': False}], 'onebot:1'), 'qq')


class StoryAliasTests(unittest.TestCase):
    def test_alias_resolution_never_follows_a_chain(self):
        self.assertEqual(resolve_story_alias([], 'a'), {})
        self.assertEqual(
            resolve_story_alias([{'aliasStoryId': 'a', 'canonicalStoryId': 'b'}], 'a'),
            {'canonical_story_id': 'b'},
        )
        chained = [
            {'aliasStoryId': 'a', 'canonicalStoryId': 'b'},
            {'aliasStoryId': 'b', 'canonicalStoryId': 'c'},
        ]
        self.assertEqual(resolve_story_alias(chained, 'a'), {'problem': 'chain'}, '多跳留给人工裁决')

    def test_alias_rows_drop_self_references(self):
        self.assertIsNone(normalize_story_alias_row(None))
        self.assertIsNone(normalize_story_alias_row({'aliasStoryId': 'a', 'canonicalStoryId': 'a'}))
        self.assertIsNone(normalize_story_alias_row({'aliasStoryId': '', 'canonicalStoryId': 'b'}))
        row = normalize_story_alias_row({'aliasStoryId': 'a', 'canonicalStoryId': 'b', 'reason': 'x'})
        self.assertEqual((row['aliasStoryId'], row['canonicalStoryId'], row['reason']), ('a', 'b', 'x'))


class SessionMatchesEndpointTests(unittest.TestCase):
    """上游出站路径内联的 `sessionMatchesEndpoint`（`service.ts:6261`）。

    这是"这条 live session 能不能当该端点的在线证据"的唯一判据；出站门控与群路门控
    都消费它（`chunk6` / `chunk11`）。**反向**：selfId 不同或平台不同就不算——
    否则一个账号的入站会给另一个账号背书。
    """

    def test_only_the_same_account_on_the_same_platform_counts(self):
        endpoint = {'selfId': '100', 'platform': 'onebot'}
        self.assertTrue(session_matches_endpoint({'selfId': '100', 'platform': 'onebot'}, endpoint))
        self.assertFalse(session_matches_endpoint({'selfId': '101', 'platform': 'onebot'}, endpoint))
        self.assertFalse(session_matches_endpoint({'selfId': '100', 'platform': 'wechat'}, endpoint))
        self.assertFalse(session_matches_endpoint(None, endpoint))
        self.assertFalse(session_matches_endpoint({'selfId': '100', 'platform': 'onebot'}, None))

    def test_one_bot_family_platforms_are_the_same_platform(self):
        self.assertTrue(session_matches_endpoint(
            {'selfId': '100', 'platform': 'napcat'}, {'selfId': '100', 'platform': 'onebot'},
        ))

    def test_a_read_only_session_view_is_accepted(self):
        """`SessionView` 是 dataclass（不是 dict）——出站观察必须能读它。"""
        from plugin.core.service.session import SessionView

        endpoint = {'selfId': '100', 'platform': 'onebot'}
        self.assertTrue(session_matches_endpoint(SessionView(platform='onebot', self_id='100'), endpoint))
        self.assertFalse(session_matches_endpoint(SessionView(platform='onebot', self_id='101'), endpoint))


class ChannelAnnotationTests(unittest.TestCase):
    """M4 §十：五规则命中即标，不命中不标。上游 `test/endpoints.test.ts` 逐条移植。"""

    def test_no_channel_data_means_no_annotation(self):
        self.assertIsNone(project_channel_context({}))

    def test_rule_1_multi_endpoint_turn(self):
        annotation = project_channel_context({
            '_channelTurnSources': [
                {'endpointId': 'ep-qq', 'channelKind': 'qq', 'receivedSeq': 1},
                {'endpointId': 'ep-wx', 'channelKind': 'wechat', 'receivedSeq': 2},
            ],
            '_channelCurrentChannel': {'endpointId': 'ep-wx', 'channelKind': 'wechat', 'conversationKind': 'private'},
        })
        self.assertIn('multi-endpoint-turn', annotation['rules'])
        self.assertEqual(len(annotation['sources']), 2)
        self.assertEqual(annotation['tag'], '[微信·私]')

    def test_rule_3_conversation_kind_switch_keeps_the_source_endpoints(self):
        annotation = project_channel_context({
            '_channelTurnSources': [{'endpointId': 'ep-qq', 'channelKind': 'qq', 'receivedSeq': 4}],
            '_channelLastEntryChannel': {'conversationKind': 'private'},
            '_channelCurrentChannel': {'conversationKind': 'group', 'channelKind': 'qq'},
        })
        self.assertIn('conversation-kind-switch', annotation['rules'])
        self.assertEqual(annotation['tag'], '[QQ·群]')
        self.assertEqual(annotation['sources'], [{'endpointId': 'ep-qq', 'channelKind': 'qq', 'receivedSeq': 4}])

    def test_rule_4_participant_endpoint_switch_and_rule_5_reply_target_differs(self):
        participated = project_channel_context({
            '_channelLastEntryChannel': {'endpointId': 'ep-a'},
            '_channelCurrentChannel': {'endpointId': 'ep-b', 'channelKind': 'qq', 'conversationKind': 'private'},
            'currentParticipant': {'id': 'p1'},
        })
        self.assertIn('participant-endpoint-switch', participated['rules'])
        replied = project_channel_context({
            '_channelCurrentChannel': {'endpointId': 'ep-a', 'channelKind': 'qq', 'conversationKind': 'private'},
            '_channelReplyEndpoint': {'endpointId': 'ep-b'},
        })
        self.assertIn('reply-target-differs', replied['rules'])
        # 没有参与者身份时规则 4 不成立（避免把陌生人的端点切换当成同一人）。
        # 没有参与者身份时规则 4 不成立（避免把陌生人的端点切换当成同一人）——
        # 此时一条规则都没命中，按"不命中不标"返回 None。
        stranger = project_channel_context({
            '_channelLastEntryChannel': {'endpointId': 'ep-a'},
            '_channelCurrentChannel': {'endpointId': 'ep-b', 'conversationKind': 'private'},
        })
        self.assertIsNone(stranger)

    def test_rule_2_is_independent_from_rule_1(self):
        turn_only = project_channel_context({
            '_channelTurnSources': [
                {'endpointId': 'ep-a', 'channelKind': 'qq', 'receivedSeq': 1},
                {'endpointId': 'ep-b', 'channelKind': 'wechat', 'receivedSeq': 2},
            ],
            '_channelCurrentChannel': {'endpointId': 'ep-b', 'channelKind': 'wechat', 'conversationKind': 'private'},
        })
        self.assertIn('multi-endpoint-turn', turn_only['rules'])
        self.assertNotIn('batch-multi-endpoint', turn_only['rules'])
        batch_only = project_channel_context({
            '_channelTurnSources': [{'endpointId': 'ep-a', 'channelKind': 'qq', 'receivedSeq': 1}],
            '_channelBatchMultiEndpoint': True,
            '_channelCurrentChannel': {'endpointId': 'ep-a', 'channelKind': 'qq', 'conversationKind': 'private'},
        })
        self.assertIn('batch-multi-endpoint', batch_only['rules'])

    def test_sources_are_deduplicated_and_stably_sorted(self):
        annotation = project_channel_context({
            '_channelTurnSources': [
                {'endpointId': 'ep-b', 'channelKind': 'qq', 'receivedSeq': 2},
                {'endpointId': 'ep-a', 'channelKind': 'qq', 'receivedSeq': 1},
                {'endpointId': 'ep-b', 'channelKind': 'qq', 'receivedSeq': 2},
                {'endpointId': '', 'channelKind': 'qq', 'receivedSeq': 3},
            ],
            '_channelCurrentChannel': {'endpointId': 'ep-b', 'conversationKind': 'private'},
        })
        self.assertEqual([item['endpointId'] for item in annotation['sources']], ['ep-a', 'ep-b'])


class ChannelDataPayloadTests(unittest.TestCase):
    """M4 §十的**接线**：`request.channelData` → payload `_channel*` → `incomingEvent.channelContext`。"""

    def test_request_channel_data_reaches_the_compiled_incoming_event(self):
        from plugin.core.narrator_prompts import to_prompt_payload
        from plugin.tests.test_narrator_prompts import request as build_request

        base = build_request([])
        base['channelData'] = {
            # 私聊 → 群聊切换 = 规则 3（不需要参与者身份，夹具最小）。
            'currentChannel': {'endpointId': 'ep-b', 'channelKind': 'wechat', 'conversationKind': 'group'},
            'lastEntryChannel': {'endpointId': 'ep-a', 'channelKind': 'qq', 'conversationKind': 'private'},
        }
        payload = to_prompt_payload(base)
        annotation = (payload.get('incomingEvent') or {}).get('channelContext')
        self.assertIsNotNone(annotation, 'channelData 必须编译进 incomingEvent.channelContext')
        self.assertIn('conversation-kind-switch', annotation['rules'])
        self.assertEqual(annotation['tag'], '[微信·群]')

    def test_without_channel_data_no_annotation_is_emitted(self):
        from plugin.core.narrator_prompts import to_prompt_payload
        from plugin.tests.test_narrator_prompts import request as build_request

        payload = to_prompt_payload(build_request([]))
        self.assertNotIn('channelContext', payload.get('incomingEvent') or {})


class TurnSourceAnnotationTests(unittest.TestCase):
    """规则 1/2 的输入侧：`buffer_*` 累积的回合端点来源 → `channelData.turnSources`。"""

    def test_turn_sources_are_sorted_and_carry_channel_kind(self):
        from plugin.core.service.chunk11 import ServiceChunk11, _turn_sources

        rows = [
            {'id': 'ep-qq', 'channelKind': 'qq'},
            {'id': 'ep-wx', 'channelKind': 'wechat'},
        ]
        turn = {'sources': [
            {'endpointId': 'ep-wx', 'receivedSeq': 2},
            {'endpointId': 'ep-qq', 'receivedSeq': 1},
        ]}
        sources = _turn_sources(turn, rows)
        self.assertEqual([item['endpointId'] for item in sources], ['ep-qq', 'ep-wx'])
        self.assertEqual(sources[1]['channelKind'], 'wechat')
        self.assertTrue(hasattr(ServiceChunk11, 'channel_data_for'))

    def test_sources_annotation_triggers_rule_1_only_with_two_endpoints(self):
        annotation = project_channel_context({
            '_channelTurnSources': [{'endpointId': 'ep-a', 'channelKind': 'qq', 'receivedSeq': 1}],
            '_channelCurrentChannel': {'endpointId': 'ep-a', 'channelKind': 'qq', 'conversationKind': 'private'},
        })
        self.assertIsNone(annotation, '单端点回合不该标注')


if __name__ == '__main__':
    unittest.main()
