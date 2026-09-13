"""Chunk5 mixin：`upstream/src/service.ts:4186-4813` 的全部成员。

逐条对应上游顺序（起始行落在 `[4186, 4813)` 内，边界成员 `deliverDueSplitSegments`
（`:4814`）归 Chunk6）：

| 上游行 | 成员 | 说明 |
| --- | --- | --- |
| 4186 | `persistTimelineSceneAnchor` | 场景锚点即时同步宿主账本 |
| 4197 | `adminSchedulePreplan` | 日程预排只读入口 |
| 4201 | `requestSchedulePreplanRebuild` | 管理员请求重建 |
| 4211/4215/4219/4223 | `alterSystemConfig` 等 4 个 getter | 转发 `ServiceBase` 的共享配置缓存 |
| 4227 | `emotionalOffsetForPrompt` | prompt 用的情绪偏移 |
| 4231 | `updateAlterSystem` | 一次 Alter 位移累进 |
| 4248 | `scheduleAlterAnalysis` | Alter 后台分析排队 |
| 4259 | `analyzeAlterSystem` | Alter 侧端分析 |
| 4312 | `appendEntry` | 剧本条目落库 + 召回缓存 |
| 4336 | `appendMemory` | 长期记忆落库 |
| 4350 | `contactThreads` | 联络证据链 |
| 4393 | `facts` | 长期事实检索与打分 |
| 4449 | `webObservations` | 送进 prompt 的网页观察 |
| 4468/4476 | `activeScene` / `activeArc` | 当前场景 / 剧情弧 |
| 4484 | `appendIntent` | 意图落库（含剧情余波） |
| 4512 | `activeConsequencesAndExpire` | 生效余波 + 到期清理 |
| 4537 | `applyIntentUpdates` | 模型关闭可见余波 |
| 4562 | `appendBrowserIntent` | 网页浏览意图 |
| 4593 | `executeDeferredBrowserIntent` | 执行到期浏览意图 |
| 4603 | `collectWebObservation` | 只读网页观察（Puppeteer → `Transport`） |
| 4672 | `saveWebObservation` | 观察落库 + 剧本条目 |
| 4691 | `persistCollectedWebObservation` | 延迟持久化立即浏览 |
| 4699 | `findCachedWebObservation` | 观察缓存命中 |
| 4711 | `withBrowserSlot` | 浏览器并发闸门 |
| 4724 | `scheduleNarrativeRetry` | 叙事失败的自动重试 |
| 4746 | `dueIntents` | 到期意图（含过期 Agency 清理） |
| 4759 | `upcomingNarrativeIntents` | 即将到来的计划 |
| 4769 | `scheduleDueIntentWake` | 到期唤醒计时器 |
| 4805 | `scheduleNextSplitWake` | 下一个拆分投递唤醒 |

本移植版的受控偏离（均在方法注释里就地说明）：

1. **数据库范围查询**：上游 `dbGet` 支持 `$lte` / `$gt` / `$in` 算子；`ServiceBase.db_get`
   刻意**拒绝算子**（见其 docstring）。本 chunk 一律"取回后 Python 侧过滤/按主键逐行取"，
   语义与上游一致。
2. **网页观察**：上游 `ctx.puppeteer.page()` → `Transport.search_web` / `visit_web`
   （`docs/PORT_PLAN_SERVICE.md` §7/§8）。浏览器不可用时的 `failed` 分支原样保留。
3. **`interlude_web_observation` 的主键**：上游写入 `id: 0` 让 ORM 生成 id；本移植版的
   `Database.insert` 会把显式 `0` 当真实主键（第二次写入就撞 UNIQUE），因此落库前省略
   `id` 字段交给 sqlite 回填（见 `save_web_observation`）。
4. **模块级辅助函数**：`normalizeBrowserIntentDraft` / `resolveBrowserTarget` /
   `isSafePublicWebUrl` / `browserIntentFromPayload` / `webObservationEntryContent` /
   `isActiveConsequence*` / `consequenceExpiresAt` / `consequenceStrength` /
   `normalizeIntentUpdates` 属于上游 `service.ts:7881-8036` 的模块级函数，按契约应落在
   `service/helpers.py`；该文件当前尚未包含它们，故本文件给出逐字等价实现，并沿用
   `base.py` 的 `_prefer_helper` 模式：`helpers.py` 一旦补上同名函数，自动改用它的版本。
5. **并发读**：上游 `facts()` 用 `Promise.all` 并发取三条查询；本移植版的 sqlite 连接
   只有写被锁保护，同一连接上的并发读会偶发 `sqlite3.InterfaceError`，故改为串行读
   （返回值与并发一致）。
6. **跨模块形状**：把库行交给 `plugin/core/script/*` 时先投影成 snake_case 领域对象
   （`_domain_fact` / `_domain_entry`），因为那些模块按 `types.py` 的 snake_case 读取。
"""

from __future__ import annotations

import asyncio
import math
import re
from typing import Any, Callable, Optional
from urllib.parse import quote as _urlencode, urlsplit

from ..time import dt_ms, format_log_time, iso, parse_dt

try:  # pragma: no cover - 取决于同批任务的落地顺序
    from . import helpers as _helpers_module
except ImportError:  # pragma: no cover
    _helpers_module = None  # type: ignore[assignment]

from ..alter import (
    advance_alter_system,
    alter_history_for_scope,
    alter_scope_cooling_down,
    alter_scope_value,
    calculate_alter_threshold,
    complete_alter_analysis,
    mark_alter_scope_analysis_attempt,
    normalize_alter_system_state,
)
from ..alter import emotional_offset_for_prompt as _alter_offset_for_prompt
from ..narrator_prompts import prompt_visible_message_content, recent_script_ownership
from ..script.episode_index import grounded_episode_tags
from ..script.knowledge_evidence import (
    contact_evidence_threads,
    knowledge_clauses,
    knowledge_related_ids,
    legacy_condition_cue,
)
from ..story_state import decode_story_state, encode_story_state
from .base import ServiceBase, normalize_database_row, pick
from .config import RECALLABLE_ENTRY_KINDS
from .helpers import (
    clamp_number,
    clip,
    fact_score,
    history_lexical_score,
    is_record,
    signed_number,
    to_date,
)

__all__ = ['ServiceChunk5']

#: 上游 `Time.second` / `Time.minute` / `Time.day`（Koishi 毫秒常量）。
_SECOND_MS = 1_000
_MINUTE_MS = 60_000
_DAY_MS = 86_400_000

#: 上游 `config.runtime.*` / `config.memory.*` / `config.browser.*` 的 schema 默认值
#: （`upstream/src/index.ts:298-324`、`275-289`）。本移植版的配置段可能尚未解析出
#: 默认值（`resolve_memory_config` 等仍由并行任务落地），故在此显式兜底。
_DEFAULT_SCENE_HOOK_CHARACTERS = 2_000
_DEFAULT_FACT_LIMIT = 20
_DEFAULT_MAX_FACTS_PER_STORY = 200
_DEFAULT_ACTIVE_CONSEQUENCES_ENABLED = True
_DEFAULT_ACTIVE_CONSEQUENCE_PROMPT_LIMIT = 6
_DEFAULT_ACTIVE_CONSEQUENCE_MAX_DAYS = 7
_DEFAULT_ACTIVE_CONSEQUENCE_STRENGTH = 0.55
_DEFAULT_NARRATIVE_RETRY_DELAY_SECONDS = 60
_DEFAULT_NARRATIVE_RETRY_MAX_ATTEMPTS = 6
_DEFAULT_BROWSER_MAX_OBSERVATIONS = 4
_DEFAULT_BROWSER_CACHE_MINUTES = 30
_DEFAULT_BROWSER_MAX_CONCURRENT_PAGES = 1
_DEFAULT_BROWSER_NAVIGATION_TIMEOUT = 15_000
_DEFAULT_BROWSER_MAX_TEXT_CHARACTERS = 12_000
_DEFAULT_BROWSER_MAX_EXCERPT_CHARACTERS = 3_000
_DEFAULT_BROWSER_SEARCH_URL_TEMPLATE = 'https://html.duckduckgo.com/html/?q={query}'

#: 上游 `normalizeIntentUpdates` 的 `status` 白名单。
_INTENT_UPDATE_STATUSES = ('completed', 'cancelled')

#: 上游 `upcomingNarrativeIntents` 里的内部意图类型（不展示给主叙事）。
_INTERNAL_INTENT_TYPES = frozenset({
    'split-message', 'browser-research', 'narrative-retry', 'proactive-check', 'active-consequence',
})

_CAMEL_BOUNDARY = re.compile(r'(?<!^)(?=[A-Z])')


# =========================================================================== #
# 键名法小工具（`docs/PORT_PLAN.md` §2）
# =========================================================================== #

def _snake(name: str) -> str:
    """`camelCase` → `snake_case`（只用于配置段名这种固定字符串）。"""
    return _CAMEL_BOUNDARY.sub('_', name).lower()


def _row(record: Any, camel: str, snake: Optional[str] = None) -> Any:
    """读一行**旧数据 / 模型产物**：上游 camelCase 优先，snake_case 兜底。"""
    return pick(record, camel, snake or _snake(camel))


def _section(config: Any, name: str) -> dict[str, Any]:
    """读一个配置段：dict 双读，对象走属性（与 `base.py` 的 `_config_section` 同义）。"""
    if config is None:
        return {}
    if isinstance(config, dict):
        value = pick(config, name, _snake(name))
        return value if isinstance(value, dict) else {}
    value = getattr(config, _snake(name), None)
    return value if isinstance(value, dict) else {}


def _cfg(section: Any, camel: str, default: Any = None) -> Any:
    """读配置项：camelCase 优先、snake_case 兜底，缺失时回落 schema 默认值。"""
    if not isinstance(section, dict):
        return default
    value = pick(section, camel, _snake(camel))
    return default if value is None else value


def _ms(value: Any) -> float:
    """上游 `dateValue(x)?.getTime() ?? 0`：无法解析的时间按 0（即"早已到期"）。"""
    parsed = parse_dt(value)
    return float(dt_ms(parsed)) if parsed is not None else 0.0


def _to_fixed(value: Any, digits: int = 2) -> str:
    """上游 `number.toFixed(digits)` 的等价物（只用于日志渲染）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    if not math.isfinite(float(value)):
        return str(value)
    return '%.*f' % (digits, float(value))


# =========================================================================== #
# 上游 `service.ts:7881-8036` 的模块级纯函数（`helpers.py` 缺失时的等价实现）
# =========================================================================== #

def _normalize_browser_intent_draft_loose(value: Any) -> Optional[dict[str, Any]]:
    """上游 `normalizeBrowserIntentDraftLoose`（`:7881`）逐字移植。

    输出**保持 camelCase**：它随后原样写进 `interlude_intent.payload`（历史库里的
    行也是这个形状），`browserIntentFromPayload` 按同一套键读回。
    """
    if not is_record(value):
        return None
    mode = pick(value, 'mode')
    if mode not in ('search', 'visit'):
        return None
    purpose = pick(value, 'purpose')
    if not isinstance(purpose, str):
        return None
    raw_query = pick(value, 'query')
    raw_url = pick(value, 'url')
    query = clip(raw_query, 500) if isinstance(raw_query, str) else ''
    url = clip(raw_url, 2_000) if isinstance(raw_url, str) else ''
    if mode == 'search' and not query:
        return None
    if mode == 'visit' and not url:
        return None
    normalized: dict[str, Any] = {'mode': mode}
    if query:
        normalized['query'] = query
    if url:
        normalized['url'] = url
    normalized['purpose'] = clip(purpose, 500)
    normalized['timing'] = 'immediate' if pick(value, 'timing') == 'immediate' else 'deferred'
    participant_id = pick(value, 'participantId', 'participant_id')
    if isinstance(participant_id, str):
        normalized['participantId'] = participant_id.strip()
    return normalized


def _normalize_browser_intent_draft(draft: Any, config: Any) -> Optional[dict[str, Any]]:
    """上游 `normalizeBrowserIntentDraft`（`:7896`）逐字移植。"""
    normalized = _normalize_browser_intent_draft_loose(draft)
    if not normalized:
        return None
    if normalized['mode'] == 'search' and not _cfg(config, 'allowSearch', True):
        return None
    if normalized['mode'] == 'visit' and not _cfg(config, 'allowVisit', True):
        return None
    return normalized


def _browser_intent_from_payload(payload: Any) -> Optional[dict[str, Any]]:
    """上游 `browserIntentFromPayload`（`:7904`）逐字移植。"""
    return _normalize_browser_intent_draft_loose({
        'mode': pick(payload, 'mode'),
        'query': pick(payload, 'query'),
        'url': pick(payload, 'url'),
        'purpose': pick(payload, 'purpose') or 'The character planned to read a public web page.',
        'timing': 'deferred',
    })


def _resolve_browser_target(draft: Any, config: Any) -> Optional[str]:
    """上游 `resolveBrowserTarget`（`:7914`）逐字移植。

    `String.replaceAll('{query}', encodeURIComponent(...))` → `str.replace` +
    `urllib.parse.quote(safe="-_.!~*'()")`（与 `encodeURIComponent` 的保留集一致）。
    """
    if pick(draft, 'mode') == 'search':
        template = str(_cfg(config, 'searchUrlTemplate', _DEFAULT_BROWSER_SEARCH_URL_TEMPLATE) or '').strip()
        if not template or '{query}' not in template:
            return None
        target = template.replace(
            '{query}', _urlencode(str(pick(draft, 'query') or ''), safe="-_.!~*'()"),
        )
        return target if _is_safe_public_web_url(target, config) else None
    url = pick(draft, 'url')
    if url and _is_safe_public_web_url(url, config):
        return url
    return None


def _is_safe_public_web_url(value: Any, config: Any) -> bool:
    """上游 `isSafePublicWebUrl`（`:7924`）逐字移植：只放行公开 http(s) 地址。"""
    try:
        parsed = urlsplit(str(value))
        if parsed.scheme not in ('http', 'https'):
            return False
        if parsed.username or parsed.password:
            return False
        host = (parsed.hostname or '').lower()
        if host.endswith('.'):
            host = host[:-1]
        if not host or host == 'localhost' or host.endswith('.localhost') or host == '::1':
            return False
        if _is_private_host(host):
            return False
        blocked = _normalize_domains(_cfg(config, 'blockedDomains', []))
        allowed = _normalize_domains(_cfg(config, 'allowedDomains', []))
        if any(_domain_matches(host, domain) for domain in blocked):
            return False
        return not allowed or any(_domain_matches(host, domain) for domain in allowed)
    except Exception:
        return False


def _normalize_domains(values: Any) -> list[str]:
    """上游 `normalizeDomains`（`:7941`）：去空白、小写、剥首尾点、丢掉空串。"""
    if not isinstance(values, (list, tuple)):
        return []
    result: list[str] = []
    for value in values:
        domain = re.sub(r'^\.+|\.+$', '', str(value if value is not None else '').strip().lower())
        if domain:
            result.append(domain)
    return result


def _domain_matches(host: str, domain: str) -> bool:
    """上游 `domainMatches`（`:7945`）。"""
    return host == domain or host.endswith('.%s' % domain)


def _is_private_host(host: str) -> bool:
    """上游 `isPrivateHost`（`:7947`）逐字移植（IPv4 私网段 + 一切 IPv6 字面量）。"""
    if re.fullmatch(r'\d{1,3}(?:\.\d{1,3}){3}', host):
        first, second = (int(part) for part in host.split('.')[:2])
        return (
            first == 10
            or first == 127
            or first == 0
            or (first == 169 and second == 254)
            or (first == 172 and 16 <= second <= 31)
            or (first == 192 and second == 168)
        )
    return ':' in host


def _web_observation_entry_content(observation: Any) -> str:
    """上游 `webObservationEntryContent`（`:7957`）逐字移植。"""
    if pick(observation, 'status') == 'success':
        source = pick(observation, 'title') or pick(observation, 'url') or 'a public web page'
        return 'The character read a public web page: %s.' % source
    return "The character's attempted web lookup did not complete: %s" % clip(
        pick(observation, 'summary'), 800,
    )


def _normalize_intent_updates(value: Any) -> list[dict[str, Any]]:
    """上游 `normalizeIntentUpdates`（`:8011`）逐字移植（最多 8 条）。"""
    if not isinstance(value, list):
        return []
    updates: list[dict[str, Any]] = []
    for item in value:
        if not is_record(item):
            continue
        raw_id = pick(item, 'id')
        if isinstance(raw_id, bool) or not isinstance(raw_id, int) or raw_id <= 0:
            continue
        status = pick(item, 'status')
        if status not in _INTENT_UPDATE_STATUSES:
            continue
        update: dict[str, Any] = {'id': int(raw_id), 'status': status}
        resolution = pick(item, 'resolution')
        if isinstance(resolution, str) and resolution.strip():
            update['resolution'] = clip(resolution, 1_000)
        updates.append(update)
    return updates[:8]


def _is_active_consequence(intent: Any) -> bool:
    """上游 `isActiveConsequence`（`:8022`）逐字移植。"""
    payload = pick(intent, 'payload')
    return (
        pick(intent, 'type') == 'active-consequence'
        and is_record(payload)
        and pick(payload, 'lifecycle') == 'active'
    )


def _is_active_consequence_draft(intent: Any) -> bool:
    """上游 `isActiveConsequenceDraft`（`:8026`）逐字移植。"""
    payload = pick(intent, 'payload')
    return (
        pick(intent, 'type') == 'active-consequence'
        and is_record(payload)
        and pick(payload, 'lifecycle') == 'active'
    )


def _consequence_expires_at(payload: Any) -> Any:
    """上游 `consequenceExpiresAt`（`:8030`）逐字移植。"""
    if not is_record(payload):
        return None
    return to_date(pick(payload, 'expiresAt', 'expires_at'))


def _consequence_strength(payload: Any, fallback: float = _DEFAULT_ACTIVE_CONSEQUENCE_STRENGTH) -> float:
    """上游 `consequenceStrength`（`:8035`）逐字移植。"""
    return clamp_number(pick(payload, 'strength') if is_record(payload) else None, fallback, 0, 1)


def _domain_fact(row: Any) -> dict[str, Any]:
    """数据库行（camelCase wire format）→ 领域事实（snake_case，`types.NarrativeFact`）。

    `plugin/core/script/*` 一律按 `types.py` 的 snake_case 读取（见
    `tests/test_script_evidence.py` 的「数据形状说明」），而库里的列名是上游
    camelCase wire format：交接处必须显式投影，不能把行原样递过去。
    """
    return {
        'id': _row(row, 'id'),
        'story_id': _row(row, 'storyId', 'story_id'),
        'participant_id': _row(row, 'participantId', 'participant_id'),
        'scope': _row(row, 'scope'),
        'content': _row(row, 'content'),
        'unresolved': _row(row, 'unresolved'),
        'status': _row(row, 'status'),
        'source_entry_ids': list(_row(row, 'sourceEntryIds', 'source_entry_ids') or []),
        # knowledge 本身就是 snake_case 的证据结构（`script/knowledge_evidence.py`）。
        'knowledge': _row(row, 'knowledge'),
    }


def _domain_entry(row: Any) -> dict[str, Any]:
    """数据库行 → 领域剧本条目（snake_case，`types.ScriptEntry`）。"""
    return {
        'id': _row(row, 'id'),
        'story_id': _row(row, 'storyId', 'story_id'),
        'participant_id': _row(row, 'participantId', 'participant_id'),
        'kind': _row(row, 'kind'),
        'actor': _row(row, 'actor'),
        'content': _row(row, 'content'),
        'occurred_at': _row(row, 'occurredAt', 'occurred_at'),
        'metadata': _row(row, 'metadata') or {},
    }


def _prefer_helper(name: str, fallback: Callable[..., Any]) -> Callable[..., Any]:
    """优先用 `helpers.py` 的移植版（与 `base.py` 的 `_prefer_helper` 同一模式）。

    `service.ts:7881-8036` 的模块级函数按 `docs/PORT_PLAN_SERVICE.md` 归 `helpers.py`；
    它们尚未落地时用本文件的等价实现，落地后自动切换，避免两份实现漂移。
    """
    candidate = getattr(_helpers_module, name, None)
    if callable(candidate):
        return candidate
    return fallback


normalize_browser_intent_draft = _prefer_helper(
    'normalize_browser_intent_draft', _normalize_browser_intent_draft,
)
browser_intent_from_payload = _prefer_helper('browser_intent_from_payload', _browser_intent_from_payload)
resolve_browser_target = _prefer_helper('resolve_browser_target', _resolve_browser_target)
is_safe_public_web_url = _prefer_helper('is_safe_public_web_url', _is_safe_public_web_url)
web_observation_entry_content = _prefer_helper(
    'web_observation_entry_content', _web_observation_entry_content,
)
normalize_intent_updates = _prefer_helper('normalize_intent_updates', _normalize_intent_updates)
is_active_consequence = _prefer_helper('is_active_consequence', _is_active_consequence)
is_active_consequence_draft = _prefer_helper('is_active_consequence_draft', _is_active_consequence_draft)
consequence_expires_at = _prefer_helper('consequence_expires_at', _consequence_expires_at)
consequence_strength = _prefer_helper('consequence_strength', _consequence_strength)


# =========================================================================== #
# 到期唤醒句柄：`DueIntentWake`（`config.py:921`） + `base.py` 暂停路径的兼容
# =========================================================================== #

class _DueIntentWake(dict):
    """`{cancel, due_at}` 的到期唤醒记录（上游 `dueIntentWakeTimers` 的值）。

    `ServiceBase.set_desktop_runtime_phase('paused')` 按**属性**读 `timer.cancel()`
    （上游写法 `timer.cancel()`），而 `config.py` 的 `DueIntentWake` 按**键**声明
    `cancel` / `due_at`。本类同时支持两种读法：`wake['due_at']` 与 `wake.cancel()`。
    """

    def __init__(self, handle: Any, due_at: float):
        super().__init__(cancel=handle, due_at=due_at)
        self._handle = handle

    @property
    def cancel(self) -> Any:
        return self._handle


# =========================================================================== #
# ServiceChunk5
# =========================================================================== #

class ServiceChunk5(ServiceBase):
    """对应 `upstream/src/service.ts` 第 4186–4813 行的成员。"""

    # ------------------------------------------------------------------ #
    # 场景锚点（`src/service.ts:4186`）
    # ------------------------------------------------------------------ #

    async def persist_timeline_scene_anchor(
        self, story_id: str, handoff: Any, entry_id: Any, now: Any,
    ) -> None:
        """上游 `persistTimelineSceneAnchor`（`:4186`）逐条移植。

        把当前场景的引子立刻对齐宿主账本，不必等散文压缩去调和已经结束的自动窗口。
        `handoff?.activity ?? handoff?.place` 的 `??`（只认 null/undefined）在本移植版
        用 `or` 表达：两个分支都是对象或空值，语义一致。
        """
        scene = await self.active_scene(story_id)
        anchor = pick(handoff, 'activity') or pick(handoff, 'place')
        if not scene or not anchor:
            return
        await self.db_set('interlude_scene', {'id': _row(scene, 'id')}, {
            'hook': clip(
                'Original #%s: %s' % (entry_id, pick(anchor, 'quote')),
                int(_cfg(self.memory_config, 'sceneHookCharacters', _DEFAULT_SCENE_HOOK_CHARACTERS)),
            ),
            # 场景/弧的摘要与检查点由后台编辑器负责；这里只写引子。
            'updatedAt': now,
        })

    # ------------------------------------------------------------------ #
    # 日程预排（`src/service.ts:4197`）
    # ------------------------------------------------------------------ #

    async def admin_schedule_preplan(self, story_id: str) -> Any:
        """上游 `adminSchedulePreplan`（`:4197`）。"""
        return await self.get_schedule_preplan(story_id)

    async def request_schedule_preplan_rebuild(self, story_id: str) -> bool:
        """上游 `requestSchedulePreplanRebuild`（`:4201`）逐条移植。"""
        current = await self.get_schedule_preplan(story_id)
        if not current:
            return False
        await self.db_set('interlude_schedule_preplan', {'storyId': story_id}, {
            'lastReviewedLocalDate': '',
            'validThrough': '1970-01-01',
            'reviewReason': 'Administrator requested a rebuild.',
            'updatedAt': self.now(),
        })
        self.schedule_compaction(story_id)
        return True

    # ------------------------------------------------------------------ #
    # 配置 getter（`src/service.ts:4211/4215/4219/4223`）
    # ------------------------------------------------------------------ #

    @property
    def alter_system_config(self) -> dict[str, Any]:
        """上游 `get alterSystemConfig()`（`:4211`）。

        解析与缓存落在 `ServiceBase`（全部 chunk 共享的配置段入口，见 base.py
        「配置段访问」），这里显式转发：本范围内声明的成员在 chunk5 里有落点，
        同时保证只有一份实现。
        """
        return super().alter_system_config

    @property
    def agency_config(self) -> dict[str, Any]:
        """上游 `get agencyConfig()`（`:4215`）：转发 `ServiceBase`。"""
        return super().agency_config

    @property
    def schedule_preplan_config(self) -> dict[str, Any]:
        """上游 `get schedulePreplanConfig()`（`:4219`）：转发 `ServiceBase`。"""
        return super().schedule_preplan_config

    @property
    def blind_mode_config(self) -> dict[str, Any]:
        """上游 `get blindModeConfig()`（`:4223`）：转发 `ServiceBase`。"""
        return super().blind_mode_config

    # ------------------------------------------------------------------ #
    # Alter System（`src/service.ts:4227-4310`）
    # ------------------------------------------------------------------ #

    def emotional_offset_for_prompt(self, story: Any) -> Optional[dict[str, Any]]:
        """上游 `emotionalOffsetForPrompt`（`:4227`）逐条移植。"""
        state = _row(pick(story, 'state'), 'alterSystem', 'alter_system')
        return _alter_offset_for_prompt(
            normalize_alter_system_state(state), self.alter_system_config,
        )

    def update_alter_system(
        self,
        story: Any,
        current: Any,
        alter: Any,
        phase: str,
        now: Any,
        participant_id: str = '',
    ) -> Optional[dict[str, Any]]:
        """上游 `updateAlterSystem`（`:4231`）逐条移植。

        `alter === undefined` 用 `alter is None` 表达（`0` 是合法位移，必须继续走）。
        """
        config = self.alter_system_config
        if not _cfg(config, 'enabled', False) or alter is None:
            return None
        result = advance_alter_system(current, alter, phase, now, config, participant_id)
        if _row(result, 'offsetExpired', 'offset_expired'):
            self.report_operation('standard', 'info', story, phase, 'Alter 情绪偏移已自然消退')
        state = _row(result, 'state')
        self.report_operation(
            'diagnostic', 'debug', story, phase,
            'Alter 状态已更新 来源=%s 本轮=%s 累计=%s 阈值=%s 权重=%s',
            _row(result, 'sourceParticipantId', 'source_participant_id') or '主角生活',
            alter,
            _row(result, 'triggerValue', 'trigger_value'),
            _to_fixed(_row(result, 'threshold'), 2),
            _to_fixed(_row(state, 'alterWeight', 'alter_weight'), 2),
        )
        return result

    def schedule_alter_analysis(self, story_id: str, phase: str, participant_id: str = '') -> None:
        """上游 `scheduleAlterAnalysis`（`:4248`）逐条移植。

        `ctx.setTimeout(fn, 0)` → 本移植版的等价计时器；回调里的
        `void this.serial(...).catch(...).finally(...)` → 后台任务 + `try/except/finally`。
        """
        task_key = '%s\u0000%s' % (story_id, participant_id)
        if task_key in self.scheduled_alter_analyses:
            return
        self.scheduled_alter_analyses.add(task_key)

        async def run() -> None:
            try:
                await self.run_in_queue(
                    story_id,
                    lambda: self.analyze_alter_system(story_id, phase, participant_id),
                )
            except Exception as error:
                self.report_standalone('warn', 'Alter 后台分析任务失败 故事=%s 错误=%s', story_id, error)
            finally:
                self.scheduled_alter_analyses.discard(task_key)

        def wake() -> None:
            asyncio.ensure_future(run())

        self.ctx.set_timeout(wake, 0)

    async def analyze_alter_system(self, story_id: str, phase: str, participant_id: str = '') -> None:
        """上游 `analyzeAlterSystem`（`:4259`）逐条移植。"""
        config = self.alter_system_config
        if not _cfg(config, 'enabled', False):
            return
        story = await self.get_story(story_id)
        if not story:
            return
        story_state = pick(story, 'state')
        state = normalize_alter_system_state(_row(story_state, 'alterSystem', 'alter_system'))
        if not state:
            return
        now = self.now()
        history = alter_history_for_scope(list(_row(state, 'history') or []), participant_id)
        trigger_value = alter_scope_value(state, participant_id)
        threshold = calculate_alter_threshold(history, config, now)
        if abs(trigger_value) < threshold or alter_scope_cooling_down(state, participant_id, now):
            return
        marked = mark_alter_scope_analysis_attempt(state, participant_id, now)
        await self.db_set('interlude_story', {'id': _row(story, 'id')}, {
            'state': encode_story_state({**decode_story_state(story_state), 'alter_system': marked}),
            'updatedAt': now,
        })
        analyze_alter = getattr(self.narrator, 'analyze_alter', None)
        if not callable(analyze_alter):
            self._report_event(
                'warn', story, phase,
                'Alter 已达到阈值，但当前叙事服务不支持侧端分析；保留累计值等待重试',
            )
            return

        trigger_direction = 1 if trigger_value > 0 else -1
        try:
            scripts: list[dict[str, Any]] = []
            for entry in await self.recent_entries(_row(story, 'id'), 50):
                if _row(entry, 'kind') != 'script':
                    continue
                content = str(_row(entry, 'content') or '')
                if not content.strip():
                    continue
                entry_participant = _row(entry, 'participantId', 'participant_id') or ''
                # 关系本地分析只看主角自己的生活条目与无归属条目。
                if participant_id:
                    if entry_participant and entry_participant != participant_id:
                        continue
                elif entry_participant:
                    continue
                scripts.append({
                    'content': content[:4_000],
                    'occurred_at': iso(_row(entry, 'occurredAt', 'occurred_at')),
                })
            scripts = scripts[-10:]
            self.report_operation(
                'standard', 'info', story, phase,
                'Alter 累积触发 数值=%s 阈值=%s 方向=%s',
                signed_number(trigger_value), _to_fixed(threshold, 2),
                '严肃' if trigger_direction > 0 else '放松',
            )
            current_offset = _row(state, 'emotionalOffset', 'emotional_offset')
            result = await analyze_alter({
                'character_name': _row(_row(_row(story, 'setting'), 'character'), 'name') or '',
                'trigger_value': trigger_value,
                'threshold': threshold,
                'direction': 'serious' if trigger_direction > 0 else 'relaxed',
                'recent_scripts': scripts,
                'history': history[-10:],
                'setting_overlay': _row(story_state, 'settingOverlay', 'setting_overlay') or {},
                'current_offset': (
                    {**current_offset, 'weight': _row(state, 'alterWeight', 'alter_weight')}
                    if current_offset else None
                ),
            }, config)
            description = str(_row(result, 'description') or '').strip()[:800]
            if not description:
                raise RuntimeError('Alter analysis returned an empty description.')
            completed = complete_alter_analysis(marked, description, threshold, now, config, participant_id)
            await self.db_set('interlude_story', {'id': _row(story, 'id')}, {
                'state': encode_story_state({**decode_story_state(story_state), 'alter_system': completed}),
                'updatedAt': now,
            })
            offset = _row(completed, 'emotionalOffset', 'emotional_offset') or {}
            self.report_operation(
                'standard', 'info', story, phase,
                '情绪偏移生成完成 方向=%s 强度=%s 描述=%s',
                _row(offset, 'direction'), _to_fixed(_row(offset, 'intensity'), 2), description,
            )
            self.report_operation('standard', 'info', story, phase, '情绪偏移已注入后续主提示词 权重=1.00')
        except Exception as error:
            self._report_event('warn', story, phase, 'Alter 分析失败，已保留累计值等待重试：%s', error)

    def _report_event(self, level: str, story: Any, phase: str, message: str, *args: Any) -> None:
        """上游 `report(level, story, phase, message, ...args)`（`:6724`，Chunk9）。

        Chunk9 落地前降级到 `ServiceBase.write_report`（同一实现，只少一层名字），
        让本 chunk 在分块并行期间也能独立运行。
        """
        report = getattr(self, 'report', None)
        if callable(report):
            report(level, story, phase, message, *args)
            return
        self.write_report(level, story, phase, message, list(args))

    # ------------------------------------------------------------------ #
    # 条目与记忆落库（`src/service.ts:4312-4342`）
    # ------------------------------------------------------------------ #

    async def append_entry(
        self, story_id: str, entry: Any, now: Any, participant_id: str = '',
    ) -> dict[str, Any]:
        """上游 `appendEntry`（`:4312`）逐条移植。

        场景条目数在压缩时推导；这里刻意不做第二次 SQLite 写，保证每次剧本追加
        既原子又便宜。
        """
        raw_kind = pick(entry, 'kind')
        kind = clip(raw_kind, 32) or 'life'
        created = await self.db_create('interlude_script_entry', {
            'storyId': story_id,
            'participantId': participant_id,
            'kind': kind,
            'actor': clip(pick(entry, 'actor') or 'character', 32),
            'content': (
                str(pick(entry, 'content') or '') if raw_kind == 'script'
                else clip(pick(entry, 'content'), 12_000)
            ),
            'occurredAt': to_date(_row(entry, 'occurredAt', 'occurred_at')) or now,
            'metadata': pick(entry, 'metadata') if is_record(pick(entry, 'metadata')) else {},
            'createdAt': now,
        })
        row = normalize_database_row('interlude_script_entry', created)
        recall_cache = self.history_vectors.get(story_id)
        if recall_cache is not None and _row(row, 'kind') in RECALLABLE_ENTRY_KINDS:
            metadata = _row(row, 'metadata') or {}
            metadata = metadata if isinstance(metadata, dict) else {}
            tags: list[str] = []
            for values in grounded_episode_tags(
                str(_row(row, 'content') or ''),
                _row(metadata, 'episodeTags', 'episode_tags') or {},
            ).values():
                tags.extend(values)
            frame_id = _row(metadata, 'frameId', 'frame_id')
            cached: dict[str, Any] = {
                'tags': tags,
                'checkpoint': _row(metadata, 'sceneCheckpoint', 'scene_checkpoint'),
                'frame_id': frame_id if isinstance(frame_id, str) else None,
                'content': prompt_visible_message_content(
                    _row(row, 'content'), recent_script_ownership(row),
                ),
                'occurred_at': iso(_row(row, 'occurredAt', 'occurred_at')),
                'participant_id': _row(row, 'participantId', 'participant_id'),
                'kind': _row(row, 'kind'),
            }
            embedding = _row(row, 'embedding')
            if isinstance(embedding, list) and embedding:
                cached['vector'] = embedding
            recall_cache[_row(row, 'id')] = cached
        return row

    async def append_memory(
        self,
        story_id: str,
        memory: Any,
        now: Any,
        participant_id: str = '',
        source_entry_id: Optional[int] = None,
    ) -> None:
        """上游 `appendMemory`（`:4336`）逐条移植。"""
        await self.db_create('interlude_memory', {
            'storyId': story_id,
            'participantId': participant_id,
            'category': clip(pick(memory, 'category'), 32) or 'fact',
            'content': clip(pick(memory, 'content'), 4_000),
            'importance': clamp_number(pick(memory, 'importance'), 0.5, 0, 1),
            'status': 'active',
            'sourceEntryId': source_entry_id if source_entry_id is not None else None,
            'createdAt': now,
            'updatedAt': now,
        })

    # ------------------------------------------------------------------ #
    # 长期事实（`src/service.ts:4350-4444`）
    # ------------------------------------------------------------------ #

    async def _rows_by_primary_ids(self, table: str, ids: Any) -> list[Any]:
        """上游 `dbGet(table, { id: { $in: ids } })` 的等价物。

        `ServiceBase.db_get` 刻意不支持 `$in`（见其 docstring），这里按主键逐行取：
        调用点给的都是有界的小集合（知识证据链 / 事实来源条目）。
        """
        if not isinstance(ids, (list, tuple, set)):
            return []
        wanted: list[int] = []
        for value in ids:
            if isinstance(value, bool) or not isinstance(value, int):
                continue
            if value not in wanted:
                wanted.append(value)
        rows: list[Any] = []
        for row_id in wanted:
            row = await self.db_read(
                lambda row_id=row_id: asyncio.to_thread(self.db.get, table, {'id': row_id}),
            )
            if row is not None:
                rows.append(normalize_database_row(table, row))
        return rows

    async def _set_intents(self, intents: Any, patch: dict[str, Any]) -> None:
        """上游 `dbSet('interlude_intent', { id: { $in: ids } }, patch)` 的等价物。"""
        for intent in intents or []:
            intent_id = _row(intent, 'id')
            if intent_id is None:
                continue
            await self.db_set('interlude_intent', {'id': intent_id}, dict(patch))

    async def contact_threads(
        self, story_id: str, selected: Any, participant_id: Optional[str] = None,
    ) -> list[Any]:
        """上游 `contactThreads`（`:4350`）逐条移植。

        独立的"生活/群导演"回合看不到任何私聊分支：`!fact.participantId
        || fact.participantId === participantId` 在 `participantId` 缺省时只放行世界级事实。
        """
        rows = await self.db_get(
            'interlude_fact', {'storyId': story_id, 'status': 'active'},
            {'limit': 1_000, 'sort': {'updatedAt': 'desc'}},
        )
        target = participant_id or ''

        def participant_of(fact: Any) -> str:
            return _row(fact, 'participantId', 'participant_id') or ''

        def visible_to_branch(fact: Any) -> bool:
            owner = participant_of(fact)
            return not owner or owner == target

        visible = [fact for fact in rows if visible_to_branch(fact)]

        def is_contact(fact: Any) -> bool:
            if _row(fact, 'scope') == 'promise':
                return True
            mode = _row(_row(fact, 'knowledge'), 'mode') or ''
            return mode in ('proposal', 'conditional', 'confirmed')

        seeds: list[Any] = []
        seen_ids: set[Any] = set()
        for fact in list(selected or []):
            if is_contact(fact) and visible_to_branch(fact):
                fact_id = _row(fact, 'id')
                if fact_id not in seen_ids:
                    seen_ids.add(fact_id)
                    seeds.append(fact)
        for fact in visible:
            if is_contact(fact) and _row(fact, 'unresolved'):
                fact_id = _row(fact, 'id')
                if fact_id not in seen_ids:
                    seen_ids.add(fact_id)
                    seeds.append(fact)
        seeds = seeds[:4]

        linked_ids: list[int] = []
        for fact in seeds:
            for related_id in knowledge_related_ids(_row(fact, 'knowledge')):
                if related_id not in linked_ids:
                    linked_ids.append(related_id)
        linked_ids = [rid for rid in linked_ids if not any(_row(fact, 'id') == rid for fact in visible)]
        if linked_ids:
            archived = await self._rows_by_primary_ids('interlude_fact', linked_ids)
            visible.extend(fact for fact in archived if visible_to_branch(fact))

        chain: dict[Any, Any] = {}
        for seed in seeds:
            chain[_row(seed, 'id')] = seed
            seed_related = knowledge_related_ids(_row(seed, 'knowledge'))
            seed_knowledge = _row(seed, 'knowledge')
            seed_content = str(_row(seed, 'content') or '')
            candidates: list[Any] = []
            for fact in visible:
                if participant_of(fact) != participant_of(seed) or _row(fact, 'id') == _row(seed, 'id'):
                    continue
                fact_knowledge = _row(fact, 'knowledge')
                same_topic = bool(
                    _row(seed_knowledge, 'topic')
                    and _row(fact_knowledge, 'topic') == _row(seed_knowledge, 'topic')
                )
                lexical = history_lexical_score(seed_content, str(_row(fact, 'content') or ''))
                if (
                    _row(fact, 'id') in seed_related
                    or _row(seed, 'id') in knowledge_related_ids(fact_knowledge)
                    or same_topic
                    or (not seed_knowledge and lexical >= 0.2)
                ):
                    candidates.append(fact)
            # 上游比较器：先按"在 seed 的关联 id 里"降序，再按字面相关度降序。
            candidates.sort(key=lambda fact, seed_related=seed_related, seed_content=seed_content: (
                -int(_row(fact, 'id') in seed_related),
                -history_lexical_score(seed_content, str(_row(fact, 'content') or '')),
            ))
            if seed_knowledge:
                related = candidates[:3]
            else:
                # 没有结构化知识证据时，条件线索优先，再补两条最近的候选。
                related = [
                    fact for fact in candidates if legacy_condition_cue(_row(fact, 'content'))
                ][:2] + candidates[:2]
            for fact in related:
                chain[_row(fact, 'id')] = fact
        facts = list(chain.values())[:12]

        source_ids: list[int] = []
        for fact in facts:
            for entry_id in _row(fact, 'sourceEntryIds', 'source_entry_ids') or []:
                if entry_id not in source_ids:
                    source_ids.append(entry_id)
            for clause in knowledge_clauses(_row(fact, 'knowledge')):
                clause_entry_id = _row(clause, 'sourceEntryId', 'source_entry_id')
                if clause_entry_id not in source_ids:
                    source_ids.append(clause_entry_id)
        if not source_ids:
            return []
        ids: list[int] = []
        for entry_id in source_ids:
            for candidate in (entry_id - 2, entry_id - 1, entry_id, entry_id + 1, entry_id + 2):
                if candidate > 0 and candidate not in ids:
                    ids.append(candidate)
        entries = await self._rows_by_primary_ids('interlude_script_entry', ids)
        # 保留整条原文；缺席的来源显式缺席，绝不被总结成"确定发生过"。
        budget = 12_000
        source_order = {entry_id: index for index, entry_id in enumerate(source_ids)}
        bounded = [entry for entry in entries if visible_to_branch(entry)]
        bounded.sort(key=lambda entry: (
            source_order.get(_row(entry, 'id'), math.inf), _row(entry, 'id') or 0,
        ))
        kept: list[Any] = []
        for entry in bounded:
            content = str(_row(entry, 'content') or '')
            if len(content) > budget:
                continue
            budget -= len(content)
            kept.append(entry)
        return contact_evidence_threads(
            [_domain_fact(fact) for fact in facts],
            [_domain_entry(entry) for entry in kept],
        )

    async def facts(
        self,
        story_id: str,
        limit: Optional[int] = None,
        query: str = '',
        participant_id: Optional[str] = None,
        turn_query_embedding: Optional[list[float]] = None,
    ) -> list[Any]:
        """上游 `facts`（`:4393`）逐条移植。

        旧实现的 50 条地板让每个实时回合都扫一大片事实表；这里保留"有界候选池"
        的写法：既够语义重排，又不把延迟敏感的路径压垮。
        """
        memory_config = self.memory_config
        if limit is None:
            limit = int(_cfg(memory_config, 'factLimit', _DEFAULT_FACT_LIMIT))
        limit = max(1, int(limit))
        max_facts = int(_cfg(memory_config, 'maxFactsPerStory', _DEFAULT_MAX_FACTS_PER_STORY))
        candidate_limit = max(300, min(max(limit * 10, max_facts * 3), 1_000))
        lane_limit = max(1, min(5, int(limit / 4) or 1))
        # 上游用 `Promise.all` 并发取这三条互不相干的查询；本移植版**串行**执行：
        # `plugin/core/database.py` 的连接是单条 sqlite3 连接，只有**写**被锁保护，
        # 同一连接上的并发读会偶发 `sqlite3.InterfaceError: bad parameter or other
        # API misuse`（`asyncio.to_thread` 会把它们丢进不同线程）。串行读的返回值
        # 与并发完全一致（受控偏离，见模块 docstring）。
        rows = await self.db_get('interlude_fact', {'storyId': story_id, 'status': 'active'}, {
            'limit': candidate_limit, 'sort': {'importance': 'desc', 'updatedAt': 'desc'},
        })
        recent_resolved_events = await self.db_get('interlude_fact', {
            'storyId': story_id, 'status': 'active', 'scope': 'event', 'unresolved': False,
        }, {'limit': lane_limit * 2, 'sort': {'updatedAt': 'desc'}})
        open_promises = await self.db_get('interlude_fact', {
            'storyId': story_id, 'status': 'active', 'scope': 'promise', 'unresolved': True,
        }, {'limit': lane_limit * 2, 'sort': {'updatedAt': 'desc'}})
        # 实时向量化会给每个用户回合加一次 HTTP 请求，因此保持显式开关；
        # 调用方已经算过的回合级向量优先复用，多个语义消费者共享一次请求。
        embedding_config = _section(_section(self.config, 'model'), 'embedding')
        if turn_query_embedding:
            query_embedding = turn_query_embedding
        elif query.strip() and _cfg(embedding_config, 'liveQuery', False):
            query_embedding = await self.embed_text(query)
        else:
            query_embedding = []

        def visible(fact: Any) -> bool:
            if participant_id is None:
                return True
            owner = _row(fact, 'participantId', 'participant_id') or ''
            return not owner or owner == participant_id

        scored = [
            (fact, fact_score(fact, memory_config, query_embedding, query))
            for fact in rows if visible(fact)
        ]
        scored.sort(key=lambda item: (
            -item[1],
            -_ms(_row(item[0], 'updatedAt', 'updated_at')),
            -(_row(item[0], 'id') or 0),
        ))

        selected: list[Any] = []
        seen: set[Any] = set()
        lane = (
            [fact for fact in recent_resolved_events if visible(fact)][:lane_limit]
            + [fact for fact in open_promises if visible(fact)][:lane_limit]
            + [fact for fact, _score in scored]
        )
        for fact in lane:
            fact_id = _row(fact, 'id')
            if fact_id in seen:
                continue
            seen.add(fact_id)
            selected.append(fact)
            if len(selected) >= limit:
                break
        return selected

    # ------------------------------------------------------------------ #
    # 网页观察读取（`src/service.ts:4449`）
    # ------------------------------------------------------------------ #

    async def web_observations(self, story_id: str, participant_id: Optional[str] = None) -> list[Any]:
        """上游 `webObservations`（`:4449`）逐条移植。

        网页浏览是可选项：默认关闭时连一次数据库读都不做。观察按"浏览分支"隔离，
        除非主人显式打开 `shareParticipantDetails`。
        """
        browser_config = self.browser_config
        if not _cfg(browser_config, 'enabled', False):
            return []
        limit = max(1, min(int(_cfg(
            browser_config, 'maxObservationsInPrompt', _DEFAULT_BROWSER_MAX_OBSERVATIONS,
        )), 20))
        rows = await self.db_get('interlude_web_observation', {'storyId': story_id}, {
            'limit': max(limit * 4, 20), 'sort': {'accessedAt': 'desc'},
        })
        share_details = bool(_cfg(self.shared_story_config, 'shareParticipantDetails', False))
        kept: list[Any] = []
        for observation in rows:
            # 失败/被拦截的尝试已经有简短的剧本事件；把它们的错误文本留在
            # 之后的每个 prompt 里只会浪费 token，还会挤掉有用的成功观察。
            if _row(observation, 'status') != 'success':
                continue
            owner = _row(observation, 'participantId', 'participant_id') or ''
            if not (share_details or not owner or owner == (participant_id or '')):
                continue
            kept.append(observation)
        kept = kept[:limit]
        kept.reverse()
        return kept

    # ------------------------------------------------------------------ #
    # 场景与弧（`src/service.ts:4468-4482`）
    # ------------------------------------------------------------------ #

    async def active_scene(self, story_id: str) -> Optional[dict[str, Any]]:
        """上游 `activeScene`（`:4468`）逐条移植。"""
        rows = await self.db_get('interlude_scene', {'storyId': story_id, 'status': 'active'}, {
            'limit': 1, 'sort': {'updatedAt': 'desc'},
        })
        return rows[0] if rows else None

    async def active_arc(self, story_id: str) -> Optional[dict[str, Any]]:
        """上游 `activeArc`（`:4476`）逐条移植。"""
        rows = await self.db_get('interlude_arc', {'storyId': story_id, 'status': 'active'}, {
            'limit': 1, 'sort': {'updatedAt': 'desc'},
        })
        return rows[0] if rows else None

    # ------------------------------------------------------------------ #
    # 意图生命周期（`src/service.ts:4484-4557`）
    # ------------------------------------------------------------------ #

    async def append_intent(
        self, story_id: str, intent: Any, now: Any, participant_id: str = '',
    ) -> None:
        """上游 `appendIntent`（`:4484`）逐条移植。

        排期计划永远面向未来；剧情余波不同：它是"已写在剧本里的事造成的当下处境"，
        所以从 now 开始，只需要一个有界的到期时间。
        """
        not_before = to_date(pick(intent, 'notBefore', 'not_before'))
        payload = pick(intent, 'payload')
        payload = payload if is_record(payload) else {}
        memory_config = self.memory_config
        active_consequence = is_active_consequence_draft(intent)
        if active_consequence and not _cfg(
            memory_config, 'activeConsequencesEnabled', _DEFAULT_ACTIVE_CONSEQUENCES_ENABLED,
        ):
            return
        requested_expires_at = consequence_expires_at(payload) if active_consequence else None
        max_lifetime = max(1, _cfg(
            memory_config, 'activeConsequenceMaxDays', _DEFAULT_ACTIVE_CONSEQUENCE_MAX_DAYS,
        )) * _DAY_MS
        expires_at = None
        if requested_expires_at is not None and dt_ms(requested_expires_at) > dt_ms(now):
            expires_at = parse_dt(min(dt_ms(requested_expires_at), dt_ms(now) + max_lifetime))
        if (
            not not_before
            or (not active_consequence and dt_ms(not_before) <= dt_ms(now))
            or (active_consequence and expires_at is None)
        ):
            return
        if active_consequence:
            normalized_payload: dict[str, Any] = dict(payload)
            normalized_payload['strength'] = consequence_strength(
                payload,
                _cfg(
                    memory_config, 'activeConsequenceDefaultStrength',
                    _DEFAULT_ACTIVE_CONSEQUENCE_STRENGTH,
                ),
            )
            normalized_payload['expiresAt'] = iso(expires_at)
        else:
            normalized_payload = payload
        await self.db_create('interlude_intent', {
            'storyId': story_id,
            'participantId': participant_id,
            'type': clip(pick(intent, 'type'), 32) or 'follow-up',
            'summary': clip(pick(intent, 'summary'), 4_000),
            'notBefore': not_before,
            'status': 'pending',
            'payload': normalized_payload,
            'createdAt': now,
            'updatedAt': now,
        })

    async def active_consequences_and_expire(
        self, story_id: str, now: Any, participant_id: Optional[str] = None,
    ) -> list[Any]:
        """上游 `activeConsequencesAndExpire`（`:4512`）逐条移植。

        剧情余波与排期意图共用意图表，但**永远不是调度工作**：它们的生命周期写在
        payload 里，老排期意图不需要迁移就保持原行为。
        """
        memory_config = self.memory_config
        if not _cfg(memory_config, 'activeConsequencesEnabled', _DEFAULT_ACTIVE_CONSEQUENCES_ENABLED):
            return []
        rows = await self.db_get('interlude_intent', {'storyId': story_id, 'status': 'pending'}, {
            'limit': 100, 'sort': {'updatedAt': 'desc'},
        })
        consequences = [row for row in rows if is_active_consequence(row)]
        now_ms = dt_ms(now)
        expired = [
            intent for intent in consequences
            if _ms(consequence_expires_at(_row(intent, 'payload'))) <= now_ms
        ]
        if expired:
            await self._set_intents(expired, {'status': 'completed', 'updatedAt': now})
        keep: list[Any] = []
        for intent in consequences:
            if _ms(_row(intent, 'notBefore', 'not_before')) > now_ms:
                continue
            expires_at = consequence_expires_at(_row(intent, 'payload'))
            if expires_at is None or dt_ms(expires_at) <= now_ms:
                continue
            owner = _row(intent, 'participantId', 'participant_id') or ''
            if participant_id is not None and owner and owner != participant_id:
                continue
            keep.append(intent)
        keep.sort(key=lambda intent: (
            -consequence_strength(_row(intent, 'payload')),
            -_ms(_row(intent, 'updatedAt', 'updated_at')),
        ))
        return keep[:max(1, int(_cfg(
            memory_config, 'activeConsequencePromptLimit', _DEFAULT_ACTIVE_CONSEQUENCE_PROMPT_LIMIT,
        )))]

    async def apply_intent_updates(
        self, story_id: str, updates: Any, now: Any, participant_id: Optional[str] = None,
    ) -> bool:
        """上游 `applyIntentUpdates`（`:4537`）逐条移植。

        只有写作者**看得见**的活跃余波才能被关闭：远端模型不能凭 id 改动任意未来计划。
        """
        if not updates:
            return False
        ids = [_row(update, 'id') for update in updates]
        rows = await self.db_get('interlude_intent', {'storyId': story_id, 'status': 'pending'})
        allowed: dict[Any, Any] = {}
        for intent in rows:
            if _row(intent, 'id') not in ids or not is_active_consequence(intent):
                continue
            owner = _row(intent, 'participantId', 'participant_id') or ''
            if participant_id and owner and owner != participant_id:
                continue
            allowed[_row(intent, 'id')] = intent
        changed = False
        for update in updates:
            intent = allowed.get(_row(update, 'id'))
            if intent is None:
                continue
            payload = dict(_row(intent, 'payload') or {})
            resolution = _row(update, 'resolution')
            if resolution:
                payload['resolution'] = resolution
            await self.db_set('interlude_intent', {'id': _row(intent, 'id')}, {
                'status': _row(update, 'status'), 'payload': payload, 'updatedAt': now,
            })
            changed = True
        return changed

    # ------------------------------------------------------------------ #
    # 网页浏览意图（`src/service.ts:4562-4721`）
    # ------------------------------------------------------------------ #

    async def append_browser_intent(
        self, story_id: str, draft: Any, now: Any, fallback_participant_id: str = '',
    ) -> None:
        """上游 `appendBrowserIntent`（`:4562`）逐条移植。

        模型永远不直接写页面内容：它只提出未来的浏览动作，由独立的浏览任务稍后
        产出观察。`fallbackParticipantId` 是"活跃参与者拥有本次实时浏览；无人值守的
        生活浏览是世界级的"这条隐私边界。
        """
        config = self.browser_config
        if not _cfg(config, 'enabled', False):
            return
        normalized = normalize_browser_intent_draft(draft, config)
        if not normalized:
            return
        participant_id = fallback_participant_id
        if participant_id:
            allowed = await self.get_participant(participant_id)
            if not allowed or not self.can_handle_participant(allowed):
                return
        not_before = parse_dt(dt_ms(now) + _SECOND_MS)
        await self.append_intent(story_id, {
            'type': 'browser-research',
            'summary': clip(_row(normalized, 'purpose'), 500) or 'The character planned to read a public web page.',
            'notBefore': iso(not_before),
            'payload': {
                'mode': _row(normalized, 'mode'),
                'query': _row(normalized, 'query') or '',
                'url': _row(normalized, 'url') or '',
                'purpose': _row(normalized, 'purpose'),
            },
        }, now, participant_id)
        self.report_standalone_operation(
            'diagnostic', 'debug', '已创建网页浏览意图：故事=%s 模式=%s',
            story_id, _row(normalized, 'mode'),
        )

    async def execute_deferred_browser_intent(
        self, story: Any, intent: Any, now: Any,
    ) -> Any:
        """上游 `executeDeferredBrowserIntent`（`:4593`）逐条移植。

        到期的浏览意图只执行一次，无论成败都标记完成：浏览器失败本身也是一件事
        （主角打不开那个页面），但它绝不阻塞之后的对话或生活推进。
        """
        payload = browser_intent_from_payload(_row(intent, 'payload'))
        observation = await self.collect_web_observation(
            story, payload, _row(intent, 'participantId', 'participant_id') or '',
            _row(intent, 'id'), now,
        )
        await self.db_set('interlude_intent', {'id': _row(intent, 'id')}, {
            'status': 'completed', 'updatedAt': self.now(),
        })
        return observation

    async def collect_web_observation(
        self,
        story: Any,
        draft: Any,
        participant_id: str,
        intent_id: Optional[int],
        now: Any,
        persist: bool = True,
    ) -> dict[str, Any]:
        """上游 `collectWebObservation`（`:4603`）逐条移植。

        上游走 Koishi Puppeteer；本移植版走 `Transport.search_web` / `visit_web`
        （只读、拒绝非公开目的地、每次观察后关闭页面这三条约束由适配层保证，
        `resolve_browser_target` 与 `is_safe_public_web_url` 在任何请求发出前先做
        同样的公开性校验）。
        """
        config = self.browser_config
        normalized = normalize_browser_intent_draft(draft, config) if draft else None
        story_id = _row(story, 'id')
        if not normalized or not _cfg(config, 'enabled', False):
            return await self.save_web_observation(
                story_id, participant_id, intent_id,
                _row(normalized, 'mode') or 'visit',
                _row(normalized, 'query') or '', _row(normalized, 'url') or '',
                '', '', '浏览未执行：功能未启用或请求不符合安全规则。', 'blocked', now, persist,
            )

        mode = _row(normalized, 'mode')
        query = _row(normalized, 'query') or ''
        target = resolve_browser_target(normalized, config)
        if not target:
            self._report_event(
                'warn', story, 'intent-due', '网页浏览被安全策略拦截：模式=%s', mode,
            )
            return await self.save_web_observation(
                story_id, participant_id, intent_id, mode, query, _row(normalized, 'url') or '',
                '', '', '浏览目标未通过公开网页安全校验。', 'blocked', now, persist,
            )

        cached = await self.find_cached_web_observation(story_id, participant_id, normalized, now)
        if cached:
            if not persist:
                return {
                    **cached, 'id': 0, 'intentId': intent_id, 'accessedAt': now, 'createdAt': now,
                }
            await self.append_entry(story_id, {
                'kind': 'web-observation', 'actor': 'system',
                'content': 'The character revisited a recent web observation: %s.' % (
                    _row(cached, 'title') or _row(cached, 'url')
                ),
                'occurredAt': iso(now),
                'metadata': {
                    'observationId': _row(cached, 'id'), 'cached': True,
                    'status': _row(cached, 'status'),
                },
            }, now, participant_id)
            return cached

        search_web = getattr(self.transport, 'search_web', None)
        visit_web = getattr(self.transport, 'visit_web', None)
        if not callable(search_web) or not callable(visit_web):
            self._report_event('warn', story, 'intent-due', '网页浏览服务不可用：未安装网页观察通道。')
            return await self.save_web_observation(
                story_id, participant_id, intent_id, mode, query, target,
                '', '', '浏览器服务不可用。', 'failed', now, persist,
            )

        async def run() -> dict[str, Any]:
            timeout = int(_cfg(config, 'navigationTimeout', _DEFAULT_BROWSER_NAVIGATION_TIMEOUT))
            max_text = int(_cfg(config, 'maxTextCharacters', _DEFAULT_BROWSER_MAX_TEXT_CHARACTERS))
            max_excerpt = int(_cfg(
                config, 'maxExcerptCharacters', _DEFAULT_BROWSER_MAX_EXCERPT_CHARACTERS,
            ))
            try:
                if mode == 'search':
                    results = await search_web(query, timeout)
                    top = results[0] if isinstance(results, list) and results else None
                    final_url = str(_row(top, 'url') or target)
                    title = clip(_row(top, 'title'), 500)
                    text = clip(_row(top, 'text') or _row(top, 'excerpt'), max_text)
                else:
                    page = await visit_web(target, timeout)
                    if not page:
                        raise RuntimeError('浏览通道没有返回内容。')
                    final_url = str(_row(page, 'url') or target)
                    title = clip(_row(page, 'title'), 500)
                    text = clip(_row(page, 'text'), max_text)
                if not is_safe_public_web_url(final_url, config):
                    raise RuntimeError('页面重定向到了不允许的地址。')
                excerpt = clip(text, max_excerpt)
                summary = clip('%s%s' % (('%s。' % title) if title else '', excerpt), max_excerpt)
                observation = await self.save_web_observation(
                    story_id, participant_id, intent_id, mode, query, final_url, title, excerpt,
                    summary or '页面没有可提取的正文。', 'success', self.now(), persist,
                )
                self.report_operation(
                    'standard', 'info', story, 'intent-due',
                    '网页读取完成 标题=%s 正文=%d字', title or '未命名页面', len(text),
                )
                if _cfg(config, 'logObservationPreview', False):
                    self._report_event('debug', story, 'intent-due', '网页观察节选：%s', excerpt)
                return observation
            except Exception as error:
                self._report_event('warn', story, 'intent-due', '网页读取失败：%s', error)
                return await self.save_web_observation(
                    story_id, participant_id, intent_id, mode, query, target, '', '',
                    '网页读取失败：%s' % clip(str(error), 500), 'failed', self.now(), persist,
                )

        return await self.with_browser_slot(run)

    async def save_web_observation(
        self,
        story_id: str,
        participant_id: str,
        intent_id: Optional[int],
        mode: str,
        query: Any,
        url: Any,
        title: Any,
        excerpt: Any,
        summary: Any,
        status: str,
        now: Any,
        persist: bool = True,
    ) -> dict[str, Any]:
        """上游 `saveWebObservation`（`:4672`）逐条移植。

        落库时**省略 `id`**：上游写 `id: 0` 交给 ORM 生成主键，而本移植版的
        `Database.insert` 会把显式 `0` 当真实主键（第二次写入撞 UNIQUE），
        故交给 sqlite 的 `lastrowid` 回填（受控偏离，见模块 docstring）。
        """
        max_excerpt = int(_cfg(
            self.browser_config, 'maxExcerptCharacters', _DEFAULT_BROWSER_MAX_EXCERPT_CHARACTERS,
        ))
        candidate: dict[str, Any] = {
            'id': 0,
            'storyId': story_id,
            'participantId': participant_id,
            'intentId': intent_id,
            'mode': mode,
            'query': clip(query, 500),
            'url': clip(url, 2_000),
            'title': clip(title, 500),
            'excerpt': clip(excerpt, max_excerpt),
            'summary': clip(summary, max_excerpt),
            'status': status,
            'accessedAt': now,
            'createdAt': now,
        }
        if not persist:
            return candidate
        observation = normalize_database_row(
            'interlude_web_observation',
            await self.db_create(
                'interlude_web_observation',
                {key: value for key, value in candidate.items() if key != 'id'},
            ),
        )
        await self.append_entry(story_id, {
            'kind': 'web-observation', 'actor': 'system',
            'content': web_observation_entry_content(observation),
            'occurredAt': iso(now),
            'metadata': {
                'observationId': _row(observation, 'id'), 'status': status, 'mode': mode,
                'url': _row(observation, 'url'),
            },
        }, now, participant_id)
        return observation

    async def persist_collected_web_observation(self, observation: Any) -> dict[str, Any]:
        """上游 `persistCollectedWebObservation`（`:4691`）逐条移植。

        实时浏览的结果先留在内存里，直到主叙事结果通过 stale 检查才落库：
        过期的两秒消息风暴不该留下持久的网页事件。
        """
        return await self.save_web_observation(
            _row(observation, 'storyId', 'story_id'),
            _row(observation, 'participantId', 'participant_id'),
            _row(observation, 'intentId', 'intent_id'),
            _row(observation, 'mode'),
            _row(observation, 'query'),
            _row(observation, 'url'),
            _row(observation, 'title'),
            _row(observation, 'excerpt'),
            _row(observation, 'summary'),
            _row(observation, 'status'),
            _row(observation, 'accessedAt', 'accessed_at'),
        )

    async def find_cached_web_observation(
        self, story_id: str, participant_id: str, draft: Any, now: Any,
    ) -> Any:
        """上游 `findCachedWebObservation`（`:4699`）逐条移植（未命中返回 `None`）。"""
        minutes = _cfg(self.browser_config, 'cacheMinutes', _DEFAULT_BROWSER_CACHE_MINUTES)
        minutes = float(minutes) if isinstance(minutes, (int, float)) and not isinstance(minutes, bool) else 0.0
        if minutes <= 0:
            return None
        cutoff = parse_dt(dt_ms(now) - minutes * _MINUTE_MS)
        rows = await self.db_get('interlude_web_observation', {
            'storyId': story_id, 'participantId': participant_id, 'status': 'success',
        }, {'limit': 20, 'sort': {'accessedAt': 'desc'}})
        mode = _row(draft, 'mode')
        for observation in rows:
            accessed_at = parse_dt(_row(observation, 'accessedAt', 'accessed_at'))
            if accessed_at is None or accessed_at < cutoff:
                continue
            if _row(observation, 'mode') != mode:
                continue
            if mode == 'search':
                if _row(observation, 'query') == (_row(draft, 'query') or ''):
                    return observation
            elif _row(observation, 'url') == (_row(draft, 'url') or ''):
                return observation
        return None

    async def with_browser_slot(self, task: Callable[[], Any]) -> Any:
        """上游 `withBrowserSlot`（`:4711`）逐条移植：浏览器并发闸门。

        上游 `new Promise(resolve => this.browserWaiters.push(resolve))` +
        `this.browserWaiters.shift()?.()`：本移植版同样把**释放函数**压进队列
        （`base.py` 把该字段声明为 `list[Callable[[], None]]`）。
        """
        maximum = max(1, int(_cfg(
            self.browser_config, 'maxConcurrentPages', _DEFAULT_BROWSER_MAX_CONCURRENT_PAGES,
        )))
        if self.browser_active >= maximum:
            loop = asyncio.get_running_loop()
            waiter: asyncio.Future[Any] = loop.create_future()

            def release() -> None:
                if not waiter.done():
                    waiter.set_result(None)

            self.browser_waiters.append(release)
            await waiter
        self.browser_active += 1
        try:
            return await task()
        finally:
            self.browser_active -= 1
            if self.browser_waiters:
                head = self.browser_waiters.pop(0)
                if callable(head):
                    head()

    # ------------------------------------------------------------------ #
    # 叙事重试与到期唤醒（`src/service.ts:4724-4811`）
    # ------------------------------------------------------------------ #

    async def schedule_narrative_retry(
        self, story_id: str, participant_id: str, now: Any, previous_attempts: int = 0,
    ) -> bool:
        """上游 `scheduleNarrativeRetry`（`:4724`）逐条移植。

        落一条有界的重试意图，让一次瞬时 provider 失败不会永久搁浅一个用户回合。
        """
        runtime = self.runtime_config
        delay_seconds = max(5, _cfg(
            runtime, 'narrativeRetryDelaySeconds', _DEFAULT_NARRATIVE_RETRY_DELAY_SECONDS,
        ))
        max_attempts = max(0, _cfg(
            runtime, 'narrativeRetryMaxAttempts', _DEFAULT_NARRATIVE_RETRY_MAX_ATTEMPTS,
        ))
        pending = await self.db_get('interlude_intent', {
            'storyId': story_id, 'participantId': participant_id, 'status': 'pending',
        })
        existing = [intent for intent in pending if _row(intent, 'type') == 'narrative-retry']
        if existing:
            await self._set_intents(existing, {'status': 'cancelled', 'updatedAt': now})
        if not participant_id or previous_attempts >= max_attempts:
            self.report_standalone(
                'warn', '叙事模型自动重试已停止 故事=%s 参与者=%s 已尝试=%d 上限=%d',
                story_id, participant_id or '全局', previous_attempts, max_attempts,
            )
            return False
        attempt = previous_attempts + 1
        not_before = parse_dt(dt_ms(now) + float(delay_seconds) * _SECOND_MS)
        await self.append_intent(story_id, {
            'type': 'narrative-retry',
            'summary': 'Retry the interrupted narrative turn after provider failure (attempt %d/%d).' % (
                attempt, max_attempts,
            ),
            'notBefore': iso(not_before),
            'payload': {'narrativeRetry': True, 'userInitiated': True, 'attempt': attempt},
        }, now, participant_id)
        self.report_standalone(
            'warn', '叙事模型请求失败，已安排自动重试 故事=%s 参与者=%s 次数=%d/%d 等待=%d秒',
            story_id, participant_id, attempt, max_attempts, delay_seconds,
        )
        return True

    async def due_intents(self, story_id: str, now: Any) -> list[Any]:
        """上游 `dueIntents`（`:4746`）逐条移植。

        上游用 `notBefore: { $lte: now }`；本移植版取回 pending 行后在 Python 侧
        过滤并排序（`ServiceBase.db_get` 不支持算子）。
        """
        intents = await self.db_get('interlude_intent', {'storyId': story_id, 'status': 'pending'})
        now_ms = dt_ms(now)
        due = [
            intent for intent in intents
            if _ms(_row(intent, 'notBefore', 'not_before')) <= now_ms
        ]
        due.sort(key=lambda intent: _ms(_row(intent, 'notBefore', 'not_before')))
        agency_enabled = bool(_cfg(self.agency_config, 'enabled', False))
        expired_agency: list[Any] = []
        for intent in due:
            if _row(intent, 'type') != 'proactive-check':
                continue
            expires_at = to_date(_row(_row(intent, 'payload') or {}, 'expiresAt', 'expires_at'))
            if not agency_enabled or expires_at is None or dt_ms(expires_at) <= now_ms:
                expired_agency.append(intent)
        if expired_agency:
            await self._set_intents(expired_agency, {'status': 'cancelled', 'updatedAt': now})
        expired_ids = {_row(intent, 'id') for intent in expired_agency}
        return [
            intent for intent in due
            if _row(intent, 'id') not in expired_ids and not is_active_consequence(intent)
        ]

    async def upcoming_narrative_intents(self, story_id: str, now: Any) -> list[Any]:
        """上游 `upcomingNarrativeIntents`（`:4759`）逐条移植。

        上游是 `notBefore: { $gt: now }` + `sort asc` + `limit 30`；本移植版先过滤
        再排序再截断，等价于同一条 SQL 的结果集。
        """
        rows = await self.db_get('interlude_intent', {'storyId': story_id, 'status': 'pending'})
        now_ms = dt_ms(now)
        upcoming = [
            intent for intent in rows
            if _ms(_row(intent, 'notBefore', 'not_before')) > now_ms
        ]
        upcoming.sort(key=lambda intent: _ms(_row(intent, 'notBefore', 'not_before')))
        return [
            intent for intent in upcoming[:30]
            if _row(intent, 'type') not in _INTERNAL_INTENT_TYPES
        ][:8]

    def schedule_due_intent_wake(self, story_id: str, not_before: Any) -> None:
        """上游 `scheduleDueIntentWake`（`:4769`）逐条移植。

        到期的意图本身才是唯一事实来源；这里只是把唤醒提前到接近模拟打字延迟的
        时刻，而不是等下一次常规 sweep。多个 `<sep/>` 段可能同时排期：保留**最早**
        的那次唤醒，下一段到期时再排下一次。
        """
        due_at = _ms(not_before)
        delay = max(0.0, due_at - float(self.now_ms()))
        existing = self.due_intent_wake_timers.get(story_id)
        if existing is not None and _row(existing, 'dueAt', 'due_at') <= due_at:
            return
        if existing is not None:
            cancel = _row(existing, 'cancel')
            if callable(cancel):
                cancel()

        def wake() -> None:
            self.due_intent_wake_timers.pop(story_id, None)
            # 一次长叙事请求可能盖过模拟打字延迟：把意图留作 pending，等调度器
            # 空闲后稍后重试，而不是等下一次常规 sweep。
            if self.database_resetting:
                return

            async def run() -> None:
                try:
                    due = await self.due_intents(story_id, self.now())
                    # 拆分消息段已经是提交过的传输事件：直接进故事队列投递，
                    # 不要让它们等五分钟的 sweep，也不要另起一次叙事请求。
                    if any(_row(intent, 'type') == 'split-message' for intent in due):
                        await self.deliver_due_split_segments(story_id)
                        if all(_row(intent, 'type') == 'split-message' for intent in due):
                            return
                    if self.sweep_running or self.has_pending_narrative(story_id):
                        retry_at = self.now_ms() + _SECOND_MS
                        retry = self.ctx.set_timeout(wake, _SECOND_MS)
                        self.due_intent_wake_timers[story_id] = _DueIntentWake(retry, retry_at)
                        return
                    await self.sweep()
                except Exception as error:
                    self.report_standalone_operation(
                        'diagnostic', 'debug', '到期消息唤醒失败 错误=%s', error,
                    )

            asyncio.ensure_future(run())

        timer = self.ctx.set_timeout(wake, delay)
        self.due_intent_wake_timers[story_id] = _DueIntentWake(timer, due_at)
        self.report_standalone_operation(
            'diagnostic', 'debug', '已设置到期计时器 故事=%s 触发时间=%s 等待=%dms',
            story_id, format_log_time(not_before, 'Asia/Shanghai'), delay,
        )

    async def schedule_next_split_wake(self, story_id: str) -> None:
        """上游 `scheduleNextSplitWake`（`:4805`）逐条移植。"""
        pending = await self.db_get('interlude_intent', {
            'storyId': story_id, 'status': 'pending', 'type': 'split-message',
        }, {'sort': {'notBefore': 'asc'}, 'limit': 1})
        if pending:
            self.schedule_due_intent_wake(story_id, _row(pending[0], 'notBefore', 'not_before'))
