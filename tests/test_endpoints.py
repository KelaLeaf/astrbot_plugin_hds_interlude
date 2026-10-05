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
    channel_context_metadata,
    channel_context_payload,
    channel_kind_for_account,
    derive_group_endpoint,
    derive_participant_user_endpoint,
    derive_story_role_endpoint,
    endpoint_account_key,
    endpoint_unique_key,
    fresh_endpoint_state,
    normalize_endpoint_row,
    normalize_story_alias_row,
    resolve_inbound_endpoint,
    resolve_story_alias,
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

    v1.9.9 起这里**只**断言状态转移本身（`fresh` / `after_connection` /
    `after_inbound` / `after_outbound`）——原先那两个消费它们的门控函数
    （`is_endpoint_deliverable` / `is_endpoint_initiate_allowed`）全仓零调用方，
    属于"有定义、有测试、没行为"的死代码，已连测试一起删除。判据本身是**状态字段**：
    重启后 `online=False`、入站把 `deliverable.allowed` 立起来、失败落冷却。
    """

    def test_fresh_state_is_conservative(self):
        fresh = fresh_endpoint_state('ep1', 1_000)
        self.assertEqual(fresh['endpoint_id'], 'ep1')
        self.assertEqual(fresh['connection'], {'online': False, 'observed_at': 1_000})
        self.assertEqual(fresh['deliverable'], {
            'allowed': False, 'checked_at': 1_000, 'note': 'fresh-start',
        })

    def test_state_is_refreshed_by_evidence(self):
        fresh = fresh_endpoint_state('ep1', 1_000)
        online = state_after_connection(fresh, True, 2_000)
        self.assertEqual(online['connection'], {'online': True, 'observed_at': 2_000})
        self.assertIs(online['deliverable']['allowed'], False, '只上线还不算可投递')

        inbound = state_after_inbound(online, 3_000)
        self.assertIs(inbound['connection']['online'], True)
        self.assertEqual(inbound['deliverable'], {'allowed': True, 'checked_at': 3_000},
                         '入站即证明可投递')

        failed = state_after_outbound(inbound, False, 'send-failed', 60_000, 4_000)
        self.assertIs(failed['deliverable']['allowed'], False)
        self.assertEqual(failed['deliverable']['cooldown_until'], 64_000, '失败落冷却')
        self.assertEqual(failed['deliverable']['note'], 'send-failed')

        recovered = state_after_outbound(failed, True, 'delivered', 60_000, 5_000)
        self.assertEqual(recovered['deliverable'], {'allowed': True, 'checked_at': 5_000},
                         '成功即刷新（冷却不再留着）')

    def test_inbound_also_refreshes_initiate_when_a_token_exists(self):
        """**反向**：没有 `initiate` 记录时不许凭空造一个出来。"""
        state = {'initiate': {'allowed': False, 'observed_at': 1}}
        refreshed = state_after_inbound(state, 3_000)
        self.assertEqual(refreshed['initiate'], {'allowed': True, 'observed_at': 3_000})
        self.assertNotIn('initiate', state_after_inbound({'connection': {}}, 3_000))


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
