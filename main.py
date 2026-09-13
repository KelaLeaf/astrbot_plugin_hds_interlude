"""HDS Interlude 的 AstrBot 插件入口（`upstream/src/index.ts` 的 AstrBot 等价物）。

职责边界（`docs/PORT_PLAN.md` §0.3）：

* 本文件**只碰 AstrBot 的插件 API 与 `plugin/adapters/`**；叙事核心的一切都经
  `AstrbotBridge`（`self.bridge`）转发，不 import 任何 `plugin/core/` 实现细节。
* 命令注册、消息中间件、生命周期、权限判定逐条对应上游 `src/index.ts`。

命令对照表（上游 `interlude.*` ↔ AstrBot `hdsi_*`）见 `COMMANDS` 与
`docs/COMMANDS.md`。AstrBot 的命令名不能带点号层级，因此统一改成下划线形式。

盲区模式（上游 `blindMode.enabled`）
-----------------------------------

上游在失明模式下**不注册任何命令**（`if (blindModeEnabled) { ctx.on('command/before-execute', () => '') } else { registerCommands(...) }`）。
AstrBot 的 `@filter.command` 是类定义期静态注册的，运行期无法"不写这个装饰器"，
所以本移植版在 `__init__` 里把本插件模块注册到 `star_handlers_registry` 的全部
管理命令 handler **真正摘掉**（`StarHandlerRegistry.remove`），效果与上游一致：

* 管理命令完全不响应（连"无权限"都不会返回）；
* 普通私聊 / 群聊叙事照常进行；
* 每个 handler 里还有一道 `if self.blind_mode: return` 的兜底守卫，防止宿主
  版本漂移导致摘除失败。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .adapters.astrbot_bridge import (
    AstrbotBridge,
    COMMAND_WORD_RE,
    build_bridge,
    endpoint_for_event,
    session_view,
)

__all__ = [
    'COMMANDS',
    'COMMAND_HANDLERS',
    'HDSInterludePlugin',
    'LOCAL_EXTENSION_COMMANDS',
    'MANAGEMENT_COMMANDS',
    'UPSTREAM_COMMAND_COUNT',
]


# =========================================================================== #
# 命令表（上游 `src/index.ts:520-868` 的 `registerCommands`）
# =========================================================================== #

@dataclass(frozen=True)
class CommandSpec:
    """一条上游命令在本移植版的落点。"""

    upstream: str
    """上游 Koishi 命令名（含点号层级）。"""

    command: str
    """AstrBot 命令名（下划线）。"""

    handler: str
    """`HDSInterludePlugin` 上的处理器方法名。"""

    permission: str
    """`admin` = 需要 `sharedStory.managerAccounts` 管理员；`member` = 通过白名单的已授权用户。"""

    usage: str
    """用法（`docs/COMMANDS.md` 对照表的"用法"列）。"""


#: 全部 32 条管理命令，顺序与上游 `registerCommands` 一致。
COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec('interlude.doctor', 'hdsi_doctor', 'hdsi_doctor', 'member', 'hdsi_doctor'),
    CommandSpec('interlude.story.start', 'hdsi_story_start', 'hdsi_story_start', 'admin', 'hdsi_story_start'),
    CommandSpec('interlude.init', 'hdsi_init', 'hdsi_init', 'admin', 'hdsi_init [旧名称]'),
    CommandSpec('interlude.setup', 'hdsi_setup', 'hdsi_setup', 'admin', 'hdsi_setup <JSON>'),
    CommandSpec('interlude.status', 'hdsi_status', 'hdsi_status', 'member', 'hdsi_status'),
    CommandSpec('interlude.pause', 'hdsi_pause', 'hdsi_pause', 'admin', 'hdsi_pause'),
    CommandSpec('interlude.resume', 'hdsi_resume', 'hdsi_resume', 'admin', 'hdsi_resume'),
    CommandSpec('interlude.advance', 'hdsi_advance', 'hdsi_advance', 'admin', 'hdsi_advance'),
    CommandSpec(
        'interlude.timeline.rebase', 'hdsi_timeline_rebase', 'hdsi_timeline_rebase', 'admin',
        'hdsi_timeline_rebase',
    ),
    CommandSpec('interlude.timeline', 'hdsi_timeline', 'hdsi_timeline', 'member', 'hdsi_timeline [条数]'),
    CommandSpec('interlude.memory', 'hdsi_memory', 'hdsi_memory', 'member', 'hdsi_memory [条数]'),
    CommandSpec('interlude.context', 'hdsi_context', 'hdsi_context', 'member', 'hdsi_context'),
    CommandSpec('interlude.compact', 'hdsi_compact', 'hdsi_compact', 'admin', 'hdsi_compact'),
    CommandSpec('interlude.script', 'hdsi_script', 'hdsi_script', 'admin', 'hdsi_script [条数]'),
    CommandSpec(
        'interlude.script.note', 'hdsi_script_note', 'hdsi_script_note', 'admin', 'hdsi_script_note <内容>',
    ),
    CommandSpec(
        'interlude.memory.facts', 'hdsi_memory_facts', 'hdsi_memory_facts', 'admin', 'hdsi_memory_facts [条数]',
    ),
    CommandSpec(
        'interlude.memory.add', 'hdsi_memory_add', 'hdsi_memory_add', 'admin',
        'hdsi_memory_add <范围> <内容>',
    ),
    CommandSpec(
        'interlude.memory.forget', 'hdsi_memory_forget', 'hdsi_memory_forget', 'admin',
        'hdsi_memory_forget <编号>',
    ),
    CommandSpec(
        'interlude.memory.intents', 'hdsi_memory_intents', 'hdsi_memory_intents', 'admin',
        'hdsi_memory_intents [条数]',
    ),
    CommandSpec(
        'interlude.memory.cancel', 'hdsi_memory_cancel', 'hdsi_memory_cancel', 'admin',
        'hdsi_memory_cancel <编号>',
    ),
    CommandSpec(
        'interlude.memory.patches', 'hdsi_memory_patches', 'hdsi_memory_patches', 'admin',
        'hdsi_memory_patches [条数]',
    ),
    CommandSpec(
        'interlude.memory.reject', 'hdsi_memory_reject', 'hdsi_memory_reject', 'admin',
        'hdsi_memory_reject <编号>',
    ),
    CommandSpec(
        'interlude.overlay.clear', 'hdsi_overlay_clear', 'hdsi_overlay_clear', 'admin',
        'hdsi_overlay_clear <部分>',
    ),
    CommandSpec('interlude.overlay.status', 'hdsi_overlay_status', 'hdsi_overlay_status', 'admin', 'hdsi_overlay_status'),
    CommandSpec(
        'interlude.overlay.compact', 'hdsi_overlay_compact', 'hdsi_overlay_compact', 'admin',
        'hdsi_overlay_compact',
    ),
    CommandSpec('interlude.schedule', 'hdsi_schedule', 'hdsi_schedule', 'member', 'hdsi_schedule'),
    CommandSpec(
        'interlude.schedule.refresh', 'hdsi_schedule_refresh', 'hdsi_schedule_refresh', 'admin',
        'hdsi_schedule_refresh',
    ),
    CommandSpec(
        'interlude.schedule.rebuild', 'hdsi_schedule_rebuild', 'hdsi_schedule_rebuild', 'admin',
        'hdsi_schedule_rebuild',
    ),
    CommandSpec(
        'interlude.database.clear', 'hdsi_database_clear', 'hdsi_database_clear', 'admin',
        'hdsi_database_clear',
    ),
    CommandSpec('interlude.purge.all', 'hdsi_purge_all', 'hdsi_purge_all', 'admin', 'hdsi_purge_all'),
    CommandSpec(
        'interlude.purge.platform', 'hdsi_purge_platform', 'hdsi_purge_platform', 'admin',
        'hdsi_purge_platform <平台>',
    ),
    CommandSpec(
        'interlude.purge.range', 'hdsi_purge_range', 'hdsi_purge_range', 'admin',
        'hdsi_purge_range <开始> <结束>',
    ),
    # 本移植版新增（上游 Koishi 没有）：配置的导出与导入。
    CommandSpec(
        'interlude.config.export', 'hdsi_config_export', 'hdsi_config_export', 'admin',
        'hdsi_config_export',
    ),
    CommandSpec(
        'interlude.config.import', 'hdsi_config_import', 'hdsi_config_import', 'admin',
        'hdsi_config_import [JSON]',
    ),
)

#: AstrBot 命令名 → 处理器方法名。
COMMAND_HANDLERS: dict[str, str] = {spec.command: spec.handler for spec in COMMANDS}

#: 本移植版**新增**的命令（上游 Koishi 的 `registerCommands()` 里没有）。
#: 上游对齐性的断言用 `len(COMMANDS) - len(LOCAL_EXTENSION_COMMANDS) == 32` 表达，
#: 这样将来再加本地命令不会把"上游 32 条都在"的性质测丢。
LOCAL_EXTENSION_COMMANDS: frozenset[str] = frozenset({
    'hdsi_config_export',
    'hdsi_config_import',
})

#: 上游 `interlude.*` 命令条数（`upstream/src/index.ts` 的 `registerCommands()`）。
UPSTREAM_COMMAND_COUNT = 32

#: 盲区模式下需要从 `star_handlers_registry` 摘除的 handler 方法名。
MANAGEMENT_COMMANDS: frozenset[str] = frozenset(COMMAND_HANDLERS.values())

#: 上游 `isFactScope`（`upstream/src/index.ts:911`）。
FACT_SCOPES = ('character', 'world', 'relationship', 'event', 'promise')

#: 上游 `interlude.overlay.clear` 的 `target` 枚举。
OVERLAY_TARGETS = ('character', 'perspective', 'relationship', 'world', 'all')

#: 上游 `askConfirmation` 的等待时长（60 秒）。
CONFIRMATION_TIMEOUT_SECONDS = 60

#: 上游 `askConfirmation` 的肯定回答正则：`/^(?:y|yes)$/i`。
CONFIRMATION_YES_RE = re.compile(r'^(?:y|yes)$', re.IGNORECASE)


def _read_file_component(component: Any) -> Optional[str]:
    """尽力从一个消息组件里读出文本内容（配置导入用）。

    只认 AstrBot 的 `File` 段（鸭子类型：同时有 `name` 与 `file_`），这样图片/语音
    这些同样带 `file` 属性的段不会被误当成配置。优先读本地路径，其次读
    `base64://` / `file://` / `http(s)` URL。任何一步失败都返回 `None`——
    导入命令自己会给"没找到配置内容"的提示，不需要抛。
    """
    import base64 as _base64  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    if not (hasattr(component, 'name') and hasattr(component, 'file_')):
        return None
    candidates = [getattr(component, 'file', None), getattr(component, 'url', None)]
    try:
        for candidate in candidates:
            if not isinstance(candidate, str) or not candidate:
                continue
            if os.path.isfile(candidate):
                with open(candidate, encoding='utf-8-sig') as handle:
                    return handle.read()
            if candidate.startswith('base64://'):
                return _base64.b64decode(candidate[len('base64://'):]).decode('utf-8-sig')
            if candidate.startswith('file://'):
                with open(candidate[len('file://'):], encoding='utf-8-sig') as handle:
                    return handle.read()
            if candidate.startswith(('http://', 'https://')):
                with urllib.request.urlopen(candidate, timeout=15) as response:  # noqa: S310
                    return response.read().decode('utf-8-sig')
    except Exception:  # noqa: BLE001 - 读不到就当没有附件
        return None
    return None

#: 上游 `requireManager` 失败文案（三种写法与上游逐字一致）。
NO_MANAGER = '当前 QQ 没有共享主剧本的管理权限。'
NO_MANAGER_DETAIL = (
    '当前 QQ 没有共享主剧本的管理权限。'
    '请在 Console 的 sharedStory.managerAccounts 中添加此 QQ，或留空允许所有获授权账号。'
)
NO_ADMIN = '无权限：当前账号不是 HDSI 管理员。'
CANCELLED = '操作已取消。'


def _pick(value: Any, camel: str, snake: Optional[str] = None) -> Any:
    """双拼写读取（与 `plugin.core.service.pick` 同义，此处不跨层 import）。"""
    if not isinstance(value, dict):
        return None
    if camel in value:
        return value[camel]
    return value.get(snake) if snake else None


def _text(value: Any) -> str:
    return '' if value is None else str(value)


def _format_fixed(value: Any) -> str:
    """上游 `.toFixed(2)` 的等价物。"""
    try:
        return '%.2f' % float(value)
    except (TypeError, ValueError):
        return '0.00'


def _dump(value: Any) -> str:
    """上游 `JSON.stringify(value)` 的等价物。"""
    if value is None:
        return 'null'
    try:
        return json.dumps(value, ensure_ascii=False, default=str, separators=(',', ':'))
    except (TypeError, ValueError):
        return str(value)


def _parse_iso(value: str) -> Any:
    """`new Date(String(x).trim())` 的等价物：解析失败返回 `None`。"""
    if not value:
        return None
    from datetime import datetime, timezone

    text = value.strip()
    if text.endswith(('Z', 'z')):
        text = text[:-1] + '+00:00'
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def format_story_start_readiness(readiness: Any, title: str = 'Console 档案检查') -> str:
    """上游 `formatStoryStartReadiness`（`upstream/src/index.ts:876`）逐字移植。"""
    data = readiness if isinstance(readiness, dict) else {}
    preview = data.get('preview') or {}
    existing = data.get('existing')
    lines = [
        title,
        '主角：%s' % (_pick(preview, 'characterName', 'character_name') or '未填写'),
        '角色设定：%s' % ('已填写' if _pick(preview, 'characterProfile', 'character_profile') else '未填写'),
        'Perspective：%s' % ('已填写' if _pick(preview, 'perspective') else '未填写'),
        '世界：%s' % ('已填写' if _pick(preview, 'world') else '未填写'),
        '时区：%s' % _pick(preview, 'timezone'),
        '主模型：%s' % _pick(preview, 'model'),
        '自动创建：%s' % ('开启' if _pick(preview, 'autoCreate', 'auto_create') else '关闭'),
    ]
    if existing:
        name = _pick(_pick(_pick(existing, 'setting'), 'character'), 'name')
        lines.append('运行中故事：%s（%s）' % (name, _pick(existing, 'status')))
    else:
        lines.append('运行中故事：尚未创建')
    lines.extend('阻断：%s' % item for item in data.get('blockers') or [])
    lines.extend('提示：%s' % item for item in data.get('warnings') or [])
    if existing:
        lines.append('结果：已有运行中故事，无需再次启动。')
    elif data.get('ready'):
        lines.append('结果：可以启动。')
    else:
        lines.append('结果：请先完成阻断项。')
    return '\n'.join(lines)


def _iter_handlers() -> Iterable[str]:
    """`COMMANDS` 里登记的全部处理器方法名（自检 / 测试用）。"""
    return (spec.handler for spec in COMMANDS)


# =========================================================================== #
# 插件
# =========================================================================== #

class HDSInterludePlugin(Star):
    """HDS Interlude 的 AstrBot `Star`。

    上游 `apply(ctx, config)` 的三件事在这里一一对应：

    * `new InterludeService(ctx, config)` → `AstrbotBridge`（`self.bridge`）；
    * `registerCommands(...)` → `@filter.command` 装饰的 32 个 handler
      （盲区模式下真的从 handler 注册表摘掉）；
    * `ctx.middleware(...)` → `on_private_message` / `on_group_message`，
      判定逻辑在 `AstrbotBridge.handle_event()`。
    """

    def __init__(self, context: Context, config: dict):
        super().__init__(context, config)
        self.config: dict = config or {}
        self.bridge: AstrbotBridge = build_bridge(context, self.config, logger)
        #: 盲区模式（上游 `blindMode.enabled`，兼容旧键 `blackBox.enabled`）。
        self.blind_mode: bool = self.bridge.blind_mode_enabled
        #: 被摘除的管理命令方法名（盲区模式下非空）。
        self.suppressed_commands: tuple[str, ...] = ()
        #: 等待 y/n 确认的回调（key = `unified_msg_origin`）。
        self._confirmations: dict[str, asyncio.Future] = {}
        if self.blind_mode:
            self.suppressed_commands = self._suppress_management_commands()
            logger.info(
                'hds-interlude：盲区模式已开启，%d 条管理命令不注册；普通叙事不受影响。'
                % len(self.suppressed_commands)
            )
        else:
            logger.info('hds-interlude：插件加载开始')

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def terminate(self) -> None:
        """优雅关闭：停后台任务、关 HTTP 客户端与数据库。"""
        self._confirmations.clear()
        try:
            await self.bridge.shutdown()
        except Exception as error:  # noqa: BLE001 - 卸载路径绝不抛回宿主
            logger.warning('hds-interlude：关闭失败 %s' % error)

    def _suppress_management_commands(self) -> tuple[str, ...]:
        """把本插件的管理命令 handler 从 AstrBot 注册表里摘掉（上游失明模式等价物）。

        AstrBot 的 `@filter.command` 在类定义期就把 handler 注册进
        `star_handlers_registry`，运行期只能从注册表里移除；`remove()` 会同时清理
        `star_handlers_map`，因此命令既不会执行、也不会出现在帮助列表里。
        """
        try:
            from astrbot.core.star.star_handler import star_handlers_registry
        except ImportError:  # pragma: no cover - 宿主版本漂移
            logger.warning('hds-interlude：无法访问 handler 注册表，改用运行时静默守卫。')
            return ()
        module_path = type(self).__module__
        removed: list[str] = []
        try:
            handlers = list(star_handlers_registry.get_handlers_by_module_name(module_path))
        except Exception as error:  # noqa: BLE001
            logger.warning('hds-interlude：读取 handler 注册表失败 %s' % error)
            return ()
        for handler in handlers:
            name = _text(getattr(handler, 'handler_name', ''))
            if name not in MANAGEMENT_COMMANDS:
                continue
            try:
                star_handlers_registry.remove(handler)
            except Exception as error:  # noqa: BLE001
                logger.warning('hds-interlude：摘除命令 %s 失败 %s' % (name, error))
                continue
            removed.append(name)
        return tuple(removed)

    def active_commands(self) -> dict[str, str]:
        """当前实际生效的 AstrBot 命令 → 处理器表（盲区模式返回空表）。"""
        if self.blind_mode:
            return {}
        return dict(COMMAND_HANDLERS)

    # ------------------------------------------------------------------ #
    # 事件准备与确认问答
    # ------------------------------------------------------------------ #

    async def _prepare(self, event: AstrMessageEvent) -> Any:
        """建 `SessionView`、登记投递坐标、确保 bridge 已启动。"""
        await self.bridge.ensure_started()
        endpoint = endpoint_for_event(event)
        session = session_view(event, endpoint)
        self.bridge.remember_event(event, session, endpoint)
        return session

    def _raw_args(self, event: AstrMessageEvent) -> list[str]:
        """命令参数：按空格切分并丢掉命令词本身。

        上游用 Koishi 的 `text`（贪婪）与 `number` 参数类型；AstrBot 的命令参数
        按位置切词、参数类型语义也不同，因此这里统一手工解析，
        `hdsi_setup <JSON>` / `hdsi_script_note <内容>` 才能吃掉整段剩余文本。
        """
        parts = _text(event.get_message_str()).split()
        if parts and COMMAND_WORD_RE.match(parts[0]):
            parts = parts[1:]
        return parts

    @staticmethod
    def _int_arg(args: list[str], index: int, default: int) -> int:
        if len(args) <= index:
            return default
        try:
            return int(str(args[index]).strip())
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _text_arg(args: list[str], index: int) -> str:
        return ' '.join(args[index:]).strip() if len(args) > index else ''

    async def _ask_confirmation(self, event: AstrMessageEvent, message: str) -> bool:
        """上游 `askConfirmation(session, message)`（`upstream/src/index.ts:870`）。

        `await session.send(...)` + `session.prompt(60_000)` 的 AstrBot 等价物：
        先发提问，再用一条 `unified_msg_origin` 作用域的 future 等下一个 y/n。
        `on_private_message` 会优先把这条回复交给 future 并吞掉事件，所以确认词
        不会混进叙事；60 秒无回复按取消处理（返回 `False`）。
        """
        key = _text(getattr(event, 'unified_msg_origin', ''))
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._confirmations[key] = future
        await event.send(event.plain_result('%s\n请在 60 秒内回复 y 或 n。' % message))
        try:
            answer = await asyncio.wait_for(future, CONFIRMATION_TIMEOUT_SECONDS)
        except (asyncio.TimeoutError, TimeoutError):
            return False
        finally:
            self._confirmations.pop(key, None)
        return bool(CONFIRMATION_YES_RE.match(_text(answer).strip()))

    def _resolve_confirmation(self, event: AstrMessageEvent) -> bool:
        """若该会话正在等 y/n，则消费这条回复并返回 `True`。"""
        key = _text(getattr(event, 'unified_msg_origin', ''))
        future = self._confirmations.get(key)
        if future is None or future.done():
            return False
        future.set_result(_text(event.get_message_str()).strip())
        event.stop_event()
        return True

    # ------------------------------------------------------------------ #
    # 权限与前置检查（上游 `requireManager` / `requireStory`）
    # ------------------------------------------------------------------ #

    def _is_manager(self, session: Any) -> bool:
        """上游 `requireManager(service, session)` → `service.canManageSession(session)`。"""
        return bool(self.bridge.service.can_manage_session(session))

    async def _require_story(self, session: Any) -> Any:
        """上游 `requireStory(service, session)`（`upstream/src/index.ts:896`）。

        文案与上游逐字一致，**只有命令名做了本地化**：上游写的
        `interlude.doctor` / `interlude.story.start` 在本插件里叫
        `hdsi_doctor` / `hdsi_story_start`，引导用户执行不存在的命令属于
        "适配引入的缺陷"，不是上游原文的一部分。
        """
        service = self.bridge.service
        if not service.can_handle_session(session):
            return (
                '当前 QQ 账号未获 HDSI 互动授权。请在 Console 的“NapCat / OneBot QQ 账号控制”中'
                '检查机器人 QQ 号、用户 QQ 白名单和启用状态。'
            )
        story = await service.find_story(session)
        if story is None:
            return (
                '当前私聊还没有故事。请先在 Console 完成档案，然后执行 hdsi_doctor；'
                '手动启动请使用 hdsi_story_start，或开启 runtime.autoCreate 后直接发送第一条私聊。'
            )
        return story

    # ------------------------------------------------------------------ #
    # 消息中间件（上游 `ctx.middleware`）
    # ------------------------------------------------------------------ #

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def on_private_message(self, event: AstrMessageEvent):
        """私聊入站：确认问答优先，其次整条交给 `bridge.handle_event()`。

        上游中间件的消费语义：叙事决定沉默时也吞掉事件（不落给其它处理器），
        由 `AstrbotBridge.handle_event()` 调用 `event.stop_event()` 实现；
        管理命令消息在 `runtime.ignore_command_messages` 下不消费，交回命令解析器。
        """
        if self._resolve_confirmation(event):
            return
        if not self.bridge.config_flag(
            'runtime', 'capture_direct_messages', 'captureDirectMessages', default=True,
        ):
            return
        replies = await self.bridge.handle_event(event)
        for reply in replies:
            yield event.plain_result(reply)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        """群聊入站：按上游 `canHandleGroupSession` 的规则选择性接入。

        上游中间件对非私聊会话一律调 `service.receiveGroup(session)`，由它自己先做
        白名单判定；这里多一道 `can_handle_group_session` 预检，纯粹是为了不去
        打扰未授权的群（判定结果与 `receiveGroup` 内部完全一致）。
        """
        session = await self._prepare(event)
        if not self.bridge.service.can_handle_group_session(session):
            return
        replies = await self.bridge.handle_event(event)
        for reply in replies:
            yield event.plain_result(reply)

    # ------------------------------------------------------------------ #
    # 命令：档案与状态
    # ------------------------------------------------------------------ #

    @filter.command('hdsi_doctor')
    async def hdsi_doctor(self, event: AstrMessageEvent):
        """检查当前 Console 档案、权限与模型是否适合启动故事。"""
        if self.blind_mode:
            return
        session = await self._prepare(event)
        readiness = await self.bridge.service.story_start_readiness(session)
        yield event.plain_result(format_story_start_readiness(readiness))

    async def _start_story_from_console(
        self,
        event: AstrMessageEvent,
        session: Any,
        legacy_name: str = '',
    ) -> str:
        """上游 `startStoryFromConsole(session, legacyName?)`（`upstream/src/index.ts:523`）。"""
        service = self.bridge.service
        if not self._is_manager(session):
            return '无权限：手动启动共享主剧本需要 HDSI 管理员权限。'
        readiness = await service.story_start_readiness(session)
        existing = readiness.get('existing') if isinstance(readiness, dict) else None
        if existing:
            name = _pick(_pick(_pick(existing, 'setting'), 'character'), 'name')
            if _pick(existing, 'status') == 'paused':
                return '当前已有 %s 的主剧本（暂停中）；请使用 hdsi_resume 恢复，不要重复启动。' % name
            return '当前已有 %s 的活动主剧本；请使用 hdsi_status 查看状态。' % name
        if not (isinstance(readiness, dict) and readiness.get('ready')):
            return format_story_start_readiness(readiness, 'Console 档案尚未适合启动')
        preview = readiness.get('preview') or {}
        trimmed = _text(legacy_name).strip()
        legacy_note = ('已忽略旧 init 的名称参数“%s”；角色名称以 Console 为准。' % trimmed) if trimmed else ''
        lines = [
            '即将从当前 Console 档案启动故事：',
            '主角：%s' % _pick(preview, 'characterName', 'character_name'),
            '角色设定：%s' % ('已填写' if _pick(preview, 'characterProfile', 'character_profile') else '未填写'),
            'Perspective：%s' % ('已填写' if _pick(preview, 'perspective') else '未填写'),
            '世界与地点：%s' % ('已填写' if _pick(preview, 'world') else '未填写'),
            '时区：%s' % _pick(preview, 'timezone'),
            '主模型：%s' % _pick(preview, 'model'),
            '自动创建：%s' % (
                '开启（首次私聊通常无需手动启动）' if _pick(preview, 'autoCreate', 'auto_create') else '关闭'
            ),
        ]
        lines.extend('提示：%s' % warning for warning in readiness.get('warnings') or [])
        if legacy_note:
            lines.append(legacy_note)
        message = '\n'.join(line for line in lines if line)
        if not await self._ask_confirmation(event, '%s\n确认从此档案启动吗？(y/n)' % message):
            return CANCELLED
        story = await service.create_story(session)
        participant = await service.find_participant(session, story)
        display = _pick(participant, 'displayName', 'display_name') or _pick(session, 'userId', 'user_id')
        name = _pick(_pick(_pick(story, 'setting'), 'character'), 'name')
        return '已从 Console 档案启动 %s 的共享主剧本，并加入 %s。' % (name, display)

    @filter.command('hdsi_story_start')
    async def hdsi_story_start(self, event: AstrMessageEvent):
        """管理员：从当前 Console 档案手动启动第一份运行中故事。"""
        if self.blind_mode:
            return
        session = await self._prepare(event)
        yield event.plain_result(await self._start_story_from_console(event, session))

    @filter.command('hdsi_init')
    async def hdsi_init(self, event: AstrMessageEvent):
        """兼容别名：请改用 `hdsi_story_start`；名称参数已忽略。"""
        if self.blind_mode:
            return
        session = await self._prepare(event)
        args = self._raw_args(event)
        yield event.plain_result(await self._start_story_from_console(event, session, args[0] if args else ''))

    @filter.command('hdsi_setup')
    async def hdsi_setup(self, event: AstrMessageEvent):
        """高级：用 JSON 单独修改当前故事设定。"""
        if self.blind_mode:
            return
        session = await self._prepare(event)
        if not self._is_manager(session):
            yield event.plain_result(NO_MANAGER_DETAIL)
            return
        story = await self._require_story(session)
        if isinstance(story, str):
            yield event.plain_result(story)
            return
        payload = self._text_arg(self._raw_args(event), 0)
        try:
            patch = json.loads(payload)
            if not isinstance(patch, dict):
                raise ValueError('设定必须是 JSON 对象。普通测试无需使用此命令。')
            updated = await self.bridge.service.update_setting(story, patch)
            name = _pick(_pick(_pick(updated, 'setting'), 'character'), 'name')
            yield event.plain_result('已保存 %s 的当前故事设定。' % name)
        except Exception as error:  # noqa: BLE001 - 上游同样把解析错误当提示返回
            yield event.plain_result('JSON 格式不正确：%s' % error)

    @filter.command('hdsi_status')
    async def hdsi_status(self, event: AstrMessageEvent):
        """查看当前故事、游标、主模型连接与主动消息开关。"""
        if self.blind_mode:
            return
        session = await self._prepare(event)
        story = await self._require_story(session)
        if isinstance(story, str):
            yield event.plain_result(story)
            return
        service = self.bridge.service
        participants = await service.participants(_pick(story, 'id'))
        state = _pick(story, 'state') or {}
        agency_enabled = self.bridge.config_flag('agency', 'enabled', default=True) is not False
        window = _pick(state, 'agencyWindow', 'agency_window')
        lines = [
            '主角：%s' % _pick(_pick(_pick(story, 'setting'), 'character'), 'name'),
            '关系人数：%d' % len(participants),
            '故事状态：%s' % _pick(story, 'status'),
            '已写到：%s' % self.bridge.iso_time(_pick(story, 'cursorAt', 'cursor_at')),
            '主模型连接：%s' % self.bridge.main_provider_label(),
            '允许主动可见消息：%s' % (
                '开启' if self.bridge.config_flag(
                    'runtime', 'allow_proactive_messages', 'allowProactiveMessages', default=False,
                ) else '关闭'
            ),
            'Agency Window：%s（%s）' % (
                '关闭' if not agency_enabled else '开启',
                _pick(window, 'activityLoad', 'activity_load') or '尚未建立',
            ),
        ]
        yield event.plain_result('\n'.join(lines))

    async def _change_status(self, session: Any, status: str) -> str:
        """上游 `changeStatus(service, session, status)`（`upstream/src/index.ts:901`）。"""
        if not self._is_manager(session):
            return NO_MANAGER
        story = await self._require_story(session)
        if isinstance(story, str):
            return story
        await self.bridge.service.set_status(story, status)
        return '故事已恢复自动处理。' if status == 'active' else '故事已暂停自动处理；已有记录不会删除。'

    @filter.command('hdsi_pause')
    async def hdsi_pause(self, event: AstrMessageEvent):
        """暂停当前故事的自动处理，不删除任何记录。"""
        if self.blind_mode:
            return
        session = await self._prepare(event)
        yield event.plain_result(await self._change_status(session, 'paused'))

    @filter.command('hdsi_resume')
    async def hdsi_resume(self, event: AstrMessageEvent):
        """恢复当前故事的自动处理。"""
        if self.blind_mode:
            return
        session = await self._prepare(event)
        yield event.plain_result(await self._change_status(session, 'active'))

    @filter.command('hdsi_advance')
    async def hdsi_advance(self, event: AstrMessageEvent):
        """手动把故事补写到现在；用于测试自动生活推进。"""
        if self.blind_mode:
            return
        session = await self._prepare(event)
        if not self._is_manager(session):
            yield event.plain_result(NO_MANAGER)
            return
        story = await self._require_story(session)
        if isinstance(story, str):
            yield event.plain_result(story)
            return
        service = self.bridge.service
        # `deliverMessages` 会把"已经发生的可见消息"经 `send_session` 投递；
        # 开一个回合捕获，让它们跟普通回合一样按顺序 `yield` 回 AstrBot，
        # 而不是绕过发送管线直发（顺序与上游一致：先可见消息，后回执）。
        capture = self.bridge.begin_capture(endpoint_for_event(event))
        try:
            messages = await service.advance_story(story)
            delivered = await service.deliver_messages(story, messages, session)
        finally:
            self.bridge.end_capture()
        for text in capture.texts:
            yield event.plain_result(text)
        if delivered:
            yield event.plain_result('剧本已补写到现在，并已发送其中已经发生的可见角色消息。')
        elif messages:
            yield event.plain_result('剧本已补写到现在；可见消息投递未完成，请查看日志。')
        else:
            yield event.plain_result('剧本已补写到现在；这次没有发生可见角色消息。')

    @filter.command('hdsi_timeline_rebase')
    async def hdsi_timeline_rebase(self, event: AstrMessageEvent):
        """管理员：以当前真实时间重建自动推进时间线，不删除历史剧本。"""
        if self.blind_mode:
            return
        session = await self._prepare(event)
        if not self._is_manager(session):
            yield event.plain_result(NO_MANAGER)
            return
        confirmed = await self._ask_confirmation(
            event,
            '将清空当前场景摘要、连续性快照和工作暂存，并从现在重新建立宿主时间线；'
            '历史剧本和长期事实会保留。确认执行吗？(y/n)',
        )
        if not confirmed:
            yield event.plain_result(CANCELLED)
            return
        story = await self._require_story(session)
        if isinstance(story, str):
            yield event.plain_result(story)
            return
        result = await self.bridge.service.rebase_timeline(story)
        timezone = _pick(_pick(story, 'setting'), 'timezone') or 'Asia/Shanghai'
        at = self.bridge.format_log_time(_pick(result, 'at'), timezone)
        suffix = '，活跃场景摘要已重置' if _pick(result, 'sceneReset', 'scene_reset') else ''
        yield event.plain_result('已在 %s 重建宿主时间线%s。' % (at, suffix))

    @filter.command('hdsi_timeline')
    async def hdsi_timeline(self, event: AstrMessageEvent):
        """查看当前账号可见的近期剧本记录；limit 为条数，默认 10。"""
        if self.blind_mode:
            return
        session = await self._prepare(event)
        story = await self._require_story(session)
        if isinstance(story, str):
            yield event.plain_result(story)
            return
        limit = self._int_arg(self._raw_args(event), 0, 10)
        service = self.bridge.service
        participant = await service.find_participant(session, story)
        participant_id = _pick(participant, 'id')
        entries = await service.recent_entries(_pick(story, 'id'), max(1, min(limit * 3, 90)))
        entries = [
            entry for entry in entries
            if not _pick(entry, 'participantId', 'participant_id')
            or _pick(entry, 'participantId', 'participant_id') == participant_id
        ][-max(1, min(limit, 30)):]
        if not entries:
            yield event.plain_result('当前故事还没有剧本记录。')
            return
        timezone = _pick(_pick(story, 'setting'), 'timezone') or 'Asia/Shanghai'
        lines = [
            '[%s] %s/%s: %s' % (
                self.bridge.format_story_display_time(_pick(entry, 'occurredAt', 'occurred_at'), timezone),
                _pick(entry, 'actor'),
                _pick(entry, 'kind'),
                _pick(entry, 'content'),
            )
            for entry in entries
        ]
        yield event.plain_result('\n'.join(lines))

    @filter.command('hdsi_memory')
    async def hdsi_memory(self, event: AstrMessageEvent):
        """查看当前账号相关的记忆摘要；limit 为条数，默认 10。"""
        if self.blind_mode:
            return
        session = await self._prepare(event)
        story = await self._require_story(session)
        if isinstance(story, str):
            yield event.plain_result(story)
            return
        limit = self._int_arg(self._raw_args(event), 0, 10)
        service = self.bridge.service
        participant = await service.find_participant(session, story)
        memories = await service.memories(
            _pick(story, 'id'), max(1, min(limit, 30)), _pick(participant, 'id'),
        )
        if not memories:
            yield event.plain_result('暂时还没有提取出耐久记忆；多进行一些对话并等待后台整理后再看。')
            return
        lines = [
            '[%s/%s] %s' % (
                _pick(memory, 'category'),
                _format_fixed(_pick(memory, 'importance')),
                _pick(memory, 'content'),
            )
            for memory in memories
        ]
        yield event.plain_result('\n'.join(lines))

    @filter.command('hdsi_context')
    async def hdsi_context(self, event: AstrMessageEvent):
        """查看运行上下文摘要：场景、关系、Overlay 与长期事实。"""
        if self.blind_mode:
            return
        session = await self._prepare(event)
        story = await self._require_story(session)
        if isinstance(story, str):
            yield event.plain_result(story)
            return
        service = self.bridge.service
        story_id = _pick(story, 'id')
        participant = await service.find_participant(session, story)
        scene, arc, facts = await asyncio.gather(
            service.active_scene(story_id),
            service.active_arc(story_id),
            service.facts(story_id, 8, '', _pick(participant, 'id')),
        )
        setting = _pick(story, 'setting') or {}
        state = _pick(story, 'state') or {}
        overlay = _pick(state, 'settingOverlay', 'setting_overlay')
        lines = [
            '场景引子：%s' % (_pick(scene, 'hook') or '尚未整理'),
            '场景摘要：%s' % (_pick(scene, 'summary') or '尚未整理'),
            '剧情弧线：%s — %s' % (_pick(arc, 'title') or '开场', _pick(arc, 'summary') or '尚未整理'),
            '当前关系：%s（%s）' % (
                _pick(participant, 'displayName', 'display_name') or _pick(session, 'userId', 'user_id'),
                _pick(participant, 'relationship') or '未填写',
            ),
            '当前关系状态：%s' % _dump(_pick(participant, 'state') if participant is not None else {}),
            '主角个体价值观 / 看待世界的方式：%s（当前 overlay：%s）' % (
                _pick(setting, 'perspective') or '未填写',
                _pick(overlay, 'perspective') or '未形成',
            ),
            '主角全局变化：%s' % _dump(overlay if overlay is not None else {}),
            '主体行动窗口：%s' % _dump(_pick(state, 'agencyWindow', 'agency_window')),
            '长期事实：%s' % (
                ' | '.join(
                    '[%s/%s] %s' % (
                        _pick(fact, 'scope'),
                        _format_fixed(_pick(fact, 'importance')),
                        _pick(fact, 'content'),
                    )
                    for fact in facts
                ) if facts else '暂无'
            ),
        ]
        yield event.plain_result('\n'.join(lines))

    @filter.command('hdsi_compact')
    async def hdsi_compact(self, event: AstrMessageEvent):
        """立即整理当前故事的旧剧本；必要时一并审查 Schedule Preplan。"""
        if self.blind_mode:
            return
        session = await self._prepare(event)
        if not self._is_manager(session):
            yield event.plain_result(NO_MANAGER)
            return
        story = await self._require_story(session)
        if isinstance(story, str):
            yield event.plain_result(story)
            return
        compacted = await self.bridge.service.compact_story(story)
        yield event.plain_result(
            '已完成一次连续性记忆整理。' if compacted else '当前还没有达到需要整理的剧本量。'
        )

    # ------------------------------------------------------------------ #
    # 命令：剧本人工管理
    # ------------------------------------------------------------------ #

    @filter.command('hdsi_script')
    async def hdsi_script(self, event: AstrMessageEvent):
        """管理员：查看当前主剧本的最近原始条目，默认 20 条。"""
        if self.blind_mode:
            return
        session = await self._prepare(event)
        if not self._is_manager(session):
            yield event.plain_result(NO_MANAGER)
            return
        story = await self._require_story(session)
        if isinstance(story, str):
            yield event.plain_result(story)
            return
        limit = self._int_arg(self._raw_args(event), 0, 20)
        entries = await self.bridge.service.recent_entries(_pick(story, 'id'), max(1, min(limit, 50)))
        if not entries:
            yield event.plain_result('当前主剧本还没有原始条目。')
            return
        blocks = []
        for entry in entries:
            participant_id = _pick(entry, 'participantId', 'participant_id')
            suffix = '/%s' % participant_id if participant_id else ''
            blocks.append('#%s [%s] %s/%s%s\n%s' % (
                _pick(entry, 'id'),
                self.bridge.iso_time(_pick(entry, 'occurredAt', 'occurred_at')),
                _pick(entry, 'actor'),
                _pick(entry, 'kind'),
                suffix,
                _pick(entry, 'content'),
            ))
        yield event.plain_result('\n\n'.join(blocks))

    @filter.command('hdsi_script_note')
    async def hdsi_script_note(self, event: AstrMessageEvent):
        """管理员：向剧本写入一条人工注记，不伪装成模型输出。"""
        if self.blind_mode:
            return
        session = await self._prepare(event)
        if not self._is_manager(session):
            yield event.plain_result(NO_MANAGER)
            return
        story = await self._require_story(session)
        if isinstance(story, str):
            yield event.plain_result(story)
            return
        content = self._text_arg(self._raw_args(event), 0)
        wrote = await self.bridge.service.add_admin_script_note(story, content)
        yield event.plain_result(
            '已写入管理员注记，后续压缩会将其纳入连续性。' if wrote else '注记为空，未写入。'
        )

    # ------------------------------------------------------------------ #
    # 命令：长期记忆管理
    # ------------------------------------------------------------------ #

    @filter.command('hdsi_memory_facts')
    async def hdsi_memory_facts(self, event: AstrMessageEvent):
        """管理员：列出长期事实及其编号，默认 20 条。"""
        if self.blind_mode:
            return
        session = await self._prepare(event)
        if not self._is_manager(session):
            yield event.plain_result(NO_MANAGER)
            return
        story = await self._require_story(session)
        if isinstance(story, str):
            yield event.plain_result(story)
            return
        limit = self._int_arg(self._raw_args(event), 0, 20)
        facts = await self.bridge.service.admin_facts(_pick(story, 'id'), limit)
        if not facts:
            yield event.plain_result('当前没有有效的长期事实。')
            return
        blocks = [
            '#%s [%s] 重要度=%s 置信度=%s 未解决=%s\n%s' % (
                _pick(fact, 'id'),
                _pick(fact, 'scope'),
                _format_fixed(_pick(fact, 'importance')),
                _format_fixed(_pick(fact, 'confidence')),
                _pick(fact, 'unresolved'),
                _pick(fact, 'content'),
            )
            for fact in facts
        ]
        yield event.plain_result('\n\n'.join(blocks))

    @filter.command('hdsi_memory_add')
    async def hdsi_memory_add(self, event: AstrMessageEvent):
        """管理员：手动添加长期事实；scope 为 character/world/relationship/event/promise。"""
        if self.blind_mode:
            return
        session = await self._prepare(event)
        if not self._is_manager(session):
            yield event.plain_result(NO_MANAGER)
            return
        args = self._raw_args(event)
        scope = args[0] if args else ''
        if scope not in FACT_SCOPES:
            yield event.plain_result('scope 必须是 character、world、relationship、event 或 promise。')
            return
        story = await self._require_story(session)
        if isinstance(story, str):
            yield event.plain_result(story)
            return
        added = await self.bridge.service.add_admin_fact(story, scope, self._text_arg(args, 1))
        yield event.plain_result('已添加高置信度长期事实。' if added else '事实内容为空，未添加。')

    @filter.command('hdsi_memory_forget')
    async def hdsi_memory_forget(self, event: AstrMessageEvent):
        """管理员：将指定长期事实标记为已失效，可审计且不会物理删除。"""
        if self.blind_mode:
            return
        session = await self._prepare(event)
        if not self._is_manager(session):
            yield event.plain_result(NO_MANAGER)
            return
        story = await self._require_story(session)
        if isinstance(story, str):
            yield event.plain_result(story)
            return
        fact_id = self._int_arg(self._raw_args(event), 0, 0)
        forgot = await self.bridge.service.forget_admin_fact(_pick(story, 'id'), fact_id)
        yield event.plain_result(
            '长期事实 #%d 已标记为失效。' % fact_id if forgot else '未找到有效的长期事实 #%d。' % fact_id
        )

    @filter.command('hdsi_memory_intents')
    async def hdsi_memory_intents(self, event: AstrMessageEvent):
        """管理员：查看等待中的计划、提醒、承诺与剧情余波。"""
        if self.blind_mode:
            return
        session = await self._prepare(event)
        if not self._is_manager(session):
            yield event.plain_result(NO_MANAGER)
            return
        story = await self._require_story(session)
        if isinstance(story, str):
            yield event.plain_result(story)
            return
        limit = self._int_arg(self._raw_args(event), 0, 20)
        intents = await self.bridge.service.admin_pending_intents(_pick(story, 'id'), limit)
        if not intents:
            yield event.plain_result('当前没有等待中的计划、提醒、承诺或剧情余波。')
            return
        blocks = []
        for intent in intents:
            payload = _pick(intent, 'payload') or {}
            active = _pick(intent, 'type') == 'active-consequence' and _pick(payload, 'lifecycle') == 'active'
            timing = (
                '持续影响至=%s' % (_pick(payload, 'expiresAt', 'expires_at') or '未设置')
                if active
                else '最早执行=%s' % self.bridge.iso_time(_pick(intent, 'notBefore', 'not_before'))
            )
            blocks.append('#%s [%s] 参与者=%s %s\n%s' % (
                _pick(intent, 'id'),
                _pick(intent, 'type'),
                _pick(intent, 'participantId', 'participant_id') or '全局',
                timing,
                _pick(intent, 'summary'),
            ))
        yield event.plain_result('\n\n'.join(blocks))

    @filter.command('hdsi_memory_cancel')
    async def hdsi_memory_cancel(self, event: AstrMessageEvent):
        """管理员：取消指定的等待中意图或延迟消息。"""
        if self.blind_mode:
            return
        session = await self._prepare(event)
        if not self._is_manager(session):
            yield event.plain_result(NO_MANAGER)
            return
        story = await self._require_story(session)
        if isinstance(story, str):
            yield event.plain_result(story)
            return
        intent_id = self._int_arg(self._raw_args(event), 0, 0)
        cancelled = await self.bridge.service.cancel_admin_intent(_pick(story, 'id'), intent_id)
        yield event.plain_result(
            '意图 #%d 已取消。' % intent_id if cancelled else '未找到等待中的意图 #%d。' % intent_id
        )

    @filter.command('hdsi_memory_patches')
    async def hdsi_memory_patches(self, event: AstrMessageEvent):
        """管理员：查看人物、关系和世界设定的演化提案。"""
        if self.blind_mode:
            return
        session = await self._prepare(event)
        if not self._is_manager(session):
            yield event.plain_result(NO_MANAGER)
            return
        story = await self._require_story(session)
        if isinstance(story, str):
            yield event.plain_result(story)
            return
        limit = self._int_arg(self._raw_args(event), 0, 20)
        patches = await self.bridge.service.admin_state_patches(_pick(story, 'id'), limit)
        if not patches:
            yield event.plain_result('当前没有设定演化提案。')
            return
        blocks = [
            '#%s [%s/%s/%s] 置信度=%s\n提案：%s\n证据：%s' % (
                _pick(patch, 'id'),
                _pick(patch, 'status'),
                _pick(patch, 'target'),
                _pick(patch, 'impact'),
                _format_fixed(_pick(patch, 'confidence')),
                _pick(patch, 'proposedValue', 'proposed_value'),
                _pick(patch, 'evidence'),
            )
            for patch in patches
        ]
        yield event.plain_result('\n\n'.join(blocks))

    @filter.command('hdsi_memory_reject')
    async def hdsi_memory_reject(self, event: AstrMessageEvent):
        """管理员：拒绝一条尚未应用的设定演化提案。"""
        if self.blind_mode:
            return
        session = await self._prepare(event)
        if not self._is_manager(session):
            yield event.plain_result(NO_MANAGER)
            return
        story = await self._require_story(session)
        if isinstance(story, str):
            yield event.plain_result(story)
            return
        patch_id = self._int_arg(self._raw_args(event), 0, 0)
        rejected = await self.bridge.service.reject_admin_state_patch(_pick(story, 'id'), patch_id)
        yield event.plain_result(
            '设定演化提案 #%d 已拒绝。' % patch_id if rejected
            else '未找到待审核的设定演化提案 #%d。' % patch_id
        )

    @filter.command('hdsi_overlay_clear')
    async def hdsi_overlay_clear(self, event: AstrMessageEvent):
        """管理员：只清理指定部分的设定演化 overlay；执行前会询问 y/n。"""
        if self.blind_mode:
            return
        session = await self._prepare(event)
        if not self._is_manager(session):
            yield event.plain_result(NO_ADMIN)
            return
        target = self._text_arg(self._raw_args(event), 0).strip().lower()
        if target not in OVERLAY_TARGETS:
            yield event.plain_result('target 必须是 character、perspective、relationship、world 或 all。')
            return
        confirmed = await self._ask_confirmation(
            event, '即将清理 %s overlay；剧本和记忆不会删除。确认执行吗？(y/n)' % target,
        )
        if not confirmed:
            yield event.plain_result(CANCELLED)
            return
        story = await self._require_story(session)
        if isinstance(story, str):
            yield event.plain_result(story)
            return
        result = await self.bridge.service.clear_setting_overlay(story, target)
        participant_note = (
            '，已清理 %s 个参与者关系 overlay' % _pick(result, 'participantCount', 'participant_count')
            if target in ('relationship', 'all') else ''
        )
        yield event.plain_result(
            '已清理 %s overlay%s；剧本、长期事实和普通记忆均未删除。' % (target, participant_note)
        )

    @filter.command('hdsi_overlay_status')
    async def hdsi_overlay_status(self, event: AstrMessageEvent):
        """管理员：查看当前 overlay、待积累提案和压缩归档状态。"""
        if self.blind_mode:
            return
        session = await self._prepare(event)
        if not self._is_manager(session):
            yield event.plain_result(NO_ADMIN)
            return
        story = await self._require_story(session)
        if isinstance(story, str):
            yield event.plain_result(story)
            return
        status = await self.bridge.service.admin_overlay_status(_pick(story, 'id'))
        overlay = _dump(_pick(status, 'state'))
        lines = [
            '当前全局 overlay：%s' % ('空' if overlay == '{}' else overlay),
            '待积累提案：%d 条（需要跨多个剧本回合和日期后才会应用）' % len(_pick(status, 'proposed') or []),
            '已应用/已归档提案：%d 条' % len(_pick(status, 'applied') or []),
            '已清理提案：%d 条' % len(_pick(status, 'cleared') or []),
            'overlay 压缩快照：%d 条' % len(_pick(status, 'snapshots') or []),
            '参与者关系 overlay：%d 个' % len(_pick(status, 'participantOverlays', 'participant_overlays') or []),
        ]
        yield event.plain_result('\n'.join(lines))

    @filter.command('hdsi_overlay_compact')
    async def hdsi_overlay_compact(self, event: AstrMessageEvent):
        """管理员：只合并和压缩已应用的 overlay，不整理普通剧本记忆。"""
        if self.blind_mode:
            return
        session = await self._prepare(event)
        if not self._is_manager(session):
            yield event.plain_result(NO_ADMIN)
            return
        story = await self._require_story(session)
        if isinstance(story, str):
            yield event.plain_result(story)
            return
        changed = await self.bridge.service.compact_overlay(story)
        yield event.plain_result('overlay 合并和压缩完成。' if changed else '没有需要合并或压缩的 overlay。')

    # ------------------------------------------------------------------ #
    # 命令：日程
    # ------------------------------------------------------------------ #

    @filter.command('hdsi_schedule')
    async def hdsi_schedule(self, event: AstrMessageEvent):
        """查看 Schedule Preplan 当前版本与未来约半天的日程。"""
        if self.blind_mode:
            return
        session = await self._prepare(event)
        story = await self._require_story(session)
        if isinstance(story, str):
            yield event.plain_result(story)
            return
        record = await self.bridge.service.admin_schedule_preplan(_pick(story, 'id'))
        if not record:
            yield event.plain_result('Schedule Preplan 尚未生成；后台会在空闲整理时建立。')
            return
        timezone = _pick(_pick(story, 'setting'), 'timezone') or 'Asia/Shanghai'
        lines = [
            'Schedule Preplan：版本 %s，覆盖 %s → %s' % (
                _pick(record, 'revision'),
                _pick(record, 'validFrom', 'valid_from'),
                _pick(record, 'validThrough', 'valid_through'),
            ),
            '最后审查：%s；原因：%s' % (
                _pick(record, 'lastReviewedLocalDate', 'last_reviewed_local_date') or '尚未',
                _pick(record, 'reviewReason', 'review_reason') or '无',
            ),
        ]
        blocks = self.bridge.schedule_window_lines(record, timezone)
        lines.append('\n'.join(blocks) if blocks else '未来约半天没有已确定的日程块。')
        yield event.plain_result('\n'.join(lines))

    async def _request_schedule_refresh(self, session: Any) -> str:
        """上游 `requestScheduleRefresh(session)`（`upstream/src/index.ts:811`）。"""
        if not self._is_manager(session):
            return NO_ADMIN
        story = await self._require_story(session)
        if isinstance(story, str):
            return story
        marked = await self.bridge.service.request_schedule_preplan_rebuild(_pick(story, 'id'))
        return (
            'Schedule Preplan 已标记为重新审查；会在当前前台回合结束后的空闲队列中处理。'
            if marked else '当前还没有 Schedule Preplan；后台会自动建立。'
        )

    @filter.command('hdsi_schedule_refresh')
    async def hdsi_schedule_refresh(self, event: AstrMessageEvent):
        """管理员：重新审查当前 Schedule Preplan，并保留旧计划作为稳定参考。"""
        if self.blind_mode:
            return
        session = await self._prepare(event)
        yield event.plain_result(await self._request_schedule_refresh(session))

    @filter.command('hdsi_schedule_rebuild')
    async def hdsi_schedule_rebuild(self, event: AstrMessageEvent):
        """管理员：兼容别名，等同于 `hdsi_schedule_refresh`。"""
        if self.blind_mode:
            return
        session = await self._prepare(event)
        yield event.plain_result(await self._request_schedule_refresh(session))

    # ------------------------------------------------------------------ #
    # 命令：删除剧本和记忆
    # ------------------------------------------------------------------ #

    @filter.command('hdsi_database_clear')
    async def hdsi_database_clear(self, event: AstrMessageEvent):
        """管理员：清空 HDSI 自有 SQLite 数据表；执行前会询问 y/n。"""
        if self.blind_mode:
            return
        session = await self._prepare(event)
        if not self._is_manager(session):
            yield event.plain_result(NO_ADMIN)
            return
        confirmed = await self._ask_confirmation(
            event, '即将清空 HDSI 自有数据库，剧本、记忆和状态记录都会删除。确认执行吗？(y/n)',
        )
        if not confirmed:
            yield event.plain_result(CANCELLED)
            return
        result = await self.bridge.service.clear_database()
        removed = _pick(result, 'removed')
        logically = _pick(result, 'logicallyCleared', 'logically_cleared')
        suffix = '，其中 %s 条因 SQLite 锁定改为逻辑清空' % logically if logically else ''
        yield event.plain_result('HDSI 数据库清空完成：处理 %s 条记录%s。' % (removed, suffix))

    @filter.command('hdsi_purge_all')
    async def hdsi_purge_all(self, event: AstrMessageEvent):
        """管理员：彻底重置所有平台的剧本、记忆与 Canon；执行前会询问 y/n。"""
        if self.blind_mode:
            return
        session = await self._prepare(event)
        if not self._is_manager(session):
            yield event.plain_result(NO_MANAGER)
            return
        confirmed = await self._ask_confirmation(
            event, '即将删除所有平台的剧本、记忆、事实、意图和状态。确认执行吗？(y/n)',
        )
        if not confirmed:
            yield event.plain_result(CANCELLED)
            return
        story = await self._require_story(session)
        if isinstance(story, str):
            yield event.plain_result(story)
            return
        await self.bridge.service.purge_all_data(_pick(story, 'id'))
        yield event.plain_result(
            '已彻底重置所有平台：旧剧本、场景摘要、剧情弧线、长期事实、记忆、意图、状态演化和参与者关系状态均已清除；'
            '当前故事保留为空白的全局主剧本，Canon 已按当前 Console 配置重建。'
        )

    @filter.command('hdsi_purge_platform')
    async def hdsi_purge_platform(self, event: AstrMessageEvent):
        """管理员：删除指定平台的全部剧本和记忆；例如 sandbox 或 onebot。"""
        if self.blind_mode:
            return
        session = await self._prepare(event)
        if not self._is_manager(session):
            yield event.plain_result(NO_MANAGER)
            return
        platform = self._text_arg(self._raw_args(event), 0).strip()
        confirmed = await self._ask_confirmation(
            event, '即将删除平台 %s 的全部剧本和记忆。确认执行吗？(y/n)' % platform,
        )
        if not confirmed:
            yield event.plain_result(CANCELLED)
            return
        normalized = platform.lower()
        if not normalized:
            yield event.plain_result('请填写平台名，例如 sandbox 或 onebot。')
            return
        count = await self.bridge.service.purge_platform_data(normalized)
        if count:
            yield event.plain_result('已清空并归档平台 %s 的 %d 部剧本；其它平台不受影响。' % (normalized, count))
        else:
            yield event.plain_result('没有找到平台 %s 的 HDSI 剧本。' % normalized)

    @filter.command('hdsi_purge_range')
    async def hdsi_purge_range(self, event: AstrMessageEvent):
        """管理员：删除时间范围内的剧本和关联记忆；时间使用 ISO-8601；执行前会询问 y/n。"""
        if self.blind_mode:
            return
        session = await self._prepare(event)
        if not self._is_manager(session):
            yield event.plain_result(NO_MANAGER)
            return
        args = self._raw_args(event)
        from_value = _parse_iso(_text(args[0]).strip() if args else '')
        to_value = _parse_iso(_text(args[1]).strip() if len(args) > 1 else '')
        if from_value is None or to_value is None or from_value > to_value:
            yield event.plain_result('时间范围无效，请使用 ISO-8601，例如 2026-08-01T00:00:00+08:00。')
            return
        confirmed = await self._ask_confirmation(
            event,
            '即将删除 %s 至 %s 范围内的剧本和关联记忆。确认执行吗？(y/n)'
            % (self.bridge.iso_time(from_value), self.bridge.iso_time(to_value)),
        )
        if not confirmed:
            yield event.plain_result(CANCELLED)
            return
        story = await self._require_story(session)
        if isinstance(story, str):
            yield event.plain_result(story)
            return
        await self.bridge.service.purge_story_range(_pick(story, 'id'), from_value, to_value)
        yield event.plain_result(
            '已删除 %s 至 %s 范围内的剧本和关联记忆；Canon 与参与者身份未删除。'
            % (self.bridge.iso_time(from_value), self.bridge.iso_time(to_value))
        )

    # ------------------------------------------------------------------ #
    # 配置导出 / 导入（本移植版新增，上游 Koishi 没有）
    # ------------------------------------------------------------------ #

    @filter.command('hdsi_config_export')
    async def hdsi_config_export(self, event: AstrMessageEvent):
        """管理员：把当前插件配置导出成一个 JSON 文件（同时落盘到插件数据目录）。

        导出的是**用户实际存下来的那份配置**（原样，不做归一化），文件里带
        `format` / `formatVersion` / 插件与上游版本，所以以后升级插件也能导回来。
        """
        if self.blind_mode:
            return
        session = await self._prepare(event)
        if not self._is_manager(session):
            yield event.plain_result(NO_MANAGER)
            return

        from .adapters.astrbot_bridge import _plugin_version  # noqa: PLC0415
        from .core.config_io import export_filename  # noqa: PLC0415

        envelope = self.bridge.export_config()
        directory = os.path.join(self.bridge.data_dir, 'exports')
        os.makedirs(directory, exist_ok=True)
        filename = export_filename(_plugin_version())
        path = os.path.join(directory, filename)
        try:
            with open(path, 'w', encoding='utf-8') as handle:
                json.dump(envelope, handle, ensure_ascii=False, indent=2)
        except OSError as error:
            yield event.plain_result('导出失败：无法写入 %s（%s）' % (path, error))
            return

        sections = envelope.get('sections') or []
        summary = '\n'.join([
            '配置已导出。',
            '插件版本：%s' % envelope.get('pluginVersion') or '-',
            '上游版本：%s' % (envelope.get('upstreamVersion') or '-'),
            '分组：%d 个（%s）' % (len(sections), '、'.join(sections[:6]) + ('…' if len(sections) > 6 else '')),
            '文件：%s' % path,
            '提示：这份文件不含剧情数据，只含配置；以后升级插件后仍可用 hdsi_config_import 导回来。',
        ])
        try:
            from astrbot.api.message_components import File, Plain  # noqa: PLC0415

            yield event.chain_result([Plain(summary), File(name=filename, file=path)])
        except Exception:  # noqa: BLE001 - 平台不支持文件段时只发文本，路径已经给出
            yield event.plain_result(summary)

    @filter.command('hdsi_config_import')
    async def hdsi_config_import(self, event: AstrMessageEvent):
        """管理员：导入一份配置；支持引用一份导出的文件，或直接在命令后粘贴 JSON。

        兼容性（**硬要求**）：带信封的新文件、早期版本没有信封的裸配置、手写的片段、
        甚至更新版本插件导出的文件都能导入；未知键原样保留，缺失键补默认值。
        """
        if self.blind_mode:
            return
        session = await self._prepare(event)
        if not self._is_manager(session):
            yield event.plain_result(NO_MANAGER)
            return

        from .core.config_io import ConfigImportError  # noqa: PLC0415

        payload = self._config_import_payload(event)
        if payload is None:
            yield event.plain_result(
                '没找到配置内容。用法：\n'
                '1) 引用（回复）一份 hdsi_config_export 导出的文件后发送 hdsi_config_import；\n'
                '2) 或把 JSON 直接贴在命令后面：hdsi_config_import {"runtime":{"auto_create":true}}'
            )
            return

        try:
            preview = self.bridge.preview_config_import(payload)
        except ConfigImportError as error:
            yield event.plain_result('导入失败：%s' % error)
            return

        diff = preview.get('diff') or {}
        lines = [
            '配置导入预览',
            '文件格式：v%s（%s）' % (preview.get('format_version'), preview.get('source')),
            '分组数：%s' % preview.get('section_count'),
            '变更：修改 %d 项，新增 %d 项，删除 %d 项'
            % (len(diff.get('changed') or []), len(diff.get('added') or []), len(diff.get('removed') or [])),
        ]
        changed = list(diff.get('changed') or [])
        if changed:
            lines.append('将被覆盖：%s' % '、'.join(changed[:8]) + ('…' if len(changed) > 8 else ''))
        for note in (preview.get('notes') or [])[:3]:
            lines.append('说明：%s' % note)
        for warning in (preview.get('warnings') or [])[:3]:
            lines.append('注意：%s' % warning)
        if not await self._ask_confirmation(event, '\n'.join(lines) + '\n确认导入吗？(y/n)'):
            yield event.plain_result(CANCELLED)
            return

        try:
            report = await self.bridge.import_config(payload)
        except ConfigImportError as error:
            yield event.plain_result('导入失败：%s' % error)
            return
        except Exception as error:  # noqa: BLE001 - 写盘失败等
            yield event.plain_result('导入失败：%s' % error)
            return

        final = report.get('diff') or {}
        yield event.plain_result('\n'.join([
            '配置已导入并生效。',
            '写入方式：%s' % report.get('saved_via'),
            '配置文件：%s' % report.get('config_path'),
            '本次修改 %d 项、新增 %d 项（未出现在文件里的项保持原值）。'
            % (len(final.get('changed') or []), len(final.get('added') or [])),
        ]))

    def _config_import_payload(self, event: AstrMessageEvent) -> Optional[str]:
        """按优先级找导入内容：命令后的内联 JSON → 本消息附件 → 被引用消息的附件。

        内联 JSON 取的是**命令词之后的原始文本**（只切掉命令词），不做按空格重新拼接——
        否则 JSON 字符串值里的连续空格会被压掉。
        """
        raw = _text(event.get_message_str()).strip()
        parts = raw.split(None, 1)
        if len(parts) > 1:
            inline = parts[1].strip()
            # 附件消息的正文里也会带上文件名，只有看着像 JSON 才当内联内容
            if inline.startswith(('{', '[')):
                return inline
        for components in self._attachment_groups(event):
            for component in components:
                content = _read_file_component(component)
                if content:
                    return content
        return None

    @staticmethod
    def _attachment_groups(event: AstrMessageEvent) -> list[list[Any]]:
        """本消息的组件列表 + 它引用（回复）的那条消息的组件列表。"""
        groups: list[list[Any]] = []
        try:
            current = list(event.get_messages() or [])
        except Exception:  # noqa: BLE001
            current = []
        if current:
            groups.append(current)
        for component in current:
            chain = getattr(component, 'chain', None)
            if isinstance(chain, (list, tuple)) and chain:
                groups.append(list(chain))
        return groups
