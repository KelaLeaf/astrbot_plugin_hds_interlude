"""插件控制台（WebUI 插件页面）的后端。

**为什么单独一个模块**：`main.py` 只负责注册路由与转发，真正的取数逻辑放这里，
这样命令表那边（32 条）与页面这边互不干扰，测试也能直接对着数据形状写。

放在 `adapters/` 而不是 `core/`：它要读 AstrBot 的 Provider、要拿 `Bridge` 的数据
目录，属于宿主侧代码；`core/` 依旧一行 astrbot 都不 import。

设计约束：

* 只读。除了配置导入导出（复用 `Bridge.import_config`），这里不改任何状态。
* 每个接口都要能在**服务没起来 / 数据库没建表 / 没配模型**时正常返回一个空壳，
  而不是抛异常——控制台要能打开，并在界面上说明"哪里还没配好"。
* 不返回任何密钥。连接行的 `api_key` 只以「有没有填」的形式出现。
"""

from __future__ import annotations

import inspect
import json
import os
from datetime import datetime
from typing import Any, Optional

from ..core import platform_actions
from ..core.database import TABLES
from ..core.meta import HDS_INTERLUDE_VERSION
#: N:1 旧分组归并（配置页显示的当前值必须与运行期读到的一致，见 `config_schema`）。
from ..core.service.config import merge_legacy_section_values
from ..core.token_stats import normalize_range, range_bounds, summarize_usage
from ..core.story_state import decode_story_state
# 作品正文 / 创作意图 / 修改理由的上限与分段长度：**单一事实源在 `core/works.py`**，
# 控制台不另抄一套数字（上限漂移过一次就等于前端与 core 各判一次）。
from ..core.works import (
    BRIEF_MAX as WORK_BRIEF_MAX,
    CONTENT_MAX as WORK_CONTENT_MAX,
    REASON_MAX as WORK_REASON_MAX,
    TITLE_MAX as WORK_TITLE_MAX,
    split_dump_parts,
)
from .astrbot_bridge import (
    CONSOLE_LOG_BUFFER as CONSOLE_LOG_MAX,
    CONSOLE_USAGE_BUFFER as CONSOLE_USAGE_MAX,
    NESTED_MODEL_SECTIONS,
    PLUGIN_NAME,
    _plugin_version,
)

__all__ = ['ConsoleApi', 'ConsoleError', 'CONSOLE_TASKS', 'CONTEXT_SECTION_LABELS', 'INTERNAL_INTENT_TYPES', 'mask_endpoint',
           'load_config_schema', 'coerce_schema_value']

#: 控制台「模型」页展示的任务顺序与中文名（与 `model_routing` 的任务键一致）。
CONSOLE_TASKS: tuple[tuple[str, str], ...] = (
    ('main', '主叙事'),
    ('compaction', '压缩与总结'),
    ('timeline', '时间导演'),
    ('alter', 'Alter 分析'),
    ('embedding', 'Embedding'),
    ('stickers', '表情包描述'),
    ('vision', '侧端识图'),
)

#: 纯宿主调度的 intent 类型：不是"她答应了什么"，用户看它只会困惑。
#: - `split-message`：拆分气泡的投递节拍（"她还在打字"），投递完就 completed；
#: - `narrative-retry`：叙事调用失败后的自动重试排程。
#: 控制台的「承诺与意图」默认只显示人话层面的意图，这些折叠起来（可展开）。
#: 上下文段名的中文标签（`helpers.CONTEXT_METRIC_SECTIONS` 的 wire 键）。
CONTEXT_SECTION_LABELS = {
    'recentEntries': '近期条目',
    'recalledHistory': '召回的历史原文',
    'memories': '压缩记忆',
    'facts': '长期事实',
    'overlaySnapshots': '设定演化',
    'followUpCommitments': '承诺回访',
    'dueIntents': '到期计划',
    'upcomingIntents': '未来计划',
    'activeConsequences': '剧情余波',
    'workingDetails': '临时细节',
    'participants': '参与者摘要',
    'webContext': '网页观察',
    'quotedMessages': '被回复的消息',
    'automaticDeliverySummaries': '自动投递摘要',
}

INTERNAL_INTENT_TYPES: frozenset[str] = frozenset({'split-message', 'narrative-retry'})

#: 共同作品（`interlude_work` 表，上游 rc28 `works.ts`；入口由本移植版补）的取数下限。
#: 服务层没就绪时控制台回这个空壳，**不抛**（§29：一个没接线的功能不该让整页打不开）。
WORKS_UNAVAILABLE_HINT = '共同作品尚未启用或服务层未就绪'
#: 一件作品最多取多少行（一个参与者一行；正常只有个位数，卡上限是防脏库）。
WORK_ROW_LIMIT = 200
#: revision 预览长度：详情里只有 head 给全文，历史版本给长度 + 预览（64 × 8000 字
#: 全量塞进一次响应会把面板拖垮，而界面主要看的是"谁在什么时候改了什么"）。
WORK_REVISION_PREVIEW = 200


# ===================================================================== #
# 配置 schema：控制台配置页的取数与写入依据
#
# 为什么要有这一层：AstrBot 自带的配置页对「列表」只提供字符串数组控件
# （`ListConfigItem`），`items` 里的行内字段定义**完全不生效**——对象行
# （`user_accounts` / `group_chats` / `providers` …）在那儿编辑会被压成字符串。
# 所以控制台自己按 `_conf_schema.json` 渲染表单，并且**只接受 schema 里声明过的
# 路径**：这是"白名单"从"手写 10 个开关"升级为"整个 schema"的关键——
# 控制台可以改配置，但改不动 schema 之外的东西。
# ===================================================================== #

#: 插件包根下的配置 schema（本文件在 `adapters/` 里）。
CONFIG_SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '_conf_schema.json',
)

#: 标量列表的元素规格键：带这些键的 `items` 是「元素规格」而不是「行内字段映射」。
_SCALAR_ITEM_KEYS = frozenset((
    'type', 'options', 'default', 'description', 'hint', 'slider', 'render_type',
    'editor_mode', 'editor_language', 'editor_theme', '_special', 'invisible',
))

#: 有专用面板 / 需要额外语义的字段：控制台配置页只显示说明，不给通用控件。
DELEGATED_FIELDS: dict[str, str] = {
    'model_center.providers': '模型连接池请在「模型」面板里编辑：那里不回显密钥，还能看用量与任务路由。',
}

#: 行为提醒（不是宿主的锅，是配置本身的坑）：路径 → 提示。
FIELD_NOTES: dict[str, str] = {
    'qq_access.user_accounts': '名单里的行决定她怎么称呼你、以及你的背景与初始关系'
                              '（label 留空时用平台昵称；profile / relationship 留空时回落到「故事档案」的默认值）。'
                              '只作用于私聊；群聊成员不需要在这里。',
    'qq_access.bot_accounts': '只决定"哪个登录账号收消息"，label 不进模型。',
    'qq_access.group_chats': '这一行决定那个群的触发方式（mention-only 只回 @ / always）与节奏，'
                             '以及群用途与角色定位。',
    'story_defaults.style': '故事级文风，接在「提示词」的全局文风之后；两级都生效。',
    'story_defaults.persona_id': '选中 AstrBot 人格会用它覆盖角色名与角色设定（留空则用下面的手填项）。',
    'prompts.style_prompt': '全局默认文风；故事档案里的「故事文风」可以在它之后再补一层。',
    'runtime.auto_create': '开启后第一次私聊会自动建故事；白名单仍优先决定谁能进来。',
}

#: 宿主配置页编辑不了「对象行列表」的提示（与 `_conf_schema.json` 里的 hint 同一句话）。
#: 控制台**不**把这句铺在字段上（用户要求别在页面上重复解释），它留在数据里给将来的界面用。
HOST_LIST_DEGRADED_NOTE = '⚠️此配置项不生效，请在「幕间控制台 → 配置」处进行配置'


_SCHEMA_CACHE: dict[str, Any] = {'mtime': None, 'schema': {}}


def load_config_schema() -> dict[str, Any]:
    """读 `plugin/_conf_schema.json`（按 mtime 缓存，改完不用重启）。"""
    try:
        mtime = os.path.getmtime(CONFIG_SCHEMA_PATH)
    except OSError:
        return {}
    if _SCHEMA_CACHE['mtime'] == mtime and _SCHEMA_CACHE['schema']:
        return _SCHEMA_CACHE['schema']
    try:
        with open(CONFIG_SCHEMA_PATH, encoding='utf-8-sig') as handle:
            schema = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(schema, dict):
        return {}
    _SCHEMA_CACHE['mtime'] = mtime
    _SCHEMA_CACHE['schema'] = schema
    return schema


def schema_row_fields(spec: Any) -> Optional[dict[str, Any]]:
    """列表字段是不是「对象行」（`items` 是字段映射）？是就返回行 schema。

    `{"type": "list", "items": {"type": "string"}}` 是**标量**列表（元素规格）；
    `{"type": "list", "items": {"qq": {...}, "label": {...}}}` 才是对象行
    —— 后者正是宿主配置页编不了的那一类。
    """
    if not isinstance(spec, dict) or spec.get('type') != 'list':
        return None
    items = spec.get('items')
    if not isinstance(items, dict) or not items:
        return None
    row = {key: value for key, value in items.items() if key not in _SCALAR_ITEM_KEYS}
    if not row:
        return None
    if all(isinstance(value, dict) and 'type' in value for value in row.values()):
        return row
    return None


def host_editor_note(path: str, spec: Any) -> Optional[dict[str, str]]:
    """这个字段在**宿主**配置页里能不能编辑好？返回 `{level, text}` 或 None。

    `level`：`warn` = 宿主控件会写坏这个字段；`info` = 有专用入口或行为提醒。
    """
    if path in DELEGATED_FIELDS:
        return {'level': 'info', 'text': DELEGATED_FIELDS[path]}
    if schema_row_fields(spec) is not None:
        return {'level': 'warn', 'text': HOST_LIST_DEGRADED_NOTE}
    note = FIELD_NOTES.get(path)
    if note:
        return {'level': 'info', 'text': note}
    return None


def _coerce_scalar(kind: str, value: Any, path: str) -> Any:
    if value is None:
        return None
    if kind == 'bool':
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            text = value.strip().lower()
            if text in ('true', '1', 'yes', 'on'):
                return True
            if text in ('false', '0', 'no', 'off', ''):
                return False
        if isinstance(value, (int, float)):
            return bool(value)
        raise ConsoleError('「%s」需要 true / false' % path)
    if kind == 'int':
        try:
            return int(float(value))
        except (TypeError, ValueError):
            raise ConsoleError('「%s」需要整数' % path) from None
    if kind == 'float':
        try:
            return float(value)
        except (TypeError, ValueError):
            raise ConsoleError('「%s」需要数字' % path) from None
    if kind in ('string', 'text', 'file'):
        if isinstance(value, (dict, list)):
            raise ConsoleError('「%s」需要文本' % path)
        return str(value)
    if kind == 'list':
        if not isinstance(value, list):
            raise ConsoleError('「%s」需要列表' % path)
        return value
    if kind in ('object', 'dict'):
        if not isinstance(value, dict):
            raise ConsoleError('「%s」需要对象' % path)
        return value
    return value


def coerce_schema_value(spec: Any, value: Any, path: str = '') -> Any:
    """把前端传来的值按 schema 声明的类型收一遍；类型不对就报可读的错。

    对象行列表会逐行按行 schema 收字段，**行里 schema 之外的键原样保留**
    （用户手写的扩展位不该被控制台吃掉）。
    """
    if not isinstance(spec, dict):
        return value
    kind = str(spec.get('type') or '')
    row = schema_row_fields(spec)
    if row is None:
        return _coerce_scalar(kind, value, path)
    if value is None:
        return None
    if not isinstance(value, list):
        raise ConsoleError('「%s」需要列表' % path)
    rows: list[Any] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ConsoleError('「%s」第 %d 行需要对象（行被压成了字符串？）' % (path, index + 1))
        clean: dict[str, Any] = dict(item)
        for key, field in row.items():
            if key in clean:
                clean[key] = coerce_schema_value(field, clean[key], '%s[].%s' % (path, key))
        rows.append(clean)
    return rows

#: 任务键 → 配置里对应的「指名模型」项（见 `AstrbotBridge.TASK_MODEL_PATHS`）。
TASK_LABELS = dict(CONSOLE_TASKS)


def _resolve_schema_field(schema: Any, path: Any) -> dict[str, Any]:
    """把 `分组.字段` 解析成 schema 里的字段声明；不在 schema 里就报错。

    这是控制台写配置的**唯一门禁**：只有 `_conf_schema.json` 声明过的路径能写，
    未知路径一律拒绝——控制台能改配置，但改不出 schema 之外的东西。
    """
    parts = [part for part in _text(path).split('.') if part]
    if not parts:
        raise ConsoleError('缺少配置路径')
    if len(parts) < 2:
        raise ConsoleError('「%s」是分组，不是配置项' % parts[0])
    spec = schema.get(parts[0]) if isinstance(schema, dict) else None
    if not isinstance(spec, dict) or spec.get('type') != 'object':
        raise ConsoleError('不认识的配置分组：%s' % parts[0])
    walked: list[str] = [parts[0]]
    for step in parts[1:]:
        # 只能沿着**对象**往下走：列表/标量字段必须整段写入，否则
        # `…providers.api_key` 这种路径会把整个列表写成一个字典。
        if spec.get('type') != 'object':
            raise ConsoleError('「%s」是 %s 字段，只能整段写入' % ('.'.join(walked), spec.get('type')))
        items = spec.get('items')
        nxt = items.get(step) if isinstance(items, dict) else None
        if not isinstance(nxt, dict):
            raise ConsoleError('不认识的配置项：%s' % '.'.join(parts))
        spec = nxt
        walked.append(step)
    return spec


def _set_schema_path(target: dict[str, Any], path: Any, value: Any) -> None:
    """把值写进嵌套字典（沿路径浅拷贝，别改到调用方手里那份配置）。"""
    parts = [part for part in _text(path).split('.') if part]
    node = target
    for step in parts[:-1]:
        child = node.get(step)
        node[step] = dict(child) if isinstance(child, dict) else {}
        node = node[step]
    if parts:
        node[parts[-1]] = value


def _drop_schema_path(target: dict[str, Any], path: Any) -> None:
    """删掉一个键：清空时让宿主按 schema 默认值重建，而不是留个 null 在配置里。"""
    parts = [part for part in _text(path).split('.') if part]
    node: Any = target
    for step in parts[:-1]:
        child = node.get(step) if isinstance(node, dict) else None
        if not isinstance(child, dict):
            return
        node = child
    if parts and isinstance(node, dict):
        node.pop(parts[-1], None)


def _mask_secrets(value: Any) -> Any:
    """密钥类字段一律不回流到浏览器（连接行的 `api_key`）。"""
    if isinstance(value, dict):
        return {
            key: ('' if key in ('api_key', 'apiKey') else _mask_secrets(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_mask_secrets(item) for item in value]
    return value


def mask_endpoint(value: Any) -> str:
    """endpoint 只显示 `主机/路径`，不显示查询串。

    有些网关把 key 或签名放在 query 里（`?api_key=...`），控制台是给人看的页面，
    没必要把它送进浏览器。
    """
    text = str(value or '').strip()
    if not text:
        return ''
    try:
        from urllib.parse import urlsplit

        parts = urlsplit(text)
        if not parts.netloc:
            return text
        return f'{parts.scheme}://{parts.netloc}{parts.path}'
    except Exception:  # noqa: BLE001 - 解析失败就原样返回（它本来也不是合法 URL）
        return text


class ConsoleError(Exception):
    """控制台能直接展示给用户的错误（比如"地址格式不对"），映射成 400。"""


def _float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ('' if value is None else str(value))


def _record(value: Any) -> dict[str, Any]:
    """dict 取值：非 dict（旧库的 null、字符串）一律当空记录，不给控制台抛异常。"""
    return value if isinstance(value, dict) else {}


def _int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _safe_count(database: Any, table: str, where: Optional[dict[str, Any]] = None) -> int:
    """计数失败返回 0（表可能还没建、或旧库缺列）。"""
    try:
        return int(database.count(table, where) or 0)
    except Exception:  # noqa: BLE001
        return 0


#: 群聊的两类条目（群号在 `metadata.groupId`，不在 `participantId`）。
GROUP_KINDS = ('group-message', 'character-group-message')
#: 聊天记录面板认的条目类型。旁白、场景、账本都不属于任何一条对话，因此不在表里。
CHAT_KINDS = ('user-message', 'character-message', *GROUP_KINDS, 'outgoing-delivery-failed')


def _chat_message(
    row: Any,
    character: str,
    names: Optional[dict[str, str]] = None,
) -> Optional[dict[str, Any]]:
    """把一条剧本条目翻译成聊天记录里的一行；不属于任何对话的条目返回 None。"""
    entry = row if isinstance(row, dict) else {}
    kind = _text(entry.get('kind'))
    if kind not in CHAT_KINDS:
        return None
    metadata = entry.get('metadata') if isinstance(entry.get('metadata'), dict) else {}
    participant_id = _text(entry.get('participantId'))
    group_id = _text(metadata.get('groupId') or metadata.get('group_id'))
    if kind in GROUP_KINDS:
        if not group_id:
            return None
        conversation = 'group:%s' % group_id
    elif participant_id:
        conversation = 'private:%s' % participant_id
    else:
        return None
    if kind == 'outgoing-delivery-failed':
        side, sender = 'system', character
    elif kind in ('user-message', 'group-message'):
        side = 'in'
        sender = (
            _text(metadata.get('senderName') or metadata.get('sender_name'))
            or _text(metadata.get('senderId') or metadata.get('sender_id'))
            or _text((names or {}).get(participant_id))
            or '对方'
        )
    else:
        side, sender = 'out', character
    return {
        'entry_id': _int(entry.get('id')),
        'at': _text(entry.get('occurredAt')),
        'conversation': conversation,
        'participant_id': participant_id,
        'group_id': group_id,
        'group_name': _text(metadata.get('groupName') or metadata.get('group_name')),
        'side': side,
        'kind': kind,
        'sender': sender,
        'text': _text(entry.get('content')),
        'quote': _text(metadata.get('quote'))[:200],
    }


def _chat_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    """对话计数：未回复条数 = 她最后一次发言之后收到的来信数。"""
    awaiting = 0
    for item in reversed(items):
        if item.get('side') == 'out':
            break
        if item.get('side') == 'in':
            awaiting += 1
    return {
        'messages': len(items),
        'incoming': len([item for item in items if item.get('side') == 'in']),
        'outgoing': len([item for item in items if item.get('side') == 'out']),
        'failed': len([item for item in items if item.get('side') == 'system']),
        'awaiting': awaiting,
    }


def _safe_all(
    database: Any,
    table: str,
    where: Optional[dict[str, Any]] = None,
    order: Any = None,
    limit: Optional[int] = None,
) -> list[Any]:
    try:
        return list(database.all(table, where, order, limit) or [])
    except Exception:  # noqa: BLE001
        return []


#: 权限四档 → （中文标签，一句话说明）。顺序与 `platform_actions.PERMISSION_TIERS` 一致，
#: 面板的下拉与统计卡都读它——档位名是**权限表里的值**，不能各写一份。
PERMISSION_TIER_LABELS: dict[str, tuple[str, str]] = {
    'global': ('所有人', '任何会话里都能用'),
    'groupadmin': ('仅群主 / 管理员', '只在群里、且发言者是群主或管理员时能用'),
    'admin': ('仅插件管理员', '只有 HDSI 管理员能用'),
    'disabled': ('关闭', '任何会话都不能用这一条'),
}

#: 危险动作的开关组中文标签（该组在 schema 里的描述就是 `platform_actions.RISK_WARNING`，
#: 那是**警示语**，不是分组名；面板的警示条逐字用它）。
RISK_GROUP_LABEL = '风险操作'

#: 风险级别 → 中文标签（面板的风险徽章）。
RISK_LABELS: dict[str, str] = {
    'safe': '安全',
    'sensitive': '敏感',
    'dangerous': '危险',
}

#: 与**平台动作权限表无关**的控制台只读页面。权限表只管 `platform_actions.ACTIONS`
#: 里那些"她能对 QQ 做的事"；「Token 统计」（`tokens`）这类页面读的是本地账本与目录，
#: 不触发任何平台动作，因此**没有**权限需求——这里把边界写下来，别把页面混进权限表
#: （页面的取数接口在 `main.py` 注册，见 `console/token-stats`）。
PERMISSIONLESS_PANELS: tuple[str, ...] = ('tokens',)


class ConsoleApi:
    """控制台的数据来源。所有方法都是协程或纯同步读，返回可 JSON 序列化的 dict。"""

    def __init__(self, bridge: Any) -> None:
        self.bridge = bridge

    # ------------------------------------------------------------------ #
    # Token 统计（本移植版新增）
    # ------------------------------------------------------------------ #

    async def token_stats(self, kind: str = 'day', from_value: str = '', to_value: str = '') -> dict[str, Any]:
        """按天 / 周 / 月 / 自选范围汇总 Token 账本。

        取数在 `console_api`（见 §29 的约定），数学在 `core/token_stats.py`（纯函数）。
        表可能很大（一天一行 × 多个模型 × 多部剧本），所以**只取区间内的行**，
        并把上限卡在 5000 行——真到那个量级说明跑了很久，页面要的是聚合而不是全量。
        """
        database = self.bridge.db
        bounds = range_bounds(kind, datetime.now(), from_value, to_value)
        rows = _safe_all(database, 'interlude_token_usage', None, 'day DESC', 5_000)
        summary = summarize_usage(rows, bounds)
        return {
            'range': normalize_range(kind),
            'from': bounds['from'],
            'to': bounds['to'],
            'timezone': 'server-local',
            **summary,
        }

    # ------------------------------------------------------------------ #
    # 总览
    # ------------------------------------------------------------------ #

    def health_snapshot(self, story_id: str = '') -> dict[str, Any]:
        """读 service 的健康快照；service 未起或没有该能力时回零值空壳。"""
        service = getattr(self.bridge, 'service', None)
        reader = getattr(service, 'health_snapshot', None)
        if not callable(reader):
            return {}
        try:
            return reader(story_id) or {}
        except Exception:  # noqa: BLE001 - 面板取数永远不许把整页打成 500
            return {}

    async def overview(self, story_id: str = '') -> dict[str, Any]:
        database = self.bridge.db
        stories = _safe_all(database, 'interlude_story', order='updatedAt DESC', limit=50)
        current = self._pick_story(stories, story_id)

        counts = {name: _safe_count(database, name) for name in TABLES}
        participants = 0
        entries = 0
        if current:
            participants = _safe_count(database, 'interlude_participant', {'storyId': current.get('id')})
            entries = _safe_count(database, 'interlude_script_entry', {'storyId': current.get('id')})

        return {
            'plugin': {
                'name': PLUGIN_NAME,
                'display_name': 'HDS Interlude',
                'version': _plugin_version(),
                'upstream_version': HDS_INTERLUDE_VERSION,
                'data_dir': _text(getattr(self.bridge, 'data_dir', '')),
                'config_path': _text(self.bridge.config_file_path()),
            },
            'service': {
                'started': bool(getattr(self.bridge, '_started', False)),
                'blind_mode': bool(getattr(self.bridge, 'blind_mode_enabled', False)),
                'story_count': len(stories),
                'participant_count': participants,
                'entry_count': entries,
            },
            'story': self._story_brief(current) if current else None,
            # 上游 1.0.1-rc28 `health.ts`：自插件重载以来的滚动健康指标。
            # 没有样本时返回零值快照（面板显示 0 而不是 500，见 §29 的控制台取数约定）。
            'health': self.health_snapshot(_text(current.get('id'))) if current else {
                'narrativeTotal': 0, 'narrativeFailed': 0, 'structureMissing': 0,
                'recoverySaved': 0, 'replyModes': {}, 'sideTaskTotal': 0, 'sideTaskFailed': 0,
                'proactiveTotal': 0, 'proactiveSent': 0, 'inputTokens': 0, 'cachedTokens': 0,
                'latenciesMs': [], 'sinceAt': '', 'successRate': 1, 'structureMissingRate': 0,
                'cacheHitRate': 0, 'proactiveRate': 0, 'medianLatencyMs': 0,
            },
            'stories': [self._story_brief(item) for item in stories],
            'flags': self._flags(),
            'routing': self._routing_rows(),
            'capability': {
                'image': self._note('image'),
                'audio': self._note('audio'),
            },
            'counts': counts,
            'context_metrics': self._context_metrics(current),
        }

    def _context_metrics(self, story: Any) -> dict[str, Any]:
        """上轮上下文构成（v1.4.0，`docs/MEMORY_MAINTENANCE.md` §5.4）。

        存在剧本 `state.extensions.last_context_metrics` 里，只保留最近一轮。
        读的时候顺手把 wire 段名翻成人话，前端不用再维护一份映射。
        """
        if not story:
            return {}
        try:
            state = decode_story_state(story.get('state'))
        except Exception:  # noqa: BLE001 - 旧库的 state 可能不成形状
            return {}
        extensions = state.get('extensions') if isinstance(state.get('extensions'), dict) else {}
        metrics = extensions.get('last_context_metrics')
        if not isinstance(metrics, dict):
            return {}
        sections = metrics.get('sections') if isinstance(metrics.get('sections'), dict) else {}
        rows = [
            {
                'key': key,
                'label': CONTEXT_SECTION_LABELS.get(key, key),
                'items': int((value or {}).get('items') or 0),
                'characters': int((value or {}).get('characters') or 0),
            }
            for key, value in sections.items()
        ]
        rows.sort(key=lambda row: -row['characters'])
        return {
            'at': _text(metrics.get('at')),
            'phase': _text(metrics.get('phase')),
            'participant_id': _text(metrics.get('participant_id')),
            'assembly_ms': int(metrics.get('assembly_ms') or 0),
            'items': int(metrics.get('items') or 0),
            'characters': int(metrics.get('characters') or 0),
            'payload_characters': int(metrics.get('payload_characters') or 0),
            'estimated_tokens': int(metrics.get('estimated_tokens') or 0),
            'sections': rows,
        }

    # ------------------------------------------------------------------ #
    # 模型
    # ------------------------------------------------------------------ #

    async def models(self, story_id: str = '') -> dict[str, Any]:
        del story_id  # 模型配置与具体故事无关，保留参数是为了接口形状统一
        section = self.bridge.section('model')
        connections = []
        for raw in section.get('providers') or []:
            if not isinstance(raw, dict):
                continue
            connections.append({
                'label': _text(raw.get('label')),
                'enabled': raw.get('enabled') is not False,
                'mode': _text(raw.get('mode')) or 'openai-compatible',
                'endpoint': mask_endpoint(raw.get('endpoint')),
                'has_endpoint': bool(_text(raw.get('endpoint')).strip()),
                'model': _text(raw.get('model')),
                'has_key': bool(_text(raw.get('api_key')).strip()),
                'tasks': [TASK_LABELS[key] for key, _ in CONSOLE_TASKS if raw.get(f'use_for_{key}') is True],
                'prices': {
                    'input': raw.get('price_input') or 0,
                    'output': raw.get('price_output') or 0,
                    'cached': raw.get('price_cached_input') or 0,
                },
                'response_format': _text(raw.get('response_format')),
                'temperature': raw.get('temperature'),
                'top_p': raw.get('top_p'),
                'max_tokens': raw.get('max_tokens'),
                'timeout': raw.get('timeout'),
            })

        failover = section.get('failover') or {}
        embedding = section.get('embedding') or {}
        vision = section.get('vision') or {}
        audio = section.get('audio') or {}

        return {
            'tasks': self._routing_rows(),
            # `audio` 不是聊天任务（它是"语音→文字"的转写模型，不参与叙事路由），
            # 所以不进 `tasks`，但页面要显示它，仍然放进 task_models。
            'task_models': {
                key: {
                    'label': label,
                    'astrbot_provider': self.bridge.task_model_id(key),
                    'modalities': sorted(self.bridge.task_provider_modalities(key)),
                }
                for key, label in (*CONSOLE_TASKS, ('audio', '语音转写'))
            },
            'connections': connections,
            'astrbot_providers': self._astrbot_providers(),
            'embedding': {
                'enabled': bool(embedding.get('enabled')),
                'semantic_history': bool(embedding.get('semantic_history')),
                'live_query': bool(embedding.get('live_query')),
                'endpoint': mask_endpoint(embedding.get('endpoint')),
                'dimensions': _int(embedding.get('dimensions')),
                'provider_id': self.bridge.task_model_id('embedding'),
            },
            'vision': {
                'enabled': bool(vision.get('enabled')),
                'mode': _text(vision.get('mode')) or 'native',
                'detail': _text(vision.get('detail')) or 'auto',
                'max_image_dimension': _int(vision.get('max_image_dimension')),
                'provider_id': self.bridge.task_model_id('vision'),
            },
            'audio': {
                'enabled': bool(audio.get('enabled')),
                'out_format': _text(audio.get('out_format')) or 'mp3',
                'max_file_size_mb': _int(audio.get('max_file_size_mb')),
                'provider_id': self.bridge.task_model_id('audio'),
            },
            'failover': {
                'enabled': failover.get('enabled') is not False,
                'strategy': _text(failover.get('strategy')) or 'priority',
                'max_attempts': _int(failover.get('max_attempts_per_provider'), 1),
                'cooldown_minutes': _int(failover.get('cooldown_minutes')),
            },
            'main': {
                'provider_label': self.bridge.main_provider_label(),
                'temperature': section.get('main_temperature'),
                'top_p': section.get('main_top_p'),
                'max_tokens': section.get('main_max_tokens'),
                'timeout': section.get('main_timeout'),
                'response_format': _text(section.get('main_response_format')),
                'streaming_mode': _text(section.get('main_streaming_mode')) or 'off',
            },
            'capability': {'image': self._note('image'), 'audio': self._note('audio')},
            'usage': self._usage(),
        }

    # ------------------------------------------------------------------ #
    # 剧本 / 记忆 / 数据库 / 日志
    # ------------------------------------------------------------------ #

    async def script(self, story_id: str = '', limit: int = 60, offset: int = 0) -> dict[str, Any]:
        database = self.bridge.db
        stories = _safe_all(database, 'interlude_story', order='updatedAt DESC', limit=50)
        current = self._pick_story(stories, story_id)
        if not current:
            return {'story': None, 'entries': [], 'total': 0, 'scenes': [], 'arcs': []}
        sid = _text(current.get('id'))
        size = max(1, min(200, _int(limit, 60)))
        skip = max(0, _int(offset, 0))
        total = _safe_count(database, 'interlude_script_entry', {'storyId': sid})
        rows = _safe_all(
            database, 'interlude_script_entry', {'storyId': sid}, 'occurredAt DESC', skip + size,
        )
        rows = rows[skip:]
        return {
            'story': self._story_brief(current),
            'total': total,
            'offset': skip,
            'limit': size,
            'entries': [self._entry_brief(row) for row in rows],
            'scenes': [
                {
                    'id': item.get('id'),
                    'status': _text(item.get('status')),
                    'hook': _text(item.get('hook')),
                    'summary': _text(item.get('summary')),
                    'started_at': _text(item.get('startedAt')),
                    'ended_at': _text(item.get('endedAt')),
                    'entry_count': _int(item.get('entryCount')),
                }
                for item in _safe_all(database, 'interlude_scene', {'storyId': sid}, 'startedAt DESC', 20)
            ],
            'arcs': [
                {
                    'id': item.get('id'),
                    'status': _text(item.get('status')),
                    'title': _text(item.get('title')),
                    'summary': _text(item.get('summary')),
                    'scene_count': _int(item.get('sceneCount')),
                }
                for item in _safe_all(database, 'interlude_arc', {'storyId': sid}, 'updatedAt DESC', 20)
            ],
        }

    async def memory(self, story_id: str = '') -> dict[str, Any]:
        database = self.bridge.db
        stories = _safe_all(database, 'interlude_story', order='updatedAt DESC', limit=50)
        current = self._pick_story(stories, story_id)
        if not current:
            return {
                'story': None, 'facts': [], 'memories': [], 'intents': [],
                'patches': [], 'overlays': [], 'participants': [],
            }
        sid = _text(current.get('id'))
        return {
            'story': self._story_brief(current),
            'facts': [self._fact_brief(row) for row in _safe_all(
                database, 'interlude_fact', {'storyId': sid}, 'importance DESC', 80,
            )],
            'memories': [self._memory_brief(row) for row in _safe_all(
                database, 'interlude_memory', {'storyId': sid}, 'importance DESC', 60,
            )],
            'intents': [self._intent_brief(row) for row in _safe_all(
                database, 'interlude_intent', {'storyId': sid}, 'notBefore DESC', 40,
            )],
            'patches': [self._patch_brief(row) for row in _safe_all(
                database, 'interlude_state_patch', {'storyId': sid}, 'createdAt DESC', 40,
            )],
            'overlays': [self._overlay_brief(row) for row in _safe_all(
                database, 'interlude_overlay_snapshot', {'storyId': sid}, 'periodEnd DESC', 20,
            )],
            'participants': [self._participant_brief(row) for row in _safe_all(
                database, 'interlude_participant', {'storyId': sid}, 'updatedAt DESC', 40,
            )],
        }

    async def database(self) -> dict[str, Any]:
        database = self.bridge.db
        tables = []
        for name, spec in TABLES.items():
            tables.append({
                'name': name,
                'columns': len(spec.fields),
                'rows': _safe_count(database, name),
                'added_later': bool(getattr(spec, 'added_later', False)),
            })
        path = ''
        try:
            path = _text(getattr(database, 'path', ''))
        except Exception:  # noqa: BLE001
            path = ''
        size = 0
        if path and os.path.isfile(path):
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
        return {
            'tables': sorted(tables, key=lambda item: item['rows'], reverse=True),
            'total_rows': sum(item['rows'] for item in tables),
            'path': path,
            'size_bytes': size,
        }

    async def logs(self, limit: int = 200, level: str = '') -> dict[str, Any]:
        size = max(1, min(CONSOLE_LOG_MAX, _int(limit, 200)))
        wanted = _text(level).strip().lower()
        records = list(getattr(self.bridge, 'log_buffer', []) or [])
        if wanted:
            records = [item for item in records if _text(item.get('level')) == wanted]
        # 最新的在前，方便页面直接渲染
        return {
            'records': list(reversed(records))[:size],
            'total': len(records),
            'capacity': CONSOLE_LOG_MAX,
        }

    # ------------------------------------------------------------------ #
    # 写操作（控制台里唯一会改状态的两处）
    # ------------------------------------------------------------------ #

    #: 控制台允许切换的开关：`键` → `(分组, 配置键, 中文名)`。
    #: **白名单**，不接受任意路径——控制台不是配置编辑器，别把整个 schema 暴露成写接口。
    FLAG_KEYS: dict[str, tuple[str, str, str]] = {
        'allow_proactive_messages': ('runtime', 'allow_proactive_messages', '主动可见消息'),
        'agency': ('agency', 'enabled', 'Agency 行动窗口'),
        'urge': ('urge', 'enabled', 'Urge 弹性推进'),
        'schedule_preplan': ('schedule_preplan', 'enabled', '日程预排'),
        'timeline_director': ('timeline_director', 'enabled', '时间导演'),
        'compaction': ('compaction', 'enabled', '后台压缩'),
        'embedding': ('embedding', 'enabled', '语义检索'),
        'vision': ('vision', 'enabled', '图片理解'),
        'audio': ('audio', 'enabled', '语音理解'),
        'browser': ('browser', 'enabled', '网页观察'),
    }

    async def set_flag(self, name: str, value: Any) -> dict[str, Any]:
        """切换一个运行开关。返回落盘后的完整开关表，前端直接替换本地状态。"""
        spec = self.FLAG_KEYS.get(_text(name))
        if not spec:
            raise ConsoleError('不认识的开关：%s' % name)
        section, key, label = spec
        target = dict(self.bridge.raw_config())
        if section in NESTED_MODEL_SECTIONS:
            # `compaction` / `embedding` / `vision` / `audio` 在 schema 里嵌在
            # `model_center` 下，顶层**没有**这个分组。只写嵌套那一处：
            #   * 顶层同名键不是合法配置（宿主下次加载当未知键删掉，还会混进导出文件），
            #     顺手把旧版本留下的垃圾清掉；
            #   * 读取侧 `bridge.section()` 对这几个段固定看嵌套那份，写一处也读得对。
            model = target.get('model_center')
            model = dict(model) if isinstance(model, dict) else {}
            nested = dict(model.get(section)) if isinstance(model.get(section), dict) else {}
            nested[key] = bool(value)
            model[section] = nested
            target['model_center'] = model
            target.pop(section, None)
        else:
            group = target.get(section)
            group = dict(group) if isinstance(group, dict) else {}
            group[key] = bool(value)
            target[section] = group
        saved_via = await self.bridge.save_raw_config(target)
        self._reload()
        return {
            'flags': self._flags(),
            'saved_via': saved_via,
            'changed': f'{label} → {"开启" if value else "关闭"}',
        }

    # ------------------------------------------------------------------ #
    # 平台动作目录与权限（唯一事实源：`core/platform_actions.py`）
    # ------------------------------------------------------------------ #

    async def actions_catalog(self) -> dict[str, Any]:
        """「动作」面板的全部数据：动作目录 + 当前生效档位 + 统计。

        三件事必须说清（与运行期**同源**，别在控制台里重算一套）：

        1. `permission` = **权限表里的档位**（`effective_permission(..., None)`，
           即用户在下拉里选的那个值；没选过就是目录声明的默认档）；
        2. `enabled` = **实际能不能用**（`is_action_enabled(...)`）= 权限表档位
           **与**配置开关的与关系：开关关掉时档位无论选什么都不生效；
        3. `config_enabled` = 那个开关的原始值（`True` / `False` / `None` = 未配置）。

        另外每条动作带 `backends`（人话标签，**顺序 = 优先级**）与 `napcat_only`，
        顶层再给一份 `napcat_only` id 清单——面板的「只看 NapCat 专属（N）」筛选与徽章
        都读它，**不在前端重算**（`backends` 的顺序就是运行期的通道优先级）。

        配置开关的分组由 `platform_actions.action_config_group()` 给出（危险动作一律进
        `actions_risks` 风险组），子键 = 动作 id。**分组不存在 = 未配置 = 不限制**，
        所以旧版本升级上来的用户不会因为 schema 还没落地就整页显示"全关"。
        """
        table = self._action_permissions()
        switches = self._action_switches()
        actions: list[dict[str, Any]] = []
        risk_counts = {level: 0 for level in platform_actions.RISK_LEVELS}
        tier_counts = {tier: 0 for tier in platform_actions.PERMISSION_TIERS}
        enabled_total = 0
        risky_enabled = 0
        for item in platform_actions.ACTIONS.values():
            switch = switches.get(item.id)
            permission = platform_actions.effective_permission(item.id, table, None)
            enabled = platform_actions.is_action_enabled(item.id, table, switch)
            risk_counts[item.risk] = risk_counts.get(item.risk, 0) + 1
            tier_counts[permission] = tier_counts.get(permission, 0) + 1
            if enabled:
                enabled_total += 1
                if item.risk == 'dangerous':
                    risky_enabled += 1
            actions.append({
                'id': item.id,
                'category': item.category,
                'category_label': platform_actions.ACTION_CATEGORIES.get(item.category, item.category),
                'label': item.label,
                'summary': item.summary,
                'risk': item.risk,
                'default_permission': item.default_permission,
                'permission': permission,
                'enabled': enabled,
                'config_enabled': switch,
                'group': platform_actions.action_config_group(item),
                'returns': item.returns,
                # 后端标注（顺序 = 优先级）：面板据此打徽章、聚「NapCat 专属」。
                # `napcat_only` 与 core 的 `PlatformAction.napcat_only` 同源，前端不再自己判。
                'backends': platform_actions.backend_labels(item),
                'napcat_only': item.napcat_only,
                # 适用范围与**这条动作实际适用**的档位：非群聊动作不下发「仅群管」
                # （选了等于关掉，界面上不该出现），前端下拉直接读它。
                'scopes': list(item.scopes),
                'tiers': list(platform_actions.permission_tiers_for(item.id)),
                'params': [
                    {
                        'name': param.name,
                        'label': param.label,
                        'type': param.type,
                        'required': bool(param.required),
                        'minimum': param.minimum,
                        'maximum': param.maximum,
                        'choices': list(param.choices),
                        'note': param.note,
                    }
                    for param in item.params
                ],
            })
        return {
            'actions': actions,
            'tiers': [
                {
                    'id': tier,
                    'label': PERMISSION_TIER_LABELS.get(tier, (tier, ''))[0],
                    'description': PERMISSION_TIER_LABELS.get(tier, (tier, ''))[1],
                }
                for tier in platform_actions.PERMISSION_TIERS
            ],
            'groups': self._action_groups(),
            # 逐字用 core 的常量（面板要显示的就是这句原话）。
            'risk_warning': platform_actions.RISK_WARNING,
            'risk_labels': dict(RISK_LABELS),
            'risky': [item.id for item in platform_actions.risky_actions()],
            # NapCat 专属动作（含"走 NapCat WS 拿 cookie 打 QZone CGI"的空间动作）：
            # 面板的「只看 NapCat 专属」筛选与说明区都用它，顺序与 core 一致。
            'napcat_only': [item.id for item in platform_actions.napcat_actions()],
            'backend_labels': dict(platform_actions.BACKEND_LABELS),
            'permissionless_panels': list(PERMISSIONLESS_PANELS),
            'permissions_path': self._action_permissions_file(),
            'stats': {
                'total': len(actions),
                'enabled': enabled_total,
                'disabled': len(actions) - enabled_total,
                'risky': risk_counts.get('dangerous', 0),
                'risky_enabled': risky_enabled,
                'napcat_only': sum(1 for row in actions if row['napcat_only']),
                'risk': risk_counts,
                'permissions': tier_counts,
            },
        }

    async def set_action_permission(self, action_id: Any, tier: Any) -> dict[str, Any]:
        """把某个动作写进权限表（`action_permissions.json`），返回归一化后的整表。

        未知动作 / 未知档位**一律拒绝**（映射成 400）：权限表是安全边界，
        静默接受一个拼错的动作 id 等于让用户以为"我关了它"，其实什么都没关。
        """
        action = platform_actions.ACTIONS.get(_text(action_id).strip())
        if action is None:
            raise ConsoleError('未知动作：%s' % (_text(action_id).strip() or '(空)'))
        level = _text(tier).strip().lower()
        if level not in platform_actions.PERMISSION_TIERS:
            raise ConsoleError('未知权限档位：%s（可选：%s）' % (
                _text(tier) or '(空)', ' / '.join(platform_actions.PERMISSION_TIERS),
            ))
        allowed = platform_actions.permission_tiers_for(action.id)
        if level not in allowed:
            # 「仅群管」对私聊动作没有意义：存下去只会变成一个永远不生效的档位。
            raise ConsoleError('「%s」不适用于动作「%s」（可选：%s）' % (
                PERMISSION_TIER_LABELS.get(level, (level, ''))[0], action.label,
                ' / '.join(PERMISSION_TIER_LABELS.get(t, (t, ''))[0] for t in allowed),
            ))
        table = self._action_permissions()
        table[action.id] = level
        saved = self._save_action_permissions(table)
        return {
            'action': action.id,
            'tier': level,
            'permissions': saved,
            'permissions_path': self._action_permissions_file(),
            'changed': '%s 的权限档位 → %s' % (
                action.label, PERMISSION_TIER_LABELS.get(level, (level, ''))[0],
            ),
        }

    async def reset_action_permissions(self) -> dict[str, Any]:
        """清空权限表：所有动作回到目录声明的默认档（危险动作仍然默认关闭）。"""
        saved = self._save_action_permissions({})
        return {
            'permissions': saved,
            'permissions_path': self._action_permissions_file(),
            'changed': '动作权限表已清空（全部回到默认档位）',
        }

    def _action_permissions(self) -> dict[str, str]:
        """当前权限表（归一化；读不到就当空表 = 全部走默认档，绝不抛）。

        坏文件由桥接侧 `_load_action_permissions()` 负责 warn，这里只保证面板能打开。
        """
        try:
            raw = self.bridge.action_permissions()
        except Exception:  # noqa: BLE001 - 权限表读取失败不该让面板打不开
            return {}
        return platform_actions.normalize_permissions(raw)

    def _save_action_permissions(self, table: dict[str, str]) -> dict[str, str]:
        """写权限表。桥接不支持（老版本 / 测试桩）或写盘失败时给**可读的 400**。"""
        saver = getattr(self.bridge, 'save_action_permissions', None)
        if not callable(saver):
            raise ConsoleError('当前桥接不支持写入动作权限表，请升级插件后重试')
        try:
            return platform_actions.normalize_permissions(saver(table))
        except ConsoleError:
            raise
        except Exception as error:  # noqa: BLE001 - 写不进去要说出来，不能假装成功
            raise ConsoleError('写入动作权限表失败：%s' % error)

    def _action_switches(self) -> dict[str, Any]:
        """每个动作的配置开关值：`True` / `False` / `None`（分组或键不存在 = 未配置）。

        只读 `bridge.section(分组)`，读不到就是未配置——总闸没配等于不限制，
        与 `effective_permission(enabled=None)` 的语义一致。
        """
        sections: dict[str, dict[str, Any]] = {}
        switches: dict[str, Any] = {}
        for item in platform_actions.ACTIONS.values():
            group = platform_actions.action_config_group(item)
            if group not in sections:
                try:
                    section = self.bridge.section(group)
                except Exception:  # noqa: BLE001 - 取配置失败按"未配置"处理
                    section = None
                sections[group] = section if isinstance(section, dict) else {}
            value = sections[group].get(item.id)
            switches[item.id] = value if isinstance(value, bool) else None
        return switches

    def _action_groups(self) -> dict[str, str]:
        """配置分组 id → 中文标签（面板用它说明"开关在哪一组"）。

        标签来自 `platform_actions.ACTION_CONFIG_GROUP_LABELS`——收敛成四个组之后
        "先到的类别定标签"会把 `actions_chat` 标成「互动」，而那一组里还有消息 /
        历史 / 状态 / 资料 / 语音 / 联系人。表里没有的分组才回落到类别标签。
        """
        groups: dict[str, str] = {
            group: label
            for group, label in platform_actions.ACTION_CONFIG_GROUP_LABELS.items()
        }
        for category, label in platform_actions.ACTION_CATEGORIES.items():
            group = platform_actions.ACTION_CONFIG_GROUPS.get(category)
            if group:
                groups.setdefault(group, label)
        groups.setdefault(platform_actions.ACTION_RISK_GROUP, RISK_GROUP_LABEL)
        return groups

    def _action_permissions_file(self) -> str:
        """权限表文件的实际路径（页面上写出来，用户能自己去看 / 备份）。"""
        data_dir = _text(getattr(self.bridge, 'data_dir', ''))
        return os.path.join(data_dir, 'action_permissions.json') if data_dir else 'action_permissions.json'

    # ------------------------------------------------------------------ #
    # 配置页（schema 驱动：所有可配置项都能在控制台改）
    # ------------------------------------------------------------------ #

    async def config_schema(self) -> dict[str, Any]:
        """把 `_conf_schema.json` + 磁盘上的当前值一起交给配置页。

        每个字段带三样东西：**当前值**（密钥类字段会抹掉）、**schema 声明**
        （类型/说明/hint/默认值/候选项），以及**兼容性提示** `note`
        ——宿主配置页编不了的字段在这里会被点名（见 `host_editor_note`）。
        """
        schema = load_config_schema()
        raw = self.bridge.raw_config()
        groups: list[dict[str, Any]] = []
        for group_key, group_spec in schema.items():
            if not isinstance(group_spec, dict) or group_spec.get('type') != 'object':
                continue
            # 当前值取**读取侧看到的那一份**（含 `LEGACY_SECTION_MERGES` 的 N:1 归并）：
            # 配置页显示的必须是运行期真正生效的值，否则又会出现"界面开着、行为关着"
            # （坑 34 的老病）。归并不是目标的普通分组原样返回。
            current = merge_legacy_section_values(raw, group_key, raw.get(group_key))
            fields: list[dict[str, Any]] = []
            for field_key, spec in (group_spec.get('items') or {}).items():
                if not isinstance(spec, dict):
                    continue
                path = '%s.%s' % (group_key, field_key)
                row = schema_row_fields(spec)
                fields.append({
                    'key': field_key,
                    'path': path,
                    'type': str(spec.get('type') or 'string'),
                    #: 原始 schema 节点：前端据此递归渲染（对象/列表/标量都走同一套）。
                    'node': spec,
                    'description': _text(spec.get('description')),
                    'hint': _text(spec.get('hint')),
                    'default': spec.get('default'),
                    'options': spec.get('options') if isinstance(spec.get('options'), list) else None,
                    'rows': [
                        {
                            'key': row_key,
                            'type': str(row_spec.get('type') or 'string'),
                            'description': _text(row_spec.get('description')),
                            'hint': _text(row_spec.get('hint')),
                            'default': row_spec.get('default'),
                            'options': row_spec.get('options') if isinstance(row_spec.get('options'), list) else None,
                        }
                        for row_key, row_spec in (row or {}).items()
                    ] or None,
                    'item_type': str((spec.get('items') or {}).get('type') or '') if row is None else '',
                    'special': _text(spec.get('_special')),
                    'invisible': bool(spec.get('invisible')),
                    'advanced': bool(spec.get('advanced')),
                    'value': _mask_secrets(current.get(field_key, None)),
                    'present': field_key in current,
                    'note': host_editor_note(path, spec),
                    'delegated': path in DELEGATED_FIELDS,
                })
            groups.append({
                'key': group_key,
                #: 短标题（`_conf_schema.json` 的 `title`）：下拉与卡片标题用它，
                #: `description` 是一句话说明，留在卡片里当副标题。
                'title': _text(group_spec.get('title')),
                'description': _text(group_spec.get('description')),
                'invisible': bool(group_spec.get('invisible')),
                'fields': fields,
            })
        return {
            'groups': groups,
            'choices': await self._vendor_choices(),
            'config_path': _text(self.bridge.config_file_path()),
            'version': _plugin_version(),
        }

    async def set_config_value(self, path: Any, value: Any) -> dict[str, Any]:
        """按 schema 路径写一个配置项（**路径必须存在于 schema**）。

        这条接口把老版本的"手写白名单"升级成"整份 schema 白名单"：
        控制台现在能改所有可配置项，但仍然改不动 schema 之外的东西。
        写盘走 `Bridge.save_raw_config()`（与配置导入同一条路径），并在内存里立即生效。
        """
        schema = load_config_schema()
        spec = _resolve_schema_field(schema, path)
        text_path = _text(path)
        if text_path.endswith(('api_key', 'apiKey')):
            if value is None:
                coerced: Any = ''
            elif value == '':
                return {  # 空串 = 保留原值（与连接池编辑器的约定一致）
                    'path': text_path, 'value': '', 'saved_via': '',
                    'changed': '%s 保持不变' % text_path,
                }
            else:
                coerced = coerce_schema_value(spec, value, text_path)
        else:
            coerced = coerce_schema_value(spec, value, text_path)
        target = dict(self.bridge.raw_config())
        if coerced is None:
            # 清空 = 删键，让宿主按 schema 默认值重建；留个 `null` 会污染强类型字段。
            _drop_schema_path(target, text_path)
        else:
            _set_schema_path(target, text_path, coerced)
        saved_via = await self.bridge.save_raw_config(target)
        self._reload()
        return {
            'path': text_path,
            'value': _mask_secrets(coerced),
            'saved_via': saved_via,
            'changed': '%s 已更新' % text_path,
            'config_path': _text(self.bridge.config_file_path()),
        }

    async def participants(self, story_id: str = '') -> dict[str, Any]:
        """已知参与者（给白名单填表用：从真人会话里直接把 QQ 与昵称带过来）。"""
        rows = _safe_all(self.bridge.db, 'interlude_participant', order='updatedAt DESC', limit=200)
        rows = [row for row in rows if isinstance(row, dict)]
        if story_id:
            rows = [row for row in rows if _text(row.get('storyId')) == story_id]
        return {
            'participants': [
                {
                    'participant_id': _text(row.get('id')),
                    'story_id': _text(row.get('storyId')),
                    'platform': _text(row.get('platform')),
                    'self_id': _text(row.get('selfId')),
                    'user_id': _text(row.get('userId')),
                    'channel_id': _text(row.get('channelId')),
                    'person_id': _text(row.get('personId')),
                    'display_name': _text(row.get('displayName')),
                    'relationship': _text(row.get('relationship')),
                    'status': _text(row.get('status')),
                    'updated_at': _text(row.get('updatedAt')),
                }
                for row in rows
            ],
        }

    async def _vendor_choices(self) -> dict[str, list[dict[str, str]]]:
        """`_special` 选择器的候选项（Provider / 人格）。"""
        providers: list[dict[str, str]] = [{'value': '', 'label': '（留空 = 默认 Provider）'}]
        for row in self._astrbot_providers():
            identifier = _text(row.get('id'))
            if not identifier:
                continue
            model = _text(row.get('model'))
            providers.append({
                'value': identifier,
                'label': '%s · %s' % (identifier, model) if model else identifier,
            })
        return {
            'select_provider': providers,
            'select_provider_stt': providers,
            # 语音走的是**宿主的 TTS 服务商**，不是对话模型：复用 `_astrbot_providers()`
            # 会选出一个发不出语音的 id（用户会以为"配了没用"）。
            'select_provider_tts': self._tts_choices(),
            'select_persona': await self._persona_choices(),
        }

    def _tts_choices(self) -> list[dict[str, str]]:
        """`_special: select_provider_tts` 的候选项：宿主里配好的 TTS 服务商。"""
        choices: list[dict[str, str]] = [{'value': '', 'label': '（留空 = 默认 TTS）'}]
        lister = getattr(self.bridge, '_tts_providers', None)
        rows = []
        if callable(lister):
            try:
                rows = list(lister() or [])
            except Exception as error:  # noqa: BLE001 - 读不到不影响整页
                log_fallback('debug', '取 TTS 服务商候选项失败：%s', error)
                rows = []
        for provider in rows:
            identifier = _text(
                getattr(provider, 'provider_id', '') or getattr(provider, 'id', '')
                or getattr(provider, 'name', '')
            )
            if not identifier:
                continue
            label = _text(getattr(provider, 'name', '')) or identifier
            choices.append({
                'value': identifier,
                'label': '%s · %s' % (identifier, label) if label != identifier else identifier,
            })
        return choices

    async def _persona_choices(self) -> list[dict[str, str]]:
        """AstrBot 里的人格列表（`select_persona` 的候选项）。

        `PersonaManager.get_all_personas` 是**协程**（`persona_mgr.py:187`）——
        漏 await 会得到 `'coroutine' object is not iterable`，整页 500（实测过）。
        """
        manager = getattr(self.bridge.context, 'persona_manager', None)
        getter = getattr(manager, 'get_all_personas', None)
        if not callable(getter):
            return []
        try:
            personas = getter()
            if inspect.isawaitable(personas):
                personas = await personas
        except Exception:  # noqa: BLE001 - 读不到人格不影响整页
            return []
        choices: list[dict[str, str]] = []
        for persona in personas or []:
            identifier = _text(getattr(persona, 'persona_id', '') or getattr(persona, 'id', ''))
            if identifier:
                choices.append({'value': identifier, 'label': identifier})
        return choices

    #: 连接行允许前端写的字段与类型（其余字段保留原值，杜绝越权写）。
    CONNECTION_FIELDS: dict[str, str] = {
        'label': 'str', 'enabled': 'bool', 'mode': 'str', 'endpoint': 'str',
        'model': 'str', 'response_format': 'str',
        # 协议（v1.7.0）：`chat-completions`（默认）/ `anthropic-messages`，
        # 以及 Anthropic 的缓存标记。**漏进白名单 = 控制台保存时静默丢掉这两项**
        # （schema 加了也没用，用户填了不生效）。
        'protocol': 'str', 'anthropic_cache': 'bool',
        'temperature': 'float', 'top_p': 'float', 'max_tokens': 'int', 'timeout': 'int',
        'extra_headers': 'str', 'extra_body': 'str',
        'reasoning_effort': 'str', 'deepseek_thinking': 'str', 'deepseek_reasoning_effort': 'str',
        'dashscope_region': 'str',
        'price_input': 'float', 'price_output': 'float', 'price_cached_input': 'float',
    }

    #: 「用途」勾选（`use_for_*`）单独处理：它们决定这条连接参与哪些任务的路由。
    TASK_FLAGS: tuple[str, ...] = (
        'use_for_main', 'use_for_compaction', 'use_for_alter',
        'use_for_embedding', 'use_for_stickers', 'use_for_vision',
        # 上游 1.0.1-rc24：世界播种器的模型选择并入用途勾选。
        'use_for_world_seeding',
    )

    async def save_connection(self, payload: Any) -> dict[str, Any]:
        """新增 / 修改一条模型连接。

        `index` 为空表示新增。`api_key` 特殊约定：
        - 字段**不传**或传空串 → **保留原值**（前端拿不到现有密钥，也不该拿到）；
        - 传 `null` → 显式清空；
        - 传非空字符串 → 覆盖。
        """
        if not isinstance(payload, dict):
            raise ConsoleError('请求体必须是对象')
        target = dict(self.bridge.raw_config())
        model = target.get('model_center')
        model = dict(model) if isinstance(model, dict) else {}
        rows = model.get('providers')
        rows = [dict(item) for item in rows if isinstance(item, dict)] if isinstance(rows, list) else []

        raw_index = payload.get('index', '')
        editing = str(raw_index).strip() != ''
        index = _int(raw_index, -1) if editing else -1
        if editing and not (0 <= index < len(rows)):
            raise ConsoleError('要修改的连接不存在：序号 %s' % raw_index)
        row: dict[str, Any] = dict(rows[index]) if editing else {}
        if not editing:
            row.setdefault('enabled', True)
            row.setdefault('mode', 'openai-compatible')
            row.setdefault('response_format', 'json-object')

        for field, kind in self.CONNECTION_FIELDS.items():
            if field not in payload:
                continue
            value = payload[field]
            if kind == 'bool':
                row[field] = bool(value)
            elif kind == 'int':
                row[field] = _int(value, _int(row.get(field), 0))
            elif kind == 'float':
                row[field] = _float(value, _float(row.get(field), 0.0))
            else:
                row[field] = _text(value)

        for flag in self.TASK_FLAGS:
            if flag in payload:
                row[flag] = bool(payload[flag])

        if 'api_key' in payload:
            key = payload['api_key']
            if key is None:
                row['api_key'] = ''
            elif _text(key).strip():
                row['api_key'] = _text(key).strip()

        endpoint = _text(row.get('endpoint')).strip()
        if endpoint and not endpoint.lower().startswith(('http://', 'https://')):
            raise ConsoleError('地址要以 http:// 或 https:// 开头（填完整的地址：Chat Completions 是 …/chat/completions，Anthropic 是 …/messages）')
        row['endpoint'] = endpoint

        label = _text(row.get('label')).strip()
        if not label:
            raise ConsoleError('连接名称不能为空')

        if editing:
            rows[index] = row
        else:
            rows.append(row)
        model['providers'] = rows
        target['model_center'] = model

        saved_via = await self.bridge.save_raw_config(target)
        self._reload()
        return {
            'saved_via': saved_via,
            'index': index if editing else len(rows) - 1,
            'connections': (await self.models())['connections'],
            'changed': ('已更新连接 %s' % label) if editing else ('已新增连接 %s' % label),
        }

    async def delete_connection(self, index: Any) -> dict[str, Any]:
        """删掉一条连接。数组会被重排，所以返回新列表让前端整体替换。"""
        target = dict(self.bridge.raw_config())
        model = target.get('model_center')
        model = dict(model) if isinstance(model, dict) else {}
        rows = model.get('providers')
        rows = [dict(item) for item in rows if isinstance(item, dict)] if isinstance(rows, list) else []
        position = _int(index, -1)
        if not (0 <= position < len(rows)):
            raise ConsoleError('要删除的连接不存在：序号 %s' % index)
        label = _text(rows[position].get('label'))
        rows.pop(position)
        model['providers'] = rows
        target['model_center'] = model
        saved_via = await self.bridge.save_raw_config(target)
        self._reload()
        return {
            'saved_via': saved_via,
            'connections': (await self.models())['connections'],
            'changed': '已删除连接 %s' % (label or ('#%d' % position)),
        }

    def _reload(self) -> None:
        """写盘之后让内存里的服务立刻用上新配置（与 `import_config` 同一条路径）。"""
        self.bridge.reload_config()

    # ------------------------------------------------------------------ #
    # Alter / Agency / 投递账本
    # ------------------------------------------------------------------ #

    async def alter(self, story_id: str = '') -> dict[str, Any]:
        """Alter 情绪：当前氛围位移、按来源分的桶、历史曲线与冷却状态。

        数据全在 `interlude_story.state.alter_system` 里（上游 `AlterSystemState`），
        不额外建表；控制台只是把它读出来画出来。
        """
        story = self._current_story(story_id)
        if not story:
            return {'story': None, 'state': None, 'history': [], 'pending': [], 'config': self._alter_config()}
        state = story.get('state') if isinstance(story.get('state'), dict) else {}
        raw_alter = state.get('alter_system', state.get('alterSystem'))
        alter_state = raw_alter if isinstance(raw_alter, dict) else {}
        raw_offset = alter_state.get('emotional_offset', alter_state.get('emotionalOffset'))
        offset = raw_offset if isinstance(raw_offset, dict) else None
        history = []
        for item in alter_state.get('history') or []:
            if not isinstance(item, dict):
                continue
            history.append({
                'turn': _int(item.get('turn')),
                'phase': _text(item.get('phase')),
                'alter': float(item.get('alter') or 0),
                'alter_value': float(item.get('alterValue', item.get('alter_value')) or 0),
                'timestamp': _text(item.get('timestamp')),
                'participant_id': _text(item.get('participantId', item.get('participant_id'))),
            })
        pending = []
        raw_pending = alter_state.get('pending_scopes', alter_state.get('pendingScopes'))
        for item in raw_pending or []:
            if not isinstance(item, dict):
                continue
            pending.append({
                'participant_id': _text(item.get('participantId', item.get('participant_id'))),
                'value': float(item.get('alterValue', item.get('alter_value')) or 0),
                'last_attempt_at': _text(item.get('lastAnalysisAttemptAt', item.get('last_analysis_attempt_at'))),
            })
        return {
            'story': self._story_brief(story),
            'state': {
                'value': float(alter_state.get('alter_value', alter_state.get('alterValue')) or 0),
                'weight': float(alter_state.get('alter_weight', alter_state.get('alterWeight')) or 0),
                'direction': _int(alter_state.get('last_trigger_direction', alter_state.get('lastTriggerDirection'))),
                'updated_at': _text(alter_state.get('last_updated_at', alter_state.get('lastUpdatedAt'))),
                'last_attempt_at': _text(alter_state.get('last_analysis_attempt_at', alter_state.get('lastAnalysisAttemptAt'))),
                'offset': {
                    'direction': _text(offset.get('direction')),
                    'description': _text(offset.get('description')),
                    'intensity': float(offset.get('intensity') or 0),
                    'generated_at': _text(offset.get('generated_at', offset.get('generatedAt'))),
                } if offset else None,
            },
            'history': history,
            'pending': pending,
            'config': self._alter_config(),
        }

    async def agency(self, story_id: str = '') -> dict[str, Any]:
        """Agency 行动窗口 + 日程预排摘要。"""
        story = self._current_story(story_id)
        if not story:
            return {'story': None, 'window': None, 'plan': None, 'config': self._agency_config()}
        state = story.get('state') if isinstance(story.get('state'), dict) else {}
        raw_window = state.get('agency_window', state.get('agencyWindow'))
        window = raw_window if isinstance(raw_window, dict) else None
        plan = self._schedule_plan(_text(story.get('id')))
        return {
            'story': self._story_brief(story),
            'window': {
                'activity_load': _text(window.get('activity_load', window.get('activityLoad'))),
                'privacy': _text(window.get('privacy')),
                'device_access': _text(window.get('device_access', window.get('deviceAccess'))),
                'next_opportunity_at': _text(window.get('next_opportunity_at', window.get('nextOpportunityAt'))),
                'valid_until': _text(window.get('valid_until', window.get('validUntil'))),
                'basis': _text(window.get('basis')),
                'source_entry_ids': window.get('source_entry_ids', window.get('sourceEntryIds')) or [],
                'updated_at': _text(window.get('updated_at', window.get('updatedAt'))),
            } if window else None,
            'plan': plan,
            'config': self._agency_config(),
        }

    async def delivery(self, story_id: str = '', limit: int = 300, status: str = '') -> dict[str, Any]:
        """投递账本：按剧本条目回放每一次行动的投递结果。

        账本存在 `script_entry.metadata.delivery_actions`（上游 M6.1），所以这里扫最近
        若干条带账本的条目，把 commit / event / segment 三层拍平成表格——页面要能回答
        "这条消息到底发出去了没有、哪个分段失败了"。
        """
        story = self._current_story(story_id)
        if not story:
            return {'story': None, 'actions': [], 'totals': {}, 'scanned': 0}
        database = self.bridge.db
        size = max(1, min(1000, _int(limit, 300)))
        rows = _safe_all(
            database, 'interlude_script_entry', {'storyId': _text(story.get('id'))},
            'occurredAt DESC', size,
        )
        wanted = _text(status).strip().lower()
        actions: list[dict[str, Any]] = []
        totals: dict[str, int] = {}
        for row in rows:
            metadata = row.get('metadata') if isinstance(row.get('metadata'), dict) else {}
            entries = metadata.get('delivery_actions')
            if not isinstance(entries, list):
                continue
            for action in entries:
                if not isinstance(action, dict):
                    continue
                segments = []
                for segment in action.get('segments') or []:
                    if not isinstance(segment, dict):
                        continue
                    segments.append({
                        'index': _int(segment.get('index')),
                        'kind': _text(segment.get('kind')),
                        'status': _text(segment.get('status')),
                        'attempts': _int(segment.get('attempts')),
                        'error': _text(segment.get('error'))[:200],
                    })
                state = _text(action.get('status'))
                totals[state] = totals.get(state, 0) + 1
                if wanted and state.lower() != wanted:
                    continue
                actions.append({
                    'entry_id': row.get('id'),
                    'occurred_at': _text(row.get('occurredAt')),
                    'commit_id': _text(action.get('commitId', action.get('commit_id'))),
                    'event_id': _text(action.get('eventId', action.get('event_id'))),
                    'status': state,
                    'target': _text(action.get('target') or action.get('channel')),
                    'attempts': _int(action.get('attempts')),
                    'updated_at': _text(action.get('updatedAt', action.get('updated_at'))),
                    'segments': segments,
                    'done': len([item for item in segments if item['status'] == 'delivered']),
                })
        return {
            'story': self._story_brief(story),
            'actions': actions,
            'totals': totals,
            'scanned': len(rows),
        }

    # ------------------------------------------------------------------ #
    # 聊天记录（按参与者 / 按群）
    # ------------------------------------------------------------------ #

    async def chats(self, story_id: str = '', scan: int = 2000) -> dict[str, Any]:
        """一条对话一行：私聊按参与者、群聊按群号。

        条目表里只有 `user-message` / `character-message` 是私聊，`group-message` /
        `character-group-message` 是群聊，`outgoing-delivery-failed` 是没送出去的主角
        消息。旁白（`script`）、场景与账本条目都不在这里出现：它们不属于任何一条对话。
        """
        story = self._current_story(story_id)
        if not story:
            return {'story': None, 'private': [], 'groups': [], 'scanned': 0, 'truncated': False}
        sid = _text(story.get('id'))
        rows, truncated = self._chat_rows(sid, scan)
        character = self._character_name(story)
        records = {_text(row.get('id')): row for row in self._participant_rows(sid)}
        names = {
            key: _text(row.get('displayName')) or key for key, row in records.items()
        }
        buckets: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            message = _chat_message(row, character, names)
            if message is not None:
                buckets.setdefault(message['conversation'], []).append(message)

        private: list[dict[str, Any]] = []
        groups: list[dict[str, Any]] = []
        for conversation, items in buckets.items():
            items.sort(key=lambda item: (_text(item.get('at')), _int(item.get('entry_id'))))
            counts = _chat_counts(items)
            last = items[-1]
            if conversation.startswith('group:'):
                group_id = conversation.split(':', 1)[1]
                groups.append({
                    'conversation': conversation, 'group_id': group_id,
                    'name': _text(last.get('group_name')) or self._group_label(group_id) or group_id,
                    **counts, 'last_at': _text(last.get('at')), 'last_text': last.get('text', '')[:200],
                })
                continue
            participant_id = conversation.split(':', 1)[1]
            record = records.get(participant_id) or {}
            state = record.get('state') if isinstance(record.get('state'), dict) else {}
            private.append({
                'conversation': conversation, 'participant_id': participant_id,
                'name': _text(record.get('displayName')) or participant_id,
                'account': _text(record.get('userId')),
                'platform': _text(record.get('platform')),
                'status': _text(record.get('status')),
                'unread': _int(state.get('unreadMessageCount')),
                'pending': _int(state.get('pendingReplyCount')),
                **counts, 'last_at': _text(last.get('at')), 'last_text': last.get('text', '')[:200],
            })
        seen = {item['group_id'] for item in groups}
        for group_id in self._configured_groups():
            if group_id in seen:
                continue
            groups.append({
                'conversation': 'group:%s' % group_id, 'group_id': group_id,
                'name': self._group_label(group_id) or group_id,
                **_chat_counts([]), 'last_at': '', 'last_text': '', 'configured': True,
            })

        def order(item: dict[str, Any]) -> tuple[str, str]:
            return (_text(item.get('last_at')), _text(item.get('name')))

        return {
            'story': self._story_brief(story),
            'private': sorted(private, key=order, reverse=True),
            'groups': sorted(groups, key=order, reverse=True),
            'scanned': len(rows),
            'truncated': truncated,
        }

    async def chat_history(
        self,
        story_id: str = '',
        conversation: str = '',
        limit: int = 200,
        before: str = '',
        scan: int = 2000,
    ) -> dict[str, Any]:
        """一条对话的往来记录（时间正序）。

        `before` 传上一页最早那条的 `at` 就是"加载更早"；`limit` 只影响返回条数，
        扫描窗口由 `scan` 控制（条目表的读取上限，页面会显示有没有被截断）。
        """
        story = self._current_story(story_id)
        if not story:
            return {'story': None, 'conversation': conversation, 'messages': [], 'has_more': False, 'scanned': 0}
        sid = _text(story.get('id'))
        target = _text(conversation).strip()
        character = self._character_name(story)
        if not target:
            return {
                'story': self._story_brief(story), 'conversation': '', 'title': '',
                'character': character, 'messages': [], 'has_more': False, 'scanned': 0,
            }
        rows, truncated = self._chat_rows(sid, scan)
        names = self._participant_names(sid)
        messages = [
            message for message in (_chat_message(row, character, names) for row in rows)
            if message is not None and message['conversation'] == target
        ]
        messages.sort(key=lambda item: (_text(item.get('at')), _int(item.get('entry_id'))))
        cutoff = _text(before)
        if cutoff:
            messages = [item for item in messages if _text(item.get('at')) < cutoff]
        size = max(1, min(1000, _int(limit, 200)))
        page = messages[-size:]
        if target.startswith('group:'):
            group_id = target.split(':', 1)[1]
            group_name = _text(page[-1].get('group_name')) if page else ''
            title = self._group_label(group_id) or group_name or group_id
        else:
            participant_id = target.split(':', 1)[1]
            title = names.get(participant_id) or participant_id
        return {
            'story': self._story_brief(story),
            'conversation': target,
            'title': title,
            'character': character,
            'messages': page,
            'has_more': len(messages) > len(page) or truncated,
            'scanned': len(rows),
            'truncated': truncated,
        }

    # ------------------------------------------------------------------ #
    # 共同作品（上游 rc28 `works.ts` 的界面；面板「作品」）
    #
    # 上游只有纯逻辑 + 存储抽象，service 层一行都没接——配置组、payload 与这里的
    # 面板入口都是本移植版补的。有一条语义不能含糊：**只有用户能接受 / 驳回**，
    # 她只能"提议"（模型侧的提案在 `chunk14.apply_work_proposal` 里落成待决）。
    # 所以这一页的写操作全是"用户动作"，没有"让模型自己接受"的入口。
    #
    # 取数走服务层（`chunk14` 的 `works_snapshot` / `shared_work_state`），拿不到就回
    # 空壳 + 说明（§29），绝不 500；写操作只在**用户看得懂的事实**上给 400
    # （未知作品 / 未知或已决提案 / 超长正文），其余异常也压成 400 文案。
    # ------------------------------------------------------------------ #

    async def works_overview(self, story_id: str = '') -> dict[str, Any]:
        """「作品」面板的清单：这部剧本里每个参与者的共同作品各一行。

        行里只放清单要用的东西（标题、当前版本号、待决提案数、运行中的写手任务数、
        最后修改时间）；正文在 `work_detail` 里给。`broken` 那一行代表"库里有这件作品
        但数据形状不被识别"——照实报出来，别让它静默消失。
        """
        config = self._works_config()
        enabled = bool(config['enabled'])
        service = self._works_service()
        story = self._current_story(story_id)
        if service is None:
            payload = self._works_shell('works_snapshot')
            payload['story'] = self._story_brief(story) if story else None
            payload['works'] = []
            return payload
        if not story:
            return {
                'available': True,
                'enabled': enabled,
                'generation_mode': _text(config.get('generation_mode')),
                'explain': self._works_explain(),
                'story': None,
                'works': [],
                'hint': '还没有剧本：先和她聊一句，剧本会自动建起来',
            }
        sid = _text(story.get('id'))
        names = self._participant_names(sid)
        works: list[dict[str, Any]] = []
        for row in _safe_all(self.bridge.db, 'interlude_work', {'storyId': sid}, None, WORK_ROW_LIMIT):
            if not isinstance(row, dict):
                continue
            participant_id = _text(row.get('participantId'))
            snapshot = await self._works_snapshot(service, sid, participant_id)
            works.append(self._work_brief(
                row, snapshot, participant_id,
                names.get(participant_id) or participant_id, enabled,
            ))
        works.sort(
            key=lambda item: (_text(item.get('updated_at')), _text(item.get('participant'))),
            reverse=True,
        )
        return {
            'available': True,
            'enabled': enabled,
            'generation_mode': _text(config.get('generation_mode')),
            'explain': self._works_explain(),
            'story': self._story_brief(story),
            'works': works,
            'hint': '',
        }

    async def work_detail(self, work_id: str) -> dict[str, Any]:
        """一件作品的全貌：当前正文、版本时间线、提案、写手任务、能不能起草。

        正文是**创作素材**，原样回，这里不做任何"安全改写"（上游把这条写进了提示词：
        文本本身绝不能被当成指令）。历史版本只带 `content_chars` + `preview`，
        只有 head 带全文——64 个版本 × 8000 字不该塞进一次面板响应。
        """
        config = self._works_config()
        enabled = bool(config['enabled'])
        service = self._works_service()
        wid = _text(work_id).strip()
        if service is None:
            payload = self._works_shell('works_snapshot')
            payload['work_id'] = wid
            return payload
        if not wid:
            raise ConsoleError('请选择一件共同作品')
        row = self._work_row(wid)
        if row is None:
            raise ConsoleError('找不到这件共同作品：%s' % wid)
        sid = _text(row.get('storyId'))
        participant_id = _text(row.get('participantId'))
        story = self._story_by_id(sid)
        record = self._participant_row(sid, participant_id)
        name = _text(record.get('displayName')) or participant_id
        snapshot = await self._works_snapshot(service, sid, participant_id)
        if snapshot is None:
            # 坏行：原数据一律保留、绝不覆盖（`core/works.py` 的硬校验），控制台照实说。
            return {
                'available': True,
                'enabled': enabled,
                'generation_mode': _text(config.get('generation_mode')),
                'explain': self._works_explain(),
                'broken': True,
                'work_id': wid,
                'story': self._story_brief(story) if story else None,
                'participant_id': participant_id,
                'participant': name,
                'title': '',
                'head': '',
                'generation': _int(row.get('generation')),
                'content': '',
                'content_chars': 0,
                'revision': 0,
                'revision_count': 0,
                'revisions': [],
                'proposals': [],
                'jobs': [],
                'pending_count': 0,
                'jobs_running': 0,
                'job_count': 0,
                'may_propose': False,
                'may_propose_reason': '这件作品的数据形状不被识别，先别动它',
                'last_failure': None,
                'limits': self._work_limits(),
                'hint': '这件作品的数据形状不被识别：原数据已保持原样，控制台没有做任何写入。',
            }
        state = self._work_state(row)
        revisions = [item for item in (snapshot.get('revisions') or []) if isinstance(item, dict)]
        proposals = [item for item in (snapshot.get('proposals') or []) if isinstance(item, dict)]
        jobs = [item for item in (snapshot.get('jobs') or []) if isinstance(item, dict)]
        head = _text(snapshot.get('head') or state.get('head'))
        head_revision = next((item for item in revisions if _text(item.get('id')) == head), None)
        content = _text((head_revision or {}).get('content'))
        projection = await self._works_state(service, sid, participant_id)
        may_propose, reason = self._work_may_propose(enabled, jobs, projection)
        # 服务层的投影比行内 state 更权威（`running` 但进程里没有的已经改标 `interrupted`），
        # 但它只在启用时存在；两边都拿不到就不编一条假的失败记录。
        last_failure = _record_or_none((projection or {}).get('lastFailure'))
        if last_failure is None:
            last_failure = _record_or_none(state.get('lastFailure'))
        return {
            'available': True,
            'enabled': enabled,
            'generation_mode': _text(config.get('generation_mode')),
            'explain': self._works_explain(),
            'broken': False,
            'work_id': wid,
            'story': self._story_brief(story) if story else None,
            'participant_id': participant_id,
            'participant': name,
            'title': _text(snapshot.get('title') or state.get('title')),
            'head': head,
            'generation': _int(snapshot.get('generation', row.get('generation'))),
            'content': content,
            'content_chars': len(content),
            'revision': self._revision_ordinal(revisions, head),
            'revision_count': len(revisions),
            'revisions': [
                self._revision_brief(item, index, head)
                for index, item in enumerate(revisions, 1)
            ],
            'proposals': [
                self._proposal_brief(item, self._revision_ordinal(revisions, _text(item.get('baseRevisionId'))))
                for item in proposals
            ],
            'jobs': [self._job_brief(item) for item in jobs],
            'pending_count': len([item for item in proposals if _text(item.get('status')) == 'pending']),
            'jobs_running': len([item for item in jobs if _text(item.get('status')) == 'running']),
            'job_count': len(jobs),
            'may_propose': may_propose,
            'may_propose_reason': reason,
            'last_failure': last_failure,
            'limits': self._work_limits(),
            'hint': '',
        }

    async def accept_work_proposal(self, work_id: str, proposal_id: str) -> dict[str, Any]:
        """接受一条提案 → 立刻多一个版本（**只有用户能做这件事**）。"""
        return await self._resolve_work_proposal(work_id, proposal_id, accept=True)

    async def reject_work_proposal(self, work_id: str, proposal_id: str) -> dict[str, Any]:
        """驳回一条提案 → 正文不动，只留一条结论（同样只有用户能做）。"""
        return await self._resolve_work_proposal(work_id, proposal_id, accept=False)

    async def create_work(
        self,
        story_id: Any = '',
        participant_id: Any = '',
        title: Any = '',
        content: Any = '',
    ) -> dict[str, Any]:
        """用户建**第一件**作品（面板「新建作品」）。

        为什么必须有这个入口：上游 `SharedWorks.create` 是整条链唯一的起点——保存提案 /
        手改 / 起草都要求作品已经存在，没有它面板就是"只能看不能开始"。

        「已有共同作品」时服务层会拒（**绝不覆盖**），那条文案原样透给用户。
        """
        service = self._works_service()
        member = getattr(service, 'create_work', None) if service is not None else None
        if not callable(member):
            return self._works_shell('create_work')
        sid = await self._story_id_for_write(story_id)
        pid = _text(participant_id).strip()
        if not pid:
            raise ConsoleError('请选择这件作品属于哪个私聊（参与者）')
        if not self._participant_exists(sid, pid):
            raise ConsoleError('这个参与者不在当前剧本里：%s（先让她和这个账号说过话）' % pid)
        name = _text(title)
        if not name.strip():
            raise ConsoleError('给这件作品起个标题')
        if _text_length(name) > WORK_TITLE_MAX:
            raise ConsoleError('标题最多 %d 字（当前 %d 字）' % (WORK_TITLE_MAX, _text_length(name)))
        text = content if isinstance(content, str) else ''
        if not text.strip():
            raise ConsoleError('作品正文不能为空')
        if _text_length(text) > WORK_CONTENT_MAX:
            raise ConsoleError('作品正文最多 %d 字（当前 %d 字）' % (WORK_CONTENT_MAX, _text_length(text)))
        try:
            result = await member(sid, pid, name, text)
        except Exception as error:  # noqa: BLE001
            raise ConsoleError('新建作品失败：%s' % error) from error
        self._require_work_ok(result, '新建作品')
        wid = _text((result or {}).get('workId')) if isinstance(result, dict) else ''
        if not wid:
            raise ConsoleError('新建作品失败：服务层没有返回 workId')
        payload = await self.work_detail(wid)
        payload['result'] = _work_result_brief(result)
        payload['changed'] = 'work-create %s' % wid
        return payload

    async def cancel_work_generation(self, work_id: Any, job_id: Any) -> dict[str, Any]:
        """取消一个还在跑的写手任务（迟到的结果会被丢弃）。

        `interrupted`（插件重启过、库里还留着 `running`）的任务也允许取消——它一直压着
        `mayPropose`，取消是把它放开的唯一入口。
        """
        service = self._works_service()
        member = getattr(service, 'cancel_work_generation', None) if service is not None else None
        if not callable(member):
            return self._works_shell('cancel_work_generation')
        wid, row = self._work_row_for_write(work_id)
        jid = _text(job_id).strip()
        if not jid:
            raise ConsoleError('请选择要取消的写手任务')
        sid = _text(row.get('storyId'))
        participant_id = _text(row.get('participantId'))
        snapshot = await self._works_snapshot(service, sid, participant_id)
        jobs = [
            item for item in ((snapshot or {}).get('jobs') or []) if isinstance(item, dict)
        ] or [
            item for item in (self._work_state(row).get('jobs') or []) if isinstance(item, dict)
        ]
        job = next((item for item in jobs if _text(item.get('id')) == jid), None)
        if job is None:
            raise ConsoleError('找不到这个写手任务：%s' % jid)
        status = _text(job.get('status'))
        if status not in ('running', 'interrupted'):
            raise ConsoleError('这个任务已经结束了（%s），不需要取消' % (status or '未知状态'))
        try:
            result = await member(sid, participant_id, jid)
        except Exception as error:  # noqa: BLE001
            raise ConsoleError('取消任务失败：%s' % error) from error
        self._require_work_ok(result, '取消任务')
        payload = await self.work_detail(wid)
        payload['changed'] = 'work-cancel %s' % jid
        return payload

    async def edit_work(self, work_id: str, content: Any, reason: Any = '') -> dict[str, Any]:
        """用户手改：登记一条用户提案并立即接受 → 一条新 revision（head 前移）。

        `reason` 是提案的理由（服务层要求非空、≤500 字）。界面把它当可选输入，
        所以留空时补一句"由用户手动修改"——**不伪造**，只是把"这是谁改的"说清楚。
        """
        service = self._works_service()
        member = getattr(service, 'edit_work', None) if service is not None else None
        if not callable(member):
            return self._works_shell('edit_work')
        wid, row = self._work_row_for_write(work_id)
        text = content if isinstance(content, str) else ''
        if not text.strip():
            raise ConsoleError('作品正文不能为空')
        if _text_length(text) > WORK_CONTENT_MAX:
            raise ConsoleError(
                '作品正文最多 %d 字（当前 %d 字）' % (WORK_CONTENT_MAX, _text_length(text)),
            )
        note = _text(reason).strip()
        if _text_length(note) > WORK_REASON_MAX:
            raise ConsoleError('修改理由最多 %d 字（当前 %d 字）' % (WORK_REASON_MAX, _text_length(note)))
        snapshot = await self._works_snapshot(service, _text(row.get('storyId')), _text(row.get('participantId')))
        head = _text((snapshot or {}).get('head'))
        if not head:
            raise ConsoleError('这件作品的数据形状不被识别，先别改它')
        edit = {
            'baseRevisionId': head,
            'content': text,
            'reason': note or '由用户手动修改',
        }
        try:
            result = await self._call_work_edit(
                member, _text(row.get('storyId')), _text(row.get('participantId')), edit,
            )
        except Exception as error:  # noqa: BLE001 - 写失败要说出来，不能假装成功
            raise ConsoleError('保存失败：%s' % error) from error
        self._require_work_ok(result, '保存')
        payload = await self.work_detail(wid)
        payload['result'] = _work_result_brief(result)
        payload['changed'] = 'work-edit %s' % wid
        return payload

    async def start_work_generation(self, work_id: str, brief: Any) -> dict[str, Any]:
        """「让她起草」：起一次异步写手任务（结果会作为**待决提案**回来）。"""
        service = self._works_service()
        member = getattr(service, 'start_work_generation', None) if service is not None else None
        if not callable(member):
            return self._works_shell('start_work_generation')
        wid, row = self._work_row_for_write(work_id)
        text = brief if isinstance(brief, str) else ''
        if not text.strip():
            raise ConsoleError('起草前先写一句创作意图')
        if _text_length(text) > WORK_BRIEF_MAX:
            raise ConsoleError('创作意图最多 %d 字（当前 %d 字）' % (WORK_BRIEF_MAX, _text_length(text)))
        sid = _text(row.get('storyId'))
        participant_id = _text(row.get('participantId'))
        snapshot = await self._works_snapshot(service, sid, participant_id)
        head = _text((snapshot or {}).get('head'))
        if not head:
            raise ConsoleError('这件作品的数据形状不被识别，先别让它起草')
        try:
            result = await self._call_work_generate(member, sid, participant_id, {
                'baseRevisionId': head,
                'brief': text,
            })
        except Exception as error:  # noqa: BLE001
            raise ConsoleError('起草任务没能开始：%s' % error) from error
        self._require_work_ok(result, '起草任务')
        payload = await self.work_detail(wid)
        payload['job'] = (result or {}).get('job') if isinstance(result, dict) else None
        payload['model_id'] = _text((result or {}).get('modelId')) if isinstance(result, dict) else ''
        payload['changed'] = 'work-generate %s' % wid
        return payload

    async def export_work(self, work_id: str) -> dict[str, Any]:
        """导出整件作品：`{parts, count}`，每段都在单条 QQ 消息的安全长度内。

        分段**由服务层的 `works_dump` 做**（`split_dump_parts`），控制台不再切一遍
        ——二次切分会把 `''.join(parts)` 的还原语义搞坏。
        """
        config = self._works_config()
        service = self._works_service()
        wid = _text(work_id).strip()
        if service is None:
            payload = self._works_shell('works_dump')
            payload.update({'work_id': wid, 'title': '', 'parts': [], 'count': 0, 'chars': 0})
            return payload
        if not wid:
            raise ConsoleError('请选择一件共同作品')
        row = self._work_row(wid)
        if row is None:
            raise ConsoleError('找不到这件共同作品：%s' % wid)
        sid = _text(row.get('storyId'))
        participant_id = _text(row.get('participantId'))
        snapshot = await self._works_snapshot(service, sid, participant_id)
        title = _text((snapshot or {}).get('title') or self._work_state(row).get('title'))
        parts = await self._works_dump(service, sid, participant_id)
        if not parts:
            return {
                'available': True,
                'enabled': bool(config['enabled']),
                'work_id': wid,
                'title': title,
                'parts': [],
                'count': 0,
                'chars': 0,
                'hint': '这件作品还没有正文，没有可导出的内容',
            }
        return {
            'available': True,
            'enabled': bool(config['enabled']),
            'work_id': wid,
            'title': title,
            'parts': parts,
            'count': len(parts),
            'chars': sum(len(part) for part in parts),
            'hint': '',
        }

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _chat_rows(self, story_id: str, scan: int) -> tuple[list[Any], bool]:
        """按类型分别取最近若干条聊天条目（别把整张条目表一次拉进内存）。"""
        size = max(50, min(20_000, _int(scan, 2000)))
        rows: list[Any] = []
        truncated = False
        for kind in CHAT_KINDS:
            found = _safe_all(
                self.bridge.db, 'interlude_script_entry',
                {'storyId': story_id, 'kind': kind}, 'occurredAt DESC', size,
            )
            if len(found) >= size:
                truncated = True
            rows.extend(row for row in found if isinstance(row, dict))
        return rows, truncated

    def _character_name(self, story: dict[str, Any]) -> str:
        setting = story.get('setting') if isinstance(story.get('setting'), dict) else {}
        character = setting.get('character') if isinstance(setting.get('character'), dict) else {}
        return _text(character.get('name')) or '角色'

    def _participant_rows(self, story_id: str) -> list[dict[str, Any]]:
        return [
            row for row in _safe_all(
                self.bridge.db, 'interlude_participant', {'storyId': story_id}, 'updatedAt DESC', 200,
            ) if isinstance(row, dict)
        ]

    def _participant_names(self, story_id: str) -> dict[str, str]:
        return {
            _text(row.get('id')): _text(row.get('displayName')) or _text(row.get('id'))
            for row in self._participant_rows(story_id)
        }

    def _participant_row(self, story_id: str, participant_id: str) -> dict[str, Any]:
        rows = _safe_all(
            self.bridge.db, 'interlude_participant',
            {'storyId': story_id, 'id': participant_id}, None, 1,
        )
        return rows[0] if rows and isinstance(rows[0], dict) else {}

    def _configured_groups(self) -> list[str]:
        section = self.bridge.section('qq_access')
        return [
            _text(_record(item).get('group_id') or _record(item).get('groupId'))
            for item in (section.get('group_chats') or section.get('groupChats') or [])
            if _text(_record(item).get('group_id') or _record(item).get('groupId'))
        ]

    def _group_label(self, group_id: str) -> str:
        section = self.bridge.section('qq_access')
        for item in (section.get('group_chats') or section.get('groupChats') or []):
            record = _record(item)
            if _text(record.get('group_id') or record.get('groupId')) == group_id:
                return _text(record.get('label')) or ''
        return ''

    def _current_story(self, story_id: str = '') -> Optional[dict[str, Any]]:
        stories = _safe_all(self.bridge.db, 'interlude_story', order='updatedAt DESC', limit=50)
        return self._pick_story(stories, story_id)

    def _alter_config(self) -> dict[str, Any]:
        section = self.bridge.section('alter_system')
        return {
            'enabled': section.get('enabled') is not False,
            'threshold': section.get('threshold'),
            'max_intensity': section.get('max_intensity', section.get('maxIntensity')),
            'decay': section.get('decay'),
            'cooldown_minutes': section.get('cooldown_minutes', section.get('cooldownMinutes')),
            'weight_step': section.get('weight_step', section.get('weightStep')),
        }

    def _agency_config(self) -> dict[str, Any]:
        section = self.bridge.section('agency')
        return {
            'enabled': section.get('enabled') is not False,
            'activity_load': section.get('activity_load', section.get('activityLoad')),
            'privacy': section.get('privacy'),
            'device_access': section.get('device_access', section.get('deviceAccess')),
            'window_hours': section.get('window_hours', section.get('windowHours')),
        }

    def _schedule_plan(self, story_id: str) -> Optional[dict[str, Any]]:
        rows = _safe_all(self.bridge.db, 'interlude_schedule_preplan', {'storyId': story_id}, None, 1)
        if not rows:
            return None
        row = rows[0]
        regimes = row.get('regimes') if isinstance(row.get('regimes'), list) else []
        exceptions = row.get('exceptions') if isinstance(row.get('exceptions'), list) else []
        days = row.get('materializedDays', row.get('materialized_days'))
        days = days if isinstance(days, list) else []
        return {
            'revision': _int(row.get('revision')),
            'timezone': _text(row.get('timezone')),
            'valid_from': _text(row.get('validFrom')),
            'valid_through': _text(row.get('validThrough')),
            'last_reviewed': _text(row.get('lastReviewedLocalDate')),
            'review_reason': _text(row.get('reviewReason'))[:300],
            'regimes': regimes[:12],
            'exceptions': exceptions[:12],
            'materialized_days': days[:14],
            'updated_at': _text(row.get('updatedAt')),
        }

    @staticmethod
    def _pick_story(stories: list[Any], story_id: str) -> Optional[dict[str, Any]]:
        cleaned = _text(story_id).strip()
        if cleaned:
            for item in stories:
                if _text(item.get('id')) == cleaned:
                    return item
        return stories[0] if stories else None

    def _story_brief(self, story: dict[str, Any]) -> dict[str, Any]:
        setting = story.get('setting') if isinstance(story.get('setting'), dict) else {}
        character = setting.get('character') if isinstance(setting.get('character'), dict) else {}
        state = story.get('state') if isinstance(story.get('state'), dict) else {}
        return {
            'id': _text(story.get('id')),
            'status': _text(story.get('status')),
            'platform': _text(story.get('platform')),
            'character': _text(character.get('name')),
            'user_name': _text((setting.get('user') or {}).get('name')) if isinstance(setting.get('user'), dict) else '',
            'timezone': _text(setting.get('timezone')),
            'cursor_at': _text(story.get('cursorAt')),
            'created_at': _text(story.get('createdAt')),
            'updated_at': _text(story.get('updatedAt')),
            'scene': _text((state.get('currentScene') or {}).get('hook')) if isinstance(state.get('currentScene'), dict) else '',
        }

    async def stories(self, limit: int = 30) -> dict[str, Any]:
        """剧本清单（控制台顶栏的「剧本」切换器用）。

        **为什么要有这个接口**：共享主剧本（上游 `sharedStoryConfig.enabled` 硬编码 true）
        下同一个角色只有一部活动剧本，但库里可能还有若干旧剧本（旧 beta 的"每 QQ 一部"
        遗留、以及被单剧本守卫归档的）。面板默认只显示"最近更新的那一部"，所以新用户
        一发消息，界面就跳到新剧本，让人以为"前面的剧本没了"。这里把库里的剧本全列出来
        （含 `archived`），供切换查看。
        """
        database = self.bridge.db
        size = max(1, min(50, _int(limit, 30)))
        rows = _safe_all(database, 'interlude_story', order='updatedAt DESC', limit=size)
        stories: list[dict[str, Any]] = []
        for row in rows:
            brief = self._story_brief(row)
            story_id = brief['id']
            brief['entries'] = _safe_count(database, 'interlude_script_entry', {'storyId': story_id})
            brief['participants'] = _safe_count(database, 'interlude_participant', {'storyId': story_id})
            brief['shared'] = story_id.startswith('character:')
            stories.append(brief)
        # `main` = 真正的共享主剧本（active 且 `character:`）；没有就是空串。
        # 注意跟 `active_story` 的区别：后者只是"面板当前读哪一部"（没有主剧本时是最近更新的
        # 那部旧剧本），控制台靠 `main` 决定显示「设为主剧本」还是「并入主剧本」。
        main = await self._shared_story_id(stories)
        for item in stories:
            item['main'] = item['id'] == main
        return {
            'stories': stories,
            'main': main,
            'active_story': _text(stories[0]['id']) if stories else '',
        }

    async def promote_story(self, source_story_id: Any) -> dict[str, Any]:
        """把选中的剧本立为共享主剧本（控制台「设为主剧本」）。

        什么时候需要它：共享主剧本的 id 由 `findStory` 在**收到消息**时才惰性迁移出来，
        而单剧本守卫（后台扫描/管理路径也会调用）可能先把别的剧本归档，于是库里会出现
        "好几部旧剧本、一部 `character:` 都没有"的中间态——这时保留哪一部当底座没法选。
        """
        service = getattr(self.bridge, 'service', None)
        if service is None or not callable(getattr(service, 'promote_story_to_canonical', None)):
            raise ConsoleError('插件服务尚未就绪，稍后再试')
        source = _text(source_story_id)
        if not source:
            raise ConsoleError('请选择要设为主剧本的那一部')
        try:
            result = await service.promote_story_to_canonical(source)
        except LookupError as error:
            raise ConsoleError('剧本不存在或已被清理：%s' % error) from error
        except ValueError as error:
            reason = _text(error)
            if reason == 'already-canonical':
                raise ConsoleError('这已经是主剧本了') from error
            if reason == 'canonical-exists':
                raise ConsoleError(
                    '已经有一部主剧本了：请用「并入主剧本」把这部并进去'
                    '（要换底座得先删掉现有主剧本，控制台不做这种毁数据的操作）',
                ) from error
            raise ConsoleError('不能设为主剧本：%s' % reason) from error
        payload = await self.stories()
        payload['promoted'] = {
            'source': source,
            'target': _text((result or {}).get('target')),
            'revived': bool((result or {}).get('revived')),
        }
        payload['changed'] = 'story-promote %s → %s' % (source, payload['promoted']['target'])
        return payload

    async def decide_patch(
        self, story_id: Any, patch_id: Any, action: Any, note: Any = '',
    ) -> dict[str, Any]:
        """审批（approve）或驳回（reject）一条设定改写候选。

        与自动闸门的关系：闸门保证"没证据不上"，用户拍板允许"证据够了但还没攒够回合"
        的那条直接生效，或者把已经生效的一条驳回撤下来。
        """
        service = getattr(self.bridge, 'service', None)
        if service is None or not callable(getattr(service, 'decide_state_patch', None)):
            raise ConsoleError('插件服务尚未就绪，稍后再试')
        sid = await self._story_id_for_write(story_id)
        decision = _text(action).strip().lower()
        if decision not in ('approve', 'reject'):
            raise ConsoleError('未知的操作：%s' % (_text(action) or '（空）'))
        try:
            result = await service.decide_state_patch(
                sid, _int_or_none(patch_id), decision, _text(note),
            )
        except ValueError as error:
            raise ConsoleError(_patch_error_text(_text(error))) from error
        payload = await self.memory(sid)
        payload['patch'] = result
        payload['changed'] = 'patch-%s #%s' % (decision, _text(result.get('id')))
        return payload

    async def rollback_patch(
        self, story_id: Any, patch_id: Any, note: Any = '',
    ) -> dict[str, Any]:
        """把一条已生效的设定改写候选撤下来（非破坏性）。"""
        service = getattr(self.bridge, 'service', None)
        if service is None or not callable(getattr(service, 'rollback_state_patch', None)):
            raise ConsoleError('插件服务尚未就绪，稍后再试')
        sid = await self._story_id_for_write(story_id)
        try:
            result = await service.rollback_state_patch(sid, _int_or_none(patch_id), _text(note))
        except ValueError as error:
            raise ConsoleError(_patch_error_text(_text(error))) from error
        payload = await self.memory(sid)
        payload['patch'] = result
        payload['changed'] = 'patch-rollback #%s' % _text(result.get('id'))
        return payload

    async def _story_id_for_write(self, story_id: Any) -> str:
        """写操作必须落在**存在的**剧本上，空 id 也要能解析成当前那部。"""
        stories = _safe_all(self.bridge.db, 'interlude_story', order='updatedAt DESC', limit=50)
        current = self._pick_story(stories, _text(story_id))
        if not current:
            raise ConsoleError('没有找到对应剧本')
        return _text(current.get('id'))

    async def merge_story(self, source_story_id: Any, target_story_id: Any = '') -> dict[str, Any]:
        """把一部旧剧本并入共享主剧本（控制台「并入主剧本」）。

        上游只在"该账号回来时那条旧分支还 active"时惰性合并，而单剧本守卫会在任何人
        发消息时先把其余 active 剧本归档（归档后不再合并）——所以升级到共享主剧本时，
        先被归档的那部剧本的内容会留在库里但不进主剧本。这个入口把用户选中的剧本
        显式并进去（条目 / 记忆 / 事实 / 场景 / 弧线等一起搬）。
        """
        service = getattr(self.bridge, 'service', None)
        if service is None or not callable(getattr(service, 'merge_story_into_canonical', None)):
            raise ConsoleError('插件服务尚未就绪，稍后再试')
        source = _text(source_story_id)
        if not source:
            raise ConsoleError('请选择要并入的剧本')
        target = _text(target_story_id)
        if not target:
            target = await self._shared_story_id()
        if not target:
            raise ConsoleError('还没有主剧本：先选一部点「设为主剧本」，再把其它旧剧本并进来')
        if target == source:
            raise ConsoleError('这就是当前主剧本，不需要并入')
        try:
            result = await service.merge_story_into_canonical(source, target)
        except LookupError as error:
            raise ConsoleError('剧本不存在或已被清理：%s' % error) from error
        except ValueError as error:
            raise ConsoleError('不能并入这部剧本：%s' % error) from error
        payload = await self.stories()
        payload['merged'] = {
            'source': source,
            'target': target,
            'participant': _text((result or {}).get('participant_id')),
            'moved': _int((result or {}).get('moved')),
        }
        payload['changed'] = 'story-merge %s → %s' % (source, target)
        return payload

    async def _shared_story_id(self, stories: Optional[list[dict[str, Any]]] = None) -> str:
        """真正的共享主剧本 id：**active 且 `character:`**，没有就空串。

        `canonical`/`active_story` 那套是"面板该读哪一部"的候选（没有主剧本时回落到
        最近更新的旧剧本）；这个只回答"主剧本到底定下来没有"。
        """
        rows = stories
        if rows is None:
            service = getattr(self.bridge, 'service', None)
            finder = getattr(service, 'shared_story_id', None)
            if callable(finder):
                try:
                    found = _text(await finder())
                except Exception:  # noqa: BLE001 - 取不到就回落到直接查库
                    found = ''
                if found:
                    return found
            rows = [self._story_brief(item) for item in _safe_all(
                self.bridge.db, 'interlude_story', order='updatedAt DESC', limit=50,
            )]
        for row in rows:
            story_id = _text(row.get('id'))
            if story_id.startswith('character:') and _text(row.get('status')) == 'active':
                return story_id
        return ''

    async def _canonical_story_id(self, stories: Optional[list[dict[str, Any]]] = None) -> str:
        """当前共享主剧本 id：优先 `character:…`，其次库里最新的 active 剧本。"""
        rows = stories
        if rows is None:
            service = getattr(self.bridge, 'service', None)
            finder = getattr(service, 'canonical_story_id', None)
            if callable(finder):
                try:
                    found = _text(await finder())
                except Exception:  # noqa: BLE001 - 取不到就回落到直接查库
                    found = ''
                if found:
                    return found
            rows = [self._story_brief(item) for item in _safe_all(
                self.bridge.db, 'interlude_story', order='updatedAt DESC', limit=50,
            )]
        for row in rows:
            if row.get('shared') or _text(row.get('id')).startswith('character:'):
                return _text(row.get('id'))
        return _text(rows[0].get('id')) if rows else ''

    def _flags(self) -> dict[str, Any]:
        bridge = self.bridge
        return {
            'allow_proactive_messages': bridge.config_flag(
                'runtime', 'allow_proactive_messages', 'allowProactiveMessages', default=False,
            ),
            'agency': bridge.config_flag('agency', 'enabled', default=True) is not False,
            'urge': bridge.config_flag('urge', 'enabled', default=False),
            'schedule_preplan': bridge.config_flag('schedule_preplan', 'enabled', default=False),
            'timeline_director': bridge.config_flag('timeline_director', 'enabled', default=False),
            'compaction': bridge.config_flag('compaction', 'enabled', default=True) is not False,
            'embedding': bridge.config_flag('embedding', 'enabled', default=False),
            'vision': bridge.config_flag('vision', 'enabled', default=False),
            'audio': bridge.config_flag('audio', 'enabled', default=False),
            'browser': bridge.config_flag('browser', 'enabled', default=False),
            'blind_mode': bool(getattr(bridge, 'blind_mode_enabled', False)),
        }

    def _routing_rows(self) -> list[dict[str, Any]]:
        """每个任务当前实际指向哪个模型。"""
        table = getattr(getattr(self.bridge, 'service', None), 'model_routing', None) or {}
        rows: list[dict[str, Any]] = []
        for key, label in CONSOLE_TASKS:
            route = table.get(key) if isinstance(table, dict) else None
            route = route if isinstance(route, dict) else {}
            providers = route.get('providers') if isinstance(route.get('providers'), list) else []
            first = providers[0] if providers and isinstance(providers[0], dict) else {}
            target = route.get('target') if isinstance(route.get('target'), dict) else {}
            bound = self.bridge.task_model_id(key)
            source = 'astrbot' if bound else ('connection' if first else 'none')
            rows.append({
                'task': key,
                'label': label,
                'source': source,
                'provider_label': _text(first.get('label')) or (f'AstrBot · {bound}' if bound else ''),
                'model': _text(target.get('model')) or _text(first.get('model')) or bound,
                'assigned': bool(route.get('assigned')),
                'available': bool(route.get('available')),
                'reason': _text(route.get('reason')),
                'candidates': len(providers),
            })
        return rows

    def _astrbot_providers(self) -> list[dict[str, Any]]:
        getter = getattr(self.bridge.context, 'get_all_providers', None)
        providers: list[Any] = []
        if callable(getter):
            try:
                providers = list(getter() or [])
            except Exception:  # noqa: BLE001
                providers = []
        rows = []
        for provider in providers:
            meta = getattr(provider, 'meta', None)
            identifier = ''
            if callable(meta):
                try:
                    identifier = _text(getattr(meta(), 'id', ''))
                except Exception:  # noqa: BLE001
                    identifier = ''
            config = getattr(provider, 'provider_config', None) or {}
            rows.append({
                'id': identifier,
                'model': _text(config.get('model')) if isinstance(config, dict) else '',
                'type': _text(config.get('type')) if isinstance(config, dict) else '',
                'provider_type': _text(config.get('provider_type')) if isinstance(config, dict) else '',
                'modalities': sorted(self.bridge.provider_modalities(provider)),
                'used_by': [
                    label for key, label in CONSOLE_TASKS if self.bridge.task_model_id(key) == identifier
                ],
            })
        return rows

    def _note(self, kind: str) -> str:
        try:
            return self.bridge.image_capability_note() if kind == 'image' else self.bridge.audio_capability_note()
        except Exception:  # noqa: BLE001
            return ''

    def _usage(self) -> dict[str, Any]:
        records = list(getattr(self.bridge, 'usage_records', []) or [])
        totals: dict[str, dict[str, Any]] = {}
        for item in records:
            task = _text(item.get('task')) or '未标注'
            bucket = totals.setdefault(task, {'task': task, 'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0, 'calls': 0})
            bucket['prompt_tokens'] += _int(item.get('prompt_tokens'))
            bucket['completion_tokens'] += _int(item.get('completion_tokens'))
            bucket['total_tokens'] += _int(item.get('total_tokens'))
            bucket['calls'] += 1
        ordered = sorted(totals.values(), key=lambda item: item['total_tokens'], reverse=True)
        return {
            'recent': list(reversed(records))[:40],
            'totals': ordered,
            'sum': {
                'calls': len(records),
                'prompt_tokens': sum(item['prompt_tokens'] for item in ordered),
                'completion_tokens': sum(item['completion_tokens'] for item in ordered),
                'total_tokens': sum(item['total_tokens'] for item in ordered),
            },
            'capacity': CONSOLE_USAGE_MAX,
        }

    def _entry_brief(self, entry: dict[str, Any]) -> dict[str, Any]:
        metadata = entry.get('metadata') if isinstance(entry.get('metadata'), dict) else {}
        content = _text(entry.get('content'))
        return {
            'id': entry.get('id'),
            'kind': _text(entry.get('kind')),
            'actor': _text(entry.get('actor')),
            'content': content[:600],
            'truncated': len(content) > 600,
            'occurred_at': _text(entry.get('occurredAt')),
            'commit_id': _text(metadata.get('commit_id')),
        }

    def _fact_brief(self, row: dict[str, Any]) -> dict[str, Any]:
        knowledge = row.get('knowledge') if isinstance(row.get('knowledge'), dict) else {}
        return {
            'id': row.get('id'),
            'scope': _text(row.get('scope')),
            'content': _text(row.get('content')),
            'importance': float(row.get('importance') or 0),
            'confidence': float(row.get('confidence') or 0),
            'status': _text(row.get('status')),
            'unresolved': bool(row.get('unresolved')),
            'knowledge_kind': _text(knowledge.get('kind')),
            'quote': _text(knowledge.get('quote'))[:200],
            'last_seen_at': _text(row.get('lastSeenAt')),
        }

    def _memory_brief(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            'id': row.get('id'),
            'category': _text(row.get('category')),
            'content': _text(row.get('content')),
            'importance': float(row.get('importance') or 0),
            'status': _text(row.get('status')),
            'updated_at': _text(row.get('updatedAt')),
        }

    def _intent_brief(self, row: dict[str, Any]) -> dict[str, Any]:
        kind = _text(row.get('type'))
        return {
            'id': row.get('id'),
            'type': kind,
            'summary': _text(row.get('summary')),
            'status': _text(row.get('status')),
            'not_before': _text(row.get('notBefore')),
            'internal': kind in INTERNAL_INTENT_TYPES,
        }

    def _patch_brief(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            'id': row.get('id'),
            'target': _text(row.get('target')),
            'path': _text(row.get('path')),
            'proposed_value': _text(row.get('proposedValue'))[:300],
            'confidence': float(row.get('confidence') or 0),
            'impact': _text(row.get('impact')),
            'status': _text(row.get('status')),
            'created_at': _text(row.get('createdAt')),
            'decided_at': _text(row.get('decidedAt')),
            'decision_note': _text(row.get('decisionNote'))[:200],
        }

    def _overlay_brief(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            'id': row.get('id'),
            'target': _text(row.get('target')),
            'tier': _text(row.get('tier')),
            'summary': _text(row.get('summary')),
            'period_end': _text(row.get('periodEnd')),
            'status': _text(row.get('status')),
        }

    def _participant_brief(self, row: dict[str, Any]) -> dict[str, Any]:
        state = row.get('state') if isinstance(row.get('state'), dict) else {}
        return {
            'id': _text(row.get('id')),
            'display_name': _text(row.get('displayName')),
            'platform': _text(row.get('platform')),
            'status': _text(row.get('status')),
            'relationship': _text(row.get('relationship'))[:200],
            'updated_at': _text(row.get('updatedAt')),
            'has_state': bool(state),
        }

    # ---- 共同作品：取服务、读行、把 state 翻成面板要的形状 ---- #

    def _works_service(self) -> Optional[Any]:
        """能干活的作品服务层；没有（旧版本 / 还没接线）就回 `None`。

        判据是**读快照这个成员在不在**：面板的取数与写操作都建立在"能读到这件作品"
        之上，缺了它就只剩空壳可回（§29）。
        """
        service = getattr(self.bridge, 'service', None)
        if service is None:
            return None
        if not callable(getattr(service, 'works_snapshot', None)):
            return None
        return service

    def _works_config(self) -> dict[str, Any]:
        """`works` 配置段（优先服务层归一化后的那份）。

        语义与 `chunk14.works_config()` 一致：缺键按默认（**关闭**），键存在时只有显式
        `false` 才算关（坑 36：别把 `0` / 缺失一律当"关闭"）。
        """
        service = getattr(self.bridge, 'service', None)
        reader = getattr(service, 'works_config', None)
        if callable(reader):
            try:
                data = reader()
                if isinstance(data, dict):
                    return data
            except Exception:  # noqa: BLE001 - 配置坏了不该让面板打不开
                pass
        try:
            section = self.bridge.section('works')
        except Exception:  # noqa: BLE001
            section = {}
        section = section if isinstance(section, dict) else {}
        value = section.get('enabled')
        return {
            'enabled': value is not False and value is not None,
            'generation_mode': _text(section.get('generation_mode') or section.get('generationMode')) or 'main',
            'model_id': _text(section.get('model_id') or section.get('modelId')),
        }

    def _works_explain(self) -> str:
        """服务层自己那句结论（`explain_works_state`）：现在是哪个模式、没生效是为什么。

        面板的空态与禁用提示直接用它，别在控制台重写一遍配置语义。
        """
        service = getattr(self.bridge, 'service', None)
        reader = getattr(service, 'explain_works_state', None)
        if not callable(reader):
            return ''
        try:
            return _text(reader()).strip()
        except Exception:  # noqa: BLE001
            return ''

    def _works_unavailable_hint(self, member: str = '') -> str:
        """空壳的说明文案：带上服务层自己那句"为什么没生效"（`explain_works_state`）。"""
        explain = self._works_explain()
        suffix = '（缺少 %s）' % member if member else ''
        return '%s%s%s' % (WORKS_UNAVAILABLE_HINT, suffix, ' ｜%s' % explain if explain else '')

    def _works_shell(self, member: str = '') -> dict[str, Any]:
        """服务层没就绪时的统一空壳（**不抛**：面板得能打开并说明原因）。"""
        config = self._works_config()
        return {
            'available': False,
            'enabled': bool(config['enabled']),
            'generation_mode': _text(config.get('generation_mode')),
            'explain': self._works_explain(),
            'hint': self._works_unavailable_hint(member),
        }

    async def _works_snapshot(self, service: Any, story_id: str, participant_id: str) -> Optional[dict[str, Any]]:
        """读一件作品的全貌；坏行 / 读取异常一律 `None`（原数据不动），不抛给前端。"""
        reader = getattr(service, 'works_snapshot', None)
        if not callable(reader) or not story_id or not participant_id:
            return None
        try:
            # 服务层的 `_entity_id` 认得 id 字符串（不必先取出整个剧本 / 参与者对象）。
            snapshot = await reader(story_id, participant_id)
        except Exception:  # noqa: BLE001 - 坏行是"读不出来"，不是"控制台出错"
            return None
        return snapshot if isinstance(snapshot, dict) else None

    async def _works_state(self, service: Any, story_id: str, participant_id: str) -> Optional[dict[str, Any]]:
        """payload 用的那份投影（`sharedWork`）：`mayPropose` / `lastFailure` 的权威来源。

        未启用 / 没有作品时服务层回 `None`，这里照收——面板不会凭空编出一个投影。
        """
        reader = getattr(service, 'shared_work_state', None)
        if not callable(reader) or not story_id or not participant_id:
            return None
        try:
            state = await reader(story_id, participant_id)
        except Exception:  # noqa: BLE001
            return None
        return state if isinstance(state, dict) else None

    async def _works_dump(self, service: Any, story_id: str, participant_id: str) -> list[str]:
        """分段导出：`works_dump` 已经是**分好段**的列表，这里只做形状归一。"""
        reader = getattr(service, 'works_dump', None)
        if not callable(reader):
            return []
        try:
            parts = await reader(story_id, participant_id)
        except Exception:  # noqa: BLE001
            return []
        if isinstance(parts, (list, tuple)):
            return [_text(part) for part in parts if _text(part)]
        if isinstance(parts, str) and parts:
            # 兜底：万一某版服务层回的是整串（不是本移植版的契约），这里补一次切分。
            return split_dump_parts(parts)
        return []

    async def _call_work_edit(self, member: Any, story_id: str, participant_id: str, edit: dict[str, Any]) -> Any:
        """调用户的 `edit_work`——服务层有两种可能的签字，按签名把参数放对位置。

        本移植版实际是 `edit_work(story, participant, edit)`（`edit` 是
        `{baseRevisionId, content, reason}` 对象，与上游 wire 形状一致）；契约摘要里
        写的是 `(story, participant, content, reason)`。两种都认，别把字典塞进正文位置。
        """
        if 'content' in _work_signature_names(member):
            return await member(story_id, participant_id, edit['content'], edit['reason'])
        return await member(story_id, participant_id, edit)

    async def _call_work_generate(self, member: Any, story_id: str, participant_id: str, request: dict[str, Any]) -> Any:
        """同上：`start_work_generation(story, participant, request|brief)` 两种签字都认。"""
        if 'brief' in _work_signature_names(member):
            return await member(story_id, participant_id, request['brief'])
        return await member(story_id, participant_id, request)

    def _work_row(self, work_id: str) -> Optional[dict[str, Any]]:
        """按主键读 `interlude_work` 的一行（控制台自己读行，写操作仍然交给服务层）。"""
        rows = _safe_all(self.bridge.db, 'interlude_work', {'id': work_id}, None, 1)
        return rows[0] if rows and isinstance(rows[0], dict) else None

    def _work_row_for_write(self, work_id: Any) -> tuple[str, dict[str, Any]]:
        """写操作的入口校验：件必须存在，且 id 不能空（未知 id 按本文件约定 400）。"""
        wid = _text(work_id).strip()
        if not wid:
            raise ConsoleError('请选择一件共同作品')
        row = self._work_row(wid)
        if row is None:
            raise ConsoleError('找不到这件共同作品：%s' % wid)
        return wid, row

    def _participant_exists(self, story_id: str, participant_id: str) -> bool:
        """这个参与者在这部剧本里登记过吗（表里一行都没有时不拦，别把旧库挡在门外）。"""
        rows = self._participant_rows(story_id)
        if not rows:
            return True
        return any(_text(row.get('id')) == participant_id for row in rows)

    def _story_by_id(self, story_id: str) -> Optional[dict[str, Any]]:
        """按 id 精确取剧本（**不用** `_current_story`：它取不到会回落到最近那一部）。"""
        if not story_id:
            return None
        rows = _safe_all(self.bridge.db, 'interlude_story', {'id': story_id}, None, 1)
        return rows[0] if rows and isinstance(rows[0], dict) else None

    @staticmethod
    def _work_state(row: dict[str, Any]) -> dict[str, Any]:
        """行里的 `state`（json 列已解码；万一拿到的是字符串就再解一次）。"""
        state = row.get('state') if isinstance(row, dict) else None
        if isinstance(state, str):
            try:
                state = json.loads(state)
            except (TypeError, ValueError):
                state = None
        return state if isinstance(state, dict) else {}

    @staticmethod
    def _revision_ordinal(revisions: list[Any], revision_id: str) -> int:
        """版本号（从 1 数）：head 在时间线里的位置；找不到就退回版本总数。"""
        for index, item in enumerate(revisions, 1):
            if isinstance(item, dict) and _text(item.get('id')) == revision_id:
                return index
        return len(revisions) if revision_id else 0

    def _work_brief(
        self,
        row: dict[str, Any],
        snapshot: Optional[dict[str, Any]],
        participant_id: str,
        name: str,
        enabled: bool,
    ) -> dict[str, Any]:
        """清单里的一行（快照拿不到也照出，`broken` 标出来）。"""
        state = self._work_state(row)
        source = snapshot if isinstance(snapshot, dict) else {}
        revisions = source.get('revisions') if isinstance(source.get('revisions'), list) else [
            item for item in (state.get('revisions') or []) if isinstance(item, dict)
        ]
        proposals = [
            item for item in (source.get('proposals') or []) if isinstance(item, dict)
        ] or [
            item for item in (state.get('proposals') or []) if isinstance(item, dict)
        ]
        jobs = [item for item in (source.get('jobs') or []) if isinstance(item, dict)] or [
            item for item in (state.get('jobs') or []) if isinstance(item, dict)
        ]
        head = _text(source.get('head') or state.get('head'))
        running = len([item for item in jobs if _text(item.get('status')) == 'running'])
        return {
            'work_id': _text(row.get('id')),
            'participant_id': participant_id,
            'participant': name or participant_id,
            'title': _text(source.get('title') or state.get('title')),
            'head': head,
            'revision': self._revision_ordinal(revisions, head),
            'revision_count': len(revisions),
            'pending_count': len([item for item in proposals if _text(item.get('status')) == 'pending']),
            'jobs_running': running,
            'job_count': len(jobs),
            'generation': _int(row.get('generation')),
            'updated_at': _work_updated_at(revisions, proposals, jobs),
            'may_propose': bool(enabled) and running == 0,
            'last_failure': _record_or_none(state.get('lastFailure')),
            'broken': snapshot is None,
        }

    def _revision_brief(self, item: dict[str, Any], ordinal: int, head: str) -> dict[str, Any]:
        """时间线里的一条版本：head 给全文，历史版本给长度 + 预览。"""
        content = _text(item.get('content'))
        revision_id = _text(item.get('id'))
        is_head = bool(head) and revision_id == head
        brief = {
            'id': revision_id,
            'ordinal': ordinal,
            'parent_id': _text(item.get('parentId')),
            'author': _text(item.get('author')),
            'proposal_id': _text(item.get('proposalId')),
            'created_at': _text(item.get('createdAt')),
            'current': is_head,
            'content_chars': len(content),
            'preview': content[:WORK_REVISION_PREVIEW],
        }
        if is_head:
            brief['content'] = content
        return brief

    def _proposal_brief(self, item: dict[str, Any], base_ordinal: int) -> dict[str, Any]:
        """提案卡：正文原样给（用户要能看到她到底想改成什么），理由与基础版本一起给。"""
        content = _text(item.get('content'))
        return {
            'id': _text(item.get('id')),
            'status': _text(item.get('status')),
            'pending': _text(item.get('status')) == 'pending',
            'author': _text(item.get('author')),
            'reason': _text(item.get('reason')),
            'content': content,
            'content_chars': len(content),
            'base_revision_id': _text(item.get('baseRevisionId')),
            'base_revision': base_ordinal,
            'created_at': _text(item.get('createdAt')),
            'source_entry_id': _int(item.get('sourceEntryId'), 0),
        }

    def _job_brief(self, item: dict[str, Any]) -> dict[str, Any]:
        """写手任务的一行（`interrupted` = 进程重载过，永远不会自己重放）。"""
        status = _text(item.get('status'))
        return {
            'id': _text(item.get('id')),
            'status': status,
            'interrupted': status == 'interrupted',
            'model_id': _text(item.get('modelId')),
            'brief': _text(item.get('brief')),
            'created_at': _text(item.get('createdAt')),
            'proposal_id': _text(item.get('proposalId')),
            'source_entry_id': _int(item.get('sourceEntryId'), 0),
        }

    def _work_may_propose(
        self,
        enabled: bool,
        jobs: list[dict[str, Any]],
        projection: Optional[dict[str, Any]],
    ) -> tuple[bool, str]:
        """能不能让她起草：服务层投影优先，拿不到就按"没有在跑的任务"自己判。

        返回 `(能不能, 不能的原因)`——界面明说原因，别只给一个灰按钮。
        """
        if not enabled:
            return False, '共同作品没启用：去「配置」页打开「共同作品」'
        if isinstance(projection, dict) and 'mayPropose' in projection:
            may = bool(projection.get('mayPropose'))
        else:
            may = not any(_text(item.get('status')) == 'running' for item in jobs)
        if may:
            return True, ''
        return False, '已有一次写手任务在跑：等她写完，或先取消那个任务'

    @staticmethod
    def _work_limits() -> dict[str, int]:
        return {'content': WORK_CONTENT_MAX, 'brief': WORK_BRIEF_MAX, 'reason': WORK_REASON_MAX}

    def _require_work_ok(self, result: Any, action: str) -> None:
        """服务层用 `{'ok': False, 'error': …}` 表达失败（不抛），转成用户看得懂的 400。"""
        if isinstance(result, dict) and result.get('ok') is not False:
            return
        reason = _text((result or {}).get('error')) if isinstance(result, dict) else ''
        raise ConsoleError('%s失败：%s' % (action, reason or '服务层没有给出原因'))

    async def _resolve_work_proposal(self, work_id: Any, proposal_id: Any, accept: bool) -> dict[str, Any]:
        """接受 / 驳回的公共路径。

        顺序刻意是"先自己看一眼，再交给服务层"：未知作品、未知提案、已经处理过的提案
        都能立刻给 400（本文件既有约定），服务层那边的 CAS / 基础版本校验照旧再兜一次。
        """
        label = '接受' if accept else '驳回'
        service = self._works_service()
        name = 'accept_work_proposal' if accept else 'reject_work_proposal'
        member = getattr(service, name, None) if service is not None else None
        if not callable(member):
            return self._works_shell(name)
        wid, row = self._work_row_for_write(work_id)
        pid = _text(proposal_id).strip()
        if not pid:
            raise ConsoleError('请选择一条提案')
        # 提案清单以服务层快照为准（它是解码 + 校验过的那份）；坏行才回落到裸 state。
        sid = _text(row.get('storyId'))
        snapshot = await self._works_snapshot(service, sid, _text(row.get('participantId')))
        proposals = [
            item for item in ((snapshot or {}).get('proposals') or []) if isinstance(item, dict)
        ] or [
            item for item in (self._work_state(row).get('proposals') or []) if isinstance(item, dict)
        ]
        proposal = next((item for item in proposals if _text(item.get('id')) == pid), None)
        if proposal is None:
            raise ConsoleError('找不到这条提案：%s' % pid)
        status = _text(proposal.get('status'))
        if status != 'pending':
            raise ConsoleError('这条提案已经处理过了（%s），结论不能改' % (status or '未知状态'))
        try:
            result = await member(wid, pid)
        except Exception as error:  # noqa: BLE001
            raise ConsoleError('%s提案失败：%s' % (label, error)) from error
        self._require_work_ok(result, '%s提案' % label)
        payload = await self.work_detail(wid)
        payload['result'] = _work_result_brief(result)
        payload['changed'] = 'work-%s %s' % ('accept' if accept else 'reject', pid)
        return payload

def _record_or_none(value: Any) -> Optional[dict[str, Any]]:
    """dict 或 `None`（`lastFailure` 只可能是对象；空对象也当"没有失败记录"）。"""
    return value if isinstance(value, dict) and value else None


def _work_updated_at(*groups: Any) -> str:
    """最后修改时间：版本 / 提案 / 任务里最新的那个 `createdAt`。

    `interlude_work` 表只有 `id` / `storyId` / `participantId` / `generation` / `state`，
    没有时间列（上游 `WorkRow` 也没有），所以"最后改过"只能从 state 里推。
    ISO 8601（UTC、Z 结尾）字符串可以直接比大小。
    """
    stamps = [
        _text(item.get('createdAt'))
        for group in groups
        for item in (group if isinstance(group, list) else [])
        if isinstance(item, dict) and _text(item.get('createdAt'))
    ]
    return max(stamps) if stamps else ''


def _text_length(value: str) -> int:
    """与 `core/works.py::_js_length` 同口径（UTF-16 码元）：控制台和 core 别各算一套。"""
    return len(value.encode('utf-16-le', errors='surrogatepass')) // 2


def _work_signature_names(member: Any) -> frozenset[str]:
    """服务层成员的参数名集合；拿不到签名就回空集（按本移植版的形状调）。"""
    try:
        return frozenset(inspect.signature(member).parameters)
    except (TypeError, ValueError):  # pragma: no cover - 内建 / C 实现没有签名
        return frozenset()


def _work_result_brief(result: Any) -> dict[str, Any]:
    """写操作的结果摘要：只挑几个键，别把整行塞进响应。"""
    data = result if isinstance(result, dict) else {}
    revision = data.get('revision') if isinstance(data.get('revision'), dict) else {}
    return {
        'work_id': _text(data.get('workId')),
        'head': _text(data.get('head')),
        'revisions': _int(data.get('revisions')),
        'revision_id': _text(revision.get('id')),
    }


def _int_or_none(value: Any) -> Any:
    """把控制台传来的 id 转成 int；转不动就原样回（让服务层报"找不到"）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def _patch_error_text(reason: str) -> str:
    """把服务层的错误码翻成用户看得懂的话。"""
    return {
        'patch-not-found': '这条设定候选不存在或已被清理',
        'already-compacted': '这条候选已经并进周期摘要，不能再回滚'
                             '（它已经成了她那段时间的经历；要改设定请走新的候选）',
        'not-applied': '只有已经生效的候选才能回滚',
        'invalid-action': '未知的操作',
        'story-not-found': '没有找到对应剧本',
    }.get(reason, '操作失败：%s' % reason)

