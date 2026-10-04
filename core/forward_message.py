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
8. **多一个入口 `forward_read_with_media(ids, fetch, limits)`（v1.8.7）**：转发的图片
   坐标（`url` / `file` / `path`）**只在入站当次的响应里有效**（QQ 图床 URL 带短效
   `rkey`），所以媒体不能"读一遍正文、再读一遍媒体"——那要发两次 `get_forward_msg`，
   而且第二次拿到的可能已经是过期的 `rkey`。这一个入口**只取一次页**，同时产出正文与
   媒体条目（`ForwardMediaRead`）。`forward_read_ids` / `forward_read_content` 保持原
   签名与返回值不变（老调用方与逐字断言都不动）。
9. **媒体预算（第四道 = 图片 v1.8.7 / 第五道 = 视频 v1.9.1）**：上游根本没有这一层
   （上游只读正文）。规则与取值见下面的「媒体预算」一节。**超出预算只截断 + 在正文里
   留可数线索**，绝不因为预算丢正文、也绝不因为取媒体失败丢正文。视频那一道默认是
   **0（不读）**：读一段视频要跑 ffmpeg 抽帧 + 占视觉预算，属于"用户明确要了才做"。
10. **视频条目也进 `media`（v1.9.1）**：图片与视频坐标走**同一张** `SessionView.media`
   表（`kind='video'`），下游 `core/video_understanding.py` 的
   `extract_session_video_sources()` 从那张表里认视频坐标——**不另造第二套媒体链路**
   （与 §46 的"结构化媒体表是唯一链路"同一条纪律）。

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

## 媒体预算（v1.8.7 图片 / v1.9.1 视频，上游没有这一层）

真机现场：一条 17 节点的转发里 15 张图，模型只看到 15 个 `[图片]`——`imageCount=0`、
`visualEvidenceMode=none`，视觉观察 / `attachments` / 表情包收藏**一样都拿不到**。
修法是"把节点里的图片坐标也交出去"，但**不能全交**：一条转发里十几张图如果全塞进
原生视觉输入，就是一个 token / 延迟 / 成本的黑洞。所以在上游那三重预算（节点数 /
字符数 / 深度）之外，**再加两道媒体预算**。

第二现场（v1.9.4 起因）：转发那道调大了，模型**还是只看到 3 张**——因为下游 `chunk3`
里还有一道写死的每回合预算，而且截断之后**一个字的线索都没留**（她既看不到其余的，
也不知道还有）。所以 v1.9.4 把每回合预算做成配置
（`model_center.vision.max_per_turn`，见 `core/vision_budget.py`）、把媒体表上限改成
跟随它，并且**每一次截断都必须能被解释**：卡上那句管单卡、当前事件里那句管整回合。

| 预算 | 位置 | 默认 | 区间 | 理由 |
| --- | --- | --- | --- | --- |
| `maxImages`（单条转发最多取几张图） | 本模块 `ForwardReadLimits` / 配置 `forward_message.max_images` | **3** | 0~10 | 转发里的图**先过这一道**。它管的是"从这张卡里取出几个坐标"：取 3 而不是 15，省的是原生图 token 与下载时间；取 0 就等于关掉"转发也看图"，是个**合法**配置（用例钉着）。取出多少张由卡上那句 `[图片×15，仅取前 3 张]` 说清 |
| `maxVideos`（单条转发最多读取几段视频） | 本模块 `ForwardReadLimits` / 配置 `forward_message.max_videos` | **1**（一张卡最多看一段） | 0~10 | 视频比图贵一个量级：每读一段就是一次 ffmpeg（抽帧 + 抽音轨）加一次视觉预算占用。默认**1**＝**配了就生效**，同时把单卡的额外成本封在一次以内（用户口径是"可配置单条转发最多读取的视频数"——配了却默认永不生效不算配置）；显式配成 **0** 才是 v1.8.7 的老行为"转发里的视频一段都不读"。取到的坐标交给 `model_center.video` 那条链，受它自己的总开关 / 群聊开关管 |
| `forward_media_turn_cap()`（整条消息的转发媒体表上限） | 本模块函数（**不暴露配置**；v1.9.4 起**跟随**每回合图片预算） | `max(6, 每回合图片预算, 单卡 max_images)` | — | 它**不是**第三道视觉预算，只是媒体条目表的安全上限：削在这里，下游就再也说不出"一共几张"。所以取值必须 ≥ 用户配得出来的任何一份（`chunk3` 的每回合预算 / 单卡 `max_images`）。真正的视觉上限由 `chunk3` 那一刀执行，并在当前事件里留 `[图片×14，本回合仅取前 3 张]` |
| 单张体积上限 | **不加新键**：复用 `stickers.max_file_size_mb`（默认 10MB） | 视觉路径 `chunk3.MAX_NATIVE_IMAGE_BYTES`（4MB）、收藏路径 `store_collected_sticker` 的 `max_file_size_mb` | — | "一张图多大算大"在这个仓库里已经有答案，再写一份就是第二个真相 |
| 只取前面的 | 节点顺序（含嵌套展开顺序） | — | — | 与三重预算同一条纪律：**排在前面的先拿**，后面的只留线索 |
| 去重 | 坐标字面量（这里）与内容 sha256（`store_collected_sticker`） | — | — | 同一张图 / 同一段视频在节点里出现两次只算一次；字节到手的路径上仍按内容哈希去重（那条判据只有一处） |

**取不到就什么都不做**：节点取不到 / 字段缺失 / 坐标不是可取回的那几类，一律连媒体
条目都不产生——正文照旧（`[图片]` 占位一个不少），**绝不因为媒体失败吞正文**。

**超预算的可见线索**（短、可数，写在那一行上）：

```
[图片×15，仅取前 3 张]      # 按预算取了 3 张（**单卡**这一道，写在这张卡上）
[图片×15，仅取前 0 张]      # 上限配成 0：一张都不取，但仍然数得出来有几张
[视频×2，仅取前 1 段]        # 默认 1：只读排在最前面的那一段
[视频×2，未取]              # 显式配成 max_videos=0：一段都不读，只标注
[视频×5，仅取前 2 段]        # 配成 2：读了 2 段（能不能真看到画面由 model_center.video 决定）
```

下游 `chunk3` 的**每回合**预算再截一刀时，写在当前事件里的是同一族的另一句
（由 `core/vision_budget.image_budget_note()` 产出，只有**真的截了**才有）：

```
[图片×14，本回合仅取前 3 张]   # 直发 + 转发 + 视频帧合流之后的候选 14 张，给了前 3 张
```

数值取自**平台响应里的图片 / 视频段数**（不是"成功取回的字节数"）：字节要等下游下载
才知道，而"她少看了 12 张 / 3 段视频"这件事必须现在就说得出来。每回合那一句取的也是
**候选坐标数**（同上：不是"下载成功的张数"）。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

__all__ = [
    'DEFAULT_LIMITS',
    'FORWARD_FETCH_TIMEOUT_MS',
    'FORWARD_MEDIA_MAX_PER_FORWARD',
    'FORWARD_MEDIA_MAX_PER_TURN',
    'FORWARD_MEDIA_TURN_CAP_MAX',
    'FORWARD_VIDEO_MAX_PER_FORWARD',
    'ForwardMedia',
    'ForwardMediaBudget',
    'ForwardMediaRead',
    'ForwardReadLimits',
    'ForwardReadResult',
    'as_record',
    'clamp_int',
    'extract_forward_ids',
    'extract_forward_media',
    'failure_result',
    'forward_media_budget',
    'forward_media_note',
    'forward_media_turn_cap',
    'forward_read_content',
    'forward_read_ids',
    'forward_read_limits',
    'forward_read_with_media',
    'normalize_forward_messages',
    'normalize_forward_segments',
    'sticker_media_signal',
    'with_timeout',
]


# --------------------------------------------------------------------------- #
# 预算
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ForwardReadLimits:
    """读取预算（上游 `ForwardReadLimits` + 本移植版的 `max_images`）。

    默认值与区间见 `DEFAULT_LIMITS` / `forward_read_limits`：上游
    `index.ts:214-219` 的 `forwardMessage` 组（`maxNodes` 1~100 / `maxCharacters`
    500~32000 / `maxDepth` **0**~8）。**`maxDepth` 的下限是 0**（0 = 不展开嵌套），
    别当成 1。

    `max_images`（v1.8.7）是**第四道预算**：单条合并转发最多取几张图的坐标。
    默认 3（与直发消息的视觉预算同量级）、区间 0~10，理由见模块 docstring。

    `max_videos`（v1.9.1）是**第五道预算**：单条合并转发最多读取几段视频。默认
    **1**（一张卡最多看一段）：配了就生效，同时把单卡的额外成本封在一次以内；
    区间 0~10，显式配 0 才是 v1.8.7 之前的"一段都不读"。
    取到的视频坐标随 `SessionView.media`（`kind='video'`）流给
    `core/video_understanding.py`，由那里的抽帧识别真正"看"（受总开关与群聊开关管）。
    """

    max_nodes: int = 30
    max_characters: int = 8_000
    max_depth: int = 3
    max_images: int = 3
    max_videos: int = 1


#: 上游 `DEFAULT_LIMITS`（`forward-message.ts:73`）+ 本移植版的媒体上限。
DEFAULT_LIMITS = ForwardReadLimits()

#: 单条转发最多取几张图（`ForwardReadLimits.max_images` 的默认值）。
FORWARD_MEDIA_MAX_PER_FORWARD = DEFAULT_LIMITS.max_images

#: 单条转发最多读取几段视频（`ForwardReadLimits.max_videos` 的默认值）。
#: **1 = 一张卡最多看一段**：转发里的视频要真的抽帧才看得见，那是 ffmpeg + 视觉预算的
#: 实打实成本，所以把它封在一次以内（与 `max_images` 默认 3 的差别就在这里：图片本来就
#: 常在转发的正文里）。显式配 0 = 关掉"转发也看视频"。
FORWARD_VIDEO_MAX_PER_FORWARD = DEFAULT_LIMITS.max_videos

#: **整条消息**里所有合并转发的媒体条目总数上限的**下限**（v1.8.7 的常量值）。
#:
#: 为什么不暴露成配置键：它封的是"媒体条目表能长多长"——链路下游
#: （attachments / 视觉来源 / 收藏）都按条数线性增长，不封顶就是把成本交给运气。
#: 真正的视觉输入上限是**每回合图片预算**（v1.9.4 起可配，见 `core/vision_budget.py`）。
#:
#: ⚠️ 它是**下限**而不是实际取值：实际取值由 `forward_media_turn_cap()` 算
#: （`max(本常量, 每回合图片预算, 单卡 max_images)`）。理由就是用户那次真机报告的
#: 一半：把 `forward_message.max_images` 调大、却被另一个看不见的常量削回 6 张，
#: 那等于"改了一个不影响结果的键"。媒体表**不是**执行视觉预算的地方 —— 视觉预算
#: 由 `chunk3` 那一刀（带可数线索）执行，这里只保证"读到的全都在表里"。
FORWARD_MEDIA_MAX_PER_TURN = 6

#: `forward_media_turn_cap()` 里预算那一份的夹取上限（**不是**每回合图片预算的上限；
#: 预算自己的区间在 `core/vision_budget.py`）。这里只防一个荒谬值把表撑爆。
FORWARD_MEDIA_TURN_CAP_MAX = 64

#: **取字节时**的单张体积上限：**不加新键**，复用表情库的 `stickers.max_file_size_mb`
#: （默认 10MB）。这里只写一个给读者看的说明性默认值，**不参与判据** —— 真正的体积闸
#: 在两条既有路径上：视觉 `chunk3.MAX_NATIVE_IMAGE_BYTES`（4MB）与收藏
#: `store_collected_sticker` 的 `max_file_size_mb`。再写一份阈值就是第二个真相。
FORWARD_MEDIA_MAX_IMAGE_MB = 10

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
    """夹取读取预算（上游 `forwardReadLimits` + 本移植版的 `maxImages` / `maxVideos`）。

    逐条对齐上游的区间：`maxNodes` 1~100（默认 30）、`maxCharacters` 500~32000
    （默认 8000）、`maxDepth` **0**~8（默认 3）；本移植版追加 `maxImages` **0**~10
    （默认 3）与 `maxVideos` **0**~10（默认 **1**，理由见模块 docstring）。
    两种拼写都认（优先 camelCase）；传一个已经夹取过的 `ForwardReadLimits` 时原样
    返回（内部递归会这么用，见 `_fetch_forward_nodes` 的第二层预算）。
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
        # 下限是 **0**：0 = "转发里的图一张都不取"（合法配置，只留可数线索），
        # 与 `maxDepth` 的下限是 0 同一种语义。写成 1 就把"关掉"这件事说死了。
        max_images=_limit_value(
            source, 'maxImages', 'max_images', 0, 10, DEFAULT_LIMITS.max_images,
        ),
        # 视频同理：**默认就是 1**（一张卡最多看一段）——预设成"配了就生效"，
        # 同时封住单卡的额外成本；显式配 0 才是"转发里的视频一段都不读"。
        max_videos=_limit_value(
            source, 'maxVideos', 'max_videos', 0, 10, DEFAULT_LIMITS.max_videos,
        ),
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
# 媒体（v1.8.7：转发节点里的图片 / 视频坐标）
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ForwardMedia:
    """转发节点里的**一条**媒体坐标（`source` 就是适配层认的那个字面量）。

    形状刻意与 `SessionView.media[]` 的前两个键同名（`source` / `kind` /
    `summary`）——适配层把它原样并进那条链路，**不另立第二套媒体表**
    （`docs/PORTING_NOTES.md` §46 的唯一链路）。

    * `kind`：`image` / `animated` / `sticker` / `sticker-candidate`，由平台响应里的段字段
      （`sub_type` / `summary`）**观测**得到，取值口径与适配层 `_image_media_kind` 一致
      （三档判据仍然只有那一处实现）。**视频也走这张表**（v1.9.1）：`kind='video'` 的条目
      只有坐标与段类型，不带画面——真正"看"它的是抽帧识别（`model_center.video` /
      `core/video_understanding.py`，坐标经 `SessionView.media` 流过去）。读不读由
      `ForwardReadLimits.max_videos` 决定（默认 1：一张卡最多读一段；配 0 则一段都不读，
      只留 `[视频×K，未取]`）；
    * `source`：可取回的坐标（`https://…` / `file://…` / 裸路径）；
    * `summary`：平台原文（`[动画表情]` 之类）。
    """

    source: str
    kind: str = 'image'
    summary: str = ''


@dataclass
class ForwardMediaBudget:
    """媒体预算的**计数状态**（可变：随遍历推进）。

    刻意**不是** frozen：它就是一次遍历的累加器（`image_count` / `taken` 在走页面时
    递增，`collected` 追加条目）。做成 frozen 会在第一张图上就抛
    `FrozenInstanceError` —— 而调用方那条路径上"收集媒体失败"是允许降级的，
    于是 bug 会被静默吞成"一张图都没有"。可变对象不用装成不可变的。
    """

    max_images: int = FORWARD_MEDIA_MAX_PER_FORWARD
    #: 单条转发最多读取几段视频（默认 1 = 一张卡最多看一段；见 `ForwardReadLimits.max_videos`）。
    max_videos: int = FORWARD_VIDEO_MAX_PER_FORWARD
    #: 平台那一侧**见到**的可取回图片数（含超预算没取的）——线索里的那个 N。
    image_count: int = 0
    #: 真正收下的条目（≤ `max_images` 张图 + ≤ `max_videos` 段视频）。
    collected: list[ForwardMedia] = field(default_factory=list)
    #: 真正收下的**图片**条目数（≤ `max_images`）。
    taken: int = 0
    #: 见到的视频段数（含超预算与重复坐标没取的）。
    video_count: int = 0
    #: 真正收下的**视频**条目数（≤ `max_videos`）。
    video_taken: int = 0
    #: 已经出现过的坐标字面量（同一张图 / 同一段视频在节点里出现两次只算一次）。
    seen: set[str] = field(default_factory=set)
    #: 「超预算线索」是否已经写进正文（每条转发只写一次，短）。
    noted: bool = False


@dataclass(frozen=True)
class ForwardMediaRead:
    """`forward_read_with_media()` 的结果：正文 + 媒体条目 + 可数的截断事实。"""

    result: ForwardReadResult
    media: tuple[ForwardMedia, ...] = ()
    #: 平台见到但**没取**的图片数（`image_count - taken`）。
    skipped_images: int = 0
    #: 见到的视频段数（含没读的那些）。
    video_count: int = 0
    #: 真正读取（收下坐标）的视频段数（≤ `max_videos`）。
    video_taken: int = 0


def forward_media_budget(limits: Any = None) -> ForwardMediaBudget:
    """从预算里取出媒体那一份（`ForwardReadLimits` / 字典都认）。"""
    resolved = forward_read_limits(limits)
    return ForwardMediaBudget(max_images=resolved.max_images, max_videos=resolved.max_videos)


def forward_media_turn_cap(image_budget: Any = None, limits: Any = None) -> int:
    """整条消息的转发媒体条目上限（v1.9.4 起**跟随每回合图片预算**）。

    三个输入取最大：

    1. `FORWARD_MEDIA_MAX_PER_TURN`（6）—— 历史硬顶，保证"两张默认卡"放得下；
    2. `image_budget` —— **调用方已经解析好的**每回合图片预算
       （`model_center.vision.max_per_turn`，解析与默认值在 `core/vision_budget.py`）：
       用户要 10 张就该有 10 个位置，别让媒体表先把它削掉 —— **削在这里，下游就再也
       说不出"一共几张"**。`None` / 脏值按 0 算（老调用方不传预算时行为与 v1.8.7 一致）；
    3. 单卡 `max_images` —— 它本身就是用户配的（0~10），读到的坐标必须都进表。

    这样任何一次截断都只剩两处可解释的闸：**单卡预算**（卡上那句
    `[图片×15，仅取前 3 张]`）与**每回合预算**（当前事件里那句
    `[图片×14，本回合仅取前 3 张]`）。媒体表这一道退化成"够用的安全上限"，
    不再制造第三种解释不了的丢失。

    刻意**不** import `vision_budget`：本模块有一条"按文件路径也能单独导入"的契约
    （`test_module_is_importable_without_the_package_context`），相对导入会把它打破。
    默认值只住在 `vision_budget` 一处，由调用方传进来。
    """
    resolved = forward_read_limits(limits)
    budget = clamp_int(image_budget, 0, FORWARD_MEDIA_TURN_CAP_MAX, 0)
    return max(FORWARD_MEDIA_MAX_PER_TURN, budget, max(0, resolved.max_images))


def forward_media_note(budget: ForwardMediaBudget) -> str:
    """超预算时写在**图片那一行**上的可数线索（短；正文照旧不丢）。

    形态（措辞钉死，用例逐字断言）：

    * 没有图片 → 空串（不多写一个字）；
    * 图片都在预算内 → 空串（**只有真的截了才说**，否则每行都挂个"×3"是噪音）；
    * 超预算 → `[图片×N，仅取前 M 张]`；`M == 0` 时是 `[图片×N，仅取前 0 张]`
      —— 0 也要说出来，否则"配成 0"看起来就像"这条转发里没有图"。
    """
    if budget.image_count <= 0 or budget.image_count <= budget.taken:
        return ''
    return '[图片×%d，仅取前 %d 张]' % (budget.image_count, budget.taken)


def extract_forward_media(node: Any, budget: ForwardMediaBudget) -> list[ForwardMedia]:
    """一个节点里的媒体 → 条目（**按预算截断**，并推进 `budget` 的计数）。

    * 图片：`url` → `file` → `path`（与适配层 `_media_source_from_attrs` 同一条
      优先级），三条都没有 → 不算（数都不数：那不是"没取"，是"拿不到"）；
    * 视频（v1.9.1）：**数**所有视频段（`video_count`，含超预算的），但只在
      `max_videos` 之内**收坐标**（`kind='video'` 的条目）——要不要真抽帧由
      `model_center.video` 那边的总开关与模式决定。默认 `max_videos=1` 时收下排在最
      前面的那一段（线索写 `[视频×K，仅取前 1 段]`）；显式配 `max_videos=0` 才与
      v1.8.7 行为逐字一致：只留 `[视频×K，未取]` 这条线索；
    * 其它段（`json` / `face` / `mface` / 文件 / 嵌套转发…）不看——本移植版没有
      它们的"取回"路径，乱认只会让下游多一次必然失败的取字节。

    `kind` 的口径与适配层的三档表**同一套字段**（`sub_type` 0/1 → `image`/`sticker`、
    2/3/7 → 候选档、`summary` 含「动画」→ `animated`）；这里只是不 import 适配层
    （`core` 不得依赖 `astrbot_bridge`），值由 `_forward_image_kind` 按同一张表算。
    视频条目的 `kind` 是字面量 `'video'`：它是**段类型**，不是那张三档表里的档位。
    """
    record = as_record(node)
    segments = record.get('message')
    if not isinstance(segments, (list, tuple)):
        return []
    collected: list[ForwardMedia] = []
    for raw in segments:
        segment = as_record(raw)
        segment_type = str(segment.get('type') or '').lower()
        data = as_record(segment.get('data'))
        if segment_type in ('image', 'img'):
            source = _forward_media_source(data)
            if not source or source in budget.seen:
                continue
            budget.seen.add(source)
            budget.image_count += 1
            if budget.taken >= budget.max_images:
                continue
            budget.taken += 1
            collected.append(ForwardMedia(
                source=source,
                kind=_forward_image_kind(data),
                summary=str(data.get('summary') or '').strip(),
            ))
        elif segment_type == 'video':
            budget.video_count += 1
            source = _forward_media_source(data)
            if not source or source in budget.seen or budget.video_taken >= budget.max_videos:
                continue
            budget.seen.add(source)
            budget.video_taken += 1
            collected.append(ForwardMedia(
                source=source, kind='video', summary=str(data.get('summary') or '').strip(),
            ))
    return collected


#: 段上的「媒体预算线索」私有键（`_fetch_forward_nodes` 写、`normalize_forward_segments`
#: 读）。为什么借段传：`normalize_forward_messages` 的签名与正文形态是上游逐字契约
#: （那两条 `test_exact_injection_shape` 之类的断言钉着），媒体线索只能**随节点**走，
#: 不能多加一个参数改变调用形态。
_SEGMENT_MEDIA_NOTE = '_hdsiForwardMediaNote'

#: 段上的「视频段数」私有键（同一个 `_fetch_forward_nodes` 写、`normalize_forward_segments`
#: 读）。视频条目（`kind='video'`）走 `SessionView.media` 那条链路，而"见过几段视频 /
#: 读了几段"这两个可数线索是**正文那一行**的旁注，只能随段走。
_SEGMENT_VIDEO_COUNT = '_hdsiForwardVideoCount'

#: 段上的「真正读取的视频段数」私有键（v1.9.1）。0 表示一段都没读（默认配置），
#: 正文里就写 `[视频×K，未取]`——与 v1.8.7 的措辞逐字一致。
_SEGMENT_VIDEO_TAKEN = '_hdsiForwardVideoTaken'


def _collect_forward_media(node: dict[str, Any], budget: ForwardMediaBudget) -> list[ForwardMedia]:
    """`extract_forward_media` + 把两条可数线索挂到段上。

    线索只挂一次、且只挂在对应类型的段上（它是**那一行**的旁注，不是整段转发的旁注）：

    * 图片超预算 → 第一张图片段上写 `[图片×N，仅取前 M 张]`；
    * 视频 → 每一段视频上写"到目前为止见过几段 / 读了几段"（末位那段最后会被写成
      总数）。默认 `max_videos=1` 时读数是 1，正文里就是 `[视频×K，仅取前 1 段]`；
      显式配成 0 时读数是 0，正文里就是 `[视频×K，未取]`（与 v1.8.7 逐字一致）。
      一张图 / 一段视频都没有时一个字都不加。
    """
    collected = extract_forward_media(node, budget)
    if collected:
        budget.collected.extend(collected)
    segments = node.get('message')
    if not isinstance(segments, (list, tuple)):
        return collected
    if not budget.noted:
        note = forward_media_note(budget)
        if note:
            for raw in segments:
                segment = as_record(raw)
                if str(segment.get('type') or '').lower() in ('image', 'img'):
                    segment[_SEGMENT_MEDIA_NOTE] = note
                    budget.noted = True  # 只有真的写进去了才算写过（否则线索会随节点丢掉）
                    break
    if budget.video_count > 0:
        for raw in segments:
            segment = as_record(raw)
            if str(segment.get('type') or '').lower() != 'video':
                continue
            prior = _int_text(segment.get(_SEGMENT_VIDEO_COUNT))
            if budget.video_count > prior:
                segment[_SEGMENT_VIDEO_COUNT] = budget.video_count
            taken = _int_text(segment.get(_SEGMENT_VIDEO_TAKEN))
            if budget.video_taken > taken:
                segment[_SEGMENT_VIDEO_TAKEN] = budget.video_taken
    return collected


def _int_text(value: Any) -> int:
    """段上的私有计数键 → 整数（读不出来当 0；不抛）。"""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _collect_forward_media_page(
    messages: Any, max_nodes: int, budget: ForwardMediaBudget,
) -> None:
    """一页节点里的媒体 → `budget`（节点预算与 `_fetch_forward_nodes` 的页内预算一致）。

    单独抽出来只为一件事：**深度到顶也要收**。上游的节点预算写在一个 `break` 循环里，
    而 `depth >= maxDepth` 那条早退在循环之前——如果把收集塞进循环，`maxDepth=0`
    时最外层那一页的图就一张都收不到了（`maxDepth` 默认 3 时看不出来，配成 0 就露）。
    """
    source = messages if isinstance(messages, (list, tuple)) else []
    remaining = max_nodes
    for node in source:
        remaining -= 1
        if remaining < 0:
            break
        if isinstance(node, dict):
            _collect_forward_media(node, budget)


#: OneBot 图片段 `sub_type` → 种类（与适配层 `_ONEBOT_IMAGE_SUB_TYPES` **同一张表**：
#: 依据见那一处的 NapCat 枚举快照；core 不 import 适配层，所以只复制这两个取值）。
_FORWARD_IMAGE_SUB_TYPES = {'0': 'image', '1': 'sticker'}

#: 平台标了"可能是表情"的 `sub_type`（候选档）。这张表只决定"属于哪一档"，
#: **收不收由 `helpers.sticker_media_signal` 那一处说得算**（名字信号在这里判、
#: GIF / alpha / 尺寸在拿到字节之后判）。
_FORWARD_IMAGE_CANDIDATE_SUB_TYPES = frozenset({'2', '3', '7'})


def _forward_image_kind(data: dict[str, Any]) -> str:
    """图片段的 `sub_type` / `summary` → 媒体种类（口径同适配层三档表）。

    * `sub_type` 0 / 1 → `image` / `sticker`（确定档）；
    * `2` / `3` / `7` → 候选档：**名字信号在这里判一次**（方括号名字 `[中午好]` ——
      入站就有、不用下载），命中 → `sticker`，否则 `sticker-candidate`（能不能收交给
      拿到字节之后的**同一个**结构检查 `helpers.sticker_media_signal`）；
    * 其余 / 缺失 → `image`；
    * `summary` 含「动画」→ 升级 `animated`（NapCat 自己的占位口径，不是我们猜的）。

    ⚠️ 候选档必须在这里判名字信号：直发图片走 `serialize_component` → `_image_media_kind`
    时判的就是它。转发不判的话，**同一个对方、同一张带名字的表情**，直发能收、转发收不了
    ——看起来像"转发里的表情不算表情"。
    """
    sub_type = str(data.get('sub_type') if data.get('sub_type') is not None else '').strip()
    kind = _FORWARD_IMAGE_SUB_TYPES.get(sub_type, '')
    if not kind and sub_type in _FORWARD_IMAGE_CANDIDATE_SUB_TYPES:
        kind = 'sticker' if sticker_media_signal(name=data.get('summary')) else 'sticker-candidate'
    if not kind:
        kind = 'image'
    summary = str(data.get('summary') or '')
    if '动画' in summary:
        kind = 'animated'
    return kind


def sticker_media_signal(name: Any = '', mime_type: Any = '', data: Any = None) -> str:
    """`helpers.sticker_media_signal` 的**惰性**入口（结构信号的唯一实现仍在 helpers）。

    为什么要包一层：`core/forward_message.py` 是"纯策略、能单独按文件路径导入"的模块
    （`test_module_is_importable_without_the_package_context` 钉着），模块级相对 import
    会让那条用例炸；而结构信号的判据**只有一处**（`helpers`，§49.1），不许在这里抄第二份。
    惰性 import + 缓存既保住独立可导入，又保住"一份实现"。

    依赖缺失 / 判据层异常时回空串（= 不命中任何信号）：候选档于是停在候选档，
    **不冒认成"确定是表情"**——判不了时的默认方向是"不认"。
    """
    global _sticker_signal_impl
    if _sticker_signal_impl is None:
        try:
            from .service.helpers import sticker_media_signal as impl  # noqa: PLC0415 - 见 docstring

            _sticker_signal_impl = impl
        except Exception:  # noqa: BLE001 - 独立导入 / 裁剪安装：判不了就不认
            return ''
    try:
        return _sticker_signal_impl(name=name, mime_type=mime_type, data=data)
    except Exception:  # noqa: BLE001 - 判据层异常不许打断正文
        return ''


#: `sticker_media_signal` 的实现缓存（`None` = 还没解析过）。
_sticker_signal_impl: Any = None


def _forward_media_source(data: dict[str, Any]) -> str:
    """图片段 → 可取回的坐标（`url` → `file` → `path`）。

    只认这三类（与适配层 `_media_source_from_attrs` 同一条优先级）。**刻意不认
    `base64://` / 裸文件名之类**：本移植版没有把它们变成字节的路径，认了就等于给
    下游挂一条必然失败的任务（"拿不准=没有"）。
    """
    for key in ('url', 'file', 'path'):
        value = str(data.get(key) or '').strip()
        if value:
            return value
    return ''


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


async def forward_read_with_media(
    ids: Any,
    fetch: Any,
    limits: Any = None,
) -> Optional[ForwardMediaRead]:
    """**只取一次页**，同时产出正文与媒体条目（v1.8.7；上游没有这一层）。

    为什么必须"一次取页"：QQ 图床 URL 里的 `rkey` 是短效的，而 `get_forward_msg` 的
    响应就是那些坐标的唯一来源——"先读一遍正文、再读一遍媒体"既多打一次平台请求，
    又可能第二次拿到的已经不是同一份数据（`rkey` 过期 / 平台缓存）。所以媒体在
    `_fetch_forward_nodes` 走那一页时就地收集（见那个函数的 `media_budget` 参数），
    与正文共用同一次响应。

    契约与失败分支与 `forward_read_ids` **逐条相同**（认不出 → `None`；读不到 →
    `ForwardMediaRead(result=failure_result())` 且 `media` 为空）。差别只有两点：

    * `result.content` 里会多出**可数线索**（`[图片×15，仅取前 3 张]` /
      `[视频×2，未取]`）——只在真的超预算 / 真的见到视频时才有；
    * 返回的是 `ForwardMediaRead`（`result` + `media` + 计数），调用方按需取用。
    """
    resolved = _normalize_ids(ids)
    if not resolved:
        return None
    budget = forward_read_limits(limits)
    media_budget = forward_media_budget(budget)
    try:
        call = _fetcher(fetch)
    except TypeError:
        return ForwardMediaRead(result=failure_result())
    try:
        nodes = await _fetch_forward_nodes(call, resolved[0], budget, 0, media_budget)
    except Exception:  # noqa: BLE001 - 上游 `catch { return failureResult() }`
        return ForwardMediaRead(result=failure_result())
    # 正文与媒体来自**同一份节点**：媒体是在 `_fetch_forward_nodes` 走页面时就地收进
    # `media_budget.collected` 的（连同段上的线索键），这里只读不写。
    # 上限是**图片 + 视频两份预算之和**：两类的截断各自在 `extract_forward_media`
    # 里按 `max_images` / `max_videos` 做好了，这里再按图片那一份切会把视频条目整段砍掉。
    limit = max(0, budget.max_images) + max(0, budget.max_videos)
    collected = media_budget.collected[:limit] if limit else []
    result = normalize_forward_messages(nodes, budget)
    return ForwardMediaRead(
        result=result,
        media=tuple(collected),
        skipped_images=max(0, media_budget.image_count - media_budget.taken),
        video_count=media_budget.video_count,
        video_taken=media_budget.video_taken,
    )


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


async def _fetch_forward_nodes(
    fetch: Any,
    identifier: str,
    limits: ForwardReadLimits,
    depth: int,
    media_budget: Optional[ForwardMediaBudget] = None,
) -> list[Any]:
    """上游 `fetchForwardNodes(internal, id, limits, depth)`；`media_budget` 是本移植版
    追加的末位参数（`None` = 不收集媒体，`forward_read_ids` 那条老路径就是 `None`）。

    取一页节点；`depth < maxDepth` 时把页内每个 `forward` 段就地展开成 `text` 段
    （`segment.type='text'; segment.data={'text': 嵌套正文}`），失败就地写成
    `[嵌套合并转发读取失败｜资源 <id>]`——**整页不会因为一个坏嵌套整体失败**。

    `media_budget` 非 `None` 时，**在就地改写之前**把节点里的图片 / 视频坐标收集起来
    （改写只影响 `forward` 段，图片段不受影响，但顺序必须与正文一致：某个节点的图片
    先于它内部的嵌套正文）。嵌套那一层的页同样按同一个预算收集。

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
    # 节点预算作用在**每一层页内**（上游语义：`remaining` 每页重置）。
    # **媒体收集必须在"深度到顶"之前**：深度上限只影响"要不要递归展开嵌套"，
    # 不影响"这一页里的图算不算数"（`maxDepth=0` 时最外层那一页的图当然要收）。
    # 顺序 = 平台给的节点顺序（含嵌套展开顺序）：先这个节点的图，再它内部的嵌套。
    if media_budget is not None:
        _collect_forward_media_page(messages, limits.max_nodes, media_budget)
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
                nested = await _fetch_forward_nodes(fetch, nested_id, limits, depth + 1, media_budget)
                inner = normalize_forward_messages(
                    nested,
                    {
                        'maxNodes': min(limits.max_nodes, 10),
                        'maxCharacters': limits.max_characters,
                        'maxDepth': limits.max_depth,
                        'maxImages': limits.max_images,
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
            # 媒体预算线索（v1.8.7）：只在 `forward_read_with_media` 那条路上由
            # `_fetch_forward_nodes` 挂上；老路径没有这个键 → 输出与历史逐字一致。
            note = str(segment.get(_SEGMENT_MEDIA_NOTE) or '').strip()
            lines.append(note or '[图片]')
        elif segment_type in ('record', 'audio'):
            lines.append('[语音]')
        elif segment_type == 'video':
            # 视频（v1.9.1）：见过几段 / 真读了几段都写在段上（见 `_collect_forward_media`）。
            # 一段都没读（显式配成 `max_videos=0`；默认是 1，会读到 `[视频×K，仅取前 1 段]`）
            # → `[视频×K，未取]`：**与 v1.8.7 逐字一致**，因为"读不了"与"用户配成不读"
            # 在这条线索上必须是同一句话（正文不该暴露配置差异）。
            count = _int_text(segment.get(_SEGMENT_VIDEO_COUNT))
            taken = _int_text(segment.get(_SEGMENT_VIDEO_TAKEN))
            if count <= 0:
                lines.append('[视频]')
            elif taken <= 0:
                lines.append('[视频×%d，未取]' % count)
            elif taken >= count:
                lines.append('[视频×%d]' % count)
            else:
                lines.append('[视频×%d，仅取前 %d 段]' % (count, taken))
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
