"""平台动作目录（本移植版新增）：模型能对 QQ 做的**全部**动作的单一事实源。

为什么要有这一层：动作会越来越多（戳一戳、点赞、撤回、群管理、定时、资料、联系人…
四十多个），如果每个动作各自散落在「提示词写一段 + chunk4 解析一处 + 校验一处 +
适配层实现一处」，加一个动作要动四个文件、漏一处就是静默失效。这里把每个动作声明成
一条 `PlatformAction`，由它**驱动**四件事：

1. **提示词注入**：`describe_actions()` 只列**当前启用**的动作（按类别分组、带参数），
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

`dangerous` 的默认档位一律是 `disabled`，且它们的开关**集中在 `action_risks` 组**，
该组描述就是那句警告：`以下功能包含风险操作不建议开启`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence

__all__ = [
    'ACTIONS',
    'ACTION_CATEGORIES',
    'ACTION_CONFIG_GROUPS',
    'ACTION_RISK_GROUP',
    'action_config_group',
    'PERMISSION_TIERS',
    'PLATFORM_ACTION_FIELD',
    'RISK_LEVELS',
    'RISK_WARNING',
    'PlatformAction',
    'action_risks',
    'actions_by_category',
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

#: 风险组开关的说明文案（用户指定的原句，逐字）。
RISK_WARNING = '以下功能包含风险操作不建议开启'


class ActionParam:
    """一个动作参数的声明（校验与提示词都读它）。"""

    __slots__ = ('name', 'label', 'type', 'required', 'minimum', 'maximum', 'choices', 'note')

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
    ) -> None:
        self.name = name
        self.label = label or name
        self.type = type
        self.required = required
        self.minimum = minimum
        self.maximum = maximum
        self.choices = tuple(choices)
        self.note = note

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

    def param(self, name: str) -> Optional[ActionParam]:
        for item in self.params:
            if item.name == name:
                return item
        return None


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


#: 动作类别 → 配置分组（**schema 与运行期共用这一条映射**，避免"开关在哪"两处各写一遍）。
#: 危险动作不在这里：它们的开关集中在 `ACTION_RISK_GROUP`。
ACTION_CONFIG_GROUPS: dict[str, str] = {
    'interaction': 'actions_interaction',
    'message': 'actions_message',
    'history': 'actions_history',
    'status': 'actions_status',
    'group_read': 'actions_group',
    'group_write': 'actions_group',
    'profile': 'actions_profile',
    'voice': 'actions_voice',
    'contact': 'actions_contact',
    'qzone': 'actions_qzone',
}

#: 危险动作的开关组（该组描述就是 `RISK_WARNING`）。
ACTION_RISK_GROUP = 'actions_risks'


def action_config_group(action: 'PlatformAction') -> str:
    """某动作的开关落在哪个配置分组（危险动作一律进风险组）。"""
    return ACTION_RISK_GROUP if action.risk == 'dangerous' else ACTION_CONFIG_GROUPS.get(
        action.category, 'actions_interaction',
    )


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
    ),
    PlatformAction(
        'get_user_group_role', 'group_read', '查群身份',
        '查某人在群里的身份（群主/管理员/成员）。',
        params=(_p('user_id', '用户号'), _p('group_id', '群号')),
        risk='safe',
    ),
    PlatformAction(
        'get_group_honor_info', 'group_read', '群荣誉',
        '看群荣誉（龙王、群聊之火…）。',
        params=(_p('group_id', '群号'), _p('type', '类型')),
        risk='safe',
    ),
    PlatformAction(
        'get_group_shut_list', 'group_read', '禁言列表',
        '看群里正在被禁言的人。',
        params=(_p('group_id', '群号'),),
        risk='safe',
    ),
    PlatformAction(
        'get_group_notice_list', 'group_read', '群公告',
        '看群公告内容（她"知道"群里通知了什么）。',
        params=(_p('group_id', '群号'),),
        risk='safe',
    ),
    PlatformAction(
        'get_group_at_all_remain', 'group_read', '@全体剩余',
        '看本群 @全体成员 还剩几次。',
        params=(_p('group_id', '群号'),),
        risk='safe',
    ),
    PlatformAction(
        'list_group_files', 'group_read', '群文件列表', '看群文件有哪些。',
        params=(_p('group_id', '群号'),),
        risk='safe',
    ),
    # ---------------- 群管理（写入） ----------------
    PlatformAction(
        'send_group_notice', 'group_write', '发群公告',
        '以机器人身份发一条群公告。',
        params=(_p('content', '内容', required=True), _p('group_id', '群号'), _p('image', '配图')),
        risk='sensitive',
    ),
    PlatformAction(
        'delete_group_notice', 'group_write', '删群公告',
        '删掉一条群公告。',
        params=(_p('notice_id', '公告编号', required=True), _p('group_id', '群号')),
        risk='sensitive',
    ),
    PlatformAction(
        'set_essence_msg', 'group_write', '设精华',
        '把一条消息设为群精华。',
        params=(_p('message_id', '消息号', required=True),),
        risk='sensitive',
    ),
    PlatformAction(
        'delete_essence_msg', 'group_write', '撤精华',
        '取消一条消息的群精华。',
        params=(_p('message_id', '消息号', required=True),),
        risk='sensitive',
    ),
    PlatformAction(
        'send_group_sign', 'group_write', '群打卡', '在群里签到。',
        params=(_p('group_id', '群号'),),
        risk='safe',
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
    ),
    PlatformAction(
        'set_group_special_title', 'group_write', '设专属头衔',
        '给群成员设专属头衔。',
        params=(_p('user_id', '用户号', required=True), _p('title', '头衔', required=True)),
        risk='dangerous',
        default_permission='disabled',
    ),
    PlatformAction(
        'set_group_add_option', 'group_write', '改加群方式',
        '改加群验证方式（允许所有人/需审核/禁止）。',
        params=(_p('option', '方式', required=True, choices=('allow', 'audit', 'refuse')),),
        risk='dangerous',
        default_permission='disabled',
    ),
    PlatformAction(
        'set_group_portrait', 'group_write', '改群头像',
        '换群头像（需要一个图片文件或路径）。',
        params=(_p('file', '图片', required=True), _p('group_id', '群号')),
        risk='dangerous',
        default_permission='disabled',
    ),
    PlatformAction(
        'set_group_name', 'group_write', '改群名',
        '改群名称。',
        params=(_p('name', '群名', required=True), _p('group_id', '群号')),
        risk='dangerous',
        default_permission='disabled',
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
    ),
    PlatformAction(
        'set_group_whole_ban', 'group_write', '全员禁言',
        '开/关全体禁言。',
        params=(_p('enable', '开启', type='bool', required=True), _p('group_id', '群号')),
        risk='dangerous',
        default_permission='disabled',
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
    ),
    PlatformAction(
        'delete_group_file', 'group_write', '删群文件',
        '删除群文件。',
        params=(_p('file_id', '文件号', required=True), _p('group_id', '群号')),
        risk='dangerous',
        default_permission='disabled',
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
    ),
    PlatformAction(
        'rename_group_file', 'group_write', '重命名群文件',
        '改群文件名。',
        params=(
            _p('file_id', '文件号', required=True),
            _p('name', '新名', required=True),
            _p('group_id', '群号'),
        ),
        risk='dangerous',
        default_permission='disabled',
    ),
    PlatformAction(
        'move_group_file', 'group_write', '移动群文件',
        '把群文件移到别的文件夹。',
        params=(
            _p('file_id', '文件号', required=True),
            _p('folder', '文件夹号', required=True),
            _p('group_id', '群号'),
        ),
        risk='dangerous',
        default_permission='disabled',
    ),
    PlatformAction(
        'create_group_file_folder', 'group_write', '建群文件夹',
        '在群里新建文件夹。',
        params=(_p('name', '名称', required=True), _p('group_id', '群号')),
        risk='dangerous',
        default_permission='disabled',
    ),
    PlatformAction(
        'delete_group_folder', 'group_write', '删群文件夹',
        '删掉群文件夹（里面的文件会一起没）。',
        params=(_p('folder', '文件夹号', required=True), _p('group_id', '群号')),
        risk='dangerous',
        default_permission='disabled',
    ),
    PlatformAction(
        'trans_group_file', 'group_write', '转存群文件',
        '把群文件转存到别处。',
        params=(_p('file_id', '文件号', required=True), _p('group_id', '群号')),
        risk='dangerous',
        default_permission='disabled',
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
            _p('ugc_right', '可见性', type='int', minimum=1, maximum=128,
               note='1 所有人 / 4 好友（默认）/ 16 部分好友 / 64 仅自己 / 128 部分不可见'),
            _p('images', '配图', type='list', note='图片路径或地址，最多 9 张'),
        ),
        risk='sensitive',
        returns='说说 tid + 可见性',
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
    ),
    PlatformAction(
        'like_qzone_post', 'qzone', '点赞说说',
        '给一条空间说说点赞。',
        params=(_p('tid', '说说 tid', required=True), _p('target_uin', '归属 QQ')),
        risk='sensitive',
    ),
    PlatformAction(
        'list_qzone_posts', 'qzone', '看空间说说',
        '看自己或好友的空间说说列表（只读；好友动态也靠它对齐正文）。',
        params=(
            _p('target_uin', '归属 QQ', note='留空=她自己'),
            _p('count', '条数', type='int', minimum=1, maximum=50),
        ),
        risk='safe',
    ),
    PlatformAction(
        'delete_qzone_post', 'qzone', '删说说',
        '删掉一条已发出的说说。**不可逆**。',
        params=(_p('tid', '说说 tid', required=True),),
        risk='dangerous',
        default_permission='disabled',
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


def actions_by_category() -> dict[str, list[PlatformAction]]:
    """按类别分组（保持目录声明顺序）。"""
    grouped: dict[str, list[PlatformAction]] = {}
    for item in _ACTION_LIST:
        grouped.setdefault(item.category, []).append(item)
    return grouped


def risky_actions() -> list[PlatformAction]:
    """全部危险动作（给 `action_risks` 配置组与控制台警示区用）。"""
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
    tier = effective_permission(action_id, table, enabled)
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
