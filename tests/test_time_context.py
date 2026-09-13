"""上游 `upstream/test/time-context.test.ts` 的逐条移植（stdlib `unittest`）。

上游把这 14 个用例放在同一个文件里，但它们分属不同模块：

* `src/time.ts`（本文件负责，已全部跑通）：时区解析、时间上下文、格式化、缓存复用；
* `src/narrator.ts` → `plugin.core.narrator`：`toPromptPayload` 相关；
* `src/service.ts` → `plugin.core.service`：`normalizeDatabaseRow` /
  `extractUserReportedTimes` / `detectLiveScriptTimeOverflow` 相关。

按 docs/PORT_PLAN.md §1 的「模块一一对应」，后两组断言的对象不属于 `core/time.py`。
它们由并行移植任务负责落地；落地前这些用例以 `SkipTest` 显式标记（**断言逐字保留，
不改弱、不删**），落地后自动生效。
"""

from __future__ import annotations

import importlib
import importlib.util
import unittest
from datetime import datetime, timezone

from plugin.core.time import (
    calendar_day_key,
    dt_ms,
    format_log_time,
    format_story_display_time,
    iso,
    local_clock_minutes,
    parse_dt,
    resolve_timezone,
    story_local_time_context,
    time_formatter_cache_size,
    utc_now,
)

_UTC = timezone.utc


def _dt(text: str) -> datetime:
    """上游 `new Date('...Z')`。"""
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _load(name: str, *attrs: str):
    """按需加载并行移植中的同侪模块；未落地时跳过（而非改弱断言）。"""
    try:
        spec = importlib.util.find_spec(f"plugin.core.{name}")
    except (ImportError, AttributeError, ValueError):
        spec = None
    if spec is None:
        raise unittest.SkipTest(f"plugin.core.{name} 由并行移植任务负责，尚未落地：跳过对应用例")
    module = importlib.import_module(f"plugin.core.{name}")
    missing = [attr for attr in attrs if not hasattr(module, attr)]
    if missing:
        raise unittest.SkipTest(f"plugin.core.{name} 暂缺 {', '.join(missing)}：跳过对应用例")
    return module


def _endorsed_clock_minutes(facts) -> set[int]:
    """上游 `new Set(facts.map(fact => Number(fact.localTime.slice(-5, -3)) * 60 + Number(fact.localTime.slice(-2))))`。"""
    return {int(fact["localTime"][-5:-3]) * 60 + int(fact["localTime"][-2:]) for fact in facts}


def _request_at(from_at: datetime, now: datetime) -> dict:
    """上游 `requestAt(from, now)`。"""
    types = _load("types", "empty_story_setting", "empty_story_state")
    setting = types.empty_story_setting()
    setting["timezone"] = "Asia/Shanghai"
    state = types.empty_story_state()
    # 上游存字符串（ISO）的字段继续存字符串（PORT_PLAN §2「时间」）。
    state["lastContinuityUpdateAt"] = iso(from_at)
    story = {
        "id": "story", "platform": "onebot", "selfId": "bot", "userId": "global",
        "channelId": "private:global", "status": "active", "setting": setting, "state": state,
        "cursorAt": from_at, "createdAt": from_at, "updatedAt": now,
    }
    return {
        "phase": "advance", "story": story, "from": from_at, "now": now, "participant": None,
        "participants": [], "shareParticipantDetails": False, "dueIntents": [],
        "activeConsequences": [], "supersededIntents": [], "recentEntries": [], "memories": [],
    }


class TimeContextTests(unittest.TestCase):
    """上游 `time-context.test.ts` 的 14 个用例，顺序与文件一致。"""

    def test_16_00_in_shanghai_is_an_authoritative_daylight_afternoon(self):
        instant = _dt("2026-08-23T08:00:00.000Z")
        local = story_local_time_context(instant, "Asia/Shanghai")
        self.assertEqual(local["local"], "2026-08-23 16:00:00")
        self.assertEqual(local["hour"], 16)
        self.assertEqual(local["period"], "afternoon")
        self.assertEqual(local["periodZh"], "下午")
        self.assertRegex(local["daylightExpectation"], "normally daylight")

    def test_long_intervals_expose_both_endpoint_clocks_and_continuity_age(self):
        narrator = _load("narrator", "to_prompt_payload")
        from_at = _dt("2026-08-22T15:00:00.000Z")  # Shanghai 23:00
        now = _dt("2026-08-23T08:00:00.000Z")  # Shanghai 16:00
        payload = narrator.to_prompt_payload(_request_at(from_at, now))
        interval = payload["authoringWindow"]["interval"]
        self.assertEqual(interval["fromLocal"], "2026-08-22 23:00:00")
        self.assertEqual(interval["nowLocal"], "2026-08-23 16:00:00")
        self.assertEqual(interval["nowLocalContext"]["period"], "afternoon")
        self.assertEqual(interval["elapsedSeconds"], 61_200)
        self.assertEqual(payload["relevantEstablishedEpisodes"]["continuitySnapshotAgeMinutes"], 1_020)

    def test_reload_style_iso_timestamp_rows_are_materialized_as_datetime_objects(self):
        types = _load("types", "empty_story_state")
        service = _load("service", "normalize_database_row")
        normalized = service.normalize_database_row("interlude_story", {
            "id": "story", "state": types.empty_story_state(),
            "cursorAt": "2026-08-23T07:55:00.000Z",
            "createdAt": "2026-08-20T00:00:00.000Z",
            "updatedAt": "2026-08-23T08:00:00.000Z",
        })
        self.assertIsInstance(normalized["cursorAt"], datetime)
        self.assertIsInstance(normalized["createdAt"], datetime)
        self.assertIsInstance(normalized["updatedAt"], datetime)
        self.assertEqual(iso(normalized["cursorAt"]), "2026-08-23T07:55:00.000Z")
        self.assertIsNone(normalized["state"].get("agencyWindow"))

    def test_timezone_formatters_are_reused_instead_of_rebuilt_on_every_turn(self):
        instant = _dt("2026-08-23T08:00:00.000Z")
        story_local_time_context(instant, "Asia/Shanghai")
        format_log_time(instant, "Asia/Shanghai")
        warmed = time_formatter_cache_size()
        for _ in range(100):
            story_local_time_context(instant, "Asia/Shanghai")
            format_log_time(instant, "Asia/Shanghai")
        self.assertEqual(time_formatter_cache_size(), warmed)
        self.assertEqual(story_local_time_context(instant, "Not/A_Timezone")["timezone"], "UTC")

    def test_recent_script_payload_carries_derived_ownership_without_changing_stored_entries(self):
        narrator = _load("narrator", "to_prompt_payload")
        now = _dt("2026-08-23T08:00:00.000Z")
        request = _request_at(now, now)
        request["recentEntries"] = [{
            "id": 1, "storyId": "story", "participantId": "participant", "kind": "script",
            "actor": "narrator", "content": "她觉得这件事有点奇怪，但没有说出口。",
            "occurredAt": now, "metadata": {}, "createdAt": now,
        }]
        payload = narrator.to_prompt_payload(request)
        self.assertEqual(
            payload["relevantEstablishedEpisodes"]["recentScript"][0]["ownership"], "protagonist-narrative")
        self.assertNotIn("ownership", request["recentEntries"][0])

    def test_timeline_display_uses_the_story_timezone_and_prints_its_gmt_offset(self):
        self.assertEqual(
            format_story_display_time(_dt("2026-08-31T00:37:00.000Z"), "Asia/Shanghai"),
            "2026-08-31 08:37:00 GMT+8")

    def test_explicit_user_reported_clocks_stay_distinct_from_the_message_receive_time(self):
        service = _load("service", "extract_user_reported_times")
        facts = service.extract_user_reported_times(
            "我 6.30 开始吃，刚吃完", _dt("2026-08-31T11:36:00.000Z"), "Asia/Shanghai")
        self.assertEqual(facts, [{
            "localTime": "2026-08-31 18:30", "relation": "past", "statement": "我 6.30 开始吃，刚吃完",
        }])

    def test_prompt_payload_keeps_receive_time_and_user_reported_action_time_as_separate_fields(self):
        narrator = _load("narrator", "to_prompt_payload")
        service = _load("service", "extract_user_reported_times")
        now = _dt("2026-08-31T11:36:00.000Z")
        request = _request_at(now, now)
        request["phase"] = "user-message"
        request["userMessage"] = "我 6.30 开始吃，刚吃完"
        request["userReportedTimes"] = service.extract_user_reported_times(
            request["userMessage"], now, "Asia/Shanghai")
        payload = narrator.to_prompt_payload(request)
        self.assertEqual(payload["incomingEvent"]["event"]["observedAtLocal"], "2026-08-31 19:36:00")
        self.assertEqual(payload["incomingEvent"]["event"]["userReportedTimes"], [{
            "localTime": "2026-08-31 18:30", "relation": "past", "statement": "我 6.30 开始吃，刚吃完",
        }])
        self.assertEqual(payload["authoringWindow"]["liveTimeBoundary"]["mustStopAtNow"], True)
        self.assertEqual(payload["authoringWindow"]["liveTimeBoundary"]["nowLocal"], "2026-08-31 19:36:00")

    def test_short_live_windows_reject_explicit_future_clocks_and_multiple_completed_lesson_stages(self):
        service = _load("service", "detect_live_script_time_overflow")
        from_at = _dt("2026-09-01T00:35:00.000Z")  # 08:35 Shanghai
        now = _dt("2026-09-01T00:45:00.000Z")  # 08:45 Shanghai
        self.assertRegex(
            service.detect_live_script_time_overflow(
                "九点二十，她走进下一节课的教室。", "user-message", from_at, now, "Asia/Shanghai") or "",
            "explicit clock")
        self.assertRegex(
            service.detect_live_script_time_overflow(
                "第一节课结束后，她去上第二节数学课。", "user-message", from_at, now, "Asia/Shanghai") or "",
            "multiple lesson stages")
        self.assertIsNone(service.detect_live_script_time_overflow(
            "她继续在第一节课记笔记。", "user-message", from_at, now, "Asia/Shanghai"))
        self.assertIsNone(service.detect_live_script_time_overflow(
            "第一节课结束后，她去上第二节数学课。", "advance", from_at, now, "Asia/Shanghai"))

    def test_midnight_wraparound_and_latency_grace_do_not_drop_legitimate_live_scripts(self):
        service = _load("service", "detect_live_script_time_overflow")
        # now = 00:01（刚过午夜）；"11:58" 是 12 小时制对 23:58（3 分钟前）的写法，不能判未来
        from_at = _dt("2026-09-01T15:56:00.000Z")  # 23:56 Shanghai
        now = _dt("2026-09-01T16:01:00.000Z")  # 00:01 Shanghai (+1 day)
        self.assertIsNone(service.detect_live_script_time_overflow(
            "屏幕亮起，23:58，他的消息停在午夜前三分钟。", "user-message", from_at, now, "Asia/Shanghai"))
        self.assertIsNone(
            service.detect_live_script_time_overflow(
                "11:58 的那条消息还停在屏幕上。", "user-message", from_at, now, "Asia/Shanghai"),
            "12 小时制歧义按刚过去的过去处理")
        # 宽限期内的微小超前（≤5 分钟）不丢弃
        self.assertIsNone(service.detect_live_script_time_overflow(
            "00:05，闹钟该响了，她还没睡。", "user-message", from_at, now, "Asia/Shanghai"))
        # 歧义视野内（<6h）的真实未来时钟仍然拒绝
        self.assertRegex(
            service.detect_live_script_time_overflow(
                "凌晨两点，她终于合上笔记本。", "user-message", from_at, now, "Asia/Shanghai") or "",
            "explicit clock")

    def test_the_current_user_message_remains_both_a_durable_event_and_the_explicit_current_event(self):
        narrator = _load("narrator", "to_prompt_payload")
        now = _dt("2026-08-23T08:00:00.000Z")
        request = _request_at(now, now)
        request["phase"] = "user-message"
        request["userMessage"] = "现在发生的这一条消息"
        request["recentEntries"] = [{
            "id": 2, "storyId": "story", "participantId": "participant", "kind": "user-message",
            "actor": "user", "content": "现在发生的这一条消息", "occurredAt": now, "metadata": {},
            "createdAt": now,
        }]
        payload = narrator.to_prompt_payload(request)
        self.assertEqual(payload["incomingEvent"]["event"]["content"], "现在发生的这一条消息")
        self.assertEqual(
            payload["relevantEstablishedEpisodes"]["recentScript"][0]["content"], "现在发生的这一条消息")
        self.assertEqual(
            payload["relevantEstablishedEpisodes"]["recentScript"][0]["ownership"],
            "user-delivered-message")

    def test_background_agency_payload_includes_relationship_identity_but_not_raw_chat_history(self):
        narrator = _load("narrator", "to_prompt_payload")
        types = _load("types", "empty_participant_state")
        now = _dt("2026-08-24T08:00:00.000Z")
        request = _request_at(now, now)
        request["agencyEnabled"] = True
        request["agencyWindow"] = None
        request["participants"] = [{
            "id": "friend", "storyId": "story", "platform": "onebot", "selfId": "bot",
            "userId": "user", "channelId": "private:user", "personId": "friend", "displayName": "小桃",
            "profile": "主角信任的朋友", "relationship": "关系亲近", "state": types.empty_participant_state(),
            "status": "active", "createdAt": now, "updatedAt": now,
        }]
        payload = narrator.to_prompt_payload(request)
        participant = payload["ongoingThreads"]["participants"][0]
        self.assertEqual(participant["displayName"], "小桃")
        self.assertEqual(participant["relationship"], "关系亲近")
        self.assertEqual(participant["profile"], "主角信任的朋友")
        self.assertEqual(len(payload["relevantEstablishedEpisodes"]["recentScript"]), 0)

    def test_headline_window_semantics_narrative_declarations_block_references_and_user_deadlines_pass(self):
        service = _load("service", "extract_user_reported_times", "detect_live_script_time_overflow")
        from_at = _dt("2026-09-03T23:30:00.000Z")  # 07:30 Shanghai
        now = _dt("2026-09-03T23:47:40.000Z")  # 07:47:40 Shanghai

        def run(message: str, script: str, now_at: datetime | None = None):
            moment = now_at or now
            facts = service.extract_user_reported_times(message, moment, "Asia/Shanghai") if message else []
            return service.detect_live_script_time_overflow(
                script, "user-message", from_at, moment, "Asia/Shanghai", _endorsed_clock_minutes(facts))

        # 2026-09-03/04 的三个真实误杀案，全部必须放行：
        self.assertIsNone(run(
            "。。。我看你怎么在八点赶到万松园",
            "水濑看了一眼时间，还不到七点五十。她想起小桃说的八点赶到万松园的约定，手忙脚乱地收拾书包。"))
        self.assertIsNone(
            run("希望你能在九点前赶到万松园", "她抓起书包冲出门。08:47，她气喘吁吁地赶到了万松园校门口。",
                _dt("2026-09-04T00:05:49.000Z")),
            "用户期限（九点前=540）授权区间内的到达时刻")
        self.assertIsNone(
            run("我们中午一起吃饭吧", "十二点的铃声响了，她收起课本走向食堂。",
                _dt("2026-09-03T03:19:43.000Z")),
            "时段词（中午=12:00）作为背书锚点")
        # 叙事宣告位（开头无计划语义、无背书）仍拦截：
        self.assertRegex(run("", "八点整，她出现在校门口，课已经开始了。") or "", "explicit clock")
        self.assertRegex(run("", "08:20，教室里已经坐满了人。") or "", "explicit clock")
        # 中段钟点不再是证据（引用/回忆区）：
        self.assertIsNone(run("", "她一边刷牙一边想着上午的事。昨天说好今天要早到。"))

    def test_user_endorsed_clocks_from_the_message_exempt_the_guard_while_unendorsed_ones_stay_blocked(self):
        service = _load("service", "extract_user_reported_times", "detect_live_script_time_overflow")
        from_at = _dt("2026-09-03T23:30:00.000Z")  # 07:30 Shanghai
        now = _dt("2026-09-03T23:47:40.000Z")  # 07:47:40 Shanghai
        message = "。。。我看你怎么在八点赶到万松园"
        facts = service.extract_user_reported_times(message, now, "Asia/Shanghai")
        self.assertTrue(
            any(fact["localTime"].endswith("08:00") for fact in facts),
            '中文“八点”必须能被提取为自报时间')
        endorsed = _endorsed_clock_minutes(facts)
        # 2026-09-03 早间的真实死循环：用户说“八点”，模型每次照写，守卫每次丢弃。
        script = "水濑看了一眼时间，还不到七点五十。她想起小桃说的八点赶到万松园的约定，手忙脚乱地收拾书包。"
        self.assertIsNone(service.detect_live_script_time_overflow(
            script, "user-message", from_at, now, "Asia/Shanghai", endorsed))
        # 阿拉伯数字自报与中文剧本写法互通。
        numeric_endorsed = _endorsed_clock_minutes(
            service.extract_user_reported_times("我8点出门", now, "Asia/Shanghai"))
        self.assertIsNone(service.detect_live_script_time_overflow(
            "八点整她出门。", "user-message", from_at, now, "Asia/Shanghai", numeric_endorsed))
        # 未背书的其它未来钟点（数字与中文）仍然拦截；背景回合不受影响。
        self.assertRegex(
            service.detect_live_script_time_overflow(
                "她抬头看了钟：08:40，才慢悠悠出门。", "user-message", from_at, now, "Asia/Shanghai",
                endorsed) or "",
            "explicit clock")
        self.assertRegex(
            service.detect_live_script_time_overflow(
                "八点四十她才出门。", "user-message", from_at, now, "Asia/Shanghai", endorsed) or "",
            "explicit clock")
        self.assertIsNone(service.detect_live_script_time_overflow(
            script, "advance", from_at, now, "Asia/Shanghai"))


class TimeMappingTests(unittest.TestCase):
    """`src/time.ts` 里上游测试未覆盖、但被其它模块依赖的输出形状（照 Intl 实测值固定）。

    唯一的例外是 `format_log_time` 的日期分隔符：ICU 的 zh-CN 实际输出 `08/23 16:00:00`，
    本移植工程按约定固定为 `MM-DD HH:MM:SS`（上游测试只调用不断言其格式，故无冲突）。
    """

    def test_story_context_utc_matches_js_to_iso_string(self):
        local = story_local_time_context(_dt("2026-08-24T08:00:00.000Z"), "Asia/Shanghai")
        self.assertEqual(local["utc"], "2026-08-24T08:00:00.000Z")
        self.assertEqual(local["date"], "2026-08-24")
        self.assertEqual(local["time"], "16:00:00")
        self.assertEqual(local["weekday"], "Monday")

    def test_weekday_and_offset_follow_intl_short_offset(self):
        instant = _dt("2026-08-23T08:00:00.000Z")
        self.assertEqual(story_local_time_context(instant, "Asia/Shanghai")["weekday"], "Sunday")
        self.assertEqual(story_local_time_context(instant, "Asia/Shanghai")["offset"], "GMT+8")
        self.assertEqual(story_local_time_context(instant, "UTC")["offset"], "GMT+0")
        self.assertEqual(story_local_time_context(instant, "Asia/Kolkata")["offset"], "GMT+5:30")
        self.assertEqual(story_local_time_context(instant, "America/New_York")["offset"], "GMT-4")  # EDT
        self.assertEqual(
            story_local_time_context(_dt("2026-01-23T08:00:00.000Z"), "America/New_York")["offset"],
            "GMT-5")  # EST

    def test_period_boundaries_and_daylight_expectation(self):
        expected = {
            "2026-08-23T21:00:00.000Z": ("morning", "上午"),      # 05:00 Shanghai
            "2026-08-24T03:59:00.000Z": ("morning", "上午"),      # 11:59
            "2026-08-24T04:00:00.000Z": ("afternoon", "下午"),    # 12:00
            "2026-08-24T09:59:00.000Z": ("afternoon", "下午"),    # 17:59
            "2026-08-24T10:00:00.000Z": ("evening", "傍晚/晚上"),  # 18:00
            "2026-08-24T13:59:00.000Z": ("evening", "傍晚/晚上"),  # 21:59
            "2026-08-24T14:00:00.000Z": ("night", "夜间"),        # 22:00
            "2026-08-23T20:59:00.000Z": ("night", "夜间"),        # 04:59
        }
        for text, (period, period_zh) in expected.items():
            local = story_local_time_context(_dt(text), "Asia/Shanghai")
            self.assertEqual((local["period"], local["periodZh"]), (period, period_zh), text)
        self.assertRegex(
            story_local_time_context(_dt("2026-08-24T14:00:00.000Z"), "Asia/Shanghai")["daylightExpectation"],
            "normally dark outside")

    def test_resolve_timezone_falls_back_to_utc(self):
        self.assertEqual(resolve_timezone("Asia/Shanghai"), "Asia/Shanghai")
        self.assertEqual(resolve_timezone("Not/A_Timezone"), "UTC")
        self.assertEqual(resolve_timezone(""), "UTC")
        self.assertEqual(resolve_timezone(None), "UTC")
        # 二次调用走 timezoneCache（含非法值）。
        self.assertEqual(resolve_timezone("Not/A_Timezone"), "UTC")

    def test_format_log_time_shape_and_invalid_input(self):
        instant = _dt("2026-08-23T08:00:00.000Z")
        # 上游 options 为 zh-CN 的两位 month/day/hour/minute/second + h23；
        # 分隔符按本移植工程约定固定为 '-'（ICU 实测为 '/'）。
        self.assertEqual(format_log_time(instant, "Asia/Shanghai"), "08-23 16:00:00")
        self.assertEqual(format_log_time(None, "Asia/Shanghai"), "-")
        self.assertEqual(format_log_time(instant, "Not/A_Timezone"), "08-23 08:00:00")
        self.assertEqual(format_story_display_time(None, "Asia/Shanghai"), "-")

    def test_local_clock_minutes_and_calendar_day_key(self):
        instant = _dt("2026-08-23T16:30:00.000Z")
        self.assertEqual(local_clock_minutes(instant, "Asia/Shanghai"), 30)  # 次日 00:30
        self.assertEqual(local_clock_minutes(_dt("2026-08-23T08:00:00.000Z"), "Asia/Shanghai"), 960)
        self.assertEqual(calendar_day_key(instant, "Asia/Shanghai"), "2026-08-24")
        self.assertEqual(calendar_day_key(instant, "UTC"), "2026-08-23")


class TimeHelperTests(unittest.TestCase):
    """PORT_PLAN §2「时间」新增的四个统一助手（上游没有对应函数）。"""

    def test_utc_now_is_aware_and_utc(self):
        now = utc_now()
        self.assertIsInstance(now, datetime)
        self.assertEqual(now.tzinfo, _UTC)

    def test_parse_dt_accepts_datetime_iso_string_and_milliseconds(self):
        expected = _dt("2026-08-24T08:00:00.000Z")
        self.assertEqual(parse_dt(expected), expected)
        self.assertEqual(parse_dt("2026-08-24T08:00:00.000Z"), expected)
        self.assertEqual(parse_dt("2026-08-24T16:00:00+08:00"), expected)
        self.assertEqual(parse_dt(dt_ms(expected)), expected)
        self.assertEqual(parse_dt("1787558400000"), expected)
        # naive 字符串按 UTC 解释（上游 Date 恒为绝对时刻）
        self.assertEqual(parse_dt("2026-08-24T08:00:00"), expected)
        self.assertIsNone(parse_dt(None))
        self.assertIsNone(parse_dt(""))
        self.assertIsNone(parse_dt("not-a-time"))
        self.assertIsNone(parse_dt(True))

    def test_iso_and_dt_ms_round_trip(self):
        instant = _dt("2026-08-24T08:00:00.000Z")
        self.assertEqual(iso(instant), "2026-08-24T08:00:00.000Z")
        self.assertEqual(iso("2026-08-24T08:00:00.000Z"), "2026-08-24T08:00:00.000Z")
        self.assertIsNone(iso(None))
        self.assertIsNone(iso("nope"))
        self.assertEqual(dt_ms(instant), 1_787_558_400_000)
        self.assertEqual(dt_ms(None), 0)
        self.assertEqual(dt_ms("nope"), 0)
        # 毫秒精度（三位）与 JS toISOString 一致，不出现微秒。
        self.assertEqual(iso(_dt("2026-08-24T08:00:00.123Z")), "2026-08-24T08:00:00.123Z")


if __name__ == "__main__":
    unittest.main()
