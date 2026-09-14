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

export interface RoutingRow {
  task: string
  label: string
  source: 'astrbot' | 'connection' | 'none'
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
  intents: Array<{ id: number; type: string; summary: string; status: string; not_before: string }>
  patches: Array<{
    id: number
    target: string
    path: string
    proposed_value: string
    confidence: number
    impact: string
    status: string
    created_at: string
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
