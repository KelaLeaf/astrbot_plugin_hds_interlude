"""Chunk12 mixin：平台动作执行层（本移植版新增）。

上游没有这一层——上游的动作是写死在脚本/服务里的（群表态、原生表情、贴纸），而本移植版
把"她能对 QQ 做什么"做成了**目录驱动**的可扩展面（见 `core/platform_actions.py`）。
本文件是执行侧，负责四件事：

1. **可用集**：把「配置开关」⊗「独立权限表」⊗「会话身份」合成"这一回合她实际能调的动作"，
   交给提示词注入（模型只该看到它真能调的）。v1.9.9 起注入是**两段式**：
   每回合只给"有哪些按钮"（短清单，无参数），她选定某条之后才给那一条的参数表
   （`resolve_platform_action_params()`，每回合最多一次额外调用）；
2. **校验**：模型写的 `platformActions` 过 `validate_actions`，越界/未知/超限一律拒绝并留证；
3. **执行**：平台类动作走 `transport.platform_action`；**本机类动作**（定时消息/定时命令）
   留在 core 里办（它们不碰平台，只写表）；
4. **留痕**：执行结果写回剧本（`[平台动作] …`）并记日志——动作发生过就该在剧本里看得见，
   否则模型下一回合不知道自己戳过谁、撤回没撤回成功。

成员清单铁律（坑 61）只约束 chunk3/chunk9，本 chunk 可以自由加方法。
"""

from __future__ import annotations

import asyncio
import functools
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from ..platform_actions import (
    ACTIONS,
    PLATFORM_ACTION_FIELD,
    VOICE_ACTION_IDS,
    action_config_group,
    describe_action_params,
    describe_action_shortlist,
    describe_actions,
    effective_permission,
    normalize_permissions,
    resolve_permission,
    validate_action,
    validate_actions,
)
from ..narrator_prompts import platform_action_shortlist_instruction
from ..scheduled_command import (
    DEFAULT_COMMAND_CATALOG,
    cron_next_run,
    normalize_cron,
    parse_iso_datetime,
)
from ..qzone import QZONE_VISIBILITY_VALUES, qzone_visible_value
from ..time import iso
from .base import ServiceBase, pick
from .config import merge_legacy_section_values, read_section_path
#: 数据目录的唯一判据（§54）：权限表与表情库根目录读同一个函数。
from .helpers import host_data_dir

__all__ = ['ServiceChunk12']

#: 权限表文件名（独立 JSON，落在插件数据目录；与 `astrbot_bridge` 的写入端同一个名字）。
ACTION_PERMISSIONS_FILE = 'action_permissions.json'

#: 一次回合最多执行几个动作（与 `validate_actions` 的上限同源，防止模型刷屏）。
MAX_ACTIONS_PER_TURN = 8

#: 一次回合最多**补问**几次参数（两段式的铁律，与贴纸两段式同一个数：一次）。
#: 第二段是"照贴纸两段式的先例"来的：第一段文本是最终有效的，第二段只填参数槽；
#: 回执里再想改动作 id / 再点一条动作，都不接着问。
PLATFORM_ACTION_FOLLOW_UP_MAX_PER_TURN = 1

#: 动作参数补问的超时（秒）。与贴纸追问同一量级（一个短 JSON 回执）。
PLATFORM_ACTION_FOLLOW_UP_TIMEOUT_SECONDS = 20.0

#: 第二段回执里唯二认的两个键（跨 chunk 的既有双拼写纪律：camelCase 与 snake_case 都认）。
_PARAM_ACTION_KEYS = ('action', 'actionId', 'id')
_PARAM_VALUE_KEYS = ('params', 'parameters', 'args')


def _plain(value: Any) -> str:
    """把"正文/说明"压成纯文本（补问 payload 用）。

    只认字符串：其它类型一律回空串，**绝不做 `str(dict)`** —— 那会把一整块结构化数据
    塞进补问 payload，既没用又容易被误读成指令。
    """
    if isinstance(value, str):
        return value
    return ''


def close_awaitable(value: Any) -> bool:
    """把一个没打算 await 的协程 **关掉**，避免 `coroutine ... was never awaited`。

    判据与 `core/service/chunk2._spawn_sticker_task` 同源（`asyncio.iscoroutine`）：
    兼容口上"宿主给了异步实现"这件事本身要修，但**警告不该由核心侧制造**——
    警告是给"忘了 await"的代码看的，我们这里是**刻意不 await**（同步上下文）。
    返回是否真的关掉了一个协程（调用方据此决定要不要额外说明）。
    """
    if not asyncio.iscoroutine(value):
        return False
    try:
        value.close()
    except Exception:  # noqa: BLE001 - 关不掉也不能影响判定
        return False
    return True


def super_admin_ids_from_host(host: Any) -> tuple[str, ...]:
    """从宿主对象取**同步**的超级管理员名单，归一化成 `tuple[str, ...]`。

    两种形状都认，都必须是同步的：

    * **属性** → 直接读（`known_super_admin_ids = ('123',)`）；
    * **方法** → `known_super_admin_ids()`。

    归一化宁可从严：拿到的不是 list/tuple/set（字符串、数字、协程…）一律当"没有名单"，
    返回空元组。**绝不把字符串当成 id 序列**——那会把 `'12345'` 拆成五个单字符 id，
    反而可能碰巧放行一个单字符用户号。拿不到就是空名单（= 没有权限，安全侧）。

    注意调用方要**先判可调用**再走方法分支（本模块 `resolve_action_session_role` 有例）：
    一个属性天然不可调用，别把合法的属性形状误判成接线错误。
    """
    if host is None:
        return ()
    raw = getattr(host, 'known_super_admin_ids', None)
    if raw is None:
        return ()
    value = raw() if callable(raw) else raw
    if asyncio.iscoroutine(value):
        # 异步实现 = 接线错误：关掉协程（不留警告），按"没有名单"回空元组。
        close_awaitable(value)
        return ()
    if not isinstance(value, (list, tuple, set, frozenset)):
        return ()
    return tuple(str(item) for item in value if item not in (None, ''))


def session_group_id(session: Any) -> str:
    """会话里的**群 id**——动作坐标的 `group_id` 判据（只此一处）。

    生产上群回合拿到的是 `SessionView`：适配层把群 id 存在 `guild_id` / `channel_id`
    （`astrbot_bridge.session_view`：`channel_id=guild_id=group_id`，`is_direct=False`），
    而它**没有** `groupId` 键（`SESSION_VIEW_KEYS` 里有 `guildId` 没有 `groupId`）。
    只读 `pick(session, 'groupId', 'group_id')` 会让**群回合的坐标看起来像私聊**：

    * `dispatch_platform_actions` 的作用域被收成 `('private',)` → 群里能做的动作
      （群公告 / 踢人 / 禁言…29 条）在投递后全被"当前会话不允许"拒掉，
      而提示词组装那一侧（`chunk4`）按 `groupContext` 判定，**刚刚才把它们教给模型**；
    * 同一条坐标还会让「正在输入」漏到群聊（`typing_target_is_group` 也读它）。

    取法（从严到宽）：

    1. 显式 `groupId` / `group_id`（dict 形状的参与者与测试夹具）——非空就用它；
    2. `isDirect` 为真 → **空**（私聊的 `channel_id` 存的是对方 user_id，不是群）；
    3. 会话自己的 `session_group_id()`（`SessionView` 的规范口：`guildId || channelId`
       ——与 `core/service/session.py:142` 同一个判据，不另造一份）。

    `'0'`（OneBot 私聊把 `group_id` 填成 `0` 的那个形状，用户贴过日志）按**私聊**处理，
    与 `typing_target_is_group()` 同一个口径（判据一处）。坐标推导**绝不抛**：
    拿不到就是空串（= 不是群），安全侧。
    """
    if pick(session, 'isDirect', 'is_direct') is True:
        return ''
    explicit = pick(session, 'groupId', 'group_id')
    if explicit not in (None, ''):
        candidate = str(explicit).strip()
    else:
        accessor = getattr(session, 'session_group_id', None)
        if not callable(accessor):
            return ''
        try:
            candidate = str(accessor() or '').strip()
        except Exception:  # noqa: BLE001 - 坐标推导失败只当"不是群"
            return ''
    return '' if candidate in ('', '0') else candidate

#: **由 core 自己办**的动作（不碰平台）：定时消息走既有的 intent 表，定时命令走
#: `interlude_scheduled_command`。其余动作一律走传输层。
CORE_HANDLED_ACTIONS = frozenset({
    'schedule_message', 'list_scheduled_messages', 'cancel_scheduled_message',
    'schedule_command', 'list_scheduled_commands', 'cancel_scheduled_command',
    # QQ 空间的三条**写动作**必须走 chunk13 的 `qzone_execute`（限流门 → 审计行 →
    # 动作 → 剧本条目），直通传输层会绕过风控与账本（上游这三个动作在叙事路径上
    # 也是走 `qzoneExecute`）。危险的 `delete_qzone_post` 上游没有对应服务成员，
    # 仍直通传输层（它打的是 NapCat **真有**的 `delete_qzone_msg`，见坑 71）。
    'publish_qzone_post', 'comment_qzone_post', 'like_qzone_post', 'forward_qzone_post',
    # v1.7.5：改说说可见范围（`emotion_cgi_update`）同样是**本机办**的写动作——
    # 它要先读回正文、过限流门、落审计行；直通传输层会绕过这一切（而且平台根本没有
    # 这条原生动作名可打）。
    'set_qzone_visibility',
    # v1.7.1：读类也收进本机——NapCat WebSocket 方案（get_cookies + QZone CGI）
    # 要按账号端点解析、并且**不能**让只读动作去撞限流门。
    #
    # ⚠️ 这两条**必须**留在本表里（v1.7.8 复核）：NapCat 的公开动作表里**没有**
    # `get_qzone_feeds` / `get_qzone_msg_list`（它只有发 / 删说说），一旦被当平台动作
    # 派到传输层，就是 `retcode 1404 不支持的Api`（用户贴过日志）。`_PLATFORM_CALLS`
    # 里那两条映射现已改成 `@unsupported`（没有任何后端认识它们），默认路径走不到。
    'list_qzone_posts', 'list_qzone_feeds',
})

#: 目录动作 id → `qzone_execute` 的 kind。
QZONE_ACTION_KINDS_BY_ID = {
    'publish_qzone_post': 'post',
    'comment_qzone_post': 'comment',
    'like_qzone_post': 'like',
    # v1.7.1：转发说说也是**写**动作（受限流门与账本管）。只读的
    # `list_qzone_feeds` / `list_qzone_posts` 不走这张表（它们由 `qzone_read` 走
    # CGI 读通道，见 `_run_core_action`）。
    'forward_qzone_post': 'forward',
    # v1.7.5：改可见范围（配额按**发帖**那一档算，见 `core/qzone.evaluate_qzone_gate`）。
    'set_qzone_visibility': 'visibility',
}

#: 定时消息在 intent 表里的类型名（内部调度账，控制台「承诺与意图」面板会标成内部）。
SCHEDULED_MESSAGE_INTENT = 'scheduled-message'

#: 「这个平台/这个会话就是不支持输入状态」的说明**按会话**节流（10 分钟）。
#: 一次性信息才值得 warn，而且不该每回合重打一遍（用户 2026-09-28 点名降噪）。
INPUT_STATUS_UNSUPPORTED_NOTE_INTERVAL_MS = 10 * 60 * 1000


class ServiceChunk12(ServiceBase):
    """Chunk12 mixin：平台动作的权限判定、校验与执行。"""

    # ------------------------------------------------------------------ #
    # 权限与可用集
    # ------------------------------------------------------------------ #

    def interlude_data_dir(self) -> str:
        """插件数据目录（`<数据目录>/action_permissions.json` 的根）。

        按**生产上真的挂着的那一个**取：`ServiceBase.__init__` 存的是 `self.ctx`
        （`chunk2` 读表情库目录、`chunk13` 读写跳闸标志都是这么取的），而 `self.context`
        在 core 里**根本不存在**——那是适配层 `AstrbotInterludeContext` 自己的属性（指宿主
        Context），core 拿不到。三种情形都有定义：

        * `ctx.base_dir` → 用它；
        * `ctx` 存在但**没有** `base_dir` / 完全没 `ctx` → 退到宿主注入的
          `context.base_dir`（单测的裸宿主常这么塞）→ 自己的 `base_dir`；
        * 一个都拿不到 → 回空串 = 回落空表 / 目录默认档，**绝不抛**。

        判据本身住在 `helpers.host_data_dir()`（§54）：表情库根目录
        （`chunk2.sticker_library_root`）读的是**同一个**函数——两处各写一遍
        "从哪个属性取数据目录"，就会出现"权限表读 A、表情库写 B"。
        """
        return host_data_dir(self)

    def timer_host(self) -> Any:
        """能排定时器的那个对象（生产上是 `ServiceBase.ctx` = `InterludeContext`）。

        `ctx` 存在但**没有** `set_timeout`（裸宿主）时退到宿主注入的 `context`；
        都没有就回 `None` = 兜底定时器不生效（绝不抛、也绝不影响投递）。
        """
        for holder in (getattr(self, 'ctx', None), getattr(self, 'context', None)):
            if callable(getattr(holder, 'set_timeout', None)):
                return holder
        return None

    def action_permission_table(self) -> dict[str, str]:
        """读独立权限表（`<数据目录>/action_permissions.json`）。

        坏文件/缺文件都只回落空表（= 全部走目录默认档），绝不抛——权限表是**运行期**
        的东西，不该因为一个手改坏的 JSON 让整条叙事链起不来。读取失败会留一条 warn。
        """
        base = self.interlude_data_dir()
        if not base:
            return {}
        path = Path(base) / ACTION_PERMISSIONS_FILE
        try:
            raw = path.read_text(encoding='utf-8')
        except FileNotFoundError:
            return {}
        except Exception as error:  # noqa: BLE001 - 读不到就按默认档
            self.report_standalone('warn', '动作权限表读取失败，按默认档运行 错误=%s', error)
            return {}
        try:
            data = json.loads(raw) if raw.strip() else {}
        except Exception as error:  # noqa: BLE001
            self.report_standalone('warn', '动作权限表不是合法 JSON，按默认档运行 错误=%s', error)
            return {}
        return normalize_permissions(data)

    def action_group_values(self, group: str) -> dict[str, Any]:
        """读一个动作配置分组（不存在就回空 dict = "未配置"，等价于不限制）。

        `group` 是**点分路径**（`robot_actions.chat` 这种嵌套段，见
        `platform_actions.ACTION_CONFIG_GROUPS`；`runtime.input_status` 同理）。

        三件事都在这里收口：

        * **两个读法都试**：`ServiceBase` 本身没有 `section()`（那是适配层
          `AstrbotBridge` 的成员，见 chunk14 的同款说明），所以宿主注入了 `section`
          就用它、否则按点分路径读 `self.config`（`read_section_path`）。
          早期版本只走 `self.section`，异常被吞掉后恒回 `{}` —— 配置页里的开关看着能点，
          运行期其实一条都不生效。
        * **N:1 旧分组归并**（`LEGACY_SECTION_MERGES`）：动作开关组 v1.7.2 由十个收敛成
          三个、v1.7.3 取消「风险操作」组、v1.7.4 又把三个并进一个父组，用户升级前设过的
          键还在旧分组里；新路径的键优先、缺的键从旧分组补，所以新旧配置读出来是同一份。
        * **嵌套路径的父级对象不算数**：`robot_actions.chat` 读不到时不能回落到
          `robot_actions`（那是一整棵子树，键名对不上），所以只按整条路径取。
        """
        values: dict[str, Any] = {}
        reader = getattr(self, 'section', None)
        if callable(reader):
            try:
                section = reader(group)
            except Exception:  # noqa: BLE001 - 旧配置里没有这个分组是正常的
                section = None
            if isinstance(section, dict):
                values = dict(section)
        raw = self.config if isinstance(self.config, dict) else {}
        if not values:
            own = read_section_path(raw, group)
            if isinstance(own, dict):
                values = dict(own)
        return merge_legacy_section_values(raw, group, values)

    def action_switch(self, action_id: str) -> Optional[bool]:
        """某动作的开关值。`None` = 该分组/键不存在（= 未配置，不构成限制）。

        注意与坑 36 同一类陷阱：这里**不把缺失当 false**——插件升级后旧配置里没有
        新分组，若把缺失当 false，所有新动作会静默全关（用户以为"没生效"）。

        v1.7.7：语音类动作（`send_voice` / `list_voices`）多一道**总闸**——
        「模型中心 → 语音 / 音频理解设置」的 `tts_enabled`。关掉后这两个动作按既有的
        "动作开关关掉"路径变成不可用（`resolve_permission` 收到 `False`），
        与正文 `<tts/>` 标记被忽略是同一个开关的两种表现，**不另造一套判定**。
        """
        if action_id in VOICE_ACTION_IDS and not self.voice_reply_enabled:
            return False
        action = ACTIONS.get(action_id)
        if action is None:
            return None
        group = self.action_group_values(action_config_group(action))
        if not group:
            return None
        master = group.get('enabled')
        if master is False:
            return False
        value = group.get(action_id)
        return None if value is None else bool(value)

    def available_platform_actions(
        self,
        session_role: str = '',
        scopes: Iterable[str] = ('private', 'group'),
        table: Optional[dict[str, str]] = None,
    ) -> list[str]:
        """这一回合实际可调的动作 id 列表（权限 ⊗ 开关 ⊗ 会话范围 ⊗ 会话身份）。"""
        permissions = self.action_permission_table() if table is None else table
        scope_set = set(scopes)
        available: list[str] = []
        for action in ACTIONS.values():
            if not (scope_set & set(action.scopes)):
                continue
            if not resolve_permission(action.id, permissions, self.action_switch(action.id), session_role):
                continue
            available.append(action.id)
        return available

    def platform_action_instruction(self, available: Optional[list[str]] = None) -> str:
        """渲染进提示词的动作目录（只列可调项）。

        ⚠️ 这是**全量参数目录**（`describe_actions`），v1.9.9 起注入路径**不再用它**——
        每回合注入的是第一段短清单（`platform_action_shortlist_instruction`），参数表只在
        她选定某条动作时出现一次（第二段）。留着它是因为控制台预览与
        `plugin/tests/test_platform_actions.py::DescribeActionsTests` 仍按它钉全量目录的形状
        （**不是**注入路径，别把它接回提示词——那正是 token 回归）。
        """
        return describe_actions(available)

    # ------------------------------------------------------------------ #
    # 两段式教学（v1.9.9，用户点名）：第一段"有哪些按钮"，第二段"这个界面怎么填"
    # ------------------------------------------------------------------ #

    def platform_action_shortlist(self, available: Optional[list[str]] = None) -> str:
        """**第一段**：可用动作的短清单（`id` + 一句短标签，无参数）。

        `available` 缺省 = 现算这一回合的可用集（判据仍是 `available_platform_actions()`
        **一处**：配置开关 ⊗ 权限表 ⊗ 会话身份都在那边判完）。显式传空列表 = 什么都不给。
        """
        ids = self.available_platform_actions() if available is None else list(available)
        return describe_action_shortlist(ids)

    def platform_action_shortlist_instruction(self, available: Optional[list[str]] = None) -> str:
        """**第一段**的完整提示词标题句 + 短清单（每回合注入用这一段）。"""
        return platform_action_shortlist_instruction(self.platform_action_shortlist(available))

    @staticmethod
    def _platform_action_drafts(decision: Any) -> list[Any]:
        """决策里的 `platformActions` 原样列表（双拼写），拿不到就回空列表。"""
        if not isinstance(decision, Mapping):
            return []
        raw = decision.get(PLATFORM_ACTION_FIELD)
        if raw is None:
            raw = decision.get('platform_actions')
        return list(raw) if isinstance(raw, (list, tuple)) else []

    def _platform_action_current_params(self, decision: Any, action_id: str) -> dict[str, Any]:
        """草稿里这条动作**已经写了的**参数（补问 payload 带上它，模型只需补缺的）。

        只回 dict；写成字符串/别的东西一律当"没有参数"。
        """
        for item in self._platform_action_drafts(decision):
            if not isinstance(item, Mapping):
                continue
            name = str(item.get('action', item.get('actionId', item.get('id'))) or '').strip()
            if name != action_id:
                continue
            for key in _PARAM_VALUE_KEYS:
                value = item.get(key)
                if isinstance(value, Mapping):
                    return {str(k): v for k, v in value.items()}
            return {}
        return {}

    def platform_action_missing_params(self, decision: Any) -> Optional[str]:
        """决策里"选了动作但参数没给全"的那一条 → 它的 id（没有就回 `None`）。

        **触发第二段的唯一判据就在这里**（与 `available_platform_actions` 一样只有一处）：

        * `platformActions: ["send_qzone_post"]`（只写 id 字符串）→ 需要补参数；
        * `platformActions: [{"action": "send_qzone_post"}]`（对象里没有 `params` 键）→ 需要补；
        * `params: {}` **不算**需要补——空对象是模型明确的"没有参数"，再补一次是白烧一次调用；
        * 一次决策里有多条需要补参数的动作 → 只认**第一条**（一次回合最多补问一次，
          其余交给普通的 `validate_actions` 拒绝并留痕，不额外烧调用）。
        """
        items = self._platform_action_drafts(decision)
        for item in items:
            if isinstance(item, str):
                name = item.strip()
            elif isinstance(item, Mapping):
                name = str(item.get('action', item.get('actionId', item.get('id'))) or '').strip()
                if any(key in item for key in _PARAM_VALUE_KEYS):
                    continue  # 已经写了 params（哪怕空对象）= 模型表过态，不再补问
            else:
                continue
            action = ACTIONS.get(name)
            if action is None or name not in self.available_platform_actions():
                continue
            return name
        return None

    async def resolve_platform_action_params(
        self,
        decision: Any,
        *,
        follow_up_budget: Optional[dict[str, Any]] = None,
        message: str = '',
    ) -> Optional[str]:
        """**第二段**：她选定了某条动作，把这条动作的参数表交给她 → 回填决策里的参数槽。

        两段式的分工（照贴纸两段式的先例，§48 甲）：

        * **第一段文本是最终有效的**：动作 id 在这里已经定了，第二段**只填参数槽**；
        * **每回合最多一次额外调用**（`follow_up_budget` 是这一回合的计数，调用方每回合
          给一个新的空 dict，与贴纸的同一个形状）；超了就不问了，交给普通校验拦；
        * 任何失败（没有这个能力 / 超时 / 抛错 / 回执解析不了 / 参数仍然不全 / 模型想换
          动作）都**不抛**，按"这一次没补上"继续 —— 但每一步都留一条可见记录
          （warn / 剧本条目），不会变成"她以为点了、其实没点"的悬案；
        * 宿主侧的调用口是可选方法 `narrator.select_platform_action_params(...)`——
          拿不到就记一条 warn 并返回 `None`（能力缺失要**可见且可行动**）。
        """
        action_id = self.platform_action_missing_params(decision)
        if not action_id:
            return None
        budget = follow_up_budget if isinstance(follow_up_budget, dict) else {}
        asked = budget.get('count')
        asked = int(asked) if isinstance(asked, (int, float)) and not isinstance(asked, bool) else 0
        if asked >= PLATFORM_ACTION_FOLLOW_UP_MAX_PER_TURN:
            self.report_standalone(
                'warn', '动作参数补问已用完本回合额度，不再补问 动作=%s', action_id,
            )
            return None
        action = ACTIONS.get(action_id)
        select = getattr(self.narrator, 'select_platform_action_params', None)
        if action is None or not callable(select):
            self.report_standalone(
                'warn',
                '动作参数补问不可用（当前连接不支持这次追问），动作按参数不足处理 动作=%s',
                action_id,
            )
            return None
        spec = describe_action_params([action_id])
        if not spec:
            return None
        budget['count'] = asked + 1
        current = self._platform_action_current_params(decision, action_id)
        try:
            receipt = await asyncio.wait_for(
                select(action_id, action.label, action.summary, spec, _plain(message), current),
                PLATFORM_ACTION_FOLLOW_UP_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            self.report_standalone('warn', '动作参数补问超时，动作按参数不足处理 动作=%s', action_id)
            return None
        except Exception as error:  # noqa: BLE001 - 追问失败只是没补上参数
            self.report_standalone(
                'warn', '动作参数补问失败，动作按参数不足处理 动作=%s 错误=%s', action_id, error,
            )
            return None
        parsed = self.parse_platform_action_params(receipt, action_id, current)
        if parsed is None:
            self.report_standalone(
                'warn',
                '动作参数补问回执不可用（缺少参数、动作被换掉或不可解析），动作按参数不足处理 动作=%s',
                action_id,
            )
            return None
        return self.apply_platform_action_params(decision, parsed)

    def parse_platform_action_params(
        self, receipt: Any, action_id: str, fallback_params: Any = None,
    ) -> Optional[dict[str, Any]]:
        """第二段回执 → **已校验**的单个动作（`{'action','params'}`）；不可用回 `None`。

        收得比第一段**更严**（第二段是补参数，不是重开决策）：

        * 动作 id 只能**逐字等于**第一段定下的那一条——换了就整条不要（第一段是最终有效的）；
        * 参数先并入 `fallback_params`（她第一段**已经写出来**的那些，别因为补问就丢掉），
          再走 `validate_action()`（唯一的校验口）：必填缺失 / 越界 / 枚举不认一律回 `None`；
        * `available` 也照常传（不因为"她自己选的"就放宽可用集）。
        """
        if not isinstance(receipt, Mapping):
            return None
        name = ''
        for key in _PARAM_ACTION_KEYS:
            value = receipt.get(key)
            if value:
                name = str(value).strip()
                break
        if name != str(action_id or '').strip():
            return None
        merged: dict[str, Any] = dict(fallback_params) if isinstance(fallback_params, Mapping) else {}
        written = False
        for key in _PARAM_VALUE_KEYS:
            if key in receipt:
                value = receipt.get(key)
                if isinstance(value, Mapping):
                    merged.update({str(k): item for k, item in value.items()})
                    written = True
                break
        if not written:
            return None
        normalized, _reason = validate_action(name, merged, self.available_platform_actions())
        return normalized

    def apply_platform_action_params(self, decision: Any, action: dict[str, Any]) -> Optional[str]:
        """把第二段的结果**写回模型那份草稿**（参数槽），返回动作 id。

        写回是必须的：落库与执行读的都是决策里那一份 `platformActions`
        （与贴纸把选中的 `assetId` 写回 `localMedia` 同一条理由）。
        两种拼写指回**同一个**对象（跨 chunk 双读的老规矩，坑 41/46）。
        按位置对齐：草稿里第 N 条就是补问的那一条。
        """
        if not isinstance(decision, Mapping):
            return None
        name = str(action.get('action') or '')
        if not name:
            return None
        raw = decision.get(PLATFORM_ACTION_FIELD)
        if raw is None:
            raw = decision.get('platform_actions')
        if not isinstance(raw, list):
            return None
        for index, item in enumerate(raw):
            if isinstance(item, str):
                if item.strip() == name:
                    raw[index] = dict(action)
                    return name
            elif isinstance(item, Mapping):
                current = str(item.get('action', item.get('actionId', item.get('id'))) or '').strip()
                if current == name and not any(key in item for key in _PARAM_VALUE_KEYS):
                    raw[index] = dict(action)
                    return name
        return None

    def risky_actions_in_use(self, table: Optional[dict[str, str]] = None) -> list[str]:
        """当前**已启用**的危险动作（控制台与启动自检用它显示警告）。"""
        permissions = self.action_permission_table() if table is None else table
        active: list[str] = []
        for action in ACTIONS.values():
            if action.risk != 'dangerous':
                continue
            if effective_permission(action.id, permissions, self.action_switch(action.id)) != 'disabled':
                active.append(action.id)
        return active

    # ------------------------------------------------------------------ #
    # 决策侧的归一化与校验
    # ------------------------------------------------------------------ #

    def normalize_platform_actions(
        self,
        decision: Any,
        session_role: str = '',
        scopes: Iterable[str] = ('private', 'group'),
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """从 decision 里取出并校验 `platformActions`（双拼写都认）。

        返回 `(通过的动作, 拒绝原因)`；拒绝原因**只进日志**，不回灌给模型——模型看不到
        拒绝原因这件事是刻意的：它下一回合会重新决策，把错误说明塞进上下文反而污染叙事。
        """
        raw = None
        if isinstance(decision, dict):
            raw = decision.get(PLATFORM_ACTION_FIELD)
            if raw is None:
                raw = decision.get('platform_actions')
        if not raw:
            return [], []
        available = self.available_platform_actions(session_role, scopes)
        return validate_actions(raw, available, limit=MAX_ACTIONS_PER_TURN)

    def super_admin_user_ids(self) -> tuple[str, ...]:
        """宿主管理员名单（**同步**取值），`admin` 档的判据来源。

        v1.9.9 修（§86）：`admin` 档的判定发生在**同步**上下文里（`resolve_action_session_role()`
        是 `def`，`chunk4` 的提示词组装也同步调它），而历史写法直接 `probe(user_id)` 探
        `transport.is_super_admin` —— 那是个 `async def`，同步调用什么都不 await：

        * 判定**恒假**（协程对象永远为真，但没人把它当结果读）→ 超管判定永远不成立；
        * 每次调用留一条 `RuntimeWarning: coroutine ... was never awaited`（全量日志 41 处）。

        现在只读**同步**的 `transport.known_super_admin_ids()`（`core/service/transport.py`
        的协议口）：它是属性 / 同步方法的形状，同步上下文能直接拿到真值。
        宿主没实现 → 空元组 → `admin` 档一律不放行（安全侧，绝不反过来）。

        兼容口：名单如果给的是**协程 / awaitable**（旧写法：`async def known_super_admin_ids`），
        `super_admin_ids_from_host()` 会把它**关掉**并返回空元组 —— 判定不放行，但
        "协程从未被 await" 的警告也不会污染日志（`asyncio.iscoroutine` 判据与
        `core/service/chunk2._spawn_sticker_task` 同源）。这种情况仍是一条**要修**的
        接线错误，所以由调用方（`resolve_action_session_role`）记一条 warn。
        """
        return super_admin_ids_from_host(getattr(self, 'transport', None))

    def resolve_action_session_role(self, session: Any = None) -> str:
        """判定"发言者在这个动作体系里是什么身份"，供 `admin` / `groupadmin` 档位使用。

        三级来源（从严到宽）：
        1. 会话/参与者的显式角色字段（适配层从群事件里取的 `sender.role`：owner/admin/member）；
        2. 传输层的**同步**超管名单（`transport.known_super_admin_ids()`，宿主管理员名单）；
        3. 都没有 → 空串（**不放行**需要身份的档位）。

        刻意"取不到就当没有身份"：这个参数是多条 `dangerous` 动作的唯一闸门，
        宁可让用户去配权限，也不能因为读不到字段就把踢人权限当成已授予。

        v1.9.9 修（§86）：第 2 条原来探的是 `transport.is_super_admin`（`async def`）并**同步**
        调用它 → 判定恒假 + 每次一条 `coroutine ... was never awaited`。现在读同步名单，
        判定真的能成立；宿主塞了个异步实现时记一条 warn（可见 + 可行动）并按"没身份"继续，
        绝不把异常冒泡出去。
        """
        for key in ('role', 'groupRole', 'group_role', 'senderRole', 'sender_role'):
            value = pick(session, key) if session is not None else None
            if value:
                normalized = str(value).strip().lower()
                return 'admin' if normalized in ('super', 'superadmin', 'super_admin') else normalized
        user_id = str(
            pick(session, 'userId', 'user_id') or pick(session, 'senderId', 'sender_id') or '',
        )
        if not user_id:
            return ''
        transport = getattr(self, 'transport', None)
        probe = getattr(transport, 'known_super_admin_ids', None)
        if probe is not None and not callable(probe):
            # **属性形状**也认（`known_super_admin_ids = ('123',)`）；但一个**标量**属性
            # （字符串/数字）不是名单，是接线错误——可见地拒绝，别静默恒假。
            ids = super_admin_ids_from_host(transport)
            if ids:
                return 'admin' if user_id in ids else ''
            if not isinstance(probe, (list, tuple, set, frozenset)):
                self.report_standalone(
                    'warn',
                    '超级管理员名单接口不是可调用的、也不是名单'
                    '（known_super_admin_ids=%s），同步权限判定读不到；'
                    '请把它实现成同步方法或返回 id 元组的属性' % type(probe).__name__,
                )
            return ''
        if callable(probe):
            try:
                ids = super_admin_ids_from_host(transport)
            except Exception as error:  # noqa: BLE001 - 身份探测失败只当没有身份
                self.report_standalone('warn', '超级管理员名单读取失败，按"无身份"继续 错误=%s', error)
                return ''
            if ids:
                return 'admin' if user_id in ids else ''
            return ''
        # 兼容口：宿主只给了历史那个异步方法（`is_super_admin`）。同步上下文里**不能** await，
        # 所以判定只能是"没有身份"——但这件事必须**可见**（warn 带下一步），
        # 绝不允许静默恒假（历史 bug 就是这么潜伏下来的）。
        legacy = getattr(transport, 'is_super_admin', None)
        if callable(legacy):
            close_awaitable(legacy(user_id))
            self.report_standalone(
                'warn',
                '超级管理员名单只有异步接口（is_super_admin），同步权限判定读不到；'
                '请实现同步的 known_super_admin_ids()，否则「仅管理员」档动作一律不可用',
            )
        return ''

    def action_target_from_participant(self, session: Any = None, channel_id: str = '') -> dict[str, Any]:
        """参与者的坐标 → 动作参数里"留空=本回合对话对象"的补全值。

        群 id 走 `session_group_id()`（**唯一**判据）：生产上的 `SessionView` 把群 id
        存在 `guild_id`/`channel_id`，只读 `groupId` 会把群回合当成私聊
        （后果见 `session_group_id()` 的文档串）。
        """
        if session is None:
            return {}
        target: dict[str, Any] = {
            'user_id': str(pick(session, 'userId', 'user_id') or ''),
            'group_id': session_group_id(session),
            'channel_id': str(channel_id or pick(session, 'channelId', 'channel_id') or ''),
            'platform': str(pick(session, 'platform') or ''),
            'self_id': str(pick(session, 'selfId', 'self_id') or ''),
            'participant_id': str(pick(session, 'id') or ''),
        }
        target['is_group'] = bool(target['group_id'])
        return {key: value for key, value in target.items() if value not in (None, '')}

    async def dispatch_platform_actions(
        self,
        story: Any,
        decision: Any,
        *,
        session: Any = None,
        session_role: str = '',
        channel_id: str = '',
    ) -> list[dict[str, Any]]:
        """回合收尾的统一入口：把决策里的 `platformActions` 校验后执行掉。

        调用点在**投递之后**（私聊 chunk3 / 群聊 chunk1）：她刚说完话再去戳一下、
        点个赞或撤回，顺序才符合直觉；失败也不影响已经发出去的内容。
        """
        target = self.action_target_from_participant(session, channel_id)
        scopes = ('private', 'group') if target.get('is_group') else ('private',)
        role = session_role or self.resolve_action_session_role(session)
        actions, rejected = self.normalize_platform_actions(decision, role, scopes)
        for reason in rejected:
            self.report_standalone('warn', '平台动作被拒绝 原因=%s', reason)
        if not actions:
            return []
        outcomes = await self.execute_platform_actions(
            story, actions, session=session, session_role=role, target=target,
        )
        succeeded = [item for item in outcomes if item['ok']]
        failed = [item for item in outcomes if not item['ok']]
        if succeeded:
            self.report_standalone(
                'info', '平台动作已执行 成功=%d 失败=%d 明细=%s',
                len(succeeded), len(failed),
                '、'.join(item['action'] for item in succeeded),
            )
        return outcomes

    # ------------------------------------------------------------------ #
    # 执行
    # ------------------------------------------------------------------ #

    async def execute_platform_actions(
        self,
        story: Any,
        actions: list[dict[str, Any]],
        *,
        session: Any = None,
        session_role: str = '',
        target: Optional[dict[str, Any]] = None,
        now: Any = None,
    ) -> list[dict[str, Any]]:
        """按顺序执行一批**已校验**的动作，返回逐个结果。

        单个动作失败**不影响后续动作**（一个戳一戳失败不该让后面的定时消息也不办），
        但每个失败都留一条 warn——静默失败会让"她明明做了却没发生"变成无法排查的悬案。
        """
        outcomes: list[dict[str, Any]] = []
        if not actions:
            return outcomes
        for item in actions:
            action_id = str(item.get('action') or '')
            params = dict(item.get('params') or {})
            try:
                if action_id in CORE_HANDLED_ACTIONS:
                    result = await self._run_core_action(
                        story, action_id, params, session=session, target=target, now=now,
                    )
                else:
                    resolved = self._resolve_action_target(params, target)
                    result = await self._run_platform_action(action_id, resolved)
            except Exception as error:  # noqa: BLE001 - 单条动作绝不打断回合
                result = {'ok': False, 'error': '%s: %s' % (type(error).__name__, error)}
            outcome = {'action': action_id, 'ok': bool(result.get('ok')), 'error': str(result.get('error') or '')}
            data = result.get('data')
            if data not in (None, {}, []):
                outcome['data'] = data
            outcomes.append(outcome)
            if not outcome['ok']:
                self.report_standalone(
                    'warn', '平台动作失败 动作=%s 原因=%s', action_id, outcome['error'] or '未知原因',
                )
        await self._record_platform_actions(story, outcomes, session=session)
        return outcomes

    async def _run_platform_action(self, action_id: str, params: dict[str, Any]) -> dict[str, Any]:
        transport = getattr(self, 'transport', None)
        runner = getattr(transport, 'platform_action', None)
        if runner is None:
            return {'ok': False, 'error': 'transport-unavailable'}
        result = await runner(action_id, params)
        return result if isinstance(result, dict) else {'ok': False, 'error': 'bad-transport-result'}

    def _resolve_action_target(self, params: dict[str, Any], target: Optional[dict[str, Any]]) -> dict[str, Any]:
        """把"留空 = 本回合对话对象"的语义落实成具体坐标。

        只补**缺的**参数：模型显式写了 `user_id` / `group_id` 就尊重它（她可能想戳别人）。
        """
        if not target:
            return params
        resolved = dict(params)
        for key in ('user_id', 'group_id', 'channel_id', 'platform', 'self_id', 'is_group'):
            if not resolved.get(key) and target.get(key) not in (None, ''):
                resolved[key] = target.get(key)
        if resolved.get('target') in (None, '', 'self', 'current'):
            resolved.pop('target', None)
        return resolved

    async def _record_platform_actions(
        self, story: Any, outcomes: list[dict[str, Any]], *, session: Any = None,
    ) -> None:
        """把动作结果写回剧本（`[平台动作] …`），下一回合她才知道自己做过什么。"""
        if not outcomes:
            return
        parts: list[str] = []
        for item in outcomes:
            label = ACTIONS[item['action']].label if item['action'] in ACTIONS else item['action']
            parts.append('%s%s' % (label, '成功' if item['ok'] else '失败（%s）' % (item['error'] or '未知原因')))
        summary = '、'.join(parts)
        story_id = str(pick(story, 'id') or '')
        if not story_id:
            return
        try:
            await self.append_entry(
                story_id,
                {
                    'kind': 'life',
                    'actor': 'system',
                    'content': '[平台动作] %s' % summary,
                    # metadata 键保持 snake_case（坑 9）：条目元数据不是 wire payload。
                    'metadata': {'platform_actions': outcomes},
                },
                self.now(),
                str(pick(session, 'participantId', 'participant_id') or ''),
            )
        except Exception as error:  # noqa: BLE001 - 留痕失败不影响动作本身
            self.report_standalone('warn', '平台动作留痕失败 错误=%s', error)

    # ------------------------------------------------------------------ #
    # 输入状态（"对方正在输入…"）
    # ------------------------------------------------------------------ #

    def input_status_config(self) -> dict[str, Any]:
        """输入状态配置（缺失 = 用默认：开、至少可见 600ms、打字节拍 25%）。

        v1.7.4 起这一段住在「运行时」组里（`runtime.input_status`），顶层 `input_status`
        只剩隐藏兼容位；两条路径由 `LEGACY_SECTION_MERGES` 归并，老配置照旧读得到。
        """
        return {
            'enabled': True,
            'min_visible_ms': 600,
            'beat_chance': 0.25,
            **self.action_group_values('runtime.input_status'),
        }

    def typing_indicator_target(self, session: Any) -> dict[str, Any]:
        return self.action_target_from_participant(session)

    @staticmethod
    def typing_target_is_group(target: Any) -> bool:
        """会话坐标是不是群聊——**输入状态只对私聊做**（用户点名）。

        判定依据是坐标里的真实会话类型：显式 `is_group: True`，或者一个非空、
        非 `'0'` 的 `group_id`。私聊在 OneBot 里会把 `group_id` 填成 `0` /
        `'0'` / 干脆不带这个键，三种都算私聊。
        """
        if not isinstance(target, dict):
            return False
        if target.get('is_group') is True:
            return True
        group_id = str(target.get('group_id') or '').strip()
        return group_id not in ('', '0')

    def typing_is_group(self, session: Any) -> bool:
        """本回合会话是不是群聊（`typing_indicator_target` 之上的判定口）。"""
        return self.typing_target_is_group(self.typing_indicator_target(session))

    async def typing_indicator(
        self,
        session: Any,
        content: str = '',
        *,
        already_waited_ms: int = 0,
        extra_delay_ms: int = 0,
        darken: bool = True,
    ) -> int:
        """点亮"正在输入"→按**这条气泡的**打字时长等待→熄灭，返回实际等待的毫秒数。

        **一次调用只服务一条气泡**（用户点名的语义）：调用方在每条气泡**发出之前**调它，
        发出之后这条气泡的灯就已经灭了；下一条要重新调一次、按它自己的字数重新等。
        绝不允许"点亮一次、跨越多条气泡"。

        - **只对私聊做**：群聊直接返回 0，一个平台调用都不发（用户点名）；
        - **刻意不挂主叙事期间**（用户明确要求）：模型思考时对方不该看到"正在输入"——
          那是她还没开始打字。这里只按"这条气泡的打字时间"点亮；
        - `already_waited_ms`：本回合已经等过的时间，不重复等待；
        - 等待时间短于 `min_visible_ms` 时**直接不点亮**（一闪而过只会显得抽风，也不值得多等）；
        - 中途按 `beat_chance` 随机"停一下再接着打"（打字节拍）；
        - `darken=False`：等完**先不熄灯**，交给调用方在**真正发出之后**调 `end_typing`
          熄灭——"发出后立刻中断"这条语义要求熄灯落在投递之后，而不是之前；
        - **任何异常都不影响投递**：输入状态是锦上添花，不是投递的前置条件。
        """
        config = self.input_status_config()
        if config.get('enabled') is False:
            return 0
        target = self.typing_indicator_target(session)
        if not target or self.typing_target_is_group(target):
            return 0
        try:
            total = max(0, int(self.typing_delay_milliseconds(content or '')) - int(already_waited_ms))
        except Exception:  # noqa: BLE001 - 打字时间算不出来就当没配
            total = 0
        total = max(total, 0) + max(0, int(extra_delay_ms))
        try:
            min_visible = max(0, int(config.get('min_visible_ms') or 0))
        except (TypeError, ValueError):
            min_visible = 600
        if total < min_visible:
            return 0
        if darken:
            lit = await self._set_input_status(target, True)
        else:
            # 交给调用方在**投递之后**熄灭：走 `_light_typing` 登记 + 挂兜底定时器，
            # 这样 `end_typing` 知道确实点过灯（没点过就不发"停止输入"——
            # 短于 `min_visible_ms` 的回复本来就不该发任何输入状态）。
            lit = await self._light_typing(target, total)
        if not lit:
            return 0
        try:
            beat_chance = config.get('beat_chance')
            beat_chance = float(beat_chance) if isinstance(beat_chance, (int, float)) else 0.0
            if beat_chance > 0 and self.random() < beat_chance and total >= 2 * min_visible:
                # 打字节拍：先熄一下（像在想措辞），再从剩下的时间里接着打。
                pause = min(total // 3, 1_500)
                await self._set_input_status(target, False)
                await asyncio.sleep(pause / 1000)
                await self._set_input_status(target, True)
                total -= pause
            if total > 0:
                await asyncio.sleep(total / 1000)
        except Exception:  # noqa: BLE001 - 打断/取消都不该冒泡
            pass
        finally:
            if darken:
                await self._set_input_status(target, False)
        return total

    async def begin_typing(self, session: Any, expected_ms: int = 0, *, delay_ms: int = 0) -> bool:
        """**非阻塞**点亮输入状态（分段气泡用）。

        分段气泡的等待发生在 intent 调度那边（`notBefore`），所以这里不能 sleep——
        只负责"把灯点上"。**逐条气泡的边界**由 `delay_ms` 决定：

        - `delay_ms <= 0`：这条气泡的等待窗口**现在**开始 → 立刻点亮；
        - `delay_ms > 0`：这条气泡的窗口要等上一条发出去才开始 → **到点才点亮**。

        这样每条气泡各自有一段属于自己的亮灯窗口（`[上一条发出, 这一条发出]`），
        而不是排期那一刻把所有分段一次点亮、跨越多条气泡（用户点名的旧行为）。
        熄灭仍由投递那一刻的 `end_typing` 负责；额外挂一个**兜底定时器**：万一投递
        路径异常，输入状态也会在 `expected_ms + 30s` 后自动熄灭（对方那头永远停在
        "正在输入"最糟）。群聊一律不点亮。

        两条定时器都排在 `ctx.set_timeout` 上（生产上的 `InterludeContext`，v1.7.9 之前
        写成 core 里根本不存在的 `self.context` → 两条兜底一起静默失效）；参数用
        `functools.partial` 自己绑，因为它的签名就是 `(callback, delay_ms)` 两个参数。
        """
        config = self.input_status_config()
        if config.get('enabled') is False:
            return False
        target = self.typing_indicator_target(session)
        if not target or self.typing_target_is_group(target):
            return False
        expected = max(0, int(expected_ms or 0))
        delay = max(0, int(delay_ms or 0))
        if delay > 0:
            # 窗口还没开始：排一条定时器到点再亮。排期失败就当没点亮（绝不影响投递）。
            host = self.timer_host()
            if host is None:
                return False
            try:
                host.set_timeout(functools.partial(self._typing_light_later, target, expected), delay)
            except Exception:  # noqa: BLE001 - 定时器只是锦上添花
                return False
            return True
        return await self._light_typing(target, expected)

    async def end_typing(self, session: Any) -> None:
        """熄灭输入状态（一条气泡投递完成后调用；重复调用安全）。群聊直接返回。

        **没点亮过就不发"停止输入"**：短于 `min_visible_ms` 的回复、开关关掉、平台不支持
        这几种情况下我们本来就没发过"开始输入"，再补一条"停止"既是多余的平台调用，
        也违背 `min_visible_ms` 的说明（"短于此时长就不发送，避免闪一下"）。
        """
        target = self.typing_indicator_target(session)
        if not target or self.typing_target_is_group(target):
            return
        key = self._typing_key(target)
        if self._typing_lit().pop(key, None) is None:
            return
        await self._set_input_status(target, False)

    async def _light_typing(self, target: dict[str, Any], expected_ms: int = 0) -> bool:
        """点亮 + 挂兜底定时器（`begin_typing` 与定时回调共用的一段）。"""
        if self.typing_target_is_group(target):
            return False
        if not await self._set_input_status(target, True):
            return False
        key = self._typing_key(target)
        self._typing_lit()[key] = target
        timeout_ms = max(5_000, int(expected_ms or 0) + 30_000)
        host = self.timer_host()
        if host is not None:
            try:
                host.set_timeout(functools.partial(self._clear_typing_later, key), timeout_ms)
            except Exception:  # noqa: BLE001 - 定时器只是兜底
                pass
        return True

    def _typing_light_later(self, target: dict[str, Any], expected_ms: int = 0) -> None:
        """到点才点亮（由 `ctx.set_timeout` 调用；同步回调里起一个即发任务）。"""
        try:
            asyncio.ensure_future(self._light_typing(target, max(0, int(expected_ms or 0))))
        except Exception:  # noqa: BLE001 - 事件循环已关就放弃
            pass

    def _typing_lit(self) -> dict[str, dict[str, Any]]:
        store = getattr(self, '_typing_lit_store', None)
        if store is None:
            store = {}
            self._typing_lit_store = store
        return store

    def _typing_key(self, target: dict[str, Any]) -> str:
        return '%s|%s|%s|%s' % (
            target.get('platform') or '', target.get('self_id') or '',
            target.get('group_id') or '', target.get('user_id') or '',
        )

    def _clear_typing_later(self, key: str = '') -> None:
        """兜底熄灭（由 `ctx.set_timeout` 调用；同步回调里起一个即发任务）。

        **刻意保留**（v1.7.9 复核，不是历史残留）：`end_typing` 只在投递路径正常走到
        时才熄灯，投递中途异常/被取消时全靠这条兜底把灯熄掉——对方那头永远停在
        "正在输入"是最糟的表现。它只影响"多一次熄灭调用"，不影响逐条亮灭的语义。
        """
        target = self._typing_lit().pop(key, None)
        if not target:
            return
        try:
            asyncio.ensure_future(self._set_input_status(target, False))
        except Exception:  # noqa: BLE001 - 事件循环已关就放弃
            pass

    @staticmethod
    def typing_error_means_unsupported(result: Any) -> bool:
        """这次失败是不是"这个平台/会话根本不支持输入状态"。

        适配层对"没有 OneBot 客户端 / 不是 OneBot 平台"显式标 `unsupported`；平台的
        明确拒绝（retcode 1404 / 文案里写"不支持"）同样算。其余一律当**真实错误**——
        真实错误绝不能因为"降噪"被吞掉（用户点名的红线）。
        """
        if not isinstance(result, dict):
            return False
        if result.get('unsupported') is True:
            return True
        if result.get('retcode') == 1404:
            return True
        return '不支持' in str(result.get('error') or '')

    async def _set_input_status(self, target: dict[str, Any], typing: bool) -> bool:
        """点亮/熄灭输入状态。群聊、平台不支持、调用失败一律返回 False。

        口径（用户 2026-09-28 点名降噪）：

        * **群聊直接不做**——不点亮、不熄灭、不调用、不告警；
        * **失败只留一条 debug**（原文带上平台错误，绝不忍吞）；
        * 只有"确实不支持"这种一次性信息才 warn，且**按会话节流** 10 分钟——
          否则每个回合都会重打一遍同样的 warn。
        """
        if self.typing_target_is_group(target):
            return False
        setter = getattr(getattr(self, 'transport', None), 'set_input_status', None)
        if not callable(setter):
            return False
        # 标签按**真实会话类型**给（这里是私聊——群聊在上面就返回了），别让平台错误文案
        # 里的"群聊"两个字把这条日志标成 `[群聊]`（用户贴日志点名）。
        category = '[系统]'
        try:
            result = await setter(target, typing)
        except Exception as error:  # noqa: BLE001 - 输入状态绝不冒泡、绝不影响投递
            self.report_standalone(
                'debug', '输入状态设置失败 输入中=%s 错误=%s', typing, error, category=category,
            )
            return False
        ok = bool(result.get('ok')) if isinstance(result, dict) else bool(result)
        if ok:
            return True
        error = str(result.get('error') or '') if isinstance(result, dict) else ''
        if self.typing_error_means_unsupported(result):
            self.note_access_skip(
                'input-status-unsupported|%s|%s' % (
                    target.get('platform') or '', target.get('self_id') or '',
                ),
                INPUT_STATUS_UNSUPPORTED_NOTE_INTERVAL_MS,
                '当前会话不支持「正在输入」状态，已停止设置 输入中=%s 原因=%s',
                typing, error or '平台没有这条能力',
                category=category,
            )
        else:
            self.report_standalone(
                'debug', '输入状态设置失败 输入中=%s 错误=%s', typing, error, category=category,
            )
        return False

    # ------------------------------------------------------------------ #
    # 本机动作：定时消息 / 定时命令
    # ------------------------------------------------------------------ #

    async def _run_core_action(
        self,
        story: Any,
        action_id: str,
        params: dict[str, Any],
        *,
        session: Any = None,
        target: Optional[dict[str, Any]] = None,
        now: Any = None,
    ) -> dict[str, Any]:
        if action_id in QZONE_ACTION_KINDS_BY_ID:
            return await self._run_qzone_action(story, action_id, params)
        if action_id in ('list_qzone_posts', 'list_qzone_feeds'):
            runner = getattr(self, 'qzone_read', None)
            if not callable(runner):
                return {'ok': False, 'error': 'qzone-unavailable'}
            kind = 'moods' if action_id == 'list_qzone_posts' else 'feed'
            include_self = action_id == 'list_qzone_posts' and not (
                params.get('target_uin') or params.get('targetUin')
            )
            result = await runner(story, kind, params, include_self=include_self)
            if not isinstance(result, dict):
                return {'ok': False, 'error': 'bad-qzone-result'}
            data = {k: v for k, v in result.items() if k not in ('ok', 'error')}
            return {'ok': bool(result.get('ok')), 'error': str(result.get('error') or ''), 'data': data or {}}
        if action_id == 'schedule_message':
            return await self._schedule_message(story, params, session=session, target=target, now=now)
        if action_id == 'list_scheduled_messages':
            return await self._list_scheduled_messages(story)
        if action_id == 'cancel_scheduled_message':
            return await self._cancel_scheduled_messages(story, params)
        if action_id == 'schedule_command':
            return await self._schedule_command(story, params, now=now)
        if action_id == 'list_scheduled_commands':
            return await self._list_scheduled_commands(story)
        if action_id == 'cancel_scheduled_command':
            return await self._cancel_scheduled_command(story, params)
        return {'ok': False, 'error': 'unsupported-core-action: %s' % action_id}

    async def _run_qzone_action(
        self, story: Any, action_id: str, params: dict[str, Any],
    ) -> dict[str, Any]:
        """把目录里的空间动作转成 `qzone_execute` 的入参（两种拼写都认）。"""
        runner = getattr(self, 'qzone_execute', None)
        if not callable(runner):
            return {'ok': False, 'error': 'qzone-unavailable'}
        kind = QZONE_ACTION_KINDS_BY_ID[action_id]
        payload: dict[str, Any] = {}
        content = params.get('content')
        if isinstance(content, str) and content.strip():
            payload['content'] = content.strip()
        for key in ('tid',):
            if params.get(key):
                payload[key] = str(params[key]).strip()
        target_uin = params.get('target_uin') or params.get('targetUin')
        if target_uin:
            payload['targetUin'] = str(target_uin).strip()
        ugc_right = params.get('ugc_right') or params.get('ugcRight')
        if isinstance(ugc_right, str) and ugc_right.strip():
            # v1.7.6：目录给模型看的是**五档中文标签**（与 `set_qzone_visibility.visible`
            # 同一份枚举），校验层已经把它译成整数；这里再兜一次手写调用——
            # 可见性写错是隐私事故，宁可当场报错也**不许**静默落到默认那一档。
            resolved_right = qzone_visible_value(ugc_right.strip())
            if resolved_right is None:
                return {'ok': False, 'error': '可见性只能是这五档之一：%s' % ' / '.join(QZONE_VISIBILITY_VALUES)}
            ugc_right = resolved_right
        if isinstance(ugc_right, int) and not isinstance(ugc_right, bool):
            payload['ugcRight'] = ugc_right
        # v1.7.5：可见范围那一档传的是**五档中文标签**（`visible`），由 `qzone_execute`
        # 译成 `ugc_right`；`target_uins` 只在「部分人可见 / 部分人不可见」两档必填。
        visible = params.get('visible')
        if isinstance(visible, str) and visible.strip():
            payload['visible'] = visible.strip()
        if params.get('target_uins'):
            payload['targetUins'] = list(params['target_uins'])
        result = await runner(story, kind, payload)
        if not isinstance(result, dict):
            return {'ok': False, 'error': 'bad-qzone-result'}
        data = {key: value for key, value in result.items() if key not in ('ok', 'error')}
        return {'ok': bool(result.get('ok')), 'error': str(result.get('error') or ''), 'data': data or {}}

    async def _schedule_message(
        self, story: Any, params: dict[str, Any], *, session: Any = None,
        target: Optional[dict[str, Any]] = None, now: Any = None,
    ) -> dict[str, Any]:
        """定时消息 = 一条 `scheduled-message` intent（**不是**硬定时器）。

        刻意走 intent：到点后它作为"到期的可能"进入上下文，由她在那一回合里**用当时的情景**
        说出来（可能因为对方在忙而改口），而不是像外挂脚本那样到点就吐一句固定文本。
        """
        content = str(params.get('content') or '').strip()
        if not content:
            return {'ok': False, 'error': 'empty-content'}
        moment = now or self.now()
        send_at = parse_iso_datetime(params.get('send_at'))
        delay = params.get('delay_minutes')
        if send_at is None and isinstance(delay, int):
            from datetime import timedelta

            send_at = moment + timedelta(minutes=delay)
        if send_at is None:
            return {'ok': False, 'error': 'missing-schedule-time'}
        if send_at <= moment:
            return {'ok': False, 'error': 'schedule-time-in-the-past'}
        schedule_target = dict(target or {})
        explicit = str(params.get('target') or '').strip()
        participant_id = str(schedule_target.get('participant_id') or '')
        try:
            row = await self.db_create('interlude_intent', {
                'storyId': str(pick(story, 'id') or ''),
                'participantId': participant_id,
                'type': SCHEDULED_MESSAGE_INTENT,
                'summary': content[:200],
                'notBefore': iso(send_at),
                'status': 'open',
                'payload': {
                    'content': content,
                    'target': explicit,
                    'channel_id': schedule_target.get('channel_id') or '',
                    'is_group': bool(schedule_target.get('is_group')),
                },
                'createdAt': iso(moment),
                'updatedAt': iso(moment),
            })
        except Exception as error:  # noqa: BLE001
            return {'ok': False, 'error': 'schedule-failed: %s' % error}
        intent_id = (row or {}).get('id')
        return {
            'ok': True,
            'data': {'id': intent_id, 'sendAt': iso(send_at), 'content': content[:200]},
        }

    async def _list_scheduled_messages(self, story: Any) -> dict[str, Any]:
        rows = await self._scheduled_message_rows(story)
        items = [
            {
                'id': row.get('id'),
                'sendAt': row.get('notBefore'),
                'content': row.get('summary'),
                'status': row.get('status'),
            }
            for row in rows
        ]
        return {'ok': True, 'data': {'count': len(items), 'items': items[:50]}}

    async def _cancel_scheduled_messages(self, story: Any, params: dict[str, Any]) -> dict[str, Any]:
        rows = await self._scheduled_message_rows(story)
        if params.get('target') == 'all':
            targets = rows
        else:
            wanted = params.get('id')
            targets = [row for row in rows if wanted is not None and int(row.get('id') or 0) == int(wanted)]
            if not targets and wanted is None:
                targets = rows[:1]  # 不写 id 又没写 all：默认取消最近排的一条，最符合直觉
        if not targets:
            return {'ok': False, 'error': 'no-such-scheduled-message'}
        moment = self.now()
        cancelled: list[int] = []
        for row in targets:
            await self.db_set(
                'interlude_intent',
                {'id': row.get('id')},
                {'status': 'cancelled', 'updatedAt': iso(moment)},
            )
            cancelled.append(int(row.get('id') or 0))
        return {'ok': True, 'data': {'cancelled': cancelled}}

    async def _scheduled_message_rows(self, story: Any) -> list[dict[str, Any]]:
        try:
            rows = await self.db_get('interlude_intent', {'storyId': str(pick(story, 'id') or '')})
        except Exception as error:  # noqa: BLE001
            self.report_standalone('warn', '定时消息列表读取失败 错误=%s', error)
            return []
        open_rows = [
            row for row in rows or []
            if str(row.get('type') or '') == SCHEDULED_MESSAGE_INTENT and str(row.get('status') or '') == 'open'
        ]
        open_rows.sort(key=lambda row: str(row.get('notBefore') or ''))
        return open_rows

    async def _schedule_command(self, story: Any, params: dict[str, Any], *, now: Any = None) -> dict[str, Any]:
        """登记一条定时命令（cron 5 段；只认目录里登记过的命令）。"""
        command = str(params.get('command') or '').strip()
        if command not in DEFAULT_COMMAND_CATALOG:
            return {'ok': False, 'error': 'unknown-command: %s' % (command or '(空)')}
        cron = normalize_cron(params.get('cron'))
        if cron is None:
            return {'ok': False, 'error': 'bad-cron-expression'}
        moment = now or self.now()
        next_run = cron_next_run(cron, moment)
        if next_run is None:
            return {'ok': False, 'error': 'cron-never-matches'}
        try:
            row = await self.db_create('interlude_scheduled_command', {
                'storyId': str(pick(story, 'id') or ''),
                'command': command,
                'params': params.get('params') if isinstance(params.get('params'), dict) else {},
                'cron': cron,
                'enabled': True,
                'nextRunAt': iso(next_run),
                'lastRunAt': None,
                'lastStatus': '',
                'lastError': '',
                'runCount': 0,
                'createdAt': iso(moment),
                'updatedAt': iso(moment),
            })
        except Exception as error:  # noqa: BLE001
            return {'ok': False, 'error': 'schedule-failed: %s' % error}
        return {
            'ok': True,
            'data': {
                'id': (row or {}).get('id'),
                'command': command,
                'cron': cron,
                'nextRunAt': iso(next_run),
            },
        }

    async def _list_scheduled_commands(self, story: Any) -> dict[str, Any]:
        rows = await self._scheduled_command_rows(story)
        items = [
            {
                'id': row.get('id'),
                'command': row.get('command'),
                'cron': row.get('cron'),
                'nextRunAt': row.get('nextRunAt'),
                'enabled': bool(row.get('enabled')),
                'runCount': row.get('runCount') or 0,
                'lastStatus': row.get('lastStatus') or '',
            }
            for row in rows
        ]
        return {'ok': True, 'data': {'count': len(items), 'items': items[:50]}}

    async def _cancel_scheduled_command(self, story: Any, params: dict[str, Any]) -> dict[str, Any]:
        wanted = params.get('id')
        rows = await self._scheduled_command_rows(story)
        targets = [row for row in rows if wanted is not None and int(row.get('id') or 0) == int(wanted)]
        if not targets:
            return {'ok': False, 'error': 'no-such-scheduled-command'}
        moment = self.now()
        for row in targets:
            await self.db_set(
                'interlude_scheduled_command',
                {'id': row.get('id')},
                {'enabled': False, 'updatedAt': iso(moment)},
            )
        return {'ok': True, 'data': {'cancelled': [int(row.get('id') or 0) for row in targets]}}

    async def _scheduled_command_rows(self, story: Any) -> list[dict[str, Any]]:
        try:
            rows = await self.db_get(
                'interlude_scheduled_command', {'storyId': str(pick(story, 'id') or '')},
            )
        except Exception as error:  # noqa: BLE001 - 表可能还没建（旧库）
            self.report_standalone('warn', '定时命令列表读取失败 错误=%s', error)
            return []
        active = [row for row in rows or [] if bool(row.get('enabled'))]
        active.sort(key=lambda row: str(row.get('nextRunAt') or ''))
        return active

    async def sweep_scheduled_commands(self, *, now: Any = None, limit: int = 5) -> int:
        """到点就执行定时命令；返回执行条数。

        `nextRunAt` 一旦到期就**先推进再执行**（避免执行期间再次被扫到导致重复触发），
        执行失败只记 `lastError`，绝不因为一条命令失败而停掉整个 sweep。
        """
        moment = now or self.now()
        executed = 0
        try:
            rows = await self.db_get('interlude_scheduled_command', {})
        except Exception:  # noqa: BLE001 - 表不存在时静默（旧库）
            return 0
        for row in rows or []:
            if executed >= limit:
                break
            if not bool(row.get('enabled')):
                continue
            due = parse_iso_datetime(row.get('nextRunAt'))
            if due is None or due > moment:
                continue
            cron = normalize_cron(row.get('cron'))
            following = cron_next_run(cron, moment) if cron else None
            await self.db_set('interlude_scheduled_command', {'id': row.get('id')}, {
                'nextRunAt': iso(following) if following else None,
                'lastRunAt': iso(moment),
                'updatedAt': iso(moment),
            })
            status, error = await self.run_scheduled_command(
                str(row.get('command') or ''), row.get('params') or {}, row=row, now=moment,
            )
            await self.db_set('interlude_scheduled_command', {'id': row.get('id')}, {
                'lastStatus': status,
                'lastError': error[:500],
                'runCount': int(row.get('runCount') or 0) + 1,
                'updatedAt': iso(moment),
            })
            executed += 1
            if status != 'ok':
                self.report_standalone(
                    'warn', '定时命令执行失败 命令=%s 原因=%s', row.get('command'), error or '未知原因',
                )
        return executed

    async def run_scheduled_command(
        self, command: str, params: dict[str, Any], *, row: Any = None, now: Any = None,
    ) -> tuple[str, str]:
        """执行一条登记过的命令，返回 `(status, error)`。

        目录里的命令都是**本插件自己的**内部动作（整理记忆、推进一次…），不是用户随便填的
        字符串——所以这里可以安全地按白名单分发。
        """
        entry = DEFAULT_COMMAND_CATALOG.get(command)
        if entry is None:
            return 'unknown-command', '未登记的命令：%s' % command
        handler_name = entry.get('handler') or ''
        handler = getattr(self, handler_name, None)
        if handler is None:
            return 'unavailable', '处理器不存在：%s' % handler_name
        try:
            await handler(params, row=row, now=now)
        except Exception as error:  # noqa: BLE001 - 命令失败只记账
            return 'failed', '%s: %s' % (type(error).__name__, error)
        return 'ok', ''

    def scheduled_command_catalog(self) -> list[dict[str, str]]:
        """给控制台与提示词看的可排期命令清单。"""
        return [
            {'command': name, 'label': entry.get('label', name), 'summary': entry.get('summary', '')}
            for name, entry in DEFAULT_COMMAND_CATALOG.items()
        ]

