"""QQ 合并转发正文读取 —— 上游 `src/forward-message.ts`（188 行）的逐条移植。

上游对应物：

| 上游 | 本模块 |
| --- | --- |
| `interface ForwardReadLimits` | `ForwardReadLimits`（dataclass） |
| `interface ForwardReadResult` | `ForwardReadResult`（dataclass） |
| `readForwardContent(session, limits)` | `forward_read_content(content, fetch, limits)` |
| `fetchForwardNodes(internal, id, …)` | `_fetch_forward_nodes(fetch, id, …)` |
| `withTimeout(promise, 30_000)` | `with_timeout(awaitable, 30_000)` |
| `forwardReadLimits` / `clampInt` | 同名 |
| `extractForwardIds` | `extract_forward_ids` |
| `normalizeForwardMessages` | `normalize_forward_messages` |
| `normalizeForwardSegments`（未导出） | `normalize_forward_segments`（导出，适配层与测试要用） |
| `failureResult`（未导出） | `failure_result`（导出，适配层失败分支要用） |

## 与上游的受控偏离（除了下面这几条，其余逐行照抄）

1. **本模块是纯策略，不认识 `session` / `bot`**。上游从
   `session.bot.internal._request` 里现取请求入口；AstrBot 的等价物是 aiocqhttp 的
   `call_action`，而它挂在**适配层**（`astrbot_bridge.onebot_client()`，还要逐层判空 +
   认 OneBot 平台）。所以这里把"取一页节点"抽成调用方给的 `fetch`：
   一个 awaitable `fetch(id) -> dict`，或一个带 `async def fetch(id) -> dict` 的对象
   （后者是 `fetch_forward_nodes` 的递归载体，见下）。适配层负责把
   `{'ok', 'error', 'data'}` 或裸 OneBot 帧翻成上游那种响应对象。
   **`core/` 里出现 `astrbot` 这三个字就要被钉**（本仓库的硬约束），这条接缝就是为此存在的。
2. **`with_timeout` 吃 awaitable，不吃 Promise 工厂**：Python 的 `asyncio.wait_for`
   本身就接受 awaitable（内部替我们取消超时分支），而上游是
   `withTimeout(Promise.resolve(internal._request(…)), 30_000)` 那种"先起后包"的写法。
   超时后抛 `asyncio.TimeoutError`，与上游抛 `Error('timeout after 30000ms')` 同义：
   调用方（`_fetch_forward_nodes` / 适配层）一律走失败分支。
3. **内部结构的键名是 snake_case**（`node_count` / `forward_count` / `max_nodes` /
   `max_characters` / `max_depth`）。本仓库的键名法：发给模型的 payload 与数据库列名
   保上游 camelCase，Python 内部结构用 snake_case。`ForwardReadResult` 只在 Python
   内部流转、不发给模型，注入正文的**文本形态**与上游逐字一致。
4. **配置解析双拼写**：上游 `forwardReadLimits` 只认 camelCase（`maxNodes` …）；
   本移植版配置段是 snake_case（`max_nodes` …），所以读值时两种拼写都认
   （优先 camelCase，符合键名法）。配置段名与兜底见适配层的 `_forward_section()`：
   **`forward_message`**（旧文件里的隐藏兼容位 `forward_message_compat` 只作兜底）。
5. **`clamp_int` 的输入更宽容**：上游 `Math.floor(Number(value))` 对 `true` / `'12'` /
   `''` / `null` 都给数（1 / 12 / 0 / 0），Python 的 `int()` 对这些会抛。这里按 JS 的
   `Number()` 语义逐条对齐（见 `clamp_int` 的 docstring），否则"配置页把布尔写进数字
   框"这种输入会变成异常而不是夹取。
6. **`normalize_forward_messages` 的 `messages` 不是列表时当空列表**：上游类型上就是
   `unknown[]`，运行时 `for…of` 一个非可迭代值会抛。适配层拿到的 `data.messages` 可能
   是 `None`（平台没给），所以这里统一收敛成 `[]` 而不是抛异常。

7. **多一个入口 `forward_read_ids(ids, fetch, limits)`**：适配层从 AstrBot 的 `Forward`
   组件拿到的是**裸 id**（那条组件带 `id`，而 `get_message_str()` 只给 `[转发消息]`），
   所以 core 除了"从正文里抠 id"的 `forward_read_content`，还提供"按已抠好的 id 读"的
   `forward_read_ids`。两者共用同一套预算 / 递归 / 失败分支，只是入口不同。

**字符预算与层级语义（与上游逐字一致，别顺手"优化"）**：

- 首行 `[合并转发内容｜节点数 N]` **会计入预算**（`used = len(首行)`）；
- 每行 `append` 逐条扣预算，放不下就在该行**就地截断**成
  `value[:remaining-5] + '[截断]'`，随后 `truncated=True`、本行之后不再追加；
- 预算耗尽时补一行 `[合并转发内容已按安全预算截断]`（它自己也要过 `append`）；
- `nodeCount` 只数**实际展开的节点**（预算用尽就 `break`），`forwardCount` 数**所有**
  遇到过的 forward 段（含未能展开的）；
- 嵌套那层用 `maxNodes = min(limits.maxNodes, 10)` 二次收敛，深度 `depth+1`；
- `depth >= maxDepth` 时**不再递归**（节点原样返回，由段归一化打成
  `[嵌套合并转发，已达到深度上限｜id]`）。

文本形态（注入当前事件的正文，逐字对齐上游）：

```
[合并转发内容｜节点数 2]
[节点 1｜甲（100）｜group]
你好
[图片]
[节点 2｜乙]
[嵌套合并转发｜资源 inner；需要递归读取]
```

失败分支的文案也是上游原文：
`[收到一条合并转发消息，但暂时无法读取内容]`、
`[嵌套合并转发读取失败｜资源 <id>]`。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

__all__ = [
    'DEFAULT_LIMITS',
    'FORWARD_FETCH_TIMEOUT_MS',
    'ForwardReadLimits',
    'ForwardReadResult',
    'as_record',
    'clamp_int',
    'extract_forward_ids',
    'failure_result',
    'forward_read_content',
    'forward_read_ids',
    'forward_read_limits',
    'normalize_forward_messages',
    'normalize_forward_segments',
    'with_timeout',
]


# --------------------------------------------------------------------------- #
# 预算
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ForwardReadLimits:
    """读取预算（上游 `ForwardReadLimits`）。

    默认值与区间见 `DEFAULT_LIMITS` / `forward_read_limits`：上游
    `index.ts:214-219` 的 `forwardMessage` 组（`maxNodes` 1~100 / `maxCharacters`
    500~32000 / `maxDepth` **0**~8）。**`maxDepth` 的下限是 0**（0 = 不展开嵌套），
    别当成 1。
    """

    max_nodes: int = 30
    max_characters: int = 8_000
    max_depth: int = 3


#: 上游 `DEFAULT_LIMITS`（`forward-message.ts:73`）。
DEFAULT_LIMITS = ForwardReadLimits()

#: 上游 `withTimeout(…, 30_000)`：单页 `get_forward_msg` 的等待上限。
FORWARD_FETCH_TIMEOUT_MS = 30_000


@dataclass(frozen=True)
class ForwardReadResult:
    """一次读取的结果（上游 `ForwardReadResult`）。

    `failed=True` 时 `content` 是那条"收到了但读不到"的占位文案——调用方**必须**
    把 `content` 注入当前事件，宁可只说"有一条合并转发"，也不能让整条消息消失。
    """

    content: str
    node_count: int
    forward_count: int
    truncated: bool
    failed: bool

    def to_payload(self) -> dict[str, Any]:
        """转成 snake_case 字典（日志 / 诊断用；不进模型 payload）。"""
        return {
            'content': self.content,
            'node_count': self.node_count,
            'forward_count': self.forward_count,
            'truncated': self.truncated,
            'failed': self.failed,
        }


def failure_result() -> ForwardReadResult:
    """上游 `failureResult()`（文案逐字一致）。"""
    return ForwardReadResult(
        content='[收到一条合并转发消息，但暂时无法读取内容]',
        node_count=0,
        forward_count=0,
        truncated=False,
        failed=True,
    )


def forward_read_limits(value: Any = None) -> ForwardReadLimits:
    """夹取读取预算（上游 `forwardReadLimits`）。

    逐条对齐上游的区间：`maxNodes` 1~100（默认 30）、`maxCharacters` 500~32000
    （默认 8000）、`maxDepth` **0**~8（默认 3）。两种拼写都认（优先 camelCase）；
    传一个已经夹取过的 `ForwardReadLimits` 时原样返回（内部递归会这么用，见
    `_fetch_forward_nodes` 的第二层预算）。
    """
    if isinstance(value, ForwardReadLimits):
        return value
    source = value if isinstance(value, dict) else {}
    return ForwardReadLimits(
        max_nodes=_limit_value(source, 'maxNodes', 'max_nodes', 1, 100, DEFAULT_LIMITS.max_nodes),
        max_characters=_limit_value(
            source, 'maxCharacters', 'max_characters', 500, 32_000, DEFAULT_LIMITS.max_characters,
        ),
        max_depth=_limit_value(source, 'maxDepth', 'max_depth', 0, 8, DEFAULT_LIMITS.max_depth),
    )


def _limit_value(source: dict[str, Any], camel: str, snake: str, low: int, high: int, fallback: int) -> int:
    """双拼写读一个预算字段（优先 camelCase）。

    **"没写"与"写了但不可用"是两回事**（上游只有 `value?.x === undefined` 那一种）：

    * 两个拼写都不在 → 回 `fallback`（配置页从没写过这个键）；
    * 写了但不等于任何数（`'abc'` / 对象）→ 回 `fallback`（`clamp_int` 的 JS NaN 语义）；
    * 写了但是个能转成数的值（含显式 `None`，JS 的 `Number(null) === 0`）→ **夹取**，
      所以 `None` 会落到下限而不是默认值。上游 `clampInt(null, …)` 也是这个结果。
    """
    for key in (camel, snake):
        if key in source:
            return clamp_int(source[key], low, high, fallback)
    return fallback


def _js_number(text: str) -> Optional[float]:
    """JS `Number(text)` 的近似：认十进制 / 十六进制（`0x`）/ `Infinity`，其余 `NaN`。

    为什么不能直接 `float(text)` 再补一句 `int(text, 16)`：Python 的 `int('abc', 16)`
    **是合法的**（= 2748），而 JS 的 `Number('abc')` 是 `NaN`。少了"必须有 `0x` 前缀"
    这道门，`maxNodes: 'abc'` 会被算成 2748 再夹到 100——一个本该回默认值的坏配置静默
    变成了合法上限（实测踩到，测试当场抓住）。
    """
    lowered = text.lower()
    if lowered in ('infinity', '+infinity', '-infinity'):
        return float('-inf') if lowered.startswith('-') else float('inf')
    if lowered.startswith(('0x', '-0x', '+0x')):
        sign = -1 if lowered.startswith('-') else 1
        digits = lowered.lstrip('+-')[2:]
        if not digits:
            return None
        try:
            return float(sign * int(digits, 16))
        except ValueError:
            return None
    try:
        return float(text)
    except ValueError:
        return None


def clamp_int(value: Any, minimum: int, maximum: int, fallback: int) -> int:
    """上游 `clampInt`：`Math.floor(Number(value))` 后夹取，非有限数回 `fallback`。

    JS 的 `Number()` 语义逐条对齐（不照抄会变成异常，而不是夹取）：

    | 输入 | `Number()` | 结果 |
    | --- | --- | --- |
    | `True` / `False` | 1 / 0 | 夹取后的 1 / 0 |
    | `'12'` / `' 12 '` | 12 | 12 |
    | `''` / `None` / `'abc'` | 0 / 0 / NaN | `min` 夹取后 / `fallback` |
    | `12.7` | 12.7 | 12（`floor`） |
    | `'1e3'` | 1000 | 夹取后的 1000 |
    | `'0x10'` | 16 | JS 认十六进制；这里也认 |
    """
    if isinstance(value, bool):
        number: float = 1.0 if value else 0.0
    elif isinstance(value, (int, float)):
        number = float(value)
    elif value is None:
        number = 0.0  # JS: Number(null) === 0
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            number = 0.0  # JS: Number('') === 0
        else:
            number = _js_number(text)
            if number is None:
                return fallback
    else:
        return fallback
    if number != number or number in (float('inf'), float('-inf')):
        # NaN / ±Infinity 回兜底值（上游 `Number.isFinite`）。**必须用 `number != number`
        # 抓 NaN**：`float('nan') in (inf, -inf)` 是 False，写成元组成员判断会漏掉 NaN。
        return fallback
    # `Math.floor`：正数截断、负数向下取整。`int()` 是朝零截断，负数要用 `//`。
    floored = int(number // 1) if number < 0 else int(number)
    return max(minimum, min(maximum, floored))


async def with_timeout(awaitable: Any, timeout_ms: int = FORWARD_FETCH_TIMEOUT_MS) -> Any:
    """上游 `withTimeout(promise, timeoutMs)`：超时抛 `asyncio.TimeoutError`。

    与上游的差别只在形态（见模块 docstring 第 2 条）：这里接受 awaitable 而不是
    Promise 工厂——`asyncio.wait_for` 会在超时后取消它，我们不需要自己清 timer。
    """
    return await asyncio.wait_for(awaitable, timeout_ms / 1000)


# --------------------------------------------------------------------------- #
# 抠 id
# --------------------------------------------------------------------------- #

#: `[CQ:forward,id=…]`（上游 `/\[CQ:forward,([^\]]+)\]/gi`）。
_CQ_FORWARD_RE = re.compile(r'\[CQ:forward,([^\]]+)\]', re.IGNORECASE)

#: `<forward id="…"/>` / `<forward id='…'></forward>`（上游靠 `h.parse`；这里两种引号都认）。
_ATTR_FORWARD_RE = re.compile(
    r'<forward\b([^>]*)>', re.IGNORECASE,
)

#: 属性级匹配（上游走 `h.parse` 之后再按键名取值）。**三个属性各来一条**，按
#: `id → res_id → forward_id` 的顺序写回字典——上游 `{...attrs, ...data}` 那种对象
#: 展开是"后写的赢"，一条 `(?:id|res_id|forward_id)` 交替正则做不到（Python 的 `\b`
#: 在 `res_id` 里匹配不上，`forward_id` 又会先被子串 `id` 命中）。
_ATTR_VALUE = r'''(?:"([^"]*)"|'([^']*)'|([^\s"'>/]+))'''
_ATTR_PATTERNS = tuple(
    (name, re.compile(r'\b%s\s*=\s*%s' % (name, _ATTR_VALUE), re.IGNORECASE))
    for name in ('id', 'res_id', 'forward_id')
)

#: 行内截断标记（上游写死的 `'[截断]'`）。
_CLIP_MARK = '[截断]'

#: 单个 id 的长度上限（上游 `id.length <= 512`）。512 个 UTF-16 码元 vs Python 码点：
#: 恶意超长串在这里的差异只是"多收/少收几个汉字"，不影响正确性，保持 512 这个数。
_MAX_ID_LENGTH = 512


def extract_forward_ids(content: Any) -> list[str]:
    """从消息正文里抠出合并转发的资源 id（上游 `extractForwardIds`）。

    两条来源，顺序与去重规则逐条对齐上游：

    1. Koishi 标记（上游走 `h.parse`，这里等价地用属性正则）：`<forward id="abc"/>`
       —— 多个属性时**后者覆盖前者**（上游 `{...attrs, ...data}` 的对象展开语义），
       所以 `<forward id="a" res_id="b"/>` 取 `b`；
    2. CQ 码 `[CQ:forward,id=def]`，字段名小写、值去空白；同一个 CQ 段里同样后者覆盖。

    过滤条件与上游一致：去空白后非空、长度 ≤ 512、**首次出现的位置决定顺序**
    （重复 id 只留第一次）。
    """
    raw = '' if content is None else str(content)
    ids: list[str] = []

    def add(value: Any) -> None:
        identifier = str(value).strip() if value is not None else ''
        if identifier and len(identifier) <= _MAX_ID_LENGTH and identifier not in ids:
            ids.append(identifier)

    for tag in _ATTR_FORWARD_RE.finditer(raw):
        attrs: dict[str, str] = {}
        for name, pattern in _ATTR_PATTERNS:
            for match in pattern.finditer(tag.group(1)):
                attrs[name] = match.group(1) or match.group(2) or match.group(3) or ''
        # 覆盖次序 id → res_id → forward_id 已在 `_ATTR_PATTERNS` 里定死；取值再按
        # 上游的 `attrs.id ?? attrs.res_id ?? attrs.forward_id` 优先。
        add(attrs.get('id') or attrs.get('res_id') or attrs.get('forward_id'))

    for match in _CQ_FORWARD_RE.finditer(raw):
        fields: dict[str, str] = {}
        for field in match.group(1).split(','):
            index = field.find('=')
            if index > 0:
                fields[field[:index].strip().lower()] = field[index + 1:].strip()
        add(fields.get('id') or fields.get('res_id') or fields.get('forward_id'))

    return ids


# --------------------------------------------------------------------------- #
# 读取（取一页节点）
# --------------------------------------------------------------------------- #

def _fetcher(fetch: Any) -> Callable[[str], Any]:
    """把 `fetch` 归一成一个 `id -> awaitable` 的可调用对象。

    接受两种形态：① 一个可调用对象（`fetch(id)` 返回 awaitable）；② 一个带
    `fetch(id)` 方法的对象（`_fetch_forward_nodes` 的递归载体，见下）。
    """
    if callable(fetch):
        return fetch
    method = getattr(fetch, 'fetch', None)
    if callable(method):
        return method
    raise TypeError('forward_read_content 需要一个可调用的 fetch（见模块 docstring）')


def _normalize_ids(value: Any) -> list[str]:
    """`forward_read_ids` 的入参归一：`str` 当消息正文抠，`list` / `tuple` 当已抠好的 id。

    刻意**不做"一个词就是 id"的猜测**——消息正文 `'普通一句话'` 与一个裸 id 在字符串
    层面无法可靠区分，猜错就会对一条普通消息发出 `get_forward_msg`（实测：这条曾让
    "没有转发就不发请求"的断言当场变红）。所以入参形状自带语义，不靠内容嗅探。
    """
    if isinstance(value, (list, tuple, set, frozenset)):
        ids: list[str] = []
        for item in value:
            identifier = str(item).strip() if item is not None else ''
            if identifier and len(identifier) <= _MAX_ID_LENGTH and identifier not in ids:
                ids.append(identifier)
        return ids
    return extract_forward_ids(value)


async def forward_read_ids(
    ids: Any,
    fetch: Any,
    limits: Any = None,
) -> Optional[ForwardReadResult]:
    """按**已经拿到的 id** 读（传 `str` 也行，那就是一条消息正文，走 `extract_forward_ids`）。

    上游只有 `readForwardContent(session, …)` 一个入口，因为它只有"从 `session.content`
    里抠 id"这一条路；本移植版的 id 来源多了一个：AstrBot 的 `Forward` 组件带 `id`，
    而 `get_message_str()` 只渲染 `[转发消息]`（id 不进文本），适配层可能已经把 id
    抠好了（`forward_ids_for_event()`）。让 core 多收一种入参，比让适配层"把裸 id 拼回
    `<forward id=…/>` 再交给 core 重新解析"干净（后者是个只为迁就签名的往返）。
    """
    resolved = _normalize_ids(ids)
    if not resolved:
        return None
    budget = forward_read_limits(limits)
    try:
        call = _fetcher(fetch)
    except TypeError:
        return failure_result()
    try:
        nodes = await _fetch_forward_nodes(call, resolved[0], budget, 0)
        return normalize_forward_messages(nodes, budget)
    except Exception:  # noqa: BLE001 - 上游 `catch { return failureResult() }`
        return failure_result()


async def forward_read_content(
    content: Any,
    fetch: Any,
    limits: Any = None,
) -> Optional[ForwardReadResult]:
    """上游 `readForwardContent(session, limits)`：读第一条合并转发的正文。

    返回 `None` = **这条消息里根本没有合并转发**（上游 `if (!ids.length) return undefined`），
    调用方什么都不用做；返回 `ForwardReadResult` = 认出来了，`content` 一律要注入
    （`failed=True` 时是"收到了但读不到"的占位文案，见 `failure_result`）。

    等价于 `forward_read_ids(extract_forward_ids(content), …)`；`fetch` 的契约见
    `_fetcher`，它抛异常（含超时、平台错误帧）就是失败分支。
    """
    return await forward_read_ids(extract_forward_ids(content), fetch, limits)


async def _fetch_forward_nodes(fetch: Any, identifier: str, limits: ForwardReadLimits, depth: int) -> list[Any]:
    """上游 `fetchForwardNodes(internal, id, limits, depth)`。

    取一页节点；`depth < maxDepth` 时把页内每个 `forward` 段就地展开成 `text` 段
    （`segment.type='text'; segment.data={'text': 嵌套正文}`），失败就地写成
    `[嵌套合并转发读取失败｜资源 <id>]`——**整页不会因为一个坏嵌套整体失败**。

    节点是原样返回的（可能被就地改写过），与上游一致。
    """
    response = await with_timeout(_call_fetch(fetch, identifier), FORWARD_FETCH_TIMEOUT_MS)
    error = _frame_error(response)
    if error:
        raise RuntimeError(error)
    data = response.get('data') if isinstance(response, dict) and 'data' in response else response
    messages = data.get('messages') if isinstance(data, dict) else None
    if not isinstance(messages, list) or not messages:
        return []
    if depth >= limits.max_depth:
        return messages
    remaining = limits.max_nodes
    for node in messages:
        remaining -= 1
        if remaining < 0:
            break
        if not isinstance(node, dict):
            continue
        segments = node.get('message')
        if not isinstance(segments, list):
            continue
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            segment_type = str(segment.get('type') or '').lower()
            if segment_type != 'forward':
                continue
            payload = segment.get('data')
            payload = payload if isinstance(payload, dict) else {}
            nested_id = str(
                payload.get('id') or payload.get('res_id') or payload.get('forward_id') or ''
            ).strip()
            if not nested_id:
                continue
            try:
                nested = await _fetch_forward_nodes(fetch, nested_id, limits, depth + 1)
                inner = normalize_forward_messages(
                    nested,
                    {
                        'maxNodes': min(limits.max_nodes, 10),
                        'maxCharacters': limits.max_characters,
                        'maxDepth': limits.max_depth,
                    },
                    depth + 1,
                )
                segment['type'] = 'text'
                segment['data'] = {'text': inner.content}
            except Exception:  # noqa: BLE001 - 坏嵌套只坏那一段
                segment['type'] = 'text'
                segment['data'] = {'text': '[嵌套合并转发读取失败｜资源 %s]' % nested_id}
    return messages


def _call_fetch(fetch: Any, identifier: str) -> Any:
    """调用 `fetch(id)` 并把异常原样带出去（超时/平台错误在 `forward_read_content` 收敛）。

    契约：**`fetch` 收一个 id 字符串**。OneBot 的 `get_forward_msg` 要
    `{'id': …}`，由适配层的闭包负责包一层——这里不猜调用方想要哪种签名。
    """
    return fetch(identifier)


def _frame_error(response: Any) -> str:
    """上游的错误帧判定：`retcode != 0` 或 `status != 'ok'`（**缺失不算错**）。

    上游先判 `retcode != null && Number(retcode) !== 0`，再判
    `status && status !== 'ok'`；两者都为真值时才算失败。OneBot 11 的实现常常两个都
    不填（`{}`），那种"空壳响应"在上游被当成成功、随后 `messages` 为空——这里保持
    同一条语义，免得把正常的空转发误判成读取失败。
    """
    if not isinstance(response, dict):
        return ''
    retcode = response.get('retcode')
    if retcode is not None:
        try:
            if int(str(retcode).strip()) != 0:
                return _frame_text(response, 'retcode=%s' % retcode)
        except (TypeError, ValueError):
            return _frame_text(response, 'retcode=%s' % retcode)
    status = response.get('status')
    if status and str(status) != 'ok':
        return _frame_text(response, str(status))
    return ''


def _frame_text(response: dict[str, Any], fallback: str) -> str:
    for key in ('wording', 'message'):
        value = str(response.get(key) or '').strip()
        if value:
            return value
    return fallback


def as_record(value: Any) -> dict[str, Any]:
    """上游 `asRecord`：非 dict（含 list / None）一律当空 dict。

    **不要**写成 `if not value`：JS 的 `isRecord({})` 为真，空 dict 是合法记录
    （`AGENTS.md` 的语义差清单里有这一条）。
    """
    if isinstance(value, dict):
        return value
    return {}


# --------------------------------------------------------------------------- #
# 归一化（节点 / 段 → 注入正文）
# --------------------------------------------------------------------------- #

def normalize_forward_messages(messages: Any, limits: Any = None, depth: int = 0) -> ForwardReadResult:
    """上游 `normalizeForwardMessages(messages, limits, depth)`。

    产出注入当前事件的正文。预算、层级、截断标记的语义见模块 docstring——这里的
    每一行都是上游的翻版，改动前先问"上游为什么这么写"。
    """
    budget = forward_read_limits(limits)
    source = messages if isinstance(messages, (list, tuple)) else []
    lines: list[str] = ['[合并转发内容｜节点数 %d]' % len(source)]
    used = len(lines[0])
    node_count = 0
    forward_count = 0
    truncated = False
    failed = False

    def append(value: str) -> bool:
        """追加一行；返回"整行都放下了吗"（上游 `append` 的布尔语义）。

        **空行也算一行**（上游 `if (!append(line)) break` 对空串同样 push），所以这里
        不能"空串直接返回成功"。
        """
        nonlocal used, truncated
        remaining = budget.max_characters - used
        if remaining <= 0:
            truncated = True
            return False
        if len(value) > remaining:
            # 上游 `value.length > remaining ? value.slice(0, max(0, remaining - 5)) + '[截断]'`：
            # `[截断]` 正好 5 个字符，所以留下的正文是 `remaining - 5`；`remaining` 只够
            # 放标记时正文被削成 0 字符，那一行就只剩 `[截断]`。
            clipped = value[:max(0, remaining - len(_CLIP_MARK))] + _CLIP_MARK
        else:
            clipped = value
        lines.append(clipped)
        used += len(clipped) + 1
        if clipped != value:
            truncated = True
        return clipped == value

    for raw_node in source:
        if node_count >= budget.max_nodes:
            truncated = True
            break
        node = as_record(raw_node)
        node_count += 1
        sender_record = as_record(node.get('sender'))
        sender = str(
            node.get('nickname') or sender_record.get('nickname') or node.get('user_id') or '未知发送者'
        ).strip() or '未知发送者'
        user_id = str(node.get('user_id') or sender_record.get('user_id') or '').strip()
        label = '%s（%s）' % (sender, user_id) if user_id else sender
        node_type = str(node.get('message_type') or '').strip()
        append('[节点 %d｜%s%s]' % (node_count, label, '｜%s' % node_type if node_type else ''))
        segments = node.get('message')
        segments = segments if isinstance(segments, list) else []
        body = normalize_forward_segments(segments, budget, depth)
        forward_count += body['forwardCount']
        failed = failed or bool(body['failed'])
        for line in body['lines']:
            if not append(line):
                break
        if body['truncated']:
            truncated = True

    if truncated:
        # 上游 `if (truncated) append('[合并转发内容已按安全预算截断]')`：**照样走
        # `append`**，预算已经耗尽时它自己也会被就地截成 `[截断]`。这样"输出的总长
        # 不超过预算 + 标记长"这条性质才成立（`truncated` 已经把话说清楚了）。
        append('[合并转发内容已按安全预算截断]')
    return ForwardReadResult(
        content='\n'.join(lines),
        node_count=node_count,
        forward_count=forward_count,
        truncated=truncated,
        failed=failed,
    )


def normalize_forward_segments(segments: Any, limits: Any, depth: int) -> dict[str, Any]:
    """上游 `normalizeForwardSegments(segments, limits, depth)`。

    返回 `{'lines', 'forwardCount', 'truncated', 'failed'}`（键名与上游一致：这个字典
    就是上游那个返回对象的字面形态，`truncated` / `failed` 目前恒为假值——上游也是
    这样，它们只是给调用方留的字段）。**逐条对齐上游的段类型分支**，包括"未知类型
    也留一条 `[未支持的消息类型：x]`"。
    """
    lines: list[str] = []
    forward_count = 0
    truncated = False
    failed = False
    budget = limits if isinstance(limits, ForwardReadLimits) else forward_read_limits(limits)
    source = segments if isinstance(segments, (list, tuple)) else []
    for raw in source:
        segment = as_record(raw)
        segment_type = str(segment.get('type') or '').lower()
        data = as_record(segment.get('data'))
        if segment_type == 'text':
            text = str(data.get('text') or '').strip()
            if text:
                lines.append(text)
        elif segment_type == 'at':
            name = str(data.get('name') or data.get('qq') or data.get('user_id') or '').strip()
            lines.append('[@%s]' % name if name else '[@]')
        elif segment_type == 'reply':
            identifier = str(data.get('id') or '').strip()
            lines.append('[回复消息 %s]' % identifier if identifier else '[回复]')
        elif segment_type == 'forward':
            forward_count += 1
            identifier = str(data.get('id') or data.get('res_id') or data.get('forward_id') or '').strip()
            if depth >= budget.max_depth:
                lines.append(
                    '[嵌套合并转发，已达到深度上限｜%s]' % identifier if identifier
                    else '[嵌套合并转发，已达到深度上限]'
                )
            else:
                lines.append(
                    '[嵌套合并转发｜资源 %s；需要递归读取]' % identifier if identifier
                    else '[嵌套合并转发；资源标识缺失]'
                )
        elif segment_type in ('image', 'img'):
            lines.append('[图片]')
        elif segment_type in ('record', 'audio'):
            lines.append('[语音]')
        elif segment_type == 'video':
            lines.append('[视频]')
        elif segment_type == 'file':
            detail = str(data.get('name') or data.get('file') or '').strip()
            lines.append('[文件：%s]' % detail if detail else '[文件]')
        elif segment_type:
            lines.append('[未支持的消息类型：%s]' % segment_type)
    return {
        'lines': lines,
        'forwardCount': forward_count,
        'truncated': truncated,
        'failed': failed,
    }
