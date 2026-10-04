"""气泡切分与正文语音标记（受控偏离，v1.7.7）。

上游只有一种正文标记：分隔符 `<sep/>`（配置项 `runtime.messageSeparator`），模型用它
把一条回复切成多个聊天气泡，投递层逐条发出。本移植版再加一个**正文语音标记**
`<tts/>`（用户点名的新功能）：**由模型自己在正文里决定"这条回复用语音发出"**，
就像它决定要不要分段一样。

语义
-----
* 气泡里出现字面 `<tts/>` → **这个气泡用语音发出**（文本合成为语音）；
* 只写 `<tts/>`、不写分隔符 → 整条回复作为**一条**语音；
* `<sep/>` 与 `<tts/>` 同时出现 → **分段发送语音**（每个分段各一条，顺序与分段一致）；
* 只在某个分段写 `<tts/>` → **只有那个分段是语音**，其余照旧发文字。
  这是"标记的粒度 = 分段"的自然结果：标记跟着它所在的那一段走。

严格度
------
只认**字面** `<tts/>`。大小写变体（`<TTS/>`）、缺斜杠（`<tts>`）、斜杠前带空格
（`<tts />`）一律**不认**，原样留在文本里。刻意不做容错：宽松化会让模型写出来的
畸形串漏进用户看到的字（与 `<sep/>` 的可见文本归一化是两件事——`service/helpers.py`
的 `_VISIBLE_SEP_RE` 只管它自己的分隔符容错）。

标记不是内容
------------
无论这个气泡最终发语音还是发文字，`<tts/>` 都从文本里删掉：绝不进用户看到的字，
也绝不被朗读。**开关关掉时也一样删**——"忽略标记"指的是退回发文字，不是把标记
本身当文本发出去。

开关
----
`voice_enabled=False`（上层读 `model.audio.tts_enabled`）时语音意图一律不成立，
但标记照样删、内容一字不少——降级成纯文字投递，绝不静默丢内容。
"""

from __future__ import annotations

import re
from typing import Any, Optional

__all__ = [
    'DEFAULT_SEPARATOR',
    'VOICE_MARKER',
    'bubble_texts',
    'normalize_bubble_segments',
    'runtime_bubble_segments',
    'split_bubble_segments',
    'strip_voice_marker',
]

#: 正文语音标记。**只认这一个字面量**（严格度见模块文档串）。
VOICE_MARKER = '<tts/>'

#: 分隔符缺失时的兜底（与 `commit_builder._DEFAULT_SEPARATOR` / 上游默认值一致）。
DEFAULT_SEPARATOR = '<sep/>'

#: 换行运行（含单个换行与 CRLF），逐字 = 上游 `splitVisibleReplyBubbles` 的 `/\r?\n+/g`。
#: 连续换行算**一个**边界：`replace` 一次吃掉整段运行，不产生空投递段。
_NEWLINE_RUN_RE = re.compile(r'\r?\n+')


def _pick(mapping: Any, *keys: str, default: Any = None) -> Any:
    """按顺序读第一个存在的键（兼容 snake_case / camelCase 两种拼写）。"""
    if isinstance(mapping, dict):
        for key in keys:
            if key in mapping:
                return mapping[key]
    return default


def strip_voice_marker(text: Any) -> tuple[str, bool]:
    """删掉一段文本里的**全部**字面 `<tts/>`，返回 `(清洗后的文本, 是否写过标记)`。

    只做删除，不动空白与其它字符（调用方各自决定要不要 `strip()`）。
    """
    value = text if isinstance(text, str) else ''
    if VOICE_MARKER not in value:
        return value, False
    return value.replace(VOICE_MARKER, ''), True


def _single_segment(text: str, voice_enabled: bool) -> dict[str, Any]:
    """整条一个分段（`<tts/>` 照样删，开关关着时 `voice` 恒 False）。"""
    cleaned, voice = strip_voice_marker(text)
    return {'content': cleaned, 'voice': bool(voice and voice_enabled)}


def split_bubble_segments(
    content: Any,
    separator: Optional[str] = DEFAULT_SEPARATOR,
    enabled: bool = True,
    voice_enabled: bool = True,
    newline_as_separator: bool = False,
) -> list[dict[str, Any]]:
    """把一条可见回复切成气泡段：`[{'content': str, 'voice': bool}, ...]`。

    切分口径与既有实现逐字一致（`commit_builder._split_bubbles` /
    `ServiceChunk6.splitOutgoingMessage`）：关掉分条、没有分隔符、或内容里根本没有
    分隔符时，整条就是一个分段；切出的空段丢掉；一段都切不出来时（内容全是分隔符）
    返回空列表——投递层按既有的"首段为空则放弃投递"处理。

    `newline_as_separator=True`（上游 1.0.1-rc36 `convertNewlineToSeparator`）：
    模型没按合约给分隔符、而是用**换行**分条时，把换行运行（单个换行与 CRLF 都算、
    连续换行只当**一个**边界）视作气泡边界再发送。两个上游守卫逐字保留：
    ① **内容已含显式分隔符时不转换**（模型自己写了 `<sep/>` 就尊重原样）；
    ② 拆分关闭（`enabled=False`）时该开关无效。转换后一段都切不出来时（内容本身
    只有换行）退回**原样一条**——上游 `parts.length ? parts : [content]`，否则纯换行
    的回复会被这个开关整条删掉。显式分隔符那条老路径仍返回空列表（既有口径）。

    每个分段里字面 `<tts/>` 被删掉，`voice` 记录该段是否要求语音；
    `voice_enabled=False` 时 `voice` 恒为 `False`（标记仍然删）。
    """
    text = content if isinstance(content, str) else ''
    if not enabled or not separator:
        return [_single_segment(text, voice_enabled)]
    normalized = text
    converted = False
    if newline_as_separator and separator not in normalized and _NEWLINE_RUN_RE.search(normalized):
        normalized = _NEWLINE_RUN_RE.sub(separator, normalized)
        converted = True
    if separator not in normalized:
        return [_single_segment(text, voice_enabled)]
    segments: list[dict[str, Any]] = []
    for part in normalized.split(separator):
        cleaned, voice = strip_voice_marker(part)
        cleaned = cleaned.strip()
        if not cleaned:
            continue
        segments.append({'content': cleaned, 'voice': bool(voice and voice_enabled)})
    if segments or not converted:
        return segments
    return [_single_segment(text, voice_enabled)]


def bubble_texts(
    content: Any,
    separator: Optional[str] = DEFAULT_SEPARATOR,
    enabled: bool = True,
) -> list[str]:
    """只要文本（`split_bubble_segments` 的纯文本视图）。

    与 `split_bubble_segments` 的唯一差别是**空结果时退回整条**（标记已删）——
    这是剧本提交侧 `_split_bubbles` 的既有口径：剧本事件必须有非空气泡，
    否则成稿校验会判「message event has empty bubbles」。
    """
    segments = split_bubble_segments(content, separator, enabled)
    if segments:
        return [str(item['content']) for item in segments]
    text = content if isinstance(content, str) else ''
    return [strip_voice_marker(text)[0]]


def runtime_bubble_segments(
    runtime: Any,
    content: Any,
    voice_enabled: bool = True,
) -> list[dict[str, Any]]:
    """按 `runtime` 配置段切分出站气泡：`[{'content', 'voice'}, ...]`。

    分隔符与 `splitReplyMessages` 的读取口径与既有实现逐字一致
    （`messageSeparator` 空/缺失 → `<sep/>`；关掉分条 → 整条一段）。
    这个函数把**配置读取**收在一处，免得每个调用方各写一遍默认值——
    发群的 Chunk2 与私聊投递的 Chunk6 都走它。

    1.0.1-rc36 的 `convertNewlineToSeparator` 也在这里读（上游 `=== true`：只有
    显式真值才开，默认关）。**开启才会把换行当边界**，关着时与 rc36 之前逐字一致。
    """
    section = runtime if isinstance(runtime, dict) else {}
    enabled = _pick(section, 'splitReplyMessages', 'split_reply_messages', True) is not False
    separator = _pick(section, 'messageSeparator', 'message_separator', '')
    separator = separator.strip() if isinstance(separator, str) else ''
    newline_as_separator = _pick(
        section, 'convertNewlineToSeparator', 'convert_newline_to_separator', False,
    ) is True
    return split_bubble_segments(
        content, separator or DEFAULT_SEPARATOR, enabled, bool(voice_enabled),
        newline_as_separator,
    )


def normalize_bubble_segments(value: Any) -> list[dict[str, Any]]:
    """把两种既有的气泡表示归一成 `[{'content', 'voice'}]`。

    * `list[str]`（剧本事件的 `bubbles`、老调用方的纯文本气泡）→ `voice=False`；
    * `list[dict]`（本移植版带语音意图的分段）→ 原样取 `content` / `voice`。

    两种都收是为了让 `prepare_outgoing_delivery` 的既有调用方（含测试夹具）不改签名
    也能继续工作——语音只是多了一个可选字段，不是另一套并存的表示。
    """
    items = value if isinstance(value, (list, tuple)) else []
    segments: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict):
            content = item.get('content')
            if not isinstance(content, str):
                content = '' if content is None else str(content)
            segments.append({'content': content, 'voice': item.get('voice') is True})
        elif isinstance(item, str):
            segments.append({'content': item, 'voice': False})
    return segments
