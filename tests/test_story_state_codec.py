"""`upstream/test/story-state-codec.test.ts` 的对应物（stdlib unittest）。

上游 3 条用例逐条移植，另补边界用例。
"""

from __future__ import annotations

import unittest

from plugin.core.story_state import (
    CURRENT_STORY_STATE_VERSION,
    decode_story_state,
    encode_story_state,
    inspect_story_state_migration,
)


class StoryStateCodecTests(unittest.TestCase):
    def test_legacy_beta10_state_upgrades_without_losing_chat_rhythm_or_unknown_extensions(self):
        legacy = {
            "narrativeUpdateCount": 7,
            "settingOverlay": {"perspective": "先理解处境", "characterTraits": ["克制"]},
            "chatRhythm": {
                "recent": [{
                    "bubbles": 2, "shape": ["s", "m"], "tail": "statement",
                    "totalChars": 18, "occurredAt": "2026-09-04T00:00:00.000Z",
                }],
                "updatedAt": "2026-09-04T00:00:00.000Z",
            },
            "futureCodecField": {"retained": True},
        }

        decoded = decode_story_state(legacy)
        self.assertEqual(decoded["schema_version"], CURRENT_STORY_STATE_VERSION)
        self.assertEqual(decoded["narrative_update_count"], 7)
        self.assertEqual(decoded["setting_overlay"]["perspective"], "先理解处境")
        self.assertEqual(decoded["setting_overlay"]["character_traits"], ["克制"])
        self.assertEqual(decoded["chat_rhythm"], legacy["chatRhythm"])
        self.assertEqual(decoded["extensions"]["futureCodecField"], {"retained": True})
        self.assertEqual(encode_story_state(decoded), decoded)
        self.assertEqual(decode_story_state(decoded), decoded)

    def test_migration_inspection_is_diagnostic_and_never_injects_configured_perspective_into_canon(self):
        inspection = inspect_story_state_migration({}, "配置中的默认视角")
        self.assertTrue(inspection["upgraded"])
        self.assertTrue(inspection["perspective_default_available"])
        self.assertIsNone(decode_story_state({})["setting_overlay"]["perspective"])

    def test_scene_frame_codec_retains_only_fields_backed_by_script_entry_provenance(self):
        decoded = decode_story_state({
            "sceneFrame": {
                "id": "frame:one", "place": "没有证据的地点", "attention": "正在读消息",
                "presentPeople": ["A"], "sourceEntryIds": [12],
                "sources": {"attention": [12], "presentPeople": [12]},
                "updatedAt": "2026-09-04T00:00:00.000Z",
            },
            "dialogueBurst": {
                "id": "burst:one", "frameId": "frame:one",
                "startedAt": "2026-09-04T00:00:00.000Z", "sourceEntryIds": [12],
            },
        })
        self.assertIsNone(decoded["scene_frame"]["place"])
        self.assertEqual(decoded["scene_frame"]["attention"], "正在读消息")
        self.assertEqual(decoded["scene_frame"]["present_people"], ["A"])
        self.assertEqual(decoded["dialogue_burst"]["frame_id"], decoded["scene_frame"]["id"])
        self.assertEqual(decode_story_state(decoded), decoded)

    def test_inspection_reports_unknown_keys_and_source_version(self):
        inspection = inspect_story_state_migration(
            {"schemaVersion": 2, "futureThing": 1, "another": 2}, ""
        )
        self.assertEqual(inspection["source_version"], 2)
        self.assertEqual(inspection["target_version"], CURRENT_STORY_STATE_VERSION)
        self.assertTrue(inspection["upgraded"])
        self.assertEqual(inspection["unknown_keys"], ["another", "futureThing"])
        self.assertFalse(inspection["perspective_default_available"])

    def test_upgrade_is_defensive_against_wrong_types(self):
        decoded = decode_story_state({
            "narrativeUpdateCount": "7",
            "settingOverlay": "not-an-object",
            "automation": [1, 2],
            "scenePresence": "nope",
            "workingDetails": [{"label": "x"}],  # 缺 value → 丢弃
            "timelineCarry": ["a", "a", "b", 3],
            "continuitySnapshot": {"current": "", "recent": [], "salient": []},
        })
        self.assertEqual(decoded["narrative_update_count"], 0)
        self.assertEqual(decoded["setting_overlay"]["character_traits"], [])
        self.assertEqual(decoded["automation"]["conversation_follow_up_at"], [])
        self.assertEqual(decoded["scene_presence"], [])
        self.assertEqual(decoded["working_details"], [])
        self.assertEqual(decoded["timeline_carry"], ["a", "b"])
        self.assertIsNone(decoded["continuity_snapshot"])

    def test_present_envelopes_are_stable_under_repeated_decode_and_encode(self):
        envelope = decode_story_state({})
        self.assertEqual(decode_story_state(envelope), envelope)
        self.assertEqual(encode_story_state(envelope), envelope)
        self.assertEqual(encode_story_state(encode_story_state(envelope)), envelope)

    def test_automatic_delivery_summaries_deduplicate_cap_and_require_fields(self):
        from plugin.core.story_state import normalize_automatic_delivery_summaries

        rows = [
            {"participantId": f"p{i}", "summary": f"s{i}",
             "sourceEntryId": i, "deliveredAt": "2026-09-04T00:00:00.000Z"}
            for i in range(1, 9)
        ]
        rows.append(dict(rows[-1]))          # 重复 → 丢
        rows.append({"participantId": "p", "summary": "", "deliveredAt": "2026-09-04T00:00:00.000Z"})
        normalized = normalize_automatic_delivery_summaries(rows)
        self.assertEqual(len(normalized), 6)  # 只保留最后 6 条
        self.assertEqual(normalized[-1]["participant_id"], "p8")
        self.assertEqual(normalized[-1]["source_entry_id"], 8)

    def test_working_details_keep_the_latest_row_per_label_and_cap_at_ten(self):
        from plugin.core.story_state import normalize_working_details

        rows = [
            {"label": f"l{i}", "value": f"v{i}", "createdAt": "2026-09-04T00:00:00.000Z"}
            for i in range(1, 13)
        ]
        rows.append({"label": "l12", "value": "updated", "createdAt": "2026-09-05T00:00:00.000Z"})
        normalized = normalize_working_details(rows)
        self.assertEqual(len(normalized), 10)
        self.assertEqual(normalized[-1]["label"], "l12")
        self.assertEqual(normalized[-1]["value"], "updated")


if __name__ == "__main__":
    unittest.main()
