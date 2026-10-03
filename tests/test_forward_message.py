# -*- coding: utf-8 -*-
"""`plugin/core/forward_message.py` 的单元测试 —— 上游 `test/forward-message.test.ts`
（43 行、4 条 `test(...)`）逐条移植 + 本移植版补的边界。

上游 4 条的对应关系（名字刻意保留上游原意，方便将来对账）：

| 上游 `test(...)` | 本文件 |
| --- | --- |
| extracts forward ids from Koishi markup and CQ segments | `ForwardIdExtractionTests.test_extracts_forward_ids_from_koishi_markup_and_cq_segments` |
| normalizes mixed forward nodes with provenance and media placeholders | `ForwardNormalizationTests.test_normalizes_mixed_forward_nodes_with_provenance_and_media_placeholders` |
| reads get_forward_msg only through the current session bot and expands nested nodes | `ForwardReadTests.test_reads_get_forward_msg_only_through_the_given_fetch_and_expands_nested_nodes` |
| returns bounded failure placeholder when action is unavailable | `ForwardReadTests.test_returns_bounded_failure_placeholder_when_action_is_unavailable` |

补的边界（移植任务书点名的那几类）：嵌套超深、节点超预算、字符截断、坏 id、
字符预算耗尽、未知段类型、`maxDepth=0`、超时、响应形状怪（坏帧 / 非字典）、
`clamp_int` 的 JS `Number()` 语义、以及"没有合并转发时不打任何日志、也绝不发请求"。

独立运行：
    cd <仓库根目录>
    python3 -m unittest plugin.tests.test_forward_message -v
"""

from __future__ import annotations

import asyncio
import importlib
import os
import subprocess
import sys
import unittest

from plugin.core.forward_message import (
    DEFAULT_LIMITS,
    FORWARD_FETCH_TIMEOUT_MS,
    FORWARD_MEDIA_MAX_PER_FORWARD,
    FORWARD_MEDIA_MAX_PER_TURN,
    FORWARD_VIDEO_MAX_PER_FORWARD,
    ForwardMediaBudget,
    ForwardReadLimits,
    ForwardReadResult,
    as_record,
    clamp_int,
    extract_forward_ids,
    extract_forward_media,
    failure_result,
    forward_media_note,
    forward_read_content,
    forward_read_ids,
    forward_read_limits,
    forward_read_with_media,
    normalize_forward_messages,
    normalize_forward_segments,
    with_timeout,
)

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(HERE)
REPO_ROOT = os.path.dirname(PLUGIN_ROOT)
MODULE_PATH = os.path.join(PLUGIN_ROOT, 'core', 'forward_message.py')


def _node(**kwargs):
    """造一个 OneBot 合并转发节点（`nickname` / `user_id` / `message` …）。"""
    return dict(kwargs)


def _text_segment(text):
    return {'type': 'text', 'data': {'text': text}}


def _image_segment(url='', sub_type='0', summary='', **extra):
    """一个 OneBot 图片段（`sub_type` / `summary` 是平台给的种类判据）。"""
    data = {'url': url, 'sub_type': sub_type, 'summary': summary}
    data.update(extra)
    return {'type': 'image', 'data': data}


def _image_node(count, prefix='https://gchat.qpic.cn/a/', start=1, **kwargs):
    """一个只装 `count` 张图的节点（默认坐标各不相同）。"""
    return _node(message=[
        _image_segment('%s%d' % (prefix, index)) for index in range(start, start + count)
    ], **kwargs)


def _nested_node(user_id, nested_id):
    """一个"只装一个 forward 段"的节点。**每次调用都新建**：`_fetch_forward_nodes`

    会**就地**把 forward 段改写成 text 段，夹具里共用同一个 dict 会让下一轮拿到已经
    展开过的对象（实测：预算用例里 `fetch` 被多调了两次，看起来像预算失效）。
    """
    return {'user_id': user_id, 'message': [{'type': 'forward', 'data': {'id': nested_id}}]}


class _FakeFetch:
    """上游测试里的 `session.bot.internal._request` 桩：记录调用、按 id 回不同页。"""

    def __init__(self, pages):
        self.pages = pages
        self.calls: list[str] = []

    async def __call__(self, identifier):
        self.calls.append(identifier)
        page = self.pages.get(identifier, {})
        return page(identifier) if callable(page) else page


# =========================================================================== #
# 预算
# =========================================================================== #

class ForwardReadLimitsTests(unittest.TestCase):
    def test_defaults_match_the_upstream_config_group(self):
        """上游 `index.ts:214-219`：30 / 8000 / 3。"""
        self.assertEqual(DEFAULT_LIMITS.max_nodes, 30)
        self.assertEqual(DEFAULT_LIMITS.max_characters, 8_000)
        self.assertEqual(DEFAULT_LIMITS.max_depth, 3)
        limits = forward_read_limits()
        self.assertEqual((limits.max_nodes, limits.max_characters, limits.max_depth), (30, 8_000, 3))

    def test_both_spellings_are_read_and_camel_case_wins(self):
        limits = forward_read_limits({'maxNodes': 12, 'max_nodes': 99, 'max_depth': 2})
        self.assertEqual(limits.max_nodes, 12, '同给两种拼写时按键名法以 camelCase 为准')
        self.assertEqual(limits.max_depth, 2)
        self.assertEqual(forward_read_limits({'max_nodes': 7}).max_nodes, 7)

    def test_clamps_into_the_upstream_ranges(self):
        """`maxNodes` 1~100、`maxCharacters` 500~32000、`maxDepth` **0**~8。"""
        low = forward_read_limits({'maxNodes': 0, 'maxCharacters': 1, 'maxDepth': -5})
        self.assertEqual((low.max_nodes, low.max_characters, low.max_depth), (1, 500, 0))
        high = forward_read_limits({'maxNodes': 5_000, 'maxCharacters': 999_999, 'maxDepth': 99})
        self.assertEqual((high.max_nodes, high.max_characters, high.max_depth), (100, 32_000, 8))

    def test_max_depth_lower_bound_is_zero_not_one(self):
        """`maxDepth=0` 是**合法**值（0 = 不展开嵌套），不是"没填"。"""
        self.assertEqual(forward_read_limits({'maxDepth': 0}).max_depth, 0)

    def test_unusable_values_fall_back_to_defaults(self):
        limits = forward_read_limits({'maxNodes': 'abc', 'maxCharacters': None, 'maxDepth': object()})
        # `None` 按 JS 的 `Number(null) === 0` 走夹取（→ 500），'abc' / object 才回默认。
        self.assertEqual(limits, ForwardReadLimits(max_nodes=30, max_characters=500, max_depth=3))
        self.assertEqual(forward_read_limits({'maxNodes': None}), ForwardReadLimits(max_nodes=1, max_characters=8_000, max_depth=3))
        self.assertEqual(forward_read_limits('not-a-dict'), DEFAULT_LIMITS)

    def test_clamp_int_follows_the_javascript_number_semantics(self):
        # 上游 `Math.floor(Number(value))`：布尔 / 数字串 / 空串 / null 都给数。
        self.assertEqual(clamp_int(True, 0, 100, 7), 1)
        self.assertEqual(clamp_int(False, 0, 100, 7), 0)
        self.assertEqual(clamp_int('12', 0, 100, 7), 12)
        self.assertEqual(clamp_int(' 12 ', 0, 100, 7), 12)
        self.assertEqual(clamp_int('', 0, 100, 7), 0, "JS 的 Number('') === 0")
        self.assertEqual(clamp_int(None, 0, 100, 7), 0, 'JS 的 Number(null) === 0')
        self.assertEqual(clamp_int('abc', 0, 100, 7), 7, 'NaN 回兜底值（不是夹到上限）')
        self.assertEqual(clamp_int(float('nan'), 0, 100, 7), 7)
        self.assertEqual(clamp_int(float('inf'), 0, 100, 7), 7)
        self.assertEqual(clamp_int(-12.5, -100, 100, 7), -13, 'Math.floor 对负数向下取整')
        self.assertEqual(clamp_int(12.7, 0, 100, 7), 12, 'floor 而不是四舍五入')
        self.assertEqual(clamp_int(-3, 0, 100, 7), 0)
        self.assertEqual(clamp_int('0x10', 0, 100, 7), 16, "JS 的 Number('0x10') === 16")
        self.assertEqual(clamp_int('abc', 0, 100, 7), 7, 'Python 的 int(\'abc\', 16) 合法，JS 的 NaN 不合法')
        self.assertEqual(clamp_int('Infinity', 0, 100, 7), 7, 'JS 的 Number("Infinity") 不是有限数')


# =========================================================================== #
# 抠 id
# =========================================================================== #

class ForwardIdExtractionTests(unittest.TestCase):
    def test_extracts_forward_ids_from_koishi_markup_and_cq_segments(self):
        """上游 1:1 —— `<forward id="abc"/>[CQ:forward,id=def]` → `['abc', 'def']`。"""
        self.assertEqual(extract_forward_ids('<forward id="abc"/>[CQ:forward,id=def]'), ['abc', 'def'])

    def test_attribute_forms_and_single_quotes(self):
        self.assertEqual(extract_forward_ids("<forward id='a'/>"), ['a'])
        self.assertEqual(extract_forward_ids('<forward res_id="r"/>'), ['r'])
        self.assertEqual(extract_forward_ids('<forward forward_id="f"/>'), ['f'])
        self.assertEqual(extract_forward_ids('<forward id="a"></forward>'), ['a'])

    def test_attribute_priority_is_id_then_res_id_then_forward_id(self):
        """上游取值是 `attrs.id ?? attrs.res_id ?? attrs.forward_id` —— **id 优先**。

        （`{...attrs, ...data}` 决定的是"同名键谁赢"，而不是"不同键谁优先"；这条是
        实测跑出来的：只写 `res_id` 才轮到它。）
        """
        self.assertEqual(extract_forward_ids('<forward id="a" res_id="b"/>'), ['a'])
        self.assertEqual(extract_forward_ids('<forward res_id="b" forward_id="c"/>'), ['b'])
        self.assertEqual(extract_forward_ids('<forward id="" res_id="b"/>'), ['b'], '空 id 让位给 res_id')

    def test_cq_fields_are_case_insensitive_and_trimmed(self):
        self.assertEqual(extract_forward_ids('[CQ:forward,ID= def ]'), ['def'])
        self.assertEqual(extract_forward_ids('[cq:forward,id=x]'), ['x'], 'CQ 码整体不区分大小写')
        self.assertEqual(extract_forward_ids('[CQ:forward,id=x,res_id=y]'), ['x'], 'CQ 与属性同一条优先级')
        self.assertEqual(extract_forward_ids('[CQ:forward,res_id=y]'), ['y'])

    def test_duplicates_keep_the_first_occurrence_order(self):
        """上游**先扫完全部标记、再扫全部 CQ 码**，所以顺序不按字符串位置，按来源分组。"""
        content = '[CQ:forward,id=a]<forward id="b"/>[CQ:forward,id=a]'
        self.assertEqual(extract_forward_ids(content), ['b', 'a'])
        self.assertEqual(extract_forward_ids('<forward id="b"/>[CQ:forward,id=a]'), ['b', 'a'])
        self.assertEqual(extract_forward_ids('[CQ:forward,id=a][CQ:forward,id=a]'), ['a'])

    def test_bad_and_missing_ids_are_dropped(self):
        self.assertEqual(extract_forward_ids('[CQ:forward,id=]'), [], '空 id 不算')
        self.assertEqual(extract_forward_ids('[CQ:forward,id=   ]'), [])
        self.assertEqual(extract_forward_ids('[CQ:image,file=x.jpg]'), [], '别的 CQ 码不算')
        self.assertEqual(extract_forward_ids('<forward/>'), [])
        self.assertEqual(extract_forward_ids(''), [])
        self.assertEqual(extract_forward_ids(None), [], 'None 当空串（上游 String(content ?? "")）')

    def test_overlong_ids_are_dropped(self):
        """上游 `id.length <= 512`：超长 id 直接丢掉，不进请求。"""
        self.assertEqual(extract_forward_ids('[CQ:forward,id=%s]' % ('x' * 513)), [])
        self.assertEqual(extract_forward_ids('[CQ:forward,id=%s]' % ('x' * 512)), ['x' * 512])

    def test_attribute_style_id_also_goes_through_the_length_gate(self):
        self.assertEqual(extract_forward_ids('<forward id="%s"/>' % ('y' * 513)), [])


# =========================================================================== #
# 归一化
# =========================================================================== #

class ForwardNormalizationTests(unittest.TestCase):
    def test_normalizes_mixed_forward_nodes_with_provenance_and_media_placeholders(self):
        """上游 1:1 —— 发送者署名 + 文本 + 图片 / 语音占位。"""
        result = normalize_forward_messages([
            {'user_id': '100', 'nickname': '甲', 'message_type': 'group', 'message': [
                {'type': 'text', 'data': {'text': '你好'}},
                {'type': 'image', 'data': {'url': 'https://example.invalid/x'}},
                {'type': 'record', 'data': {'file': 'x'}},
            ]},
        ])
        self.assertRegex(result.content, r'甲（100）')
        self.assertRegex(result.content, r'你好')
        self.assertRegex(result.content, r'\[图片\]')
        self.assertRegex(result.content, r'\[语音\]')
        self.assertFalse(result.failed)
        self.assertFalse(result.truncated)
        self.assertEqual(result.node_count, 1)

    def test_exact_injection_shape(self):
        """注入形态钉死（下游模型看到的就是这串，别顺手改标点）。"""
        result = normalize_forward_messages([
            {'user_id': '100', 'nickname': '甲', 'message_type': 'group', 'message': [
                {'type': 'text', 'data': {'text': '你好'}},
                {'type': 'image', 'data': {'url': 'https://example.invalid/x'}},
            ]},
        ])
        self.assertEqual(
            result.content,
            '[合并转发内容｜节点数 1]\n[节点 1｜甲（100）｜group]\n你好\n[图片]',
        )

    def test_sender_falls_back_to_user_id_then_unknown(self):
        self.assertIn('[节点 1｜100（100）]', normalize_forward_messages([{'user_id': '100'}]).content)
        self.assertIn('[节点 1｜未知发送者]', normalize_forward_messages([{}]).content)
        # 上游也读嵌套的 `node.sender.nickname` / `node.sender.user_id`。
        nested = normalize_forward_messages([{'sender': {'nickname': '乙', 'user_id': 'b'}}])
        self.assertIn('乙（b）', nested.content)

    def test_blank_nickname_falls_back_to_user_id(self):
        """上游 `String(… ?? …).trim() || '未知发送者'`：全空白的昵称不算昵称。"""
        self.assertIn('100', normalize_forward_messages([{'nickname': '   ', 'user_id': '100'}]).content)
        self.assertIn('未知发送者', normalize_forward_messages([{'nickname': '   '}]).content)

    def test_node_budget_marks_the_result_truncated(self):
        messages = [
            _node(user_id='1', message=[_text_segment('一')]),
            _node(user_id='2', message=[_text_segment('二')]),
            _node(user_id='3', message=[_text_segment('三')]),
        ]
        result = normalize_forward_messages(messages, {'maxNodes': 1})
        self.assertEqual(result.node_count, 1)
        self.assertTrue(result.truncated)
        self.assertNotIn('二', result.content)
        self.assertNotIn('三', result.content)
        self.assertIn('[合并转发内容已按安全预算截断]', result.content)
        # 首行的"节点数"是**平台给的节点数**（上游也用 `messages.length`），不是展开数。
        self.assertIn('[合并转发内容｜节点数 3]', result.content)

    def test_character_budget_clips_in_place(self):
        result = normalize_forward_messages(
            [_node(user_id='1', message=[_text_segment('y' * 2_000)])],
            {'maxCharacters': 500},
        )
        lines = result.content.split('\n')
        self.assertTrue(result.truncated)
        self.assertTrue(lines[2].endswith('[截断]'))
        # 上游的算术：`remaining = maxCharacters - used`，截断后行长 = remaining - 5 + 3。
        # 上游的算术（Python 的 `len` 数码点，与 JS 的 `length` 数 UTF-16 码元在
        # 中文下不同，所以这里按码点复算）：remaining = 500 - used，截断行长 =
        # (remaining - 5) + len('[截断]')。
        # `remaining` 是这一行**还剩的预算**；上游 `used += clipped.length + 1` 把行尾的
        # 换行也算进预算，所以"最后一行"最多能写到 `remaining - 1` 个字符。截断后的行长
        # = (remaining - 5) + len('[截断]') = remaining，比 `remaining - 1` 多 1 —— 这点
        # 溢出是上游的既有形状（总长仍在 `预算 + 标记长` 之内），照抄不"修正"。
        remaining = 500 - (len(lines[0]) + 1) - (len(lines[1]) + 1)
        self.assertEqual(len(lines[2]), remaining + 1)
        self.assertLessEqual(len(result.content), 500 + len('[截断]'))

    def test_character_budget_overflow_leaves_the_inline_clip_marker(self):
        """预算在最后一行用完时，末尾那行**没有空间**再补
        `[合并转发内容已按安全预算截断]`（上游的 `append` 会直接返回 False），
        但行内的 `[截断]` 已经把话说清楚了，且总长守住预算。"""
        result = normalize_forward_messages(
            [_node(user_id='1', message=[_text_segment('z' * 900)])],
            {'maxCharacters': 500},
        )
        self.assertTrue(result.truncated)
        lines = result.content.split('\n')
        self.assertTrue(lines[-1].endswith('[截断]'), lines[-1][-12:])
        self.assertLessEqual(len(result.content), 500 + len('[截断]'))

    def test_budget_notice_appears_when_whole_lines_were_dropped(self):
        """预算被**整行**挤爆时（不是被截断），末尾的那句预算提示才放得下。"""
        result = normalize_forward_messages(
            [_node(user_id='1', message=[_text_segment('z' * 300), _text_segment('w' * 300)])],
            {'maxCharacters': 500},
        )
        self.assertTrue(result.truncated)
        self.assertIn('[截断]', result.content)
        self.assertLessEqual(len(result.content), 500 + 64)

    def test_empty_messages_still_produce_a_header(self):
        result = normalize_forward_messages([])
        self.assertEqual(result.content, '[合并转发内容｜节点数 0]')
        self.assertEqual((result.node_count, result.forward_count), (0, 0))
        self.assertFalse(result.failed)

    def test_non_list_input_is_treated_as_empty(self):
        """平台没给 `messages` 时（None）当空列表，不抛异常。"""
        for value in (None, {}, 'x', 3):
            result = normalize_forward_messages(value)
            self.assertEqual(result.node_count, 0)

    def test_nested_forward_is_marked_and_counted(self):
        nested = normalize_forward_messages([
            _node(user_id='1', message=[
                {'type': 'forward', 'data': {'id': 'inner'}},
                {'type': 'forward', 'data': {}},
            ]),
        ])
        self.assertEqual(nested.forward_count, 2, '数的是遇到过的 forward 段（含标识缺失的那个）')
        self.assertIn('[嵌套合并转发｜资源 inner；需要递归读取]', nested.content)
        self.assertIn('[嵌套合并转发；资源标识缺失]', nested.content)

    def test_nested_forward_at_the_depth_ceiling_says_so(self):
        at_ceiling = normalize_forward_messages(
            [_node(user_id='1', message=[{'type': 'forward', 'data': {'id': 'i'}}])],
            {'maxDepth': 0},
        )
        self.assertIn('[嵌套合并转发，已达到深度上限｜i]', at_ceiling.content)
        # 深度 1 的归一化调用同样在 `depth >= maxDepth` 时收口。
        deeper = normalize_forward_messages(
            [_node(user_id='1', message=[{'type': 'forward', 'data': {'id': 'i'}}])],
            {'maxDepth': 1}, 1,
        )
        self.assertIn('[嵌套合并转发，已达到深度上限｜i]', deeper.content)
        self.assertIn('[嵌套合并转发，已达到深度上限]', normalize_forward_messages(
            [_node(user_id='1', message=[{'type': 'forward', 'data': {}}])], {'maxDepth': 0},
        ).content)

    def test_segment_types_follow_the_upstream_branches(self):
        result = normalize_forward_messages([_node(user_id='1', message=[
            {'type': 'text', 'data': {'text': '   '}},          # 空白文本被丢掉
            {'type': 'at', 'data': {'name': '甲'}},
            {'type': 'at', 'data': {}},
            {'type': 'reply', 'data': {'id': '99'}},
            {'type': 'reply', 'data': {}},
            {'type': 'IMAGE', 'data': {}},                       # 类型名大小写不敏感
            {'type': 'video', 'data': {}},
            {'type': 'file', 'data': {'name': 'a.txt'}},
            {'type': 'file', 'data': {}},
            {'type': 'poke', 'data': {}},
            {'data': {}},                                        # 没有 type：整段跳过
            None,                                                # 非字典：整段跳过
        ])])
        for expected in ('[@甲]', '[@]', '[回复消息 99]', '[回复]', '[图片]', '[视频]',
                         '[文件：a.txt]', '[文件]', '[未支持的消息类型：poke]'):
            self.assertIn(expected, result.content, result.content)

    def test_normalize_forward_segments_returns_the_upstream_shape(self):
        body = normalize_forward_segments(
            [_text_segment('abc'), {'type': 'forward', 'data': {'id': 'x'}}],
            forward_read_limits(), 0,
        )
        self.assertEqual(body['lines'], ['abc', '[嵌套合并转发｜资源 x；需要递归读取]'])
        self.assertEqual(body['forwardCount'], 1)
        self.assertFalse(body['truncated'])
        self.assertFalse(body['failed'])

    def test_as_record_treats_empty_dict_as_a_record(self):
        """JS 的 `isRecord({})` 为真 —— 别用 `if not value` 判。"""
        self.assertEqual(as_record({}), {})
        self.assertEqual(as_record(None), {})
        self.assertEqual(as_record([]), {})
        self.assertEqual(as_record('x'), {})

    def test_failure_result_matches_the_upstream_placeholder(self):
        result = failure_result()
        self.assertTrue(result.failed)
        self.assertEqual(result.content, '[收到一条合并转发消息，但暂时无法读取内容]')
        self.assertEqual((result.node_count, result.forward_count, result.truncated), (0, 0, False))

    def test_result_payload_is_snake_case(self):
        payload = failure_result().to_payload()
        self.assertEqual(sorted(payload), ['content', 'failed', 'forward_count', 'node_count', 'truncated'])


# =========================================================================== #
# 读取（fetch + 超时 + 失败分支）
# =========================================================================== #

class ForwardReadTests(unittest.TestCase):
    def test_reads_get_forward_msg_only_through_the_given_fetch_and_expands_nested_nodes(self):
        """上游 1:1 —— 只用给定的 fetch 入口，且嵌套会被展开成文本。"""
        fetch = _FakeFetch({
            'outer': {'data': {'messages': [
                {'user_id': '1', 'nickname': 'A', 'message': [{'type': 'forward', 'data': {'id': 'inner'}}]},
            ]}},
            'inner': {'data': {'messages': [
                {'user_id': '2', 'nickname': 'B', 'message': [_text_segment('内层')]},
            ]}},
        })
        result = asyncio.run(forward_read_content('<forward id="outer"/>', fetch, {'maxDepth': 2}))
        self.assertIsNotNone(result)
        self.assertEqual(fetch.calls, ['outer', 'inner'])
        self.assertRegex(result.content, r'内层')
        # 嵌套段在递归里被就地改写成 `text`，所以**展开成功的那一层不留下 forward 段**
        # ——`forward_count` 是 0（上游同样如此：它数的是"读到时就还是 forward 的段"）。
        self.assertEqual(result.forward_count, 0)
        # 嵌套正文是"归一化后的整块"（带自己的首行），贴在外层节点下面。
        self.assertIn('[合并转发内容｜节点数 1]', result.content)

    def test_returns_bounded_failure_placeholder_when_action_is_unavailable(self):
        """上游 1:1 —— 取不到请求入口时给失败占位，绝不抛。"""

        async def unavailable(_identifier):
            raise RuntimeError('no request entry')

        result = asyncio.run(forward_read_content('<forward id="x"/>', unavailable))
        self.assertTrue(result.failed)
        self.assertRegex(result.content, r'暂时无法读取内容')

    def test_returns_none_when_there_is_no_forward(self):
        """上游 `if (!ids.length) return undefined`：没有转发就什么都不做。"""
        calls: list[str] = []

        async def fetch(identifier):
            calls.append(identifier)
            return {}

        self.assertIsNone(asyncio.run(forward_read_content('普通一句话', fetch)))
        self.assertIsNone(asyncio.run(forward_read_content('[CQ:image,file=a.jpg]', fetch)))
        self.assertEqual(calls, [], '没有合并转发时一个请求都不许发')

    def test_read_ids_accepts_an_already_extracted_id(self):
        """适配层那条路径：AstrBot 的 `Forward` 组件只给裸 id，core 得能直接按 id 读。"""
        fetch = _FakeFetch({'res-1': {'data': {'messages': [_node(user_id='1', message=[_text_segment('裸 id')])]}}})
        result = asyncio.run(forward_read_ids(['res-1'], fetch))
        self.assertEqual(fetch.calls, ['res-1'])
        self.assertIn('裸 id', result.content)

    def test_read_ids_with_no_ids_returns_none_without_asking(self):
        fetch = _FakeFetch({})
        self.assertIsNone(asyncio.run(forward_read_ids([], fetch)))
        self.assertIsNone(asyncio.run(forward_read_ids(['   '], fetch)))
        self.assertEqual(fetch.calls, [], '没有 id 就不许发请求')

    def test_read_ids_still_takes_message_content(self):
        fetch = _FakeFetch({'b': {'data': {'messages': []}}})
        asyncio.run(forward_read_ids('[CQ:forward,id=b]', fetch))
        self.assertEqual(fetch.calls, ['b'])

    def test_non_callable_fetch_degrades_instead_of_raising(self):
        result = asyncio.run(forward_read_content('<forward id="x"/>', object()))
        self.assertTrue(result.failed)

    def test_platform_error_frame_is_a_failure(self):
        async def fetch(_identifier):
            return {'retcode': 1200, 'wording': '合并转发已过期'}

        result = asyncio.run(forward_read_content('<forward id="x"/>', fetch))
        self.assertTrue(result.failed)

    def test_status_field_is_also_honoured(self):
        async def fetch(_identifier):
            return {'status': 'failed', 'message': 'boom'}

        self.assertTrue(asyncio.run(forward_read_content('<forward id="x"/>', fetch)).failed)

    def test_empty_or_shapeless_responses_are_not_failures(self):
        """OneBot 实现常常回空壳（`retcode` / `status` 都没有）：上游当成成功、正文为空。"""
        pages = [
            {},
            {'data': {}},
            {'data': {'messages': None}},
            {'status': 'ok', 'retcode': 0},
            'not-a-dict',
        ]
        for page in pages:
            async def fetch(_identifier, page=page):
                return page

            result = asyncio.run(forward_read_content('<forward id="x"/>', fetch))
            self.assertIsNotNone(result, page)
            self.assertFalse(result.failed, page)
            self.assertEqual(result.node_count, 0, page)
            self.assertIn('[合并转发内容｜节点数 0]', result.content)

    def test_timeout_is_a_failure_not_an_exception(self):
        async def slow(_identifier):
            await asyncio.sleep(5)
            return {'data': {'messages': []}}

        async def scenario():
            with self.assertRaises((asyncio.TimeoutError, TimeoutError)):
                # `forward_read_content` 自己等 30 秒；测试里用外层 wait_for 掐掉，
                # 证明"慢 fetch"不会静默变成成功——超时一路抛到调用方（适配层接住）。
                await asyncio.wait_for(
                    forward_read_content('<forward id="x"/>', slow), timeout=0.05,
                )

        asyncio.run(scenario())

    def test_with_timeout_raises_on_a_slow_awaitable_and_passes_results_through(self):
        async def slow():
            await asyncio.sleep(1)
            return 'late'

        async def quick():
            return 'ok'

        async def scenario():
            with self.assertRaises((asyncio.TimeoutError, TimeoutError)):
                await with_timeout(slow(), 10)
            return await with_timeout(quick(), 10)

        self.assertEqual(asyncio.run(scenario()), 'ok')
        self.assertEqual(FORWARD_FETCH_TIMEOUT_MS, 30_000)

    def test_one_bad_nested_forward_does_not_sink_the_whole_page(self):
        """坏嵌套只坏那一段（上游在段级 try/catch），外层照常出正文。"""
        async def fetch(identifier):
            if identifier == 'outer':
                return {'data': {'messages': [
                    {'user_id': '1', 'message': [{'type': 'forward', 'data': {'id': 'inner'}}]},
                    {'user_id': '2', 'message': [_text_segment('外层也还在')]},
                ]}}
            raise RuntimeError('inner 读不到')

        result = asyncio.run(forward_read_content('<forward id="outer"/>', fetch, {'maxDepth': 3}))
        self.assertFalse(result.failed)
        self.assertIn('[嵌套合并转发读取失败｜资源 inner]', result.content)
        self.assertIn('外层也还在', result.content)

    def test_nested_expansion_stops_at_max_depth(self):
        calls: list[str] = []

        async def fetch(identifier):
            calls.append(identifier)
            return {'data': {'messages': [
                {'user_id': '1', 'message': [{'type': 'forward', 'data': {'id': 'deep'}}]},
            ]}}

        result = asyncio.run(forward_read_content('<forward id="top"/>', fetch, {'maxDepth': 0}))
        self.assertEqual(calls, ['top'], 'maxDepth=0 时一个嵌套请求都不许发')
        self.assertIn('[嵌套合并转发，已达到深度上限｜deep]', result.content)

    def test_nested_expansion_stops_at_the_node_budget(self):
        """上游 `let remaining = limits.maxNodes; … if (remaining-- <= 0) break`：

        `maxNodes=1` 时页内**只有第 1 个节点**会被检查嵌套（后两个连看都不看）。
        """
        calls: list[str] = []

        async def fetch(identifier):
            calls.append(identifier)
            return {'data': {'messages': [_nested_node('1', 'a1'), _nested_node('2', 'a2')]}}

        result = asyncio.run(forward_read_content('<forward id="top"/>', fetch, {'maxNodes': 1, 'maxDepth': 3}))
        # 节点预算作用在**每一层页内**：`top` 只检查了节点 1（→ 抓 a1），`a1` 自己那页
        # 同样只检查节点 1（→ 又抓 a1），一直抓到深度上限 —— 所以前缀是 `top,a1,a1,a1`
        # （深度 0/1/2 各抓一次 + 深度 3 的最后一页不再抓）。**深度上限让递归有界**，
        # 哪怕平台一直回同一个 id 也不会失控（上游同样是这个形状，别"顺手优化"）。
        # 这条的 `/资源 a2/` 只出现在**第一层页内没被检查**的那个节点上，而那是内层
        # 归一化产出的文本（本层看不到）——所以这里断言"预算真的挡住了第二个节点"用
        # **调用次数**，不用正文。
        self.assertEqual(calls, ['top', 'a1', 'a1', 'a1'])
        self.assertEqual(len(calls), 4, '不是每页两个节点都递归：节点预算确实生效了')

    def test_object_fetch_is_accepted(self):
        """`_fetch_forward_nodes` 递归时用的正是"带 fetch 方法的对象"这条契约。"""

        class Fetcher:
            def __init__(self):
                self.calls = []

            async def fetch(self, identifier):
                self.calls.append(identifier)
                # `data.messages` 装的是**节点**，节点里的 `message` 才是段列表
                # （上游测试夹具也是这个形状；把段直接塞进 `messages` 会静默变成空节点）。
                return {'data': {'messages': [
                    {'user_id': '2', 'nickname': 'B', 'message': [_text_segment('来自对象')]},
                ]}}

        fetcher = Fetcher()
        result = asyncio.run(forward_read_content('<forward id="x"/>', fetcher))
        self.assertEqual(fetcher.calls, ['x'])
        self.assertIn('来自对象', result.content)

    def test_only_the_first_id_is_read(self):
        """上游只读 `ids[0]`（一条消息里多个转发卡片时也只读第一条）。"""
        fetch = _FakeFetch({'a': {'data': {'messages': []}}})
        asyncio.run(forward_read_content('[CQ:forward,id=a][CQ:forward,id=b]', fetch))
        self.assertEqual(fetch.calls, ['a'])

    def test_limits_are_clamped_before_use(self):
        fetch = _FakeFetch({'a': {'data': {'messages': [
            _node(user_id='1', message=[_text_segment('x' * 600)]),
        ]}}})
        result = asyncio.run(forward_read_content(
            '<forward id="a"/>', fetch, {'maxNodes': 0, 'maxCharacters': 10, 'maxDepth': 99},
        ))
        # 越界值被夹到下限（1 / 500 / 8），而不是被当"没填"。
        self.assertLessEqual(len(result.content), 500 + 32)


# =========================================================================== #
# 媒体（v1.8.7：第四道预算 + 三条出口的原料）
# =========================================================================== #

class ForwardMediaBudgetTests(unittest.TestCase):
    """单条转发的媒体预算：默认 3、区间 0~10、超预算截断但**数得出来**。"""

    def test_defaults_and_range(self):
        self.assertEqual(FORWARD_MEDIA_MAX_PER_FORWARD, 3)
        self.assertEqual(DEFAULT_LIMITS.max_images, 3)
        self.assertEqual(forward_read_limits().max_images, 3)
        self.assertEqual(forward_read_limits({'maxImages': 0}).max_images, 0, '0 是合法值')
        self.assertEqual(forward_read_limits({'max_images': 0}).max_images, 0, '两种拼写都认')
        self.assertEqual(forward_read_limits({'maxImages': -5}).max_images, 0, '夹到下限 0')
        self.assertEqual(forward_read_limits({'maxImages': 99}).max_images, 10, '夹到上限 10')
        # "没写"与"写了不可用"仍是两回事（与三重预算同一条语义）。
        self.assertEqual(forward_read_limits({'maxImages': 'abc'}).max_images, 3)
        self.assertEqual(forward_read_limits({'maxImages': None}).max_images, 0, 'Number(null)=0')

    def test_exactly_the_limit_is_taken_and_the_rest_is_counted(self):
        budget = ForwardMediaBudget(max_images=3)
        media = extract_forward_media(_image_node(15), budget)
        self.assertEqual(len(media), 3, '恰好取上限张')
        self.assertEqual([item.source for item in media],
                         ['https://gchat.qpic.cn/a/1', 'https://gchat.qpic.cn/a/2',
                          'https://gchat.qpic.cn/a/3'], '只取排在前面的')
        self.assertEqual(budget.image_count, 15, '见到几张要数满')
        self.assertEqual(budget.taken, 3)
        self.assertEqual(forward_media_note(budget), '[图片×15，仅取前 3 张]')

    def test_limit_zero_takes_nothing_but_says_how_many_were_there(self):
        """**反向用例**：上限配成 0 也必须按规则走（不是"永远全取"）。"""
        budget = ForwardMediaBudget(max_images=0)
        self.assertEqual(extract_forward_media(_image_node(15), budget), [])
        self.assertEqual(budget.image_count, 15)
        self.assertEqual(forward_media_note(budget), '[图片×15，仅取前 0 张]')

    def test_limit_one_takes_exactly_one(self):
        budget = ForwardMediaBudget(max_images=1)
        media = extract_forward_media(_image_node(4), budget)
        self.assertEqual([item.source for item in media], ['https://gchat.qpic.cn/a/1'])
        self.assertEqual(forward_media_note(budget), '[图片×4，仅取前 1 张]')

    def test_no_note_when_everything_fits(self):
        budget = ForwardMediaBudget(max_images=3)
        extract_forward_media(_image_node(2), budget)
        self.assertEqual(forward_media_note(budget), '', '没超预算就一个字都不加')
        empty = ForwardMediaBudget(max_images=3)
        self.assertEqual(forward_media_note(empty), '', '一张图都没有也不加字')

    def test_the_same_image_twice_counts_once(self):
        """去重：同一张图在节点里出现两次只算一次（坐标字面量）。"""
        node = _node(message=[
            _image_segment('https://gchat.qpic.cn/a/1'),
            _image_segment('https://gchat.qpic.cn/a/2'),
            _image_segment('https://gchat.qpic.cn/a/1'),
        ])
        budget = ForwardMediaBudget(max_images=5)
        media = extract_forward_media(node, budget)
        self.assertEqual([item.source for item in media],
                         ['https://gchat.qpic.cn/a/1', 'https://gchat.qpic.cn/a/2'])
        self.assertEqual(budget.image_count, 2, '重复的那张不计数')
        self.assertEqual(forward_media_note(budget), '')

    def test_segments_without_a_usable_coordinate_do_not_count(self):
        """`url` / `file` / `path` 都没有 = 拿不到，不是"没取"——数都不数。"""
        node = _node(message=[
            {'type': 'image', 'data': {}},
            _image_segment('https://gchat.qpic.cn/a/1'),
        ])
        budget = ForwardMediaBudget(max_images=5)
        media = extract_forward_media(node, budget)
        self.assertEqual(len(media), 1)
        self.assertEqual(budget.image_count, 1)

    def test_file_and_path_are_fallbacks_for_the_coordinate(self):
        node = _node(message=[
            {'type': 'image', 'data': {'file': 'file:///tmp/a.png'}},
            {'type': 'image', 'data': {'path': '/tmp/b.png'}},
        ])
        budget = ForwardMediaBudget(max_images=5)
        self.assertEqual([item.source for item in extract_forward_media(node, budget)],
                         ['file:///tmp/a.png', '/tmp/b.png'])

    def test_kind_follows_the_platform_sub_type_table(self):
        """种类只看平台段字段（三档表的同一张表）：0 图 / 1 收藏表情 / 2-3-7 候选。"""
        node = _node(message=[
            _image_segment('https://gchat.qpic.cn/a/1', sub_type='0'),
            _image_segment('https://gchat.qpic.cn/a/2', sub_type='1', summary='[中午好]'),
            _image_segment('https://gchat.qpic.cn/a/3', sub_type='0', summary='[动画表情]'),
            _image_segment('https://gchat.qpic.cn/a/4', sub_type='7'),
            _image_segment('https://gchat.qpic.cn/a/5', sub_type='5'),
            _image_segment('https://gchat.qpic.cn/a/6', sub_type=''),
        ])
        budget = ForwardMediaBudget(max_images=10)
        kinds = [(item.kind, item.summary) for item in extract_forward_media(node, budget)]
        self.assertEqual(kinds, [
            ('image', ''),
            ('sticker', '[中午好]'),
            ('animated', '[动画表情]'),
            ('sticker-candidate', ''),
            ('image', ''),
            ('image', ''),
        ])

    def test_the_default_max_videos_is_one_so_one_video_is_read(self):
        """默认（`max_videos=1`）：一张卡最多读一段——**配了就生效**（用户口径）。

        v1.9.1 的初版默认是 0，等于"配置项存在但默认永不生效"；用户原话是"可配置单条
        转发最多读取的视频数"，所以默认取 1：既读得动，又把单卡的额外成本封在一次以内。
        """
        self.assertEqual(DEFAULT_LIMITS.max_videos, 1)
        self.assertEqual(FORWARD_VIDEO_MAX_PER_FORWARD, 1)
        node = _node(message=[
            {'type': 'video', 'data': {'url': 'https://gchat.qpic.cn/v/1'}},
            _image_segment('https://gchat.qpic.cn/a/1'),
            {'type': 'video', 'data': {'url': 'https://gchat.qpic.cn/v/2'}},
        ])
        budget = ForwardMediaBudget(max_images=5)
        media = extract_forward_media(node, budget)
        self.assertEqual([(item.kind, item.source) for item in media],
                         [('video', 'https://gchat.qpic.cn/v/1'),
                          ('image', 'https://gchat.qpic.cn/a/1')])
        self.assertEqual(budget.video_count, 2)
        self.assertEqual(budget.video_taken, 1)

    def test_explicitly_setting_max_videos_to_zero_only_counts(self):
        """**反向**：显式配 0（不是默认）才回到 v1.8.7 的老行为——视频只标注不读。"""
        node = _node(message=[
            {'type': 'video', 'data': {'url': 'https://gchat.qpic.cn/v/1'}},
            _image_segment('https://gchat.qpic.cn/a/1'),
            {'type': 'video', 'data': {'url': 'https://gchat.qpic.cn/v/2'}},
        ])
        budget = ForwardMediaBudget(max_images=5, max_videos=0)
        media = extract_forward_media(node, budget)
        self.assertEqual([item.source for item in media], ['https://gchat.qpic.cn/a/1'])
        self.assertEqual(budget.video_count, 2)
        self.assertEqual(budget.video_taken, 0)

    def test_the_video_budget_takes_exactly_the_limit_and_counts_the_rest(self):
        """**变异保护**：配了几段就读几段（忽略上限 = 全读 → 红）。"""
        node = _node(message=[
            {'type': 'video', 'data': {'url': 'https://gchat.qpic.cn/v/%d' % index}}
            for index in range(1, 4)
        ])
        budget = ForwardMediaBudget(max_images=3, max_videos=1)
        media = extract_forward_media(node, budget)
        self.assertEqual([(item.kind, item.source) for item in media],
                         [('video', 'https://gchat.qpic.cn/v/1')], '只取排在前面的那一段')
        self.assertEqual(budget.video_count, 3, '见到几段要数满')
        self.assertEqual(budget.video_taken, 1)

    def test_video_budget_range_and_spellings(self):
        self.assertEqual(forward_read_limits({'maxVideos': 2}).max_videos, 2)
        self.assertEqual(forward_read_limits({'max_videos': 2}).max_videos, 2, '两种拼写都认')
        self.assertEqual(forward_read_limits({'maxVideos': -5}).max_videos, 0, '夹到下限 0')
        self.assertEqual(forward_read_limits({'maxVideos': 99}).max_videos, 10, '夹到上限 10')
        # "没写"回默认 1（一张卡最多读一段）；`null` 是 Number(null)=0 → 夹成 0（两种都合法，语义不同）。
        self.assertEqual(forward_read_limits({}).max_videos, 1)
        self.assertEqual(forward_read_limits({'maxVideos': None}).max_videos, 0)

    def test_the_same_video_twice_is_read_once(self):
        node = _node(message=[
            {'type': 'video', 'data': {'url': 'https://gchat.qpic.cn/v/1'}},
            {'type': 'video', 'data': {'url': 'https://gchat.qpic.cn/v/1'}},
        ])
        budget = ForwardMediaBudget(max_images=3, max_videos=10)
        media = extract_forward_media(node, budget)
        self.assertEqual([item.source for item in media], ['https://gchat.qpic.cn/v/1'])
        self.assertEqual(budget.video_count, 2, '重复的段照样数（那是"见过几段"）')
        self.assertEqual(budget.video_taken, 1)


class ForwardMediaReadTests(unittest.TestCase):
    """`forward_read_with_media()`：一次取页，正文 + 媒体条目 + 可数线索。"""

    def _fetch(self, pages):
        return _FakeFetch(pages)

    def test_media_and_text_come_from_a_single_fetch(self):
        fetch = self._fetch({'a': {'data': {'messages': [_image_node(2, nickname='甲')]}}})
        read = asyncio.run(forward_read_with_media(['a'], fetch, {'maxImages': 3}))
        self.assertEqual(fetch.calls, ['a'], '只许取一次页（rkey 是短效的）')
        self.assertFalse(read.result.failed)
        self.assertEqual([item.source for item in read.media],
                         ['https://gchat.qpic.cn/a/1', 'https://gchat.qpic.cn/a/2'])
        self.assertEqual(read.skipped_images, 0)
        self.assertIn('甲', read.result.content)

    def test_over_budget_truncates_and_leaves_a_countable_clue(self):
        fetch = self._fetch({'a': {'data': {'messages': [_image_node(15)]}}})
        read = asyncio.run(forward_read_with_media(['a'], fetch, {'maxImages': 3}))
        self.assertEqual(len(read.media), 3)
        self.assertEqual(read.skipped_images, 12)
        self.assertIn('[图片×15，仅取前 3 张]', read.result.content)
        # 正文一行都不能丢：图后面的文字照旧在。
        self.assertIn('[合并转发内容｜节点数 1]', read.result.content)

    def test_budget_zero_keeps_the_text_and_the_placeholder(self):
        """**反向用例**：上限 0 → 一张都不取，但正文与占位符逐字照旧 + 线索。"""
        fetch = self._fetch({'a': {'data': {'messages': [
            _node(message=[_text_segment('你好'), _image_segment('https://gchat.qpic.cn/a/1')]),
        ]}}})
        read = asyncio.run(forward_read_with_media(['a'], fetch, {'maxImages': 0}))
        self.assertEqual(read.media, ())
        self.assertEqual(read.skipped_images, 1)
        self.assertIn('你好', read.result.content)
        self.assertIn('[图片×1，仅取前 0 张]', read.result.content)

    def test_the_clue_lands_on_the_image_line_not_on_the_text_line(self):
        fetch = self._fetch({'a': {'data': {'messages': [
            _node(message=[_text_segment('先说话'), _image_segment('https://gchat.qpic.cn/a/1'),
                           _image_segment('https://gchat.qpic.cn/a/2')]),
        ]}}})
        read = asyncio.run(forward_read_with_media(['a'], fetch, {'maxImages': 1}))
        lines = read.result.content.split('\n')
        self.assertEqual(lines[-2:], ['[图片×2，仅取前 1 张]', '[图片]'])

    def test_videos_are_taken_by_default_one_per_forward(self):
        """**默认（1）**：正文写"仅取前 1 段"，媒体条目里带上那一段视频坐标。

        初版默认 0（配了也不生效）已被用户裁决改掉；这里钉住"默认真的读一段"。
        """
        fetch = self._fetch({'a': {'data': {'messages': [
            _node(message=[
                {'type': 'video', 'data': {'url': 'https://gchat.qpic.cn/v/1'}},
                {'type': 'video', 'data': {'url': 'https://gchat.qpic.cn/v/2'}},
            ]),
        ]}}})
        read = asyncio.run(forward_read_with_media(['a'], fetch, {'maxImages': 3}))
        self.assertEqual([(item.kind, item.source) for item in read.media],
                         [('video', 'https://gchat.qpic.cn/v/1')])
        self.assertEqual(read.video_count, 2)
        self.assertEqual(read.video_taken, 1)
        self.assertIn('[视频×2，仅取前 1 段]', read.result.content)

    def test_videos_are_annotated_and_not_taken_when_the_budget_is_zero(self):
        """**反向**：显式配 `maxVideos=0` → 只标注不读，措辞与 v1.8.7 逐字一致。"""
        fetch = self._fetch({'a': {'data': {'messages': [
            _node(message=[
                {'type': 'video', 'data': {'url': 'https://gchat.qpic.cn/v/1'}},
                {'type': 'video', 'data': {'url': 'https://gchat.qpic.cn/v/2'}},
            ]),
        ]}}})
        read = asyncio.run(forward_read_with_media(['a'], fetch, {'maxImages': 3, 'maxVideos': 0}))
        self.assertEqual(read.media, ())
        self.assertEqual(read.video_count, 2)
        self.assertEqual(read.video_taken, 0)
        self.assertIn('[视频×2，未取]', read.result.content, '显式 0 时措辞逐字不变')

    def test_videos_are_taken_when_the_budget_allows_and_the_note_says_how_many(self):
        """**变异保护**：上限配成 1、节点里有 3 段 → 只读 1 段，正文写"仅取前 1 段"。"""
        fetch = self._fetch({'a': {'data': {'messages': [
            _node(message=[
                {'type': 'video', 'data': {'url': 'https://gchat.qpic.cn/v/1'}},
                {'type': 'video', 'data': {'url': 'https://gchat.qpic.cn/v/2'}},
                {'type': 'video', 'data': {'url': 'https://gchat.qpic.cn/v/3'}},
            ]),
        ]}}})
        read = asyncio.run(forward_read_with_media(['a'], fetch, {'maxImages': 3, 'maxVideos': 1}))
        self.assertEqual([item.kind for item in read.media], ['video'])
        self.assertEqual([item.source for item in read.media], ['https://gchat.qpic.cn/v/1'])
        self.assertEqual(read.video_count, 3)
        self.assertEqual(read.video_taken, 1)
        self.assertIn('[视频×3，仅取前 1 段]', read.result.content)

    def test_taking_every_video_says_only_how_many_there_were(self):
        fetch = self._fetch({'a': {'data': {'messages': [
            _node(message=[
                {'type': 'video', 'data': {'url': 'https://gchat.qpic.cn/v/1'}},
                {'type': 'video', 'data': {'url': 'https://gchat.qpic.cn/v/2'}},
            ]),
        ]}}})
        read = asyncio.run(forward_read_with_media(['a'], fetch, {'maxVideos': 5}))
        self.assertEqual(len(read.media), 2)
        self.assertIn('[视频×2]', read.result.content)
        self.assertNotIn('未取', read.result.content)

    def test_video_entries_do_not_eat_the_image_budget(self):
        """图片与视频是**两份**预算：视频条目混在 `collected` 里，不许把图片挤掉。"""
        fetch = self._fetch({'a': {'data': {'messages': [
            _node(message=[
                {'type': 'video', 'data': {'url': 'https://gchat.qpic.cn/v/1'}},
                _image_segment('https://gchat.qpic.cn/a/1'),
                _image_segment('https://gchat.qpic.cn/a/2'),
            ]),
        ]}}})
        read = asyncio.run(forward_read_with_media(
            ['a'], fetch, {'maxImages': 2, 'maxVideos': 1},
        ))
        self.assertEqual([item.kind for item in read.media], ['video', 'image', 'image'])
        self.assertEqual(read.skipped_images, 0)

    def test_unknown_segment_types_keep_their_current_text(self):
        fetch = self._fetch({'a': {'data': {'messages': [
            _node(message=[{'type': 'json', 'data': {'data': '{}'}},
                           _image_segment('https://gchat.qpic.cn/a/1', sub_type='1')]),
        ]}}})
        read = asyncio.run(forward_read_with_media(['a'], fetch, {'maxImages': 3}))
        self.assertIn('[未支持的消息类型：json]', read.result.content)
        self.assertEqual([item.kind for item in read.media], ['sticker'])

    def test_node_budget_bounds_the_media_too(self):
        """三重预算仍然先生效：节点预算之外的节点连图片都不该被看到。"""
        fetch = self._fetch({'a': {'data': {'messages': [
            _image_node(1, prefix='https://gchat.qpic.cn/first/', nickname='1'),
            _image_node(1, prefix='https://gchat.qpic.cn/second/', nickname='2'),
        ]}}})
        read = asyncio.run(forward_read_with_media(
            ['a'], fetch, {'maxImages': 5, 'maxNodes': 1},
        ))
        self.assertEqual([item.source for item in read.media], ['https://gchat.qpic.cn/first/1'])
        self.assertNotIn('second', read.result.content)

    def test_nested_media_is_collected_in_display_order(self):
        """嵌套展开顺序 = 正文顺序：先外层节点的图，再嵌套页里的图。"""
        pages = {
            'outer': {'data': {'messages': [{'nickname': 'A', 'message': [
                _image_segment('https://gchat.qpic.cn/a/outer'),
                {'type': 'forward', 'data': {'id': 'inner'}},
            ]}]}},
            'inner': {'data': {'messages': [{'nickname': 'B', 'message': [
                _image_segment('https://gchat.qpic.cn/a/inner'),
            ]}]}},
        }
        read = asyncio.run(forward_read_with_media(['outer'], self._fetch(pages), {'maxDepth': 3}))
        self.assertEqual([item.source for item in read.media],
                         ['https://gchat.qpic.cn/a/outer', 'https://gchat.qpic.cn/a/inner'])

    def test_media_is_still_collected_at_the_depth_cap(self):
        """`maxDepth=0` 只表示"不展开嵌套"，**不是**"不收集这一页的图"。

        （实现里踩过一次：收集写在节点循环里、而深度早退在循环之前，`maxDepth=0`
        时最外层那一页的图一张都收不到。默认 `maxDepth=3` 完全看不出来。）
        """
        pages = {
            'outer': {'data': {'messages': [{'nickname': 'A', 'message': [
                _image_segment('https://gchat.qpic.cn/a/outer'),
                {'type': 'forward', 'data': {'id': 'inner'}},
            ]}]}},
            'inner': {'data': {'messages': [{'nickname': 'B', 'message': [
                _image_segment('https://gchat.qpic.cn/a/inner'),
            ]}]}},
        }
        fetch = self._fetch(pages)
        read = asyncio.run(forward_read_with_media(['outer'], fetch, {'maxDepth': 0}))
        self.assertEqual(fetch.calls, ['outer'], 'maxDepth=0 时一个嵌套请求都不发')
        self.assertEqual([item.source for item in read.media], ['https://gchat.qpic.cn/a/outer'])

    def test_fetch_failure_keeps_the_text_and_yields_no_media(self):
        """取不到节点：正文照旧、媒体为空，**绝不吞正文**。"""
        async def broken(_identifier):
            raise RuntimeError('连接断了')

        read = asyncio.run(forward_read_with_media(['a'], broken, {'maxImages': 3}))
        self.assertTrue(read.result.failed)
        self.assertEqual(read.result.content, '[收到一条合并转发消息，但暂时无法读取内容]')
        self.assertEqual(read.media, ())
        self.assertEqual((read.skipped_images, read.video_count), (0, 0))

    def test_no_forward_returns_none_without_asking(self):
        fetch = self._fetch({})
        self.assertIsNone(asyncio.run(forward_read_with_media([], fetch)))
        self.assertEqual(fetch.calls, [], '没有 id 就不许发请求')

    def test_platform_error_frame_is_a_failure_with_no_media(self):
        async def fetch(_identifier):
            return {'retcode': 1200, 'wording': '合并转发已过期'}

        read = asyncio.run(forward_read_with_media(['a'], fetch))
        self.assertTrue(read.result.failed)
        self.assertEqual(read.media, ())

    def test_the_old_entry_points_never_collect_media(self):
        """老入口（`forward_read_ids` / `forward_read_content`）行为逐字不变。"""
        fetch = self._fetch({'a': {'data': {'messages': [_image_node(3)]}}})
        result = asyncio.run(forward_read_ids(['a'], fetch, {'maxImages': 3}))
        self.assertEqual(
            result.content,
            '[合并转发内容｜节点数 1]\n[节点 1｜未知发送者]\n[图片]\n[图片]\n[图片]',
            '老入口的正文里不许出现媒体线索',
        )

    def test_a_broken_nested_forward_still_yields_the_outer_media(self):
        """坏嵌套只坏那一段：外层图照收、正文照旧（失败不吞线索）。"""
        async def fetch(identifier):
            if identifier == 'outer':
                return {'data': {'messages': [{'nickname': 'A', 'message': [
                    _image_segment('https://gchat.qpic.cn/a/outer'),
                    {'type': 'forward', 'data': {'id': 'inner'}},
                ]}]}}
            raise RuntimeError('inner 读不到')

        read = asyncio.run(forward_read_with_media(['outer'], fetch, {'maxDepth': 3}))
        self.assertIn('[嵌套合并转发读取失败｜资源 inner]', read.result.content)
        self.assertEqual([item.source for item in read.media], ['https://gchat.qpic.cn/a/outer'])

    def test_the_per_turn_cap_is_a_single_constant(self):
        """整条消息的转发媒体总数上限是一个 core 常量（不暴露配置）。"""
        self.assertEqual(FORWARD_MEDIA_MAX_PER_TURN, 6)
        self.assertGreaterEqual(FORWARD_MEDIA_MAX_PER_TURN, FORWARD_MEDIA_MAX_PER_FORWARD)


# =========================================================================== #
# 纪律：core 不得 import astrbot
# =========================================================================== #

class CorePurityTests(unittest.TestCase):
    def test_module_source_never_imports_astrbot(self):
        with open(MODULE_PATH, encoding='utf-8') as handle:
            source = handle.read()
        self.assertNotIn('import astrbot', source)
        self.assertNotIn('from astrbot', source)

    def test_importing_the_module_does_not_pull_astrbot_in(self):
        """干净子进程里只 import 本模块，`sys.modules` 里不许出现 astrbot。"""
        # 两种仓库布局下模块的顶层包名不同（开发/CNB 是 `plugin.core.*`，
        # GitHub 发布仓是 `core.*`），所以**按文件路径所在目录**起子进程，
        # 一律用 `core.forward_message` 这个与布局无关的名字（坑：写死 `plugin.` 会让
        # 发布仓布局整项失败）。
        program = (
            'import sys, importlib\n'
            'importlib.import_module(%r)\n'
            'leaked = sorted(m for m in sys.modules if m == "astrbot" or m.startswith("astrbot."))\n'
            'print("LEAKED:" + ",".join(leaked))\n' % ('core.forward_message',)
        )
        proc = subprocess.run(
            [sys.executable, '-c', program], cwd=PLUGIN_ROOT, capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('LEAKED:\n', proc.stdout + '\n', proc.stdout)

    def test_module_is_importable_without_the_package_context(self):
        """纯策略模块：单独按文件路径也能导入（上游那边它就是个独立文件）。"""
        name = 'hdsi_forward_message_standalone'
        spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
        module = importlib.util.module_from_spec(spec)
        # 3.13 的 dataclass 会在装饰时回 `sys.modules` 找自己的模块（拿 `__dict__`），
        # 所以按文件路径导入必须先把模块登记进去——不登记会抛 AttributeError。
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(name, None)
        self.assertEqual(module.extract_forward_ids('<forward id="a"/>'), ['a'])
        self.assertEqual(module.DEFAULT_LIMITS.max_nodes, 30)


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
