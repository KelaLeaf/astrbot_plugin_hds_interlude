"""`upstream/test/qzone.test.ts`（253 行）的逐条移植 + 本移植版的边界与接线用例。

上游测试的对应关系（14 条 `test(...)` 一条不落）：

| 上游用例 | 本文件 |
| --- | --- |
| `resolveQzoneConfig applies conservative defaults and clamps out-of-range values` | `ResolveConfigTests.test_applies_conservative_defaults_and_clamps_out_of_range_values` |
| `evaluateQzoneGate enforces per-kind daily caps and counts pending as used` | `GateTests.test_enforces_per_kind_daily_caps_and_counts_pending_as_used` |
| `evaluateQzoneGate enforces cross-kind minimum interval and disabled switch` | `GateTests.test_enforces_cross_kind_minimum_interval_and_disabled_switch` |
| `normalizeQzoneMsgEntry drops malformed rows and coerces fields` | `NormalizeTests.test_msg_entry_drops_malformed_rows_and_coerces_fields` |
| `normalizeQzoneFeedEntry keeps structural fields only; freshQzoneFeeds filters by window` | `NormalizeTests.test_feed_entry_keeps_structural_fields_and_fresh_feeds_filters_by_window` |
| `callQzoneAction validates echo frames and preserves retcode for risk classification` | `CallActionTests.test_validates_echo_frames_and_preserves_retcode` |
| `qzoneIntentFromPayload validates the three actions and their required fields` | `IntentPayloadTests.test_validates_the_three_actions_and_their_required_fields` |
| `qzoneFeedCandidates keeps only fresh talk-type unseen feeds, capped at 2` | `FeedFilterTests.test_keeps_only_fresh_talk_type_unseen_feeds_capped_at_two` |
| `matchQzoneFeedContent aligns by exact tid only` | `FeedFilterTests.test_match_content_aligns_by_exact_tid_only` |
| `qzoneVisibilityLabel maps the five rights` | `VisibilityTests.test_maps_the_five_rights` |
| `feed-seen rows never consume action quota or interval; unknown outcomes do` | `GateTests.test_feed_seen_rows_never_consume_quota_and_unknown_outcomes_do` |
| `transport errors are flagged ambiguous; explicit failure frames are not` | `CallActionTests.test_transport_errors_are_ambiguous_and_failure_frames_are_not` |
| `comment/like tids must match a strict charset` | `IntentPayloadTests.test_comment_and_like_tids_must_match_a_strict_charset` |
| `matchQzoneFeedContent is exact-tid only — no nearest-time fallback` | `FeedFilterTests.test_match_content_is_exact_tid_only` |
| `qzoneExecute serializes quota reservation: concurrent posts cannot both pass a cap of 1` | `ServiceExecuteTests.test_concurrent_posts_cannot_both_pass_a_cap_of_one` |
| `executeQzoneIntent rejects comment/like tids that were never observed` | `ServiceIntentTests.test_rejects_comment_and_like_tids_that_were_never_observed` |

另加本移植版自己的用例：本地日边界（与「Token 统计」同口径）、48h 窗口、最小间隔
边界、端点分桶、坏 payload、可见日志出口、单飞锁、脚本条目形状，以及
`ConfigWiringTests`——配置段从隐藏兼容位 `qzone_compat` 转正成真分组 `qzone` 那次
静默失效的回归（**夹具和实现一起写错时全绿也抓不到**，所以那条用例既走
`normalize_config` 真链路，又把 `_conf_schema.json` 的分组键与
`resolve_qzone_config` 的键对账）。

**传输层是按契约 stub 的**：`async call_onebot(action, params) ->
{'ok', 'error', 'data'}`，绝不真实联网、不出现真实 QQ 号。

运行：`python3 -m unittest plugin.tests.test_qzone -v`
"""

from __future__ import annotations

import ast
import asyncio
import json
import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from typing import Any, Optional

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from plugin.core import qzone as q  # noqa: E402
from plugin.core.database import Database  # noqa: E402
from plugin.core.narrator_prompts import to_prompt_payload  # noqa: E402
from plugin.core.service import InterludeService  # noqa: E402
from plugin.core.service.base import InterludeContext, ServiceChunk0  # noqa: E402
from plugin.core.service.chunk13 import ServiceChunk13  # noqa: E402
from plugin.core.time import iso, parse_dt  # noqa: E402
from plugin.core.token_stats import day_key  # noqa: E402

#: 夹具用的**通用**地址/账号（绝不是谁的机器/QQ 号）。
STORY = {
    'id': 'character:testbot:1',
    'platform': 'onebot',
    'selfId': '10001',
    'status': 'active',
    'setting': {},
    'state': {},
}
STORY_ID = STORY['id']

#: 服务器本地时区（限流门按本地日切分，用例必须跟随运行环境）。
LOCAL_TZ = datetime.now().astimezone().tzinfo


def local(day: int, hour: int, minute: int = 0) -> datetime:
    """构造一个"本地墙上时间"（带本地偏移，parse 后正好落在同一本地日）。"""
    return datetime(2026, 9, day, hour, minute, tzinfo=LOCAL_TZ)


NOW = local(29, 15, 0)
BASE_CONFIG = q.resolve_qzone_config({'enabled': True})



def record(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        'storyId': 's1', 'kind': 'post', 'tid': 't1', 'status': 'confirmed', 'createdAt': NOW,
    }
    row.update(overrides)
    return row


# --------------------------------------------------------------------------- #
# 纯策略：上游逐条
# --------------------------------------------------------------------------- #


class ResolveConfigTests(unittest.TestCase):
    def test_applies_conservative_defaults_and_clamps_out_of_range_values(self):
        defaults = q.resolve_qzone_config()
        self.assertIs(defaults['enabled'], False)
        self.assertEqual(defaults['daily_post_cap'], 3)
        self.assertEqual(defaults['min_interval_minutes'], 90)
        self.assertEqual(defaults['feed_window_minutes'], 120)
        clamped = q.resolve_qzone_config({
            'enabled': True, 'dailyPostCap': 99,
            'minIntervalMinutes': 1, 'feedWindowMinutes': 9,
        })
        self.assertEqual(clamped['daily_post_cap'], 20)
        self.assertEqual(clamped['min_interval_minutes'], 10)
        self.assertEqual(clamped['feed_window_minutes'], 15)

    def test_snake_case_spelling_is_accepted_and_floor_is_js_floor(self):
        """本移植版配置段是 snake_case；`Math.floor` 对负数向 -inf（不是截断）。"""
        resolved = q.resolve_qzone_config({
            'enabled': True, 'daily_post_cap': 4.9, 'daily_comment_cap': -3,
            'daily_like_cap': 7.2, 'min_interval_minutes': 60, 'feed_window_minutes': 300,
        })
        self.assertEqual(resolved['daily_post_cap'], 4)
        self.assertEqual(resolved['daily_comment_cap'], 0, '负数夹到下限 0')
        self.assertEqual(resolved['daily_like_cap'], 7)
        self.assertEqual(resolved['min_interval_minutes'], 60)
        self.assertEqual(resolved['feed_window_minutes'], 300)

    def test_missing_key_keeps_default_but_explicit_null_is_zero(self):
        """上游 `Number(undefined) === NaN` / `Number(null) === 0` 语义不同，别合并。"""
        missing = q.resolve_qzone_config({'enabled': True})
        self.assertEqual(missing['daily_post_cap'], 3)
        explicit_null = q.resolve_qzone_config({'enabled': True, 'daily_post_cap': None})
        self.assertEqual(explicit_null['daily_post_cap'], 0, 'null → 0 → 夹到下限')
        non_numeric = q.resolve_qzone_config({'enabled': True, 'daily_post_cap': 'abc'})
        self.assertEqual(non_numeric['daily_post_cap'], 3, 'NaN 保持默认')
        numeric_string = q.resolve_qzone_config({'enabled': True, 'daily_post_cap': '5'})
        self.assertEqual(numeric_string['daily_post_cap'], 5, "JS Number('5') === 5")

    def test_enabled_requires_strict_true(self):
        for value in (1, 'true', 'yes', {}, []):
            with self.subTest(value=value):
                self.assertIs(q.resolve_qzone_config({'enabled': value})['enabled'], False)
        self.assertIs(q.resolve_qzone_config({'enabled': True})['enabled'], True)


class GateTests(unittest.TestCase):
    def test_enforces_per_kind_daily_caps_and_counts_pending_as_used(self):
        now = local(29, 15, 0)
        morning = now - timedelta(hours=6)
        used = [
            record(kind='post', createdAt=morning),
            record(kind='post', createdAt=morning + timedelta(hours=2)),
            record(kind='post', status='pending', createdAt=morning + timedelta(hours=4)),
            # failed 不计数；昨日动作不计入今日
            record(kind='post', status='failed', createdAt=now - timedelta(hours=2)),
            record(kind='like', createdAt=now - timedelta(hours=30)),
        ]
        config = dict(BASE_CONFIG, min_interval_minutes=10)
        gate = q.evaluate_qzone_gate(used, config, 'post', now + timedelta(hours=1))
        self.assertIs(gate['allowed'], False)
        self.assertEqual(gate['reason'], 'daily-cap')
        self.assertEqual(gate['used_today'], 3)
        # comment/like 独立配额，不受 post 占满影响
        comment_gate = q.evaluate_qzone_gate(used, config, 'comment', now + timedelta(hours=1))
        self.assertIs(comment_gate['allowed'], True)
        self.assertEqual(comment_gate['cap'], 6)

    def test_enforces_cross_kind_minimum_interval_and_disabled_switch(self):
        now = local(29, 15, 0)
        recent_like = record(kind='like', createdAt=now - timedelta(minutes=30))
        blocked = q.evaluate_qzone_gate([recent_like], BASE_CONFIG, 'post', now)
        self.assertIs(blocked['allowed'], False)
        self.assertEqual(blocked['reason'], 'min-interval')
        later = q.evaluate_qzone_gate([recent_like], BASE_CONFIG, 'post', now + timedelta(minutes=91))
        self.assertIs(later['allowed'], True)
        off = q.evaluate_qzone_gate([], q.resolve_qzone_config({'enabled': False}), 'post', now)
        self.assertIs(off['allowed'], False)
        self.assertEqual(off['reason'], 'disabled')

    def test_feed_seen_rows_never_consume_quota_and_unknown_outcomes_do(self):
        now = local(29, 15, 0)
        config = dict(BASE_CONFIG, min_interval_minutes=90)
        # 一分钟前刚读到好友动态（feed-seen）——不得挡住动作
        seen_only = [record(kind='feed-seen', tid='feedkey', createdAt=now - timedelta(minutes=1))]
        self.assertIs(q.evaluate_qzone_gate(seen_only, config, 'post', now)['allowed'], True)
        # 结果未知（unknown）的动作按已发生保守计入：占间隔、占配额
        unknown_action = [record(kind='like', status='unknown', createdAt=now - timedelta(minutes=1))]
        blocked = q.evaluate_qzone_gate(unknown_action, config, 'post', now)
        self.assertIs(blocked['allowed'], False)
        self.assertEqual(blocked['reason'], 'min-interval')
        cap_config = dict(BASE_CONFIG, min_interval_minutes=10, daily_post_cap=1)
        unknown_post = [record(kind='post', status='unknown', createdAt=now - timedelta(hours=3))]
        self.assertEqual(q.evaluate_qzone_gate(unknown_post, cap_config, 'post', now)['reason'], 'daily-cap')

    def test_min_interval_tick_boundary_is_strictly_less_than(self):
        """上游 `< minIntervalMinutes`：正好到点放行，差一毫秒不放行（tick 边界）。"""
        now = local(29, 15, 0)
        config = dict(BASE_CONFIG, min_interval_minutes=90)
        exactly = [record(kind='like', createdAt=now - timedelta(minutes=90))]
        self.assertIs(q.evaluate_qzone_gate(exactly, config, 'post', now)['allowed'], True)
        one_ms_short = [record(kind='like', createdAt=now - timedelta(minutes=90) + timedelta(milliseconds=1))]
        blocked = q.evaluate_qzone_gate(one_ms_short, config, 'post', now)
        self.assertIs(blocked['allowed'], False)
        self.assertEqual(blocked['reason'], 'min-interval')

    def test_daily_count_uses_the_server_local_day_same_as_token_stats(self):
        """跨本地午夜：昨天的动作不计入今天的配额（与 `day_key` 同口径）。"""
        local_now = datetime.now().astimezone()
        midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        before = midnight - timedelta(minutes=10)
        after = midnight + timedelta(minutes=10)
        self.assertNotEqual(q.local_day_key(before), q.local_day_key(after))
        for instant in (before, after, local(29, 15, 0)):
            self.assertEqual(q.local_day_key(instant), day_key(instant), '与 Token 统计同口径')
        config = dict(BASE_CONFIG, daily_post_cap=1, min_interval_minutes=10)
        gate = q.evaluate_qzone_gate([record(kind='post', createdAt=before)], config, 'post', after)
        self.assertEqual(gate['used_today'], 0, '昨天 23:50 的动作不算今天的')
        self.assertIs(gate['allowed'], True)

    def test_unparseable_created_at_is_skipped_like_a_null_column(self):
        now = local(29, 15, 0)
        config = dict(BASE_CONFIG, daily_post_cap=1, min_interval_minutes=10)
        rows = [record(kind='post', createdAt=None), record(kind='post', createdAt='not-a-date')]
        gate = q.evaluate_qzone_gate(rows, config, 'post', now)
        self.assertEqual(gate['used_today'], 0)
        self.assertIs(gate['allowed'], True)

    def test_upstream_shaped_input_and_iso_strings_are_accepted(self):
        """两种调用形状都收：`(records, config, kind, now)` 与上游的 `{kind, now}`。"""
        now = local(29, 15, 0)
        config = dict(BASE_CONFIG, min_interval_minutes=10)
        rows = [record(createdAt=(now - timedelta(minutes=1)).isoformat())]
        shaped = q.evaluate_qzone_gate(rows, config, {'kind': 'post', 'now': now})
        positional = q.evaluate_qzone_gate(rows, config, 'post', now)
        self.assertEqual(shaped, positional)
        self.assertEqual(shaped['reason'], 'min-interval')

    def test_zero_cap_blocks_everything(self):
        now = local(29, 15, 0)
        config = dict(BASE_CONFIG, daily_post_cap=0)
        gate = q.evaluate_qzone_gate([], config, 'post', now)
        self.assertIs(gate['allowed'], False)
        self.assertEqual(gate['reason'], 'daily-cap')
        self.assertEqual(gate['cap'], 0)


class EndpointBucketingTests(unittest.TestCase):
    def test_records_are_filtered_per_endpoint_but_legacy_rows_count_everywhere(self):
        rows = [
            record(endpointId='ep-a'),
            record(endpointId='ep-b'),
            record(),
        ]
        self.assertEqual(len(q.qzone_records_for_endpoint(rows, 'ep-a')), 2, '本端点 + 无归因历史行')
        self.assertEqual(len(q.qzone_records_for_endpoint(rows, 'ep-b')), 2)
        self.assertEqual(len(q.qzone_records_for_endpoint(rows, '')), 3, '不传端点 = 全量')
        self.assertEqual(len(q.qzone_records_for_endpoint(rows, None)), 3)

    def test_gate_only_counts_the_same_endpoint(self):
        now = local(29, 15, 0)
        config = dict(BASE_CONFIG, daily_post_cap=1, min_interval_minutes=10)
        other_endpoint = [record(endpointId='ep-b', createdAt=now - timedelta(hours=2))]
        filtered = q.qzone_records_for_endpoint(other_endpoint, 'ep-a')
        self.assertIs(q.evaluate_qzone_gate(filtered, config, 'post', now)['allowed'], True)


class NormalizeTests(unittest.TestCase):
    def test_msg_entry_drops_malformed_rows_and_coerces_fields(self):
        ok = q.normalize_qzone_msg_entry({
            'tid': 12345, 'content': '今天天气不错', 'time': 1_760_000_000,
            'comment_num': '3', 'is_private': True, 'images': ['u1', 2, ''],
        })
        self.assertIsNotNone(ok)
        self.assertEqual(ok['tid'], '12345')
        self.assertEqual(ok['comment_num'], 3)
        self.assertIs(ok['is_private'], True)
        self.assertEqual(ok['images'], ['u1', '2'])
        self.assertEqual(ok['time'].timestamp() * 1000, 1_760_000_000_000)
        self.assertIsNone(q.normalize_qzone_msg_entry({'content': 'no tid'}))
        self.assertIsNone(q.normalize_qzone_msg_entry(None))

    def test_msg_entry_edge_cases(self):
        # images 最多 9 张；非数组 → 空
        many = q.normalize_qzone_msg_entry({'tid': 't', 'images': [str(i) for i in range(20)]})
        self.assertEqual(many['images'], [str(i) for i in range(9)])
        self.assertEqual(q.normalize_qzone_msg_entry({'tid': 't', 'images': 'nope'})['images'], [])
        # is_private 只认严格 true（'true' / 1 都不算）
        self.assertIs(q.normalize_qzone_msg_entry({'tid': 't', 'is_private': 'true'})['is_private'], False)
        self.assertIs(q.normalize_qzone_msg_entry({'tid': 't', 'is_private': 1})['is_private'], False)
        # 时间非法 → epoch（不是 now）
        epoch = q.normalize_qzone_msg_entry({'tid': 't', 'time': 'nope'})
        self.assertEqual(epoch['time'].timestamp(), 0)
        # tid 为空白串 → 丢
        self.assertIsNone(q.normalize_qzone_msg_entry({'tid': '   '}))
        # comment_num 缺失 → 0
        self.assertEqual(q.normalize_qzone_msg_entry({'tid': 't'})['comment_num'], 0)
        self.assertIsNone(q.normalize_qzone_msg_entry('junk'))

    def test_feed_entry_keeps_structural_fields_and_fresh_feeds_filters_by_window(self):
        now = local(29, 15, 0)
        feed = q.normalize_qzone_feed_entry(
            {'uin': 10001, 'nickname': '好友A', 'time': 1_760_000_000, 'appid': 311, 'key': 'abc'}, now,
        )
        self.assertIsNotNone(feed)
        self.assertEqual(feed['uin'], '10001')
        self.assertEqual(feed['appid'], 311)
        self.assertIsNone(q.normalize_qzone_feed_entry({'uin': 10001}))
        feeds = [
            {'uin': '1', 'nickname': '', 'time': now - timedelta(minutes=10), 'appid': 311, 'key': 'k1'},
            {'uin': '2', 'nickname': '', 'time': now - timedelta(hours=5), 'appid': 311, 'key': 'k2'},
        ]
        fresh = q.fresh_qzone_feeds(feeds, BASE_CONFIG, now)
        self.assertEqual([item['key'] for item in fresh], ['k1'])

    def test_feed_entry_time_falls_back_to_now_and_requires_key_and_uin(self):
        now = local(29, 15, 0)
        fallback = q.normalize_qzone_feed_entry({'uin': '1', 'key': 'k', 'time': 'nope'}, now)
        self.assertEqual(fallback['time'], now)
        self.assertIsNone(q.normalize_qzone_feed_entry({'uin': '1', 'key': '   '}, now))
        self.assertIsNone(q.normalize_qzone_feed_entry({'uin': '', 'key': 'k'}, now))
        self.assertEqual(q.normalize_qzone_feed_entry({'uin': '1', 'key': 'k'}, now)['appid'], 0)


class VisibilityTests(unittest.TestCase):
    def test_maps_the_five_rights(self):
        self.assertEqual(q.qzone_visibility_label(1), '所有人可见')
        self.assertEqual(q.qzone_visibility_label(4), '好友可见')
        self.assertEqual(q.qzone_visibility_label(64), '仅自己可见')
        self.assertEqual(q.qzone_visibility_label(0), '好友可见')

    def test_all_five_levels_and_unknown_fall_back_to_friends(self):
        self.assertEqual(q.qzone_visibility_label(16), '部分好友可见')
        self.assertEqual(q.qzone_visibility_label(128), '部分好友不可见')
        for odd in (None, '1', True, 7, 999):
            with self.subTest(value=odd):
                self.assertEqual(q.qzone_visibility_label(odd), '好友可见', '非法档位回落好友可见')


class IntentPayloadTests(unittest.TestCase):
    def test_validates_the_three_actions_and_their_required_fields(self):
        # 发帖：内容必填、默认好友可见、隐私档白名单
        post = q.qzone_intent_from_payload({'action': 'post', 'content': '今晚的风很好。'})
        self.assertEqual(post, {
            'action': 'post', 'content': '今晚的风很好。',
            'targetUin': None, 'targetName': None, 'ugcRight': 4,
        })
        private_post = q.qzone_intent_from_payload({'action': 'post', 'content': '写给自己。', 'ugcRight': 64})
        self.assertEqual(private_post['ugcRight'], 64)
        self.assertIsNone(q.qzone_intent_from_payload({'action': 'post', 'content': ''}))
        self.assertIsNone(q.qzone_intent_from_payload({'action': 'post', 'content': 'x' * 2_001}))
        self.assertEqual(
            q.qzone_intent_from_payload({'action': 'post', 'content': 'ok', 'ugcRight': 7})['ugcRight'], 4,
            '非法隐私档回退默认好友可见',
        )
        # 评论：tid + 内容必填、长度上限、目标账号数字校验
        comment = q.qzone_intent_from_payload({
            'action': 'comment', 'content': '哈哈哈', 'tid': '58a87a00',
            'targetUin': '8038488', 'targetName': 'creme',
        })
        self.assertEqual(comment, {
            'action': 'comment', 'content': '哈哈哈', 'tid': '58a87a00',
            'targetUin': '8038488', 'targetName': 'creme',
        })
        self.assertIsNone(q.qzone_intent_from_payload({'action': 'comment', 'content': '无目标'}))
        self.assertIsNone(q.qzone_intent_from_payload({'action': 'comment', 'content': 'x' * 501, 'tid': 't'}))
        self.assertIsNone(
            q.qzone_intent_from_payload({'action': 'comment', 'content': '好', 'tid': 'tid12345', 'targetUin': 'abc'})['targetUin'],
            '非数字目标回退为未提供',
        )
        # 点赞：tid 必填
        like = q.qzone_intent_from_payload({'action': 'like', 'tid': '58a87a00', 'targetUin': '2233029096'})
        self.assertEqual(like, {
            'action': 'like', 'tid': '58a87a00', 'targetUin': '2233029096', 'targetName': None,
        })
        self.assertIsNone(q.qzone_intent_from_payload({'action': 'like'}))
        # 未知动作 / 非 object
        self.assertIsNone(q.qzone_intent_from_payload({'action': 'share', 'content': 'x'}))
        self.assertIsNone(q.qzone_intent_from_payload('junk'))

    def test_comment_and_like_tids_must_match_a_strict_charset(self):
        self.assertIsNotNone(q.qzone_intent_from_payload({'action': 'like', 'tid': '58a87a00746eb96ab88f0000'}))
        self.assertIsNone(q.qzone_intent_from_payload({'action': 'like', 'tid': 'ab'}))
        self.assertIsNone(q.qzone_intent_from_payload({'action': 'like', 'tid': 'tid with spaces!!'}))
        self.assertIsNone(q.qzone_intent_from_payload({'action': 'comment', 'content': '好', 'tid': 'x' * 65}))
        self.assertIsNotNone(q.qzone_intent_from_payload({'action': 'like', 'tid': 'x' * 64}), '64 位合法')
        self.assertIsNone(q.qzone_intent_from_payload({'action': 'like', 'tid': '中文说说标识'}))

    def test_payload_edge_cases(self):
        self.assertIsNone(q.qzone_intent_from_payload(None))
        self.assertIsNone(q.qzone_intent_from_payload([]))
        self.assertIsNone(q.qzone_intent_from_payload({'action': None, 'content': 'x'}))
        self.assertIsNone(q.qzone_intent_from_payload({'action': 'POST', 'content': 'x'}), '严格小写')
        # 内容先 trim 再看长度
        self.assertIsNone(q.qzone_intent_from_payload({'action': 'post', 'content': '   '}))
        self.assertEqual(q.qzone_intent_from_payload({'action': 'post', 'content': '  你好  '})['content'], '你好')
        # targetName 截断到 40
        long_name = q.qzone_intent_from_payload({
            'action': 'like', 'tid': 'tid1234', 'targetName': 'n' * 60,
        })
        self.assertEqual(len(long_name['targetName']), 40)
        # targetUin 长度边界（4–12 位数字）
        self.assertIsNone(q.qzone_intent_from_payload({'action': 'like', 'tid': 'tid1234', 'targetUin': '123'})['targetUin'])
        self.assertIsNone(q.qzone_intent_from_payload({'action': 'like', 'tid': 'tid1234', 'targetUin': '1' * 13})['targetUin'])
        self.assertEqual(
            q.qzone_intent_from_payload({'action': 'like', 'tid': 'tid1234', 'targetUin': '1234'})['targetUin'], '1234',
        )
        # ugcRight 数字字符串也算（JS Number('64') === 64）
        self.assertEqual(
            q.qzone_intent_from_payload({'action': 'post', 'content': 'x', 'ugcRight': '64'})['ugcRight'], 64,
        )


class FeedFilterTests(unittest.TestCase):
    def _feed(self, key: str, appid: int, uin: str, minutes_ago: int) -> dict[str, Any]:
        return {
            'uin': uin, 'nickname': 'n%s' % key, 'time': NOW - timedelta(minutes=minutes_ago),
            'appid': appid, 'key': key,
        }

    def test_keeps_only_fresh_talk_type_unseen_feeds_capped_at_two(self):
        feeds = [
            self._feed('k1', 311, '10001', 20),          # ✓ 新鲜说说
            self._feed('ad', 6600, '0', 5),              # ✗ 广告位
            self._feed('k2', 311, '10002', 40),          # ✓
            self._feed('seen', 311, '10003', 10),        # ✗ 已入账
            self._feed('k3', 311, '10004', 50),          # ✓ 但超过单轮上限
            self._feed('old', 311, '10005', 200),        # ✗ 超出时间窗（默认 120 分钟）
            self._feed('official', 5000, '20050606', 5),  # ✗ 官方号
        ]
        candidates = q.qzone_feed_candidates(feeds, {'seen'}, q.resolve_qzone_config({'enabled': True}), NOW)
        self.assertEqual([item['key'] for item in candidates], ['k1', 'k2'])

    def test_candidate_filters_edge_cases(self):
        feeds = [
            self._feed('zero-uin', 311, '0', 5),
            self._feed('empty-uin', 311, '', 5),
            self._feed('string-appid', '311', '10009', 5),
            self._feed('exactly-window', 311, '10010', 120),
            self._feed('just-over-window', 311, '10011', 121),
        ]
        candidates = q.qzone_feed_candidates(feeds, set(), BASE_CONFIG, NOW)
        self.assertEqual([item['key'] for item in candidates], ['exactly-window'])
        self.assertEqual(q.qzone_feed_candidates([], set(), BASE_CONFIG, NOW), [])

    def test_match_content_aligns_by_exact_tid_only(self):
        feed = {'uin': '10001', 'nickname': 'a', 'time': NOW, 'appid': 311, 'key': 'feedkey'}
        entries = [
            self._msg('other', '更早的一条', -60),
            self._msg('almost', '三十秒前', -0.5),
        ]
        # 审计修复：近似时间配对会错配连发正文——只认 tid 精确命中，否则空串。
        self.assertEqual(q.match_qzone_feed_content(entries, feed), '')
        self.assertEqual(
            q.match_qzone_feed_content(entries + [self._msg('feedkey', '精确命中', -2)], feed), '精确命中',
        )
        self.assertEqual(q.match_qzone_feed_content([], feed), '')

    def test_match_content_is_exact_tid_only(self):
        feed = {'uin': '10001', 'nickname': 'a', 'time': NOW, 'appid': 311, 'key': 'feedkey'}
        near_miss = [self._msg('other', '连发的另一条', -0.5)]
        self.assertEqual(q.match_qzone_feed_content(near_miss, feed), '')
        self.assertEqual(
            q.match_qzone_feed_content(near_miss + [self._msg('feedkey', '精确命中', 0)], feed), '精确命中',
        )

    @staticmethod
    def _msg(tid: str, content: str, minutes_ago: float) -> dict[str, Any]:
        return {
            'tid': tid, 'content': content, 'time': NOW + timedelta(minutes=minutes_ago),
            'comment_num': 0, 'is_private': False, 'images': [],
        }


# --------------------------------------------------------------------------- #
# 动作调用（传输层按契约 stub）
# --------------------------------------------------------------------------- #


class _StubTransport:
    """按契约的 `Transport`：`call_onebot(action, params) -> {'ok','error','data'}`。

    `http` 可选：给了才**多一条** `request_text`（QZone CGI 的原始 HTTP）。
    不给 = 这个传输层没有原始 HTTP 能力——v1.7.10 起空间动作只有 CGI 一条路，
    所以要走通读通道的用例必须像真机一样给出 `http`（夹具不许比生产更宽容，坑 39）。
    """

    def __init__(self, handler: Any = None, http: Any = None) -> None:
        self.handler = handler
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.http_calls: list[dict[str, Any]] = []
        if http is not None:
            self.http_handler = http

            async def request_text(method: str, url: str, headers: object = None,
                                   data: object = None) -> object:
                self.http_calls.append({
                    'method': method, 'url': url,
                    'headers': dict(headers or {}), 'data': dict(data or {}),
                })
                result = self.http_handler(method, url, headers, data)
                if asyncio.iscoroutine(result):
                    result = await result
                if isinstance(result, BaseException):
                    raise result
                return result

            self.request_text = request_text  # type: ignore[assignment]

    async def call_onebot(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((action, dict(params)))
        if self.handler is None:
            return {'ok': True, 'error': '', 'data': {}}
        result = self.handler(action, params)
        if asyncio.iscoroutine(result):
            result = await result
        if isinstance(result, BaseException):
            raise result
        return result


class CallActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_validates_echo_frames_and_preserves_retcode(self):
        ok = await q.call_qzone_action(
            _StubTransport(lambda a, p: {'status': 'ok', 'retcode': 0, 'data': {'tid': '777'}}).call_onebot,
            'send_qzone_msg',
        )
        self.assertEqual(ok, {'tid': '777'})
        with self.assertRaises(q.QzoneActionError) as caught:
            await q.call_qzone_action(
                _StubTransport(
                    lambda a, p: {'status': 'failed', 'retcode': 1200, 'message': '操作过于频繁'},
                ).call_onebot,
                'send_qzone_msg',
            )
        self.assertEqual(caught.exception.retcode, 1200)
        self.assertRegex(str(caught.exception), '操作过于频繁')
        # 传输契约的失败帧（只有 error + ok=false）也要能读出来
        with self.assertRaises(q.QzoneActionError) as contract:
            await q.call_qzone_action(
                _StubTransport(lambda a, p: {'ok': False, 'error': '操作过于频繁'}).call_onebot,
                'send_qzone_msg',
            )
        self.assertRegex(str(contract.exception), '操作过于频繁')
        self.assertIs(contract.exception.ambiguous, False)
        # 抛异常的调用口 = 结果未知
        with self.assertRaises(q.QzoneActionError) as raised:
            await q.call_qzone_action(_StubTransport(lambda a, p: RuntimeError('socket closed')).call_onebot, 'like_qzone')
        self.assertRegex(str(raised.exception), 'socket closed')
        self.assertIs(raised.exception.ambiguous, True)
        # 能力探测
        self.assertIs(await q.probe_qzone_available(_StubTransport(lambda a, p: RuntimeError('nope')).call_onebot), False)
        self.assertIs(await q.probe_qzone_available(_StubTransport().call_onebot), True)

    async def test_transport_errors_are_ambiguous_and_failure_frames_are_not(self):
        for marker in ('socket hang up', 'request timeout', '连接超时', 'ECONNRESET'):
            with self.subTest(marker=marker):
                with self.assertRaises(q.QzoneActionError) as caught:
                    await q.call_qzone_action(
                        _StubTransport(lambda a, p, m=marker: {'ok': False, 'error': m}).call_onebot,
                        'send_qzone_msg',
                    )
                self.assertIs(caught.exception.ambiguous, True)
        with self.assertRaises(q.QzoneActionError) as explicit:
            await q.call_qzone_action(
                _StubTransport(lambda a, p: {'ok': False, 'error': '操作过于频繁', 'retcode': 1200}).call_onebot,
                'send_qzone_msg',
            )
        self.assertIs(explicit.exception.ambiguous, False)
        # 宿主明确打标时以标记为准
        with self.assertRaises(q.QzoneActionError) as flagged:
            await q.call_qzone_action(
                _StubTransport(lambda a, p: {'ok': False, 'error': 'boom', 'ambiguous': True}).call_onebot,
                'send_qzone_msg',
            )
        self.assertIs(flagged.exception.ambiguous, True)
        # 传输层契约的"结果未知，请勿自动重试"尾巴（没打标时也要认出来）
        with self.assertRaises(q.QzoneActionError) as tailed:
            await q.call_qzone_action(
                _StubTransport(
                    lambda a, p: {'ok': False, 'error': 'send_qzone_msg 调用异常：超时（结果未知，请勿自动重试）'},
                ).call_onebot,
                'send_qzone_msg',
            )
        self.assertIs(tailed.exception.ambiguous, True)
        # "传输层不可用"= 压根没发出去 → 不能算结果未知（否则白白占配额）
        with self.assertRaises(q.QzoneActionError) as unavailable:
            await q.call_qzone_action(
                _StubTransport(lambda a, p: {'ok': False, 'error': 'transport-unavailable'}).call_onebot,
                'send_qzone_msg',
            )
        self.assertIs(unavailable.exception.ambiguous, False)

    async def test_data_defaults_to_empty_and_params_are_forwarded(self):
        transport = _StubTransport()
        self.assertEqual(await q.call_qzone_action(transport.call_onebot, 'get_qzone_msg_list', {'num': 1}), {})
        self.assertEqual(transport.calls, [('get_qzone_msg_list', {'num': 1})])
        transport2 = _StubTransport(lambda a, p: {'ok': True})
        self.assertEqual(await q.call_qzone_action(transport2.call_onebot, 'x'), {})
        self.assertEqual(transport2.calls, [('x', {})], 'params 缺省发空 dict')
        malformed = await q.call_qzone_action(
            _StubTransport(lambda a, p: {'ok': True, 'data': {'tid': 't'}}).call_onebot, 'y',
        )
        self.assertEqual(malformed, {'tid': 't'})


class ConfigWiringTests(unittest.TestCase):
    """「配置名字对不上」这类静默失效的回归（schema 已把 `qzone_compat` 转正为 `qzone`）。

    夹具与实现一起写错时，全绿也抓不到 bug——所以这里既测接线，也把
    `_conf_schema.json` 的 `qzone` 分组键与 `resolve_qzone_config` 的键**对账**。
    """

    @staticmethod
    def _section(config):
        from plugin.core.service.chunk13 import _qzone_config_section

        return _qzone_config_section(config)

    def test_normalized_config_reaches_the_service_layer(self):
        from plugin.core.service.config import normalize_config

        normalized = normalize_config({'qzone': {'enabled': True, 'daily_post_cap': 5}})
        section = self._section(normalized)
        self.assertIsInstance(section, dict)
        self.assertIs(section['enabled'], True)
        resolved = q.resolve_qzone_config(section)
        self.assertIs(resolved['enabled'], True)
        self.assertEqual(resolved['daily_post_cap'], 5)
        self.assertIs(resolved['auto_feed'], False, '没写 auto_feed 就是关的')

    def test_schema_group_name_matches_what_the_service_reads(self):
        schema = json.loads(
            (pathlib.Path(q.__file__).parent.parent / '_conf_schema.json').read_text(encoding='utf-8-sig')
        )
        self.assertIn('qzone', schema, 'schema 里的分组名必须是 qzone（qzone_compat 是旧兼容位）')
        group = schema['qzone']
        self.assertEqual(group['type'], 'object')
        keys = set(group['items'])
        self.assertEqual(
            keys, set(q.DEFAULT_QZONE_CONFIG),
            'schema 的 qzone 键与 resolve_qzone_config 的键必须一一对应（加了键就要有人读）',
        )
        self.assertIs(group['items']['auto_feed']['default'], False)

    def test_new_group_is_preferred_and_legacy_compat_group_still_reads(self):
        legacy = {'qzone_compat': {'enabled': True, 'daily_post_cap': 2}}
        self.assertIs(q.resolve_qzone_config(self._section(legacy))['enabled'], True, '旧文件仍要能跑')
        self.assertEqual(q.resolve_qzone_config(self._section(legacy))['daily_post_cap'], 2)
        both = {
            'qzone': {'enabled': True, 'daily_post_cap': 7},
            'qzone_compat': {'enabled': False, 'daily_post_cap': 1},
        }
        section = self._section(both)
        self.assertEqual(section['daily_post_cap'], 7, '两个都在时以 qzone 为准')
        camel = {'qzoneCompat': {'enabled': True}}
        self.assertIs(q.resolve_qzone_config(self._section(camel))['enabled'], True, 'camelCase 兜底拼写')
        self.assertIsNone(self._section({}), '两处都没有才算没配')
        self.assertIsNone(self._section(None))

    def test_object_shaped_config_is_read_too(self):
        class _Config:
            qzone = {'enabled': True, 'daily_post_cap': 4}

        self.assertEqual(self._section(_Config())['daily_post_cap'], 4)

    def test_auto_feed_is_a_strict_boolean_outside_the_bounds_table(self):
        for value in (1, 'true', 'yes'):
            with self.subTest(value=value):
                self.assertIs(q.resolve_qzone_config({'auto_feed': value})['auto_feed'], False)
        self.assertIs(q.resolve_qzone_config({'auto_feed': True})['auto_feed'], True)
        self.assertIs(q.resolve_qzone_config({'autoFeed': True})['auto_feed'], True)
        self.assertNotIn('auto_feed', q.QZONE_CONFIG_BOUNDS)
        self.assertIs(q.DEFAULT_QZONE_CONFIG['auto_feed'], False)


class StrategyModuleHygieneTests(unittest.TestCase):
    def test_module_does_not_import_astrbot_and_only_imports_stdlib_or_core(self):
        path = pathlib.Path(q.__file__)
        tree = ast.parse(path.read_text(encoding='utf-8'))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:  # 相对导入（`from .time import …`）= core 内模块
                    continue
                imported.append(node.module or '')
        self.assertNotIn('astrbot', imported)
        for name in imported:
            self.assertFalse(name.startswith('astrbot'), name)
            self.assertTrue(
                name.split('.')[0] in {'__future__', 'json', 'math', 're', 'datetime', 'typing'}
                or name.startswith('.'),
                '只许标准库与 core 内模块：%s' % name,
            )

    def test_required_exports_are_present(self):
        for name in (
            'DEFAULT_QZONE_CONFIG', 'resolve_qzone_config', 'evaluate_qzone_gate',
            'normalize_qzone_msg_entry', 'normalize_qzone_feed_entry', 'fresh_qzone_feeds',
            'qzone_records_for_endpoint', 'QZONE_FEED_APPID_TALK', 'qzone_visibility_label',
            'qzone_intent_from_payload', 'TID_PATTERN', 'qzone_feed_candidates',
            'match_qzone_feed_content', 'QZONE_ACTION_KINDS',
        ):
            with self.subTest(name=name):
                self.assertTrue(hasattr(q, name), name)

    def test_action_kinds_are_exactly_the_write_actions(self):
        # v1.7.1 多了 `forward`（转发按评论类互动计配额）；v1.7.5 多了 `visibility`
        # （改可见范围，按**发帖**配额计，见 `evaluate_qzone_gate`）。
        self.assertEqual(
            q.QZONE_ACTION_KINDS,
            frozenset({'post', 'comment', 'like', 'forward', 'visibility'}),
        )
        self.assertNotIn('feed-seen', q.QZONE_ACTION_KINDS)
        self.assertEqual(q.QZONE_FEED_APPID_TALK, 311)

    def test_visibility_labels_map_onto_the_verified_ugc_right_values(self):
        """五档中文标签 → `ugc_right`：值域与 `qzone_cgi.QZONE_VISIBLE` **同一张表**。

        这是"模型写的枚举"与"打到腾讯的整数"之间的唯一桥梁；对不上就会出现
        "选了仅自己可见、实际发成好友可见"这种隐私事故。
        """
        from plugin.core import qzone_cgi as cgi  # noqa: PLC0415

        self.assertEqual(q.QZONE_VISIBILITY_VALUES, {
            '所有人可见': 1, '仅 QQ 好友可见': 4, '部分人可见': 16,
            '部分人不可见': 128, '仅自己可见': 64,
        })
        self.assertEqual(
            set(q.QZONE_VISIBILITY_VALUES.values()), set(cgi.QZONE_VISIBLE.values()),
        )
        self.assertEqual(cgi.QZONE_VISIBLE_TARGETED, (16, 128))
        # 名单档与 `ugc_right` 的对应关系就是"哪些档要 allow_uins"那一对。
        self.assertEqual(
            {q.QZONE_VISIBILITY_VALUES['部分人可见'], q.QZONE_VISIBILITY_VALUES['部分人不可见']},
            set(cgi.QZONE_VISIBLE_TARGETED),
        )
        for label, value in q.QZONE_VISIBILITY_VALUES.items():
            with self.subTest(label=label):
                self.assertEqual(q.qzone_visible_value(label), value)
                # 审计用的历史标签（上游移植）认同一组值——两边不能各说各话。
                self.assertNotEqual(q.qzone_visibility_label(value), '')
        self.assertIsNone(q.qzone_visible_value('仅好友可见'))
        self.assertIsNone(q.qzone_visible_value(None))

    def test_visibility_never_becomes_a_deferred_intent(self):
        """改可见范围是**回合内即时**动作：不做延迟意图（否则可能出现"三小时后偷偷改"）。"""
        payload = {
            'action': 'visibility', 'tid': 'abcdef', 'ugcRight': 16,
            'targetUins': ['10001'], 'content': '旧正文',
        }
        self.assertIsNone(q.qzone_intent_from_payload(payload))

    def test_the_visibility_quota_comes_from_the_post_cap(self):
        """改可见范围按**发帖**配额算：用满发帖额度时它也一起被拦住。"""
        config = dict(BASE_CONFIG, daily_post_cap=1, daily_comment_cap=6, min_interval_minutes=0)
        used = [record(kind='post', createdAt=NOW)]
        with self.subTest(msg='post 额度用满 → visibility 一起被拦'):
            gate = q.evaluate_qzone_gate(used, config, 'visibility', NOW)
            self.assertIs(gate['allowed'], False)
            self.assertEqual(gate['reason'], 'daily-cap')
            self.assertEqual(gate['cap'], 1, 'visibility 的额度就是发帖那一档')
        # 改过可见性的行**要计入**发帖额度（否则模型能无限次改）。
        rich = dict(config, daily_post_cap=3)
        edited = [record(kind='visibility', createdAt=NOW)]
        self.assertEqual(
            q.evaluate_qzone_gate(edited, rich, 'post', NOW)['used_today'], 1,
            'visibility 审计行必须计进它所属的那档配额',
        )
        self.assertEqual(
            q.evaluate_qzone_gate(edited, rich, 'visibility', NOW)['used_today'], 1,
        )
        # 评论额度是另一档：改可见性不吃它，也不会被它撑大。
        self.assertEqual(
            q.evaluate_qzone_gate(edited, rich, 'comment', NOW)['used_today'], 0,
        )


# --------------------------------------------------------------------------- #
# 服务层宿主（只实现 chunk13 用到的兄弟成员）
# --------------------------------------------------------------------------- #


class _Host(ServiceChunk13):
    """最小宿主：数据库/队列/日志按真实语义 stub，绝不用范围算子。"""

    def __init__(
        self,
        rows: Optional[list[dict[str, Any]]] = None,
        transport: Any = None,
        config: Any = None,
    ) -> None:
        self.config = {'qzone': config if config is not None else {
            'enabled': True, 'daily_post_cap': 3, 'daily_comment_cap': 6,
            'daily_like_cap': 12, 'min_interval_minutes': 90, 'feed_window_minutes': 120,
            'auto_feed': True,
        }}
        self.transport = transport
        self.rows: list[dict[str, Any]] = list(rows or [])
        self.intents: list[dict[str, Any]] = []
        self.entries: list[dict[str, Any]] = []
        self.reports: list[tuple[str, ...]] = []
        self.standalone: list[tuple[str, ...]] = []
        self.access_notes: dict[str, int] = {}
        self.queues: dict[str, Any] = {}
        self.desktop_runtime_phase = 'running'
        self.database_resetting = False
        self.canonical_story: Optional[dict[str, Any]] = dict(STORY)
        self.now_value: datetime = NOW
        self._sequence = 0
        self._qzone_feed_sweep_running = False

    # ---- 兄弟成员桩 ----
    def now(self) -> datetime:
        return self.now_value

    async def db_get(self, table: str, query: Any = None, options: Any = None) -> list[dict[str, Any]]:
        where = query or {}
        for key, value in where.items():
            if isinstance(value, dict) and any(str(name).startswith('$') for name in value):
                raise NotImplementedError('本移植版 db_get 不支持查询算子 %s' % list(value))
        if table == 'interlude_qzone_post':
            return [row for row in self.rows if all(row.get(key) == value for key, value in where.items())]
        if table == 'interlude_intent':
            return [row for row in self.intents if all(row.get(key) == value for key, value in where.items())]
        return []

    async def db_create(self, table: str, data: dict[str, Any]) -> dict[str, Any]:
        self._sequence += 1
        if table == 'interlude_qzone_post':
            row = dict(data)
            row['id'] = self._sequence
            self.rows.append(row)
            return row
        if table == 'interlude_intent':
            row = dict(data)
            row['id'] = self._sequence
            self.intents.append(row)
            return row
        return dict(data)

    async def db_set(self, table: str, where: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
        collection = self.rows if table == 'interlude_qzone_post' else self.intents
        for row in collection:
            if row.get('id') == where.get('id'):
                row.update(patch)
                return row
        return {}

    async def append_entry(self, story_id: str, entry: Any, now: Any, participant_id: str = '') -> dict[str, Any]:
        self._sequence += 1
        stored = dict(entry)
        stored['id'] = self._sequence
        stored['storyId'] = story_id
        self.entries.append(stored)
        return stored

    def report_operation(self, verbosity: str, level: str, story: Any, phase: str, message: str, *args: Any) -> None:
        self.reports.append((verbosity, level, phase, message % args if args else message))

    def report_standalone(self, level: str, message: str, *args: Any, **_kwargs: Any) -> None:
        self.standalone.append((level, message % args if args else message))

    def note_access_skip(self, key: str, interval_ms: int, message: str, *args: Any) -> bool:
        """复用 chunk0 的**真实**节流实现（生产里 MRO 上一定有它）。"""
        return ServiceChunk0.note_access_skip(self, key, interval_ms, message, *args)

    async def get_canonical_story(self, preferred_id: Optional[str] = None) -> Optional[dict[str, Any]]:
        return self.canonical_story

    def can_handle_story(self, story: Any) -> bool:
        return True


class ServiceExecuteTests(unittest.IsolatedAsyncioTestCase):
    def _succeeding_transport(self, sequence: list[int]) -> _StubTransport:
        def handler(action: str, params: dict[str, Any]) -> dict[str, Any]:
            if action == 'send_qzone_msg':
                sequence.append(1)
                return {'ok': True, 'error': '', 'data': {'tid': 't%d' % len(sequence)}}
            return {'ok': True, 'error': '', 'data': {}}

        return _StubTransport(handler)

    async def test_successful_post_writes_confirmed_row_and_script_entry(self):
        host = _Host(config=dict(BASE_CONFIG, daily_post_cap=5))
        pending_at_send: list[str] = []

        def handler(action: str, params: dict[str, Any]) -> dict[str, Any]:
            # 网络调用发生在故事串行队列之外，但 pending 预留行必须已经落库
            pending_at_send.append(host.rows[-1]['status'])
            return {'ok': True, 'error': '', 'data': {'tid': 't1'}}

        transport = _StubTransport(handler)
        host.transport = transport
        result = await host.qzone_execute(STORY, 'post', {'content': '今晚的风很好。', 'ugcRight': 64})
        self.assertEqual(result, {'ok': True, 'tid': 't1', 'error': ''})
        self.assertEqual(transport.calls, [('send_qzone_msg', {'content': '今晚的风很好。', 'ugc_right': 64})])
        self.assertEqual(pending_at_send, ['pending'], '动作发出前 pending 审计行必须先落库')
        self.assertEqual(host.rows[0]['status'], 'confirmed')
        self.assertEqual(host.rows[0]['tid'], 't1')
        self.assertEqual(host.rows[0]['ugcRight'], 64)
        self.assertEqual(host.rows[0]['storyId'], STORY_ID)
        self.assertIsNotNone(host.rows[0]['postedAt'])
        self.assertEqual(len(host.entries), 1)
        entry = host.entries[0]
        self.assertEqual(entry['kind'], 'system')
        self.assertEqual(entry['actor'], 'character')
        self.assertTrue(entry['content'].startswith('[空间动态] '))
        self.assertIn('今晚的风很好。', entry['content'])
        self.assertIn('仅自己可见', entry['content'])
        self.assertEqual(entry['metadata'], {'qzone_kind': 'post', 'tid': 't1', 'ugc_right': 64})
        self.assertEqual(entry['storyId'], STORY_ID)
        self.assertTrue(any('已发表' in item[-1] for item in host.reports), '成功出口要有可见日志')

    async def test_successful_comment_targets_the_post_and_logs(self):
        # 评论**只能**走 QZone CGI（平台侧没有这条动作）：夹具给出 `request_text`。
        transport = _StubTransport(_sweep_handler(), http=lambda *a: '{"code":0}')
        host = _Host(transport=transport)
        result = await host.qzone_execute(
            STORY, 'comment', {'content': '哈哈哈', 'tid': '58a87a00', 'targetUin': '10002'},
        )
        self.assertEqual(result, {'ok': True, 'tid': '58a87a00', 'error': ''})
        self.assertEqual(len(transport.http_calls), 1)
        call = transport.http_calls[0]
        self.assertIn('emotion_cgi_re_feeds', call['url'])
        self.assertEqual(call['data']['topicId'], '58a87a00')
        self.assertEqual(call['data']['content'], '哈哈哈')
        self.assertEqual(call['data']['hostUin'], '10002')
        self.assertEqual(
            [name for name, _ in transport.calls
             if name not in q.QZONE_COOKIE_APIS + ('get_login_info',)],
            [], '一个空间平台动作都不许发出去',
        )
        self.assertEqual(host.rows[0]['status'], 'confirmed')
        self.assertEqual(len(host.entries), 1)
        self.assertIn('[空间动态] 她评论了 QQ 10002的说说：哈哈哈', host.entries[0]['content'])
        self.assertEqual(host.entries[0]['metadata'], {'qzone_kind': 'comment', 'tid': '58a87a00'})

    async def test_like_confirms_without_a_script_entry(self):
        transport = _StubTransport(_sweep_handler(), http=lambda *a: '{"code":0}')
        host = _Host(transport=transport)
        result = await host.qzone_execute(STORY, 'like', {'tid': '58a87a00', 'targetUin': '10002'})
        self.assertEqual(result, {'ok': True, 'tid': '58a87a00', 'error': ''})
        self.assertEqual(len(transport.http_calls), 1)
        self.assertIn('internal_dolike_app', transport.http_calls[0]['url'])
        self.assertEqual(host.rows[0]['status'], 'confirmed')
        self.assertEqual(host.entries, [], '点赞成功不单独进剧本（过细）')
        self.assertTrue(any('点赞已发出' in item[-1] for item in host.reports), '静默的成功出口要补可见日志')

    async def test_non_numeric_target_uin_is_omitted(self):
        transport = _StubTransport(_sweep_handler(), http=lambda *a: '{"code":0}')
        host = _Host(transport=transport)
        await host.qzone_execute(STORY, 'like', {'tid': '58a87a00', 'targetUin': 'not-a-number'})
        call = transport.http_calls[0]
        self.assertIn('internal_dolike_app', call['url'])
        # 非数字的 target_uin 直接**不带这个键**（`_target_uin_param`）：宁可少发一个
        # 归属字段，也不把 `Number(NaN)` 发到腾讯那边去。
        self.assertNotIn('not-a-number', json.dumps(call['data']))

    async def test_disabled_channel_returns_the_upstream_error_without_any_call(self):
        transport = _StubTransport()
        host = _Host(transport=transport, config={'enabled': False})
        result = await host.qzone_execute(STORY, 'post', {'content': 'x'})
        self.assertIs(result['ok'], False)
        self.assertIn('QQ 空间通道未启用', result['error'])
        self.assertEqual(result['tid'], '')
        self.assertEqual(transport.calls, [])
        self.assertEqual(host.rows, [])

    async def test_missing_transport_capability_fails_visibly_without_audit_row(self):
        host = _Host(transport=None)
        result = await host.qzone_execute(STORY, 'post', {'content': 'x'})
        self.assertIs(result['ok'], False)
        self.assertIn('没有可用的 OneBot', result['error'])
        self.assertEqual(host.rows, [])
        self.assertTrue(any(level == 'warn' for level, _ in host.standalone), '缺能力必须 warn（可见）')

    async def test_prefer_self_id_requires_an_exact_registered_story_role_endpoint(self):
        transport = _StubTransport()
        host = _Host(transport=transport)
        host.endpoint_rows = []  # 注册表里没有这个账号

        async def ensure_registry() -> None:
            return None

        host.ensure_endpoint_registry = ensure_registry  # type: ignore[assignment]
        result = await host.qzone_execute(STORY, 'post', {'content': 'x'}, '10002')
        self.assertIs(result['ok'], False)
        self.assertIn('未注册为本故事的角色端点', result['error'])
        self.assertEqual(transport.calls, [], '精确匹配失败绝不切到别的账号')
        self.assertEqual(host.rows, [])

    async def test_registered_endpoint_wins_and_lands_on_the_audit_row(self):
        transport = _StubTransport(lambda a, p: {'ok': True, 'error': '', 'data': {'tid': 't9'}})
        host = _Host(transport=transport)
        host.endpoint_rows = [{
            'id': 'ep-1', 'ownerKind': 'story-role', 'ownerId': STORY_ID,
            'accountKey': 'onebot:10001', 'platform': 'onebot', 'selfId': '10001', 'enabled': True,
        }]

        async def ensure_registry() -> None:
            return None

        host.ensure_endpoint_registry = ensure_registry  # type: ignore[assignment]
        result = await host.qzone_execute(STORY, 'post', {'content': 'x'}, '10001')
        self.assertIs(result['ok'], True)
        self.assertEqual(host.rows[0]['endpointId'], 'ep-1')

    async def test_daily_cap_blocks_with_the_upstream_error_and_visible_log(self):
        transport = _StubTransport()
        host = _Host(transport=transport, rows=[record(kind='post', createdAt=NOW - timedelta(hours=2))],
                     config=dict(BASE_CONFIG, daily_post_cap=1, min_interval_minutes=10))
        result = await host.qzone_execute(STORY, 'post', {'content': 'x'})
        self.assertIs(result['ok'], False)
        self.assertEqual(result['error'], '今日发帖已达上限（1/1）。')
        self.assertEqual(transport.calls, [])
        self.assertEqual(len(host.rows), 1, '被拦下不落新行')
        self.assertTrue(any('限流门拦下' in item[-1] for item in host.reports))

    async def test_min_interval_blocks_with_a_minutes_message(self):
        transport = _StubTransport()
        host = _Host(transport=transport, rows=[record(kind='like', createdAt=NOW - timedelta(minutes=30))])
        result = await host.qzone_execute(STORY, 'post', {'content': 'x'})
        self.assertIs(result['ok'], False)
        self.assertEqual(result['error'], '距离上一条空间动作不足最小间隔（90 分钟）。')
        self.assertEqual(transport.calls, [])

    async def test_transport_timeout_marks_unknown_and_forbids_retry(self):
        transport = _StubTransport(lambda a, p: {'ok': False, 'error': 'request timeout'})
        host = _Host(transport=transport)
        result = await host.qzone_execute(STORY, 'post', {'content': 'x'})
        self.assertIs(result['ok'], False)
        self.assertIn('结果未知', result['error'])
        self.assertEqual(host.rows[0]['status'], 'unknown')
        self.assertEqual(host.entries, [], '结果未知不进剧本')
        self.assertTrue(any(level == 'warn' and '结果未知' in text for level, text in host.standalone))

    async def test_explicit_failure_frame_marks_failed_and_keeps_the_message(self):
        transport = _StubTransport(lambda a, p: {'ok': False, 'error': '操作过于频繁', 'retcode': 1200})
        host = _Host(transport=transport)
        result = await host.qzone_execute(STORY, 'post', {'content': 'x'})
        self.assertIs(result['ok'], False)
        self.assertEqual(result['error'], result['error'].strip())
        self.assertIn('操作过于频繁', result['error'])
        self.assertNotIn('结果未知', result['error'])
        self.assertEqual(host.rows[0]['status'], 'failed')
        self.assertIn('操作过于频繁', host.rows[0]['error'])
        self.assertTrue(any(level == 'warn' and '失败' in text for level, text in host.standalone))

    async def test_error_text_is_truncated_to_500_chars(self):
        transport = _StubTransport(lambda a, p: {'ok': False, 'error': 'x' * 900})
        host = _Host(transport=transport)
        result = await host.qzone_execute(STORY, 'post', {'content': 'x'})
        self.assertEqual(len(host.rows[0]['error']), 500)
        self.assertIs(result['ok'], False)

    async def test_success_frame_without_tid_is_unknown_and_never_retried(self):
        transport = _StubTransport(lambda a, p: {'ok': True, 'error': '', 'data': {}})
        host = _Host(transport=transport)
        result = await host.qzone_execute(STORY, 'post', {'content': 'x'})
        self.assertIs(result['ok'], False)
        self.assertIn('未返回说说 tid', result['error'])
        self.assertIn('不会自动重试', result['error'])
        self.assertEqual(host.rows[0]['status'], 'unknown')
        self.assertEqual(host.rows[0]['error'], '服务端未返回 tid，结果未知。')
        self.assertEqual(host.entries, [])
        self.assertTrue(any('无 tid' in text for _, text in host.standalone))

    async def test_audit_row_only_records_actions_within_the_48h_window(self):
        old = record(kind='post', createdAt=NOW - timedelta(hours=49))
        fresh = record(kind='post', createdAt=NOW - timedelta(hours=3))
        transport = _StubTransport(lambda a, p: {'ok': True, 'error': '', 'data': {'tid': 't1'}})
        host = _Host(transport=transport, rows=[old, fresh], config=dict(BASE_CONFIG, daily_post_cap=2, min_interval_minutes=10))
        await host.qzone_execute(STORY, 'post', {'content': 'x'})
        # 49h 前那行不参与计数，所以这次不会被日上限拦下
        self.assertIs(len(transport.calls), 1)
        recent = await host._qzone_recent_rows(NOW)
        self.assertEqual(
            sorted(row['createdAt'] for row in recent), sorted([fresh['createdAt'], NOW]),
            '48h 窗口内的行（含刚落的 pending 行）都算，49h 前那行不算',
        )

    async def test_concurrent_posts_cannot_both_pass_a_cap_of_one(self):
        """上游同名用例：配额=1 时并发两次发帖必须恰有一次成功。"""
        sequence: list[int] = []
        transport = self._succeeding_transport(sequence)
        host = _Host(transport=transport, config=dict(BASE_CONFIG, daily_post_cap=1, min_interval_minutes=10))
        first, second = await asyncio.gather(
            host.qzone_execute(STORY, 'post', {'content': '第一条'}),
            host.qzone_execute(STORY, 'post', {'content': '第二条'}),
        )
        self.assertNotEqual(first['ok'], second['ok'], '配额=1 时并发两次发帖必须恰有一次成功')
        self.assertEqual(len([row for row in host.rows if row['kind'] == 'post' and row['status'] != 'failed']), 1)
        self.assertEqual(len(sequence), 1)

    async def test_rate_limiter_blocks_stay_out_of_the_action_quota(self):
        """被拦下的动作不落 pending 行（否则限流门会自我加码）。"""
        transport = _StubTransport()
        host = _Host(transport=transport, rows=[record(kind='like', createdAt=NOW - timedelta(minutes=1))])
        for _ in range(3):
            await host.qzone_execute(STORY, 'post', {'content': 'x'})
        self.assertEqual(len(host.rows), 1, '只有既有的那一行')

    async def test_qzone_caller_and_available_follow_the_transport_capability(self):
        self.assertIs(_Host(transport=None).qzone_caller(), False)
        self.assertIs(_Host(transport=object()).qzone_caller(), False, '没有 call_onebot 就不算可用')
        host = _Host(transport=_StubTransport())
        self.assertIs(host.qzone_caller(), True)
        self.assertIs(await host.qzone_available(), True)
        self.assertEqual(host.transport.calls, [('get_qzone_msg_list', {'num': 1})])

    async def test_qzone_available_is_false_when_probe_fails(self):
        host = _Host(transport=_StubTransport(lambda a, p: {'ok': False, 'error': 'not logged in'}))
        self.assertIs(await host.qzone_available(), False)
        raising = _Host(transport=_StubTransport(lambda a, p: RuntimeError('boom')))
        self.assertIs(await raising.qzone_available(), False)
        self.assertIs(await _Host(transport=None).qzone_available(), False)

    async def test_runtime_config_is_read_fresh_every_call(self):
        transport = self._succeeding_transport([])
        host = _Host(transport=transport, config=dict(BASE_CONFIG, daily_post_cap=1, min_interval_minutes=10))
        first = await host.qzone_execute(STORY, 'post', {'content': 'a'})
        self.assertIs(first['ok'], True)
        host.config['qzone']['daily_post_cap'] = 2  # 控制台改配置
        host.now_value = NOW + timedelta(minutes=30)       # 越过最小间隔
        second = await host.qzone_execute(STORY, 'post', {'content': 'b'})
        self.assertIs(second['ok'], True, '改了配置立刻生效（不缓存 qzoneRuntime）')


class ServiceIntentTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_comment_and_like_tids_that_were_never_observed(self):
        executed = 0
        host = _Host(transport=_StubTransport())
        host.intents = [{'id': 9, 'storyId': STORY_ID, 'status': 'pending'}]

        async def stub_execute(story: Any, kind: str, payload: Any = None, prefer: str = '') -> dict[str, Any]:
            nonlocal executed
            executed += 1
            return {'ok': True, 'tid': '', 'error': ''}

        host.qzone_execute = stub_execute  # type: ignore[assignment]
        await host.execute_qzone_intent(
            STORY, {'id': 9, 'payload': {'action': 'like', 'tid': 'deadbeefcafe'}}, NOW,
        )
        self.assertEqual(executed, 0, '未入账的 tid 不得触达执行器')
        self.assertEqual(host.intents[0]['status'], 'completed', '意图仍要完成，防止账本排水被卡')
        self.assertEqual([item['id'] for item in host.intents], [9])
        self.assertTrue(any('target-unknown' in item[-1] for item in host.reports))

    async def test_bad_payload_completes_the_intent_without_touching_the_channel(self):
        for payload in (None, 'junk', {'action': 'share'}, {'action': 'like', 'tid': 'ab'}):
            with self.subTest(payload=payload):
                executed = 0
                host = _Host(transport=_StubTransport())
                host.intents = [{'id': 3, 'status': 'pending'}]

                async def stub_execute(story: Any, kind: str, data: Any = None, prefer: str = '') -> dict[str, Any]:
                    nonlocal executed
                    executed += 1
                    return {'ok': True, 'tid': '', 'error': ''}

                host.qzone_execute = stub_execute  # type: ignore[assignment]
                await host.execute_qzone_intent(STORY, {'id': 3, 'payload': payload}, NOW)
                self.assertEqual(executed, 0)
                self.assertEqual(host.intents[0]['status'], 'completed')
                self.assertTrue(any('payload-invalid' in item[-1] for item in host.reports))
                self.assertEqual(host.standalone, [], '坏 payload 不是 warn 级事故')

    async def test_observed_feed_tid_is_allowed_and_self_post_tid_too(self):
        for known in (
            record(kind='feed-seen', tid='58a87a00', status='confirmed'),
            record(kind='post', tid='58a87a00', status='confirmed'),
        ):
            with self.subTest(kind=known['kind']):
                calls: list[tuple[str, Any, Any, Any]] = []
                host = _Host(rows=[known])
                host.intents = [{'id': 5, 'status': 'pending'}]

                async def stub_execute(story: Any, kind: str, data: Any = None, prefer: str = '') -> dict[str, Any]:
                    calls.append((kind, data, prefer, story['id']))
                    return {'ok': True, 'tid': '58a87a00', 'error': ''}

                host.qzone_execute = stub_execute  # type: ignore[assignment]
                await host.execute_qzone_intent(
                    STORY,
                    {'id': 5, 'payload': {'action': 'comment', 'content': '好', 'tid': '58a87a00', 'targetUin': '10002'}},
                    NOW,
                )
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0][0], 'comment')
                self.assertEqual(calls[0][1], {
                    'content': '好', 'tid': '58a87a00', 'targetUin': '10002', 'ugcRight': None,
                })
                self.assertEqual(calls[0][2], STORY['selfId'], '执行账号按故事角色端点精确匹配')
                self.assertEqual(host.intents[0]['status'], 'completed')

    async def test_failed_action_still_completes_the_intent_with_a_truncated_note(self):
        host = _Host(transport=_StubTransport(lambda a, p: {'ok': False, 'error': '操作过于频繁' * 40}))
        host.intents = [{'id': 7, 'status': 'pending'}]
        await host.execute_qzone_intent(STORY, {'id': 7, 'payload': {'action': 'post', 'content': 'x'}}, NOW)
        self.assertEqual(host.intents[0]['status'], 'completed')
        note = [item[-1] for item in host.reports if 'QQ 空间意图已处理' in item[-1]][0]
        self.assertIn('failed:', note)
        self.assertLessEqual(len(note.split('failed:')[1]), 80)

    async def test_intent_set_failure_is_visible_and_does_not_raise(self):
        host = _Host(transport=_StubTransport())

        async def failing_set(table: str, where: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError('database is locked')

        host.db_set = failing_set  # type: ignore[assignment]
        await host.execute_qzone_intent(STORY, {'id': 7, 'payload': {'action': 'share'}}, NOW)
        self.assertTrue(any('意图完成失败' in text for _, text in host.standalone))
        self.assertTrue(any('payload-invalid' in item[-1] for item in host.reports))


# --------------------------------------------------------------------------- #
# 服务层：好友动态轮询
# --------------------------------------------------------------------------- #


def _feed(key: str, uin: str, minutes_ago: int = 20, appid: int = 311, nickname: str = '') -> dict[str, Any]:
    return {
        'uin': uin, 'nickname': nickname, 'time': NOW - timedelta(minutes=minutes_ago),
        'appid': appid, 'key': key,
    }


#: 编的 cookie（`p_skey` 是算 `g_tk` 的唯一输入）。
SWEEP_COOKIES = 'uin=o010001; skey=@abc; p_skey=pin1n2x3'


def _cgi_feed_text(key: str, uin: str, seconds_ago: int = 1200, appid: int = 311,
                   nickname: str = '') -> str:
    """QZone CGI 的**一页好友动态**：腾讯那页是 `{ver:` 伪分隔的 HTML 串。

    字段用单引号、正文在 `html:'…'` 里——与 `core/qzone_cgi.py::parse_feed_item`
    认的形状逐字一致（时间字段叫 `abstime` 不是 `time`）。
    """
    stamp = int(NOW.timestamp() - seconds_ago)
    return (
        "{ver:1,key:'%s',appid:%d,uin:%s,nickname:'%s',abstime:%d,html:'<div>正文</div>',}"
        % (key, appid, uin, nickname, stamp)
    )


def _cgi_moods_text(rows: list[dict[str, Any]]) -> str:
    """QZone CGI 的说说列表（JSONP，回调名与 `build_mood_list_request` 一致）。"""
    return '_preloadCallback(%s);' % json.dumps({'code': 0, 'msglist': rows}, ensure_ascii=False)


def _raw_msg(tid: str, content: str, seconds_ago: int = 1200) -> dict[str, Any]:
    """CGI 说说列表里的一条（`created_time` 是秒级时间戳）。"""
    return {
        'tid': tid, 'content': content, 'created_time': int(NOW.timestamp() - seconds_ago),
        'cmtnum': 0,
    }


def _sweep_http(feed_text: str, moods_text: str) -> Any:
    """CGI 的两个入口按 URL 分派（读好友动态 / 读某人说说）。"""
    def http(method: str, url: str, headers: object, data: object) -> str:
        if 'feeds3_html_more' in url:
            return feed_text
        if 'emotion_cgi_msglist_v6' in url:
            return moods_text
        return '{"code":0}'

    return http


def _sweep_handler(calls: Optional[list] = None) -> Any:
    """NapCat 侧的 OneBot 直通：认**取登录态**的那几条（两个取凭据接口 + 登录信息）。

    现代 NapCat 两个取凭据接口都有，主路是 `get_credentials`
    （`core/qzone.QZONE_COOKIE_APIS`）；顺序由
    `test_qzone_napcat_channel.CredentialApiOrderTests` 专门钉住。
    """
    def handler(action: str, params: dict) -> dict:
        if calls is not None:
            calls.append((action, dict(params)))
        if action == 'get_credentials':
            return {'ok': True, 'error': '', 'data': {'cookies': SWEEP_COOKIES, 'token': 1869525896}}
        if action == 'get_cookies':
            return {'ok': True, 'error': '', 'data': {'cookies': SWEEP_COOKIES, 'bkn': '1869525896'}}
        if action == 'get_login_info':
            return {'ok': True, 'error': '', 'data': {'user_id': 10001}}
        return {'ok': False, 'error': 'NapCat 没有这个动作'}

    return handler


class ServiceFeedSweepTests(unittest.IsolatedAsyncioTestCase):
    def _sweep_transport(self, feeds: list[str], msgs: Optional[list[dict]] = None) -> _StubTransport:
        """好友动态轮询的传输层：CGI（`request_text`）+ 取登录态（`call_onebot`）。

        v1.7.10 起空间动作**只有 CGI 一条路**，所以夹具必须同时给出这两样；
        没给 `request_text` 的传输层连一条动态都读不到（见
        `test_without_the_cgi_channel_the_sweep_reports_why_and_stays_empty`）。
        """
        return _StubTransport(
            _sweep_handler(), http=_sweep_http(''.join(feeds), _cgi_moods_text(msgs or [])),
        )

    async def test_fresh_feeds_become_entries_and_are_recorded_as_seen(self):
        msgs = [_raw_msg('k1', '今天去吃火锅了'), _raw_msg('other', '另一条')]
        transport = self._sweep_transport([_cgi_feed_text('k1', '10002', nickname='好友A')], msgs)
        host = _Host(transport=transport)
        host.endpoint_rows = [{
            'id': 'ep-1', 'ownerKind': 'story-role', 'ownerId': STORY_ID, 'accountKey': 'onebot:10001',
            'platform': 'onebot', 'selfId': '10001', 'enabled': True,
        }]
        host.endpoint_address_sync = lambda legacy, kind, oid: {  # type: ignore[assignment]
            'platform': 'onebot', 'selfId': '10001', 'endpointId': 'ep-1',
        }
        await host.qzone_feed_sweep()
        self.assertIs(host._qzone_feed_sweep_running, False, '单飞锁必须复位')
        self.assertEqual(len(host.entries), 1)
        entry = host.entries[0]
        self.assertEqual(entry['kind'], 'friend-feed')
        self.assertEqual(entry['actor'], 'system')
        # v1.9.1（§55）：正文到手 → 写成**她的观察**（"她刷到了…"），而不是"插件记账"。
        # 说说自己的发布时间改挂在正文括号里（它不再是条目的 occurredAt，事实不能丢）。
        self.assertEqual(
            entry['content'],
            '[好友动态] 她刷到了 好友A 的说说（发布 %s）：今天去吃火锅了'
            % iso(NOW - timedelta(seconds=1200)),
        )
        self.assertEqual(entry['metadata'], {
            'qzone_feed_key': 'k1', 'qzone_feed_uin': '10002', 'qzone_feed_nickname': '好友A',
            'qzone_feed_time': iso(NOW - timedelta(seconds=1200)),
        })
        # ⚠️ 条目时间是**她刷到动态的时刻**（NOW），不是说说自己的发布时间（§55）：
        # 按故事时间倒序的两个消费方（模型窗口 / 控制台首页）都会把旧时间戳排到后面，
        # 于是"抓到了、正文也有"却哪边都看不见。
        self.assertEqual(entry['occurredAt'], iso(NOW))
        seen = [row for row in host.rows if row['kind'] == 'feed-seen']
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]['tid'], 'k1')
        self.assertEqual(seen[0]['targetUin'], '10002')
        self.assertEqual(seen[0]['status'], 'confirmed')
        self.assertEqual(seen[0]['endpointId'], 'ep-1')
        self.assertIs(seen[0]['storyId'], STORY_ID)
        self.assertTrue(any('好友动态已入账' in item[-1] for item in host.reports))
        # **只走 CGI**：两个读动作都打 QZone 的 HTTP 接口，一个平台动作都没发出去
        # （`comment_qzone` / `get_qzone_feeds` 这些名字在任何后端上都不存在）。
        # 每个 CGI 动作各取一次登录态（`call_qzone_cgi` 不传 `auth` 时自己取）：
        # 一轮轮询 = 两次 `get_credentials` + 两次 `get_login_info`，**零个**空间平台动作。
        self.assertEqual(
            [name for name, _ in transport.calls],
            ['get_credentials', 'get_login_info', 'get_credentials', 'get_login_info'],
        )
        self.assertNotIn('get_qzone_feeds', [name for name, _ in transport.calls])
        self.assertEqual(len(transport.http_calls), 2)
        self.assertIn('feeds3_html_more', transport.http_calls[0]['url'])
        self.assertEqual(transport.http_calls[0]['data']['pagenum'], '1')
        self.assertEqual(transport.http_calls[0]['data']['count'], '20')
        self.assertIn('emotion_cgi_msglist_v6', transport.http_calls[1]['url'])
        self.assertEqual(transport.http_calls[1]['data']['uin'], '10002')
        self.assertEqual(transport.http_calls[1]['data']['num'], '5')

    async def test_content_mismatch_keeps_metadata_only(self):
        """正文只认 tid 精确命中：拉不到就只记"某人发了说说"。

        §55 的硬要求：**没看到就别声称看到**——正文没到手时不许写成
        "她刷到了…：<内容>"，只许说"她刷到了某人的一条说说，但正文没取到"，
        并留一条可行动的 warn。
        """
        transport = self._sweep_transport(
            [_cgi_feed_text('k1', '10002', nickname='好友A')], [_raw_msg('other', '别人的正文')],
        )
        host = _Host(transport=transport)
        await host.qzone_feed_sweep()
        content = host.entries[0]['content']
        self.assertIn('[好友动态] 她刷到了 好友A 的一条说说', content)
        self.assertIn('正文没取到', content)
        self.assertNotIn('别人的正文', content, '对不上 tid 的正文一个字都不许写进去')
        self.assertTrue(any('正文=无' in item[-1] for item in host.reports))
        self.assertTrue(
            any('没看到内容' in message for _level, message in host.standalone),
            '正文没取到是能力缺失，必须有一条可行动的 warn',
        )

    async def test_msg_list_failure_falls_back_to_metadata_only(self):
        """正文那一次 CGI 失败（腾讯间歇抽风）→ 按"只有元数据"入账，不是整轮丢。"""
        def http(method: str, url: str, headers: object, data: object) -> str:
            if 'feeds3_html_more' in url:
                return _cgi_feed_text('k1', '10002', nickname='好友A')
            raise RuntimeError('request timeout')

        host = _Host(transport=_StubTransport(_sweep_handler(), http=http))
        await host.qzone_feed_sweep()
        self.assertEqual(len(host.entries), 1)
        self.assertIn('正文没取到', host.entries[0]['content'])
        self.assertNotIn('她刷到了 好友A 的说说：', host.entries[0]['content'])
        self.assertEqual(len(host.rows), 1, '正文拉不到也要记 feed-seen，避免下轮重复')
        self.assertTrue(
            any('没看到内容' in message for _level, message in host.standalone),
            '异常原文要跟着可见 warn 一起出来（不许静默吞掉）',
        )

    async def test_nickname_is_optional_and_uin_is_the_fallback_owner(self):
        transport = self._sweep_transport([_cgi_feed_text('k1', '10002')])
        host = _Host(transport=transport)
        await host.qzone_feed_sweep()
        self.assertIn('[好友动态] 她刷到了 QQ 10002 的一条说说', host.entries[0]['content'])

    async def test_seen_keys_within_seven_days_are_not_reingested(self):
        rows = [record(kind='feed-seen', tid='k1', status='confirmed', createdAt=NOW - timedelta(days=1))]
        transport = self._sweep_transport([_cgi_feed_text('k1', '10002')])
        host = _Host(transport=transport, rows=rows)
        await host.qzone_feed_sweep()
        self.assertEqual(host.entries, [])
        self.assertEqual(len(host.rows), 1)

    async def test_stale_seen_keys_beyond_seven_days_can_be_seen_again(self):
        rows = [record(kind='feed-seen', tid='k1', status='confirmed', createdAt=NOW - timedelta(days=8))]
        transport = self._sweep_transport([_cgi_feed_text('k1', '10002')])
        host = _Host(transport=transport, rows=rows)
        await host.qzone_feed_sweep()
        self.assertEqual(len(host.entries), 1)

    async def test_at_most_two_candidates_per_round(self):
        feeds = [_cgi_feed_text('k1', '10001'), _cgi_feed_text('k2', '10002'), _cgi_feed_text('k3', '10003')]
        host = _Host(transport=self._sweep_transport(feeds))
        await host.qzone_feed_sweep()
        self.assertEqual(len(host.entries), 2)

    async def test_channel_gates_short_circuit_before_any_call(self):
        transport = self._sweep_transport([_cgi_feed_text('k1', '10002')])
        disabled = _Host(transport=transport, config={'enabled': False})
        await disabled.qzone_feed_sweep()
        self.assertEqual(transport.calls, [])
        paused = _Host(transport=transport)
        paused.desktop_runtime_phase = 'paused'
        await paused.qzone_feed_sweep()
        self.assertEqual(transport.calls, [])
        resetting = _Host(transport=transport)
        resetting.database_resetting = True
        await resetting.qzone_feed_sweep()
        self.assertEqual(transport.calls, [])
        no_transport = _Host(transport=None)
        await no_transport.qzone_feed_sweep()
        self.assertEqual(no_transport.entries, [])

    async def test_no_canonical_story_or_unhandled_story_skips_quietly(self):
        transport = self._sweep_transport([_cgi_feed_text('k1', '10002')])
        without_story = _Host(transport=transport)
        without_story.canonical_story = None
        await without_story.qzone_feed_sweep()
        self.assertEqual(transport.calls, [])
        unhandled = _Host(transport=transport)
        unhandled.can_handle_story = lambda story: False  # type: ignore[assignment]
        await unhandled.qzone_feed_sweep()
        self.assertEqual(transport.calls, [])

    async def test_single_flight_lock_prevents_a_second_concurrent_sweep(self):
        transport = self._sweep_transport([_cgi_feed_text('k1', '10002')])
        host = _Host(transport=transport)
        host._qzone_feed_sweep_running = True
        await host.qzone_feed_sweep()
        self.assertEqual(transport.calls, [], '已在跑就不再进一轮')
        host._qzone_feed_sweep_running = False

    async def test_without_the_cgi_channel_the_sweep_reports_why_and_stays_empty(self):
        """传输层没有原始 HTTP 能力（拿不到 QZone CGI）= **通道缺失**，不是网络抖动。

        这种"每轮都失败且每轮都一样"的原因必须**看得见**（按小时节流一条 warn 说清
        下一步做什么）；早先它会掉进"feeds 间歇失败：静默跳过"，用户只看到
        "开了自动浏览却永远没动静"。反过来，真正的间歇失败仍然静默（下一条用例）。
        """
        host = _Host(transport=_StubTransport(_sweep_handler()))
        await host.qzone_feed_sweep()
        self.assertEqual(host.entries, [])
        warned = [text for level, text in host.standalone if level == 'warn']
        self.assertTrue(warned, '通道缺失必须留下一条可见说明')
        self.assertIn('需要 NapCat 通道', warned[0])
        self.assertIn('get_qzone_feeds', warned[0])
        self.assertEqual(host._qzone_feed_sweep_running, False)

    async def test_an_intermittent_cgi_failure_is_silent_and_the_append_failure_is_a_warn(self):
        def http(method: str, url: str, headers: object, data: object) -> str:
            return None  # 这一轮 CGI 没回执（网络/平台拦截）

        failing = _Host(transport=_StubTransport(_sweep_handler(), http=http))
        await failing.qzone_feed_sweep()
        self.assertEqual(failing.entries, [])
        self.assertEqual(failing.standalone, [], 'CGI 间歇失败静默跳过，下轮再试')
        self.assertEqual(failing._qzone_feed_sweep_running, False)

        broken = _Host(transport=self._sweep_transport([_cgi_feed_text('k1', '10002')]))

        async def failing_append(story_id: str, entry: Any, now: Any, participant_id: str = '') -> dict[str, Any]:
            raise RuntimeError('append failed')

        broken.append_entry = failing_append  # type: ignore[assignment]
        await broken.qzone_feed_sweep()
        self.assertTrue(any(level == 'warn' and '轮询失败' in text for level, text in broken.standalone))
        self.assertEqual(broken._qzone_feed_sweep_running, False)

    async def test_malformed_feed_rows_are_dropped(self):
        transport = self._sweep_transport([
            "{ver:1,appid:311,uin:10002,}",          # 没有 key
            "{ver:1,appid:311,key:'k2',}",           # 没有 uin
            'junk',
            _cgi_feed_text('k3', '10003'),
        ])
        host = _Host(transport=transport)
        await host.qzone_feed_sweep()
        self.assertEqual(len(host.entries), 1)
        self.assertEqual(host.rows[0]['tid'], 'k3')

    async def test_auto_feed_off_skips_the_sweep_and_says_so_once_per_hour(self):
        transport = self._sweep_transport([_cgi_feed_text('k1', '10002')])
        host = _Host(
            transport=transport,
            config=dict(BASE_CONFIG, auto_feed=False),
        )
        self.assertIs(host.qzone_runtime()['auto_feed'], False)
        await host.qzone_feed_sweep()
        self.assertEqual(transport.calls, [], '没开「自动浏览好友动态」就不轮询')
        self.assertEqual(host.entries, [])
        self.assertEqual(len(host.standalone), 1, '必须留一条可见说明，不能静默')
        level, text = host.standalone[0]
        self.assertEqual(level, 'warn')
        self.assertIn('自动浏览好友动态', text)
        # 一小时内的后续轮询不再重复打（节流），过了一小时再打一条
        await host.qzone_feed_sweep()
        self.assertEqual(len(host.standalone), 1)
        host.now_value = NOW + timedelta(hours=1, minutes=1)
        await host.qzone_feed_sweep()
        self.assertEqual(len(host.standalone), 2)
        self.assertEqual(transport.calls, [])
        self.assertIs(host._qzone_feed_sweep_running, False, '被开关拦下不占单飞锁')

    async def test_auto_feed_on_sweeps_and_manual_actions_ignore_it(self):
        transport = self._sweep_transport([_cgi_feed_text('k1', '10002')])
        host = _Host(transport=transport)
        await host.qzone_feed_sweep()
        self.assertEqual(len(host.entries), 1)
        # 手动/意图触发的动作不受 auto_feed 影响
        manual = _Host(
            transport=_StubTransport(lambda a, p: {'ok': True, 'error': '', 'data': {'tid': 't1'}}),
            config=dict(BASE_CONFIG, auto_feed=False),
        )
        result = await manual.qzone_execute(STORY, 'post', {'content': 'x'})
        self.assertIs(result['ok'], True)

    async def test_poll_interval_formula_matches_upstream(self):
        host = _Host(transport=_StubTransport())
        expected = {
            15: 15, 30: 15, 60: 30, 120: 60, 121: 60, 720: 60, 20: 15,
        }
        for window, minutes in expected.items():
            with self.subTest(window=window):
                host.config['qzone']['feed_window_minutes'] = window
                self.assertEqual(host.qzone_feed_poll_minutes(), minutes)


def _feed_text_at(now: datetime, key: str, uin: str, *, nickname: str = '',
                  seconds_ago: int = 600) -> str:
    """相对**给定时刻**的一页好友动态（`_cgi_feed_text` 绑的是模块级 `NOW`）。"""
    return (
        "{ver:1,key:'%s',appid:311,uin:%s,nickname:'%s',abstime:%d,html:'<div>正文</div>',}"
        % (key, uin, nickname, int(now.timestamp() - seconds_ago))
    )


def _moods_text_at(now: datetime, rows: list[dict[str, Any]]) -> str:
    """相对给定时刻的说说列表（JSONP，形状与 `core/qzone_cgi.py` 认的一致）。"""
    return '_preloadCallback(%s);' % json.dumps({'code': 0, 'msglist': rows}, ensure_ascii=False)


def _mood_at(now: datetime, tid: str, content: str, *, seconds_ago: int = 600) -> dict[str, Any]:
    return {
        'tid': tid, 'content': content,
        'created_time': int(now.timestamp() - seconds_ago), 'cmtnum': 0,
    }


class ServiceFeedObservationPipelineTests(unittest.IsolatedAsyncioTestCase):
    """§55：刷到的好友动态必须**真的进到模型 payload**，不是只落库。

    真机症状是"日志说已入账、正文也有，剧本里却完全没有她看到了什么"。这条链有四跳：

    1. **取回**：`chunk13.qzone_feed_sweep` 打 QZone CGI（transport 是唯一替身）；
    2. **落库**：`append_entry`（`chunk5.py`）写 `interlude_script_entry`；
    3. **选择**：`recent_entries_for_prompt`（`chunk1.py:371`）按**故事时间倒序**取窗口；
    4. **组装**：`narrator_prompts.to_prompt_payload` → `recentScript`。

    这里跑的是**真实 `InterludeService`**（真 sqlite + 真实写入方），所以第 2–4 跳一个桩
    都没有；正因如此它才抓得到第 3 跳那个"时间戳用的是说说的发布时间、条目却是现在入账"
    的排序陷阱（§55 的根因）。
    """

    def _service(self, **qzone: Any) -> tuple[Any, Any, datetime]:
        tmp = tempfile.TemporaryDirectory(prefix='hdsi_qzone_feed_')
        self.addCleanup(tmp.cleanup)
        database = Database(':memory:')
        self.addCleanup(database.close)
        database.register_tables()
        config = {'qzone': {
            'enabled': True, 'auto_feed': True, 'feed_window_minutes': 120,
            'daily_post_cap': 3, 'daily_comment_cap': 6, 'daily_like_cap': 12,
            'min_interval_minutes': 90, **qzone,
        }}
        service = InterludeService(
            InterludeContext(base_dir=tmp.name, database=database), config, database, None,
        )
        now = service.now()
        database.insert('interlude_story', {
            'id': STORY['id'], 'platform': 'onebot', 'selfId': '10001', 'userId': '',
            'channelId': '', 'status': 'active',
            'setting': {'timezone': 'Asia/Shanghai'}, 'state': {},
            'cursorAt': now, 'createdAt': now, 'updatedAt': now,
        })
        return service, database, now

    async def _busy_story(self, service: Any, now: datetime, count: int = 55) -> None:
        """先垫一屏**更近**的对话条目：没有它，排序陷阱抓不出来。

        默认 `contextEntryLimit=20` → `recent_entries_for_prompt` 至少取 50 条，
        时间窗 60 分钟。一条"现在入账、时间戳却是 20 分钟前"的动态要挤进这 50 条，
        就得看它跟这些条目的相对顺序——这正是真机上"抓到了却看不到"的现场。
        """
        for index in range(count):
            await service.append_entry(STORY['id'], {
                'kind': 'user-message', 'actor': 'user', 'content': '第 %d 条' % index,
                'occurredAt': iso(now - timedelta(seconds=5 * index)), 'metadata': {},
            }, now)

    #: 动态自身的发布时间：**90 分钟前**。它落在配置的 120 分钟新鲜度窗内（会被收），
    #: 却在 `recent_entries_for_prompt` 的 **60 分钟**时间窗之外——这正是真机那个
    #: "抓到了、正文也有，就是哪边都看不到"的现场（见每个用例的注释）。
    FEED_AGE_SECONDS = 90 * 60

    def _sweep_transport(self, now: datetime, feeds: list[tuple[str, str, str]],
                         moods: list[dict[str, Any]]) -> _StubTransport:
        text = ''.join(
            _feed_text_at(now, key, uin, nickname=nick, seconds_ago=self.FEED_AGE_SECONDS)
            for key, uin, nick in feeds
        )
        body = _moods_text_at(now, [
            dict(row, created_time=int(now.timestamp() - self.FEED_AGE_SECONDS))
            for row in moods
        ])
        return _StubTransport(_sweep_handler(), http=_sweep_http(text, body))

    async def _recent_script(self, service: Any, database: Any, now: datetime) -> list[Any]:
        """第 3+4 跳：选择 → 组装，拿到模型真正看得见的 `recentScript`。"""
        recent = await service.recent_entries_for_prompt(STORY['id'], now)
        story = database.get('interlude_story', {'id': STORY['id']})
        payload = to_prompt_payload({
            'story': story, 'from': now, 'now': now, 'phase': 'advance',
            'recentEntries': recent,
        })
        return payload['relevantEstablishedEpisodes']['recentScript']

    async def test_a_fetched_feed_reaches_the_model_payload_with_its_content(self):
        """**端到端**：两条带正文的动态 → 正文真的出现在给模型的 payload 里。

        这两条动态发布在 90 分钟前（窗内、会被收），而她**现在**才刷到：如果条目的
        时间戳写的是发布时刻，它就会掉出 `recent_entries_for_prompt` 的 50 条窗口与
        60 分钟窗——把 `occurredAt` 改回发布时刻，这条用例当场红（§55 的根因）。
        """
        service, database, now = self._service()
        service.transport = self._sweep_transport(
            now,
            [('k1', '10002', '青屿'), ('k2', '10003', '孤岛')],
            [_mood_at(now, 'k1', '今天天气不错，去公园走了走'),
             _mood_at(now, 'k2', '新买的相机到了')],
        )
        await self._busy_story(service, now)
        await service.qzone_feed_sweep()
        # 落库那一跳：两条都在库里（这一步以前也是对的）
        stored = [row for row in database.all('interlude_script_entry', {'storyId': STORY['id']})
                  if row['kind'] == 'friend-feed']
        self.assertEqual(len(stored), 2)
        # 选择 + 组装那两跳：正文必须原样进 payload，而且写成"她看到了"
        script = await self._recent_script(service, database, now)
        feed_items = [item for item in script if item['kind'] == 'friend-feed']
        self.assertEqual(len(feed_items), 2, '被挤掉的动态 = 她压根不知道刷到过什么')
        contents = [item['content'] for item in feed_items]
        self.assertTrue(any('青屿' in text and '今天天气不错，去公园走了走' in text
                            for text in contents), contents)
        self.assertTrue(any('孤岛' in text and '新买的相机到了' in text
                            for text in contents), contents)
        self.assertTrue(all('她刷到了' in text for text in contents), contents)

    async def test_the_model_payload_never_claims_she_saw_content_that_never_arrived(self):
        """反向：正文对不上 tid → payload 里只有"有这条说说"，一个字的正文都不许有。"""
        service, database, now = self._service()
        warned: list[str] = []
        service.note_access_skip = lambda key, interval, message, *args, **kwargs: (  # type: ignore[assignment]
            warned.append(message % args if args else message) or True
        )
        service.transport = self._sweep_transport(
            now, [('k1', '10002', '青屿')], [_mood_at(now, 'other', '别人的正文')],
        )
        await self._busy_story(service, now)
        await service.qzone_feed_sweep()
        script = await self._recent_script(service, database, now)
        feed_items = [item for item in script if item['kind'] == 'friend-feed']
        self.assertEqual(len(feed_items), 1)
        content = feed_items[0]['content']
        self.assertIn('她刷到了 青屿 的一条说说', content)
        self.assertIn('正文没取到', content)
        self.assertNotIn('别人的正文', content, '没看到就别声称看到')
        self.assertNotIn('：', content.split('正文没取到')[0], '没有正文就不许出现冒号正文段')
        self.assertTrue(warned, '能力缺失要留一条可行动 warn')
        self.assertIn('没看到内容', warned[0])
        self.assertIn('下一步', warned[0])

    async def test_the_feed_entry_is_not_buried_behind_its_publish_time(self):
        """排序陷阱的**反向**用例：条目时间必须是"她刷到的时刻"。

        如果把它写回说说的发布时间（90 分钟前），在 55 条更近的条目之后它既出不了
        50 条窗口、也进不了 60 分钟窗——上面那条端到端用例当场红。这里把判据本身钉住，
        失败时的输出直接指出是时间戳错了（而不是让人去猜排序）。
        """
        service, database, now = self._service()
        service.transport = self._sweep_transport(
            now, [('k1', '10002', '青屿')], [_mood_at(now, 'k1', '正文')],
        )
        await self._busy_story(service, now)
        await service.qzone_feed_sweep()
        row = [item for item in database.all('interlude_script_entry', {'storyId': STORY['id']})
               if item['kind'] == 'friend-feed'][0]
        occurred = parse_dt(row['occurredAt'])
        self.assertLess(abs((occurred - now).total_seconds()), 5,
                        '条目时间 = 她刷到动态的时刻（不是说说的发布时间）')
        self.assertGreater(occurred, parse_dt(row['metadata']['qzone_feed_time']),
                           '刷到的时刻必须晚于说说发布时间')
        self.assertEqual(
            row['metadata']['qzone_feed_time'],
            iso((now - timedelta(seconds=self.FEED_AGE_SECONDS)).replace(microsecond=0)),
            '说说自己的时间进 metadata',
        )


if __name__ == '__main__':
    unittest.main()
