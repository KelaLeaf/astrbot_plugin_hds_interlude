"""管理命令的两道防线：回执常量存在、确认问答只认真正的文字消息。

背景（2026-09-28 用户在真机上踩到的）：发 `/hdsi_purge_all` 后
① 提问发出 2 秒就报 `NameError: name 'CANCELLED' is not defined`——
   某次重构把 `CANCELLED` / `NO_ADMIN` / `NO_MANAGER` / `NO_MANAGER_DETAIL`
   的定义连根删了、只留下引用，于是**所有**取消与无权限分支都会抛异常；
② 原因不是用户答错，而是 NapCat 的「对方正在输入…」通知（`notice`）被当成回答，
   用空文本把等待中的 future 结掉，等同"取消"；用户随后真打的 `y` 反而进了叙事。

两道防线各有一组用例，外加一个静态哨兵：**本文件里被读取、却从未被绑定过的
全大写名字**一律报错。同类事故（漏删常量 / 漏导入）都会在它这里现形。
"""

from __future__ import annotations

import ast
import asyncio
import builtins
import io
import pathlib
import re
import sys
import types
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

# 先导入桥测试模块：它在 import 时装好 `astrbot` 桩，`plugin.main` 才能被导入。
from plugin.tests.test_astrbot_bridge import _make_plugin  # noqa: E402

from plugin import main as main_module  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
PLUGIN_ROOT = HERE.parent

#: 只盯"常量形状"的名字：全大写、至少三个字符、带下划线或纯大写词。
CONSTANT_NAME = re.compile(r'^[A-Z][A-Z0-9_]{2,}$')


def _bound_names(tree: ast.AST) -> set[str]:
    """文件里**任意位置**绑定过的名字（模块级、函数内 import、局部常量、参数、推导式…）。

    只做"本文件自洽"的检查：跨模块的名字由 import 语句本身绑定，因此
    "读得到、却在本文件里从未绑定"就是一个确凿的漏定义。
    """
    names: set[str] = set(dir(builtins))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add((alias.asname or alias.name).split('.')[0])
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                names.update(sub.id for sub in ast.walk(target) if isinstance(sub, ast.Name))
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
            target = getattr(node, 'target', None)
            if isinstance(target, ast.Name):
                names.add(target.id)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            target = getattr(node, 'target', None)
            if target is not None:
                names.update(sub.id for sub in ast.walk(target) if isinstance(sub, ast.Name))
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            names.add(getattr(node, 'name', '<lambda>'))
            arguments = node.args
            for argument in (
                list(arguments.args) + list(arguments.posonlyargs)
                + list(arguments.kwonlyargs) + [arguments.vararg, arguments.kwarg]
            ):
                if argument is not None:
                    names.add(argument.arg)
        elif isinstance(node, ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names.update(node.names)
        elif isinstance(node, ast.MatchAs) and node.name:
            names.add(node.name)
    return names


def undefined_constants(path: pathlib.Path) -> list[str]:
    """返回该文件里"被读取但从未绑定"的全大写名字（`名称:行号`）。"""
    with path.open(encoding='utf-8') as handle:
        tree = ast.parse(handle.read())
    bound = _bound_names(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id not in bound and CONSTANT_NAME.match(node.id):
            found.append('%s:%d' % (node.id, node.lineno))
    return found


class UndefinedConstantGuardTests(unittest.TestCase):
    """静态哨兵：漏删常量/漏导入这类事故，编译期看不出来，跑分支才炸。"""

    def test_no_constant_is_used_without_being_defined(self):
        offenders: list[str] = []
        for path in sorted(PLUGIN_ROOT.rglob('*.py')):
            if '__pycache__' in path.parts:
                continue
            for item in undefined_constants(path):
                offenders.append('%s → %s' % (path.relative_to(PLUGIN_ROOT), item))
        self.assertEqual(offenders, [], '有常量被读取却从未定义：%s' % offenders)

    def test_command_receipt_constants_exist(self):
        """`CANCELLED` 那一族是命令回执的唯一来源，缺一个就是一批分支全崩。"""
        for name in ('CANCELLED', 'NO_ADMIN', 'NO_MANAGER', 'NO_MANAGER_DETAIL'):
            value = getattr(main_module, name, None)
            self.assertIsInstance(value, str, '%s 未定义' % name)
            self.assertTrue(value.strip(), '%s 是空文案' % name)
        self.assertEqual(main_module.CANCELLED, '操作已取消。')

    def test_every_command_receipt_is_reachable(self):
        """命令表里的每条处理器都能取到方法（改名/删方法不该留下空表项）。"""
        plugin = main_module.HDSInterludePlugin
        for spec in main_module.COMMANDS:
            self.assertTrue(callable(getattr(plugin, spec.handler, None)), spec.handler)


class _Event:
    """够用的私聊事件桩：确认问答只看 `unified_msg_origin` / `raw_message` / 文本。"""

    def __init__(self, text: str = '', post_type: str = 'message') -> None:
        raw: dict[str, object] = {'post_type': post_type}
        if post_type != 'message':
            raw['notice_type'] = 'notify'
            raw['sub_type'] = 'input_status'
        self.unified_msg_origin = 'onebot:FriendMessage:1000008890'
        self.message_obj = types.SimpleNamespace(raw_message=raw)
        self._text = text
        self.stopped = False

    def get_message_str(self) -> str:
        return self._text

    def stop_event(self) -> None:
        self.stopped = True


class ConfirmationGateTests(unittest.TestCase):
    """确认问答：只有真正的文字回答才算数。"""

    def setUp(self):
        # 走真实的插件实例：确认表就在 `__init__` 里建，别用影子类绕过它。
        self.host = _make_plugin()
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.future = self.loop.create_future()
        self.host._confirmations['onebot:FriendMessage:1000008890'] = self.future

    def test_typing_notice_does_not_answer(self):
        """「对方正在输入…」是通知：既不能当取消，也不能把等待的 future 结掉。"""
        event = _Event(post_type='notice')
        self.assertFalse(self.host._resolve_confirmation(event))
        self.assertFalse(self.future.done(), '通知不该结束等待中的确认')
        self.assertFalse(event.stopped)

    def test_empty_message_does_not_answer(self):
        """图片/贴纸回复没有文本，不能算"取消"。"""
        event = _Event('')
        self.assertFalse(self.host._resolve_confirmation(event))
        self.assertFalse(self.future.done())

    def test_real_answer_resolves_and_is_consumed(self):
        event = _Event(' y ')
        self.assertTrue(self.host._resolve_confirmation(event))
        self.assertEqual(self.future.result(), 'y')
        self.assertTrue(event.stopped, '确认回答必须被吞掉，不能进叙事')

    def test_no_answer_pending_is_not_consumed(self):
        self.host._confirmations.clear()
        event = _Event('y')
        self.assertFalse(self.host._resolve_confirmation(event))
        self.assertFalse(event.stopped)


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
