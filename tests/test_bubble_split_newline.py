"""上游 1.0.1-rc36：小模型换行分句开关 `runtime.convertNewlineToSeparator`。

上游语义（逐条对账 `upstream/src/service.ts:10414-10430` 的
`splitVisibleReplyBubbles`、`upstream/src/service.ts:6577-6583` 的
`splitOutgoingMessage` 读取口、`upstream/test/permission-gate.test.ts:104-125`
的七条断言，以及 `upstream/src/index.ts:209` 的 schema）：

* 开关**默认关**，按 `=== true` 读（只有显式真值才开）；
* 开着时把**换行运行**（`/\\r?\\n+/g`：单个换行、连续换行、CRLF 都算）当气泡边界；
  连续换行只产生**一个**边界，不产生空投递段；
* **内容已含显式分隔符时不转换**（模型自己写了 `<sep/>` 就尊重原样）；
* **拆条关闭时该开关无效**（整条一条）；
* 内容只有换行（转换后切不出任何段）时退回**原样一条**，不产生空投递列表；
* 判断只有**一处**：`core/bubbles.split_bubble_segments`；配置只有一处读取口
  `runtime_bubble_segments`，`ServiceChunk6.split_outgoing_message` 与发群
  `send_group_message` 都走它。

运行：`python3 -m unittest plugin.tests.test_bubble_split_newline -v`
"""

from __future__ import annotations

import pathlib
import unittest

from plugin.core.bubbles import (
    runtime_bubble_segments,
    split_bubble_segments,
)
from plugin.core.service import InterludeContext, NullTransport
from plugin.core.service.chunk6 import ServiceChunk6

ON = {'convert_newline_to_separator': True}
OFF = {'convert_newline_to_separator': False}
ON_CAMEL = {'convertNewlineToSeparator': True}


def _runtime(**extra: object) -> dict:
    return {'message_separator': '<sep/>', 'split_reply_messages': True, **extra}


def _texts(segments: list[dict]) -> list[str]:
    return [segment['content'] for segment in segments]


def _service(runtime: dict) -> ServiceChunk6:
    ctx = InterludeContext(logger=None, database=None, bots=None)
    return ServiceChunk6(ctx, {'runtime': runtime, 'logging': {'level': 'debug'}}, None, NullTransport())


class NewlineAsSeparatorTests(unittest.TestCase):
    """①-④：开 / 关 / 已含分隔符 / 连续空行。"""

    def test_off_by_default_leaves_newlines_in_one_bubble(self):
        """③ 关（默认）：换行原样留在单条里——行为与 rc36 之前逐字一致。"""
        self.assertEqual(_texts(split_bubble_segments('第一句\n第二句')), ['第一句\n第二句'])
        self.assertEqual(_texts(split_bubble_segments('第一句\n第二句', newline_as_separator=False)),
                         ['第一句\n第二句'])

    def test_on_splits_a_single_newline_run_into_bubbles(self):
        """① 开 + 换行两条 → 真分成两条气泡（单个换行也算边界）。"""
        self.assertEqual(
            _texts(split_bubble_segments('早呀\n昨晚睡得好吗', newline_as_separator=True)),
            ['早呀', '昨晚睡得好吗'],
        )
        self.assertEqual(
            _texts(split_bubble_segments('早呀\r\n昨晚睡得好吗', newline_as_separator=True)),
            ['早呀', '昨晚睡得好吗'],
        )

    def test_on_treats_a_run_of_blank_lines_as_exactly_one_boundary(self):
        """④ 开 + 连续多个空行 → 只当一个边界（上游 `\\r?\\n+` 一次吃掉整段运行）。"""
        self.assertEqual(
            _texts(split_bubble_segments('甲\n\n\n乙', newline_as_separator=True)),
            ['甲', '乙'],
        )
        self.assertEqual(
            _texts(split_bubble_segments('早呀\r\n\r\n昨晚睡得好吗', newline_as_separator=True)),
            ['早呀', '昨晚睡得好吗'],
        )
        self.assertEqual(
            _texts(split_bubble_segments('第一句\n第二句\n\n第三句', newline_as_separator=True)),
            ['第一句', '第二句', '第三句'],
        )

    def test_an_explicit_separator_wins_over_the_newline_conversion(self):
        """② 开 + 已含 `<sep/>` → **不转换**（逐字不变：换行留在它原来那一段里）。"""
        self.assertEqual(
            _texts(split_bubble_segments('甲<sep/>乙\n丙', newline_as_separator=True)),
            ['甲', '乙\n丙'],
        )
        # 反向对照：同一段内容在关着时结果**相同**——证明"不转换"不是另一条路径。
        self.assertEqual(
            _texts(split_bubble_segments('甲<sep/>乙\n丙', newline_as_separator=False)),
            ['甲', '乙\n丙'],
        )

    def test_splitting_off_makes_the_switch_irrelevant(self):
        """拆条关闭 → 开关无效，整条发送（上游 `splitEnabled:false` 守卫）。"""
        self.assertEqual(
            _texts(split_bubble_segments('甲\n乙', enabled=False, newline_as_separator=True)),
            ['甲\n乙'],
        )

    def test_newline_only_content_never_becomes_an_empty_delivery_list(self):
        """纯换行内容：转换后一段都切不出来 → 退回原样一条（上游 `parts.length ? parts : [content]`）。

        否则这个"让回复更好看"的开关会反过来**删掉**一条回复。
        """
        self.assertEqual(
            _texts(split_bubble_segments('\n\n', newline_as_separator=True)),
            ['\n\n'],
        )

    def test_the_explicit_separator_path_keeps_the_existing_empty_list_convention(self):
        """显式分隔符那条老路径不动：内容全是分隔符仍返回空列表（既有投递口径）。"""
        self.assertEqual(split_bubble_segments('<sep/>', newline_as_separator=False), [])
        self.assertEqual(split_bubble_segments('<sep/>', newline_as_separator=True), [])


class RuntimeReadingTests(unittest.TestCase):
    """配置读取口：`runtime_bubble_segments` 一处读、双拼写认、`=== true` 语义。"""

    def test_the_runtime_reader_accepts_both_spellings(self):
        self.assertEqual(_texts(runtime_bubble_segments(_runtime(**ON), '甲\n乙')), ['甲', '乙'])
        self.assertEqual(_texts(runtime_bubble_segments(_runtime(**ON_CAMEL), '甲\n乙')), ['甲', '乙'])
        self.assertEqual(_texts(runtime_bubble_segments(_runtime(**OFF), '甲\n乙')), ['甲\n乙'])

    def test_only_an_explicit_true_opens_the_switch(self):
        """上游是 `=== true`：字符串 'true' 不算（与"漏键"同一条路）。"""
        for value in (None, 0, 1, 'true', 'yes', [], {}):
            with self.subTest(value=value):
                runtime = _runtime()
                if value is not None:
                    runtime['convert_newline_to_separator'] = value
                self.assertEqual(_texts(runtime_bubble_segments(runtime, '甲\n乙')), ['甲\n乙'])

    def test_missing_key_means_off(self):
        self.assertEqual(_texts(runtime_bubble_segments({'message_separator': '<sep/>'}, '甲\n乙')),
                         ['甲\n乙'])


class DeliveryWiringTests(unittest.TestCase):
    """投递那一段（Chunk6 私聊）真的按开关走，且**没有第二套分条逻辑**。"""

    def test_split_outgoing_message_follows_the_switch(self):
        self.assertEqual(_service(_runtime(**ON)).split_outgoing_message('甲\n乙'), ['甲', '乙'])
        self.assertEqual(_service(_runtime(**OFF)).split_outgoing_message('甲\n乙'), ['甲\n乙'])
        # 上游默认值（配置整段缺失）= 关。
        self.assertEqual(_service({}).split_outgoing_message('甲\n乙'), ['甲\n乙'])

    def test_explicit_separator_still_wins_inside_the_service(self):
        service = _service(_runtime(**ON))
        self.assertEqual(service.split_outgoing_message('甲<sep/>乙\n丙'), ['甲', '乙\n丙'])

    def test_voice_marker_still_rides_along_when_newlines_split(self):
        """受控偏离不丢：换行分出来的每一段各自带自己的语音意图。"""
        segments = _service(_runtime(**ON)).split_outgoing_segments('甲<tts/>\n乙')
        self.assertEqual(segments, [
            {'content': '甲', 'voice': True},
            {'content': '乙', 'voice': False},
        ])


class SingleJudgementSiteTests(unittest.TestCase):
    """判据一处：换行分条不许在别处再写一套（变异守卫）。"""

    def test_the_newline_judgement_lives_in_exactly_one_module(self):
        """换行替换只许住在 `core/bubbles.py` 一处。

        `service/config.py` 会在默认值表 / TypedDict 里**提到**这个键名（那是配置声明，
        不是判据），所以这里盯的是"另一套换行替换"：正文里的 `\\r?\\n+` 正则、
        `newline_as_separator` 参数、以及那个私有正则常量，别处一个都不许有。
        """
        root = pathlib.Path(__file__).resolve().parents[1] / 'core'
        offenders = []
        for path in root.rglob('*.py'):
            text = path.read_text(encoding='utf-8')
            if path.name == 'bubbles.py':
                self.assertEqual(text.count('_NEWLINE_RUN_RE.sub('), 1,
                                 'bubbles.py 里换行替换只许出现一次')
                continue
            relative = str(path.relative_to(root))
            if '_NEWLINE_RUN_RE' in text or 'newline_as_separator' in text \
                    or r"'\r?\n+'" in text or r'"\r?\n+"' in text:
                offenders.append(relative)
        self.assertEqual(offenders, [], '换行分句只许有一处判据（core/bubbles.py）')
