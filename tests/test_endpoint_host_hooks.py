"""平台生命周期 → 端点连接状态（上游 `src/service.ts:914-921` 的 AstrBot 等价物）。

## 这一批修的是什么

M3 的端点投递门控（`chunk11.endpoint_gate_reason` / `is_endpoint_deliverable`）现在
**真的会拦投递**，而 `restore_endpoint_state` 重启后一律把 `connection.online` 归零。
上游在 `startBackgroundTasks()` 里挂了连接器生命周期事件补这一跳
（`upstream/src/service.ts:916-920`：`bot-status-updated` → 在线、`bot-removed` → 离线），
本移植版的 `note_endpoint_connection` 却**全仓没有调用方**。

真机可见的后果：重启后，一条**到期的分段 / 延迟消息**若在**下一次入站之前**触发
（那时 `session=None`），会被判 `endpoint-offline` 拦住——她主动发的话发不出去，
现场只看到"没发出去"。

## AstrBot 4.28 上到底能挂什么（读宿主源码，行号是这么来的）

| 宿主 | 位置 |
| --- | --- |
| `on_platform_loaded`（平台加载完成） | 装饰器 `astrbot/core/star/register/star_handler.py:347`；`EventType` 见 `core/star/star_handler.py:226`；触发点 `core/platform/manager.py:229-236` |
| `on_astrbot_loaded`（AstrBot 加载完成） | 装饰器 `.../star_handler.py:337`；`EventType` 见 `core/star/star_handler.py:225`；触发点 `core/core_lifecycle.py:367-375` |

**没有**"平台卸载 / 断开"事件：`EventType` 全表（`core/star/star_handler.py:219-241`）
里没有这一项，`PlatformManager.terminate_platform`（`core/platform/manager.py:275`）
与 `reload`（`:264`）都不发事件。所以"断开"没有即时钩子——这是受控降级，
`docs/PORTING_NOTES.md` §99 记着。

两个钩子的回调都**不带参数**（`await handler.handler()`，平台身份由我们自己现探
`context.platform_manager`）。

## 用例怎么反向

* ① 写回**之前**状态必须是离线（否则"写回生效"证明不了任何事）；
* ② 宿主清单里没有这个平台 / 适配器 status=error → 写回**离线**（断开方向真的会翻）；
* ③ 端到端：重启（同一份库、全新进程内状态）+ **无 session** 的到期消息，
  只要宿主报过在线就**真的发出去**——把"写回那一跳"去掉，这条必红；
* ④ 宿主没报过在线 → 同一份状态下仍按离线拦住，且理由可见（门控没被削弱）；
* ⑤ 拿不到宿主平台清单 → 一条可见 warn、一个字都不写、不抛异常（旧宿主不许炸）；
* 钩子不存在（老宿主）→ 加载不失败 + 点名 warn 一次（装饰器在类定义期求值，
  那里抛异常等于插件 import 不进来）。
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

# 先装 AstrBot 桩（`astrbot_bridge` / `main` 在 import 期就要它），再 import 被测模块。
from plugin.tests import test_astrbot_bridge as bridge_tests  # noqa: E402
from plugin.tests.test_endpoint_gate import _flush_state_writes  # noqa: E402
from plugin import main as main_module  # noqa: E402
from plugin.adapters import astrbot_bridge as bridge_module  # noqa: E402
from plugin.core import logging as interlude_logging  # noqa: E402
from plugin.core.database import Database  # noqa: E402
from plugin.core.types import (  # noqa: E402
    empty_participant_state,
    empty_story_setting,
    empty_story_state,
)

UTC = timezone.utc
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)

STORY_ID = 'character:onebot:100'
PARTICIPANT_ID = 'onebot:100:200'
ACCOUNT_KEY = 'onebot:100'

#: 宿主报过在线的原话（③/⑤ 都拿它当"宿主生命周期事件发生了"）。
HOST_REPORT = '_on_host_platform_loaded'

#: 写不回连接状态时那条 warn 的**独有**片段（与"缺钩子"那条区分开：两条都提到平台）。
SYNC_PROBLEM_WARN = '写不回去'


def _config() -> dict:
    return {
        'model': {}, 'runtime': {}, 'storyDefaults': {},
        'logging': {'level': 'debug', 'verbosity': 'diagnostic', 'format': 'layered'},
    }


class _HostPlatform:
    """宿主平台实例桩：`meta()` + `status`（真宿主是 `PlatformStatus` 枚举）。

    `status` 只影响"显式失败"（`error` / `stopped`）——`on_platform_loaded` 触发时
    真宿主的 `_task_wrapper` 往往还没跑、status 还是 `pending`，所以默认给 `running`。
    """

    def __init__(self, adapter: str, platform_id: str, status: str = 'running') -> None:
        self._meta = type('_Meta', (), {'name': adapter, 'id': platform_id})()
        self.status = status

    def meta(self):
        return self._meta


class _PlatformStatus:
    """真宿主 `PlatformStatus` 是枚举：`status.value` 才是字符串。"""

    def __init__(self, value: str) -> None:
        self.value = value


class _HostHookTestCase(unittest.IsolatedAsyncioTestCase):
    """真库 + 真服务 + 真桥的夹具（不落盘，`:memory:`）。"""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.logs: list[tuple[str, str]] = []
        self._sink = lambda level, text: self.logs.append((level, text))
        interlude_logging.set_log_sink(self._sink)
        self.addCleanup(interlude_logging.set_log_sink, interlude_logging._default_sink)
        # 插件装配时 `logger` 是宿主的 logger，服务层的报告会走它（`service_logger`
        # = ctx.logger），所以两处都要看：`self.logs` 是 interlude 的 sink，
        # `self.host_log` 是 AstrBot 那一侧。
        self.host_log = main_module.logger
        self._host_log_start = len(self.host_log.messages)

        self.db = Database(':memory:')
        self.addCleanup(self.db.close)
        self.db.register_tables()
        self.db.insert('interlude_story', {
            'id': STORY_ID, 'platform': 'onebot', 'selfId': '100', 'userId': '',
            'channelId': '', 'status': 'active', 'setting': empty_story_setting(),
            'state': empty_story_state(), 'cursorAt': NOW, 'createdAt': NOW, 'updatedAt': NOW,
        })
        self.db.insert('interlude_participant', {
            'id': PARTICIPANT_ID, 'storyId': STORY_ID, 'platform': 'onebot', 'selfId': '100',
            'userId': '200', 'channelId': 'private:200', 'personId': '200',
            'displayName': '主人', 'profile': '', 'relationship': '',
            'state': empty_participant_state(), 'status': 'active',
            'createdAt': NOW, 'updatedAt': NOW,
        })
        self.context = bridge_tests.FakeContext()
        self.context.platform_manager = bridge_tests.FakePlatformManager(
            [_HostPlatform('aiocqhttp', 'default')],
        )

    # ---- 构造（数据库与数据目录都钉住，绝不落盘） ----

    def _plugin(self, context=None, config=None):
        with mock.patch.object(bridge_module, 'Database', lambda path: self.db), \
                mock.patch.object(bridge_module, 'plugin_data_dir',
                                  lambda *a, **k: self._tmp.name):
            plugin = main_module.HDSInterludePlugin(
                context if context is not None else self.context, config or _config(),
            )
        plugin.bridge.db = self.db
        interlude_logging.set_log_sink(self._sink)
        return plugin

    def _warnings(self) -> list[str]:
        """本次用例新增的 warn 级文本（两处出口合并；报告不许只进"看不见"的通道）。"""
        host = [
            text for level, text in self.host_log.messages[self._host_log_start:]
            if level in ('warn', 'warning', 'error')
        ]
        return [text for level, text in self.logs if level in ('warn', 'warning', 'error')] + host

    @staticmethod
    def _endpoint_for(service, owner_kind: str, owner_id: str) -> str:
        for row in service.endpoint_rows:
            if row.get('ownerKind') == owner_kind and row.get('ownerId') == owner_id:
                return row['id']
        raise AssertionError('注册表里没有 %s:%s 的端点' % (owner_kind, owner_id))

    def _story(self) -> dict:
        return {'id': STORY_ID, 'platform': 'onebot', 'selfId': '100'}

    def _participant(self) -> dict:
        return self.db.get('interlude_participant', {'id': PARTICIPANT_ID})

    def _online(self, plugin, endpoint_id: str) -> bool:
        return plugin.bridge.service.endpoint_states[endpoint_id]['connection']['online']

    async def _deliver(self, plugin, content: str = '到期了') -> list:
        """到期消息的投递入口（`session=None` = 下一次入站之前）。"""
        return await plugin.bridge.service.send_outgoing_messages(
            self._story(), [{'participant_id': PARTICIPANT_ID, 'content': content}],
            self._participant(), None,
        )

    def _snapshot_count(self) -> int:
        return len(self.db.all('interlude_endpoint_state') or [])


# =========================================================================== #
# ① 写回当天状态 / ② 断开 → 离线
# =========================================================================== #

class HostConnectionWriteBackTests(_HostHookTestCase):
    """宿主平台生命周期 → `note_endpoint_connection`（上游 `service.ts:8021` 的调用方）。"""

    async def test_a_host_report_observes_the_endpoint_as_online(self) -> None:
        plugin = self._plugin()
        # 先把注册表装好（钩子自己也会做，幂等），好观察写回**之前**的保守值。
        await plugin.bridge.ensure_started()
        await plugin.bridge.service.ensure_endpoint_registry()
        endpoint_id = self._endpoint_for(
            plugin.bridge.service, 'participant-user', PARTICIPANT_ID,
        )
        self.assertIs(self._online(plugin, endpoint_id), False, '重启初值：连接一律按离线')
        self.assertEqual(
            plugin.bridge.service.endpoint_gate_reason(endpoint_id), 'endpoint-offline',
        )
        self.assertEqual(self._snapshot_count(), 0, '还没人写过状态快照')

        await getattr(plugin, HOST_REPORT)()  # = 宿主的事件回调（零参数）

        self.assertIs(self._online(plugin, endpoint_id), True, '宿主报到已加载 → 端点在线')
        self.assertIsNone(
            plugin.bridge.service.endpoint_gate_reason(endpoint_id),
            '在线 + fresh-start 例外 → 第一次投递放行',
        )
        await _flush_state_writes(plugin.bridge.service)
        self.assertEqual(
            self._snapshot_count(), len(plugin.bridge.service.endpoint_rows),
            '每一条端点行都落了在线快照',
        )
        # 桩环境本身会打两条与本次无关的 warn（拿不到配置 schema / 缺钩子，见
        # `test_a_missing_hook_is_named_in_a_visible_warning_exactly_once`），
        # 这里只钉"连接状态这条路没有抱怨"。
        self.assertEqual(
            [text for text in self._warnings() if SYNC_PROBLEM_WARN in text], [],
            '这条路不许有"写不回去"的抱怨：%s' % self._warnings(),
        )

    async def test_the_write_back_is_the_host_current_observation_not_a_restart_restore(self) -> None:
        """写回的是"宿主此刻加载着这个平台"，不是回读快照——两者判据分开可辨。"""
        first = self._plugin()
        await first.bridge.ensure_started()
        await first.bridge.service.ensure_endpoint_registry()
        endpoint_id = self._endpoint_for(first.bridge.service, 'participant-user', PARTICIPANT_ID)
        await getattr(first, HOST_REPORT)()
        await _flush_state_writes(first.bridge.service)
        self.assertIs(self._online(first, endpoint_id), True)

        # "重启"：同一份库、全新进程内状态 → 快照里明明记着在线，也一律归零。
        second = self._plugin()
        await second.bridge.ensure_started()
        await second.bridge.service.ensure_endpoint_registry()
        row = self.db.get('interlude_endpoint_state', {'endpointId': endpoint_id})
        self.assertIs(row['state']['connection']['online'], True, '快照真的记着在线')
        self.assertIs(self._online(second, endpoint_id), False, '绝不跨重启恢复在线事实')
        # 宿主再报一次 → 才重新在线（写回 = 现探，不是回读）。
        await getattr(second, HOST_REPORT)()
        self.assertIs(self._online(second, endpoint_id), True)

    async def test_a_platform_the_host_no_longer_loads_goes_offline(self) -> None:
        """**反向**：断开（宿主清单里没了 / 适配器 status=error）必须真翻成离线。"""
        plugin = self._plugin()
        endpoint_id = None
        await getattr(plugin, HOST_REPORT)()
        endpoint_id = self._endpoint_for(plugin.bridge.service, 'participant-user', PARTICIPANT_ID)
        self.assertIs(self._online(plugin, endpoint_id), True)

        # 1) 宿主清单里没有这个平台了（`terminate_platform` 不发事件，靠下一次现探）
        self.context.platform_manager = bridge_tests.FakePlatformManager([])
        await getattr(plugin, HOST_REPORT)()
        self.assertIs(self._online(plugin, endpoint_id), False, '平台没了 → 端点离线')
        self.assertEqual(
            plugin.bridge.service.endpoint_gate_reason(endpoint_id), 'endpoint-offline',
        )

        # 2) 适配器还在清单里但已显式失败（`PlatformStatus.ERROR`）→ 同样离线
        self.context.platform_manager = bridge_tests.FakePlatformManager(
            [_HostPlatform('aiocqhttp', 'default', status=_PlatformStatus('error'))],
        )
        await getattr(plugin, HOST_REPORT)()
        self.assertIs(self._online(plugin, endpoint_id), False, '适配器挂了 → 端点离线')

    async def test_an_unloaded_platform_does_not_take_its_siblings_down(self) -> None:
        """**反向**：多平台时，"B 还没加载"不许把已经加载的 A 一起判离线。"""
        self.db.insert('interlude_story', {
            'id': 'character:wechat:700', 'platform': 'wechat', 'selfId': '700', 'userId': '',
            'channelId': '', 'status': 'active', 'setting': empty_story_setting(),
            'state': empty_story_state(), 'cursorAt': NOW, 'createdAt': NOW, 'updatedAt': NOW,
        })
        self.context.platform_manager = bridge_tests.FakePlatformManager(
            [_HostPlatform('aiocqhttp', 'default')],
        )
        plugin = self._plugin()
        await getattr(plugin, HOST_REPORT)()
        service = plugin.bridge.service
        onebot = self._endpoint_for(service, 'story-role', STORY_ID)
        wechat = self._endpoint_for(service, 'story-role', 'character:wechat:700')
        self.assertIs(self._online(plugin, onebot), True, '加载着的平台在线')
        self.assertIs(self._online(plugin, wechat), False, '没加载的平台离线')


# =========================================================================== #
# ③ 端到端：重启后、下一次入站之前到期的消息
# =========================================================================== #

class RestartDueDeliveryTests(_HostHookTestCase):
    """本任务的意义：`session=None` 的到期消息，靠宿主生命周期那一跳才发得出去。"""

    async def _after_a_restart(self, context):
        """重启前的那个进程：宿主报过在线（快照里因此记着在线），然后换一个进程。"""
        first = self._plugin()
        await getattr(first, HOST_REPORT)()  # 钩子自己会装注册表
        await _flush_state_writes(first.bridge.service)
        # —— 重启：同一份库、全新进程内状态；**不再手工装注册表**，
        #    冷启动的加载就该由钩子那一跳自己完成（生产的时序就是这样）——
        return self._plugin(context=context)

    def _restart_context(self):
        context = bridge_tests.FakeContext()
        context.platform_manager = bridge_tests.FakePlatformManager(
            [_HostPlatform('aiocqhttp', 'default')],
        )
        return context

    async def test_a_due_message_goes_out_after_a_restart_once_the_host_reports(self) -> None:
        context = self._restart_context()
        second = await self._after_a_restart(context)

        # 宿主报到在线 —— 这就是平台生命周期钩子做的事（也是唯一的写回入口）。
        await getattr(second, HOST_REPORT)()

        delivered = await self._deliver(second)
        self.assertEqual([item['content'] for item in delivered], ['到期了'])
        self.assertEqual(len(context.sent), 1, '真的走出宿主投递口')
        umo, chain = context.sent[0]
        self.assertEqual(umo, 'default:FriendMessage:200', '投递坐标用宿主平台实例 id')
        self.assertEqual([getattr(part, 'text', '') for part in chain.chain], ['到期了'])

    async def test_a_due_message_stays_blocked_when_the_host_never_reports(self) -> None:
        """**反向**：同一条到期消息，宿主没报过在线 → 拦住 + 理由可见（门控没被削弱）。"""
        context = self._restart_context()
        second = await self._after_a_restart(context)

        self.assertEqual(await self._deliver(second), [])
        self.assertEqual(context.sent, [], '没有在线事实就不许投递')
        self.assertTrue(any(
            '消息被端点门控阻止' in text and 'endpoint-offline' in text
            for text in self._warnings()
        ), '被拒必须留可见理由：%s' % self._warnings())
        endpoint_id = self._endpoint_for(
            second.bridge.service, 'participant-user', PARTICIPANT_ID,
        )
        self.assertIs(self._online(second, endpoint_id), False, '重启后在线事实归零')


# =========================================================================== #
# ⑤ 拿不到宿主平台清单：可见 warn、一个字不写、不抛
# =========================================================================== #

class HostPlatformListUnavailableTests(_HostHookTestCase):
    """旧宿主 / 契约变了：看不见平台管理器时**不许猜**，也不许把全部端点判离线。"""

    async def test_an_unreachable_host_platform_list_warns_and_writes_nothing(self) -> None:
        context = bridge_tests.FakeContext()
        del context.platform_manager  # 宿主没暴露平台管理器
        plugin = self._plugin(context=context)

        await getattr(plugin, HOST_REPORT)()  # 不抛异常就是这条的前提之一

        self.assertTrue(any(
            '拿不到宿主当前的平台连接清单' in text for text in self._warnings()
        ), '必须点名 warn：%s' % self.logs)
        self.assertEqual(self._snapshot_count(), 0, '一个字都不能写（全判离线是新造一堵墙）')
        # 同一条原因只 warn 一次（钩子会触发好几次）。
        before = len(self._warnings())
        await getattr(plugin, HOST_REPORT)()
        self.assertEqual(len(self._warnings()), before, '同一条原因不重复刷屏')

    async def test_the_problem_is_reported_structurally_too(self) -> None:
        """桥的返回值要能分辨"看不见宿主"与"宿主一个平台都没有"。"""
        plugin = self._plugin()
        context_without_manager = bridge_tests.FakeContext()
        del context_without_manager.platform_manager
        with mock.patch.object(plugin.bridge, 'context', context_without_manager):
            result = await plugin.bridge.sync_host_endpoint_connections()
        self.assertIs(result['ok'], False)
        self.assertEqual(result['problem'], 'host-platform-list-unavailable')

        # 对照：宿主在、但一个平台都没加载 → ok=True（这是实情，不是故障）。
        plugin.bridge.context = bridge_tests.FakeContext()
        result = await plugin.bridge.sync_host_endpoint_connections()
        self.assertIs(result['ok'], True)
        self.assertEqual(result['loaded'], [])


# =========================================================================== #
# 钩子接线本身（宿主缺钩子 = 加载不失败 + 点名 warn）
# =========================================================================== #

class HostHookWiringTests(_HostHookTestCase):
    def test_both_host_hooks_are_decorated_in_the_class_body(self) -> None:
        """钉住用的是宿主**真实存在**的那两个钩子名（AST 读源码，不靠人眼）。"""
        source = pathlib.Path(main_module.__file__).read_text(encoding='utf-8')
        found: dict[str, str] = {}
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if (isinstance(decorator, ast.Call)
                        and isinstance(decorator.func, ast.Name)
                        and decorator.func.id == 'host_lifecycle_hook'
                        and decorator.args
                        and isinstance(decorator.args[0], ast.Constant)):
                    found[decorator.args[0].value] = node.name
        self.assertEqual(found, {
            'on_platform_loaded': '_on_host_platform_loaded',
            'on_astrbot_loaded': '_on_host_ready',
        })
        for name in found.values():
            self.assertTrue(
                inspect.iscoroutinefunction(getattr(main_module.HDSInterludePlugin, name)),
                '%s 必须是协程（宿主 `await handler.handler()`）' % name,
            )

    def test_the_real_host_hook_is_used_when_it_exists(self) -> None:
        """宿主有钩子时，拿到的是**工厂里面的装饰器**，不是工厂本人。

        ⚠️ v1.9.11 真机回归就死在这一条上（坑 86）：AstrBot 4.28 的
        `register_on_platform_loaded(**kwargs)`（`star_handler.py:337/347`）是
        **装饰器工厂**，必须 `@...on_platform_loaded()`。当时这条用例断言的是
        `assertIs(host_lifecycle_hook(...), fake_hook)` —— 把"返回工厂本身"这个
        **错误契约**钉死了，于是测试全绿、真机 `TypeError` 插件直接加载失败。
        现在断言的是"工厂被调用过一次，拿到里面的 decorator 并真的注册上"。
        """
        calls: list[dict] = []

        def fake_factory(**_kwargs):          # 与真宿主同形：工厂 + 内层装饰器
            calls.append(_kwargs)

            def decorator(func):
                func._hdsi_registered = True  # type: ignore[attr-defined]
                return func

            return decorator

        with mock.patch.object(
            main_module.filter, 'on_platform_loaded', fake_factory, create=True,
        ):
            hook = main_module.host_lifecycle_hook('on_platform_loaded')

        self.assertEqual(calls, [{}], '工厂必须被调用一次（真实宿主形态）')
        self.assertIsNot(hook, fake_factory, '不许把工厂本人当装饰器返回')

        def handler():
            return 'ok'

        registered = hook(handler)
        self.assertIs(registered, handler)
        self.assertTrue(getattr(handler, '_hdsi_registered', False), '装饰器必须真的生效')

    def test_a_bare_decorator_host_form_still_works(self) -> None:
        """老宿主 / 宿主改回裸装饰器形态时，`register()` 会抛 TypeError → 退回裸形态。"""
        def bare(func):
            func._hdsi_registered_bare = True  # type: ignore[attr-defined]
            return func

        with mock.patch.object(
            main_module.filter, 'on_astrbot_loaded', bare, create=True,
        ):
            hook = main_module.host_lifecycle_hook('on_astrbot_loaded')

        def handler():
            return 'ok'

        self.assertIs(hook(handler), handler)
        self.assertTrue(getattr(handler, '_hdsi_registered_bare', False))

    def test_a_missing_hook_degrades_to_a_no_op_decorator(self) -> None:
        """钩子不存在时装饰器必须原样放行（类定义期抛异常 = 插件 import 不进来）。"""
        with mock.patch.object(main_module, '_MISSING_HOST_HOOKS', []) as missing:
            hook = main_module.host_lifecycle_hook('on_platform_loaded_that_never_existed')

        def handler():
            return 'ok'

        self.assertIs(hook(handler), handler)
        self.assertEqual(missing, ['on_platform_loaded_that_never_existed'])

    def test_the_stub_host_really_lacks_both_hooks(self) -> None:
        """桩宿主的 `filter` 里**没有**这两个钩子——所以上面那条降级路径是真被走到的。

        少了这条，"缺钩子兜底"可能只是没被测到（而生产上老宿主一走就炸在
        类定义期：`@host_lifecycle_hook(...)` 拿到 `None` 直接 `TypeError`）。
        """
        for name in ('on_platform_loaded', 'on_astrbot_loaded'):
            self.assertIsNone(getattr(main_module.filter, name, None), name)
        self.assertEqual(
            sorted(main_module._MISSING_HOST_HOOKS),
            ['on_astrbot_loaded', 'on_platform_loaded'],
            '两个钩子都要被记账（记账 = 加载时会点名 warn）',
        )

    def test_a_missing_hook_is_named_in_a_visible_warning_exactly_once(self) -> None:
        logger_stub = main_module.logger
        before = len(logger_stub.messages)
        with mock.patch.object(main_module, '_MISSING_HOST_HOOKS', ['on_platform_loaded']), \
                mock.patch.object(main_module, '_MISSING_HOST_HOOKS_WARNED', set()):
            # 插件**照常加载**（这条断言本身就是"不许因此加载失败"）。
            plugin = self._plugin()
            plugin._warn_missing_host_hooks()  # 同一条原因第二次不刷屏
            fresh = [text for level, text in logger_stub.messages[before:]
                     if level == 'warning']
        self.assertEqual(
            len([text for text in fresh if 'on_platform_loaded' in text]), 1,
            '缺钩子必须**点名 warn 一次**：%s' % fresh,
        )
        self.assertTrue(any('被判离线拦下' in text for text in fresh), fresh)


if __name__ == '__main__':
    unittest.main()
