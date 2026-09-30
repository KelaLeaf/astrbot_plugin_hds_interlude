"""Chunk13 mixin：QQ 空间（说说）通道的服务层。

上游对应成员（`src/service.ts:6769-6951`，纯策略在 `core/qzone.py`）：

| 上游 | 本文件 |
| --- | --- |
| `qzoneRuntime`（字段，`applyConfig` 里解析一次） | `qzone_runtime()`（每次读当前配置） |
| `qzoneCaller(preferSelfId?)` | `qzone_caller(prefer_self_id='')` |
| `qzoneExecute(story, kind, input, preferSelfId?)` | `qzone_execute(story, kind, input, prefer_self_id='')` |
| `qzoneAvailable(preferSelfId?)` | `qzone_available(prefer_self_id='')` |
| `executeQzoneIntent(story, intent, now)` | `execute_qzone_intent(story, intent, now)` |
| `qzoneFeedSweepRunning`（字段） | `_qzone_feed_sweep_running`（类属性，默认 False） |
| `qzoneFeedSweep()` | `qzone_feed_sweep()` |

## 与上游的受控偏离（其余逐条照抄）

1. **账号选择**：上游在 `ctx.bots` 里按 `selfId` 找在线 OneBot 连接；本移植版所有
   平台出站都收敛到 `Transport`（移植约定：找不到账号 = `transport-unavailable`），
   因此 `qzone_caller` 只判断传输层是否具备 `call_onebot` 能力。上游"`preferSelfId`
   必须精确匹配本故事的角色端点、找不到**绝不**切到别的账号"那一段**原样保留**
   （`_qzone_address`）。
2. **`qzone_runtime` 每次现算**：上游在 `applyConfig` 里缓存；本移植版的
   `qzone` 配置段可以在控制台改，缓存会导致"改了不生效"。
3. **48h 窗口 / 7 天去重账本**：上游用 `createdAt: {$gte}` 查询；本移植版
   `db_get` 对范围算子显式抛错（见 PORTING_NOTES「范围算子查询退化」），改为
   「取全量 + Python 侧过滤」。审计表只记动作与已入账动态，量级可控。
4. **可见日志**：上游把"被限流门拦下""意图已处理""好友动态已入账"写成
   `diagnostic`/`debug`，而本移植版 `diagnostic` 频道默认不显示（坑 25/45/48）。
   这里统一用 `standard`/`info`；成败与否（`warn`）与上游一致。
5. **剧本条目**：按本移植版的约定，`kind` 用 `'system'`、`metadata` 用 snake_case
   （`qzone_kind` / `tid` / `ugc_right`），`actor` 保持上游的 `'character'`（那是
   **她自己**做的事）。上游的 `metadata` 是 `qzoneKind` camelCase。
6. **`prefer_self_id` 未传时** `qzone_execute` 取故事角色端点
   （`endpoint_address_sync`，与 chunk11 同源）。
7. **`qzone.auto_feed`**（本移植版新增的开关，上游 `qzoneFeedSweep` 恒开）：只有
   打开它才轮询好友动态——「允许她浏览好友动态并在合适时评论/点赞」。没打开时
   按小时节流打一条**看得见**的说明，免得用户以为"开了 QQ 空间却什么都不发生"。
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Optional

from ..endpoints import endpoint_account_key
from ..qzone import (
    QzoneActionError,
    call_qzone_action,
    evaluate_qzone_gate,
    match_qzone_feed_content,
    normalize_qzone_feed_entry,
    normalize_qzone_msg_entry,
    probe_qzone_available,
    qzone_feed_candidates,
    qzone_intent_from_payload,
    qzone_records_for_endpoint,
    qzone_visibility_label,
    resolve_qzone_config,
)
from ..time import dt_ms, iso, parse_dt
from .base import ServiceBase, pick
from .helpers import clip

__all__ = ['ServiceChunk13']

#: 限流门读多少小时的审计行（上游 `48 * Time.hour`）。
QZONE_AUDIT_WINDOW_HOURS = 48
#: 好友动态去重账本回看天数（上游 `7 * 24 * Time.hour`）。
QZONE_SEEN_WINDOW_DAYS = 7
#: `get_qzone_feeds` 每轮只吃首页（深翻页不可靠）。
QZONE_FEED_PAGE_NUM = 1
QZONE_FEED_PAGE_SIZE = 20
#: 对齐正文时拉取好友最近的几条说说（只认 tid 精确命中）。
QZONE_FEED_MSG_NUM = 5
#: 失败原因的落库截断长度（上游 `.slice(0, 500)`）。
QZONE_ERROR_MAX_CHARS = 500
#: 「开了 QQ 空间但没开自动浏览好友动态」这条说明的节流间隔（毫秒）。
QZONE_AUTO_FEED_NOTE_INTERVAL_MS = 60 * 60 * 1000
#: 剧本条目正文的截断长度（上游 `clip(content, 120)` / `clip(content, 80)`）。
QZONE_POST_SUMMARY_CHARS = 120
QZONE_COMMENT_SUMMARY_CHARS = 80

_MILLISECONDS_PER_MINUTE = 60_000


def _qzone_config_section(config: Any) -> Any:
    """读 `qzone` 配置段；`qzone_compat` 只作旧文件的兜底（两种拼写都认）。

    schema 那边已把隐藏兼容位 `qzone_compat` 转正成真分组 `qzone`，所以**先读
    `qzone`**——只读旧名会让"用户把 QQ 空间打开也不生效"，而且是静默的。
    """
    if config is None:
        return None
    names = ('qzone', 'qzoneCompat', 'qzone_compat')
    if isinstance(config, Mapping):
        for name in names:
            if name in config and config[name] is not None:
                return config[name]
        return None
    for name in names:
        value = getattr(config, name, None)
        if value is not None:
            return value
    return None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _rows(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _row_ms(row: Any) -> Optional[int]:
    """审计行的 `createdAt` 毫秒数；无法解析返回 None（等价 SQL 里 `>=` 不成立）。"""
    parsed = parse_dt(pick(row, 'createdAt', 'created_at'))
    return None if parsed is None else dt_ms(parsed)


def _kind_label(kind: Any) -> str:
    """上游那串 `kind === 'post' ? '发帖' : …` 的中文档位名。"""
    if kind == 'post':
        return '发帖'
    if kind == 'comment':
        return '评论'
    return '点赞'


def _target_uin_param(value: Any) -> Any:
    """`target_uin` 参数：上游 `Number(targetUin)`；非数字则**不带这个键**。

    JS 的 `Number('abc')` 是 NaN、序列化成 JSON 会变 `null`；本移植版直接省略，
    免得把 `null` 当"归属 0"发给 SnowLuma。
    """
    if value is None or value == '':
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        try:
            number = float(str(value).strip())
        except (TypeError, ValueError):
            return None
    if not math.isfinite(number) or number != int(number):
        return None
    return int(number)


def _js_round(value: float) -> int:
    """JS `Math.round`：半值向上（Python 的 `round` 是银行家舍入）。"""
    return int(math.floor(value + 0.5))


class ServiceChunk13(ServiceBase):
    """Chunk13：QQ 空间通道（门控 → 审计 → 动作 → 回写）。"""

    #: 好友动态轮询单飞锁（上游 `qzoneFeedSweepRunning = false` 字段）。
    _qzone_feed_sweep_running = False

    # ------------------------------------------------------------------ #
    # 运行时配置与调用口
    # ------------------------------------------------------------------ #

    def qzone_runtime(self) -> dict[str, Any]:
        """解析后的 QQ 空间配置（上游 `this.qzoneRuntime`）。

        上游在 `applyConfig` 里解析一次并缓存；本移植版每次读**当前**配置段
        `qzone`——控制台改了配置就该立刻生效。
        """
        return resolve_qzone_config(_qzone_config_section(getattr(self, 'config', None)))

    def _qzone_call_onebot(self) -> Any:
        """`Transport.call_onebot` 的绑定方法；传输层没这个能力时返回 None。"""
        transport = getattr(self, 'transport', None)
        call = getattr(transport, 'call_onebot', None)
        return call if callable(call) else None

    def qzone_caller(self, prefer_self_id: str = '') -> bool:
        """是否有可用的空间动作调用口（上游 `qzoneCaller(preferSelfId?)`）。

        上游返回"某个在线 OneBot 的 `_request` 包装"或 `undefined`；本移植版统一
        走 `Transport`，所以这里只判断传输层是否具备 `call_onebot`。
        `prefer_self_id` 的**精确匹配**语义在 `_qzone_address` 里保留（找不到就
        失败，绝不切到别的账号）。
        """
        return self._qzone_call_onebot() is not None

    async def _qzone_address(self, story: Any, prefer_self_id: str = '') -> Optional[dict[str, Any]]:
        """解析执行账号（上游 `qzoneExecute` 开头那段）。

        显式指定 `prefer_self_id` 时按 `accountKey` 精确匹配本故事的角色端点，
        找不到返回 `None`（调用方给失败文案）；未指定时经 `endpoint_address_sync`
        解析（注册表未命中即回落故事自身的账号，单平台零影响）。
        """
        platform = pick(story, 'platform')
        story_self_id = pick(story, 'selfId', 'self_id')
        story_id = str(pick(story, 'id') or '')
        if prefer_self_id:
            try:
                await self.ensure_endpoint_registry()
            except Exception:  # noqa: BLE001 - 注册表不可用不该让动作直接崩
                pass
            key = endpoint_account_key(platform, str(prefer_self_id))
            for row in _rows(getattr(self, 'endpoint_rows', None)):
                if (
                    pick(row, 'ownerKind', 'owner_kind') == 'story-role'
                    and str(pick(row, 'ownerId', 'owner_id') or '') == story_id
                    and pick(row, 'accountKey', 'account_key') == key
                    and pick(row, 'enabled')
                ):
                    return {
                        'platform': pick(row, 'platform'),
                        'selfId': pick(row, 'selfId', 'self_id'),
                        'endpointId': pick(row, 'id'),
                    }
            return None
        legacy = {'platform': platform, 'selfId': story_self_id}
        sync = getattr(self, 'endpoint_address_sync', None)
        if not callable(sync):
            return legacy
        address = sync(legacy, 'story-role', story_id)
        return address if isinstance(address, Mapping) else legacy

    # ------------------------------------------------------------------ #
    # 动作执行
    # ------------------------------------------------------------------ #

    async def _qzone_recent_rows(self, now: Any) -> list[Any]:
        """限流门的输入：48h 内的审计行（范围算子退化 → 取全量 + Python 侧过滤）。"""
        rows = await self.db_get('interlude_qzone_post', {})
        cutoff = dt_ms(now) - QZONE_AUDIT_WINDOW_HOURS * 60 * 60 * 1000
        kept: list[Any] = []
        for row in rows:
            at_ms = _row_ms(row)
            if at_ms is not None and at_ms >= cutoff:
                kept.append(row)
        return kept

    async def _qzone_set_status(self, row_id: Any, patch: dict[str, Any]) -> None:
        """回写审计行状态：失败只 warn（审计写不进去不该打断动作回执）。"""
        try:
            await self.db_set('interlude_qzone_post', {'id': row_id}, patch)
        except Exception as error:  # noqa: BLE001
            self.report_standalone('warn', 'QQ 空间审计行回写失败 行=%s 错误=%s', row_id, error)

    async def qzone_execute(
        self,
        story: Any,
        kind: str,
        input: Any = None,
        prefer_self_id: str = '',
    ) -> dict[str, Any]:
        """执行一条空间动作（上游 `qzoneExecute`）：限流门 → pending 审计行（在故事
        串行队列内**原子预留配额**）→ SnowLuma 动作（网络调用留在队列外）→ 回写
        `confirmed` / `failed` / `unknown`。

        传输类异常与"成功帧但无 tid"都记 `unknown`——结果不明按已发生保守计入
        配额，且绝不自动重试非幂等动作。
        """
        payload = _mapping(input)
        runtime = self.qzone_runtime()
        if not runtime.get('enabled'):
            return {'ok': False, 'tid': '', 'error': 'QQ 空间通道未启用（Console → 扩展 → QQ 空间）。'}
        address = await self._qzone_address(story, prefer_self_id)
        if address is None:
            return {
                'ok': False, 'tid': '',
                'error': '账号 %s 未注册为本故事的角色端点（精确匹配失败，不自动切换账号）。' % prefer_self_id,
            }
        endpoint_id = pick(address, 'endpointId', 'endpoint_id')
        account_self_id = str(pick(address, 'selfId', 'self_id') or '')
        call = self._qzone_call_onebot()
        if call is None:
            self.report_standalone('warn', 'QQ 空间动作没有可用连接 账号=%s 原因=传输层未提供 call_onebot', account_self_id)
            return {
                'ok': False, 'tid': '',
                'error': '没有可用的 OneBot（SnowLuma）连接（账号 %s 不在线）；不会改用其他账号执行空间动作。' % account_self_id,
            }
        now = self.now()
        story_id = str(pick(story, 'id') or '')

        async def reserve() -> dict[str, Any]:
            """串行队列内完成"过门 + 落 pending 行"，杜绝并发动作双双过门。"""
            recent = await self._qzone_recent_rows(now)
            gate = evaluate_qzone_gate(
                qzone_records_for_endpoint(recent, endpoint_id), runtime, kind, now,
            )
            if not gate.get('allowed'):
                return {'blocked': gate.get('reason'), 'gate': gate, 'row': None}
            data: dict[str, Any] = {
                'storyId': story_id,
                'kind': kind,
                'tid': payload.get('tid') or '',
            }
            target_uin = payload.get('targetUin')
            if target_uin:
                data['targetUin'] = str(target_uin)
            if kind == 'post' and payload.get('content'):
                data['content'] = clip(str(payload.get('content')), 2_000)
                right = payload.get('ugcRight')
                if right:  # 上游 `...(input.ugcRight ? { ugcRight } : {})`
                    data['ugcRight'] = right
            if endpoint_id:
                data['endpointId'] = endpoint_id
            data['status'] = 'pending'
            data['createdAt'] = now
            row = await self.db_create('interlude_qzone_post', data)
            return {'blocked': None, 'gate': gate, 'row': row}

        reserved = await self.serial(story_id, reserve)
        if reserved.get('blocked') or not reserved.get('row'):
            gate = reserved.get('gate') or {}
            blocked = reserved.get('blocked')
            self.report_operation(
                'standard', 'info', story, 'user-message',
                'QQ 空间动作被限流门拦下 类型=%s 原因=%s 今日=%s/%s',
                kind, blocked, gate.get('used_today'), gate.get('cap'),
            )
            if blocked == 'daily-cap':
                return {
                    'ok': False, 'tid': '',
                    'error': '今日%s已达上限（%s/%s）。' % (_kind_label(kind), gate.get('used_today'), gate.get('cap')),
                }
            return {
                'ok': False, 'tid': '',
                'error': '距离上一条空间动作不足最小间隔（%s 分钟）。' % runtime.get('min_interval_minutes'),
            }
        pending_id = pick(reserved.get('row'), 'id')
        right = payload.get('ugcRight')
        if right is None:
            right = 4
        try:
            if kind == 'post':
                data = _mapping(await call_qzone_action(call, 'send_qzone_msg', {
                    'content': payload.get('content') or '',
                    'ugc_right': right,
                }))
                tid = str(pick(data, 'tid') or '').strip()
                if not tid:
                    # 成功帧却拿不到 tid：帖子可能已发出但无法追踪——记 unknown，
                    # 不宣称已确认，也不自动重试。
                    await self._qzone_set_status(pending_id, {
                        'status': 'unknown', 'error': '服务端未返回 tid，结果未知。',
                    })
                    self.report_standalone('warn', 'QQ 空间说说发表结果未知（无 tid），已按保守计入配额且不自动重试')
                    return {
                        'ok': False, 'tid': '',
                        'error': '发表结果未知：服务端未返回说说 tid，为避免重复发帖不会自动重试。',
                    }
                await self._qzone_set_status(pending_id, {
                    'tid': tid, 'status': 'confirmed', 'postedAt': self.now(),
                })
                # 已发布的事实进剧本：她自己会记得发过什么（`[空间动态]` 前缀走
                # SOCIAL SURFACE 规则）。
                await self.append_entry(story_id, {
                    'kind': 'system', 'actor': 'character',
                    'content': '[空间动态] 她发表了说说：%s（%s）' % (
                        clip(payload.get('content'), QZONE_POST_SUMMARY_CHARS),
                        qzone_visibility_label(right),
                    ),
                    'occurredAt': iso(now),
                    'metadata': {'qzone_kind': 'post', 'tid': tid, 'ugc_right': right},
                }, now)
                self.report_operation(
                    'standard', 'info', story, 'user-message',
                    'QQ 空间说说已发表 tid=%s 可见性=%s', tid, right,
                )
                return {'ok': True, 'tid': tid, 'error': ''}
            if kind == 'comment':
                params: dict[str, Any] = {
                    'tid': payload.get('tid') or '',
                    'content': payload.get('content') or '',
                }
                target_uin = _target_uin_param(payload.get('targetUin'))
                if target_uin is not None:
                    params['target_uin'] = target_uin
                await call_qzone_action(call, 'comment_qzone', params)
                await self._qzone_set_status(pending_id, {
                    'status': 'confirmed', 'postedAt': self.now(),
                })
                await self.append_entry(story_id, {
                    'kind': 'system', 'actor': 'character',
                    'content': '[空间动态] 她评论了%s的说说：%s' % (
                        ' QQ %s' % payload.get('targetUin') if payload.get('targetUin') else '',
                        clip(payload.get('content'), QZONE_COMMENT_SUMMARY_CHARS),
                    ),
                    'occurredAt': iso(now),
                    'metadata': {'qzone_kind': 'comment', 'tid': payload.get('tid') or ''},
                }, now)
                self.report_operation(
                    'standard', 'info', story, 'user-message',
                    'QQ 空间评论已发出 tid=%s 归属=%s', payload.get('tid') or '', payload.get('targetUin') or '自己',
                )
                return {'ok': True, 'tid': payload.get('tid') or '', 'error': ''}
            params = {'tid': payload.get('tid') or ''}
            target_uin = _target_uin_param(payload.get('targetUin'))
            if target_uin is not None:
                params['target_uin'] = target_uin
            await call_qzone_action(call, 'like_qzone', params)
            await self._qzone_set_status(pending_id, {
                'status': 'confirmed', 'postedAt': self.now(),
            })
            # 点赞成功不单独进剧本（过细），但要有一条可见日志（上游这条路径是静默的）。
            self.report_operation(
                'standard', 'info', story, 'user-message',
                'QQ 空间点赞已发出 tid=%s 归属=%s', payload.get('tid') or '', payload.get('targetUin') or '自己',
            )
            return {'ok': True, 'tid': payload.get('tid') or '', 'error': ''}
        except Exception as error:  # noqa: BLE001 - 上游同样是 catch-all
            message = str(error)
            # `ambiguous` = 请求可能已到达服务端（超时/断连）：记 `unknown` 而非
            # `failed`——`unknown` 保守计入配额（可能已发生），语义上禁止自动重试。
            ambiguous = isinstance(error, QzoneActionError) and error.ambiguous
            await self._qzone_set_status(pending_id, {
                'status': 'unknown' if ambiguous else 'failed',
                'error': message[:QZONE_ERROR_MAX_CHARS],
            })
            self.report_standalone(
                'warn', 'QQ 空间动作%s 类型=%s 错误=%s',
                '结果未知' if ambiguous else '失败', kind, message,
            )
            return {
                'ok': False, 'tid': '',
                'error': message + '（结果未知：请求可能已生效，为避免重复不会自动重试。）' if ambiguous else message,
            }

    async def qzone_available(self, prefer_self_id: str = '') -> bool:
        """通道能力探测（上游 `qzoneAvailable`，只读）：`get_qzone_msg_list` 通不通。"""
        call = self._qzone_call_onebot()
        if call is None or not self.qzone_caller(prefer_self_id):
            return False
        try:
            return await probe_qzone_available(call)
        except Exception:  # noqa: BLE001 - 探测失败就是"不可用"
            return False

    # ------------------------------------------------------------------ #
    # 到期意图的执行侧
    # ------------------------------------------------------------------ #

    async def execute_qzone_intent(self, story: Any, intent: Any, now: Any = None) -> None:
        """到期 `qzone-action` 意图的执行侧（上游 `executeQzoneIntent`）。

        payload 校验 → 目标来源绑定 → 限流门 → 动作 → **完成意图**。坏 payload /
        限流 / 通道失败都直接完成意图（成败进审计表），不回流叙事——坏 payload
        永远不该卡住账本排水。
        """
        moment = now if now is not None else self.now()
        intent_id = pick(intent, 'id')

        async def finish(note: str) -> None:
            try:
                await self.db_set('interlude_intent', {'id': intent_id}, {
                    'status': 'completed', 'updatedAt': moment,
                })
            except Exception as error:  # noqa: BLE001 - 完成不了也要留下可见记录
                self.report_standalone('warn', 'QQ 空间意图完成失败 意图=%s 错误=%s', intent_id, error)
            self.report_operation(
                'standard', 'info', story, 'intent-due',
                'QQ 空间意图已处理 意图=%s 结果=%s', intent_id, note,
            )

        request = qzone_intent_from_payload(pick(intent, 'payload'))
        if request is None:
            await finish('payload-invalid')
            return
        if request.get('action') != 'post':
            # 目标来源绑定：评论/点赞的 tid 必须来自本账号实际读到并入账的好友动态
            # （`feed-seen` 行）或她自己已发表的说说（`confirmed` 的 post 行）——
            # 模型编造的 tid 一律拒绝，防止对任意帖子执行写操作。
            targets = await self.db_get('interlude_qzone_post', {'tid': request.get('tid') or ''})
            known = any(
                pick(row, 'status') == 'confirmed' and pick(row, 'kind') in ('feed-seen', 'post')
                for row in targets
            )
            if not known:
                await finish('target-unknown')
                return
        result = await self.qzone_execute(story, request.get('action'), {
            'content': request.get('content'),
            'tid': request.get('tid'),
            'targetUin': request.get('targetUin'),
            'ugcRight': request.get('ugcRight'),
        }, str(pick(story, 'selfId', 'self_id') or ''))
        # 点赞成功不单独进剧本（过细）；发帖/评论的条目由 `qzone_execute` 写入。
        await finish('ok' if result.get('ok') else 'failed:%s' % clip(result.get('error') or '', 80))

    # ------------------------------------------------------------------ #
    # 好友动态轮询
    # ------------------------------------------------------------------ #

    def qzone_feed_poll_minutes(self) -> int:
        """轮询间隔（上游 `applyConfig`）：`min(60, max(15, round(feedWindow/2)))` 分钟。"""
        window = int(self.qzone_runtime().get('feed_window_minutes') or 120)
        return min(60, max(15, _js_round(window / 2.0)))

    async def qzone_feed_sweep(self) -> None:
        """好友动态轮询（上游 `qzoneFeedSweep`）：感知零模型调用——新鲜说说写成
        `[好友动态]` 条目，反应留给回合内决策。

        单飞锁 + 失败只 warn，**绝不上抛**；feeds 接口间歇失败就静默跳过，下轮再试。

        `qzone.auto_feed`（本移植版新增）没打开时**不轮询**——这个开关就是
        「允许她浏览好友动态」；这时按小时节流打一条可见说明，不然用户会以为
        "QQ 空间开了却什么都不发生"。
        """
        runtime = self.qzone_runtime()
        if (
            getattr(self, 'desktop_runtime_phase', 'running') == 'paused'
            or getattr(self, 'database_resetting', False)
            or not runtime.get('enabled')
            or getattr(self, '_qzone_feed_sweep_running', False)
        ):
            return
        if not runtime.get('auto_feed'):
            self.note_access_skip(
                'qzone-auto-feed-off', QZONE_AUTO_FEED_NOTE_INTERVAL_MS,
                'QQ 空间通道已启用，但「自动浏览好友动态」是关的：本轮及以后都不会'
                '轮询好友动态（在「幕间控制台 → 配置 → QQ 空间」里打开'
                '「自动浏览好友动态」即可；手动/意图触发的发说说、评论、点赞不受影响）',
            )
            return
        self._qzone_feed_sweep_running = True
        try:
            story = await self.get_canonical_story()
            if not story or not self.can_handle_story(story):
                return
            story_id = str(pick(story, 'id') or '')
            # 轮询账号经端点注册表解析（多角色端点下不再读故事旧 selfId）。
            legacy = {'platform': pick(story, 'platform'), 'selfId': pick(story, 'selfId', 'self_id')}
            sync = getattr(self, 'endpoint_address_sync', None)
            address = sync(legacy, 'story-role', story_id) if callable(sync) else legacy
            if not isinstance(address, Mapping):
                address = legacy
            sweep_endpoint_id = pick(address, 'endpointId', 'endpoint_id')
            call = self._qzone_call_onebot()
            if call is None:
                return
            now = self.now()
            try:
                raw = _mapping(await call_qzone_action(call, 'get_qzone_feeds', {
                    'page_num': QZONE_FEED_PAGE_NUM,
                    'count': QZONE_FEED_PAGE_SIZE,
                }))
                feeds = [
                    entry for entry in (
                        normalize_qzone_feed_entry(item, now)
                        for item in _rows(pick(raw, 'feeds'))
                    ) if entry
                ]
            except Exception:  # noqa: BLE001 - feeds CGI 间歇失败：静默跳过，下轮再试
                return
            # 去重账本：`feed-seen` 行的 tid 即 `feeds.key`（7 天窗足够覆盖时间窗双倍）。
            seen_rows = await self.db_get('interlude_qzone_post', {'kind': 'feed-seen'})
            seen_cutoff = dt_ms(now) - QZONE_SEEN_WINDOW_DAYS * 24 * 60 * 60 * 1000
            seen_keys: set[str] = set()
            for row in seen_rows:
                at_ms = _row_ms(row)
                if at_ms is None or at_ms < seen_cutoff:
                    continue
                key = str(pick(row, 'tid') or '')
                if key:
                    seen_keys.add(key)
            for feed in qzone_feed_candidates(feeds, seen_keys, runtime, now):
                content = ''
                try:
                    list_raw = _mapping(await call_qzone_action(call, 'get_qzone_msg_list', {
                        'target_uin': _target_uin_param(pick(feed, 'uin')),
                        'num': QZONE_FEED_MSG_NUM,
                    }))
                    entries = [
                        entry for entry in (
                            normalize_qzone_msg_entry(item)
                            for item in _rows(pick(list_raw, 'msglist'))
                        ) if entry
                    ]
                    content = match_qzone_feed_content(entries, feed)
                except Exception:  # noqa: BLE001 - 正文拉取失败按元数据处理
                    content = ''
                owner = pick(feed, 'nickname') or 'QQ %s' % pick(feed, 'uin')
                await self.append_entry(story_id, {
                    'kind': 'friend-feed', 'actor': 'system',
                    'content': '[好友动态] %s发布了说说%s' % (
                        owner, '：%s' % clip(content, QZONE_COMMENT_SUMMARY_CHARS) if content else '',
                    ),
                    'occurredAt': iso(pick(feed, 'time')),
                    'metadata': {
                        'qzone_feed_key': pick(feed, 'key'),
                        'qzone_feed_uin': pick(feed, 'uin'),
                        'qzone_feed_nickname': pick(feed, 'nickname'),
                    },
                }, now)
                seen_row: dict[str, Any] = {
                    'storyId': story_id,
                    'kind': 'feed-seen',
                    'tid': pick(feed, 'key'),
                    'targetUin': pick(feed, 'uin'),
                    'content': str(pick(feed, 'nickname') or '')[:100],
                    'status': 'confirmed',
                    'createdAt': now,
                }
                if sweep_endpoint_id:
                    seen_row['endpointId'] = sweep_endpoint_id
                await self.db_create('interlude_qzone_post', seen_row)
                self.report_operation(
                    'standard', 'info', story, 'advance',
                    '好友动态已入账 归属=%s 正文=%s', owner, '有' if content else '无',
                )
        except Exception as error:  # noqa: BLE001 - 轮询绝不上抛
            self.report_standalone('warn', 'QQ 空间动态轮询失败 错误=%s', error)
        finally:
            self._qzone_feed_sweep_running = False
