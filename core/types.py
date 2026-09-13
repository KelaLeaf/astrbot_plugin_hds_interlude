"""上游 `src/types.ts` 的 Python 对应物：HDS Interlude 的跨模块数据协议。

移植自 Koishi / TypeScript 上游快照 `upstream/src/types.ts`
（上游版本 `1.0.1-beta6-rebuild`，1157 行）。这是整个移植工程的公共协议文件，
所有模块一律从这里 import 领域类型。

语言映射约定（详见 `docs/PORT_PLAN.md`）：
- 上游 `export interface X {...}` → `class X(TypedDict, total=False)`。
  上游的必填字段（声明中无 `?`）用 `Required[...]` 标注；可选字段（有 `?`）保持裸注解。
  注意：本模块使用 `from __future__ import annotations`（因为上游存在大量前向引用，
  例如 `StoryState` 引用后面才定义的 `SceneFrame`），此时部分 CPython 版本的
  `TypedDict.__required_keys__` 会退化为空集——这**不影响运行**（TypedDict 不做运行期
  校验），静态检查器（mypy / pyright）仍能正确识别。
- 上游 `export type X = 'a' | 'b'` → `X = Literal['a', 'b']`；
  `export type X = SomeInterface` → `X = SomeInterface`（类型别名）。
- 字段名 `camelCase` → `snake_case`；类型名保持上游原名（本来就是 Python 类名风格）。
- 上游未单独命名的**内联对象字面量**被抽出为 TypedDict，命名规则为「宿主类型 + 语义」
  （如 `StorySettingUser`、`NarrativeInteractionReply`），并紧挨宿主类定义。
- 上游 `Date` → `datetime`（PEP 484 语义，统一 UTC，见 `core/time.py`）；
  `Record<string, unknown>` → `dict[str, Any]`；`Partial<Record<K, V>>` → `dict[K, V]`；
  `Partial<T>` → `T`（本模块 TypedDict 全部 `total=False`，等价于 TS 的 `Partial`）。
- 函数类型字段（如 `onEarlyReply?: (reply) => Promise<boolean>`）→
  `Callable[[EarlyNarrativeReply], Awaitable[bool]]`（来自 `collections.abc`）。
- 上游 `NarrativeProvider` / `NarrativeCompactor` / `NarrativeEmbedder` 描述的是
  **对象形状**（方法多为异步函数）→ Python 用 `typing.Protocol`。
- 上游 `export const emptyXxx = () => ({...})` → `empty_xxx()` 工厂函数，默认值逐字照抄。
- 唯一的关键字冲突：上游字段 `from`（Python 保留字）无法写在类体里，相关类改用
  **函数式 TypedDict 语法**，从而把运行期键名原样保留为 `'from'`
  （不改成 `from_`，避免序列化形状无谓漂移）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol, Required, TypedDict

if TYPE_CHECKING:
    # 上游 `import('./script/...')` 的跨模块类型引用。只做类型检查，避免循环 import。
    from .script.authored_actions import AuthoredAction
    from .script.knowledge_evidence import ContactEvidenceThread, KnowledgeEvidence
    from .script.life_handoff import LifeHandoff


# ========== 剧本与状态 ==========

StoryStatus = Literal['active', 'paused', 'archived']


class CharacterSetting(TypedDict, total=False):
    """角色设定：名字 + 人物小传。"""

    name: Required[str]
    profile: Required[str]


class StorySettingUser(TypedDict, total=False):
    """`StorySetting.user` 的内联对象形状（上游未单独命名）。"""

    display_name: Required[str]
    profile: Required[str]


class StorySetting(TypedDict, total=False):
    """初始 canon：只由显式配置修改。"""

    # 初始 canon：只由显式配置修改。模型引起的长期变化应写入
    # StoryState.setting_overlay，避免一次生成把人物设定直接改写。
    character: Required[CharacterSetting]
    user: Required[StorySettingUser]
    relationship: Required[str]
    world: Required[str]
    # 独立的外壳层：主角的价值观与看待世界的方式。
    perspective: Required[str]
    supporting_cast: Required[str]
    location: Required[str]
    style: Required[str]
    timezone: Required[str]


class StoryState(TypedDict, total=False):
    """主剧本的运行期状态。"""

    # 由 src/story-state.ts 做版本管理。旧 beta10 数据行是版本 0。
    schema_version: int
    # 来自未知未来版本的顶层值，会在老运行期读写一轮后原样保留在这里。
    extensions: dict[str, Any]
    # 演化的 overlay。原始 setting 仍是故事的 canon/base。
    setting_overlay: Required[StorySettingOverlay]
    active_scene_id: int
    active_arc_id: int
    # 低频连续性注记：首次自动补写时刷新，此后每 15 次成功的叙事更新刷新一次。
    continuity_snapshot: ContinuitySnapshot
    narrative_update_count: Required[int]
    last_continuity_update_at: str
    # 强制下一次成功的叙事写入重建连续性。
    continuity_dirty: bool
    # 自动推进时钟；ISO 字符串便于跨进程/数据库 JSON 持久化。
    automation: Required[StoryAutomationState]
    alter_system: AlterSystemState
    agency_window: AgencyWindowState
    # 小规模、有证据支撑的在场名单，仅由场景压缩产出。
    scene_presence: list[ScenePresenceState]
    # 最近完成的背景投递；对实时用户回合不可见。
    automatic_delivery_summaries: list[AutomaticDeliverySummary]
    # 场景压缩捕获的小而具体的在办细节（验证码、订单、跑腿）；
    # 它们会自然过期，且永不修改 canon。
    working_details: list[WorkingDetail]
    # 来源修订号：防止延迟的后台复核重新打开已结案的细节。
    working_detail_resolutions: dict[str, int]
    # 宿主持有的未结状态，由最近一次已完成的自动事件账本结转而来。
    # 它的优先级高于从散文里提取的草稿措辞。
    timeline_carry: list[str]
    # 宿主测量的发消息节奏状态；以主角自身表达习惯的形式呈现给叙事，
    # 绝不作为系统指令出现。
    chat_rhythm: ChatRhythmState
    # 当前场景的 M4 物化视图。每个派生值都带有条目溯源。
    scene_frame: SceneFrame
    # 单个场景内部的对话连续性；时间的流逝永不改变它的身份。
    dialogue_burst: DialogueBurstState


SceneFrameField = Literal[
    'place', 'presentPeople', 'ongoingActivity', 'postureOrMotion',
    'attention', 'deviceAccess', 'privacy', 'affectiveBaseline',
    'openMotions', 'openTopics', 'narrativeFocus',
]


class SceneFrame(TypedDict, total=False):
    """当前场景的 M4 物化视图。"""

    id: Required[str]
    local_boundary_entry_id: int
    scene_id: int
    place: str
    present_people: Required[list[str]]
    ongoing_activity: str
    posture_or_motion: str
    attention: str
    device_access: str
    privacy: str
    affective_baseline: str
    open_motions: Required[list[str]]
    open_topics: Required[list[str]]
    narrative_focus: str
    # 下面所有来源的并集；保留下来用于紧凑校验与检索。
    source_entry_ids: Required[list[int]]
    sources: Required[dict[SceneFrameField, list[int]]]
    updated_at: Required[str]


class DialogueBurstState(TypedDict, total=False):
    """单个场景内部的对话连续性（一个 burst = 一轮连续对话）。"""

    id: Required[str]
    frame_id: Required[str]
    started_at: Required[str]
    source_entry_ids: Required[list[int]]
    last_event_id: str
    # 哈希后的关系/群组/生活作用域；原始私密标识永不进入共享状态。
    scope_key: str
    # 哈希后的词法键，仅用于察觉真正的话题中断。
    topic_keys: list[str]


class WorkingDetail(TypedDict, total=False):
    """一条极小的结构化草稿条目。它不是持久事实：存在的意义是把
    小而具体的细节带过压缩边界，然后过期。"""

    participant_id: str
    knowledge: KnowledgeEvidence
    label: Required[str]
    value: Required[str]
    # 严格位于未来的 ISO-8601；过期细节在注入时被剪除。
    expires_at: str
    created_at: Required[str]
    source_entry_ids: list[int]


ScenePresenceStatus = Literal['present', 'off-scene', 'expected']


class ScenePresenceState(TypedDict, total=False):
    """场景在场状态。"""

    name: Required[str]
    status: Required[ScenePresenceStatus]
    basis: Required[str]
    source_entry_ids: Required[list[int]]
    updated_at: Required[str]


class AutomaticDeliverySummary(TypedDict, total=False):
    """一条有界的、动作级提醒，绝不是聊天记录的第二份副本。"""

    participant_id: Required[str]
    summary: Required[str]
    source_entry_id: int
    delivered_at: Required[str]


SchedulePreplanBlockKind = Literal['fixed', 'routine', 'flexible', 'open']
SchedulePreplanWeekday = Literal['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']


class SchedulePreplanBlock(TypedDict, total=False):
    """日程预排里的一个时间块。"""

    id: Required[str]
    start: Required[str]
    end: Required[str]
    label: Required[str]
    kind: Required[SchedulePreplanBlockKind]
    location: str
    # 细粒度模式下的一种可能性，绝不是已确认的约定。
    # 宿主只在临近该时段时才揭示它，且绝不让它锚定时间。
    tentative: bool
    source_entry_ids: list[int]


# 一段作息制度。上游字段 `from` 是 Python 保留字，无法写进类体，
# 因此改用函数式 TypedDict 语法，把运行期键名原样保留为 `'from'`。
# 函数式语法的取值是**运行期**求值的 dict 字面量，`from __future__ import
# annotations` 不生效，故这里的注解一律写成字符串（可同时表达前向引用）。
SchedulePreplanRegime = TypedDict('SchedulePreplanRegime', {
    'id': 'Required[str]',
    'label': 'Required[str]',
    'from': 'Required[str]',
    'to': 'str',
    'weekly': 'Required[dict[SchedulePreplanWeekday, list[SchedulePreplanBlock]]]',
    'source_entry_ids': 'list[int]',
}, total=False)


class SchedulePreplanException(TypedDict, total=False):
    """日程预排的例外日（整天替换或局部修补）。"""

    date: Required[str]
    mode: Required[Literal['replace', 'patch']]
    reason: Required[str]
    remove_block_ids: list[str]
    blocks: list[SchedulePreplanBlock]
    source_entry_ids: list[int]


class SchedulePreplanDay(TypedDict, total=False):
    """物化后的某一天日程。"""

    date: Required[str]
    blocks: Required[list[SchedulePreplanBlock]]


class SchedulePreplanRecord(TypedDict, total=False):
    """日程预排的持久记录。"""

    story_id: Required[str]
    revision: Required[int]
    timezone: Required[str]
    valid_from: Required[str]
    valid_through: Required[str]
    last_reviewed_local_date: Required[str]
    last_evidence_entry_id: Required[int]
    review_reason: Required[str]
    regimes: Required[list[SchedulePreplanRegime]]
    exceptions: Required[list[SchedulePreplanException]]
    materialized_days: Required[list[SchedulePreplanDay]]
    created_at: Required[datetime]
    updated_at: Required[datetime]


class SchedulePreplanProposal(TypedDict, total=False):
    """模型提出的日程预排变更建议。"""

    outcome: Required[Literal['unchanged', 'extend', 'patch', 'replace']]
    reason: Required[str]
    confidence: float
    source_entry_ids: list[int]
    regimes: list[SchedulePreplanRegime]
    exceptions: list[SchedulePreplanException]


class SchedulePreplanReviewRequest(TypedDict, total=False):
    """日程预排复核请求。"""

    local_date: Required[str]
    horizon_days: Required[int]
    variation_level: Literal['stable', 'contextual', 'granular']
    current: Required[SchedulePreplanRecord | None]
    evidence_entries: Required[list[ScriptEntry]]


class SchedulePreplanWindowBlock(SchedulePreplanBlock, total=False):
    """上游的交叉类型 `SchedulePreplanBlock & { date: string }`。"""

    date: Required[str]


# 上游字段 `from` / `to` 是 Python 保留字，见上文说明。
SchedulePreplanWindow = TypedDict('SchedulePreplanWindow', {
    'name': "Required[Literal['Schedule Preplan']]",
    'timezone': 'Required[str]',
    'from': 'Required[str]',
    'to': 'Required[str]',
    'planned_not_observed': 'Required[Literal[True]]',
    'revision': 'Required[int]',
    'blocks': 'Required[list[SchedulePreplanWindowBlock]]',
}, total=False)


class ContinuitySnapshot(TypedDict, total=False):
    """主角当前状态与显著线索的紧凑、原位替换式提醒。"""

    current: Required[str]
    next: Required[list[str]]
    recent: Required[list[str]]
    salient: Required[list[str]]


class ParticipantState(TypedDict, total=False):
    """一个角色可以在同一主剧本里维持多段关系。

    这份状态属于某一个真实的人/账号，而不是属于整个世界，因此一段对话
    不会意外覆盖另一个人的关系笔记或待发消息。
    """

    open_threads: Required[list[str]]
    relationship_notes: Required[list[str]]
    relationship_overlay: str
    unread_message_count: Required[int]
    pending_reply_count: Required[int]
    last_user_message_at: str
    last_character_message_at: str


class StoryAutomationState(TypedDict, total=False):
    """自动推进时钟（`StoryState.automation`）。"""

    # 对话活跃期结束时间；在此之前只处理必要的到期意图，不补写日常生活。
    quiet_until: str
    # 下一次自动生活补写的最早时间。
    next_advance_at: str
    # 失败的时间线窗口的重试闸门。持久化它，provider 失败才不会在每次后台扫描时
    # 反复进入叙事器，也不会在进程重启后立刻复发。
    timeline_retry_at: str
    timeline_retry_from: str
    last_auto_advance_at: str
    last_user_message_at: str
    # 从最近一次对话终点出发安排的短程连续性补写。
    conversation_follow_up_at: list[str]
    # 提供 10/20 分钟连续性上下文的关系分支。普通后台推进时省略。
    conversation_follow_up_participant_id: str


AgencyActivityLoad = Literal['free', 'occupied', 'overloaded']
AgencyPrivacy = Literal['private', 'shared', 'public']
AgencyDeviceAccess = Literal['available', 'limited', 'unavailable']


class AgencyWindowState(TypedDict, total=False):
    """Agency Window（可行动窗口）状态。"""

    activity_load: Required[AgencyActivityLoad]
    privacy: Required[AgencyPrivacy]
    device_access: Required[AgencyDeviceAccess]
    next_opportunity_at: str
    valid_until: Required[str]
    basis: Required[str]
    source_entry_ids: Required[list[int]]
    updated_at: Required[str]


ProactiveContactOrigin = Literal['life-event', 'promise', 'practical-update', 'relationship-follow-up']
ProactiveDisclosure = Literal['ordinary', 'personal']
ProactiveOutcome = Literal['send-now', 'recheck-later', 'let-go']


class ProactiveContactDraft(TypedDict, total=False):
    """主动联系草稿。"""

    participant_id: Required[str]
    origin: Required[ProactiveContactOrigin]
    motive: Required[str]
    disclosure: Required[ProactiveDisclosure]
    source_entry_ids: list[int]
    willingness: float
    outcome: Required[ProactiveOutcome]
    not_before: str
    expires_at: str


class AgencyConfig(TypedDict, total=False):
    """Agency Window 配置。"""

    enabled: Required[bool]
    max_window_minutes: Required[int]
    minimum_proactive_interval_minutes: Required[int]
    max_candidate_hours: Required[int]


class StorySettingOverlay(TypedDict, total=False):
    """随剧情演化的人设/关系/世界增量（叠加在 canon 之上）。"""

    character_profile: str
    # 独立外壳层当前累积的表达。
    perspective: str
    relationship: str
    world: str
    supporting_cast: str
    location: str
    # 小规模、累积的特质变化，以有证据支撑的注记表达。
    character_traits: list[str]


class InterludeStory(TypedDict, total=False):
    """一条主剧本。"""

    id: Required[str]
    platform: Required[str]
    self_id: Required[str]
    user_id: Required[str]
    channel_id: Required[str]
    status: Required[StoryStatus]
    setting: Required[StorySetting]
    state: Required[StoryState]
    cursor_at: Required[datetime]
    created_at: Required[datetime]
    updated_at: Required[datetime]


class InterludeParticipant(TypedDict, total=False):
    """一条私聊端点，以及它在某个主剧本内的关系分支。"""

    id: Required[str]
    story_id: Required[str]
    platform: Required[str]
    self_id: Required[str]
    user_id: Required[str]
    channel_id: Required[str]
    # 多个账号可以刻意共享同一个人格 id（真实的人）。
    person_id: Required[str]
    display_name: Required[str]
    profile: Required[str]
    relationship: Required[str]
    state: Required[ParticipantState]
    status: Required[Literal['active', 'paused']]
    created_at: Required[datetime]
    updated_at: Required[datetime]


class ScriptEntry(TypedDict, total=False):
    """剧本条目：世界观/系统事件与对话都写在这里。"""

    id: Required[int]
    story_id: Required[str]
    # 世界/系统事件为空；否则标识涉及的账号。
    participant_id: Required[str]
    kind: Required[str]
    actor: Required[str]
    content: Required[str]
    occurred_at: Required[datetime]
    metadata: Required[dict[str, Any]]
    # 用于历史召回的可选语义向量；回填推进到该条目之前为空。
    embedding: list[float]
    created_at: Required[datetime]


class NarrativeMemory(TypedDict, total=False):
    """叙事记忆条目。"""

    id: Required[int]
    story_id: Required[str]
    participant_id: Required[str]
    category: Required[str]
    content: Required[str]
    importance: Required[int]
    status: Required[str]
    source_entry_id: Required[int | None]
    created_at: Required[datetime]
    updated_at: Required[datetime]


SceneStatus = Literal['active', 'closed']


class InterludeScene(TypedDict, total=False):
    """一个场景。"""

    id: Required[int]
    story_id: Required[str]
    status: Required[SceneStatus]
    started_at: Required[datetime]
    ended_at: Required[datetime | None]
    hook: Required[str]
    summary: Required[str]
    entry_count: Required[int]
    # 最近一次已经被写入场景摘要的条目；下一轮只压缩它之后的新内容。
    last_entry_id: Required[int | None]
    created_at: Required[datetime]
    updated_at: Required[datetime]


class InterludeArc(TypedDict, total=False):
    """一个剧情弧。"""

    id: Required[int]
    story_id: Required[str]
    status: Required[Literal['active', 'closed']]
    title: Required[str]
    summary: Required[str]
    scene_count: Required[int]
    created_at: Required[datetime]
    updated_at: Required[datetime]


StatePatchTarget = Literal['character', 'perspective', 'world', 'relationship']
StatePatchStatus = Literal['proposed', 'applied', 'compacted', 'rejected', 'cleared']


class StatePatchProposal(TypedDict, total=False):
    """设定演化提案。"""

    # 压缩器提出、插件审核的变化，而不是对 canon 的直接写入。
    # 保留证据和状态可支持审计、人工确认与日后的重新评估。
    id: Required[int]
    story_id: Required[str]
    participant_id: Required[str]
    target: Required[StatePatchTarget]
    path: Required[str]
    proposed_value: Required[str]
    evidence: Required[str]
    confidence: Required[float]
    impact: Required[Literal['minor', 'major']]
    status: Required[StatePatchStatus]
    source_entry_ids: Required[list[int]]
    created_at: Required[datetime]
    applied_at: Required[datetime | None]


class OverlaySnapshot(TypedDict, total=False):
    """设定演化的一层已压缩、可审计的快照。原始已应用补丁保持原样，
    只有在快照落库之后才被标记为 compacted。"""

    id: Required[int]
    story_id: Required[str]
    participant_id: Required[str]
    target: Required[StatePatchTarget]
    # 为了数据库兼容仍写作 weekly/monthly；运行期窗口分别是五天和十天。
    tier: Required[Literal['weekly', 'monthly']]
    period_start: Required[datetime]
    period_end: Required[datetime]
    summary: Required[str]
    major_events: Required[list[str]]
    source_patch_ids: Required[list[int]]
    status: Required[Literal['active', 'superseded']]
    created_at: Required[datetime]
    updated_at: Required[datetime]


# 上游 `NarrativeFact['scope']`（FactDraft 也复用它）。
NarrativeFactScope = Literal['character', 'world', 'relationship', 'event', 'promise']


class NarrativeFact(TypedDict, total=False):
    """长期事实。"""

    knowledge: KnowledgeEvidence
    id: Required[int]
    story_id: Required[str]
    # 为空表示世界级事实；否则是关系专属事实。
    participant_id: Required[str]
    scope: Required[NarrativeFactScope]
    content: Required[str]
    importance: Required[float]
    confidence: Required[float]
    unresolved: Required[bool]
    embedding: list[float]
    status: Required[Literal['active', 'superseded']]
    source_entry_ids: Required[list[int]]
    last_seen_at: Required[datetime]
    created_at: Required[datetime]
    updated_at: Required[datetime]


IntentStatus = Literal['pending', 'completed', 'cancelled']


class NarrativeIntent(TypedDict, total=False):
    """叙事意图（未来要做/要联系的事）。"""

    id: Required[int]
    story_id: Required[str]
    # 未来联系所指向的私聊关系。
    participant_id: Required[str]
    type: Required[str]
    summary: Required[str]
    not_before: Required[datetime]
    status: Required[IntentStatus]
    payload: Required[dict[str, Any]]
    created_at: Required[datetime]
    updated_at: Required[datetime]


class WebObservation(TypedDict, total=False):
    """通过 Koishi Puppeteer 采集的一条有界、只读的观察。

    网页是不可信的源材料：只有下面提取出的文本会送回叙事器，
    绝不包含页面 HTML、脚本、cookie 或任何动作。
    """

    id: Required[int]
    story_id: Required[str]
    # 为空表示世界级观察；否则属于某一段关系。
    participant_id: Required[str]
    intent_id: Required[int | None]
    mode: Required[Literal['search', 'visit']]
    query: Required[str]
    url: Required[str]
    title: Required[str]
    excerpt: Required[str]
    summary: Required[str]
    status: Required[Literal['success', 'failed', 'blocked', 'deleted']]
    accessed_at: Required[datetime]
    created_at: Required[datetime]


class ScriptEntryDraft(TypedDict, total=False):
    """待写入的剧本条目草稿。"""

    kind: Required[str]
    actor: str
    content: Required[str]
    occurred_at: str
    metadata: dict[str, Any]


class MemoryDraft(TypedDict, total=False):
    """待写入的记忆草稿。"""

    category: Required[str]
    content: Required[str]
    importance: int
    participant_id: str


class IntentDraft(TypedDict, total=False):
    """待写入的意图草稿。"""

    type: Required[str]
    summary: Required[str]
    not_before: Required[str]
    payload: dict[str, Any]
    participant_id: str


class IntentUpdateDraft(TypedDict, total=False):
    """叙事器可以在剧本自然吸收某个进行中的叙事后果之后将其关闭。
    已排期的意图走自己的到期回合完成，因此这里刻意只限于已存在的持久上下文。"""

    id: Required[int]
    status: Required[Literal['completed', 'cancelled']]
    resolution: str


class AutomaticDeliveryPick(TypedDict, total=False):
    """上游 `Pick<AutomaticDeliverySummary, 'summary' | 'sourceEntryId'>`。"""

    summary: Required[str]
    source_entry_id: int


class OutgoingScriptEvent(TypedDict, total=False):
    """`OutgoingMessageDraft.scriptEvent` 的内联对象形状。"""

    commit_id: Required[str]
    event_id: Required[str]
    event_kind: Required[Literal['outgoing-message', 'group-message']]
    caused_by_event_ids: Required[list[str]]
    full_content: Required[str]
    bubble_index: Required[int]
    bubble_count: Required[int]


class OutgoingMessageDraft(TypedDict, total=False):
    """待投递的出站消息。"""

    participant_id: Required[str]
    content: Required[str]
    # 只附加在没有当前用户的背景投递上。
    automatic_delivery: AutomaticDeliveryPick
    # 可见回复契约只在传输成功之后才被记录。
    interaction: NarrativeInteraction | None
    # 分条气泡只在第一条气泡投递成功后才排期。
    later_segments: list[str]
    # 让投递失败时能说明它是否源自一个实时用户回合。
    user_initiated: bool
    # 当字面引用被转换成平台回复时，由传输层写入。
    quote_message_id: str
    # 稳定的回链，指向写下所有这些气泡的那一条剧本事件。
    script_event: OutgoingScriptEvent


class BrowserIntentDraft(TypedDict, total=False):
    """叙事器提出的未来浏览动作。在 Puppeteer 执行完毕并产出
    WebObservation 之前，它都不是已观察事实。"""

    mode: Required[Literal['search', 'visit']]
    query: str
    url: str
    purpose: Required[str]
    timing: Literal['deferred', 'immediate']
    participant_id: str


class ConversationActionDraft(TypedDict, total=False):
    """同一个写作回合内生成的、发往另一条关系分支的消息。"""

    action_id: str
    participant_id: Required[str]
    mode: Required[Literal['immediate', 'delayed']]
    content: Required[str]
    send_at: str
    # 0..1：主角此刻到底有多想发起这次联系。
    willingness: float
    # 简短审计注记，说明这次联系的具体理由。
    reason: str


InteractionReplyMode = Literal['none', 'immediate', 'delayed']


class NarrativeInteractionReply(TypedDict, total=False):
    """`NarrativeInteraction.reply` 的内联对象形状。"""

    mode: Required[InteractionReplyMode]
    content: str
    action_id: str
    send_at: str
    # 不透明的当前回合消息引用；只有当宿主声明支持引用回复时才被接受。
    reply_to: str


class NarrativeInteraction(TypedDict, total=False):
    """机器可读的可见回复契约。"""

    seen: Required[bool]
    reply: Required[NarrativeInteractionReply]


class EarlyGroupReply(TypedDict, total=False):
    """`EarlyNarrativeReply.groupReply` 的内联对象形状。"""

    mode: Required[Literal['immediate']]
    content: Required[str]
    reply_to: str


class EarlyNarrativeReply(TypedDict, total=False):
    """一条在流式叙事脚本之前就已解码完毕的完整传输字段。"""

    kind: Required[Literal['private', 'group']]
    content: Required[str]
    interaction: NarrativeInteraction
    group_reply: EarlyGroupReply


ChatReactionName = Literal['like', 'smile', 'laugh', 'heart', 'surprised', 'sad', 'angry']
NativeFaceSemantic = Literal['smile', 'laugh', 'sweat', 'awkward', 'heart', 'surprised', 'sad', 'angry']


class ChatActionCapabilities(TypedDict, total=False):
    """能力是瞬时的：只有当 Console 配置与实时平台连接器都允许时，
    它才会出现在 prompt 里。"""

    platform: Required[Literal['qq', 'wechat']]
    quote_reply: Required[bool]
    reactions: Required[list[ChatReactionName]]
    native_faces: list[NativeFaceSemantic]
    expression_threshold: float


class MessageReactionDraft(TypedDict, total=False):
    """待执行的消息表态（表情回应）。"""

    message_ref: Required[str]
    reaction: Required[ChatReactionName]


class StickerCatalogEntry(TypedDict, total=False):
    """表情包目录条目（面向 prompt 的瞬时目录）。"""

    asset_id: Required[str]
    group: Required[str]
    description: Required[str]
    aliases: Required[list[str]]
    animated: Required[bool]


class LocalMediaDraft(TypedDict, total=False):
    """本地表情/图片的选用草稿。"""

    asset_id: Required[str]
    placement: Literal['standalone', 'after-text']
    willingness: float


class NativeFaceDraft(TypedDict, total=False):
    """QQ 原生表情的选用草稿。"""

    semantic: Required[NativeFaceSemantic]
    willingness: Required[float]


class StickerAsset(TypedDict, total=False):
    """表情包资产（持久层）。"""

    id: Required[int]
    asset_id: Required[str]
    file_path: Required[str]
    group: Required[str]
    mime_type: Required[str]
    animated: Required[bool]
    size: Required[int]
    hash: Required[str]
    description: Required[str]
    aliases: Required[list[str]]
    status: Required[Literal['pending', 'active', 'missing']]
    # 用于目录过滤的可选语义向量；Embedding 回填推进到该资产之前为空。
    embedding: list[float]
    created_at: Required[datetime]
    updated_at: Required[datetime]


class QuotedMessageContext(TypedDict, total=False):
    """对入站回合所显式引用消息的有界快照。"""

    sender_id: Required[str]
    sender_name: Required[str]
    speaker: Required[str]
    content: Required[str]


class IndexedQuotedMessageContext(QuotedMessageContext, total=False):
    """带批内序号的引用消息快照。"""

    message_index: Required[int]


FollowUpCommitmentKind = Literal['thinking', 'checking', 'decision', 'emotional-settle']


class FollowUpCommitmentDraft(TypedDict, total=False):
    """面向用户的、承诺稍后给答复的约定；作为既有 intent 行持久化。"""

    kind: Required[FollowUpCommitmentKind]
    summary: Required[str]
    not_before: Required[str]
    expires_at: str
    source_entry_ids: list[int]


class FollowUpResolutionDraft(TypedDict, total=False):
    """对既有承诺的处置（兑现/改期/取消）。"""

    id: Required[int]
    outcome: Required[Literal['fulfilled', 'rescheduled', 'cancelled']]
    not_before: str


class DecisionGroupReply(TypedDict, total=False):
    """`NarrativeDecision.groupReply` 的内联对象形状。"""

    mode: Required[Literal['none', 'immediate']]
    content: str
    action_id: str
    # 不透明引用，只能从当前 groupContext 中选取。
    reply_to: str


class NarrativeDecision(TypedDict, total=False):
    """主叙事模型一次写作产出的结构化决策。"""

    urge: Any
    # 主叙事模型写下的连续散文。
    script: str
    # 宿主解析出的字面发言片段；绝不是一份独立的答复。
    authored_actions: list[AuthoredAction]
    life_handoff: LifeHandoff
    # 本回合引入的净氛围位移：-5 松弛，+5 严肃。
    alter: float
    # 可选的外部行动能力更新；它绝不控制散文风格。
    agency_window: AgencyWindowState  # 上游 Partial<AgencyWindowState>；本 TypedDict 为 total=False，等价
    # 面向 advance/proactive-check 回合的可选、有生活依据的联系决策。
    proactive_contact: ProactiveContactDraft
    # 仅当请求明确要求低频连续性刷新时才出现。
    continuity: ContinuitySnapshot
    # 写在散文之后的机器可读结果。
    interaction: NarrativeInteraction
    # 当一次可见的用户回合答复承诺稍后回答时必填。
    follow_up_commitment: FollowUpCommitmentDraft
    # 在实时或到期回合中，结清一条可见的待办承诺。
    follow_up_resolutions: list[FollowUpResolutionDraft]
    # 仅在需要立即背景投递时必填；它记录本次新传达的增量。
    automatic_delivery_summary: str
    memories: list[MemoryDraft]
    intents: list[IntentDraft]
    # 结清本回合可见的、进行中的叙事后果意图。
    intent_updates: list[IntentUpdateDraft]
    browser_intents: list[BrowserIntentDraft]
    # 只作用于当前参与者；世界状态走压缩提案。
    state_patch: ParticipantState  # 上游 Partial<ParticipantState>；本 TypedDict 为 total=False，等价
    # 可选的外发动作，指向同一主剧本里的其它账号。
    cross_conversation_actions: list[ConversationActionDraft]
    # 可选的、对触发本回合的已配置 OneBot 群的可见回复。
    group_reply: DecisionGroupReply
    # 当前实现里最多执行一条通过校验的表态。
    message_reactions: list[MessageReactionDraft]
    # 从本回合瞬时的表情目录中选出的确切本地表情。
    local_media: LocalMediaDraft
    # 当前实时回复可选的一条语义化 QQ 原生表情。
    native_face: NativeFaceDraft


NarrativePhase = Literal['advance', 'conversation-follow-up', 'user-message', 'intent-due']


class NarrativeImage(TypedDict, total=False):
    """当前私聊回合的瞬时原生视觉附件。
    它刻意永不写入剧本条目、记忆或事实。"""

    id: Required[str]
    mime_type: Required[str]
    data_uri: Required[str]


class NarrativeAudio(TypedDict, total=False):
    """当前私聊回合的瞬时原生音频附件。
    载荷是 QQ 语音记录经 SnowLuma 服务端转码后的结果；
    它刻意永不写入剧本条目、记忆或事实。"""

    id: Required[str]
    # OpenAI 兼容的 input_audio 格式标记，例如 'mp3'。
    format: Required[str]
    base64: Required[str]


class UserReportedTime(TypedDict, total=False):
    """用户在消息里明说的时钟引用。"""

    local_time: Required[str]
    relation: Required[Literal['past', 'future', 'current']]
    statement: Required[str]


class WritingOptions(TypedDict, total=False):
    """`NarrativeRequest.writingOptions` 的内联对象形状。
    可执行的写作能力开关；prompt 与宿主共用同一组开关。"""

    message_separator: Required[str]
    split_reply_messages: Required[bool]
    browser_mode: Required[Literal['disabled', 'deferred-only', 'allow-immediate']]


class DevelopmentTendency(TypedDict, total=False):
    """`NarrativeRequest.developmentTendencies` 的元素形状。"""

    target: Required[StatePatchTarget]
    tendency: Required[str]
    source_entry_ids: Required[list[int]]


# 上游字段 `from` 是 Python 保留字，故用函数式 TypedDict 语法；
# 注解一律写成字符串（该语法运行期求值，且存在前向引用）。
NarrativeRequest = TypedDict('NarrativeRequest', {
    'urge_enabled': 'bool',
    'contact_threads': 'list[ContactEvidenceThread]',
    # 主模型只读取经过预算控制的连续性包，不读取完整历史。
    'phase': 'Required[NarrativePhase]',
    # 在本回合刷新紧凑的连续性注记。
    'refresh_continuity': 'bool',
    # 先前一份未发布的草稿缺少必需的可见回复结构。
    'output_recovery': 'bool',
    'story': 'Required[InterludeStory]',
    'from': 'Required[datetime]',
    'now': 'Required[datetime]',
    'user_message': 'str',
    # 用户在**这一条**消息里明说的时钟引用。它们描述的是被报告的过去/未来事件，
    # 而不是消息的接收时间。
    'user_reported_times': 'list[UserReportedTime]',
    # 仅在**这一个**入站用户事件里观察到的原生图像输入。
    'images': 'list[NarrativeImage]',
    # 仅在**这一个**入站用户事件里观察到的原生音频输入。
    'audio': 'list[NarrativeAudio]',
    # 由单独配置的视觉模型产出的纯文本观察。
    # 它们是当前事件的瞬时上下文，永不进入剧本存储。
    'visual_observations': 'list[str]',
    # 宿主校验过的自动窗口事件计划。散文渲染这份计划，
    # 但不再是时间真相的来源。
    'timeline_plan': 'TimelinePlan',
    # 最近一次由宿主持有的、来自此前已完成自动节拍的未结状态。
    'timeline_carry': 'list[str]',
    # 触发本回合的关系；无人值守的生活更新为 null。
    'participant': 'Required[InterludeParticipant | None]',
    # 其它当前已登记的关系分支，按相关度排序。
    'participants': 'Required[list[InterludeParticipant]]',
    # 其它参与者的敏感细节是可选共享的，因为模型可能部署在远端。
    'share_participant_details': 'Required[bool]',
    'due_intents': 'Required[list[NarrativeIntent]]',
    # 有界的、宿主持有的未来计划；连续性不再以自由文本重复它们。
    'upcoming_intents': 'list[NarrativeIntent]',
    # 已经在推进中的后果。它们是上下文，永不作为新到期事件。
    'active_consequences': 'Required[list[NarrativeIntent]]',
    'superseded_intents': 'Required[list[NarrativeIntent]]',
    'recent_entries': 'Required[list[ScriptEntry]]',
    # 可执行的写作能力；prompt 与宿主共用同一组开关。
    'writing_options': 'WritingOptions',
    # 该时间点及之后的原始聊天条目不受常规散文预算限制。
    'recent_protection_since': 'datetime',
    'memories': 'Required[list[NarrativeMemory]]',
    'scene_context': 'SceneContext',
    'facts': 'list[NarrativeFact]',
    # 更早的设定演化，与实时的三天 overlay 分开。
    'overlay_snapshots': 'list[OverlaySnapshot]',
    'development_tendencies': 'list[DevelopmentTendency]',
    # 最近、经过安全过滤的网页观察，可作为叙事上下文。
    'web_context': 'list[WebObservation]',
    # 仅群场景回合出现；私聊隐私保持不变。
    'group_context': 'GroupContext',
    # 仅当当前私聊批次里有一条或多条消息引用了更早内容时出现。
    'quoted_messages': 'list[IndexedQuotedMessageContext]',
    # 除非当前平台已注册并启用相应动作，否则整体省略。
    'chat_capabilities': 'ChatActionCapabilities',
    # 除非本地表情库已启用、已填充且本回合可用，否则省略。
    'sticker_catalog': 'list[StickerCatalogEntry]',
    'alter_enabled': 'bool',
    'emotional_offset': 'EmotionalOffsetPrompt | None',
    'agency_enabled': 'bool',
    'agency_window': 'AgencyWindowState | None',
    # 仅后台 advance/follow-up 回合出现。
    'automatic_delivery_summaries': 'list[AutomaticDeliverySummary]',
    # 至多两条关系本地的承诺，仅在实时/到期回合包含。
    'follow_up_commitments': 'list[NarrativeIntent]',
    # 只有未来十二小时的 Schedule Preplan 会暴露给叙事。
    'schedule_preplan': 'SchedulePreplanWindow | None',
    # 瞬时传输回调；永不序列化进模型载荷。
    'on_early_reply': 'Callable[[EarlyNarrativeReply], Awaitable[bool]]',
    # 从剧本状态带来的小而具体的在办细节（已剪除、有上限）。
    'working_details': 'list[WorkingDetail]',
    # 与当前消息语义相关的更早时刻；仅私聊实时回合。
    'recalled_history': 'list[RecalledMoment]',
    # 确定性的 M4 续写脚手架；模型不能直接改写它。
    'scene_frame': 'SceneFrame',
    'dialogue_burst': 'DialogueBurstState',
}, total=False)


TimelineBeatKind = Literal['activity', 'thought', 'state']


class TimelineBeat(TypedDict, total=False):
    """一条紧凑的相对时间事件账本条目。`at` 恒在 [0, 1] 区间内，
    由宿主映射到当前叙事窗口上。"""

    at: Required[float]
    kind: Required[TimelineBeatKind]
    summary: Required[str]


class TimelinePlan(TypedDict, total=False):
    """宿主校验过的时间线计划。"""

    beats: Required[list[TimelineBeat]]
    carry: list[str]


# 上游 `Extract<NarrativePhase, 'advance' | 'conversation-follow-up' | 'intent-due'>`。
TimelinePlanPhase = Literal['advance', 'conversation-follow-up', 'intent-due']


class RecentScriptContinuation(TypedDict, total=False):
    """`TimelinePlanRequest.recentScriptContinuation` 的内联对象形状。"""

    content: Required[str]
    occurred_at: Required[datetime]
    host_timeline_ledger: str


# 上游字段 `from` 是 Python 保留字，故用函数式 TypedDict 语法（注解为字符串）。
TimelinePlanRequest = TypedDict('TimelinePlanRequest', {
    'recalled_history': 'list[RecalledMoment]',
    'contact_threads': 'list[ContactEvidenceThread]',
    'story': 'Required[InterludeStory]',
    'participant': 'Required[InterludeParticipant | None]',
    'phase': 'Required[TimelinePlanPhase]',
    'from': 'Required[datetime]',
    'now': 'Required[datetime]',
    'scene': 'Required[InterludeScene | None]',
    'facts': 'Required[list[NarrativeFact]]',
    'recent_entries': 'Required[list[ScriptEntry]]',
    # 最近一份可见的完整原始剧本。它是给时间编辑器的连续性交接；
    # 当这份剧本渲染了一个自动窗口时，随附的宿主账本始终是权威。
    'recent_script_continuation': 'RecentScriptContinuation | None',
    'due_intents': 'Required[list[NarrativeIntent]]',
    'schedule_preplan': 'SchedulePreplanWindow | None',
}, total=False)


class RecalledMoment(TypedDict, total=False):
    """为语义历史召回块检索出的一个更早的剧本时刻。"""

    id: Required[int]
    occurred_at: Required[str]
    content: Required[str]
    # 匹配锚点前后包含的、连续且不可变的剧本行。
    source_entry_ids: list[int]


class GroupMessageContext(TypedDict, total=False):
    """一条群消息的上下文。"""

    sender_id: Required[str]
    sender_name: Required[str]
    # 面向 prompt 渲染的、人类可读的稳定身份标签。
    speaker: Required[str]
    # 不透明的、对 prompt 安全的引用；适配器未提供可寻址消息 id 时为 undefined。
    message_ref: str
    # 仅运行期使用的适配器 id；永不序列化进模型 prompt。
    message_id: str
    # 仅当这条入站群消息显式引用了另一条消息时出现。
    quote: QuotedMessageContext
    content: Required[str]
    occurred_at: Required[datetime]
    direction: Literal['user', 'character']


class GroupContext(TypedDict, total=False):
    """群场景上下文。"""

    group_id: Required[str]
    channel_id: Required[str]
    label: Required[str]
    purpose: Required[str]
    character_role: Required[str]
    messages: Required[list[GroupMessageContext]]


# ========== 提供者协议（上游为对象形状，Python 用 Protocol 表达） ==========


class NarrativeProvider(Protocol):
    """叙事提供者：产出一次叙事决策。"""

    async def decide(self, request: NarrativeRequest) -> NarrativeDecision:
        """产出一次叙事决策。"""
        ...

    # 可选方法（上游 `analyzeAlter?`）：低频的旁路分析；
    # 提供者若不实现，触发点保持挂起（pending）。
    async def analyze_alter(self, request: AlterAnalysisRequest, config: AlterSystemConfig) -> AlterAnalysisDecision:
        """低频的旁路分析。"""
        ...


# ========== 工厂函数（上游 `export const emptyXxx = () => ({...})`） ==========


def empty_story_setting() -> StorySetting:
    """上游 `emptyStorySetting()`：空剧本设定（默认值逐字照抄）。"""
    return {
        'character': {'name': 'Unnamed character', 'profile': ''},
        'user': {'display_name': '', 'profile': ''},
        'relationship': '', 'world': '', 'perspective': '', 'supporting_cast': '', 'location': '',
        'style': 'Realistic, restrained, and centered on ordinary life.',
        'timezone': 'Asia/Shanghai',
    }


def empty_story_state() -> StoryState:
    """上游 `emptyStoryState()`：空剧本状态（默认值逐字照抄）。"""
    return {'schema_version': 1, 'setting_overlay': {'character_traits': []}, 'automation': {}, 'narrative_update_count': 0}


def empty_participant_state() -> ParticipantState:
    """上游 `emptyParticipantState()`：空参与者状态（默认值逐字照抄）。"""
    return {
        'open_threads': [], 'relationship_notes': [], 'unread_message_count': 0, 'pending_reply_count': 0,
    }


class PreviousSceneSummary(TypedDict, total=False):
    """紧邻其前的一个已关闭场景的紧凑摘要。把它暴露出来，
    是为了让原始上下文窗口与剧情弧之间不出现记忆断层。"""

    started_at: Required[str]
    ended_at: Required[str]
    summary: Required[str]


class SceneContext(TypedDict, total=False):
    """场景上下文。"""

    scene: Required[InterludeScene | None]
    arc: Required[InterludeArc | None]
    previous_scenes: list[PreviousSceneSummary]


# 上游字段 `from` 是 Python 保留字，故用函数式 TypedDict 语法（注解为字符串）。
CompactionRequest = TypedDict('CompactionRequest', {
    # 后台压缩只处理已发生、且尚未写进当前场景摘要的原始条目。
    # 它与主叙事回合分离，不能增加用户发送消息时的等待时间。
    'story': 'Required[InterludeStory]',
    'from': 'Required[datetime]',
    'now': 'Required[datetime]',
    'entries': 'Required[list[ScriptEntry]]',
    'scene': 'Required[InterludeScene | None]',
    'arc': 'Required[InterludeArc | None]',
    # 增量检查点之前的原始文本；是上下文，不是新证据。
    'preceding_entries': 'list[ScriptEntry]',
    'development_candidates': 'list[StatePatchProposal]',
    'participants': 'Required[list[InterludeParticipant]]',
    'facts': 'Required[list[NarrativeFact]]',
    'schedule_preplan': 'SchedulePreplanReviewRequest',
}, total=False)


class FactDraft(TypedDict, total=False):
    """待写入的事实草稿。"""

    knowledge: KnowledgeEvidence
    scope: Required[NarrativeFactScope]
    participant_id: str
    content: Required[str]
    importance: float
    confidence: float
    unresolved: bool
    source_entry_ids: list[int]
    # 本条证据所兑现、取消或以其它方式结清的既有未结事实。
    resolves_fact_ids: list[int]


class InteractionReview(TypedDict, total=False):
    """`StatePatchDraft.interactionReview` 的内联对象形状。"""

    outcome: Required[Literal['supported', 'contested', 'unresolved']]
    feedback_entry_ids: Required[list[int]]
    response_entry_ids: Required[list[int]]


class StatePatchDraft(TypedDict, total=False):
    """待处理的设定演化草稿。"""

    interaction_review: InteractionReview
    target: Required[StatePatchTarget]
    participant_id: str
    path: Required[str]
    proposed_value: Required[str]
    evidence: Required[str]
    confidence: float
    impact: Literal['minor', 'major']
    source_entry_ids: list[int]
    # 针对既有、仍属暂定候选的显式反证。
    contradicts_proposal_ids: list[int]


class ScenePresenceDraft(TypedDict, total=False):
    """待写入的在场状态草稿。"""

    name: Required[str]
    status: Required[ScenePresenceStatus]
    basis: Required[str]
    source_entry_ids: Required[list[int]]


class EpisodeTag(TypedDict, total=False):
    """`CompactionDecision.episodeTags` 的元素形状（单条剧集索引标签）。"""

    source_entry_id: Required[int]
    people: list[str]
    places: list[str]
    objects: list[str]
    topics: list[str]
    commitments: list[str]
    outcomes: list[str]
    dates: list[str]


class SceneBoundary(TypedDict, total=False):
    """`CompactionDecision.scene.boundary` 的内联对象形状。"""

    reason: Required[str]
    source_entry_ids: Required[list[int]]


class CompactionSceneDecision(TypedDict, total=False):
    """`CompactionDecision.scene` 的内联对象形状。"""

    hook: str
    summary: str
    close: bool
    boundary: SceneBoundary
    presence: list[ScenePresenceDraft]


class CompactionArcDecision(TypedDict, total=False):
    """`CompactionDecision.arc` 的内联对象形状。"""

    title: str
    summary: str


class CompactionDecision(TypedDict, total=False):
    """压缩模型的结构化决策。"""

    episode_tags: list[EpisodeTag]
    scene: CompactionSceneDecision
    arc: CompactionArcDecision
    facts: list[FactDraft]
    state_patches: list[StatePatchDraft]
    working_details: list[WorkingDetailDraft]
    schedule_preplan: SchedulePreplanProposal


class WorkingDetailDraft(TypedDict, total=False):
    """待写入的工作细节草稿。"""

    # 对同一件有证据的事项做显式改名；绝不做模糊去重。
    replaces_label: str
    knowledge: KnowledgeEvidence
    label: Required[str]
    value: Required[str]
    resolved: bool
    expires_at: str
    source_entry_ids: list[int]


# 上游字段 `from` / `to` 是 Python 保留字，故用函数式 TypedDict 语法（注解为字符串）。
OverlayCompactionRequest = TypedDict('OverlayCompactionRequest', {
    'story': 'Required[InterludeStory]',
    'participant': 'InterludeParticipant',
    'target': 'Required[StatePatchTarget]',
    'tier': "Required[Literal['weekly', 'monthly']]",  # 上游 OverlaySnapshot['tier']
    'from': 'Required[datetime]',
    'to': 'Required[datetime]',
    'patches': 'Required[list[StatePatchProposal]]',
    'snapshots': 'list[OverlaySnapshot]',
}, total=False)


class OverlayCompactionDecision(TypedDict, total=False):
    """overlay 压缩决策。"""

    summary: Required[str]
    major_events: list[str]


class NarrativeCompactor(Protocol):
    """叙事压缩器协议。"""

    async def compact(self, request: CompactionRequest) -> CompactionDecision:
        """压缩场景与长期事实。"""
        ...

    async def compact_overlay(self, request: OverlayCompactionRequest) -> OverlayCompactionDecision:
        """压缩设定演化 overlay。"""
        ...

    # 可选方法（上游 `planSchedulePreplan?`）：一个独立的每日小复盘。
    # 把它放在场景压缩之外，可以避免一次冗长的摘要响应把 schedule 字段挤掉。
    async def plan_schedule_preplan(self, request: SchedulePreplanReviewRequest) -> SchedulePreplanProposal | None:
        """复核并预排日程。"""
        ...

    # 可选方法（上游 `planTimeline?`）：低温的自动窗口导演。
    # 缺少计划意味着宿主必须推迟写入，而不是让自由散文推进现实。
    async def plan_timeline(self, request: TimelinePlanRequest) -> TimelinePlan | None:
        """为自动窗口排布时间线计划。"""
        ...


class NarrativeEmbedder(Protocol):
    """叙事向量化协议。"""

    # 可选方法（上游 `identity?()`）。
    def identity(self) -> str:
        """返回向量化实现的身份标识。"""
        ...

    async def embed(self, input: str) -> list[float]:
        """把一段文本向量化。"""
        ...


# ========== Alter System 类型 ==========


class AlterSystemState(TypedDict, total=False):
    """Alter 系统状态。"""

    alter_value: Required[float]
    alter_weight: Required[float]
    last_trigger_direction: Required[Literal[-1, 0, 1]]
    emotional_offset: Required[EmotionalOffset | None]
    history: Required[list[AlterHistoryEntry]]
    # 按关系/全局来源分开存放待处理位移。可见 offset 仍是故事级的，
    # 而它的旁路分析只看真正为触发桶做出贡献的那些证据。
    pending_scopes: list[AlterPendingScope]
    last_updated_at: Required[str]
    last_analysis_attempt_at: str


class EmotionalOffset(TypedDict, total=False):
    """情绪偏移（氛围位移的可见表达）。"""

    direction: Required[Literal['serious', 'relaxed']]
    description: Required[str]
    intensity: Required[float]
    generated_at: Required[str]


class EmotionalOffsetPrompt(EmotionalOffset, total=False):
    """注入 prompt 的情绪偏移（带权重）。"""

    weight: Required[float]


class AlterHistoryEntry(TypedDict, total=False):
    """一条 Alter 历史记录。"""

    turn: Required[int]
    phase: Required[NarrativePhase]
    alter: Required[float]
    alter_value: Required[float]
    timestamp: Required[str]
    # 为空表示主角自身/全局生活剧本。
    participant_id: str


class AlterPendingScope(TypedDict, total=False):
    """按关系分桶的待处理 Alter 位移。"""

    participant_id: Required[str]
    alter_value: Required[float]
    last_analysis_attempt_at: str


class RhythmSignature(TypedDict, total=False):
    """宿主从已投递回复中提取的单轮表达节奏签名。"""

    bubbles: Required[int]
    shape: Required[list[Literal['s', 'm', 'l', 'xl']]]
    tail: Required[Literal['question', 'imperative', 'statement', 'word']]
    total_chars: Required[int]
    occurred_at: Required[str]


class CollapsedRhythm(TypedDict, total=False):
    """连续同构节奏的定型状态；streak 驱动注入强度阶梯。"""

    template_key: Required[str]
    reason: Required[Literal['same-structure', 'tail-repeat', 'length-box']]
    streak: Required[int]


class ChatRhythmState(TypedDict, total=False):
    """发消息节奏状态。"""

    recent: Required[list[RhythmSignature]]
    collapsed: CollapsedRhythm
    exhausted: bool
    last_directive_level: Literal[1, 2, 3]
    updated_at: Required[str]


class ChatRhythmConfig(TypedDict, total=False):
    """已废弃的配置形状，仅为既有安装保留兼容。"""

    enabled: Required[bool]
    # 检测策略档位：gentle 仅结构同构；balanced 增加尾段复读与字数箱体；
    # aggressive 收紧样本要求。
    mode: Required[Literal['gentle', 'balanced', 'aggressive']]
    history_limit: Required[int]
    collapse_min_samples: Required[int]
    exhaust_limit: Required[int]


class RecentScript(TypedDict, total=False):
    """`AlterAnalysisRequest.recentScripts` 的元素形状。"""

    content: Required[str]
    occurred_at: Required[str]


class AlterAnalysisRequest(TypedDict, total=False):
    """Alter 旁路分析请求。"""

    character_name: Required[str]
    trigger_value: Required[float]
    threshold: Required[float]
    direction: Required[Literal['serious', 'relaxed']]
    recent_scripts: Required[list[RecentScript]]
    history: Required[list[AlterHistoryEntry]]
    setting_overlay: Required[StorySettingOverlay]
    current_offset: Required[EmotionalOffsetPrompt | None]


class AlterAnalysisDecision(TypedDict, total=False):
    """Alter 旁路分析决策。"""

    description: Required[str]


class AlterSystemConfig(TypedDict, total=False):
    """Alter 系统配置。"""

    enabled: Required[bool]
    base_threshold: Required[float]
    density_factor: Required[float]
    same_direction_boost: Required[float]
    opposite_decay: Required[float]
    min_weight: Required[float]
    max_intensity: Required[float]
    model_id: str
    provider_id: str
    model: str
    temperature: float
    top_p: float
    max_tokens: int
    timeout: int
    prompt: str
