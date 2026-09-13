# -*- coding: utf-8 -*-
"""上游 `test/configuration.test.ts` 与 `test/release-consistency.test.ts` 的移植。

上游断言的是 **Koishi Console Schema 的形状与默认值**；本移植版的对应物是
`plugin/_conf_schema.json`（AstrBot 格式）。因此这里逐条把上游断言改写成
「读本插件的 schema JSON，断言字段存在 / type 合法 / default 与上游一致 /
options 枚举集合一致 / 数值范围一致」。

上游中依赖运行期纯函数的断言（`resolveBlindModeConfig` / `configuredProviders` /
`normalizeInteraction` / `visibleReplyMode` / `hasRequiredNarrativeScript`）不属于
「配置 schema」范畴，归各自的模块测试；`resolve_blind_mode_config` 例外——它是
schema 默认值的直接消费者，本文件用 `skipUnless` 在它落地后自动启用。

独立运行：
    cd /home/kela/文档/harness/hds-interlude
    python3 -m unittest plugin.tests.test_configuration -v
"""

from __future__ import annotations


import json
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(HERE)
REPO_ROOT = os.path.dirname(PLUGIN_ROOT)

SCHEMA_PATH = os.path.join(PLUGIN_ROOT, "_conf_schema.json")
METADATA_PATH = os.path.join(PLUGIN_ROOT, "metadata.yaml")
CHANGELOG_PATH = os.path.join(PLUGIN_ROOT, "CHANGELOG.md")
META_PY_PATH = os.path.join(PLUGIN_ROOT, "core", "meta.py")
UPSTREAM_SYNC_PATH = os.path.join(REPO_ROOT, "docs", "UPSTREAM_SYNC.md")
UPSTREAM_PACKAGE_PATH = os.path.join(REPO_ROOT, "upstream", "package.json")

#: AstrBot `_conf_schema.json` 允许的 type 集合（astrbot/core/config/default.py:DEFAULT_VALUE_MAP）。
ALLOWED_TYPES = {"string", "text", "int", "float", "bool", "object", "list", "template_list", "file"}

#: 上游 Console 的分组顺序（`upstream/test/configuration.test.ts:11-14`）。
UPSTREAM_SECTION_ORDER = [
    "storyDefaults", "model", "onebot", "sharedStory", "runtime", "urge",
    "schedulePreplan", "timelineDirector", "agency",
    "chatActions", "stickers", "memory", "alterSystem", "browser", "blindMode", "logging",
    "chatRhythm",
]

#: 上游键 → 本插件顶层键（顺序与上游一致）。
SECTION_MAP = [
    ("storyDefaults", "story_defaults"),
    ("model", "model_center"),
    ("onebot", "qq_access"),
    ("sharedStory", "shared_story"),
    ("runtime", "runtime"),
    ("urge", "urge"),
    ("schedulePreplan", "schedule_preplan"),
    ("timelineDirector", "timeline_director"),
    ("agency", "agency"),
    ("chatActions", "chat_actions"),
    ("stickers", "stickers"),
    ("memory", "memory"),
    ("alterSystem", "alter_system"),
    ("browser", "browser"),
    ("blindMode", "blind_mode"),
    ("logging", "logging"),
    ("chatRhythm", "chat_rhythm"),
]

#: 上游 Console 的完整字段清单（camelCase），用于逐项对账 coverage。
#: 来源：`upstream/src/index.ts` 中各组 Schema.object 的键。
UPSTREAM_FIELDS = {
    "story_defaults": [
        "characterName", "characterProfile", "perspective", "userProfile", "relationship",
        "world", "supportingCast", "location", "style", "timezone",
    ],
    "model_center": [
        "vision", "audio", "providers", "mainTemperature", "mainTopP", "mainMaxTokens",
        "mainTimeout", "mainResponseFormat", "mainStreamingMode", "mainPayloadOrder",
        "failover", "mainPrompt", "formatPrompt", "fixedPrompt", "stylePrompt",
        "embedding", "compaction",
    ],
    "qq_access": [
        "enabled", "botAccounts", "userAccounts", "groupChats", "ignoreSelfMessages",
    ],
    "shared_story": [
        "autoEnrollParticipants", "allowCrossConversationMessages", "shareParticipantDetails",
        "maxCrossConversationActions", "participantContextLimit", "managerAccounts",
    ],
    "runtime": [
        "splitReplyMessages", "messageSeparator", "typingBaseDelaySeconds",
        "typingCharactersPerSecond", "typingMaxDelaySeconds", "typingJitterRatio",
        "userMessageDebounceSeconds", "narrativeRetryDelaySeconds", "narrativeRetryMaxAttempts",
        "captureDirectMessages", "autoCreate", "ignoreCommandMessages",
        "allowProactiveMessages", "proactiveWillingnessThreshold", "sweepIntervalMinutes",
        "minimumAdvanceMinutes", "maxStoriesPerSweep", "contextEntryLimit",
        "contextTimeWindowMinutes", "memoryLimit", "maxScriptCharacters",
        "maxMessageCharacters", "minimumDelayedReplySeconds", "maximumDelayedReplyMinutes",
        "cancelDelayedRepliesOnUserMessage", "autoAdvanceEnabled", "autoAdvanceIntervalMinutes",
        "autoAdvanceJitterMinutes", "conversationFollowUpMinutes",
        "conversationFollowUpJitterMinutes", "restWindows",
    ],
    "urge": ["enabled", "frequency", "proactiveWillingnessThreshold", "advanced"],
    "schedule_preplan": [
        "enabled", "horizonDays", "variationLevel", "candidateActivationProbability",
        "candidateRevealMinutes", "reviewAfterLocalHour", "anchorAutoAdvance",
    ],
    "timeline_director": ["enabled"],
    "agency": [
        "enabled", "maxWindowMinutes", "minimumProactiveIntervalMinutes", "maxCandidateHours",
    ],
    "chat_actions": [
        "enabled", "platforms", "quoteReply", "messageReactions", "allowedReactions",
        "nativeFaces", "expressionThreshold", "allowedNativeFaces",
    ],
    "stickers": [
        "enabled", "directory", "max_file_size_mb", "catalogLimit", "descriptionMaxTokens",
        "descriptionResponseFormat",
    ],
    "memory": [
        "enabled", "backgroundIntervalMinutes", "sceneEntryThreshold",
        "sceneCharacterThreshold", "recentEntryLimit", "factLimit",
        "statePatchConfidenceThreshold", "majorStatePatchConfidenceThreshold",
        "statePatchMinEvidence", "statePatchMinTurns", "statePatchMinDays",
        "statePatchCooldownHours", "maxFactsPerStory", "maxStoriesPerCompactionRun",
        "compactionEntryLimit", "compactionCharacterLimit", "sceneHookCharacters",
        "sceneSummaryCharacters", "arcSummaryCharacters", "previousSceneSummaries",
        "factContentCharacters", "factImportanceWeight", "factConfidenceWeight",
        "factRecencyWeight", "semanticWeight", "unresolvedWeight",
        "autoApplyStatePatches", "allowMajorStateChanges", "activeConsequencesEnabled",
        "activeConsequencePromptLimit", "activeConsequenceMaxDays",
        "activeConsequenceDefaultStrength", "overlayCompressionEnabled",
        "overlayRecentDays", "overlayMonthlyAfterDays", "overlayWeeklyWindowDays",
        "overlayMonthlyWindowDays", "overlayWeeklySummaryCharacters",
        "overlayMonthlySummaryCharacters",
    ],
    "alter_system": [
        "enabled", "baseThreshold", "densityFactor", "sameDirectionBoost", "oppositeDecay",
        "minWeight", "maxIntensity", "temperature", "top_p", "max_tokens", "timeout", "prompt",
    ],
    "browser": [
        "enabled", "mode", "allowSearch", "allowVisit", "searchUrlTemplate",
        "allowedDomains", "blockedDomains", "maxConcurrentPages", "maxResearchPerSweep",
        "navigationTimeout", "waitUntil", "maxTextCharacters", "maxExcerptCharacters",
        "maxObservationsInPrompt", "cacheMinutes", "allowGroupTriggeredResearch",
        "logObservationPreview",
    ],
    "blind_mode": ["enabled", "healthReportMinutes"],
    "logging": [
        "level", "verbosity", "format", "colors", "colorTheme", "kaomoji",
        "logScriptPreview", "logMessageContent", "previewLength",
    ],
    "chat_rhythm": ["enabled", "mode", "historyLimit", "collapseMinSamples", "exhaustLimit"],
}

#: 已弃用 / 隐藏的旧字段（上游 `CONFIGURATION_GUIDE.md`「隐藏的历史兼容字段」）。
UPSTREAM_COMPAT_FIELDS = {
    "shared_story_compat": ["enabled", "participantPresets"],
    "runtime_compat": ["pauseAfterConversationMinutes", "staleNarrativeRequestWindowSeconds"],
}

#: 上游深度字段（嵌套 / 列表项）的默认值断言：(节点路径..., 键, 期望默认值)
DEEP_DEFAULTS = [
    (("model_center", "vision"), "enabled", False),
    (("model_center", "vision"), "mode", "native"),
    (("model_center", "vision"), "detail", "auto"),
    (("model_center", "vision"), "max_image_dimension", 1024),
    (("model_center", "audio"), "enabled", False),
    (("model_center", "audio"), "out_format", "mp3"),
    (("model_center", "audio"), "max_file_size_mb", 10),
    (("model_center", "audio"), "max_per_message", 1),
    (("model_center", "failover"), "enabled", True),
    (("model_center", "failover"), "strategy", "priority"),
    (("model_center", "failover"), "max_attempts_per_provider", 1),
    (("model_center", "failover"), "cooldown_minutes", 5),
    (("model_center", "embedding"), "enabled", False),
    (("model_center", "embedding"), "live_query", False),
    (("model_center", "embedding"), "semantic_history", False),
    (("model_center", "embedding"), "semantic_sticker_filter", True),
    (("model_center", "embedding"), "dimensions", 0),
    (("model_center", "embedding"), "timeout", 10000),
    (("model_center", "embedding"), "max_input_characters", 4000),
    (("model_center", "embedding"), "backfill_batch_size", 5),
    (("model_center", "compaction"), "enabled", True),
    (("model_center", "compaction"), "temperature", 0.3),
    (("model_center", "compaction"), "top_p", 1.0),
    (("model_center", "compaction"), "max_tokens", 2048),
    (("model_center", "compaction"), "timeout", 60000),
    (("model_center", "compaction"), "response_format", "json-object"),
    (("urge", "advanced"), "half_life_minutes", 45),
    (("urge", "advanced"), "burst_threshold", 0.75),
    (("urge", "advanced"), "jitter", 0.15),
    (("urge", "advanced"), "extreme_chance", 0.03),
    (("urge", "advanced"), "burst_ttl_minutes", 35),
    (("urge", "advanced"), "burst_budget", 3),
    (("urge", "advanced"), "burst_contact_min_minutes", 5),
    (("blind_mode",), "health_report_minutes", 10),
    (("black_box",), "health_report_minutes", 10),
]

#: 上游 provider 行的默认值（`upstream/src/index.ts:17-38` defaultProvider）。
UPSTREAM_PROVIDER_DEFAULT = {
    "label": "Primary model",
    "enabled": True,
    "endpoint": "",
    "apiKey": "",
    "model": "",
    "temperature": 0.8,
    "topP": 1,
    "maxTokens": 4096,
    "timeout": 60000,
    "responseFormat": "json-object",
    "extraHeaders": "",
    "extraBody": "",
    "useForMain": True,
    "useForCompaction": True,
    "useForAlter": True,
    "useForEmbedding": False,
    "useForStickers": False,
    "useForVision": False,
    "mode": "openai-compatible",
}

#: 上游字段名 → 本插件 snake_case 键名（配置映射表，见 `docs/CONFIG_MAP.md`）。
CAMEL_TO_SNAKE = {
    "characterName": "character_name", "characterProfile": "character_profile",
    "userProfile": "user_profile", "supportingCast": "supporting_cast",
    "maxImageDimension": "max_image_dimension", "outFormat": "out_format",
    "maxFileSizeMB": "max_file_size_mb", "maxPerMessage": "max_per_message",
    "apiKey": "api_key", "reasoningEffort": "reasoning_effort",
    "deepseekThinking": "deepseek_thinking",
    "deepseekReasoningEffort": "deepseek_reasoning_effort",
    "dashscopeRegion": "dashscope_region", "extraHeaders": "extra_headers",
    "extraBody": "extra_body", "useForMain": "use_for_main",
    "useForCompaction": "use_for_compaction", "useForAlter": "use_for_alter",
    "useForEmbedding": "use_for_embedding", "useForStickers": "use_for_stickers",
    "useForVision": "use_for_vision", "priceInput": "price_input",
    "priceOutput": "price_output", "priceCachedInput": "price_cached_input",
    "maxAttemptsPerProvider": "max_attempts_per_provider",
    "cooldownMinutes": "cooldown_minutes", "semanticHistory": "semantic_history",
    "liveQuery": "live_query", "maxInputCharacters": "max_input_characters",
    "backfillBatchSize": "backfill_batch_size",
    "semanticStickerFilter": "semantic_sticker_filter",
    "topP": "top_p", "maxTokens": "max_tokens", "responseFormat": "response_format",
    "mainPrompt": "main_prompt", "formatPrompt": "format_prompt",
    "fixedPrompt": "fixed_prompt", "stylePrompt": "style_prompt",
    "mainTemperature": "main_temperature", "mainTopP": "main_top_p",
    "mainMaxTokens": "main_max_tokens", "mainTimeout": "main_timeout",
    "mainResponseFormat": "main_response_format",
    "mainStreamingMode": "main_streaming_mode",
    "mainPayloadOrder": "main_payload_order",
    "botAccounts": "bot_accounts", "userAccounts": "user_accounts",
    "groupChats": "group_chats", "ignoreSelfMessages": "ignore_self_messages",
    "personId": "person_id", "groupId": "group_id",
    "characterRole": "character_role", "responseMode": "response_mode",
    "contextLimit": "context_limit", "debounceSeconds": "debounce_seconds",
    "cooldownSeconds": "cooldown_seconds", "maxScore": "max_score",
    "probabilityAmplifier": "probability_amplifier",
    "decayHalfLifeSeconds": "decay_half_life_seconds", "replyCost": "reply_cost",
    "baseGain": "base_gain", "quoteGain": "quote_gain", "keywordGain": "keyword_gain",
    "autoEnrollParticipants": "auto_enroll_participants",
    "allowCrossConversationMessages": "allow_cross_conversation_messages",
    "shareParticipantDetails": "share_participant_details",
    "maxCrossConversationActions": "max_cross_conversation_actions",
    "participantContextLimit": "participant_context_limit",
    "managerAccounts": "manager_accounts", "splitReplyMessages": "split_reply_messages",
    "messageSeparator": "message_separator",
    "typingBaseDelaySeconds": "typing_base_delay_seconds",
    "typingCharactersPerSecond": "typing_characters_per_second",
    "typingMaxDelaySeconds": "typing_max_delay_seconds",
    "typingJitterRatio": "typing_jitter_ratio",
    "userMessageDebounceSeconds": "user_message_debounce_seconds",
    "narrativeRetryDelaySeconds": "narrative_retry_delay_seconds",
    "narrativeRetryMaxAttempts": "narrative_retry_max_attempts",
    "captureDirectMessages": "capture_direct_messages", "autoCreate": "auto_create",
    "ignoreCommandMessages": "ignore_command_messages",
    "allowProactiveMessages": "allow_proactive_messages",
    "proactiveWillingnessThreshold": "proactive_willingness_threshold",
    "sweepIntervalMinutes": "sweep_interval_minutes",
    "minimumAdvanceMinutes": "minimum_advance_minutes",
    "maxStoriesPerSweep": "max_stories_per_sweep",
    "contextEntryLimit": "context_entry_limit",
    "contextTimeWindowMinutes": "context_time_window_minutes",
    "memoryLimit": "memory_limit", "maxScriptCharacters": "max_script_characters",
    "maxMessageCharacters": "max_message_characters",
    "minimumDelayedReplySeconds": "minimum_delayed_reply_seconds",
    "maximumDelayedReplyMinutes": "maximum_delayed_reply_minutes",
    "cancelDelayedRepliesOnUserMessage": "cancel_delayed_replies_on_user_message",
    "autoAdvanceEnabled": "auto_advance_enabled",
    "autoAdvanceIntervalMinutes": "auto_advance_interval_minutes",
    "autoAdvanceJitterMinutes": "auto_advance_jitter_minutes",
    "conversationFollowUpMinutes": "conversation_follow_up_minutes",
    "conversationFollowUpJitterMinutes": "conversation_follow_up_jitter_minutes",
    "restWindows": "rest_windows", "minIntervalMinutes": "min_interval_minutes",
    "maxIntervalMinutes": "max_interval_minutes",
    "hotMin": "hot_min", "hotMax": "hot_max", "idleMin": "idle_min", "idleMax": "idle_max",
    "burstMin": "burst_min", "burstMax": "burst_max", "slowMin": "slow_min",
    "slowMax": "slow_max", "halfLifeMinutes": "half_life_minutes",
    "burstThreshold": "burst_threshold", "extremeChance": "extreme_chance",
    "burstTtlMinutes": "burst_ttl_minutes", "burstBudget": "burst_budget",
    "burstContactMinMinutes": "burst_contact_min_minutes",
    "horizonDays": "horizon_days", "variationLevel": "variation_level",
    "candidateActivationProbability": "candidate_activation_probability",
    "candidateRevealMinutes": "candidate_reveal_minutes",
    "reviewAfterLocalHour": "review_after_local_hour",
    "anchorAutoAdvance": "anchor_auto_advance", "maxWindowMinutes": "max_window_minutes",
    "minimumProactiveIntervalMinutes": "minimum_proactive_interval_minutes",
    "maxCandidateHours": "max_candidate_hours", "quoteReply": "quote_reply",
    "messageReactions": "message_reactions", "allowedReactions": "allowed_reactions",
    "nativeFaces": "native_faces", "expressionThreshold": "expression_threshold",
    "allowedNativeFaces": "allowed_native_faces", "catalogLimit": "catalog_limit",
    "descriptionMaxTokens": "description_max_tokens",
    "descriptionResponseFormat": "description_response_format",
    "backgroundIntervalMinutes": "background_interval_minutes",
    "sceneEntryThreshold": "scene_entry_threshold",
    "sceneCharacterThreshold": "scene_character_threshold",
    "recentEntryLimit": "recent_entry_limit", "factLimit": "fact_limit",
    "statePatchConfidenceThreshold": "state_patch_confidence_threshold",
    "majorStatePatchConfidenceThreshold": "major_state_patch_confidence_threshold",
    "statePatchMinEvidence": "state_patch_min_evidence",
    "statePatchMinTurns": "state_patch_min_turns",
    "statePatchMinDays": "state_patch_min_days",
    "statePatchCooldownHours": "state_patch_cooldown_hours",
    "maxFactsPerStory": "max_facts_per_story",
    "maxStoriesPerCompactionRun": "max_stories_per_compaction_run",
    "compactionEntryLimit": "compaction_entry_limit",
    "compactionCharacterLimit": "compaction_character_limit",
    "sceneHookCharacters": "scene_hook_characters",
    "sceneSummaryCharacters": "scene_summary_characters",
    "arcSummaryCharacters": "arc_summary_characters",
    "previousSceneSummaries": "previous_scene_summaries",
    "factContentCharacters": "fact_content_characters",
    "factImportanceWeight": "fact_importance_weight",
    "factConfidenceWeight": "fact_confidence_weight",
    "factRecencyWeight": "fact_recency_weight", "semanticWeight": "semantic_weight",
    "unresolvedWeight": "unresolved_weight",
    "autoApplyStatePatches": "auto_apply_state_patches",
    "allowMajorStateChanges": "allow_major_state_changes",
    "activeConsequencesEnabled": "active_consequences_enabled",
    "activeConsequencePromptLimit": "active_consequence_prompt_limit",
    "activeConsequenceMaxDays": "active_consequence_max_days",
    "activeConsequenceDefaultStrength": "active_consequence_default_strength",
    "overlayCompressionEnabled": "overlay_compression_enabled",
    "overlayRecentDays": "overlay_recent_days",
    "overlayMonthlyAfterDays": "overlay_monthly_after_days",
    "overlayWeeklyWindowDays": "overlay_weekly_window_days",
    "overlayMonthlyWindowDays": "overlay_monthly_window_days",
    "overlayWeeklySummaryCharacters": "overlay_weekly_summary_characters",
    "overlayMonthlySummaryCharacters": "overlay_monthly_summary_characters",
    "baseThreshold": "base_threshold", "densityFactor": "density_factor",
    "sameDirectionBoost": "same_direction_boost",
    "oppositeDecay": "opposite_decay", "minWeight": "min_weight",
    "maxIntensity": "max_intensity", "allowSearch": "allow_search",
    "allowVisit": "allow_visit", "searchUrlTemplate": "search_url_template",
    "allowedDomains": "allowed_domains", "blockedDomains": "blocked_domains",
    "maxConcurrentPages": "max_concurrent_pages",
    "maxResearchPerSweep": "max_research_per_sweep",
    "navigationTimeout": "navigation_timeout", "waitUntil": "wait_until",
    "maxTextCharacters": "max_text_characters",
    "maxExcerptCharacters": "max_excerpt_characters",
    "maxObservationsInPrompt": "max_observations_in_prompt",
    "cacheMinutes": "cache_minutes",
    "allowGroupTriggeredResearch": "allow_group_triggered_research",
    "logObservationPreview": "log_observation_preview",
    "healthReportMinutes": "health_report_minutes", "colorTheme": "color_theme",
    "logScriptPreview": "log_script_preview",
    "logMessageContent": "log_message_content", "previewLength": "preview_length",
    "historyLimit": "history_limit", "collapseMinSamples": "collapse_min_samples",
    "exhaustLimit": "exhaust_limit", "participantPresets": "participant_presets",
    "pauseAfterConversationMinutes": "pause_after_conversation_minutes",
    "staleNarrativeRequestWindowSeconds": "stale_narrative_request_window_seconds",
    "groupGate": "group_gate",
}


def snake(camel: str) -> str:
    return CAMEL_TO_SNAKE.get(camel, camel)


def load_schema() -> dict:
    with open(SCHEMA_PATH, encoding="utf-8-sig") as fp:
        return json.load(fp)


def load_meta_version() -> str:
    from plugin.core.meta import HDS_INTERLUDE_VERSION

    return HDS_INTERLUDE_VERSION


def read(path: str) -> str:
    with open(path, encoding="utf-8") as fp:
        return fp.read()


def iter_fields(node: dict, path: str = ""):
    """深度遍历 schema，产出 (路径, 字段名, 字段定义)。"""
    for key, spec in node.items():
        here = f"{path}.{key}" if path else key
        yield here, key, spec
        if not isinstance(spec, dict):
            continue
        if spec.get("type") == "object":
            yield from iter_fields(spec.get("items", {}), here)
        elif spec.get("type") == "list":
            items = spec.get("items")
            if not isinstance(items, dict):
                continue
            # list[标量]：items 是单个 schema 节点（形如 {"type": "string"}），不是字段。
            # list[dict]：items 是「字段名 → 子 schema」，本身是容器，不能单独成行。
            if isinstance(items.get("type"), str):
                continue
            for sub_key, sub_spec in items.items():
                yield f"{here}[].{sub_key}", sub_key, sub_spec
                if isinstance(sub_spec, dict) and sub_spec.get("type") == "object":
                    yield from iter_fields(sub_spec.get("items", {}), f"{here}[].{sub_key}")


class ConfigurationSchemaTest(unittest.TestCase):
    """上游 `test/configuration.test.ts` 的等价移植。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = load_schema()

    def section(self, top_key: str) -> dict:
        return self.schema[top_key]["items"]

    # -- 上游第 1 条：Console sections follow the documented setup order ---------

    def test_console_sections_follow_the_documented_setup_order(self):
        # 上游断言 `Object.keys(Config.dict)`；本插件把「顺序」表达为 top-level 键顺序。
        expected_active = [
            "story_defaults", "model_center", "qq_access", "shared_story", "runtime",
            "urge", "schedule_preplan", "timeline_director", "agency", "chat_actions",
            "stickers", "memory", "alter_system", "browser", "blind_mode", "logging",
        ]
        top_keys = list(self.schema.keys())
        active_order = [k for k in top_keys if k in set(expected_active)]
        self.assertEqual(active_order, expected_active,
                         "顶层分组顺序必须照抄上游 Console：必填→结构→节奏→表达→内在→扩展→维护")
        for upstream, local in SECTION_MAP:
            with self.subTest(upstream=upstream):
                self.assertIn(local, top_keys)
        # storyDefaults / model / onebot 是前三项：必填组排在结构组之前。
        self.assertEqual(active_order[:3], ["story_defaults", "model_center", "qq_access"])
        # 已弃用兼容项必须全部排在活动分组之后。
        first_compat = min(top_keys.index(k) for k in
                           ("chat_rhythm", "black_box", "shared_story_compat",
                            "runtime_compat", "model_compat"))
        self.assertGreater(first_compat, top_keys.index("logging"),
                           "兼容项必须排在所有活动分组之后")

    def test_section_descriptions_carry_the_group_numbering(self):
        # 上游断言 headers[0] ^【必填 1】、[3] ^【结构 4】、[4] ^【节奏 5】、
        # [9] ^【表达 10】、[11] ^【内在 12】、[13] ^【扩展 14】、[14] ^【维护 15】。
        expected = {
            "story_defaults": "【必填 1】", "model_center": "【必填 2】",
            "qq_access": "【必填 3】", "shared_story": "【结构 4】",
            "runtime": "【节奏 5】", "urge": "【节奏 6】",
            "schedule_preplan": "【节奏 7】", "timeline_director": "【节奏 8】",
            "agency": "【节奏 9】", "chat_actions": "【表达 10】",
            "stickers": "【表达 11】", "memory": "【内在 12】",
            "alter_system": "【内在 13】", "browser": "【扩展 14】",
            "blind_mode": "【维护 15】", "logging": "【维护 16】",
            "chat_rhythm": "【已弃用】",
        }
        for key, prefix in expected.items():
            with self.subTest(section=key):
                self.assertTrue(self.schema[key]["description"].startswith(prefix),
                                f"{key}: {self.schema[key]['description']!r}")

    # -- 上游第 2 条：chat actions are opt-in and platform-scoped ---------------

    def test_chat_actions_are_opt_in_and_platform_scoped(self):
        actions = self.section("chat_actions")
        self.assertIs(actions["enabled"]["default"], False)
        self.assertEqual(actions["platforms"]["default"], ["qq"])
        self.assertIs(actions["quote_reply"]["default"], True)
        self.assertIs(actions["message_reactions"]["default"], True)
        self.assertIs(actions["native_faces"]["default"], True)
        self.assertEqual(actions["expression_threshold"]["default"], 0.7)
        self.assertEqual(actions["allowed_reactions"]["default"],
                         ["like", "smile", "laugh", "heart"])
        self.assertEqual(actions["allowed_native_faces"]["default"],
                         ["smile", "laugh", "sweat", "awkward"])

    # -- 上游第 3 条：local sticker library is opt-in ---------------------------

    def test_local_sticker_library_is_opt_in(self):
        stickers = self.section("stickers")
        self.assertIs(stickers["enabled"]["default"], False)
        self.assertEqual(stickers["directory"]["default"], "data/hds-interlude/stickers")
        self.assertEqual(stickers["catalog_limit"]["default"], 40)
        self.assertEqual(stickers["description_response_format"]["default"], "json-object")

    # -- 上游第 4 条：Blind Mode defaults --------------------------------------

    def test_blind_mode_defaults_to_a_minimal_ten_minute_heartbeat(self):
        blind = self.section("blind_mode")
        self.assertIs(blind["enabled"]["default"], False)
        self.assertEqual(blind["health_report_minutes"]["default"], 10)

    @unittest.skipUnless(
        os.path.exists(os.path.join(PLUGIN_ROOT, "core", "service", "config.py")),
        "service/config.py 尚未落地",
    )
    def test_resolve_blind_mode_config_matches_schema_defaults(self):
        """上游还断言了 `resolveBlindModeConfig()`；本插件对应物是 service/config.py。"""
        try:
            from plugin.core.service import config as config_mod  # noqa: PLC0415
        except Exception as exc:  # pragma: no cover - 并行开发中的模块
            self.skipTest(f"service/config.py 暂不可导入：{exc}")
        resolver = getattr(config_mod, "resolve_blind_mode_config", None)
        if resolver is None:
            self.skipTest("resolve_blind_mode_config 尚未落地")
        blind = self.section("blind_mode")
        self.assertEqual(resolver(), {
            "enabled": blind["enabled"]["default"],
            "health_report_minutes": blind["health_report_minutes"]["default"],
        })
        self.assertEqual(resolver({"enabled": True, "healthReportMinutes": 2}),
                         {"enabled": True, "health_report_minutes": 2})

    # -- 上游第 5 条：ignored compatibility switches stay out of the active UI --

    def test_ignored_compatibility_switches_stay_out_of_the_active_console(self):
        self.assertNotIn("enabled", self.section("shared_story"))
        self.assertNotIn("pause_after_conversation_minutes", self.section("runtime"))
        self.assertNotIn("stale_narrative_request_window_seconds", self.section("runtime"))
        self.assertEqual(self.section("runtime")["user_message_debounce_seconds"]["default"], 2)
        # 兼容项必须存在（旧配置不丢），但必须隐藏。
        self.assertIs(self.schema["shared_story_compat"].get("invisible"), True)
        self.assertIs(self.schema["runtime_compat"].get("invisible"), True)
        self.assertIs(self.schema["model_compat"].get("invisible"), True)
        self.assertIs(self.schema["chat_rhythm"].get("invisible"), True)
        self.assertIs(self.schema["black_box"].get("invisible"), True)

    # -- 上游第 6 条：runtime and plugin exports share one version constant -----

    def test_runtime_and_plugin_exports_share_one_version_constant(self):
        # 上游：`version === HDS_INTERLUDE_VERSION === '1.0.1-beta6-rebuild'`。
        # 本插件 `plugin/_conf_schema.json` 不再是版本的载体，等价物是 core/meta.py。
        self.assertEqual(load_meta_version(), "1.0.1-beta6-rebuild")
        self.assertIn("HDS_INTERLUDE_VERSION", read(META_PY_PATH))

    # -- 上游第 7 条：layered colored logs are the Console default --------------

    def test_layered_colored_logs_are_the_console_default(self):
        logging = self.section("logging")
        self.assertEqual(logging["format"]["default"], "layered")
        self.assertIs(logging["colors"]["default"], True)
        self.assertEqual(logging["color_theme"]["default"], "dark")
        self.assertIs(logging["kaomoji"]["default"], True)

    # -- 上游第 8 条：model Console centralizes connections without IDs ---------

    def test_model_console_centralizes_connections_without_exposing_ids(self):
        model = self.section("model_center")
        self.assertEqual(list(model.keys())[:3], ["vision", "audio", "providers"])
        for absent in ("mode", "zhipu", "models", "main_model_id", "mainModelId"):
            self.assertNotIn(absent, model)
        self.assertEqual(model["main_response_format"]["default"], "json-object")
        self.assertEqual(model["main_streaming_mode"]["default"], "off")
        self.assertEqual(model["main_payload_order"]["default"], "legacy")
        self.assertEqual(model["vision"]["items"]["mode"]["default"], "native")
        self.assertEqual(model["vision"]["items"]["detail"]["default"], "auto")

    def test_provider_row_exposes_every_upstream_field(self):
        items = self.section("model_center")["providers"]["items"]
        for camel, default in UPSTREAM_PROVIDER_DEFAULT.items():
            with self.subTest(field=camel):
                key = snake(camel)
                # snake() 对已是 snake_case 的键原样返回，两种拼写都要认。
                if key not in items:
                    key = camel
                self.assertIn(key, items)
                self.assertEqual(items[key]["default"], default,
                                 f"providers[].{key} 默认值与上游 defaultProvider 不一致")
        # temperature / timeout / topP / maxTokens 属于 ProviderAssignments 之外的连接级字段。
        for extra in ("temperature", "top_p", "max_tokens", "timeout",
                      "response_format", "reasoning_effort", "deepseek_thinking",
                      "deepseek_reasoning_effort", "dashscope_region",
                      "price_input", "price_output", "price_cached_input"):
            self.assertIn(extra, items)

    def test_provider_mode_enum_matches_upstream_presets(self):
        mode = self.section("model_center")["providers"]["items"]["mode"]
        self.assertEqual(mode["options"], [
            "openai-compatible", "zhipu-official", "openai-official", "deepseek-official",
            "moonshot-official", "dashscope-official", "siliconflow-official", "openrouter",
            "gemini-openai",
        ])
        self.assertEqual(mode["default"], "openai-compatible")

    def test_deepseek_official_provider_exposes_independent_thinking_controls(self):
        items = self.section("model_center")["providers"]["items"]
        self.assertEqual(items["deepseek_thinking"]["default"], "disabled")
        self.assertEqual(items["deepseek_thinking"]["options"], ["disabled", "enabled"])
        self.assertEqual(items["deepseek_reasoning_effort"]["default"], "low")
        self.assertEqual(items["deepseek_reasoning_effort"]["options"], ["low", "high", "max"])
        self.assertEqual(items["reasoning_effort"]["default"], "high")
        self.assertEqual(items["reasoning_effort"]["options"], ["low", "high", "max"])
        self.assertEqual(items["dashscope_region"]["options"],
                         ["beijing", "singapore", "us"])

    # -- 上游第 10 条：Agency Window exposes only four bounded controls ---------

    def test_agency_window_exposes_only_the_four_bounded_controls(self):
        agency = self.section("agency")
        self.assertEqual(list(agency.keys()), [
            "enabled", "max_window_minutes", "minimum_proactive_interval_minutes",
            "max_candidate_hours",
        ])
        self.assertIs(agency["enabled"]["default"], True)
        self.assertEqual(agency["max_window_minutes"]["default"], 240)
        self.assertEqual(agency["minimum_proactive_interval_minutes"]["default"], 60)
        self.assertEqual(agency["max_candidate_hours"]["default"], 24)

    # -- 上游第 11 条：Schedule Preplan is lightweight --------------------------

    def test_schedule_preplan_is_lightweight_and_enabled_by_default(self):
        schedule = self.section("schedule_preplan")
        self.assertEqual(list(schedule.keys()), [
            "enabled", "horizon_days", "variation_level",
            "candidate_activation_probability", "candidate_reveal_minutes",
            "review_after_local_hour", "anchor_auto_advance",
        ])
        self.assertIs(schedule["enabled"]["default"], True)
        self.assertEqual(schedule["horizon_days"]["default"], 14)
        self.assertEqual(schedule["variation_level"]["default"], "stable")
        self.assertEqual(schedule["variation_level"]["options"],
                         ["stable", "contextual", "granular"])
        self.assertNotIn("model_id", schedule)
        self.assertNotIn("provider_id", schedule)

    # -- 上游第 13 条：Timeline director ---------------------------------------

    def test_timeline_director_is_enabled_by_default(self):
        director = self.section("timeline_director")
        self.assertEqual(list(director.keys()), ["enabled"])
        self.assertIs(director["enabled"]["default"], True)

    # -- 上游第 14 条：memory compaction defaults -------------------------------

    def test_memory_compaction_defaults_leave_a_wider_short_conversation_buffer(self):
        memory = self.section("memory")
        self.assertEqual(memory["scene_entry_threshold"]["default"], 16)
        self.assertEqual(memory["scene_character_threshold"]["default"], 10000)
        self.assertEqual(memory["state_patch_min_evidence"]["default"], 3)
        self.assertEqual(memory["state_patch_min_turns"]["default"], 3)
        self.assertEqual(memory["state_patch_min_days"]["default"], 2)

    # -- 上游第 15 条：recent context floor + window ----------------------------

    def test_recent_context_combines_a_fifty_entry_floor_with_a_one_hour_window(self):
        runtime = self.section("runtime")
        self.assertEqual(runtime["context_entry_limit"]["default"], 50)
        self.assertEqual(runtime["context_time_window_minutes"]["default"], 60)

    # -- 上游第 16 条：separate optional perspective layer ----------------------

    def test_console_exposes_a_separate_optional_perspective_layer(self):
        story = self.section("story_defaults")
        self.assertEqual(story["perspective"]["default"], "")
        self.assertEqual(story["timezone"]["default"], "Asia/Shanghai")
        self.assertEqual(story["character_name"]["default"], "Unnamed character")
        # AstrBot 专属：人格导入与补充设定。
        self.assertEqual(story["persona_id"]["_special"], "select_persona")
        self.assertEqual(story["extra_setting"]["default"], "")

    # -- 上游第 17 条：native audio understanding -------------------------------

    def test_native_audio_understanding_remains_opt_in(self):
        audio = self.section("model_center")["audio"]["items"]
        self.assertIs(audio["enabled"]["default"], False)
        self.assertEqual(audio["out_format"]["default"], "mp3")
        self.assertEqual(audio["max_file_size_mb"]["default"], 10)
        self.assertEqual(audio["max_per_message"]["default"], 1)
        self.assertNotIn("voice_transcription", self.section("qq_access"))

    # -- 上游第 18 条：group willingness is opt-in and per-group ---------------

    def test_group_willingness_is_opt_in_and_scoped_to_each_group_rule(self):
        groups = self.section("qq_access")["group_chats"]["items"]
        willingness = groups["willingness"]["items"]
        self.assertIs(willingness["enabled"]["default"], False)
        self.assertEqual(willingness["decay_half_life_seconds"]["default"], 180)
        self.assertEqual(willingness["threshold"]["default"], 0.24)
        self.assertEqual(willingness["max_score"]["default"], 1)
        self.assertEqual(willingness["probability_amplifier"]["default"], 1.3)
        self.assertEqual(willingness["reply_cost"]["default"], 0.55)
        # 群规则自身字段。
        self.assertEqual(groups["response_mode"]["options"], ["mention-only", "always"])
        self.assertEqual(groups["response_mode"]["default"], "mention-only")
        self.assertEqual(groups["context_limit"]["default"], 20)
        self.assertEqual(groups["cooldown_seconds"]["default"], 60)

    # -- 上游第 19 条：Urge ----------------------------------------------------

    def test_urge_is_opt_in_with_frequency_presets(self):
        urge = self.section("urge")
        self.assertIs(urge["enabled"]["default"], False)
        self.assertEqual(urge["frequency"]["default"], "medium")
        self.assertEqual(urge["frequency"]["options"], ["low", "medium", "high", "custom"])
        self.assertEqual(urge["proactive_willingness_threshold"]["default"], 0.4)

    # -- 上游第 20 条：Alter System --------------------------------------------

    def test_alter_system_defaults(self):
        alter = self.section("alter_system")
        self.assertIs(alter["enabled"]["default"], True)
        self.assertEqual(alter["base_threshold"]["default"], 10)
        self.assertEqual(alter["density_factor"]["default"], 0.3)
        self.assertEqual(alter["same_direction_boost"]["default"], 0.05)
        self.assertEqual(alter["opposite_decay"]["default"], 0.15)
        self.assertEqual(alter["min_weight"]["default"], 0.2)
        self.assertEqual(alter["max_intensity"]["default"], 2)
        self.assertEqual(alter["temperature"]["default"], 0.3)
        self.assertEqual(alter["top_p"]["default"], 1)
        self.assertEqual(alter["max_tokens"]["default"], 400)
        self.assertEqual(alter["timeout"]["default"], 30000)

    # -- 上游第 21 条：Browser ------------------------------------------------

    def test_browser_defaults_are_conservative(self):
        browser = self.section("browser")
        self.assertIs(browser["enabled"]["default"], False)
        self.assertEqual(browser["mode"]["default"], "deferred-only")
        self.assertEqual(browser["mode"]["options"], ["deferred-only", "allow-immediate"])
        self.assertIs(browser["allow_search"]["default"], True)
        self.assertIs(browser["allow_visit"]["default"], True)
        self.assertIn("{query}", browser["search_url_template"]["default"])
        self.assertEqual(browser["allowed_domains"]["default"], [])
        self.assertEqual(browser["blocked_domains"]["default"], [])
        self.assertEqual(browser["max_concurrent_pages"]["default"], 1)
        self.assertEqual(browser["wait_until"]["options"],
                         ["domcontentloaded", "networkidle2"])
        self.assertIs(browser["allow_group_triggered_research"]["default"], False)
        self.assertIs(browser["log_observation_preview"]["default"], False)

    # -- 上游第 22 条：Shared Story -------------------------------------------

    def test_shared_story_defaults(self):
        shared = self.section("shared_story")
        self.assertIs(shared["auto_enroll_participants"]["default"], True)
        self.assertIs(shared["allow_cross_conversation_messages"]["default"], True)
        self.assertIs(shared["share_participant_details"]["default"], False)
        self.assertEqual(shared["max_cross_conversation_actions"]["default"], 1)
        self.assertEqual(shared["participant_context_limit"]["default"], 6)
        self.assertEqual(shared["manager_accounts"]["default"], [])

    # -- 上游第 23 条：OneBot / NapCat ----------------------------------------

    def test_onebot_gate_defaults_and_account_tables(self):
        qq = self.section("qq_access")
        self.assertIs(qq["enabled"]["default"], False)
        self.assertEqual(qq["bot_accounts"]["default"], [])
        self.assertEqual(qq["user_accounts"]["default"], [])
        self.assertEqual(qq["group_chats"]["default"], [])
        self.assertIs(qq["ignore_self_messages"]["default"], True)
        # 用户白名单行的字段（上游 OneBotUserAccount）。
        user = qq["user_accounts"]["items"]
        for key in ("qq", "label", "person_id", "profile", "relationship", "enabled"):
            self.assertIn(key, user)
        self.assertIs(user["enabled"]["default"], True)
        bot = qq["bot_accounts"]["items"]
        self.assertEqual(list(bot.keys()), ["qq", "label", "enabled"])

    # -- 上游第 24 条：已弃用的 chatRhythm 仍是完整兼容项 ----------------------

    def test_deprecated_chat_rhythm_remains_as_a_hidden_compat_section(self):
        rhythm = self.section("chat_rhythm")
        self.assertEqual(list(rhythm.keys()), [
            "enabled", "mode", "history_limit", "collapse_min_samples", "exhaust_limit",
        ])
        self.assertIs(rhythm["enabled"]["default"], True)
        self.assertEqual(rhythm["mode"]["default"], "balanced")
        self.assertEqual(rhythm["mode"]["options"], ["gentle", "balanced", "aggressive"])
        self.assertEqual(rhythm["history_limit"]["default"], 12)
        self.assertEqual(rhythm["collapse_min_samples"]["default"], 5)
        self.assertEqual(rhythm["exhaust_limit"]["default"], 6)
        self.assertIs(self.schema["chat_rhythm"]["invisible"], True)

    def test_deprecated_black_box_remains_as_a_hidden_compat_section(self):
        black_box = self.section("black_box")
        self.assertEqual(list(black_box.keys()), ["enabled", "health_report_minutes"])
        self.assertIs(black_box["enabled"]["default"], False)
        self.assertIs(self.schema["black_box"]["invisible"], True)

    # -- 覆盖度对账 ------------------------------------------------------------

    def test_every_upstream_field_is_covered(self):
        missing = []
        for group, fields in UPSTREAM_FIELDS.items():
            present = set(self.section(group).keys())
            for camel in fields:
                if snake(camel) not in present:
                    missing.append(f"{group}.{camel} -> {snake(camel)}")
        self.assertEqual(missing, [], f"未覆盖的上游字段：{missing}")

    #: 上游没有、本移植版新增的字段（AstrBot 生态接入）。
    LOCAL_ONLY_FIELDS = {
        "story_defaults": {"persona_id", "extra_setting"},
    }

    def test_upstream_field_count_matches(self):
        expected = sum(len(v) for v in UPSTREAM_FIELDS.values())
        actual = sum(len(self.section(g)) for g in UPSTREAM_FIELDS)
        local_only = sum(len(v) for v in self.LOCAL_ONLY_FIELDS.values())
        self.assertEqual(actual, expected + local_only,
                         f"上游 {expected} 项 + 本移植版新增 {local_only} 项"
                         f"，本插件实际 {actual} 项")

    def test_compat_groups_cover_the_hidden_legacy_fields(self):
        for group, fields in UPSTREAM_COMPAT_FIELDS.items():
            present = set(self.section(group).keys())
            for camel in fields:
                self.assertIn(snake(camel), present, f"{group}.{camel} 未保留")
            self.assertIs(self.schema[group].get("invisible"), True,
                          f"{group} 必须隐藏（invisible=true）")

    def test_deep_defaults_match_upstream(self):
        for path, key, expected in DEEP_DEFAULTS:
            with self.subTest(path=".".join(path), key=key):
                node = self.schema
                for step in path:
                    node = node[step]["items"]
                self.assertIn(key, node)
                self.assertEqual(node[key]["default"], expected)

    def test_deep_sections_are_complete(self):
        model = self.section("model_center")
        # providers 行的完整字段数（上游 ProviderIdentity + 模式字段 + ProviderAssignments）。
        self.assertGreaterEqual(len(model["providers"]["items"]), 21)
        # 提示词四件套（上游 3.4 节）。
        for key in ("main_prompt", "format_prompt", "fixed_prompt", "style_prompt"):
            self.assertIn(key, model)
            self.assertIn(key, self.section("prompts"))
        self.assertEqual(
            model["main_prompt"]["default"],
            self.section("prompts")["main_prompt"]["default"],
        )
        self.assertEqual(
            model["style_prompt"]["default"],
            self.section("prompts")["style_prompt"]["default"],
        )

    # -- AstrBot 格式铁律（AGENTS.md 坑 1） -------------------------------------

    def test_every_type_is_in_the_astrbot_allowed_set(self):
        bad = [(path, spec.get("type")) for path, _, spec in iter_fields(self.schema)
               if spec.get("type") not in ALLOWED_TYPES]
        self.assertEqual(bad, [], f"非法 type：{bad}")
        # 明确点名常见的标准 JSON Schema 拼写。
        for wrong in ("integer", "number", "array", "boolean", "str", "dict"):
            self.assertNotIn(wrong, ALLOWED_TYPES)

    def test_objects_use_items_never_properties(self):
        offenders = [path for path, _, spec in iter_fields(self.schema)
                     if "properties" in spec]
        self.assertEqual(offenders, [], f"object 误用 properties：{offenders}")
        without_items = [path for path, _, spec in iter_fields(self.schema)
                         if spec.get("type") == "object" and "items" not in spec]
        self.assertEqual(without_items, [], f"object 缺少 items：{without_items}")

    def test_lists_declare_an_empty_or_list_default(self):
        for path, _, spec in iter_fields(self.schema):
            if spec.get("type") != "list":
                continue
            with self.subTest(path=path):
                self.assertIn("default", spec, "list 必须显式给出 default")
                self.assertIsInstance(spec["default"], list)

    def test_object_defaults_are_ignored_by_astrbot_but_stay_objects(self):
        # AstrBot `_config_schema_to_default_config` 对 object 递归展开 items，
        # default 不参与落盘；这里只要求不要写成非 dict 造成误读。
        for path, _, spec in iter_fields(self.schema):
            if spec.get("type") == "object" and "default" in spec:
                with self.subTest(path=path):
                    self.assertIsInstance(spec["default"], dict)

    def test_enum_defaults_are_members_of_their_options(self):
        for path, _, spec in iter_fields(self.schema):
            if "options" not in spec:
                continue
            with self.subTest(path=path):
                self.assertIn(spec["default"], spec["options"],
                              f"{path} 的 default 不在 options 内")

    def test_defaults_have_the_declared_python_shape(self):
        shapes = {"bool": bool, "int": int, "float": float, "string": str,
                  "text": str, "list": list, "object": dict}
        for path, _, spec in iter_fields(self.schema):
            type_ = spec.get("type")
            if type_ not in shapes:
                continue
            if "default" not in spec:
                # list[dict] 本身是容器，AstrBot 不读它的 default。
                continue
            default = spec.get("default")
            with self.subTest(path=path, type=type_):
                if type_ == "float":
                    self.assertIsInstance(default, (int, float))
                    self.assertNotIsInstance(default, bool)
                elif type_ == "int":
                    self.assertIsInstance(default, int)
                    self.assertNotIsInstance(default, bool)
                else:
                    self.assertIsInstance(default, shapes[type_])

    def test_every_field_has_a_short_chinese_description(self):
        for path, _, spec in iter_fields(self.schema):
            with self.subTest(path=path):
                desc = spec.get("description", "")
                self.assertTrue(desc, "description 不能为空")
                # 配置项标题要简洁：不放 URL、不写长句。
                self.assertNotIn("http", desc.lower())
                self.assertLessEqual(len(desc), 60, f"description 过长：{desc!r}")

    def test_top_level_keys_are_snake_case(self):
        for key in self.schema:
            self.assertRegex(key, r"^[a-z][a-z0-9_]*$", f"{key} 不是 snake_case")

    def test_schema_file_is_utf8_with_bom(self):
        with open(SCHEMA_PATH, "rb") as fp:
            head = fp.read(3)
        self.assertEqual(head, b"\xef\xbb\xbf", "_conf_schema.json 必须带 BOM")

    def test_schema_file_is_plain_json_object(self):
        with open(SCHEMA_PATH, encoding="utf-8-sig") as fp:
            parsed = json.load(fp)
        self.assertIsInstance(parsed, dict)
        for key, spec in parsed.items():
            self.assertIsInstance(spec, dict, f"{key} 的值必须是对象")
            self.assertIn("type", spec, f"{key} 缺少 type")


class ReleaseConsistencyTest(unittest.TestCase):
    """上游 `test/release-consistency.test.ts` 的等价移植。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.meta_version = load_meta_version()

    def test_metadata_version_matches_the_runtime_meta_constant(self):
        metadata = read(METADATA_PATH)
        match = re.search(r"^version:\s*(\S+)\s*$", metadata, re.MULTILINE)
        self.assertIsNotNone(match, "metadata.yaml 缺少 version 字段")
        plugin_version = match.group(1)
        self.assertRegex(plugin_version, r"^v?\d+\.\d+\.\d+",
                         "metadata.yaml 的 version 必须是 vX.Y.Z 或 X.Y.Z")
        # 上游：`manifest.version === HDS_INTERLUDE_VERSION`。
        # 本插件 metadata.yaml 记的是**插件版本**，core/meta.py 记的是**上游版本**。
        # 二者语义不同，因此断言 v 前缀约定自洽，而不是硬编码 v0.1.0。
        self.assertNotEqual(plugin_version, "", "metadata.yaml 的 version 不能为空")
        self.assertRegex(plugin_version, r"^v?[0-9]+\.[0-9]+\.[0-9]+")

    def test_changelog_top_entry_matches_the_plugin_version(self):
        metadata = read(METADATA_PATH)
        plugin_version = re.search(r"^version:\s*(\S+)\s*$", metadata, re.MULTILINE).group(1)
        changelog = read(CHANGELOG_PATH)
        headings = re.findall(r"^##\s+(\S+)", changelog, re.MULTILINE)
        self.assertTrue(headings, "CHANGELOG.md 没有 ## 二级标题")
        self.assertEqual(headings[0].lstrip("v"), plugin_version.lstrip("v"),
                         "CHANGELOG.md 顶部条目必须与 metadata.yaml 的 version 一致")

    def test_plugin_metadata_declares_the_expected_identity(self):
        metadata = read(METADATA_PATH)
        self.assertIn("name: astrbot_plugin_hds_interlude", metadata)
        self.assertIn("repo: https://github.com/KelaLeaf/astrbot_plugin_hds_interlude", metadata)
        self.assertRegex(metadata, r"astrbot_version:\s*\"?[><=~!]", "必须声明 astrbot_version")

    def test_upstream_version_constant_records_the_snapshot_version(self):
        self.assertEqual(self.meta_version, "1.0.1-beta6-rebuild")
        self.assertRegex(read(META_PY_PATH),
                         r'HDS_INTERLUDE_VERSION\s*=\s*["\']1\.0\.1-beta6-rebuild["\']')

    def test_upstream_package_json_matches_the_snapshot_version(self):
        with open(UPSTREAM_PACKAGE_PATH, encoding="utf-8") as fp:
            manifest = json.load(fp)
        self.assertEqual(manifest["version"], self.meta_version,
                         "upstream/package.json 的版本必须与 core/meta.py 记录一致")

    def test_upstream_sync_documents_the_version_mapping(self):
        sync = read(UPSTREAM_SYNC_PATH)
        self.assertIn("## 版本对应", sync)
        rows = re.findall(r"^\|\s*v?[\d.]+[^|]*\|\s*([^|]+?)\s*\|", sync, re.MULTILINE)
        self.assertTrue(rows, "docs/UPSTREAM_SYNC.md 的版本对应表没有数据行")
        # 该文档由移植收尾统一更新；此处只要求「表在 + 记录的上游版本存在」。
        recorded = " ".join(rows)
        self.assertRegex(recorded, r"\d+\.\d+",
                         "版本对应表必须写明上游版本号")

    def test_port_plan_records_the_upstream_version(self):
        plan = read(os.path.join(REPO_ROOT, "docs", "PORT_PLAN.md"))
        self.assertIn(self.meta_version, plan,
                      "docs/PORT_PLAN.md 必须注明上游快照版本")

    def test_config_map_documents_every_schema_section(self):
        """`docs/CONFIG_MAP.md` 必须覆盖 schema 的每个顶层分组。"""
        config_map = read(os.path.join(REPO_ROOT, "docs", "CONFIG_MAP.md"))
        schema = load_schema()
        for key in schema:
            with self.subTest(section=key):
                self.assertIn(f"`{key}`", config_map,
                              f"CONFIG_MAP.md 缺少分组 {key}")

    def test_config_map_records_upstream_camel_case_keys(self):
        config_map = read(os.path.join(REPO_ROOT, "docs", "CONFIG_MAP.md"))
        for camel in ("storyDefaults", "onebot", "sharedStory", "schedulePreplan",
                      "timelineDirector", "chatActions", "alterSystem", "blindMode"):
            with self.subTest(key=camel):
                self.assertIn(f"`{camel}`", config_map)

    def test_config_map_has_the_required_columns(self):
        config_map = read(os.path.join(REPO_ROOT, "docs", "CONFIG_MAP.md"))
        self.assertIn("| 上游键 | 本插件键 | 类型 | 默认值 | 说明 |", config_map)


if __name__ == "__main__":
    unittest.main(verbosity=2)
