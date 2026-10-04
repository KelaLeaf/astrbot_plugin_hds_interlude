/** 后端 `adapters/console_api.py` 返回的数据形状。字段名与那边逐字对应。 */

export interface StoryBrief {
  id: string
  status: string
  platform: string
  character: string
  user_name: string
  timezone: string
  cursor_at: string
  created_at: string
  updated_at: string
  scene: string
}

/** `GET console/stories`：库里的全部剧本（含归档），供顶栏切换器使用。 */
export interface StoryListItem extends StoryBrief {
  entries: number
  participants: number
  shared: boolean
  /** 是不是当前共享主剧本（`main` 那一部）。 */
  main: boolean
}

export interface StoryListPayload {
  stories: StoryListItem[]
  /** 真正的共享主剧本 id（active 且 `character:`）；没有主剧本时是空串。 */
  main: string
  /** 面板默认读哪一部（没有主剧本时 = 最近更新的那部旧剧本）。 */
  active_story: string
  /** 仅「并入主剧本」的响应里出现。 */
  merged?: { source: string; target: string; participant: string; moved: number }
  /** 仅「设为主剧本」的响应里出现。 */
  promoted?: { source: string; target: string; revived: boolean }
  changed?: string
}

export interface RoutingRow {
  task: string
  label: string
  /**
   * `disabled` = 这一行的判定就是 `disabled`（功能被显式关掉，如 `compaction` / `embedding`）；
   * `none` = 没指名 Provider、也没有连接行 —— 此刻 core 路由是 `unavailable`
   * （`SilentNarrator`），**不是**"走 AstrBot 默认 Provider"（v1.9.4 §60）。
   */
  source: 'astrbot' | 'connection' | 'disabled' | 'none'
  /** 「来源」那一档的显示文案（后端 `ROUTING_SOURCE_LABELS` 一处给出，前端只画不翻）。 */
  source_label: string
  provider_label: string
  model: string
  assigned: boolean
  available: boolean
  reason: string
  candidates: number
}

export interface OverviewPayload {
  plugin: {
    name: string
    display_name: string
    version: string
    upstream_version: string
    data_dir: string
    config_path: string
  }
  service: {
    started: boolean
    blind_mode: boolean
    story_count: number
    participant_count: number
    entry_count: number
  }
  story: StoryBrief | null
  stories: StoryBrief[]
  flags: Record<string, boolean>
  routing: RoutingRow[]
  capability: { image: string; audio: string }
  counts: Record<string, number>
  /** 上轮上下文构成（v1.4.0）；没有跑过回合时是空对象。 */
  context_metrics?: ContextMetrics
}

export interface ContextMetrics {
  at: string
  phase: string
  participant_id: string
  assembly_ms: number
  items: number
  characters: number
  payload_characters: number
  estimated_tokens: number
  sections: Array<{ key: string; label: string; items: number; characters: number }>
}

export interface ConnectionRow {
  label: string
  enabled: boolean
  mode: string
  endpoint: string
  has_endpoint: boolean
  model: string
  has_key: boolean
  tasks: string[]
  prices: { input: number; output: number; cached: number }
  response_format: string
  temperature: number | null
  top_p: number | null
  max_tokens: number | null
  timeout: number | null
}

export interface AstrBotProvider {
  id: string
  model: string
  type: string
  provider_type: string
  modalities: string[]
  used_by: string[]
}

export interface UsageRecord {
  at: string
  task: string
  model: string
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
}

export interface ModelsPayload {
  tasks: RoutingRow[]
  task_models: Record<string, { label: string; astrbot_provider: string; modalities: string[] }>
  connections: ConnectionRow[]
  astrbot_providers: AstrBotProvider[]
  embedding: {
    enabled: boolean
    semantic_history: boolean
    live_query: boolean
    endpoint: string
    dimensions: number
    provider_id: string
  }
  vision: { enabled: boolean; mode: string; detail: string; max_image_dimension: number; provider_id: string }
  audio: { enabled: boolean; out_format: string; max_file_size_mb: number; provider_id: string }
  failover: { enabled: boolean; strategy: string; max_attempts: number; cooldown_minutes: number }
  main: {
    provider_label: string
    temperature: number | null
    top_p: number | null
    max_tokens: number | null
    timeout: number | null
    response_format: string
    streaming_mode: string
  }
  capability: { image: string; audio: string }
  usage: {
    recent: UsageRecord[]
    totals: Array<{ task: string; prompt_tokens: number; completion_tokens: number; total_tokens: number; calls: number }>
    sum: { calls: number; prompt_tokens: number; completion_tokens: number; total_tokens: number }
    capacity: number
  }
}

export interface ScriptEntry {
  id: number
  kind: string
  actor: string
  content: string
  truncated: boolean
  occurred_at: string
  commit_id: string
}

export interface ScriptPayload {
  story: StoryBrief | null
  total: number
  offset: number
  limit: number
  entries: ScriptEntry[]
  scenes: Array<{
    id: number
    status: string
    hook: string
    summary: string
    started_at: string
    ended_at: string
    entry_count: number
  }>
  arcs: Array<{ id: number; status: string; title: string; summary: string; scene_count: number }>
}

export interface FactRow {
  id: number
  scope: string
  content: string
  importance: number
  confidence: number
  status: string
  unresolved: boolean
  knowledge_kind: string
  quote: string
  last_seen_at: string
}

export interface MemoryPayload {
  story: StoryBrief | null
  facts: FactRow[]
  memories: Array<{ id: number; category: string; content: string; importance: number; status: string; updated_at: string }>
  intents: Array<{
    id: number
    type: string
    summary: string
    status: string
    not_before: string
    /** 纯宿主调度（拆分气泡节拍 / 失败重试），默认折叠。 */
    internal?: boolean
  }>
  patches: Array<{
    id: number
    target: string
    path: string
    proposed_value: string
    confidence: number
    impact: string
    status: string
    created_at: string
    /** 控制台审批 / 回滚的留痕（v1.4.0）。 */
    decided_at?: string
    decision_note?: string
  }>
  overlays: Array<{ id: number; target: string; tier: string; summary: string; period_end: string; status: string }>
  participants: Array<{
    id: string
    display_name: string
    platform: string
    status: string
    relationship: string
    updated_at: string
    has_state: boolean
  }>
}

export interface DatabasePayload {
  tables: Array<{ name: string; columns: number; rows: number; added_later: boolean }>
  total_rows: number
  path: string
  size_bytes: number
}

export interface LogPayload {
  records: Array<{ at: string; level: string; text: string }>
  total: number
  capacity: number
}

export interface ImportPreview {
  report: {
    format_version: number
    source: string
    section_count: number
    diff: { added: string[]; removed: string[]; changed: string[]; same: number }
    notes: string[]
    warnings: string[]
  }
  payload: string
}


export interface AlterPayload {
  story: StoryBrief | null
  state: {
    value: number
    weight: number
    direction: number
    updated_at: string
    last_attempt_at: string
    offset: { direction: string; description: string; intensity: number; generated_at: string } | null
  } | null
  history: Array<{
    turn: number
    phase: string
    alter: number
    alter_value: number
    timestamp: string
    participant_id: string
  }>
  pending: Array<{ participant_id: string; value: number; last_attempt_at: string }>
  config: Record<string, unknown>
}

export interface AgencyPayload {
  story: StoryBrief | null
  window: {
    activity_load: string
    privacy: string
    device_access: string
    next_opportunity_at: string
    valid_until: string
    basis: string
    source_entry_ids: number[]
    updated_at: string
  } | null
  plan: {
    revision: number
    timezone: string
    valid_from: string
    valid_through: string
    last_reviewed: string
    review_reason: string
    regimes: unknown[]
    exceptions: unknown[]
    materialized_days: unknown[]
    updated_at: string
  } | null
  config: Record<string, unknown>
}

export interface DeliverySegment {
  index: number
  kind: string
  status: string
  attempts: number
  error: string
}

export interface DeliveryAction {
  entry_id: number
  occurred_at: string
  commit_id: string
  event_id: string
  status: string
  target: string
  attempts: number
  updated_at: string
  segments: DeliverySegment[]
  done: number
}

export interface DeliveryPayload {
  story: StoryBrief | null
  actions: DeliveryAction[]
  totals: Record<string, number>
  scanned: number
}

/** 配置页（schema 驱动）：`console/config` 的形状。 */
export interface ConfigRowSpec {
  key: string
  type: string
  description: string
  hint: string
  default?: unknown
  options?: string[] | null
}

export interface ConfigNote {
  level: 'warn' | 'info'
  text: string
}

export interface ConfigField {
  key: string
  path: string
  type: string
  /** 原始 schema 节点（前端递归渲染的依据）。 */
  node: Record<string, unknown>
  description: string
  hint: string
  default?: unknown
  options?: string[] | null
  rows: ConfigRowSpec[] | null
  item_type: string
  special: string
  invisible: boolean
  value: unknown
  present: boolean
  note: ConfigNote | null
  delegated: boolean
}

export interface ConfigGroup {
  key: string
  /** 短标题（schema 的 `title`）：下拉与卡片标题。 */
  title: string
  description: string
  /** 分组级状态词（目前只有「模型中心」：视频理解的 FFmpeg 状态，如 `✅ FFmpeg 已识别`）。
   *  空串＝这个分组没有状态可报。**只显示状态词，不附解释**。 */
  status?: string
  invisible: boolean
  fields: ConfigField[]
}

export interface ConfigSchemaPayload {
  groups: ConfigGroup[]
  choices: Record<string, Array<{ value: string; label: string }>>
  config_path: string
  version: string
}

/** 已知参与者（白名单一键填入用）。 */
export interface ParticipantRow {
  participant_id: string
  story_id: string
  platform: string
  self_id: string
  user_id: string
  channel_id: string
  person_id: string
  display_name: string
  relationship: string
  status: string
  updated_at: string
}

/** 一条对话（私聊按参与者、群聊按群号）。 */
export interface ChatConversation {
  conversation: string
  name: string
  participant_id?: string
  group_id?: string
  account?: string
  platform?: string
  status?: string
  unread?: number
  pending?: number
  messages: number
  incoming: number
  outgoing: number
  failed: number
  /** 她最后一次发言之后收到的来信数（"还在等她"）。 */
  awaiting: number
  last_at: string
  last_text: string
  /** 只在配置里列着、还没说过话的群。 */
  configured?: boolean
}

export interface ChatsPayload {
  story: StoryBrief | null
  private: ChatConversation[]
  groups: ChatConversation[]
  scanned: number
  truncated: boolean
}

export interface ChatMessage {
  entry_id: number
  at: string
  side: 'in' | 'out' | 'system'
  kind: string
  sender: string
  text: string
  quote: string
}

export interface ChatHistoryPayload {
  story: StoryBrief | null
  conversation: string
  title: string
  character: string
  messages: ChatMessage[]
  has_more: boolean
  scanned: number
  truncated?: boolean
}

/** Token 统计（`console/token-stats`）。数值都是 token 数，`hitRate` 是 0~1 的比例。 */
export interface TokenUsageBucket {
  model?: string
  task?: string
  provider: string
  inputTokens: number
  outputTokens: number
  cachedTokens: number
  totalTokens: number
  calls: number
  hitRate: number
  /** 这一桶有没有 token 数据（false 且 calls>0 = 这些调用没回用量，显示 `—`）。 */
  hasTokens: boolean
}

export interface TokenUsageDay {
  day: string
  inputTokens: number
  outputTokens: number
  cachedTokens: number
  calls: number
  hitRate: number
  hasTokens: boolean
}

export interface TokenStatsPayload {
  range: 'day' | 'week' | 'month' | 'custom'
  from: string
  to: string
  timezone: string
  totals: {
    inputTokens: number
    outputTokens: number
    cachedTokens: number
    totalTokens: number
    calls: number
    hitRate: number
    hasTokens: boolean
  }
  byModel: TokenUsageBucket[]
  byTask: TokenUsageBucket[]
  series: TokenUsageDay[]
}

/* ----------------------------------------------- 平台动作目录（`console/actions`） */

/** 权限四档。与后端 `platform_actions.PERMISSION_TIERS` 同序同值。 */
export type PermissionTier = 'global' | 'groupadmin' | 'admin' | 'disabled'

/** 风险分级。`dangerous` 默认档位就是 `disabled`。 */
export type ActionRisk = 'safe' | 'sensitive' | 'dangerous'

export interface ActionParamBrief {
  name: string
  label: string
  type: string
  required: boolean
  minimum: number | null
  maximum: number | null
  choices: string[]
  note?: string
}

/** 目录里的一行动作。`permission` 是权限表里的档位，`enabled` 才是"实际能不能用"。 */
export interface PlatformActionRow {
  id: string
  category: string
  category_label: string
  label: string
  summary: string
  risk: ActionRisk | string
  default_permission: PermissionTier | string
  permission: PermissionTier | string
  enabled: boolean
  /** 配置开关的原始值；`null` = 该分组/键不存在（未配置 = 不限制）。 */
  config_enabled: boolean | null
  /** 开关落在哪个配置子组（**点分路径**，如 `robot_actions.chat`）。 */
  group: string
  /** 后端标签（人话，**顺序 = 通道优先级**）；缺省 = 后端是标准 OneBot。 */
  backends?: string[]
  /** NapCat 专属：后端里没有标准 OneBot 的那几条（非 NapCat 后端无法使用）。 */
  napcat_only?: boolean
  /** 适用范围：`private` / `group`（两者都有时都给）。 */
  scopes?: string[]
  /** **这条动作实际适用**的权限档位（后端下发；非群聊动作没有 `groupadmin`）。 */
  tiers?: string[]
  returns?: string
  params: ActionParamBrief[]
}

export interface PermissionTierBrief {
  id: PermissionTier | string
  label: string
  description: string
}

export interface ActionsCatalogPayload {
  actions: PlatformActionRow[]
  tiers: PermissionTierBrief[]
  /** 配置子组路径（`robot_actions.chat` 这种）→ 子组中文名（会话动作 / 群管理动作 / QQ 空间动作）。 */
  groups: Record<string, string>
  /** 危险动作的警示语（core 里的原文，逐字）。 */
  risk_warning: string
  /** 全部危险动作 id。 */
  risky: string[]
  /** NapCat 专属动作 id（含走 NapCat WS 的空间动作），顺序与 core 一致。 */
  napcat_only?: string[]
  /** 后端 id → 人话标签（core 的 `BACKEND_LABELS` 原文）。 */
  backend_labels?: Record<string, string>
  permissions_path: string
  stats: {
    total: number
    enabled: number
    disabled: number
    risky: number
    risky_enabled: number
    /** NapCat 专属动作条数（筛选按钮上的那个 N）。 */
    napcat_only?: number
    risk: Record<string, number>
    permissions: Record<string, number>
  }
  risk_labels?: Record<string, string>
}

/* ---------------------------------------------------- 共同作品（`console/works`） */

/** `lastFailure`：某条剧本条目的提案没保存成（服务层留的痕）。 */
export interface WorkFailure {
  sourceEntryId?: number
  status?: string
  at?: string
}

/** 清单里的一行（每个参与者一件作品）。 */
export interface WorkListRow {
  work_id: string
  participant_id: string
  participant: string
  title: string
  head: string
  /** 当前版本号（head 在时间线里的位置，从 1 数）。 */
  revision: number
  revision_count: number
  pending_count: number
  jobs_running: number
  job_count: number
  generation: number
  updated_at: string
  may_propose: boolean
  last_failure: WorkFailure | null
  /** 库里有这件作品但数据形状不被识别（原数据保持原样）。 */
  broken: boolean
}

export interface WorksOverviewPayload {
  /** 服务层能不能干活；`false` 时只有 `hint` 有意义。 */
  available: boolean
  enabled: boolean
  generation_mode: string
  /** 服务层自己那句结论（`explain_works_state`）：什么模式、没生效是为什么。 */
  explain: string
  story: StoryBrief | null
  works: WorkListRow[]
  hint: string
}

export interface WorkRevisionRow {
  id: string
  ordinal: number
  parent_id: string
  author: string
  proposal_id: string
  created_at: string
  current: boolean
  content_chars: number
  /** 历史版本只有预览；head 才带 `content` 全文。 */
  preview: string
  content?: string
}

export interface WorkProposalRow {
  id: string
  status: string
  pending: boolean
  author: string
  reason: string
  content: string
  content_chars: number
  base_revision_id: string
  base_revision: number
  created_at: string
  source_entry_id: number
}

export interface WorkJobRow {
  id: string
  status: string
  /** `running` 但进程里没有 = 中断（不会自动重跑）。 */
  interrupted: boolean
  model_id: string
  brief: string
  created_at: string
  proposal_id: string
  source_entry_id: number
}

export interface WorkDetailPayload {
  available: boolean
  enabled: boolean
  generation_mode: string
  /** 服务层自己那句结论（`explain_works_state`）。 */
  explain: string
  broken?: boolean
  hint: string
  work_id: string
  story: StoryBrief | null
  participant_id: string
  participant: string
  title: string
  head: string
  generation: number
  /** 当前版本正文（**创作素材**，原样显示）。 */
  content: string
  content_chars: number
  revision: number
  revision_count: number
  revisions: WorkRevisionRow[]
  proposals: WorkProposalRow[]
  jobs: WorkJobRow[]
  pending_count: number
  jobs_running: number
  job_count: number
  may_propose: boolean
  may_propose_reason: string
  last_failure: WorkFailure | null
  limits: { content: number; brief: number; reason: number }
  /** 仅写操作的响应里出现。 */
  job?: { id?: string; status?: string } | null
  model_id?: string
  result?: { work_id: string; head: string; revisions: number; revision_id: string }
  changed?: string
}

export interface WorkExportPayload {
  available: boolean
  enabled: boolean
  work_id: string
  title: string
  parts: string[]
  count: number
  chars: number
  hint: string
}

/* ------------------------------------------------ 表情库（`console/stickers`） */

/** 一条素材（一行的 wire 形状；字段名与 `console_api.sticker_item()` 逐字一致）。 */
export interface StickerItem {
  assetId: string
  name: string
  description: string
  /** `image`（静止）| `animated`（会动）。 */
  kind: string
  /** `auto`（她收到时自动收藏的）| `manual`（从表情库目录扫进来的）。 */
  source: string
  addedAt: string
  updatedAt: string
  /** 被她选中投递过几次。 */
  uses: number
  disabled: boolean
  /** 相对表情库根目录的文件名（只显示，不用来拼路径）。 */
  file: string
  /** **相对**地址：要经 `endpointUrl()` 拼上插件名才能当图片 `src`。 */
  thumbnailUrl: string
  /** 描述是不是**人写的**（自动描述不会覆盖它）。 */
  manual: boolean
  /** `active` | `pending` | `missing` | `disabled`。 */
  status: string
  group: string
  /** 分组的**稳定 id**（= 磁盘目录名；筛选、移动都用它，空串 = 未分组桶）。 */
  groupId: string
  /** 分组显示名（只有内置组会与 `groupId` 不同）。 */
  groupName: string
  size: number
  aliases: unknown[]
  mimeType: string
}

/** 各状态的条数（`total` 是当前筛选条件下的合计）。 */
export interface StickerCounts {
  total: number
  active: number
  pending: number
  missing: number
  disabled: number
}

export interface StickerListPayload {
  items: StickerItem[]
  total: number
  /** 后端的扫描窗口被卡住了（素材比窗口还多，更早的没算进来）。 */
  truncated: boolean
  limit: number
  offset: number
  counts: StickerCounts
  /** 库总闸（`stickers.enabled`）：关着时重扫会被拒。 */
  enabled: boolean
  /** 自动收藏开关（`stickers.auto_collect`）。 */
  auto_collect: boolean
  directory: string
  root: string
}

export interface StickerUpdateResult {
  assetId: string
  changed: string[]
  item: StickerItem
}

export interface StickerRestoreResult extends StickerUpdateResult {
  hint?: string
}

export interface StickerDeleteResult {
  assetId: string
  /** `true` = 文件真删了；`false` = 只标记（可恢复）。 */
  purged: boolean
  deletedFile: boolean
  file: string
  changed: string[]
}

export interface StickerRescanResult {
  scanned: boolean
  /** 扫完库里有多少条。 */
  assets: number
  /** 这一轮新增了几条。 */
  added: number
}

/* ------------------------------- 表情库分组（`console/sticker-groups`，v1.8.4） */

/**
 * 一个分组。**`groupId` 的字面量就是磁盘上的目录名**（后端 v1.8.4 起）：顺序 =
 * 内置组 → 有描述的（按建组时间）→ 其余目录 / 有素材挂着的 → 空桶，就是后端给的顺序。
 *
 * 接口还会回一个 `registered` 兼容字段（恒 `true`）——**前端不读**："磁盘上有目录 /
 * 表里有行 / 有素材挂着"都算正式分组，读了它就会凭空多出"未注册"这种第二等公民。
 */
export interface StickerGroup {
  /** 目录名。空串 = 未分组桶（它筛不出来，也不能当移动 / 上传的目标）。 */
  groupId: string
  /** 显示名。**只有内置组**会与 `groupId` 不同（`collected` → 「未整理」）。 */
  name: string
  /** 给模型看的那份描述（"这一组什么风格、什么场合用"）；空串 = 没写。 */
  description: string
  /** 组里的素材张数（精确计数）。 */
  count: number
  /** 内置默认组：目录名不许改、也不给删除入口（后端都会 400）。 */
  builtin: boolean
  createdAt: string
  updatedAt: string
}

export interface StickerGroupListPayload {
  items: StickerGroup[]
  total: number
  truncated: boolean
  /** 删组分素材默认挪去哪 / 上传不给分组落哪。**直接用，别在前端写死**。 */
  defaultGroupId: string
}

export interface StickerGroupSaveResult {
  groupId: string
  item: StickerGroup
}

export interface StickerGroupDeleteResult {
  groupId: string
  deleted: boolean
  /** 被挪走的素材条数（删组**绝不删素材**）。 */
  moved: number
  moveTo: string
}

export interface StickerMoveResult {
  moved: number
  /** 改完之后库里那几行（据此就地更新列表）。 */
  item: StickerItem[]
}

export interface StickerUploadResult {
  assetId: string
  /** `true` = 库里已有同内容（不新建、不覆盖）。 */
  duplicated: boolean
  item: StickerItem
}
