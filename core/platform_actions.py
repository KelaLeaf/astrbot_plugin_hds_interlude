"""平台动作目录（本移植版新增）：模型能对 QQ 做的**全部**动作的单一事实源。

为什么要有这一层：动作会越来越多（戳一戳、点赞、撤回、群管理、定时、资料、联系人…
四十多个），如果每个动作各自散落在「提示词写一段 + chunk4 解析一处 + 校验一处 +
适配层实现一处」，加一个动作要动四个文件、漏一处就是静默失效。这里把每个动作声明成
一条 `PlatformAction`，由它**驱动**四件事：

1. **提示词注入**（v1.9.9 起是**两段式**，见 `describe_action_shortlist` / `describe_action_params`）：
   每回合只注入"屏幕上有哪些按钮"（`id` + 一句短标签，**不含参数**）；她选定某条动作之后，
   才把**那一条**的参数表交给她去填（最多一次额外调用）。两段都只列**当前启用**的动作，
   模型看到的就是它真能调的；
2. **校验**：`validate_action()` 按参数声明检查类型/范围/枚举，越界一律拒绝并给出理由；
3. **权限**：`resolve_permission()` 把「配置开关」与「独立权限表」合成生效档位；
4. **控制台与文档**：面板、权限表（`action_permissions.json`）与 `docs/ACTIONS.md` 都读它。

键名约定：动作 id 用 **snake_case**（与 NapCat 的 API 名一致，便于 grep 与排障）；
决策里承载它们的字段是 wire 上的 `platformActions`（camelCase，与其它 payload 字段一致）；
参数名同样 snake_case。权限表 JSON 的键就是动作 id。

**风险分级**（`risk`）：

- `safe`：只读或低影响（查资料、查群信息、戳一戳、点赞、定时消息）；
- `sensitive`：会影响对方感知或机器人身份（撤回、改签名/头像、改 QQ 状态、拉历史）；
- `dangerous`：不可逆或影响真实社交关系（踢人、禁言、全员禁言、设管理、删好友、
  改群名/群公告之外的破坏性操作、群文件删除）。

`dangerous` 的默认档位一律是 `disabled`，而且**开关默认关着**；开关落在它自己类别所属
的那个子组里（群管理类进 `robot_actions.group`、空间类进 `robot_actions.qzone`、
其余进 `robot_actions.chat`），每个危险开关的 `hint` 就是那句警告 `RISK_WARNING`。

v1.7.2 曾把危险动作单独收进一个"风险操作"组，随后按用户要求取消了这个分组：
「都在机器人动作配置组，然后分类会话动作 / 群管理动作 / QQ 空间动作，这里不用单独把
风险操作分离一个类，因为控制台动作页已经有标注了」。**危险是动作的属性**（控制台
「动作」页给它们打红色徽章），不是配置页里独立的一类分组。

v1.7.4 起这一层只有**一个**配置页父组 `robot_actions`（标题「机器人动作」），底下三个
子组 = `chat` / `group` / `qzone`（标题「会话动作 / 群管理动作 / QQ 空间动作」）。
落点写在 `ACTION_CONFIG_GROUPS` 里，是**点分路径**（`robot_actions.chat` 这种）。
旧组名（`actions_chat` / `actions_group` / `actions_qzone` / 七个 v1.6.0 老组）仍留在
schema 里当隐藏兼容位（读配置时照旧认它里面的键），细节见 `docs/PORTING_NOTES.md` §37。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence

from .qzone import QZONE_VISIBILITY_VALUES

__all__ = [
    'ACTIONS',
    'ACTION_CATEGORIES',
    'ACTION_CONFIG_GROUPS',
    'ACTION_CONFIG_GROUP_LABELS',
    'ACTION_CONFIG_GROUP_ROOTS',
    'ACTION_CONFIG_SECTION',
    'VOICE_ACTION_IDS',
    'action_config_group',
    'PERMISSION_TIERS',
    'PLATFORM_ACTION_FIELD',
    'QZONE_VISIBILITY_ALIASES',
    'QZONE_VISIBILITY_CHOICE_VALUES',
    'QZONE_VISIBILITY_LABELS',
    'RISK_LEVELS',
    'RISK_WARNING',
    'PlatformAction',
    'action_param_example',
    'action_risks',
    'actions_by_category',
    'describe_action_params',
    'describe_action_shortlist',
    'describe_actions',
    'effective_permission',
    'is_action_enabled',
    'normalize_permissions',
    'permission_for',
    'resolve_permission',
    'risky_actions',
    'validate_action',
    'validate_actions',
]

#: 决策里承载平台动作的字段（wire camelCase）。
PLATFORM_ACTION_FIELD = 'platformActions'

#: 生效档位（与参考插件的四档同构）。`groupadmin` = 仅群聊里群主/管理员可用。
PERMISSION_TIERS = ('global', 'groupadmin', 'admin', 'disabled')

RISK_LEVELS = ('safe', 'sensitive', 'dangerous')

#: 危险动作开关的说明文案（用户指定的原句，逐字）。
RISK_WARNING = '此标签下功能具有一定风险，易误操作，请谨慎开启。'


class ActionParam:
    """一个动作参数的声明（校验与提示词都读它）。"""

    __slots__ = (
        'name', 'label', 'type', 'required', 'minimum', 'maximum', 'choices', 'note',
        'aliases', 'choice_values',
    )

    def __init__(
        self,
        name: str,
        label: str = '',
        type: str = 'string',  # string | int | bool | list | object
        required: bool = False,
        minimum: Optional[float] = None,
        maximum: Optional[float] = None,
        choices: Sequence[str] = (),
        note: str = '',
        aliases: Optional[Mapping[str, str]] = None,
        choice_values: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.name = name
        self.label = label or name
        self.type = type
        self.required = required
        self.minimum = minimum
        self.maximum = maximum
        self.choices = tuple(choices)
        self.note = note
        #: **旧写法 → 规范枚举值**（v1.7.6）。给"模型手里还留着老提示词记忆"的参数留的
        #: 兼容口：键是历史上允许过的标量写法，值是 `choices` 里那一项。校验层先查它，
        #: 命中就按规范值继续走；查不到又不在 `choices` 里 —— 照旧拒绝并列可选值。
        self.aliases: dict[str, str] = dict(aliases or {})
        #: **规范枚举值 → 对外（wire / 平台）取值**。缺省表示"枚举值本身就是对外取值"
        #: （例如 `set_qzone_visibility.visible` 直接吃中文标签）。写动作的枚举常常
        #: 比平台值的可读性好得多，这一层把两件事拆开：模型看标签，平台收数字。
        self.choice_values: dict[str, Any] = dict(choice_values or {})

    def describe(self) -> str:
        parts = [self.name]
        if self.choices:
            parts.append('(%s)' % '|'.join(self.choices))
        elif self.type != 'string':
            parts.append(':%s' % self.type)
        if self.minimum is not None or self.maximum is not None:
            low = '' if self.minimum is None else '%g' % self.minimum
            high = '' if self.maximum is None else '%g' % self.maximum
            parts.append('[%s~%s]' % (low, high))
        if self.required:
            parts.append('必填')
        return ' '.join(parts)


@dataclass(frozen=True)
class PlatformAction:
    """一条平台动作的声明。"""

    id: str
    category: str
    label: str
    summary: str
    params: tuple[ActionParam, ...] = ()
    risk: str = 'safe'
    #: 独立权限表里的默认档位（危险动作默认关闭）。
    default_permission: str = 'global'
    #: 对应的配置开关键（在 `_conf_schema.json` 的哪个 key 下），由 schema 生成器使用。
    scopes: tuple[str, ...] = ('private', 'group')
    returns: str = ''
    aliases: tuple[str, ...] = field(default=())
    #: 能承载这条动作的后端，**顺序 = 优先级**。语义见 `BACKEND_LABELS`：
    #: `onebot` = 任何 OneBot 实现都有的标准动作；`napcat` = 只有 NapCat 家族能做
    #: （含"用 NapCat 的 WebSocket 拿 cookie 再打 QZone CGI"这条路）。
    #:
    #: **为什么只剩两档还留着这套结构**（v1.7.10 复核）：它表达的是**现在真实存在**的
    #: 能力差别——换成 Lagrange / LLOneBot / go-cqhttp 这些 OneBot 实现，`get_cookies`
    #: 拿不到 cookie，靠它的 9 条动作（QQ 空间那一组 + `update_qq_status`）就做不了。
    #: 控制台据此打「NapCat 专属」徽章、给「只看 NapCat 专属」筛选（前端 `actions-view.ts`
    #: 与这里有对账用例），所以**不是**多后端脚手架的死代码。
    #: 目录**只声明正式通道**：上游 Koishi 那套扩展动作名在任何后端上都不存在，
    #: 已于 v1.7.10 从适配层删除（`_PLATFORM_CALLS` 里不再有任何"历史动作名"兜底）。
    backends: tuple[str, ...] = ('onebot',)

    @property
    def napcat_only(self) -> bool:
        """NapCat 专属：非 NapCat 后端无法使用。控制台据此打标、聚在一起。"""
        return bool(self.backends) and 'onebot' not in self.backends

    def param(self, name: str) -> Optional[ActionParam]:
        for item in self.params:
            if item.name == name:
                return item
        return None


#: 「改说说可见范围」的五档可见性（**用户指定的原话**，顺序也照它）。
#:
#: 这是模型 / 界面看到的枚举；对应的 `ugc_right` 整数值在
#: `core/qzone.py::QZONE_VISIBILITY_VALUES`（两表顺序与键逐字相同，有对账用例），
#: 数值本身的权威依据见 `core/qzone_cgi.py::QZONE_VISIBLE`。
QZONE_VISIBILITY_LABELS: tuple[str, ...] = (
    '所有人可见',
    '仅 QQ 好友可见',
    '部分人可见',
    '部分人不可见',
    '仅自己可见',
)

#: 「五档中文标签 → 打到腾讯的 `ugc_right` 整数」——**唯一数值表**在
#: `core/qzone.py::QZONE_VISIBILITY_VALUES`（权威依据见 `core/qzone_cgi.py::QZONE_VISIBLE`）。
#: 这里只在模块内派生两份**视图**，一处都不重抄：
#:
#: * `QZONE_VISIBILITY_CHOICE_VALUES`：规范枚举值 → 整数（`ugc_right` 的对外取值）；
#: * `QZONE_VISIBILITY_ALIASES`：旧的裸整数写法（`'1'`/`'4'`/…）→ 规范枚举值，
#:   给"模型手里还留着旧提示词记忆"留的兼容口（v1.7.6）。
QZONE_VISIBILITY_CHOICE_VALUES: dict[str, int] = dict(QZONE_VISIBILITY_VALUES)
QZONE_VISIBILITY_ALIASES: dict[str, str] = {
    str(value): label for label, value in QZONE_VISIBILITY_VALUES.items()
}

#: 动作类别 → 中文标签（控制台与提示词分组用）。
ACTION_CATEGORIES: dict[str, str] = {
    'interaction': '互动',
    'message': '消息与定时',
    'history': '历史消息',
    'status': 'QQ 状态',
    'group_read': '群信息（只读）',
    'group_write': '群管理（写入）',
    'profile': '个人资料',
    'voice': '语音',
    'contact': '联系人与群',
    'qzone': 'QQ 空间',
}


#: 动作开关的**父分组**（`_conf_schema.json` 的顶层组，v1.7.4 起只有一个）。
#:
#: 用户在配置页看到的是「机器人动作」一张卡片，里面三个子组；schema 里的真实形状是
#: `robot_actions.items.{chat,group,qzone}`。契约与 `model_center` 的嵌套段一致
#: （宿主配置页的 `AstrBotConfig` 对 `type: "object"` 的子项递归渲染，见坑 24/34）。
ACTION_CONFIG_SECTION = 'robot_actions'

#: 动作类别 → 配置分组（**schema 与运行期共用这一条映射**，避免"开关在哪"两处各写一遍）。
#:
#: **值是点分路径**（`robot_actions.chat`）：父组是「机器人动作」，三个子组是
#: 「会话动作 / 群管理动作 / QQ 空间动作」。v1.7.2 先把十个 `actions_*` 组收敛成三个顶层组、
#: v1.7.3 取消「风险操作」独立组、v1.7.4 再把这三个并进一个父组——三次都**只动分组名，
#: 不动键名**（键逐字 = 动作 id），旧分组由读取侧的 N:1 归并兜底
#: （`core/service/config.py` 的 `LEGACY_SECTION_MERGES`，键也是点分路径）。
#:
#: **危险动作没有单独的组**：它们按 `category` 落进三个子组之一（用户明确要求不把
#: 危险动作单独分成一类，见模块 docstring）。
ACTION_CONFIG_GROUPS: dict[str, str] = {
    'interaction': 'robot_actions.chat',
    'message': 'robot_actions.chat',
    'history': 'robot_actions.chat',
    'status': 'robot_actions.chat',
    'profile': 'robot_actions.chat',
    'voice': 'robot_actions.chat',
    'contact': 'robot_actions.chat',
    'group_read': 'robot_actions.group',
    'group_write': 'robot_actions.group',
    'qzone': 'robot_actions.qzone',
}

#: 配置子分组（点分路径）→ 组标题（控制台「动作」页用来说明"这个开关在哪一组"，
#: 与 schema 的 `title` 同源）。
#:
#: 为什么单列一张表而不是按类别推：多个类别并进同一组之后，"先到的类别定标签"会
#: 把 `robot_actions.chat` 标成「互动」，而那一组里还有消息/历史/状态/资料/语音/联系人。
#: 落点仍然只有一个来源（`ACTION_CONFIG_GROUPS`），`test_configuration.py` 断言两张表的
#: 键集合完全相等，改一处漏一处当场红。标题就是那三个**子组中文名**（配置页里显示的
#: 也是它们），不再带「动作：」前缀——父组已经叫「机器人动作」了。
ACTION_CONFIG_GROUP_LABELS: dict[str, str] = {
    'robot_actions.chat': '会话动作',
    'robot_actions.group': '群管理动作',
    'robot_actions.qzone': 'QQ 空间动作',
}

#: 落点路径的**根**去重集合（`{'robot_actions'}`）：控制台/配置页要按顶层组处理它们时用。
ACTION_CONFIG_GROUP_ROOTS: frozenset[str] = frozenset(
    group.split('.', 1)[0] for group in ACTION_CONFIG_GROUPS.values()
)


def action_config_group(action: 'PlatformAction') -> str:
    """某动作的开关落在哪个配置分组（**点分路径**；危险动作也回自己的类别组）。"""
    return ACTION_CONFIG_GROUPS.get(action.category, 'robot_actions.chat')


def _p(*args: Any, **kwargs: Any) -> ActionParam:
    return ActionParam(*args, **kwargs)


# --------------------------------------------------------------------------- #
# 目录本体（单一事实源）
# --------------------------------------------------------------------------- #

_ACTION_LIST: tuple[PlatformAction, ...] = (
    # ---------------- 互动 ----------------
    PlatformAction(
        'send_poke', 'interaction', '戳一戳',
        '戳一下对方（比发消息更轻的招呼）。',
        params=(
            _p('target', '目标', note='留空=本回合的对话对象'),
            _p('user_id', '用户号'),
            _p('group_id', '群号'),
        ),
        risk='safe',
        returns='ok / error',
    ),
    PlatformAction(
        'send_like', 'interaction', '点赞',
        '给对方的名片点赞。',
        params=(
            _p('user_id', '用户号', note='留空=本回合的对话对象'),
            _p('times', '次数', type='int', minimum=1, maximum=20),
        ),
        risk='safe',
        returns='次数',
    ),
    PlatformAction(
        'recall_message', 'interaction', '撤回消息',
        '撤回她刚发出去的一条消息（说错了、改口、反悔）。',
        params=(
            _p('target', '目标', choices=('last', 'entry', 'message'), note='默认 last=最近一条'),
            _p('entry_id', '条目编号', type='int'),
            _p('message_id', '平台消息号'),
        ),
        risk='sensitive',
        returns='被撤回的消息号',
    ),
    # ---------------- 消息与定时 ----------------
    PlatformAction(
        'schedule_message', 'message', '定时消息',
        '把一条消息排到将来某个时刻发出（例如明天早上说早安）。',
        params=(
            _p('content', '内容', required=True),
            _p('send_at', '发送时刻', note='ISO-8601；与 delay_minutes 二选一'),
            _p('delay_minutes', '延后分钟', type='int', minimum=1, maximum=43_200),
            _p('target', '目标', note='留空=本回合的对话对象'),
        ),
        risk='safe',
        returns='排期编号 + 发送时刻',
    ),
    PlatformAction(
        'list_scheduled_messages', 'message', '定时消息列表',
        '看还有哪些定时消息没发出去。',
        risk='safe',
    ),
    PlatformAction(
        'cancel_scheduled_message', 'message', '取消定时消息',
        '取消一条（或全部）还没发出的定时消息。',
        params=(
            _p('id', '排期编号', type='int'),
            _p('target', '目标', choices=('all',)),
        ),
        risk='safe',
    ),
    PlatformAction(
        'schedule_command', 'message', '定时命令',
        '按周期重复执行一个已登记的命令（例如每天凌晨整理记忆）。',
        params=(
            _p('command', '命令', required=True, note='见目录里登记的可排期命令'),
            _p('cron', '周期', note='5 段 cron：分 时 日 月 周，例如 30 8 * * *'),
            _p('params', '参数', type='object'),
        ),
        risk='safe',
        returns='排期编号 + 下次执行时刻',
    ),
    PlatformAction(
        'list_scheduled_commands', 'message', '定时命令列表',
        '看已登记的定时命令。',
        risk='safe',
    ),
    PlatformAction(
        'cancel_scheduled_command', 'message', '取消定时命令',
        '取消一条定时命令。',
        params=(_p('id', '排期编号', type='int', required=True),),
        risk='safe',
    ),
    # ---------------- 历史消息 ----------------
    PlatformAction(
        'get_group_msg_history', 'history', '拉群历史',
        '拉取某个群最近的聊天记录（她不在场时发生的事也能补上）。',
        params=(
            _p('group_id', '群号'),
            _p('count', '条数', type='int', minimum=1, maximum=50),
            _p('before', '早于该序号', type='int'),
        ),
        risk='sensitive',
        returns='消息列表（含发送者、时间、内容）',
        scopes=('group',),
    ),
    PlatformAction(
        'get_friend_msg_history', 'history', '拉私聊历史',
        '拉取与某个人的最近聊天记录。',
        params=(
            _p('user_id', '用户号'),
            _p('count', '条数', type='int', minimum=1, maximum=50),
            _p('before', '早于该序号', type='int'),
        ),
        risk='sensitive',
        returns='消息列表',
        scopes=('private',),
    ),
    # ---------------- QQ 状态 ----------------
    PlatformAction(
        'update_qq_status', 'status', '改 QQ 状态',
        '设置机器人账号的在线状态（在线/离开/忙碌/听歌中…）。',
        params=(
            _p('status', '状态', type='int', minimum=10, maximum=100,
               note='10 在线 / 30 离开 / 40 隐身 / 50 忙碌 / 60 请勿打扰 / 20 听歌中 等'),
            _p('minutes', '持续分钟', type='int', minimum=1, maximum=1_440,
               note='到点自动恢复离线状态；留空=不自动恢复'),
            _p('text', '自定义状态文本'),
        ),
        risk='sensitive',
        returns='生效状态',
        # `set_online_status` / `set_diy_online_status` 是 NapCat 的动作，标准 OneBot 没有。
        backends=('napcat',),
    ),
    PlatformAction(
        'get_qq_status', 'status', '查 QQ 状态', '看机器人账号当前的在线状态。',
        risk='safe',
    ),
    PlatformAction(
        'get_fun_status_list', 'status', '查可选状态', '列出可用的自定义状态（供改状态时挑）。',
        risk='safe',
    ),
    # ---------------- 群信息（只读） ----------------
    PlatformAction(
        'get_group_members_info', 'group_read', '群成员',
        '看群成员名单（含身份、名片、入群时间）。',
        params=(
            _p('group_id', '群号'),
            _p('limit', '条数', type='int', minimum=1, maximum=100),
        ),
        risk='safe',
        scopes=('group',),
    ),
    PlatformAction(
        'get_user_group_role', 'group_read', '查群身份',
        '查某人在群里的身份（群主/管理员/成员）。',
        params=(_p('user_id', '用户号'), _p('group_id', '群号')),
        risk='safe',
        scopes=('group',),
    ),
    PlatformAction(
        'get_group_honor_info', 'group_read', '群荣誉',
        '看群荣誉（龙王、群聊之火…）。',
        params=(_p('group_id', '群号'), _p('type', '类型')),
        risk='safe',
        scopes=('group',),
    ),
    PlatformAction(
        'get_group_shut_list', 'group_read', '禁言列表',
        '看群里正在被禁言的人。',
        params=(_p('group_id', '群号'),),
        risk='safe',
        scopes=('group',),
    ),
    PlatformAction(
        'get_group_notice_list', 'group_read', '群公告',
        '看群公告内容（她"知道"群里通知了什么）。',
        params=(_p('group_id', '群号'),),
        risk='safe',
        scopes=('group',),
    ),
    PlatformAction(
        'get_group_at_all_remain', 'group_read', '@全体剩余',
        '看本群 @全体成员 还剩几次。',
        params=(_p('group_id', '群号'),),
        risk='safe',
        scopes=('group',),
    ),
    PlatformAction(
        'list_group_files', 'group_read', '群文件列表', '看群文件有哪些。',
        params=(_p('group_id', '群号'),),
        risk='safe',
        scopes=('group',),
    ),
    # ---------------- 群管理（写入） ----------------
    PlatformAction(
        'send_group_notice', 'group_write', '发群公告',
        '以机器人身份发一条群公告。',
        params=(_p('content', '内容', required=True), _p('group_id', '群号'), _p('image', '配图')),
        risk='sensitive',
        scopes=('group',),
    ),
    PlatformAction(
        'delete_group_notice', 'group_write', '删群公告',
        '删掉一条群公告。',
        params=(_p('notice_id', '公告编号', required=True), _p('group_id', '群号')),
        risk='sensitive',
        scopes=('group',),
    ),
    PlatformAction(
        'set_essence_msg', 'group_write', '设精华',
        '把一条消息设为群精华。',
        params=(_p('message_id', '消息号', required=True),),
        risk='sensitive',
        scopes=('group',),
    ),
    PlatformAction(
        'delete_essence_msg', 'group_write', '撤精华',
        '取消一条消息的群精华。',
        params=(_p('message_id', '消息号', required=True),),
        risk='sensitive',
        scopes=('group',),
    ),
    PlatformAction(
        'send_group_sign', 'group_write', '群打卡', '在群里签到。',
        params=(_p('group_id', '群号'),),
        risk='safe',
        scopes=('group',),
    ),
    PlatformAction(
        'set_group_card', 'group_write', '改群名片',
        '改群昵称（默认改自己）。',
        params=(
            _p('user_id', '用户号', note='留空=改机器人自己'),
            _p('card', '名片', required=True),
            _p('group_id', '群号'),
        ),
        risk='sensitive',
        scopes=('group',),
    ),
    PlatformAction(
        'set_group_special_title', 'group_write', '设专属头衔',
        '给群成员设专属头衔。',
        params=(_p('user_id', '用户号', required=True), _p('title', '头衔', required=True)),
        risk='dangerous',
        default_permission='disabled',
        scopes=('group',),
    ),
    PlatformAction(
        'set_group_add_option', 'group_write', '改加群方式',
        '改加群验证方式（允许所有人/需审核/禁止）。',
        params=(_p('option', '方式', required=True, choices=('allow', 'audit', 'refuse')),),
        risk='dangerous',
        default_permission='disabled',
        scopes=('group',),
    ),
    PlatformAction(
        'set_group_portrait', 'group_write', '改群头像',
        '换群头像（需要一个图片文件或路径）。',
        params=(_p('file', '图片', required=True), _p('group_id', '群号')),
        risk='dangerous',
        default_permission='disabled',
        scopes=('group',),
    ),
    PlatformAction(
        'set_group_name', 'group_write', '改群名',
        '改群名称。',
        params=(_p('name', '群名', required=True), _p('group_id', '群号')),
        risk='dangerous',
        default_permission='disabled',
        scopes=('group',),
    ),
    PlatformAction(
        'set_group_ban', 'group_write', '禁言',
        '禁言某人（秒数；0 = 解除禁言）。',
        params=(
            _p('user_id', '用户号', required=True),
            _p('duration', '时长秒', type='int', minimum=0, maximum=2_592_000, required=True),
            _p('group_id', '群号'),
        ),
        risk='dangerous',
        default_permission='disabled',
        scopes=('group',),
    ),
    PlatformAction(
        'set_group_whole_ban', 'group_write', '全员禁言',
        '开/关全体禁言。',
        params=(_p('enable', '开启', type='bool', required=True), _p('group_id', '群号')),
        risk='dangerous',
        default_permission='disabled',
        scopes=('group',),
    ),
    PlatformAction(
        'set_group_kick', 'group_write', '踢人',
        '把某人踢出群（可选择不再接收其加群请求）。',
        params=(
            _p('user_id', '用户号', required=True),
            _p('reject_add', '拒绝再加群', type='bool'),
            _p('group_id', '群号'),
        ),
        risk='dangerous',
        default_permission='disabled',
        scopes=('group',),
    ),
    PlatformAction(
        'set_group_admin', 'group_write', '设管理',
        '设置/取消某人的群管理员身份。',
        params=(
            _p('user_id', '用户号', required=True),
            _p('enable', '设为管理', type='bool', required=True),
            _p('group_id', '群号'),
        ),
        risk='dangerous',
        default_permission='disabled',
        scopes=('group',),
    ),
    PlatformAction(
        'delete_group_file', 'group_write', '删群文件',
        '删除群文件。',
        params=(_p('file_id', '文件号', required=True), _p('group_id', '群号')),
        risk='dangerous',
        default_permission='disabled',
        scopes=('group',),
    ),
    PlatformAction(
        'upload_group_file', 'group_write', '传群文件',
        '把一个文件上传到群。',
        params=(
            _p('file', '文件路径', required=True),
            _p('name', '显示名'),
            _p('folder', '文件夹号'),
            _p('group_id', '群号'),
        ),
        risk='dangerous',
        default_permission='disabled',
        scopes=('group',),
    ),
    PlatformAction(
        'rename_group_file', 'group_write', '重命名群文件',
        '改群文件名。',
        params=(
            _p('file_id', '文件号', required=True),
            _p('name', '新名', required=True),
            _p('current_folder', '所在目录', note='留空 = 根目录'),
            _p('group_id', '群号'),
        ),
        risk='dangerous',
        default_permission='disabled',
        scopes=('group',),
    ),
    PlatformAction(
        'move_group_file', 'group_write', '移动群文件',
        '把群文件移到别的文件夹。',
        params=(
            _p('file_id', '文件号', required=True),
            _p('folder', '目标文件夹', required=True),
            _p('current_folder', '所在目录', note='留空 = 根目录'),
            _p('group_id', '群号'),
        ),
        risk='dangerous',
        default_permission='disabled',
        scopes=('group',),
    ),
    PlatformAction(
        'create_group_file_folder', 'group_write', '建群文件夹',
        '在群里新建文件夹。',
        params=(_p('name', '名称', required=True), _p('group_id', '群号')),
        risk='dangerous',
        default_permission='disabled',
        scopes=('group',),
    ),
    PlatformAction(
        'delete_group_folder', 'group_write', '删群文件夹',
        '删掉群文件夹（里面的文件会一起没）。',
        params=(_p('folder', '文件夹号', required=True), _p('group_id', '群号')),
        risk='dangerous',
        default_permission='disabled',
        scopes=('group',),
    ),
    PlatformAction(
        'trans_group_file', 'group_write', '转存群文件',
        '把群文件转存到别处。',
        params=(_p('file_id', '文件号', required=True), _p('group_id', '群号')),
        risk='dangerous',
        default_permission='disabled',
        scopes=('group',),
    ),
    # ---------------- 个人资料 ----------------
    PlatformAction(
        'set_qq_profile', 'profile', '改资料',
        '改机器人账号的昵称/个性签名。',
        params=(_p('nickname', '昵称'), _p('personal_note', '个性签名')),
        risk='sensitive',
    ),
    PlatformAction(
        'set_qq_avatar', 'profile', '换头像',
        '换机器人账号的头像。',
        params=(_p('file', '图片', required=True),),
        risk='sensitive',
    ),
    PlatformAction(
        'get_qq_profile', 'profile', '查自己资料', '看机器人账号当前的昵称与签名。',
        risk='safe',
    ),
    # ---------------- 语音 ----------------
    PlatformAction(
        'send_voice', 'voice', '发语音',
        '把一段话用语音发出去（走宿主 TTS）。',
        params=(
            _p('content', '文本', required=True),
            _p('voice', '音色', note='留空=用配置里的默认音色'),
            _p('target', '目标', note='留空=本回合的对话对象'),
        ),
        risk='safe',
        returns='ok / error',
    ),
    PlatformAction(
        'list_voices', 'voice', '可用音色', '列出宿主 TTS 能用的音色。',
        risk='safe',
    ),
    # ---------------- QQ 空间 ----------------
    PlatformAction(
        'publish_qzone_post', 'qzone', '发说说',
        '以她本人身份发一条 QQ 空间说说。',
        params=(
            _p('content', '正文', required=True),
            # v1.7.6：可见性**统一成与 `set_qzone_visibility.visible` 同一份枚举**
            # （同字符串、同顺序，都指向 `QZONE_VISIBILITY_LABELS`）——同一件事两种写法
            # 会把模型绕晕。旧提示词里的裸整数（1/4/16/64/128）仍然认（`aliases`），
            # 校验层按**同一张数值表**译成 `ugc_right`；wire 层照旧发数字。
            _p('ugc_right', '可见性', choices=QZONE_VISIBILITY_LABELS,
               aliases=QZONE_VISIBILITY_ALIASES,
               choice_values=QZONE_VISIBILITY_CHOICE_VALUES,
               note='默认「仅 QQ 好友可见」；旧的裸数字 1/4/16/64/128 也认'),
            _p('images', '配图', type='list', note='图片路径/URL/base64，最多 9 张（NapCat 原生支持）'),
            _p('target_uins', '可见性作用的 QQ', type='list',
               note='可见性为「部分人可见」/「部分人不可见」时必填'),
        ),
        risk='sensitive',
        returns='说说 tid + 可见性',
        # 空间动作统一走 NapCat WebSocket 方案（`get_cookies` + QZone CGI）。
        backends=('napcat',),
    ),
    PlatformAction(
        'comment_qzone_post', 'qzone', '评论说说',
        '评论一条空间说说（她自己的或好友的，需先由 `list_qzone_posts` / 动态感知拿到 tid）。',
        params=(
            _p('tid', '说说 tid', required=True),
            _p('content', '评论正文', required=True),
            _p('target_uin', '归属 QQ'),
        ),
        risk='sensitive',
        backends=('napcat',),
    ),
    PlatformAction(
        'like_qzone_post', 'qzone', '点赞说说',
        '给一条空间说说点赞。',
        params=(_p('tid', '说说 tid', required=True), _p('target_uin', '归属 QQ')),
        risk='sensitive',
        backends=('napcat',),
    ),
    PlatformAction(
        'list_qzone_posts', 'qzone', '看空间说说',
        '看自己或好友的空间说说列表（只读；好友动态也靠它对齐正文）。',
        params=(
            _p('target_uin', '归属 QQ', note='留空=她自己'),
            _p('count', '条数', type='int', minimum=1, maximum=50),
        ),
        risk='safe',
        backends=('napcat',),
    ),
    PlatformAction(
        'list_qzone_feeds', 'qzone', '看好友动态',
        '看好友动态信息流（只读）：她可以自己决定要不要了解别人最近在说什么。',
        params=(
            _p('page', '页码', type='int', minimum=1, maximum=20),
            _p('count', '条数', type='int', minimum=1, maximum=30),
        ),
        risk='safe',
        backends=('napcat',),
    ),
    PlatformAction(
        'forward_qzone_post', 'qzone', '转发说说',
        '转发一条说说（可带一句附言）。',
        params=(
            _p('tid', '说说 tid', required=True),
            _p('target_uin', '原作者 QQ'),
            _p('content', '转发附言'),
        ),
        risk='sensitive',
        backends=('napcat',),
    ),
    PlatformAction(
        'set_qzone_visibility', 'qzone', '改说说可见范围',
        '改一条**她自己发的**说说的可见范围（谁能看见）。带图 / 带视频的说说照常改'
        '（不会动她的图片与视频）；转发的改不了。',
        params=(
            _p('tid', '说说 tid', required=True),
            _p('visible', '可见范围', required=True, choices=QZONE_VISIBILITY_LABELS),
            _p('target_uins', '可见性作用的 QQ', type='list',
               note='visible 为「部分人可见」/「部分人不可见」时必填'),
        ),
        risk='sensitive',
        returns='说说 tid + 生效的可见范围',
        # 走 NapCat WebSocket 方案（`get_cookies` 后打 QZone CGI）：
        # `emotion_cgi_update` 两个平台都没有原生动作（见 `qzone_cgi` 模块 docstring）。
        backends=('napcat',),
    ),
    PlatformAction(
        'delete_qzone_post', 'qzone', '删说说',
        '删掉一条已发出的说说。**不可逆**。',
        params=(_p('tid', '说说 tid', required=True),),
        risk='dangerous',
        default_permission='disabled',
        backends=('napcat',),
    ),
    # ---------------- 联系人与群 ----------------
    PlatformAction(
        'list_contacts', 'contact', '联系人列表',
        '列出好友与群（她可以自己决定要了解谁）。',
        params=(
            _p('type', '类型', choices=('friends', 'groups', 'all')),
            _p('limit', '条数', type='int', minimum=1, maximum=200),
        ),
        risk='safe',
    ),
    PlatformAction(
        'search_contacts', 'contact', '搜联系人',
        '按关键词搜好友/群（昵称、备注、群名、群号）。',
        params=(_p('keyword', '关键词', required=True), _p('limit', '条数', type='int', minimum=1, maximum=50)),
        risk='safe',
    ),
    PlatformAction(
        'get_user_profile', 'contact', '看资料卡片',
        '看某个人的资料卡（昵称、签名、等级、性别等）。',
        params=(_p('user_id', '用户号', required=True),),
        risk='safe',
    ),
    PlatformAction(
        'get_group_info', 'contact', '看群资料',
        '看某个群的资料（群名、人数、简介、群主等）。',
        params=(_p('group_id', '群号', required=True),),
        risk='safe',
    ),
    PlatformAction(
        'handle_friend_request', 'contact', '处理加好友请求',
        '同意或拒绝**别人加机器人**的请求（不能主动加人）。',
        params=(
            _p('flag', '请求标记', required=True),
            _p('approve', '同意', type='bool', required=True),
            _p('remark', '备注'),
        ),
        risk='sensitive',
    ),
    PlatformAction(
        'handle_group_request', 'contact', '处理被拉群',
        '同意或拒绝**别人拉机器人进群**的邀请/申请（不能主动加群）。',
        params=(
            _p('flag', '请求标记', required=True),
            _p('approve', '同意', type='bool', required=True),
            _p('sub_type', '类型', choices=('add', 'invite')),
            _p('reason', '理由'),
        ),
        risk='sensitive',
    ),
    PlatformAction(
        'delete_friend', 'contact', '删好友',
        '删除好友（可拉黑）。**不可逆**。',
        params=(_p('user_id', '用户号', required=True), _p('block', '同时拉黑', type='bool')),
        risk='dangerous',
        default_permission='disabled',
    ),
)

#: 动作 id → 声明。刻意**不含**「主动加好友 / 主动加群」：
#: 需求明确要求只能被动同意，不做主动添加，所以目录里根本没有这两个动作。
ACTIONS: dict[str, PlatformAction] = {item.id: item for item in _ACTION_LIST}

if len(ACTIONS) != len(_ACTION_LIST):  # pragma: no cover - 目录写错时立刻炸
    raise RuntimeError('平台动作目录里存在重复 id')


#: 后端 → 人话标签（控制台与文档共用一处，别再各写一遍）。
#: **只有正式通道**：执行期的回退实现不进这张表，也就不可能被下发到面板上。
BACKEND_LABELS: dict[str, str] = {
    'onebot': '标准 OneBot',
    'napcat': 'NapCat 专属',
}


def napcat_actions() -> list[PlatformAction]:
    """NapCat 专属动作（`backends` 里没有 `onebot` 的那些），按 id 排序。

    控制台「只显示 NapCat 专属」与文档的"非 NapCat 后端无法使用"清单都用它。
    """
    return sorted((a for a in ACTIONS.values() if a.napcat_only), key=lambda a: a.id)


def backend_labels(action: PlatformAction) -> list[str]:
    """一条动作的后端标签（按优先级顺序，去重）。"""
    seen: list[str] = []
    for name in action.backends:
        label = BACKEND_LABELS.get(name, name)
        if label not in seen:
            seen.append(label)
    return seen


def actions_by_category() -> dict[str, list[PlatformAction]]:
    """按类别分组（保持目录声明顺序）。"""
    grouped: dict[str, list[PlatformAction]] = {}
    for item in _ACTION_LIST:
        grouped.setdefault(item.category, []).append(item)
    return grouped


#: 语音类动作 id（`category == 'voice'`）：`send_voice` / `list_voices`。
#: 它们多一道总闸——「模型中心 → 语音 / 音频理解设置」的 `tts_enabled`（v1.7.7）：
#: 关掉后这两个动作按既有的"动作开关关掉"路径不可用，与正文 `<tts/>` 标记被忽略
#: 是同一个开关的两种表现。从目录派生，别在别处再抄一份 id 清单。
VOICE_ACTION_IDS: frozenset[str] = frozenset(
    item.id for item in _ACTION_LIST if item.category == 'voice'
)


def risky_actions() -> list[PlatformAction]:
    """全部危险动作（给控制台警示区与文档的"危险动作"清单用）。

    它们的开关不再有专门的组，落在各自类别所属的那个子组里
    （`action_config_group()`），且默认 `false` + 默认档位 `disabled`。
    """
    return [item for item in _ACTION_LIST if item.risk == 'dangerous']


def action_risks() -> dict[str, str]:
    """动作 id → 风险级别。"""
    return {item.id: item.risk for item in _ACTION_LIST}


# --------------------------------------------------------------------------- #
# 权限：配置开关 ⊗ 独立权限表
# --------------------------------------------------------------------------- #

def normalize_permissions(value: Any) -> dict[str, str]:
    """把外部的权限表归一化：只认目录里的动作 id，只认四档。

    坏值（未知动作、未知档位、非字符串）**直接丢掉**——权限表是安全边界，
    宁可回到默认档位，也不能因为一条脏数据放行一个危险动作。
    """
    if not isinstance(value, Mapping):
        return {}
    normalized: dict[str, str] = {}
    for key, item in value.items():
        action = ACTIONS.get(str(key))
        if action is None:
            continue
        tier = str(item).strip().lower()
        if tier in PERMISSION_TIERS:
            normalized[action.id] = tier
    return normalized


def permission_for(action_id: str, table: Any = None) -> str:
    """某动作在权限表里的档位（缺省用目录声明的默认档）。"""
    action = ACTIONS.get(action_id)
    if action is None:
        return 'disabled'
    stored = normalize_permissions(table).get(action_id)
    return stored or action.default_permission


def effective_permission(action_id: str, table: Any = None, enabled: Any = None) -> str:
    """合出生效档位：**配置开关关掉 → 一律 disabled**（开关是总闸）。

    `enabled` 是配置里该动作所属功能的开关值（None = 不限制）。
    """
    if enabled is False:
        return 'disabled'
    return permission_for(action_id, table)


def is_action_enabled(action_id: str, table: Any = None, enabled: Any = None) -> bool:
    return effective_permission(action_id, table, enabled) != 'disabled'


def permission_tiers_for(action_id: str) -> tuple[str, ...]:
    """这条动作**实际适用**的权限档位（控制台下拉只列这些）。

    - `groupadmin`（仅群管）只对群聊动作有意义：非群聊动作不下发这一档，
      否则界面上会出现一个"选了等于关掉"的档位（用户直接指出过这个）。
    - `admin`（仅管理员）与 `global`（任何人）在任何会话都有意义，一律保留。
    """
    action = ACTIONS.get(str(action_id or '').strip())
    scopes = tuple(action.scopes) if action is not None else ('private', 'group')
    allowed = [tier for tier in PERMISSION_TIERS
               if tier != 'groupadmin' or 'group' in scopes]
    return tuple(allowed)


def normalize_tier_for(action_id: str, tier: Any) -> str:
    """把不适用的档位收敛成**这条动作的默认档位**。

    手改 `action_permissions.json` 塞了 `groupadmin`（而这是条私聊动作）时，
    不能让它静默变成"永远不能用"——那看起来像 bug。收敛成默认档 + 一条 warn。
    """
    value = str(tier or '').strip().lower()
    if value not in PERMISSION_TIERS:
        return 'disabled'
    if value in permission_tiers_for(action_id):
        return value
    return default_permission_for(action_id)


def default_permission_for(action_id: str) -> str:
    """这条动作在权限表里的默认档位（目录声明，危险动作默认关闭）。"""
    action = ACTIONS.get(str(action_id or '').strip())
    return action.default_permission if action is not None else 'global'


def resolve_permission(
    action_id: str,
    table: Any = None,
    enabled: Any = None,
    session_role: str = '',
) -> bool:
    """按档位判定当前会话能不能用这个动作。

    - `global`：任何会话都能用；
    - `groupadmin`：**仅群聊**且发言者是群主/管理员时能用（私聊回落管理员）；
    - `admin`：只有插件管理员能用（由适配层传入 `session_role='admin'`）；
    - `disabled`：永远不能用。
    """
    tier = normalize_tier_for(action_id, effective_permission(action_id, table, enabled))
    if tier == 'disabled':
        return False
    if tier == 'global':
        return True
    role = str(session_role or '').strip().lower()
    if tier == 'groupadmin':
        return role in ('owner', 'admin')
    if tier == 'admin':
        return role == 'admin'
    return False


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #

def _coerce(value: Any, kind: str) -> Any:
    """按声明做一次宽容转换；转不动就返回 `_BAD`。"""
    if kind == 'int':
        if isinstance(value, bool):
            return _BAD
        if isinstance(value, int):
            return value
        if isinstance(value, float) and float(value).is_integer():
            return int(value)
        if isinstance(value, str) and value.strip().lstrip('-').isdigit():
            return int(value.strip())
        return _BAD
    if kind == 'bool':
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ('true', 'false'):
            return value.strip().lower() == 'true'
        return _BAD
    if kind == 'object':
        return dict(value) if isinstance(value, Mapping) else _BAD
    if kind == 'list':
        return list(value) if isinstance(value, (list, tuple)) else _BAD
    return value if isinstance(value, str) else _BAD


_BAD = object()


def _choice_alias_key(value: Any) -> Optional[str]:
    """枚举参数的**旧写法**查表键；不是标量就返回 `None`（交给 `_coerce` 判类型错）。

    只做"数字 / 数字串 → 去空白的规范文本"这一件事：`4` / `'4'` / `' 4 '` / `4.0`
    都指向同一个键，`True` 不算（`bool` 是 `int` 的子类，别让它蒙到 `'1'`）。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if float(value).is_integer() else None
    if isinstance(value, str):
        return value.strip() or None
    return None


def _resolve_choice(param: ActionParam, value: Any) -> Any:
    """把枚举参数的输入收敛成 `choices` 里的一项；不是枚举参数就原样返回。

    顺序：**先查 `aliases`（旧写法）**再查 `choices`。旧写法命中就换成规范值继续走；
    数字写法既不在 `aliases` 也不在 `choices` 里时直接报**可选值清单**——比
    "类型不对（应为 string）" 有用得多（模型写的 8 是"档位不存在"，不是"类型错"）。
    """
    if not param.choices:
        return value
    if isinstance(value, str) and value in param.choices:
        return value
    if param.aliases:
        key = _choice_alias_key(value)
        if key is not None:
            mapped = param.aliases.get(key)
            if mapped is not None:
                return mapped
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return _CHOICE_REJECTED
    return value


#: `_resolve_choice` 的"这个写法不存在"哨兵（避免在这里拼错误文案，调用方统一报）。
_CHOICE_REJECTED = object()


def validate_action(
    action_id: Any,
    params: Any = None,
    available: Optional[Iterable[str]] = None,
) -> tuple[Optional[dict[str, Any]], str]:
    """校验一个动作 + 参数，返回 `(归一化后的动作, 错误原因)`。

    成功时第一个元素形如 `{'action': 'send_like', 'params': {'user_id': '…', 'times': 3}}`；
    失败时第一个元素是 `None`，第二个是**给日志看的中文原因**（模型看不到它，
    但运维能从 warn 里定位是模型乱写还是目录没开）。
    """
    name = str(action_id or '').strip()
    action = ACTIONS.get(name)
    if action is None:
        return None, '未知动作：%s' % (name or '(空)')
    if available is not None and name not in set(available):
        return None, '动作未启用或当前会话不允许：%s' % name

    raw = params if isinstance(params, Mapping) else {}
    # 别名容错：模型偶尔会写 camelCase（`userId`），双读一次不算放水。
    def read(param: ActionParam) -> Any:
        if param.name in raw:
            return raw[param.name]
        camel = _camel(param.name)
        return raw.get(camel)

    normalized: dict[str, Any] = {}
    for param in action.params:
        value = read(param)
        if value is None or value == '':
            if param.required:
                return None, '动作 %s 缺少必填参数 %s' % (name, param.name)
            continue
        if param.choices:
            value = _resolve_choice(param, value)
            if value is _CHOICE_REJECTED:
                return None, '动作 %s 的参数 %s 只能是 %s' % (
                    name, param.name, '|'.join(param.choices),
                )
        coerced = _coerce(value, param.type)
        if coerced is _BAD:
            return None, '动作 %s 的参数 %s 类型不对（应为 %s）' % (name, param.name, param.type)
        if param.choices and str(coerced) not in param.choices:
            return None, '动作 %s 的参数 %s 只能是 %s' % (name, param.name, '|'.join(param.choices))
        if param.type == 'int':
            if param.minimum is not None and coerced < param.minimum:
                return None, '动作 %s 的参数 %s 小于下限 %g' % (name, param.name, param.minimum)
            if param.maximum is not None and coerced > param.maximum:
                return None, '动作 %s 的参数 %s 超过上限 %g' % (name, param.name, param.maximum)
        if param.type == 'string' and len(str(coerced)) > 2000:
            return None, '动作 %s 的参数 %s 过长' % (name, param.name)
        # 枚举的"对外取值"映射放在最后：模型/界面看到的是标签，wire 与平台拿到的是
        # 目录声明的那份值（例如 `ugc_right` 的 1/4/16/64/128）。
        if param.choice_values and coerced in param.choice_values:
            coerced = param.choice_values[coerced]
        normalized[param.name] = coerced
    return {'action': name, 'params': normalized}, ''


def validate_actions(
    value: Any,
    available: Optional[Iterable[str]] = None,
    limit: int = 8,
) -> tuple[list[dict[str, Any]], list[str]]:
    """校验一批动作：返回 `(通过的动作, 拒绝原因)`。

    上限 `limit` 是防滥用：一次回合塞几十个动作既不符合叙事，也会刷屏。
    超出上限的部分**按拒绝**计入原因（不静默丢弃）。
    """
    if not isinstance(value, list):
        return [], []
    accepted: list[dict[str, Any]] = []
    rejected: list[str] = []
    for item in value:
        if len(accepted) >= limit:
            rejected.append('本轮动作超过上限 %d，已忽略多余部分' % limit)
            break
        if not isinstance(item, Mapping):
            rejected.append('动作条目不是对象')
            continue
        action_id = item.get('action', item.get('actionId', item.get('id')))
        params = item.get('params', item.get('parameters', item.get('args')))
        normalized, reason = validate_action(action_id, params, available)
        if normalized is None:
            rejected.append(reason)
            continue
        accepted.append(normalized)
    return accepted, rejected


def _camel(name: str) -> str:
    head, *rest = name.split('_')
    return head + ''.join(part.title() for part in rest)


# --------------------------------------------------------------------------- #
# 提示词注入
# --------------------------------------------------------------------------- #

def describe_actions(
    available: Optional[Iterable[str]] = None,
    *,
    scopes: Optional[Iterable[str]] = None,
    max_actions: int = 200,
) -> str:
    """把**当前可用**的动作渲染成一段紧凑目录（进提示词）。

    `available` 为空（None）时表示"不限制"，用于控制台预览；正常调用一定传实际可用集合，
    否则模型会看到一堆其实调不动的动作。
    """
    allowed = None if available is None else set(available)
    scope_set = set(scopes) if scopes else None
    lines: list[str] = []
    for category, items in actions_by_category().items():
        chosen = [
            item for item in items
            if (allowed is None or item.id in allowed)
            and (scope_set is None or scope_set & set(item.scopes))
        ][:max_actions]
        if not chosen:
            continue
        lines.append('%s：' % ACTION_CATEGORIES.get(category, category))
        for item in chosen:
            params = '，'.join(param.describe() for param in item.params)
            suffix = ('（%s）' % params) if params else ''
            lines.append('- %s：%s%s' % (item.id, item.summary, suffix))
    return '\n'.join(lines)


def describe_action_shortlist(available: Iterable[str]) -> str:
    """**第一段**（每回合都注入）：只列"屏幕上有哪些按钮"。

    形状：`类别：` + 每行 `- <id> <短标签>`。**不含任何参数说明和参数枚举**——
    参数是第二段的事（`describe_action_params`）。用户点名的两段式就是这一句：
    "先让她知道屏幕上有哪些按钮，她选定某个按钮之后，再告诉她这个界面怎么填"。

    `available` **必填**（空集合 → 空串）：可用集是"配置开关 ⊗ 权限表 ⊗ 会话身份"的
    合成结果，判据在 `chunk12.available_platform_actions()` **一处**；这里只做渲染，
    绝不再判一次权限（两处判据必然分叉）。传 `None`（"不限制"）**刻意不认**——
    全量参数目录回归正是这一层要防的（token 回归）。
    """
    allowed = {str(item) for item in (available or ())}
    if not allowed:
        return ''
    lines: list[str] = []
    for category, items in actions_by_category().items():
        chosen = [item for item in items if item.id in allowed]
        if not chosen:
            continue
        lines.append('%s：' % ACTION_CATEGORIES.get(category, category))
        for item in chosen:
            lines.append('- %s %s' % (item.id, item.label))
    return '\n'.join(lines)


def action_param_example(action: 'PlatformAction') -> dict[str, Any]:
    """这条动作的一个**示例参数对象**（第二段的示教用）。

    只补必填参数：拿模型自己填得出来的值当示例才有意义，把可选项也塞满反而教它
    "每个参数都要写"。枚举取第一项（那是目录里声明的合法值）。
    """
    params: dict[str, Any] = {}
    for param in action.params:
        if not param.required:
            continue
        if param.choices:
            params[param.name] = param.choices[0]
        elif param.type == 'int':
            params[param.name] = int(param.minimum) if param.minimum is not None else 1
        elif param.type == 'bool':
            params[param.name] = True
        elif param.type == 'list':
            params[param.name] = []
        elif param.type == 'object':
            params[param.name] = {}
        else:
            params[param.name] = '<%s>' % param.name
    return params


def describe_action_params(available: Iterable[str]) -> str:
    """**第二段**（只有她选定某个动作时才给）：这些动作"这个界面怎么填"。

    每条给：一句话说明 + 参数表（`name:type [范围] (枚举) 必填` + 标签/说明）+
    一个**可照抄的示例**。复杂动作（发图文说说这类）最吃这一段。

    与第一段同一条纪律：`available` 就是这一回合的可用集（判据仍只有一处），
    这里只按它筛选 + 渲染，**不新增可调动作、也不放宽任何参数约束**——
    模型照第二段填出来的参数仍要过 `validate_action()`（那是唯一的校验口）。
    """
    allowed = {str(item) for item in (available or ())}
    if not allowed:
        return ''
    lines: list[str] = []
    for item in _ACTION_LIST:
        if item.id not in allowed:
            continue
        lines.append('%s（%s）：%s' % (item.id, item.label, item.summary))
        if item.params:
            for param in item.params:
                detail = param.describe()
                if param.label and param.label != param.name:
                    detail += ' %s' % param.label
                if param.note:
                    detail += '（%s）' % param.note
                lines.append('- %s' % detail)
        else:
            lines.append('- 无参数')
        example = action_param_example(item)
        lines.append('- 示例：{"action":"%s","params":%s}' % (
            item.id, json.dumps(example, ensure_ascii=False),
        ))
    return '\n'.join(lines)
