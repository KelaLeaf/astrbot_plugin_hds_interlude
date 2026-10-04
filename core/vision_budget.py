"""每回合图片预算（v1.9.4）：`model_center.vision.max_per_turn` 的**唯一**判据。

真机现场（用户 2026-10-04）：一条 17 节点的合并转发（节点 2–15 全是图）+ 同批 3 张直发图，
模型只拿到 3 张，而且**上下文里没有任何"总共有多少张、只给了几张"的可数线索**——
她既看不到其余，也不知道还有。更早的现场（§52）：15 张转发图，`imageCount = 0`。

修法分两半，都住在这一处：

* **预算可配**：默认 `3`（省成本那侧 —— 维持 v1.9.1 及之前的行为）。
  schema 的 `default` 与 `CONFIG_DEFAULTS['model']['vision']` 都照 `VISION_IMAGE_BUDGET_DEFAULT`
  抄，`test_configuration.py` 三方对账（照 `video_understanding.VIDEO_CONFIG_DEFAULTS` 那套做法）。
* **不再与单卡上限打架**（v1.9.4）：本项**没被改过**（= 默认 3）时，有效值向上跟随
  「合并转发 → 单条转发最多读取的图片数」（`forward_message.max_images`）—— 用户只调
  那一个键，模型就真的能多看到几张。判据仍然只有 `resolve_image_budget()` 一处，
  转发那一侧只提供**已经夹好的**单卡上限，不重复算一份预算。
* **截断可见**：`image_budget_note(available, granted)` 给模型一句短、可数的线索
  （`[图片×14，本回合仅取前 3 张]`，沿用既有占位文本的形态）；`note_image_budget_skip()`
  给日志一条**同原因节流**的 warn（丢的是内容，用户要看得到，坑 25）。

为什么单独一个模块：`service/config.py`（默认值）与 `service/chunk3.py`（执行）都要它，
而 `chunk3` 反过来 import `config`（**有环**），所以常量与解析不能住在任何一边；
`core/forward_message.py`（转发媒体表上限跟随预算）也要它。

本模块 stdlib 之外零依赖、不 import 兄弟模块、不 import astrbot（与 `video_understanding.py`
同一条纪律）。
"""

from __future__ import annotations

from typing import Any, Optional

__all__ = [
    'VISION_IMAGE_BUDGET_DEFAULT',
    'VISION_IMAGE_BUDGET_MIN',
    'VISION_IMAGE_BUDGET_WARN_INTERVAL_MS',
    'image_budget_note',
    'normalize_image_budget',
    'note_image_budget_skip',
    'resolve_image_budget',
]

# --------------------------------------------------------------------------- #
# 预算常量（**默认值只有这一处**）
# --------------------------------------------------------------------------- #

#: 每回合交给模型的图片数上限（`model_center.vision.max_per_turn` 的默认值）。
#: 取 **3** = 省成本那侧：与 v1.9.1 及之前的行为逐字一致（直发的视觉 `sources[:3]`）。
#: 调大不是白给的：每张图都是一次下载 + 一份原生视觉 token，token / 延迟 / 费用同步上升。
VISION_IMAGE_BUDGET_DEFAULT = 3

#: 下限。**刻意不是 0**：0 = "一张图都不给模型"，那正是 `vision.enabled = false`
#: （总开关）的语义；再开一个"关掉"的入口就是第二个真相（同一把闸两处判）。
VISION_IMAGE_BUDGET_MIN = 1

#: **本项没有上界**（v1.9.7）。早先这里有一个我们自己拍的 `20`，读出来的值一律夹进去：
#: 用户填 30 会静默变成 20，界面 / 日志里都没有一个字说明——正是"改了配置不生效"那一类。
#: 现在读得出多少就是多少：真撞上结构性极限（模型的图片数 / 上下文窗口、网关的限制）
#: 会由**对方**报错，那是一句看得见的话；插件的责任只是**照配置发**、并在真的截断时
#: 留下可数线索（`image_budget_note`）与节流 warn（`note_image_budget_skip`）。
#: 代价仍要写清：每多一张 = 一次下载 + 一份原生视觉 token，token / 延迟 / 费用同步上升。

#: 截断告警的节流间隔（毫秒）。与 `MEDIA_OBSERVABILITY_WARN_INTERVAL_MS` 同档：
#: 丢内容这件事必须让人看见，但同一条原因不能刷屏。
VISION_IMAGE_BUDGET_WARN_INTERVAL_MS = 10 * 60 * 1000


# --------------------------------------------------------------------------- #
# 解析（双拼写 / 脏值回默认 / 夹到区间）
# --------------------------------------------------------------------------- #

def _pick(record: Any, *names: str) -> Any:
    """按给定拼写逐个取值（camelCase / snake_case 双读，与 `service.base.pick` 同语义）。

    照抄 `video_understanding._pick`：本模块是顶层 core 模块，不 import `service` 包
    （那会与 `service` → `vision_budget` 的方向形成环）。
    """
    if isinstance(record, dict):
        for name in names:
            if name in record:
                return record[name]
        return None
    for name in names:
        if hasattr(record, name):
            return getattr(record, name)
    return None


def _text(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return '' if value is None else str(value)
    return value if isinstance(value, str) else str(value)


def normalize_image_budget(value: Any) -> int:
    """一个裸值 → 每回合图片预算（脏值 / 缺失回默认，读得出的值夹到区间）。"""
    if isinstance(value, bool) or value is None:
        return VISION_IMAGE_BUDGET_DEFAULT
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return VISION_IMAGE_BUDGET_DEFAULT
    return max(VISION_IMAGE_BUDGET_MIN, number)


def _forward_limit(forward_limit: Any) -> Optional[int]:
    """单卡转发上限（调用方给的**已夹取值**）→ 整数；读不出来回 `None`（= 没这条线）。"""
    if isinstance(forward_limit, bool) or forward_limit is None:
        return None
    try:
        return int(float(forward_limit))
    except (TypeError, ValueError):
        return None


def resolve_image_budget(section: Any, forward_limit: Any = None) -> int:
    """`model_center.vision` 段 → 每回合图片预算（缺键 = 默认 3）。

    读不出来（脏值 / bool / 非数）一律回 `VISION_IMAGE_BUDGET_DEFAULT`：用户手改坏了
    配置不该悄悄变成"不限制"或者"一张都不给"。读得出来的值夹到
    `>= VISION_IMAGE_BUDGET_MIN`（**没有上界**）。

    **两张卡不再打架（v1.9.4）**：`forward_limit` 是
    `forward_message.forward_read_limits(段).max_images`（单条转发最多读取的图片数，
    由**那一处**解析好；不传 = 老行为，只有本项说了算）。

    规则只有一条 —— **本项没被改过**（读出来等于 `VISION_IMAGE_BUDGET_DEFAULT`）时，
    有效值向上跟随单卡上限：用户只把「单条转发最多读取的图片数」调到 10，这一回合就
    真的有 10 个位置，而不是被一个他没动过的默认 3 削掉（"改了配置却还是 3 张"）。
    本项一旦改成别的值（≠ 3）就以本项为准，单卡上限只管"从卡里取几个坐标"。只向上
    跟随：把单卡上限调**小**（1 / 0）不会连累直发图的额度。

    "没被改过"的判定是"值等于 schema 默认值"——与全项目其他配置项同一条口径
    （宿主每次加载都按 schema 补默认值，落盘后的配置分不出"写过 3"与"没写"）。
    """
    value = normalize_image_budget(_pick(section, 'maxPerTurn', 'max_per_turn'))
    if value != VISION_IMAGE_BUDGET_DEFAULT:
        return value
    limit = _forward_limit(forward_limit)
    if limit is None:
        return value
    return max(value, limit)


# --------------------------------------------------------------------------- #
# 截断线索（给模型）+ 节流日志（给人）
# --------------------------------------------------------------------------- #

def image_budget_note(available: Any, granted: Any) -> str:
    """超预算时的那句可数线索（短；**只有真的截了才说**）。

    形态与转发卡上那句 `[图片×N，仅取前 M 张]` 同一族，多一个"本回合"限定它管的是
    **整个回合**（直发 + 转发 + 视频帧合流之后）而不是某一张卡：

    * 没有候选图 / 预算内全给了 → 空串（没截断就不许说"仅取前 N"，否则每回合都挂个
      `×3` 是噪音，也会让模型误以为被削过）；
    * 超预算 → `[图片×14，本回合仅取前 3 张]`。数值是**平台给出的候选图数**
      （不是"成功下载的字节数"）：字节要等下游才失败得起，而"她少看了 11 张"这件事
      必须现在就说出来。
    """
    try:
        total = int(available)
        taken = int(granted)
    except (TypeError, ValueError):
        return ''
    if total <= 0 or taken >= total:
        return ''
    return '[图片×%d，本回合仅取前 %d 张]' % (total, max(0, taken))


def note_image_budget_skip(
    service: Any,
    session: Any,
    available: Any,
    granted: Any,
    budget: Any = None,
) -> bool:
    """按"会话 + 原因"节流的一条 warn：本回合截掉了多少张图（返回这次是否打了）。

    为什么是 warn 而不是 diagnostic（坑 25）：这里是**真的丢了内容** —— 用户只会在
    剧本里看见她少讲了几张图。不给一条可行动的日志，用户就只能猜"是不是模型不听话"。
    节流键里带会话坐标（与 `note_access_skip` 同一套写法），同一条原因 10 分钟一条。

    `budget` 保留在签名里（调用方与既有用例按旧签名传），v1.9.5 起文案不再渲染具体上限
    —— 用户从「模型中心 → 图片理解」看得到。
    """
    note = getattr(service, 'note_access_skip', None)
    if not callable(note):
        return False
    platform = _text(_pick(session, 'platform')) or '?'
    self_id = _text(_pick(session, 'selfId', 'self_id')) or '?'
    scope = (
        _text(_pick(session, 'channelId', 'channel_id'))
        or _text(_pick(session, 'userId', 'user_id'))
        or '?'
    )
    try:
        total = int(available)
        taken = int(granted)
    except (TypeError, ValueError):
        return False
    return bool(note(
        'image-budget|%s|%s|%s' % (platform, self_id, scope),
        VISION_IMAGE_BUDGET_WARN_INTERVAL_MS,
        '本回合收到 %d 张图片，按「每回合图片数上限」只交给模型前 %d 张'
        '（可在「模型中心 → 图片理解」调大）。',
        total, taken,
    ))
