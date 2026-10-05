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
    cd <仓库根目录> && python3 -m unittest plugin.tests.test_narrator_prompts -v
"""

from __future__ import annotations

import json
import re
import unittest
from datetime import datetime, timedelta, timezone

from plugin.core.narrator_prompts import (
    compact_prompt_entries,
    compaction_prompt,
    prompt_visible_message_content,
    recent_script_ownership,
    schedule_preplan_prompt,
    sticker_description_instruction,
    sticker_instruction,
    sticker_selection_instruction,
    story_state_for_prompt,
    system_prompt,
    to_prompt_payload,
    writing_affordances,
)
from plugin.core.specialization import (
    ADMIN_NOTES_FULL,
    BUBBLE_AFFORDANCE,
    BUBBLE_VOICE_AFFORDANCE,
    BUBBLE_VOICE_DISABLED,
    CHANNEL_CONTEXT_FULL,
    CHANNEL_CONTEXT_LITE,
    CHANNELS_FULL,
    CHANNELS_LITE,
    CONTENT_ONLY_TRANSPORT,
    EXTRA_CLAUDE,
    EXTRA_DEEPSEEK,
    EXTRA_GEMINI_FLASH,
    LENGTH_CLAUDE,
    LENGTH_DEEPSEEK,
    LENGTH_GEMINI_FLASH,
    LITE_ADMIN_NOTES,
    LITE_WORLD_EVENTS,
    LIVED_LENGTH_PROMPT,
    LIVED_WRITING_PROMPT,
    MULTI_PLATFORM_TRANSPORT_SELECTION,
    REPETITION_GUARD_TAIL,
    TYPED_MESSAGES_BASE,
    TYPED_MESSAGES_GEMINI_FLASH,
    TYPED_MESSAGES_KIMI,
    WORLD_EVENTS_FULL,
    lite_blocks,
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


def system_prompt_specialty(phase: str, specialty: dict | None, **kwargs: object) -> str:
    """上游 `systemPrompt(...)` + 尾部关键字（特化档 / 多平台开关 / 写作选项）。"""
    return system_prompt(phase, '', '', '', '', '', specialty=specialty, **kwargs)  # type: ignore[arg-type]


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

    def test_current_event_exposes_attachment_kinds(self) -> None:
        """附件种类进 wire payload（受控偏离 §29）。

        没有这一项，表情包、实拍照片、小程序卡片在提示词里长得一模一样。
        """
        req = request([], '看我发的', overrides={
            'images': [{'id': 'turn-image-1', 'data_uri': 'data:image/png;base64,AA'}],
            'attachments': [
                {'index': 1, 'kind': 'sticker', 'label': '[动画表情]', 'summary': '[动画表情]'},
                {'index': 0, 'kind': 'card', 'label': '[QQ小程序：宝箱]', 'summary': ''},
            ],
        })
        payload = to_prompt_payload(req, {'cacheFirst': True})
        event = payload['incomingEvent']['event']
        self.assertEqual(event['type'], 'private-message-batch')
        self.assertEqual(event['imageCount'], 1)
        self.assertEqual(event['attachments'][0]['kind'], 'sticker')
        self.assertEqual(event['attachments'][0]['label'], '[动画表情]')
        # 空 summary 不进 payload（别给模型一个空字符串当证据）
        self.assertNotIn('summary', event['attachments'][1])

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

    def test_the_group_branch_reads_the_same_visual_evidence_fields(self) -> None:
        """v1.9.9：群分支与私聊分支读**同一套**视觉证据字段。

        侧端识图（`vision.mode = sidecar`）下 `imageCount` 是 0，`visualObservations`
        就是这一回合的图片证据。群分支此前不读它 → 群里发来的图 / 视频帧的观察结果
        永远到不了模型（实测：群 `incomingEvent.event` 里根本没有该键）。
        """
        group = {'group_id': '111', 'channel_id': '111', 'label': '群',
                 'purpose': '闲聊', 'character_role': '群友', 'messages': []}
        observations = ['1. 一只橘猫趴在窗台上。']

        req = request([], None, {'group_context': group, 'visual_observations': observations})
        event = to_prompt_payload(req, {'cacheFirst': True})['incomingEvent']['event']
        self.assertEqual(event['type'], 'group-message-batch')
        self.assertEqual(event['imageCount'], 0)
        self.assertEqual(event['visualEvidenceMode'], 'sidecar-observations')
        self.assertEqual(event['visualObservations'], observations, '观察结果必须逐字进群回合')

        # 私聊那条**一个字都不变**（同一个字段、同一套语义）。
        private = request([], '看图', {'visual_observations': observations})
        private_event = to_prompt_payload(private, {'cacheFirst': True})['incomingEvent']['event']
        self.assertEqual(private_event['type'], 'private-message-batch')
        self.assertEqual(private_event['visualEvidenceMode'], 'sidecar-observations')
        self.assertEqual(private_event['visualObservations'], observations)

    def test_the_group_visual_mode_is_honest_about_what_is_present(self) -> None:
        """**反向**：没有画面时说 `none`、有原生图时说 `native-images`，且空观察不进键。"""
        group = {'group_id': '111', 'channel_id': '111', 'label': '群',
                 'purpose': '闲聊', 'character_role': '群友', 'messages': []}

        text_only = to_prompt_payload(
            request([], None, {'group_context': group}), {'cacheFirst': True},
        )['incomingEvent']['event']
        self.assertEqual(text_only['visualEvidenceMode'], 'none')
        self.assertNotIn('visualObservations', text_only)

        empty_observations = to_prompt_payload(
            request([], None, {'group_context': group, 'visual_observations': []}),
            {'cacheFirst': True},
        )['incomingEvent']['event']
        self.assertEqual(empty_observations['visualEvidenceMode'], 'none')
        self.assertNotIn('visualObservations', empty_observations)

        native = to_prompt_payload(request([], None, {
            'group_context': group,
            'images': [{'id': 'turn-image-1', 'data_uri': 'data:image/png;base64,AA'}],
            'visual_observations': ['1. 不该出现的观察。'],
        }), {'cacheFirst': True})['incomingEvent']['event']
        self.assertEqual(native['visualEvidenceMode'], 'native-images')
        self.assertEqual(native['imageCount'], 1)
        self.assertEqual(native['visualObservations'], ['1. 不该出现的观察。'],
                         '原生图优先只体现在 mode 上：给了观察就照实带上（两条证据并存）')

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
        # rc16 起非流式的传输协议句是 0.1.x 的简洁形态（不再是 SCRIPT-FIRST 镜像）。
        self.assertRegex(user, r'When interaction is permitted, its shape is')
        self.assertRegex(user, r'For this private turn, interaction describes ONLY messages to the current private participant')
        self.assertNotRegex(user, r'return groupReply as')
        self.assertNotRegex(user, r'INDEPENDENT LIFE ADVANCE')
        self.assertRegex(advance, r'CURRENT PHASE: INDEPENDENT LIFE ADVANCE')
        self.assertRegex(advance, r'This independent-life phase has no current reply channel')
        self.assertNotRegex(advance, r'For this private turn, interaction describes ONLY')
        self.assertNotRegex(advance, r'interruptedOutgoingDrafts')
        self.assertRegex(follow_up, r'place its exact words at the sending action in script')
        self.assertRegex(due, r'CURRENT PHASE: DUE INTENT')
        self.assertRegex(due, r'For this private turn, interaction describes ONLY messages to the current private participant')

    def test_private_interaction_protocol_is_neutral_about_reading_and_explicit_about_read_but_silent(self) -> None:
        user = system_prompt_6('user-message', False, False)
        # 协议示例不得用字面 false 充当默认值（弱指令模型会照抄示例值）。
        self.assertNotRegex(user, r'interaction as \{"seen":false')
        self.assertRegex(user, r'"seen":<true\|false>')
        self.assertRegex(user, r'seen and reply are independent fields')
        # 已读不回是明确合法的普通状态。
        self.assertRegex(user, r'seen=true with reply\.mode=none is the ordinary read-but-does-not-answer state')
        # rc16 起 immediate 回复写成 content（不再教 actionId 引用）；剧本里写了发送就必须 immediate。
        self.assertRegex(user, r'The content must be exactly the words the script shows her sending')
        self.assertRegex(user, r'Whenever the script shows her actually sending words to the current private participant, reply\.mode must be immediate')
        # 气泡边界：rc17 的负向规则（换行永远不分条）。
        self.assertRegex(user, r'line breaks never separate bubbles')
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
        # 上游 1.0.1-rc25：单条 `perspective` 是总述，`perspectives` 是一组**独立且同等权威**
        # 的条目（可以互相冲突），overlay 在冲突处优先。
        self.assertRegex(enabled, r'is a general statement')
        self.assertRegex(enabled, r'array of independent, equally authoritative entries')
        self.assertRegex(enabled, r'each stays its own lens and they may be in tension')
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

    def test_group_catalog_replaces_the_flat_catalog_for_the_two_step_choice(self) -> None:
        """§48 甲：第一段只给分组目录（不列条目），第二段的提示词另有一条。"""
        groups = [{'groupId': 'collected', 'name': '未整理', 'description': '还没归组。', 'count': 3}]
        prompt = system_prompt_6(
            'user-message', False, False, False, False, False, None, False, None, False, False,
            False, False, None, None, False, groups,
        )
        self.assertRegex(prompt, r'CURRENT LOCAL STICKER LIBRARY')
        self.assertRegex(prompt, r'stickerGroupCatalog')
        self.assertRegex(prompt, r'localMedia: \{"stickerGroupId"')
        self.assertNotRegex(prompt, r'at most one exact listed sticker', '第一段没有条目可挑')
        # 平铺目录（inline）那一段**逐字未变**：两级选择不许动老路径的提示词。
        flat = system_prompt_6('user-message', False, False, False, False, False, None, False, [
            {'assetId': 'a-1', 'group': 'g', 'description': '一只猫', 'aliases': [], 'animated': False},
        ])
        self.assertRegex(flat, r'at most one exact listed sticker')
        self.assertNotRegex(flat, r'stickerGroupCatalog')
        self.assertEqual(sticker_instruction(None, 0.7, None), '')
        self.assertEqual(sticker_instruction(None, 0.7, []), '')

    def test_describe_instruction_asks_for_a_group_only_when_one_is_supplied(self) -> None:
        """§48 乙：不带目录时那次描述调用的问法逐字不变。"""
        plain = sticker_description_instruction(None)
        self.assertRegex(plain, r'Describe this local chat sticker')
        self.assertNotRegex(plain, r'"group"')
        with_groups = sticker_description_instruction(
            [{'groupId': 'g-1', 'name': '猫猫', 'description': '猫、躺平', 'count': 2}],
        )
        self.assertRegex(with_groups, r'"group":\{"existing"')
        self.assertRegex(with_groups, r'"new"')
        self.assertIn('猫猫', with_groups)
        self.assertIn('prefer an existing group', with_groups)
        # §50：两个变体都要问"这到底是不是表情包"（判据在服务层，提示词只负责问出来）。
        for text in (plain, with_groups):
            with self.subTest(text=text[:40]):
                self.assertIn('"is_sticker"', text)
                self.assertIn('"confidence"', text)
                self.assertIn('when unsure answer is_sticker:true', text)

    def test_selection_instruction_asks_for_one_asset_and_the_message(self) -> None:
        text = sticker_selection_instruction(0.7)
        self.assertRegex(text, r'stickerAssetId')
        self.assertRegex(text, r'"content"')
        self.assertRegex(text, r'0\.7')
        self.assertRegex(text, r'never invent an assetId')


class PositiveNarrativePromptTests(unittest.TestCase):
    """`upstream/test/positive-narrative-prompt.test.ts`。"""

    def test_live_writing_is_guided_by_event_density_instead_of_elapsed_time_length_quotas(self) -> None:
        prompt = system_prompt_6('user-message')
        # rc16 起第 1 块是 LIVED_WRITING_PROMPT，第 2 块是长度块（LIVED_LENGTH_PROMPT）。
        self.assertTrue(prompt.startswith(LIVED_WRITING_PROMPT))
        self.assertRegex(prompt, r'Write this as a living stage script in prose')
        self.assertRegex(prompt, r'Length follows what actually happens')
        self.assertIn(LIVED_LENGTH_PROMPT, prompt)
        self.assertRegex(prompt, r'a quiet interval has its own occupation, pace and texture')
        self.assertRegex(prompt, r'including during a rapid exchange')
        self.assertRegex(prompt, r'length of the sent words does not set the depth or length')
        self.assertRegex(prompt, r'When no prior original passage is available')
        self.assertNotRegex(prompt, r'may consist mainly of dialogue|interval can pass lightly')
        self.assertNotRegex(prompt, r'400-700 characters')

    def test_the_visible_reply_remains_an_event_inside_one_causal_script(self) -> None:
        prompt = system_prompt_6('user-message')
        self.assertRegex(prompt, r'next passage AFTER the last completed original')
        # 上游 rc16 起非流式协议不再教 `<say id>` 标记（镜像只留在 opt-in 流式分支）。
        self.assertNotRegex(prompt, r'say id=')
        self.assertRegex(prompt, r'When interaction is permitted, its shape is')
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
        self.assertRegex(private_turn, r'For this private turn, interaction describes ONLY messages to the current private participant')
        self.assertNotRegex(private_turn, r'return groupReply as')
        self.assertRegex(group_turn, r'return groupReply as')
        self.assertRegex(group_turn, r'the exact words posted to the group now')
        self.assertNotRegex(group_turn, r'For this private turn, interaction describes ONLY')
        self.assertRegex(group_turn, r'actually posts to the group')
        self.assertNotRegex(group_turn, r'actually sends a private reply')
        self.assertNotRegex(advance, r'For this private turn, interaction describes ONLY')


class AdminNoteBudgetTests(unittest.TestCase):
    """上游 1.0.1-rc3/rc4：管理员注记有语义权重，且在预算里**有上限地**受保护。"""

    @staticmethod
    def _entry(entry_id: int, kind: str, content: str, at: str = '2026-09-28T10:00:00+00:00') -> dict:
        return {'id': entry_id, 'kind': kind, 'content': content, 'occurredAt': at}

    def test_admin_notes_carry_explicit_semantic_weight_in_the_prompt(self):
        prompt = system_prompt_6('user-message')
        self.assertIn('entries whose content begins with [管理员注记] are authoritative', prompt)
        self.assertIn('carry more weight than ordinary system events', prompt)
        self.assertIn('she follows them without needing to see or reference the note itself', prompt)

    def test_a_huge_admin_note_is_truncated_within_budget_instead_of_pushing_out_the_script(self):
        entries = [
            self._entry(1, 'script', 'A' * 400),
            self._entry(2, 'admin-note', '[管理员注记] ' + 'B' * 9_000),
            self._entry(3, 'script', 'C' * 400),
            self._entry(4, 'admin-note', '短注记'),
        ]
        out = compact_prompt_entries(entries, 12_000)
        by_id = {entry['id']: entry for entry in out}
        # 注记被保留但截断到保护额度内（3 条 × 2000 字、总量 ≤ 预算的 50%）。
        self.assertTrue(by_id[2]['content'].startswith('[管理员注记]'))
        self.assertLessEqual(len(by_id[2]['content']), 2_100)
        self.assertIn('注记截断', by_id[2]['content'])
        # 原始剧本仍然在窗口里（"保护"没有退化成"抢占"）。
        self.assertIn(3, by_id)
        # 超长注记只出现一次，不会既按保护额、又按普通条目重复投放。
        self.assertEqual(len([entry for entry in out if entry['id'] == 2]), 1)

    def test_short_admin_notes_are_protected_verbatim(self):
        entries = [self._entry(1, 'admin-note', '把称呼改成小凌')]
        out = compact_prompt_entries(entries, 1_000)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['content'], '把称呼改成小凌')


class ModelSpecialtyPromptTests(unittest.TestCase):
    """模型特化档在组装层的表现（上游 `upstream/test/specialization.test.ts` 的提示词断言）。"""

    def test_typed_messages_block_appears_exactly_once_per_phase(self) -> None:
        """打字块在私聊 / 推进 / 群聊三相位都恰好一块。"""
        private = system_prompt_6('user-message')
        advance = system_prompt_6('advance')
        group = system_prompt_6('user-message', False, False, False, False, False, None, False, None, False, False, False, True)
        for name, prompt in (('private', private), ('advance', advance), ('group', group)):
            with self.subTest(phase=name):
                self.assertEqual(prompt.count('WRITING BELIEVABLE TYPED MESSAGES:'), 1)
                self.assertIn(TYPED_MESSAGES_BASE, prompt)

    def test_family_offsets_replace_the_length_and_typed_blocks(self) -> None:
        claude = system_prompt_specialty('user-message', {'tier': 'full', 'family': 'claude'})
        self.assertIn(LENGTH_CLAUDE, claude)
        self.assertIn(EXTRA_CLAUDE, claude)          # extraAfterPhase
        self.assertNotIn(LIVED_LENGTH_PROMPT, claude)

        flash = system_prompt_specialty('user-message', {'tier': 'full', 'family': 'gemini-flash'})
        self.assertIn(LENGTH_GEMINI_FLASH, flash)    # length 块
        self.assertIn(TYPED_MESSAGES_GEMINI_FLASH, flash)   # typed 块
        self.assertIn(EXTRA_GEMINI_FLASH, flash)     # extraAfterLength（紧跟在长度块之后）
        self.assertLess(flash.index(LENGTH_GEMINI_FLASH), flash.index(EXTRA_GEMINI_FLASH))
        self.assertLess(flash.index(EXTRA_GEMINI_FLASH), flash.index(TYPED_MESSAGES_GEMINI_FLASH))
        self.assertNotIn(LIVED_LENGTH_PROMPT, flash)
        self.assertNotIn(TYPED_MESSAGES_BASE, flash)

        kimi = system_prompt_specialty('user-message', {'tier': 'full', 'family': 'kimi'})
        self.assertIn(TYPED_MESSAGES_KIMI, kimi)

        deepseek = system_prompt_specialty('user-message', {'tier': 'full', 'family': 'deepseek'})
        self.assertIn(LENGTH_DEEPSEEK, deepseek)

    def test_standard_and_full_tiers_pick_their_transport_contract(self) -> None:
        standard = system_prompt_specialty('user-message', {'tier': 'standard', 'family': 'generic'})
        self.assertIn(CONTENT_ONLY_TRANSPORT, standard)
        self.assertNotIn('say id=', standard)
        self.assertNotIn('When interaction is permitted', standard)

        full = system_prompt_specialty('user-message', {'tier': 'full', 'family': 'generic'})
        self.assertNotIn(CONTENT_ONLY_TRANSPORT, full)
        self.assertIn('When interaction is permitted, its shape is', full)
        # rc16 起 full 档的非流式协议也不再教 `<say id>` 标记：上游
        # `positive-narrative-prompt.test.ts` 与 `narrative-prompts.test.ts` 都断言
        # 默认（full）提示词 `doesNotMatch(/say id=/)`。
        self.assertNotIn('say id=', full)

        lite = system_prompt_specialty('user-message', {'tier': 'lite', 'family': 'generic'})
        self.assertNotIn('EVIDENCE AND EXPECTATION', lite)
        self.assertNotIn('CROSS-CHANNEL DELIVERY', lite)
        self.assertNotIn('SCRIPT-FIRST TRANSPORT MIRROR', lite)
        self.assertNotIn('say id=', lite)
        self.assertIn(CONTENT_ONLY_TRANSPORT, lite)
        self.assertEqual(list(lite_blocks('user-message', False))[:3],
                         [LIVED_WRITING_PROMPT, LIVED_LENGTH_PROMPT, TYPED_MESSAGES_BASE])

    def test_deepseek_transport_line_is_family_scoped_and_crosses_tiers(self) -> None:
        deepseek = system_prompt_specialty('user-message', {'tier': 'standard', 'family': 'deepseek'})
        self.assertIn('TRANSPORT IS PER-TURN', deepseek)
        self.assertIn(CONTENT_ONLY_TRANSPORT, deepseek)
        self.assertIn(EXTRA_DEEPSEEK, deepseek)

        glm = system_prompt_specialty('user-message', {'tier': 'standard', 'family': 'glm'})
        self.assertNotIn('TRANSPORT IS PER-TURN', glm)
        self.assertIn(CONTENT_ONLY_TRANSPORT, glm)

        generic = system_prompt_specialty('user-message', {'tier': 'standard', 'family': 'generic'})
        self.assertNotIn('TRANSPORT IS PER-TURN', generic)

        lite = system_prompt_specialty('user-message', {'tier': 'lite', 'family': 'deepseek'})
        self.assertIn('TRANSPORT IS PER-TURN', lite)
        self.assertIn(CONTENT_ONLY_TRANSPORT, lite)

    def test_full_tier_carries_the_standing_blocks_and_lite_does_not(self) -> None:
        full = system_prompt_specialty('user-message', {'tier': 'full', 'family': 'generic'})
        for block in (ADMIN_NOTES_FULL, WORLD_EVENTS_FULL, CHANNELS_FULL, CHANNEL_CONTEXT_FULL):
            with self.subTest(block=block[:24]):
                self.assertIn(block, full)
        # 常设块排在写作能力段之后。
        self.assertGreater(full.index(ADMIN_NOTES_FULL), full.index('Browsing uses deferred work'))
        self.assertGreater(full.index(WORLD_EVENTS_FULL), full.index(ADMIN_NOTES_FULL))
        self.assertGreater(full.index(CHANNELS_FULL), full.index(WORLD_EVENTS_FULL))
        self.assertGreater(full.index(CHANNEL_CONTEXT_FULL), full.index(CHANNELS_FULL))

        lite = system_prompt_specialty('user-message', {'tier': 'lite', 'family': 'generic'})
        for block in (ADMIN_NOTES_FULL, WORLD_EVENTS_FULL, CHANNELS_FULL, CHANNEL_CONTEXT_FULL):
            with self.subTest(lite_block=block[:24]):
                self.assertNotIn(block, lite)
        # lite 用精简版的 CHANNELS / CHANNEL CONTEXT 与 ADMIN NOTES / WORLD EVENTS。
        self.assertIn(CHANNELS_LITE, lite)
        self.assertIn(CHANNEL_CONTEXT_LITE, lite)
        self.assertIn(LITE_ADMIN_NOTES, lite)
        self.assertIn(LITE_WORLD_EVENTS, lite)

    def test_streaming_branch_keeps_the_script_first_mirror(self) -> None:
        """流式分支保持原样（上游只在 opt-in 流式路径保留镜像）。"""
        streamed = system_prompt_6('user-message', False, False, False, False, False, None, False, None, False, True)
        self.assertRegex(streamed, r'SCRIPT-FIRST TRANSPORT MIRROR')
        self.assertRegex(streamed, r'supply reply\.content directly instead of an id')
        self.assertNotIn(CONTENT_ONLY_TRANSPORT, streamed)
        # standard 档 + 流式仍走镜像函数（上游 `tier === 'standard' && !streamingReplyFirst`）。
        standard_streamed = system_prompt_specialty(
            'user-message', {'tier': 'standard', 'family': 'generic'}, streaming_reply_first=True)
        self.assertRegex(standard_streamed, r'SCRIPT-FIRST TRANSPORT MIRROR')
        self.assertNotIn(CONTENT_ONLY_TRANSPORT, standard_streamed)

    def test_repetition_guard_renders_only_a_detected_fixed_bubble_run(self) -> None:
        hit = system_prompt_specialty(
            'user-message', None, writing_options={'messageRepetition': {'bubbles': 2, 'consecutive': 3}})
        self.assertIn('REPETITION GUARD', hit)
        self.assertIn('exactly 2 separate chat bubbles', hit)
        self.assertIn('each of her last 3', hit)
        guard = hit[hit.index('REPETITION GUARD'):]
        self.assertTrue(guard.startswith('REPETITION GUARD (host observation about the recent script):'))
        self.assertIn(REPETITION_GUARD_TAIL, guard)
        # 渲染在写作能力段尾部（气泡段与 browser 段之后）。
        self.assertGreater(hit.index('REPETITION GUARD'), hit.index('Browsing uses deferred work'))

        single = system_prompt_specialty(
            'user-message', None, writing_options={'messageRepetition': {'bubbles': 1, 'consecutive': 3}})
        self.assertNotIn('REPETITION GUARD', single)

        absent = system_prompt_specialty('user-message', None, writing_options={})
        self.assertNotIn('REPETITION GUARD', absent)

    def test_multi_platform_transport_selection_follows_the_host_flag(self) -> None:
        off = system_prompt_specialty('user-message', None)
        self.assertNotIn('MULTI-PLATFORM TRANSPORT SELECTION', off)
        self.assertNotIn(MULTI_PLATFORM_TRANSPORT_SELECTION, off)

        on = system_prompt_specialty('user-message', None, channel_selection_enabled=True)
        self.assertIn(MULTI_PLATFORM_TRANSPORT_SELECTION, on)

        lite_on = system_prompt_specialty(
            'user-message', {'tier': 'lite', 'family': 'generic'}, channel_selection_enabled=True)
        self.assertIn(MULTI_PLATFORM_TRANSPORT_SELECTION, lite_on)

    def test_no_specialty_is_byte_identical_to_full_generic(self) -> None:
        for phase in ('user-message', 'advance', 'conversation-follow-up', 'intent-due'):
            with self.subTest(phase=phase):
                self.assertEqual(
                    system_prompt_6(phase),
                    system_prompt_specialty(phase, {'tier': 'full', 'family': 'generic'}),
                )
        self.assertEqual(
            system_prompt_specialty('user-message', None, channel_selection_enabled=True),
            system_prompt_specialty('user-message', {'tier': 'full', 'family': 'generic'},
                                    channel_selection_enabled=True),
        )


class VoiceMarkerPromptTests(unittest.TestCase):
    """正文语音标记 `<tts/>` 的提示词（v1.7.7 受控偏离）。

    与 `<sep/>` 那段放在一起、同一套措辞：**想用语音回就写标记，配合分隔符就是
    分段语音**。开关关掉时不许再教它用（照 `splitReplyMessages is False` 的既有写法）。
    """

    def test_the_marker_is_taught_next_to_the_bubble_separator(self) -> None:
        affordances = writing_affordances({})
        self.assertIn(BUBBLE_AFFORDANCE, affordances)
        self.assertIn(BUBBLE_VOICE_AFFORDANCE, affordances)
        self.assertIn('<tts/>', BUBBLE_VOICE_AFFORDANCE)
        self.assertIn('with the separator, each marked segment goes out as its own voice message',
                      BUBBLE_VOICE_AFFORDANCE, '要一句话说清"配合分隔符就是分段语音"')
        self.assertLess(affordances.index(BUBBLE_AFFORDANCE), affordances.index(BUBBLE_VOICE_AFFORDANCE))
        # 默认分隔符下，同一段提示词里同时出现两个标记（模型一次读齐）。
        self.assertIn('<sep/>', affordances)
        self.assertIn('<tts/>', affordances)

    def test_absent_key_keeps_teaching_it(self) -> None:
        """缺键 = 今天的行为（`tts_enabled` 默认开着）：老调用方拿到的提示词包含它。"""
        for options in ({}, {'ttsEnabled': True}, {'tts_enabled': True}):
            with self.subTest(options=options):
                self.assertIn(BUBBLE_VOICE_AFFORDANCE, writing_affordances(options))

    def test_disabled_switch_never_teaches_the_token(self) -> None:
        for options in ({'ttsEnabled': False}, {'tts_enabled': False}):
            with self.subTest(options=options):
                affordances = writing_affordances(options)
                self.assertIn(BUBBLE_VOICE_DISABLED, affordances)
                self.assertNotIn(BUBBLE_VOICE_AFFORDANCE, affordances)
                self.assertNotIn('<tts/>', affordances, '关掉的东西不该把 token 教给模型')

    def test_both_blocks_render_together_in_the_full_system_prompt(self) -> None:
        prompt = system_prompt_6('user-message')
        self.assertIn(BUBBLE_AFFORDANCE, prompt)
        self.assertIn(BUBBLE_VOICE_AFFORDANCE, prompt)

        disabled = system_prompt_specialty(
            'user-message', None, writing_options={'ttsEnabled': False},
        )
        self.assertIn(BUBBLE_AFFORDANCE, disabled)
        self.assertIn(BUBBLE_VOICE_DISABLED, disabled)
        self.assertNotIn('<tts/>', disabled)

    def test_split_disabled_and_voice_disabled_render_together(self) -> None:
        """两个开关都关：气泡段变成"别写分隔符"，语音段变成"别写语音标记"。"""
        affordances = writing_affordances({'splitReplyMessages': False, 'ttsEnabled': False})
        self.assertIn('Message splitting is disabled', affordances)
        self.assertIn(BUBBLE_VOICE_DISABLED, affordances)
        self.assertNotIn('<sep/>', affordances)
        self.assertNotIn('<tts/>', affordances)


class DeliveryRealityFactTests(unittest.TestCase):
    """逐段投递事实：计划被取消 / 没确认送达时，下一回合必须读得到"哪句真发了、哪句没有"。

    受控偏离见 `docs/PORTING_NOTES.md` §58。上游 `ongoingThreads.deliveryReality`
    的每一段只有英语 `outcome` 枚举（`cancelled` / `not-confirmed`），模型读到的是枚举，
    不是"这句没发出去"，于是照自己正文里的「发出去，屏幕按灭」继续写。这里为每段补一行
    极短中文事实（状态词 + 原文）；文案判据只有 `core/script/delivery_reality.segment_fact`。
    """

    #: 真机上她写下的那句正文——它就是"两句都发出去了"的来源。
    PROSE = '她把两句话打了出来，发出去，屏幕按灭。'

    @staticmethod
    def _entry(entry_id: int, segments: list[tuple[str, str]], prose: str,
               commit_id: str = 'c1') -> dict:
        """一条带 M6.1 账本的剧本条目（夹具用生产写入方的形状：`delivery_actions` snake_case）。"""
        return {
            'id': entry_id, 'story_id': 'story', 'participant_id': 'participant',
            'kind': 'script', 'actor': 'narrator', 'content': prose,
            'occurred_at': NOW - timedelta(minutes=5), 'created_at': NOW - timedelta(minutes=5),
            'metadata': {
                'commit_id': commit_id, 'narrative_authority': 'original-v2',
                'delivery_actions': [{
                    'commit_id': commit_id, 'event_id': f'{commit_id}:e1',
                    'event_kind': 'outgoing-message', 'participant_id': 'participant',
                    'status': 'partial',
                    'segments': [
                        {'index': index, 'kind': 'message', 'content': text, 'status': status}
                        for index, (text, status) in enumerate(segments)
                    ],
                    'updated_at': NOW.isoformat(),
                }],
            },
        }

    def _payload(self, entries: list[dict]) -> dict:
        """走真实入口（`to_prompt_payload`）拿下一回合给模型的 payload。"""
        return to_prompt_payload(request(entries, '在吗', {'participant': {'id': 'participant'}}))

    def _facts(self, entries: list[dict]) -> list[str]:
        reality = self._payload(entries)['ongoingThreads']['deliveryReality']
        return [segment['fact'] for item in reality for segment in item['segments']]

    def test_a_cancelled_segment_next_to_a_delivered_one_reaches_the_next_turn_with_its_text(self) -> None:
        """① 一回合两段：一段 delivered、一段 cancelled → 逐段状态 + 原文都在 payload 里。"""
        entry = self._entry(1, [('唔，那不算摸鱼咯', 'delivered'),
                                ('……难怪叫鸡场悟道', 'cancelled')], self.PROSE)
        self.assertEqual(
            [segment['fact'] for segment in
             self._payload([entry])['ongoingThreads']['deliveryReality'][0]['segments']],
            ['已送达：唔，那不算摸鱼咯', '已取消（没发出去）：……难怪叫鸡场悟道'],
        )
        rendered = stringify(self._payload([entry]))
        self.assertIn('已送达：唔，那不算摸鱼咯', rendered)
        self.assertIn('已取消（没发出去）：……难怪叫鸡场悟道', rendered)

    def test_a_segment_the_platform_never_confirmed_stays_unconfirmed(self) -> None:
        """真机另一种现场：两句里第二句 `pending`（未确认送达），不许写成已送达。"""
        entry = self._entry(1, [('唔，那不算摸鱼咯', 'delivered'),
                                ('……就着那些下饭啊', 'pending')], self.PROSE)
        self.assertEqual(self._facts([entry]),
                         ['已送达：唔，那不算摸鱼咯', '未确认送达：……就着那些下饭啊'])

    def test_each_segment_fact_names_what_actually_happened(self) -> None:
        """③ 判据表：四种段状态各自的事实词，且没发出去 / 没确认的不许写成已送达。"""
        for status, label in (('delivered', '已送达'), ('pending', '未确认送达'),
                              ('failed', '未确认送达（出错）'),
                              ('cancelled', '已取消（没发出去）')):
            with self.subTest(status=status):
                # 全 delivered 的行动按上游规则整条不进上下文，因此给被测那段配一个
                # 尚未确认的兄弟段，让它真的出现在 payload 里。
                entry = self._entry(1, [('那句原文', status), ('尚未确认的另一句', 'pending')],
                                    self.PROSE)
                facts = self._facts([entry])
                self.assertEqual(facts[0], f'{label}：那句原文')
                if status != 'delivered':
                    self.assertNotIn('已送达', facts[0], '没发出去 / 没确认的不许写成已送达')

    def test_a_fully_delivered_turn_adds_no_noise(self) -> None:
        """② 全部 delivered → 不出现"未送达 / 已取消"这类噪音。"""
        entry = self._entry(1, [('唔，那不算摸鱼咯', 'delivered'),
                                ('……就着那些下饭啊', 'delivered')], self.PROSE)
        payload = self._payload([entry])
        self.assertEqual(payload['ongoingThreads']['deliveryReality'], [])
        rendered = stringify(payload)
        for word in ('已送达', '未确认送达', '已取消', 'not-confirmed', 'cancelled',
                     'delivery-not-confirmed-after-error'):
            self.assertNotIn(word, rendered)


class DeliveryDisciplinePromptTests(unittest.TestCase):
    """提示词纪律：不许把已取消的分段写成"发出去 / 说出去了"（`docs/PORTING_NOTES.md` §58）。"""

    def test_the_writing_prompt_forbids_narrating_a_cancelled_segment_as_sent(self) -> None:
        for phase in ('user-message', 'advance'):
            with self.subTest(phase=phase):
                prompt = system_prompt_6(phase)
                self.assertIn('Never write a cancelled segment as sent or spoken', prompt)
                self.assertIn('never write a not-confirmed segment as confirmed', prompt)
                # 纪律必须把三个状态词与事实行绑在一起，模型才认得出 payload 里的中文。
                self.assertIn('已送达 = platform-accepted', prompt)
                self.assertIn('未确认送达 = receipt unknown', prompt)
                self.assertIn('已取消（没发出去）= never sent', prompt)

    def test_the_compaction_prompt_never_freezes_a_cancelled_segment_as_sent(self) -> None:
        """压缩提示词也要守同一条纪律：摘要是**跨回合活下来**的那份记录。"""
        prompt = compaction_prompt('')
        self.assertIn('never carry a cancelled segment into a summary or fact as sent', prompt)
        self.assertIn('keep an unconfirmed one unconfirmed', prompt)


class BrowserContractPromptTests(unittest.TestCase):
    """浏览契约的**字段名**：提示词教的必须就是解析器读的（`docs/PORTING_NOTES.md` §79）。

    上游一直教单数 `browserIntent`（`narrator.ts:1750` 起），而解析器读的是复数数组
    `browserIntents`（`service.ts:10678`）——模型照提示词吐的那个对象永远读不到，
    她只能一直"还在转"。这里把两边钉在一起：提示词里出现的**每一个**浏览器字段名，
    解析器都必须能读（两种拼写都行，但都不许读不到）。
    """

    #: 一份合法草稿（search 需要 query，visit 需要公开 url）。
    DRAFT = {'mode': 'search', 'query': '妹妹', 'purpose': '想看看他妹妹是谁', 'timing': 'deferred'}

    def prompt_field_names(self, text: str) -> set[str]:
        """提示词里出现的字段名（`browserIntent` / `browserIntents`）。"""
        return set(re.findall(r'browserIntents?\b', text))

    def test_every_browser_field_name_the_prompt_teaches_is_readable(self) -> None:
        from plugin.core.service.chunk4 import _browser_intent_drafts

        texts = [
            writing_affordances({'browserMode': mode})
            for mode in ('disabled', 'deferred-only', 'allow-immediate')
        ]
        # 常设块里那句 `An entry in browserIntents …` 也点名了字段。
        texts.append(system_prompt_6('user-message'))
        seen: set[str] = set()
        for text in texts:
            names = self.prompt_field_names(text)
            self.assertTrue(names, text)
            for name in names:
                seen.add(name)
                with self.subTest(name=name):
                    self.assertEqual(
                        _browser_intent_drafts({name: [dict(self.DRAFT)]}), [self.DRAFT],
                        '提示词点名的字段，解析器必须读得到（数组形态）',
                    )
                    self.assertEqual(
                        _browser_intent_drafts({name: dict(self.DRAFT)}), [self.DRAFT],
                        '提示词点名的字段，解析器必须读得到（单对象形态）',
                    )
        self.assertIn('browserIntents', seen)

    def test_the_taught_contract_is_the_plural_list_the_parser_reads(self) -> None:
        for mode in ('deferred-only', 'allow-immediate'):
            with self.subTest(mode=mode):
                text = writing_affordances({'browserMode': mode})
                self.assertIn('browserIntents as a list with at most one item', text)
                # 上游那句"return at most one browserIntent"（单数对象）必须不再出现。
                self.assertNotIn('at most one browserIntent.', text)
        # disabled 那一段本来就是复数（"leave browserIntents empty"）。
        self.assertIn('leave browserIntents empty', writing_affordances({'browserMode': 'disabled'}))


class PlatformActionInstructionTests(unittest.TestCase):
    """动作教学第一段：`platform_action_instruction(request)`（v1.9.9，§86）。

    这是**注入路径**那个入口（`narrator.ts` 逐字调它）。用户点名的两段式要求它只给
    "屏幕上有哪些按钮"：**不含参数说明、不含参数枚举**；参数表只有她选定某条动作之后
    才由 `platform_action_params_instruction()` 给。
    """

    def test_request_entry_renders_only_ids_and_labels(self) -> None:
        from plugin.core.narrator_prompts import platform_action_instruction

        text = platform_action_instruction({'platformActions': ['send_poke', 'publish_qzone_post']})
        self.assertIn('- send_poke 戳一戳', text)
        self.assertIn('- publish_qzone_post 发说说', text)
        self.assertNotIn('content', text)
        self.assertNotIn('ugc_right', text)
        self.assertNotIn('必填', text)
        self.assertNotIn('|', text)

    def test_request_entry_ignores_ids_that_are_not_available(self) -> None:
        from plugin.core.narrator_prompts import platform_action_instruction

        # 可用集里没有的一律不渲染（可用集由 chunk12 一处判完，这里只渲染）。
        self.assertEqual(platform_action_instruction({'platformActions': ['nosuch_action']}), '')

    def test_request_entry_accepts_both_spellings_and_rejects_junk(self) -> None:
        from plugin.core.narrator_prompts import platform_action_instruction

        snake = platform_action_instruction({'platform_actions': ['send_poke']})
        camel = platform_action_instruction({'platformActions': ['send_poke']})
        self.assertEqual(snake, camel)
        for junk in ({}, None, 'x', {'platformActions': None}, {'platformActions': 'send_poke'}):
            with self.subTest(junk=junk):
                self.assertEqual(platform_action_instruction(junk), '')


class SchedulePreplanPromptTests(unittest.TestCase):
    """`schedule_preplan_prompt()`：审查教学两行（上游 `src/narrator.ts:2374,2375`）。

    上游 `test/schedule-preplan.test.ts:190` 对**源码**做正则断言（那一份在
    `test_schedule_preplan.SchedulePreplanTeachingTests`，读 `narrator_prompts.py` 源码）；
    这里守的是**渲染结果**：三个 variationLevel 下两行都逐字在，且不会漏进主叙事提示词。
    """

    #: 上游 `src/narrator.ts:2374` 逐字（含末尾句号）。
    CANCELLATION_LINE = (
        'A cancellation, a reschedule, or a newly confirmed one-off arrangement in committed script '
        'belongs to exceptions for its exact date. Do NOT change weekly blocks because of a single '
        'occurrence: a regime may change only when evidence shows the new time repeating on separate '
        'dates or being stated as permanent.'
    )
    #: 上游 `src/narrator.ts:2375` 逐字（破折号是 U+2014）。
    EVIDENCE_LINE = (
        'Exception evidence must be committed fact — the plan was actually cancelled, the time was actually '
        'moved, or the arrangement was explicitly confirmed. A wish, a suggestion, a tentative idea, or an '
        'unexecuted plan in conversation is not evidence for any exception or regime change.'
    )

    def test_teaching_lines_are_rendered_verbatim_for_every_variation_level(self) -> None:
        for level in ('stable', 'contextual', 'granular'):
            with self.subTest(variation_level=level):
                prompt = schedule_preplan_prompt(level)
                self.assertIn(self.CANCELLATION_LINE, prompt)
                self.assertIn(self.EVIDENCE_LINE, prompt)

    def test_teaching_lines_stay_inside_the_schedule_preplan_prompt(self) -> None:
        """反向：这两行只属于 Preplan 侧任务，主叙事提示词里一个字都不该有。"""
        main = system_prompt(
            'advance', '', '', '', '', '', False, False, False, False, False, None, False, None, False)
        self.assertNotIn('Do NOT change weekly blocks', main)
        self.assertNotIn('is not evidence for any exception or regime change', main)
        self.assertIn('Do NOT change weekly blocks', schedule_preplan_prompt('stable'))


if __name__ == '__main__':
    unittest.main()
