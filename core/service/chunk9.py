"""Chunk9 mixin：`upstream/src/service.ts:6714-7084`（类体的最后一段）。

逐条对应上游成员、**保持上游顺序**：

| 上游行 | 上游成员 | 本文件 |
| --- | --- | --- |
| 6714 | `developmentForPrompt` | `development_for_prompt` |
| 6724 | `report` | `report` |
| 6731 | `reportOperation` | `report_operation` |
| 6736 | `writeReport` | `write_report` |
| 6757 | `reportStandalone` | `report_standalone` |
| 6763 | `reportTokenUsage` | `report_token_usage` |
| 6770 | `reportStandaloneOperation` | `report_standalone_operation` |
| 6775 | `writeStandalone` | `write_standalone` |
| 6792 | `resolveCompactionFacts` | `resolve_compaction_facts` |
| 6804 | `markContinuityDirty` | `mark_continuity_dirty` |
| 6811 | `emitLog` | `emit_log` |
| 6818 | `reportBlindModeHealth` | `report_blind_mode_health` |
| 6827 | `allowsVerbosity` | `allows_verbosity` |
| 6833 | `getStory` | `get_story` |
| 6851 | `serial` | `serial` |
| 6863 | `dbWrite` | `db_write` |
| 6874 | `dbRead` | `db_read` |
| 6893 | `dbGet` | `db_get` |
| 6903 | `repairCanonicalOneBotStoryTransport` | `repair_canonical_one_bot_story_transport` |
| 6914 | `retryDbWrite` | `retry_db_write` |
| 6939 | `dbCreate` | `db_create` |
| 6956 | `findPossiblyCommittedCreate` | `find_possibly_committed_create` |
| 6986 | `dbSet` | `db_set` |
| 6990 | `dbRemove` | `db_remove` |
| 6999 | `purgeTable` | `purge_table` |

## 与 `base.py` 的重叠（务必先读）

`base.py` 的作者把上游**同属本范围**的共享基础设施（`serial` / `dbRead` /
`dbGet` / `dbCreate` / `findPossiblyCommittedCreate` / `dbSet` / `dbRemove` /
`purgeTable`）提前实现进了 `ServiceBase`，好让 chunk0–chunk8 在 chunk9 落地前
就能跑。为了**既有上游顺序的成员清单、又不产生第二份会漂移的实现**，本文件对这
8 个成员写成**显式委托**（`super().xxx(...)`：MRO 上 `ServiceChunk9` 之后就是
`ServiceBase`），语义与上游逐条一致；其余 17 个成员是本文件的全量移植。

`allows_verbosity` / `emit_log` / `write_report` / `write_standalone` /
`report_standalone` / `report_standalone_operation` / `report_operation` 虽然在
`base.py` 里也有，但**本文件是权威实现**，理由有二：

1. `base.py` 用 `log_layered()` 渲染 —— 它**既渲染又投递 sink**，随后又走
   `emit_log`，配置了 `service_logger` 时同一条日志会输出两次。上游只有一条输出
   路径（渲染 → `serviceLogger`），本文件改用**纯渲染** `format_layered_log()`
   再由 `emit_log` 单点投递，与上游一致。
2. `emit_log` 在 `service_logger` 缺位时直接落到 `plugin.core.logging` 的 sink
   （`get_log_sink()`），即 移植约定 要求的"与 sink 通道对接"。

## 键名约定

* `development_for_prompt` 的返回值会被 `narrator_prompts.to_prompt_payload()`
  **原样 JSON 序列化进提示词**（`developmentTendencies`），因此输出键名逐字保持
  上游 camelCase（`sourceEntryIds`），一个字都不能改。
* 数据库列名保持上游 camelCase；从数据库行/会话里读入的一律 `pick()` 双读。
* 本文件内部变量与 `report_*` 的参数名保持 snake_case。

## 本移植版的必要偏离（移植约定）

* `resolveCompactionFacts` 上游用 `{ id: { $in: ids } }`。本移植版的
  `Database` 只支持等值 `where`（见 `base.db_get` 的显式约定），故改为
  "按 `storyId` + `status` 取回后在 Python 侧过滤"，结果集与上游一致。
* `dbGet` 与本文件其它读取路径共用 `base._normalize_query` 的校验：遇到 `$in`
  之类算子会**显式报错**而不是静默给出偏窄结果集。

不 import `astrbot`。
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Optional

from .. import logging as interlude_logging
from ..logging import format_layered_log, phase_label, render_log_message
from ..story_state import decode_story_state, encode_story_state, inspect_story_state_migration
from ..time import parse_dt
from .base import ServiceBase, _config_section, is_one_bot_platform, pick
from .helpers import clip, history_lexical_score

__all__ = ['ServiceChunk9', 'MAX_SAFE_INTEGER']

#: JS `Number.MAX_SAFE_INTEGER`：`Number.isSafeInteger` 的上界。
MAX_SAFE_INTEGER = 2 ** 53 - 1

#: 上游 `writeReport` / `writeStandalone` 共用的级别序号（`:6741` / `:6783`）。
_LEVEL_RANK = {'silent': 0, 'error': 1, 'warn': 2, 'info': 3, 'debug': 4}

#: 上游 `allowsVerbosity` 的序号（`:6828`）。
_VERBOSITY_RANK = {'summary': 1, 'standard': 2, 'diagnostic': 3}

#: 上游 `retryDbWrite` 的退避序列（`:6931`）。
_WRITE_RETRY_DELAYS_MS = (100, 250, 500, 1_000, 2_000, 3_000, 5_000)


def _is_safe_integer(value: Any) -> bool:
    """JS `Number.isSafeInteger(value)`：整数且落在 ±2^53-1 内（布尔不算）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return abs(value) <= MAX_SAFE_INTEGER


def _js_string(value: Any) -> str:
    """JS `String(value)` 的等价物（`String(undefined)` → `'undefined'`）。

    `repairCanonicalOneBotStoryTransport` 用 `String(bot.selfId) === String(story.selfId)`
    比较两个可能缺省的账号 id；Python 的 `str(None)` 是 `'None'` 而不是 `'undefined'`，
    两边都缺省时结果仍是"相等"，故这里显式对齐 JS 语义。
    """
    if value is None:
        return 'undefined'
    if value is True:
        return 'true'
    if value is False:
        return 'false'
    return str(value)


def _logging_section(service: Any) -> dict[str, Any]:
    """读 `config.logging`（dict / dataclass 都认；缺省为空 dict）。"""
    section = _config_section(getattr(service, 'config', None), 'logging')
    return section if isinstance(section, dict) else {}


def _format_token_usage_line(record: Any) -> str:
    """`narrator.format_token_usage_line()`（上游 `formatTokenUsageLine`）。

    `narrator.py` 是重型模块（含模型客户端），按 `base.py` 的既有做法**惰性导入**；
    未落地时返回空串，`report_token_usage` 因此不发日志（等价上游"没有可报告的用量"）。
    """
    try:
        from ..narrator import format_token_usage_line
    except ImportError:  # pragma: no cover - 取决于同批任务的落地顺序
        return ''
    return format_token_usage_line(record) or ''


class ServiceChunk9(ServiceBase):
    """Chunk9 mixin：演化候选投影 / 分层日志通道 / 盲区模式 / 数据库读写管道。

    跨 mixin 调用一律 `self.其他方法()`（MRO 自动解析）。
    """

    # ------------------------------------------------------------------ #
    # 演化候选（`src/service.ts:6714`）
    # ------------------------------------------------------------------ #

    async def development_for_prompt(
        self,
        story_id: str,
        participant_id: Optional[str],
        query: str,
    ) -> list[dict[str, Any]]:
        """上游 `developmentForPrompt(storyId, participantId, query)`（`:6714`）。

        从已应用的设定演化补丁里挑出**与该回合问题最相关**的两条倾向，供主叙事
        软性参考。上游的链式写法逐段对应：

        `filter(path.startsWith('development.') && sourceEntryIds.length && (!participantId || participantId === participantId))`
        → `map(item => ({item, score: historyLexicalScore(query, proposedValue)}))`
        → `filter(score >= 0.12)` → `sort(score desc)` → `slice(0, 2)` → `map(...)`。

        返回项的键名是**发给模型的 payload**（`developmentTendencies`），逐字保持上游
        camelCase（`sourceEntryIds`），见键名约定。
        """
        text = query if isinstance(query, str) else ''
        if not text.strip():
            return []
        rows = await self.db_get(
            'interlude_state_patch',
            {'storyId': story_id, 'status': 'applied'},
            {'limit': 100, 'sort': {'appliedAt': 'desc'}},
        )
        scored: list[tuple[dict[str, Any], float]] = []
        for item in rows:
            path = pick(item, 'path')
            if not isinstance(path, str) or not path.startswith('development.'):
                continue
            source_ids = pick(item, 'sourceEntryIds', 'source_entry_ids')
            if not isinstance(source_ids, list) or not len(source_ids):
                continue
            item_participant = pick(item, 'participantId', 'participant_id')
            if item_participant and item_participant != participant_id:
                continue
            proposed = pick(item, 'proposedValue', 'proposed_value')
            scored.append((item, history_lexical_score(text, proposed if isinstance(proposed, str) else '')))
        relevant = [(item, score) for item, score in scored if score >= 0.12]
        # JS `Array.prototype.sort` 自 ES2019 起稳定；Python `sorted` 同样稳定，
        # 因此同分候选的相对顺序与上游一致。
        relevant.sort(key=lambda pair: -pair[1])
        selected: list[dict[str, Any]] = []
        for item, _score in relevant[:2]:
            selected.append({
                'target': pick(item, 'target'),
                'tendency': clip(pick(item, 'proposedValue', 'proposed_value'), 300),
                'sourceEntryIds': list(pick(item, 'sourceEntryIds', 'source_entry_ids') or []),
            })
        return selected

    # ------------------------------------------------------------------ #
    # 分层日志通道（`src/service.ts:6724-6789`）
    # ------------------------------------------------------------------ #

    def report(
        self,
        level: str,
        story: Any,
        phase: str,
        message: str,
        *args: Any,
    ) -> None:
        """上游 `report(level, story, phase, message, ...args)`（`:6724`）。"""
        self.write_report(level, story, phase, message, args)

    def report_operation(
        self,
        verbosity: str,
        level: str,
        story: Any,
        phase: str,
        message: str,
        *args: Any,
    ) -> None:
        """上游 `reportOperation(...)`（`:6731`）。

        Emit an operational record only when the selected verbosity includes it.
        Summary is for outcomes, standard is for scheduler/model activity, and
        diagnostic is for skip reasons and internal counters.
        """
        if not self.allows_verbosity(verbosity):
            return
        self.write_report(level, story, phase, message, args)

    def write_report(
        self,
        level: str,
        story: Any,
        phase: str,
        message: str,
        args: Any = None,
    ) -> None:
        """上游 `writeReport(level, story, phase, message, args)`（`:6736`）逐条移植。

        盲区模式是**先手闸门**：开启时任何 error/warn 只置健康标记，绝不输出正文
        （`reportBlindModeHealth` 是盲区模式下唯一的 HDSI 记录）。
        """
        if self.blind_mode_config.get('enabled'):
            if level in ('error', 'warn'):
                self.blind_mode_health_issue = True
            return
        logging_config = _logging_section(self)
        if _LEVEL_RANK.get(logging_config.get('level') or 'info', 3) < _LEVEL_RANK.get(level, 3):
            return
        rendered = render_log_message(message, args)
        story_detail = (
            ' 故事=%s' % pick(story, 'id')
            if (logging_config.get('verbosity') or 'standard') == 'diagnostic' else ''
        )
        setting = pick(story, 'setting') or {}
        character = pick(setting, 'character') or {}
        protagonist = str(pick(character, 'name') or '')
        logging_format = logging_config.get('format') or 'layered'
        if logging_format == 'layered':
            # 注意：这里用**纯渲染** `format_layered_log`，投递统一由 `emit_log`
            # 完成（见本模块 docstring「与 base.py 的重叠」）。
            output = format_layered_log({
                'level': level,
                'phase': phase,
                'protagonist': protagonist,
                'message': message,
                'args': list(args or []),
                'colors': logging_config.get('colors') is not False,
                'color_theme': logging_config.get('colorTheme') or 'dark',
                'kaomoji': logging_config.get('kaomoji') is not False,
            })
        elif logging_format == 'compact':
            output = '[%s] %s %s%s' % (phase_label(phase), protagonist, rendered, story_detail)
        else:
            output = '[%s] %s\n事件：%s%s' % (phase_label(phase), protagonist, rendered, story_detail)
        self.emit_log(level, output)

    def report_standalone(self, level: str, message: str, *args: Any) -> None:
        """上游 `reportStandalone(level, message, ...args)`（`:6757`）。"""
        self.write_standalone(level, message, args)

    def report_token_usage(self, record: Any) -> None:
        """上游 `reportTokenUsage(record)`（`:6763`）。

        One log line per model call: token counts, cache hit rate and optional
        billing from the per-connection price fields.
        """
        if self.desktop_event_sink is not None:
            self.desktop_event_sink('token', record)
        line = _format_token_usage_line(record)
        if not line:
            return
        self.report_standalone(
            'info', 'Token 用量[%s] 模型=%s %s', pick(record, 'task'), pick(record, 'model'), line,
        )

    def report_standalone_operation(
        self,
        verbosity: str,
        level: str,
        message: str,
        *args: Any,
    ) -> None:
        """上游 `reportStandaloneOperation(verbosity, level, message, ...args)`（`:6770`）。"""
        if not self.allows_verbosity(verbosity):
            return
        self.write_standalone(level, message, args)

    def write_standalone(self, level: str, message: str, args: Any = None) -> None:
        """上游 `writeStandalone(level, message, args)`（`:6775`）逐条移植。"""
        if self.blind_mode_config.get('enabled'):
            if level in ('error', 'warn'):
                self.blind_mode_health_issue = True
            return
        logging_config = _logging_section(self)
        if _LEVEL_RANK.get(logging_config.get('level') or 'info', 3) < _LEVEL_RANK.get(level, 3):
            return
        if (logging_config.get('format') or 'layered') == 'layered':
            output = format_layered_log({
                'level': level,
                'protagonist': 'HDSI',
                'message': message,
                'args': list(args or []),
                'standalone': True,
                'colors': logging_config.get('colors') is not False,
                'color_theme': logging_config.get('colorTheme') or 'dark',
                'kaomoji': logging_config.get('kaomoji') is not False,
            })
        else:
            output = '[系统] %s' % render_log_message(message, args)
        self.emit_log(level, output)

    # ------------------------------------------------------------------ #
    # 事实结算与连续性脏标记（`src/service.ts:6792-6809`）
    # ------------------------------------------------------------------ #

    async def resolve_compaction_facts(
        self,
        story_id: str,
        value: Any,
        allowed_ids: Any,
        now: Any,
    ) -> bool:
        """上游 `resolveCompactionFacts(storyId, value, allowedIds, now)`（`:6792`）。

        压缩器只能结算**本次请求里确实交付给它**的事实 id（`allowedIds` 白名单），
        其余一律忽略；一项都没命中时返回 `False`，调用方据此跳过"已结算"的回执。

        **本移植版的改写**：上游一次性 `dbSet(..., { id: { $in: [...] } })`；本移植版
        的 `Database` 只支持等值 `where`，故逐行结算（仍然是"只把 unresolved 的置位"，
        且都在写队列里串行执行）。结果与上游等价。
        """
        allowed = set(allowed_ids) if isinstance(allowed_ids, (set, frozenset, list, tuple)) else set()
        ids: list[int] = []
        if isinstance(value, list):
            seen: set[int] = set()
            for raw in value:
                if not _is_safe_integer(raw) or raw <= 0 or raw not in allowed or raw in seen:
                    continue
                seen.add(raw)
                ids.append(raw)
        ids = ids[:20]
        if not ids:
            return False
        # 上游用 `{ storyId, id: {$in: ids}, status: 'active' }` 查询。
        rows = await self.db_get('interlude_fact', {'storyId': story_id, 'status': 'active'})
        id_set = set(ids)
        unresolved = [row for row in rows if row.get('id') in id_set and row.get('unresolved')]
        if not unresolved:
            return False
        changed_at = parse_dt(now) or self.now()
        for fact in unresolved:
            await self.db_set(
                'interlude_fact',
                {'id': fact.get('id')},
                {'unresolved': False, 'lastSeenAt': changed_at, 'updatedAt': changed_at},
            )
        return True

    async def mark_continuity_dirty(self, story_id: str, now: Any) -> None:
        """上游 `markContinuityDirty(storyId, now)`（`:6804`）逐条移植。

        已经脏了就不再写库（避免每个回合都产生一次无意义的 UPDATE）。
        """
        story = await self.get_story(story_id)
        state = decode_story_state(pick(story, 'state'))
        if state.get('continuity_dirty'):
            return
        changed_at = parse_dt(now) or self.now()
        patched = dict(state)
        patched['continuity_dirty'] = True
        await self.db_set(
            'interlude_story',
            {'id': story_id},
            {'state': encode_story_state(patched), 'updatedAt': changed_at},
        )

    # ------------------------------------------------------------------ #
    # 日志出口与盲区模式心跳（`src/service.ts:6811-6830`）
    # ------------------------------------------------------------------ #

    def emit_log(self, level: str, output: str) -> None:
        """上游 `emitLog(level, output)`（`:6811`）：Koishi logger 的等价出口。

        本移植版的唯一投递点：配了 `service_logger` 就投它，否则落到
        `plugin.core.logging` 的 sink（AstrBot 适配层通过 `set_log_sink` 接管）。
        投递本身绝不抛异常 —— 日志失败不能影响叙事回合。
        """
        logger = self.service_logger
        if logger is None:
            self._emit_to_sink(level, output)
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
        except Exception:  # pragma: no cover - 宿主 logger 异常不得冒泡
            self._emit_to_sink(level, output)

    @staticmethod
    def _emit_to_sink(level: str, output: str) -> None:
        """把一条**已渲染**的日志交给 `plugin.core.logging` 的 sink。"""
        try:
            interlude_logging.get_log_sink()(level, output)
        except Exception:  # pragma: no cover - sink 异常不得冒泡
            return

    def report_blind_mode_health(self) -> None:
        """上游 `reportBlindModeHealth()`（`:6818`）：盲区模式的健康心跳。

        This is the sole HDSI record emitted in Blind Mode. It deliberately
        carries no story, account, model, message, or failure-detail content.

        上游直接调 `this.serviceLogger.info(...)`；本移植版改走 `emit_log`，
        语义相同（有 logger 走 logger，没有就走 sink），否则无 logger 的环境里
        这条唯一的心跳会彻底消失。
        """
        status = '需关注' if (self.blind_mode_health_issue or self.database_resetting) else '正常'
        scheduler = '运行中' if self.background_started else '未就绪'
        self.emit_log('info', '[失明模式] 运行状态=%s 后台任务=%s' % (status, scheduler))
        self.blind_mode_health_issue = False

    def allows_verbosity(self, required: str) -> bool:
        """上游 `allowsVerbosity(required)`（`:6827`）。

        未配置/未知取值一律按 `standard`（与 `base.py` 的兜底一致，比上游
        `rank[undefined]` 判假更接近"默认 standard"的 Console 语义）。
        """
        configured = _logging_section(self).get('verbosity') or 'standard'
        return _VERBOSITY_RANK.get(configured, 2) >= _VERBOSITY_RANK.get(required, 2)

    # ------------------------------------------------------------------ #
    # 故事读取与状态迁移提示（`src/service.ts:6833-6849`）
    # ------------------------------------------------------------------ #

    async def get_story(self, story_id: str) -> Any:
        """上游 `getStory(id)`（`:6833`）逐条移植。

        找不到就抛错（上游 `throw new Error`）；每个故事**只报告一次**状态迁移
        诊断（`reportedStateMigrations`），本阶段只报告、不自动改写 Canon。
        """
        rows = await self.db_get('interlude_story', {'id': story_id})
        story = rows[0] if rows else None
        if not story:
            raise RuntimeError('Interlude story not found: %s' % story_id)
        resolved_id = story.get('id')
        if resolved_id not in self.reported_state_migrations:
            self.reported_state_migrations.add(resolved_id)
            configured_perspective = str(pick(self.story_defaults, 'perspective') or '')
            inspection = inspect_story_state_migration(story.get('state'), configured_perspective)
            setting = pick(story, 'setting') or {}
            persisted_perspective = str(pick(setting, 'perspective') or '')
            if inspection.get('perspective_default_available') and not persisted_perspective.strip():
                self.report_operation(
                    'diagnostic', 'debug', story, 'advance',
                    '状态迁移提示：当前故事未持久化 Perspective，但 Console 默认值可用；本阶段只报告，不自动改写 Canon',
                )
            unknown_keys = inspection.get('unknown_keys') or []
            if len(unknown_keys):
                self.report_operation(
                    'diagnostic', 'debug', story, 'advance',
                    '状态迁移保留未知扩展字段 数量=%d', len(unknown_keys),
                )
        return story

    # ------------------------------------------------------------------ #
    # 串行队列与数据库管道（`src/service.ts:6851-7006`）
    # ------------------------------------------------------------------ #

    def serial(self, key: str, task: Callable[[], Any]) -> Any:
        """上游 `serial(id, task)`（`:6851`）。

        队列机器（`queues` + 失败不堵链 + 队尾清理）由 `ServiceBase.run_in_queue`
        提供，本方法是它在 chunk9 的显式入口（同一实现，不做第二份拷贝）。
        """
        return super().serial(key, task)

    async def db_write(self, task: Callable[[], Any]) -> Any:
        """上游 `dbWrite(task)`（`:6863`）：写队列串行 + 有界重试。

        上游 `this.databaseWriteQueue.then(() => retryDbWrite(task), () => retryDbWrite(task))`
        的两个分支都跑 `retryDbWrite`，即**前一次失败不影响本次**；`write()` 的
        `asyncio.Lock` 等价这条串行链。
        """

        async def runner(_database: Any) -> Any:
            return await self.retry_db_write(task)

        return await self.write(runner)

    async def db_read(self, task: Callable[[], Any]) -> Any:
        """上游 `dbRead(task)`（`:6874`）。

        A SQLite read can fail during the same short filesystem hiccup as a
        write. Reads stay concurrent for normal performance; only transient
        driver errors receive a small bounded retry instead of aborting a user
        turn. —— 实现在 `ServiceBase.db_read`（同一份，避免漂移）。
        """
        return await super().db_read(task)

    async def db_get(self, table: str, query: Any, options: Any = None) -> list[Any]:
        """上游 `dbGet(table, query, options)`（`:6893`）。

        实现在 `ServiceBase.db_get`：归一化查询、`limit`/`sort` 解析、
        `normalizeDatabaseRow` 逐行归一化、瞬时错误小步重试。
        """
        return await super().db_get(table, query, options)

    async def repair_canonical_one_bot_story_transport(self, story: Any, session: Any) -> Any:
        """上游 `repairCanonicalOneBotStoryTransport(story, session)`（`:6903`）。

        Repair only a stale canonical story whose configured bot is no longer
        online. A live OneBot session is stronger evidence than historical story
        metadata, while a still-online story bot remains untouched.
        """
        platform = pick(session, 'platform')
        session_self_id = pick(session, 'selfId', 'self_id')
        if not is_one_bot_platform(platform) or not session_self_id:
            return story
        story_platform = pick(story, 'platform')
        story_self_id = pick(story, 'selfId', 'self_id')
        has_live_story_bot = False
        for bot in self.ctx.bots():
            if _js_string(pick(bot, 'selfId', 'self_id')) != _js_string(story_self_id):
                continue
            bot_platform = pick(bot, 'platform')
            if bot_platform == story_platform or (
                is_one_bot_platform(bot_platform) and is_one_bot_platform(story_platform)
            ):
                has_live_story_bot = True
                break
        if has_live_story_bot or (
            story_platform == platform and _js_string(story_self_id) == _js_string(session_self_id)
        ):
            return story
        now = self.now()
        await self.db_set(
            'interlude_story',
            {'id': story.get('id')},
            {'platform': platform, 'selfId': session_self_id, 'updatedAt': now},
        )
        self.report_standalone(
            'warn', '主剧本投递账号已自愈 故事=%s 平台=%s 账号=%s',
            story.get('id'), platform, session_self_id,
        )
        repaired = dict(story)
        repaired.update({'platform': platform, 'selfId': session_self_id, 'updatedAt': now})
        return repaired

    async def retry_db_write(self, task: Callable[[], Any]) -> Any:
        """上游 `retryDbWrite(task)`（`:6914`）逐条移植。

        sql.js/SQLite may briefly report disk I/O or locking errors while
        Koishi flushes its in-memory database. A short retry is useful, but
        logging every transient attempt as a warning makes normal file flush
        contention look like a fatal HDSI failure. Keep the retry bounded, add a
        little jitter, and only warn on the final failure.
        """
        attempt = 0
        while True:
            try:
                return await task()
            except Exception as error:
                transient = self.is_transient_database_error(error)
                if attempt >= 7 or not transient:
                    if transient:
                        self.report_standalone('warn', 'SQLite 写入连续失败，已停止重试 错误=%s', error)
                    raise
                base_delay = _WRITE_RETRY_DELAYS_MS[attempt] if attempt < len(_WRITE_RETRY_DELAYS_MS) else 5_000
                delay = base_delay + int(self.rng() * min(250, base_delay / 4))
                self.report_standalone_operation(
                    'diagnostic', 'debug',
                    'SQLite 写入暂时失败，准备重试 等待=%dms 次数=%d 错误=%s',
                    delay, attempt + 1, error,
                )
                await asyncio.sleep(delay / 1000.0)
                attempt += 1

    async def db_create(self, table: str, data: Any) -> Any:
        """上游 `dbCreate(table, data)`（`:6939`）。

        实现在 `ServiceBase.db_create`（瞬时错误时先用
        `find_possibly_committed_create` 找回已提交行，避免重复的拆分意图/
        剧本条目/记忆）。
        """
        return await super().db_create(table, data)

    async def find_possibly_committed_create(self, table: str, data: Any) -> Any:
        """上游 `findPossiblyCommittedCreate(table, data)`（`:6956`）。

        实现在 `ServiceBase.find_possibly_committed_create`：按表逐列比对
        （`interlude_intent` / `interlude_script_entry` / `interlude_memory`
        走业务列，其余走主键）。
        """
        return await super().find_possibly_committed_create(table, data)

    async def db_set(self, table: str, query: Any, data: Any) -> Any:
        """上游 `dbSet(table, query, data)`（`:6986`）。实现在 `ServiceBase.db_set`。"""
        return await super().db_set(table, query, data)

    async def db_remove(self, table: str, query: Any) -> Any:
        """上游 `dbRemove(table, query)`（`:6990`）。实现在 `ServiceBase.db_remove`。"""
        return await super().db_remove(table, query)

    async def purge_table(self, table: str, query: Any, fallback: Any) -> None:
        """上游 `purgeTable(table, query, fallback)`（`:6999`）。

        SQLite/sql.js may fail physical DELETE when its backing file is locked.
        Fall back to redaction so an administrative purge still completes and the
        removed content is no longer exposed to prompts or management commands.
        —— 实现在 `ServiceBase.purge_table`。
        """
        return await super().purge_table(table, query, fallback)
