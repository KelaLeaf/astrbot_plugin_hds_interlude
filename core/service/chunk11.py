"""Chunk11 mixin：端点注册表与剧本别名（上游 1.0.1-rc28 的 M1a/M1b/M2/M3 服务层）。

纯策略在 `core/endpoints.py`；本文件只做数据库、内存状态与写队列。

上游对应成员（`src/service.ts:6961-7460`）：

| 上游 | 本文件 |
| --- | --- |
| `enqueueEndpointWrite` | `enqueue_endpoint_write` |
| `ensureEndpointRegistry` / `reconcileEndpointRegistry` | `ensure_endpoint_registry` / `reconcile_endpoint_registry` |
| `registerStoryRoleEndpointRow` / `registerParticipantUserEndpointRow` | 同名 snake 版 |
| `recordStoryAlias` | `record_story_alias` |
| `addStoryEndpoint` / `disableStoryEndpoint` | `add_story_endpoint` / `disable_story_endpoint` |
| `linkParticipantEndpoint` / `unlinkParticipantEndpoint` | `link_participant_endpoint` / `unlink_participant_endpoint` |
| `listParticipantEndpoints` / `listStoryEndpoints` | `list_participant_endpoints` / `list_story_endpoints` |
| `resolveInboundEndpointFor` / `touchEndpointStateInbound` | `resolve_inbound_endpoint_for` / `touch_endpoint_state_inbound` |
| `noteEndpointConnection` / `noteEndpointOutbound` | `note_endpoint_connection` / `note_endpoint_outbound` |
| `endpointAddressSync` / `resolveMostActiveEndpointId` / `narrativeEndpointSelection` | `endpoint_address_sync` / `resolve_most_active_endpoint_id` / `narrative_endpoint_selection` |

⚠️ `db_get` 的范围算子（`$in` / `$ne` …）在本移植版**显式抛错**（见 PORTING_NOTES 的
「范围算子查询退化」），所以上游那几处按状态过滤的查询在这里是「取全量 + Python 侧过滤」。
表很小（端点行数 = 账号数 + 参与者数 + 群数），代价可忽略。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Optional

from ..endpoints import (
    channel_context_metadata,
    derive_group_endpoint,
    derive_participant_user_endpoint,
    derive_story_role_endpoint,
    endpoint_account_key,
    endpoint_unique_key,
    fresh_endpoint_state,
    normalize_endpoint_row,
    normalize_story_alias_row,
    resolve_inbound_endpoint,
    resolve_story_alias,
    state_after_connection,
    state_after_inbound,
    state_after_outbound,
)
from ..time import dt_ms, iso
from ..token_stats import merge_usage, normalize_usage_record
from .base import ServiceBase, pick, story_id_for_character

__all__ = ['ServiceChunk11']

#: 上游 `endpointOwnerKind` 的三值（用于 `note_endpoint_outbound` 的过滤）。
_OWNER_KINDS = ('story-role', 'participant-user', 'group')


class ServiceChunk11(ServiceBase):
    """Chunk11 mixin：端点注册表 / 别名重定向 / 多通道投递选择。"""

    # ------------------------------------------------------------------ #
    # 写队列与注册表装载
    # ------------------------------------------------------------------ #

    def _endpoint_lock(self) -> asyncio.Lock:
        """延迟创建写队列锁（`asyncio.Lock` 必须在事件循环里创建才安全）。"""
        lock = getattr(self, '_endpoint_write_lock', None)
        if lock is None:
            lock = asyncio.Lock()
            self._endpoint_write_lock = lock
        return lock

    async def enqueue_endpoint_write(self, task: Any) -> Any:
        """上游 `enqueueEndpointWrite`：所有注册表变更**串行执行**，杜绝并发"查后写"重复。

        上游用 promise 链；这里用 `asyncio.Lock`（同一语义、异常也不断链）。
        """
        async with self._endpoint_lock():
            return await task()

    async def ensure_endpoint_registry(self, force: bool = False) -> None:
        """上游 `ensureEndpointRegistry`：单飞锁 + 幂等装载。"""
        if getattr(self, 'endpoint_registry_ready', False) and not force:
            return
        in_flight = getattr(self, '_endpoint_registry_inflight', None)
        if in_flight is not None:
            await in_flight
            return
        self._endpoint_registry_inflight = asyncio.ensure_future(self.reconcile_endpoint_registry())
        try:
            await self._endpoint_registry_inflight
        finally:
            self._endpoint_registry_inflight = None

    async def reconcile_endpoint_registry(self) -> None:
        """上游 `reconcileEndpointRegistry`：把库里已有的故事/参与者/群规则并轨成端点行。

        `active` 与 `paused` **都登记**（暂停故事的账号同样要能被 resume/status 与入站
        重定向找到）；`archived` 由迁移路径单独处理。
        """
        now = self.now()
        raw_rows = await self.db_get('interlude_endpoint', {})
        existing = [row for row in (normalize_endpoint_row(item) for item in raw_rows) if row]
        by_key = {endpoint_unique_key(row): row for row in existing}
        drafts: list[dict[str, Any]] = []

        def add_draft(draft: dict[str, Any]) -> None:
            key = endpoint_unique_key(draft)
            if key in by_key:
                return
            by_key[key] = draft
            drafts.append({**draft, 'id': uuid.uuid4().hex[:32]})

        stories = [
            row for row in await self.db_get('interlude_story', {})
            if pick(row, 'status') in ('active', 'paused')
        ]
        access = self._access_config() if hasattr(self, '_access_config') else {}
        group_rules = [
            rule for rule in (pick(access, 'groupChats', 'group_chats') or [])
            if isinstance(rule, dict) and pick(rule, 'enabled', 'enabled') is not False
        ]
        for story in stories:
            if not pick(story, 'id') or not pick(story, 'selfId', 'self_id'):
                continue
            add_draft(derive_story_role_endpoint(story, now))
            for rule in group_rules:
                add_draft(derive_group_endpoint(story, rule, now))
        for participant in await self.db_get('interlude_participant', {}):
            if pick(participant, 'status') != 'active':
                continue
            if pick(participant, 'id') and pick(participant, 'selfId', 'self_id') \
                    and pick(participant, 'userId', 'user_id'):
                add_draft(derive_participant_user_endpoint(participant, now))

        persisted: list[dict[str, Any]] = []
        for draft in drafts:
            # 先落库、成功才进内存——失败行不产生幽灵端点（重启即消失的假行）。
            try:
                await self.db_create('interlude_endpoint', draft)
                persisted.append(draft)
            except Exception as error:  # noqa: BLE001 - 一行失败不阻断整轮
                self.report_standalone(
                    'warn', '端点行写入失败，已跳过（不进内存，下次 reconcile 重试）键=%s 错误=%s',
                    endpoint_unique_key(draft), error,
                )
        if persisted:
            self.report_standalone_operation(
                'diagnostic', 'debug', '端点注册表迁移完成 新增=%d/%d 总数=%d',
                len(persisted), len(drafts), len(existing) + len(persisted),
            )
        self.endpoint_rows = [*existing, *persisted]
        states = getattr(self, 'endpoint_states', None) or {}
        for row in self.endpoint_rows:
            states.setdefault(row['id'], fresh_endpoint_state(row['id'], dt_ms(now)))
        self.endpoint_states = states

        # M1b：剧本别名迁移（与端点同一幂等通道）——为每个故事登记"按账号推导的 ID →
        # 既有剧本 ID"。故事主键保持不动（冻结的稳定角色 ID），推导形态一律经别名重定向。
        self.story_alias_rows = [
            row for row in (
                normalize_story_alias_row(item)
                for item in await self.db_get('interlude_story_alias', {})
            ) if row
        ]
        aliases_added = 0
        for story in stories:
            story_id = pick(story, 'id')
            self_id = pick(story, 'selfId', 'self_id')
            if not story_id or not self_id:
                continue
            derived = story_id_for_character(pick(story, 'platform') or 'onebot', str(self_id))
            if derived == story_id:
                continue
            if await self.record_story_alias(derived, story_id, 'M1b-stable-id'):
                aliases_added += 1
        if aliases_added:
            self.report_standalone_operation(
                'diagnostic', 'debug', '剧本别名迁移完成 新增=%d 总数=%d',
                aliases_added, len(self.story_alias_rows),
            )
        self.endpoint_registry_ready = True

    # ------------------------------------------------------------------ #
    # 运行期增量登记
    # ------------------------------------------------------------------ #

    async def register_story_role_endpoint_row(self, story: Any) -> None:
        """上游 `registerStoryRoleEndpointRow`：故事创建时同步登记角色端点。"""

        async def task() -> None:
            try:
                await self.ensure_endpoint_registry()
                now = self.now()
                draft = derive_story_role_endpoint(story, now)
                if any(endpoint_unique_key(row) == endpoint_unique_key(draft)
                       for row in self.endpoint_rows):
                    return
                row = {**draft, 'id': uuid.uuid4().hex[:32]}
                await self.db_create('interlude_endpoint', row)
                self.endpoint_rows.append(row)
                self.endpoint_states.setdefault(row['id'], fresh_endpoint_state(row['id'], dt_ms(now)))
                await self.record_story_alias(
                    story_id_for_character_of(story), pick(story, 'id'), 'story-created',
                )
            except Exception as error:  # noqa: BLE001 - 登记失败置脏，下次 reconcile 重试
                self.endpoint_registry_ready = False
                self.report_standalone(
                    'warn', '故事端点登记失败，注册表已置脏待重试 故事=%s 错误=%s',
                    pick(story, 'id'), error,
                )

        await self.enqueue_endpoint_write(task)

    async def register_participant_user_endpoint_row(self, participant: Any) -> None:
        """上游 `registerParticipantUserEndpointRow`：参与者创建时登记用户端点。"""

        async def task() -> None:
            try:
                await self.ensure_endpoint_registry()
                now = self.now()
                draft = derive_participant_user_endpoint(participant, now)
                if any(endpoint_unique_key(row) == endpoint_unique_key(draft)
                       for row in self.endpoint_rows):
                    return
                row = {**draft, 'id': uuid.uuid4().hex[:32]}
                await self.db_create('interlude_endpoint', row)
                self.endpoint_rows.append(row)
                self.endpoint_states.setdefault(row['id'], fresh_endpoint_state(row['id'], dt_ms(now)))
            except Exception as error:  # noqa: BLE001
                self.endpoint_registry_ready = False
                self.report_standalone(
                    'warn', '参与者端点登记失败，注册表已置脏待重试 参与者=%s 错误=%s',
                    pick(participant, 'id'), error,
                )

        await self.enqueue_endpoint_write(task)

    async def record_story_alias(self, alias_story_id: str, canonical_story_id: str, reason: str) -> bool:
        """上游 `recordStoryAlias`：幂等写入「推导 ID → 稳定剧本 ID」的重定向。

        自指（alias == canonical）直接拒绝；已存在的同向行算成功；**反向行会拒绝并
        保留原裁决**——双向别名会让 `resolve_story_alias` 永远判 `chain`。
        """
        alias = str(alias_story_id or '').strip()
        canonical = str(canonical_story_id or '').strip()
        if not alias or not canonical or alias == canonical:
            return False
        rows = getattr(self, 'story_alias_rows', None) or []
        for row in rows:
            if pick(row, 'aliasStoryId', 'alias_story_id') == alias:
                return pick(row, 'canonicalStoryId', 'canonical_story_id') == canonical
            if pick(row, 'aliasStoryId', 'alias_story_id') == canonical \
                    and pick(row, 'canonicalStoryId', 'canonical_story_id') == alias:
                self.report_standalone(
                    'warn', '剧本别名反向冲突，保留原裁决 推导=%s 既有=%s→%s', alias, canonical, alias,
                )
                return False
        record = {
            'aliasStoryId': alias, 'canonicalStoryId': canonical,
            'reason': str(reason or '')[:255], 'createdAt': self.now(),
        }
        try:
            await self.db_create('interlude_story_alias', record)
        except Exception as error:  # noqa: BLE001
            self.report_standalone('warn', '剧本别名写入失败 推导=%s 规范=%s 错误=%s', alias, canonical, error)
            return False
        self.story_alias_rows = [*rows, record]
        return True

    def resolve_story_alias_id(self, story_id: str) -> dict[str, Any]:
        """别名解析（纯函数在 `core/endpoints.py`）：命中返回 canonical，链式返回问题标记。"""
        return resolve_story_alias(getattr(self, 'story_alias_rows', None) or [], story_id)

    async def resolve_story_id_alias(self, alias_story_id: str) -> Optional[str]:
        """上游 `resolveStoryIdAlias`：解析推导 ID → 稳定剧本 ID（含两类问题告警）。

        - 链式（多跳）→ 记一次 warn 并返回 None（把裁决留给人）；
        - 悬空（canonical 查不到剧本）→ 记一次 warn 并返回 None（忽略重定向）。
        """
        if not alias_story_id:
            return None
        problems = getattr(self, '_story_alias_problems', None)
        if problems is None:
            problems = set()
            self._story_alias_problems = problems
        result = self.resolve_story_alias_id(alias_story_id)
        if result.get('problem') == 'chain':
            key = 'chain:%s' % alias_story_id
            if key not in problems:
                problems.add(key)
                self.report_standalone('warn', '剧本别名形成链式指向（双射失败，人工裁决）别名=%s', alias_story_id)
            return None
        canonical = result.get('canonical_story_id')
        if not canonical:
            return None
        rows = await self.db_get('interlude_story', {'id': canonical})
        if not rows:
            key = 'dangling:%s' % alias_story_id
            if key not in problems:
                problems.add(key)
                self.report_standalone(
                    'warn', '剧本别名指向不存在的剧本（悬空，忽略重定向）别名=%s 目标=%s',
                    alias_story_id, canonical,
                )
            return None
        return canonical

    def note_unknown_one_bot_account(self, platform: Any, self_id: Any) -> None:
        """上游 findStory 的「陌生账号」告警（同一个账号只喊一次）。"""
        problems = getattr(self, '_story_alias_problems', None)
        if problems is None:
            problems = set()
            self._story_alias_problems = problems
        key = 'unknown-account:%s:%s' % (platform, self_id)
        if key in problems:
            return
        problems.add(key)
        self.report_standalone(
            'warn',
            '未注册的 OneBot 账号发来消息，已拒绝挂载到主剧本（如需迁移账号请用 hdsi_story_endpoint add）'
            '账号=%s 平台=%s', self_id, platform,
        )

    async def remove_story_alias(self, alias_story_id: str, reason: str = 'manual-rollback') -> dict[str, Any]:
        """上游 `removeStoryAlias`：回滚一条别名（删行 + 在规范故事上写审计条目）。"""
        alias = str(alias_story_id or '').strip()
        rows = getattr(self, 'story_alias_rows', None) or []
        row = next((
            item for item in rows
            if pick(item, 'aliasStoryId', 'alias_story_id') == alias
        ), None)
        if row is None:
            return {'ok': False, 'error': '别名不存在：%s' % alias}
        canonical = pick(row, 'canonicalStoryId', 'canonical_story_id')
        try:
            await self.db_remove('interlude_story_alias', {'aliasStoryId': alias})
        except Exception as error:  # noqa: BLE001
            return {'ok': False, 'error': '回滚失败：%s' % error}
        self.story_alias_rows = [
            item for item in rows if pick(item, 'aliasStoryId', 'alias_story_id') != alias
        ]
        await self._endpoint_migration_entry(
            canonical,
            '[通道迁移] 已回滚剧本别名 %s → %s（原因：%s）' % (alias, canonical, reason),
            {'alias_rollback': True, 'alias_story_id': alias},
        )
        self.report_standalone_operation('standard', 'warn', '剧本别名已回滚 别名=%s 原因=%s', alias, reason)
        return {'ok': True}

    def list_story_aliases(self) -> list[dict[str, Any]]:
        """上游 `listStoryAliases`（M1b 只读视图）。"""
        return list(getattr(self, 'story_alias_rows', None) or [])

    # ------------------------------------------------------------------ #
    # 管理入口（6 条命令用）
    # ------------------------------------------------------------------ #

    async def add_story_endpoint(
        self, story: Any, platform: str, self_id: str, channel_kind: str = 'qq',
    ) -> dict[str, Any]:
        """上游 `addStoryEndpoint`：给故事注册第二（或更多）角色端点。"""
        account = str(self_id or '').strip()
        if not account:
            return {'ok': False, 'error': '账号不能为空。'}

        async def task() -> dict[str, Any]:
            await self.ensure_endpoint_registry()
            now = self.now()
            account_key = endpoint_account_key(platform, account)
            conflict = next((
                row for row in self.endpoint_rows
                if pick(row, 'ownerKind', 'owner_kind') == 'story-role'
                and pick(row, 'accountKey', 'account_key') == account_key
            ), None)
            if conflict is not None:
                if pick(conflict, 'ownerId', 'owner_id') == pick(story, 'id'):
                    return {'ok': True, 'endpointId': pick(conflict, 'id'), 'error': '该账号已注册为本故事端点。'}
                return {
                    'ok': False,
                    'error': '账号 %s 已被故事 %s 注册（唯一键 role:%s）。'
                             % (account, pick(conflict, 'ownerId', 'owner_id'), account_key),
                }
            row = {
                'id': uuid.uuid4().hex[:32], 'ownerKind': 'story-role', 'ownerId': pick(story, 'id'),
                'channelKind': 'wechat' if channel_kind == 'wechat' else 'qq',
                'platform': platform, 'accountKey': account_key, 'selfId': account,
                'enabled': True, 'createdAt': now, 'updatedAt': now,
            }
            try:
                await self.db_create('interlude_endpoint', row)
            except Exception as error:  # noqa: BLE001
                self.report_standalone('warn', '角色端点写入失败 故事=%s 账号=%s 错误=%s', pick(story, 'id'), account, error)
                return {'ok': False, 'error': '写入失败：%s' % error}
            self.endpoint_rows.append(row)
            self.endpoint_states.setdefault(row['id'], fresh_endpoint_state(row['id'], dt_ms(now)))
            # 第二端点的推导 ID 同步登记别名——其消息经 M1b 重定向直达本故事。
            await self.record_story_alias(
                story_id_for_character(platform, account), pick(story, 'id'), 'endpoint-added',
            )
            await self._endpoint_migration_entry(
                pick(story, 'id'), '[通道迁移] 角色端点已注册：%s 账号 %s（%s）'
                % (platform, account, row['channelKind']),
                {'endpoint_added': True, 'endpoint_id': row['id']},
            )
            self.report_standalone_operation(
                'standard', 'warn', '角色端点已注册 故事=%s 平台=%s 账号=%s 端点=%s',
                pick(story, 'id'), platform, account, row['id'],
            )
            return {'ok': True, 'endpointId': row['id']}

        return await self.enqueue_endpoint_write(task)

    async def disable_story_endpoint(self, endpoint_id: str) -> dict[str, Any]:
        """上游 `disableStoryEndpoint`：停用（身份与历史保留；最后一个启用端点不许停）。"""
        await self.ensure_endpoint_registry()
        row = next((
            item for item in self.endpoint_rows
            if pick(item, 'id') == endpoint_id
            and pick(item, 'ownerKind', 'owner_kind') == 'story-role'
        ), None)
        if row is None:
            return {'ok': False, 'error': '端点不存在：%s' % endpoint_id}
        if not pick(row, 'enabled'):
            return {'ok': True, 'error': '该端点已处于停用状态。'}
        enabled = [
            item for item in self.endpoint_rows
            if pick(item, 'ownerKind', 'owner_kind') == 'story-role'
            and pick(item, 'ownerId', 'owner_id') == pick(row, 'ownerId', 'owner_id')
            and pick(item, 'enabled')
        ]
        if len(enabled) <= 1:
            return {'ok': False, 'error': '不能停用故事的最后一个启用端点。'}
        try:
            await self.db_set('interlude_endpoint', {'id': endpoint_id}, {'enabled': False, 'updatedAt': self.now()})
        except Exception:  # noqa: BLE001 - 与上游一致：写失败也不阻断内存翻转
            pass
        row['enabled'] = False
        self.report_standalone_operation(
            'standard', 'warn', '角色端点已停用 故事=%s 账号=%s 端点=%s',
            pick(row, 'ownerId', 'owner_id'), pick(row, 'selfId', 'self_id'), endpoint_id,
        )
        return {'ok': True}

    async def link_participant_endpoint(
        self, participant: Any, platform: str, user_id: str,
    ) -> dict[str, Any]:
        """上游 `linkParticipantEndpoint`：把同一个人的另一个号链入既有参与者（幂等）。"""
        account = str(user_id or '').strip()
        if not account:
            return {'ok': False, 'error': '用户 ID 不能为空。'}
        await self.ensure_endpoint_registry()
        account_key = endpoint_account_key(
            platform, self.resolve_role_account_key(pick(participant, 'storyId', 'story_id'), platform),
        )
        existing = next((
            row for row in self.endpoint_rows
            if pick(row, 'ownerKind', 'owner_kind') == 'participant-user'
            and pick(row, 'accountKey', 'account_key') == account_key
            and str(pick(row, 'userId', 'user_id') or '') == account
        ), None)
        if existing is not None:
            if pick(existing, 'ownerId', 'owner_id') == pick(participant, 'id'):
                return {'ok': True, 'endpointId': pick(existing, 'id'), 'error': '该用户端点已链接到此参与者。'}
            return {'ok': False, 'error': '用户 %s 已链接到参与者 %s——一个人格一处。'
                                          % (account, pick(existing, 'ownerId', 'owner_id'))}
        now = self.now()
        row = {
            'id': uuid.uuid4().hex[:32], 'ownerKind': 'participant-user', 'ownerId': pick(participant, 'id'),
            'channelKind': 'qq', 'platform': platform, 'accountKey': account_key,
            'selfId': account_key.split(':', 1)[1] if ':' in account_key else account_key,
            'userId': account, 'conversationKind': 'private',
            'enabled': True, 'createdAt': now, 'updatedAt': now,
        }
        try:
            await self.db_create('interlude_endpoint', row)
        except Exception as error:  # noqa: BLE001
            self.report_standalone('warn', '用户端点写入失败 参与者=%s 用户=%s 错误=%s', pick(participant, 'id'), account, error)
            return {'ok': False, 'error': '写入失败：%s' % error}
        self.endpoint_rows.append(row)
        self.endpoint_states.setdefault(row['id'], fresh_endpoint_state(row['id'], dt_ms(now)))
        display = pick(participant, 'displayName', 'display_name') or pick(participant, 'id')
        await self._endpoint_migration_entry(
            pick(participant, 'storyId', 'story_id'),
            '[通道迁移] 用户端点已链接：%s %s → 参与者 %s' % (platform, account, display),
            {'endpoint_linked': True, 'endpoint_id': row['id']},
        )
        self.report_standalone_operation(
            'standard', 'warn', '用户端点已链接 参与者=%s 平台=%s 用户=%s 端点=%s',
            pick(participant, 'id'), platform, account, row['id'],
        )
        return {'ok': True, 'endpointId': row['id']}

    async def unlink_participant_endpoint(self, endpoint_id: str) -> dict[str, Any]:
        """上游 `unlinkParticipantEndpoint`：解除链接（可撤销；身份与历史保留）。"""
        await self.ensure_endpoint_registry()
        row = next((
            item for item in self.endpoint_rows
            if pick(item, 'id') == endpoint_id
            and pick(item, 'ownerKind', 'owner_kind') == 'participant-user'
        ), None)
        if row is None:
            return {'ok': False, 'error': '用户端点不存在：%s' % endpoint_id}
        if not pick(row, 'enabled'):
            return {'ok': True, 'error': '该端点已处于解除状态。'}
        try:
            await self.db_set('interlude_endpoint', {'id': endpoint_id}, {'enabled': False, 'updatedAt': self.now()})
        except Exception:  # noqa: BLE001
            pass
        row['enabled'] = False
        owner_id = str(pick(row, 'ownerId', 'owner_id') or '')
        participant = await self.get_participant_by_id(owner_id) if hasattr(self, 'get_participant_by_id') else None
        story_id = pick(participant, 'storyId', 'story_id') if participant else ''
        if story_id:
            await self._endpoint_migration_entry(
                story_id,
                '[通道迁移] 用户端点已解除链接：%s %s（端点 %s）'
                % (pick(row, 'platform'), pick(row, 'userId', 'user_id'), endpoint_id),
                {'endpoint_unlinked': True, 'endpoint_id': endpoint_id},
            )
        self.report_standalone_operation('standard', 'warn', '用户端点已解除 端点=%s 参与者=%s', endpoint_id, owner_id)
        return {'ok': True}

    def list_participant_endpoints(self, participant_id: str) -> list[dict[str, Any]]:
        """上游 `listParticipantEndpoints`。"""
        return [
            {
                'endpointId': pick(row, 'id'), 'platform': pick(row, 'platform'),
                'userId': pick(row, 'userId', 'user_id') or '', 'enabled': bool(pick(row, 'enabled')),
            }
            for row in getattr(self, 'endpoint_rows', [])
            if pick(row, 'ownerKind', 'owner_kind') == 'participant-user'
            and pick(row, 'ownerId', 'owner_id') == participant_id
        ]

    def list_story_endpoints(self, story_id: str) -> list[dict[str, Any]]:
        """上游 `listStoryEndpoints`（带在线状态与最近入站时间）。"""
        states = getattr(self, 'endpoint_states', {}) or {}
        listed: list[dict[str, Any]] = []
        for row in getattr(self, 'endpoint_rows', []):
            if pick(row, 'ownerKind', 'owner_kind') != 'story-role' or pick(row, 'ownerId', 'owner_id') != story_id:
                continue
            state = states.get(pick(row, 'id')) or {}
            connection = pick(state, 'connection') or {}
            listed.append({
                'endpointId': pick(row, 'id'), 'platform': pick(row, 'platform'),
                'selfId': pick(row, 'selfId', 'self_id'), 'channelKind': pick(row, 'channelKind', 'channel_kind'),
                'enabled': bool(pick(row, 'enabled')),
                'online': pick(connection, 'online') is True,
                'lastInboundAt': pick(connection, 'observedAt', 'observed_at'),
            })
        return listed

    def resolve_role_account_key(self, story_id: str, platform: str) -> str:
        """上游 `resolveRoleAccountKey`：取该故事任一启用角色端点的 `selfId`。

        上游在无端点时用 `platform` 兜底（`endpointAccountKey` 折叠后等效）。
        """
        role = next((
            row for row in getattr(self, 'endpoint_rows', [])
            if pick(row, 'ownerKind', 'owner_kind') == 'story-role'
            and pick(row, 'ownerId', 'owner_id') == story_id
            and pick(row, 'enabled')
        ), None)
        if role is not None:
            return str(pick(role, 'selfId', 'self_id') or '')
        return str(platform or '')

    # ------------------------------------------------------------------ #
    # 入站解析与端点状态
    # ------------------------------------------------------------------ #

    async def resolve_inbound_endpoint_for(self, source: Any) -> Optional[dict[str, Any]]:
        """上游 `resolveInboundEndpointFor`：未注册返回 None（回落旧路径）。"""
        await self.ensure_endpoint_registry()
        return resolve_inbound_endpoint(getattr(self, 'endpoint_rows', []), source)

    async def channel_metadata_for(self, source: Any, extra: Any = None) -> Optional[dict[str, Any]]:
        """上游 `channelMetadataFor`：注册表命中才返回（未迁移/陌生账号不标注）。"""
        resolution = await self.resolve_inbound_endpoint_for(source)
        if not resolution:
            return None
        endpoint = resolution.get('group_endpoint') or resolution.get('user_endpoint') \
            or resolution.get('role_endpoint')
        return channel_context_metadata(endpoint, extra)

    async def touch_endpoint_state_inbound(self, source: Any) -> None:
        """上游 `touchEndpointStateInbound`：入站即连接在线 + 可投递（并暴露重复端点）。"""
        resolution = await self.resolve_inbound_endpoint_for(source)
        if not resolution:
            return
        duplicates = resolution.get('duplicate_role_account_keys') or []
        if duplicates:
            warned = getattr(self, '_endpoint_drift_warned', None)
            if warned is None:
                warned = set()
                self._endpoint_drift_warned = warned
            key = 'dup-role:%s' % ','.join(duplicates)
            if key not in warned:
                warned.add(key)
                self.report_standalone(
                    'warn',
                    '检测到重复角色端点（脏数据，取首行生效，请人工核查 interlude_endpoint）账号键=%s',
                    ', '.join(duplicates),
                )
        stamp = dt_ms(self.now())
        states = self.endpoint_states
        for endpoint in (
            resolution.get('role_endpoint'), resolution.get('user_endpoint'),
            resolution.get('group_endpoint'),
        ):
            if not endpoint:
                continue
            previous = states.get(pick(endpoint, 'id')) or fresh_endpoint_state(pick(endpoint, 'id'), stamp)
            states[pick(endpoint, 'id')] = state_after_inbound(previous, stamp)

    def note_endpoint_connection(self, account_key: str, online: bool) -> None:
        """上游 `noteEndpointConnection`：连接器在线状态回写（connection 维）。"""
        stamp = dt_ms(self.now())
        states = getattr(self, 'endpoint_states', None)
        if states is None:
            return
        for row in getattr(self, 'endpoint_rows', []):
            if pick(row, 'accountKey', 'account_key') != account_key:
                continue
            previous = states.get(pick(row, 'id')) or fresh_endpoint_state(pick(row, 'id'), stamp)
            states[pick(row, 'id')] = state_after_connection(previous, online, stamp)

    def note_endpoint_outbound(
        self, owner_kind: str, owner_id: str, ok: bool, note: str, address: Any = None,
    ) -> None:
        """上游 `noteEndpointOutbound`：出站结果回写（deliverable 维）。

        给了 `address` 时**只更新地址匹配的端点**——多端点下不串刷兄弟端点。
        """
        if owner_kind not in _OWNER_KINDS:
            return
        stamp = dt_ms(self.now())
        states = getattr(self, 'endpoint_states', None)
        if states is None:
            return
        for row in getattr(self, 'endpoint_rows', []):
            if pick(row, 'ownerKind', 'owner_kind') != owner_kind or pick(row, 'ownerId', 'owner_id') != owner_id:
                continue
            if address is not None and (
                pick(row, 'platform') != pick(address, 'platform')
                or str(pick(row, 'selfId', 'self_id')) != str(pick(address, 'selfId', 'self_id'))
            ):
                continue
            previous = states.get(pick(row, 'id')) or fresh_endpoint_state(pick(row, 'id'), stamp)
            states[pick(row, 'id')] = state_after_outbound(
                previous, ok, note, 0 if ok else 5 * 60_000, stamp,
            )

    # ------------------------------------------------------------------ #
    # 出站地址同步与多通道选择
    # ------------------------------------------------------------------ #

    def endpoint_address_sync(self, legacy: Any, owner_kind: str = '', owner_id: str = '') -> dict[str, Any]:
        """上游 `endpointAddressSync`：注册表命中且启用时以注册表为准，漂移只告警一次。

        同一归属多行时**不取第一行**：优先最近观测过连接的端点，平手按 `createdAt`
        稳定排序，并一次性告警。
        """
        if not owner_kind or not owner_id or not getattr(self, 'endpoint_registry_ready', False):
            return legacy
        rows = [
            row for row in getattr(self, 'endpoint_rows', [])
            if pick(row, 'ownerKind', 'owner_kind') == owner_kind
            and pick(row, 'ownerId', 'owner_id') == owner_id
            and pick(row, 'enabled')
        ]
        if not rows:
            return legacy
        states = getattr(self, 'endpoint_states', {}) or {}
        warned = getattr(self, '_endpoint_drift_warned', None)
        if warned is None:
            warned = set()
            self._endpoint_drift_warned = warned
        if len(rows) > 1:
            def sort_key(row: dict[str, Any]) -> tuple[int, int, float]:
                state = states.get(pick(row, 'id')) or {}
                connection = pick(state, 'connection') or {}
                online = 1 if pick(connection, 'online') is True else 0
                seen = int(pick(connection, 'observedAt', 'observed_at') or 0)
                created = pick(row, 'createdAt', 'created_at')
                created_ms = dt_ms(created) if hasattr(created, 'timestamp') else 0
                return (-online, -seen, created_ms)

            rows = sorted(rows, key=sort_key)
            warn_key = 'ambiguous:%s:%s' % (owner_kind, owner_id)
            if warn_key not in warned:
                warned.add(warn_key)
                self.report_standalone(
                    'warn', '同一归属存在多个启用端点，出站地址按最近连接观测选取 归属=%s:%s 选中=%s',
                    owner_kind, owner_id, pick(rows[0], 'id'),
                )
        row = rows[0]
        legacy_platform = pick(legacy, 'platform')
        legacy_self_id = str(pick(legacy, 'selfId', 'self_id') or '')
        if str(pick(row, 'selfId', 'self_id')) != legacy_self_id or pick(row, 'platform') != legacy_platform:
            warn_key = 'drift:%s:%s:%s' % (pick(row, 'id'), legacy_platform, legacy_self_id)
            if warn_key not in warned:
                warned.add(warn_key)
                self.report_standalone(
                    'warn', '出站地址以端点注册表为准（旧字段漂移）端点=%s 注册=%s/%s 旧=%s/%s',
                    pick(row, 'id'), pick(row, 'platform'), pick(row, 'selfId', 'self_id'),
                    legacy_platform, legacy_self_id,
                )
        return {'platform': pick(row, 'platform'), 'selfId': pick(row, 'selfId', 'self_id'), 'endpointId': pick(row, 'id')}

    def delivery_address_for(self, story: Any, participant: Any) -> dict[str, Any]:
        """上游 M1a 出站地址解析（v3 §五）：注册表命中时以注册表为准，否则原样返回。

        返回 participant 的一份**浅拷贝**（只在地址真的不同/命中端点时才复制），
        适配层随后照常把它解析成 UMO。单平台零影响：注册表只有派生端点时地址与
        旧字段一致。
        """
        if not isinstance(participant, dict):
            return participant
        owner_kind = 'participant-user' if pick(participant, 'id') and pick(participant, 'userId', 'user_id') \
            else 'story-role'
        owner_id = pick(participant, 'id') if owner_kind == 'participant-user' else pick(story, 'id')
        legacy = {
            'platform': pick(participant, 'platform') or pick(story, 'platform'),
            'selfId': pick(participant, 'selfId', 'self_id') or pick(story, 'selfId', 'self_id'),
        }
        synced = self.endpoint_address_sync(legacy, owner_kind, owner_id)
        endpoint_id = pick(synced, 'endpointId', 'endpoint_id')
        if not endpoint_id:
            return participant
        address = {
            **participant,
            'platform': pick(synced, 'platform'),
            'selfId': pick(synced, 'selfId', 'self_id'),
            'endpointId': endpoint_id,
        }
        return address

    def resolve_most_active_endpoint_id(self, participant_id: str) -> Optional[str]:
        """上游 `resolveMostActiveEndpointId`（M3 §八）：最近在线端点；无在线取首个启用。"""
        rows = [
            row for row in getattr(self, 'endpoint_rows', [])
            if pick(row, 'ownerKind', 'owner_kind') == 'participant-user'
            and pick(row, 'ownerId', 'owner_id') == participant_id
            and pick(row, 'enabled')
        ]
        if not rows:
            return None
        states = getattr(self, 'endpoint_states', {}) or {}
        best: Optional[tuple[int, str]] = None
        for row in rows:
            state = states.get(pick(row, 'id')) or {}
            connection = pick(state, 'connection') or {}
            if pick(connection, 'online') is not True:
                continue
            observed = int(pick(connection, 'observedAt', 'observed_at') or 0)
            if best is None or observed > best[0]:
                best = (observed, pick(row, 'id'))
        if best is not None:
            return best[1]
        return pick(rows[0], 'id')

    def narrative_endpoint_selection(
        self, story: Any, participants: list[Any], groups: list[Any],
    ) -> dict[str, Any]:
        """上游 `narrativeEndpointSelection`：**只有跨通道种类**才启用多平台传输契约。

        同一平台上的多个账号（例如两个 QQ 号）保持旧的轻量提示词——注册表绝不整份
        暴露给模型，只会给出本次请求合法的目标。
        """
        role_rows = [
            row for row in getattr(self, 'endpoint_rows', [])
            if pick(row, 'ownerKind', 'owner_kind') == 'story-role'
            and pick(row, 'ownerId', 'owner_id') == pick(story, 'id')
            and pick(row, 'enabled')
        ]
        role_account_keys = {pick(row, 'accountKey', 'account_key') for row in role_rows}
        participant_ids = {pick(item, 'id') for item in participants}
        states = getattr(self, 'endpoint_states', {}) or {}
        options: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add(row: dict[str, Any], target_id: str, target_kind: str, conversation_kind: str) -> None:
            if not pick(row, 'enabled'):
                return
            key = '%s:%s' % (pick(row, 'id'), target_id)
            if key in seen:
                return
            seen.add(key)
            state = states.get(pick(row, 'id')) or {}
            options.append({
                'endpointId': pick(row, 'id'), 'targetId': target_id, 'targetKind': target_kind,
                'conversationKind': conversation_kind,
                'channelKind': pick(row, 'channelKind', 'channel_kind'),
                'online': pick(pick(state, 'connection') or {}, 'online') is True,
            })

        for row in getattr(self, 'endpoint_rows', []):
            if pick(row, 'ownerKind', 'owner_kind') == 'participant-user' \
                    and pick(row, 'ownerId', 'owner_id') in participant_ids:
                add(row, pick(row, 'ownerId', 'owner_id'), 'participant', 'private')
        from .base import normalize_group_id as _normalize_group  # 局部导入

        for group in groups:
            group_id = _normalize_group(pick(group, 'groupId', 'group_id'))
            for row in role_rows:
                add(row, pick(group, 'participantId', 'participant_id'), 'group', 'group')
            for row in getattr(self, 'endpoint_rows', []):
                if pick(row, 'ownerKind', 'owner_kind') == 'group' \
                        and pick(row, 'accountKey', 'account_key') in role_account_keys \
                        and _normalize_group(pick(row, 'groupId', 'group_id') or pick(row, 'ownerId', 'owner_id')) == group_id:
                    add(row, pick(group, 'participantId', 'participant_id'), 'group', 'group')
        channel_kinds = {option['channelKind'] for option in options}
        if len(channel_kinds) < 2:
            return {'enabled': False, 'options': []}
        return {'enabled': True, 'options': options}

    async def channel_data_for(
        self, story: Any, participant: Any, group_context: Any, prompt_entries: list[Any],
    ) -> Optional[dict[str, Any]]:
        """上游 `service.ts:4037` 的 `channelData` 构建（M4 §十的输入侧）。

        只把**宿主已确认的路由/来源事实**交给作者；当前入站条目本身不能被当成"上一条"，
        否则私↔群与同人异端规则永远比较不到真正的前一条历史记录。

        本移植版的取舍：`turnSources` / `batchMultiEndpoint` 依赖逐回合的端点来源记账
        （入站 ingest 还没带端点 id），因此这里只产出**可确定**的三项
        （`currentChannel` / `lastEntryChannel` / `replyEndpoint`）——规则 3/4/5 生效，
        规则 1/2 在端点来源记账接上后自然补齐。单平台时注册表只有派生端点，
        `currentChannel` 与历史通道一致 → 一条规则都不命中 → 不产生标注。
        """
        await self.ensure_endpoint_registry()
        data: dict[str, Any] = {}
        story_id = pick(story, 'id')
        # 规则 1/2 的输入：本回合的入站端点来源（`buffer_*` 累积，flush 消费后清空）。
        turn = None
        if participant is not None:
            turns = getattr(self, 'buffered_narrative_turns', None)
            if isinstance(turns, dict):
                turn = turns.get(pick(participant, 'id'))
        sources = _turn_sources(turn, self.endpoint_rows)
        if sources:
            data['turnSources'] = sources
            active = pick(turn, 'activeBatchEndpointIds', 'active_batch_endpoint_ids') or []
            if isinstance(active, list) and len({str(item) for item in active}) > 1:
                data['batchMultiEndpoint'] = True
        story_role = next((
            row for row in self.endpoint_rows
            if pick(row, 'ownerKind', 'owner_kind') == 'story-role'
            and pick(row, 'ownerId', 'owner_id') == story_id
            and pick(row, 'enabled')
        ), None)
        current = None
        if participant is not None:
            address = self.endpoint_address_sync(
                {'platform': pick(participant, 'platform'), 'selfId': pick(participant, 'selfId', 'self_id')},
                'participant-user', pick(participant, 'id'),
            )
            if pick(address, 'endpointId', 'endpoint_id'):
                row = next((
                    item for item in self.endpoint_rows if pick(item, 'id') == pick(address, 'endpointId', 'endpoint_id')
                ), None)
                if row is not None:
                    current = {
                        'endpointId': pick(row, 'id'),
                        'channelKind': pick(row, 'channelKind', 'channel_kind'),
                        'conversationKind': 'group' if group_context else 'private',
                    }
                    data['replyEndpoint'] = {
                        'endpointId': pick(row, 'id'), 'channelKind': pick(row, 'channelKind', 'channel_kind'),
                    }
        if current is None and story_role is not None and group_context:
            current = {
                'endpointId': pick(story_role, 'id'),
                'channelKind': pick(story_role, 'channelKind', 'channel_kind'),
                'conversationKind': 'group',
            }
            data['replyEndpoint'] = {
                'endpointId': pick(story_role, 'id'),
                'channelKind': pick(story_role, 'channelKind', 'channel_kind'),
            }
        if current is not None:
            data['currentChannel'] = current
        # 历史通道：取最近一条带 `metadata.channel_context` 的条目（当前入站批次是否
        # 已入列由调用方决定跳过一个：`skip_latest` 为真时回看两条）。
        tagged = [
            entry for entry in prompt_entries
            if isinstance(pick(entry, 'metadata'), dict)
            and isinstance(pick(pick(entry, 'metadata'), 'channel_context', 'channelContext'), dict)
        ]
        if tagged:
            previous = pick(pick(tagged[-1], 'metadata'), 'channel_context', 'channelContext')
            data['lastEntryChannel'] = {
                'endpointId': pick(previous, 'endpoint_id', 'endpointId'),
                'channelKind': pick(previous, 'channel_kind', 'channelKind'),
                'conversationKind': pick(previous, 'conversation_kind', 'conversationKind'),
            }
        return data or None

    # ------------------------------------------------------------------ #
    # Token 用量账本（本移植版新增：控制台「Token 统计」页的写入侧）
    # ------------------------------------------------------------------ #

    async def record_token_usage(self, record: Any, story_id: str = '') -> bool:
        """把一次模型调用的用量累加进账本（按 `(day, storyId, task, model)` 聚合）。

        并发安全：读-改-写放在 `_token_usage_lock` 里串行执行。**失败只记 warn 不出抛**
        ——账本写不进去不该影响任何一次模型调用。
        """
        delta = normalize_usage_record(record, self.now(), story_id)
        if delta is None:
            return False
        lock = getattr(self, '_token_usage_lock', None)
        if lock is None:
            lock = asyncio.Lock()
            self._token_usage_lock = lock
        try:
            async with lock:
                rows = await self.db_get('interlude_token_usage', {
                    'day': delta['day'], 'storyId': delta['storyId'],
                    'task': delta['task'], 'model': delta['model'],
                })
                existing = rows[0] if rows else None
                merged = merge_usage(existing, delta, self.now())
                if existing is None:
                    await self.db_create('interlude_token_usage', {**merged, 'createdAt': self.now()})
                else:
                    await self.db_set('interlude_token_usage', {'id': pick(existing, 'id')}, {
                        key: merged[key] for key in (
                            'provider', 'inputTokens', 'outputTokens', 'cachedTokens', 'calls', 'updatedAt',
                        )
                    })
            return True
        except Exception as error:  # noqa: BLE001 - 账本失败不阻断调用
            self.report_standalone('warn', 'Token 用量记账失败（不影响调用）模型=%s 任务=%s 错误=%s',
                                   delta['model'], delta['task'], error)
            return False

    # ------------------------------------------------------------------ #
    # 内部助手
    # ------------------------------------------------------------------ #

    async def _endpoint_migration_entry(self, story_id: str, content: str, metadata: dict[str, Any]) -> None:
        """上游把端点变更也写成一条 `system` 剧本条目（审计可见）。失败不阻断。"""
        if not story_id:
            return
        try:
            await self.append_entry(story_id, {
                'kind': 'system', 'actor': 'system', 'content': content,
                'occurredAt': iso(self.now()), 'metadata': metadata,
            }, self.now())
        except Exception:  # noqa: BLE001 - 审计写入失败不该让端点操作回滚
            pass


def _turn_sources(turn: Any, endpoint_rows: list[Any]) -> list[dict[str, Any]]:
    """回合的入站端点来源 → `channelData.turnSources`（带 channelKind，按接收序号排序）。"""
    if not isinstance(turn, dict):
        return []
    raw = pick(turn, 'sources')
    if not isinstance(raw, list) or not raw:
        return []
    kinds = {
        pick(row, 'id'): ('wechat' if pick(row, 'channelKind', 'channel_kind') == 'wechat' else 'qq')
        for row in endpoint_rows
    }
    sources = [
        {
            'endpointId': str(pick(item, 'endpointId', 'endpoint_id') or ''),
            'receivedSeq': int(pick(item, 'receivedSeq', 'received_seq') or 0),
            'channelKind': kinds.get(pick(item, 'endpointId', 'endpoint_id'), 'qq'),
        }
        for item in raw
        if pick(item, 'endpointId', 'endpoint_id')
    ]
    sources.sort(key=lambda item: (item['receivedSeq'], item['endpointId']))
    return sources


def story_id_for_character_of(story: Any) -> str:
    """从故事行读出「按账号推导的 ID」（`story_id_for_character(platform, selfId)`）。"""
    return story_id_for_character(pick(story, 'platform') or 'onebot', str(pick(story, 'selfId', 'self_id') or ''))
