"""service 的类字段、构造、队列/数据库/日志基础设施，以及 Chunk0 mixin。

对应上游 `upstream/src/service.ts:624-1247`：

- `ServiceBase`：`src/service.ts:624-717` 的**全部实例字段**逐一声明（snake_case），
  外加共享基础设施（`InterludeContext` / `write()` / `run_in_queue()` / `serial()` /
  `db_get()` / `db_create()` / `db_set()` / `db_remove()` / 分层日志）。
- `ServiceChunk0`：`src/service.ts:717-1247` 的全部成员（constructor、后台调度、
  narrator/compactor/embedder 注入、桌面桥入口与只读投影、权限判定、故事与参与者）。

模块级共用小工具（供全部 9 个 mixin 复用，见 `docs/PORT_PLAN.md` §2「键名法」）：

- `pick(value, camel, snake=None)`：**从外部读入**的 dict 双读，优先上游 camelCase。
  这是全工程唯一的公开实现，`plugin.core.service` 会 re-export。
- `normalize_account_id` / `is_one_bot_platform` / `is_enabled_account` /
  `normalize_group_id` / `story_id_for_character` / `legacy_story_id_for` /
  `participant_id_for` / `participant_id_for_story` / `same_participant_endpoint` /
  `merge_setting` / `normalize_database_row`：上游 `service.ts` 里的模块级纯函数。
  **优先**复用 `helpers.py` 的移植版（`_prefer_helper`），缺失时用本文件的等价实现，
  这样本层既能独立导入/测试，也不会和 helpers 移植任务产生两份漂移的实现。

`InterludeContext` 是 Koishi `Context` 的等价容器；它持有 `database`（不 import
`astrbot`），并把时间/随机/模型解析做成显式注入点。
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from collections.abc import Awaitable, Callable
from typing import Any, Optional

from ..time import dt_ms, iso, parse_dt, utc_now
from .. import logging as interlude_logging
from ..database import timestamp_columns
from ..logging import format_layered_log, log_layered, phase_label
from ..story_state import decode_story_state
from ..types import InterludeParticipant, InterludeStory, StorySetting, StoryState, StoryStatus

# helpers.py 由并行的移植任务产出：优先用它的实现，缺失时用本文件的等价实现。
try:  # pragma: no cover - 取决于同批任务的落地顺序
    from . import helpers as _helpers_module
except ImportError:  # pragma: no cover
    _helpers_module = None  # type: ignore[assignment]

try:  # pragma: no cover
    from .config import Config
except ImportError:  # pragma: no cover - Config 接口尚未落地时用宽松标注
    Config = dict  # type: ignore[assignment,misc]

__all__ = [
    'Config',
    'GROUP_SKIP_NOTE_INTERVAL_MS',
    'InterludeContext',
    'ServiceBase',
    'ServiceChunk0',
    'TimerHandle',
    'is_enabled_account',
    'is_one_bot_platform',
    'legacy_story_id_for',
    'merge_setting',
    'normalize_account_id',
    'normalize_database_row',
    'normalize_group_id',
    'participant_id_for',
    'participant_id_for_story',
    'pick',
    'same_participant_endpoint',
    'story_id_for_character',
]

#: 群聊"为什么没动静"的可见日志节流窗口（10 分钟）。
#: 群消息是持续的，不节流会淹掉整个日志；但完全静默会让"群聊没生效"变成只能靠猜。
GROUP_SKIP_NOTE_INTERVAL_MS = 600_000


def _prefer_helper(name: str, fallback: Callable[..., Any]) -> Callable[..., Any]:
    """优先返回 `helpers.py` 里的同名移植版；缺失时返回本文件的等价实现。"""
    if _helpers_module is not None:
        candidate = getattr(_helpers_module, name, None)
        if callable(candidate):
            return candidate
    return fallback


# =========================================================================== #
# 键名法共享小工具
# =========================================================================== #

def pick(value: Any, camel: str, snake: Optional[str] = None) -> Any:
    """从**外部读入**的 dict 里取值，两种拼写都认，优先上游 camelCase。

    与 `plugin/core/agency.py` / `plugin/core/story_state.py` 里的同名私有实现
    语义逐字一致（`docs/PORT_PLAN.md` §2「键名法」）。本移植版把它的**唯一公开
    位置**定在这里，`plugin.core.service` 会 re-export；9 个 chunk 一律
    `from .base import pick`，不要再各写各的。

    - 既不是 dict 也不支持 `__getitem__` 时返回 `None`；
    - `camel` 命中（即使值为 `None`）就返回它，否则回落到 `snake`。
    """
    if isinstance(value, dict):
        if camel in value:
            return value[camel]
        return value.get(snake) if snake else None
    getter = getattr(value, '__getitem__', None)
    if getter is None:
        return None
    for key in ((camel, snake) if snake is not None else (camel,)):
        try:
            return getter(key)
        except (KeyError, TypeError, IndexError):
            continue
    return None


def _fallback_normalize_account_id(value: Any) -> str:
    """上游 `normalizeAccountId`（`src/service.ts:7682`）逐字移植。

    归一化带传输前缀的 QQ id（`private:123` / `onebot:123` / `qq:123`），
    最多剥三层：`normalized.replace(/^(?:private|user|onebot|napcat|qq):/i, '').trim()`。
    """
    normalized = str(value if value is not None else '').strip().lower()
    prefixes = ('private:', 'user:', 'onebot:', 'napcat:', 'qq:')
    for _ in range(3):
        updated = normalized
        for prefix in prefixes:
            if updated.startswith(prefix):
                updated = updated[len(prefix):].strip()
                break
        if updated == normalized:
            break
        normalized = updated
    return normalized


def _fallback_is_one_bot_platform(platform: Any) -> bool:
    """上游 `isOneBotPlatform`（`src/service.ts:7026`）逐字移植。"""
    value = str(platform if platform is not None else '').lower()
    return (
        value == 'onebot'
        or value.startswith('onebot:')
        or value == 'napcat'
        or value.startswith('napcat:')
        or value == 'qq:onebot'
        or value.startswith('qq:onebot:')
    )


def _fallback_is_enabled_account(accounts: Any, qq: Any) -> bool:
    """上游 `isEnabledAccount`（`src/service.ts:7705`）逐字移植。

    `accounts` 是 `config.onebot.botAccounts` / `userAccounts` 那类规则列表，
    元素既可能是 dict（上游 camelCase `qq` / `enabled`）也可能是适配器对象。
    """
    normalized = normalize_account_id(qq)
    if not normalized:
        return False
    if not isinstance(accounts, (list, tuple)):
        return False
    for account in accounts:
        if pick(account, 'enabled') is False:
            continue
        if normalize_account_id(pick(account, 'qq')) == normalized:
            return True
    return False


def _fallback_normalize_group_id(value: Any) -> str:
    """上游 `normalizeGroupId`（`src/service.ts:7251`）逐字移植。

    `String(value || '').trim().replace(/^(?:group|guild):/i, '')`
    """
    text = ('' if value is None else str(value)).strip()
    lowered = text.lower()
    for prefix in ('group:', 'guild:'):
        if lowered.startswith(prefix):
            return text[len(prefix):]
    return text


def _fallback_story_id_for_character(platform: Any, self_id: Any) -> str:
    """上游 `storyIdForCharacter`（`src/service.ts:7009`）。"""
    return 'character:%s:%s' % (platform, self_id)


def _fallback_legacy_story_id_for(platform: Any, self_id: Any, user_id: Any) -> str:
    """上游 `legacyStoryIdFor`（`src/service.ts:7011`）。"""
    return '%s:%s:%s' % (platform, self_id, user_id)


def _fallback_participant_id_for(platform: Any, self_id: Any, user_id: Any) -> str:
    """上游 `participantIdFor`（`src/service.ts:7013`）。"""
    return '%s:%s:%s' % (platform, self_id, user_id)


def _fallback_participant_id_for_story(story_id: Any, platform: Any, self_id: Any, user_id: Any) -> str:
    """上游 `participantIdForStory`（`src/service.ts:7015`）：超长时截断到 255。"""
    return ('%s:%s' % (participant_id_for(platform, self_id, user_id), story_id))[:255]


def _fallback_same_participant_endpoint(participant: Any, session: Any) -> bool:
    """上游 `sameParticipantEndpoint`（`src/service.ts:7019`）逐字移植。"""
    participant_platform = pick(participant, 'platform') or ''
    session_platform = pick(session, 'platform') or ''
    onebot_pair = is_one_bot_platform(participant_platform) and is_one_bot_platform(session_platform)
    return (
        (participant_platform == session_platform or onebot_pair)
        and normalize_account_id(pick(participant, 'selfId', 'self_id'))
        == normalize_account_id(pick(session, 'selfId', 'self_id'))
        and normalize_account_id(pick(participant, 'userId', 'user_id'))
        == normalize_account_id(pick(session, 'userId', 'user_id'))
    )


def _fallback_merge_setting(base: Any, patch: Any) -> StorySetting:
    """上游 `mergeSetting`（`src/service.ts:8078`）逐字移植。

    `{...base, ...patch, character: {...base.character, ...patch.character},
    user: {...base.user, ...patch.user}}`：`character` / `user` 是**浅合并**，
    且 `patch` 里没有这两个键时保留 `base` 的原值。
    """
    base_record = base if isinstance(base, dict) else {}
    patch_record = patch if isinstance(patch, dict) else {}
    merged = dict(base_record)
    merged.update(patch_record)
    for key in ('character', 'user'):
        current = base_record.get(key)
        current = dict(current) if isinstance(current, dict) else {}
        incoming = patch_record.get(key)
        if isinstance(incoming, dict):
            current.update(incoming)
        merged[key] = current
    return merged  # type: ignore[return-value]


def _normalize_participant_state(value: Any) -> dict[str, Any]:
    """上游 `normalizeParticipantState`（`src/service.ts:8090`）的等价实现，输出 snake_case。"""
    record = value if isinstance(value, dict) else {}
    open_threads = pick(record, 'openThreads', 'open_threads')
    relationship_notes = pick(record, 'relationshipNotes', 'relationship_notes')
    unread = pick(record, 'unreadMessageCount', 'unread_message_count')
    pending = pick(record, 'pendingReplyCount', 'pending_reply_count')
    state: dict[str, Any] = {}
    for key, item in record.items():
        if key in ('openThreads', 'relationshipNotes', 'unreadMessageCount', 'pendingReplyCount'):
            continue
        state[key] = item
    state['open_threads'] = [str(item) for item in open_threads] if isinstance(open_threads, list) else []
    state['relationship_notes'] = [str(item) for item in relationship_notes] if isinstance(relationship_notes, list) else []
    state['unread_message_count'] = unread if isinstance(unread, int) and not isinstance(unread, bool) else 0
    state['pending_reply_count'] = pending if isinstance(pending, int) and not isinstance(pending, bool) else 0
    return state


def _fallback_normalize_database_row(table: str, value: Any) -> Any:
    """上游 `normalizeDatabaseRow`（`src/service.ts:8169`）的等价物。

    `plugin/core/database.py` 的 `decode_row` 已经把时间戳列转成 `datetime`、
    把 JSON 列转成对象，所以这里只补两件上游特有的工作：

    - `interlude_story.state` → `decodeStoryState`（`story_state.decode_story_state`）；
    - `interlude_participant.state` → `normalizeParticipantState`。

    另外对时间戳列做一次幂等兜底：热重载/外部写入可能留下 ISO 字符串。
    """
    if not isinstance(value, dict):
        return value
    row = dict(value)
    try:
        date_fields = timestamp_columns(table)
    except KeyError:
        date_fields = frozenset()
    for field in date_fields:
        if row.get(field) is None:
            continue
        row[field] = parse_dt(row[field]) or row[field]
    if table == 'interlude_story':
        created_at = parse_dt(row.get('createdAt')) or utc_now()
        updated_at = parse_dt(row.get('updatedAt')) or created_at
        row['createdAt'] = created_at
        row['updatedAt'] = updated_at
        row['cursorAt'] = parse_dt(row.get('cursorAt')) or updated_at
        row['state'] = decode_story_state(row.get('state'))
    elif table == 'interlude_participant':
        created_at = parse_dt(row.get('createdAt')) or utc_now()
        row['createdAt'] = created_at
        row['updatedAt'] = parse_dt(row.get('updatedAt')) or created_at
        row['state'] = _normalize_participant_state(row.get('state'))
    return row


#: 公开的小工具：优先 helpers.py 的移植版，缺失时用本文件的等价实现。
normalize_account_id = _prefer_helper('normalize_account_id', _fallback_normalize_account_id)
is_one_bot_platform = _prefer_helper('is_one_bot_platform', _fallback_is_one_bot_platform)
is_enabled_account = _prefer_helper('is_enabled_account', _fallback_is_enabled_account)
normalize_group_id = _prefer_helper('normalize_group_id', _fallback_normalize_group_id)
story_id_for_character = _prefer_helper('story_id_for_character', _fallback_story_id_for_character)
legacy_story_id_for = _prefer_helper('legacy_story_id_for', _fallback_legacy_story_id_for)
participant_id_for = _prefer_helper('participant_id_for', _fallback_participant_id_for)
participant_id_for_story = _prefer_helper('participant_id_for_story', _fallback_participant_id_for_story)
same_participant_endpoint = _prefer_helper('same_participant_endpoint', _fallback_same_participant_endpoint)
merge_setting = _prefer_helper('merge_setting', _fallback_merge_setting)
normalize_database_row = _prefer_helper('normalize_database_row', _fallback_normalize_database_row)


def log_fallback(level: str, message: str, *args: Any) -> None:
    """无实例上下文处的降级日志（`NullTransport` / 构造期错误）。

    不读配置、不看 blind mode，只保证一定落到 sink；日志本身绝不抛异常。
    """
    try:
        log_layered({
            'level': level if level in ('error', 'warn', 'info', 'debug') else 'info',
            'message': message,
            'args': list(args),
            'standalone': True,
            'kaomoji': True,
        })
    except Exception:  # pragma: no cover
        return


# =========================================================================== #
# 计时器：Koishi `ctx.setTimeout` / `ctx.setInterval` 的等价物
# =========================================================================== #

class TimerHandle:
    """Koishi 计时器返回值的等价物：**可调用**（`turn.timer()` 取消）且带 `.cancel()`。

    上游 `BufferedNarrativeTurn.timer?: () => void` 与桌面暂停路径的
    `timer.cancel()` 两种写法都要能用。
    """

    __slots__ = ('_handle', '_cancel')

    def __init__(self, handle: Any, cancel: Optional[Callable[[], None]] = None):
        self._handle = handle
        self._cancel = cancel

    def __call__(self) -> None:
        self.cancel()

    def cancel(self) -> None:
        try:
            if self._cancel is not None:
                self._cancel()
            else:
                self._handle.cancel()
        except Exception:  # pragma: no cover - 已触发的计时器取消是 no-op
            return


# =========================================================================== #
# InterludeContext：Koishi `Context` 的等价容器
# =========================================================================== #

class InterludeContext:
    """上游 `ctx` 里 service 真正用到的那部分能力。

    上游 `src/service.ts` 对 `this.ctx` 的全部用法：

    | 上游 | 本容器 |
    | --- | --- |
    | `ctx.logger('hds-interlude')` | `logger`（`plugin.core.logging` 的适配对象） |
    | `ctx.database.get/create/set/remove` | `database`（`plugin.core.database.Database`） |
    | `ctx.http.get(url, {responseType:'arraybuffer'})` | `http_get(url) -> bytes \\| None` |
    | `ctx.bots`（`find`/`some` 找账号） | `bots()` -> list[Any] |
    | `ctx.baseDir`（表情库根目录） | `base_dir` |
    | `ctx.setTimeout` / `ctx.setInterval` | `set_timeout` / `set_interval` |
    | `ctx.on('ready', ...)` | `on_ready` / `emit_ready` |

    `provider_resolver` / `clock` / `random` 是本移植版新增的显式注入点：
    narrator 工厂与时间/随机源不再依赖 Koishi 的全局状态。
    """

    def __init__(
        self,
        logger: Any = None,
        database: Any = None,
        provider_resolver: Any = None,
        clock: Optional[Callable[[], Any]] = None,
        random: Any = None,
        http_get: Optional[Callable[[str], Awaitable[Optional[bytes]]]] = None,
        bots: Optional[Callable[[], list[Any]]] = None,
        base_dir: str = '',
        http: Any = None,
    ):
        self.logger = logger
        self.database = database
        self.provider_resolver = provider_resolver
        self.clock = clock or utc_now
        self.random = random if random is not None else random_random
        self.http_get = http_get or _no_http_get
        self._bots = bots or _no_bots
        self.base_dir = base_dir
        # 上游 `ctx.http`：narrator 等模块需要 `HttpClient`（`post_json` / `iterate_sse`）。
        # 适配层可以注入；未注入时由 `ensure_http()` 惰性建一个 httpx 实现。
        self.http = http
        self._ready_handlers: list[Callable[[], Any]] = []
        self._pump_tasks: set[asyncio.Task[Any]] = set()

    # ---- HTTP 客户端 ----

    def ensure_http(self) -> Any:
        """返回 `HttpClient`（上游 `ctx.http`）；未注入时惰性创建 httpx 实现。

        惰性创建保证 `core/` 在没装 httpx 的环境里仍可导入（只在真的要用模型时才需要）。
        """
        if self.http is not None:
            return self.http
        from ..narrator import HttpxHttpClient
        self.http = HttpxHttpClient()
        return self.http

    # ---- 账号列表 ----

    def bots(self) -> list[Any]:
        """上游 `ctx.bots`（`:1736` / `:2154` / `:5390` / `:6905`）。"""
        try:
            return list(self._bots())
        except Exception:  # pragma: no cover
            return []

    # ---- 计时器 ----

    def set_timeout(self, callback: Callable[[], Any], delay_ms: float) -> TimerHandle:
        """上游 `ctx.setTimeout(fn, ms)`：返回可取消句柄。

        回调是同步的（上游同样是同步回调里 `void this.xxx()`），因此包装层负责
        把协程结果挂成后台任务，避免 coroutine 从未被 await。
        """
        loop = asyncio.get_running_loop()
        handle = loop.call_later(max(0.0, float(delay_ms) / 1000.0), lambda: self._run_callback(callback))
        return TimerHandle(handle)

    def set_interval(self, callback: Callable[[], Any], delay_ms: float) -> TimerHandle:
        """上游 `ctx.setInterval(fn, ms)`：周期执行直到 `cancel()`。"""
        loop = asyncio.get_running_loop()
        period = max(0.0, float(delay_ms) / 1000.0)
        state: dict[str, Any] = {'handle': None, 'cancelled': False}

        def _tick() -> None:
            if state['cancelled']:
                return
            self._run_callback(callback)
            if not state['cancelled']:
                state['handle'] = loop.call_later(period, _tick)

        state['handle'] = loop.call_later(period, _tick)

        def _cancel() -> None:
            state['cancelled'] = True
            handle = state.get('handle')
            if handle is not None:
                handle.cancel()

        return TimerHandle(state['handle'], _cancel)

    def _run_callback(self, callback: Callable[[], Any]) -> None:
        try:
            result = callback()
        except Exception as error:  # pragma: no cover - 后台任务异常不冒泡到事件循环
            log_fallback('warn', '后台计时器回调失败 错误=%s', error)
            return
        if asyncio.iscoroutine(result):
            task = asyncio.ensure_future(result)
            self._pump_tasks.add(task)
            task.add_done_callback(self._pump_tasks.discard)

    # ---- 生命周期事件 ----

    def on_ready(self, handler: Callable[[], Any]) -> None:
        """上游 `ctx.on('ready', handler)`（`:736`）。"""
        self._ready_handlers.append(handler)

    def emit_ready(self) -> None:
        """适配器在 AstrBot 插件就绪时调用，触发已注册的 ready 处理器。"""
        for handler in list(self._ready_handlers):
            self._run_callback(handler)


async def _no_http_get(url: str) -> Optional[bytes]:
    return None


def _no_bots() -> list[Any]:
    return []


def random_random() -> float:
    """默认随机源：`random.random()`（`docs/PORT_PLAN.md` §2）。"""
    return random.random()


def _as_mapping(value: Any) -> Optional[dict[str, Any]]:
    """把 dict / 对象统一读成 dict（配置段既可能是 dict 也可能是 dataclass）。"""
    if isinstance(value, dict):
        return value
    if value is None:
        return None
    if hasattr(value, '__dict__'):
        return {key: item for key, item in vars(value).items() if not key.startswith('_')}
    return None


def _camel_to_snake(name: str) -> str:
    return re.sub(r'(?<!^)(?=[A-Z])', '_', name).lower()


def _config_section(config: Any, name: str) -> dict[str, Any]:
    """读一个配置段：dict 先 camelCase 后 snake_case，对象走属性，空 dict 兜底。"""
    if config is None:
        return {}
    if isinstance(config, dict):
        return _as_mapping(pick(config, name, _camel_to_snake(name))) or {}
    return _as_mapping(getattr(config, name, None)) or {}


def _config_value(config: Any, camel: str, snake: Optional[str] = None, default: Any = None) -> Any:
    """读一个配置项：dict 双读，对象走属性，最后回落 default。"""
    if config is None:
        return default
    if isinstance(config, dict):
        value = pick(config, camel, snake or _camel_to_snake(camel))
        return default if value is None else value
    value = getattr(config, snake or _camel_to_snake(camel), None)
    return default if value is None else value


# =========================================================================== #
# ServiceBase
# =========================================================================== #

class ServiceBase:
    """`InterludeService` 的全部实例字段 + 共享基础设施。

    字段清单逐条对应 `upstream/src/service.ts:624-717`（类体开头到 constructor
    之前）。上游 `Map`/`Set`/数组字面量 → `dict`/`set`/`list`；
    上游 `Promise` 队列 → `asyncio.Future` 链。
    """

    # ---- 构造参数 ----

    #: 上游 `constructor(ctx: Context, public config: Config)`。
    ctx: InterludeContext
    #: 解析后的插件配置（`config.py` 的 `Config`）。
    config: Any
    #: `plugin/core/database.py` 的 `Database`。
    db: Any
    #: 平台出站通道（`transport.py` 的 `Transport`）。
    transport: Any

    # ---- 提供者（`src/service.ts:626-630`） ----

    narrator: Any
    compactor: Any
    embedder: Any
    sticker_describer: Any
    vision_describer: Any

    # ---- 表情库与历史向量（`src/service.ts:631-642`） ----

    sticker_catalog: list[dict[str, Any]]
    history_vectors: dict[str, dict[int, Any]]
    history_vectors_ready: set[str]
    history_vector_loads: dict[str, Any]
    history_backfills: set[str]
    history_backoff: dict[str, float]
    automatic_recall_cache: dict[str, Any]

    # ---- 退避与熔断（`src/service.ts:643-650`） ----

    schedule_preplan_backoff: dict[str, float]
    timeline_backoff: dict[str, dict[str, float]]
    timeline_director_failures: dict[str, int]
    compaction_backoff: dict[str, dict[str, Any]]

    # ---- 表情索引与扫描（`src/service.ts:651-652`） ----

    sticker_by_id: dict[str, dict[str, Any]]
    sticker_scan_running: bool

    # ---- 串行队列与入站缓冲（`src/service.ts:653-670`） ----

    queues: dict[str, Any]
    buffered_narrative_turns: dict[str, dict[str, Any]]
    buffered_group_turns: dict[str, dict[str, Any]]
    group_member_name_cache: dict[str, dict[str, Any]]
    group_member_name_lookups: dict[str, Any]
    group_willingness: dict[str, dict[str, Any]]
    due_intent_wake_timers: dict[str, Any]
    interrupted_typing_participants: set[str]
    narrating_stories: set[str]

    # ---- 后台整理（`src/service.ts:671-675`） ----

    fact_backfills: set[str]
    scheduled_compactions: set[str]
    scheduled_alter_analyses: set[str]

    # ---- 全局写队列与浏览器闸门（`src/service.ts:676-681`） ----

    database_write_queue: Any
    browser_active: int
    browser_waiters: list[Callable[[], None]]

    # ---- 运行标志（`src/service.ts:682-688`） ----

    service_logger: Any
    background_started: bool
    database_resetting: bool
    sweep_running: bool
    compaction_sweep_running: bool
    blind_mode_health_issue: bool

    # ---- 配置缓存与模型路由（`src/service.ts:689-701`） ----

    cached_audio_config: Any
    cached_sticker_config: Any
    cached_alter_system_config: Any
    cached_agency_config: Any
    cached_schedule_preplan_config: Any
    cached_blind_mode_config: Any
    cached_auto_advance_config: Any
    cached_shared_story_config: Any
    cached_memory_config: Any
    cached_browser_config: Any
    model_routing: Any

    # ---- 迁移诊断与桌面桥（`src/service.ts:702-715`） ----

    reported_state_migrations: set[str]
    desktop_runtime_phase: str
    desktop_event_sink: Optional[Callable[[str, Any], None]]
    desktop_delivery_handler: Optional[Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]]

    def __init__(
        self,
        ctx: InterludeContext,
        config: Any,
        db: Any = None,
        transport: Any = None,
    ):
        """装配字段 + 队列/日志基础设施 + 模型路由（上游 constructor 的前半段）。

        上游把 `registerTables(ctx)` / `resolveModelRouting(...)` / 五个 provider
        工厂放在 constructor 里；本移植版保留同样的顺序与默认值，但把 `narrator.py`
        的工厂换成**惰性、可失败**的调用：`narrator.py` 由并行任务产出，缺失时服务
        仍可构造（等价上游"模型未配置"的降级态）。
        """
        self.ctx = ctx
        self.config = config
        self.db = db if db is not None else getattr(ctx, 'database', None)
        self.transport = transport

        # ---- 提供者 ----
        self.narrator = None
        self.compactor = None
        self.embedder = None
        self.sticker_describer = None
        self.vision_describer = None

        # ---- 表情库与历史向量 ----
        self.sticker_catalog = []
        self.history_vectors = {}
        self.history_vectors_ready = set()
        self.history_vector_loads = {}
        self.history_backfills = set()
        self.history_backoff = {}
        self.automatic_recall_cache = {}

        # ---- 退避与熔断 ----
        self.schedule_preplan_backoff = {}
        self.timeline_backoff = {}
        self.timeline_director_failures = {}
        self.compaction_backoff = {}

        # ---- 表情索引与扫描 ----
        self.sticker_by_id = {}
        self.sticker_scan_running = False

        # ---- 串行队列与入站缓冲 ----
        self.queues = {}
        self.buffered_narrative_turns = {}
        self.buffered_group_turns = {}
        self.group_member_name_cache = {}
        self.group_member_name_lookups = {}
        self.group_willingness = {}
        self.due_intent_wake_timers = {}
        self.interrupted_typing_participants = set()
        self.narrating_stories = set()
        #: 可见的"为什么没动静"日志节流表（key → 上次打印的毫秒时间戳）。
        #: 见 `note_access_skip`：群聊的拒绝/跳过至少要让人看见一次，但绝不能刷屏。
        self.access_notes = {}

        # ---- 后台整理 ----
        self.fact_backfills = set()
        self.scheduled_compactions = set()
        self.scheduled_alter_analyses = set()

        # ---- 全局写队列与浏览器闸门 ----
        # 上游：`databaseWriteQueue: Promise<unknown> = Promise.resolve()`。
        self.database_write_queue = None
        self._db_write_lock = asyncio.Lock()
        self.browser_active = 0
        self.browser_waiters = []

        # ---- 运行标志 ----
        self.service_logger = getattr(ctx, 'logger', None)
        self.background_started = False
        self.database_resetting = False
        self.sweep_running = False
        self.compaction_sweep_running = False
        self.blind_mode_health_issue = False

        # ---- 配置缓存与模型路由 ----
        self.cached_audio_config = None
        self.cached_sticker_config = None
        self.cached_alter_system_config = None
        self.cached_agency_config = None
        self.cached_schedule_preplan_config = None
        self.cached_blind_mode_config = None
        self.cached_auto_advance_config = None
        self.cached_shared_story_config = None
        self.cached_memory_config = None
        self.cached_browser_config = None
        # 上游 `resolveModelRouting(config.model, config.alterSystem)`。
        self.model_routing = self._resolve_model_routing()

        # ---- 迁移诊断与桌面桥 ----
        self.reported_state_migrations = set()
        self.desktop_runtime_phase = 'running'
        self.desktop_event_sink = None
        self.desktop_delivery_handler = None

        # ---- 计时器句柄（上游只靠 `ctx` 持有，这里留一个可取消的引用） ----
        self._sweep_timer: Optional[TimerHandle] = None
        self._compaction_timer: Optional[TimerHandle] = None
        self._blind_mode_timer: Optional[TimerHandle] = None
        self._sticker_scan_timer: Optional[TimerHandle] = None

    # ------------------------------------------------------------------ #
    # 构造期基础设施
    # ------------------------------------------------------------------ #

    def _resolve_model_routing(self) -> Any:
        """上游 `resolveModelRouting(config.model, config.alterSystem)`。"""
        try:
            from ..model_routing import resolve_model_routing
        except ImportError:  # pragma: no cover
            return {}
        model_config = _config_section(self.config, 'model')
        alter_config = _config_section(self.config, 'alterSystem')
        try:
            return resolve_model_routing(model_config, alter_config)
        except Exception:  # pragma: no cover - 配置不完整时按"未路由"处理
            return {}

    def register_tables(self) -> None:
        """上游 `registerTables(ctx)`（`src/service.ts:720`）。

        `plugin.core.database.Database.register_tables()` 同步且幂等。
        """
        database = self.db
        register = getattr(database, 'register_tables', None)
        if not callable(register):
            return
        try:
            register()
        except Exception as error:  # pragma: no cover
            log_fallback('warn', '数据表注册失败 错误=%s', error)

    def _create_providers(self) -> None:
        """上游 `createNarrator` / `createCompactor` / `createEmbedder` /
        `createStickerDescriber` / `createVisionDescriber`（`src/service.ts:725-729`）。

        `narrator.py` 缺失时保持 `None`（上游"主叙事模型未配置"的等价降级），
        服务仍可构造与测试。
        """
        try:
            from .. import narrator as narrator_module
        except ImportError:  # pragma: no cover - 取决于同批任务落地顺序
            return
        model_config = _config_section(self.config, 'model')
        blind_enabled = bool(_config_section(self.config, 'blindMode').get('enabled'))

        def on_usage(record: Any) -> None:
            reporter = getattr(self, 'report_token_usage', None)
            if callable(reporter):
                reporter(record)
            else:
                self._log_compat('debug', 'Token 用量 %s', record)

        # (工厂名, 字段名, 是否 VisionDescriber 家族)
        factories = (
            ('create_narrator', 'narrator'),
            ('create_compactor', 'compactor'),
            ('create_embedder', 'embedder'),
            ('create_sticker_describer', 'sticker_describer'),
            ('create_vision_describer', 'vision_describer'),
        )
        for factory_name, attribute in factories:
            factory = getattr(narrator_module, factory_name, None)
            if not callable(factory):
                continue
            try:
                http = self.ctx.ensure_http()
                if factory_name == 'create_embedder':
                    provider = factory(http, model_config, self.model_routing)
                else:
                    provider = factory(
                        http, model_config, blind_enabled, on_usage, self.model_routing,
                    )
            except Exception as error:  # pragma: no cover
                log_fallback('warn', '模型提供者创建失败 工厂=%s 错误=%s', factory_name, error)
                continue
            setattr(self, attribute, provider)

    # ------------------------------------------------------------------ #
    # 配置段访问（`cachedXxxConfig` 的惰性解析）
    # ------------------------------------------------------------------ #

    def _config_cache(
        self, attribute: str, resolver_name: str, section: str, parent: Optional[str] = None,
    ) -> dict[str, Any]:
        """读缓存的配置段；首次访问时调用配置层 `resolve*` 并缓存。

        上游用 getter 做同样的事（`cachedAudioConfig?: AudioConfig` 等），注释里
        写明"Console 重载会创建新的 service 实例，所以实例级缓存是安全的"。

        `parent` 用于上游把分组套在另一段下的配置（`config.model.audio`）：
        `parent='model', section='audio'` 读 `config['model']['audio']`。

        归一化函数按「先 `config.py`、后 `helpers.py`」的顺序查找
        （`resolve_blind_mode_config` 在 config.py，`resolve_audio_config` /
        `resolve_sticker_config` 在 helpers.py）；都缺失时原样返回该段。
        """
        cached = getattr(self, attribute, None)
        if cached is not None:
            return cached
        if parent is None:
            raw = _config_section(self.config, section)
        else:
            raw = _config_section(_config_section(self.config, parent), section)
        resolved: Any = raw
        try:
            from . import config as config_module
            resolvers = [config_module]
            if _helpers_module is not None:
                resolvers.append(_helpers_module)
            for module in resolvers:
                resolver = getattr(module, resolver_name, None)
                if callable(resolver):
                    resolved = resolver(raw)
                    break
        except ImportError:  # pragma: no cover - config.py 尚未落地
            resolved = raw
        if not isinstance(resolved, dict):
            resolved = _as_mapping(resolved) or raw
        setattr(self, attribute, resolved)
        return resolved

    @property
    def audio_config(self) -> dict[str, Any]:
        """上游 `get audioConfig()`（`src/service.ts:2247`）：`config.model.audio`。

        段位必须是 `model` 下的 `audio`（Console「模型中心 → 原生音频理解」）：
        读顶层 `config['audio']` 会永远拿到空段，原生音频静默失效。
        """
        return self._config_cache(
            'cached_audio_config', 'resolve_audio_config', 'audio', parent='model',
        )

    @property
    def sticker_config(self) -> dict[str, Any]:
        """上游 `get stickerConfig()`（`src/service.ts:2260`）：**顶层** `config.stickers`。

        上游读的是 `this.config.stickers`（`upstream/src/index.ts:425` 的 `Stickers`
        schema 挂在配置根上），不是 `model.stickers`。
        """
        return self._config_cache('cached_sticker_config', 'resolve_sticker_config', 'stickers')

    @property
    def alter_system_config(self) -> dict[str, Any]:
        return self._config_cache('cached_alter_system_config', 'resolve_alter_system_config', 'alterSystem')

    @property
    def agency_config(self) -> dict[str, Any]:
        return self._config_cache('cached_agency_config', 'resolve_agency_config', 'agency')

    @property
    def schedule_preplan_config(self) -> dict[str, Any]:
        return self._config_cache('cached_schedule_preplan_config', 'resolve_schedule_preplan_config', 'schedulePreplan')

    @property
    def blind_mode_config(self) -> dict[str, Any]:
        """上游 `blindModeConfig`：`resolveBlindModeConfig(config.blindMode ?? config.blackBox)`。"""
        cached = self.cached_blind_mode_config
        if cached is not None:
            return cached
        raw = _config_section(self.config, 'blindMode') or _config_section(self.config, 'blackBox')
        resolved: Any = raw
        try:
            from . import config as config_module
            resolver = getattr(config_module, 'resolve_blind_mode_config', None)
            if not callable(resolver):
                resolver = getattr(config_module, 'resolve_black_box_config', None)
            if callable(resolver):
                resolved = resolver(raw)
        except ImportError:  # pragma: no cover
            resolved = raw
        if not isinstance(resolved, dict):
            resolved = _as_mapping(resolved) or raw
        self.cached_blind_mode_config = resolved
        return resolved

    @property
    def auto_advance_config(self) -> dict[str, Any]:
        return self._config_cache('cached_auto_advance_config', 'resolve_auto_advance_config', 'runtime')

    @property
    def shared_story_config(self) -> dict[str, Any]:
        return self._config_cache('cached_shared_story_config', 'resolve_shared_story_config', 'sharedStory')

    @property
    def memory_config(self) -> dict[str, Any]:
        return self._config_cache('cached_memory_config', 'resolve_memory_config', 'memory')

    @property
    def browser_config(self) -> dict[str, Any]:
        return self._config_cache('cached_browser_config', 'resolve_browser_config', 'browser')

    @property
    def runtime_config(self) -> dict[str, Any]:
        """上游 `this.config.runtime`（大量 `?? 默认值` 读取的源头）。"""
        return _config_section(self.config, 'runtime')

    @property
    def story_defaults(self) -> dict[str, Any]:
        """上游 `this.config.storyDefaults`。"""
        return _config_section(self.config, 'storyDefaults')

    # ------------------------------------------------------------------ #
    # 时间与随机
    # ------------------------------------------------------------------ #

    def now(self) -> Any:
        """当前时刻（timezone-aware UTC）。上游 `new Date()`。"""
        clock = getattr(self.ctx, 'clock', None)
        if callable(clock):
            parsed = parse_dt(clock())
            if parsed is not None:
                return parsed
        return utc_now()

    def now_ms(self) -> int:
        """上游 `Date.now()`。"""
        return dt_ms(self.now())

    def rng(self) -> float:
        """随机源（默认 `random.random()`）。上游 `Math.random()`。"""
        source = getattr(self.ctx, 'random', None)
        if callable(source):
            return float(source())
        method = getattr(source, 'random', None)
        if callable(method):
            return float(method())
        return random.random()

    def rng_int(self, upper: int) -> int:
        """上游 `Math.floor(Math.random() * n)`。"""
        return int(self.rng() * max(0, int(upper)))

    # ------------------------------------------------------------------ #
    # 日志
    # ------------------------------------------------------------------ #

    def _log_compat(self, level: str, message: str, *args: Any) -> None:
        """降级日志：不读配置、不看 blind mode，只保证一定落到 sink。"""
        log_fallback(level, message, *args)

    def allows_verbosity(self, required: str) -> bool:
        """上游 `allowsVerbosity`（`src/service.ts:6827`）逐字移植。"""
        rank = {'summary': 1, 'standard': 2, 'diagnostic': 3}
        logging_config = _config_section(self.config, 'logging')
        configured = logging_config.get('verbosity') or 'standard'
        return rank.get(configured, 2) >= rank.get(required, 2)

    def emit_log(self, level: str, output: str) -> None:
        """上游 `emitLog`（`src/service.ts:6811`）：走 Koishi logger 的等价物。

        `output` 是**已经渲染好**的文本（`write_report` / `write_standalone` 的产物），
        所以这里只负责投递、**绝不能再渲染一次**：早先没有宿主 logger 时走的是
        `log_fallback(level, output)`，而它会把这段文本当成"原始消息"再套一层分层渲染，
        于是每条 report / standalone 日志被投递两次、第二份还是嵌套的（见坑 49）。
        """
        logger = self.service_logger
        if logger is None:
            # 没有宿主 logger：直接交给 sink（默认写 stderr），只投一次。
            try:
                interlude_logging.get_log_sink()(level, output)
            except Exception:  # pragma: no cover - 日志通道绝不抛异常
                return
            return
        try:
            if level == 'error':
                logger.error(output)
            elif level == 'warn':
                logger.warning(output)
            elif level == 'info':
                logger.info(output)
            else:
                logger.debug(output)
        except Exception:  # pragma: no cover
            log_fallback(level, output)

    def write_standalone(self, level: str, message: str, args: Any) -> None:
        """上游 `writeStandalone`（`src/service.ts:6778`）逐条移植。"""
        if self.blind_mode_config.get('enabled'):
            if level in ('error', 'warn'):
                self.blind_mode_health_issue = True
            return
        rank = {'silent': 0, 'error': 1, 'warn': 2, 'info': 3, 'debug': 4}
        logging_config = _config_section(self.config, 'logging')
        if rank.get(logging_config.get('level') or 'info', 3) < rank.get(level, 3):
            return
        if (logging_config.get('format') or 'layered') == 'layered':
            # 用**纯渲染**（`format_layered_log`）：投递统一在下面的 `emit_log` 里做一次。
            # 早先用 `log_layered` 会先投一次 sink、再被 `emit_log` 投一次（见坑 49）。
            output = format_layered_log({
                'level': level,
                'protagonist': 'HDSI',
                'message': message,
                'args': list(args or []),
                'standalone': True,
                'colors': logging_config.get('colors') is not False,
                'color_theme': pick(logging_config, 'colorTheme', 'color_theme') or 'dark',
                'kaomoji': logging_config.get('kaomoji') is not False,
            })
        else:
            output = '[系统] %s' % interlude_logging.render_log_message(message, args)
        self.emit_log(level, output)

    def write_report(self, level: str, story: Any, phase: str, message: str, args: Any) -> None:
        """上游 `writeReport`（`src/service.ts:6736`）逐条移植。"""
        if self.blind_mode_config.get('enabled'):
            if level in ('error', 'warn'):
                self.blind_mode_health_issue = True
            return
        rank = {'silent': 0, 'error': 1, 'warn': 2, 'info': 3, 'debug': 4}
        logging_config = _config_section(self.config, 'logging')
        if rank.get(logging_config.get('level') or 'info', 3) < rank.get(level, 3):
            return
        rendered = interlude_logging.render_log_message(message, args)
        story_detail = (
            ' 故事=%s' % pick(story, 'id')
            if (logging_config.get('verbosity') or 'standard') == 'diagnostic' else ''
        )
        character = pick(pick(story, 'setting'), 'character')
        protagonist = str(pick(character, 'name') or '') if character else ''
        logging_format = logging_config.get('format') or 'layered'
        if logging_format == 'layered':
            # 同上：纯渲染 + `emit_log` 单次投递。
            output = format_layered_log({
                'level': level,
                'phase': phase,
                'protagonist': protagonist,
                'message': message,
                'args': list(args or []),
                'colors': logging_config.get('colors') is not False,
                'color_theme': pick(logging_config, 'colorTheme', 'color_theme') or 'dark',
                'kaomoji': logging_config.get('kaomoji') is not False,
            })
        elif logging_format == 'compact':
            output = '[%s] %s %s%s' % (phase_label(phase), protagonist, rendered, story_detail)
        else:
            output = '[%s] %s\n事件：%s%s' % (phase_label(phase), protagonist, rendered, story_detail)
        self.emit_log(level, output)

    def report_standalone(self, level: str, message: str, *args: Any) -> None:
        """上游 `reportStandalone(level, message, ...args)`（`src/service.ts:6757`）。"""
        self.write_standalone(level, message, args)

    def report_standalone_operation(self, verbosity: str, level: str, message: str, *args: Any) -> None:
        """上游 `reportStandaloneOperation(verbosity, level, message, ...args)`（`:6770`）。"""
        if not self.allows_verbosity(verbosity):
            return
        self.write_standalone(level, message, args)

    def report_operation(
        self,
        verbosity: str,
        level: str,
        story: Any,
        phase: str,
        message: str,
        *args: Any,
    ) -> None:
        """上游 `reportOperation(verbosity, level, story, phase, message, ...args)`（`:6730`）。"""
        if not self.allows_verbosity(verbosity):
            return
        self.write_report(level, story, phase, message, args)

    def log(self, level: str, phase: str, message: str, *args: Any) -> None:
        """`self.log(level, phase, message, *args)`：分层日志的通用入口。

        等价上游 `reportOperation(...)`，但不需要调用方先拿到 story；
        `phase` 只影响渲染分类。
        """
        if not self.allows_verbosity('diagnostic' if phase == 'diagnostic' else 'standard'):
            return
        logging_config = _config_section(self.config, 'logging')
        if (logging_config.get('format') or 'layered') == 'layered':
            output = log_layered({
                'level': level,
                'phase': phase,
                'message': message,
                'args': list(args or []),
                'colors': logging_config.get('colors') is not False,
                'color_theme': pick(logging_config, 'colorTheme', 'color_theme') or 'dark',
                'kaomoji': logging_config.get('kaomoji') is not False,
            })
        else:
            output = interlude_logging.render_log_message(message, args)
        self.emit_log(level, output)

    def is_blind_mode_enabled(self) -> bool:
        """`blindModeConfig.enabled` 的便捷读法。"""
        return bool(self.blind_mode_config.get('enabled'))

    # ------------------------------------------------------------------ #
    # 数据库：全局写队列 + 读重试
    # ------------------------------------------------------------------ #

    async def write(self, fn: Callable[[Any], Any]) -> Any:
        """把任意写操作串行化（等价上游 `databaseWriteQueue`）。

        上游 `dbWrite` 用 `databaseWriteQueue.then(...)` 串成一条链；本移植版用
        `asyncio.Lock` + 每次调用一条独立任务实现同样的"全局串行"语义。

        `fn(self.db)` 可以返回协程或普通值（同步写库调用很常见，例如
        `lambda db: db.insert(...)`），两种都会被正确 await；返回值原样返回，
        异常照常抛出且不阻塞后续写。
        """
        async with self._db_write_lock:
            result = fn(self.db)
            if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
                return await result
            return result

    async def db_write(self, task: Callable[[], Awaitable[Any]]) -> Any:
        """上游 `dbWrite(task)`：在写队列里执行一个无参异步任务。"""
        async def runner(_database: Any) -> Any:
            return await task()

        return await self.write(runner)

    def is_transient_database_error(self, error: Any) -> bool:
        """上游 `isTransientDatabaseError`（`src/service.ts:7700`）。"""
        return bool(re.search(
            r'disk\s*i/o|database is locked|busy|unable to open', str(error), re.IGNORECASE,
        ))

    async def db_read(self, task: Callable[[], Awaitable[Any]]) -> Any:
        """上游 `dbRead`（`src/service.ts:6874`）：瞬时错误做小步重试。"""
        delays = (50, 125, 250)
        attempt = 0
        while True:
            try:
                return await task()
            except Exception as error:
                transient = self.is_transient_database_error(error)
                if attempt >= len(delays) or not transient:
                    if transient:
                        self.report_standalone('warn', 'SQLite 读取连续失败，已停止重试 错误=%s', error)
                    raise
                delay = delays[attempt] + int(self.rng() * 25)
                self.report_standalone_operation(
                    'diagnostic', 'debug',
                    'SQLite 读取暂时失败，准备重试 等待=%dms 次数=%d 错误=%s',
                    delay, attempt + 1, error,
                )
                await asyncio.sleep(delay / 1000.0)
                attempt += 1

    async def db_get(self, table: str, query: Any, options: Any = None) -> list[Any]:
        """上游 `dbGet(table, query, options)`（`src/service.ts:6893`）。

        `options` 支持上游用到的形状：`{'limit': n, 'sort': {'occurredAt': 'DESC'}}`。

        **注意**：`plugin/core/database.py` 的 `Database.all()` 只支持等值 `where`
        与 `order`/`limit`。上游的范围查询（`{'occurredAt': {'$gte': ...}}`、
        `{'id': {'$lt': ...}}`）在本移植版必须由调用方改写为"先取足够行，再在
        Python 侧过滤"；本方法对算子**显式报错**，不静默给出偏窄的结果集。
        """
        async def task() -> list[Any]:
            where = self._normalize_query(query if isinstance(query, dict) else {})
            limit = None
            order = None
            if isinstance(options, dict):
                raw_limit = pick(options, 'limit')
                if isinstance(raw_limit, int) and not isinstance(raw_limit, bool):
                    limit = raw_limit
                order = self._normalize_order(pick(options, 'sort'))
            rows = await asyncio.to_thread(self.db.all, table, where, order, limit)
            return [normalize_database_row(table, row) for row in rows]

        return await self.db_read(task)

    @staticmethod
    def _normalize_query(where: dict[str, Any]) -> dict[str, Any]:
        """校验查询条件：不接受上游的 `$gte`/`$lte`/`$lt`/`$in` 算子。"""
        for key, value in where.items():
            if isinstance(value, dict):
                operators = [name for name in value if str(name).startswith('$')]
                if operators:
                    raise NotImplementedError(
                        'db_get 不支持查询算子 %s（列=%s）；请在 Python 侧过滤' % (operators, key),
                    )
        return dict(where)

    @staticmethod
    def _normalize_order(sort: Any) -> Optional[list[str]]:
        """上游 `sort: { updatedAt: 'desc' }` → `['updatedAt DESC']`。"""
        if isinstance(sort, str):
            return [sort]
        if not isinstance(sort, dict):
            return None
        parts: list[str] = []
        for column, direction in sort.items():
            text = str(direction).strip().upper()
            parts.append('%s %s' % (column, text if text in ('ASC', 'DESC') else 'ASC'))
        return parts or None

    async def db_create(self, table: str, data: Any) -> Any:
        """上游 `dbCreate(table, data)`（`src/service.ts:6939`）。"""
        async def task() -> Any:
            try:
                return await asyncio.to_thread(self.db.insert, table, dict(data))
            except Exception as error:
                if not self.is_transient_database_error(error):
                    raise
                existing = await self.find_possibly_committed_create(table, data)
                if existing is not None:
                    return existing
                raise

        return await self.db_write(task)

    async def find_possibly_committed_create(self, table: str, data: Any) -> Any:
        """上游 `findPossiblyCommittedCreate`（`src/service.ts:6956`）。"""
        if not isinstance(data, dict):
            return None
        story_id = data.get('storyId')
        if not isinstance(story_id, str) or not story_id:
            return None
        rows = await self.db_get(table, {'storyId': story_id}, {'limit': 100})

        def same_timestamp(left: Any, right: Any) -> bool:
            a = parse_dt(left)
            b = parse_dt(right)
            return a is not None and b is not None and abs(dt_ms(a) - dt_ms(b)) < 2000

        for row in rows:
            if table == 'interlude_intent':
                if (
                    row.get('participantId') == data.get('participantId')
                    and row.get('type') == data.get('type')
                    and row.get('summary') == data.get('summary')
                    and same_timestamp(row.get('notBefore'), data.get('notBefore'))
                    and json.dumps(row.get('payload') or {}, sort_keys=True, ensure_ascii=False)
                    == json.dumps(data.get('payload') or {}, sort_keys=True, ensure_ascii=False)
                ):
                    return row
            elif table == 'interlude_script_entry':
                if (
                    row.get('participantId') == data.get('participantId')
                    and row.get('kind') == data.get('kind')
                    and row.get('actor') == data.get('actor')
                    and row.get('content') == data.get('content')
                    and same_timestamp(row.get('occurredAt'), data.get('occurredAt'))
                ):
                    return row
            elif table == 'interlude_memory':
                if (
                    row.get('participantId') == data.get('participantId')
                    and row.get('category') == data.get('category')
                    and row.get('content') == data.get('content')
                    and same_timestamp(row.get('createdAt'), data.get('createdAt'))
                ):
                    return row
            elif isinstance(data.get('id'), str) and row.get('id') == data.get('id'):
                return row
        return None

    async def db_set(self, table: str, query: Any, data: Any) -> Any:
        """上游 `dbSet(table, query, data)`（`src/service.ts:6986`）。"""
        async def task() -> Any:
            return await asyncio.to_thread(self.db.update, table, dict(query), dict(data))

        return await self.db_write(task)

    async def db_remove(self, table: str, query: Any) -> Any:
        """上游 `dbRemove(table, query)`（`src/service.ts:6990`）。"""
        async def task() -> Any:
            return await asyncio.to_thread(self.db.remove, table, dict(query))

        return await self.db_write(task)

    async def purge_table(self, table: str, query: Any, fallback: Any) -> None:
        """上游 `purgeTable(table, query, fallback)`（`src/service.ts:6999`）。"""
        try:
            await self.db_remove(table, query)
        except Exception as error:
            self.report_standalone('warn', 'SQLite 物理删除失败，改用逻辑删除 表=%s 错误=%s', table, error)
            await self.db_set(table, query, fallback)

    # ------------------------------------------------------------------ #
    # 串行队列（同一 story 串行）
    # ------------------------------------------------------------------ #

    def queue_for(self, key: str) -> Any:
        """取某个串行键当前的队列尾（等价 `this.queues.get(key)`）。"""
        return self.queues.get(key)

    def run_in_queue(self, key: str, task: Callable[[], Awaitable[Any]]) -> Awaitable[Any]:
        """等价上游 `serial(id, task)`（`src/service.ts:6851`）。

        - 前一次失败不会永久堵住同一个键（异常照常传给本次调用方）；
        - 队尾在任务结束后被清理（`finally` 语义）。

        返回一个 `Future`：调用方照常 `await`，异常在 await 时抛出。
        """
        loop = asyncio.get_event_loop()
        previous = self.queues.get(key)
        future: asyncio.Future[Any] = loop.create_future()

        async def runner() -> None:
            if previous is not None:
                try:
                    await previous
                except Exception:
                    pass
            try:
                result = await task()
            except BaseException as error:
                if not future.done():
                    future.set_exception(error)
            else:
                if not future.done():
                    future.set_result(result)
            finally:
                if self.queues.get(key) is future:
                    self.queues.pop(key, None)

        asyncio.ensure_future(runner())
        self.queues[key] = future
        return future

    #: 上游方法名（`src/service.ts:6851`）：与 `run_in_queue` 同一实现。
    serial = run_in_queue


# =========================================================================== #
# ServiceChunk0：src/service.ts:717-1247
# =========================================================================== #

class ServiceChunk0(ServiceBase):
    """Chunk0 mixin：生命周期 / 桌面桥 / 权限 / 故事与参与者 / 管理读写。

    逐条对应 `src/service.ts:717-1247`。跨 mixin 调用一律 `self.其他方法()`
    （MRO 自动解析），不跨文件 import 其它 chunk。
    """

    # ------------------------------------------------------------------ #
    # constructor（`src/service.ts:717-739`）
    # ------------------------------------------------------------------ #

    def __init__(self, ctx: InterludeContext, config: Any, db: Any = None, transport: Any = None):
        super().__init__(ctx, config, db, transport)
        self.register_tables()
        self._create_providers()
        # 上游用 `ctx.setTimeout(() => this.startBackgroundTasks(), 0)` 把定时器注册
        # 推迟一个事件循环，避免 Console 加载/重载卡顿。等价：call_soon。
        try:
            asyncio.get_running_loop().call_soon(self.start_background_tasks)
        except RuntimeError:
            # 构造发生在事件循环之外（同步测试/CLI）时，由调用方显式调用
            # `start_background_tasks()`。
            self._log_compat('debug', '事件循环未运行：后台调度推迟到显式启动')
        # 上游 `ctx.on('ready', () => reportStandaloneOperation('summary','info','服务已就绪'))`。
        if hasattr(ctx, 'on_ready'):
            ctx.on_ready(lambda: self.report_standalone_operation('summary', 'info', '服务已就绪'))
        self.report_standalone_operation(
            'summary', 'info',
            '服务初始化完成 主叙事路由=%s 共享主剧本=%s 自动推进=%s',
            '已配置' if self._main_route_available() else '未配置',
            bool(self.shared_story_config.get('enabled')),
            bool(self.auto_advance_config.get('enabled')),
        )
        self.report_standalone_operation(
            'diagnostic', 'debug', '模型任务路由 %s', self._format_model_routing(),
        )

    def _main_route_available(self) -> bool:
        """上游 `this.modelRouting.main.available`。"""
        return bool(pick(pick(self.model_routing, 'main'), 'available'))

    def _format_model_routing(self) -> str:
        """上游 `formatModelRouting(this.modelRouting)`。"""
        try:
            from ..model_routing import format_model_routing
        except ImportError:  # pragma: no cover
            return ''
        try:
            return format_model_routing(self.model_routing)
        except Exception:  # pragma: no cover
            return ''

    # ------------------------------------------------------------------ #
    # 后台调度（`src/service.ts:741-756`）
    # ------------------------------------------------------------------ #

    def start_background_tasks(self) -> None:
        """上游 `startBackgroundTasks`（`src/service.ts:741`）逐条移植。"""
        if self.background_started:
            return
        self.background_started = True
        # 生活推进与记忆压缩都按故事串行。
        sweep_interval = max(1, int(_config_value(
            self.runtime_config, 'sweepIntervalMinutes', 'sweep_interval_minutes', 1,
        )))
        self._sweep_timer = self.ctx.set_interval(
            lambda: self._spawn(self._sweep_guarded()), sweep_interval * 60_000,
        )
        if self.memory_config.get('enabled') or self.schedule_preplan_config.get('enabled'):
            memory_interval = int(
                pick(self.memory_config, 'backgroundIntervalMinutes', 'background_interval_minutes') or 1
            )
            self._compaction_timer = self.ctx.set_interval(
                lambda: self._spawn(self._compact_stories_guarded()), max(1, memory_interval) * 60_000,
            )
        if self.blind_mode_config.get('enabled'):
            health_minutes = int(
                pick(self.blind_mode_config, 'healthReportMinutes', 'health_report_minutes') or 1
            )
            self._blind_mode_timer = self.ctx.set_interval(
                lambda: self.report_blind_mode_health(), max(1, health_minutes) * 60_000,
            )
        if self.sticker_config.get('enabled'):
            self.ctx.set_timeout(lambda: self._spawn(self.scan_sticker_library()), 0)
            self._sticker_scan_timer = self.ctx.set_interval(
                lambda: self._spawn(self.scan_sticker_library()), 5 * 60_000,
            )
        self.report_standalone_operation(
            'standard', 'info', '后台调度已启动 剧本扫描=%d分钟 记忆扫描=%d分钟',
            sweep_interval,
            pick(self.memory_config, 'backgroundIntervalMinutes', 'background_interval_minutes') or 0,
        )

    @staticmethod
    def _spawn(awaitable: Any) -> None:
        """把后台协程挂成任务（等价上游 `void this.xxx().catch(...)`）。"""
        if asyncio.iscoroutine(awaitable):
            asyncio.ensure_future(awaitable)

    async def _sweep_guarded(self) -> None:
        """上游 `void this.sweep().catch(e => reportStandalone('warn', '后台推进失败 错误=%s', e))`。"""
        try:
            await self.sweep()
        except Exception as error:
            self.report_standalone('warn', '后台推进失败 错误=%s', error)

    async def _compact_stories_guarded(self) -> None:
        """上游 `void this.compactStories().catch(...)` 的等价物。"""
        try:
            await self.compact_stories()
        except Exception as error:
            self.report_standalone('warn', '后台整理失败 错误=%s', error)

    # ------------------------------------------------------------------ #
    # 提供者注入（`src/service.ts:758-762`）
    # ------------------------------------------------------------------ #

    def set_narrator(self, provider: Any) -> None:
        """上游 `setNarrator(provider)`。"""
        self.narrator = provider

    def get_narrator(self) -> Any:
        """上游 `getNarrator()`。"""
        return self.narrator

    def set_compactor(self, provider: Any) -> None:
        """上游 `setCompactor(provider)`。"""
        self.compactor = provider

    def set_embedder(self, provider: Any) -> None:
        """上游 `setEmbedder(provider)`：允许自定义/本地向量服务而不换主叙事。"""
        self.embedder = provider

    # ------------------------------------------------------------------ #
    # 桌面桥（`src/service.ts:764-925`）
    # ------------------------------------------------------------------ #

    def set_desktop_event_sink(self, sink: Optional[Callable[[str, Any], None]] = None) -> None:
        """上游 `setDesktopEventSink(sink?)`。普通 Koishi 使用下永不安装。"""
        self.desktop_event_sink = sink

    def set_desktop_delivery_handler(
        self,
        handler: Optional[Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]] = None,
    ) -> None:
        """上游 `setDesktopDeliveryHandler(handler?)`。

        注册后台投递通道（typ-0 worker 专用）；卸载时传 `None` 复原。
        """
        self.desktop_delivery_handler = handler

    def get_desktop_runtime_phase(self) -> str:
        """上游 `getDesktopRuntimePhase()`。"""
        return self.desktop_runtime_phase

    async def set_desktop_runtime_phase(self, phase: str) -> None:
        """上游 `setDesktopRuntimePhase(phase)`（`src/service.ts:769`）逐条移植。"""
        self.desktop_runtime_phase = phase
        if phase == 'paused':
            for timer in list(self.due_intent_wake_timers.values()):
                cancel = getattr(timer, 'cancel', None)
                if callable(cancel):
                    cancel()
            self.due_intent_wake_timers.clear()
            for turn in self.buffered_narrative_turns.values():
                timer = turn.get('timer')
                if timer:
                    timer()
                turn['timer'] = None
            for turn in self.buffered_group_turns.values():
                timer = turn.get('timer')
                if timer:
                    timer()
                turn['timer'] = None
        elif phase == 'running':
            # 只恢复暂停前已经持久化的回合：既不丢消息，也不制造新的用户事件。
            # 缓冲回合 dict 里同一个字段**两种拼写都有**（`helpers._turn_set` 两种都写），
            # 这里双读，避免"写 camel / 读 snake"式的静默失配（2026-09-26 的合并 bug）。
            for key, turn in list(self.buffered_narrative_turns.items()):
                if (turn.get('timer') or pick(turn, 'inFlightRequestId', 'in_flight_request_id')
                        or not turn.get('messages')):
                    continue
                revision = (turn.get('next_revision') or 0) + 1
                turn['next_revision'] = revision
                turn['timer'] = self.ctx.set_timeout(
                    lambda key=key, revision=revision: self._spawn(
                        self.flush_buffered_narrative(key, revision),
                    ),
                    0,
                )
            for key, turn in list(self.buffered_group_turns.items()):
                if turn.get('timer') or not turn.get('messages'):
                    continue
                revision = (turn.get('revision') or 0) + 1
                turn['revision'] = revision
                turn['timer'] = self.ctx.set_timeout(
                    lambda key=key, revision=revision: self._spawn(self.flush_group_turn(key, revision)),
                    0,
                )
        if self.desktop_event_sink is not None:
            self.desktop_event_sink('runtime-snapshot', await self.desktop_runtime_snapshot())

    async def desktop_runtime_snapshot(self) -> dict[str, Any]:
        """上游 `desktopRuntimeSnapshot()`（`src/service.ts:800`）。"""
        stories = await self.db_get(
            'interlude_story', {}, {'limit': 20, 'sort': {'updatedAt': 'DESC'}},
        )
        return {
            'phase': self.desktop_runtime_phase,
            'stories': [
                {
                    'id': story.get('id'),
                    'status': story.get('status'),
                    'cursorAt': iso(story.get('cursorAt')),
                    'updatedAt': iso(story.get('updatedAt')),
                }
                for story in stories
            ],
        }

    async def desktop_timeline_snapshot(self) -> dict[str, Any]:
        """上游 `desktopTimelineSnapshot()`（`src/service.ts:809`）：只读投影。"""
        story = await self.get_canonical_story()
        if not story:
            return {'storyId': '', 'entries': [], 'scenes': [], 'facts': []}
        story_id = story.get('id')
        entries, scenes, facts, preplan = await asyncio.gather(
            self.db_get('interlude_script_entry', {'storyId': story_id}, {'limit': 120, 'sort': {'occurredAt': 'DESC'}}),
            self.db_get('interlude_scene', {'storyId': story_id}, {'limit': 12, 'sort': {'startedAt': 'DESC'}}),
            self.db_get('interlude_fact', {'storyId': story_id, 'status': 'active'}, {'limit': 24, 'sort': {'importance': 'DESC', 'updatedAt': 'DESC'}}),
            self.get_schedule_preplan(story_id),
        )
        return {
            'storyId': story_id,
            'cursorAt': iso(story.get('cursorAt')),
            'updatedAt': iso(story.get('updatedAt')),
            'timezone': pick(pick(story, 'setting'), 'timezone'),
            'entries': [
                {
                    'id': entry.get('id'), 'storyId': entry.get('storyId'),
                    'participantId': entry.get('participantId'), 'kind': entry.get('kind'),
                    'actor': entry.get('actor'), 'content': entry.get('content'),
                    'occurredAt': iso(entry.get('occurredAt')), 'metadata': entry.get('metadata'),
                }
                for entry in reversed(entries)
            ],
            'scenes': [
                {
                    'id': scene.get('id'), 'status': scene.get('status'),
                    'startedAt': iso(scene.get('startedAt')), 'endedAt': iso(scene.get('endedAt')),
                    'hook': scene.get('hook'), 'summary': scene.get('summary'),
                    'entryCount': scene.get('entryCount'),
                }
                for scene in scenes
            ],
            'facts': [
                {
                    'id': fact.get('id'), 'scope': fact.get('scope'), 'content': fact.get('content'),
                    'importance': fact.get('importance'), 'confidence': fact.get('confidence'),
                    'unresolved': fact.get('unresolved'), 'updatedAt': iso(fact.get('updatedAt')),
                }
                for fact in facts
            ],
            'preplan': None if not preplan else {
                'revision': preplan.get('revision'), 'timezone': preplan.get('timezone'),
                'validFrom': preplan.get('validFrom'), 'validThrough': preplan.get('validThrough'),
                'materializedDays': preplan.get('materializedDays'),
            },
        }

    async def desktop_purge_range(self, from_value: Any, to_value: Any) -> dict[str, Any]:
        """上游 `desktopPurgeRange(from, to)`（`src/service.ts:846`）逐条移植。"""
        story = await self.get_canonical_story()
        if not story:
            raise RuntimeError('当前 worker 没有可操作的剧本。')
        start = parse_dt(from_value)
        end = parse_dt(to_value)
        if start is None or end is None or start > end:
            raise RuntimeError('选区删除时间范围无效。')

        async def task() -> dict[str, Any]:
            await self.purge_story_range(story.get('id'), start, end)
            return {'storyId': story.get('id')}

        return await self.run_in_queue(story.get('id'), task)

    async def desktop_timeline_range(self, request: Any = None) -> dict[str, Any]:
        """上游 `desktopTimelineRange(request)`（`src/service.ts:864`）。

        依赖 `helpers.py` 的 `normalize_desktop_timeline_range_request` /
        `desktop_timeline_track_for_entry` / `desktop_timeline_entry_view`；
        未落地时返回空范围（降级，不抛异常）。
        """
        story = await self.get_canonical_story()
        if not story:
            return self._empty_desktop_timeline_range()
        normalize = getattr(_helpers_module, 'normalize_desktop_timeline_range_request', None) if _helpers_module else None
        if not callable(normalize):
            self.report_standalone('debug', '桌面时间线读取不可用：helpers 未提供范围归一化')
            return self._empty_desktop_timeline_range()
        query = normalize(request if isinstance(request, dict) else {})
        story_id = story.get('id')
        # 上游用 `occurredAt: {$gte, $lte}` 与 `id: {$lt: cursorId}` 做范围/游标查询；
        # 本移植版的 Database 只支持等值 where，故取回后在 Python 侧裁剪。
        entry_rows = await self.db_get(
            'interlude_script_entry', {'storyId': story_id},
            {'limit': 501, 'sort': {'occurredAt': 'DESC', 'id': 'DESC'}},
        )
        tracks = query.get('tracks') or set()
        range_from = query.get('from')
        range_to = query.get('to')
        cursor_id = query.get('cursorId')
        track_for = getattr(_helpers_module, 'desktop_timeline_track_for_entry', None)
        entry_view = getattr(_helpers_module, 'desktop_timeline_entry_view', None)
        selected: list[Any] = []
        for entry in entry_rows:
            occurred = parse_dt(entry.get('occurredAt'))
            if occurred is None or range_from is None or range_to is None:
                continue
            if occurred < range_from or occurred > range_to:
                continue
            if cursor_id is not None and (entry.get('id') or 0) >= cursor_id:
                continue
            if entry.get('kind') == 'redacted':
                continue
            if callable(track_for) and track_for(entry) not in tracks:
                continue
            selected.append(entry)
        limit = int(query.get('limit') or 240)
        selected = selected[:limit]
        selected.reverse()
        entries = [entry_view(entry) for entry in selected] if callable(entry_view) else []
        scenes = (
            await self.db_get('interlude_scene', {'storyId': story_id}, {'limit': 100, 'sort': {'startedAt': 'DESC'}})
            if 'scenes' in tracks else []
        )
        facts = (
            await self.db_get('interlude_fact', {'storyId': story_id, 'status': 'active'}, {'limit': 100, 'sort': {'updatedAt': 'DESC'}})
            if 'facts' in tracks else []
        )
        preplan = await self.get_schedule_preplan(story_id) if 'preplan' in tracks else None
        latest_entry_id = entry_rows[0].get('id') if entry_rows else 0
        latest_scene = ''
        for scene in scenes:
            started = iso(scene.get('startedAt')) or ''
            if started > latest_scene:
                latest_scene = started
        latest_fact = ''
        for fact in facts:
            updated = iso(fact.get('updatedAt')) or ''
            if updated > latest_fact:
                latest_fact = updated
        preplan_revision = preplan.get('revision', 0) if isinstance(preplan, dict) else 0
        return {
            'protocol': 4,
            'storyId': story_id,
            'revision': '%s:%s:%s:%s:%s' % (
                iso(story.get('updatedAt')), latest_entry_id, preplan_revision, latest_scene, latest_fact,
            ),
            'range': {'from': iso(range_from), 'to': iso(range_to)},
            'cursorAt': iso(story.get('cursorAt')),
            'updatedAt': iso(story.get('updatedAt')),
            'timezone': pick(pick(story, 'setting'), 'timezone'),
            'entries': entries,
            'scenes': scenes,
            'facts': facts,
            'preplan': None if not preplan else {
                'revision': preplan.get('revision'), 'timezone': preplan.get('timezone'),
                'validFrom': preplan.get('validFrom'), 'validThrough': preplan.get('validThrough'),
                'materializedDays': preplan.get('materializedDays'),
            },
            'nextCursor': ('entry:%s' % selected[0].get('id')) if len(entry_rows) == 501 and selected else None,
        }

    @staticmethod
    def _empty_desktop_timeline_range() -> dict[str, Any]:
        """上游 `emptyDesktopTimelineRange()`（`src/service.ts:167`）。"""
        now = iso(utc_now())
        return {
            'protocol': 4, 'storyId': '', 'revision': 'empty',
            'range': {'from': now, 'to': now}, 'entries': [], 'scenes': [], 'facts': [],
        }

    async def set_desktop_cursor_at(self, cursor_at: Any) -> None:
        """上游 `setDesktopCursorAt(cursorAt)`（`src/service.ts:911`）。"""
        story = await self.get_canonical_story()
        if not story:
            raise RuntimeError('当前 worker 没有可操作的剧本。')

        async def task() -> None:
            await self.db_set(
                'interlude_story', {'id': story.get('id')},
                {'cursorAt': parse_dt(cursor_at), 'updatedAt': self.now()},
            )

        await self.run_in_queue(story.get('id'), task)

    async def receive_desktop_event(self, event: Any, session: Any) -> bool:
        """上游 `receiveDesktopEvent(event, session)`（`src/service.ts:919`）。"""
        if self.desktop_runtime_phase != 'running':
            return False
        received_at = parse_dt(pick(event, 'occurredAt', 'occurred_at'))
        if received_at is None:
            return False
        if pick(event, 'kind') == 'group':
            return await self.receive_group(session, received_at)
        return await self.receive(session, received_at)

    # ------------------------------------------------------------------ #
    # 权限判定（`src/service.ts:927-996`）
    # ------------------------------------------------------------------ #

    def can_handle_session(self, session: Any) -> bool:
        """这条私聊/会话能不能进叙事（**v1.3.0 起平台无关**）。

        判定在 `explain_session_access` 里（那份同时给出原因），这里只取布尔值。
        """
        return self.explain_session_access(session)[0]

    def can_handle_group_session(self, session: Any) -> bool:
        """这个群能不能进叙事（判定见 `explain_group_access`，v1.3.0 起平台无关）。"""
        return self.explain_group_access(session)[0]

    # ---- v1.3.0：三张名单各自一个"仅处理名单内"开关 ---------------------- #
    #
    # 上游把这件事做成**一个总闸**（`onebot.enabled`）+ 三张名单：闸门关着时私聊全放行、
    # 群聊一律不接；闸门开着时空白名单＝全部拒绝，而且**只对 OneBot 家族生效**。
    # 本移植版按用户要求改成正交的三开关（`PORTING_NOTES.md` §22）：
    #
    #   * 每个开关**关闭**（默认）＝ 名单只用来做**针对性处理**（称呼 / 背景 / 初始关系 /
    #     群规则），名单外的照样进，走 `story_defaults` 的默认设定；
    #   * 每个开关**打开**＝ 只处理名单内的，名单外不接；
    #   * 三张名单**互相独立**，不再有"必须两个都命中"的耦合；
    #   * **所有平台一视同仁**（telegram / webchat 也受这套名单约束）。

    def _access_config(self) -> dict[str, Any]:
        """接入与名单那一段（schema 分组 `qq_access`，上游名 `onebot`）。"""
        return _config_section(self.config, 'onebot')

    def _access_flag(self, camel: str, snake: str, default: bool = False) -> bool:
        value = pick(self._access_config(), camel, snake)
        return default if value is None else bool(value)

    def explain_session_access(self, session: Any) -> tuple[bool, str]:
        """私聊路径的**原因版**：返回 `(是否放行, 原因)`。

        门顺序：忽略自己的消息 → 机器人名单（仅当"仅处理名单内"打开）→ 用户名单（同）。
        """
        config = self._access_config()
        self_id = normalize_account_id(pick(session, 'selfId', 'self_id'))
        user_id = normalize_account_id(pick(session, 'userId', 'user_id'))
        if pick(config, 'ignoreSelfMessages', 'ignore_self_messages') and self_id and self_id == user_id:
            return False, '机器人自己发的消息（`ignore_self_messages`）'
        if self._access_flag('botAccountsOnly', 'bot_accounts_only'):
            if not is_enabled_account(pick(config, 'botAccounts', 'bot_accounts'), self_id):
                return False, ('「仅处理名单内的机器人账号」开着，而这个登录账号（selfId=%s）不在 '
                               '`bot_accounts` 里' % (pick(session, 'selfId', 'self_id') or '?'))
        if self._access_flag('userAccountsOnly', 'user_accounts_only'):
            if not is_enabled_account(pick(config, 'userAccounts', 'user_accounts'), user_id):
                return False, ('「仅处理名单内的用户」开着，而发送者（userId=%s）不在 `user_accounts` 里'
                               % (pick(session, 'userId', 'user_id') or '?'))
        return True, ''

    def explain_group_access(self, session: Any) -> tuple[bool, str]:
        """群聊路径的**原因版**：返回 `(是否放行, 原因)`。

        为什么不只留布尔值：适配层拿到 `False` 就直接 `return`，而 core 里那几条拒绝
        报告走的是 `diagnostic` 频道——`logging.verbosity` 默认 `standard`，**日志里一个字
        都没有**（用户 2026-09-26 的日志：群里 @ 了机器人、Kela 也说了话，HDSI 全程沉默，
        看起来就是"群聊功能完全没生效"）。原因串给适配层，由它按 `note_group_skip` 节流打出来。
        """
        group_id = self._session_group_id(session)
        self_id = normalize_account_id(pick(session, 'selfId', 'self_id'))
        user_id = normalize_account_id(pick(session, 'userId', 'user_id'))
        if self._access_flag('ignoreSelfMessages', 'ignore_self_messages', default=True) \
                and self_id and self_id == user_id:
            return False, '机器人自己发的消息（`ignore_self_messages`）'
        if self._access_flag('botAccountsOnly', 'bot_accounts_only'):
            # 群聊**只看机器人账号**那一张名单：群成员不需要在用户名单里
            # （上游语义，v1.3.0 保留；被"仅处理名单内的用户"误伤过一次，别写回去）。
            if not is_enabled_account(
                pick(self._access_config(), 'botAccounts', 'bot_accounts'), self_id,
            ):
                return False, ('「仅处理名单内的机器人账号」开着，而这个登录账号（selfId=%s）不在 '
                               '`bot_accounts` 里' % (pick(session, 'selfId', 'self_id') or '?'))
        if self._access_flag('groupChatsOnly', 'group_chats_only'):
            if self.group_rule(group_id) is None:
                if self._listed_group_rule(group_id) is not None:
                    return False, '这条群规则写了 `enabled=false`（群号=%s）' % group_id
                return False, ('「仅处理名单内的群聊」开着，而这个群（%s）不在 `group_chats` 里'
                               % (group_id or '?'))
        return True, ''

    def default_group_rule(self, group_id: Any) -> dict[str, Any]:
        """名单外群的默认群规则（`group_chats_only` 关闭时用）。

        上游没有这条路径（群规则不存在就直接不接），本移植版按用户要求补上
        （`PORTING_NOTES.md` §22）。取值与 schema 里群规则的默认值一致：
        `mention-only`（不 @ 就不说话，最保守）、防抖 1 秒、冷却 60 秒、附带 20 条上下文、
        本地意愿门**关闭**（`evaluate_group_willingness` 未配置时 `should_call=True`）。
        """
        return {
            'groupId': normalize_group_id(group_id),
            'enabled': True,
            'label': '',
            'purpose': '',
            'characterRole': '',
            # 与 schema 里群规则的默认值一致：不 @ 就不说话（最保守）。
            'responseMode': 'mention-only',
            'contextLimit': 20,
            'debounceSeconds': 1.0,
            'cooldownSeconds': 60,
        }

    def group_rule_or_default(self, group_id: Any) -> dict[str, Any]:
        """名单里就用那一条，否则用 `default_group_rule`。"""
        return self.group_rule(group_id) or self.default_group_rule(group_id)

    def describe_access(self) -> list[tuple[str, str]]:
        """启动时把"接入与名单"的现状说清楚，返回 `[(级别, 文本), ...]`。

        为什么要在启动时就说：接入侧被名单挡掉是**完全静默**的（消息进得来、
        handler 也被调到，但什么都不发生）。等到用户 @ 半天没人理再回来问，
        中间已经浪费了一天（用户 2026-09-26 就是这样）。
        """
        config = self._access_config()
        notes: list[tuple[str, str]] = []
        specs = (
            ('botAccountsOnly', 'bot_accounts_only', 'botAccounts', 'bot_accounts', '机器人账号名单'),
            ('userAccountsOnly', 'user_accounts_only', 'userAccounts', 'user_accounts', '用户名单（私聊）'),
            ('groupChatsOnly', 'group_chats_only', 'groupChats', 'group_chats', '群聊名单'),
        )
        parts: list[str] = []
        for flag_camel, flag_snake, list_camel, list_snake, label in specs:
            only = self._access_flag(flag_camel, flag_snake)
            size = len(pick(config, list_camel, list_snake) or [])
            parts.append('%s %d 条→%s' % (label, size, '仅名单内' if only else '名单外也接'))
            if only and size == 0:
                notes.append(('warn', '%s打开了「仅处理名单内」但名单是空的：这一类消息全部不会被接入。'
                              % label))
        if not self._access_flag('groupChatsOnly', 'group_chats_only'):
            parts.append('名单外的群按默认群规则处理（不 @ 就不说话）')
        return notes + [('info', '接入与名单：%s' % '；'.join(parts))]

    def note_access_skip(self, key: str, interval_ms: int, message: str, *args: Any) -> bool:
        """同一条"没动静"的原因在 `interval_ms` 内只打一次（返回这次是否打了）。

        为什么是节流而不是静默：群聊里一条拒绝原因每分钟能来十几条，直接打 warn 会把
        整个日志淹掉（坑 45 就是这个教训）；但"群聊完全没生效"这种状态必须至少说一次，
        否则用户只能靠猜。key 里带上原因本身，所以「换了原因」会立刻重新打一条。
        """
        now = self.now_ms()
        last = self.access_notes.get(key)
        if last is not None and now - last < interval_ms:
            return False
        self.access_notes[key] = now
        self.report_standalone('warn', message, *args)
        return True

    def note_group_skip(self, session: Any, reason: str) -> bool:
        """群聊消息没能进叙事时，打一条**看得见**的 warn（按"群 + 原因"节流 10 分钟）。

        节流键里带原因，所以"原因变了"会立刻重新打一条——排查时能看到门是一道道关上的。
        """
        group_id = self._session_group_id(session)
        sender = normalize_account_id(pick(session, 'userId', 'user_id')) or '?'
        name = str(pick(session, 'username', 'user_name') or '').strip()
        content = str(pick(session, 'content') or pick(session, 'rawContent', 'raw_content') or '')
        preview = content.replace('\n', ' ')[:60]
        return self.note_group_skip_reason(
            group_id, reason,
            '发送者=%s%s 内容=%s' % (sender, ('(%s)' % name) if name else '', preview or '(空)'),
        )

    def note_group_skip_reason(self, group_id: Any, reason: str, detail: str = '') -> bool:
        """`note_group_skip` 的无会话版本：群回合已建成、但没走到模型调用（意愿 / 冷却 / 暂停）。"""
        normalized = normalize_group_id(group_id) or '?'
        return self.note_access_skip(
            'group-skip:%s:%s' % (normalized, reason),
            GROUP_SKIP_NOTE_INTERVAL_MS,
            '群聊消息未接入：%s ｜ 群=%s%s', reason, normalized,
            (' %s' % detail) if detail else '',
        )

    def group_rule(self, group_id: Any) -> Optional[dict[str, Any]]:
        """上游 `groupRule(groupId)`（`src/service.ts:967`）。

        `find(group => group.enabled !== false && normalizeGroupId(group.groupId) === normalized)`
        —— 注意 `enabled !== false` 意味着**缺省视为启用**。
        """
        normalized = normalize_group_id(group_id)
        config = _config_section(self.config, 'onebot')
        for group in pick(config, 'groupChats', 'group_chats') or []:
            if pick(group, 'enabled') is False:
                continue
            if normalize_group_id(pick(group, 'groupId', 'group_id')) == normalized:
                return group if isinstance(group, dict) else _as_mapping(group)
        return None

    def _listed_group_rule(self, group_id: Any) -> Optional[Any]:
        """**不管 `enabled`** 地找那条群规则：用来区分"没列进白名单"与"列了但关掉了"。"""
        normalized = normalize_group_id(group_id)
        config = _config_section(self.config, 'onebot')
        for group in pick(config, 'groupChats', 'group_chats') or []:
            if normalize_group_id(pick(group, 'groupId', 'group_id')) == normalized:
                return group
        return None

    def can_handle_participant(self, participant: Any) -> bool:
        """上游 `canHandleParticipant(participant)`（`src/service.ts:973`）。

        v1.3.0 起与 `explain_session_access` 用同一套判定（平台无关、两张名单各自一个开关）：
        出站 / 侧写路径上"这个人还算不算我们的人"必须与入站闸门一致，否则会出现
        "进来时算、投递时又被拒绝"的分裂。
        """
        return self.explain_session_access(participant)[0]

    def can_manage_session(self, session: Any) -> bool:
        """上游 `canManageSession(session)`（`src/service.ts:981`）逐条移植。"""
        if not self.can_handle_session(session):
            self.report_standalone_operation(
                'diagnostic', 'debug', '私聊被接入名单拦截 平台=%s 机器人ID=%s 用户ID=%s',
                pick(session, 'platform'), pick(session, 'selfId', 'self_id'), pick(session, 'userId', 'user_id'),
            )
            return False
        managers = [
            str(value if value is not None else '').strip()
            for value in (pick(self.shared_story_config, 'managerAccounts', 'manager_accounts') or [])
        ]
        managers = [value for value in managers if value]
        if not managers:
            return True
        return any(
            normalize_account_id(value) == normalize_account_id(pick(session, 'userId', 'user_id'))
            for value in managers
        )

    def can_handle_story(self, story: Any) -> bool:
        """上游 `canHandleStory(story)`（`src/service.ts:991`）：后台生活更新只要求机器人账号仍启用。

        v1.3.0 起同样平台无关，且只在「仅处理名单内的机器人账号」打开时才看名单
        （`docs/PORTING_NOTES.md` §22）。
        """
        if not self._access_flag('botAccountsOnly', 'bot_accounts_only'):
            return True
        return is_enabled_account(
            pick(self._access_config(), 'botAccounts', 'bot_accounts'),
            normalize_account_id(pick(story, 'selfId', 'self_id')),
        )

    @staticmethod
    def _session_group_id(session: Any) -> str:
        """上游 `sessionGroupId(session)`（`src/service.ts:7246`）：`guildId || channelId`。"""
        guild_id = pick(session, 'guildId', 'guild_id') or ''
        channel_id = pick(session, 'channelId', 'channel_id') or ''
        return normalize_group_id(guild_id or channel_id or '')

    # ------------------------------------------------------------------ #
    # 故事查找（`src/service.ts:998-1080`）
    # ------------------------------------------------------------------ #

    async def find_story(self, session: Any) -> Optional[InterludeStory]:
        """上游 `findStory(session)`（`src/service.ts:998`）逐条移植。"""
        platform = pick(session, 'platform')
        self_id = pick(session, 'selfId', 'self_id')
        user_id = pick(session, 'userId', 'user_id')
        if self.shared_story_config.get('enabled'):
            # 共享模式刻意在整个实例里只有一部 canonical 活动剧本。
            shared_id = story_id_for_character(platform, self_id)
            existing = await self.get_canonical_story(shared_id)
            if not existing:
                # 暂停中的故事对 active-only 的主查询不可见，但管理命令必须仍能找到它。
                existing = await self.get_paused_story(shared_id)
            if existing:
                existing = await self.repair_canonical_one_bot_story_transport(existing, session)
                if pick(existing, 'platform') == platform and pick(existing, 'id') != shared_id:
                    return await self.migrate_legacy_story(existing, session)
                await self.migrate_legacy_branch_into_shared(existing, session)
                return existing
        legacy_id = legacy_story_id_for(platform, self_id, user_id)
        rows = await self.db_get('interlude_story', {'id': legacy_id})
        existing = rows[0] if rows else None
        if existing or not self.shared_story_config.get('enabled'):
            return existing
        # 旧 beta 版本每个 QQ 一部剧本：该 QQ 首次回来时惰性迁移成共享剧本的第一条关系分支。
        legacy_rows = await self.db_get('interlude_story', {'id': legacy_id})
        legacy = legacy_rows[0] if legacy_rows else None
        return await self.migrate_legacy_story(legacy, session) if legacy else None

    async def get_paused_story(self, preferred_id: Optional[str] = None) -> Optional[InterludeStory]:
        """上游 `getPausedStory(preferredId?)`（`src/service.ts:1033`）逐字移植。"""
        paused = await self.db_get('interlude_story', {'status': 'paused'}, {'sort': {'updatedAt': 'DESC'}})
        if not paused:
            return None
        if preferred_id:
            for story in paused:
                if story.get('id') == preferred_id:
                    return story
        for story in paused:
            if str(story.get('id') or '').startswith('character:'):
                return story
        return paused[0]

    async def get_canonical_story(self, preferred_id: Optional[str] = None) -> Optional[InterludeStory]:
        """上游 `getCanonicalStory(preferredId?)`（`src/service.ts:1046`）逐字移植。

        解析并强制"唯一全局活动剧本"：优先 id 命中，否则保留最近更新的一行，
        并把其余活动行立即归档。
        """
        active = await self.db_get('interlude_story', {'status': 'active'}, {'sort': {'updatedAt': 'DESC'}})
        if not active:
            return None
        canonical = None
        if preferred_id:
            for story in active:
                if story.get('id') == preferred_id:
                    canonical = story
                    break
        if canonical is None:
            for story in active:
                if str(story.get('id') or '').startswith('character:'):
                    canonical = story
                    break
        if canonical is None:
            canonical = active[0]
        now = self.now()
        for story in active:
            if story.get('id') == canonical.get('id'):
                continue
            await self.db_set('interlude_story', {'id': story.get('id')}, {'status': 'archived', 'updatedAt': now})
            self.report_standalone(
                'warn', '主剧本归档完成 原因=检测到多个活动故事 保留=%s 已归档=%s 范围=%s',
                canonical.get('id'), story.get('id'), '全局',
            )
        return canonical

    async def find_participant(self, session: Any, story: Any = None) -> Optional[InterludeParticipant]:
        """上游 `findParticipant(session, story?)`（`src/service.ts:1063`）逐字移植。

        旧 beta 的 participant id 对 bot/user 是全局的，不能只信那个 id：
        共享模式切换或迁移时同一对账号可能暂时挂在另一部剧本下，
        按故事绑定的查找能避免误移/误曝另一条关系分支。
        """
        resolved = story if story is not None else await self.find_story(session)
        if not resolved:
            return None
        rows = await self.db_get('interlude_participant', {'storyId': resolved.get('id')})
        for item in rows:
            if same_participant_endpoint(item, session):
                return item
        return None

    async def participants(self, story_id: str, include_paused: bool = False) -> list[InterludeParticipant]:
        """上游 `participants(storyId, includePaused = false)`（`src/service.ts:1075`）。

        `b.updatedAt.getTime() - a.updatedAt.getTime()` → 按 `updatedAt` 降序。
        """
        rows = await self.db_get('interlude_participant', {'storyId': story_id})
        filtered = [row for row in rows if include_paused or row.get('status') == 'active']
        filtered.sort(key=lambda row: dt_ms(parse_dt(row.get('updatedAt')) or utc_now()), reverse=True)
        return filtered

    # ------------------------------------------------------------------ #
    # 故事与参与者写入（`src/service.ts:1082-1243`）
    # ------------------------------------------------------------------ #

    async def create_story(self, session: Any, name: Optional[str] = None) -> InterludeStory:
        """上游 `createStory(session, name?)`（`src/service.ts:1082`）逐条移植。"""
        if not self.can_handle_session(session) and not self.can_handle_group_session(session):
            raise RuntimeError('This session is not allowed to use HDS Interlude.')
        existing = await self.find_story(session)
        if existing:
            if pick(session, 'isDirect', 'is_direct'):
                await self.ensure_participant(existing, session)
            return existing
        now = self.now()
        setting = self.initial_story_setting(name)
        platform = pick(session, 'platform')
        self_id = pick(session, 'selfId', 'self_id')
        user_id = pick(session, 'userId', 'user_id')
        character = pick(setting, 'character') or {}
        story: InterludeStory = {
            'id': story_id_for_character(platform, self_id)
            if self.shared_story_config.get('enabled')
            else legacy_story_id_for(platform, self_id, user_id),
            'platform': platform, 'selfId': self_id, 'userId': '',
            'channelId': '', 'status': 'active', 'setting': setting,
            'state': _empty_story_state(),
            'cursorAt': now, 'createdAt': now, 'updatedAt': now,
        }
        try:
            await self.db_create('interlude_story', story)
        except Exception as error:
            # 两个账号几乎同时私聊一个新机器人：数据库主键是最终仲裁者，
            # 加入胜者而不是让其中一方的首条消息失败。
            raced_rows = await self.db_get('interlude_story', {'id': story['id']})
            raced = raced_rows[0] if raced_rows else None
            if not raced:
                raise error
            await self.ensure_continuity(raced, now)
            await self.ensure_participant(raced, session, now)
            return raced
        await self.ensure_continuity(story, now)
        if pick(session, 'isDirect', 'is_direct'):
            await self.ensure_participant(story, session, now)
        await self.append_entry(story['id'], {
            'kind': 'setup', 'actor': 'system',
            'content': 'The story begins with %s.' % pick(character, 'name'),
            'occurredAt': iso(now), 'metadata': {},
        }, now)
        await self.schedule_next_automatic_advance(story['id'], now)
        return story

    async def story_start_readiness(self, session: Any) -> dict[str, Any]:
        """上游 `storyStartReadiness(session)`（`src/service.ts:1122`）逐条移植。

        `new Intl.DateTimeFormat('en-US', { timeZone })` 的等价校验用
        `zoneinfo.ZoneInfo`（`docs/PORT_PLAN.md` §2 Node → Python 映射表）。
        """
        setting = self.initial_story_setting()
        blockers: list[str] = []
        warnings: list[str] = []
        if not self.can_handle_session(session):
            blockers.append('当前机器人账号或用户账号未通过 OneBot 白名单。')
        character = pick(setting, 'character') or {}
        if not str(pick(character, 'name') or '').strip():
            blockers.append('storyDefaults.characterName 为空。')
        if not str(pick(character, 'profile') or '').strip():
            blockers.append('storyDefaults.characterProfile 尚未填写。')
        timezone = str(pick(setting, 'timezone') or '')
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(timezone)
        except Exception:
            blockers.append('时区无效：%s' % timezone)
        providers = pick(self.model_routing, 'providers') or []
        if any(pick(provider, 'enabled') and pick(provider, 'endpoint') for provider in providers):
            if not self._main_route_available():
                blockers.append('没有可用的主叙事模型：请在模型中心勾选一条“用作主叙事模型”。')
        else:
            warnings.append('尚未配置启用的模型连接：可用于安装验证，但不会生成远程叙事。')
        if not str(pick(setting, 'perspective') or '').strip():
            warnings.append('Perspective 尚未填写；主角将仅使用 Canon 与已有 Overlay。')
        if not str(pick(setting, 'world') or '').strip():
            warnings.append('world 尚未填写；建议在 Console 补充现实边界与地点背景。')
        existing = await self.find_story(session)
        return {
            'ready': not blockers,
            'existing': existing,
            'blockers': blockers,
            'warnings': warnings,
            'preview': {
                'characterName': pick(character, 'name'),
                'characterProfile': bool(str(pick(character, 'profile') or '').strip()),
                'perspective': bool(str(pick(setting, 'perspective') or '').strip()),
                'world': bool(str(pick(setting, 'world') or '').strip()),
                'timezone': timezone,
                'model': self.main_model_label(),
                'autoCreate': pick(self.runtime_config, 'autoCreate', 'auto_create') is not False,
            },
        }

    async def ensure_participant(
        self,
        story: InterludeStory,
        session: Any,
        now: Any = None,
        known_existing: Any = None,
    ) -> InterludeParticipant:
        """上游 `ensureParticipant(story, session, now = new Date(), knownExisting?)`
        （`src/service.ts:1158`）逐条移植。"""
        now = parse_dt(now) or self.now()
        user_id = pick(session, 'userId', 'user_id')
        account = self.user_account_rule(user_id)
        preset = self.participant_preset(user_id)
        existing = known_existing if known_existing is not None else await self.find_participant(session, story)
        if existing:
            # Console 对白名单的编辑是有意的身份变更；关系演化放
            # participant.state.relationshipOverlay，这里只刷新基础资料。
            person_id = (
                _trim(pick(account, 'personId', 'person_id'))
                or _trim(pick(preset, 'personId', 'person_id'))
                or existing.get('personId')
                or user_id
            )
            display_name = (
                _trim(pick(account, 'label'))
                or _trim(pick(preset, 'label'))
                or existing.get('displayName')
                or pick(session, 'username')
                or user_id
            )
            profile = (
                _trim(pick(account, 'profile'))
                or _trim(pick(preset, 'profile'))
                or existing.get('profile')
                or self.story_defaults.get('userProfile')
            )
            relationship = (
                _trim(pick(account, 'relationship'))
                or _trim(pick(preset, 'relationship'))
                or existing.get('relationship')
                or self.story_defaults.get('relationship')
            )
            channel_id = pick(session, 'channelId', 'channel_id')
            changed = (
                existing.get('storyId') != story.get('id')
                or existing.get('channelId') != channel_id
                or existing.get('personId') != person_id
                or existing.get('displayName') != display_name
                or existing.get('profile') != profile
                or existing.get('relationship') != relationship
            )
            if changed:
                await self.db_set('interlude_participant', {'id': existing.get('id')}, {
                    'storyId': story.get('id'), 'channelId': channel_id, 'personId': person_id,
                    'displayName': display_name, 'profile': profile, 'relationship': relationship,
                    'updatedAt': now,
                })
                self.report_operation(
                    'diagnostic', 'debug', story, 'user-message',
                    '参与者资料已从 Console 同步 参与者=%s', existing.get('id'),
                )
            merged = dict(existing)
            merged.update({
                'storyId': story.get('id'), 'channelId': channel_id, 'personId': person_id,
                'displayName': display_name, 'profile': profile, 'relationship': relationship,
                'updatedAt': now if changed else existing.get('updatedAt'),
            })
            return merged  # type: ignore[return-value]
        platform = pick(session, 'platform')
        self_id = pick(session, 'selfId', 'self_id')
        base_id = participant_id_for(platform, self_id, user_id)
        # 历史 id 空闲时保留；若旧的分账号剧本仍占用它，改用确定性的故事后缀，
        # 而不是在回滚/迁移时抢走那条分支的主键。
        globally_existing = await self.get_participant(base_id)
        if not globally_existing or pick(globally_existing, 'storyId', 'story_id') == story.get('id'):
            participant_id = base_id
        else:
            participant_id = participant_id_for_story(story.get('id'), platform, self_id, user_id)
        participant: InterludeParticipant = {
            'id': participant_id, 'storyId': story.get('id'), 'platform': platform,
            'selfId': self_id, 'userId': user_id, 'channelId': pick(session, 'channelId', 'channel_id'),
            'personId': (
                _trim(pick(account, 'personId', 'person_id'))
                or _trim(pick(preset, 'personId', 'person_id'))
                or user_id
            ),
            'displayName': (
                _trim(pick(account, 'label'))
                or _trim(pick(preset, 'label'))
                or pick(session, 'username')
                or user_id
            ),
            'profile': (
                _trim(pick(account, 'profile'))
                or _trim(pick(preset, 'profile'))
                or self.story_defaults.get('userProfile')
            ),
            'relationship': (
                _trim(pick(account, 'relationship'))
                or _trim(pick(preset, 'relationship'))
                or self.story_defaults.get('relationship')
            ),
            'state': _empty_participant_state(), 'status': 'active', 'createdAt': now, 'updatedAt': now,
        }
        try:
            await self.db_create('interlude_participant', participant)
        except Exception as error:
            # 两条首条私聊可能都还没进入故事队列：主键解决这次竞争。
            raced = await self.find_participant(session, story)
            if not raced:
                raise error
            return raced
        await self.append_entry(story.get('id'), {
            'kind': 'participant-joined', 'actor': 'system',
            'content': "%s entered the character's relationship network." % participant['displayName'],
            'occurredAt': iso(now), 'metadata': {'personId': participant['personId']},
        }, now, participant['id'])
        return participant

    async def update_setting(self, story: InterludeStory, patch: Any) -> InterludeStory:
        """上游 `updateSetting(story, patch)`（`src/service.ts:1222`）。"""
        setting = merge_setting(pick(story, 'setting') or {}, patch)
        now = self.now()
        await self.db_set('interlude_story', {'id': story.get('id')}, {'setting': setting, 'updatedAt': now})
        updated = dict(story)
        updated['setting'] = setting
        updated['updatedAt'] = now
        return updated  # type: ignore[return-value]

    async def set_status(self, story: InterludeStory, status: StoryStatus) -> InterludeStory:
        """上游 `setStatus(story, status)`（`src/service.ts:1229`）。"""
        now = self.now()
        await self.db_set('interlude_story', {'id': story.get('id')}, {'status': status, 'updatedAt': now})
        updated = dict(story)
        updated['status'] = status
        updated['updatedAt'] = now
        return updated  # type: ignore[return-value]

    async def recent_entries(self, story_id: str, limit: Optional[int] = None) -> list[Any]:
        """上游 `recentEntries(storyId, limit = config.runtime.contextEntryLimit)`
        （`src/service.ts:1235`）逐条移植。"""
        if limit is None:
            limit = _config_value(self.runtime_config, 'contextEntryLimit', 'context_entry_limit', 20)
        bounded = max(1, min(int(limit), 200))
        rows = await self.db_get(
            'interlude_script_entry', {'storyId': story_id},
            {'limit': bounded, 'sort': {'occurredAt': 'DESC'}},
        )
        # 批次 4：purge fallback 墓碑不进 prompt（物理删除通常无残留，此为软删兜底过滤）。
        filtered = [entry for entry in rows if entry.get('kind') != 'redacted']
        filtered.reverse()
        return filtered


def _trim(value: Any) -> str:
    """上游 `String(value ?? '').trim()` 的等价物（用于 `||` 链里的真值判断）。"""
    return value.strip() if isinstance(value, str) else ''


def _empty_story_state() -> StoryState:
    """`types.empty_story_state()`（上游 `emptyStoryState()`）。"""
    from ..types import empty_story_state
    return empty_story_state()


def _empty_participant_state() -> dict[str, Any]:
    """`types.empty_participant_state()`（上游 `emptyParticipantState()`）。"""
    from ..types import empty_participant_state
    return empty_participant_state()
