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
    PLUGIN_NAME,
    build_bridge,
    endpoint_for_event,
    session_view,
)
from .adapters.console_api import ConsoleApi

__all__ = ['COMMANDS', 'COMMAND_HANDLERS', 'HDSInterludePlugin', 'MANAGEMENT_COMMANDS']


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
)

#: AstrBot 命令名 → 处理器方法名。
COMMAND_HANDLERS: dict[str, str] = {spec.command: spec.handler for spec in COMMANDS}

#: 盲区模式下需要从 `star_handlers_registry` 摘除的 handler 方法名。
MANAGEMENT_COMMANDS: frozenset[str] = frozenset(COMMAND_HANDLERS.values())

#: 上游 `isFactScope`（`upstream/src/index.ts:911`）。
FACT_SCOPES = ('character', 'world', 'relationship', 'event', 'promise')

#: 上游 `interlude.overlay.clear` 的 `target` 枚举。
OVERLAY_TARGETS = ('character', 'perspective', 'relationship', 'world', 'all')

#: 上游 `askConfirmation` 的等待时长（60 秒）。
CONFIRMATION_TIMEOUT_SECONDS = 60

#: 启动自检等待 Provider 管理器就绪的上限秒数（AstrBot 4.28 里插件先于模型加载）。
CAPABILITY_CHECK_WAIT_SECONDS = 45

#: 上游 `askConfirmation` 的肯定回答正则：`/^(?:y|yes)$/i`。
CONFIRMATION_YES_RE = re.compile(r'^(?:y|yes)$', re.IGNORECASE)


def _to_int(value: Any, default: int) -> int:
    """查询参数转 int（拿不到/格式不对就用默认值，别让一个坏参数把面板打挂）。"""
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


async def _page_import_payload() -> Optional[str]:
    """从插件页请求里取配置文本。

    优先级：**multipart 上传的文件**（`file` / `config` 字段）→ JSON body 里的
    `payload`（文本）或 `config`（对象）。取不到返回 `None`，由调用方给出提示。
    """
    from astrbot.api.web import request  # noqa: PLC0415 - 宿主 API，测试里用桩

    try:
        files = await request.files()
    except Exception:  # noqa: BLE001 - 不是 multipart
        files = None
    if files:
        upload = files.get('file') or files.get('config')
        if upload is not None:
            try:
                data = await upload.read()
            except Exception:  # noqa: BLE001
                data = None
            if data:
                return data.decode('utf-8-sig', errors='replace')

    try:
        body = await request.json(default=None)
    except Exception:  # noqa: BLE001
        body = None
    if isinstance(body, dict):
        if isinstance(body.get('payload'), str):
            return body['payload']
        if isinstance(body.get('config'), dict):
            return json.dumps(body['config'], ensure_ascii=False)
    return None


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
        #: 控制台（WebUI 插件页面）的取数入口；逻辑在 `adapters/console_api.py`。
        self._console = ConsoleApi(self.bridge)
        self._register_config_page_apis(context)
        #: 盲区模式（上游 `blindMode.enabled`，兼容旧键 `blackBox.enabled`）。
        self.blind_mode: bool = self.bridge.blind_mode_enabled
        #: 被摘除的管理命令方法名（盲区模式下非空）。
        self.suppressed_commands: tuple[str, ...] = ()
        #: 等待 y/n 确认的回调（key = `unified_msg_origin`）。
        self._confirmations: dict[str, asyncio.Future] = {}
        #: 启动自检的后台任务（`initialize()` 里创建，`terminate()` 里取消）。
        self._capability_task: asyncio.Task | None = None
        if self.blind_mode:
            self.suppressed_commands = self._suppress_management_commands()
            logger.info(
                'hds-interlude：盲区模式已开启，%d 条管理命令不注册；普通叙事不受影响。'
                % len(self.suppressed_commands)
            )
        else:
            logger.info('hds-interlude：插件加载开始')

    async def initialize(self) -> None:
        """AstrBot 的异步初始化钩子：起一个**延后的模型能力自检**。

        为什么是"延后"：**AstrBot 4.28 里插件先于模型加载**——`initialize()` 跑的时候
        `provider_manager` 还是空的（实测：`context.get_provider_by_id()` 返回 `None`
        并打一条 "Provider … was not found" 宿主警告，模型要再过约 0.5 秒才装上）。
        所以这里只登记一个后台任务，等 Provider 就绪后再自检；顺带也避免了那条
        误导性的宿主警告。

        自检要解决的问题：用户配了 `vision.mode = native`（默认）却选了一个只声明
        `text` 的主模型时，图片会静默失效。结论会进日志，`hdsi_status` 里也带同一句话。
        """
        try:
            self._capability_task = asyncio.create_task(self._self_check_model_capabilities())
        except RuntimeError:  # pragma: no cover - 没有运行中的事件循环（测试/极旧宿主）
            self._capability_task = None

    async def _self_check_model_capabilities(self) -> None:
        """等 Provider 管理器就绪，再做一次能力自检（失败绝不影响插件运行）。"""
        try:
            for _ in range(CAPABILITY_CHECK_WAIT_SECONDS):
                if self.bridge.any_provider_loaded():
                    break
                await asyncio.sleep(1)
            await self.bridge.log_model_capabilities()
        except asyncio.CancelledError:  # pragma: no cover - 关插件时正常取消
            raise
        except Exception as error:  # noqa: BLE001 - 自检失败绝不能挡住插件启动
            logger.warning('hds-interlude：模型能力自检失败：%s' % error)

    # ------------------------------------------------------------------ #
    # 配置备份页（WebUI 插件页面 `pages/config-backup/`）
    # ------------------------------------------------------------------ #

    def _register_config_page_apis(self, context: Context) -> None:
        """注册插件控制台页面（`pages/console/`）用的 Web API。

        **为什么不做成聊天命令**：这些是配置 / 观测界面的事，跟聊天无关。
        AstrBot 的内置配置页由 `_conf_schema.json` 驱动、插不进自定义按钮，官方给的
        扩展点是**插件页面**（`pages/<名>/index.html` + `window.AstrBotPluginPage`
        bridge，明确支持文件上传下载与自定义交互），所以这里注册页面要调的后端接口，
        配套页面（Vite + Preact 构建产物）放在 `plugin/pages/console/`。

        取数逻辑在 `adapters/console_api.py`；这里只做路由注册与响应包装，
        免得 `main.py` 继续膨胀。
        """
        specs = (
            # 控制台各面板（全部 GET，只读）
            (f'/{PLUGIN_NAME}/console/overview', self.page_console_overview, ['GET'],
             '控制台：总览'),
            (f'/{PLUGIN_NAME}/console/models', self.page_console_models, ['GET'],
             '控制台：模型与用量'),
            (f'/{PLUGIN_NAME}/console/script', self.page_console_script, ['GET'],
             '控制台：剧本条目'),
            (f'/{PLUGIN_NAME}/console/memory', self.page_console_memory, ['GET'],
             '控制台：记忆与事实'),
            (f'/{PLUGIN_NAME}/console/database', self.page_console_database, ['GET'],
             '控制台：数据库概览'),
            (f'/{PLUGIN_NAME}/console/logs', self.page_console_logs, ['GET'],
             '控制台：运行日志'),
            (f'/{PLUGIN_NAME}/console/alter', self.page_console_alter, ['GET'],
             '控制台：Alter 情绪'),
            (f'/{PLUGIN_NAME}/console/agency', self.page_console_agency, ['GET'],
             '控制台：Agency 与日程'),
            (f'/{PLUGIN_NAME}/console/delivery', self.page_console_delivery, ['GET'],
             '控制台：投递账本'),
            # 写操作（控制台里唯一会改状态的两处，都是白名单）
            (f'/{PLUGIN_NAME}/console/flags', self.page_console_set_flag, ['POST'],
             '控制台：切换运行开关'),
            (f'/{PLUGIN_NAME}/console/connections', self.page_console_save_connection, ['POST'],
             '控制台：新增 / 修改模型连接'),
            (f'/{PLUGIN_NAME}/console/connections-delete', self.page_console_delete_connection, ['POST'],
             '控制台：删除模型连接'),
            # 配置备份（原 config-backup 页并入控制台）
            (f'/{PLUGIN_NAME}/config-export', self.page_config_export, ['GET'],
             '导出 HDS Interlude 配置'),
            (f'/{PLUGIN_NAME}/config-import-preview', self.page_config_import_preview, ['POST'],
             '配置导入预览'),
            (f'/{PLUGIN_NAME}/config-import-apply', self.page_config_import_apply, ['POST'],
             '应用配置导入'),
        )
        for route, handler, methods, desc in specs:
            try:
                context.register_web_api(route, handler, methods, desc)
            except Exception as error:  # noqa: BLE001 - 宿主版本漂移时别拖垮插件加载
                logger.warning('hds-interlude：注册控制台 API %s 失败：%s' % (route, error))

    # ---- 控制台各面板（薄包装：取数在 adapters/console_api.py） ---- #

    async def page_console_overview(self):
        return await self._console_json(lambda api, q: api.overview(q('story_id')))

    async def page_console_models(self):
        return await self._console_json(lambda api, q: api.models(q('story_id')))

    async def page_console_script(self):
        return await self._console_json(lambda api, q: api.script(
            q('story_id'), _to_int(q('limit'), 60), _to_int(q('offset'), 0),
        ))

    async def page_console_memory(self):
        return await self._console_json(lambda api, q: api.memory(q('story_id')))

    async def page_console_database(self):
        return await self._console_json(lambda api, q: api.database())

    async def page_console_logs(self):
        return await self._console_json(lambda api, q: api.logs(
            _to_int(q('limit'), 200), q('level'),
        ))

    async def page_console_alter(self):
        return await self._console_json(lambda api, q: api.alter(q('story_id')))

    async def page_console_agency(self):
        return await self._console_json(lambda api, q: api.agency(q('story_id')))

    async def page_console_delivery(self):
        return await self._console_json(lambda api, q: api.delivery(
            q('story_id'), _to_int(q('limit'), 300), q('status'),
        ))

    # ---- 控制台的写操作 ---- #

    async def page_console_set_flag(self):
        return await self._console_write(lambda api, body: api.set_flag(
            body.get('name'), body.get('value'),
        ))

    async def page_console_save_connection(self):
        return await self._console_write(lambda api, body: api.save_connection(body))

    async def page_console_delete_connection(self):
        return await self._console_write(lambda api, body: api.delete_connection(body.get('index')))

    async def _console_write(self, action):
        """跑一个控制台写操作。

        跟读接口的区别：用户能直接看到失败原因（比如"地址格式不对"），所以
        `ConsoleError` 映射成 400 + 原文案；其它异常仍是 500 + 泛化文案。
        每次成功的写操作都记一条 warn 日志——控制台改了用户的配置，得留下痕迹。
        """
        from astrbot.api.web import error_response, json_response, request

        from .adapters.console_api import ConsoleError

        try:
            body = await request.json(default=None)
        except Exception:  # noqa: BLE001 - 不是 JSON 就当空对象
            body = None
        if not isinstance(body, dict):
            return error_response('请求体必须是 JSON 对象', status_code=400)
        try:
            payload = await action(self._console, body)
        except ConsoleError as error:
            return error_response(str(error), status_code=400)
        except Exception as error:  # noqa: BLE001
            logger.warning('hds-interlude：控制台写操作失败：%s' % error)
            return error_response('操作失败：%s' % error, status_code=500)
        logger.warning('hds-interlude：控制台修改了配置：%s' % payload.get('changed', '?'))
        return json_response(payload)

    async def _console_json(self, loader):
        """跑一个控制台取数函数并把结果包成 JSON 响应。

        单个面板出错不该让整页打不开，所以这里把异常转成 500 + 可读文案；
        前端会把它显示在对应面板里。
        """
        from astrbot.api.web import error_response, json_response, request

        def query(name: str) -> str:
            try:
                value = request.query.get(name, '')
            except Exception:  # noqa: BLE001 - 取不到查询参数就当空
                return ''
            return str(value) if value is not None else ''

        try:
            return json_response(await loader(self._console, query))
        except Exception as error:  # noqa: BLE001
            logger.warning('hds-interlude：控制台取数失败：%s' % error)
            return error_response('控制台取数失败：%s' % error, status_code=500)

    async def page_config_export(self):
        """下载当前配置（带格式信封，AstrBot 会按 `filename` 触发下载）。"""
        from astrbot.api.web import error_response, file_response

        from .adapters.astrbot_bridge import _plugin_version
        from .core.config_io import export_filename

        try:
            envelope = self.bridge.export_config()
        except Exception as error:  # noqa: BLE001
            return error_response('导出失败：%s' % error, status_code=500)

        filename = export_filename(_plugin_version())
        path = os.path.join(self.bridge.data_dir, 'exports', filename)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w', encoding='utf-8') as handle:
                json.dump(envelope, handle, ensure_ascii=False, indent=2)
        except OSError as error:
            return error_response('导出失败：无法写入 %s（%s）' % (path, error), status_code=500)
        return file_response(path, filename=filename, content_type='application/json')

    async def page_config_import_preview(self):
        """解析上传的配置并返回变更预览（**不写盘**）。"""
        from astrbot.api.web import error_response, json_response

        from .core.config_io import ConfigImportError

        payload = await _page_import_payload()
        if payload is None:
            return error_response('没有收到配置文件，请选择一个由本插件导出的 JSON 文件。')
        try:
            preview = self.bridge.preview_config_import(payload)
        except ConfigImportError as error:
            return error_response(str(error))
        return json_response({
            'report': {
                'format_version': preview.get('format_version'),
                'source': preview.get('source'),
                'section_count': preview.get('section_count'),
                'diff': preview.get('diff'),
                'notes': preview.get('notes'),
                'warnings': preview.get('warnings'),
            },
            # 回给前端、确认时原样送回：服务端不留 pending，无状态最省心
            'payload': payload,
        })

    async def page_config_import_apply(self):
        """应用导入（前端把预览时拿到的 payload 原样送回）。"""
        from astrbot.api.web import error_response, json_response, request

        from .core.config_io import ConfigImportError

        payload = None
        try:
            body = await request.json(default=None)
        except Exception:  # noqa: BLE001
            body = None
        if isinstance(body, dict) and isinstance(body.get('payload'), str):
            payload = body['payload']
        if payload is None:
            payload = await _page_import_payload()
        if payload is None:
            return error_response('没有收到要导入的配置。')

        try:
            report = await self.bridge.import_config(payload)
        except ConfigImportError as error:
            return error_response(str(error))
        except Exception as error:  # noqa: BLE001
            return error_response('导入失败：%s' % error, status_code=500)
        return json_response({
            'saved_via': report.get('saved_via'),
            'config_path': report.get('config_path'),
            'format_version': report.get('format_version'),
            'diff': report.get('diff'),
        })

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def terminate(self) -> None:
        """优雅关闭：停后台任务、关 HTTP 客户端与数据库。"""
        self._confirmations.clear()
        task = getattr(self, '_capability_task', None)
        if task is not None and not task.done():
            task.cancel()
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
        # 能力自检需要知道「到底是哪个模型在服务」：会话默认模型是异步解析的，
        # 这里补解析一次，`hdsi_status` 才能在第一次对话之前就给出结论。
        if not self.bridge.task_model_id('main') and not self.bridge._resolved_chat_provider_id:
            await self.bridge.resolve_chat_provider_id()
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
            *[
                '模型能力：%s' % note
                for note in (
                    self.bridge.image_capability_note(),
                    self.bridge.audio_capability_note(),
                )
                if note
            ],
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
