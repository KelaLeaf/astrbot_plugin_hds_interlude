"""`plugin/core/service/helpers.py` + `config.py` 的单元测试（stdlib `unittest`）。

逐条移植自上游测试：

* `upstream/test/time-context.test.ts` 中断言 `normalizeDatabaseRow` /
  `extractUserReportedTimes` / `detectLiveScriptTimeOverflow` 的用例
  （完整版见 `plugin/tests/test_time_context.py`，那边通过 `plugin.core.service`
  惰性加载；本文件聚焦 `plugin.core.service.helpers` 本体，断言逐字一致）；
* `upstream/test/timeline-director-normalize.test.ts`（全部 3 条）；
* `upstream/test/agency.test.ts` 里 `groupDueIntents` 的用例；
* `upstream/test/configuration.test.ts` 中 `normalizeGroupVisibleReply` /
  `visibleReplyMode` / `normalizeInteraction` / `hasRequiredNarrativeScript` 的用例；
* `upstream/test/sticker-vision-helpers.test.ts`（全部 5 条）；
* `upstream/test/group-identity.test.ts` 中 `formatGroupSpeaker` /
  `normalizeGroupChatActions` / `normalizeAllowedReactions` /
  `calibratedNativeFaceWillingness` / `normalizeQuotedMessageContent` /
  `describeQuotedMessage` 的用例；
* `upstream/test/voice-transcription.test.ts` 中语音 / 附件 / 音频格式用例。

上游 `m10-cooperation.test.ts` **没有** `groupDueIntents` 用例（它只 import
`InterludeService` 类），故无对应用例可移植——此处如实记录，不臆造。
"""

from __future__ import annotations

import unittest
import zlib
from datetime import datetime, timezone
from urllib.parse import quote

from plugin.core.service import helpers as h

_UTC = timezone.utc


def _dt(text: str) -> datetime:
    """上游 `new Date('...Z')`。"""
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


class DatabaseRowTests(unittest.TestCase):
    """`upstream/test/time-context.test.ts` 的 `normalizeDatabaseRow` 用例。"""

    def test_reload_style_iso_timestamp_rows_are_materialized_as_datetime_objects(self):
        # 上游断言：
        #   const normalized = normalizeDatabaseRow('interlude_story', {...})
        #   assert.ok(normalized.cursorAt instanceof Date) ×3
        #   assert.equal(normalized.cursorAt.toISOString(), '2026-08-23T07:55:00.000Z')
        #   assert.equal(normalized.state.agencyWindow, undefined)
        normalized = h.normalize_database_row('interlude_story', {
            'id': 'story',
            'state': {
                'schema_version': 1, 'setting_overlay': {'character_traits': []},
                'automation': {}, 'narrative_update_count': 0,
            },
            'cursorAt': '2026-08-23T07:55:00.000Z',
            'createdAt': '2026-08-20T00:00:00.000Z',
            'updatedAt': '2026-08-23T08:00:00.000Z',
        })
        self.assertIsInstance(normalized['cursorAt'], datetime)
        self.assertIsInstance(normalized['createdAt'], datetime)
        self.assertIsInstance(normalized['updatedAt'], datetime)
        self.assertEqual(normalized['cursorAt'].isoformat(), '2026-08-23T07:55:00+00:00')
        self.assertIsNone(normalized['state'].get('agencyWindow'))

    def test_story_row_fills_created_updated_and_cursor_from_each_other(self):
        # 上游：`toDate(row.createdAt) ?? new Date()` / `?? updatedAt` / `?? updatedAt`。
        normalized = h.normalize_database_row('interlude_story', {'cursorAt': None, 'createdAt': None})
        self.assertIsInstance(normalized['createdAt'], datetime)
        self.assertEqual(normalized['updatedAt'], normalized['createdAt'])
        self.assertEqual(normalized['cursorAt'], normalized['updatedAt'])

    def test_cursor_at_explicit_null_materializes_from_updated_at(self):
        normalized = h.normalize_database_row('interlude_story', {
            'createdAt': '2026-08-20T00:00:00.000Z',
            'updatedAt': '2026-08-23T08:00:00.000Z',
            'cursorAt': None,
        })
        self.assertEqual(normalized['cursorAt'].isoformat(), '2026-08-23T08:00:00+00:00')

    def test_story_state_json_column_is_decoded_into_an_object(self):
        normalized = h.normalize_database_row('interlude_story', {
            'state': '{"schemaVersion": 1, "settingOverlay": {"characterTraits": []}, "automation": {}}',
        })
        # 上游把 state 列交给 decodeStoryState；本移植版额外容忍 json 列仍是字符串
        # （Database.decode_row 之外的裸读路径）。
        self.assertIsInstance(normalized['state'], dict)

    def test_participant_row_normalizes_state_and_timestamps(self):
        normalized = h.normalize_database_row('interlude_participant', {
            'createdAt': '2026-08-20T00:00:00.000Z',
            'state': {'openThreads': ['a'], 'unreadMessageCount': 3.9},
        })
        self.assertEqual(normalized['updatedAt'], normalized['createdAt'])
        self.assertEqual(normalized['state']['openThreads'], ['a'])
        self.assertEqual(normalized['state']['unreadMessageCount'], 3)

    def test_other_tables_only_materialize_their_timestamp_columns(self):
        normalized = h.normalize_database_row('interlude_script_entry', {
            'occurredAt': '2026-08-23T08:00:00.000Z', 'createdAt': None, 'participantId': 'p',
        })
        self.assertIsInstance(normalized['occurredAt'], datetime)
        self.assertIsNone(normalized['createdAt'])
        self.assertEqual(normalized['participantId'], 'p')

    def test_non_record_rows_pass_through_untouched(self):
        self.assertIsNone(h.normalize_database_row('interlude_story', None))
        self.assertEqual(h.normalize_database_row('interlude_story', 'x'), 'x')

    def test_database_date_fields_match_the_upstream_table_map(self):
        # 上游 DATABASE_DATE_FIELDS 的逐表映射（列名为数据库 wire format，camelCase）。
        self.assertEqual(h.database_date_fields('interlude_intent'), ['notBefore', 'createdAt', 'updatedAt'])
        self.assertEqual(
            h.database_date_fields('interlude_overlay_snapshot'),
            ['periodStart', 'periodEnd', 'createdAt', 'updatedAt'],
        )
        self.assertEqual(h.database_date_fields('interlude_unknown_table'), [])


class UserReportedTimeTests(unittest.TestCase):
    """`upstream/test/time-context.test.ts` 的 `extractUserReportedTimes` 用例。"""

    def test_explicit_user_reported_clocks_stay_distinct_from_the_message_receive_time(self):
        facts = h.extract_user_reported_times(
            '我 6.30 开始吃，刚吃完', _dt('2026-08-31T11:36:00.000Z'), 'Asia/Shanghai')
        self.assertEqual(facts, [{
            'localTime': '2026-08-31 18:30', 'relation': 'past', 'statement': '我 6.30 开始吃，刚吃完',
        }])

    def test_chinese_clock_reference_is_extracted(self):
        # 上游 `test_user_endorsed_clocks_...`：中文"八点"必须能被提取为自报时间。
        facts = h.extract_user_reported_times(
            '。。。我看你怎么在八点赶到万松园', _dt('2026-09-03T23:47:40.000Z'), 'Asia/Shanghai')
        self.assertTrue(any(fact['localTime'].endswith('08:00') for fact in facts))

    def test_arabic_and_chinese_forms_agree_on_the_endorsed_minute(self):
        now = _dt('2026-09-03T23:47:40.000Z')
        arabic = h.extract_user_reported_times('我8点出门', now, 'Asia/Shanghai')
        chinese = h.extract_user_reported_times('八点整她出门', now, 'Asia/Shanghai')
        self.assertEqual({fact['localTime'][-5:] for fact in arabic}, {'08:00'})
        self.assertEqual({fact['localTime'][-5:] for fact in chinese}, {'08:00'})

    def test_period_word_anchors_and_wraparound(self):
        facts = h.extract_user_reported_times(
            '我们中午一起吃饭吧', _dt('2026-09-03T03:19:43.000Z'), 'Asia/Shanghai')
        self.assertTrue(any(fact['localTime'].endswith('12:00') for fact in facts))

    def test_duplicate_clocks_are_deduplicated_and_capped_at_four(self):
        facts = h.extract_user_reported_times(
            '八点八点九点十点十一点十二点', _dt('2026-09-03T03:19:43.000Z'), 'Asia/Shanghai')
        self.assertLessEqual(len(facts), 4)
        keys = [(fact['localTime'], fact['statement']) for fact in facts]
        self.assertEqual(len(keys), len(set(keys)))


class LiveScriptOverflowTests(unittest.TestCase):
    """`upstream/test/time-context.test.ts` 的 `detectLiveScriptTimeOverflow` 用例。"""

    def setUp(self):
        # from = 08:35 Shanghai，now = 08:45 Shanghai（上游第一条守卫用例）。
        self.from_at = _dt('2026-09-01T00:35:00.000Z')
        self.now = _dt('2026-09-01T00:45:00.000Z')

    def test_short_live_windows_reject_explicit_future_clocks_and_multiple_lesson_stages(self):
        self.assertRegex(
            h.detect_live_script_time_overflow(
                '九点二十，她走进下一节课的教室。', 'user-message', self.from_at, self.now, 'Asia/Shanghai') or '',
            'explicit clock')
        self.assertRegex(
            h.detect_live_script_time_overflow(
                '第一节课结束后，她去上第二节数学课。', 'user-message', self.from_at, self.now, 'Asia/Shanghai') or '',
            'multiple lesson stages')
        self.assertIsNone(h.detect_live_script_time_overflow(
            '她继续在第一节课记笔记。', 'user-message', self.from_at, self.now, 'Asia/Shanghai'))
        self.assertIsNone(h.detect_live_script_time_overflow(
            '第一节课结束后，她去上第二节数学课。', 'advance', self.from_at, self.now, 'Asia/Shanghai'))

    def test_midnight_wraparound_and_latency_grace_do_not_drop_legitimate_live_scripts(self):
        from_at = _dt('2026-09-01T15:56:00.000Z')  # 23:56 Shanghai
        now = _dt('2026-09-01T16:01:00.000Z')      # 00:01 Shanghai (+1 day)
        self.assertIsNone(h.detect_live_script_time_overflow(
            '屏幕亮起，23:58，他的消息停在午夜前三分钟。', 'user-message', from_at, now, 'Asia/Shanghai'))
        self.assertIsNone(
            h.detect_live_script_time_overflow(
                '11:58 的那条消息还停在屏幕上。', 'user-message', from_at, now, 'Asia/Shanghai'),
            '12 小时制歧义按刚过去的过去处理')
        self.assertIsNone(h.detect_live_script_time_overflow(
            '00:05，闹钟该响了，她还没睡。', 'user-message', from_at, now, 'Asia/Shanghai'))
        self.assertRegex(
            h.detect_live_script_time_overflow(
                '凌晨两点，她终于合上笔记本。', 'user-message', from_at, now, 'Asia/Shanghai') or '',
            'explicit clock')

    def test_headline_window_semantics_narrative_declarations_block_references_and_user_deadlines_pass(self):
        from_at = _dt('2026-09-03T23:30:00.000Z')   # 07:30 Shanghai
        now = _dt('2026-09-03T23:47:40.000Z')       # 07:47:40 Shanghai

        def run(message: str, script: str, now_at: datetime | None = None):
            moment = now_at or now
            facts = h.extract_user_reported_times(message, moment, 'Asia/Shanghai') if message else []
            return h.detect_live_script_time_overflow(
                script, 'user-message', from_at, moment, 'Asia/Shanghai', _endorsed_clock_minutes(facts))

        self.assertIsNone(run(
            '。。。我看你怎么在八点赶到万松园',
            '水濑看了一眼时间，还不到七点五十。她想起小桃说的八点赶到万松园的约定，手忙脚乱地收拾书包。'))
        self.assertIsNone(
            run('希望你能在九点前赶到万松园', '她抓起书包冲出门。08:47，她气喘吁吁地赶到了万松园校门口。',
                _dt('2026-09-04T00:05:49.000Z')),
            '用户期限（九点前=540）授权区间内的到达时刻')
        self.assertIsNone(
            run('我们中午一起吃饭吧', '十二点的铃声响了，她收起课本走向食堂。',
                _dt('2026-09-03T03:19:43.000Z')),
            '时段词（中午=12:00）作为背书锚点')
        self.assertRegex(run('', '八点整，她出现在校门口，课已经开始了。') or '', 'explicit clock')
        self.assertRegex(run('', '08:20，教室里已经坐满了人。') or '', 'explicit clock')
        self.assertIsNone(run('', '她一边刷牙一边想着上午的事。昨天说好今天要早到。'))

    def test_user_endorsed_clocks_from_the_message_exempt_the_guard_while_unendorsed_ones_stay_blocked(self):
        from_at = _dt('2026-09-03T23:30:00.000Z')
        now = _dt('2026-09-03T23:47:40.000Z')
        message = '。。。我看你怎么在八点赶到万松园'
        facts = h.extract_user_reported_times(message, now, 'Asia/Shanghai')
        self.assertTrue(any(fact['localTime'].endswith('08:00') for fact in facts),
                        '中文“八点”必须能被提取为自报时间')
        endorsed = _endorsed_clock_minutes(facts)
        script = '水濑看了一眼时间，还不到七点五十。她想起小桃说的八点赶到万松园的约定，手忙脚乱地收拾书包。'
        self.assertIsNone(h.detect_live_script_time_overflow(
            script, 'user-message', from_at, now, 'Asia/Shanghai', endorsed))
        numeric_endorsed = _endorsed_clock_minutes(
            h.extract_user_reported_times('我8点出门', now, 'Asia/Shanghai'))
        self.assertIsNone(h.detect_live_script_time_overflow(
            '八点整她出门。', 'user-message', from_at, now, 'Asia/Shanghai', numeric_endorsed))
        self.assertRegex(
            h.detect_live_script_time_overflow(
                '她抬头看了钟：08:40，才慢悠悠出门。', 'user-message', from_at, now, 'Asia/Shanghai',
                endorsed) or '',
            'explicit clock')
        self.assertRegex(
            h.detect_live_script_time_overflow(
                '八点四十她才出门。', 'user-message', from_at, now, 'Asia/Shanghai', endorsed) or '',
            'explicit clock')
        self.assertIsNone(h.detect_live_script_time_overflow(
            script, 'advance', from_at, now, 'Asia/Shanghai'))

    def test_long_live_windows_and_empty_scripts_are_never_guarded(self):
        from_at = _dt('2026-09-01T00:00:00.000Z')
        now = _dt('2026-09-01T02:00:00.000Z')  # 120 分钟 > 60
        self.assertIsNone(h.detect_live_script_time_overflow(
            '九点二十，她走进下一节课的教室。', 'user-message', from_at, now, 'Asia/Shanghai'))
        self.assertIsNone(h.detect_live_script_time_overflow(
            '  ', 'user-message', self.from_at, self.now, 'Asia/Shanghai'))
        self.assertIsNone(h.detect_live_script_time_overflow(
            None, 'user-message', self.from_at, self.now, 'Asia/Shanghai'))


def _endorsed_clock_minutes(facts) -> set[int]:
    """上游 `new Set(facts.map(f => Number(f.localTime.slice(-5,-3)) * 60 + Number(f.localTime.slice(-2))))`。"""
    return {int(fact['localTime'][-5:-3]) * 60 + int(fact['localTime'][-2:]) for fact in facts}


class TimelinePlanNormalizeTests(unittest.TestCase):
    """`upstream/test/timeline-director-normalize.test.ts` 的全部 3 条。"""

    def test_coerces_string_positions_and_near_miss_kinds(self):
        plan = h.normalize_timeline_plan({
            'beats': [
                {'at': '0', 'kind': 'activity', 'summary': '继续随堂练习'},
                {'at': '50%', 'kind': 'scene', 'summary': '课堂进行中'},
                {'at': 1, 'kind': '状态', 'summary': '窗口结束时仍在课堂'},
            ],
            'carry': ['午间验收未发生'],
        })
        self.assertIsNotNone(plan, '宽容解析应接受字符串 at 与别名 kind')
        self.assertEqual([beat['at'] for beat in plan['beats']], [0, 0.5, 1])
        self.assertEqual(plan['beats'][1]['kind'], 'activity')
        self.assertEqual(plan['beats'][2]['kind'], 'state')
        self.assertEqual(plan['carry'], ['午间验收未发生'])

    def test_still_rejects_genuinely_unusable_beats(self):
        self.assertIsNone(h.normalize_timeline_plan({'beats': [{'at': 'abc', 'kind': 'activity', 'summary': 'x'}]}))
        self.assertIsNone(h.normalize_timeline_plan({'beats': [{'at': 0.5, 'kind': 3, 'summary': 'x'}]}))
        self.assertIsNone(h.normalize_timeline_plan({'beats': [{'at': 0.5, 'kind': 'activity'}]}))
        self.assertIsNone(h.normalize_timeline_plan({'beats': []}))
        self.assertIsNone(h.normalize_timeline_plan({'noBeats': True}))
        self.assertIsNone(h.normalize_timeline_plan('not an object'))

    def test_explains_each_rejected_beat(self):
        reason = h.describe_timeline_plan_rejection({
            'beats': [
                {'at': '0', 'kind': 'activity', 'summary': 'ok'},
                {'at': 'abc', 'kind': 3},
                {},
            ],
        })
        self.assertRegex(reason, '节点校验详情')
        self.assertRegex(reason, '通过')
        self.assertRegex(reason, 'at="abc" 无法解析')
        self.assertRegex(reason, 'kind=3 非法')
        self.assertRegex(reason, 'summary 为空')
        self.assertEqual(h.describe_timeline_plan_rejection({}), '缺少 beats 数组')
        self.assertEqual(h.describe_timeline_plan_rejection({'beats': []}),
                         'beats 为空数组（模型未产出任何节点）')
        self.assertEqual(h.describe_timeline_plan_rejection(None), '返回不是 JSON 对象')

    def test_beats_are_sorted_and_capped_at_four(self):
        plan = h.normalize_timeline_plan({'beats': [
            {'at': 0.9, 'kind': 'state', 'summary': 'd'},
            {'at': 0.1, 'kind': 'state', 'summary': 'a'},
            {'at': 0.5, 'kind': 'state', 'summary': 'c'},
            {'at': 0.3, 'kind': 'state', 'summary': 'b'},
            {'at': 0.7, 'kind': 'state', 'summary': 'e'},
        ]})
        # 按 at 升序后：a(0.1) b(0.3) c(0.5) e(0.7) d(0.9)，取前 4 条。
        self.assertEqual([beat['summary'] for beat in plan['beats']], ['a', 'b', 'c', 'e'])
        self.assertNotIn('carry', plan)

    def test_prompt_projection_replaces_automatic_script_prose_with_the_host_ledger(self):
        entry = {
            'id': 1, 'kind': 'script', 'content': '原始散文',
            'metadata': {'timelinePlan': {'beats': [{'at': 0.5, 'kind': 'activity', 'summary': '上课'}],
                                          'carry': ['未发生的事']}},
        }
        projected = h.timeline_entry_prompt_projection(entry)
        self.assertEqual(
            projected['content'],
            '[Host timeline ledger for this completed automatic window: 50% activity: 上课. Carry: 未发生的事]')
        self.assertEqual(entry['content'], '原始散文')
        # 原始权威条目与普通剧本条目原样返回。
        original = {'kind': 'script', 'metadata': {'narrativeAuthority': 'original-v2', 'timelinePlan': {'beats': []}}}
        self.assertIs(h.timeline_entry_prompt_projection(original), original)
        plain = {'kind': 'character-message', 'themetadata': None}
        self.assertIs(h.timeline_entry_prompt_projection(plain), plain)


class GroupDueIntentTests(unittest.TestCase):
    """`upstream/test/agency.test.ts` 的 `groupDueIntents` 用例。

    上游 `m10-cooperation.test.ts` 只 import `InterludeService` 类，并不含
    `groupDueIntents` 断言，故无可移植用例。
    """

    def test_proactive_checks_are_isolated_from_ordinary_due_messages_for_the_same_participant(self):
        now = _dt('2026-08-24T08:00:00.000Z')

        def intent(intent_id: int, intent_type: str) -> dict:
            return {
                'id': intent_id, 'storyId': 'story', 'participantId': 'friend', 'type': intent_type,
                'summary': intent_type, 'notBefore': now, 'status': 'pending', 'payload': {},
                'createdAt': now, 'updatedAt': now,
            }

        batches = h.group_due_intents([intent(1, 'delayed-reply'), intent(2, 'proactive-check')])
        self.assertEqual(len(batches), 2)
        self.assertEqual(sorted(batch[0]['type'] for batch in batches), ['delayed-reply', 'proactive-check'])

    def test_batches_are_keyed_by_participant_and_family_ordered_by_not_before_then_id(self):
        early = _dt('2026-08-24T08:00:00.000Z')
        late = _dt('2026-08-24T09:00:00.000Z')

        def intent(intent_id: int, participant: str, intent_type: str, at: datetime) -> dict:
            return {'id': intent_id, 'participantId': participant, 'type': intent_type,
                    'notBefore': at, 'summary': str(intent_id)}

        batches = h.group_due_intents([
            intent(5, 'a', 'delayed-reply', late),
            intent(3, 'b', 'delayed-reply', early),
            intent(2, 'a', 'delayed-reply', early),
            intent(9, 'a', 'delayed-reply', early),
        ])
        self.assertEqual(len(batches), 2)
        by_first_id = sorted(batch[0]['id'] for batch in batches)
        self.assertEqual(by_first_id, [2, 3])
        a_batch = [batch for batch in batches if batch[0]['participantId'] == 'a'][0]
        self.assertEqual([item['id'] for item in a_batch], [2, 9, 5])

    def test_global_intents_share_the_global_key(self):
        now = _dt('2026-08-24T08:00:00.000Z')
        batches = h.group_due_intents([
            {'id': 1, 'participantId': None, 'type': 'delayed-reply', 'notBefore': now},
            {'id': 2, 'participantId': '', 'type': 'delayed-reply', 'notBefore': now},
        ])
        self.assertEqual(len(batches), 1)
        self.assertEqual([item['id'] for item in batches[0]], [1, 2])


class VisibleReplyTests(unittest.TestCase):
    """`upstream/test/configuration.test.ts` 的可见回复 / 交互契约用例。"""

    def test_group_transport_accepts_its_explicit_field_and_the_legacy_immediate_interaction_fallback(self):
        self.assertEqual(h.normalize_group_visible_reply({'mode': 'immediate', 'content': '群内回复'}, None, 100), '群内回复')
        self.assertEqual(
            h.normalize_group_visible_reply(None, {'seen': True, 'reply': {'mode': 'immediate', 'content': '兼容回复'}}, 100),
            '兼容回复')
        self.assertEqual(h.normalize_group_visible_reply(None, {'seen': True, 'reply': {'mode': 'none'}}, 100), '')
        self.assertEqual(h.normalize_group_visible_reply({'mode': 'immediate', 'content': '[表情]'}, None, 100), '')
        self.assertEqual(
            h.normalize_group_visible_reply({'mode': 'immediate', 'content': '第一句<sep>第二句'}, None, 100),
            '第一句<sep/>第二句')
        self.assertEqual(
            h.normalize_group_visible_reply({'mode': 'immediate', 'content': '第一句＜sep＞第二句'}, None, 100),
            '第一句<sep/>第二句')

    def test_reply_mode_logs_distinguish_missing_live_replies_from_normal_background_silence(self):
        self.assertEqual(h.visible_reply_mode({}, 'user-message'), '未提供或无效')
        self.assertEqual(h.visible_reply_mode({}, 'conversation-follow-up'), '无可见投递')
        self.assertEqual(h.visible_reply_mode({}, 'advance'), '无可见投递')
        self.assertEqual(
            h.visible_reply_mode(
                {'crossConversationActions': [{'participantId': 'friend', 'mode': 'immediate', 'content': '在吗'}]},
                'advance'),
            '主动联系')
        self.assertEqual(
            h.visible_reply_mode({'interaction': {'seen': True, 'reply': {'mode': 'none'}}}, 'intent-due'), 'none')

    def test_reply_mode_covers_group_scope_and_delayed_plans(self):
        group_context = {'groupId': '100', 'messages': []}
        self.assertEqual(
            h.visible_reply_mode({'groupReply': {'mode': 'immediate', 'content': 'x'}}, 'user-message', group_context),
            'group:immediate')
        self.assertEqual(
            h.visible_reply_mode({'interaction': {'seen': True, 'reply': {'mode': 'none'}}}, 'user-message', group_context),
            'group-fallback:none')
        self.assertEqual(h.visible_reply_mode({}, 'user-message', group_context), '未提供或无效')
        self.assertEqual(
            h.visible_reply_mode(
                {'crossConversationActions': [{'mode': 'delayed', 'content': 'x'}]}, 'advance'),
            '计划联系')

    def test_normalize_interaction_keeps_seen_and_reply_independent(self):
        now = _dt('2026-09-09T12:00:00Z')
        runtime = {'maxMessageCharacters': 500, 'messageSeparator': '<sep/>',
                   'minimumDelayedReplySeconds': 10, 'maximumDelayedReplyMinutes': 120}
        self.assertEqual(h.normalize_interaction({'seen': True, 'reply': {'mode': 'none'}}, now, runtime),
                         {'seen': True, 'reply': {'mode': 'none'}})
        self.assertEqual(h.normalize_interaction({'seen': False, 'reply': {'mode': 'none'}}, now, runtime),
                         {'seen': False, 'reply': {'mode': 'none'}})
        self.assertEqual(
            h.normalize_interaction({'seen': False, 'reply': {'mode': 'immediate', 'content': '想起来还没回你'}},
                                    now, runtime),
            {'seen': False, 'reply': {'mode': 'immediate', 'content': '想起来还没回你'}})
        self.assertEqual(h.normalize_interaction({'seen': False, 'reply': {'mode': 'immediate'}}, now, runtime),
                         {'seen': False, 'reply': {'mode': 'none'}})
        self.assertIsNone(h.normalize_interaction({'seen': True, 'reply': {'mode': 'later'}}, now, runtime))

    def test_delayed_replies_outside_the_allowed_window_still_collapse_to_none(self):
        now = _dt('2026-09-09T12:00:00Z')
        runtime = {'maxMessageCharacters': 500, 'messageSeparator': '<sep/>',
                   'minimumDelayedReplySeconds': 10, 'maximumDelayedReplyMinutes': 120}
        send_at = h.iso(h.parse_dt(h.dt_ms(now) + 5 * 60_000))
        self.assertEqual(
            h.normalize_interaction({'seen': False, 'reply': {'mode': 'delayed', 'content': '晚点说', 'sendAt': send_at}},
                                    now, runtime),
            {'seen': False, 'reply': {'mode': 'delayed', 'content': '晚点说', 'sendAt': send_at}})
        too_soon = h.iso(h.parse_dt(h.dt_ms(now) + 1_000))
        self.assertEqual(
            h.normalize_interaction({'seen': True, 'reply': {'mode': 'delayed', 'content': '太早', 'sendAt': too_soon}},
                                    now, runtime),
            {'seen': True, 'reply': {'mode': 'none'}})

    def test_interaction_snake_case_input_is_also_accepted(self):
        # 键名法：从外部读入时 camelCase 与 snake_case 双读。
        now = _dt('2026-09-09T12:00:00Z')
        runtime = {'maxMessageCharacters': 500, 'messageSeparator': '<sep/>',
                   'minimumDelayedReplySeconds': 10, 'maximumDelayedReplyMinutes': 120}
        self.assertEqual(
            h.normalize_interaction({'seen': False, 'reply': {'mode': 'delayed', 'content': '晚点说',
                                                             'send_at': h.iso(h.parse_dt(h.dt_ms(now) + 60_000))}},
                                    now, runtime)['reply']['mode'],
            'delayed')

    def test_visible_replies_never_materialize_echoed_attachment_markup_into_real_sends(self):
        # upstream/test/voice-transcription.test.ts
        runtime = {'maxMessageCharacters': 500, 'messageSeparator': '<sep/>',
                   'minimumDelayedReplySeconds': 10, 'maximumDelayedReplyMinutes': 120}
        interaction = h.normalize_interaction({
            'seen': True,
            'reply': {
                'mode': 'immediate',
                'content': '给你听听这个<file src="http://223.109.208.77/asn.com/qqdownloadftnv5?rkey=xyz" '
                           'name="音频.mp3"/>还有图<img src="https://cdn/x.jpg"/>[CQ:record,file=a.mp3]',
            },
        }, h.utc_now(), runtime)
        self.assertEqual(interaction['reply']['content'], '给你听听这个还有图')

    def test_real_model_turns_require_non_empty_narrative_prose(self):
        self.assertEqual(h.has_required_narrative_script({'script': '主角收起手机，继续往前走。'}), True)
        self.assertEqual(h.has_required_narrative_script({'script': '   \n'}), False)
        self.assertEqual(
            h.has_required_narrative_script({'interaction': {'seen': True, 'reply': {'mode': 'none'}}}), False)

    def test_blind_mode_defaults_to_a_minimal_ten_minute_heartbeat(self):
        # upstream/test/configuration.test.ts
        self.assertEqual(h.resolve_blind_mode_config(),
                         {'enabled': False, 'health_report_minutes': 10})
        self.assertEqual(h.resolve_blind_mode_config({'enabled': True, 'healthReportMinutes': 2}),
                         {'enabled': True, 'health_report_minutes': 2})
        # 上限 / 下限夹取（上游 Math.max(1, Math.min(1440, ...))）。
        self.assertEqual(h.resolve_blind_mode_config({'healthReportMinutes': 0})['health_report_minutes'], 1)
        self.assertEqual(h.resolve_blind_mode_config({'healthReportMinutes': 99_999})['health_report_minutes'], 1_440)
        # @deprecated 别名指向同一函数。
        self.assertIs(h.resolve_black_box_config, h.resolve_blind_mode_config)


class StickerVisionHelperTests(unittest.TestCase):
    """`upstream/test/sticker-vision-helpers.test.ts` 的全部 5 条。"""

    def test_rank_passes_through_when_below_the_limit_or_without_a_query_vector(self):
        assets = [{'id': 1}, {'id': 2}, {'id': 3}]
        self.assertEqual(h.rank_sticker_catalog(assets, [1, 0], 12), assets)
        self.assertEqual(h.rank_sticker_catalog(assets, [], 2), assets)

    def test_orders_by_cosine_similarity_and_fills_leftover_slots_with_unvectorized_assets(self):
        assets = [
            {'id': 1, 'embedding': [0, 1]},
            {'id': 2, 'embedding': [1, 0]},
            {'id': 3},
            {'id': 4, 'embedding': [0.9, 0.1]},
        ]
        ranked = h.rank_sticker_catalog(assets, [1, 0], 3)
        self.assertEqual([item['id'] for item in ranked], [2, 4, 1])

    def test_semantic_sticker_limit_is_twelve(self):
        self.assertEqual(h.SEMANTIC_STICKER_LIMIT, 12)

    def test_should_downscale_image_gates_mime_types_and_small_payloads(self):
        big = 'A' * 220_000
        small = 'A' * 1_000
        self.assertEqual(h.should_downscale_image('image/jpeg', 'data:image/jpeg;base64,%s' % big), True)
        self.assertEqual(h.should_downscale_image('image/png', 'data:image/png;base64,%s' % big), True)
        self.assertEqual(h.should_downscale_image('image/webp', 'data:image/webp;base64,%s' % big), True)
        self.assertEqual(h.should_downscale_image('image/gif', 'data:image/gif;base64,%s' % big), False)
        self.assertEqual(h.should_downscale_image('image/svg+xml', 'data:image/svg+xml;base64,%s' % big), False)
        self.assertEqual(h.should_downscale_image('image/jpeg', 'data:image/jpeg;base64,%s' % small), False)
        self.assertEqual(h.should_downscale_image('image/jpeg', 'data:image/jpeg;base64,'), False)

    def test_stable_sticker_asset_id_keeps_punctuation_colliding_filenames_globally_distinct(self):
        hash_a = 'a' * 64
        hash_b = 'b' * 64
        self.assertEqual(h.stable_sticker_asset_id('bq (6).png', hash_a),
                         h.stable_sticker_asset_id('bq (6).png', hash_a))
        self.assertNotEqual(h.stable_sticker_asset_id('bq (6).png', hash_a),
                            h.stable_sticker_asset_id('bq [6].png', hash_b))
        self.assertRegex(h.stable_sticker_asset_id('bq (6).png', hash_a), r'aaaaaaaaaaaaaaaa$')


class StickerGuessHelperTests(unittest.TestCase):
    """第二层判据的纯函数（本移植版新增，受控偏离 `§45.7`）。

    这一层只在**便宜**这一侧正确才有意义：宽高要能在不引入 Pillow 的前提下读出来，
    明显不像表情包的图要在调模型之前被挡掉，而"收不收"的阈值只有一处。
    """

    def test_image_dimensions_are_read_from_the_header_of_the_four_allowed_formats(self):
        self.assertEqual(h.guess_image_dimensions(png(320, 200)), (320, 200))
        self.assertEqual(h.guess_image_dimensions(png(320, 200, color_type=6)), (320, 200))
        self.assertEqual(h.guess_image_dimensions(gif(96, 64)), (96, 64))
        self.assertEqual(h.guess_image_dimensions(jpeg(640, 480)), (640, 480))
        self.assertEqual(h.guess_image_dimensions(webp_vp8x(300, 200)), (300, 200))
        self.assertEqual(h.guess_image_dimensions(webp_vp8(120, 90)), (120, 90))
        self.assertEqual(h.guess_image_dimensions(webp_vp8l(64, 48)), (64, 48))

    def test_unparsable_or_unknown_bytes_have_no_dimensions(self):
        for label, payload in (
            ('空字节', b''),
            ('None', None),
            ('纯文本', b'this is not an image at all, really'),
            ('只有魔数的残缺 PNG', b'\x89PNG\r\n\x1a\n' + b'x' * 4),
            ('没有 SOF 的 JPEG', b'\xff\xd8' + b'\xff\xd9'),
            ('VP8X 但没有尺寸字段', b'RIFF\x10\x00\x00\x00WEBPVP8X\x04\x00\x00\x00\x00\x00\x00\x00'),
        ):
            with self.subTest(label=label):
                self.assertIsNone(h.guess_image_dimensions(payload))

    def test_the_prefilter_only_rejects_what_clearly_is_not_a_sticker(self):
        # 近方形 + 两边都小 = 表情包的典型尺寸。
        self.assertTrue(h.sticker_guess_candidate(png(120, 120), 'image/png'))
        self.assertTrue(h.sticker_guess_candidate(png(512, 400, color_type=2), 'image/png'))
        # 大图 / 长宽比明显像照片或截图 → 不值得问模型。
        self.assertFalse(h.sticker_guess_candidate(png(2000, 1500, color_type=2), 'image/png'))
        self.assertFalse(h.sticker_guess_candidate(png(800, 300, color_type=2), 'image/png'))
        self.assertFalse(h.sticker_guess_candidate(jpeg(1920, 1080), 'image/jpeg'))
        # 解析不出宽高 = 拿不准 = 不花钱。
        self.assertFalse(h.sticker_guess_candidate(b'\x89PNG\r\n\x1a\n' + b'x' * 40, 'image/png'))
        self.assertFalse(h.sticker_guess_candidate(None, 'image/png'))

    def test_gifs_and_transparent_pngs_are_always_candidates(self):
        # 聊天里 GIF 几乎只当动图 / 表情用，哪怕它是张大图。
        self.assertTrue(h.sticker_guess_candidate(gif(800, 600), 'image/gif'))
        # 透明底是表情的典型特征：色彩类型 4 / 6 与 tRNS 三种都要认。
        self.assertTrue(h.sticker_guess_candidate(png(900, 900, color_type=6), 'image/png'))
        self.assertTrue(h.sticker_guess_candidate(png(900, 900, color_type=4), 'image/png'))
        self.assertTrue(h.sticker_guess_candidate(png(900, 900, color_type=2, trns=True), 'image/png'))
        # 不透明的大 PNG 仍然被挡（alpha 才放行）。
        self.assertFalse(h.sticker_guess_candidate(png(900, 900, color_type=2), 'image/png'))

    def test_the_name_signal_is_the_platform_name_only(self):
        """名字信号（§49.1）：**方括号包起来的平台命名**才算，`[图片]` 占位不算。

        它是唯一"不用字节"的结构信号，也是候选档（`sub_type` 2/3/7）在入站时能判的那一半。
        """
        self.assertEqual(h.sticker_media_signal(name='[中午好]'), h.STICKER_SIGNAL_NAME)
        self.assertEqual(h.sticker_media_signal(name='[动画表情]'), h.STICKER_SIGNAL_NAME)
        # `[图片]` 是普通图占位（NapCat 对 picSubType=0 写的就是它）—— 不算名字。
        self.assertEqual(h.sticker_media_signal(name='[图片]'), h.STICKER_SIGNAL_NONE)
        for value in ('中午好', '[中午好', '中午好]', '[]', '', None, '[图片] x', 'x[图片]'):
            with self.subTest(name=value):
                self.assertEqual(h.sticker_media_signal(name=value), h.STICKER_SIGNAL_NONE)
        # 名字与字节信号各自独立：缺名字时尺寸仍然照判。
        self.assertEqual(h.sticker_media_signal(mime_type='image/gif', data=b''), h.STICKER_SIGNAL_GIF)

    def test_the_prefilter_delegates_to_the_shared_signal(self):
        """第二层预筛就是共享信号函数的薄包装（§49.1：一份实现两处用）。"""
        for payload, mime in (
            (png(120, 120), 'image/png'),
            (gif(800, 600), 'image/gif'),
            (png(2000, 1500, color_type=2), 'image/png'),
            (png(900, 900, color_type=6), 'image/png'),
        ):
            with self.subTest(mime=mime):
                self.assertEqual(
                    h.sticker_guess_candidate(payload, mime),
                    bool(h.sticker_media_signal(mime_type=mime, data=payload)),
                )

    def test_the_internal_candidate_kind_is_normalized_on_the_wire(self):
        """候选档是**内部**状态：出 wire 一律变普通图，提示词只认那 5 个值（§49.1）。"""
        self.assertEqual(h.wire_media_kind(h.STICKER_CANDIDATE_KIND), 'image')
        for kind in ('image', 'sticker', 'animated', 'market', 'card'):
            with self.subTest(kind=kind):
                self.assertEqual(h.wire_media_kind(kind), kind)
        # 外部写法原样透传（不许多管闲事），空值回落到普通图。
        self.assertEqual(h.wire_media_kind('photo'), 'photo')
        self.assertEqual(h.wire_media_kind(''), 'image')
        self.assertEqual(h.wire_media_kind(None), 'image')
        self.assertNotIn(h.STICKER_CANDIDATE_KIND, h.WIRE_MEDIA_KINDS)

    def test_acceptance_needs_a_confident_boolean_yes(self):
        accepted = h.sticker_guess_result({
            'is_sticker': True, 'kind': 'meme', 'confidence': 0.85, 'description': '  一只猫  ',
        })
        self.assertEqual(accepted, {
            'is_sticker': True, 'kind': 'meme', 'confidence': 0.85, 'description': '一只猫',
        })
        # 阈值是启发式常量：正好在阈值上算通过。
        self.assertIsNotNone(h.sticker_guess_result(
            {'is_sticker': True, 'confidence': h.GUESS_STICKER_MIN_CONFIDENCE},
        ))
        rejected = [
            ('判成照片', {'is_sticker': False, 'kind': 'photo', 'confidence': 0.99}),
            ('置信度不够', {'is_sticker': True, 'confidence': 0.59}),
            ('缺 confidence', {'is_sticker': True, 'kind': 'meme'}),
            ('confidence 不是数', {'is_sticker': True, 'confidence': '0.9'}),
            ('confidence 是布尔', {'is_sticker': True, 'confidence': True}),
            ('is_sticker 是字符串', {'is_sticker': 'true', 'confidence': 0.99}),
            ('is_sticker 是 1（不是布尔）', {'is_sticker': 1, 'confidence': 0.99}),
            ('空对象', {}),
            ('不是对象', 'not json'),
            ('None', None),
            ('列表', [1, 2]),
        ]
        for label, value in rejected:
            with self.subTest(label=label):
                self.assertIsNone(h.sticker_guess_result(value), label)

    def test_unknown_kinds_fall_back_to_other_and_camel_case_aliases_are_read(self):
        # `kind` 只做白名单归一，**不参与**收不收的判断。
        self.assertEqual(h.sticker_guess_result(
            {'is_sticker': True, 'kind': 'Meme!!!', 'confidence': 0.7},
        )['kind'], 'other')
        self.assertEqual(h.sticker_guess_result(
            {'is_sticker': True, 'kind': 'caption_photo', 'confidence': 0.7},
        )['kind'], 'caption_photo')
        # 中转站改写了键名也认（本仓库读外部输入一律双读）。
        accepted = h.sticker_guess_result({'isSticker': True, 'confidence': 0.7})
        self.assertEqual(accepted['description'], '', '没给描述就是空串（调用方据此走描述流程）')

    def test_the_heuristic_constants_are_the_documented_ones(self):
        self.assertEqual(h.GUESS_STICKER_KIND, 'image')
        self.assertEqual(h.GUESS_STICKER_MAX_DIMENSION, 512)
        self.assertEqual(h.GUESS_STICKER_MAX_ASPECT, 1.6)
        self.assertEqual(h.GUESS_STICKER_MIN_CONFIDENCE, 0.6)
        self.assertEqual(h.STICKER_GUESS_KINDS, (
            'meme', 'reaction', 'caption_photo', 'photo', 'screenshot', 'other',
        ))


def png(
    width: int, height: int, color_type: int = 2, trns: bool = False,
) -> bytes:
    """一张结构合法的 PNG（宽高真的写在 `IHDR` 里）。"""
    ihdr = (
        width.to_bytes(4, 'big') + height.to_bytes(4, 'big') + bytes([8, color_type, 0, 0, 0])
    )
    body = _chunk(b'IHDR', ihdr)
    if trns:
        body += _chunk(b'tRNS', b'\x00' * 6)
    body += _chunk(b'IDAT', b'') + _chunk(b'IEND', b'')
    return b'\x89PNG\r\n\x1a\n' + body


def _chunk(name: bytes, data: bytes) -> bytes:
    return len(data).to_bytes(4, 'big') + name + data + zlib.crc32(name + data).to_bytes(4, 'big')


def gif(width: int, height: int) -> bytes:
    """一张 GIF：逻辑屏幕描述符里带宽高（小端 16 位）。"""
    return b'GIF89a' + width.to_bytes(2, 'little') + height.to_bytes(2, 'little') + b'\x00' * 8


def jpeg(width: int, height: int) -> bytes:
    """一张 JPEG：`SOI` + `APP0` + `SOF0`（尺寸就在 SOF 里）。"""
    app0 = b'\xff\xe0' + (16).to_bytes(2, 'big') + b'JFIF\x00' + b'\x00' * 9
    sof = (
        b'\xff\xc0' + (17).to_bytes(2, 'big') + b'\x08'
        + height.to_bytes(2, 'big') + width.to_bytes(2, 'big') + b'\x03' + b'\x00' * 9
    )
    return b'\xff\xd8' + app0 + sof + b'\xff\xd9'


def _webp(chunk: bytes) -> bytes:
    body = b'WEBP' + chunk
    return b'RIFF' + len(body).to_bytes(4, 'little') + body


def webp_vp8x(width: int, height: int) -> bytes:
    """扩展格式：画布尺寸是 24 位小端的「宽-1 / 高-1」。"""
    payload = b'\x00' * 4 + (width - 1).to_bytes(3, 'little') + (height - 1).to_bytes(3, 'little')
    return _webp(b'VP8X' + len(payload).to_bytes(4, 'little') + payload)


def webp_vp8(width: int, height: int) -> bytes:
    """有损格式：帧头起始码之后的两个 16 位（有效 14 位）。"""
    payload = (
        b'\x00\x00\x00' + b'\x9d\x01\x2a'
        + width.to_bytes(2, 'little') + height.to_bytes(2, 'little') + b'\x00' * 4
    )
    return _webp(b'VP8 ' + len(payload).to_bytes(4, 'little') + payload)


def webp_vp8l(width: int, height: int) -> bytes:
    """无损格式：签名 0x2F + 位打包的（宽-1, 高-1）各 14 位。"""
    bits = (width - 1) | ((height - 1) << 14)
    payload = b'\x2f' + bits.to_bytes(4, 'little')
    return _webp(b'VP8L' + len(payload).to_bytes(4, 'little') + payload)


class GroupIdentityTests(unittest.TestCase):
    """`upstream/test/group-identity.test.ts` 的纯函数用例。"""

    def test_group_speaker_labels_retain_both_display_name_and_stable_qq_identity(self):
        self.assertEqual(h.format_group_speaker('渔社', '2171322646'), '群成员「渔社」（QQ：2171322646）')
        self.assertEqual(h.format_group_speaker('', '2171322646'), '群成员（QQ：2171322646）')

    def test_chat_action_validation_accepts_only_advertised_actions_and_supplied_references(self):
        context = {
            'groupId': '100', 'channelId': 'group:100', 'label': '测试群', 'purpose': '', 'characterRole': '',
            'messages': [{
                'senderId': '200', 'senderName': '成员', 'speaker': '群成员「成员」（QQ：200）',
                'messageRef': 'msg-7', 'messageId': '-12345', 'content': '这条消息可以被操作',
                'occurredAt': _dt('2026-08-28T00:00:00.000Z'), 'direction': 'user',
            }],
        }
        capabilities = {'platform': 'qq', 'quoteReply': True, 'reactions': ['like', 'heart']}
        valid = {
            'groupReply': {'mode': 'immediate', 'content': '指定回复', 'replyTo': 'msg-7'},
            'messageReactions': [{'messageRef': 'msg-7', 'reaction': 'heart'}],
        }
        self.assertEqual(h.normalize_group_chat_actions(valid, capabilities, context), {
            'replyTo': {'messageRef': 'msg-7', 'messageId': '-12345'},
            'reactions': [{'messageRef': 'msg-7', 'messageId': '-12345', 'reaction': 'heart'}],
        })
        invalid = h.normalize_group_chat_actions({
            'groupReply': {'mode': 'immediate', 'content': '越界回复', 'replyTo': 'msg-999'},
            'messageReactions': [{'messageRef': 'msg-7', 'reaction': 'angry'}],
        }, capabilities, context)
        self.assertEqual(invalid, {'reactions': []})
        self.assertEqual(h.normalize_group_chat_actions(valid, None, context), {'reactions': []})

    def test_reaction_allowlist_is_semantic_deduplicated_and_bounded(self):
        self.assertEqual(h.normalize_allowed_reactions(['like', 'like', 'unknown', 'heart']), ['like', 'heart'])

    def test_native_face_threshold_is_calibrated_against_reply_meaning(self):
        self.assertLess(h.calibrated_native_face_willingness('sweat', 1, '你还好意思问咋了'), 0.95)
        self.assertLess(h.calibrated_native_face_willingness('laugh', 1, '哈哈哈你也太离谱了'), 0.95)
        self.assertGreaterEqual(h.calibrated_native_face_willingness('laugh', 1, '哈哈哈你也太离谱了'), 0.7)
        # 没有可见文本对应物时一律 0。
        self.assertEqual(h.calibrated_native_face_willingness('heart', 1, ''), 0)
        self.assertEqual(h.calibrated_native_face_willingness('heart', 1, '<sep/>'), 0)

    def test_quoted_messages_retain_author_ownership_and_bounded_readable_content(self):
        self.assertEqual(h.normalize_quoted_message_content('看这个<image src="x"/><record src="y"/>'),
                         '看这个[图片][语音]')
        quote = h.describe_quoted_message({
            'selfId': '100',
            'quote': {'user': {'id': '100', 'name': '机器人旧名'}, 'content': '这是主角之前说的话'},
        }, 'Yukiyo')
        self.assertEqual(quote, {
            'senderId': '100', 'senderName': 'Yukiyo', 'speaker': '主角「Yukiyo」', 'content': '这是主角之前说的话',
        })

    def test_quoted_message_from_another_member_uses_display_name_and_id(self):
        quote = h.describe_quoted_message({
            'selfId': '100',
            'quote': {'user': {'id': '200'}, 'member': {'nick': '小桃'}, 'content': '在吗'},
        })
        self.assertEqual(quote, {
            'senderId': '200', 'senderName': '小桃', 'speaker': '消息发送者「小桃」（ID：200）', 'content': '在吗',
        })
        self.assertIsNone(h.describe_quoted_message({'selfId': '100'}))


class VoiceAndAttachmentTests(unittest.TestCase):
    """`upstream/test/voice-transcription.test.ts` 的纯函数用例。"""

    def test_onebot_record_cq_segments_are_recognized_as_incoming_voice(self):
        self.assertEqual(h.extract_session_voice_count({'content': '[CQ:record,file=voice.amr]'}), 1)
        self.assertEqual(h.extract_session_voice_count({'content': '普通文字'}), 0)

    def test_voice_records_resolve_to_onebot_file_tokens_for_the_native_audio_channel(self):
        self.assertEqual(
            h.extract_session_audio_sources({'content': '[CQ:record,file=ABC.silk,url=https://example.com/v.silk]'}),
            ['onebot-file:ABC.silk'])
        self.assertEqual(
            h.extract_session_audio_sources({'content': '<audio file="xyz.amr"/>'}),
            ['onebot-file:xyz.amr'])
        # 裸 URL 无法在服务端转码，因此不进这个通道。
        self.assertEqual(
            h.extract_session_audio_sources({'content': '<record url="https://example.com/raw.silk"/>'}),
            [])
        # 内联的、模型可读容器里的适配器音频原样接受。
        self.assertEqual(
            h.extract_session_audio_sources({'content': '<audio src="data:audio/mp3;base64,AAAA"/>'}),
            ['data:audio/mp3;base64,AAAA'])
        self.assertEqual(h.extract_session_audio_sources({'content': '普通文字'}), [])

    def test_qq_audio_files_enter_the_native_audio_channel(self):
        session = {
            'content': '<file src="http://223.109.208.77:80/asn.com/qqdownloadftnv5?ver=2&rkey=abc" '
                       'file="Mirai Post - ISKL（original mix）.mp3" file-id="fid-1" file-size="3492875" '
                       'name="Mirai Post - ISKL（original mix）.mp3" size="3492875" id="fid-1"/>',
        }
        sources = h.extract_session_audio_sources(session)
        self.assertEqual(len(sources), 1)
        self.assertRegex(sources[0], r'^file-url:http://223\.109\.208\.77:80/asn\.com/qqdownloadftnv5\?ver=2&rkey=abc#')
        self.assertTrue(sources[0].endswith(':3492875'), '体积随源携带')
        self.assertIn(quote('Mirai Post - ISKL（original mix）.mp3', safe=''), sources[0], '文件名随源携带')
        self.assertEqual(
            h.extract_session_audio_sources(
                {'content': '<file src="https://cdn.example.com/dl?k=1" name="报告.pdf" size="1200"/>'}),
            [])

    def test_extract_session_file_facts_reports_names_sizes_and_audio_flags_without_url_soup(self):
        facts = h.extract_session_file_facts({
            'content': '听听<file src="https://cdn.example.com/dl?k=1" name="demo.mp3" size="4096"/>',
        })
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]['name'], 'demo.mp3')
        self.assertEqual(facts[0]['audio'], True)
        self.assertEqual(facts[0]['size'], 4096)
        plain = h.extract_session_file_facts(
            {'content': '<file src="https://cdn.example.com/dl?k=2" name="笔记.zip" size="99"/>'})
        self.assertEqual(plain[0]['audio'], False)
        fallback = h.extract_session_file_facts(
            {'content': '<file src="https://x/y.mp3" name="y.mp3" size="1"/>'})
        self.assertGreaterEqual(len(fallback), 1)
        self.assertEqual(h.extract_session_file_facts({'content': '普通文字'}), [])

    def test_guess_audio_format_sniffs_containers_from_magic_bytes_and_filename_hints(self):
        self.assertEqual(h.guess_audio_format(b'', 'song.MP3'), 'mp3')
        self.assertEqual(h.guess_audio_format(bytes([0x49, 0x44, 0x33, 4, 0, 0, 0, 0]), 'noext'), 'mp3')
        self.assertEqual(h.guess_audio_format(bytes([0xFF, 0xFB, 0x90, 0x00]), 'noext'), 'mp3')
        self.assertEqual(h.guess_audio_format(b'RIFF' + bytes(4) + b'WAVE', 'noext'), 'wav')
        self.assertEqual(h.guess_audio_format(b'OggS\x00\x02', 'noext'), 'ogg')
        self.assertEqual(h.guess_audio_format(bytes(4) + b'ftypM4A ', 'noext'), 'm4a')
        self.assertEqual(h.guess_audio_format(b'fLaC', 'noext'), 'flac')
        self.assertEqual(h.guess_audio_format(b'#!AMR\n', 'noext'), 'amr')
        self.assertEqual(h.guess_audio_format(b'????'), '')

    def test_group_inbound_attachments_become_fact_placeholders_instead_of_url_soup(self):
        self.assertEqual(
            h.describe_group_attachments(
                '看看这个<img src="https:// multimedia.qq.com.cn/download?appid=1400&rkey=SECRET"/>和'
                '<file src="http://223.109.208.77/asn.com/qqdownloadftnv5?rkey=xyz" name="demo.mp3" size="4096"/>'),
            '看看这个[图片]和[文件：demo.mp3]')
        self.assertEqual(h.describe_group_attachments('听这个[CQ:record,file=abc.silk,url=https://x/y.silk]'),
                         '听这个[语音]')
        self.assertEqual(h.describe_group_attachments('正常文字和[微笑]表情'), '正常文字和[微笑]表情')

    def test_config_typed_dicts_use_snake_case_field_names(self):
        # 配置层裁决：配置一律 snake_case（AstrBot `_conf_schema.json` 的落盘形状）。
        from plugin.core.service import config as c
        self.assertIn('health_report_minutes', c.BlindModeConfig.__annotations__)
        self.assertIn('character_role', c.GroupChatRule.__annotations__)
        self.assertIn('context_time_window_minutes', c.RuntimeConfig.__annotations__)
        self.assertIn('story_defaults', c.Config.__annotations__)
        self.assertIn('chat_actions', c.Config.__annotations__)
        self.assertIn('schedule_preplan', c.Config.__annotations__)
        self.assertNotIn('healthReportMinutes', c.BlindModeConfig.__annotations__)

    def test_desktop_timeline_projection_keeps_the_protocol_key_names(self):
        from plugin.core.service import config as c
        entry = {
            'id': 7, 'storyId': 'story', 'participantId': 'p', 'kind': 'script', 'actor': 'narrator',
            'content': 'x', 'occurredAt': _dt('2026-08-23T08:00:00.000Z'),
            'metadata': {'commitId': 'c1', 'timelineWindow': {'from': '2026-08-23T07:00:00.000Z'}},
        }
        view = c.desktop_timeline_entry_view(entry)
        self.assertEqual(view['entityId'], 'entry:7')
        self.assertEqual(view['track'], 'script')
        self.assertEqual(view['occurredAt'], '2026-08-23T08:00:00.000Z')
        self.assertEqual(view['startedAt'], '2026-08-23T07:00:00.000Z')
        self.assertIsNone(view['endedAt'])
        self.assertEqual(view['commitId'], 'c1')

        # 未来时钟引用：桌面视口上限 14 天，倒置自动交换。
        normalized = c.normalize_desktop_timeline_range_request({
            'from': '2026-09-10T00:00:00.000Z', 'to': '2026-08-01T00:00:00.000Z', 'limit': 0,
            'tracks': ['script', 'bogus'], 'cursor': 'entry:12',
        })
        self.assertEqual(normalized['from'].isoformat(), '2026-08-01T00:00:00+00:00')
        self.assertEqual(normalized['limit'], 240)
        self.assertEqual(normalized['tracks'], ['script'])
        self.assertEqual(normalized['cursorId'], 12)
        self.assertEqual(normalized['detailLevel'], 'summary')
        self.assertEqual(c.DESKTOP_TIMELINE_TRACKS, ['script', 'messages', 'system', 'scenes', 'facts', 'preplan'])


class LexicalScoreTests(unittest.TestCase):
    """`historyLexicalScore` / `shouldRequestTurnEmbedding` 的边界行为。"""

    def test_lexical_score_rewards_exact_phrase_and_chinese_bigrams(self):
        self.assertEqual(h.history_lexical_score('', '任何内容'), 0)
        self.assertGreater(h.history_lexical_score('万松园', '她去万松园了'), 0)
        self.assertEqual(h.history_lexical_score('完全无关的词', '她去万松园了'), 0)
        self.assertLessEqual(h.history_lexical_score('万松园校门口', '她去万松园校门口了'), 1)

    def test_should_request_turn_embedding_requires_a_feature_that_uses_it(self):
        from plugin.core.service import config as c
        embedding = {'enabled': True, 'liveQuery': False, 'semanticHistory': False,
                     'semanticStickerFilter': True}
        self.assertFalse(c.should_request_turn_embedding(embedding, True, 12))
        self.assertTrue(c.should_request_turn_embedding(embedding, True, 13))
        self.assertFalse(c.should_request_turn_embedding(embedding, False, 99))
        self.assertTrue(c.should_request_turn_embedding(
            {'enabled': True, 'liveQuery': True}, False, 0))
        self.assertFalse(c.should_request_turn_embedding({'enabled': False}, True, 99))
        self.assertFalse(c.should_request_turn_embedding(None, True, 99))



class RecallFusionTests(unittest.TestCase):
    """v1.4.0 召回融合：本地查询改写 + 两路排名融合（`docs/MEMORY_MAINTENANCE.md` §5.2）。"""

    CONFIG = {
        'semanticWeight': 1.0, 'factImportanceWeight': 0.35, 'factConfidenceWeight': 0.2,
        'factRecencyWeight': 0.2, 'unresolvedWeight': 0.2,
    }

    def _fact(self, fact_id, content, importance=0.5, embedding=None, scope='world'):
        return {
            'id': fact_id, 'content': content, 'importance': importance, 'confidence': 0.5,
            'scope': scope, 'unresolved': False, 'embedding': embedding or [],
            # 用"现在"当 lastSeenAt，让新近项确定性地取满分（评分用的是真实时钟）。
            'lastSeenAt': h.iso(h.utc_now()),
        }

    def test_rewrite_strips_scaffolding_and_question_particles(self):
        cases = {
            '你还记得我上次说的那个面包店吗': '我上次说的面包店',
            '你记不记得那本书呢': '那本书',
            '我上次跟你说的那家店': '那家店',
            '今天天气怎么样': '今天天气样',
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(h.rewrite_recall_query(text), expected)

    def test_rewrite_keeps_short_or_unusable_queries(self):
        for text in ('猫', '冰美式', '', '   '):
            with self.subTest(text=text):
                self.assertEqual(h.rewrite_recall_query(text), text.strip())

    def test_ranks_share_ties(self):
        self.assertEqual(h._ranks([1.0, 0.5, 0.5, 0.0]), [1, 2, 2, 4])
        self.assertEqual(h._ranks([]), [])

    def test_an_irrelevant_fact_earns_no_lane_credit(self):
        facts = [self._fact(1, '主人喜欢喝冰美式', 0.3, [1.0, 0.0]),
                 self._fact(2, '她养了一只叫团子的猫', 0.95, [0.0, 1.0])]
        lanes = h.fact_lane_scores(facts, self.CONFIG, [1.0, 0.0], '你还记得冰美式吗')
        self.assertAlmostEqual(lanes[0]['lexicalRrf'], 1.0, places=6)
        self.assertAlmostEqual(lanes[0]['semanticRrf'], 1.0, places=6)
        self.assertEqual(lanes[1]['lexicalRrf'], 0.0, '零重合不该靠"排最后一名"白拿分')
        self.assertEqual(lanes[1]['semanticRrf'], 0.0)
        scores = [h.fact_hybrid_score(fact, self.CONFIG, lane) for fact, lane in zip(facts, lanes)]
        self.assertGreater(scores[0], scores[1], '重要度不该压过两路都命中的事实')

    def test_rank_multiplier_rewards_the_better_ranked_lane(self):
        facts = [self._fact(1, '面包店周一不开门'), self._fact(2, '面包店里的猫叫团子')]
        lanes = h.fact_lane_scores(facts, self.CONFIG, [], '面包店周一')
        self.assertEqual(lanes[0]['lexicalRank'], 1)
        self.assertGreater(lanes[0]['lexicalRrf'], lanes[1]['lexicalRrf'])

    def test_absent_lane_contributes_nothing(self):
        facts = [self._fact(1, '主人喜欢喝冰美式')]
        lanes = h.fact_lane_scores(facts, self.CONFIG, [], '')
        self.assertIsNone(lanes[0]['lexicalRank'])
        self.assertIsNone(lanes[0]['semanticRank'])
        self.assertEqual(lanes[0]['lexicalRrf'], 0.0)
        self.assertEqual(lanes[0]['semanticRrf'], 0.0)
        # 没有查询可用时，排序完全由结构分决定（与上游一致）。
        self.assertAlmostEqual(
            h.fact_hybrid_score(facts[0], self.CONFIG, lanes[0]),
            h.fact_structural_score(facts[0], self.CONFIG), places=9,
        )

    def test_rewritten_query_only_helps(self):
        content = '那家面包店周一不开门'
        plain = h.history_lexical_score('你还记得我上次说的那个面包店吗', content)
        lanes = h.fact_lane_scores([self._fact(1, content)], self.CONFIG, [], '你还记得我上次说的那个面包店吗')
        self.assertGreater(lanes[0]['lexicalScore'], plain)
        self.assertGreaterEqual(lanes[0]['lexical'], 0.0)
        with_rewrite = h.fact_lane_scores([self._fact(1, content)], self.CONFIG, [],
                                          '你还记得我上次说的那个面包店吗', rewrite=False)
        self.assertEqual(with_rewrite[0]['lexicalScore'], with_rewrite[0]['lexical'])

    def test_structural_score_matches_upstream_when_no_relevance(self):
        fact = self._fact(1, '任意内容', importance=0.8)
        fact['confidence'] = 0.6
        expected = (
            0.8 * 0.35 + 0.6 * 0.2 + 1.0 * 0.2
        )
        self.assertAlmostEqual(h.fact_structural_score(fact, self.CONFIG), expected, places=9)

    def test_an_open_promise_keeps_its_structural_bonus(self):
        promise = self._fact(1, '答应回电话', importance=0.1, scope='promise')
        promise['unresolved'] = True
        plain = self._fact(2, '答应回电话', importance=0.1, scope='promise')
        self.assertGreater(
            h.fact_structural_score(promise, self.CONFIG),
            h.fact_structural_score(plain, self.CONFIG),
        )


class ImageHashTests(unittest.TestCase):
    """图片感知哈希与去重（v1.4.0，`docs/MEMORY_MAINTENANCE.md` §5.5）。"""

    @staticmethod
    def _png(kind: str) -> bytes:
        import io as _io

        from PIL import Image, ImageDraw

        image = Image.new('L', (64, 64), 0)
        draw = ImageDraw.Draw(image)
        if kind == 'bar':
            draw.rectangle([4, 4, 30, 60], fill=255)
        elif kind == 'circle':
            draw.ellipse([10, 10, 54, 54], fill=255)
        elif kind == 'stripes':
            for x in range(0, 64, 8):
                draw.rectangle([x, 0, x + 3, 63], fill=255)
        buffer = _io.BytesIO()
        image.save(buffer, 'PNG')
        return buffer.getvalue()

    def _require_pillow(self) -> None:
        try:
            import PIL  # noqa: F401
        except Exception:  # pragma: no cover - 环境没装
            self.skipTest('未安装 Pillow')

    def test_flat_images_have_no_usable_hash(self):
        self._require_pillow()
        import io as _io

        from PIL import Image

        buffer = _io.BytesIO()
        Image.new('L', (32, 32), 7).save(buffer, 'PNG')
        self.assertEqual(h.image_perceptual_hash(buffer.getvalue()), '',
                         '纯色图没有结构可比，不能参与去重')
        self.assertEqual(h.image_perceptual_hash(b'nope'), '')

    def test_different_images_hash_far_apart(self):
        self._require_pillow()
        bar = h.image_perceptual_hash(self._png('bar'))
        circle = h.image_perceptual_hash(self._png('circle'))
        self.assertTrue(bar and circle)
        self.assertGreater(h.hamming_distance(bar, circle), h.IMAGE_HASH_TOLERANCE)

    def test_the_same_image_hashes_identically_even_after_recompression(self):
        self._require_pillow()
        import io as _io

        from PIL import Image

        original = self._png('stripes')
        buffer = _io.BytesIO()
        Image.open(_io.BytesIO(original)).convert('RGB').save(buffer, 'JPEG', quality=60)
        first = h.image_perceptual_hash(original)
        again = h.image_perceptual_hash(buffer.getvalue())
        self.assertTrue(first)
        self.assertLessEqual(h.hamming_distance(first, again), h.IMAGE_HASH_TOLERANCE)

    def test_hamming_distance_rejects_malformed_input(self):
        self.assertEqual(h.hamming_distance('', 'ff'), 64)
        self.assertEqual(h.hamming_distance('ff', 'fff'), 64)
        self.assertEqual(h.hamming_distance('zz', 'ff'), 64)
        self.assertEqual(h.hamming_distance('ff', 'ff'), 0)

    def test_split_and_remember_track_recent_hashes(self):
        images = [{'id': 1, 'perceptualHash': 'ffffffffffffffff'},
                  {'id': 2, 'perceptualHash': '0000000000000000'}]
        fresh, skipped = h.split_described_images(images, ['ffffffffffffffff'])
        self.assertEqual([item['id'] for item in fresh], [2])
        self.assertEqual(skipped, ['ffffffffffffffff'])
        merged = h.remember_described_hashes(['ffffffffffffffff'], images, limit=2)
        self.assertEqual(len(merged), 2)
        capped = h.remember_described_hashes([], [{'perceptualHash': '%016x' % index} for index in range(5)], limit=3)
        self.assertEqual(len(capped), 3)

    def test_images_without_a_hash_are_never_deduplicated(self):
        images = [{'id': 1}, {'id': 2, 'perceptualHash': ''}]
        fresh, skipped = h.split_described_images(images, ['ffffffffffffffff'])
        self.assertEqual([item['id'] for item in fresh], [1, 2])
        self.assertEqual(skipped, [])


class QuoteBackfillTests(unittest.TestCase):
    """引用回填（v1.4.0，`docs/MEMORY_MAINTENANCE.md` §5.5）。"""

    ENTRIES = [
        {'id': 7, 'content': '我把伞放门口了', 'metadata': {}},
        {'id': 9, 'content': '群里说的那件事', 'metadata': {'messageId': 'abc-1'}},
        {'id': 11, 'content': '空的元数据', 'metadata': None},
    ]

    def test_synthetic_ref_resolves_to_our_own_entry(self):
        self.assertEqual(h.backfilled_quote_content({'messageId': 'msg-7'}, self.ENTRIES), '我把伞放门口了')

    def test_platform_id_resolves_through_entry_metadata(self):
        self.assertEqual(h.backfilled_quote_content({'id': 'abc-1'}, self.ENTRIES), '群里说的那件事')

    def test_unknown_ids_and_garbage_stay_empty(self):
        self.assertEqual(h.backfilled_quote_content({'messageId': 'nope'}, self.ENTRIES), '')
        self.assertEqual(h.backfilled_quote_content({'messageId': 'msg-404'}, self.ENTRIES), '')
        self.assertEqual(h.backfilled_quote_content(None, self.ENTRIES), '')
        self.assertEqual(h.backfilled_quote_content({}, self.ENTRIES), '')
        self.assertEqual(h.backfilled_quote_content({'messageId': 'msg-7'}, []), '')

    def test_existing_content_is_never_overwritten(self):
        quote = {'messageId': 'msg-7', 'content': '平台给的原文'}
        self.assertEqual(h.backfilled_quote_content(quote, self.ENTRIES), '')

    def test_a_malformed_entry_does_not_break_the_lookup(self):
        self.assertEqual(h.backfilled_quote_content({'id': 'abc-1'}, [None, 'x', *self.ENTRIES]),
                         '群里说的那件事')


class MediaLabelTests(unittest.TestCase):
    """入站媒体标记 → 语义标签（受控偏离 §29）。

    用户报的现象：她分不清 QQ 表情、表情包图片、实拍照片、网图——因为适配器把
    `sub_type` / `summary` 丢在解析层，提示词里只剩一个 `[图片]`。
    """

    def test_plain_image_stays_the_upstream_placeholder(self):
        self.assertEqual(h.describe_image_media('src="https://example.com/a.jpg"'), '[图片]')

    def test_sticker_and_animated_are_distinguished(self):
        self.assertEqual(h.describe_image_media('src="https://x/a.png" kind="sticker"'), '[表情包]')
        self.assertEqual(
            h.describe_image_media('src="https://x/a.png" kind="sticker" summary="[动画表情]"'),
            '[动画表情]',
        )
        self.assertEqual(h.describe_image_media('kind="animated"'), '[动画表情]')

    def test_market_and_card_labels(self):
        self.assertEqual(h.describe_image_media('kind="market"'), '[QQ 商城表情]')
        self.assertEqual(
            h.describe_card_media('app="com.tencent.miniapp_01" title="QQ经典农场"'),
            '[QQ小程序：QQ经典农场]',
        )
        self.assertEqual(h.describe_card_media('app="com.tencent.tuwen" title="这条新闻"'), '[分享卡片：这条新闻]')
        self.assertEqual(h.describe_card_media(''), '[分享卡片]')

    def test_normalize_media_segments_keeps_kinds_and_names_faces(self):
        text = h.normalize_media_segments(
            '给你看<img src="https://x/a.png" kind="sticker"/>'
            '<face id="277"/><card app="com.tencent.miniapp" title="宝箱"/>',
        )
        self.assertIn('[表情包]', text)
        self.assertIn('[QQ 原生表情：汪汪（ID: 277）]', text)
        self.assertIn('[QQ小程序：宝箱]', text)

    def test_quoted_message_content_carries_the_kind(self):
        quoted = h.normalize_quoted_message_content(
            '看这个<img src="https://x/a.png" kind="sticker" summary="[动画表情]"/>',
        )
        self.assertIn('[动画表情]', quoted)
        self.assertNotIn('<img', quoted)

    def test_group_attachments_carries_the_kind(self):
        text = h.describe_group_attachments(
            '<img src="https://x/a.png" kind="sticker"/><record file="v.silk"/>',
        )
        self.assertIn('[表情包]', text)
        self.assertIn('[语音]', text)


class ContextMetricsTests(unittest.TestCase):
    """上轮上下文构成（v1.4.0，`docs/MEMORY_MAINTENANCE.md` §5.4）。"""

    def test_estimate_tokens_counts_cjk_per_character(self):
        self.assertEqual(h.estimate_tokens(''), 0)
        self.assertEqual(h.estimate_tokens('她今天去了面包店'), 8)
        self.assertLess(h.estimate_tokens('hello world'), 9)

    def test_metrics_measure_every_section_that_is_present(self):
        request = {
            'phase': 'user-message',
            'recentEntries': [{'id': 1, 'content': '你好'}],
            'facts': [{'id': 2}, {'id': 3}],
            'followUpCommitments': [],
            'sceneContext': {'scene': {'summary': '在厨房里'}},
        }
        metrics = h.context_metrics(request, 12.6, 'user-message', 'p1')
        self.assertEqual(metrics['assembly_ms'], 12)
        self.assertEqual(metrics['phase'], 'user-message')
        self.assertEqual(metrics['participant_id'], 'p1')
        self.assertEqual(metrics['sections']['recentEntries']['items'], 1)
        self.assertEqual(metrics['sections']['facts']['items'], 2)
        self.assertNotIn('followUpCommitments', metrics['sections'], '空段不必占一行')
        self.assertEqual(metrics['items'], 3)
        self.assertGreater(metrics['characters'], 0)
        self.assertGreater(metrics['payload_characters'], metrics['characters'])
        self.assertGreater(metrics['estimated_tokens'], 0)

    def test_metrics_never_drop_the_scene_characters(self):
        without = h.context_metrics({'phase': 'advance'}, 0, 'advance')
        with_scene = h.context_metrics(
            {'phase': 'advance', 'sceneContext': {'scene': {'summary': '厨房'}}}, 0, 'advance',
        )
        self.assertGreater(with_scene['characters'], without['characters'])

    def test_metrics_survive_a_non_serializable_request(self):
        class _Opaque:
            def __repr__(self) -> str:
                return '<opaque>'

        metrics = h.context_metrics({'phase': 'advance', 'facts': [{'id': 1, 'x': _Opaque()}]}, 0, 'advance')
        self.assertEqual(metrics['sections']['facts']['items'], 1)
        self.assertGreater(metrics['payload_characters'], 0)

    def test_metrics_accept_a_timestamp(self):
        from datetime import datetime, timezone
        moment = datetime(2026, 9, 27, 3, 0, tzinfo=timezone.utc)
        self.assertTrue(h.context_metrics({}, 0, '', '', moment)['at'].startswith('2026-09-27'))


class NormalizeConfigTests(unittest.TestCase):
    """`normalize_config`：camelCase → snake_case 归一 + 默认值补全。"""

    def test_signature_and_non_dict_input(self):
        from plugin.core.service import config as c
        # 签名：normalize_config(raw: Any) -> dict[str, Any]
        for raw in (None, 'x', 42, ['a']):
            normalized = c.normalize_config(raw)
            self.assertIsInstance(normalized, dict)
            self.assertEqual(normalized['runtime']['max_message_characters'], 2_000)

    def test_upstream_camel_case_keys_are_normalized(self):
        from plugin.core.service import config as c
        normalized = c.normalize_config({
            'storyDefaults': {'characterName': 'Yukiyo', 'timezone': 'Asia/Tokyo'},
            'blindMode': {'enabled': True, 'healthReportMinutes': 2},
            'runtime': {'maxMessageCharacters': 300, 'autoAdvanceIntervalMinutes': 20},
            'sharedStory': {'maxCrossConversationActions': 3, 'participantContextLimit': 9},
        })
        self.assertEqual(normalized['story_defaults']['character_name'], 'Yukiyo')
        self.assertEqual(normalized['story_defaults']['timezone'], 'Asia/Tokyo')
        self.assertEqual(normalized['blind_mode'], {'enabled': True, 'health_report_minutes': 2})
        self.assertEqual(normalized['runtime']['max_message_characters'], 300)
        self.assertEqual(normalized['runtime']['auto_advance_interval_minutes'], 20)
        self.assertEqual(normalized['shared_story']['max_cross_conversation_actions'], 3)
        self.assertEqual(normalized['shared_story']['participant_context_limit'], 9)
        self.assertNotIn('storyDefaults', normalized)
        self.assertNotIn('blindMode', normalized)

    def test_missing_groups_are_filled_from_upstream_console_defaults(self):
        from plugin.core.service import config as c
        normalized = c.normalize_config({})
        # 顶层分组全部补齐（上游 src/index.ts 的 Console 分组）。
        for group in ('story_defaults', 'model', 'onebot', 'shared_story', 'runtime', 'urge',
                      'schedule_preplan', 'timeline_director', 'agency', 'chat_actions',
                      'stickers', 'memory', 'alter_system', 'browser', 'blind_mode', 'logging'):
            self.assertIn(group, normalized, group)
        # 逐字核对若干有上游断言的默认值。
        self.assertEqual(normalized['blind_mode'], {'enabled': False, 'health_report_minutes': 10})
        self.assertEqual(normalized['chat_actions']['enabled'], False)
        self.assertEqual(normalized['chat_actions']['platforms'], ['qq'])
        self.assertEqual(normalized['chat_actions']['expression_threshold'], 0.7)
        self.assertEqual(normalized['chat_actions']['allowed_reactions'], ['like', 'smile', 'laugh', 'heart'])
        self.assertEqual(normalized['stickers']['directory'], 'data/hds-interlude/stickers')
        self.assertEqual(normalized['stickers']['catalog_limit'], 40)
        self.assertEqual(normalized['stickers']['description_response_format'], 'json-object')
        self.assertEqual(normalized['logging']['format'], 'layered')
        self.assertEqual(normalized['logging']['colors'], True)
        self.assertEqual(normalized['logging']['color_theme'], 'dark')
        self.assertEqual(normalized['logging']['kaomoji'], True)
        self.assertEqual(normalized['runtime']['context_entry_limit'], 50)
        self.assertEqual(normalized['runtime']['context_time_window_minutes'], 60)
        self.assertEqual(normalized['runtime']['user_message_debounce_seconds'], 2)
        self.assertEqual(normalized['memory']['scene_entry_threshold'], 16)
        self.assertEqual(normalized['memory']['scene_character_threshold'], 10_000)
        self.assertEqual(normalized['schedule_preplan']['horizon_days'], 14)
        self.assertEqual(normalized['schedule_preplan']['variation_level'], 'stable')
        self.assertEqual(normalized['agency']['max_window_minutes'], 240)
        self.assertEqual(normalized['model']['main_response_format'], 'json-object')
        self.assertEqual(normalized['model']['main_streaming_mode'], 'off')
        self.assertEqual(normalized['model']['vision']['mode'], 'native')
        self.assertEqual(normalized['model']['vision']['detail'], 'auto')
        self.assertEqual(normalized['model']['audio']['out_format'], 'mp3')
        self.assertEqual(normalized['model']['audio']['max_file_size_mb'], 10)
        self.assertEqual(normalized['model']['audio']['max_per_message'], 1)
        self.assertEqual(normalized['story_defaults']['perspective'], '')

    def test_lists_of_rows_are_normalized_and_unknown_groups_are_preserved(self):
        from plugin.core.service import config as c
        normalized = c.normalize_config({
            'onebot': {'groupChats': [{'groupId': '100', 'characterRole': '同学',
                                       'responseMode': 'always', 'contextLimit': 20}]},
            'someFutureGroup': {'someNewKnob': 1},
        })
        chat = normalized['onebot']['group_chats'][0]
        self.assertEqual(chat['group_id'], '100')
        self.assertEqual(chat['character_role'], '同学')
        self.assertEqual(chat['response_mode'], 'always')
        # 未知分组：键名照常归一，值原样保留（不静默丢弃用户配置）。
        self.assertEqual(normalized['some_future_group'], {'some_new_knob': 1})

    def test_astrbot_provider_keys_and_opaque_model_json_are_left_alone(self):
        from plugin.core.service import config as c
        normalized = c.normalize_config({
            'model': {
                'providers': [{
                    'api_key': 'sk-x', 'api_base': 'https://example.test/v1', 'provider_type': 'openai',
                    'custom_extra_body': {'temperature': 0.5, 'topP': 1},
                    'reasoningEffort': 'high',
                }],
                'custom_extra_body': {'temperature': 0.5},
            },
        })
        provider = normalized['model']['providers'][0]
        # AstrBot provider 字段本来就是 snake_case：原样保留，适配层才读得到。
        self.assertEqual(provider['api_key'], 'sk-x')
        self.assertEqual(provider['api_base'], 'https://example.test/v1')
        self.assertEqual(provider['provider_type'], 'openai')
        # custom_extra_body 的内容是模型 JSON 参数：整块不做递归改名。
        self.assertEqual(provider['custom_extra_body'], {'temperature': 0.5, 'topP': 1})
        self.assertEqual(normalized['model']['custom_extra_body'], {'temperature': 0.5})
        # 上游 Console 的 provider 字段仍然归一为 snake_case。
        self.assertEqual(provider['reasoning_effort'], 'high')

    def test_defaults_are_deep_copied_so_callers_cannot_pollute_the_global_table(self):
        from plugin.core.service import config as c
        first = c.normalize_config({})
        first['runtime']['rest_windows'][0]['start'] = '00:00'
        first['story_defaults']['character_name'] = 'mutated'
        second = c.normalize_config({})
        self.assertEqual(second['runtime']['rest_windows'][0]['start'], '23:00')
        self.assertEqual(second['story_defaults']['character_name'], 'Unnamed character')
        self.assertEqual(c.CONFIG_DEFAULTS['runtime']['rest_windows'][0]['start'], '23:00')

    def test_resolve_blind_mode_config_is_single_sourced(self):
        from plugin.core.service import config as c
        # 上游 configuration.test.ts 的两条断言。
        self.assertEqual(c.resolve_blind_mode_config(), {'enabled': False, 'health_report_minutes': 10})
        self.assertEqual(c.resolve_blind_mode_config({'enabled': True, 'healthReportMinutes': 2}),
                         {'enabled': True, 'health_report_minutes': 2})
        # helpers 里拿到的是同一个对象，不存在两份会漂移的实现。
        self.assertIs(h.resolve_blind_mode_config, c.resolve_blind_mode_config)
        self.assertIs(c.resolve_black_box_config, c.resolve_blind_mode_config)
        self.assertIs(h.resolve_black_box_config, c.resolve_blind_mode_config)


class PromptSectionTests(unittest.TestCase):
    """提示词四件套：权威分组 `prompts`，core 读 `model.*`（本移植版的分组搬家）。

    背景：上游把提示词放在 `model` 组（`src/index.ts` 的 `ModelConfig`），本移植版
    在配置页单开了一组 `prompts`。两边键名逐字相同，搬家由
    `resolve_prompt_fields`（读）与 `to_schema_shape`（写）负责。

    v1.1.0 的真实事故：schema 的 `model_center` 里也留了一份同名同默认值的副本，
    而 core 只读 `model_center` → 用户在「提示词」页写的东西**静默失效**。
    """

    def test_prompts_group_is_authoritative_when_it_is_not_the_default(self):
        from plugin.core.service import config as c
        normalized = c.normalize_config({
            'prompts': {'style_prompt': '用中文写，句子偏短。', 'fixed_prompt': '不许出戏。'},
            'model_center': {'main_temperature': 0.5},
        })
        self.assertEqual(normalized['model']['style_prompt'], '用中文写，句子偏短。')
        self.assertEqual(normalized['model']['fixed_prompt'], '不许出戏。')
        # 没写的键仍是上游默认值。
        self.assertEqual(normalized['model']['main_prompt'],
                         c.CONFIG_DEFAULTS['model']['main_prompt'])

    def test_prompts_group_value_equal_to_the_builtin_default_is_treated_as_untouched(self):
        """默认值不算"用户写了东西"——否则老配置里 `model_center` 的自定义会被顶掉。"""
        from plugin.core.service import config as c
        normalized = c.normalize_config({
            'prompts': {'style_prompt': c.CONFIG_DEFAULTS['model']['style_prompt']},
            'model_center': {'style_prompt': '老配置里真正生效的那份'},
        })
        self.assertEqual(normalized['model']['style_prompt'], '老配置里真正生效的那份')

    def test_legacy_model_center_prompts_survive_when_prompts_is_empty(self):
        from plugin.core.service import config as c
        normalized = c.normalize_config({
            'prompts': {'style_prompt': '', 'fixed_prompt': '   '},
            'model_center': {'style_prompt': '旧版文件里的文风', 'fixed_prompt': '旧版固定约束'},
        })
        self.assertEqual(normalized['model']['style_prompt'], '旧版文件里的文风')
        self.assertEqual(normalized['model']['fixed_prompt'], '旧版固定约束')

    def test_prompts_group_itself_is_preserved(self):
        from plugin.core.service import config as c
        normalized = c.normalize_config({'prompts': {'style_prompt': 'x', '未知键': 1}})
        self.assertEqual(normalized['prompts']['style_prompt'], 'x')
        self.assertEqual(normalized['prompts']['未知键'], 1)

    def test_to_schema_shape_moves_prompts_out_of_model_center(self):
        from plugin.core.service import config as c
        shaped = c.to_schema_shape(c.normalize_config({
            'prompts': {'style_prompt': '用中文写。'},
            'model_center': {'main_temperature': 0.5},
        }))
        for key in c.PROMPT_FIELD_KEYS:
            self.assertNotIn(key, shaped['model_center'], f'model_center.{key} 不该再有一份')
            self.assertIn(key, shaped['prompts'], f'prompts.{key} 缺失')
        self.assertEqual(shaped['prompts']['style_prompt'], '用中文写。')
        # 非提示词键照旧留在模型中心。
        self.assertEqual(shaped['model_center']['main_temperature'], 0.5)

    def test_legacy_file_prompts_are_rescued_into_the_prompts_group_on_write(self):
        """只写了 `model_center.style_prompt` 的旧文件，写盘后提示词落在 prompts 组。"""
        from plugin.core.service import config as c
        shaped = c.to_schema_shape(c.normalize_config({
            'model_center': {'style_prompt': '旧版文风'},
        }))
        self.assertEqual(shaped['prompts']['style_prompt'], '旧版文风')
        self.assertNotIn('style_prompt', shaped['model_center'])

    def test_schema_shape_is_idempotent_and_round_trips(self):
        from plugin.core.service import config as c
        raw = {'prompts': {'main_prompt': 'm', 'style_prompt': 's'}, 'runtime': {'auto_create': True}}
        first = c.to_schema_shape(c.normalize_config(raw))
        second = c.to_schema_shape(c.normalize_config(first))
        self.assertEqual(first['prompts'], second['prompts'])
        self.assertEqual(first['runtime'], second['runtime'])
        self.assertEqual(second['prompts']['main_prompt'], 'm')
        self.assertEqual(second['prompts']['style_prompt'], 's')
        # 写盘形状里 `prompts` 是唯一的提示词入口；再读回来 core 仍从 `model` 拿到。
        self.assertNotIn('style_prompt', second['model_center'])
        self.assertEqual(c.normalize_config(second)['model']['style_prompt'], 's')

    def test_to_schema_shape_without_a_model_section_does_not_invent_prompts(self):
        from plugin.core.service import config as c
        self.assertEqual(c.to_schema_shape({'runtime': {'auto_create': True}}),
                         {'runtime': {'auto_create': True}})
        self.assertEqual(c.to_schema_shape(None), {})


# --------------------------------------------------------------------------- #
# 两级表情选择 / 描述时定组（§48 的纯函数部分）
# --------------------------------------------------------------------------- #

class StickerGroupDirectoryTests(unittest.TestCase):
    """`stickerGroupDirectory` 的等价物：模型可见的**同一份**分组目录。"""

    def _assets(self):
        return [
            {'assetId': 'a-1', 'group': 'collected'},
            {'assetId': 'a-2', 'group': 'collected'},
            {'assetId': 'b-1', 'group': '猫猫'},
            {'assetId': 'c-1', 'group': 'legacy-dir'},
            {'assetId': 'd-1', 'group': ''},
        ]

    def _rows(self):
        # 描述表的行：**键就是目录名**，没有 `name` 列（组名 = 目录名）。
        return [
            {'groupId': '猫猫', 'description': '猫、躺平', 'createdAt': '2026-01-01'},
            {'groupId': '空组', 'description': '还没素材', 'createdAt': '2026-01-02'},
            {'groupId': 'collected', 'description': '改过描述', 'createdAt': ''},
        ]

    def _dirs(self):
        # 磁盘上真有目录的（含一个刚建好、还没素材、也没描述行的空组）。
        return ['猫猫', '空组', 'legacy-dir', 'disk-only']

    def test_directories_list_only_what_the_caller_needs(self):
        assets, rows, dirs = self._assets(), self._rows(), self._dirs()
        sending = h.sticker_group_directory(assets, rows, directories=dirs)
        self.assertEqual(
            [(item['groupId'], item['name'], item['description'], item['count']) for item in sending],
            [('collected', h.COLLECTED_STICKER_GROUP_NAME, '改过描述', 2),  # 内置组永远第一
             ('猫猫', '猫猫', '猫、躺平', 1),                                # 描述行按 createdAt
             ('legacy-dir', 'legacy-dir', '', 1)],                        # 没有描述行的目录名照样列
            '甲：只列真的有条目的组（空组选了也空手而归），空 group 桶不列',
        )
        organising = h.sticker_group_directory(assets, rows, include_empty=True, directories=dirs)
        self.assertEqual([item['groupId'] for item in organising],
                         ['collected', '猫猫', '空组', 'disk-only', 'legacy-dir'],
                         '乙：还没素材的组也要列（描述行的 + 磁盘上的空目录）')
        self.assertEqual(organising[2]['count'], 0)
        # 没有素材、也没有描述行的空目录**只在乙里出现**（甲里选了也空手而归）。
        self.assertNotIn('disk-only', [item['groupId'] for item in sending])
        self.assertEqual(
            [item['groupId'] for item in h.sticker_group_directory(
                assets, rows, limit=2, directories=dirs)],
            ['collected', '猫猫'],
            '提示词里的目录有上限',
        )

    def test_builtin_defaults_come_from_the_shared_constants(self):
        directory = h.sticker_group_directory(
            [{'assetId': 'a', 'group': 'collected'}], [],
        )
        self.assertEqual(directory[0]['name'], h.COLLECTED_STICKER_GROUP_NAME)
        self.assertEqual(directory[0]['description'], h.COLLECTED_STICKER_GROUP_DESCRIPTION)
        self.assertEqual(h.COLLECTED_STICKER_GROUP_ID, 'collected', 'id 是目录名，不许动')

    def test_items_need_a_description_and_respect_the_limit(self):
        assets = [
            {'assetId': 'a-1', 'group': 'g-1', 'description': '一只猫'},
            {'assetId': 'a-2', 'group': 'g-1', 'description': '  '},
            {'assetId': 'a-3', 'group': 'g-2', 'description': '一只狗'},
            {'assetId': '', 'group': 'g-1', 'description': '没有 id'},
        ]
        self.assertEqual(
            h.sticker_group_items(assets, 'g-1'),
            [{'assetId': 'a-1', 'description': '一只猫'}],
            '没描述的条目进不了候选（模型只能靠描述挑）',
        )
        self.assertEqual(h.sticker_group_items(assets, 'g-2', limit=1),
                         [{'assetId': 'a-3', 'description': '一只狗'}])
        self.assertEqual(h.sticker_group_items(assets, ''), [])
        self.assertEqual(
            h.sticker_group_directory_ids([{'groupId': 'x'}, {'groupId': ''}, 'noise']), {'x'},
        )


class StickerGroupNameTests(unittest.TestCase):
    """分组名 = **目录名**：命名规则的唯一定义处（`safe_sticker_group_name`）。"""

    def test_cjk_and_common_symbols_are_allowed(self):
        for name in ('猫猫', '日常（打招呼）', 'dogs-and_cats.v2', '表情 包', 'a b', '狗狗2'):
            with self.subTest(name=name):
                self.assertEqual(h.safe_sticker_group_name(name), name)
                self.assertEqual(h.sticker_group_name_problem(name), '')

    def test_path_separators_and_reserved_characters_are_rejected(self):
        for name in ('a/b', 'a\\b', 'a:b', 'a*b', 'a?b', 'a"b', 'a<b', 'a>b', 'a|b', '..',
                     '.hidden', 'a\nb', 'a\tb', '\x00abc'):
            with self.subTest(name=name):
                self.assertEqual(h.safe_sticker_group_name(name), '')
                self.assertTrue(h.sticker_group_name_problem(name), name)

    def test_length_limit_is_in_bytes_not_characters(self):
        """文件系统按**字节**算：33 个汉字（99 字节）行，34 个（102 字节）不行。"""
        self.assertEqual(h.safe_sticker_group_name('猫' * 33), '猫' * 33)
        self.assertEqual(h.safe_sticker_group_name('猫' * 34), '')
        self.assertEqual(len('猫' * 33), 33, '33 个**字符**——上一版按字符限长就挡不住了')

    def test_outer_whitespace_is_normalized_away(self):
        # 归一化（而不是拒绝）：写进磁盘的名字里永远没有首尾空白，界面回的是真值。
        self.assertEqual(h.safe_sticker_group_name('  猫猫  '), '猫猫')
        self.assertEqual(h.safe_sticker_group_name('   '), '')
        self.assertTrue(h.sticker_group_name_problem('   '))

    def test_the_root_bucket_name_is_reserved_only_for_writes(self):
        """`default` = 根目录素材的桶：**写入侧**拒（保留名），读侧照收（老目录要能显示）。"""
        self.assertEqual(h.STICKER_GROUP_ROOT_BUCKET, 'default')
        self.assertIn('default', h.STICKER_GROUP_RESERVED_NAMES)
        # 文件系统合法性那一关**不**拒它：扫描遇到既有的 default 目录照常收录。
        self.assertEqual(h.sticker_group_name_problem('default'), '')
        self.assertEqual(h.safe_sticker_group_name('default'), 'default')
        # 写入侧（新建 / 改名 / 移动 / 上传的目标）一律拒，并给出明确文案。
        self.assertIn('保留名', h.sticker_group_name_problem('default', reserved=True))
        self.assertEqual(h.safe_sticker_group_name('default', reserved=True), '')
        # 别的名字不受影响（保留名只钉这一个）。
        self.assertEqual(h.safe_sticker_group_name('默认组', reserved=True), '默认组')
        self.assertEqual(h.safe_sticker_group_name('Defaults', reserved=True), 'Defaults')


class StickerSelectionParsingTests(unittest.TestCase):
    """模型原样返回的 JSON：两种拼写都认，拿不准一律回空 / None。"""

    def test_group_choice_reads_local_media_first_then_the_top_level(self):
        self.assertEqual(
            h.parse_sticker_group_choice({'localMedia': {'stickerGroupId': '猫猫'}}), '猫猫',
            '中文组名是常态：它现在**就是目录名**',
        )
        self.assertEqual(
            h.parse_sticker_group_choice({'localMedia': {'sticker_group_id': 'g-2'}}), 'g-2',
        )
        self.assertEqual(h.parse_sticker_group_choice({'stickerGroupId': 'g-3'}), 'g-3')
        self.assertEqual(h.parse_sticker_group_choice({'sticker_group_id': 'g-4'}), 'g-4')
        # localMedia 优先：它就是"要发什么"的那一处。
        self.assertEqual(
            h.parse_sticker_group_choice(
                {'localMedia': {'stickerGroupId': 'g-1'}, 'stickerGroupId': 'g-9'},
            ),
            'g-1',
        )
        for junk in ({}, None, [], {'localMedia': {}}, {'localMedia': {'stickerGroupId': ''}},
                     {'stickerGroupId': '../etc'}, {'stickerGroupId': '  '},
                     {'stickerGroupId': 'a/b'}, {'stickerGroupId': '.hidden'},
                     {'stickerGroupId': 'x' * 200}):
            with self.subTest(junk=junk):
                self.assertEqual(h.parse_sticker_group_choice(junk), '')

    def test_selection_receipt_normalizes_without_deciding(self):
        self.assertEqual(
            h.parse_sticker_selection_receipt(
                {'stickerAssetId': 'a-1', 'willingness': 0.8, 'content': '正文'},
            ),
            {'assetId': 'a-1', 'content': '正文', 'willingness': 0.8},
        )
        self.assertEqual(
            h.parse_sticker_selection_receipt({'sticker_asset_id': 'a-2', 'content': ''}),
            {'assetId': 'a-2', 'content': '', 'willingness': None},
        )
        self.assertEqual(h.parse_sticker_selection_receipt(None), {})
        # 兼容 `assetId`：老提示词（平铺目录）回的也是它。
        self.assertEqual(h.parse_sticker_selection_receipt({'assetId': 'a-3'})['assetId'], 'a-3')

    def test_auto_group_receipt_cleans_text_and_rejects_bad_shapes(self):
        self.assertEqual(
            h.parse_sticker_auto_group({'group': {'existing': 'g-1'}}),
            {'mode': 'existing', 'groupId': 'g-1'},
        )
        self.assertEqual(
            h.parse_sticker_auto_group({'group': {'new': {'name': '  猫   猫  ', 'description': 'a\nb'}}}),
            {'mode': 'new', 'name': '猫 猫', 'description': 'a b'},
            '控制字符换空格、压空白（名字要能进提示词）',
        )
        # 名字会变成**磁盘目录名**：与人工建组同一条规则（按字节限长 + 禁路径字符）。
        self.assertIsNone(h.parse_sticker_auto_group({'group': {'new': {'name': 'x' * 200}}}))
        self.assertIsNone(h.parse_sticker_auto_group(
            {'group': {'new': {'name': '猫' * 40}}},  # 40 × 3 字节 = 120 > 100
        ))
        self.assertIsNone(h.parse_sticker_auto_group({'group': {'new': {'name': 'a/b'}}}))
        self.assertIsNone(h.parse_sticker_auto_group({'group': {'new': {'name': '..'}}}))
        self.assertIsNone(h.parse_sticker_auto_group({'group': {'existing': '../x'}}))
        self.assertIsNone(h.parse_sticker_auto_group({'group': {'new': {'name': '   '}}}))
        self.assertIsNone(h.parse_sticker_auto_group({'group': 'nonsense'}))
        self.assertIsNone(h.parse_sticker_auto_group({'description': '只有描述'}))
        self.assertIsNone(h.parse_sticker_auto_group(None))

    def test_follow_up_content_only_fills_a_missing_visible_reply(self):
        """正文**以第一段为准**（§48.1）：第二段只能填空，不能覆盖。"""
        # 第一段写了正文 → 一字不动（这就是"第二段不许重写"的那条协议）。
        written = {'interaction': {'reply': {'mode': 'immediate', 'content': '第一段写的'}}}
        self.assertFalse(h.apply_sticker_follow_up_content(written, '第二段想改的'))
        self.assertEqual(written['interaction']['reply']['content'], '第一段写的')
        group_written = {'groupReply': {'mode': 'immediate', 'content': '群里的第一段'}}
        self.assertFalse(h.apply_sticker_follow_up_content(group_written, '第二段想改的'))
        self.assertEqual(group_written['groupReply']['content'], '群里的第一段')
        # 该说话但正文是空的 → 才补。
        private = {'interaction': {'reply': {'mode': 'immediate', 'content': '   '}}}
        self.assertTrue(h.apply_sticker_follow_up_content(private, '补上的'))
        self.assertEqual(private['interaction']['reply']['content'], '补上的')
        group = {'groupReply': {'mode': 'immediate', 'content': ''}}
        self.assertTrue(h.apply_sticker_follow_up_content(group, '补上的'))
        self.assertEqual(group['groupReply']['content'], '补上的')
        # 沉默 / 延后 / 没有回复位：不许凭空造一条消息出来。
        for decision in (
            {'interaction': {'reply': {'mode': 'none', 'content': ''}}},
            {'interaction': {'reply': {'mode': 'deferred', 'content': ''}}},
            {'interaction': {}},
            {'groupReply': {'mode': 'none', 'content': ''}},
            {},
        ):
            with self.subTest(decision=decision):
                self.assertFalse(h.apply_sticker_follow_up_content(decision, '新的'))
        self.assertFalse(h.apply_sticker_follow_up_content(private, '   '))
        self.assertEqual(
            h.visible_reply_text({'interaction': {'reply': {'mode': 'immediate', 'content': '  '}}}), '',
        )
        self.assertEqual(
            h.visible_reply_text({'groupReply': {'mode': 'immediate', 'content': '群里的'}}), '群里的',
        )


if __name__ == '__main__':
    unittest.main()
