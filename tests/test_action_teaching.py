"""动作教学两段式（v1.9.9，§86）与超管判定的同步化（任务 2）。

用户点名的两段式：**先让她知道"屏幕上有哪些按钮"，她选定某个按钮之后，
再告诉她"这个界面怎么填"**（省 token）。这里钉三件事：

1. **第一段**（每回合注入）逐字不含任何参数说明——参数属于第二段；
2. 两段都按**同一份**可用集过滤（判据仍是 `available_platform_actions()` 一处）；
3. 选定之后**第二段**才给出那一条动作的参数表；每回合最多补问一次；
   任何失败/不可知都要**可见**（warn），不能静默变成"她以为点了"。

以及任务 2：`resolve_action_session_role` 曾经**同步探 async** 的 `is_super_admin`
→ 判定恒假 + `RuntimeWarning: coroutine ... was never awaited`。现在读同步名单。

每条铁律都配一条**反向用例**（关掉/改回去必须红），见 `ReverseMutationTests`。
"""

from __future__ import annotations

import asyncio
import gc
import pathlib
import sys
import unittest
import warnings
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from plugin.core import platform_actions as pa  # noqa: E402
from plugin.core.narrator_prompts import (  # noqa: E402
    MAX_PLATFORM_ACTIONS_PER_TURN,
    _PLATFORM_ACTION_INSTRUCTION,
    platform_action_instruction,
    platform_action_params_instruction,
    platform_action_shortlist_instruction,
)
from plugin.core.service.chunk12 import (  # noqa: E402
    PLATFORM_ACTION_FOLLOW_UP_MAX_PER_TURN,
    ServiceChunk12,
    close_awaitable,
    super_admin_ids_from_host,
)
from plugin.core.service.chunk3 import ServiceChunk3  # noqa: E402
from plugin.tests.test_platform_dispatch import _Host  # noqa: E402
#: 回合级夹具（第二段真的接在 `flush_group_turn` / `flush_buffered_narrative` 那条线上）。
from plugin.tests.test_service_chunk1 import (  # noqa: E402
    PRIVATE_STORY_ID,
    ServiceHarness,
    group_rule_stub,
    group_session,
)
from plugin.tests.test_service_chunk3 import NOW as PRIVATE_NOW  # noqa: E402
from plugin.tests.test_service_chunk3 import _FlushHost  # noqa: E402

ADMIN_ID = '900001'
OTHER_ID = '10001'

#: 第一段**绝不允许**出现的参数名（逐字断言用）：跨 §86 全部做参数的动作。
PARAM_NAMES = (
    'content', 'times', 'ugc_right', 'delay_minutes', 'send_at', 'entry_id',
    'message_id', 'group_id', 'user_id', 'target_uins', 'images', 'duration',
)

#: 第一段**绝不允许**出现的参数说明碎片（枚举 / 范围 / 必填标记）。
PARAM_HINTS = ('必填', ':int', ':list', ':bool', ':object', '[1~', '|', '必填')


class _Ctx:
    """最小 ctx（`interlude_data_dir()` 只读 `base_dir`）。"""

    def __init__(self, base_dir: str = '') -> None:
        self.base_dir = base_dir


class _Narrator:
    """第二段追问的宿主口（`select_platform_action_params`）。"""

    def __init__(self, receipt=None, *, raises: bool = False, delay: float = 0.0) -> None:
        self.receipt = receipt
        self.raises = raises
        self.delay = delay
        self.calls: list[tuple] = []

    async def select_platform_action_params(
        self, action_id, label, summary, spec, message, current,
    ):
        self.calls.append((action_id, label, summary, spec, message, current))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raises:
            raise RuntimeError('provider exploded')
        return self.receipt


class _RosterTransport:
    """只提供**同步**超管名单的传输层替身（`shape` 决定属性还是方法）。"""

    def __init__(self, ids=(), *, shape: str = 'method') -> None:
        self._ids = tuple(ids)
        self.shape = shape

    def known_super_admin_ids(self):
        return self._ids

    async def is_super_admin(self, user_id: str) -> bool:
        return str(user_id) in self._ids


class _PropertyRosterTransport(_RosterTransport):
    """名单口写成了**属性**（不是方法）：也要认得。"""

    def __init__(self, ids=(ADMIN_ID,)) -> None:
        super().__init__(ids)
        self.known_super_admin_ids = tuple(ids)


class _AsyncNameTransport:
    """接线错误：名单口写成了 `async def`（v1.9.9 的兼容口要接住它）。"""

    async def known_super_admin_ids(self):
        return (ADMIN_ID,)


class _AsyncLegacyTransport:
    """更老的接线：只有 `async def is_super_admin`，没有同步名单口。"""

    async def is_super_admin(self, user_id: str) -> bool:
        return str(user_id) == ADMIN_ID


class _BrokenNameTransport:
    """名单口存在但**不可调用**也不是名单（例如误写成字符串）：要可见地拒绝。"""

    known_super_admin_ids = 'not-a-callable'


class _ExplodingNameTransport:
    def known_super_admin_ids(self):
        raise RuntimeError('config unreadable')


def _admin_session(user_id: str = ADMIN_ID) -> dict:
    return {'userId': user_id, 'platform': 'aiocqhttp', 'selfId': '9'}


def _gather_warnings(callback, drop: Any = None) -> list[warnings.WarningMessage]:
    """在**捕获**模式下跑 callback 并强制一次 gc（未 await 的协程靠 GC 才报）。

    `drop` 是 callback 产物的持有者（例如装返回值的 list）：先清空它再 GC，
    否则那个协程还被引用着、永远不会被回收，警告也就不会出现。
    """
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        callback()
        if drop is not None:
            drop.clear()
        gc.collect()
    return [item for item in caught if issubclass(item.category, RuntimeWarning)]


class StageOneShortlistTests(unittest.TestCase):
    """第一段：只有"有哪些按钮"，**没有**参数（用户点名的省 token 要点）。"""

    def test_shortlist_has_no_parameter_words_at_all(self):
        host = _Host(config={})
        text = host.platform_action_shortlist_instruction()
        self.assertTrue(text)
        for name in PARAM_NAMES:
            with self.subTest(param=name):
                self.assertNotIn(name, text, '第一段里不允许出现参数名')
        for hint in PARAM_HINTS:
            with self.subTest(hint=hint):
                self.assertNotIn(hint, text, '第一段里不允许出现参数说明/枚举/范围/必填标记')

    def test_old_full_catalog_is_what_the_shortlist_is_measured_against(self):
        """对照组：全量参数目录**确实**带参数（否则上一条断言等于没测）。"""
        host = _Host(config={})
        old = host.platform_action_instruction()
        self.assertIn('content 必填', old)
        self.assertIn('times :int [1~20]', old)

    def test_shortlist_line_shape_is_id_plus_short_label(self):
        host = _Host(config={})
        text = host.platform_action_shortlist()
        self.assertIn('互动：', text)
        entry = [line for line in text.splitlines() if line.startswith('- send_poke')]
        self.assertEqual(entry, ['- send_poke 戳一戳'])

    def test_empty_availability_renders_nothing(self):
        self.assertEqual(pa.describe_action_shortlist([]), '')
        self.assertEqual(platform_action_shortlist_instruction(''), '')
        self.assertEqual(platform_action_instruction({'platformActions': []}), '')
        self.assertEqual(platform_action_instruction(None), '')
        self.assertEqual(platform_action_instruction({}), '')

    def test_shortlist_never_accepts_unrestricted_none(self):
        """`None`（“不限制”）是控制台预览口，**不是**注入口：注入路径不许回全量。"""
        self.assertIn('set_group_ban', pa.describe_actions(None))
        self.assertNotIn('set_group_ban', pa.describe_action_shortlist([]))

    def test_shortlist_carries_the_same_rules_as_the_old_header(self):
        """第一段的标题句仍要说清 wire 形状与上限（只是变短，不是丢约束）。"""
        text = platform_action_shortlist_instruction('互动：\n- send_poke 戳一戳')
        self.assertIn('"platformActions"', text)
        self.assertIn('at most %d per turn' % MAX_PLATFORM_ACTIONS_PER_TURN, text)
        self.assertIn('- send_poke 戳一戳', text)

    def test_the_two_entries_share_one_header_text(self):
        """两个入口的标题句是**同一份**（各写一遍必然分叉）。"""
        via_request = platform_action_instruction({'platformActions': ['send_poke']})
        via_catalog = platform_action_shortlist_instruction('互动：\n- send_poke 戳一戳')
        self.assertEqual(
            via_request.split('Available actions:', 1)[0],
            via_catalog.split('Available actions:', 1)[0],
        )


class StageOneFilterTests(unittest.TestCase):
    """第一段按**会话能力集**过滤（群聊动作不出现在私聊）。"""

    def test_group_only_actions_do_not_show_up_in_a_private_turn(self):
        host = _Host(config={})
        table = {'set_group_kick': 'global'}
        host.action_permission_table = lambda: dict(table)
        private = host.available_platform_actions('', ('private',))
        group = host.available_platform_actions('', ('group',))
        private_text = host.platform_action_shortlist(private)
        group_text = host.platform_action_shortlist(group)
        self.assertIn('- send_poke 戳一戳', private_text)
        self.assertNotIn('set_group_kick', private_text)
        self.assertNotIn('send_group_notice', private_text)
        self.assertIn('set_group_kick', group_text)

    def test_private_only_action_does_not_show_up_in_a_group_turn(self):
        host = _Host(config={})
        private_text = host.platform_action_shortlist(host.available_platform_actions('', ('private',)))
        group_text = host.platform_action_shortlist(host.available_platform_actions('', ('group',)))
        self.assertIn('get_friend_msg_history', private_text)
        self.assertNotIn('get_friend_msg_history', group_text)

    def test_switch_off_hides_the_action_from_the_shortlist(self):
        """第一段与第二段都吃配置开关：关掉的动作连"按钮"都不该出现。"""
        host = _Host(config={'robot_actions': {'chat': {'enabled': True, 'send_poke': False}}})
        text = host.platform_action_shortlist()
        self.assertNotIn('send_poke', text)
        self.assertIn('send_like', text)

    def test_available_set_is_computed_in_one_place(self):
        """两段用的是**同一份**可用集：独立算一遍就会分叉。"""
        host = _Host(config={})
        called: list[str] = []
        original = host.available_platform_actions

        def spy(*args, **kwargs):
            called.append('x')
            return original(*args, **kwargs)

        host.available_platform_actions = spy
        available = host.available_platform_actions()
        self.assertEqual(len(called), 1)
        self.assertIn('send_poke', pa.describe_action_shortlist(available))
        self.assertIn('send_poke', available)


class StageTwoParamsTests(unittest.TestCase):
    """第二段：选定之后给"这个界面怎么填"。"""

    def test_params_form_carries_the_parameter_table(self):
        text = platform_action_params_instruction(
            'publish_qzone_post',
            pa.ACTIONS['publish_qzone_post'].summary,
            pa.describe_action_params(['publish_qzone_post']),
        )
        self.assertIn('publish_qzone_post', text)
        self.assertIn('content 必填 正文', text)
        self.assertIn('所有人可见|仅 QQ 好友可见', text)
        self.assertIn('Parameter form:', text)

    def test_params_form_is_only_for_the_named_action(self):
        """第二段不把别的动作的参数表一起带出来（那等于回到全量目录）。"""
        text = platform_action_params_instruction(
            'send_poke', pa.ACTIONS['send_poke'].summary, pa.describe_action_params(['send_poke']),
        )
        self.assertIn('send_poke', text)
        self.assertNotIn('publish_qzone_post', text)
        self.assertNotIn('ugc_right', text)

    def test_params_form_needs_both_the_id_and_the_spec(self):
        self.assertEqual(platform_action_params_instruction('', '', ''), '')
        self.assertEqual(platform_action_params_instruction('send_poke', '', ''), '')
        self.assertEqual(platform_action_params_instruction('nosuch', 'x', 'form'), '')
        self.assertNotEqual(
            platform_action_params_instruction('send_poke', 'x', 'send_poke：戳一戳'), '',
        )

    def test_missing_params_detection(self):
        host = _Host(config={})
        self.assertEqual(
            host.platform_action_missing_params({'platformActions': ['publish_qzone_post']}),
            'publish_qzone_post',
        )
        # 对象里没有 params 键 → 要补
        self.assertEqual(
            host.platform_action_missing_params({'platformActions': [{'action': 'send_like'}]}),
            'send_like',
        )
        # params 显式写了（哪怕空对象）= 模型表过态，不再补问
        self.assertIsNone(
            host.platform_action_missing_params({'platformActions': [{'action': 'send_like', 'params': {}}]}),
        )
        # 已经写全了（必填齐）→ 不需要补；校验层会照常判
        self.assertIsNone(host.platform_action_missing_params(
            {'platformActions': [{'action': 'send_like', 'params': {'user_id': OTHER_ID, 'times': 2}}]},
        ))
        # 未启用 / 未知 / 不是动作 → 都不是补问对象
        host_off = _Host(config={'robot_actions': {'chat': {'send_poke': False}}})
        self.assertIsNone(host_off.platform_action_missing_params({'platformActions': ['send_poke']}))
        self.assertIsNone(host.platform_action_missing_params({'platformActions': ['nosuch_action']}))
        self.assertIsNone(host.platform_action_missing_params({}))
        self.assertIsNone(host.platform_action_missing_params(None))

    def test_missing_params_takes_only_the_first_draft(self):
        host = _Host(config={})
        first = host.platform_action_missing_params(
            {'platformActions': ['send_like', 'publish_qzone_post']},
        )
        self.assertEqual(first, 'send_like')

    def test_parse_rejects_a_different_action(self):
        """第一段文本是最终有效的：第二段回执换了动作 id 整条不要。"""
        host = _Host(config={})
        self.assertIsNone(host.parse_platform_action_params(
            {'action': 'publish_qzone_post', 'params': {'content': '早'}}, 'send_like',
        ))
        self.assertIsNone(host.parse_platform_action_params(None, 'send_like'))
        self.assertIsNone(host.parse_platform_action_params({'action': 'send_like'}, 'send_like'))

    def test_parse_validates_parameters(self):
        host = _Host(config={})
        ok = host.parse_platform_action_params(
            {'action': 'send_like', 'params': {'user_id': OTHER_ID, 'times': 3}}, 'send_like',
        )
        self.assertEqual(ok, {'action': 'send_like', 'params': {'user_id': OTHER_ID, 'times': 3}})
        # 越界 → 拒绝（校验口还是 validate_action 那一处）
        self.assertIsNone(host.parse_platform_action_params(
            {'action': 'send_like', 'params': {'user_id': OTHER_ID, 'times': 99}}, 'send_like',
        ))
        # camelCase 参数写法照旧认
        camel = host.parse_platform_action_params(
            {'action': 'send_like', 'params': {'userId': OTHER_ID, 'times': 1}}, 'send_like',
        )
        self.assertEqual(camel['params']['user_id'], OTHER_ID)

    def test_parse_keeps_the_parameters_she_already_wrote(self):
        host = _Host(config={})
        merged = host.parse_platform_action_params(
            {'action': 'send_like', 'params': {'times': 3}},
            'send_like',
            {'user_id': OTHER_ID},
        )
        self.assertEqual(merged['params'], {'user_id': OTHER_ID, 'times': 3})

    def test_apply_writes_back_to_the_same_dict(self):
        """两种拼写必须指回**同一个**对象（跨 chunk 双读的老规矩，坑 41/46）。"""
        decision = {'platformActions': ['send_like']}
        decision['platform_actions'] = decision['platformActions']
        host = _Host(config={})
        action = {'action': 'send_like', 'params': {'user_id': OTHER_ID, 'times': 2}}
        self.assertEqual(host.apply_platform_action_params(decision, action), 'send_like')
        self.assertIs(decision['platform_actions'], decision['platformActions'])
        self.assertEqual(decision['platformActions'], [action])
        # 已经是对象但没写 params 的那种草稿也要能写回
        draft = {'platformActions': [{'action': 'send_like'}]}
        self.assertEqual(host.apply_platform_action_params(draft, action), 'send_like')
        self.assertEqual(draft['platformActions'], [action])
        # 对不上 / 已经写了参数 → 不写
        other = {'platformActions': [{'action': 'send_like', 'params': {'times': 1}}]}
        self.assertIsNone(host.apply_platform_action_params(other, action))
        self.assertEqual(other['platformActions'], [{'action': 'send_like', 'params': {'times': 1}}])


class StageTwoTriggerTests(unittest.IsolatedAsyncioTestCase):
    """第二段的触发、成功回填与每一次失败（都要**可见**）。"""

    def setUp(self):
        self.host = _Host(config={})

    async def test_follow_up_asks_once_and_fills_the_params(self):
        narrator = _Narrator({'action': 'send_like', 'params': {'user_id': OTHER_ID, 'times': 4}})
        self.host.narrator = narrator
        self.host.narrator_reports = []
        self.host.report_standalone = lambda level, message, *args: self.host.narrator_reports.append(
            (level, message % args if args else message),
        )
        decision = {'platformActions': ['send_like']}
        budget: dict = {}
        result = await self.host.resolve_platform_action_params(
            decision, follow_up_budget=budget, message='在吗',
        )
        self.assertEqual(result, 'send_like')
        self.assertEqual(budget['count'], 1)
        self.assertEqual(len(narrator.calls), 1)
        action_id, label, summary, spec, message, current = narrator.calls[0]
        self.assertEqual(action_id, 'send_like')
        self.assertEqual(label, '点赞')
        self.assertIn('times :int [1~20]', spec)
        self.assertEqual(message, '在吗')
        self.assertEqual(current, {})
        self.assertEqual(
            decision['platformActions'],
            [{'action': 'send_like', 'params': {'user_id': OTHER_ID, 'times': 4}}],
        )
        self.assertEqual(self.host.narrator_reports, [])

    async def test_budget_caps_the_extra_call_at_one_per_turn(self):
        narrator = _Narrator({'action': 'send_like', 'params': {'user_id': OTHER_ID, 'times': 4}})
        self.host.narrator = narrator
        reports: list = []
        self.host.report_standalone = lambda level, message, *args: reports.append(
            (level, message % args if args else message),
        )
        decision = {'platformActions': ['send_like']}
        budget: dict = {}
        self.assertIsNotNone(await self.host.resolve_platform_action_params(
            decision, follow_up_budget=budget,
        ))
        # 第二次：同一回合额度已用完 → 不再调用，且要说明
        decision2 = {'platformActions': ['send_like']}
        self.assertIsNone(await self.host.resolve_platform_action_params(
            decision2, follow_up_budget=budget,
        ))
        self.assertEqual(len(narrator.calls), 1)
        self.assertEqual(budget['count'], PLATFORM_ACTION_FOLLOW_UP_MAX_PER_TURN)
        self.assertTrue(any(level == 'warn' and '额度' in text for level, text in reports))

    async def test_progress_preserved_after_successful_follow_up(self):
        """补问成功后**不能再补问**：否则同一回合无限续杯。"""
        narrator = _Narrator({'action': 'send_like', 'params': {'user_id': OTHER_ID, 'times': 1}})
        self.host.narrator = narrator
        report = lambda *args: None  # noqa: E731
        self.host.report_standalone = report
        budget: dict = {}
        await self.host.resolve_platform_action_params(
            {'platformActions': ['send_like']}, follow_up_budget=budget,
        )
        await self.host.resolve_platform_action_params(
            {'platformActions': ['send_like']}, follow_up_budget=budget,
        )
        self.assertEqual(len(narrator.calls), 1)

    async def test_missing_capability_is_visible_and_actionable(self):
        self.host.narrator = object()  # 没有 select_platform_action_params
        reports: list = []
        self.host.report_standalone = lambda level, message, *args: reports.append(
            (level, message % args if args else message),
        )
        self.assertIsNone(await self.host.resolve_platform_action_params(
            {'platformActions': ['publish_qzone_post']}, follow_up_budget={},
        ))
        self.assertEqual(len(reports), 1)
        level, text = reports[0]
        self.assertEqual(level, 'warn')
        self.assertIn('publish_qzone_post', text)
        self.assertIn('不可用', text)

    async def test_timeout_and_exception_are_visible(self):
        for narrator, needle in (
            (_Narrator({'action': 'send_like'}, delay=5.0), '超时'),
            (_Narrator(raises=True), '失败'),
        ):
            with self.subTest(needle=needle):
                host = _Host(config={})
                host.narrator = narrator
                reports: list = []
                host.report_standalone = lambda level, message, *args: reports.append(
                    (level, message % args if args else message),
                )
                with unittest.mock.patch(
                    'plugin.core.service.chunk12.PLATFORM_ACTION_FOLLOW_UP_TIMEOUT_SECONDS', 0.01,
                ):
                    result = await host.resolve_platform_action_params(
                        {'platformActions': ['send_like']}, follow_up_budget={},
                    )
                self.assertIsNone(result)
                self.assertTrue(any(level == 'warn' and needle in text for level, text in reports),
                                '失败/超时必须留可见记录：%r' % (reports,))

    async def test_unusable_receipt_is_visible(self):
        """回执换了动作 / 参数仍不全 / 不是对象 → 全部留可见记录，不静默。"""
        for receipt in (
            {'action': 'publish_qzone_post', 'params': {'content': '早'}},
            {'action': 'send_like', 'params': {'times': 99}},
            {'action': 'send_like'},
            None, 'not-json',
        ):
            with self.subTest(receipt=receipt):
                host = _Host(config={})
                host.narrator = _Narrator(receipt)
                reports: list = []
                host.report_standalone = lambda level, message, *args: reports.append(
                    (level, message % args if args else message),
                )
                result = await host.resolve_platform_action_params(
                    {'platformActions': ['send_like']}, follow_up_budget={},
                )
                self.assertIsNone(result)
                self.assertTrue(any(level == 'warn' for level, _ in reports))

    async def test_no_missing_params_means_no_extra_call(self):
        narrator = _Narrator({'action': 'send_like', 'params': {'user_id': OTHER_ID, 'times': 1}})
        self.host.narrator = narrator
        self.assertIsNone(await self.host.resolve_platform_action_params(
            {'platformActions': [{'action': 'send_like', 'params': {'user_id': OTHER_ID}}]},
            follow_up_budget={},
        ))
        self.assertEqual(narrator.calls, [], '没有要补的动作就不该多发一次调用')

    async def test_failed_action_still_flows_into_the_script(self):
        """动作结果必须回流剧本（失败可见且可行动）。"""
        host = _Host(config={})
        host.narrator = _Narrator({'action': 'send_like', 'params': {'user_id': OTHER_ID, 'times': 1}})
        host.report_standalone = lambda *args, **kwargs: None
        decision = {'platformActions': [{'action': 'send_like', 'params': {'user_id': OTHER_ID, 'times': 1}}]}
        outcomes = await host.dispatch_platform_actions({'id': 's1'}, decision, session=None)
        self.assertTrue(outcomes)
        self.assertTrue(host.entries, '动作结果必须写进剧本（她下一回合才看得见）')
        self.assertEqual(host.entries[-1]['content'].split(']')[0] + ']', '[平台动作]')


class TokenComparisonTests(unittest.TestCase):
    """④ 注入长度对比：**出数字**（写进 `docs/PORTING_NOTES.md` §86）。"""

    def test_shortlist_is_materially_shorter_than_the_full_catalog(self):
        host = _Host(config={})
        available = host.available_platform_actions()
        old = host.platform_action_instruction(available)
        new = host.platform_action_shortlist_instruction(available)
        self.assertLess(len(new), len(old) * 0.75, '第一段必须明显短于全量参数目录')
        self.assertLess(len(new), 2200)
        # 数字本身也要钉住（文档里的对比表读的就是这两条）
        self.assertGreater(len(old), 3000)
        self.assertLess(len(new), 2100)
        # 第二段只在"她真的挑了那条"时出现，且只含那一条
        second = platform_action_params_instruction(
            'publish_qzone_post', pa.ACTIONS['publish_qzone_post'].summary,
            pa.describe_action_params(['publish_qzone_post']),
        )
        self.assertLess(len(second), 1200)

    def test_the_header_did_not_get_longer(self):
        """标题句是每回合都付的那一份：只能变短。"""
        self.assertLess(len(_PLATFORM_ACTION_INSTRUCTION), 900)


class SuperAdminRoleTests(unittest.TestCase):
    """任务 2 的用例 ①②：超管 → 真；非超管 → 假。"""

    def test_super_admin_session_resolves_to_admin(self):
        host = _Host(config={})
        host.transport = _RosterTransport([ADMIN_ID])
        self.assertEqual(host.resolve_action_session_role(_admin_session()), 'admin')

    def test_non_admin_session_resolves_to_no_role(self):
        host = _Host(config={})
        host.transport = _RosterTransport([ADMIN_ID])
        self.assertEqual(host.resolve_action_session_role(_admin_session(OTHER_ID)), '')

    def test_explicit_group_role_still_wins(self):
        host = _Host(config={})
        host.transport = _RosterTransport([ADMIN_ID])
        self.assertEqual(host.resolve_action_session_role({'role': 'owner', 'userId': OTHER_ID}), 'owner')
        # super 写法归一成 admin（既有口径，不动）
        self.assertEqual(host.resolve_action_session_role({'role': 'super', 'userId': OTHER_ID}), 'admin')

    def test_missing_or_empty_roster_means_no_permission(self):
        for transport in (None, _RosterTransport([]), _BrokenNameTransport(), _ExplodingNameTransport()):
            with self.subTest(transport=type(transport).__name__ if transport else None):
                host = _Host(config={})
                host.transport = transport
                reports: list = []
                host.report_standalone = lambda level, message, *args: reports.append(
                    (level, message % args if args else message),
                )
                self.assertEqual(host.resolve_action_session_role(_admin_session()), '')
                if isinstance(transport, _BrokenNameTransport):
                    self.assertTrue(reports, '不可调用的名单口必须可见')

    def test_property_shaped_roster_is_accepted(self):
        host = _Host(config={})
        host.transport = _PropertyRosterTransport()
        self.assertEqual(host.resolve_action_session_role(_admin_session()), 'admin')

    def test_admin_tier_action_becomes_available_for_a_super_admin(self):
        """判定要真的影响"能调什么"（否则修了等于没修）。"""
        table = {'set_group_kick': 'admin'}
        host = _Host(config={})
        host.transport = _RosterTransport([ADMIN_ID])
        host.action_permission_table = lambda: dict(table)
        private_role = host.resolve_action_session_role(_admin_session())
        other_role = host.resolve_action_session_role(_admin_session(OTHER_ID))
        self.assertEqual(private_role, 'admin')
        self.assertEqual(other_role, '')
        self.assertIn('set_group_kick', host.available_platform_actions(private_role, ('group',)))
        self.assertNotIn('set_group_kick', host.available_platform_actions(other_role, ('group',)))

    def test_roster_normalization_refuses_junk(self):
        class _Strings:
            known_super_admin_ids = ADMIN_ID

        class _Ints:
            known_super_admin_ids = int(ADMIN_ID)

        self.assertEqual(super_admin_ids_from_host(_Strings()), ())
        self.assertEqual(super_admin_ids_from_host(_Ints()), ())
        self.assertEqual(super_admin_ids_from_host(None), ())
        self.assertEqual(super_admin_ids_from_host(object()), ())
        self.assertEqual(
            super_admin_ids_from_host(_RosterTransport([ADMIN_ID, None, ''])), (ADMIN_ID,),
        )


class SuperAdminWarningTests(unittest.TestCase):
    """任务 2 的用例 ③④：不再出现 `coroutine ... was never awaited`；反向则红。"""

    def test_no_unawaited_coroutine_warning_on_the_production_path(self):
        host = _Host(config={})
        host.transport = _RosterTransport([ADMIN_ID])
        host.report_standalone = lambda *args, **kwargs: None
        caught = _gather_warnings(lambda: host.resolve_action_session_role(_admin_session()))
        self.assertEqual([str(item.message) for item in caught], [])

    def test_no_unawaited_coroutine_warning_with_null_transport(self):
        """生产默认就是空传输层：这条路径以前每次留一条 RuntimeWarning。"""
        from plugin.core.service.transport import NullTransport

        host = _Host(config={})
        host.transport = NullTransport()
        host.report_standalone = lambda *args, **kwargs: None
        caught = _gather_warnings(lambda: host.resolve_action_session_role(_admin_session()))
        self.assertEqual([str(item.message) for item in caught], [])

    def test_reverse_a_sync_probe_of_an_async_method_does_warn(self):
        """反向：**同步探 async** 是原 bug —— 这里复现它的形状（必须红）。

        这就是 `chunk12.py` 修复前的写法：`if probe(user_id): ...`，而 `probe` 是
        `async def`。协程没人 await → GC 时一条 `RuntimeWarning`，且判定恒假。
        """
        host = _Host(config={})
        host.transport = _AsyncLegacyTransport()
        host.report_standalone = lambda *args, **kwargs: None

        probe = getattr(host.transport, 'is_super_admin', None)
        results: list = []

        def mutation():
            results.append(probe(ADMIN_ID))  # 同步探 async —— 原 bug 的形状

        caught = _gather_warnings(mutation, drop=results)
        self.assertTrue(caught, '原 bug 的形状必须留下 RuntimeWarning（否则这条反向用例没测到东西）')
        self.assertIn('never awaited', str(caught[0].message))

    def test_legacy_async_only_transport_does_not_warn(self):
        """兼容口：宿主只留了异步方法时，我们关掉协程并留一条可见 warn。"""
        host = _Host(config={})
        host.transport = _AsyncLegacyTransport()
        reports: list = []
        host.report_standalone = lambda level, message, *args: reports.append(
            (level, message % args if args else message),
        )
        caught = _gather_warnings(lambda: host.resolve_action_session_role(_admin_session()))
        self.assertEqual([str(item.message) for item in caught], [])
        self.assertEqual(host.resolve_action_session_role(_admin_session()), '')
        self.assertTrue(any(level == 'warn' and 'known_super_admin_ids' in text for level, text in reports))

    def test_async_shaped_roster_is_closed_silently(self):
        host = _Host(config={})
        host.transport = _AsyncNameTransport()
        host.report_standalone = lambda *args, **kwargs: None
        caught = _gather_warnings(lambda: host.resolve_action_session_role(_admin_session()))
        self.assertEqual([str(item.message) for item in caught], [])
        self.assertEqual(host.resolve_action_session_role(_admin_session()), '')

    def test_close_awaitable_only_closes_coroutines(self):
        async def _run():
            return 1

        coro = _run()
        self.assertTrue(close_awaitable(coro))
        self.assertTrue(asyncio.iscoroutine(coro) and coro.cr_frame is None)
        self.assertFalse(close_awaitable('not-a-coroutine'))
        self.assertFalse(close_awaitable(None))


class ReverseMutationTests(unittest.TestCase):
    """⑤ 反向：把清单改回全量参数 / 第二段缺失 / 超管改回同步探 async → 必须红。"""

    def test_reverse_catalog_back_to_full_parameters_fails_the_guard(self):
        """① 把第一段清单换回全量参数目录 → 第一段的逐字断言当场红。"""

        def guard(text: str) -> None:
            if 'content 必填' in text or 'times :int [1~20]' in text:
                raise AssertionError('第一段里出现了参数说明（token 回归）')

        host = _Host(config={})
        guard(host.platform_action_shortlist_instruction())  # 现状：过
        with self.assertRaises(AssertionError):
            guard(host.platform_action_instruction())        # 变异：红

    def test_reverse_guard_would_fail_if_the_shortlist_were_unfiltered(self):
        """反向：清单不吃可用集（回全量）时，长度与"没参数"两条断言一起红。"""
        host = _Host(config={})
        available = host.available_platform_actions()
        unfiltered = pa.describe_actions(None)
        current = host.platform_action_shortlist_instruction(available)
        self.assertLess(len(current), 2200)
        self.assertGreater(len(unfiltered), 4000, '不限制 = 全量目录，明显更长')
        with self.assertRaises(AssertionError):
            self.assertLess(len(unfiltered), 2200)

    def test_reverse_missing_second_stage_yields_no_parameter_form(self):
        """反向：第二段缺失（不调 `describe_action_params`）→ 参数表断言红。"""
        host = _Host(config={})
        without_form = host.platform_action_shortlist()
        self.assertNotIn('必填', without_form)
        with self.assertRaises(AssertionError):
            self.assertIn('content 必填', without_form)

    def test_reverse_removing_the_follow_up_makes_required_params_unfillable(self):
        """反向：没有第二段，`publish_qzone_post` 的必填参数永远填不出来（动作作废）。"""
        host = _Host(config={})
        host.report_standalone = lambda *args, **kwargs: None
        self.assertEqual(host.parse_platform_action_params(
            {'action': 'publish_qzone_post', 'params': {}}, 'publish_qzone_post',
        ), None)
        # 有了第二段给的参数就能过
        self.assertIsNotNone(host.parse_platform_action_params(
            {'action': 'publish_qzone_post', 'params': {'content': '今天的云很好看'}},
            'publish_qzone_post',
        ))


def _noop_sync(*_args: Any, **_kwargs: Any) -> None:
    return None


async def _noop(*_args: Any, **_kwargs: Any) -> None:
    return None


async def _noop_false(*_args: Any, **_kwargs: Any) -> bool:
    return False


async def _empty_list(*_args: Any, **_kwargs: Any) -> list:
    return []


class _ActionFlushHost(_FlushHost, ServiceChunk12):
    """私聊夹具（`test_service_chunk3` 的真回合宿主）+ **真** chunk12 的第二段。

    执行侧换成记录式 dispatcher：本文件只钉"补问发生在 dispatch 之前、参数已回填"，
    动作真被执行那件事由群聊用例（真 `dispatch_platform_actions`）覆盖。
    """

    async def dispatch_platform_actions(self, story: Any, decision: Any, **_kwargs: Any) -> list:
        self.dispatch_calls = getattr(self, 'dispatch_calls', [])
        self.dispatch_calls.append((story, decision))
        return []


class GroupTurnFollowUpTests(ServiceHarness):
    """群聊（`chunk1.flush_group_turn`）：第二段真的接在 `dispatch_platform_actions` 之前。

    §86.4 第 3 条：`resolve_platform_action_params(decision, follow_up_budget=…)`——
    每回合一个**新的**空预算（最多多问一次）；她只写了动作 id 时才补问，
    `params: {}` 是"模型表过态"不补问；补上的参数必须真的走到执行（留痕 `[平台动作]`）。
    """

    #: 群回合的会话坐标：**生产形状**（`SessionView`，与 `astrbot_bridge.session_view()`
    #: 一样把群 id 同时放进 `channel_id` 与 `guild_id`、`is_direct=False`）。
    #: 这里刻意**不**用 `{'groupId': '9'}` 那种 dict——那种夹具会让"群"在坐标里白给，
    #: 掩盖"群 id 认不出来"的真 bug（坑 39/46/66）。
    def _session(self) -> Any:
        return group_session(channel_id='9', guild_id='9')

    def _turn(self, decision: dict, narrator: Any, config: dict | None = None) -> tuple:
        service = self.make_service(config)
        self.make_story()
        self._put_turn(service, decision, revision=3)
        service.narrator = narrator
        sent: list = []
        #: 第二回合要换一份**新的**决策草稿（第一回合那份已经被第二段回填过了）。
        box = {'decision': decision}

        async def decide(*_args: Any, **_kwargs: Any) -> dict:
            return {'decision': box['decision'], 'succeeded': True}

        async def persist(*_args: Any, **_kwargs: Any) -> dict:
            return {'messages': [], 'commit': None, 'scriptEntry': None, 'script_entry': None}

        async def send(*args: Any, **kwargs: Any) -> dict:
            sent.append((args, kwargs))
            return {'deliveredSegments': [], 'complete': True, 'segmentOutcomes': []}

        service.try_decide = decide
        service.persist_decision = persist
        service.send_group_message = send
        service.semantic_turn_embedding_enabled = lambda: False
        service.sticker_catalog_for_session = _empty_list
        service.group_chat_capabilities = lambda _session, _messages: None
        service.schedule_compaction = _noop_sync
        service.update_script_delivery_outcome = _noop
        service.group_cooldown_active = _noop_false
        return service, {'sent': sent, 'box': box}

    def _put_turn(self, service: Any, decision: dict, revision: int) -> None:
        service.buffered_group_turns['key'] = {
            'story_id': PRIVATE_STORY_ID, 'group_id': '9',
            'rule': group_rule_stub(debounceSeconds=0),
            'channel_id': '9', 'latest_session': self._session(),
            'messages': [{'content': '在吗'}], 'revision': revision,
            'mentioned_bot': False, 'quoted_bot': False,
        }

    def _action_entries(self) -> list[dict]:
        return [
            row for row in self.rows('interlude_script_entry', {'storyId': PRIVATE_STORY_ID})
            if str(row.get('content') or '').startswith('[平台动作]')
        ]

    # ---- 触发一次 → 参数回填 → 动作真的执行 ----

    async def test_a_chosen_action_triggers_one_follow_up_and_then_runs(self):
        narrator = _Narrator({'action': 'send_group_notice', 'params': {'content': '今晚八点开黑'}})
        decision = {'groupReply': {'mode': 'none'}, 'platformActions': ['send_group_notice']}
        service, ctx = self._turn(decision, narrator)
        await service.flush_group_turn('key', 3)

        self.assertEqual([call[0] for call in narrator.calls], ['send_group_notice'])
        self.assertEqual(
            decision['platformActions'],
            [{'action': 'send_group_notice', 'params': {'content': '今晚八点开黑'}}],
            '第二段只填参数槽：动作 id 与形状都写回模型那份草稿',
        )
        entries = self._action_entries()
        self.assertTrue(entries, '参数补上之后动作必须真的走到执行（留痕 `[平台动作]`）')
        self.assertIn('发群公告', entries[-1]['content'])
        self.assertNotIn(
            '平台动作被拒绝', self.sink.text(),
            '群回合的坐标必须认出"群"：仅群聊动作不该被作用域拒掉（生产形状的 SessionView）',
        )

    async def test_params_already_written_run_without_a_follow_up(self):
        narrator = _Narrator({'action': 'send_group_notice', 'params': {'content': '不该被用到'}})
        decision = {
            'groupReply': {'mode': 'none'},
            'platformActions': [
                {'action': 'send_group_notice', 'params': {'content': '今晚八点开黑'}},
            ],
        }
        service, ctx = self._turn(decision, narrator)
        await service.flush_group_turn('key', 3)
        self.assertEqual(narrator.calls, [], '参数已经写全 → 不许再烧一次调用')
        self.assertTrue(self._action_entries())

    async def test_an_empty_params_object_means_no_follow_up_call(self):
        """`params: {}` 是模型明确的"没有参数"，不是"要补"（烧一次调用也补不出来）。"""
        narrator = _Narrator({'action': 'send_group_notice', 'params': {'content': '今晚八点开黑'}})
        decision = {
            'groupReply': {'mode': 'none'},
            'platformActions': [{'action': 'send_group_notice', 'params': {}}],
        }
        service, ctx = self._turn(decision, narrator)
        await service.flush_group_turn('key', 3)
        self.assertEqual(narrator.calls, [])
        self.assertEqual(self._action_entries(), [])
        self.assertIn('平台动作被拒绝', self.sink.text())

    async def test_every_turn_gets_a_fresh_follow_up_budget(self):
        """每回合一个**新的**空预算：第二回合照样能补问（预算提到回合外就是一次都不问）。"""
        narrator = _Narrator({'action': 'send_group_notice', 'params': {'content': '今晚八点开黑'}})
        decision = {'groupReply': {'mode': 'none'}, 'platformActions': ['send_group_notice']}
        service, ctx = self._turn(decision, narrator)
        await service.flush_group_turn('key', 3)
        second = {'groupReply': {'mode': 'none'}, 'platformActions': ['send_group_notice']}
        ctx['box']['decision'] = second
        self._put_turn(service, second, revision=4)
        await service.flush_group_turn('key', 4)
        self.assertEqual(len(narrator.calls), 2)

    # ---- 失败可见、本回合照跑 ----

    async def test_a_mismatched_receipt_is_refused_and_visible(self):
        """回执换了动作 → 拒绝 + warn；第一段那一条按参数不足处理（绝不被顶替）。"""
        narrator = _Narrator({'action': 'set_group_card', 'params': {'card': '阿猫'}})
        decision = {'groupReply': {'mode': 'none'}, 'platformActions': ['send_group_notice']}
        service, ctx = self._turn(decision, narrator)
        await service.flush_group_turn('key', 3)
        self.assertEqual(len(narrator.calls), 1, '拒绝了就不再重试（每回合最多一次）')
        self.assertEqual(decision['platformActions'], ['send_group_notice'], '第一段是最终有效的')
        text = self.sink.text()
        self.assertIn('动作参数补问回执不可用', text)
        self.assertIn('send_group_notice', text)

    async def test_a_timeout_still_lets_the_turn_finish_with_a_warning(self):
        narrator = _Narrator(
            {'action': 'send_group_notice', 'params': {'content': '今晚八点开黑'}}, delay=5.0,
        )
        decision = {
            'groupReply': {'mode': 'immediate', 'content': '好'},
            'platformActions': ['send_group_notice'],
        }
        service, ctx = self._turn(decision, narrator)
        with unittest.mock.patch(
            'plugin.core.service.chunk12.PLATFORM_ACTION_FOLLOW_UP_TIMEOUT_SECONDS', 0.01,
        ):
            await service.flush_group_turn('key', 3)
        self.assertNotIn('key', service.buffered_group_turns, '本回合照跑（缓冲照常清空）')
        self.assertTrue(ctx['sent'], '可见正文照发')
        text = self.sink.text()
        self.assertIn('动作参数补问超时', text)
        self.assertIn('send_group_notice', text)


class PrivateTurnFollowUpTests(unittest.IsolatedAsyncioTestCase):
    """私聊（`chunk3.flush_buffered_narrative`）：第二段同样在 dispatch 之前。"""

    def _host(self, decision: dict, narrator: Any) -> Any:
        host = _ActionFlushHost()
        host.narrator = narrator
        host.dispatch_calls = []

        async def try_decide(*_args: Any) -> dict:
            return {
                'decision': decision, 'succeeded': True, 'effectiveNow': PRIVATE_NOW,
                'immediateObservations': [],
            }

        async def dispatch(story: Any, current: Any, **_kwargs: Any) -> list:
            host.dispatch_calls.append((story, current))
            return []

        host.try_decide = try_decide
        host.dispatch_platform_actions = dispatch
        host.buffered_narrative_turns = {
            'k': {
                'storyId': 's', 'participantId': 'p',
                'messages': [{'content': '在吗', 'occurredAt': PRIVATE_NOW,
                              'imageSources': [], 'audioSources': []}],
                'latestSession': 'session', 'timer': None, 'nextRevision': 3,
                'inFlightRequestId': None, 'obsoleteRequestIds': set(),
            },
        }
        return host

    async def test_the_private_turn_asks_once_before_dispatching(self):
        narrator = _Narrator({'action': 'send_like', 'params': {'user_id': OTHER_ID, 'times': 3}})
        decision = {
            'interaction': {'seen': True, 'reply': {'mode': 'immediate', 'content': '在的。'}},
            'platformActions': ['send_like'],
        }
        host = self._host(decision, narrator)
        await ServiceChunk3.flush_buffered_narrative(host, 'k', 3)

        self.assertEqual([call[0] for call in narrator.calls], ['send_like'])
        self.assertEqual(
            decision['platformActions'],
            [{'action': 'send_like', 'params': {'user_id': OTHER_ID, 'times': 3}}],
        )
        self.assertTrue(host.dispatch_calls, '补齐参数之后动作必须真的交给 dispatch')
        self.assertEqual(
            host.dispatch_calls[0][1]['platformActions'],
            [{'action': 'send_like', 'params': {'user_id': OTHER_ID, 'times': 3}}],
        )

    async def test_params_written_means_no_follow_up_in_the_private_turn(self):
        narrator = _Narrator({'action': 'send_like', 'params': {'user_id': OTHER_ID, 'times': 9}})
        decision = {
            'interaction': {'seen': True, 'reply': {'mode': 'immediate', 'content': '在的。'}},
            'platformActions': [{'action': 'send_like', 'params': {'user_id': OTHER_ID, 'times': 1}}],
        }
        host = self._host(decision, narrator)
        await ServiceChunk3.flush_buffered_narrative(host, 'k', 3)
        self.assertEqual(narrator.calls, [])
        self.assertTrue(host.dispatch_calls)

    async def test_a_missing_capability_is_visible_on_the_private_path(self):
        decision = {
            'interaction': {'seen': True, 'reply': {'mode': 'immediate', 'content': '在的。'}},
            'platformActions': ['send_like'],
        }
        host = self._host(decision, object())  # 没有 select_platform_action_params
        await ServiceChunk3.flush_buffered_narrative(host, 'k', 3)
        self.assertTrue(
            any('动作参数补问不可用' in str(item) for item in host.logs),
            '能力缺失必须可见且可行动：%r' % (host.logs,),
        )
        self.assertTrue(host.dispatch_calls, '补不上参数也要照常走 dispatch（它自己会拒绝并留痕）')


class FollowUpCallSiteReverseTests(GroupTurnFollowUpTests):
    """反向：拔掉那一跳 / 把预算提到回合外 → 正方向的守卫必须红。"""

    async def test_reverse_removing_the_hop_leaves_her_action_unfillable(self):
        narrator = _Narrator({'action': 'send_group_notice', 'params': {'content': '今晚八点开黑'}})
        decision = {'groupReply': {'mode': 'none'}, 'platformActions': ['send_group_notice']}
        service, ctx = self._turn(decision, narrator)

        async def no_hop(*_args: Any, **_kwargs: Any) -> None:
            return None

        service.resolve_platform_action_params = no_hop  # 变异：那一跳不做事（等价于删掉）
        await service.flush_group_turn('key', 3)
        self.assertEqual(narrator.calls, [], '摘掉那一跳 → 第二段永远不会被调用')
        with self.assertRaises(AssertionError):
            self.assertTrue(self._action_entries(), '参数补不上 → 动作执行不了（守卫红）')

    async def test_reverse_a_shared_budget_blocks_the_second_turn(self):
        """变异：预算提到回合外（共享 dict）→ 第二回合问不出来。"""
        narrator = _Narrator({'action': 'send_group_notice', 'params': {'content': '今晚八点开黑'}})
        decision = {'groupReply': {'mode': 'none'}, 'platformActions': ['send_group_notice']}
        service, ctx = self._turn(decision, narrator)
        real = service.resolve_platform_action_params
        shared: dict = {}

        async def shared_budget(current: Any, **_kwargs: Any) -> Any:
            return await real(current, follow_up_budget=shared)

        service.resolve_platform_action_params = shared_budget
        await service.flush_group_turn('key', 3)
        second = {'groupReply': {'mode': 'none'}, 'platformActions': ['send_group_notice']}
        ctx['box']['decision'] = second
        self._put_turn(service, second, revision=4)
        await service.flush_group_turn('key', 4)
        self.assertEqual(len(narrator.calls), 1, '共享预算 = 第二回合问不出来（变异后的样子）')
        with self.assertRaises(AssertionError):
            self.assertEqual(len(narrator.calls), 2)


if __name__ == '__main__':
    unittest.main()
