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



if __name__ == '__main__':
    unittest.main()
