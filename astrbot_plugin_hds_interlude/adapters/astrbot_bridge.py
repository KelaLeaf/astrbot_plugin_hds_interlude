"""AstrBot 事件 ↔ core 模型桥接层。

把 AstrBot 的消息事件翻译进 `core.types.NarrativeContext`，调用 core 的主叙事，
并把结构化决策翻译回 AstrBot 回复。
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from ..core import time as htime
from ..core.model import ChatMessage, ModelConfig
from ..core.types import (
    InterludeParticipant,
    InterludeStory,
    NarrativeContext,
    NarrativeDecision,
    NarrativeDecisionKind,
    NarrativeIntent,
    NarrativeIntentKind,
    ScriptEntry,
    ScriptEntryKind,
    StorySetting,
)

_UTC = timezone.utc

# 持久化文件名（plugin 数据目录下）
_STORY_FILE = "hds_story.json"
_PARTICIPANTS_FILE = "hds_participants.json"
_ENTRIES_FILE = "hds_entries.json"
_FACTS_FILE = "hds_facts.json"


class _JsonStore:
    """极简的 JSON 文件存储，替代 Koishi 数据库。后续可换 SQLite。"""

    def __init__(self, data_dir):
        self._dir = data_dir

    def _path(self, name: str):
        import os
        return os.path.join(self._dir, name)

    def load(self, name: str):
        import os
        path = self._path(name)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None

    def save(self, name: str, data) -> None:
        import os
        path = self._path(name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=self._default)

    @staticmethod
    def _default(obj):
        """把 dataclass 和 Enum 转成可序列化对象。"""
        if isinstance(obj, datetime):
            return obj.isoformat()
        if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
            return dataclasses.asdict(obj)
        if isinstance(obj, Enum):
            return obj.value
        raise TypeError(f"Object of type {obj.__class__.__name__} is not JSON serializable")


class AstrbotBridge:
    """在 AstrBot 插件生命周期中被持有：管理剧本、参与者、剧本条目、记忆。"""

    def __init__(self, data_dir, conf: dict):
        self.store = _JsonStore(data_dir)
        self.conf = conf or {}
        self._story: Optional[InterludeStory] = None
        self._participants: dict[str, InterludeParticipant] = {}
        self._entries: list[ScriptEntry] = []
        self._facts: list[dict] = []
        self._load()

    # ---- 持久化 ----
    def _load(self) -> None:
        story_raw = self.store.load(_STORY_FILE)
        if story_raw:
            self._story = self._build_story(story_raw)
        participants_raw = self.store.load(_PARTICIPANTS_FILE) or []
        for p in participants_raw:
            key = p.get("person_id") or p.get("id") or p.get("user_id")
            if key:
                self._participants[key] = self._build_participant(p)
        self._entries = [
            ScriptEntry(**{k: v for k, v in e.items() if k in set(ScriptEntry.__dataclass_fields__)})
            for e in (self.store.load(_ENTRIES_FILE) or [])
        ]
        self._facts = self.store.load(_FACTS_FILE) or []

    def _build_story(self, raw: dict) -> Optional[InterludeStory]:
        """从 JSON dict 恢复 InterludeStory，重建嵌套 dataclass。"""
        from ..core.types import AutomationState, ContinuitySnapshot, SettingOverlay, StoryState

        setting = raw.get("setting") or {}
        state = raw.get("state") or {}
        st = StorySetting(**{
            k: v for k, v in setting.items() if k in set(StorySetting.__dataclass_fields__)
        })
        overlay = SettingOverlay(**{
            k: v for k, v in (state.get("setting_overlay") or {}).items()
            if k in set(SettingOverlay.__dataclass_fields__)
        })
        snap_raw = state.get("continuity_snapshot")
        snap = ContinuitySnapshot(**snap_raw) if snap_raw else None
        automation = AutomationState(
            **{k: v for k, v in (state.get("automation") or {}).items()
               if k in set(AutomationState.__dataclass_fields__)}
        )
        st_state = StoryState(
            setting_overlay=overlay,
            continuity_snapshot=snap,
            automation=automation,
            **{k: v for k, v in state.items()
               if k in {"active_scene_id", "active_arc_id", "narrative_update_count",
                        "last_continuity_update_at"}},
        )
        return InterludeStory(
            id=raw.get("id", "hds-main"),
            platform=raw.get("platform", ""),
            self_id=raw.get("self_id", ""),
            user_id=raw.get("user_id", ""),
            channel_id=raw.get("channel_id", ""),
            status=self._status(raw.get("status")),
            setting=st,
            state=st_state,
        )

    def _build_participant(self, raw: dict) -> InterludeParticipant:
        from ..core.types import ParticipantState

        state_raw = raw.get("state") or {}
        pt_state = ParticipantState(
            **{k: v for k, v in state_raw.items() if k in set(ParticipantState.__dataclass_fields__)}
        )
        return InterludeParticipant(
            id=raw.get("id", ""),
            story_id=raw.get("story_id", ""),
            platform=raw.get("platform", ""),
            self_id=raw.get("self_id", ""),
            user_id=raw.get("user_id", ""),
            channel_id=raw.get("channel_id", ""),
            person_id=raw.get("person_id", ""),
            display_name=raw.get("display_name", ""),
            profile=raw.get("profile", ""),
            relationship=raw.get("relationship", ""),
            state=pt_state,
            status=raw.get("status", "active"),
        )

    def _status(self, raw):
        from ..core.types import StoryStatus
        if isinstance(raw, StoryStatus):
            return raw
        try:
            return StoryStatus(raw)
        except ValueError:
            return StoryStatus.active

    def _persist(self) -> None:
        if self._story:
            self.store.save(_STORY_FILE, dataclasses.asdict(self._story))
        self.store.save(_PARTICIPANTS_FILE, [
            dataclasses.asdict(p) for p in self._participants.values()
        ])
        self.store.save(_ENTRIES_FILE, [dataclasses.asdict(e) for e in self._entries])
        self.store.save(_FACTS_FILE, self._facts)

    # ---- 剧本 ----
    def ensure_story(self, platform: str, self_id: str, user_id: str, channel_id: str) -> Optional[InterludeStory]:
        """返回现有剧本；若 auto_create 且无剧本则基于 story 配置创建。"""
        if self._story and self._story.status.value == "active":
            return self._story
        conf_story = self.conf.get("story", {})
        if not self.conf.get("runtime", {}).get("auto_create"):
            return None
        setting = StorySetting(
            character_name=conf_story.get("character_name", "Unnamed character"),
            character_profile=conf_story.get("character_profile", ""),
            world=conf_story.get("world", ""),
            style=conf_story.get("style", setting_default_style := "现实主义日常叙事，情绪克制，关系变化缓慢而具体。"),
            timezone=conf_story.get("timezone", "Asia/Shanghai"),
        )
        self._story = InterludeStory(
            id="hds-main",
            platform=platform,
            self_id=self_id,
            user_id=user_id,
            channel_id=channel_id,
            setting=setting,
            created_at=datetime.now(_UTC),
            updated_at=datetime.now(_UTC),
        )
        self._persist()
        return self._story

    def get_participant(self, person_id: str, story: InterludeStory, display_name: str = "") -> InterludeParticipant:
        if person_id in self._participants:
            return self._participants[person_id]
        p = InterludeParticipant(
            id=person_id, story_id=story.id, user_id=person_id,
            person_id=person_id, display_name=display_name or person_id,
        )
        self._participants[person_id] = p
        self._persist()
        return p

    # ---- 剧本条目 ----
    def append_entry(self, story: InterludeStory, kind: ScriptEntryKind, content: str, participant_id: str = "") -> ScriptEntry:
        e = ScriptEntry(
            id=len(self._entries) + 1,
            story_id=story.id,
            kind=kind,
            participant_id=participant_id,
            content=content,
            created_at=datetime.now(_UTC).isoformat(),
        )
        self._entries.append(e)
        # 简单滚动上限，避免无限膨胀
        if len(self._entries) > 400:
            self._entries = self._entries[-400:]
        story.updated_at = datetime.now(_UTC)
        self._persist()
        return e

    def recent_entries(self, limit: int) -> list[ScriptEntry]:
        return list(self._entries[-limit:])

    # ---- 记忆（长期事实）----
    def facts(self, limit: int) -> list[str]:
        return [f["content"] for f in self._facts[-limit:]]

    def add_facts(self, contents: list[str]) -> None:
        now = datetime.now(_UTC).isoformat()
        for c in contents:
            self._facts.append({"content": c, "created_at": now})
        if len(self._facts) > 200:
            self._facts = self._facts[-200:]
        self._persist()

    # ---- 主叙事入口 ----
    async def handle_user_message(
        self,
        platform: str,
        self_id: str,
        user_id: str,
        channel_id: str,
        content: str,
        *,
        get_provider_callable=None,
    ) -> tuple[Optional[str], NarrativeDecision, bool]:
        """处理一条用户私聊消息。

        返回 (可见回复, 决策, 是否被消费)。
        """
        story = self.ensure_story(platform, self_id, user_id, channel_id)
        if story is None:
            return "尚未创建主剧本：请先在插件配置中开启自动创建，或手动触发启动。", \
                NarrativeDecision(decided=NarrativeDecisionKind.remain_silent, reason="no story"), True

        participant = self.get_participant(user_id, story)
        now = datetime.now(_UTC).isoformat()
        self.append_entry(story, ScriptEntryKind.user_event, content, participant_id=user_id)

        conf_model = self.conf.get("model", {})
        cfg = ModelConfig(
            enabled=bool(conf_model.get("enabled")),
            endpoint=conf_model.get("endpoint", ""),
            api_key=conf_model.get("api_key", ""),
            model=conf_model.get("model", ""),
            temperature=float(conf_model.get("temperature", 0.8)),
            max_tokens=int(conf_model.get("max_tokens", 4096)),
            timeout_seconds=int(conf_model.get("timeout_seconds", 60)),
        )

        context = NarrativeContext(
            story=story,
            setting=story.setting,
            recent_entries=self.recent_entries(int(self.conf.get("runtime", {}).get("context_entry_limit", 30))),
            facts=self.facts(int(self.conf.get("runtime", {}).get("memory_limit", 20))),
            continuity=story.state.continuity_snapshot,
            participant=participant,
            current_intent=NarrativeIntent(kind=NarrativeIntentKind.message, content=content, participant_id=user_id),
            local_time=htime.story_local_time_context(datetime.now(_UTC), story.setting.timezone),
        )

        from ..core.narrative import run_narrative
        prompts = self.conf.get("prompts", {})
        decision = await run_narrative(
            context,
            cfg,
            astrobot_call=get_provider_callable,
            prompts=prompts,
        )

        # 写回剧本（补写的生活 + 可见回复）
        if decision.script_append:
            self.append_entry(story, ScriptEntryKind.script, decision.script_append)
        if decision.visible_reply:
            self.append_entry(story, ScriptEntryKind.character_message, decision.visible_reply, participant_id=user_id)

        story.state.narrative_update_count += 1
        self._persist()
        return decision.visible_reply or None, decision, True
