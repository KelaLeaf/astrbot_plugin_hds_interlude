"""HDS Interlude 的 AstrBot 插件入口。

只碰 AstrBot 的插件 API 与 `plugin/adapters/`；叙事核心的一切都经 `AstrbotBridge`
（`self.bridge`）转发。命令注册、消息中间件、生命周期、权限判定与上游 `src/index.ts`
逐条对应，命令名从上游的点号层级改成下划线形式（`interlude.memory.facts` 在这里叫
`hdsi_memory_facts`，上游写法也照样认）。

盲区模式开启时，本插件注册到 `star_handlers_registry` 的管理命令会被真正摘掉，
管理命令完全不响应，而私聊/群聊叙事照常。
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
    is_non_message_event,
    session_view,
)
from .adapters.console_api import ConsoleApi
from .core.health import format_health_lines
from .core.video_understanding import apply_ffmpeg_status_hint, resolve_video_config

__all__ = ['COMMANDS', 'COMMAND_HANDLERS', 'HDSInterludePlugin', 'MANAGEMENT_COMMANDS']


def video_mode_label(config: Any) -> str:
    """启动日志里那句"当前视频识别模式"（`enabled=false` 时明说关着）。

    段位与 core 一致：schema 分组名是 `model_center`，core 读的仍是 `model`，
    两处都认（见 `core/video_understanding.video_config`）。
    """
    section = config.get('model') if isinstance(config, dict) else None
    if not isinstance(section, dict):
        section = config.get('model_center') if isinstance(config, dict) else None
    resolved = resolve_video_config(
        section.get('video') if isinstance(section, dict) else None,
    )
    if not resolved['enabled']:
        return '关闭（识别模式 %s 只在开启后生效）' % resolved['mode']
    return '开启（识别模式 %s）' % resolved['mode']


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
    """用法。"""


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
    # ── 上游 1.0.1-rc28（单剧本多通道 M1a/M1b/M2）────────────────────────────
    CommandSpec(
        'interlude.participant.link', 'hdsi_participant_link', 'hdsi_participant_link', 'admin',
        'hdsi_participant_link <参与者ID> <用户ID>',
    ),
    CommandSpec(
        'interlude.participant.unlink', 'hdsi_participant_unlink', 'hdsi_participant_unlink', 'admin',
        'hdsi_participant_unlink <端点ID>',
    ),
    CommandSpec(
        'interlude.participant.endpoints', 'hdsi_participant_endpoints', 'hdsi_participant_endpoints', 'admin',
        'hdsi_participant_endpoints <参与者ID>',
    ),
    CommandSpec(
        'interlude.story.endpoint', 'hdsi_story_endpoint', 'hdsi_story_endpoint', 'admin',
        'hdsi_story_endpoint [add <平台> <账号> [qq|wechat] | disable <端点ID>]',
    ),
    CommandSpec(
        'interlude.story.alias', 'hdsi_story_alias', 'hdsi_story_alias', 'admin',
        'hdsi_story_alias [remove <别名ID>]',
    ),
    CommandSpec('interlude.reset', 'hdsi_reset', 'hdsi_reset', 'admin', 'hdsi_reset'),
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

#: 管理命令的统一回执文案（上游 `index.ts` 逐字）。
#: ⚠️ 这几个常量在某次重构里被连定义一起删掉、只留了引用，于是**所有**取消/无权限
#: 分支都抛 `NameError`（用户在 `/hdsi_purge_all` 上踩到：回复 n 时崩，命令没执行成）。
#: 测试 `test_command_guards.py` 专门钉住它们存在。
NO_MANAGER = '当前 QQ 没有共享主剧本的管理权限。'
NO_MANAGER_DETAIL = (
    '当前 QQ 没有共享主剧本的管理权限。'
    '请在 Console 的 sharedStory.managerAccounts 中添加此 QQ，或留空允许所有获授权账号。'
)
NO_ADMIN = '无权限：当前账号不是 HDSI 管理员。'
CANCELLED = '操作已取消。'


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


#: 上传表情时允许随请求带的三个可选参数（表单字段名，也是查询串上的名字）。
_STICKER_UPLOAD_FIELDS: tuple[tuple[str, str], ...] = (
    ('groupId', 'group_id'),
    ('description', 'description'),
    ('name', 'name'),
)


async def _sticker_upload_payload() -> tuple[Optional[bytes], dict[str, str]]:
    """从插件页请求里取**上传的表情字节 + 可选参数**（控制台「上传表情」）。

    两条通道的来由（宿主 bridge 的实际能力，见 astrbot 的 `plugin_page_bridge.js`
    里 `files:upload` 分支）：

    * 字节只走 `multipart/form-data` 的 **`file`** 字段——宿主 `upload(endpoint, file)`
      的字段名是写死的（`form.append("file", …)`），`upload` 只是给手敲 curl 的宽容别名；
    * `groupId` / `description` / `name` **表单字段优先，其次查询串**：bridge 只能发
      一个文件字段，前端唯一能带参数的路就是把它们挂在端点的查询串上。
      两种拼写都认（wire 是 camelCase，手敲 curl 的人常写 snake_case）。

    **上传的文件名一个字符都不进业务**：命名一律用内容哈希（见
    `chunk2.upload_sticker_asset`），这里连 `upload.filename` 都不读。
    """
    from astrbot.api.web import request  # noqa: PLC0415 - 宿主 API，测试里用桩

    try:
        form = await request.form()
    except Exception:  # noqa: BLE001 - 不是 multipart / 老宿主没有 form()
        form = None
    options: dict[str, str] = {}
    for camel, snake in _STICKER_UPLOAD_FIELDS:
        value: Any = None
        if form is not None:
            try:
                value = form.get(camel)
            except Exception:  # noqa: BLE001 - 表单桩缺 get 就当没给
                value = None
        if value is None:
            try:
                value = request.query.get(camel)
                if value is None and snake != camel:
                    value = request.query.get(snake)
            except Exception:  # noqa: BLE001 - 拿不到查询参数就是没给
                value = None
        if value is not None:
            options[camel] = value if isinstance(value, str) else str(value)

    data: Optional[bytes] = None
    try:
        files = await request.files()
    except Exception:  # noqa: BLE001 - 不是 multipart
        files = None
    if files:
        upload = files.get('file') or files.get('upload')
        if upload is not None:
            try:
                data = await upload.read()
            except Exception:  # noqa: BLE001
                data = None
            if isinstance(data, str):  # 桩/老宿主回文本时按字节处理，别把 str 传下去
                data = data.encode('utf-8', errors='replace')
    return data, options


def _text(value: Any) -> str:
    return '' if value is None else str(value)


#: 表情包按扩展名给的 `Content-Type`（控制台的 `sticker-file` 用）。
#: 与 `core/service/chunk2.sticker_mime` 同一套映射；这里不跨层 import 那个私有实现，
#: 因为 `main.py` 的约定是"只经 bridge"。
_STICKER_CONTENT_TYPES = {
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.gif': 'image/gif',
    '.webp': 'image/webp',
}


def _sticker_content_type(path: str) -> str:
    """按扩展名给图片 MIME（认不出来按 PNG：浏览器仍会按魔数渲染）。"""
    return _STICKER_CONTENT_TYPES.get(os.path.splitext(_text(path))[1].lower(), 'image/png')


#: `sticker-file?inline=` 认的真值写法（前端 bridge 发的是 `inline=1`；
#: 其余写法只是方便手敲 curl 调试，缺省 / 其他值一律走 blob 老行为）。
_STICKER_INLINE_TRUE = ('1', 'true', 'yes', 'on')


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


def _local_time_text(value: Any) -> str:
    """把 ISO 时间戳渲染成本地可读文本（别名列表用；解析不出就原样回显）。"""
    parsed = _parse_iso(value) if isinstance(value, str) else value
    if not isinstance(parsed, datetime):
        return str(value or '')
    return parsed.astimezone().strftime('%Y-%m-%d %H:%M')


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
        #: 视频抽帧识别的 FFmpeg 状态（v1.9.0）：同时写进**内存里的** schema，
        #: 让配置页「识别模式」旁边显示「✅ FFmpeg 已识别 / ⚠️ 未发现 FFmpeg」
        #: （文本标记：宿主的 hint 是纯文本渲染，着不了色，依据见那个模块的注释）。
        #: 宿主每打开一次配置页都现取 `config.schema` 这个活对象
        #: （`astrbot/dashboard/services/config_service.py:866`），而
        #: `AstrbotConfig.save_config()` 只写配置值、**schema 从不落盘**
        #: （`astrbot/core/config/astrbot_config.py:262-272`）——所以仓库里的
        #: `_conf_schema.json` 一个字都不改。详见 `core/video_understanding.py`。
        #: 拿不到 schema（测试 / 极旧宿主）时它只回一个状态串，不抛异常。
        self.video_ffmpeg_status: str = apply_ffmpeg_status_hint(
            getattr(self.config, 'schema', None),
        )
        #: 盲区模式（上游 `blindMode.enabled`，兼容旧键 `blackBox.enabled`）。
        self.blind_mode: bool = self.bridge.blind_mode_enabled
        #: 被摘除的管理命令方法名（盲区模式下非空）。
        self.suppressed_commands: tuple[str, ...] = ()
        #: 等待 y/n 确认的回调（key = `unified_msg_origin`）。
        self._confirmations: dict[str, asyncio.Future] = {}
        #: 启动自检的后台任务（`initialize()` 里创建，`terminate()` 里取消）。
        self._capability_task: asyncio.Task | None = None
        # 一条节流日志（每次加载一条；视频理解真跑起来时缺 FFmpeg 会有可行动的 warn，
        # 那条走 `bridge.video_capability_note()` 的启动自检）。
        logger.info(
            'hds-interlude：视频抽帧识别 %s（%s）'
            % (self.video_ffmpeg_status, video_mode_label(self.config))
        )
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
        self._log_access()

    def _log_access(self) -> None:
        """启动时就说清楚"接入与名单"的现状。

        被名单挡掉是**完全静默**的：消息进得来、handler 也被调到，但什么都不发生
        （用户 2026-09-26 的日志就是这样，直到主动来问才发现）。这里把三张名单各自的
        现状（几条 / 是否"仅名单内"）打进启动日志；"开了仅名单内但名单是空的"这种
        一定不会生效的组合用 warn。
        """
        try:
            notes = self.bridge.service.describe_access()
        except Exception as error:  # noqa: BLE001 - 启动自述失败不能挡住插件
            logger.debug('hds-interlude：接入与名单自述失败：%s' % error)
            return
        for level, text in notes:
            if level == 'warn':
                logger.warning('hds-interlude：%s' % text)
            else:
                logger.info('hds-interlude：%s' % text)

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
            (f'/{PLUGIN_NAME}/console/chats', self.page_console_chats, ['GET'],
             '控制台：对话清单（按参与者 / 按群）'),
            (f'/{PLUGIN_NAME}/console/chat-history', self.page_console_chat_history, ['GET'],
             '控制台：某条对话的往来记录'),
            # 写操作（都走白名单：开关认 FLAG_KEYS，连接行认 CONNECTION_FIELDS，
            # 配置页认 `_conf_schema.json` 里声明过的路径）
            (f'/{PLUGIN_NAME}/console/flags', self.page_console_set_flag, ['POST'],
             '控制台：切换运行开关'),
            (f'/{PLUGIN_NAME}/console/connections', self.page_console_save_connection, ['POST'],
             '控制台：新增 / 修改模型连接'),
            (f'/{PLUGIN_NAME}/console/connections-delete', self.page_console_delete_connection, ['POST'],
             '控制台：删除模型连接'),
            # 配置页：schema 驱动的全量配置（替代宿主配置页那个"字符串数组"控件）
            (f'/{PLUGIN_NAME}/console/config', self.page_console_config, ['GET'],
             '控制台：配置 schema 与当前值'),
            (f'/{PLUGIN_NAME}/console/config-set', self.page_console_config_set, ['POST'],
             '控制台：写入一个配置项'),
            (f'/{PLUGIN_NAME}/console/participants', self.page_console_participants, ['GET'],
             '控制台：已知参与者（白名单一键填入）'),
            (f'/{PLUGIN_NAME}/console/stories', self.page_console_stories, ['GET'],
             '控制台：剧本清单（含归档）'),
            (f'/{PLUGIN_NAME}/console/token-stats', self.page_console_token_stats, ['GET'],
             '控制台：Token 用量统计（按天/周/月/自选范围）'),
            (f'/{PLUGIN_NAME}/console/actions', self.page_console_actions, ['GET'],
             '控制台：平台动作目录与权限档位'),
            (f'/{PLUGIN_NAME}/console/action-permission', self.page_console_action_permission, ['POST'],
             '控制台：设置一个平台动作的权限档位'),
            (f'/{PLUGIN_NAME}/console/action-permissions-reset', self.page_console_action_permissions_reset, ['POST'],
             '控制台：清空平台动作权限表（回默认档）'),
            (f'/{PLUGIN_NAME}/console/patch-decide', self.page_console_patch_decide, ['POST'],
             '设定候选审批'),
            (f'/{PLUGIN_NAME}/console/patch-rollback', self.page_console_patch_rollback, ['POST'],
             '设定候选回滚'),
            (f'/{PLUGIN_NAME}/console/story-merge', self.page_console_story_merge, ['POST'],
             '控制台：把旧剧本并入共享主剧本'),
            (f'/{PLUGIN_NAME}/console/story-promote', self.page_console_story_promote, ['POST'],
             '控制台：把选中的剧本立为共享主剧本'),
            # 共同作品（上游 rc28 `works.ts` 的界面；入口由本移植版补）。
            # 「作品」面板只读 + 用户动作：**只有用户能接受 / 驳回**她的提案。
            (f'/{PLUGIN_NAME}/console/works', self.page_console_works, ['GET'],
             '控制台：共同作品清单'),
            (f'/{PLUGIN_NAME}/console/work', self.page_console_work, ['GET'],
             '控制台：一件共同作品的全貌'),
            (f'/{PLUGIN_NAME}/console/work-create', self.page_console_work_create, ['POST'],
             '控制台：新建一件共同作品'),
            (f'/{PLUGIN_NAME}/console/work-accept', self.page_console_work_accept, ['POST'],
             '控制台：接受一条作品提案'),
            (f'/{PLUGIN_NAME}/console/work-reject', self.page_console_work_reject, ['POST'],
             '控制台：驳回一条作品提案'),
            (f'/{PLUGIN_NAME}/console/work-edit', self.page_console_work_edit, ['POST'],
             '控制台：用户手改共同作品正文'),
            (f'/{PLUGIN_NAME}/console/work-generate', self.page_console_work_generate, ['POST'],
             '控制台：让她起草一版（异步写手任务）'),
            (f'/{PLUGIN_NAME}/console/work-export', self.page_console_work_export, ['GET'],
             '控制台：导出共同作品（按消息长度分段）'),
            (f'/{PLUGIN_NAME}/console/work-cancel', self.page_console_work_cancel, ['POST'],
             '控制台：取消一个写手任务'),
            # 表情库（v1.8.0）：列表 / 原图 / 改描述 / 删除 / 重扫。
            # 删除语义是**默认只标记**，`purge=true` 才连文件一起删（见 PORTING_NOTES §45）。
            (f'/{PLUGIN_NAME}/console/stickers', self.page_console_stickers, ['GET'],
             '控制台：本地表情库清单'),
            (f'/{PLUGIN_NAME}/console/sticker-file', self.page_console_sticker_file, ['GET'],
             '控制台：读取一张表情包（缺省回原图字节，`inline=1` 回 base64 JSON）'),
            (f'/{PLUGIN_NAME}/console/sticker-update', self.page_console_sticker_update, ['POST'],
             '控制台：改表情包的描述 / 名字 / 停用'),
            (f'/{PLUGIN_NAME}/console/sticker-restore-description',
             self.page_console_sticker_restore_description, ['POST'],
             '控制台：把表情包交回自动描述'),
            (f'/{PLUGIN_NAME}/console/sticker-delete', self.page_console_sticker_delete, ['POST'],
             '控制台：删除表情包（默认只标记，purge 才删文件）'),
            (f'/{PLUGIN_NAME}/console/sticker-rescan', self.page_console_sticker_rescan, ['POST'],
             '控制台：重扫表情库目录'),
            # 表情库分组与上传（v1.8.3，§47）：新建分组 / 删除分组 / 批量移动 / 上传表情。
            (f'/{PLUGIN_NAME}/console/sticker-groups', self.page_console_sticker_groups, ['GET'],
             '控制台：表情库分组清单'),
            (f'/{PLUGIN_NAME}/console/sticker-group-save', self.page_console_sticker_group_save,
             ['POST'], '控制台：新建 / 修改表情库分组'),
            (f'/{PLUGIN_NAME}/console/sticker-group-delete', self.page_console_sticker_group_delete,
             ['POST'], '控制台：删除表情库分组（组内素材先挪走）'),
            (f'/{PLUGIN_NAME}/console/sticker-move', self.page_console_sticker_move, ['POST'],
             '控制台：批量移动表情到分组'),
            (f'/{PLUGIN_NAME}/console/sticker-upload', self.page_console_sticker_upload, ['POST'],
             '控制台：上传表情（multipart，字段名 file）'),
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

    async def page_console_chats(self):
        return await self._console_json(lambda api, q: api.chats(
            q('story_id'), _to_int(q('scan'), 2000),
        ))

    async def page_console_chat_history(self):
        return await self._console_json(lambda api, q: api.chat_history(
            q('story_id'), q('conversation'), _to_int(q('limit'), 200),
            q('before'), _to_int(q('scan'), 2000),
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

    # ---- 控制台的配置页（schema 驱动） ---- #

    async def page_console_config(self):
        return await self._console_json(lambda api, q: api.config_schema())

    async def page_console_config_set(self):
        return await self._console_write(lambda api, body: api.set_config_value(
            body.get('path'), body.get('value'),
        ))

    async def page_console_participants(self):
        return await self._console_json(lambda api, q: api.participants(q('story_id')))

    async def page_console_stories(self):
        return await self._console_json(lambda api, q: api.stories())

    async def page_console_token_stats(self):
        """Token 用量统计：`range=day|week|month|custom`（自选时带 `from` / `to`）。"""
        return await self._console_json(lambda api, q: api.token_stats(
            q('range'), q('from'), q('to'),
        ))

    async def page_console_actions(self):
        """平台动作目录 + 当前权限档位（面板「动作」）。"""
        return await self._console_json(lambda api, q: api.actions_catalog())

    async def page_console_action_permission(self):
        """设置一个动作的权限档位（未知动作 / 未知档位会被 400 拒绝）。"""
        return await self._console_write(lambda api, body: api.set_action_permission(
            body.get('action'), body.get('tier'),
        ))

    async def page_console_action_permissions_reset(self):
        """清空动作权限表：所有动作回目录默认档。"""
        return await self._console_write(lambda api, body: api.reset_action_permissions())

    async def page_console_story_merge(self):
        return await self._console_write(lambda api, body: api.merge_story(
            body.get('source_story_id'), body.get('target_story_id'),
        ))

    async def page_console_patch_decide(self):
        return await self._console_write(lambda api, body: api.decide_patch(
            body.get('story_id'), body.get('patch_id'), body.get('action'), body.get('note', ''),
        ))

    async def page_console_patch_rollback(self):
        return await self._console_write(lambda api, body: api.rollback_patch(
            body.get('story_id'), body.get('patch_id'), body.get('note', ''),
        ))

    async def page_console_story_promote(self):
        return await self._console_write(lambda api, body: api.promote_story(
            body.get('source_story_id'),
        ))

    # ---- 控制台的「作品」面板（共同作品） ---- #

    async def page_console_works(self):
        """共同作品清单：这部剧本里每个参与者一件。"""
        return await self._console_json(lambda api, q: api.works_overview(q('story_id')))

    async def page_console_work(self):
        """一件作品的全貌（正文原样回，面板自己决定怎么显示）。"""
        return await self._console_json(lambda api, q: api.work_detail(q('work_id')))

    async def page_console_work_create(self):
        """新建第一件作品（`story_id` 留空 = 面板当前那部剧本）。

        已有共同作品时服务层会拒（**绝不覆盖**），那条文案原样回给用户。
        """
        return await self._console_write(lambda api, body: api.create_work(
            body.get('story_id'), body.get('participant_id'),
            body.get('title'), body.get('content'),
        ))

    async def page_console_work_accept(self):
        """接受一条提案——**只有用户能做这件事**（她只能提议）。"""
        return await self._console_write(lambda api, body: api.accept_work_proposal(
            body.get('work_id'), body.get('proposal_id'),
        ))

    async def page_console_work_reject(self):
        """驳回一条提案（正文不动，只留结论）。"""
        return await self._console_write(lambda api, body: api.reject_work_proposal(
            body.get('work_id'), body.get('proposal_id'),
        ))

    async def page_console_work_edit(self):
        """用户手改正文：一条新版本（`reason` 是给这条版本留的理由）。"""
        return await self._console_write(lambda api, body: api.edit_work(
            body.get('work_id'), body.get('content'), body.get('reason', ''),
        ))

    async def page_console_work_generate(self):
        """让她起草一版：异步写手任务，结果作为待决提案回来。"""
        return await self._console_write(lambda api, body: api.start_work_generation(
            body.get('work_id'), body.get('brief'),
        ))

    async def page_console_work_cancel(self):
        """取消一个写手任务（`interrupted` 的遗留任务也能取消）。"""
        return await self._console_write(lambda api, body: api.cancel_work_generation(
            body.get('work_id'), body.get('job_id'),
        ))

    async def page_console_work_export(self):
        """导出整件作品：`{parts, count}`，每段都在单条消息的安全长度内。"""
        return await self._console_json(lambda api, q: api.export_work(q('work_id')))

    # ---- 控制台的「表情库」面板（v1.8.0） ---- #

    async def page_console_stickers(self):
        """本地表情库清单（`status` / `kind` / `source` / `q` 都可选）。"""
        return await self._console_json(lambda api, q: api.stickers(
            q('status'), q('kind'), q('source'), q('q'),
            _to_int(q('limit'), 60), _to_int(q('offset'), 0),
        ))

    async def page_console_sticker_file(self):
        """回一张表情包：缺省是**原始图片字节**（宿主按 `file_response` 走 blob）。

        `inline=1` 时改成回 base64 JSON 信封（`{assetId, mimeType, size, data}`）——
        沙箱 iframe 里 `<img src>` 拿不到登录态、bridge 又只有 JSON 通道，形状见
        `docs/PORTING_NOTES.md` §45.8（前端 `src/sticker-images.ts` 已按它接好）。
        **不带 `inline` 时逐字保持老的 blob 行为**（老客户端兼容），两条分支共用同一批
        校验与异常分支，所以 400 / 404 的措辞逐字一致。

        `file_response` 之外还要给下载文件名：素材的 `filePath` basename 就是它在
        表情库里的名字，直接拿来当 `filename` 最直观（前端也可以不下载、只显示）。
        """
        from astrbot.api.web import error_response, file_response, json_response

        from .adapters.console_api import ConsoleError

        asset_id = ''
        inline = False
        try:
            from astrbot.api.web import request

            asset_id = str(request.query.get('assetId', '') or '')
            raw_inline = request.query.get('inline', '')
            inline = str(raw_inline or '').strip().lower() in _STICKER_INLINE_TRUE
        except Exception:  # noqa: BLE001 - 取不到查询参数就是没给
            asset_id = ''
            inline = False
        try:
            if inline:
                return json_response(await self._console.sticker_file_inline(asset_id))
            path = await self._console.sticker_file(asset_id)
        except ConsoleError as error:
            return error_response(str(error), status_code=400)
        except FileNotFoundError:
            return error_response('表情包文件不存在（可能已被删除）', status_code=404)
        except Exception as error:  # noqa: BLE001
            logger.warning('hds-interlude：读取表情包失败：%s' % error)
            return error_response('读取表情包失败：%s' % error, status_code=500)
        return file_response(
            path, filename=os.path.basename(path), content_type=_sticker_content_type(path),
        )

    async def page_console_sticker_update(self):
        """改描述 / 名字 / 停用（白名单在 `ConsoleApi.update_sticker` 里）。"""
        return await self._console_write(lambda api, body: api.update_sticker(body))

    async def page_console_sticker_restore_description(self):
        """把描述交回自动描述（摘掉手工标记）。"""
        return await self._console_write(
            lambda api, body: api.restore_sticker_description(body),
        )

    async def page_console_sticker_delete(self):
        """删除一条素材：默认只标记，`purge=true` 才连文件一起删。"""
        return await self._console_write(lambda api, body: api.delete_sticker(body))

    async def page_console_sticker_rescan(self):
        """重扫表情库目录（跑完整 `scan_sticker_library()`）。"""
        return await self._console_write(lambda api, body: api.rescan_stickers(body))

    # ---- 控制台的「表情库」面板：分组与上传（v1.8.3，§47） ---- #

    async def page_console_sticker_groups(self):
        """表情库分组清单（**目录即分组**：内置默认组永远在，磁盘上有目录的也在）。"""
        return await self._console_json(lambda api, _q: api.sticker_groups())

    async def page_console_sticker_group_save(self):
        """新建 / 改名 / 写描述（白名单在 `ConsoleApi.save_sticker_group` 里）。"""
        return await self._console_write(lambda api, body: api.save_sticker_group(body))

    async def page_console_sticker_group_delete(self):
        """删分组：组内素材**先搬进目标目录**（默认进内置默认组），绝不悄悄删素材。"""
        return await self._console_write(lambda api, body: api.delete_sticker_group(body))

    async def page_console_sticker_move(self):
        """批量改归属（**先校验后写**：有一条 assetId 不合法就一条都不写）。"""
        return await self._console_write(lambda api, body: api.move_stickers(body))

    async def page_console_sticker_upload(self):
        """上传一张表情（`multipart/form-data`，字段名固定 **`file`**）。

        与其它控制台写操作的区别只有一处：请求体不是 JSON，所以这里不用
        `_console_write` 的 JSON 解析，而是自己取字节 + 三个可选参数
        （`groupId` / `description` / `name`，表单字段或查询串，见
        `_sticker_upload_payload()` 的两条通道说明）。

        校验 / 去重 / 落盘 / 建档 / 描述全在 `ConsoleApi.upload_sticker()` →
        服务层的 `upload_sticker_asset()`；`ConsoleError` 映射成 400 + 原文案。
        """
        from astrbot.api.web import error_response, json_response

        from .adapters.console_api import ConsoleError

        data, options = await _sticker_upload_payload()
        if not data:
            return error_response('没有收到文件内容（multipart 的 file 字段）', status_code=400)
        try:
            payload = await self._console.upload_sticker(
                data, options.get('groupId', ''), options.get('description'),
                options.get('name', ''),
            )
        except ConsoleError as error:
            return error_response(str(error), status_code=400)
        except Exception as error:  # noqa: BLE001
            logger.warning('hds-interlude：上传表情失败：%s' % error)
            return error_response('上传失败：%s' % error, status_code=500)
        logger.warning('hds-interlude：控制台上传了表情：%s' % payload.get('assetId', '?'))
        return json_response(payload)

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
        """若该会话正在等 y/n，则消费这条回复并返回 `True`。

        **只有真正的文字消息才算回答**：NapCat 的「对方正在输入…」是 `notice`，宿主照样
        派到消息处理器上；以前它会带着空文本进来，把等待中的 future 用 `''` 结掉 ——
        确认被当成"取消"，用户随后真打的 `y` 反而成了普通聊天消息（用户 2026-09-28 的
        `/hdsi_purge_all` 现场：提问后 2 秒就报"操作已取消"，然后她开始回答一个 "y"）。
        非消息事件与空文本一律放行给别的分支，确认继续等（60 秒超时按取消处理）。
        """
        key = _text(getattr(event, 'unified_msg_origin', ''))
        future = self._confirmations.get(key)
        if future is None or future.done():
            return False
        non_message, _label = is_non_message_event(event)
        if non_message:
            return False
        answer = _text(event.get_message_str()).strip()
        if not answer:
            return False
        future.set_result(answer)
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

        **我们自己出错也要吞掉事件**：宿主里往往还跑着第二个聊天 Agent（默认 Agent /
        别的拟人插件），一条异常一旦冒出去，同一段私聊就会冒出第二个人格回答。
        错误照常记 `error` 级日志（不隐藏 bug），但事件不交回。
        """
        if self._resolve_confirmation(event):
            return
        capture = self.bridge.config_flag(
            'runtime', 'capture_direct_messages', 'captureDirectMessages', default=True,
        )
        if not capture:
            return
        try:
            await self.bridge.handle_event(event)
        except Exception as error:  # noqa: BLE001 - 私聊归属不能因为一次异常就漏给别的 Agent
            logger.error('hds-interlude：私聊事件处理失败，已吞掉事件以免其它 Agent 接手：%s' % error)
            event.stop_event()
            return
        for reply in self.bridge.turn_replies():
            yield self._reply_result(event, reply)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        """群聊入站：按上游 `canHandleGroupSession` 的规则选择性接入。

        上游中间件对非私聊会话一律调 `service.receiveGroup(session)`，由它自己先做
        白名单判定；这里多一道 `can_handle_group_session` 预检，纯粹是为了不去
        打扰未授权的群（判定结果与 `receiveGroup` 内部完全一致）。

        **不通过时要说得出为什么**：以前这里直接 `return`，加上 core 的拒绝报告走的是
        `diagnostic` 频道（默认 verbosity 下不打印），结果是群消息在日志里**一点痕迹都没有**
        ——用户只能得出"群聊功能完全没生效"。现在把原因按"群 + 原因"节流 10 分钟打一条
        warn（键与 `receiveGroup` 内部一致，所以不会重复）。
        """
        session = await self._prepare(event)
        allowed, reason = self.bridge.service.explain_group_access(session)
        if not allowed:
            self.bridge.service.note_group_skip(session, reason)
            return
        await self.bridge.handle_event(event)
        for reply in self.bridge.turn_replies():
            yield self._reply_result(event, reply)

    @staticmethod
    def _reply_result(event: AstrMessageEvent, reply: dict[str, Any]):
        """本回合一条可见回复交回宿主的结果：语音段发 `Record`，其余发纯文本。

        `reply['voice']` 是适配层**已经合成好的音频文件路径**（正文 `<tts/>` 标记
        指定的分段）；空串表示这条照旧发文字——合不出来时适配层会退回文字并打 warn，
        所以这里看到的永远是"要么有文件、要么是文字"，没有第三种。
        """
        path = reply.get('voice') if isinstance(reply, dict) else ''
        if path:
            return event.chain_result(AstrbotBridge.voice_components(path))
        return event.plain_result(reply.get('content') if isinstance(reply, dict) else reply)

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
        # 上游 1.0.1-rc28 的健康指标：命令行长文本不好读，给一份 6 行人话摘要
        # （数值与 Console 面板同源，见 `core/health.py::format_health_lines`）。
        snapshot = service.health_snapshot(_pick(story, 'id')) if hasattr(service, 'health_snapshot') else {}
        if snapshot:
            lines.append('健康指标（自本次重载）：')
            lines.extend('· %s' % line for line in format_health_lines(snapshot))
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

    @filter.command('hdsi_participant_link')
    async def hdsi_participant_link(self, event: AstrMessageEvent):
        """管理员：把同一个人的另一个号链入既有参与者（第二端点消息进入同一关系分支）。"""
        if self.blind_mode:
            return
        session = await self._prepare(event)
        if not self._is_manager(session):
            yield event.plain_result(NO_MANAGER)
            return
        args = self._raw_args(event)
        participant_id = self._text_arg(args, 0).strip()
        account = self._text_arg(args, 1).strip()
        if not participant_id or not account:
            yield event.plain_result('用法：hdsi_participant_link <参与者ID> <用户ID>')
            return
        service = self.bridge.service
        participant = await service.get_participant(participant_id) if hasattr(service, 'get_participant') else None
        if not participant:
            yield event.plain_result('参与者不存在：%s' % participant_id)
            return
        platform = _pick(session, 'platform') or 'onebot'
        result = await service.link_participant_endpoint(participant, platform, account)
        if _pick(result, 'ok'):
            display = _pick(participant, 'displayName', 'display_name') or participant_id
            yield event.plain_result('用户端点已链接（%s → %s）。' % (account, display))
            return
        yield event.plain_result('链接失败：%s' % _pick(result, 'error'))

    @filter.command('hdsi_participant_unlink')
    async def hdsi_participant_unlink(self, event: AstrMessageEvent):
        """管理员：解除一个用户端点链接（可撤销；身份与历史保留）。"""
        if self.blind_mode:
            return
        session = await self._prepare(event)
        if not self._is_manager(session):
            yield event.plain_result(NO_MANAGER)
            return
        endpoint_id = self._text_arg(self._raw_args(event), 0).strip()
        if not endpoint_id:
            yield event.plain_result('用法：hdsi_participant_unlink <端点ID>')
            return
        result = await self.bridge.service.unlink_participant_endpoint(endpoint_id)
        if _pick(result, 'ok'):
            yield event.plain_result('端点已解除链接（%s）。' % endpoint_id)
            return
        yield event.plain_result('解除失败：%s' % _pick(result, 'error'))

    @filter.command('hdsi_participant_endpoints')
    async def hdsi_participant_endpoints(self, event: AstrMessageEvent):
        """管理员：列出参与者名下全部用户端点。"""
        if self.blind_mode:
            return
        session = await self._prepare(event)
        if not self._is_manager(session):
            yield event.plain_result(NO_MANAGER)
            return
        participant_id = self._text_arg(self._raw_args(event), 0).strip()
        if not participant_id:
            yield event.plain_result('用法：hdsi_participant_endpoints <参与者ID>')
            return
        await self.bridge.service.ensure_endpoint_registry()
        endpoints = self.bridge.service.list_participant_endpoints(participant_id)
        if not endpoints:
            yield event.plain_result('参与者 %s 没有用户端点。' % participant_id)
            return
        lines = [
            '%s %s %s 端点=%s' % (
                '●' if _pick(item, 'enabled') else '○', _pick(item, 'platform'),
                _pick(item, 'userId', 'user_id'), _pick(item, 'endpointId', 'endpoint_id'),
            )
            for item in endpoints
        ]
        yield event.plain_result('用户端点（%d 个）：\n%s' % (len(endpoints), '\n'.join(lines)))

    @filter.command('hdsi_story_endpoint')
    async def hdsi_story_endpoint(self, event: AstrMessageEvent):
        """管理员：管理剧本的角色端点（账号迁移的唯一显式途径）。"""
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
        parts = [part for part in self._text_arg(self._raw_args(event), 0).split() if part]
        if parts and parts[0] == 'add':
            if len(parts) < 3:
                yield event.plain_result('用法：hdsi_story_endpoint add <平台> <账号> [qq|wechat]')
                return
            channel_kind = 'wechat' if len(parts) > 3 and parts[3] == 'wechat' else 'qq'
            result = await service.add_story_endpoint(story, parts[1], parts[2], channel_kind)
            if _pick(result, 'ok'):
                yield event.plain_result('角色端点已注册（%s %s，%s，端点 %s）。' % (
                    parts[1], parts[2], channel_kind, _pick(result, 'endpointId', 'endpoint_id')))
                return
            yield event.plain_result('注册失败：%s' % _pick(result, 'error'))
            return
        if parts and parts[0] == 'disable':
            if len(parts) < 2:
                yield event.plain_result('用法：hdsi_story_endpoint disable <端点ID>')
                return
            result = await service.disable_story_endpoint(parts[1])
            if _pick(result, 'ok'):
                yield event.plain_result('端点已停用（%s）。' % parts[1])
                return
            yield event.plain_result('停用失败：%s' % _pick(result, 'error'))
            return
        if parts:
            yield event.plain_result('用法：hdsi_story_endpoint [add <平台> <账号> [qq|wechat] | disable <端点ID>]')
            return
        await service.ensure_endpoint_registry()
        endpoints = service.list_story_endpoints(_pick(story, 'id'))
        if not endpoints:
            yield event.plain_result('当前故事没有登记任何角色端点。')
            return
        lines = [
            '%s %s %s（%s%s）端点=%s' % (
                '●' if _pick(item, 'enabled') else '○', _pick(item, 'platform'), _pick(item, 'selfId', 'self_id'),
                _pick(item, 'channelKind', 'channel_kind'),
                '·在线' if _pick(item, 'online') else '·离线',
                _pick(item, 'endpointId', 'endpoint_id'),
            )
            for item in endpoints
        ]
        yield event.plain_result('角色端点（%d 个）：\n%s' % (len(endpoints), '\n'.join(lines)))

    @filter.command('hdsi_story_alias')
    async def hdsi_story_alias(self, event: AstrMessageEvent):
        """管理员：查看/回滚剧本别名重定向（单剧本多通道 M1b）。"""
        if self.blind_mode:
            return
        session = await self._prepare(event)
        if not self._is_manager(session):
            yield event.plain_result(NO_MANAGER)
            return
        service = self.bridge.service
        await service.ensure_endpoint_registry()
        text = self._text_arg(self._raw_args(event), 0).strip()
        if text.startswith('remove'):
            alias_id = text[len('remove'):].strip()
            if not alias_id:
                yield event.plain_result('用法：hdsi_story_alias remove <别名ID>')
                return
            result = await service.remove_story_alias(
                alias_id, 'by %s' % (_pick(session, 'userId', 'user_id') or 'admin'),
            )
            if _pick(result, 'ok'):
                yield event.plain_result('已回滚别名 %s（审计已写入剧本条目）。' % alias_id)
                return
            yield event.plain_result('回滚失败：%s' % _pick(result, 'error'))
            return
        if text:
            yield event.plain_result('用法：hdsi_story_alias [remove <别名ID>]')
            return
        aliases = service.list_story_aliases()
        if not aliases:
            yield event.plain_result('当前没有剧本别名。')
            return
        lines = [
            '%s → %s（%s，%s）' % (
                _pick(row, 'aliasStoryId', 'alias_story_id'),
                _pick(row, 'canonicalStoryId', 'canonical_story_id'),
                _pick(row, 'reason'), _local_time_text(_pick(row, 'createdAt', 'created_at')),
            )
            for row in aliases
        ]
        yield event.plain_result('剧本别名（%d 条）：\n%s' % (len(aliases), '\n'.join(lines)))

    @filter.command('hdsi_reset')
    async def hdsi_reset(self, event: AstrMessageEvent):
        """管理员：完全重置——清空数据库并把角色设定重置为 Console 档案当前值。"""
        if self.blind_mode:
            return
        session = await self._prepare(event)
        if not self._is_manager(session):
            yield event.plain_result('无权限。')
            return
        story = await self._require_story(session)
        if isinstance(story, str):
            yield event.plain_result(story)
            return
        confirmed = await self._ask_confirmation(
            event, '这将删除所有剧本、记忆、事实，并将角色设定重置为 Console 档案当前值。确定吗？(y/n)',
        )
        if not confirmed:
            yield event.plain_result(CANCELLED)
            return
        await self.bridge.service.clear_database()
        await self.bridge.service.purge_all_story_data(_pick(story, 'id'))
        yield event.plain_result(
            '已完全重置。数据库已清空，角色设定已回到 Console 故事档案（storyDefaults）模板。\n'
            '如需更换角色身份，请在 Console 修改故事档案后重新开始。'
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
