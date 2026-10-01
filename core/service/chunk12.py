"""Chunk12 mixin：平台动作执行层（本移植版新增）。

上游没有这一层——上游的动作是写死在脚本/服务里的（群表态、原生表情、贴纸），而本移植版
把"她能对 QQ 做什么"做成了**目录驱动**的可扩展面（见 `core/platform_actions.py`）。
本文件是执行侧，负责四件事：

1. **可用集**：把「配置开关」⊗「独立权限表」⊗「会话身份」合成"这一回合她实际能调的动作"，
   交给提示词注入（模型只该看到它真能调的）；
2. **校验**：模型写的 `platformActions` 过 `validate_actions`，越界/未知/超限一律拒绝并留证；
3. **执行**：平台类动作走 `transport.platform_action`；**本机类动作**（定时消息/定时命令）
   留在 core 里办（它们不碰平台，只写表）；
4. **留痕**：执行结果写回剧本（`[平台动作] …`）并记日志——动作发生过就该在剧本里看得见，
   否则模型下一回合不知道自己戳过谁、撤回没撤回成功。

成员清单铁律（坑 61）只约束 chunk3/chunk9，本 chunk 可以自由加方法。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Iterable, Optional

from ..platform_actions import (
    ACTIONS,
    PLATFORM_ACTION_FIELD,
    action_config_group,
    describe_actions,
    effective_permission,
    normalize_permissions,
    resolve_permission,
    validate_actions,
)
from ..scheduled_command import (
    DEFAULT_COMMAND_CATALOG,
    cron_next_run,
    normalize_cron,
    parse_iso_datetime,
)
from ..time import iso
from .base import ServiceBase, pick
from .config import merge_legacy_section_values, read_section_path

__all__ = ['ServiceChunk12']

#: 权限表文件名（独立 JSON，落在插件数据目录；与 `astrbot_bridge` 的写入端同一个名字）。
ACTION_PERMISSIONS_FILE = 'action_permissions.json'

#: 一次回合最多执行几个动作（与 `validate_actions` 的上限同源，防止模型刷屏）。
MAX_ACTIONS_PER_TURN = 8

#: **由 core 自己办**的动作（不碰平台）：定时消息走既有的 intent 表，定时命令走
#: `interlude_scheduled_command`。其余动作一律走传输层。
CORE_HANDLED_ACTIONS = frozenset({
    'schedule_message', 'list_scheduled_messages', 'cancel_scheduled_message',
    'schedule_command', 'list_scheduled_commands', 'cancel_scheduled_command',
    # QQ 空间的三条**写动作**必须走 chunk13 的 `qzone_execute`（限流门 → 审计行 →
    # 动作 → 剧本条目），直通传输层会绕过风控与账本（上游这三个动作在叙事路径上
    # 也是走 `qzoneExecute`）。只读的 `list_qzone_posts` / 危险的 `delete_qzone_post`
    # 上游没有对应服务成员，直通传输层。
    'publish_qzone_post', 'comment_qzone_post', 'like_qzone_post', 'forward_qzone_post',
    # v1.7.5：改说说可见范围（`emotion_cgi_update`）同样是**本机办**的写动作——
    # 它要先读回正文、过限流门、落审计行；直通传输层会绕过这一切（而且平台根本没有
    # 这条原生动作名可打）。
    'set_qzone_visibility',
    # v1.7.1：读类也收进本机——NapCat WebSocket 方案（get_cookies + QZone CGI）
    # 要按账号端点解析、并且**不能**让只读动作去撞限流门。
    'list_qzone_posts', 'list_qzone_feeds',
})

#: 目录动作 id → `qzone_execute` 的 kind。
QZONE_ACTION_KINDS_BY_ID = {
    'publish_qzone_post': 'post',
    'comment_qzone_post': 'comment',
    'like_qzone_post': 'like',
    # v1.7.1：转发说说也是**写**动作（受限流门与账本管）。只读的
    # `list_qzone_feeds` / `list_qzone_posts` 与危险的 `delete_qzone_post` 直通传输层。
    'forward_qzone_post': 'forward',
    # v1.7.5：改可见范围（配额按**发帖**那一档算，见 `core/qzone.evaluate_qzone_gate`）。
    'set_qzone_visibility': 'visibility',
}

#: 定时消息在 intent 表里的类型名（内部调度账，控制台「承诺与意图」面板会标成内部）。
SCHEDULED_MESSAGE_INTENT = 'scheduled-message'


class ServiceChunk12(ServiceBase):
    """Chunk12 mixin：平台动作的权限判定、校验与执行。"""

    # ------------------------------------------------------------------ #
    # 权限与可用集
    # ------------------------------------------------------------------ #

    def action_permission_table(self) -> dict[str, str]:
        """读独立权限表（`<数据目录>/action_permissions.json`）。

        坏文件/缺文件都只回落空表（= 全部走目录默认档），绝不抛——权限表是**运行期**
        的东西，不该因为一个手改坏的 JSON 让整条叙事链起不来。读取失败会留一条 warn。
        """
        base = self.context.base_dir if getattr(self, 'context', None) is not None else ''
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
        """
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
        """渲染进提示词的动作目录（只列可调项）。"""
        return describe_actions(available)

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

    def resolve_action_session_role(self, session: Any = None) -> str:
        """判定"发言者在这个动作体系里是什么身份"，供 `admin` / `groupadmin` 档位使用。

        三级来源（从严到宽）：
        1. 会话/参与者的显式角色字段（适配层从群事件里取的 `sender.role`：owner/admin/member）；
        2. 传输层的超管查询（`transport.is_super_admin(user_id)`，宿主管理员名单）；
        3. 都没有 → 空串（**不放行**需要身份的档位）。

        刻意"取不到就当没有身份"：这个参数是多条 `dangerous` 动作的唯一闸门，
        宁可让用户去配权限，也不能因为读不到字段就把踢人权限当成已授予。
        """
        for key in ('role', 'groupRole', 'group_role', 'senderRole', 'sender_role'):
            value = pick(session, key) if session is not None else None
            if value:
                normalized = str(value).strip().lower()
                return 'admin' if normalized in ('super', 'superadmin', 'super_admin') else normalized
        user_id = str(
            pick(session, 'userId', 'user_id') or pick(session, 'senderId', 'sender_id') or '',
        )
        probe = getattr(getattr(self, 'transport', None), 'is_super_admin', None)
        if user_id and callable(probe):
            try:
                if probe(user_id):
                    return 'admin'
            except Exception:  # noqa: BLE001 - 身份探测失败只当没有身份
                return ''
        return ''

    def action_target_from_participant(self, session: Any = None, channel_id: str = '') -> dict[str, Any]:
        """参与者的坐标 → 动作参数里"留空=本回合对话对象"的补全值。"""
        if session is None:
            return {}
        target: dict[str, Any] = {
            'user_id': str(pick(session, 'userId', 'user_id') or ''),
            'group_id': str(pick(session, 'groupId', 'group_id') or ''),
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

    async def typing_indicator(
        self,
        session: Any,
        content: str = '',
        *,
        already_waited_ms: int = 0,
        extra_delay_ms: int = 0,
    ) -> int:
        """点亮"正在输入"→按打字时间等待→熄灭，返回实际等待的毫秒数。

        **刻意不挂主叙事期间**（用户明确要求）：模型思考时对方不该看到"正在输入"——
        那是她还没开始打字。这里只按"这条气泡的打字时间"点亮，**一条气泡一次点亮/熄灭**，
        所以分气泡投递天然就是多次闪光。

        - `already_waited_ms`：本回合已经等过的时间（首条消息的打字下限），不重复等待；
        - 等待时间短于 `min_visible_ms` 时**直接不点亮**（一闪而过只会显得抽风，也不值得多等）；
        - 中途按 `beat_chance` 随机"停一下再接着打"（打字节拍）；
        - **任何异常都不影响投递**：输入状态是锦上添花，不是投递的前置条件。
        """
        config = self.input_status_config()
        if config.get('enabled') is False:
            return 0
        target = self.typing_indicator_target(session)
        if not target:
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
        lit = await self._set_input_status(target, True)
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
            await self._set_input_status(target, False)
        return total

    async def begin_typing(self, session: Any, expected_ms: int = 0) -> bool:
        """**非阻塞**点亮输入状态（分段气泡用）。

        分段气泡的等待发生在 intent 调度那边（`notBefore`），所以这里不能 sleep——只点亮，
        再由投递那一刻调 `end_typing` 熄灭。额外挂一个**兜底定时器**：万一投递路径异常，
        输入状态也会在 `expected_ms + 30s` 后自动熄灭（对方那头永远停在"正在输入"最糟）。
        """
        config = self.input_status_config()
        if config.get('enabled') is False:
            return False
        target = self.typing_indicator_target(session)
        if not target:
            return False
        if not await self._set_input_status(target, True):
            return False
        key = self._typing_key(target)
        self._typing_lit()[key] = target
        timeout_ms = max(5_000, int(expected_ms or 0) + 30_000)
        try:
            self.context.set_timeout(self._clear_typing_later, timeout_ms, key)
        except Exception:  # noqa: BLE001 - 定时器只是兜底
            pass
        return True

    async def end_typing(self, session: Any) -> None:
        """熄灭输入状态（分段气泡投递完成后调用；重复调用安全）。"""
        target = self.typing_indicator_target(session)
        if not target:
            return
        key = self._typing_key(target)
        self._typing_lit().pop(key, None)
        await self._set_input_status(target, False)

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
        """兜底熄灭（由 `context.set_timeout` 调用；同步回调里起一个即发任务）。"""
        target = self._typing_lit().pop(key, None)
        if not target:
            return
        try:
            asyncio.ensure_future(self._set_input_status(target, False))
        except Exception:  # noqa: BLE001 - 事件循环已关就放弃
            pass

    async def _set_input_status(self, target: dict[str, Any], typing: bool) -> bool:
        """点亮/熄灭输入状态。平台不支持、没有账号、调用失败一律静默返回 False。"""
        setter = getattr(getattr(self, 'transport', None), 'set_input_status', None)
        if not callable(setter):
            return False
        try:
            result = await setter(target, typing)
        except Exception as error:  # noqa: BLE001
            self.report_standalone('debug', '输入状态设置失败 typing=%s 错误=%s', typing, error)
            return False
        return bool(result.get('ok')) if isinstance(result, dict) else bool(result)

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

