"""正文语音标记 `<tts/>`（v1.7.7 受控偏离）的单元测试。

覆盖四层，每一层单独可查：

1. **解析**（`core/bubbles.py`）：标记粒度 = 分段；标记本身绝不进发出的文本；
   畸形标记（`<tts>` / `<TTS/>` / `<tts />`）**不认**且原样留在文本里。
2. **剧本提交**（`core/script/commit_builder.py`）：气泡与正文都不带标记，
   成稿校验仍然通过，投递定位仍能按（带标记的）模型原文找到那条事件。
3. **投递准备**（`core/delivery.py`）：语音意图随分段一起流转（首条 `voice`、
   后续段 `later_segments_voice`），非语音路径的 payload 形状一字不变。
4. **开关与动作可用性**（`core/service/*`）：`model.audio.tts_enabled` 关掉时
   标记被忽略（**退回文字、内容一字不少**），`send_voice` / `list_voices` 不可用。

运行：`python3 -m unittest plugin.tests.test_voice_marker -v`
"""

from __future__ import annotations

import unittest

from plugin.core.bubbles import (
    VOICE_MARKER,
    bubble_texts,
    normalize_bubble_segments,
    runtime_bubble_segments,
    split_bubble_segments,
    strip_voice_marker,
)
from plugin.core.delivery import attach_message_event, prepare_outgoing_delivery
from plugin.core.script.commit_builder import (
    _canonical_bubble_content,
    _split_bubbles,
    decision_to_script_commit,
    find_outgoing_script_event,
)
from plugin.core.script.validator import validate_script_commit
from plugin.core.platform_actions import ACTIONS, VOICE_ACTION_IDS
from plugin.core.service import InterludeContext, NullTransport
from plugin.core.service.chunk6 import ServiceChunk6
from plugin.core.service.chunk12 import ServiceChunk12

NOW = __import__('datetime').datetime(2026, 9, 5, 8, 0, tzinfo=__import__('datetime').timezone.utc)
PROSE = '她想了想，还是把话说了出来。'


def _config(**model_audio: object) -> dict:
    return {
        'model': {'audio': dict(model_audio)},
        'runtime': {},
        'logging': {'level': 'debug', 'verbosity': 'diagnostic'},
    }


def _service(config: dict | None = None) -> ServiceChunk6:
    ctx = InterludeContext(logger=None, database=None, bots=None)
    return ServiceChunk6(ctx, config if config is not None else _config(), None, NullTransport())


# =========================================================================== #
# 1. 解析
# =========================================================================== #

class MarkerParsingTests(unittest.TestCase):
    """`core/bubbles.py`：标记粒度 = 分段，标记本身不是内容。"""

    def test_a_single_marker_makes_the_whole_reply_one_voice_segment(self):
        segments = split_bubble_segments('晚安，早点睡' + VOICE_MARKER)
        self.assertEqual(segments, [{'content': '晚安，早点睡', 'voice': True}])
        self.assertNotIn(VOICE_MARKER, segments[0]['content'])

    def test_separator_plus_marker_sends_every_segment_as_its_own_voice(self):
        segments = split_bubble_segments('甲' + VOICE_MARKER + '<sep/>乙' + VOICE_MARKER)
        self.assertEqual(segments, [
            {'content': '甲', 'voice': True},
            {'content': '乙', 'voice': True},
        ])

    def test_the_marker_granularity_is_the_segment(self):
        """只在某一段写了标记 → 只有那一段是语音（其余照旧发文字）。"""
        segments = split_bubble_segments('先回一句<sep/>这段用语音' + VOICE_MARKER + '<sep/>再补一句')
        self.assertEqual([item['voice'] for item in segments], [False, True, False])
        self.assertEqual([item['content'] for item in segments], ['先回一句', '这段用语音', '再补一句'])
        self.assertTrue(all(VOICE_MARKER not in item['content'] for item in segments))

    def test_malformed_markers_are_not_recognized_and_stay_in_the_text(self):
        """`<tts>` / `<TTS/>` / `<tts />` 一律不认，原样留给用户看到。

        宽松化会让模型写出来的畸形串漏进发出的文本——所以这里要的是**反例**：
        它们既不触发语音，也**不被删掉**。
        """
        for malformed in ('<tts>', '<TTS/>', '<tts />', '<Tts/>', '<tts>text'):
            with self.subTest(marker=malformed):
                segments = split_bubble_segments('晚安' + malformed)
                self.assertEqual(len(segments), 1)
                self.assertFalse(segments[0]['voice'], '畸形标记不得被当成语音意图')
                self.assertIn(malformed, segments[0]['content'], '畸形标记必须原样留在文本里')

    def test_a_reply_with_marker_split_disabled_is_one_voice_segment(self):
        segments = split_bubble_segments('甲<sep/>乙' + VOICE_MARKER, '<sep/>', False)
        self.assertEqual(segments, [{'content': '甲<sep/>乙', 'voice': True}])

    def test_voice_disabled_still_strips_the_marker_and_keeps_every_character(self):
        """开关关掉 = **退回发文字**：标记删掉、内容一字不少（不是丢消息）。"""
        segments = split_bubble_segments('甲' + VOICE_MARKER + '<sep/>乙', '<sep/>', True, False)
        self.assertEqual(segments, [
            {'content': '甲', 'voice': False},
            {'content': '乙', 'voice': False},
        ])

    def test_blank_separator_falls_back_to_the_default(self):
        # 分隔符只写空白 = 没写（沿用既有口径），回落 `<sep/>`。
        segments = runtime_bubble_segments({'messageSeparator': '   '}, '甲<sep/>乙' + VOICE_MARKER)
        self.assertEqual(segments[0]['content'], '甲')
        self.assertEqual(segments[1], {'content': '乙', 'voice': True})

    def test_all_separator_content_yields_no_segment(self):
        self.assertEqual(split_bubble_segments('<sep/>'), [])

    def test_bubble_texts_falls_back_to_the_whole_text_without_the_marker(self):
        # 全是分隔符 → 一段都切不出来 → 退回整条（上游 `splitBubbles` 的既有兜底），
        # 但标记必须已经删掉。
        self.assertEqual(bubble_texts('<sep/>' + VOICE_MARKER), ['<sep/>'])
        self.assertEqual(bubble_texts(VOICE_MARKER), [''])
        self.assertEqual(bubble_texts('甲<sep/>乙'), ['甲', '乙'])

    def test_strip_voice_marker_reports_whether_it_was_there(self):
        self.assertEqual(strip_voice_marker('甲' + VOICE_MARKER), ('甲', True))
        self.assertEqual(strip_voice_marker('甲' + VOICE_MARKER * 2), ('甲', True))
        self.assertEqual(strip_voice_marker('甲'), ('甲', False))
        # 非字符串按空文本处理（配置 / 模型给的畸形值不该在这里炸）。
        self.assertEqual(strip_voice_marker(None), ('', False))

    def test_runtime_segments_read_the_configured_separator(self):
        self.assertEqual(
            runtime_bubble_segments({'messageSeparator': '||'}, '甲||乙' + VOICE_MARKER),
            [{'content': '甲', 'voice': False}, {'content': '乙', 'voice': True}],
        )
        self.assertEqual(
            runtime_bubble_segments({'splitReplyMessages': False}, '甲||乙' + VOICE_MARKER),
            [{'content': '甲||乙', 'voice': True}],
        )

    def test_normalize_bubble_segments_accepts_both_existing_shapes(self):
        self.assertEqual(normalize_bubble_segments(['甲', '乙']),
                         [{'content': '甲', 'voice': False}, {'content': '乙', 'voice': False}])
        self.assertEqual(normalize_bubble_segments([{'content': '甲', 'voice': True}]),
                         [{'content': '甲', 'voice': True}])
        self.assertEqual(normalize_bubble_segments(None), [])


# =========================================================================== #
# 2. 剧本提交
# =========================================================================== #

def _commit(content: str, **overrides: object) -> dict:
    input_: dict = {
        'story_id': 's',
        'participant_id': 'alice',
        'phase': 'user-message',
        'from': NOW,
        'now': NOW,
        'decision': {
            'script': PROSE,
            'interaction': {'seen': True, 'reply': {'mode': 'immediate', 'content': content}},
        },
        'message_separator': '<sep/>',
        'split_reply_messages': True,
        'frame_id': 'frame-1',
        'burst_id': 'burst-1',
    }
    input_.update(overrides)
    return decision_to_script_commit(input_)


class CommitBuilderMarkerTests(unittest.TestCase):
    """标记绝不进剧本正文 / 气泡，也不破坏成稿校验与投递定位。"""

    def test_bubbles_and_canonical_content_drop_the_marker(self):
        commit = _commit('甲' + VOICE_MARKER + '<sep/>乙' + VOICE_MARKER)
        event = commit['events'][-1]
        self.assertEqual(event['bubbles'], ['甲', '乙'])
        self.assertEqual(event['content'], '甲<sep/>乙')
        self.assertNotIn(VOICE_MARKER, event['content'])
        self.assertTrue(validate_script_commit(commit, '<sep/>')['valid'])

    def test_split_disabled_also_drops_the_marker_from_the_canonical_content(self):
        """关掉分条时 `canonicalBubbleContent` 原样回正文——但标记必须先删，
        否则成稿校验会拿"删过标记的气泡"跟"没删标记的正文"比对，判成 content mismatch。"""
        raw = '甲' + VOICE_MARKER + '乙'
        commit = _commit(raw, **{'split_reply_messages': False})
        event = commit['events'][-1]
        self.assertEqual(event['content'], '甲乙')
        self.assertTrue(validate_script_commit(commit, '<sep/>')['valid'])

    def test_the_script_event_is_still_located_by_the_raw_model_reply(self):
        """模型原文带标记，剧本事件不带；投递定位必须仍然命中那条事件。"""
        raw = '甲' + VOICE_MARKER + '<sep/>乙'
        commit = _commit(raw)
        event = find_outgoing_script_event(commit, 'alice', 'immediate', raw, '<sep/>')
        self.assertIsNotNone(event)
        self.assertEqual(event['bubbles'], ['甲', '乙'])

    def test_helper_functions_keep_the_upstream_fallbacks(self):
        self.assertEqual(_split_bubbles('a<sep/>b'), ['a', 'b'])
        self.assertEqual(_split_bubbles('a<sep/>b', '<sep/>', False), ['a<sep/>b'])
        self.assertEqual(_split_bubbles(VOICE_MARKER), [''])
        self.assertEqual(_canonical_bubble_content('甲' + VOICE_MARKER, ['甲'], '<sep/>', False), '甲')


# =========================================================================== #
# 3. 投递准备
# =========================================================================== #

class DeliveryPreparationTests(unittest.TestCase):
    def test_voice_travels_with_the_segment_it_belongs_to(self):
        prepared = prepare_outgoing_delivery(
            {'participant_id': 'alice', 'content': '甲<sep/>乙<sep/>丙'},
            [{'content': '甲', 'voice': False},
             {'content': '乙', 'voice': True},
             {'content': '丙', 'voice': True}],
        )
        self.assertIsNotNone(prepared)
        self.assertEqual(prepared['content'], '甲')
        self.assertNotIn('voice', prepared, '首段不是语音时不许凭空多一个键')
        self.assertEqual(prepared['later_segments'], ['乙', '丙'])
        self.assertEqual(prepared['later_segments_voice'], [True, True])

    def test_first_segment_voice_marks_the_message(self):
        prepared = prepare_outgoing_delivery(
            {'content': 'x'}, [{'content': '甲', 'voice': True}, {'content': '乙', 'voice': False}],
        )
        self.assertTrue(prepared['voice'])
        self.assertNotIn('later_segments_voice', prepared)

    def test_plain_text_bubbles_keep_the_historical_payload_shape(self):
        """老调用方传 `list[str]`：payload 与历史版本逐字一致（一个键都不多）。"""
        prepared = prepare_outgoing_delivery({'participant_id': 'alice', 'content': '甲<sep/>乙'},
                                             ['甲', '乙'])
        self.assertEqual(prepared, {
            'participant_id': 'alice', 'content': '甲', 'later_segments': ['乙'],
        })

    def test_empty_first_segment_still_abandons_the_delivery(self):
        self.assertIsNone(prepare_outgoing_delivery({'content': 'x'}, [{'content': '', 'voice': True}]))

    def test_script_event_identity_survives_the_voice_shape(self):
        event = {'commit_id': 'c', 'event_id': 'c:e1', 'kind': 'outgoing-message',
                 'actor': 'protagonist', 'occurred_at': '2026-09-05T08:00:00.000Z',
                 'content': '甲<sep/>乙', 'bubbles': ['甲', '乙'],
                 'delivery_mode': 'immediate', 'caused_by_event_ids': []}
        prepared = prepare_outgoing_delivery(
            attach_message_event({'participant_id': 'alice', 'content': event['content']}, event),
            [{'content': '甲', 'voice': False}, {'content': '乙', 'voice': True}],
        )
        self.assertEqual(prepared['script_event']['bubble_count'], 2)


# =========================================================================== #
# 4. 开关与动作可用性
# =========================================================================== #

class VoiceSwitchTests(unittest.TestCase):
    """`model.audio.tts_enabled`：缺键按 True，关掉后退回文字。"""

    def test_missing_key_keeps_todays_behaviour(self):
        service = _service(_config())
        self.assertTrue(service.voice_reply_enabled)
        self.assertEqual(service.split_outgoing_segments('甲' + VOICE_MARKER),
                         [{'content': '甲', 'voice': True}])

    def test_disabled_switch_ignores_the_marker_but_never_drops_content(self):
        for spelling in ({'tts_enabled': False}, {'ttsEnabled': False}):
            with self.subTest(spelling=spelling):
                service = _service(_config(**spelling))
                self.assertFalse(service.voice_reply_enabled)
                self.assertEqual(
                    service.split_outgoing_segments('甲' + VOICE_MARKER + '<sep/>乙' + VOICE_MARKER),
                    [{'content': '甲', 'voice': False}, {'content': '乙', 'voice': False}],
                    '关掉开关 = 退回发文字，内容一字不少，标记也不许发出去',
                )

    def test_ignoring_the_marker_leaves_a_visible_warning(self):
        """标记被忽略必须看得见：剧本散文里往往写着"（发了条语音）"。"""
        service = _service(_config(tts_enabled=False))
        warnings: list[str] = []
        service.report_standalone = lambda level, message, *args: warnings.append(
            '%s:%s' % (level, message % args if args else message),
        )
        service.split_outgoing_segments('晚安' + VOICE_MARKER)
        self.assertTrue(any('语音标记已忽略' in item for item in warnings), warnings)
        # 开关开着时不该刷这条：正常路径没有"被忽略"这回事。
        reported: list[str] = []
        service = _service(_config())
        service.report_standalone = lambda level, message, *args: reported.append(level)
        service.split_outgoing_segments('晚安' + VOICE_MARKER)
        self.assertEqual(reported, [])

    def test_switch_is_read_from_the_model_audio_section_not_the_top_level(self):
        """段位错了就是静默失效：顶层 `audio` 永远读不到（与 `audio_config` 同一条纪律）。"""
        service = _service({'model': {}, 'audio': {'tts_enabled': False}, 'runtime': {}})
        self.assertTrue(service.voice_reply_enabled)

    def test_disabled_switch_removes_the_voice_actions(self):
        enabled = ServiceChunk12(InterludeContext(logger=None), _config(), None, NullTransport())
        disabled = ServiceChunk12(
            InterludeContext(logger=None), _config(tts_enabled=False), None, NullTransport(),
        )
        self.assertEqual(sorted(VOICE_ACTION_IDS), ['list_voices', 'send_voice'])
        self.assertTrue(all(action_id in ACTIONS for action_id in VOICE_ACTION_IDS))
        for action_id in sorted(VOICE_ACTION_IDS):
            with self.subTest(action=action_id):
                self.assertNotEqual(enabled.action_switch(action_id), False)
                self.assertIs(disabled.action_switch(action_id), False,
                              '关掉总闸后语音类动作必须走既有的"开关关掉"路径')
        self.assertIn('send_voice', enabled.available_platform_actions('', ('private',), {}))
        self.assertNotIn('send_voice', disabled.available_platform_actions('', ('private',), {}))

    def test_other_actions_are_untouched_by_the_switch(self):
        disabled = ServiceChunk12(
            InterludeContext(logger=None), _config(tts_enabled=False), None, NullTransport(),
        )
        available = disabled.available_platform_actions('', ('private',), {})
        self.assertNotIn('send_voice', available)
        self.assertIn('send_poke', available)


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
