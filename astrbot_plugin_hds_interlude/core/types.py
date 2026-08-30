"""HDS Interlude 核心领域模型。

这是从上游 `src/types.ts` 移植的概念子集。领域模型与 AstrBot 解耦，
只表达叙事本身（剧本、参与者、意图、事实、剧本条目），以便复用和后续跟进上游。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class StoryStatus(str, Enum):
    active = "active"
    paused = "paused"
    archived = "archived"


class NarrativeDecisionKind(str, Enum):
    """主叙事对当前用户事件的处置方式。"""
    see = "see"            # 看见并进入当前对话
    reply = "reply"        # 回复
    remain_silent = "remain_silent"  # 沉默
    delay = "delay"        # 延迟


@dataclass
class StorySetting:
    """初始 canon：只由显式配置修改。模型引起的长期变化写入 StoryState.setting_overlay。"""
    character_name: str = "Unnamed character"
    character_profile: str = ""
    perspective: str = ""          # 独立的外壳人格层：主角看待世界的方式
    user_profile: str = ""
    relationship: str = ""
    world: str = ""
    supporting_cast: str = ""
    location: str = ""
    style: str = "现实主义日常叙事，情绪克制，关系变化缓慢而具体。"
    timezone: str = "Asia/Shanghai"


@dataclass
class SettingOverlay:
    """随长期剧情演化的人设/关系/世界增量。原始 setting 仍是 canon/base。"""
    character_profile: Optional[str] = None
    perspective: Optional[str] = None
    relationship: Optional[str] = None
    world: Optional[str] = None
    supporting_cast: Optional[str] = None
    location: Optional[str] = None
    character_traits: list[str] = field(default_factory=list)

    def merge(self, base: StorySetting) -> StorySetting:
        """返回 base 加上本 overlay 的合并结果（浅合并）。"""
        return StorySetting(
            character_name=base.character_name,
            character_profile=self.character_profile or base.character_profile,
            perspective=self.perspective or base.perspective,
            user_profile=base.user_profile,
            relationship=self.relationship or base.relationship,
            world=self.world or base.world,
            supporting_cast=self.supporting_cast or base.supporting_cast,
            location=self.location or base.location,
            style=base.style,
            timezone=base.timezone,
        )


@dataclass
class ContinuitySnapshot:
    """压缩后替换原位的连续性快照，衔接上次状态。"""
    current: str = ""
    next: list[str] = field(default_factory=list)
    recent: list[str] = field(default_factory=list)
    salient: list[str] = field(default_factory=list)


@dataclass
class ParticipantState:
    """一个真实人物/账号在某主剧本里的关系分支状态。"""
    open_threads: list[str] = field(default_factory=list)
    relationship_notes: list[str] = field(default_factory=list)
    relationship_overlay: Optional[str] = None
    unread_message_count: int = 0
    pending_reply_count: int = 0
    last_user_message_at: Optional[str] = None
    last_character_message_at: Optional[str] = None


@dataclass
class AutomationState:
    """自动推进时钟。"""
    quiet_until: Optional[str] = None
    next_advance_at: Optional[str] = None
    last_auto_advance_at: Optional[str] = None
    last_user_message_at: Optional[str] = None
    conversation_follow_up_at: list[str] = field(default_factory=list)


@dataclass
class StoryState:
    setting_overlay: SettingOverlay = field(default_factory=SettingOverlay)
    active_scene_id: Optional[int] = None
    active_arc_id: Optional[int] = None
    continuity_snapshot: Optional[ContinuitySnapshot] = None
    narrative_update_count: int = 0
    last_continuity_update_at: Optional[str] = None
    automation: AutomationState = field(default_factory=AutomationState)


@dataclass
class InterludeStory:
    id: str
    platform: str = ""
    self_id: str = ""
    user_id: str = ""
    channel_id: str = ""
    status: StoryStatus = StoryStatus.active
    setting: StorySetting = field(default_factory=StorySetting)
    state: StoryState = field(default_factory=StoryState)
    cursor_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass
class InterludeParticipant:
    id: str
    story_id: str
    platform: str = ""
    self_id: str = ""
    user_id: str = ""
    channel_id: str = ""
    person_id: str = ""
    display_name: str = ""
    profile: str = ""
    relationship: str = ""
    state: ParticipantState = field(default_factory=ParticipantState)
    status: str = "active"
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class ScriptEntryKind(str, Enum):
    """剧本条目类型：区分外部事件与角色内部产出。"""
    user_event = "user"
    character_message = "character"   # 已成功投递的角色消息
    script = "script"                 # 剧本段落
    summary = "summary"


@dataclass
class ScriptEntry:
    id: int
    story_id: str
    kind: ScriptEntryKind
    participant_id: Optional[str] = None
    content: str = ""
    created_at: Optional[str] = None
    meta: dict = field(default_factory=dict)


class NarrativeIntentKind(str, Enum):
    message = "message"
    group = "group"
    advance = "advance"
    system = "system"


@dataclass
class NarrativeIntent:
    """进入主叙事的一次事件。"""
    kind: NarrativeIntentKind
    participant_id: str = ""
    content: str = ""
    created_at: Optional[str] = None
    source: str = ""


@dataclass
class NarrativeDecision:
    """主模型对当前事件的结构化处置决策。"""
    visible_reply: str = ""
    script_append: str = ""                 # 已补写的生活
    decided: NarrativeDecisionKind = NarrativeDecisionKind.see
    reason: str = ""
    delayed_reply_seconds: Optional[int] = None
    delayed_reply_at: Optional[str] = None
    intentions: list[dict] = field(default_factory=list)   # 待办/承诺/延迟动作


@dataclass
class NarrativeFact:
    id: str
    story_id: str
    content: str = ""
    importance: float = 0.5
    confidence: float = 0.5
    source_entry_ids: list[int] = field(default_factory=list)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


@dataclass
class StatePatchDraft:
    """模型建议的设定演化。"""
    target: str = ""          # character_profile / relationship / world ...
    content: str = ""
    reason: str = ""
    confidence: float = 0.0
    patch_type: str = "minor"  # minor / major


@dataclass
class NarrativeContext:
    """送入主叙事模型的上下文。"""
    story: InterludeStory
    setting: StorySetting
    recent_entries: list[ScriptEntry] = field(default_factory=list)
    facts: list[NarrativeFact] = field(default_factory=list)
    continuity: Optional[ContinuitySnapshot] = None
    participant: Optional[InterludeParticipant] = None
    current_intent: Optional[NarrativeIntent] = None
    local_time: Optional[dict] = None   # 时间上下文
