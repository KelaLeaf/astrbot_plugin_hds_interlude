"""端点注册表与剧本别名（上游 `src/endpoints.ts`，1.0.1-rc28 的 M1a/M1b/M2 身份层）。

纯策略模块（与 `group_willingness` / `world_seeder` 同构）：**无 IO、无副作用**，
状态由 service 持有（`interlude_endpoint` 表 + 内存端点状态）。

单平台零影响纪律：只建表、派生端点行、并轨解析与元数据标注——注册表只有派生单端点时，
一切解析结果必须与旧字段逐字节一致。

键名约定：本模块产出的**持久行**（`EndpointRow` / `StoryAliasRecord`）键名逐字
camelCase（进数据库 / 出 API）；**内存状态**（`EndpointState`）与**条目元数据**
（`channel_context_metadata`）用 snake_case（见坑 9），转 camelCase 只发生在 wire 边界
（`channel_context_payload`）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence, TypedDict

from .time import parse_dt

__all__ = [
    'ENDPOINT_CHANNEL_KINDS',
    'ENDPOINT_OWNER_KINDS',
    'channel_context_metadata',
    'channel_context_payload',
    'channel_kind_for_account',
    'derive_group_endpoint',
    'derive_participant_user_endpoint',
    'derive_story_role_endpoint',
    'endpoint_account_key',
    'endpoint_unique_key',
    'fresh_endpoint_state',
    'normalize_endpoint_row',
    'normalize_group_id',
    'normalize_story_alias_row',
    'resolve_inbound_endpoint',
    'resolve_story_alias',
    'state_after_connection',
    'state_after_inbound',
    'state_after_outbound',
]

ENDPOINT_CHANNEL_KINDS = ('qq', 'wechat')
ENDPOINT_OWNER_KINDS = ('story-role', 'participant-user', 'group')

#: 上游 `isOneBotFamilyPlatform` 的镜像副本（`endpoints.ts` 不得反向 import service，
#: 两处保持同步）。我方 service 侧的 `is_one_bot_platform` 是同一语义。
_ONE_BOT_FAMILY = ('onebot', 'napcat', 'qq:onebot')


class EndpointRow(TypedDict, total=False):
    """`interlude_endpoint` 持久行：描述符 + 管理配置（`enabled` 与动态状态分离）。"""

    id: str
    ownerKind: str
    ownerId: str
    channelKind: str
    platform: str
    accountKey: str
    selfId: str
    userId: str
    channelId: str
    groupId: str
    conversationKind: str
    enabled: bool
    createdAt: Any
    updatedAt: Any


def is_one_bot_family_platform(platform: Any) -> bool:
    """上游 `isOneBotFamilyPlatform`：OneBot 家族判定（大小写不敏感）。"""
    value = str(platform if platform is not None else '').lower()
    return value in _ONE_BOT_FAMILY or value.startswith(tuple(f'{item}:' for item in _ONE_BOT_FAMILY))


def endpoint_account_key(platform: Any, self_id: Any) -> str:
    """上游 `endpointAccountKey`：平台隔离的账号键。

    OneBot 家族折叠到 `onebot:` 前缀（历史行不变），原生平台（wechat 等）用专属前缀——
    不同平台同 `selfId` 不再碰撞。键本身已编码平台族，解析时的平台校验由键匹配隐式完成。
    """
    account = str(self_id or '').strip()
    if is_one_bot_family_platform(platform):
        return 'onebot:%s' % account
    return '%s:%s' % (str(platform or '').lower(), account)


def normalize_group_id(value: Any) -> str:
    """上游 `normalizedGroupId`：`group:123` / `guild:123` 与裸群号等价。

    派生与解析双侧都过这层，避免配置形态差异导致群端点永不命中。
    """
    return str(value if value is not None else '').strip().removeprefix('group:').removeprefix('guild:')


def _normalize_group_id_ci(value: Any) -> str:
    """同上的大小写不敏感版本（上游用 `/^(?:group|guild):/i`）。"""
    text = str(value if value is not None else '').strip()
    lowered = text.lower()
    for prefix in ('group:', 'guild:'):
        if lowered.startswith(prefix):
            return text[len(prefix):]
    return text


def endpoint_unique_key(row: Mapping[str, Any]) -> str:
    """上游 `endpointUniqueKey`：注册表防重复约束（地址可变，键随之校验，主键不变）。"""
    owner_kind = str(_get(row, 'ownerKind', 'owner_kind') or '')
    account_key = str(_get(row, 'accountKey', 'account_key') or '')
    owner_id = str(_get(row, 'ownerId', 'owner_id') or '')
    if owner_kind == 'story-role':
        return 'role:%s' % account_key
    if owner_kind == 'participant-user':
        return 'user:%s:%s:%s' % (
            owner_id, account_key, str(_get(row, 'userId', 'user_id') or ''),
        )
    return 'group:%s:%s:%s' % (
        account_key, str(_get(row, 'channelId', 'channel_id') or ''),
        str(_get(row, 'groupId', 'group_id') or ''),
    )


def derive_story_role_endpoint(story: Mapping[str, Any], now: Any = None) -> EndpointRow:
    """上游 `deriveStoryRoleEndpoint`：旧故事的单一角色端点。`id` 留空由持久层生成。"""
    stamp = now if now is not None else _utc_now()
    return {
        'id': '', 'ownerKind': 'story-role', 'ownerId': _str(_get(story, 'id')),
        'channelKind': 'qq', 'platform': _str(_get(story, 'platform')),
        'accountKey': endpoint_account_key(_get(story, 'platform'), _get(story, 'selfId', 'self_id')),
        'selfId': _str(_get(story, 'selfId', 'self_id')),
        'enabled': True, 'createdAt': stamp, 'updatedAt': stamp,
    }


def derive_participant_user_endpoint(participant: Mapping[str, Any], now: Any = None) -> EndpointRow:
    """上游 `deriveParticipantUserEndpoint`：旧参与者的单一用户端点。"""
    stamp = now if now is not None else _utc_now()
    return {
        'id': '', 'ownerKind': 'participant-user', 'ownerId': _str(_get(participant, 'id')),
        'channelKind': 'qq', 'platform': _str(_get(participant, 'platform')),
        'accountKey': endpoint_account_key(
            _get(participant, 'platform'), _get(participant, 'selfId', 'self_id'),
        ),
        'selfId': _str(_get(participant, 'selfId', 'self_id')),
        'userId': _str(_get(participant, 'userId', 'user_id')),
        'conversationKind': 'private',
        'enabled': True, 'createdAt': stamp, 'updatedAt': stamp,
    }


def derive_group_endpoint(story: Mapping[str, Any], rule: Mapping[str, Any], now: Any = None) -> EndpointRow:
    """上游 `deriveGroupEndpoint`：群规则的群端点（群规则是群端点的唯一配置来源）。"""
    stamp = now if now is not None else _utc_now()
    group_id = _normalize_group_id_ci(_get(rule, 'groupId', 'group_id'))
    return {
        'id': '', 'ownerKind': 'group', 'ownerId': group_id,
        'channelKind': 'qq', 'platform': _str(_get(story, 'platform')),
        'accountKey': endpoint_account_key(_get(story, 'platform'), _get(story, 'selfId', 'self_id')),
        'selfId': _str(_get(story, 'selfId', 'self_id')), 'groupId': group_id,
        'conversationKind': 'group',
        'enabled': True, 'createdAt': stamp, 'updatedAt': stamp,
    }


def normalize_endpoint_row(raw: Any) -> Optional[EndpointRow]:
    """上游 `normalizeEndpointRow`：防御性归一化，坏行返回 None（丢弃）。"""
    if not _is_record(raw):
        return None
    owner_kind = _str(_get(raw, 'ownerKind', 'owner_kind'))
    if owner_kind not in ENDPOINT_OWNER_KINDS:
        return None
    identifier = _str(_get(raw, 'id')).strip()
    account_key = _str(_get(raw, 'accountKey', 'account_key')).strip()
    owner_id = _str(_get(raw, 'ownerId', 'owner_id')).strip()
    if not identifier or not account_key or not owner_id:
        return None
    channel_kind = 'wechat' if _get(raw, 'channelKind', 'channel_kind') == 'wechat' else 'qq'
    conversation = _get(raw, 'conversationKind', 'conversation_kind')
    if conversation == 'group':
        conversation_kind: Optional[str] = 'group'
    elif conversation == 'private':
        conversation_kind = 'private'
    elif owner_kind == 'group':
        conversation_kind = 'group'
    elif owner_kind == 'participant-user':
        conversation_kind = 'private'
    else:
        conversation_kind = None
    row: EndpointRow = {
        'id': identifier, 'ownerKind': owner_kind, 'ownerId': owner_id,
        'channelKind': channel_kind, 'platform': _str(_get(raw, 'platform')) or 'onebot',
        'accountKey': account_key, 'selfId': _str(_get(raw, 'selfId', 'self_id')),
        'enabled': _get(raw, 'enabled') is not False,
        'createdAt': _date_value(_get(raw, 'createdAt', 'created_at')),
        'updatedAt': _date_value(_get(raw, 'updatedAt', 'updated_at')),
    }
    for camel, snake in (('userId', 'user_id'), ('channelId', 'channel_id'), ('groupId', 'group_id')):
        value = _get(raw, camel, snake)
        if value is not None and _str(value) != '':
            row[camel] = _str(value)  # type: ignore[literal-required]
    if conversation_kind is not None:
        row['conversationKind'] = conversation_kind
    return row


# --------------------------------------------------------------------------- #
# 入站反向解析（v3 §三）：accountKey → 端点行 → 故事，先于 story 查询
# --------------------------------------------------------------------------- #

def resolve_inbound_endpoint(rows: Sequence[Mapping[str, Any]], source: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """上游 `resolveInboundEndpoint`（纯函数）。边界行为：

    - `accountKey` 未注册 → `None`（调用方回落旧路径，**绝不自动挂载**）；
    - 重复行 → 首行生效，并把 `duplicate_role_account_keys` 暴露给调用方一次性告警；
    - 用户端点未命中 → 只返回角色端点。
    """
    account_key = endpoint_account_key(_get(source, 'platform'), _get(source, 'selfId', 'self_id'))
    role_matches = [
        row for row in rows
        if _get(row, 'ownerKind', 'owner_kind') == 'story-role'
        and _get(row, 'accountKey', 'account_key') == account_key
        and _get(row, 'enabled') is not False
    ]
    if not role_matches:
        return None
    resolution: dict[str, Any] = {'role_endpoint': role_matches[0]}
    if len(role_matches) > 1:
        # P2-10：重复角色端点是脏数据，不静默取首行。
        resolution['duplicate_role_account_keys'] = [account_key]
    group_id = _get(source, 'groupId', 'group_id')
    if group_id:
        normalized = _normalize_group_id_ci(group_id)
        group_endpoint = next((
            row for row in rows
            if _get(row, 'ownerKind', 'owner_kind') == 'group'
            and _get(row, 'accountKey', 'account_key') == account_key
            and _normalize_group_id_ci(_get(row, 'groupId', 'group_id')) == normalized
            and _get(row, 'enabled') is not False
        ), None)
        if group_endpoint is not None:
            resolution['group_endpoint'] = group_endpoint
    else:
        user_id = _get(source, 'userId', 'user_id')
        if user_id:
            user_endpoint = next((
                row for row in rows
                if _get(row, 'ownerKind', 'owner_kind') == 'participant-user'
                and _get(row, 'accountKey', 'account_key') == account_key
                and _get(row, 'userId', 'user_id') == _str(user_id)
                and _get(row, 'enabled') is not False
            ), None)
            if user_endpoint is not None:
                resolution['user_endpoint'] = user_endpoint
    return resolution


# --------------------------------------------------------------------------- #
# 条目通道上下文（v3 §十）：消除 kind 双义的完整结构
# --------------------------------------------------------------------------- #

def channel_context_metadata(endpoint: Mapping[str, Any], extra: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """上游 `channelContextMetadata` 的**持久化形态**（snake_case，见坑 9）。"""
    extra = extra if isinstance(extra, Mapping) else {}
    owner_kind = _get(endpoint, 'ownerKind', 'owner_kind')
    conversation = _get(endpoint, 'conversationKind', 'conversation_kind')
    if conversation is None:
        conversation = 'group' if owner_kind == 'group' else 'private'
    payload: dict[str, Any] = {
        'endpoint_id': _str(_get(endpoint, 'id')),
        'channel_kind': _str(_get(endpoint, 'channelKind', 'channel_kind')) or 'qq',
        'platform': _str(_get(endpoint, 'platform')),
        'account_key': _str(_get(endpoint, 'accountKey', 'account_key')),
        'self_id': _str(_get(endpoint, 'selfId', 'self_id')),
        'conversation_kind': conversation,
    }
    for camel, snake in (('channelId', 'channel_id'), ('userId', 'user_id'), ('groupId', 'group_id')):
        value = _get(extra, camel, snake)
        if value is not None and _str(value) != '':
            payload[snake] = _str(value)
    return payload


def channel_context_payload(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """把 `channel_context_metadata` 的结果转成 **wire camelCase**（发给模型时用）。"""
    mapping = (
        ('endpoint_id', 'endpointId'), ('channel_kind', 'channelKind'), ('account_key', 'accountKey'),
        ('self_id', 'selfId'), ('conversation_kind', 'conversationKind'),
        ('channel_id', 'channelId'), ('user_id', 'userId'), ('group_id', 'groupId'),
        ('platform', 'platform'),
    )
    payload: dict[str, Any] = {}
    for snake, camel in mapping:
        value = _get(metadata, snake, camel)
        if value is not None and _str(value) != '':
            payload[camel] = _str(value)
    return payload


# --------------------------------------------------------------------------- #
# EndpointState（v3 §四）：三维时效，过期即保守，重启归零
# --------------------------------------------------------------------------- #

def fresh_endpoint_state(endpoint_id: str, now: Any = None) -> dict[str, Any]:
    """上游 `freshEndpointState`：进程重启后的保守初值（一切未知按不可用处理）。"""
    stamp = int(now) if now is not None else int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    return {
        'endpoint_id': str(endpoint_id),
        'connection': {'online': False, 'observed_at': stamp},
        'deliverable': {'allowed': False, 'checked_at': stamp, 'note': 'fresh-start'},
    }


def state_after_connection(state: Mapping[str, Any], online: bool, now: Any = None) -> dict[str, Any]:
    """上游 `stateAfterConnection`。"""
    stamp = _now_ms(now)
    return {**state, 'connection': {'online': bool(online), 'observed_at': stamp}}


def state_after_inbound(state: Mapping[str, Any], now: Any = None) -> dict[str, Any]:
    """上游 `stateAfterInbound`：入站即证明连接在线且此刻可投递（也刷新 initiate）。"""
    stamp = _now_ms(now)
    updated: dict[str, Any] = {
        **state,
        'connection': {'online': True, 'observed_at': stamp},
        'deliverable': {'allowed': True, 'checked_at': stamp},
    }
    initiate = _get(state, 'initiate')
    if isinstance(initiate, Mapping):
        updated['initiate'] = {**initiate, 'allowed': True, 'observed_at': stamp}
    return updated


def state_after_outbound(
    state: Mapping[str, Any], ok: bool, note: str, cooldown_ms: int = 0, now: Any = None,
) -> dict[str, Any]:
    """上游 `stateAfterOutbound`。"""
    stamp = _now_ms(now)
    if ok:
        deliverable: dict[str, Any] = {'allowed': True, 'checked_at': stamp}
    else:
        deliverable = {'allowed': False, 'checked_at': stamp}
        if cooldown_ms > 0:
            deliverable['cooldown_until'] = stamp + int(cooldown_ms)
        deliverable['note'] = str(note)
    return {**state, 'deliverable': deliverable}


# --------------------------------------------------------------------------- #
# 剧本别名（M1b）：推导 ID → 稳定剧本 ID 的持久重定向
# --------------------------------------------------------------------------- #

def normalize_story_alias_row(raw: Any) -> Optional[dict[str, Any]]:
    """上游 `normalizeStoryAliasRow`：自指行（alias == canonical）直接丢弃。"""
    if not _is_record(raw):
        return None
    alias_story_id = _str(_get(raw, 'aliasStoryId', 'alias_story_id')).strip()
    canonical_story_id = _str(_get(raw, 'canonicalStoryId', 'canonical_story_id')).strip()
    if not alias_story_id or not canonical_story_id or alias_story_id == canonical_story_id:
        return None
    return {
        'aliasStoryId': alias_story_id,
        'canonicalStoryId': canonical_story_id,
        'reason': _str(_get(raw, 'reason'))[:255],
        'createdAt': _date_value(_get(raw, 'createdAt', 'created_at')),
    }


def resolve_story_alias(rows: Sequence[Mapping[str, Any]], alias_story_id: Any) -> dict[str, Any]:
    """上游 `resolveStoryAlias`：命中返回 canonical；链式（多跳）返回 `problem='chain'`。

    **不跟随多跳**——把裁决留给人。悬空（canonical 无对应故事）由调用方查库后判定。
    """
    target = _str(alias_story_id)
    row = next((
        item for item in rows
        if _str(_get(item, 'aliasStoryId', 'alias_story_id')) == target
    ), None)
    if row is None:
        return {}
    canonical = _str(_get(row, 'canonicalStoryId', 'canonical_story_id'))
    if any(_str(_get(item, 'aliasStoryId', 'alias_story_id')) == canonical for item in rows):
        return {'problem': 'chain'}
    return {'canonical_story_id': canonical}


def channel_kind_for_account(rows: Sequence[Mapping[str, Any]], account_key: str) -> str:
    """上游 `channelKindForAccount`：按 accountKey 判别通道类型（未注册默认 qq）。"""
    row = next((
        item for item in rows
        if _get(item, 'accountKey', 'account_key') == account_key and _get(item, 'enabled') is not False
    ), None)
    return 'wechat' if row is not None and _get(row, 'channelKind', 'channel_kind') == 'wechat' else 'qq'


# --------------------------------------------------------------------------- #
# 内部助手
# --------------------------------------------------------------------------- #

def _is_record(value: Any) -> bool:
    return isinstance(value, Mapping)


def _get(value: Any, *keys: str) -> Any:
    """双读：按顺序取第一个非 None 的键（优先上游 camelCase 拼写）。"""
    if not isinstance(value, Mapping):
        return None
    for key in keys:
        found = value.get(key)
        if found is not None:
            return found
    return None


def _str(value: Any) -> str:
    return '' if value is None else str(value)


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _now_ms(now: Any) -> int:
    if now is None:
        return int(_utc_now().timestamp() * 1000)
    if isinstance(now, datetime):
        return int(now.timestamp() * 1000)
    parsed = parse_dt(now)
    if parsed is not None:
        return int(parsed.timestamp() * 1000)
    return int(now)


def _date_value(value: Any) -> Any:
    """上游 `toDateValue`：非法值回落"现在"（持久层随后按 ISO 串写盘）。"""
    if isinstance(value, datetime):
        return value
    parsed = parse_dt(value)
    return parsed if parsed is not None else _utc_now()
