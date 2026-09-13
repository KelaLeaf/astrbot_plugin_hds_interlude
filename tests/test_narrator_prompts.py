"""上游提示词/payload 测试的 Python 移植（stdlib `unittest`）。

逐条移植自：
- `upstream/test/payload-order.test.ts`（190 行）
- `upstream/test/narrative-prompts.test.ts`（180 行）
- `upstream/test/positive-narrative-prompt.test.ts`（50 行）

约定（docs/PORT_PLAN.md §3）：
- 每条 `test(...)` → 一个 `test_*` 方法，断言逐条对照（含 `assert.doesNotMatch` → `assertNotRegex`）。
- 断言里的字符串**逐字照抄上游**（含 `’`、`“ ”` 等标点），不得改写。
- 上游测试用 camelCase 的对象字面量构造请求；本移植版内部数据是 snake_case
  （`types.py` 的 TypedDict），因此 fixture 用 snake_case 构造——`to_prompt_payload`
  两种拼写都认（`_pick`），而**模型可见的 payload 键名仍是上游 camelCase**，
  断言里的键名与上游逐字一致。
- 上游用 `JSON.stringify` 做前缀比较；这里用 `stringify()`（`separators=(',', ':')`、
  `ensure_ascii=False`）对齐 JS 的输出形状。

运行：
    cd /home/kela/文档/harness/hds-interlude && python3 -m unittest plugin.tests.test_narrator_prompts -v
"""

from __future__ import annotations

import json
import re
import unittest
from datetime import datetime, timedelta, timezone

from plugin.core.narrator_prompts import (
    prompt_visible_message_content,
    recent_script_ownership,
    story_state_for_prompt,
    system_prompt,
    to_prompt_payload,
)
from plugin.core.types import empty_story_setting, empty_story_state

#: 上游 `const now = new Date('2026-08-30T11:22:00.000Z')`。
NOW = datetime(2026, 8, 30, 11, 22, 0, tzinfo=timezone.utc)

#: 上游 `JSON.stringify` 的等价输出（无空格分隔符 + 不转义非 ASCII）。
def stringify(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def system_prompt_6(phase: str, *extra: object) -> str:
    """上游测试反复使用的 `systemPrompt(phase, '', '', '', '', '', ...)` 形式。"""
    return system_prompt(phase, '', '', '', '', '', *extra)  # type: ignore[arg-type]


def story() -> dict:
    """上游 `story()`。"""
    return {
        'id': 'story', 'platform': 'onebot', 'self_id': 'bot', 'user_id': '', 'channel_id': '', 'status': 'active',
        'setting': empty_story_setting(),
        'state': {**empty_story_state(), 'continuity_snapshot': {'current': '在房间', 'next': [], 'recent': [], 'salient': []}},
        'cursor_at': NOW, 'created_at': NOW, 'updated_at': NOW,
    }


def entry(entry_id: int, kind: str, content: str, offset_minutes: int) -> dict:
    """上游 `entry(id, kind, content, offsetMinutes)`。"""
    occurred_at = NOW - timedelta(minutes=offset_minutes)
    actor = 'narrator' if kind == 'script' else 'system' if kind == 'intent-cancelled' else 'character'
    return {
        'id': entry_id, 'story_id': 'story', 'participant_id': 'participant', 'kind': kind,
        'actor': actor, 'content': content, 'occurred_at': occurred_at, 'metadata': {}, 'created_at': occurred_at,
    }


def request(recent_entries: list[dict], user_message: str | None = None, overrides: dict | None = None) -> dict:
    """上游 `request(recentEntries, userMessage?, overrides?)`。"""
    base = {
        'phase': 'user-message', 'story': story(), 'from': NOW, 'now': NOW, 'user_message': user_message,
        'participant': None, 'participants': [], 'share_participant_details': False, 'due_intents': [],
        'active_consequences': [], 'superseded_intents': [], 'memories': [], 'recent_entries': recent_entries,
    }
    if overrides:
        base.update(overrides)
    return base


def common_prefix_length(left: str, right: str) -> int:
    """上游 `commonPrefixLength(left, right)`。"""
    index = 0
    limit = min(len(left), len(right))
    while index < limit and left[index] == right[index]:
        index += 1
    return index


class PayloadOrderTests(unittest.TestCase):
    """`upstream/test/payload-order.test.ts`。"""

    def test_both_payload_modes_use_the_seven_part_script_continuation_scaffold(self) -> None:
        req = request([entry(1, 'character-message', '拿到了。确实挺大杯。', 50)], '你健忘吗')
        legacy = to_prompt_payload(req)
        explicit_off = to_prompt_payload(req, {'cacheFirst': False})
        self.assertEqual(list(explicit_off.keys()), list(legacy.keys()))
        self.assertEqual(list(legacy.keys()), [
            'storyIdentity', 'relevantEstablishedEpisodes', 'currentSceneEvidence', 'ongoingThreads',
            'availableNearFuture', 'incomingEvent', 'authoringWindow',
        ])
        self.assertFalse('recentExchange' in legacy['relevantEstablishedEpisodes'])

    def test_cache_first_puts_stable_blocks_first_and_per_turn_fields_beside_the_decision_point(self) -> None:
        req = request([entry(1, 'character-message', '拿到了。确实挺大杯。', 50)], '你健忘吗')
        payload = to_prompt_payload(req, {'cacheFirst': True})
        self.assertEqual(list(payload.keys()), [
            'storyIdentity', 'relevantEstablishedEpisodes', 'currentSceneEvidence', 'ongoingThreads',
            'availableNearFuture', 'incomingEvent', 'authoringWindow',
        ])
        established = payload['relevantEstablishedEpisodes']
        self.assertEqual(list(established.keys())[0], 'recentScript')
        self.assertTrue('recentExchange' in established)
        self.assertTrue('interval' in payload['authoringWindow'])

    def test_recent_exchange_anchors_only_transport_exchanges(self) -> None:
        req = request([
            entry(1, 'user-message', '旧的一句', 240),
            entry(2, 'character-message', '拿到了。确实挺大杯。', 200),
            entry(3, 'script', '剧' * 900, 150),
            entry(4, 'character-message', '嗯。', 100),
            entry(5, 'user-message', '你健忘吗', 5),
        ], '你健忘吗')
        payload = to_prompt_payload(req, {'cacheFirst': True})
        items = payload['relevantEstablishedEpisodes']['recentExchange']
        self.assertEqual(len(items), 3)
        received = next(item for item in items if item['content'] == '拿到了。确实挺大杯。')
        self.assertEqual(received.get('tag'), 'protagonist')
        self.assertTrue('你健忘吗' not in stringify(items))
        self.assertTrue('剧' * 20 not in stringify(items), '剧本文字不应进入 transport 尾部锚点')
        total = sum(len(item['content']) for item in items)
        self.assertTrue(total <= 1600)
        self.assertTrue(any('旧的一句' in item['content'] for item in items), '跳过剧本文字后，最近三条真实收发消息应补足尾部块')

    def test_consecutive_user_turns_keep_append_only_history_at_the_cacheable_front(self) -> None:
        long_script = '剧' * 3000
        turn_one = request([
            entry(1, 'script', long_script, 60),
            entry(2, 'character-message', '拿到了。', 55),
            entry(3, 'user-message', '在吗', 50),
        ], '在吗')
        turn_two = request([
            entry(1, 'script', long_script, 60),
            entry(2, 'character-message', '拿到了。', 55),
            entry(3, 'user-message', '在吗', 50),
            entry(4, 'script', '后续剧情。', 10),
            entry(5, 'character-message', '嗯。', 8),
            entry(6, 'user-message', '吃饭没', 5),
        ], '吃饭没', {'from': NOW - timedelta(minutes=6)})

        cache_one = stringify(to_prompt_payload(turn_one, {'cacheFirst': True}))
        cache_two = stringify(to_prompt_payload(turn_two, {'cacheFirst': True}))
        cache_prefix = common_prefix_length(cache_one, cache_two)
        # 分叉点恰好是 recentScript 数组的收口：追加式历史让旧内容全部留在缓存前缀内。
        history_end = cache_one.index('],"sceneContext"')
        self.assertTrue(cache_prefix >= history_end - 1, '前缀分叉点不得早于对话史数组结束')
        self.assertTrue(cache_prefix >= len(cache_one) * 0.5, f'cache-first 前缀命中过短: {cache_prefix}/{len(cache_one)}')
        self.assertTrue(cache_prefix < cache_one.index('"currentSceneEvidence"'), '分叉点应位于当前场景证据视图之前')

        legacy_one = stringify(to_prompt_payload(turn_one))
        legacy_two = stringify(to_prompt_payload(turn_two))
        self.assertTrue(common_prefix_length(legacy_one, legacy_two) >= legacy_one.index('],"sceneContext"') - 1)
        self.assertTrue(len(cache_one) < len(legacy_one), 'compact tags should keep cache-first payload smaller')

    def test_group_turns_keep_recent_exchange_empty(self) -> None:
        req = request([entry(1, 'group-message', '群里说话', 5)], None, {
            'group_context': {'group_id': '111', 'channel_id': '111', 'label': '群', 'purpose': '闲聊', 'character_role': '群友', 'messages': []},
        })
        payload = to_prompt_payload(req, {'cacheFirst': True})
        self.assertEqual(payload['relevantEstablishedEpisodes']['recentExchange'], [])

    def test_compact_tags_collapse_kind_actor_triples(self) -> None:
        req = request([
            entry(1, 'character-message', 'a', 50),
            entry(2, 'character-group-message', 'b', 49),
            entry(3, 'character-platform-action', 'c', 48),
            entry(4, 'script', 'd', 47),
            entry(5, 'user-message', 'e', 46),
            entry(6, 'group-message', 'f', 45),
            entry(7, 'intent-cancelled', 'g', 44),
        ], '当前消息')
        payload = to_prompt_payload(req, {'cacheFirst': True})
        self.assertEqual([item['tag'] for item in payload['relevantEstablishedEpisodes']['recentScript']], [
            'protagonist', 'protagonist(group)', 'protagonist(action)', 'protagonist-narration', 'user', 'group-member', 'system',
        ])
        self.assertTrue(all('participantId' not in item for item in payload['relevantEstablishedEpisodes']['recentScript']))

    def test_participant_id_survives_only_when_the_history_spans_several_branches(self) -> None:
        same = [entry(1, 'user-message', 'a', 50), entry(2, 'character-message', 'b', 49)]
        payload_same = to_prompt_payload(request(same, 'x'), {'cacheFirst': True})
        self.assertTrue(all('participantId' not in item for item in payload_same['relevantEstablishedEpisodes']['recentScript']))
        mixed = list(same)
        mixed[1] = {**mixed[1], 'participant_id': 'onebot:bot:222'}
        payload_mixed = to_prompt_payload(request(mixed, 'x'), {'cacheFirst': True})
        self.assertTrue(all(
            item['participantId'] in ('participant', 'onebot:bot:222')
            for item in payload_mixed['relevantEstablishedEpisodes']['recentScript']
        ))

    def test_the_ownership_legend_matches_the_payload_mode(self) -> None:
        legacy = system_prompt_6('user-message')
        compact = system_prompt_6('user-message', False, False, False, False, False, None, False, None, False, False, True)
        self.assertRegex(legacy, r'ownership label is authoritative')
        self.assertNotRegex(legacy, r'compact tag')
        self.assertRegex(compact, r'compact tag that is authoritative')
        self.assertRegex(compact, r'protagonist\(group\)')
        self.assertRegex(compact, r'protagonist\(action\)')

    def test_working_details_recalled_script_and_previous_scenes_ride_the_payload_in_the_right_zones(self) -> None:
        req = request([entry(1, 'user-message', '在吗', 5)], '在吗', {
            'working_details': [{
                'label': '奶茶取餐码', 'value': '8914',
                'expires_at': (NOW + timedelta(hours=1)).isoformat().replace('+00:00', 'Z'),
                'created_at': NOW.isoformat().replace('+00:00', 'Z'),
            }],
            'recalled_history': [{'id': 99, 'occurred_at': '2026-08-30T10:00:00.000Z', 'content': '拿到了。确实挺大杯。'}],
            'scene_context': {
                'scene': None, 'arc': None,
                'previous_scenes': [{'started_at': '2026-08-30T09:00:00.000Z', 'ended_at': '2026-08-30T10:30:00.000Z', 'summary': '上一场景摘要'}],
            },
        })
        legacy = to_prompt_payload(req)
        self.assertEqual(legacy['ongoingThreads']['workingDetails'][0]['value'], '8914')
        self.assertEqual(legacy['relevantEstablishedEpisodes']['recalledScript'][0]['id'], 99)
        self.assertEqual(legacy['relevantEstablishedEpisodes']['sceneContext']['previousScenes'][0]['summary'], '上一场景摘要')
        cache = to_prompt_payload(req, {'cacheFirst': True})
        self.assertEqual(cache['ongoingThreads']['workingDetails'][0]['value'], '8914')
        self.assertEqual(cache['relevantEstablishedEpisodes']['recalledScript'][0]['id'], 99)
        self.assertEqual(cache['incomingEvent']['event']['content'], '在吗')

    def test_the_fixed_contract_documents_the_three_memory_blocks(self) -> None:
        prompt = system_prompt_6('user-message')
        self.assertRegex(prompt, r'previousScenes, when supplied')
        self.assertRegex(prompt, r'workingDetails, when supplied')
        self.assertRegex(prompt, r'recalledScript, when supplied')

    def test_the_fixed_contract_explains_recent_exchange_only_for_cache_first_payloads(self) -> None:
        plain = system_prompt_6('user-message')
        cache_aware = system_prompt_6('user-message', False, False, False, False, False, None, False, None, False, False, True)
        self.assertNotRegex(plain, r'recentExchange')
        self.assertRegex(cache_aware, r'recentExchange at the end duplicates the tail of recentScript')


class NarrativePromptTests(unittest.TestCase):
    """`upstream/test/narrative-prompts.test.ts`。"""

    def test_alter_scoring_is_requested_only_while_the_system_is_enabled(self) -> None:
        enabled = system_prompt_6('user-message', False, True)
        self.assertRegex(enabled, r'integer field named alter from -5 to \+5')
        self.assertRegex(enabled, r'not the existing atmosphere')
        self.assertRegex(enabled, r'bounded internal weather')
        self.assertRegex(enabled, r'may color the rhythm and form of her messages')
        self.assertRegex(enabled, r'still choose their content and direction')

        disabled = system_prompt_6('user-message', False, False)
        self.assertNotRegex(disabled, r'field named alter')

    def test_current_events_lead_reappraisal_before_relationship_tendencies(self) -> None:
        prompt = system_prompt_6('user-message', False, False)
        self.assertRegex(prompt, r'immediate relational thread')
        self.assertRegex(prompt, r'established tendencies supply nuance')
        self.assertRegex(prompt, r'New events can sustain or revise that reading')
        self.assertRegex(prompt, r'a tendency is context, never a verdict')

    def test_internal_alter_accumulator_and_history_never_leak_into_the_main_prompt_state(self) -> None:
        state = {
            **empty_story_state(),
            'alter_system': {
                'alter_value': 8, 'alter_weight': 0.6, 'last_trigger_direction': 1,
                'emotional_offset': None, 'history': [], 'last_updated_at': '2026-08-22T00:00:00.000Z',
            },
            'agency_window': {
                'activity_load': 'free', 'privacy': 'private', 'device_access': 'available',
                'valid_until': '2026-08-22T01:00:00.000Z', 'basis': '测试',
                'source_entry_ids': [1], 'updated_at': '2026-08-22T00:00:00.000Z',
            },
        }
        prompt_state = story_state_for_prompt(state)
        self.assertEqual('alterSystem' in prompt_state, False)
        self.assertEqual('agencyWindow' in prompt_state, False)
        # 上游断言只覆盖 camelCase 键；本移植版内部是 snake_case，额外确认它也不泄漏。
        self.assertEqual('alter_system' in prompt_state, False)
        self.assertEqual('agency_window' in prompt_state, False)
        self.assertEqual(prompt_state['automation'], {})

    def test_the_fixed_contract_makes_local_endpoint_time_authoritative_after_long_gaps(self) -> None:
        prompt = system_prompt_6('user-message', False, False)
        self.assertRegex(prompt, r'interval\.nowLocalContext')
        self.assertRegex(prompt, r'16:00/afternoon')
        self.assertRegex(prompt, re.compile(r'continuity snapshot can be stale after reload or a long gap', re.IGNORECASE))

    def test_interrupted_typing_is_context_but_never_delivered_speech(self) -> None:
        prompt = system_prompt_6('user-message', False, False)
        self.assertRegex(prompt, r'interruptedOutgoingDrafts')
        self.assertRegex(prompt, r'not as words the user received')
        self.assertRegex(prompt, r'never send it automatically')

    def test_each_request_includes_only_its_current_phase_strategy(self) -> None:
        user = system_prompt_6('user-message', False, False)
        advance = system_prompt_6('advance', False, False)
        follow_up = system_prompt_6('conversation-follow-up', False, False)
        due = system_prompt_6('intent-due', False, False)
        self.assertRegex(user, r'CURRENT PHASE: USER MESSAGE')
        self.assertRegex(user, r'SCRIPT-FIRST TRANSPORT MIRROR')
        self.assertRegex(user, r'For this private turn, return interaction')
        self.assertNotRegex(user, r'return groupReply as')
        self.assertNotRegex(user, r'INDEPENDENT LIFE ADVANCE')
        self.assertRegex(advance, r'CURRENT PHASE: INDEPENDENT LIFE ADVANCE')
        self.assertRegex(advance, r'This independent-life phase has no current reply channel')
        self.assertNotRegex(advance, r'For this private turn, return interaction')
        self.assertNotRegex(advance, r'interruptedOutgoingDrafts')
        self.assertRegex(follow_up, r'place its exact words at the sending action in script')
        self.assertRegex(due, r'CURRENT PHASE: DUE INTENT')
        self.assertRegex(due, r'For this private turn, return interaction')

    def test_private_interaction_protocol_is_neutral_about_reading_and_explicit_about_read_but_silent(self) -> None:
        user = system_prompt_6('user-message', False, False)
        # 协议示例不得用字面 false 充当默认值（弱指令模型会照抄示例值）。
        self.assertNotRegex(user, r'interaction as \{"seen":false')
        self.assertRegex(user, r'"seen":<true\|false>')
        self.assertRegex(user, r'seen and reply are independent fields')
        # 已读不回是明确合法的普通状态。
        self.assertRegex(user, r'seen=true with reply\.mode=none is the ordinary read-but-does-not-answer state')
        # 无 say 标记时允许 content 直传，堵住"只教 actionId"的静默丢弃悬崖。
        self.assertRegex(user, r'supply reply\.content directly instead of an id')
        # 未读计数是客观到达记录，不是注意力或义务。
        self.assertRegex(user, r'unreadMessageCount is the registered count of arrived messages not yet marked read')
        # 跟进/到期回合：seen=false 不得再暗示回复必须为 none。
        follow_up = system_prompt_6('conversation-follow-up', False, False)
        self.assertRegex(follow_up, r'reply may still be immediate or delayed when a message is genuinely sent now')

    def test_a_missing_visible_reply_structure_triggers_a_fresh_output_recovery_instruction(self) -> None:
        ordinary = system_prompt_6('user-message', False, False, False, False, False)
        recovery = system_prompt_6('user-message', False, False, False, False, True)
        self.assertNotRegex(ordinary, r'OUTPUT RECOVERY')
        self.assertRegex(recovery, r'OUTPUT RECOVERY')
        self.assertRegex(recovery, r'fresh unpublished decision')

    def test_recent_script_ownership_makes_narrative_thoughts_unambiguously_protagonist_owned(self) -> None:
        self.assertEqual(recent_script_ownership({'kind': 'script', 'actor': 'narrator'}), 'protagonist-narrative')
        self.assertEqual(recent_script_ownership({'kind': 'user-message', 'actor': 'user'}), 'user-delivered-message')
        self.assertEqual(recent_script_ownership({'kind': 'character-message', 'actor': 'character'}), 'protagonist-delivered-message')
        self.assertEqual(recent_script_ownership({'kind': 'group-message', 'actor': 'user'}), 'external-group-message')
        self.assertEqual(recent_script_ownership({'kind': 'intent-cancelled', 'actor': 'system'}), 'system-event')

        prompt = system_prompt_6('user-message', False, False)
        self.assertRegex(prompt, r'ownership label is authoritative')
        self.assertRegex(prompt, r'a thought about the user is not a thought by the user')

    def test_agency_window_is_available_only_on_background_action_phases_and_stays_separate_from_alter(self) -> None:
        advance = system_prompt_6('advance', False, True, True)
        self.assertRegex(advance, r'agencyWindow may be')
        self.assertRegex(advance, r'Write the protagonist’s life first')
        self.assertRegex(advance, r'must not copy emotionalOffset, infer contact from Alter values')
        self.assertRegex(advance, r'recheck-later')

        user = system_prompt_6('user-message', False, True, True)
        self.assertNotRegex(user, r'agencyWindow may be')
        self.assertNotRegex(user, r'Agency Window describes only practical action capacity')

    def test_perspective_is_a_conditional_individual_values_layer_not_a_recurring_story_theme(self) -> None:
        absent = system_prompt_6('user-message', False, False, False, False)
        enabled = system_prompt_6('user-message', False, False, False, True)
        self.assertNotRegex(absent, r'INDIVIDUAL VALUES AND WAY OF SEEING THE WORLD')
        self.assertRegex(enabled, r'INDIVIDUAL VALUES AND WAY OF SEEING THE WORLD')
        self.assertRegex(enabled, r'separate outer personality layer, distinct from the character canon')
        self.assertRegex(enabled, r'not a story theme, moral review')

        state = {**empty_story_state(), 'setting_overlay': {'character_traits': [], 'perspective': '更愿意先理解人的处境。'}}
        self.assertEqual(story_state_for_prompt(state)['settingOverlay']['perspective'], '更愿意先理解人的处境。')

    def test_chat_action_fields_enter_the_fixed_prompt_only_for_registered_turn_capabilities(self) -> None:
        disabled = system_prompt_6('user-message')
        enabled = system_prompt_6('user-message', False, False, False, False, False, {
            'platform': 'qq', 'quoteReply': True, 'reactions': ['like', 'heart'],
        })
        self.assertNotRegex(disabled, r'CURRENT REGISTERED CHAT ACTIONS')
        self.assertNotRegex(disabled, r'messageReactions')
        self.assertRegex(enabled, r'CURRENT REGISTERED CHAT ACTIONS \(qq\)')
        self.assertRegex(enabled, r'replyTo')
        self.assertRegex(enabled, r'like\|heart')

    def test_native_face_expressions_require_semantic_intent_and_an_explicit_threshold(self) -> None:
        prompt = system_prompt_6('user-message', False, False, False, False, False, {
            'platform': 'qq', 'quoteReply': False, 'reactions': [], 'nativeFaces': ['sweat', 'laugh'], 'expressionThreshold': 0.7,
        })
        self.assertRegex(prompt, r'nativeFace')
        self.assertRegex(prompt, r'willingness')
        self.assertRegex(prompt, r'reaches 0.7')
        self.assertRegex(prompt, r'Do not write bracketed face labels')

    def test_legacy_bracket_faces_are_projected_as_expression_semantics_for_protagonist_history(self) -> None:
        self.assertEqual(prompt_visible_message_content('那就是你傻[流汗]', 'protagonist-delivered-message'), '那就是你傻〈附带汗颜表情〉')
        self.assertEqual(prompt_visible_message_content('用户写了[流汗]', 'user-delivered-message'), '用户写了[流汗]')

    def test_local_sticker_catalog_is_conditional_and_only_permits_exact_listed_assets(self) -> None:
        absent = system_prompt_6('user-message')
        enabled = system_prompt_6('user-message', False, False, False, False, False, None, False, [
            {'assetId': 'laugh/dog', 'group': 'laugh', 'description': '金毛躺平，表示摆烂。', 'aliases': ['躺平'], 'animated': True},
        ])
        self.assertNotRegex(absent, r'CURRENT LOCAL STICKER LIBRARY')
        self.assertRegex(enabled, r'CURRENT LOCAL STICKER LIBRARY')
        self.assertRegex(enabled, r'at most one exact listed sticker')


class PositiveNarrativePromptTests(unittest.TestCase):
    """`upstream/test/positive-narrative-prompt.test.ts`。"""

    def test_live_writing_is_guided_by_event_density_instead_of_elapsed_time_length_quotas(self) -> None:
        prompt = system_prompt_6('user-message')
        self.assertRegex(prompt, r'Length and detail follow what actually happens')
        self.assertRegex(prompt, r'including during a rapid exchange')
        self.assertRegex(prompt, r'quiet interval also has its own occupation')
        self.assertRegex(prompt, r'length of the sent words does not set the depth or length')
        self.assertRegex(prompt, r'When no prior original passage is available')
        self.assertNotRegex(prompt, r'may consist mainly of dialogue|interval can pass lightly')
        self.assertNotRegex(prompt, r'400-700 characters')

    def test_the_visible_reply_remains_an_event_inside_one_causal_script(self) -> None:
        prompt = system_prompt_6('user-message')
        self.assertRegex(prompt, r'next passage AFTER the last completed original')
        self.assertRegex(prompt, r'write speech once, inside the living script')
        self.assertRegex(prompt, r'same causal passage')

    def test_continuity_evolves_positively_without_forced_novelty_templates(self) -> None:
        prompt = system_prompt_6('user-message')
        self.assertRegex(prompt, r'an exchange can stay open')
        self.assertRegex(prompt, r'motives remain implicit')
        self.assertNotRegex(prompt, r'fresh piece of writing')
        self.assertNotRegex(prompt, r'putting the phone away')

    def test_dense_dialogue_continues_from_changed_beats_without_a_fixed_reply_ceremony(self) -> None:
        prompt = system_prompt_6('user-message')
        self.assertRegex(prompt, r'first change not yet written')
        self.assertRegex(prompt, r'need not be restated, but remain present wherever they touch her attention or mood')
        self.assertNotRegex(prompt, r'Keep a consideration, draft, or typing moment')
        self.assertNotRegex(prompt, r'First write the life that has unfolded')

    def test_m5_exposes_only_the_transport_channel_available_to_the_current_phase(self) -> None:
        private_turn = system_prompt_6('user-message')
        group_turn = system_prompt_6('user-message', False, False, False, False, False, None, False, None, False, False, False, True)
        advance = system_prompt_6('advance')
        self.assertRegex(private_turn, r'For this private turn, return interaction')
        self.assertNotRegex(private_turn, r'return groupReply as')
        self.assertRegex(group_turn, r'return groupReply as')
        self.assertNotRegex(group_turn, r'For this private turn, return interaction')
        self.assertRegex(group_turn, r'actually posts to the group')
        self.assertNotRegex(group_turn, r'actually sends a private reply')
        self.assertNotRegex(advance, r'For this private turn, return interaction')


if __name__ == '__main__':
    unittest.main()
