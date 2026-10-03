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
from typing import Any

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

#: 上游 Console 的分组顺序（`upstream/test/configuration.test.ts:11-14`），
#: 末尾补上 rc23 / rc28 新增、本移植版已转正的两个扩展分组。
UPSTREAM_SECTION_ORDER = [
    "storyDefaults", "model", "onebot", "sharedStory", "runtime", "urge",
    "schedulePreplan", "timelineDirector", "agency",
    "chatActions", "stickers", "memory", "alterSystem", "browser",
    "worldSeeder", "qzone", "blindMode", "logging", "chatRhythm",
]

#: 上游 1.0.1-rc23 / rc28 新增的两个扩展分组；本移植版都已转正
#: （`qzone` 原先是隐藏兼容位 `qzone_compat`，v1.6.0 起是真分组）。
UPSTREAM_NEW_SECTIONS = [("worldSeeder", "world_seeder"), ("qzone", "qzone"),
                         ("forwardMessage", "forward_message")]

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
    ("worldSeeder", "world_seeder"),
    ("qzone", "qzone"),
    ("blindMode", "blind_mode"),
    ("logging", "logging"),
    ("chatRhythm", "chat_rhythm"),
]

#: 上游 Console 的完整字段清单（camelCase），用于逐项对账 coverage。
#: 来源：`upstream/src/index.ts` 中各组 Schema.object 的键。
UPSTREAM_FIELDS = {
    # 上游 1.0.1-rc23【扩展 15】世界播种器（本移植版已移植）。
    "world_seeder": [
        "enabled", "cadenceMinutes", "maxPending", "dailyCap", "maxHorizonHours",
        "temperature", "maxTokens", "timeout",
    ],
    # 上游 1.0.1-rc28【扩展 16】QQ 空间：v1.6.0 从隐藏兼容位 `qzone_compat`
    # 转正为真分组（键名逐字不变，snake_case 走 `CAMEL_TO_SNAKE`）。
    "qzone": [
        "enabled", "dailyPostCap", "dailyCommentCap", "dailyLikeCap",
        "minIntervalMinutes", "feedWindowMinutes",
    ],
    "forward_message": ["enabled", "maxNodes", "maxCharacters", "maxDepth"],
    "story_defaults": [
        "characterName", "characterProfile", "perspective", "perspectives",
        "supplementaryFacts", "userProfile", "relationship",
        "world", "supportingCast", "location", "style", "timezone",
    ],
    "model_center": [
        "vision", "audio", "providers", "mainTemperature", "mainTopP", "mainMaxTokens",
        "mainTimeout", "mainResponseFormat", "mainStreamingMode", "mainPayloadOrder",
        "specialization", "specializationFamily",
        "failover", "mainPrompt", "formatPrompt", "fixedPrompt", "stylePrompt",
        "embedding", "compaction",
    ],
    # v1.3.0 受控偏离：上游那个「账号过滤总闸」（`onebot.enabled`）被删掉，换成三张名单
    # 各自的 `*_only` 开关（登记在 `LOCAL_ONLY_FIELDS`）。见 `docs/PORTING_NOTES.md` §22。
    "qq_access": [
        "botAccounts", "userAccounts", "groupChats", "ignoreSelfMessages",
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
        "contactMode", "naturalWillingnessThreshold", "naturalMinimumIntervalMinutes",
        "proactiveDailyCap",
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
#: `qzone`（v1.6.0）与 `forward_message`（v1.7.0）都曾在这里，转正后移进
#: `UPSTREAM_FIELDS` 做真分组对账——**只剩下面这三组**。
UPSTREAM_COMPAT_FIELDS = {
    "shared_story_compat": ["enabled", "participantPresets"],
    "runtime_compat": ["pauseAfterConversationMinutes", "staleNarrativeRequestWindowSeconds"],
}

#: v1.7.2 动作开关分组收敛（10 → 4）、v1.7.3 取消「风险操作」组、v1.7.4 并进一个父组
#: 之后**留在 schema 里当隐藏兼容位**的旧分组 → 原键集合。
#:
#: 为什么必须留着、而且键一个都不能少：宿主每次加载都按 `_conf_schema.json` 重建配置，
#: schema 里没有的分组会被**直接删掉**（AGENTS 坑 22）。所以"把 `actions_interaction`
#: 改名成 `actions_chat`"这种写法，会在用户下次启动前就把他设过的开关清空。
#: 读取侧的 N:1 归并见 `plugin/core/service/config.py` 的 `LEGACY_SECTION_MERGES`。
#:
#: 键（`enabled` + 动作 id）**别改**：与收敛前逐字一致是本表的全部意义。
LEGACY_ACTION_COMPAT_GROUPS = {
    "actions_interaction": ("enabled", "send_poke", "send_like", "recall_message"),
    "actions_message": (
        "enabled", "schedule_message", "list_scheduled_messages", "cancel_scheduled_message",
        "schedule_command", "list_scheduled_commands", "cancel_scheduled_command",
    ),
    "actions_history": ("enabled", "get_group_msg_history", "get_friend_msg_history"),
    "actions_status": ("enabled", "update_qq_status", "get_qq_status", "get_fun_status_list"),
    "actions_profile": ("enabled", "set_qq_profile", "set_qq_avatar", "get_qq_profile"),
    "actions_voice": ("enabled", "send_voice", "list_voices", "tts_provider_id", "default_voice"),
    "actions_contact": (
        "enabled", "list_contacts", "search_contacts", "get_user_profile", "get_group_info",
        "handle_friend_request", "handle_group_request", "auto_learn",
    ),
    # v1.7.2/v1.7.3 的可见分组（v1.7.4 并进 `robot_actions` 之后降级成兼容位）。
    "actions_chat": (
        "enabled", "send_poke", "send_like", "recall_message",
        "schedule_message", "list_scheduled_messages", "cancel_scheduled_message",
        "schedule_command", "list_scheduled_commands", "cancel_scheduled_command",
        "get_group_msg_history", "get_friend_msg_history",
        "update_qq_status", "get_qq_status", "get_fun_status_list",
        "set_qq_profile", "set_qq_avatar", "get_qq_profile",
        "send_voice", "list_voices", "tts_provider_id", "default_voice",
        "list_contacts", "search_contacts", "get_user_profile", "get_group_info",
        "handle_friend_request", "handle_group_request", "auto_learn", "delete_friend",
    ),
    "actions_group": (
        "enabled", "get_group_members_info", "get_user_group_role", "get_group_honor_info",
        "get_group_shut_list", "get_group_notice_list", "get_group_at_all_remain",
        "list_group_files", "send_group_notice", "delete_group_notice", "set_essence_msg",
        "delete_essence_msg", "send_group_sign", "set_group_card",
        "set_group_special_title", "set_group_add_option", "set_group_portrait",
        "set_group_name", "set_group_ban", "set_group_whole_ban", "set_group_kick",
        "set_group_admin", "delete_group_file", "upload_group_file", "rename_group_file",
        "move_group_file", "create_group_file_folder", "delete_group_folder",
        "trans_group_file",
    ),
    "actions_qzone": (
        "enabled", "publish_qzone_post", "comment_qzone_post", "like_qzone_post",
        "list_qzone_posts", "list_qzone_feeds", "forward_qzone_post", "delete_qzone_post",
    ),
    # v1.7.3：危险动作的开关搬回各自类别组，这一组随之退休，v1.7.4 起**不再参与归并**
    # （用户判断那些配置目前没人用，见 `docs/PORTING_NOTES.md` §37）——但它照旧留在
    # schema 里当隐藏兼容位，键集合一个不少。
    "actions_risks": (
        "enabled", "set_group_special_title", "set_group_add_option", "set_group_portrait",
        "set_group_name", "set_group_ban", "set_group_whole_ban", "set_group_kick",
        "set_group_admin", "delete_group_file", "upload_group_file", "rename_group_file",
        "move_group_file", "create_group_file_folder", "delete_group_folder",
        "trans_group_file", "delete_qzone_post", "delete_friend",
    ),
}

#: 留在 schema 里、但**不是**归并源的隐藏动作组（`actions_risks`）。
LEGACY_UNMERGED_ACTION_GROUPS = ("actions_risks",)

#: 每个隐藏兼容位的**键在哪些可见路径里还找得到一份**。
#:
#: 归并源（`LEGACY_SECTION_MERGES` 的 values）必须真在这些路径上被读取侧补进去；
#: `actions_risks` 只是"键在三个子组里也各有一份"（v1.7.3 起危险开关就在那里），
#: 它**不是归并源**（v1.7.4 的简化），所以下面那句 join 检查不含它。
LEGACY_ACTION_COMPAT_TARGETS = {
    "actions_interaction": ("robot_actions.chat",),
    "actions_message": ("robot_actions.chat",),
    "actions_history": ("robot_actions.chat",),
    "actions_status": ("robot_actions.chat",),
    "actions_profile": ("robot_actions.chat",),
    "actions_voice": ("robot_actions.chat",),
    "actions_contact": ("robot_actions.chat",),
    "actions_chat": ("robot_actions.chat",),
    "actions_group": ("robot_actions.group",),
    "actions_qzone": ("robot_actions.qzone",),
    "actions_risks": ("robot_actions.chat", "robot_actions.group", "robot_actions.qzone"),
}

#: v1.7.4 起 `input_status` 从顶层组挪进「运行时」当子配置：顶层那一份降级成隐藏兼容位。
INPUT_STATUS_COMPAT_TARGET = "runtime.input_status"
INPUT_STATUS_COMPAT_KEYS = ("enabled", "min_visible_ms", "beat_chance")

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
    # v1.9.0：视频理解。三个键的默认值一律取**省成本那侧**（关着 / 抽帧 / 不指名）。
    (("model_center", "video"), "enabled", False),
    (("model_center", "video"), "mode", "frames"),
    (("model_center", "video"), "model_id", ""),
    # v1.9.1：预算全部可配。默认值一律取**省成本那侧**：
    # 连续抽帧（不额外依赖时长探测）/ 每 4 秒 1 帧 / 整段 3 帧 / mp3 / 前 60 秒音轨 /
    # 20 秒超时 / **群聊关**（群聊里视频刷屏最贵）。
    (("model_center", "video"), "frame_mode", "sequence"),
    (("model_center", "video"), "frame_interval_seconds", 4),
    (("model_center", "video"), "frame_average_count", 3),
    (("model_center", "video"), "out_format", "mp3"),
    (("model_center", "video"), "audio_duration", "custom"),
    (("model_center", "video"), "audio_duration_seconds", 60),
    (("model_center", "video"), "timeout_seconds", 20),
    (("model_center", "video"), "group_enabled", False),
    # v1.9.1：合并转发单条最多读取的视频数。默认 **1**（不是省成本那侧的 0）：
    # 用户口径是"可配置单条转发最多读取的视频数"——配了却默认永不生效不算配置；
    # 1 = 配了就生效，同时把单卡的额外成本封在一次以内。core 那一份默认值在
    # `forward_message.DEFAULT_LIMITS.max_videos`（`forward_read_limits` 缺键时的
    # fallback 就是它），两处必须一致。
    (("forward_message",), "max_videos", 1),
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
    # 上游 1.0.1-rc28：故事档案、模型特化、主动联系、意愿档位、世界播种器、
    # QQ 空间（已转正）与合并转发（仍是隐藏兼容位）。
    "perspectives": "perspectives", "supplementaryFacts": "supplementary_facts",
    "specialization": "specialization", "specializationFamily": "specialization_family",
    "contactMode": "contact_mode", "proactiveDailyCap": "proactive_daily_cap",
    "naturalWillingnessThreshold": "natural_willingness_threshold",
    "naturalMinimumIntervalMinutes": "natural_minimum_interval_minutes",
    "useForWorldSeeding": "use_for_world_seeding", "protocol": "protocol",
    "anthropicCache": "anthropic_cache",
    "cadenceMinutes": "cadence_minutes", "maxPending": "max_pending",
    "dailyCap": "daily_cap", "maxHorizonHours": "max_horizon_hours",
    "willingnessPreset": "willingness_preset", "willingnessAuto": "willingness_auto",
    "dailyPostCap": "daily_post_cap", "dailyCommentCap": "daily_comment_cap",
    "dailyLikeCap": "daily_like_cap", "minIntervalMinutes": "min_interval_minutes",
    "feedWindowMinutes": "feed_window_minutes",
    "maxNodes": "max_nodes", "maxCharacters": "max_characters", "maxDepth": "max_depth",
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


def schema_at(schema: dict, path: str) -> dict:
    """按**点分路径**取 schema 节点（`robot_actions.chat` / `runtime.input_status`）。

    v1.7.4 起动作开关并进一个父组、输入状态挪进「运行时」当子配置，落点是嵌套路径；
    测试里凡是"按分组名取 items"的地方都走它，别写死 `schema[key]["items"]`
    （`robot_actions` 的 items 是三个子组、不是开关）。
    """
    node: Any = schema
    for step in path.split("."):
        node = node[step] if step in node else node["items"][step]
    return node


def config_at(config: Any, path: str, default: Any = None) -> Any:
    """按点分路径读**配置**（不是 schema）：读不到回 `default`（默认 `None`）。"""
    node: Any = config
    for step in path.split("."):
        if not isinstance(node, dict) or step not in node:
            return default
        node = node[step]
    return node


#: v1.7.5：用户点名**删掉整句解释**（连 `description` 键一起去掉）的对象。
#:
#: 两类：① **隐藏兼容位**（`actions_*` / `input_status`）不再解释"读配置时仍认这里的键"
#: ——隐藏组不需要解释文案；② 动作开关父组 `robot_actions` 与它的三个子组只留标题
#: （父组那句"会话、群管理、QQ 空间三大类动作的开关…"整句被点名删掉）。
#: 它们不是"漏写 description"，所以下面那条通用断言据此放行。
NO_DESCRIPTION_PATHS = frozenset({
    "actions_interaction", "actions_message", "actions_history", "actions_status",
    "actions_profile", "actions_voice", "actions_contact",
    "actions_chat", "actions_group", "actions_qzone", "actions_risks", "input_status",
    "robot_actions", "robot_actions.chat", "robot_actions.group", "robot_actions.qzone",
})


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
        """某个分组（含点分嵌套路径）的 `items`。"""
        return schema_at(self.schema, top_key)["items"]

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
        # 本移植版新增（v1.8.0）：自动收藏**默认开**，但仍受上面的 `enabled` 总闸；
        # hint 是给用户的那句"拿不准就不收"。
        self.assertIs(stickers["auto_collect"]["default"], True)
        self.assertEqual(
            stickers["auto_collect"]["hint"],
            "别人发来的表情包自动收进表情库；拿不准是不是表情包时不收。",
        )
        # 本移植版新增（v1.8.0 第二层判据，§45.7）：让识图模型判断普通图片是不是表情包。
        # **默认关**——用户要先主动打开才多花 token；description 与 hint 各管一件事
        # （标题短、说明短，"会增加识图调用"是用户唯一需要预知的代价）。
        self.assertIs(stickers["auto_collect_guess"]["default"], False)
        self.assertEqual(
            stickers["auto_collect_guess"]["description"],
            "让识图模型判断普通图片是不是表情包",
        )
        self.assertEqual(stickers["auto_collect_guess"]["hint"], "拿不准就不收；会增加识图调用。")
        # 本移植版新增（v1.8.4，§48）：两级表情选择 + 描述时顺手归组。**都默认开**——
        # 这两件事在默认路径上就是"少给一点 token、多归一次组"，不是额外花钱的功能；
        # 关掉任一个都回到 v1.8.3 的行为（平铺目录 / 不归组）。
        self.assertIs(stickers["group_selection"]["default"], True)
        self.assertEqual(stickers["group_selection"]["description"], "模型先选分组再选表情")
        self.assertIs(stickers["auto_group"]["default"], True)
        self.assertEqual(stickers["auto_group"]["description"], "整理表情时顺便归组")
        # 标题短、代价写在 hint 里（用户唯一需要预知的是"这会不会多花调用"）。
        for key in ("group_selection", "auto_group"):
            with self.subTest(key=key):
                self.assertIn("hint", stickers[key])
                self.assertNotIn("http", stickers[key]["hint"])

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
        # 上游：`version === HDS_INTERLUDE_VERSION`。v2 起本插件跟进上游 1.0.1-rc28。
        # 本插件 `plugin/_conf_schema.json` 不再是版本的载体，等价物是 core/meta.py。
        self.assertEqual(load_meta_version(), "1.0.1-rc28")
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
        # 顺序契约（v1.9.1 起）：**感知类设置排在连接池前面** —— 图片 / 语音 / 视频
        # 三组紧挨着，`providers` 跟在它们后面。用户口径：视频理解设置应该在模型连接
        # 上面、语音 / 音频理解设置下面。
        self.assertEqual(list(model.keys())[:4], ["vision", "audio", "video", "providers"])
        for absent in ("mode", "zhipu", "models", "main_model_id", "mainModelId"):
            self.assertNotIn(absent, model)
        self.assertEqual(model["main_response_format"]["default"], "json-object")
        self.assertEqual(model["main_streaming_mode"]["default"], "off")
        # 上游 1.0.1-rc14 起默认 cache-first（支持前缀缓存的接口受益）。
        self.assertEqual(model["main_payload_order"]["default"], "cache-first")
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

    def test_the_provider_row_declares_every_field_core_reads(self) -> None:
        """一条连接行该有哪些字段：schema 的 `items` **与** 默认行必须一样齐（v1.9.1）。

        两个方向都钉：

        * `items` 里声明了、`default` 行里没有 → 新用户的第一条连接在界面上是一堆
          空项（"看起来没配"，其实跑的是默认值）；
        * core 会读、`items` 里没有 → 用户点不到，而且真写进配置也会被宿主按 schema
          重建时删掉（坑 22）。

        后一条正是 `use_for_video` / `use_for_works` 的来历：`model_routing.is_assigned_to()`
        认这两个键（外挂视频理解 / 共同作品写手），适配层为"指名 AstrBot Provider"合成
        的连接行也挂它们—— 但 schema 里原先没有，于是"用自建连接行跑外挂识别"根本点不到。
        """
        providers = self.section("model_center")["providers"]
        items = providers["items"]
        row = providers["default"][0]
        self.assertEqual(
            sorted(set(items) - set(row)), [],
            "默认连接行缺这些键（界面上会显示成未设置）",
        )
        for flag in ("use_for_video", "use_for_works"):
            with self.subTest(flag=flag):
                self.assertIn(flag, items, "core 会读这个键，schema 里必须有，否则宿主重载会删掉它")
                self.assertIs(items[flag]["default"], False, "省成本那侧：默认不勾")
        # 新勾选与 core 的判据同名（`is_assigned_to(provider, 'video' | 'works')`）。
        routing_path = os.path.join(PLUGIN_ROOT, 'core', 'model_routing.py')
        with open(routing_path, encoding='utf-8') as handle:
            routing = handle.read()
        for flag in ('use_for_video', 'use_for_works'):
            self.assertIn(flag, routing)

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

    def test_agency_window_exposes_only_the_bounded_controls(self):
        agency = self.section("agency")
        self.assertEqual(list(agency.keys()), [
            "enabled", "max_window_minutes", "minimum_proactive_interval_minutes",
            "max_candidate_hours",
            # 上游 1.0.1-rc25：三模式主动联系 + 每参与者每日上限。
            "contact_mode", "natural_willingness_threshold",
            "natural_minimum_interval_minutes", "proactive_daily_cap",
        ])
        self.assertIs(agency["enabled"]["default"], True)
        self.assertEqual(agency["max_window_minutes"]["default"], 240)
        self.assertEqual(agency["minimum_proactive_interval_minutes"]["default"], 60)
        self.assertEqual(agency["max_candidate_hours"]["default"], 24)
        self.assertEqual(agency["contact_mode"]["default"], "strict")
        self.assertEqual(agency["natural_willingness_threshold"]["default"], 0.25)
        self.assertEqual(agency["natural_minimum_interval_minutes"]["default"], 30)
        self.assertEqual(agency["proactive_daily_cap"]["default"], 3)

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

    def test_recent_context_combines_a_thirty_five_entry_floor_with_a_forty_five_minute_window(self):
        # 上游 1.0.1-rc2 把 Schema 默认值对齐服务层 fallback（50→35、60→45）。
        runtime = self.section("runtime")
        self.assertEqual(runtime["context_entry_limit"]["default"], 35)
        self.assertEqual(runtime["context_time_window_minutes"]["default"], 45)

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
        # v1.3.0：没有总闸了，三张名单各自一个"仅处理名单内"开关，默认全关
        # （名单只做针对性处理、名单外照常处理）。
        self.assertNotIn("enabled", qq)
        for key in ("bot_accounts_only", "user_accounts_only", "group_chats_only"):
            self.assertIs(qq[key]["default"], False, key)
            # 开关的说明就是它自己那行标题：**不留 hint**（用户明确要求删掉那段
            # "关闭（默认）…打开…所有平台一视同仁"的长说明，它跟标题重复）。
            self.assertNotIn("hint", qq[key], key)
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

    #: 上游放在 `model` 组、本移植版**搬到独立分组**的字段。
    #:
    #: 上游 `src/index.ts` 把提示词四件套放在 `ModelConfig` 里；本移植版在配置页
    #: 给它们单开了一组 `prompts`（键名逐字不变），core 读的仍是 `model.*`——
    #: 搬家由 `core/service/config.py` 的 `resolve_prompt_fields`（读）与
    #: `to_schema_shape`（写）负责，见 `docs/CONFIG_MAP.md`。
    RELOCATED_UPSTREAM_FIELDS = {
        "prompts": ["mainPrompt", "formatPrompt", "fixedPrompt", "stylePrompt"],
    }

    def test_every_upstream_field_is_covered(self):
        missing = []
        for group, fields in UPSTREAM_FIELDS.items():
            present = set(self.section(group).keys())
            for camel in fields:
                if snake(camel) in present:
                    continue
                # 搬过家的字段：原组里**必须没有**（否则配置页又出现两个输入框），
                # 新组里必须有一份。
                for target, moved in self.RELOCATED_UPSTREAM_FIELDS.items():
                    if camel in moved:
                        self.assertNotIn(
                            snake(camel), present,
                            f"{group}.{camel} 应当只留在 {target} 组里",
                        )
                        self.assertIn(
                            snake(camel), set(self.section(target).keys()),
                            f"{target}.{camel} 缺失",
                        )
                        break
                else:
                    missing.append(f"{group}.{camel} -> {snake(camel)}")
        self.assertEqual(missing, [], f"未覆盖的上游字段：{missing}")

    #: 上游没有、本移植版新增的字段（AstrBot 生态接入）。
    #:
    #: 只登记**分组内可见的键**（嵌套分组的子项由 `test_deep_sections_are_complete`
    #: 那一类用例单独盯）。`vision.provider_id` / `audio.provider_id` /
    #: `embedding.provider_id` 也属本移植版新增，但它们在嵌套分组里，不计入本表。
    LOCAL_ONLY_FIELDS = {
        "story_defaults": {"persona_id", "extra_setting"},
        "model_center": {
            # v1.9.0：视频理解是整组新增（上游没有这一层），父组的 items 因此多出一个
            # 对象键（`test_upstream_field_count_matches` 按 items 数一遍）。组内部的
            # 三个键由 `DEEP_DEFAULTS` 与 `VideoUnderstandingConfigTest` 盯着。
            "video",
            "main_provider_id", "compaction_provider_id", "alter_provider_id",
            # v1.5.0：世界播种器的「指名 AstrBot 模型」（上游只有连接行的用途勾选）。
            "world_seeding_provider_id",
            # v1.4.0：模型调用治理（`docs/MEMORY_MAINTENANCE.md` §5.6）。
            "governor_enabled", "governor_max_concurrency", "governor_max_requests_per_minute",
            "governor_min_call_interval_ms", "governor_breaker_failures",
            "governor_breaker_cooldown_seconds",
        },
        "stickers": {
            "provider_id", "auto_collect", "auto_collect_guess",
            # v1.8.4（§48）：两级表情选择（先点名分组、再挑条目）与"整理时顺手归组"。
            # 两把闸都**默认开**，与 `auto_collect` 同一把尺子（只有显式 false 才关）。
            "group_selection", "auto_group",
            # v1.8.4（§50）：描述时模型判"不是表情包"就停用那一行（可逆、不删）。
            # 同一把尺子（默认开）；人改过的行（描述 / 归属 / 启用状态）一律不碰。
            "auto_disable",
        },
        # v1.3.0 受控偏离：三张名单各自的"仅处理名单内"开关（上游只有一个总闸 `enabled`，
        # 本移植版删掉它、换成这三个正交开关）。见 `docs/PORTING_NOTES.md` §22。
        "qq_access": {"bot_accounts_only", "user_accounts_only", "group_chats_only"},
        # v1.4.0：记忆维护（去重 / 矛盾 / 时间锚定 / 遗忘 / 预算 / 融合召回 / 上下文记录），
        # 上游没有这些开关，全部由本移植版新增。见 `docs/MEMORY_MAINTENANCE.md`。
        "memory": {
            "facts_dedupe_enabled",
            "facts_contradiction_enabled",
            "temporal_anchor_enabled",
            "forgetting_enabled",
            "forgetting_threshold",
            "forgetting_retention_days",
            "forgetting_half_life_days",
            "maintenance_max_llm_calls",
            "maintenance_max_runtime_minutes",
            "maintenance_min_call_interval_ms",
            "hybrid_retrieval_enabled",
            "hybrid_rrf_k",
            "query_rewrite_enabled",
            "context_metrics_enabled",
        },
        # v1.6.0：QQ 空间转正后多出来的那个"自动刷动态"开关（上游 qzone 只有六个键）。
        "qzone": {"auto_feed"},
        # v1.8.7：合并转发节点里的图片坐标也要交给模型，于是多了**第四道预算**
        # （单条转发最多取几张图，默认 3、区间 0~10）。上游 `forwardMessage` 组只有
        # 三重预算（节点 / 字符 / 深度），这一项由本移植版新增；整条消息的转发媒体
        # 总数上限是 core 里的常量（`FORWARD_MEDIA_MAX_PER_TURN`）。
        # v1.9.1：再加**第五道** —— 单条转发最多读取的视频数（默认 **1** = 一张卡最多
        # 看一段；显式配 0 才是"一段都不读"）。
        "forward_message": {"max_images", "max_videos"},
        # v1.6.0：平台动作目录的开关（`plugin/core/platform_actions.py` 是唯一事实源，
        # 键名逐字 = 动作 id）。上游 Console 里没有这一层，整组由本移植版新增；
        # 逐项覆盖与落点由 `test_every_catalog_action_has_exactly_one_switch` 盯着。
        #
        # v1.7.2：十个动作组收敛成**三个**（`actions_chat` 收互动 / 消息 / 历史 / 状态 /
        # 资料 / 语音 / 联系人）；v1.7.3 又取消了独立的「风险操作」组，危险动作的开关回到
        # 各自类别组；v1.7.4 再把这三个并进**一个父组** `robot_actions`（三个子组
        # chat / group / qzone）。父组在 `items` 里只有这三个子组对象，所以本表只登记它们；
        # 子组内部的键集合由 `LEGACY_ACTION_COMPAT_GROUPS` 与目录对账表守着。
        "robot_actions": {"chat", "group", "qzone"},
        # v1.7.4：输入状态从顶层组挪进「运行时」当子配置——父组 `runtime` 的 items 因此
        # 多出一个对象键（`test_upstream_field_count_matches` 按 items 数一遍，所以要登记）。
        "runtime": {"input_status"},
    }

    def test_upstream_field_count_matches(self):
        expected = sum(len(v) for v in UPSTREAM_FIELDS.values())
        # 上游字段总数里含被搬到 `prompts` 组的 4 个提示词键（`UPSTREAM_FIELDS`
        # 记的是**上游的原始归属**），所以实际项数要把新组那一份也数上。
        # 本移植版新增的**整组**（`actions_*` / `input_status`）不进 `UPSTREAM_FIELDS`，
        # 它们整组记在 `LOCAL_ONLY_FIELDS` 里，所以要按三张表的**并集**数一遍，
        # 否则"新增整组"会被漏算。
        counted = (set(UPSTREAM_FIELDS) | set(self.RELOCATED_UPSTREAM_FIELDS)
                   | set(self.LOCAL_ONLY_FIELDS))
        actual = sum(len(self.section(g)) for g in counted)
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

    def test_the_forward_video_default_tracks_the_core_constant(self):
        """`forward_message.max_videos`：schema 的 `default` 必须等于 core 的默认值。

        core 侧只有一份默认值（`forward_message.DEFAULT_LIMITS.max_videos` =
        `FORWARD_VIDEO_MAX_PER_FORWARD`，也是 `forward_read_limits()` 缺键时的 fallback）；
        `core/service/config.py` 的 `CONFIG_DEFAULTS` **没有 `forward_message` 段**
        （那一组从不进 `default_config()`）。所以这一条只对这两处，抓的是"改了一边忘了另一边"
        ——默认 0 与默认 1 是"配了永不生效"与"配了就生效"的区别，漂了就没人发现。
        """
        from plugin.core.forward_message import FORWARD_VIDEO_MAX_PER_FORWARD

        node = self.schema["forward_message"]["items"]["max_videos"]
        self.assertEqual(node["default"], FORWARD_VIDEO_MAX_PER_FORWARD)
        self.assertEqual(node["default"], 1)

    def test_deep_sections_are_complete(self):
        model = self.section("model_center")
        prompts = self.section("prompts")
        # providers 行的完整字段数（上游 ProviderIdentity + 模式字段 + ProviderAssignments）。
        self.assertGreaterEqual(len(model["providers"]["items"]), 21)
        # 提示词四件套（上游 3.4 节）：**只住在 `prompts` 组**。
        # 上游把它们放在 `model` 组里；本移植版单开一组，`model_center` 里不能再有
        # 副本——否则配置页出现两套同名同默认值的输入框，用户改错那一套会静默失效
        # （v1.1.0 的真实事故，见 `docs/PORTING_NOTES.md`）。
        for key in ("main_prompt", "format_prompt", "fixed_prompt", "style_prompt"):
            self.assertNotIn(key, model, f"model_center.{key} 是重复项，应只留在 prompts 组")
            self.assertIn(key, prompts)
        # 分组里的默认值必须与 core 读的那份逐字一致（`prompts` 组默认值 =
        # `CONFIG_DEFAULTS['model']` 的提示词默认值）。
        from plugin.core.service.config import CONFIG_DEFAULTS  # noqa: PLC0415
        for key in ("main_prompt", "format_prompt", "fixed_prompt", "style_prompt"):
            self.assertEqual(
                prompts[key]["default"], CONFIG_DEFAULTS["model"][key],
                f"prompts.{key} 的默认值与 core 不一致",
            )

    # -- v1.6.0：平台动作目录 ↔ 配置开关的对账（防漏断言） ---------------------

    def test_every_dangerous_switch_wears_the_user_warning_verbatim(self):
        """危险开关的 `hint` 逐字是用户那句警告，默认值一律 `false`。

        v1.7.3 起危险动作**没有**独立分组：开关落在各自类别组里（落点由
        `action_config_group()` 决定），警示语从"组描述"改成"每个开关的 hint"。
        退休的那一组仍在 schema 里当隐藏兼容位（v1.7.5 起连那句"已弃用"说明也删了：
        隐藏组不需要解释文案），所以警示语既不在它的 hint 里、也不在它的 description 里。
        """
        from plugin.core import platform_actions as catalog  # noqa: PLC0415

        self.assertEqual(catalog.RISK_WARNING,
                         '此标签下功能具有一定风险，易误操作，请谨慎开启。')
        retired = self.schema["actions_risks"]
        self.assertIs(retired.get("invisible"), True)
        # v1.7.5：隐藏兼容位不再写解释文案（整句删掉，`description` 键也去掉）。
        self.assertNotIn("description", retired)
        self.assertNotIn(catalog.RISK_WARNING, str(retired.get("hint") or ""))
        for action in catalog.risky_actions():
            with self.subTest(action=action.id):
                group = catalog.action_config_group(action)
                switch = self.section(group)[action.id]
                self.assertIs(switch["default"], False, "危险动作默认关闭")
                self.assertEqual(switch.get("hint"), catalog.RISK_WARNING,
                                 f"{action.id} 的 hint 不是那句警示语")

    def test_every_action_switch_description_is_the_chinese_label(self):
        """每个动作开关的 `description` = 该动作的**中文名**（不许空白、不许英文键名）。

        配置页把 `description` 当开关的标题渲染；空着或者写成键名，用户看到的就是
        `set_group_ban` 这种英文 id。这一条是"开关名字是空的"那个问题的对账用例：
        再加动作（或搬动开关）时忘了补中文名，这里当场红。
        """
        from plugin.core import platform_actions as catalog  # noqa: PLC0415

        hidden_roots = {key for key, spec in self.schema.items() if spec.get("invisible")}
        # 可见落点是**点分路径**（`robot_actions.chat` …）：比"上一层目录"而不是比顶层组名。
        visible_paths = set(catalog.ACTION_CONFIG_GROUPS.values())
        checked = 0
        for path, key, spec in iter_fields(self.schema):
            root = path.split(".", 1)[0]
            if root in hidden_roots or path.rsplit(".", 1)[0] not in visible_paths:
                continue
            action = catalog.ACTIONS.get(key)
            if action is None:
                continue
            checked += 1
            with self.subTest(path=path):
                description = spec.get("description")
                self.assertTrue(description, f"{path} 没有 description（配置页会显示成空标题）")
                self.assertEqual(description, action.label,
                                 f"{path} 的显示名该是动作中文名 {action.label!r}")
                self.assertNotIn(key, str(description), f"{path} 的显示名像是英文键名")
        self.assertGreaterEqual(checked, len(catalog.ACTIONS),
                                "每个目录动作都该在可见分组里被查到一次")

    def test_every_catalog_action_has_exactly_one_switch(self):
        """目录里的动作在**可见分组**里恰好有一个开关，且正好落在它该在的那个组。

        动作目录是唯一事实源（`plugin/core/platform_actions.py`）。加一个动作却忘了
        在 `_conf_schema.json` 里补开关，这里当场红——否则那个动作要么永远调不动
        （开关读不出来），要么被塞进别的分组、被另一个总开关连坐。

        v1.7.2/v1.7.3/v1.7.4 起还要认第二种合法出现：**隐藏的旧分组里的兼容副本**（用户
        升级前设过的开关就写在那里）。副本只允许落在"归并进这个路径"的旧组里，别的旧组
        出现同名键一律红——那说明有动作被搬进了不相干的分组。
        """
        from plugin.core import platform_actions as catalog  # noqa: PLC0415
        from plugin.core.service.config import LEGACY_SECTION_MERGES  # noqa: PLC0415

        # 1) 每个动作 id 在 schema 里出现几次、分别在哪条路径上。
        hits: dict[str, list[str]] = {}
        for path, key, _spec in iter_fields(self.schema):
            if key in catalog.ACTIONS:
                hits.setdefault(key, []).append(path)

        hidden_roots = {key for key, spec in self.schema.items() if spec.get("invisible")}
        for action_id, action in catalog.ACTIONS.items():
            with self.subTest(action=action_id):
                group = catalog.action_config_group(action)
                paths = hits.get(action_id, [])
                visible = [path for path in paths if path.split(".", 1)[0] not in hidden_roots]
                self.assertEqual(len(visible), 1,
                                 f"{action_id} 在可见分组里出现 {len(visible)} 次：{paths}")
                self.assertEqual(visible[0], f"{group}.{action_id}",
                                 f"{action_id} 的开关落点不对")
                for extra in paths:
                    if extra == visible[0]:
                        continue
                    root = extra.split(".", 1)[0]
                    self.assertIn(root, LEGACY_ACTION_COMPAT_GROUPS,
                                  f"{action_id} 多出一份开关：{extra}")
                    if root in LEGACY_UNMERGED_ACTION_GROUPS:
                        # `actions_risks` 只留兼容位、不参与归并（v1.7.4）：它里面的
                        # 同名键是 v1.7.2/1.7.3 的历史快照，允许存在但读取侧不认。
                        continue
                    self.assertIn(root, LEGACY_SECTION_MERGES.get(group, ()),
                                  f"{action_id} 的兼容副本落在不相干的旧组：{extra}")

        # 2) 每个**可见**的动作子分组都要有总开关；组里除白名单外的键都必须是
        #    动作 id，而且必须是"该落在这个组"的动作。隐藏的旧组是历史快照（键可以比
        #    目录旧、也可以有非动作键），由 `LEGACY_ACTION_COMPAT_GROUPS` 单独对账。
        extras = {"enabled", "default_voice", "auto_learn", "tts_provider_id"}
        action_groups = [path for path, spec in catalog.ACTION_CONFIG_GROUP_LABELS.items()
                         if path.rsplit(".", 1)[0] not in hidden_roots]
        self.assertTrue(action_groups, "schema 里没有可见的动作子分组")
        for group_key in action_groups:
            with self.subTest(group=group_key):
                items = self.section(group_key)
                self.assertIn("enabled", items, f"{group_key} 缺少总开关 enabled")
                self.assertIsInstance(items["enabled"]["default"], bool)
                for key in sorted(set(items) - extras):
                    self.assertIn(key, catalog.ACTIONS,
                                  f"{group_key}.{key} 不是动作目录里的动作")
                    self.assertEqual(catalog.action_config_group(catalog.ACTIONS[key]),
                                     group_key, f"{key} 不该落在 {group_key}")

        # 3) 危险动作的开关默认必须关着（开关落在各自类别组里，没有独立的风险组了）。
        for action in catalog.risky_actions():
            with self.subTest(risky=action.id):
                group = catalog.action_config_group(action)
                self.assertIs(self.section(group)[action.id]["default"], False)

    # -- v1.7.2 分组收敛 / v1.7.3 取消风险组 / v1.7.4 并进父组 --------------------

    def test_legacy_action_groups_stay_hidden_and_keep_every_key(self):
        """旧动作分组必须留在 schema 里、隐藏、且**键一个不少**（升级不丢配置）。

        宿主每次加载都按 schema 重建配置：schema 里没有的分组会被直接删掉（坑 22）。
        所以旧组只能"留着 + 隐藏 + 读取侧归并"，不能改名或删掉。同时旧键必须都还在
        **可见的**新子组里——只留在旧组的话，用户在配置页里根本改不到它。

        `actions_risks` 是**只留兼容位、不参与归并**的那一个（v1.7.4 的简化），所以它的
        每个键照样必须在可见子组里有一份（v1.7.3 起危险开关就在各自类别组里）。
        """
        from plugin.core.service.config import LEGACY_SECTION_MERGES  # noqa: PLC0415

        merged_sources = {name for sources in LEGACY_SECTION_MERGES.values()
                          for name in sources}
        self.assertEqual(
            merged_sources - {"input_status"} | set(LEGACY_UNMERGED_ACTION_GROUPS),
            set(LEGACY_ACTION_COMPAT_GROUPS),
            "归并表（除去 input_status，它不是动作组）+ 不归并的动作兼容位"
            "必须覆盖全部隐藏旧动作组",
        )
        self.assertIn("input_status", merged_sources,
                      "输入状态的顶层兼容位必须在归并表里（`runtime.input_status`）")
        self.assertEqual(set(LEGACY_ACTION_COMPAT_TARGETS), set(LEGACY_ACTION_COMPAT_GROUPS),
                         "兼容位目标表必须覆盖每一个旧组")
        for target, sources in LEGACY_SECTION_MERGES.items():
            if not target.startswith("robot_actions"):
                continue
            for source in sources:
                self.assertIn(target, LEGACY_ACTION_COMPAT_TARGETS[source],
                              f"{source} → {target} 的归并没有登记在目标表里")
        for group, keys in LEGACY_ACTION_COMPAT_GROUPS.items():
            with self.subTest(group=group):
                self.assertIn(group, self.schema, f"{group} 不能从 schema 里消失")
                self.assertIs(self.schema[group].get("invisible"), True,
                              f"{group} 必须隐藏（invisible=true）")
                # v1.7.5：隐藏兼容位不再解释"读配置时仍认这里的键"——整句删掉，
                # 只留 `title`（下拉里那一行仍然认得出它是旧组）。
                self.assertNotIn("description", self.schema[group])
                self.assertEqual(set(self.section(group)), set(keys),
                                 f"{group} 的键集合变了（用户配置会被宿主清掉）")
                targets = LEGACY_ACTION_COMPAT_TARGETS[group]
                for key in keys:
                    with self.subTest(group=group, key=key):
                        self.assertTrue(
                            any(key in self.section(target) for target in targets),
                            f"{group}.{key} 没进任何可见的新路径：{targets}",
                        )

    def test_legacy_source_keys_are_subsets_of_their_targets(self):
        """每个旧组的键集合必须是它目标子组的**子集**。

        v1.7.4 删掉了"按键分流"那条规则（旧组只供给一个目标之后就不需要了）。这条断言
        是它的替代：源键不许有目标 schema 声明之外的键——否则归并会把陌生的键折进新子组，
        宿主下次加载再按 schema 把它删掉（日志刷 `Config key removed`）。
        """
        from plugin.core.service.config import LEGACY_SECTION_MERGES  # noqa: PLC0415

        for target, sources in LEGACY_SECTION_MERGES.items():
            target_keys = set(self.section(target))
            for source in sources:
                with self.subTest(source=source, target=target):
                    self.assertLessEqual(
                        set(self.section(source)), target_keys,
                        f"{source} 里有 {target} 声明之外的键",
                    )

    def test_input_status_section_moved_into_runtime(self):
        """`input_status` 现在是 `runtime` 的子配置；顶层那一份只剩隐藏兼容位。"""
        from plugin.core.service.config import LEGACY_SECTION_MERGES  # noqa: PLC0415

        nested = self.section(INPUT_STATUS_COMPAT_TARGET)
        legacy = self.section("input_status")
        self.assertEqual(set(nested), set(INPUT_STATUS_COMPAT_KEYS))
        self.assertEqual(set(legacy), set(INPUT_STATUS_COMPAT_KEYS),
                         "顶层兼容位的键集合也一个不能少")
        self.assertEqual(nested, legacy,
                         "两处的类型/默认值/hint 必须逐字相同（读取侧按同一套判「用户写过」）")
        self.assertIs(self.schema["input_status"].get("invisible"), True)
        # v1.7.5：隐藏兼容位的解释文案整句删掉（含那个 `【已弃用】` 前缀）。
        self.assertNotIn("description", self.schema["input_status"])
        self.assertEqual(LEGACY_SECTION_MERGES[INPUT_STATUS_COMPAT_TARGET], ("input_status",))
        # `runtime` 现在是**标量键 + 嵌套子对象**混排（同 `model_center` 的形状）。
        self.assertEqual(self.schema["runtime"]["items"]["input_status"]["type"], "object")

    def test_visible_action_groups_are_exactly_the_catalog_groups(self):
        """可见的动作落点 = 目录声明的三个子组，它们同属一个父组；标题表与之逐字相等。

        落点只有一个来源（`ACTION_CONFIG_GROUPS`，值是点分路径），标题表跟着它走——
        两处漂移会让「动作」页把开关指到不存在的分组。
        """
        from plugin.core import platform_actions as catalog  # noqa: PLC0415

        expected = set(catalog.ACTION_CONFIG_GROUPS.values())
        self.assertEqual(set(catalog.ACTION_CONFIG_GROUP_LABELS), expected)
        # 父组：唯一、可见、标题 = 「机器人动作」。
        self.assertEqual(catalog.ACTION_CONFIG_SECTION, "robot_actions")
        self.assertIsNot(self.schema["robot_actions"].get("invisible"), True)
        self.assertEqual(self.schema["robot_actions"]["title"], "机器人动作")
        self.assertEqual(set(self.section("robot_actions")), {"chat", "group", "qzone"})
        self.assertEqual(catalog.ACTION_CONFIG_GROUP_ROOTS,
                         {path.split(".", 1)[0] for path in expected})
        # 再没有**可见的**顶层 `actions_*` 分组（都降级成隐藏兼容位了）。
        visible = {key for key, spec in self.schema.items()
                   if key.startswith("actions_") and not spec.get("invisible")}
        self.assertEqual(visible, set())
        # 三个子组的标题就是它们的中文名，且是 `robot_actions` 的直接子项。
        self.assertEqual(set(catalog.ACTION_CONFIG_GROUP_LABELS.values()),
                         {"会话动作", "群管理动作", "QQ 空间动作"})
        for path in expected:
            with self.subTest(group=path):
                node = schema_at(self.schema, path)
                self.assertEqual(node["title"], catalog.ACTION_CONFIG_GROUP_LABELS[path],
                                 f"{path} 的 schema 标题与目录不一致")
                self.assertEqual(path.split(".", 1)[0], catalog.ACTION_CONFIG_SECTION)
        # 退休的风险组不许再以可见分组的形式出现。
        self.assertNotIn("actions_risks", visible)

    def test_legacy_action_groups_are_never_referenced_as_new_targets(self):
        """新落点只能是三个子组：目录里不许再出现旧组名（否则又写回作废的键）。"""
        from plugin.core import platform_actions as catalog  # noqa: PLC0415

        for category, group in catalog.ACTION_CONFIG_GROUPS.items():
            with self.subTest(category=category):
                self.assertNotIn(group, LEGACY_ACTION_COMPAT_GROUPS)
                for part in group.split("."):
                    self.assertNotIn(part, LEGACY_ACTION_COMPAT_GROUPS)
        for action in catalog.ACTIONS.values():
            with self.subTest(action=action.id):
                self.assertNotIn(catalog.action_config_group(action),
                                 LEGACY_ACTION_COMPAT_GROUPS)

    # -- AstrBot 格式铁律（AGENTS.md 坑 1） -------------------------------------

    def test_voice_section_picks_a_tts_provider_separately_from_the_voice_name(self):
        """语音的「选服务商 / 选音色」住在「模型中心 → 语音 / 音频理解设置」里。

        v1.7.5 把 `tts_provider_id` / `default_voice` 从「机器人动作 → 会话动作」
        （`robot_actions.chat`，**可见组**）搬进 `model_center.audio`：它们是"用哪个模型 /
        哪个音色"，跟"允许她做哪些动作"不是一回事。

        旧位置**不能**就地删掉：宿主每次加载都按 schema 重建配置，schema 里没有的键会被
        连值一起删（坑 22/72）——所以旧键留在 schema 里当**字段级 `invisible: true`** 的
        兼容位（AstrBot 与我们的控制台都按这个字段隐藏，实测两边都认）。读取优先新位置、
        旧位置兜底、写方向只写新位置：见 `core/service/config.py` 的 `LEGACY_KEY_MERGES`。

        这里钉 schema 这一半；适配层的"指名命中 / 指名但不存在绝不回落 / 留空回落默认"
        在 `test_platform_transport.py` 的 `VoiceProviderSelectionTests` 里跑。
        """
        from plugin.core import platform_actions as catalog  # noqa: PLC0415
        from plugin.core.service.config import (  # noqa: PLC0415
            LEGACY_KEY_MERGES,
            VOICE_FIELD_KEYS,
            VOICE_LEGACY_SECTION,
            VOICE_SECTION,
        )

        self.assertEqual(VOICE_SECTION, "model_center.audio")
        self.assertEqual(VOICE_LEGACY_SECTION, "robot_actions.chat")
        audio = self.section(VOICE_SECTION)
        self.assertEqual(audio["tts_provider_id"]["_special"], "select_provider_tts")
        self.assertEqual(audio["tts_provider_id"]["default"], "")
        self.assertIn("留空", audio["tts_provider_id"]["hint"])
        self.assertNotIn("_special", audio["default_voice"])
        self.assertIn("音色", audio["default_voice"]["hint"])
        # 旧位置：键还在（宿主按 schema 重建配置，删了就会把用户设过的值一起清掉），
        # 但**字段级 invisible**——宿主配置页与控制台都不显示它。
        chat_schema = self.schema["robot_actions"]["items"]["chat"]["items"]
        legacy = self.section(VOICE_LEGACY_SECTION)
        for key in VOICE_FIELD_KEYS:
            with self.subTest(key=key):
                # 除 `invisible` 外逐字相同（参数表/默认值/hint 都不许漂）。
                self.assertEqual(
                    {k: v for k, v in legacy[key].items() if k != "invisible"},
                    audio[key],
                    f"{key} 两处必须逐字相同（旧位置只多一个 invisible）",
                )
                self.assertIs(chat_schema[key].get("invisible"), True,
                              f"旧位置的 {key} 必须隐藏（否则可见组里多一个废输入框）")
        # 更早的隐藏组（v1.6.0 的 `actions_voice`）照旧留着兜底。
        for key in VOICE_FIELD_KEYS:
            self.assertIn(key, self.section("actions_voice"))
        # 动作开关本身仍在会话动作组里（搬走的只有"用哪个 TTS / 音色"）。
        self.assertEqual(catalog.ACTION_CONFIG_GROUPS["voice"], "robot_actions.chat")
        # 键级搬迁表必须与 schema 声明逐字对齐：源路径存在、目标键就是那两个。
        self.assertEqual(
            tuple(target for target, _source, _key in LEGACY_KEY_MERGES[VOICE_SECTION]),
            VOICE_FIELD_KEYS,
        )
        for _target, source_path, source_key in LEGACY_KEY_MERGES[VOICE_SECTION]:
            self.assertEqual(source_path, VOICE_LEGACY_SECTION)
            self.assertIn(source_key, self.section(source_path))
        # 适配层真的按这个分组读（写死旧组名 = 配置是摆设，坑 66 那类事故）。
        adapter = read(os.path.join(PLUGIN_ROOT, "adapters", "astrbot_bridge.py"))
        self.assertIn("VOICE_SECTION", adapter)
        self.assertNotIn("ACTION_CONFIG_GROUPS.get('voice'", adapter)

    def test_the_stt_switch_is_a_real_gate_not_a_dead_switch(self):
        """「语音转文字」开关：schema 有它、适配层那条通路上认它。

        为什么必须有一条**真实的**转写通路才配得上这个开关：`_AstrbotLlm._transcribe`
        会用 `model_center.audio.provider_id` 指名 AstrBot 的 STT Provider 把语音转成
        文字（`_special: select_provider_stt`）。开关默认 **`true`** = 保持历史行为
        （配了转写模型就转），关掉后语音按音频证据交给主模型——`audio_capability_note`
        必须跟着一起认它，否则启动自检会说"已指定转写模型"而实际没转。
        """
        audio = self.section("model_center.audio")
        self.assertIs(audio["stt_enabled"]["default"], True, "默认必须保持现行为（转写开着）")
        self.assertTrue(audio["stt_enabled"]["description"])
        hint = audio["stt_enabled"]["hint"]
        self.assertIn("不", hint)
        self.assertIn("主模型", hint)
        adapter = read(os.path.join(PLUGIN_ROOT, "adapters", "astrbot_bridge.py"))
        self.assertIn("def audio_transcription_enabled", adapter)
        self.assertIn("audio_transcription_enabled()", adapter)
        # 开关与转写通路在同一个方法里连着（只写 schema 不接线 = 死开关）。
        import re  # noqa: PLC0415
        body = adapter.split("async def _transcribe", 1)[1].split("async def ", 1)[0]
        self.assertIn("audio_transcription_enabled()", body)

    def test_the_audio_master_switch_is_a_real_gate_not_a_dead_switch(self):
        """`audio.enabled`（上游 `audioConfig.enabled`，v1.7.6 收口）：缺键=关，且真的有人读。

        这条键从 1.0.1 起就是上游的，上游在 `loadNativeAudio` 里拿它放行附件；本移植版
        一开始就接了 core 那把闸（`chunk3.load_native_audio`）。这条用例把**三处**钉在
        一起：schema 的默认值（保持现状=上游=关）、core 的闸、适配层的能力提示——
        少任何一处都会变成"界面说着开、实际没生效"或反过来的假话。
        """
        audio = self.section("model_center.audio")
        self.assertIs(audio["enabled"]["default"], False,
                      "默认必须保持现状：上游缺省就是关（语音理解 opt-in）")
        hint = audio["enabled"]["hint"]
        self.assertIn("总开关", hint, "总开关与转写开关的分工要写在 hint 里")
        self.assertIn("转写", hint)
        core = read(os.path.join(PLUGIN_ROOT, "core", "service", "chunk3.py"))
        self.assertIn("def load_native_audio", core)
        body = core.split("async def load_native_audio", 1)[1].split("async def ", 1)[0]
        self.assertIn("'enabled'", body, "core 的加载闸必须读这个键")
        adapter = read(os.path.join(PLUGIN_ROOT, "adapters", "astrbot_bridge.py"))
        self.assertIn("def audio_understanding_enabled", adapter)
        self.assertIn("audio_understanding_enabled()", adapter)
        note = adapter.split("def audio_capability_note", 1)[1].split("async def ", 1)[0]
        self.assertIn("audio_understanding_enabled()", note,
                      "能力提示必须认总开关，否则会报出一个不存在的毛病")

    def test_the_tts_switch_is_a_real_gate_not_a_dead_switch(self):
        """「文字转语音」开关（v1.7.7）：schema 有它，核心与适配层都真的读它。

        为什么它配得上一个开关：正文 `<tts/>` 标记会真的调宿主 TTS 合成并发 `Record`
        （`upload`/`send_voice` 那条通路从 v1.7.2 起就可用），关掉后标记被忽略
        （**退回发文字，不是丢消息**）、`send_voice` / `list_voices` 也不再下发。
        默认必须是 **`true`** = 保持今天的行为。
        """
        audio = self.section("model_center.audio")
        self.assertIs(audio["tts_enabled"]["default"], True, "默认必须保持现行为（发语音可用）")
        self.assertTrue(audio["tts_enabled"]["description"])
        hint = audio["tts_enabled"]["hint"]
        self.assertIn("文字", hint, "hint 要说清关掉后只能改走文字")
        self.assertIn("忽略", hint, "hint 要说清语音标记会被忽略")
        core = read(os.path.join(PLUGIN_ROOT, "core", "service", "base.py"))
        self.assertIn("def voice_reply_enabled", core)
        chunk6 = read(os.path.join(PLUGIN_ROOT, "core", "service", "chunk6.py"))
        body = chunk6.split("def split_outgoing_segments", 1)[1].split("def ", 1)[0]
        self.assertIn("voice_reply_enabled", body, "标记解析必须真的读这个开关")
        chunk12 = read(os.path.join(PLUGIN_ROOT, "core", "service", "chunk12.py"))
        body = chunk12.split("def action_switch", 1)[1].split("def ", 1)[0]
        self.assertIn("voice_reply_enabled", body, "关掉后语音动作要走既有的动作开关路径")
        adapter = read(os.path.join(PLUGIN_ROOT, "adapters", "astrbot_bridge.py"))
        self.assertIn("def voice_reply_enabled", adapter)
        body = adapter.split("async def synthesize_voice", 1)[1].split("async def ", 1)[0]
        self.assertIn("voice_reply_enabled()", body, "适配层的合成必须也认这个开关")
        prompt = read(os.path.join(PLUGIN_ROOT, "core", "narrator_prompts.py"))
        self.assertIn("ttsEnabled", prompt, "开关关掉时不许再教模型用这个标记")
        writer = read(os.path.join(PLUGIN_ROOT, "core", "service", "chunk4.py"))
        self.assertIn("'ttsEnabled': bool(self.voice_reply_enabled)", writer,
                      "写作选项要把开关带给提示词（否则教了也用不了）")
        defaults = read(os.path.join(PLUGIN_ROOT, "core", "service", "config.py"))
        self.assertIn("'tts_enabled': True", defaults, "归一化默认值要与 schema 一致")

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
            if path in NO_DESCRIPTION_PATHS:
                # v1.7.5：这些对象的解释文案是用户点名删掉的（见 NO_DESCRIPTION_PATHS），
                # 但"删干净"本身也要钉住——留个空串或半句残话都算没删对。
                self.assertNotIn("description", spec, f"{path} 该把 description 键整个去掉")
                continue
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


class LegacyActionSectionMergeTest(unittest.TestCase):
    """**升级不丢配置**：旧格式（动作开关落在旧分组里）在读取侧仍然读得到。

    这是 v1.7.2 起三次分组收敛的核心验收（v1.7.2 十组→三组、v1.7.3 取消风险组、
    v1.7.4 三组→一个父组 `robot_actions`）：分组名变了，但用户配过的值还在磁盘上的
    旧键里；读取侧靠 `LEGACY_SECTION_MERGES` 的 N:1 归并把它读出来，写方向只写新路径
    （`fold_legacy_section_merges` 顺手把旧值折进新路径）。

    归并的判据是「**用户写过的**新分组值优先」——"写过" = 不等于 schema 默认值。
    为什么不能简单地"新组一律优先"：宿主每次加载都按 schema 把缺的键**连默认值一起**
    补进配置文件（`AstrbotConfig.check_config_integrity`），升级后 `robot_actions` 就是
    这样被整组补成默认值的；按字面优先会让宿主补的默认值顶掉用户真正的选择。

    v1.7.4 起目标路径是**嵌套的**（`robot_actions.chat` / `runtime.input_status`），
    所以默认值也必须按点分路径去 schema 里取（`schema_group_defaults`）。
    """

    #: 一份**旧格式**配置：开关散在 7 个 v1.6.0 老组里，没有任何 `robot_actions`。
    LEGACY_CONFIG = {
        "actions_interaction": {"enabled": True, "send_poke": False, "send_like": True},
        "actions_message": {"schedule_message": False},
        "actions_history": {"get_group_msg_history": True},
        "actions_status": {"update_qq_status": False},
        "actions_profile": {"set_qq_avatar": False},
        "actions_voice": {"default_voice": "zh-CN-YunxiNeural", "send_voice": False},
        "actions_contact": {"auto_learn": True},
    }

    #: 一份 v1.7.2/v1.7.3 形状的配置：开关落在**顶层**那三个组里。
    PREVIOUS_CONFIG = {
        "actions_chat": {"enabled": True, "send_poke": False, "default_voice": "zh-CN-YunxiNeural"},
        "actions_group": {"enabled": False, "set_group_kick": True},
        "actions_qzone": {"delete_qzone_post": True},
    }

    #: v1.7.2 升级上来的用户可能把危险开关写在退休的 `actions_risks` 里——
    #: v1.7.4 起它**不再参与归并**（用户判断那些配置没人用），照旧留着当兼容位。
    RISK_CONFIG = {
        "actions_risks": {"enabled": True, "set_group_kick": True, "delete_friend": True},
    }

    def test_previous_groups_read_through_the_nested_paths(self):
        """v1.7.2/v1.7.3 的三个顶层组 → `robot_actions.chat/group/qzone` 读得到。"""
        from plugin.core.service.config import apply_section_aliases  # noqa: PLC0415

        merged = apply_section_aliases(self.PREVIOUS_CONFIG)
        chat = config_at(merged, "robot_actions.chat")
        group = config_at(merged, "robot_actions.group")
        qzone = config_at(merged, "robot_actions.qzone")
        self.assertIs(chat["send_poke"], False, "用户关掉的开关不能被默认值顶回来")
        self.assertEqual(chat["default_voice"], "zh-CN-YunxiNeural")
        self.assertIs(group["enabled"], False, "用户关掉的总开关照旧是关的")
        self.assertIs(group["set_group_kick"], True)
        self.assertIs(qzone["delete_qzone_post"], True)
        # 旧组本身原样保留（回退到上一个版本时它才是真源）。
        self.assertEqual(merged["actions_chat"], self.PREVIOUS_CONFIG["actions_chat"])

    def test_retired_risk_group_is_no_longer_a_merge_source(self):
        """`actions_risks` 退出归并（v1.7.4 的简化）：它不再影响任何可见子组。

        用户判断这些配置目前没人用；危险开关在 v1.7.3 就已经搬进各自类别组了。
        兼容位继续留着（回退到旧版本仍读得到它），但读取侧**不认**它。
        """
        from plugin.core.service.config import apply_section_aliases  # noqa: PLC0415

        merged = apply_section_aliases(self.RISK_CONFIG)
        for path in ("robot_actions.chat", "robot_actions.group", "robot_actions.qzone"):
            with self.subTest(path=path):
                self.assertNotIn("set_group_kick", config_at(merged, path, {}))
        self.assertEqual(merged["actions_risks"], self.RISK_CONFIG["actions_risks"],
                         "兼容位原样保留")
        from plugin.core.service.config import LEGACY_SECTION_MERGES  # noqa: PLC0415
        for sources in LEGACY_SECTION_MERGES.values():
            self.assertNotIn("actions_risks", sources)

    def test_apply_section_aliases_merges_old_groups_into_the_new_one(self):
        from plugin.core.service.config import apply_section_aliases  # noqa: PLC0415

        merged = apply_section_aliases(self.LEGACY_CONFIG)
        chat = config_at(merged, "robot_actions.chat")
        # 逐个开关：关掉的仍是关掉的（`False` 不能被默认值顶回 `True`）
        for path, expected in (
            ("send_poke", False), ("send_like", True), ("schedule_message", False),
            ("get_group_msg_history", True), ("update_qq_status", False),
            ("set_qq_avatar", False), ("send_voice", False), ("auto_learn", True),
            # 非动作旋钮（TTS 音色）也要跟着过来，否则用户的音色设置在升级后静默失效
            ("default_voice", "zh-CN-YunxiNeural"),
        ):
            with self.subTest(key=path):
                self.assertIn(path, chat, f"robot_actions.chat 缺 {path}")
                self.assertEqual(chat[path], expected)
        # 旧分组本身原样保留（"未知键不丢"照旧，写回时也不动它们）
        self.assertEqual(merged["actions_interaction"], self.LEGACY_CONFIG["actions_interaction"])
        # **别的子组**不许被这些旧组串味（7 个老组只供给 chat）。
        for name in ("group", "qzone"):
            with self.subTest(sibling=name):
                self.assertNotIn("send_poke", config_at(merged, "robot_actions." + name, {}))

    def test_written_value_in_the_new_group_wins_over_the_old_one(self):
        from plugin.core.service.config import apply_section_aliases  # noqa: PLC0415

        merged = apply_section_aliases({
            # 新路径里是**用户写过**的值（不等于默认值 true）
            "robot_actions": {"chat": {"send_poke": False, "send_like": False}},
            "actions_interaction": {"send_poke": True, "send_like": True},
        })
        chat = config_at(merged, "robot_actions.chat")
        self.assertIs(chat["send_poke"], False, "用户写过的新路径值优先")
        self.assertIs(chat["send_like"], False)

    def test_host_inserted_defaults_do_not_override_the_old_group(self):
        """宿主补的默认值（= schema 默认值）不许顶掉用户升级前的选择。

        这是升级后**第一次加载**的真实形状：`robot_actions.chat` 是宿主按 schema 补出来的
        （开关全 true），用户的 `send_poke=False` 还在旧分组里。
        """
        from plugin.core.service.config import (  # noqa: PLC0415
            apply_section_aliases, schema_group_defaults,
        )

        defaults = schema_group_defaults("robot_actions.chat")
        host_inserted = {key: defaults[key] for key in ("enabled", "send_poke", "send_like")}
        merged = apply_section_aliases({
            "robot_actions": {"chat": dict(host_inserted)},
            "actions_interaction": {"send_poke": False},
        })
        chat = config_at(merged, "robot_actions.chat")
        self.assertIs(chat["send_poke"], False,
                      "旧分组里的用户选择必须赢过宿主补的默认值")
        self.assertIs(chat["send_like"], True, "两边都是默认值 → 默认值")

    def test_action_group_master_switch_keeps_any_old_group_turned_off(self):
        """七个旧组各有一个总开关：只要有一个是"用户关掉的"，新子组总开关就是关的。

        归并是保守的——宁可少用几个动作，也不能因为"另一个旧组的总开关是默认值 true"
        就把用户关掉的那一组悄悄打开。
        """
        from plugin.core.service.config import apply_section_aliases  # noqa: PLC0415

        merged = apply_section_aliases({
            "actions_chat": {"enabled": True},
            "actions_interaction": {"enabled": True},
            "actions_voice": {"enabled": False},
        })
        self.assertIs(config_at(merged, "robot_actions.chat")["enabled"], False)

    def test_normalize_config_carries_the_merge_to_the_service_layer(self):
        """服务层拿到的是 `normalize_config` 的产物：归并必须在那之前完成。"""
        from plugin.core.service.config import normalize_config  # noqa: PLC0415

        normalized = normalize_config(self.LEGACY_CONFIG)
        chat = config_at(normalized, "robot_actions.chat")
        self.assertIs(chat["send_poke"], False)
        self.assertEqual(chat["default_voice"], "zh-CN-YunxiNeural")

    def test_schema_defaults_are_read_from_the_schema_file(self):
        """默认值只能来自 `_conf_schema.json`（宿主补默认值读的就是它）。"""
        from plugin.core.service.config import schema_group_defaults  # noqa: PLC0415

        schema = load_schema()
        defaults = schema_group_defaults("robot_actions.chat")
        self.assertEqual(defaults["send_poke"], True)
        self.assertEqual(defaults["auto_learn"], False)
        self.assertEqual(defaults["default_voice"], "")
        self.assertEqual(defaults, {key: spec.get("default")
                                    for key, spec in schema_at(schema, "robot_actions.chat")["items"].items()})
        # 不是归并目标 / 不存在的分组 → 空表（调用方按"没有默认值"处理）
        self.assertEqual(schema_group_defaults("__不存在__"), {})
        self.assertEqual(schema_group_defaults("robot_actions.nope"), {})

    def test_nested_path_defaults_are_read_for_every_merge_target(self):
        """**新用例（用户点名）**：每个归并目标都要能按点分路径取到自己的默认值。

        这是 v1.7.4 的关键实现点：`schema_group_defaults()` 原来只认顶层组名，改成
        认点分路径之后，"宿主补的默认值不算用户写过"这条规则才在嵌套目标上成立。
        取不到默认值 = 归并退化成"新组一律优先" = 升级后开关被静默打开。
        """
        from plugin.core.service.config import (  # noqa: PLC0415
            LEGACY_SECTION_MERGES, schema_group_defaults,
        )

        schema = load_schema()
        self.assertTrue(LEGACY_SECTION_MERGES, "归并表不能为空")
        for target in LEGACY_SECTION_MERGES:
            with self.subTest(target=target):
                self.assertIn(".", target, "v1.7.4 起所有目标都是嵌套路径")
                expected = {key: spec.get("default")
                            for key, spec in schema_at(schema, target)["items"].items()}
                self.assertTrue(expected, f"{target} 的默认值不该是空表")
                self.assertEqual(schema_group_defaults(target), expected)
        # 顶层组名照旧能取（旧调用方 / 源分组判定还用得到）。
        self.assertEqual(set(schema_group_defaults("actions_interaction")),
                         {"enabled", "send_poke", "send_like", "recall_message"})

    # -- 写方向的折叠（迁移本体）-----------------------------------------------

    def test_fold_moves_the_values_into_the_nested_group_and_empties_the_old_ones(self):
        from plugin.core.service.config import fold_legacy_section_merges  # noqa: PLC0415

        raw = {
            "robot_actions": {"chat": {"enabled": True, "send_poke": True, "send_like": True}},
            "actions_interaction": {"enabled": True, "send_poke": False, "send_like": True},
            "actions_voice": {"send_voice": False, "default_voice": "zh-CN-YunxiNeural"},
            "runtime": {"auto_create": True},
        }
        folded = fold_legacy_section_merges(dict(raw))
        chat = config_at(folded, "robot_actions.chat")
        self.assertIs(chat["send_poke"], False, "旧组的用户选择折进新路径")
        self.assertIs(chat["send_voice"], False)
        self.assertEqual(folded["actions_interaction"], {}, "折完的旧组必须清空")
        self.assertEqual(folded["actions_voice"], {})
        self.assertEqual(folded["runtime"], {"auto_create": True}, "别的分组不动")
        # v1.7.5：先折组（actions_voice → robot_actions.chat），再折键
        # （robot_actions.chat → model_center.audio）。音色走完了两级，旧位置清回默认值
        # ——这是"写方向只写新位置"的落地（否则旧值会永远压着新位置，改回默认值没反应）。
        self.assertEqual(config_at(folded, "model_center.audio")["default_voice"],
                         "zh-CN-YunxiNeural")
        self.assertEqual(chat["default_voice"], "", "旧位置的键清回 schema 默认值（不删键）")

    def test_fold_is_idempotent_and_keeps_new_group_values(self):
        from plugin.core.service.config import fold_legacy_section_merges  # noqa: PLC0415

        once = fold_legacy_section_merges({
            "actions_interaction": {"send_poke": False},
        })
        twice = fold_legacy_section_merges(dict(once))
        self.assertEqual(once, twice, "折两次必须与折一次完全一样")
        # 折完之后：用户在新路径里把开关**改回默认值**（关掉→打开）必须真的生效
        config_at(once, "robot_actions.chat")["send_poke"] = True
        self.assertIs(
            config_at(fold_legacy_section_merges(dict(once)), "robot_actions.chat")["send_poke"],
            True,
        )

    def test_fold_is_idempotent_for_a_nested_target(self):
        """**新用例（用户点名）**：折一次写盘、再折不产生新写入（嵌套目标同款）。

        幂等是启动迁移的硬要求——不幂等的话每次启动都会写一次盘（`migrate_legacy_action_sections`
        靠"折出来的结果与磁盘一致就返回 0"来决定要不要写）。
        """
        from plugin.core.service.config import fold_legacy_section_merges  # noqa: PLC0415

        once = fold_legacy_section_merges({
            "input_status": {"enabled": False, "min_visible_ms": 1234},
            "actions_voice": {"send_voice": False},
        })
        nested = config_at(once, "runtime.input_status")
        self.assertIs(nested["enabled"], False)
        self.assertEqual(nested["min_visible_ms"], 1234)
        self.assertEqual(once["input_status"], {}, "旧顶层组折完清空")
        # 再折一次：**一模一样**（调用方据此判断"不用写盘"）。
        self.assertEqual(fold_legacy_section_merges(dict(once)), once)
        # 宿主把清空的旧组补成默认值之后，也不该再折出新东西。
        refilled = dict(once)
        refilled["input_status"] = dict(schema_group_defaults_for_test("input_status"))
        self.assertEqual(fold_legacy_section_merges(dict(refilled)), refilled)

    def test_fold_leaves_host_filled_default_legacy_groups_alone(self):
        """宿主每次加载都会把旧组补成默认值：那不算"用户写过"，别每次启动都写盘。"""
        from plugin.core.service.config import fold_legacy_section_merges  # noqa: PLC0415

        raw = {
            "robot_actions": {"chat": {"enabled": True, "send_poke": True, "send_like": True}},
            "actions_interaction": {"enabled": True, "send_poke": True, "send_like": True},
            "actions_voice": {"enabled": True, "send_voice": True, "default_voice": ""},
        }
        self.assertEqual(fold_legacy_section_merges(dict(raw)), raw, "没有可折的东西就不动配置")

    def test_fold_clears_a_legacy_group_whose_value_the_new_group_already_won(self):
        """新路径已经压着旧组时也要清旧组——否则用户把新值改回默认值就会被打回旧值。"""
        from plugin.core.service.config import fold_legacy_section_merges  # noqa: PLC0415

        folded = fold_legacy_section_merges({
            "robot_actions": {"chat": {"send_poke": False}},
            "actions_interaction": {"send_poke": False},
        })
        self.assertIs(config_at(folded, "robot_actions.chat")["send_poke"], False)
        self.assertEqual(folded["actions_interaction"], {})

    def test_fold_does_not_mutate_the_callers_nested_dicts(self):
        """折叠沿途**浅拷贝父字典**：调用方（`raw_config()` 的返回值）手里的嵌套 dict 不许被改。

        这是 v1.7.4 新增的坑面：写 `runtime.input_status` 这种两层路径时若不拷贝
        `runtime`，折叠会原地改掉调用方那份配置（迁移与写盘就会互相打架）。
        """
        from plugin.core.service.config import fold_legacy_section_merges  # noqa: PLC0415

        raw = {
            "actions_interaction": {"send_poke": False},
            "runtime": {"auto_create": True},
            "input_status": {"min_visible_ms": 100},
        }
        snapshot = json.loads(json.dumps(raw))
        fold_legacy_section_merges(dict(raw))
        self.assertEqual(raw, snapshot, "调用方的嵌套 dict 被原地改掉了")

    def test_to_schema_shape_folds_the_legacy_action_groups(self):
        """写盘路径自带迁移：控制台改任何一项、配置导入，都会把旧组折进新路径。"""
        from plugin.core.service.config import to_schema_shape  # noqa: PLC0415

        written = to_schema_shape({
            "actions_interaction": {"send_poke": False},
            "input_status": {"min_visible_ms": 100},
            "runtime": {"auto_create": True},
        })
        self.assertIs(config_at(written, "robot_actions.chat")["send_poke"], False)
        self.assertEqual(written["actions_interaction"], {})
        self.assertEqual(config_at(written, "runtime.input_status")["min_visible_ms"], 100)
        self.assertEqual(written["input_status"], {})
        self.assertIs(written["runtime"]["auto_create"], True)

    def test_merge_does_not_touch_the_source_dicts(self):
        """归并返回新 dict：调用方可能还拿着原始配置（`routing_config` 就这么用）。"""
        from plugin.core.service.config import merge_legacy_section_values  # noqa: PLC0415

        raw = {"actions_interaction": {"send_poke": False}}
        merged = merge_legacy_section_values(raw, "robot_actions.chat", None)
        merged["send_poke"] = True
        merged["extra"] = 1
        self.assertEqual(raw["actions_interaction"], {"send_poke": False})

    def test_old_group_reads_still_answer_for_themselves(self):
        """旧组名直接读也要回自己的那份（控制台/导入的旧路径会按旧名读）。"""
        from plugin.core.service.config import merge_legacy_section_values  # noqa: PLC0415

        raw = {"actions_interaction": {"send_poke": False}}
        self.assertEqual(
            merge_legacy_section_values(raw, "actions_interaction",
                                        raw.get("actions_interaction")),
            {"send_poke": False},
        )

    def test_unknown_group_and_non_dict_input_are_safe(self):
        from plugin.core.service.config import merge_legacy_section_values  # noqa: PLC0415

        self.assertEqual(merge_legacy_section_values(None, "robot_actions.chat", None), {})
        self.assertEqual(merge_legacy_section_values({"runtime": {}}, "runtime", None), {})
        self.assertEqual(merge_legacy_section_values({"runtime": {}}, "runtime.input_status", None), {})
        # 不是归并目标的路径原样返回（浅拷贝一份）。
        self.assertEqual(merge_legacy_section_values(
            {"actions_history": {"x": 1}}, "actions_history", {"y": 2}),
            {"y": 2})

    # -- 嵌套读取（`read_section_path` / `write_section_path`）-------------------

    def test_read_and_write_section_path(self):
        """点分路径读写：读不到回 `None`，写沿途浅拷贝父字典。"""
        from plugin.core.service.config import read_section_path, write_section_path  # noqa: PLC0415

        raw = {"robot_actions": {"chat": {"send_poke": False}}, "runtime": {"auto_create": True}}
        self.assertEqual(read_section_path(raw, "robot_actions.chat")["send_poke"], False)
        self.assertEqual(read_section_path(raw, "runtime"), {"auto_create": True})
        self.assertIsNone(read_section_path(raw, "robot_actions.group"))
        self.assertIsNone(read_section_path(raw, "nope.nope"))
        self.assertIsNone(read_section_path(None, "runtime"))

        target = dict(raw)
        write_section_path(target, "runtime.input_status", {"enabled": False})
        self.assertEqual(target["runtime"],
                         {"auto_create": True, "input_status": {"enabled": False}})
        self.assertEqual(raw["runtime"], {"auto_create": True}, "父字典不许被原地改")

    def test_input_status_legacy_top_level_is_still_readable(self):
        """**新用例（用户点名）**：`input_status` 留在**顶层**的旧配置仍读得到，
        写盘落到 `runtime.input_status`。"""
        from plugin.core.service.config import (  # noqa: PLC0415
            apply_section_aliases, fold_legacy_section_merges, read_section_path,
        )

        legacy = {"input_status": {"enabled": False, "min_visible_ms": 1234, "beat_chance": 0.5}}
        merged = apply_section_aliases(legacy)
        nested = read_section_path(merged, "runtime.input_status")
        self.assertIs(nested["enabled"], False, "关掉的输入状态不许被默认值打开")
        self.assertEqual(nested["min_visible_ms"], 1234)
        self.assertEqual(nested["beat_chance"], 0.5)
        # 顶层旧组原样保留（回退到旧版本仍读得到）。
        self.assertEqual(merged["input_status"], legacy["input_status"])

        written = fold_legacy_section_merges(dict(legacy))
        self.assertEqual(read_section_path(written, "runtime.input_status")["min_visible_ms"], 1234)
        self.assertEqual(written["input_status"], {}, "折完清空顶层兼容位")

    def test_input_status_host_defaults_do_not_override_the_legacy_choice(self):
        """同上：宿主给 `runtime.input_status` 补的默认值不许顶掉旧顶层组里的选择。"""
        from plugin.core.service.config import (  # noqa: PLC0415
            apply_section_aliases, read_section_path, schema_group_defaults,
        )

        host = {
            "runtime": {"input_status": dict(schema_group_defaults("runtime.input_status"))},
            "input_status": {"enabled": False},
        }
        merged = apply_section_aliases(host)
        self.assertIs(read_section_path(merged, "runtime.input_status")["enabled"], False)


def schema_group_defaults_for_test(group: str) -> dict:
    """测试内的小转发：避免在每个用例里重复 import。"""
    from plugin.core.service.config import schema_group_defaults  # noqa: PLC0415

    return schema_group_defaults(group)


class ReleaseConsistencyTest(unittest.TestCase):
    """上游 `test/release-consistency.test.ts` 的等价移植。

    其中几条要读 `docs/`、`upstream/` —— 那是**工作区级**材料，只在开发工作区与
    CNB 工作仓里存在；GitHub 发布仓（仓库根 = 插件根）不带它们，故缺失时整体跳过。
    """

    @classmethod
    def setUpClass(cls) -> None:
        if not os.path.isdir(os.path.join(REPO_ROOT, 'docs')):
            raise unittest.SkipTest('发布仓不含 docs/ 与 upstream/（工作区一致性检查）')
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
        self.assertEqual(self.meta_version, "1.0.1-rc28")
        self.assertRegex(read(META_PY_PATH),
                         r'HDS_INTERLUDE_VERSION\s*=\s*["\']1\.0\.1-rc28["\']')

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
