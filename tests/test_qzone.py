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
import base64
import json
import pathlib
import shutil
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

    def test_msg_entry_reads_the_raw_cgi_spellings_for_images_and_forwards(self):
        """§70：归一化层必须认得**原始 QZone CGI** 的拼写。

        上游那份是照 SnowLuma 的字段表写的（`time` / `images`），而真机走 CGI
        （`created_time` / `pic` / `rt_con` / `rt_tid`）。只认一种拼写的后果不是报错，
        而是"一条只有图片的转发说说"在这里被削成"没有正文的空条目"。
        """
        entry = q.normalize_qzone_msg_entry({
            'tid': 'k1', 'content': '', 'created_time': 1_760_000_000,
            'pic': [{'url1': 'https://example.invalid/a.jpg', 'width': 1, 'height': 1}],
            'rt_tid': 'ORIG-1', 'rt_uin': '10009', 'rt_con': {'content': '原说说正文'},
        })
        self.assertEqual(entry['time'].timestamp() * 1000, 1_760_000_000_000)
        self.assertEqual(entry['images'], ['https://example.invalid/a.jpg'])
        self.assertEqual(entry['forward'], {'content': '原说说正文', 'tid': 'ORIG-1'})
        # 两种拼写混在一行也要都能用（判据只有这一处，别处不许再各读一份）
        mixed = q.normalize_qzone_msg_entry({
            'tid': 't', 'images': ['s1'], 'pic': [{'url1': 'c1'}], 'rt_content': '扁平原文',
        })
        self.assertEqual(mixed['images'], ['s1', 'c1'])
        self.assertEqual(mixed['forward']['content'], '扁平原文')
        # 不是转发 → None（不是空字典）；图片仍然封顶 9 个
        plain = q.normalize_qzone_msg_entry({'tid': 't', 'pic': [{'url1': 'u%d' % i} for i in range(20)]})
        self.assertIsNone(plain['forward'])
        self.assertEqual(len(plain['images']), 9)

    def test_msg_entry_reads_the_raw_cgi_video_field(self):
        """`video`（v1.9.6）：与 `pic` 同一条纪律——**不读它，发视频的说说在这里就只剩正文**。

        形状取自参考实现 `qzone_api-1.1.0/qzone_api/utils/html_parser.py::parse_feed_data`
        对 `msg['video']` 的处理（`url3` 是播放直链、`url1` 是封面），也就是
        `qzone_cgi.parse_mood` 已经解析出来的那六个字段（v1.7.9）。
        封面 `url1` **不进** `videos`（那是图片，混进来会被当成"还有一张图"）。
        """
        from plugin.core.qzone_cgi import parse_mood  # noqa: PLC0415

        raw = {
            'tid': 'k1', 'content': '看这个', 'created_time': 1_760_000_000,
            'video': [{
                'url3': 'https://video.invalid/v.mp4', 'url1': 'https://video.invalid/c.jpg',
                'video_id': 'VID-1', 'video_time': '12345',
            }],
        }
        # 真链路：CGI 原始回执 → parse_mood → normalize（夹具造生产真的写的东西）
        entry = q.normalize_qzone_msg_entry(parse_mood(raw))
        self.assertEqual(entry['videos'], ['https://video.invalid/v.mp4'])
        self.assertEqual(entry['images'], [], '封面是图片不是视频，但也不在 pic 里')
        # 扁平回执的拼写（`videos`，元素直接是 URL 字符串）也要认
        flat = q.normalize_qzone_msg_entry({'tid': 't', 'videos': ['https://v/1.mp4']})
        self.assertEqual(flat['videos'], ['https://v/1.mp4'])
        # 坏形状一律收成空表（不是列表 / 元素不是 dict 都不许抛）
        for value in (None, 'x', 3, [None, 7, {}]):
            with self.subTest(value=value):
                self.assertEqual(
                    q.normalize_qzone_msg_entry({'tid': 't', 'video': value})['videos'], [],
                )
        # 封顶 3 段（一条说说装不下更多；多出来只是重复抽帧）
        many = q.normalize_qzone_msg_entry(
            {'tid': 't', 'video': [{'url3': 'https://v/%d.mp4' % i} for i in range(5)]},
        )
        self.assertEqual(len(many['videos']), 3)

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

    def test_lookup_separates_a_missing_tid_from_a_post_without_text(self):
        """§70 的**唯一判据**：`found=False`（列表里没有这条 tid）与"命中了、只是这条
        说说本身没有文字"必须分开。

        上游那个只回字符串的版本把两者都压成 `''`，真机上一条只有图片的转发动态因此
        被报成「说说不含这条 tid」——用户照着去查 NapCat 登录态（完全查错了方向）。
        """
        feed = {'uin': '10001', 'nickname': 'a', 'time': NOW, 'appid': 311, 'key': 'feedkey'}
        self.assertEqual(
            q.qzone_feed_content_lookup([], feed),
            {'found': False, 'content': '', 'images': [], 'videos': [], 'forward': None},
        )
        # key 为空 → 一样算"没对上"（不许把空 key 当成命中）
        self.assertIs(q.qzone_feed_content_lookup([self._msg('', '', 0)], {'key': ''})['found'], False)
        matched = q.qzone_feed_content_lookup([self._msg('feedkey', '', -1)], feed)
        self.assertIs(matched['found'], True)
        self.assertEqual(matched['content'], '')
        forwarded = q.qzone_feed_content_lookup([dict(
            self._msg('feedkey', '', -1), forward={'content': '原说说', 'tid': 'ORIG-1'},
        )], feed)
        self.assertIs(forwarded['found'], True)
        self.assertEqual(forwarded['forward']['content'], '原说说')
        # 兼容外壳回**取得到的那段文字**（自己的正文优先，其次转发原文）
        self.assertEqual(q.match_qzone_feed_content(
            [dict(self._msg('feedkey', '', -1), forward={'content': '原说说', 'tid': 'O'})], feed,
        ), '原说说')

    @staticmethod
    def _msg(tid: str, content: str, minutes_ago: float) -> dict[str, Any]:
        return {
            'tid': tid, 'content': content, 'time': NOW + timedelta(minutes=minutes_ago),
            'comment_num': 0, 'is_private': False, 'images': [],
        }


class ReactionDeltaTests(unittest.TestCase):
    """被评论 / 被点赞感知的纯函数层。

    上游对端：`upstream/src/qzone.ts:203`（`qzoneReactionDeltas`）+
    `upstream/test/qzone.test.ts:256`（五态：首次立基线 / 增量>0 / 回落下修 / 缺席不动 /
    空 tid）。上游只算评论（注释逐字写着「赞数上游尚未暴露字段」）；本移植版按 rc33
    「被赞也要知道」把**回执里本来就有**的赞数一并比对——多出来的用例在下面标了 `[本移植版]`。
    键名按本仓库 Python 内部约定用 snake_case（上游 `contentExcerpt` → `content_excerpt`），
    `previous` / `current` 与上游逐字同名，方便逐条对账。
    """

    ENTRIES = [
        {'tid': 'a', 'content': '今天的晚霞', 'comment_num': 3},
        {'tid': 'b', 'content': '考试结束啦', 'comment_num': 1},
    ]

    def test_upstream_five_states(self):
        entries = self.ENTRIES
        # ① 首次观测：只立基线，零感知（新帖自带评论是常态）
        first = q.qzone_reaction_deltas(
            [record(tid='a')], entries,
        )
        self.assertEqual(len(first['deltas']), 0)
        self.assertEqual(first['baselines'], [
            {'tid': 'a', 'comment_num': 3, 'like_num': None},
        ])
        # ② 增量：a 3→5 报 2 条；b 首次立基线不报
        second = q.qzone_reaction_deltas(
            [record(tid='a', commentNum=3), record(tid='b')],
            [dict(entries[0], comment_num=5), entries[1]],
        )
        self.assertEqual(len(second['deltas']), 1)
        self.assertEqual(second['deltas'][0]['previous'], 3)
        self.assertEqual(second['deltas'][0]['current'], 5)
        self.assertEqual(second['deltas'][0]['content_excerpt'], '今天的晚霞')
        self.assertEqual([item['tid'] for item in second['baselines']], ['a', 'b'])
        # ③ 回落（删评）：5→4 静默下修基线，不产出
        third = q.qzone_reaction_deltas([record(tid='a', commentNum=5)], [dict(entries[0], comment_num=4)])
        self.assertEqual(len(third['deltas']), 0)
        self.assertEqual(third['baselines'], [{'tid': 'a', 'comment_num': 4, 'like_num': None}])
        # ④ 帖子不在拉取列表：基线保持、不产出、不写 baselines
        absent = q.qzone_reaction_deltas([record(tid='zzz', commentNum=2)], entries)
        self.assertEqual(len(absent['deltas']), 0)
        self.assertEqual(len(absent['baselines']), 0)
        # ⑤ 空 tid 不参与
        empty = q.qzone_reaction_deltas([record(tid=' ')], entries)
        self.assertEqual(len(empty['baselines']), 0)

    def test_like_increment_is_reported_alongside_comments(self):
        """[本移植版] 赞数增量：评论 3→5、赞 2→6 → 一条 delta 两件事都在。"""
        result = q.qzone_reaction_deltas(
            [record(tid='a', commentNum=3, likeNum=2)],
            [dict(self.ENTRIES[0], comment_num=5, like_num=6)],
        )
        delta = result['deltas'][0]
        self.assertEqual((delta['previous'], delta['current']), (3, 5))
        self.assertEqual((delta['like_previous'], delta['like_current']), (2, 6))
        self.assertEqual(result['baselines'][0]['like_num'], 6)

    def test_a_missing_like_count_never_becomes_a_ghost_increment(self):
        """[本移植版] **反向**：回执里没有赞数（`like_num` 缺失）时不许按 0 比。

        若把 `None` 当 0，一条"基线 2 个赞、这轮回执没说"的说说会永远算出 `2 > 0`
        的幽灵增量——每轮都报一次"收到了 2 个新赞"。
        """
        result = q.qzone_reaction_deltas(
            [record(tid='a', commentNum=3, likeNum=2)],
            [dict(self.ENTRIES[0], comment_num=3, like_num=None)],
        )
        self.assertEqual(result['deltas'], [], '没有可比的赞数就不产出')
        self.assertIsNone(result['baselines'][0]['like_num'])

    def test_like_only_post_still_produces_a_delta(self):
        """[本移植版] 只涨赞不涨评论也要产出（评论部分留 `None`）。"""
        result = q.qzone_reaction_deltas(
            [record(tid='a', commentNum=3, likeNum=1)],
            [dict(self.ENTRIES[0], comment_num=3, like_num=4)],
        )
        delta = result['deltas'][0]
        self.assertIsNone(delta['previous'])
        self.assertIsNone(delta['current'])
        self.assertEqual((delta['like_previous'], delta['like_current']), (1, 4))


class OnebotTargetIdTests(unittest.TestCase):
    """OneBot 多通道账号标识校验（rc33，上游 `src/service.ts:9719`）。"""

    def test_digits_become_numbers_and_other_accounts_stay_strings(self):
        from plugin.core.service.chunk13 import onebot_target_id
        self.assertEqual(onebot_target_id('10002'), 10002)
        self.assertEqual(onebot_target_id('10002'), 10002)
        self.assertEqual(onebot_target_id(10002), 10002)
        self.assertEqual(onebot_target_id('wxid_abc123'), 'wxid_abc123')
        self.assertEqual(onebot_target_id('12345@chatroom'), '12345@chatroom')
        # 上游 `/^(?:private:|group:)/` 前缀先剥掉再判类型
        self.assertEqual(onebot_target_id('private:10002'), 10002)
        self.assertEqual(onebot_target_id('group:10002'), 10002)

    def test_qzone_read_channel_refuses_non_qq_accounts(self):
        """**反向**：认不出数字 QQ 号时返回 `None` + 原因，绝不"省掉键去查自己"。"""
        from plugin.core.service.chunk13 import _qzone_target_uin
        self.assertEqual(_qzone_target_uin('10002'), (10002, ''))
        self.assertEqual(_qzone_target_uin('wxid_abc'), (None, 'non-qq'))
        self.assertEqual(_qzone_target_uin(''), (None, 'missing'))
        self.assertEqual(_qzone_target_uin(None), (None, 'missing'))


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
            'match_qzone_feed_content', 'qzone_feed_content_lookup', 'QZONE_ACTION_KINDS',
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

    def report(self, level: str, story: Any, phase: str, message: str, *args: Any) -> None:
        """`chunk9.report`（生产 MRO 上一定有它）的最小替身：只记录、不格式化。"""
        self.reports.append((level, phase, message % args if args else message))

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


def _raw_msg(tid: str, content: str, seconds_ago: int = 1200, **extra: Any) -> dict[str, Any]:
    """CGI 说说列表里的一条（`created_time` 是秒级时间戳）。

    `extra` 用来补**真实回执里本来就有的**字段：图片在 `pic[]`，转发链在
    `rt_con` / `rt_tid`（依据见 `docs/PORTING_NOTES.md` §70，不是我们自己编的形状）。
    """
    return dict({
        'tid': tid, 'content': content, 'created_time': int(NOW.timestamp() - seconds_ago),
        'cmtnum': 0,
    }, **extra)


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
        warned = [text for level, text in host.standalone if level == 'warn']
        self.assertTrue(
            any('没看到内容' in message for message in warned),
            '异常原文要跟着可见 warn 一起出来（不许静默吞掉）',
        )
        self.assertTrue(any('request timeout' in message for message in warned),
                        'warn 里要带上真实原因，别只说"没取到"')

    async def test_nickname_is_optional_and_uin_is_the_fallback_owner(self):
        transport = self._sweep_transport([_cgi_feed_text('k1', '10002')])
        host = _Host(transport=transport)
        await host.qzone_feed_sweep()
        self.assertIn('[好友动态] 她刷到了 QQ 10002 的一条说说', host.entries[0]['content'])

    async def test_a_forwarded_feed_takes_the_original_content(self):
        """② 转发动态：转发链上有原内容 → **原内容进事实**，并标明"这是一条转发"。

        反向：把转发链的读取去掉（归一化只认 `images`、不看 `rt_con`/`rt_tid`），
        这条当场红——转发的人自己没有附言，只剩"只有图片 / 原内容没取到"。
        """
        mood = _raw_msg('k1', '', **{
            'rt_tid': 'ORIG-1', 'rt_uin': '10009', 'rt_uinname': '广告墙',
            'rt_con': {'content': '广告原文'},
        })
        host = _Host(transport=self._sweep_transport(
            [_cgi_feed_text('k1', '10002', nickname='好友A')], [mood],
        ))
        await host.qzone_feed_sweep()
        content = host.entries[0]['content']
        self.assertIn('[好友动态] 她刷到了 好友A 转发的说说', content)
        self.assertIn('广告原文', content)
        self.assertNotIn('正文没取到', content)
        self.assertEqual(host.entries[0]['metadata']['qzone_feed_forward_tid'], 'ORIG-1')
        self.assertEqual(
            [text for level, text in host.standalone if level == 'warn'], [],
            '转发原文取到了 = 不是故障，一条 warn 都不该有',
        )
        self.assertTrue(any('正文=有' in item[-1] for item in host.reports))

    async def test_an_image_only_feed_is_a_fact_not_a_missing_content(self):
        """③ 只有图片的说说 → 记「只有图片」这条事实，**不许**说"取不到正文"，**没有 warn**。

        反向：把它也当"取不到正文"处理（`facts['found']` 那一档走 warn 分支），这条当场红。
        """
        mood = _raw_msg('k1', '', **{
            'pic': [{'url1': 'https://example.invalid/ad.jpg', 'width': 1, 'height': 1}],
        })
        host = _Host(transport=self._sweep_transport(
            [_cgi_feed_text('k1', '10002', nickname='好友A')], [mood],
        ))
        await host.qzone_feed_sweep()
        content = host.entries[0]['content']
        self.assertIn('[好友动态] 她刷到了 好友A 的一条说说', content)
        self.assertIn('只有图片', content)
        self.assertNotIn('正文没取到', content, '这条本来就没有文字，不是"没取到"')
        self.assertEqual(
            [text for level, text in host.standalone if level == 'warn'], [],
            '一条只有图片的说说不是故障：不许刷 warn（真机就是被这句引去查登录态的）',
        )
        self.assertEqual(
            host.entries[0]['metadata']['qzone_feed_images'], ['https://example.invalid/ad.jpg'],
            '图片引用要留在条目上（同一个 metadata 槽，没另造媒体通道）',
        )
        self.assertTrue(
            any(level == 'debug' and '没有文字' in text for level, text in host.standalone),
            '不是故障也要留一条 debug（事实 + 一条 debug，不刷 warn）',
        )
        self.assertTrue(any('正文=只有图片' in item[-1] for item in host.reports))

    async def test_a_tid_that_is_not_in_the_list_warns_with_the_tid_it_looked_up(self):
        """④ tid 对不上：warn 里必须读得出**查的是哪个 tid、列表里有几条**。"""
        host = _Host(transport=self._sweep_transport(
            [_cgi_feed_text('k1', '10002', nickname='好友A')],
            [_raw_msg('other', '别人的正文'), _raw_msg('other-2', '还有一条')],
        ))
        await host.qzone_feed_sweep()
        self.assertIn('正文没取到', host.entries[0]['content'])
        self.assertNotIn('别人的正文', host.entries[0]['content'], '没看到就别声称看到')
        warned = [text for level, text in host.standalone if level == 'warn']
        self.assertTrue(warned, '列表里没有这条 tid 是能力缺失，必须可见')
        self.assertIn('tid=k1', warned[0])
        self.assertIn('最近 2 条说说里没有它', warned[0])
        self.assertNotIn('qzone_feed_images', host.entries[0]['metadata'])
        self.assertTrue(any('正文=无' in item[-1] for item in host.reports))

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


class ServiceReactionSweepTests(unittest.IsolatedAsyncioTestCase):
    """被评论 / 被点赞感知的**服务层**：上游 `src/service.ts:7432`（`qzoneReactionSweep`）。

    上游对端的行为判据（逐条）：
    * 只读拉取失败 → 基线不动、下轮重试，但必须可见（`src/service.ts:7450`）；
    * 无增量的帖子也推进基线（含首次初始化，`:7456`）；
    * 单轮预算 3、逐条提交、先写基线再写条目、基线失败本轮中止（`:7465-7496`）。
    上游没有服务层测试（只有 `qzone.test.ts:256` 的纯函数层），所以这里是**我们自建**的
    端到端用例，每条都配了反向。
    """

    def _transport(self, msgs: list[dict[str, Any]]) -> _StubTransport:
        return _StubTransport(_sweep_handler(), http=_sweep_http('', _cgi_moods_text(msgs)))

    @staticmethod
    def _post(index: int = 1, **overrides: Any) -> dict[str, Any]:
        row: dict[str, Any] = {
            'id': index, 'storyId': STORY_ID, 'kind': 'post', 'tid': 't%d' % index,
            'status': 'confirmed', 'createdAt': NOW,
        }
        row.update(overrides)
        return row

    async def test_new_comments_become_a_script_entry_even_with_auto_feed_off(self):
        """评论 3→5：入账一条 `[空间动态]`，基线推进到 5。

        `auto_feed=False` 是刻意的：被评论感知读的是**她自己**的说说，
        与"浏览别人动态"那个开关无关——关掉自动浏览她也该知道有人评论了她。
        """
        host = _Host(
            rows=[self._post(commentNum=3)],
            transport=self._transport([_raw_msg('t1', '今天的晚霞', cmtnum=5)]),
            config=dict(BASE_CONFIG, auto_feed=False),
        )
        await host.qzone_feed_sweep()
        entries = [item for item in host.entries if item['kind'] == 'friend-feed']
        self.assertEqual(len(entries), 1, '被评论感知必须入账（与 auto_feed 无关）')
        content = entries[0]['content']
        self.assertIn('[空间动态]', content)
        self.assertIn('今天的晚霞', content)
        self.assertIn('收到了 2 条新评论（累计 5 条）', content)
        self.assertEqual(entries[0]['actor'], 'system')
        self.assertEqual(entries[0]['metadata']['qzone_tid'], 't1')
        self.assertEqual(entries[0]['metadata']['qzone_reactions'],
                         {'previous': 3, 'current': 5, 'like_previous': None, 'like_current': None})
        self.assertEqual(host.rows[0]['commentNum'], 5, '基线要推进（先写基线再写条目）')
        self.assertIs(host._qzone_feed_sweep_running, False)

    async def test_the_first_observation_only_sets_the_baseline(self):
        """**反向**：首次观测（没有 `commentNum`）只立基线、零条目。"""
        host = _Host(
            rows=[self._post()],
            transport=self._transport([_raw_msg('t1', '今天的晚霞', cmtnum=3)]),
        )
        await host.qzone_feed_sweep()
        self.assertEqual([item for item in host.entries if item['kind'] == 'friend-feed'], [])
        self.assertEqual(host.rows[0]['commentNum'], 3, '首次观测必须立基线，否则该帖永久失聪')

    async def test_a_removed_comment_only_lowers_the_baseline(self):
        """**反向**：删评（5→4）静默下修基线，不产出幽灵增量。"""
        host = _Host(
            rows=[self._post(commentNum=5)],
            transport=self._transport([_raw_msg('t1', 'x', cmtnum=4)]),
        )
        await host.qzone_feed_sweep()
        self.assertEqual([item for item in host.entries if item['kind'] == 'friend-feed'], [])
        self.assertEqual(host.rows[0]['commentNum'], 4)

    async def test_the_per_round_budget_leaves_the_fourth_post_unaccounted(self):
        """单轮预算 3：第 4 条**不推进基线**（下轮重新发现），绝不永久丢失。"""
        rows = [self._post(index, commentNum=1) for index in range(1, 5)]
        msgs = [_raw_msg('t%d' % index, '第 %d 条' % index, cmtnum=5) for index in range(1, 5)]
        host = _Host(rows=rows, transport=self._transport(msgs))
        await host.qzone_feed_sweep()
        entries = [item for item in host.entries if item['kind'] == 'friend-feed']
        self.assertEqual(len(entries), 3, '预算 3')
        self.assertEqual([row['commentNum'] for row in host.rows], [5, 5, 5, 1],
                         '第 4 条基线不动 = 下轮还能发现它')

    async def test_a_failing_baseline_write_aborts_the_round(self):
        """**反向**：基线回写失败 → 本轮中止（不写条目），避免"旧基线重算同一增量"重复入账。"""
        host = _Host(
            rows=[self._post(commentNum=3)],
            transport=self._transport([_raw_msg('t1', 'x', cmtnum=5)]),
        )

        async def failing_set(table: str, where: Any, patch: Any) -> dict[str, Any]:
            raise RuntimeError('database is locked')

        host.db_set = failing_set  # type: ignore[assignment]
        await host.qzone_feed_sweep()
        self.assertEqual([item for item in host.entries if item['kind'] == 'friend-feed'], [],
                         '基线没写进去就不许写条目')
        self.assertEqual(host.rows[0]['commentNum'], 3)
        self.assertTrue(any('基线回写失败' in item[-1] for item in host.reports))

    async def test_a_failing_reaction_fetch_is_visible_and_leaves_the_baseline_alone(self):
        """**反向**：被评论列表拉取失败 → 可见 warn、基线不动、下轮重试。"""

        def http(method: str, url: str, headers: object, data: object) -> Any:
            if 'emotion_cgi_msglist_v6' in url:
                return None  # 这一轮说说列表没回执
            return ''

        host = _Host(
            rows=[self._post(commentNum=3)],
            transport=_StubTransport(_sweep_handler(), http=http),
            config=dict(BASE_CONFIG, auto_feed=False),
        )
        await host.qzone_feed_sweep()
        self.assertEqual(host.rows[0]['commentNum'], 3)
        self.assertEqual([item for item in host.entries if item['kind'] == 'friend-feed'], [])
        self.assertTrue(any('被评论列表拉取失败' in item[-1] for item in host.reports),
                        '拉取失败必须可见（此前零日志）')

    async def test_a_like_increment_is_reported_and_baselined(self):
        """被点赞：赞 2→6 与评论 3→5 一条条目里都说清，`likeNum` 一起推进。"""
        host = _Host(
            rows=[self._post(commentNum=3, likeNum=2)],
            transport=self._transport([_raw_msg('t1', '晚霞', cmtnum=5, likecount=6)]),
        )
        await host.qzone_feed_sweep()
        entries = [item for item in host.entries if item['kind'] == 'friend-feed']
        self.assertEqual(len(entries), 1)
        self.assertIn('收到了 2 条新评论（累计 5 条）', entries[0]['content'])
        self.assertIn('收到了 4 个新赞（累计 6 个）', entries[0]['content'])
        self.assertEqual(host.rows[0]['likeNum'], 6)

    async def test_a_channel_without_like_counts_degrades_visibly(self):
        """**反向**：回执没有赞数 → 可见 warn，且**不许**把 `likeNum` 写成 0。

        把"不可知"写成 0 会让下一轮凭空报出"收到了 N 个新赞"（幽灵增量）。
        """
        host = _Host(
            rows=[self._post(commentNum=3, likeNum=2)],
            transport=self._transport([_raw_msg('t1', '晚霞', cmtnum=3)]),
        )
        await host.qzone_feed_sweep()
        self.assertEqual(host.rows[0]['likeNum'], 2, '不可知 ≠ 0：旧基线原样留着，不许被写坏')
        warned = [text for _level, text in host.standalone]
        self.assertTrue(any('点赞' in text and '不可知' in text for text in warned),
                        '能力缺失要留可见说明：%s' % warned)
        self.assertTrue(any('下一步' in text for text in warned), '还要给可行动的下一步')
        # 从没立过基线的那一条：连键都不该出现（不是 0）
        fresh = _Host(
            rows=[self._post(commentNum=3)],
            transport=self._transport([_raw_msg('t1', '晚霞', cmtnum=3)]),
        )
        await fresh.qzone_feed_sweep()
        self.assertNotIn('likeNum', fresh.rows[0])

    async def test_a_non_qq_sweep_account_is_refused_visibly_without_any_read(self):
        """**反向**（rc33 多通道账号标识校验）：轮询账号不是数字 QQ 号 → 不查、可见 warn。

        "省掉 `targetUin`"会让 CGI 返回**我自己**的说说列表——那就是认错账号
        （拿她的帖子去比对别人的评论）。
        """
        host = _Host(
            rows=[self._post(commentNum=3)],
            transport=self._transport([_raw_msg('t1', 'x', cmtnum=5)]),
            config=dict(BASE_CONFIG, auto_feed=False),
        )
        host.canonical_story = dict(STORY, selfId='wxid_abc123')
        await host.qzone_feed_sweep()
        self.assertEqual(host.transport.http_calls, [], '认不出账号就不许发读请求')
        self.assertEqual(host.rows[0]['commentNum'], 3)
        warned = [text for _level, text in host.standalone]
        self.assertTrue(any('不是数字 QQ 号' in text for text in warned), warned)
        self.assertTrue(any('端点注册表' in text for text in warned), '要说清下一步该配什么')

    async def test_a_feed_from_a_non_qq_account_never_queries_our_own_mood_list(self):
        """**反向**（同上，好友动态这条链）：动态的 `uin` 不是数字 QQ 号 → 只留"刷到过"。"""
        # 动态文本里的 `uin` 平时是裸数字；字符串账号要带引号才表达得出来。
        feed_text = (
            "{ver:1,key:'k1',appid:311,uin:'wxid_abc',nickname:'好友',abstime:%d,"
            "html:'<div>正文</div>',}" % int(NOW.timestamp() - 600)
        )
        host = _Host(
            rows=[],
            transport=_StubTransport(
                _sweep_handler(), http=_sweep_http(feed_text, _cgi_moods_text([])),
            ),
        )
        await host.qzone_feed_sweep()
        entries = [item for item in host.entries if item['kind'] == 'friend-feed']
        self.assertEqual(len(entries), 1, '动态本身照常入账（她确实刷到了）')
        self.assertIn('但正文没取到', entries[0]['content'], '认不出账号就不许声称看到了内容')
        moods_calls = [call for call in host.transport.http_calls
                       if 'emotion_cgi_msglist_v6' in call['url']]
        self.assertEqual(moods_calls, [], '认不出账号就不许查列表（查了就是认错账号）')
        self.assertTrue(any('数字 QQ 号' in text for _level, text in host.standalone))


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


def _mood_at(now: datetime, tid: str, content: str, *, seconds_ago: int = 600,
             **extra: Any) -> dict[str, Any]:
    return dict({
        'tid': tid, 'content': content,
        'created_time': int(now.timestamp() - seconds_ago), 'cmtnum': 0,
    }, **extra)


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

    async def test_an_image_only_feed_reaches_the_payload_as_a_fact(self):
        """③ 端到端：只有图片的转发动态进到模型 payload 时说的是「只有图片」/转发事实，
        **不是**"取不到正文"，也没有 warn（§70：这不是故障）。

        反向：把它当"取不到正文"处理 → 这里会读到"正文没取到"且出现 warn。
        """
        service, database, now = self._service()
        warned: list[str] = []
        service.note_access_skip = lambda key, interval, message, *args, **kwargs: (  # type: ignore[assignment]
            warned.append(message % args if args else message) or True
        )
        service.transport = self._sweep_transport(
            now, [('k1', '10002', '青屿')],
            [_mood_at(now, 'k1', '', pic=[{'url1': 'https://example.invalid/ad.jpg'}])],
        )
        await self._busy_story(service, now)
        await service.qzone_feed_sweep()
        stored = [row for row in database.all('interlude_script_entry', {'storyId': STORY['id']})
                  if row['kind'] == 'friend-feed']
        self.assertEqual(len(stored), 1)
        self.assertIn('只有图片', stored[0]['content'])
        self.assertNotIn('正文没取到', stored[0]['content'])
        self.assertEqual(stored[0]['metadata']['qzone_feed_images'],
                         ['https://example.invalid/ad.jpg'])
        script = await self._recent_script(service, database, now)
        feed_items = [item for item in script if item['kind'] == 'friend-feed']
        self.assertEqual(len(feed_items), 1, '只有图片的说说也要进 payload（她确实刷到了）')
        self.assertIn('只有图片', feed_items[0]['content'])
        self.assertEqual(warned, [], '只有图片不是故障：不许刷 warn')

    async def test_a_forwarded_feed_reaches_the_payload_with_the_original_content(self):
        """② 端到端：转发链上的原内容进 payload，并且标明"这是一条转发"。"""
        service, database, now = self._service()
        service.transport = self._sweep_transport(
            now, [('k1', '10002', '青屿')],
            [_mood_at(now, 'k1', '', rt_tid='ORIG-1', rt_uin='10009',
                      rt_con={'content': '转发来的原内容'})],
        )
        await self._busy_story(service, now)
        await service.qzone_feed_sweep()
        script = await self._recent_script(service, database, now)
        feed_items = [item for item in script if item['kind'] == 'friend-feed']
        self.assertEqual(len(feed_items), 1)
        self.assertIn('青屿 转发的说说', feed_items[0]['content'])
        self.assertIn('转发来的原内容', feed_items[0]['content'])


# --------------------------------------------------------------------------- #
# v1.9.6：动态里的图片 / 视频按已配置的模型能力自动识别
# --------------------------------------------------------------------------- #

#: 一张 1×1 的**真** PNG（走的是生产那条 `transport.fetch_image → image_bytes_to_native`
#: 的下载 / 转码路；不编一个假 dict 冒充"图片进了视觉通道"）。
_TINY_PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=='
)


class _RecordingDescriber:
    """识图模型替身：记下**每一批**收到的图片，回一句可断言的观察。"""

    def __init__(self) -> None:
        self.batches: list[list[dict[str, Any]]] = []

    def available(self) -> bool:
        return True

    async def describe_images(self, images: Any, user_text: str = '', detail: str = 'auto',
                              kinds: Any = None) -> list[str]:
        batch = [dict(item) for item in images]
        self.batches.append(batch)
        return ['第 %d 张：一只橘猫' % index for index in range(1, len(batch) + 1)]


class _MediaTransport(_StubTransport):
    """既有 CGI / OneBot 替身 + **一条** `fetch_image`（动态图片的下载口）。"""

    def __init__(self, handler: Any = None, http: Any = None,
                 image: Optional[bytes] = None) -> None:
        super().__init__(handler, http)
        self.image = image
        self.image_urls: list[str] = []

    async def fetch_image(self, url: str) -> Optional[bytes]:
        self.image_urls.append(url)
        return self.image


class ServiceFeedMediaRecognitionTests(unittest.IsolatedAsyncioTestCase):
    """动态媒体识别：**按已配置的能力自动决定做不做**，走既有的两条链（v1.9.6）。

    跑的是**真实 `InterludeService`**：`load_native_images` / `describe_current_images` /
    `collect_video_sources` 全是生产实现，唯一的替身是传输层（CGI + 取图）与识图模型。
    所以这些用例证明的是"图片真的进了既有视觉通道"，而不是"我们调了一个桩"。

    判据只有一处（`chunk13.qzone_feed_media_capability`），读的是既有总开关：
    图片 = `model_center.vision.enabled` + 有可用识图模型；视频 = `model_center.video.enabled`。
    """

    FEED_AGE_SECONDS = 90 * 60
    PIC = 'https://a1.qpic.cn/psc?/V1/photo%d.jpg'

    def _service(self, *, vision: bool = False, video: bool = False,
                 vision_mode: str = 'sidecar', video_mode: str = 'frames',
                 model: Optional[dict[str, Any]] = None,
                 forward: Optional[dict[str, Any]] = None,
                 **qzone: Any) -> tuple[Any, Any, datetime]:
        tmp = tempfile.TemporaryDirectory(prefix='hdsi_qzone_media_')
        self.addCleanup(tmp.cleanup)
        database = Database(':memory:')
        self.addCleanup(database.close)
        database.register_tables()
        config: dict[str, Any] = {
            'qzone': {
                'enabled': True, 'auto_feed': True, 'feed_window_minutes': 120,
                'daily_post_cap': 3, 'daily_comment_cap': 6, 'daily_like_cap': 12,
                'min_interval_minutes': 90, **qzone,
            },
            'model': dict({
                'vision': {'enabled': vision, 'mode': vision_mode, 'max_per_turn': 3},
                'video': {'enabled': video, 'mode': video_mode},
            }, **(model or {})),
            # `forward_message` 只是**夹具默认值**：v1.9.6 起动态那条路一个字都不读它
            # （曾经的 `min(feed_video_cap, max_videos)` 耦合已解开，见 §74）。
            'forward_message': {'max_videos': 1, **(forward or {})},
        }
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

    def _sweep_transport(self, now: datetime, *, feeds: list[tuple[str, str, str]],
                         moods: list[dict[str, Any]], image: Optional[bytes] = None) -> _MediaTransport:
        text = ''.join(
            _feed_text_at(now, key, uin, nickname=nick, seconds_ago=self.FEED_AGE_SECONDS)
            for key, uin, nick in feeds
        )
        body = _moods_text_at(now, [
            dict(row, created_time=int(now.timestamp() - self.FEED_AGE_SECONDS))
            for row in moods
        ])
        return _MediaTransport(
            _sweep_handler(), http=_sweep_http(text, body),
            image=_TINY_PNG if image is None else image,
        )

    def _warnings(self, service: Any) -> list[str]:
        """把节流口换成收集器（生产里它是 logger；`note_access_skip` 就是 warn 的出口）。"""
        warned: list[str] = []
        service.note_access_skip = lambda key, interval, message, *args, **kwargs: (  # type: ignore[assignment]
            warned.append(message % args if args else message) or True
        )
        return warned

    async def _feed_entry(self, service: Any, database: Any) -> dict[str, Any]:
        rows = [row for row in database.all('interlude_script_entry', {'storyId': STORY['id']})
                if row['kind'] == 'friend-feed']
        self.assertEqual(len(rows), 1)
        return rows[0]

    # ---- ① 识图开着 + 动态带图 ------------------------------------------- #

    async def test_images_enter_the_existing_vision_channel_clipped_by_the_cap(self):
        """① 图片理解开着 + 配了识图模型 → 动态里的图片**真进既有视觉通道**。

        "真进"的证据有两处，缺一不可：**取图口收到的是哪几个坐标**（前 2 张，不是 3 张）
        与**识图模型收到几张**（2 张）。上限来自本组的新键（`feed_image_cap`），
        而"每回合图片数上限"（默认 3）在同一处判据里跟着生效。

        反向（变异）：把上限写死成常量（`taken = 1` 或 `taken = len(images)`，不读配置），
        这条当场红——取图坐标与识图张数都会跟配置对不上。
        """
        service, database, now = self._service(vision=True, feed_image_cap=2)
        warned = self._warnings(service)
        describer = _RecordingDescriber()
        service.vision_describer = describer
        transport = self._sweep_transport(
            now, feeds=[('k1', '10002', '青屿')],
            moods=[_mood_at(now, 'k1', '今天天气很好', pic=[
                {'url1': self.PIC % index} for index in (1, 2, 3)
            ])],
        )
        service.transport = transport
        await service.qzone_feed_sweep()
        entry = await self._feed_entry(service, database)
        # 取回来的坐标 = 配置的上限（2），顺序与 CGI 给的一致
        self.assertEqual(transport.image_urls, [self.PIC % 1, self.PIC % 2])
        # 识图模型收到的就是这 2 张（走的是生产那条 fetch → 转 native 的路）
        self.assertEqual([len(batch) for batch in describer.batches], [2])
        self.assertTrue(all(item.get('data_uri', '').startswith('data:image/')
                            for item in describer.batches[0]))
        # 观察以事实的形式进条目（她自己"看到"了什么）
        self.assertIn('[图片观察] 第 1 张：一只橘猫', entry['content'])
        self.assertIn('[图片观察] 第 2 张：一只橘猫', entry['content'])
        # 线索可数：一共有 3 张、这次只看了前 2 张
        self.assertIn('[图片×3，单次仅取前 2 张]', entry['content'])
        # 截断要有一条**可行动**的 warn，并且点名是哪一道闸
        self.assertTrue(warned, '截断必须可见（丢的是内容）')
        self.assertIn('单次识别图片上限', warned[0])
        self.assertIn('QQ 空间', warned[0])
        # 三条图片引用照样全部留在条目上（事实不许被上限改写）
        self.assertEqual(
            entry['metadata']['qzone_feed_images'], [self.PIC % index for index in (1, 2, 3)],
        )

    async def test_the_per_turn_budget_can_be_the_binding_gate(self):
        """④（第二道闸）「每回合图片数上限」更小时，warn 点的是**它**——两道闸不许互相遮蔽。

        反向：只按本组的新键算（忽略 `max_per_turn`），这里会取到 9 张、warn 也会指错键，
        于是"改了每回合上限也不生效"这类现场就再也查不出来（用户今天刚被这个坑过一次）。
        """
        service, database, now = self._service(
            vision=True, feed_image_cap=9,
            model={'vision': {'enabled': True, 'mode': 'sidecar', 'max_per_turn': 2}},
        )
        warned = self._warnings(service)
        describer = _RecordingDescriber()
        service.vision_describer = describer
        transport = self._sweep_transport(
            now, feeds=[('k1', '10002', '青屿')],
            moods=[_mood_at(now, 'k1', '出去玩', pic=[
                {'url1': self.PIC % index} for index in range(1, 5)
            ])],
        )
        service.transport = transport
        await service.qzone_feed_sweep()
        entry = await self._feed_entry(service, database)
        self.assertEqual(transport.image_urls, [self.PIC % index for index in (1, 2)])
        self.assertIn('[图片×4，单次仅取前 2 张]', entry['content'])
        self.assertIn('每回合图片数上限', warned[0])
        self.assertIn('图片理解', warned[0])
        self.assertNotIn('单次识别图片上限', warned[0])

    # ---- ② 能力关着 / 没配模型 ------------------------------------------- #

    async def test_images_stay_a_fact_without_any_call_when_vision_is_off(self):
        """② 图片理解**关着** → 不调用、如实标注、**没有 warn**（这不是故障）。

        反向（变异）：把"关着也调用"（去掉能力判据）→ 这条红：取图口会有坐标、
        识图模型会被叫到，而用户明明把开关关了。
        """
        service, database, now = self._service(vision=False)
        warned = self._warnings(service)
        describer = _RecordingDescriber()
        service.vision_describer = describer
        transport = self._sweep_transport(
            now, feeds=[('k1', '10002', '青屿')],
            moods=[_mood_at(now, 'k1', '今天天气很好', pic=[
                {'url1': self.PIC % index} for index in (1, 2, 3)
            ])],
        )
        service.transport = transport
        await service.qzone_feed_sweep()
        entry = await self._feed_entry(service, database)
        self.assertEqual(transport.image_urls, [], '关着就一个坐标都不许取')
        self.assertEqual(describer.batches, [], '关着就一次识图都不许调')
        self.assertIn('[图片×3，未识别]', entry['content'], '如实标注：有几张、没看')
        self.assertEqual(warned, [], '关掉不是故障：一条 warn 都不该有')
        # 动态照常入账（能力缺失不影响"她刷到了什么"这件事）
        seen = [row for row in database.all('interlude_qzone_post', {'storyId': STORY['id']})
                if row.get('kind') == 'feed-seen']
        self.assertEqual(len(seen), 1)
        self.assertEqual(len(entry['metadata']['qzone_feed_images']), 3)

    async def test_vision_on_without_a_vision_model_warns_actionably(self):
        """能力**开着却没配识图模型**：不调用、如实标注，并且有一条可行动的 warn。

        这条与上面那条的区别就是"这不是用户关的，是配漏了"——所以必须可见。
        """
        service, database, now = self._service(vision=True)
        warned = self._warnings(service)
        service.vision_describer = None
        service.transport = self._sweep_transport(
            now, feeds=[('k1', '10002', '青屿')],
            moods=[_mood_at(now, 'k1', '', pic=[{'url1': self.PIC % 1}])],
        )
        await service.qzone_feed_sweep()
        entry = await self._feed_entry(service, database)
        self.assertIn('[图片×1，未识别]', entry['content'])
        self.assertTrue(warned, '配漏了识图模型必须让人看见')
        self.assertIn('识图模型', warned[0])

    async def test_an_image_that_cannot_be_fetched_is_reported_not_silent(self):
        """能力开着、模型也有，但图片**一张都没取回来** → 不许静默。

        （真机现场：QZone 相册 CDN 之外的图片域名会被取图白名单拦下。）
        """
        service, database, now = self._service(vision=True, feed_image_cap=2)
        warned = self._warnings(service)
        service.vision_describer = _RecordingDescriber()
        transport = self._sweep_transport(
            now, feeds=[('k1', '10002', '青屿')],
            moods=[_mood_at(now, 'k1', '', pic=[{'url1': 'https://photo.invalid/x.jpg'}])],
            image=None,
        )
        transport.image = None
        service.transport = transport
        await service.qzone_feed_sweep()
        entry = await self._feed_entry(service, database)
        self.assertIn('[图片×1，未识别]', entry['content'])
        self.assertTrue(warned, '取不回来也是能力缺失，必须可见')
        self.assertIn('没取回来', warned[0])

    # ---- ③ 视频 ----------------------------------------------------------- #

    async def test_videos_run_the_existing_video_chain(self):
        """③ 视频理解开着 + 动态带视频 → 走**既有视频链**（同一个判据）。

        这里让链自己给出确定性的降级（`mode=native`：宿主没有原生视频通路），
        证据是：条目的正文里出现了**视频链自己那句事实**（`[视频：…`）与它的 warn——
        说明这条坐标真的进了 `collect_video_sources`，而不是我们在 chunk13 里另写一套。
        """
        from plugin.core.video_understanding import NATIVE_UNSUPPORTED_REASON  # noqa: PLC0415

        service, database, now = self._service(video=True, video_mode='native')
        warned = self._warnings(service)
        service.transport = self._sweep_transport(
            now, feeds=[('k1', '10002', '青屿')],
            moods=[_mood_at(now, 'k1', '看这个', video=[
                {'url3': 'https://video.invalid/v.mp4', 'url1': 'https://video.invalid/c.jpg',
                 'video_id': 'VID-1'},
            ])],
        )
        await service.qzone_feed_sweep()
        entry = await self._feed_entry(service, database)
        self.assertIn('[视频：', entry['content'])
        self.assertIn(NATIVE_UNSUPPORTED_REASON[:24], entry['content'])
        self.assertEqual(entry['metadata']['qzone_feed_videos'], ['https://video.invalid/v.mp4'])
        self.assertTrue(any('视频' in message for message in warned),
                        '视频链的降级说明必须可见（走的是它自己那条 warn）')

    async def test_video_frames_join_the_same_vision_channel(self):
        """③ 视频链抽出的帧**再进同一条视觉通道**（判据仍是 `capability['images']`）。"""
        from unittest import mock  # noqa: PLC0415

        import plugin.core.video_understanding as video  # noqa: PLC0415

        service, database, now = self._service(vision=True, video=True)
        self._warnings(service)
        describer = _RecordingDescriber()
        service.vision_describer = describer
        service.transport = self._sweep_transport(
            now, feeds=[('k1', '10002', '青屿')],
            moods=[_mood_at(now, 'k1', '看这个', video=[
                {'url3': 'https://video.invalid/v.mp4', 'video_id': 'VID-1'},
            ])],
        )
        frame_dir = tempfile.mkdtemp(prefix='hdsi_qzone_frames_')
        self.addCleanup(lambda: shutil.rmtree(frame_dir, ignore_errors=True))
        frame = pathlib.Path(frame_dir) / 'frame-1.png'
        frame.write_bytes(_TINY_PNG)
        extraction = video.VideoExtraction(frames=(str(frame),), workdir=frame_dir)
        with mock.patch.object(video, 'ffmpeg_available', lambda: True), \
                mock.patch.object(video, 'extract_video', lambda *a, **k: extraction):
            await service.qzone_feed_sweep()
        entry = await self._feed_entry(service, database)
        # 帧走的是同一个视觉通道：识图模型收到了那 1 帧，观察写明来自视频
        self.assertEqual([len(batch) for batch in describer.batches], [1])
        self.assertIn('[视频观察] 第 1 张：一只橘猫', entry['content'])

    async def test_video_frames_without_a_vision_channel_are_reported(self):
        """视频开着、图片理解关着 → 帧**没有通道可去**：丢掉，但要留事实 + 一条可行动 warn。

        依据与群回合那条 `GROUP_FRAMES_NO_CHANNEL_REASON` 同一条尺子：抽帧花了钱却没人看，
        不许静默（否则模型会以为她看见了画面）。
        """
        from unittest import mock  # noqa: PLC0415

        import plugin.core.video_understanding as video  # noqa: PLC0415

        service, database, now = self._service(vision=False, video=True)
        warned = self._warnings(service)
        service.transport = self._sweep_transport(
            now, feeds=[('k1', '10002', '青屿')],
            moods=[_mood_at(now, 'k1', '看这个', video=[
                {'url3': 'https://video.invalid/v.mp4', 'video_id': 'VID-1'},
            ])],
        )
        frame_dir = tempfile.mkdtemp(prefix='hdsi_qzone_frames_')
        self.addCleanup(lambda: shutil.rmtree(frame_dir, ignore_errors=True))
        frame = pathlib.Path(frame_dir) / 'frame-1.png'
        frame.write_bytes(_TINY_PNG)
        extraction = video.VideoExtraction(frames=(str(frame),), workdir=frame_dir)
        with mock.patch.object(video, 'ffmpeg_available', lambda: True), \
                mock.patch.object(video, 'extract_video', lambda *a, **k: extraction):
            await service.qzone_feed_sweep()
        entry = await self._feed_entry(service, database)
        self.assertIn('[图片×1，未识别]', entry['content'])
        self.assertTrue(any('帧没有交给任何模型' in message for message in warned))

    async def test_more_videos_than_the_cap_are_counted_and_warned(self):
        """④ 视频截断：线索含总数与取数 + 一条点名「单次获取视频上限」的节流 warn。

        反向（变异）：把上限写死成 1（不读 `feed_video_cap`）→ 这条红：线索会变成
        "仅取前 1 段"，而这条动态按配置该取 2 段。
        """
        service, database, now = self._service(
            video=True, video_mode='native', feed_video_cap=2,
        )
        warned = self._warnings(service)
        service.transport = self._sweep_transport(
            now, feeds=[('k1', '10002', '青屿')],
            moods=[_mood_at(now, 'k1', '三段', video=[
                {'url3': 'https://video.invalid/a.mp4', 'video_id': 'A'},
                {'url3': 'https://video.invalid/b.mp4', 'video_id': 'B'},
                {'url3': 'https://video.invalid/c.mp4', 'video_id': 'C'},
            ])],
        )
        await service.qzone_feed_sweep()
        entry = await self._feed_entry(service, database)
        self.assertIn('[视频×3，单次仅取前 2 段]', entry['content'])
        self.assertTrue(any('单次获取视频上限' in message for message in warned))
        self.assertFalse(any('单条转发' in message for message in warned))

    async def test_the_video_cap_alone_decides_how_many_segments_are_read(self):
        """① 只调「单次获取视频上限」=3（`forward_message` 一个字不动）→ **真的读 3 段**。

        v1.9.6 解开的耦合：早先有效段数 = `min(feed_video_cap, forward_message.max_videos)`，
        而后者默认 1，于是"只调本组的键"照样只取 1 段。证据是 ffmpeg 真的被叫了 3 次
        （`extract_video` 的调用次数），不是"我们算出来 3 段"。

        反向（变异）：把 `min(..., video_read_budget(self))` 加回去 → 这条红：
        `forward_message.max_videos` 默认 1，只会抽 1 次帧。
        """
        from unittest import mock  # noqa: PLC0415

        import plugin.core.video_understanding as video  # noqa: PLC0415

        service, database, now = self._service(video=True, feed_video_cap=3)
        self._warnings(service)
        service.transport = self._sweep_transport(
            now, feeds=[('k1', '10002', '青屿')],
            moods=[_mood_at(now, 'k1', '三段', video=[
                {'url3': 'https://video.invalid/a.mp4', 'video_id': 'A'},
                {'url3': 'https://video.invalid/b.mp4', 'video_id': 'B'},
                {'url3': 'https://video.invalid/c.mp4', 'video_id': 'C'},
            ])],
        )
        frame_dir = tempfile.mkdtemp(prefix='hdsi_qzone_frames_')
        self.addCleanup(lambda: shutil.rmtree(frame_dir, ignore_errors=True))
        frame = pathlib.Path(frame_dir) / 'frame-1.png'
        frame.write_bytes(_TINY_PNG)
        extraction = video.VideoExtraction(frames=(str(frame),), workdir=frame_dir)
        calls: list[int] = []

        def _extract(*args: Any, **kwargs: Any) -> Any:
            calls.append(1)
            return extraction

        with mock.patch.object(video, 'ffmpeg_available', lambda: True), \
                mock.patch.object(video, 'extract_video', _extract):
            await service.qzone_feed_sweep()
        entry = await self._feed_entry(service, database)
        self.assertEqual(len(calls), 3, '配置 3 段就要真的抽 3 次帧（转发键不再是上限）')
        self.assertNotIn('单次仅取前', entry['content'], '按配置全取了，就不该有截断线索')

    async def test_the_per_turn_vision_budget_can_be_the_binding_gate_for_videos(self):
        """④（第二道闸）「每回合图片数上限」更小时，warn 点的是**它**。

        视频帧与图片共用同一条视觉通道，所以那道预算也是动态视频的上限——但它是
        **每回合视觉预算**，不是合并转发那个键（v1.9.6 起动态这条路不读 `forward_message`）。
        """
        service, database, now = self._service(
            video=True, video_mode='native', feed_video_cap=3,
            model={'vision': {'enabled': False, 'mode': 'sidecar', 'max_per_turn': 1}},
        )
        warned = self._warnings(service)
        service.transport = self._sweep_transport(
            now, feeds=[('k1', '10002', '青屿')],
            moods=[_mood_at(now, 'k1', '三段', video=[
                {'url3': 'https://video.invalid/a.mp4', 'video_id': 'A'},
                {'url3': 'https://video.invalid/b.mp4', 'video_id': 'B'},
                {'url3': 'https://video.invalid/c.mp4', 'video_id': 'C'},
            ])],
        )
        await service.qzone_feed_sweep()
        entry = await self._feed_entry(service, database)
        self.assertIn('[视频×3，单次仅取前 1 段]', entry['content'])
        self.assertTrue(any('每回合图片数上限' in message for message in warned))
        self.assertTrue(any('图片理解' in message for message in warned))
        self.assertFalse(any('单条转发' in message for message in warned))

    async def test_forward_message_keys_never_touch_the_qzone_feed_path(self):
        """④ `forward_message` 段一个字都不读：把它调成 0 也不影响动态。

        合并转发是"聊天记录卡片"的键，与 QQ 空间动态没有任何关系（v1.9.6 解开的
        "逻辑串味"）。这条把两个键都设成 0（转发那一侧的"一段都不读 / 一张都不取"），
        动态照旧按本组上限取满：图片 3 张、视频 3 段。

        反向（变异）：把任一处 `min(..., forward_message.max_*)` 加回去 → 这条红
        （取数会掉到 0 或 1）。
        """
        from unittest import mock  # noqa: PLC0415

        import plugin.core.video_understanding as video  # noqa: PLC0415

        service, database, now = self._service(
            vision=True, video=True, feed_image_cap=3, feed_video_cap=3,
            forward={'max_videos': 0, 'max_images': 0},
        )
        warned = self._warnings(service)
        describer = _RecordingDescriber()
        service.vision_describer = describer
        transport = self._sweep_transport(
            now, feeds=[('k1', '10002', '青屿')],
            moods=[_mood_at(now, 'k1', '全都要', pic=[
                {'url1': self.PIC % index} for index in (1, 2, 3)
            ], video=[
                {'url3': 'https://video.invalid/a.mp4', 'video_id': 'A'},
                {'url3': 'https://video.invalid/b.mp4', 'video_id': 'B'},
                {'url3': 'https://video.invalid/c.mp4', 'video_id': 'C'},
            ])],
        )
        service.transport = transport
        frame_dir = tempfile.mkdtemp(prefix='hdsi_qzone_frames_')
        self.addCleanup(lambda: shutil.rmtree(frame_dir, ignore_errors=True))
        frame = pathlib.Path(frame_dir) / 'frame-1.png'
        frame.write_bytes(_TINY_PNG)
        extraction = video.VideoExtraction(frames=(str(frame),), workdir=frame_dir)
        calls: list[int] = []

        def _extract(*args: Any, **kwargs: Any) -> Any:
            calls.append(1)
            return extraction

        with mock.patch.object(video, 'ffmpeg_available', lambda: True), \
                mock.patch.object(video, 'extract_video', _extract):
            await service.qzone_feed_sweep()
        entry = await self._feed_entry(service, database)
        self.assertEqual(transport.image_urls, [self.PIC % index for index in (1, 2, 3)])
        self.assertEqual(len(calls), 3, '转发键设成 0 也拦不住动态视频（这条路不读它）')
        self.assertNotIn('单次仅取前', entry['content'])
        self.assertFalse(any('单条转发' in message for message in warned))
        self.assertFalse(any('合并转发' in message for message in warned))

    async def test_videos_are_marked_as_unread_when_video_understanding_is_off(self):
        """视频能力关着 → 不调用、如实标注、无 warn（与图片那条同一条尺子）。"""
        service, database, now = self._service(video=False)
        warned = self._warnings(service)
        service.transport = self._sweep_transport(
            now, feeds=[('k1', '10002', '青屿')],
            moods=[_mood_at(now, 'k1', '看这个', video=[
                {'url3': 'https://video.invalid/v.mp4', 'video_id': 'VID-1'},
            ])],
        )
        await service.qzone_feed_sweep()
        entry = await self._feed_entry(service, database)
        self.assertIn('[视频×1，未识别]', entry['content'])
        self.assertEqual(warned, [])
        self.assertEqual(len(entry['metadata']['qzone_feed_videos']), 1)

    # ---- 失败不阻断 / 能力判据一处 --------------------------------------- #

    async def test_a_recognition_failure_never_blocks_the_entry(self):
        """识别这一步出任何岔子，动态**照常入账**，并留一条可见 warn。"""
        service, database, now = self._service(vision=True)
        warned = self._warnings(service)
        service.vision_describer = _RecordingDescriber()
        service.transport = self._sweep_transport(
            now, feeds=[('k1', '10002', '青屿')],
            moods=[_mood_at(now, 'k1', '今天天气很好', pic=[{'url1': self.PIC % 1}])],
        )

        async def boom(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError('识图通道炸了')

        service._qzone_feed_media_lines = boom  # type: ignore[assignment]
        await service.qzone_feed_sweep()
        entry = await self._feed_entry(service, database)
        self.assertIn('今天天气很好', entry['content'])
        self.assertEqual(len(entry['metadata']['qzone_feed_images']), 1)
        self.assertTrue(any('识别失败' in message for message in warned))

    def test_the_capability_judgement_reads_the_existing_switches(self):
        """能力判据只有一处，读的是既有总开关 / 既有判据，**不新增"是否识图"开关**。"""

        class _Stub:
            config = {'model': {'vision': {'enabled': True}, 'video': {'enabled': True}}}
            vision_describer = None

        from plugin.core.service.chunk13 import qzone_feed_media_capability  # noqa: PLC0415

        stub = _Stub()
        # 图片理解开着但没配识图模型 → 图片那一半**不做**，并且说得清为什么
        off = qzone_feed_media_capability(stub)
        self.assertIs(off['images'], False)
        self.assertIn('识图模型', off['image_reason'])
        self.assertIs(off['videos'], True)
        stub.vision_describer = _RecordingDescriber()
        on = qzone_feed_media_capability(stub)
        self.assertIs(on['images'], True)
        self.assertEqual(on['image_reason'], '')
        # 两个开关都关 → 两半都不做
        class _Off:
            config = {'model': {'vision': {'enabled': False}, 'video': {'enabled': False}}}
            vision_describer = _RecordingDescriber()

        both_off = qzone_feed_media_capability(_Off())
        self.assertIs(both_off['images'], False)
        self.assertIs(both_off['videos'], False)
        self.assertIn('图片理解', both_off['image_reason'])
        self.assertIn('视频理解', both_off['video_reason'])


if __name__ == '__main__':
    unittest.main()
