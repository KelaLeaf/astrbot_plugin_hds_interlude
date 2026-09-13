"""service Chunk1 mixin —— `upstream/src/service.ts:1248-1928` 的逐条移植。

范围（成员声明起始行，顺序与上游一致）：

| 上游行 | 成员 | 主题 |
| --- | --- | --- |
| 1248 | `recentEntriesForPrompt` | 条数下限 + 近日时间窗双取，按 id 去重合并 |
| 1264 | `memories` | 长期记忆读取（参与者过滤 + 相关性排序） |
| 1277 | `adminFacts` | 管理视图：活跃长期事实 |
| 1284 | `adminPendingIntents` | 管理视图：待办意图 |
| 1291 | `adminStatePatches` | 管理视图：设定演化提案 |
| 1299 | `addAdminScriptNote` | 管理员注记条目 |
| 1312 | `addAdminFact` | 管理员高置信事实 |
| 1325 | `forgetAdminFact` | 事实软删（保留审计行） |
| 1332 | `cancelAdminIntent` | 意图取消 |
| 1339 | `rejectAdminStatePatch` | 提案驳回 |
| 1347 | `clearSettingOverlay` | 设定覆盖清理（串行入口） |
| 1355 | `rebaseTimeline` | 宿主时间线重置 |
| 1383 | `clearSettingOverlayUnlocked` | 覆盖 / 关系覆盖 / 提案 / 快照一并失效 |
| 1431 | `purgeAllStoryData` | 单剧本全量清除（软删墓碑兜底） |
| 1456 | `purgeAllData` | 全平台重置，只留一部 canonical 空剧本 |
| 1471 | `purgePlatformData` | 单平台（含 OneBot 别名族）清除 |
| 1486 | `clearDatabase` | 只清 HDSI 自有表（物理删除 + 逻辑清空兜底） |
| 1543 | `purgeStoryRange` | 按时间区间清除剧本与派生记忆 |
| 1605 | `receiveGroup` | **群聊入站主入口** |
| 1645 | `receive` | **私聊入站主入口** |
| 1720 | `groupSenderName` | 群成员显示名（账号规则 → 观察值 → 12h 缓存） |
| 1735 | `lookupGroupMemberName` | 群成员名查询（走 `transport.fetch_member_name`） |
| 1750 | `bufferGroupMessage` | 群回合缓冲（debounce 计时器 + revision 闸门） |
| 1769 | `flushGroupTurn` | 群回合刷出（意愿判定 → 冷却 → 主叙事 → 投递） |

界限说明（重要）
----------------
任务清单里提到的「群冷却 / 群表态执行 / 贴纸与原生表情解析与发送 /
`bufferUserNarrative` / `signalIncomingInterruption` / `deliverEarlyPrivateReply` /
`describeUserEvent`」**声明起始行都落在 1929-2572（Chunk2）**，
按 `docs/PORT_PLAN_SERVICE.md`「每个 mixin 文件只包含本行范围内的成员」，
它们不在本文件里；本文件通过 `self.xxx()` 跨 mixin 调用（Python MRO 解析），
`receive` / `receiveGroup` / `flushGroupTurn` 因此能照上游顺序调用它们。

模块级辅助函数
--------------
上游在 `service.ts` 模块作用域定义了 `samePlatformFamily` / `mentionsBot` /
`quotesBot` / `targetableMessageId` / `groupMessageRef` / `normalizeGroupDisplayName`
（`service.ts:7300-7710`），它们归 `docs/PORT_PLAN_SERVICE.md` 的 Helpers 块
（`helpers.py`）。`helpers.py` 目前只导出了私有版 `_normalize_group_display_name`，
其余五个尚未落地，因此这里用 base.py 同款的「**优先 helpers、缺失时本文件等价实现**」
模式（`_prefer` 系列）；`helpers.py` 一旦补上同名导出，本文件的本地实现自动让位。

键名法（`docs/PORT_PLAN.md` §2）
-------------------------------
- 内部中转结构（缓冲回合、`accepted` / `result`）一律 snake_case；
- **发给模型的 payload**（`groupContext` 及其 messages）与**数据库列名 / metadata**
  保持上游 camelCase；
- 从模型输出、旧数据、跨 mixin 读入的 dict 一律 `pick` 双读。
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from ..time import dt_ms, format_log_time, iso, parse_dt
from ..group_willingness import consume_group_willingness, evaluate_group_willingness
from ..script.commit_builder import find_group_script_event
from ..script.contract import message_event_reference
from ..script.delivery_ledger import platform_action_reference
from ..story_state import decode_story_state, encode_story_state
from ..types import empty_participant_state, empty_story_state
from .base import (
    ServiceBase,
    _config_section,
    _config_value,
    is_one_bot_platform,
    normalize_account_id,
    normalize_group_id,
    pick,
)
from .helpers import (
    clip,
    describe_group_attachments,
    describe_quoted_message,
    extract_session_file_facts,
    extract_session_voice_count,
    format_group_speaker,
    narrative_cursor,
    normalize_group_chat_actions,
    normalize_group_visible_reply,
    normalize_participant_state,
)

__all__ = ['ServiceChunk1']

#: JS `Time.hour` / `Time.second`（Koishi `Time` 的单位毫秒）。
_HOUR_MS = 3_600_000
_SECOND_MS = 1_000

#: 上游 `clearDatabase` 里按顺序清空的表（`src/service.ts:1493-1496`）。
_CLEAR_DATABASE_TABLES = (
    'interlude_script_entry', 'interlude_memory', 'interlude_intent',
    'interlude_scene', 'interlude_arc', 'interlude_fact', 'interlude_state_patch',
    'interlude_overlay_snapshot', 'interlude_web_observation', 'interlude_schedule_preplan',
    'interlude_participant', 'interlude_story',
)

_MISSING = object()


# =========================================================================== #
# 模块级辅助：优先 helpers.py 的移植版，缺失时本文件的等价实现
# =========================================================================== #

try:  # pragma: no cover - 取决于 helpers.py 的导出进度
    from .helpers import same_platform_family as _same_platform_family  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover
    def _same_platform_family(left: Any, right: Any) -> bool:
        """上游 `samePlatformFamily`（`src/service.ts:7676`）。

        OneBot 的传输别名（`onebot` / `onebot:x` / `napcat` / `qq:onebot`）在管理员
        视角下是同一个平台族；其余按小写全等比较。
        """
        if is_one_bot_platform(left) and is_one_bot_platform(right):
            return True
        return str(left if left is not None else '').strip().lower() == \
            str(right if right is not None else '').strip().lower()

try:  # pragma: no cover
    from .helpers import normalize_group_display_name as _normalize_group_display_name  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover
    try:
        # helpers.py 目前只有私有版（`helpers.py:724`，与上游逐字对应）。
        from .helpers import _normalize_group_display_name  # type: ignore[attr-defined]
    except ImportError:
        import re as _re

        _NEWLINE_RE = _re.compile(r'[\r\n]')

        def _normalize_group_display_name(*candidates: Any) -> str:
            """上游 `normalizeGroupDisplayName`（`src/service.ts:7570`）：首个非空候选，截断 80。"""
            for candidate in candidates:
                name = _NEWLINE_RE.sub(' ', str(candidate if candidate is not None else '')).strip()
                if name:
                    return name[:80]
            return ''

try:  # pragma: no cover
    from .helpers import targetable_message_id as _targetable_message_id  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover
    import re as _re2

    _INTEGER_ID_RE = _re2.compile(r'^-?\d+$')

    def _targetable_message_id(value: Any) -> Optional[str]:
        """上游 `targetableMessageId`（`src/service.ts:7300`）。

        只有十进制（可负）且非 `0` 的 id 才能作为平台动作目标；
        其余（含空串）返回 `None`（上游 `undefined`，Python 侧统一用 `None`）。
        """
        text = str(value if value is not None else '').strip()
        return text if _INTEGER_ID_RE.match(text) and text != '0' else None

try:  # pragma: no cover
    from .helpers import group_message_ref as _group_message_ref  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover
    def _group_message_ref(entry_id: Any) -> str:
        """上游 `groupMessageRef`（`src/service.ts:7305`）：`msg-<非负整数>`。"""
        try:
            value = int(entry_id)
        except (TypeError, ValueError):
            value = 0
        return 'msg-%d' % max(0, value)

try:  # pragma: no cover
    from .helpers import mentions_bot as _mentions_bot  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover
    def _mentions_bot(session: Any) -> bool:
        """上游 `mentionsBot`（`src/service.ts:7578`）。

        `SessionView`（适配层提供的等价 session）自带 `mentioned_bot()`，
        它按 AstrBot 的段模型判定 `at` 段；其它形状（dict / 原生 Koishi session）
        回落到上游的**内容匹配**实现：正文包含机器人账号 id 或 `<at ... id=...>`。
        """
        method = getattr(session, 'mentioned_bot', None)
        if callable(method):
            return bool(method())
        self_id = normalize_account_id(_session_read(session, 'selfId', 'self_id'))
        if not self_id:
            return False
        content = str(_session_read(session, 'content') or '')
        if self_id in content:
            return True
        import re as _re3
        return bool(_re3.search(r'<at[^>]+id=["\']?%s' % _re3.escape(self_id), content, _re3.IGNORECASE))

try:  # pragma: no cover
    from .helpers import quotes_bot as _quotes_bot  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover
    def _quotes_bot(session: Any) -> bool:
        """上游 `quotesBot`（`src/service.ts:7696`）：引用消息的作者就是机器人自己。"""
        method = getattr(session, 'quoted_bot', None)
        if callable(method):
            return bool(method())
        quote = _session_read(session, 'quote')
        user = pick(quote, 'user') if isinstance(quote, dict) else None
        quote_user = pick(user, 'id', 'user_id') if isinstance(user, dict) else None
        return str(quote_user if quote_user is not None else '') == \
            str(_session_read(session, 'selfId', 'self_id') or '')


def _session_read(session: Any, camel: str, snake: Optional[str] = None) -> Any:
    """读 Koishi `Session` 字段：同时支持 `SessionView`（snake_case 属性）与 dict。

    `plugin/adapters/astrbot_bridge.py` 提供的是 `plugin.core.service.session.SessionView`
    （`docs/PORT_PLAN_SERVICE.md` §6），它的字段是 snake_case；测试与桌面桥可能直接给
    dict（camelCase 或 snake_case）。这里统一成一个读取口。
    """
    if session is None:
        return None
    if snake is None:
        # `selfId` → `self_id` 这类常规命名；`author` / `content` / `quote` 两写同形。
        import re as _re4
        snake = _re4.sub(r'(?<!^)(?=[A-Z])', '_', camel).lower()
    if isinstance(session, dict):
        return pick(session, camel, snake)
    value = getattr(session, snake, _MISSING)
    if value is not _MISSING:
        return value
    return getattr(session, camel, None)


def _spawn(awaitable: Any) -> None:
    """把后台协程挂成任务（等价 Chunk0 的 `_spawn` / 上游 `void this.xxx()`）。

    放在模块级是为了让本 mixin 能独立测试（Chunk0 未混入时 `self._spawn` 不存在），
    同时不新增上游没有的类成员。
    """
    if asyncio.iscoroutine(awaitable):
        asyncio.ensure_future(awaitable)


def _clear_database_fallback(table: str) -> dict[str, Any]:
    """上游 `clearDatabase` 里那串嵌套三元（`src/service.ts:1504-1521`）的静态分支。

    `interlude_story` / `interlude_participant` 两个分支需要实例方法
    （`initialStorySetting`）或 types 工厂，直接写在 `clear_database` 里。
    """
    if table == 'interlude_script_entry':
        return {'kind': 'redacted', 'actor': 'system', 'content': '[HDSI 数据库已清空]', 'metadata': {'redacted': True}}
    if table == 'interlude_memory':
        return {'status': 'deleted', 'content': '[HDSI 数据库已清空]'}
    if table == 'interlude_intent':
        return {'status': 'cancelled', 'summary': '[HDSI 数据库已清空]'}
    if table in ('interlude_scene', 'interlude_arc'):
        return {'status': 'closed', 'hook': '', 'summary': '', 'entryCount': 0, 'sceneCount': 0}
    if table == 'interlude_fact':
        return {'status': 'superseded', 'content': '[HDSI 数据库已清空]'}
    if table == 'interlude_web_observation':
        return {'status': 'deleted', 'url': '', 'title': '', 'excerpt': '', 'summary': '[HDSI 数据库已清空]'}
    if table == 'interlude_schedule_preplan':
        return {
            'regimes': [], 'exceptions': [], 'materializedDays': [],
            'validFrom': '1970-01-01', 'validThrough': '1970-01-01',
            'lastReviewedLocalDate': '', 'reviewReason': '[HDSI 数据库已清空]',
        }
    return {'status': 'rejected', 'proposedValue': '[HDSI 数据库已清空]', 'evidence': ''}


def _config_limit(section: Any, camel: str, snake: str, default: Any) -> Any:
    """读一个可空配置项：缺失（`None`）时用上游 schema 的默认值。"""
    raw = _config_value(section, camel, snake, None)
    return default if raw is None else raw


def _message_characters(runtime: Any) -> int:
    """上游 `normalizeGroupVisibleReply(..., this.config.runtime.maxMessageCharacters, ...)`。

    配置缺失时上游是 `String.prototype.slice(0, undefined)` —— **不截断**，
    因此这里用 `2**31-1`（"不截断"）而不是某个会静默砍掉群回复的小默认值
    （与 Chunk2 的 `_message_characters` 同一处理）。
    """
    raw = _config_value(runtime, 'maxMessageCharacters', 'max_message_characters', None)
    if raw is None or isinstance(raw, bool):
        return 2 ** 31 - 1
    return int(raw)


def _group_context_messages(messages: Any) -> list[dict[str, Any]]:
    """把 `groupMessages()` 的输出归一成上游 `GroupMessageContext` 的 wire 形状（camelCase）。

    上游的群上下文对象逐字是 `{ senderId, senderName, speaker, messageRef,
    messageId, quote, content, occurredAt, direction }`（键名法：发给模型/适配层的
    payload 保持上游拼写）。Chunk2 的 `group_messages()` 内部按 snake_case 造这个结构
    （`sender_id` / `message_ref` / `occurred_at`），而 `helpers.normalize_group_chat_actions`
    与 `to_prompt_payload` 读的引用键是 camelCase —— 因此这里在**调用点**把它折回上游形状：
    `replyTo` / `messageReactions` 才不会被静默丢弃，prompt 里也和上游一字不差。
    读取用 `pick` 双读，所以两种拼写都能接受。
    """
    result: list[dict[str, Any]] = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        item: dict[str, Any] = {
            'senderId': pick(message, 'senderId', 'sender_id'),
            'senderName': pick(message, 'senderName', 'sender_name'),
            'speaker': pick(message, 'speaker'),
            'content': pick(message, 'content'),
            'occurredAt': pick(message, 'occurredAt', 'occurred_at'),
            'direction': pick(message, 'direction'),
        }
        message_id = pick(message, 'messageId', 'message_id')
        if message_id:
            item['messageId'] = message_id
        message_ref = pick(message, 'messageRef', 'message_ref')
        if message_ref:
            item['messageRef'] = message_ref
        quote = pick(message, 'quote')
        if quote:
            item['quote'] = quote
        result.append(item)
    return result


def _chat_capabilities_wire(capabilities: Any) -> Any:
    """把能力声明折回上游 `ChatActionCapabilities` 的 wire 形状（camelCase）。

    同上：`group_chat_capabilities()` 产出 snake_case（`quote_reply` / `native_faces` /
    `expression_threshold`），而 `normalize_group_chat_actions` 用 camelCase 读
    `quoteReply`。这里补齐 camelCase 别名并移除 snake_case 别名，保证送给模型与
    适配层的对象与上游逐字一致（`resolve_native_face` / `narrator_prompts` 都是双读，不受影响）。
    """
    if not isinstance(capabilities, dict):
        return capabilities
    wire = dict(capabilities)
    for camel, snake in (
        ('quoteReply', 'quote_reply'),
        ('nativeFaces', 'native_faces'),
        ('expressionThreshold', 'expression_threshold'),
    ):
        value = pick(capabilities, camel, snake)
        if value is not None:
            wire[camel] = value
        wire.pop(snake, None)
    return wire


# =========================================================================== #
# ServiceChunk1
# =========================================================================== #

class ServiceChunk1(ServiceBase):
    """对应 `upstream/src/service.ts` 第 1248–1928 行的成员。"""

    # ------------------------------------------------------------------ #
    # 记忆读取（`src/service.ts:1248-1274`）
    # ------------------------------------------------------------------ #

    async def recent_entries_for_prompt(self, story_id: str, now: Any) -> list[Any]:
        """上游 `recentEntriesForPrompt(storyId, now)`（`src/service.ts:1248`）。

        实时叙事同时保**条数下限**与**近期墙钟窗口**：一串密集对话可以超出名义回合数，
        却不会立刻抹掉同一小时内更早说过的话。两次查询按 id 去重后，按
        `occurredAt` 升序、`id` 升序返回。

        移植说明：上游用 `occurredAt: { $gte }` 做范围查询，本移植版的
        `Database.all()` 只支持等值 where（`base.py:db_get` 对算子显式报错），
        因此改成「先按 occurredAt 倒序取 500 行，再在 Python 侧按 cutoff 过滤」——
        与上游取到的是同一批行（窗口内最新的至多 500 行）。
        """
        raw_count = _config_limit(self.runtime_config, 'contextEntryLimit', 'context_entry_limit', 20)
        count = max(50, min(int(raw_count), 200))
        raw_minutes = _config_limit(self.runtime_config, 'contextTimeWindowMinutes', 'context_time_window_minutes', 60)
        minutes = max(0.0, min(float(raw_minutes), 1_440.0))
        moment = parse_dt(now) or self.now()
        cutoff_ms = dt_ms(moment) - int(minutes * 60_000)

        async def count_rows() -> list[Any]:
            return await self.db_get(
                'interlude_script_entry', {'storyId': story_id},
                {'limit': count, 'sort': {'occurredAt': 'DESC'}},
            )

        async def time_rows() -> list[Any]:
            if minutes <= 0:
                return []
            rows = await self.db_get(
                'interlude_script_entry', {'storyId': story_id},
                {'limit': 500, 'sort': {'occurredAt': 'DESC'}},
            )
            selected = []
            for row in rows:
                occurred = parse_dt(pick(row, 'occurredAt', 'occurred_at'))
                if occurred is not None and dt_ms(occurred) >= cutoff_ms:
                    selected.append(row)
            return selected

        count_result, time_result = await asyncio.gather(count_rows(), time_rows())
        by_id: dict[Any, Any] = {}
        for entry in [*count_result, *time_result]:
            by_id[pick(entry, 'id')] = entry
        # 上游：`left.occurredAt.getTime() - right.occurredAt.getTime() || left.id - right.id`。
        return sorted(
            by_id.values(),
            key=lambda entry: (
                dt_ms(parse_dt(pick(entry, 'occurredAt', 'occurred_at')) or moment),
                int(pick(entry, 'id') or 0),
            ),
        )

    async def memories(
        self,
        story_id: str,
        limit: Optional[int] = None,
        participant_id: Optional[str] = None,
    ) -> list[Any]:
        """上游 `memories(storyId, limit = config.runtime.memoryLimit, participantId?)`
        （`src/service.ts:1264`）。

        读取上界是 `max(1, min(limit * 4, 500))`（先多取再按关系过滤/截断），
        排序 `importance` 降序、`updatedAt` 降序，最后 `slice(0, limit)`。
        """
        if limit is None:
            limit = int(_config_limit(self.runtime_config, 'memoryLimit', 'memory_limit', 20))
        limit = int(limit)
        bounded = max(1, min(limit * 4, 500))
        rows = await self.db_get(
            'interlude_memory', {'storyId': story_id, 'status': 'active'},
            {'limit': bounded, 'sort': {'importance': 'DESC', 'updatedAt': 'DESC'}},
        )
        filtered = [
            row for row in rows
            if participant_id is None
            or not pick(row, 'participantId', 'participant_id')
            or pick(row, 'participantId', 'participant_id') == participant_id
        ]
        filtered.sort(
            key=lambda row: (
                float(pick(row, 'importance') or 0),
                dt_ms(parse_dt(pick(row, 'updatedAt', 'updated_at')) or self.now()),
            ),
            reverse=True,
        )
        return filtered[:limit]

    # ------------------------------------------------------------------ #
    # 管理视图（`src/service.ts:1277-1296`）
    # ------------------------------------------------------------------ #

    async def admin_facts(self, story_id: str, limit: int = 20) -> list[Any]:
        """上游 `adminFacts(storyId, limit = 20)`（`src/service.ts:1277`）：全局 + 关系专属事实。"""
        return await self.db_get(
            'interlude_fact', {'storyId': story_id, 'status': 'active'},
            {'limit': max(1, min(int(limit), 100)), 'sort': {'updatedAt': 'DESC'}},
        )

    async def admin_pending_intents(self, story_id: str, limit: int = 20) -> list[Any]:
        """上游 `adminPendingIntents(storyId, limit = 20)`（`src/service.ts:1284`）。"""
        return await self.db_get(
            'interlude_intent', {'storyId': story_id, 'status': 'pending'},
            {'limit': max(1, min(int(limit), 100)), 'sort': {'notBefore': 'ASC'}},
        )

    async def admin_state_patches(self, story_id: str, limit: int = 20) -> list[Any]:
        """上游 `adminStatePatches(storyId, limit = 20)`（`src/service.ts:1291`）。"""
        return await self.db_get(
            'interlude_state_patch', {'storyId': story_id},
            {'limit': max(1, min(int(limit), 100)), 'sort': {'createdAt': 'DESC'}},
        )

    # ------------------------------------------------------------------ #
    # 管理写入（`src/service.ts:1299-1345`）
    # ------------------------------------------------------------------ #

    async def add_admin_script_note(self, story: Any, content: Any) -> bool:
        """上游 `addAdminScriptNote(story, content)`（`src/service.ts:1299`）。

        追加一条审计可见的系统注记，不假装它来自模型。空文本直接返回 `False`。
        """
        story_id = pick(story, 'id')
        limit = int(_config_limit(self.runtime_config, 'maxScriptCharacters', 'max_script_characters', 8_000))
        text = clip(content, limit)
        if not text:
            return False
        now = self.now()
        await self.append_entry(story_id, {
            'kind': 'admin-note', 'actor': 'system', 'content': '[管理员注记] %s' % text,
            'occurredAt': iso(now), 'metadata': {'source': 'administrator'},
        }, now)
        self.schedule_compaction(story_id)
        return True

    async def add_admin_fact(self, story: Any, scope: str, content: Any) -> bool:
        """上游 `addAdminFact(story, scope, content)`（`src/service.ts:1312`）。

        为「必须活过压缩」的修正写入一条高置信事实（`importance=0.8`、`confidence=1`）。
        """
        story_id = pick(story, 'id')
        limit = int(_config_limit(self.memory_config, 'factContentCharacters', 'fact_content_characters', 4_000))
        text = clip(content, limit)
        if not text:
            return False
        now = self.now()
        await self.db_create('interlude_fact', {
            'storyId': story_id, 'participantId': '', 'scope': scope, 'content': text,
            'importance': 0.8, 'confidence': 1, 'unresolved': False,
            'embedding': await self.embed_text(text),
            'status': 'active', 'sourceEntryIds': [], 'lastSeenAt': now,
            'createdAt': now, 'updatedAt': now,
        })
        return True

    async def forget_admin_fact(self, story_id: str, fact_id: int) -> bool:
        """上游 `forgetAdminFact(storyId, id)`（`src/service.ts:1325`）。

        可逆删除：事实保留为 `superseded` 行以便审计。
        """
        rows = await self.db_get(
            'interlude_fact', {'id': fact_id, 'storyId': story_id, 'status': 'active'},
        )
        if not rows:
            return False
        await self.db_set('interlude_fact', {'id': fact_id}, {'status': 'superseded', 'updatedAt': self.now()})
        return True

    async def cancel_admin_intent(self, story_id: str, intent_id: int) -> bool:
        """上游 `cancelAdminIntent(storyId, id)`（`src/service.ts:1332`）。"""
        rows = await self.db_get(
            'interlude_intent', {'id': intent_id, 'storyId': story_id, 'status': 'pending'},
        )
        if not rows:
            return False
        await self.db_set('interlude_intent', {'id': intent_id}, {'status': 'cancelled', 'updatedAt': self.now()})
        return True

    async def reject_admin_state_patch(self, story_id: str, patch_id: int) -> bool:
        """上游 `rejectAdminStatePatch(storyId, id)`（`src/service.ts:1339`）。"""
        rows = await self.db_get(
            'interlude_state_patch', {'id': patch_id, 'storyId': story_id, 'status': 'proposed'},
        )
        if not rows:
            return False
        # 上游只改 status：拒绝不是一次内容变更，故不刷新 updatedAt（该表也没有这一列）。
        await self.db_set('interlude_state_patch', {'id': patch_id}, {'status': 'rejected'})
        return True

    # ------------------------------------------------------------------ #
    # 设定覆盖清理与时间线重置（`src/service.ts:1347-1429`）
    # ------------------------------------------------------------------ #

    async def clear_setting_overlay(self, story: Any, target: str) -> dict[str, Any]:
        """上游 `clearSettingOverlay(story, target)`（`src/service.ts:1347`）。

        只清演化的 overlay，保留 Canon、剧本与记忆；先让缓冲叙事失效，再按故事串行执行。
        """
        story_id = pick(story, 'id')
        self.invalidate_buffered_narratives(story_id)

        async def task() -> dict[str, Any]:
            current = await self.get_story(story_id)
            return await self.clear_setting_overlay_unlocked(current, target)

        return await self.serial(story_id, task)

    async def rebase_timeline(self, story: Any) -> dict[str, Any]:
        """上游 `rebaseTimeline(story)`（`src/service.ts:1355`）。

        在不删除历史归档的前提下开启一段干净的、宿主拥有的时间线：用于从
        「散文即权威」的旧版本升级后，清理可能已经混进 active scene / 草稿的未来污染。
        """
        story_id = pick(story, 'id')
        self.invalidate_buffered_narratives(story_id)

        async def task() -> dict[str, Any]:
            current = await self.get_story(story_id)
            if not current:
                raise RuntimeError('剧本不存在，无法重置时间线。')
            current_id = pick(current, 'id')
            now = self.now()
            latest = await self.db_get(
                'interlude_script_entry', {'storyId': current_id},
                {'limit': 1, 'sort': {'id': 'DESC'}},
            )
            active_scene = await self.active_scene(current_id)
            if active_scene:
                latest_id = pick(latest[0], 'id') if latest else None
                if latest_id is None:
                    latest_id = pick(active_scene, 'lastEntryId', 'last_entry_id')
                await self.db_set('interlude_scene', {'id': pick(active_scene, 'id')}, {
                    'hook': 'Host timeline rebased at %s.' % format_log_time(
                        now, pick(pick(current, 'setting'), 'timezone'),
                    ),
                    'summary': (
                        'The host resumed the current timeline here. Earlier script remains '
                        'archived context; no future statement from it is an event after this point.'
                    ),
                    'lastEntryId': latest_id,
                    'entryCount': 0,
                    'updatedAt': now,
                })
            state = dict(decode_story_state(pick(current, 'state')))
            # 上游 `{ ...state, workingDetails: [], timelineCarry: [],
            # continuitySnapshot: undefined, continuityDirty: true }`：
            # `undefined` ≡ 删除该键（Python 侧不用 `None` 顶替）。
            state.pop('continuity_snapshot', None)
            state.pop('continuitySnapshot', None)
            state['working_details'] = []
            state['timeline_carry'] = []
            state['continuity_dirty'] = True
            await self.db_set('interlude_story', {'id': current_id}, {
                'state': encode_story_state(state), 'cursorAt': now, 'updatedAt': now,
            })
            await self.append_entry(current_id, {
                'kind': 'timeline-rebase', 'actor': 'system',
                'content': (
                    'Host timeline rebased. Earlier narrative prose remains an archive '
                    'and no longer defines future events.'
                ),
                'occurredAt': iso(now), 'metadata': {'timelineRebase': True},
            }, now)
            return {'at': now, 'sceneReset': bool(active_scene)}

        return await self.serial(story_id, task)

    async def clear_setting_overlay_unlocked(self, story: Any, target: str) -> dict[str, Any]:
        """上游 `clearSettingOverlayUnlocked(story, target)`（`src/service.ts:1383`）。

        清理 overlay、关系覆盖，并把**活跃 overlay 行与待定候选**一并失效：
        提案本身保留作审计，否则清理前创建的候选日后仍会被应用，悄悄复活旧人格/旧关系。
        `target` 取值 `character` / `perspective` / `relationship` / `world` / `all`。
        """
        if not story:
            raise RuntimeError('剧本不存在，无法清理设定覆盖。')
        story_id = pick(story, 'id')
        now = self.now()
        state_value = pick(story, 'state')
        overlay = dict(pick(state_value, 'settingOverlay', 'setting_overlay') or {})
        if target in ('character', 'all'):
            overlay.pop('characterProfile', None)
            overlay.pop('character_profile', None)
            overlay['character_traits'] = []
            overlay.pop('characterTraits', None)
        if target in ('perspective', 'all'):
            overlay.pop('perspective', None)
        if target in ('relationship', 'all'):
            overlay.pop('relationship', None)
        if target in ('world', 'all'):
            overlay.pop('world', None)
        merged_state = dict(decode_story_state(state_value))
        merged_state['setting_overlay'] = overlay
        await self.db_set('interlude_story', {'id': story_id}, {
            'state': encode_story_state(merged_state), 'updatedAt': now,
        })

        participant_count = 0
        if target in ('relationship', 'all'):
            participants = await self.participants(story_id, True)
            for participant in participants:
                # helpers 的 normalize_participant_state 输出 camelCase（持久化 wire format）。
                state = normalize_participant_state(pick(participant, 'state'))
                if not pick(state, 'relationshipOverlay', 'relationship_overlay'):
                    continue
                participant_count += 1
                state.pop('relationshipOverlay', None)
                state.pop('relationship_overlay', None)
                await self.db_set('interlude_participant', {'id': pick(participant, 'id')}, {
                    'state': state, 'updatedAt': now,
                })

        patches = await self.db_get('interlude_state_patch', {'storyId': story_id})
        for patch in patches:
            if pick(patch, 'status') not in ('proposed', 'applied', 'compacted'):
                continue
            if target != 'all' and pick(patch, 'target') != target:
                continue
            await self.db_set('interlude_state_patch', {'id': pick(patch, 'id')}, {'status': 'cleared'})

        snapshots = await self.db_get(
            'interlude_overlay_snapshot', {'storyId': story_id, 'status': 'active'},
        )
        for snapshot in snapshots:
            if target != 'all' and pick(snapshot, 'target') != target:
                continue
            await self.db_set('interlude_overlay_snapshot', {'id': pick(snapshot, 'id')}, {
                'status': 'superseded', 'updatedAt': now,
            })
        return {'participantCount': participant_count}

    # ------------------------------------------------------------------ #
    # 清库与区间清除（`src/service.ts:1431-1601`）
    # ------------------------------------------------------------------ #

    async def purge_all_story_data(self, story_id: str) -> None:
        """上游 `purgeAllStoryData(storyId)`（`src/service.ts:1431`）。

        破坏性管理操作（调用方必须先校验确认短语）。全量清除后还会用**当前 Console
        配置**重建 Canon，旧档案因此不可能在后续 prompt 里复活。
        """
        self.invalidate_buffered_narratives(story_id)
        await self.purge_table('interlude_script_entry', {'storyId': story_id}, {
            'kind': 'redacted', 'actor': 'system', 'content': '[管理员已删除剧本内容]',
            'metadata': {'redacted': True},
        })
        await self.purge_table('interlude_memory', {'storyId': story_id}, {
            'status': 'deleted', 'content': '[管理员已删除记忆]',
        })
        await self.purge_table('interlude_intent', {'storyId': story_id}, {
            'status': 'cancelled', 'summary': '[管理员已取消意图]',
        })
        await self.purge_table('interlude_scene', {'storyId': story_id}, {
            'status': 'closed', 'hook': '', 'summary': '', 'entryCount': 0,
        })
        await self.purge_table('interlude_arc', {'storyId': story_id}, {
            'status': 'closed', 'summary': '', 'sceneCount': 0,
        })
        await self.purge_table('interlude_fact', {'storyId': story_id}, {
            'status': 'superseded', 'content': '[管理员已删除事实]',
        })
        await self.purge_table('interlude_state_patch', {'storyId': story_id}, {
            'status': 'rejected', 'proposedValue': '[管理员已删除提案]', 'evidence': '',
        })
        await self.purge_table('interlude_overlay_snapshot', {'storyId': story_id}, {
            'status': 'superseded', 'summary': '[管理员已删除 overlay 归档]',
            'majorEvents': [], 'sourcePatchIds': [],
        })
        await self.purge_table('interlude_web_observation', {'storyId': story_id}, {
            'status': 'deleted', 'url': '', 'title': '', 'excerpt': '',
            'summary': '[管理员已删除网页观察]',
        })
        await self.purge_table('interlude_schedule_preplan', {'storyId': story_id}, {
            'regimes': [], 'exceptions': [], 'materializedDays': [],
            'validFrom': '1970-01-01', 'validThrough': '1970-01-01',
            'lastReviewedLocalDate': '', 'reviewReason': '[管理员已删除 Schedule Preplan]',
        })
        now = self.now()
        story = await self.get_story(story_id)
        setting = self.initial_story_setting()
        await self.db_set('interlude_story', {'id': story_id}, {
            'setting': setting, 'state': empty_story_state(), 'cursorAt': now, 'updatedAt': now,
        })
        await self.reset_participant_canon(story_id, now)
        rebuilt = dict(story or {})
        rebuilt.update({'setting': setting, 'state': empty_story_state(), 'cursorAt': now})
        await self.ensure_continuity(rebuilt, now)

    async def purge_all_data(self, preferred_story_id: Optional[str] = None) -> Optional[str]:
        """上游 `purgeAllData(preferredStoryId?)`（`src/service.ts:1456`）。

        重置所有平台，最终只保留**恰好一部**空白的全局 canonical 剧本，并返回它的 id。
        """
        all_stories = await self.db_get('interlude_story', {}, {'sort': {'updatedAt': 'DESC'}})
        active = [story for story in all_stories if pick(story, 'status') == 'active']
        if not active:
            return None
        canonical = None
        if preferred_story_id:
            for story in active:
                if pick(story, 'id') == preferred_story_id:
                    canonical = story
                    break
        if canonical is None:
            canonical = active[0]
        canonical_id = pick(canonical, 'id')
        for story in all_stories:
            await self.purge_all_story_data(pick(story, 'id'))
        now = self.now()
        for story in all_stories:
            if pick(story, 'id') == canonical_id:
                continue
            await self.db_set('interlude_story', {'id': pick(story, 'id')}, {
                'status': 'archived', 'updatedAt': now,
            })
        return canonical_id

    async def purge_platform_data(self, platform: str) -> int:
        """上游 `purgePlatformData(platform)`（`src/service.ts:1471`）。

        只删某个适配器/平台族的记录，不碰其它平台；返回被处理的剧本数。
        """
        all_stories = await self.db_get('interlude_story', {}, {'sort': {'updatedAt': 'DESC'}})
        targets = [
            story for story in all_stories
            if _same_platform_family(pick(story, 'platform'), platform)
        ]
        for story in targets:
            await self.purge_all_story_data(pick(story, 'id'))
            await self.db_set('interlude_story', {'id': pick(story, 'id')}, {
                'status': 'archived', 'updatedAt': self.now(),
            })
        return len(targets)

    async def clear_database(self) -> dict[str, Any]:
        """上游 `clearDatabase()`（`src/service.ts:1486`）。

        只清 HDSI 自有的表：Koishi 的 users/channels 与其它插件刻意不动；在驱动仍打开的
        情况下从命令里删物理 SQLite 文件是不安全的。单次清空有重入闸门。
        """
        if self.database_resetting:
            raise RuntimeError('HDSI 数据库清空已经在进行中。')
        self.database_resetting = True
        self.invalidate_buffered_narratives()
        self.invalidate_history_vectors()
        try:
            removed = 0
            logically_cleared = 0
            for table in _CLEAR_DATABASE_TABLES:
                rows = await self.db_get(table, {})
                if not rows:
                    continue
                removed += len(rows)
                try:
                    await self.db_remove(table, {})
                except Exception as error:
                    # 保留既有的磁盘 I/O 兜底：内容被涂掉、剧本被归档，
                    # 于是被锁住的 sqlite 文件无法让旧故事复活。
                    self.report_standalone('warn', 'SQLite 清空表失败，改用逻辑清空 表=%s 错误=%s', table, error)
                    for row in rows:
                        key = (
                            {'storyId': pick(row, 'storyId', 'story_id')}
                            if table == 'interlude_schedule_preplan'
                            else {'id': pick(row, 'id')}
                        )
                        if table == 'interlude_story':
                            fallback = {
                                'status': 'archived', 'setting': self.initial_story_setting(),
                                'state': empty_story_state(),
                            }
                        elif table == 'interlude_participant':
                            fallback = {
                                'status': 'paused', 'profile': '', 'relationship': '',
                                'state': empty_participant_state(),
                            }
                        else:
                            fallback = _clear_database_fallback(table)
                        await self.db_set(table, key, fallback)
                        logically_cleared += 1
            return {'removed': removed, 'logicallyCleared': logically_cleared}
        finally:
            self.database_resetting = False

    async def purge_story_range(self, story_id: str, from_value: Any, to_value: Any) -> None:
        """上游 `purgeStoryRange(storyId, from, to)`（`src/service.ts:1543`）。

        删除时间戳与区间重叠的剧本行与派生记忆记录（共同退化为软删墓碑）。
        """
        start = parse_dt(from_value)
        end = parse_dt(to_value)
        self.invalidate_buffered_narratives(story_id)
        self.invalidate_history_vectors(story_id)

        def in_range(value: Any) -> bool:
            parsed = parse_dt(value)
            return parsed is not None and start is not None and end is not None and start <= parsed <= end

        entries = await self.db_get('interlude_script_entry', {'storyId': story_id})
        entry_ids = {
            pick(entry, 'id') for entry in entries
            if in_range(pick(entry, 'occurredAt', 'occurred_at'))
        }
        for entry in entries:
            if pick(entry, 'id') in entry_ids:
                await self.purge_table('interlude_script_entry', {'id': pick(entry, 'id')}, {
                    'kind': 'redacted', 'actor': 'system', 'content': '[管理员已删除剧本内容]',
                    'metadata': {'redacted': True},
                })

        memories = await self.db_get('interlude_memory', {'storyId': story_id})
        for memory in memories:
            source_entry_id = pick(memory, 'sourceEntryId', 'source_entry_id')
            sourced = source_entry_id is not None and source_entry_id in entry_ids
            if in_range(pick(memory, 'createdAt', 'created_at')) or sourced:
                await self.purge_table('interlude_memory', {'id': pick(memory, 'id')}, {
                    'status': 'deleted', 'content': '[管理员已删除记忆]',
                })

        facts = await self.db_get('interlude_fact', {'storyId': story_id})
        for fact in facts:
            source_ids = pick(fact, 'sourceEntryIds', 'source_entry_ids') or []
            sourced = any(item in entry_ids for item in source_ids)
            if (
                in_range(pick(fact, 'createdAt', 'created_at'))
                or in_range(pick(fact, 'updatedAt', 'updated_at'))
                or in_range(pick(fact, 'lastSeenAt', 'last_seen_at'))
                or sourced
            ):
                await self.purge_table('interlude_fact', {'id': pick(fact, 'id')}, {
                    'status': 'superseded', 'content': '[管理员已删除事实]',
                })

        intents = await self.db_get('interlude_intent', {'storyId': story_id})
        for intent in intents:
            if (
                in_range(pick(intent, 'createdAt', 'created_at'))
                or in_range(pick(intent, 'notBefore', 'not_before'))
                or in_range(pick(intent, 'updatedAt', 'updated_at'))
            ):
                await self.purge_table('interlude_intent', {'id': pick(intent, 'id')}, {
                    'status': 'cancelled', 'summary': '[管理员已取消意图]',
                })

        start_ms = dt_ms(start) if start is not None else 0
        end_ms = dt_ms(end) if end is not None else 0
        scenes = await self.db_get('interlude_scene', {'storyId': story_id})
        for scene in scenes:
            # 上游：`scene.startedAt <= to && (!scene.endedAt || scene.endedAt >= from)`。
            started = parse_dt(pick(scene, 'startedAt', 'started_at'))
            ended = parse_dt(pick(scene, 'endedAt', 'ended_at'))
            overlaps = (dt_ms(started) if started is not None else 0) <= end_ms \
                and (ended is None or dt_ms(ended) >= start_ms)
            if overlaps:
                await self.purge_table('interlude_scene', {'id': pick(scene, 'id')}, {
                    'status': 'closed', 'hook': '', 'summary': '', 'entryCount': 0,
                })

        arcs = await self.db_get('interlude_arc', {'storyId': story_id})
        for arc in arcs:
            if in_range(pick(arc, 'createdAt', 'created_at')) or in_range(pick(arc, 'updatedAt', 'updated_at')):
                await self.purge_table('interlude_arc', {'id': pick(arc, 'id')}, {
                    'status': 'closed', 'summary': '', 'sceneCount': 0,
                })

        patches = await self.db_get('interlude_state_patch', {'storyId': story_id})
        for patch in patches:
            if in_range(pick(patch, 'createdAt', 'created_at')) or in_range(pick(patch, 'appliedAt', 'applied_at')):
                await self.purge_table('interlude_state_patch', {'id': pick(patch, 'id')}, {
                    'status': 'rejected', 'proposedValue': '[管理员已删除提案]', 'evidence': '',
                })

        observations = await self.db_get('interlude_web_observation', {'storyId': story_id})
        for observation in observations:
            if in_range(pick(observation, 'createdAt', 'created_at')) \
                    or in_range(pick(observation, 'accessedAt', 'accessed_at')):
                await self.purge_table('interlude_web_observation', {'id': pick(observation, 'id')}, {
                    'status': 'deleted', 'url': '', 'title': '', 'excerpt': '',
                    'summary': '[管理员已删除网页观察]',
                })

        if entry_ids:
            await self.db_set('interlude_schedule_preplan', {'storyId': story_id}, {
                'lastReviewedLocalDate': '', 'validThrough': '1970-01-01',
                'reviewReason': 'Source range was purged; Schedule Preplan requires review.',
                'updatedAt': self.now(),
            })

        story = await self.get_story(story_id)
        await self.ensure_continuity(story, self.now())

    # ------------------------------------------------------------------ #
    # 入站主入口（`src/service.ts:1603-1718`）
    # ------------------------------------------------------------------ #

    async def receive_group(self, session: Any, received_at: Any = None) -> bool:
        """上游 `receiveGroup(session, receivedAt = new Date())`（`src/service.ts:1605`）。

        已配置的 OneBot 群聊入口：群成员**不需要**私聊授权，访问由群白名单控制。
        `mention-only` 模式下未被 @ 的消息直接丢弃。消息先按故事串行落库，
        再进入群回合缓冲（debounce 后由 `flush_group_turn` 处理）。
        """
        if self.database_resetting or not self.can_handle_group_session(session):
            return False
        group_id = self._session_group_id(session)
        rule = self.group_rule(group_id)
        if not rule:
            return False
        mentioned_bot = _mentions_bot(session)
        quoted_bot = _quotes_bot(session)
        if pick(rule, 'responseMode', 'response_mode') == 'mention-only' and not mentioned_bot:
            return False
        story = await self.find_story(session)
        if not story and bool(_config_value(self.runtime_config, 'autoCreate', 'auto_create', False)):
            story = await self.create_story(session)
        if not story or pick(story, 'status') != 'active':
            return False
        now = parse_dt(received_at) or self.now()
        story_id = pick(story, 'id')
        sender_id = normalize_account_id(_session_read(session, 'userId', 'user_id'))
        sender_name = await self.group_sender_name(group_id, sender_id, session)
        character = pick(pick(story, 'setting'), 'character') or {}
        quote = describe_quoted_message(session, pick(character, 'name') or '主角')
        message_content = describe_group_attachments(_session_read(session, 'content'))

        async def task() -> Any:
            current = await self.get_story(story_id)
            if not current:
                raise RuntimeError('剧本不存在，无法记录群消息。')
            metadata: dict[str, Any] = {
                'groupId': group_id, 'senderId': sender_id, 'senderName': sender_name,
                'channelId': _session_read(session, 'channelId', 'channel_id'),
                'messageId': _session_read(session, 'messageId', 'message_id'),
            }
            if quote:
                metadata['quote'] = quote
            return await self.append_entry(story_id, {
                'kind': 'group-message', 'actor': 'user', 'content': message_content,
                'occurredAt': iso(now), 'metadata': metadata,
            }, now)

        accepted = await self.serial(story_id, task)
        message_id = _targetable_message_id(_session_read(session, 'messageId', 'message_id'))
        message: dict[str, Any] = {
            'senderId': sender_id, 'senderName': sender_name,
            'speaker': format_group_speaker(sender_name, sender_id),
        }
        if message_id:
            message['messageId'] = message_id
            message['messageRef'] = _group_message_ref(pick(accepted, 'id'))
        if quote:
            message['quote'] = quote
        message['content'] = message_content
        message['occurredAt'] = now
        message['direction'] = 'user'
        self.buffer_group_message(story, rule, session, message, mentioned_bot, quoted_bot)
        self.report_operation(
            'summary', 'info', story, 'user-message',
            '收到群聊消息 群=%s 发送者=%s', group_id, sender_id,
        )
        return True

    async def receive(self, session: Any, received_at: Any = None) -> bool:
        """上游 `receive(session, receivedAt = new Date())`（`src/service.ts:1645`）。

        **私聊入站主入口**。权限在 find/create **之前**判定，因此未授权的 QQ 仅靠发一条
        私聊既不能触发模型，也无法创建持久剧本。

        时间在等待故事队列**之前**同步标记（`signal_incoming_interruption`），
        这样刚到达的消息可以让「即将落库」的模型请求失效，也能让到期的拆分投递在
        真正发送前停住。
        """
        if self.database_resetting:
            return False
        if not self.can_handle_session(session):
            return False
        story = await self.find_story(session)
        if not story and bool(_config_value(self.runtime_config, 'autoCreate', 'auto_create', False)):
            story = await self.create_story(session)
        if not story or pick(story, 'status') != 'active':
            self.report_standalone_operation(
                'diagnostic', 'debug',
                '私聊未处理：故事不存在或已暂停 平台=%s 机器人ID=%s 用户ID=%s',
                _session_read(session, 'platform'), _session_read(session, 'selfId', 'self_id'),
                _session_read(session, 'userId', 'user_id'),
            )
            return False
        story_id = pick(story, 'id')
        now = parse_dt(received_at) or self.now()
        participant = await self.find_participant(session, story)
        if participant:
            # 白名单行可能在这个 QQ 首次加入共享剧本之后被编辑过：
            # 组装模型上下文之前先刷新当前关系分支（无变化时 ensureParticipant 不写库）。
            participant = await self.ensure_participant(story, session, now, participant)
        elif bool(_config_value(self.runtime_config, 'autoCreate', 'auto_create', False)) \
                or bool(pick(self.shared_story_config, 'autoEnrollParticipants', 'auto_enroll_participants')):
            participant = await self.ensure_participant(story, session)
        if not participant or pick(participant, 'status') != 'active':
            self.report_operation(
                'diagnostic', 'debug', story, 'user-message',
                '私聊未处理：参与者不存在或已暂停 用户ID=%s', _session_read(session, 'userId', 'user_id'),
            )
            return False
        content = _session_read(session, 'content')
        if not str(content or '').strip() and not extract_session_voice_count(session):
            return False
        observed = self.describe_vision_event(session)
        if (
            not str(pick(observed, 'content') or '').strip()
            and not (pick(observed, 'sources') or [])
            and not extract_session_voice_count(session)
            and not extract_session_file_facts(session)
        ):
            return False
        self.signal_incoming_interruption(story, participant)
        user_input = self.describe_user_event(story, session)
        self.report_operation(
            'summary', 'info', story, 'user-message',
            '收到参与者私聊消息 参与者=%s', pick(participant, 'id'),
        )
        logging_config = _config_section(self.config, 'logging')
        if pick(logging_config, 'logMessageContent', 'log_message_content'):
            preview_length = int(_config_limit(logging_config, 'previewLength', 'preview_length', 500))
            self.report_operation(
                'diagnostic', 'info', story, 'user-message', '用户消息内容：%s',
                str(pick(user_input, 'content') or '')[:preview_length],
            )

        async def task() -> Optional[dict[str, Any]]:
            current = await self.get_story(story_id)
            current_participant = await self.get_participant(pick(participant, 'id'))
            if not current_participant or pick(current_participant, 'status') != 'active':
                return None
            incoming_participant = await self.record_incoming_message(current_participant, now)
            superseded = await self.cancel_pending_outgoing_messages(
                pick(current, 'id'),
                pick(incoming_participant, 'id'),
                now,
                bool(_config_value(
                    self.runtime_config,
                    'cancelDelayedRepliesOnUserMessage', 'cancel_delayed_replies_on_user_message', False,
                )),
            )
            user_content = pick(user_input, 'content')
            images = pick(user_input, 'sources') or []
            audio = pick(user_input, 'audioSources', 'audio_sources') or []
            quote = pick(user_input, 'quote')
            metadata: dict[str, Any] = {
                'platform': _session_read(session, 'platform'),
                'messageId': _session_read(session, 'messageId', 'message_id'),
                'personId': pick(incoming_participant, 'personId', 'person_id'),
            }
            if images:
                metadata['imageCount'] = len(images)
            if audio:
                metadata['audioCount'] = len(audio)
            if quote:
                metadata['quote'] = quote
            await self.append_entry(pick(current, 'id'), {
                'kind': 'user-message', 'actor': 'user', 'content': user_content,
                'occurredAt': iso(now), 'metadata': metadata,
            }, now, pick(incoming_participant, 'id'))
            # 消息在到达时即持久化；模型请求本身在下面被 debounce，
            # 因此一串消息可以合成一个连贯的写作回合。
            await self.pause_automatic_advance_after_user_message(pick(current, 'id'), now)
            return {
                'story': current, 'participant': incoming_participant,
                'now': now, 'superseded': superseded,
            }

        accepted = await self.serial(story_id, task)
        if not accepted:
            return False
        self.buffer_user_narrative(
            accepted['story'], accepted['participant'], session, accepted['now'],
            accepted['superseded'], pick(user_input, 'content'),
            pick(user_input, 'sources') or [],
            pick(user_input, 'audioSources', 'audio_sources') or [],
            pick(user_input, 'quote'),
        )
        images = pick(user_input, 'sources') or []
        audio = pick(user_input, 'audioSources', 'audio_sources') or []
        if images:
            vision_config = _config_section(_config_section(self.config, 'model'), 'vision')
            self.report_operation(
                'standard', 'info', accepted['story'], 'user-message',
                '当前事件包含图片附件 数量=%d 原生识图=%s', len(images),
                '开启' if pick(vision_config, 'enabled') else '关闭',
            )
        if audio:
            self.report_operation(
                'standard', 'info', accepted['story'], 'user-message',
                '当前事件包含语音附件 数量=%d 原生音频=%s', len(audio),
                '开启' if pick(self.audio_config, 'enabled') else '关闭',
            )
        self.report_operation(
            'standard', 'info', accepted['story'], 'user-message',
            '用户回合已入队 参与者=%s 已取消旧计划=%d',
            pick(accepted['participant'], 'id'), len(accepted['superseded'] or []),
        )
        return True

    # ------------------------------------------------------------------ #
    # 群成员名缓存（`src/service.ts:1720-1748`）
    # ------------------------------------------------------------------ #

    async def group_sender_name(self, group_id: str, user_id: str, session: Any) -> str:
        """上游 `groupSenderName(groupId, userId, session)`（`src/service.ts:1720`）。

        顺序：账号规则 label → 会话观察到的昵称 → 12 小时缓存 → 平台查询 → 回落 userId。
        同一 key 的并发查询共用一个 in-flight 任务（等价上游 `Map<string, Promise>`）。
        """
        account = self.user_account_rule(user_id)
        author = _session_read(session, 'author')
        author = author if isinstance(author, dict) else {}
        observed = _normalize_group_display_name(
            pick(account, 'label'), pick(author, 'nick'),
            _session_read(session, 'username'), pick(author, 'name'), pick(author, 'username'),
        )
        if observed:
            return observed

        key = '%s:%s' % (normalize_group_id(group_id), user_id)
        cached = self.group_member_name_cache.get(key)
        if cached and float(pick(cached, 'expiresAt', 'expires_at') or 0) > self.now_ms():
            return pick(cached, 'name') or ''

        pending = self.group_member_name_lookups.get(key)
        if pending is None:
            # 用 Task 而不是裸协程：上游那个 Promise 可以被多个并发调用者 await。
            pending = asyncio.ensure_future(self.lookup_group_member_name(
                key, group_id, user_id, _session_read(session, 'selfId', 'self_id'),
            ))
        self.group_member_name_lookups[key] = pending
        try:
            name = await pending
            return name or user_id
        finally:
            self.group_member_name_lookups.pop(key, None)

    async def lookup_group_member_name(self, cache_key: str, group_id: str, user_id: str, self_id: str) -> str:
        """上游 `lookupGroupMemberName(cacheKey, groupId, userId, selfId)`（`src/service.ts:1735`）。

        上游在这里直接找 OneBot 机器人并调 `bot.getGuildMember`；本移植版按
        `docs/PORT_PLAN_SERVICE.md` §7 把平台调用收敛到 `transport.fetch_member_name`
        （「本平台到底有没有群成员查询能力」由适配器回答：不支持时返回 `''`）。
        查询成功则写入 12 小时缓存。
        """
        fetch = getattr(self.transport, 'fetch_member_name', None)
        if not callable(fetch):
            return ''
        try:
            name = _normalize_group_display_name(
                await fetch(normalize_group_id(group_id), user_id),
            )
        except Exception:
            return ''
        if not name:
            return ''
        self.group_member_name_cache[cache_key] = {
            'name': name, 'expires_at': self.now_ms() + 12 * _HOUR_MS,
        }
        return name

    # ------------------------------------------------------------------ #
    # 群回合缓冲与刷出（`src/service.ts:1750-1927`）
    # ------------------------------------------------------------------ #

    def buffer_group_message(
        self,
        story: Any,
        rule: Any,
        session: Any,
        message: dict[str, Any],
        mentioned_bot: bool,
        quoted_bot: bool,
    ) -> None:
        """上游 `bufferGroupMessage(story, rule, session, message, mentionedBot, quotedBot)`
        （`src/service.ts:1750`）。

        按 `故事:群` 聚合一批消息，用 `debounceSeconds` 计时器 + 单调递增的 `revision`
        保证只有最后一次安排会真正刷出；@ 与引用机器人的事实是**累积**的。
        """
        story_id = pick(story, 'id')
        group_id = normalize_group_id(pick(rule, 'groupId', 'group_id'))
        key = '%s:%s' % (story_id, group_id)
        existing = self.buffered_group_turns.get(key)
        turn = existing if existing is not None else {
            'story_id': story_id, 'group_id': group_id, 'rule': rule,
            'channel_id': _session_read(session, 'channelId', 'channel_id'),
            'messages': [], 'revision': 0, 'mentioned_bot': False, 'quoted_bot': False,
        }
        if turn.get('timer'):
            turn['timer']()
        turn['channel_id'] = _session_read(session, 'channelId', 'channel_id')
        turn['latest_session'] = session
        turn.setdefault('messages', []).append(message)
        turn['mentioned_bot'] = bool(turn.get('mentioned_bot')) or bool(mentioned_bot)
        turn['quoted_bot'] = bool(turn.get('quoted_bot')) or bool(quoted_bot)
        revision = int(turn.get('revision') or 0) + 1
        turn['revision'] = revision
        debounce = _config_limit(rule, 'debounceSeconds', 'debounce_seconds', 1)
        delay = max(0.0, float(debounce if debounce is not None else 1)) * _SECOND_MS
        turn['timer'] = self.ctx.set_timeout(
            lambda: _spawn(self.flush_group_turn(key, revision)), delay,
        )
        self.buffered_group_turns[key] = turn

    async def flush_group_turn(self, key: str, revision: int) -> None:
        """上游 `flushGroupTurn(key, revision)`（`src/service.ts:1769`）。

        与私聊回合不同，群批次在模型请求开始之后**仍然可投递**：新到的群消息构成下一批，
        于是繁忙的群聊不会永久饿死主角的回复。整段流程：
        意愿判定 → 冷却检查 → 串行快照 → 主叙事 `try_decide` → 串行落库 `persist_decision`
        → 表态 / 群投递 / 贴纸 / 原生表情 → 消耗意愿分数 → 排期压缩。
        """
        turn = self.buffered_group_turns.get(key)
        if turn is None or int(turn.get('revision') or 0) != revision:
            return
        if self.database_resetting or self.desktop_runtime_phase == 'paused':
            return
        if turn.get('story_id') in self.narrating_stories:
            turn['timer'] = self.ctx.set_timeout(
                lambda: _spawn(self.flush_group_turn(key, revision)), 250,
            )
            return
        turn['timer'] = None
        batch = list(turn.get('messages') or [])
        del turn['messages'][:]
        if not batch:
            self.buffered_group_turns.pop(key, None)
            return

        story_id = turn.get('story_id')
        try:
            story = await self.get_story(story_id)
        except Exception as error:
            self.report_standalone('warn', '群聊回合读取剧本失败，已放弃本批消息 故事=%s 错误=%s', story_id, error)
            if not turn.get('messages') and not turn.get('timer'):
                self.buffered_group_turns.pop(key, None)
            return
        if pick(story, 'status') != 'active':
            if not turn.get('messages') and not turn.get('timer'):
                self.buffered_group_turns.pop(key, None)
            return

        rule = turn.get('rule') or {}
        group_id = turn.get('group_id')
        willingness = evaluate_group_willingness(
            self.group_willingness.get(key),
            pick(rule, 'willingness'),
            {
                'now': self.now_ms(),
                'message_count': len(batch),
                'content': '\n'.join(str(pick(item, 'content') or '') for item in batch),
                'mentioned_bot': bool(turn.get('mentioned_bot')),
                'quoted_bot': bool(turn.get('quoted_bot')),
            },
            rng=self.rng,
        )
        self.group_willingness[key] = willingness['state']
        turn['mentioned_bot'] = False
        turn['quoted_bot'] = False
        if not willingness['should_call']:
            self.report_operation(
                'diagnostic', 'debug', story, 'user-message',
                '群聊意愿未触发模型调用 群=%s 分数=%s 概率=%s 原因=%s', group_id,
                '%.3f' % willingness['state']['score'],
                '%.3f' % willingness['probability'],
                willingness['reason'],
            )
            if not turn.get('messages') and not turn.get('timer'):
                self.buffered_group_turns.pop(key, None)
            return
        if await self.group_cooldown_active(
            pick(story, 'id'), group_id, _config_limit(rule, 'cooldownSeconds', 'cooldown_seconds', 1),
        ):
            self.report_operation(
                'diagnostic', 'debug', story, 'user-message',
                '群聊仍在冷却期，跳过群发言 群=%s', group_id,
            )
            if not turn.get('messages') and not turn.get('timer'):
                self.buffered_group_turns.pop(key, None)
            return
        self.report_operation(
            'standard', 'info', story, 'user-message',
            '群聊消息准备进入主叙事 群=%s 模式=%s 意愿=%s', group_id,
            pick(rule, 'responseMode', 'response_mode'), '%.3f' % willingness['state']['score'],
        )

        self.narrating_stories.add(story_id)
        try:
            async def snapshot_task() -> dict[str, Any]:
                current = await self.get_story(pick(story, 'id'))
                if not current:
                    raise RuntimeError('剧本不存在，无法刷出群回合。')
                context_messages = await self.group_messages(
                    pick(current, 'id'), group_id, _config_limit(rule, 'contextLimit', 'context_limit', 20),
                )
                current_now = self.now()
                return {
                    'story': current,
                    'from': narrative_cursor(current, current_now),
                    'now': current_now,
                    'contextMessages': context_messages,
                }

            snapshot = await self.serial(pick(story, 'id'), snapshot_task)
            # 上游这里构造的就是模型 payload 的形状 → camelCase（键名法）。
            context_messages = _group_context_messages(snapshot['contextMessages'])
            group_context: dict[str, Any] = {
                'groupId': group_id,
                'channelId': turn.get('channel_id'),
                'label': pick(rule, 'label'),
                'purpose': pick(rule, 'purpose'),
                'characterRole': pick(rule, 'characterRole', 'character_role'),
                'messages': context_messages,
            }
            chat_capabilities = _chat_capabilities_wire(
                self.group_chat_capabilities(turn.get('latest_session'), context_messages),
            )
            user_message = '\n\n'.join(
                '[群聊连续消息 %d｜%s]\n%s' % (index + 1, pick(item, 'speaker'), pick(item, 'content'))
                for index, item in enumerate(batch)
            )
            if self.semantic_turn_embedding_enabled():
                embedding_config = _config_section(_config_section(self.config, 'model'), 'embedding')
                embedding_limit = int(_config_limit(
                    embedding_config, 'maxInputCharacters', 'max_input_characters', 4_000,
                ))
                turn_query_embedding: Optional[list[float]] = await self.embed_text(
                    user_message[:embedding_limit],
                )
            else:
                turn_query_embedding = None
            sticker_catalog = await self.sticker_catalog_for_session(
                turn.get('latest_session'), turn_query_embedding,
            )
            decision_result = await self.try_decide(
                snapshot['story'], None, 'user-message', snapshot['from'], snapshot['now'],
                user_message, [], [], group_context, [], [], chat_capabilities, [],
                sticker_catalog, turn_query_embedding,
            )
            decision = pick(decision_result, 'decision') or {}
            succeeded = bool(pick(decision_result, 'succeeded'))
            chat_actions = normalize_group_chat_actions(decision, chat_capabilities, group_context)
            sticker = self.resolve_sticker(pick(decision, 'localMedia', 'local_media'), sticker_catalog)
            native_face = None if sticker else self.resolve_native_face(decision, chat_capabilities)

            async def persist_task() -> dict[str, Any]:
                if self.database_resetting or not succeeded:
                    return {
                        'content': '', 'messages': [], 'chat_actions': {'reactions': []},
                        'sticker': None, 'native_face': None, 'commit': None, 'script_entry': None,
                    }
                current = await self.get_story(pick(story, 'id'))
                if not current:
                    raise RuntimeError('剧本不存在，无法落库群回合决策。')
                patched = dict(decision)
                patched['messageReactions'] = [
                    {'messageRef': item.get('messageRef'), 'reaction': item.get('reaction')}
                    for item in (chat_actions.get('reactions') or [])
                ]
                # 上游 `localMedia: sticker ? decision.localMedia : undefined`：
                # 没有选中贴纸时该键必须消失，否则承诺了不存在的投递。
                if sticker:
                    patched['localMedia'] = pick(decision, 'localMedia', 'local_media')
                else:
                    patched.pop('localMedia', None)
                    patched.pop('local_media', None)
                if native_face:
                    patched['nativeFace'] = pick(decision, 'nativeFace', 'native_face')
                else:
                    patched.pop('nativeFace', None)
                    patched.pop('native_face', None)
                persisted = await self.persist_decision(
                    current, None, patched, snapshot['from'], snapshot['now'], False, 'user-message',
                )
                content = normalize_group_visible_reply(
                    pick(decision, 'groupReply', 'group_reply'),
                    pick(decision, 'interaction'),
                    _message_characters(self.runtime_config),
                    str(_config_limit(self.runtime_config, 'messageSeparator', 'message_separator', '<sep/>')),
                )
                await self.db_set('interlude_story', {'id': pick(current, 'id')}, {
                    'cursorAt': snapshot['now'], 'updatedAt': self.now(),
                })
                if succeeded:
                    await self.schedule_conversation_follow_ups_after_turn(
                        pick(current, 'id'), snapshot['now'], pick(decision, 'interaction'),
                    )
                return {
                    'content': content,
                    'messages': pick(persisted, 'messages') or [],
                    'chat_actions': chat_actions,
                    'sticker': sticker,
                    'native_face': native_face,
                    'commit': pick(persisted, 'commit'),
                    'script_entry': pick(persisted, 'scriptEntry', 'script_entry'),
                }

            result = await self.serial(pick(story, 'id'), persist_task)
            platform_entry_id = pick(result['script_entry'], 'id')

            def reference_for(reaction: dict[str, Any]) -> Any:
                return platform_action_reference(
                    result['commit'], platform_entry_id, 'message-reaction',
                    '%s:%s' % (reaction.get('messageRef'), reaction.get('reaction')),
                )

            completed_reactions = (
                await self.execute_group_reactions(
                    snapshot['story'], turn.get('latest_session'), group_id,
                    result['chat_actions'].get('reactions') or [], reference_for,
                )
                if (result['chat_actions'].get('reactions') and turn.get('latest_session'))
                else 0
            )
            if result['content']:
                group_delivery = await self.send_group_message(
                    snapshot['story'], turn.get('channel_id'), result['content'],
                    pick(result['chat_actions'].get('replyTo'), 'messageId', 'message_id'),
                    turn.get('latest_session'),
                )
            else:
                group_delivery = {'deliveredSegments': [], 'complete': False, 'segmentOutcomes': []}
            delivered_segments = pick(group_delivery, 'deliveredSegments', 'delivered_segments') or []
            segment_outcomes = pick(group_delivery, 'segmentOutcomes', 'segment_outcomes') or []
            if segment_outcomes or delivered_segments:
                async def record_task() -> None:
                    current = await self.get_story(story_id)
                    if not current:
                        return
                    current_id = pick(current, 'id')
                    recorded_at = self.now()
                    group_event = find_group_script_event(result['commit']) if result['commit'] else None
                    script_event = (
                        message_event_reference(group_event, 0, platform_entry_id)
                        if group_event else None
                    )
                    if script_event:
                        for outcome in segment_outcomes:
                            await self.update_script_delivery_outcome(
                                current_id,
                                {**script_event, 'segment_index': pick(outcome, 'index')},
                                pick(outcome, 'status'), recorded_at, pick(outcome, 'reason'),
                            )
                    if delivered_segments:
                        metadata: dict[str, Any] = {
                            'groupId': group_id, 'channelId': turn.get('channel_id'),
                        }
                        if script_event:
                            metadata.update(script_event)
                        metadata['deliverySegmentIndexes'] = [
                            pick(item, 'index') for item in segment_outcomes
                            if pick(item, 'status') == 'delivered'
                        ]
                        if not pick(group_delivery, 'complete'):
                            metadata['partialDelivery'] = True
                            metadata['deliveredSegments'] = len(delivered_segments)
                        reply_to = pick(result['chat_actions'].get('replyTo'), 'messageRef', 'message_ref')
                        if reply_to:
                            metadata['replyTo'] = reply_to
                        await self.append_entry(current_id, {
                            'kind': 'character-group-message', 'actor': 'character',
                            'content': '<sep/>'.join(str(item) for item in delivered_segments),
                            'occurredAt': iso(recorded_at), 'metadata': metadata,
                        }, recorded_at)

                await self.serial(story_id, record_task)

            sticker_delivered = False
            if result['sticker'] and turn.get('latest_session'):
                sticker_delivered = await self.send_sticker(
                    snapshot['story'], turn.get('latest_session'), turn.get('channel_id'),
                    result['sticker'], group_id,
                    platform_action_reference(
                        result['commit'], platform_entry_id, 'local-media',
                        pick(result['sticker'], 'assetId', 'asset_id'),
                    ),
                )
            native_face_delivered = False
            if result['native_face'] and turn.get('latest_session'):
                native_face_delivered = await self.send_native_face(
                    snapshot['story'], turn.get('latest_session'), turn.get('channel_id'),
                    result['native_face'], group_id,
                    platform_action_reference(
                        result['commit'], platform_entry_id, 'native-face', result['native_face'],
                    ),
                )
            if delivered_segments or completed_reactions or sticker_delivered or native_face_delivered:
                self.group_willingness[key] = consume_group_willingness(
                    self.group_willingness.get(key), pick(rule, 'willingness'), self.now_ms(),
                )
            self.schedule_compaction(story_id)
        except Exception as error:
            self.report('warn', story, 'user-message', '群聊主叙事失败，保持静默 群=%s 错误=%s', group_id, error)
        finally:
            self.narrating_stories.discard(story_id)
            if not turn.get('messages') and not turn.get('timer'):
                self.buffered_group_turns.pop(key, None)
