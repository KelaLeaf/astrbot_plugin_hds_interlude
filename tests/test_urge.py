"""上游 `test/urge.test.ts` 的逐条移植（stdlib `unittest`，零依赖）。

运行：
    cd /home/kela/文档/harness/hds-interlude && python3 -m unittest plugin.tests.test_urge -v

对应关系
--------
* `urge.test.ts` 里 11 条只测 `src/urge.ts` 的用例 → `UrgeTests`。
* `Urge round trips through existing story JSON ...` 需要 `core/story_state.py`
  + `core/narrator.py` → `StoryJsonRoundTripTests`（并行 Agent 产出后自动启用）。
* 末尾 5 条 `real service scheduling ...` 是 `service.ts` 集成用例 →
  `ServiceIntegrationTests`（`core/service.py` 就绪后自动启用）。
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

# 把仓库根加入 sys.path，便于以插件包结构 import（与 tests/test_core.py 同法）。
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
_PLUGIN_DIR = os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

htime = importlib.import_module(f"{_PLUGIN_DIR}.core.time")
urge = importlib.import_module(f"{_PLUGIN_DIR}.core.urge")

resolve_urge_config = urge.resolve_urge_config
normalize_urge_state = urge.normalize_urge_state
urge_user_event = urge.urge_user_event
urge_density = urge.urge_density
commit_urge = urge.commit_urge
acknowledge_urge = urge.acknowledge_urge
urge_burst_active = urge.urge_burst_active
plan_urge = urge.plan_urge
urge_instruction = urge.urge_instruction

# 上游 `const now = Date.parse('2026-09-07T04:00:00Z'), minute = 60_000`
NOW = datetime(2026, 9, 7, 4, 0, 0, tzinfo=timezone.utc)
MINUTE = timedelta(minutes=1)

# 上游 `resolveUrgeConfig({ enabled: true, advanced: { jitter: 0, extremeChance: 0 } })`
C = resolve_urge_config({"enabled": True, "advanced": {"jitter": 0, "extremeChance": 0}})
# 上游 `const high = { value: .9, pace: 'normal', basisQuote: '她想再问一句。' }`
HIGH = {"value": .9, "pace": "normal", "basisQuote": "她想再问一句。"}


def ms(value) -> int:
    """上游测试里的毫秒数（`Date.parse` 结果）等价物。"""
    return urge.dt_ms(value)


def parse(value):
    """等价上游 `Date.parse(...)`：解析成功返回 aware datetime，失败返回 None。"""
    return htime.parse_dt(value)


def fresh():
    """上游 `const fresh = () => normalizeUrgeState({}, now)`"""
    return normalize_urge_state({}, NOW)


def arm():
    """上游 `const arm = () => commitUrge(fresh(), high, high.basisQuote, 9, 'alice', now, c, () => .5)`"""
    return commit_urge(fresh(), HIGH, HIGH["basisQuote"], 9, "alice", NOW, C, lambda: .5)


def _json_round_trip(value):
    """等价 `JSON.parse(JSON.stringify(value))`（容忍 datetime → ISO 字符串）。"""
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _optional_module(name: str):
    try:
        return importlib.import_module(f"{_PLUGIN_DIR}.core.{name}")
    except Exception:  # 并行 Agent 尚未产出 / 语法未定稿
        return None


_story_state = _optional_module("story_state")
_service = _optional_module("service")
_narrator = _optional_module("narrator") or _optional_module("narrative")

_STORY_JSON_READY = bool(
    _story_state is not None
    and hasattr(_story_state, "decode_story_state")
    and hasattr(_story_state, "encode_story_state")
    and _narrator is not None
    and hasattr(_narrator, "story_state_for_prompt")
)
_SERVICE_METHODS = (
    "pause_automatic_advance_after_user_message",
    "pause_automatic_advance_after_delayed_reply",
    "is_automatic_advance_due",
    "schedule_next_automatic_advance",
    "schedule_conversation_follow_ups_after_turn",
    "record_automatic_delivery",
    "schedule_preplan_anchored_time",
)
_SERVICE_READY = bool(
    _service is not None
    and hasattr(_service, "InterludeService")
    and all(hasattr(_service.InterludeService, name) for name in _SERVICE_METHODS)
    and "effective_urge_runtime" in dir(_service.InterludeService)
)

_CONF_SCHEMA_PATH = os.path.join(_REPO_ROOT, _PLUGIN_DIR, "_conf_schema.json")


def _load_conf_schema() -> dict:
    try:
        with open(_CONF_SCHEMA_PATH, encoding="utf-8-sig") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


class UrgeTests(unittest.TestCase):
    """`src/urge.ts` 的纯逻辑用例（与上游测试一一对应）。"""

    def test_legacy_json_including_empty_partial_state_always_reads_defensively(self):
        raws = [
            None,
            {},
            [],
            "bad",
            {"version": 1},
            {"version": 1, "buckets": {}, "armed": [], "burst": {"started": "bad"}},
            {"version": 99, "buckets": [ms(NOW)]},
        ]
        for raw in raws:
            with self.subTest(raw=raw):
                state = normalize_urge_state(raw, NOW)
                self.assertIsInstance(state["buckets"], list)
                self.assertIsNotNone(parse(plan_urge(state, NOW, C)["next_advance_at"]))

    def test_dense_real_turns_shorten_cadence_split_messages_merge_silence_decays(self):
        state = fresh()
        for i in range(6):
            state = urge_user_event(state, NOW - (5 - i) * 2 * MINUTE)
        state = urge_user_event(state, NOW + timedelta(milliseconds=1000))
        self.assertEqual(len(state["buckets"]), 6)
        self.assertEqual(urge_density(state, NOW, C), 1)
        self.assertEqual(urge_density(state, NOW + 45 * MINUTE, C), .5)
        self.assertTrue(
            plan_urge(state, NOW, C, 0, False, lambda: .5)["minutes"]
            < plan_urge(state, NOW + 90 * MINUTE, C, 0, False, lambda: .5)["minutes"]
        )

    def test_only_committed_quoted_script_can_supply_urge_never_a_missing_imagined_quote(self):
        self.assertIsNone(commit_urge(fresh(), HIGH, "别的剧本", 9, "alice", NOW, C)["armed"])
        self.assertIsNone(
            commit_urge(fresh(), {**HIGH, "value": float("nan")}, HIGH["basisQuote"], 9, "alice", NOW, C)["armed"]
        )
        self.assertEqual(arm()["armed"]["participant_id"], "alice")
        self.assertIsNone(commit_urge(fresh(), HIGH, HIGH["basisQuote"], 9, None, NOW, C)["armed"])

    def test_burst_begins_only_at_matching_actual_delivery_duplicate_other_receipts_do_not_restart_it(self):
        state = arm()
        self.assertIsNone(state["burst"])
        self.assertIs(acknowledge_urge(state, "bob", 9, NOW), state)
        self.assertIs(acknowledge_urge(state, "alice", 10, NOW), state)
        active = acknowledge_urge(state, "alice", 9, NOW)
        self.assertEqual(active["burst"]["started"], ms(NOW))
        self.assertIs(acknowledge_urge(active, "alice", 9, NOW + MINUTE), active)
        self.assertFalse(urge_burst_active(active, NOW, C, "bob"))

    def test_burst_cadence_backs_off_and_exhausts_budget_without_self_rearming(self):
        state, t, waits = acknowledge_urge(arm(), "alice", 9, NOW), NOW, []
        for i in range(3):
            plan = plan_urge(state, t, C, 0, False, lambda: .5)
            waits.append(plan["minutes"])
            t = parse(plan["next_advance_at"])
            state = commit_urge(plan["state"], HIGH, HIGH["basisQuote"], 10 + i, "alice", t, C, lambda: .5)
        self.assertEqual(waits, [5, 10, 20])
        final = plan_urge(state, t, C, 0, False, lambda: .5)
        self.assertIsNone(final["state"]["burst"])
        self.assertIsNone(commit_urge(final["state"], HIGH, HIGH["basisQuote"], 20, "alice", t, C)["armed"])
        self.assertIs(urge_user_event(final["state"], t)["spent"], False)

    def test_low_urge_stops_acceleration_without_inventing_density(self):
        active = acknowledge_urge(arm(), "alice", 9, NOW)
        low = commit_urge(active, {**HIGH, "value": .1}, HIGH["basisQuote"], 10, "alice", NOW, C)
        self.assertIsNone(low["burst"])
        self.assertEqual(low["buckets"], [])
        self.assertIs(low["spent"], True)

    def test_slow_rest_device_constraints_win_over_high_urge_real_input_clears_slow(self):
        slow = commit_urge(
            fresh(),
            {**HIGH, "pace": "slow", "suggested_delay_minutes": 120},
            HIGH["basisQuote"],
            10,
            "alice",
            NOW,
            C,
        )
        self.assertEqual(plan_urge(slow, NOW, C, 0, False, lambda: .5)["minutes"], 120)
        self.assertEqual(plan_urge(slow, NOW, C, 180, False, lambda: .5)["minutes"], 180)
        active = acknowledge_urge(arm(), "alice", 9, NOW)
        self.assertIsNone(plan_urge(active, NOW, C, 0, True)["state"]["burst"])
        self.assertEqual(urge_user_event(slow, NOW)["pace"], "normal")

    def test_restart_expires_stale_armed_burst_state_and_keeps_the_already_sampled_deadline(self):
        state = acknowledge_urge(arm(), "alice", 9, NOW)
        plan = plan_urge(state, NOW, C)
        stored = _json_round_trip({"urge": plan["state"], "next_advance_at": plan["next_advance_at"]})
        self.assertEqual(stored["next_advance_at"], plan["next_advance_at"])
        self.assertFalse(
            urge_burst_active(normalize_urge_state(stored["urge"], NOW + 120 * MINUTE), NOW + 120 * MINUTE, C)
        )
        self.assertIsNone(normalize_urge_state(arm(), NOW + 11 * MINUTE)["armed"])

    def test_urge_handoff_exists_only_on_automatic_main_narration_and_does_not_request_independent_speech(self):
        self.assertEqual(urge_instruction(True, "user-message"), "")
        self.assertEqual(urge_instruction(False, "advance"), "")
        self.assertRegex(urge_instruction(True, "intent-due"), "full script")

    def test_console_schema_has_opt_in_defaults_and_advanced_ranges_validate(self):
        # 上游 `Config.dict.urge`（Koishi Schema）：默认关闭、medium、门槛 .4。
        schema = _load_conf_schema().get("urge")
        if isinstance(schema, dict):
            items = schema.get("items") if isinstance(schema.get("items"), dict) else {}
            defaults = {key: value.get("default") for key, value in items.items() if isinstance(value, dict)}
            self.assertIs(defaults.get("enabled"), False)
            self.assertEqual(defaults.get("frequency"), "medium")
        config = resolve_urge_config({})
        self.assertIs(config["enabled"], False)
        self.assertEqual(config["frequency"], "medium")
        self.assertEqual(resolve_urge_config(config)["willingness"], .4)
        # 上游此处断言 Koishi Schema 对 burstBudget:99 抛错；AstrBot schema 不做数值校验，
        # 等价契约由解析层承担：越界即夹取（而不是保留非法值）。
        self.assertEqual(resolve_urge_config({"advanced": {"burstBudget": 99}})["budget"], 10)
        self.assertEqual(resolve_urge_config({"advanced": {"hotMin": 30, "hotMax": 2}})["hot"], [30, 30])


@unittest.skipUnless(
    _STORY_JSON_READY, "需要 core/story_state.py + core/narrator.py（并行 Agent 产出后自动启用）"
)
class StoryJsonRoundTripTests(unittest.TestCase):
    """上游 `Urge round trips through existing story JSON ...`。"""

    def test_urge_round_trips_through_existing_story_json_without_schema_migration_or_leaking_into_prompts(self):
        state = _story_state.decode_story_state({"extensions": {"urge": arm(), "custom": "keep"}})
        restored = _story_state.decode_story_state(_json_round_trip(_story_state.encode_story_state(state)))
        self.assertEqual(normalize_urge_state(restored["extensions"]["urge"], NOW)["armed"]["entry_id"], 9)
        public = _narrator.story_state_for_prompt(restored)
        self.assertIsNone((public.get("extensions") or {}).get("urge"))
        self.assertEqual((public.get("extensions") or {})["custom"], "keep")


def _host(enabled: bool = True):
    """对应上游 `function host(enabled = true)`。"""
    service = object.__new__(_service.InterludeService)  # 等价 `Object.create(InterludeService.prototype)`
    story = {
        "id": "s",
        "setting": {"timezone": "Asia/Shanghai"},
        "cursor_at": NOW,
        "state": _story_state.decode_story_state(
            {"automation": {"conversation_follow_up_at": [htime.iso(NOW)]}}
        ),
    }

    async def get_story(_story_id):
        return story

    async def db_set(_table, _query, update):
        story.update(update)

    service.config = {
        "urge": {"enabled": enabled},
        "runtime": {
            "auto_advance_enabled": True,
            "auto_advance_interval_minutes": 40,
            "auto_advance_jitter_minutes": 0,
            "rest_windows": [],
            "proactive_willingness_threshold": .65,
        },
        "schedule_preplan": {"enabled": False},
    }
    service.get_story = get_story
    service.db_set = db_set
    service.report_operation = lambda *args, **kwargs: None
    return service, lambda: story


@unittest.skipUnless(
    _SERVICE_READY,
    "需要 core/service.py 提供 snake_case 调度 API（并行 Agent 产出后自动启用）",
)
class ServiceIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """上游末尾 5 条 `service.ts` 集成用例。"""

    async def test_real_service_scheduling_replaces_fixed_followups_persists_one_deadline_and_shares_threshold(self):
        service, story = _host()
        await service.pause_automatic_advance_after_user_message("s", NOW)
        self.assertEqual(story()["state"]["automation"]["conversation_follow_up_at"], [])
        before = story()["state"]["automation"]["next_advance_at"]
        service.is_automatic_advance_due(story(), NOW + MINUTE)
        self.assertEqual(story()["state"]["automation"]["next_advance_at"], before)
        self.assertEqual(service.effective_urge_runtime["proactive_willingness_threshold"], .4)
        service.config["urge"]["enabled"] = False
        self.assertEqual(service.effective_urge_runtime["proactive_willingness_threshold"], .65)
        await service.schedule_next_automatic_advance("s", NOW)
        self.assertEqual(story()["state"]["automation"]["next_advance_at"], htime.iso(NOW + 40 * MINUTE))

    async def test_delayed_reply_anchors_urge_at_actual_planned_endpoint_without_adding_density(self):
        service, story = _host()
        await service.pause_automatic_advance_after_delayed_reply("s", NOW + 15 * MINUTE, "alice")
        anchored = normalize_urge_state(story()["state"]["extensions"]["urge"], NOW + 15 * MINUTE)
        self.assertEqual(len(anchored["buckets"]), 0)
        self.assertTrue(parse(story()["state"]["automation"]["next_advance_at"]) > NOW + 15 * MINUTE)

    async def test_disabled_urge_preserves_old_followup_scheduling(self):
        service, story = _host(False)
        await service.schedule_conversation_follow_ups_after_turn("s", NOW, None, "alice")
        self.assertEqual(len(story()["state"]["automation"]["conversation_follow_up_at"]), 2)
        self.assertIsNone((story()["state"].get("extensions") or {}).get("urge"))

    async def test_service_first_bubble_receipt_activates_urge_but_still_waits_to_summarize_all_bubbles(self):
        service, story = _host()
        story()["state"]["extensions"] = {"urge": arm()}

        async def db_get(*args, **kwargs):
            return [
                {
                    "metadata": {
                        "delivery_actions": [
                            {"participant_id": "alice", "event_kind": "outgoing-message", "status": "partial"}
                        ]
                    }
                }
            ]

        service.db_get = db_get
        service.report_standalone = lambda *args, **kwargs: self.fail("unexpected projection error")
        await service.record_automatic_delivery("s", "alice", {"source_entry_id": 9, "summary": "contact"}, NOW)
        self.assertEqual(story()["state"]["extensions"]["urge"]["burst"]["participant_id"], "alice")
        self.assertEqual(story()["state"]["extensions"]["urge"]["burst"]["used"], 1)
        self.assertEqual(len(story()["state"].get("automatic_delivery_summaries") or []), 0)
        deadline = story()["state"]["automation"]["next_advance_at"]
        await service.record_automatic_delivery(
            "s", "alice", {"source_entry_id": 9, "summary": "contact"}, NOW + MINUTE
        )
        self.assertEqual(story()["state"]["automation"]["next_advance_at"], deadline)

    async def test_service_slow_excludes_soft_preplan_and_due_planning_never_overwrites_unrelated_state(self):
        service, story = _host()
        slow = commit_urge(
            fresh(),
            {**HIGH, "pace": "slow", "suggested_delay_minutes": 120},
            HIGH["basisQuote"],
            9,
            None,
            NOW,
            C,
        )
        story()["state"]["extensions"] = {"urge": slow, "untouched": {"x": 1}}
        story()["state"]["automation"]["timeline_retry_at"] = htime.iso(NOW + 2 * MINUTE)
        service.schedule_preplan_anchored_time = lambda *args, **kwargs: self.fail(
            "soft Preplan must not shorten slow"
        )
        await service.schedule_next_automatic_advance("s", NOW)
        self.assertTrue(parse(story()["state"]["automation"]["next_advance_at"]) >= NOW + 110 * MINUTE)
        self.assertEqual(story()["state"]["extensions"]["untouched"], {"x": 1})
        self.assertEqual(story()["state"]["automation"]["timeline_retry_at"], htime.iso(NOW + 2 * MINUTE))


if __name__ == "__main__":
    unittest.main(verbosity=2)
