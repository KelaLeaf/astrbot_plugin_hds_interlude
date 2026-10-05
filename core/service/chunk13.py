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

import json
import math
import re
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

from ..endpoints import endpoint_account_key
from ..qzone import (
    QZONE_VISIBILITY_VALUES,
    QzoneActionError,
    call_qzone_action,
    call_qzone_cgi,
    QZONE_CGI_BY_ID,
    QZONE_COOKIE_APIS,
    QZONE_COOKIE_DOMAINS,
    QzoneCgiUnavailable,
    evaluate_qzone_gate,
    normalize_qzone_feed_entry,
    normalize_qzone_msg_entry,
    probe_qzone_available,
    qzone_action_label,
    qzone_feed_candidates,
    qzone_feed_content_lookup,
    qzone_intent_from_payload,
    qzone_media_limit_note,
    qzone_media_limit_warning,
    qzone_reaction_deltas,
    qzone_records_for_endpoint,
    qzone_visible_value,
    qzone_visibility_label,
    resolve_qzone_config,
)
from ..time import dt_ms, iso, parse_dt
#: 动态里的图片 / 视频走的是**既有**那两条链：`_vision_config` / `_image_budget` 是
#: 图片理解那两处判据（chunk3），`collect_video_sources` / `video_config` 是视频理解
#: 那两处判据（`core/video_understanding.py`）。这里只**读结果**，一份都不重算——
#: 所以"判据一处"在动态这条路上也成立。
#: ⚠️ **不读 `forward_message`**：那是"合并转发"的键，与 QQ 空间动态无关（v1.9.6 解开
#: 的耦合——`min(feed_video_cap, forward_message.max_videos)` 曾让"只调本组上限"照样
#: 只取 1 段）。动态视频段数的第二道闸是**每回合视觉预算**（`_image_budget`，帧与图片
#: 共用同一条视觉通道），不是转发键。
#: chunk3 不 import chunk13（与 chunk1 → chunk3 同一条依据），这条模块级依赖不成环。
from ..video_understanding import (
    collect_video_sources,
    video_config,
)
from .base import ServiceBase, pick
from .chunk3 import _image_budget, _vision_config
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
#: 改可见范围前回看多少条说说找"当前正文"（update 要把正文原样带回去），
#: 以及改完之后回读校验时同样回看多少条。
QZONE_VISIBILITY_LOOKUP_COUNT = 30
#: **改可见范围的跳闸标志**（落在插件数据目录，照 `action_permissions.json` 的做法）。
#:
#: 只有一种情况会写下它：回读校验实测到"改完可见范围后这条说说的**附件变少**"
#: ——那就说明"富文本字段留空 = 不改动图片/视频"这个前提（见 §43 的 H1 裁定）
#: 至少在这条说说不成立。此后**带附件**的说说一律拒绝改可见范围（纯文字不受影响），
#: 直到用户**删掉这个文件**（日志里会写明路径与做法）。绝不允许静默毁第二条说说。
QZONE_VISIBILITY_GUARD_FILE = 'qzone_visibility_guard.json'
#: 回读校验比对附件时，附件 = 图片 + 视频（`parse_mood` 的 `pic` / `video`）。
QZONE_COMMENT_SUMMARY_CHARS = 80
#: 「好友动态只拿到元数据、正文没取到」这条说明的节流间隔（毫秒，§55）。
QZONE_FEED_CONTENT_NOTE_INTERVAL_MS = 30 * 60 * 1000
#: 动态媒体（图片 / 视频）识别相关说明的节流间隔（毫秒）。与 `VISION_IMAGE_BUDGET_WARN_INTERVAL_MS`
#: / `VIDEO_WARN_INTERVAL_MS` 同档：能力缺失与截断都必须让人看见，但不能刷屏。
QZONE_FEED_MEDIA_NOTE_INTERVAL_MS = 10 * 60 * 1000

# --------------------------------------------------------------------------- #
# 被评论 / 被点赞感知（rc29 + rc33）
# --------------------------------------------------------------------------- #

#: 只看最近多少天她自己发出去的说说（上游 `7 * 24 * Time.hour`）。
QZONE_REACTION_WINDOW_DAYS = 7
#: 一次拉取她自己的说说列表取几条（上游 `num: 10`）。
QZONE_REACTION_MSG_NUM = 10
#: **单轮入账预算**（上游 `reactionBudget = 3`）：一轮最多为 3 条说说写感知条目，
#: 其余增量**不推进基线**、留给下一轮重新发现——这是上游防"第 4 条起永久丢失"的判据，
#: 顺序（先写基线、再写条目）也是它的一部分，别只搬纯函数不搬这段。
QZONE_REACTION_BUDGET = 3
#: 「这条读通道拿不到点赞数」这条降级说明的节流间隔（毫秒）。
QZONE_REACTION_LIKE_NOTE_INTERVAL_MS = 60 * 60 * 1000


_MILLISECONDS_PER_MINUTE = 60_000


def qzone_feed_media_capability(service: Any) -> dict[str, Any]:
    """动态媒体识别的能力判据（**唯一一处**）：不新增"是否识图"开关。

    读的是**既有**的两个总开关，一个字都不另造：

    * **图片**：`model_center.vision.enabled` 开着 **且** 有可用的识图模型
      （`service.vision_describer.available()`，即连接池里勾了「用于侧端识图」的那条）。
      两者缺一，动态里的图片就**不调用**任何模型——如实标注 + （只在"开着但没配模型"时）
      一条可行动的 warn。"图片理解关着"不是故障，不刷 warn。
    * **视频**：`model_center.video.enabled` 开着。模式（抽帧 / 原生 / 外挂）、ffmpeg 有没有、
      群聊那道路径**都交给既有视频链自己判**（`collect_video_sources` 开头那四道闸），
      这里不预判——预判就是第二套判据。

    返回 `{'images', 'image_reason', 'videos', 'video_reason'}`：`*_reason` 是给人看的
    一句"为什么没做"（能力真缺时进 warn，关着时只作内部记录）。
    """
    vision = _vision_config(service)
    vision_on = pick(vision, 'enabled') is True
    describer = getattr(service, 'vision_describer', None)
    available = getattr(describer, 'available', None)
    images_ready = bool(vision_on and callable(available) and available())
    if not vision_on:
        image_reason = '「图片理解」没开（模型中心 → 图片理解）'
    elif not images_ready:
        image_reason = '没有可用的识图模型（模型中心 → 图片理解 → 图片解析模型）'
    else:
        image_reason = ''
    video = video_config(service)
    videos_on = pick(video, 'enabled') is True
    return {
        'images': images_ready,
        'image_reason': image_reason,
        'videos': videos_on,
        'video_reason': '' if videos_on else '「视频理解」没开（模型中心 → 视频理解）',
    }


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


def _as_list(value: Any) -> list[Any]:
    """把平台回执里的列表字段收敛成 list（None / 字典 / 标量都别炸）。"""
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return list(value.values())
    return []


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _rows(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _row_ms(row: Any) -> Optional[int]:
    """审计行的 `createdAt` 毫秒数；无法解析返回 None（等价 SQL 里 `>=` 不成立）。"""
    parsed = parse_dt(pick(row, 'createdAt', 'created_at'))
    return None if parsed is None else dt_ms(parsed)


def _same_reaction_baseline(row: Any, patch: Mapping[str, Any]) -> bool:
    """要写的基线与行上现有值完全一致 → 不必写库（上游 `(row.commentNum ?? null) !== baseline.commentNum`）。

    只在**已知字段**上比：`patch` 里没有点赞数（这条回执没给）时不做比较，
    免得把"不可知"写成 0 再自我确认。
    """
    for key, value in patch.items():
        current = pick(row, key)
        if current is None and value is None:
            continue
        if current is None or value is None:
            return False
        if isinstance(current, bool) or isinstance(value, bool):
            if current != value:
                return False
            continue
        try:
            if int(current) != int(value):
                return False
        except (TypeError, ValueError):
            if current != value:
                return False
    return True


class _QzoneTargetAccountNotQq(RuntimeError):
    """多通道账号标识校验（rc33）：这条动态的账号**不是数字 QQ 号**。

    QQ 空间读通道（`emotion_cgi_msglist_v6`）只能按数字 QQ 号定位某个人的说说列表；
    `wxid_*` / `@chatroom` 这类字符串账号读不到。旧实现会把 `targetUin` 这个键**省掉**
    ——CGI 于是按"我自己"返回，那就是**认错账号**（拿别人的动态去比对她的说说）。
    这里改成显式失败，让上层写一条能照着做的 warn。异常只在本模块内部流转。
    """

    def __init__(self, value: Any, reason: str) -> None:
        super().__init__(
            '账号 %s %s（空间读通道只按数字 QQ 号定位，省掉 targetUin 会查成"我自己"）'
            % (
                '(空)' if value in (None, '') else value,
                '不是数字 QQ 号' if reason == 'non-qq' else '没有账号标识',
            )
        )
        self.target_reason = reason


def _kind_label(kind: Any) -> str:
    """上游那串 `kind === 'post' ? '发帖' : …` 的中文档位名。"""
    if kind == 'post':
        return '发帖'
    if kind == 'comment':
        return '评论'
    if kind == 'forward':
        return '转发'
    if kind == 'visibility':
        return '改可见范围'
    return '点赞'


def onebot_target_id(value: Any) -> Any:
    """上游 `onebotTargetId`（`upstream/src/service.ts:9719`）：通道目标 ID 的类型规则。

    * 先剥掉 `private:` / `group:` 前缀（上游 `/^(?:private:|group:)/`）；
    * **纯数字串转 `int`**（QQ 的 `user_id` / `group_id` 规范类型）；
    * 其余（`wxid_xxx` / `@chatroom` 等**字符串账号**）**原样返回**。

    上游注释点名的 bug：早先 `Number()` 一刀切会把非数字 ID 变成 `NaN` 直发上游，
    多通道（QQ / 微信 / 其它 OneBot 实现）下就会**认错账号**。返回类型不同
    （`int` vs `str`）本身就是这条判据的可见结果，调用方必须分开处理。
    """
    if isinstance(value, bool) or value is None:
        return ''
    if isinstance(value, (int, float)):
        return int(value) if float(value).is_integer() else str(value)
    raw = re.sub(r'^(?:private:|group:)', '', str(value).strip(), flags=re.IGNORECASE)
    return int(raw) if re.fullmatch(r'\d+', raw) else raw


def _qzone_target_uin(value: Any) -> tuple[Optional[int], str]:
    """QQ 空间读通道的 `targetUin` + **认不出账号时的原因**（判据一处）。

    QQ 空间的 CGI（`emotion_cgi_msglist_v6`）只能按**数字 QQ 号**定位某人的说说列表；
    `onebot_target_id()` 认出来的字符串账号（`wxid_*` / `@chatroom`）在这里**没法查**。
    旧实现把它当 `Number(NaN)` 直接**省掉这个键**——于是 CGI 会按"我自己"去查，
    这正是 rc33 那条「多通道下别认错账号」在空间链路上的现场。

    返回 `(target, reason)`：`reason` 为空 = 可以用；`'missing'` = 这条动态没带账号；
    `'non-qq'` = 账号不是数字 QQ 号（调用方必须**可见地**跳过并说明下一步，
    绝不能省掉键去查自己的列表）。
    """
    target = onebot_target_id(value)
    if isinstance(target, int):
        return target, ''
    if not target:
        return None, 'missing'
    return None, 'non-qq'


def _target_uin_param(value: Any) -> Any:
    """`target_uin` 参数：上游 `Number(targetUin)`；非数字则**不带这个键**。

    JS 的 `Number('abc')` 是 NaN、序列化成 JSON 会变 `null`；本移植版直接省略，
    免得把 `null` 当"归属 0"发给平台。判据复用 `onebot_target_id()`（一处），
    所以 `private:10002` 这类带前缀的写法同样认得出来（rc33 的账号标识规则）。
    """
    target = onebot_target_id(value)
    return target if isinstance(target, int) else None


def _note_qzone_feed_media(service: Any, key: str, message: str, *args: Any) -> bool:
    """动态媒体那条**可见**说明的出口（节流优先；返回是否说出了口）。

    与 `video_understanding._warn()` 同一条纪律：宿主 / 替身没给节流口时**照样要说话**
    （退化成一条裸 warn）——绝不能因为"节流口不在"就把能力缺失吞成静默（坑 25）。
    """
    note = getattr(service, 'note_access_skip', None)
    if callable(note):
        return bool(note(key, QZONE_FEED_MEDIA_NOTE_INTERVAL_MS, message, *args))
    report = getattr(service, 'report_standalone', None)
    if callable(report):
        report('warn', message % args if args else message)
        return True
    return False


def _note_qzone_media_limit(
    service: Any, key: str, kind: str, available: Any, granted: Any, source: str,
) -> bool:
    """截断那条**节流可行动** warn（说清楚是哪一道闸、去哪儿调大；返回是否打了）。

    与 `vision_budget.note_image_budget_skip()` 同一条纪律：丢的是内容，用户只能从
    剧本里看出"她少看了几张"，不给日志就只剩猜（坑 25）。节流键带这条原因本身。
    """
    text = qzone_media_limit_warning(kind, available, granted, source)
    if not text:
        return False
    return _note_qzone_feed_media(service, key, '%s', text)


def _qzone_feed_observation(
    owner: Any, facts: Any, published_at: Any = '', media: Any = None,
) -> str:
    """一条好友动态进剧本时的**观察措辞**（唯一实现，§55 / §70）。

    这一条要同时满足三件事，所以措辞不能随便改：

    1. **她确实看到的**内容要写成观察（"她刷到了…：<正文>"）——系统提示词把
       `actor=system` 的条目解释成"插件记账"，光写"某好友发布了说说"，模型就不会
       把它当成她的见闻（真机症状：动态抓到了、正文也有，剧本里却完全没有
       "她看到了什么"）。
    2. **没看到就别声称看到**：正文没取到（列表里没有这条 tid / 拉取失败）时只记
       "有这么一条说说"，一个字的正文都不许编，也不许写成"她看到了内容"。
    3. **"她没有文字"与"我们没取到"是两件事**（§70 真机）：只有图片的说说、
       转发动态本来就没有自己的文字——那种情况写"只有图片"/"转发的说说"，
       **不许**写"正文没取到"（那会把用户引去查登录态）。

    `facts` 是 `qzone.qzone_feed_content_lookup` 的产出（判据只有那一处）：
    `found` / `content` / `images` / `videos` / `forward`。转发链上有原文时优先把它当正文
    （"原内容是什么"是这条事实的重点）；原文与附言都拿不到就如实说"原内容没取到"。

    `published_at` 是说说**自己的**发布时间：它不再当条目的 `occurredAt`
    （见 `qzone_feed_sweep` 的说明），所以放在正文里保留这一条事实。

    `media` 是 `_qzone_feed_media_lines()` 的产出（一个字符串列表）：图片 / 视频那几条
    事实与线索**接在同一段观察后面**（同一个条目、同一处措辞判据）——图片和视频不是
    另一条动态，分成两个条目会让模型以为她刷到了两条。
    """
    name = str(owner if owner is not None else '').strip() or '一位好友'
    published = str(published_at if published_at is not None else '').strip()
    stamp = '（发布 %s）' % published if published else ''
    data = facts if isinstance(facts, Mapping) else {}
    text = str(data.get('content') or '').strip()
    images = _rows(data.get('images'))
    forward = data.get('forward') if isinstance(data.get('forward'), Mapping) else None
    lines = [
        str(item).strip() for item in _rows(media) if str(item or '').strip()
    ]
    tail = ('\n' + '\n'.join(lines)) if lines else ''
    if forward is not None:
        original = str(pick(forward, 'content') or '').strip()
        if original or text:
            return '[好友动态] 她刷到了 %s 转发的说说%s：%s%s' % (
                name, stamp, clip(original or text, QZONE_COMMENT_SUMMARY_CHARS), tail,
            )
        if images:
            return '[好友动态] 她刷到了 %s 转发的说说%s，只有图片%s' % (name, stamp, tail)
        return '[好友动态] 她刷到了 %s 转发的说说%s，原内容没取到%s' % (name, stamp, tail)
    if text:
        return '[好友动态] 她刷到了 %s 的说说%s：%s%s' % (
            name, stamp, clip(text, QZONE_COMMENT_SUMMARY_CHARS), tail,
        )
    if images:
        return '[好友动态] 她刷到了 %s 的一条说说%s，只有图片%s' % (name, stamp, tail)
    if data.get('found'):
        return '[好友动态] 她刷到了 %s 的一条说说%s，没有文字%s' % (name, stamp, tail)
    return '[好友动态] 她刷到了 %s 的一条说说%s，但正文没取到%s' % (name, stamp, tail)


def _qzone_feed_row(raw: Any) -> dict[str, Any]:
    """把动态行补成"归一化层认识"的形状：只做**时间字段的搬运**。

    平台接口回的是 `abstime`（秒，`feedstime` 是人读格式），而归一化层只认 `time`。
    而 `normalize_qzone_feed_entry` / `fresh_qzone_feeds` / `qzone_feed_candidates`
    只认 `time`——不补这一下，CGI 通道回来的每条动态都会被当成 1970 年、被新鲜度
    过滤整批丢掉（"看着在轮询、其实一条都不进剧本"）。

    **只动这一个键**：协议层（`core/qzone_cgi.py`）的解析结果一个字不改。
    """
    row = dict(raw) if isinstance(raw, Mapping) else {}
    if 'time' in row:
        return row
    for source in ('abstime', 'feedstime'):
        stamp = row.get(source)
        if stamp not in (None, ''):
            row['time'] = stamp
            break
    return row


def _js_round(value: float) -> int:
    """JS `Math.round`：半值向上（Python 的 `round` 是银行家舍入）。"""
    return int(math.floor(value + 0.5))


def _qzone_channel_required_text(action: str, reason: Any = '') -> str:
    """空间动作**当前做不了**时的失败文案（`_qzone_run_action` 用）。

    必须让人看完就知道下一步做什么：说清这条能力靠谁（NapCat 取登录凭据 + QZone CGI）、
    别人为什么不行（这条动作在任何后端的 API 清单里都不存在，发给平台只会换回
    `retcode 1404 不支持的Api`）、**取凭据要哪两个接口 / 哪个版本**、以及当前卡在哪一步。
    `reason` 由 `qzone_cgi_auth` 逐接口逐域写好（"接口不存在（1404）"、"没登录"、
    "这个域没 p_skey" 三种现场一眼分得开）。
    """
    label = qzone_action_label(action)
    why = str(reason or '').strip() or 'NapCat 的取凭据通道本次没有走通'
    return (
        '「%s」需要 NapCat 通道（取登录凭据 → 打 QZone CGI），当前没走通：%s。'
        '这条动作不在任何后端的 API 清单里（NapCat 的空间动作只有发/删说说两条，'
        '`%s` 发给平台只会换回 retcode 1404 不支持的Api），所以只能靠这条通道。'
        '可执行的路：① 确认 NapCat 在线且已登录、版本够新——取凭据要 `%s` '
        '（AstrBot 参考插件 astrbot_plugin_qzone_tools v5.7.5 把这条要求写成'
        '「NapCat > 4.17.55」）；两个接口按这个顺序试，域按 %s 依次试，'
        '谁能给出 p_skey 就用谁；'
        '② 平台实例是 aiocqhttp/NapCat（其它 OneBot 实现给不出 cookie）；'
        '③ 都正常还是失败就看上面那句原因——它是平台/传输层给的原话。'
    ) % (label, why, action, ' / '.join(QZONE_COOKIE_APIS),
         ' / '.join(QZONE_COOKIE_DOMAINS))


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

    def _qzone_cgi_request(self) -> Any:
        """NapCat WS 方案的原始 HTTP 入口（`Transport.request_text`）；拿不到就 None。"""
        transport = getattr(self, 'transport', None)
        return getattr(transport, 'request_text', None)

    async def qzone_read(
        self, story: Any, kind: str, params: Any = None, *, include_self: bool = False,
    ) -> dict[str, Any]:
        """只读的空间动作（好友动态 / 某人说说）：**NapCat WebSocket 方案优先**。

        与 `qzone_execute` 的区别：只读动作**不占配额、不落 pending 行**（上游的限流门
        只管写），但仍然按账号端点解析——账号没登记就明确报错，绝不悄悄换账号。
        """
        payload = _mapping(params)
        runtime = self.qzone_runtime()
        call = self._qzone_call_onebot()
        request = self._qzone_cgi_request()
        if not callable(call):
            return {
                'ok': False,
                'error': _qzone_channel_required_text('get_qzone_feeds', '没有可用的 OneBot 连接'),
            }
        target = str(payload.get('targetUin') or payload.get('target_uin') or '').strip()
        count = payload.get('count')
        try:
            count_value = int(count) if count not in (None, '') else 10
        except (TypeError, ValueError):
            count_value = 10
        cgi_action = 'feed' if kind == 'feed' else 'moods'
        cgi_params: dict[str, Any] = {'count': max(1, min(50, count_value)), 'page': 1}
        if target:
            cgi_params['targetUin'] = target
        if kind == 'feed':
            rows = await self._qzone_run_action(call, 'get_qzone_feeds', cgi_params, cgi_action)
            items = _as_list(rows.get('feeds') if isinstance(rows, dict) else rows)
            return {'ok': True, 'error': '', 'feeds': items, 'count': len(items)}
        if not target and not include_self:
            return {'ok': False, 'error': '读取某人的说说需要 target_uin（留空时只表示"她自己"）。'}
        rows = await self._qzone_run_action(call, 'get_qzone_msg_list', cgi_params, cgi_action)
        items = _as_list(rows.get('posts') if isinstance(rows, dict) else rows)
        return {'ok': True, 'error': '', 'posts': items, 'count': len(items)}

    async def _qzone_run_action(
        self, call: Any, action: str, params: dict[str, Any], cgi_action: str = '',
    ) -> Any:
        """执行一次空间动作：**只有一条路**——NapCat 的 `get_cookies` + QZone CGI。

        参考实现 `Eganchiyu/qzone-sdk` 的 NapCat 认证：AstrBot 本来就用 WebSocket 连着
        NapCat，所以 `get_cookies` 直通即可，**不需要额外依赖**。

        `action` 是**本插件内部的动作键**（`QZONE_CGI_BY_ID`），不是任何后端的 action 名：
        这条动作在平台的 API 清单里根本不存在，所以**没有"回退平台动作"这条路**。
        拿不到 cookie / 传输层没有原始 HTTP 能力时直接以一句能照着做的话失败
        （`QzoneCgiUnavailable` + `_qzone_channel_required_text`），绝不把不存在的 API
        打出去换一个 `retcode 1404 不支持的Api`。

        历史：v1.7.1–v1.7.9 这里挂着一条"回退上游 Koishi QQ 空间适配器扩展动作"的分支，
        在 AstrBot 上那个后端根本不存在——真机表现就是每轮好友动态扫描都以
        `retcode 1404 不支持的Api get_qzone_feeds` + "结果未知，请勿自动重试"告终
        （用户贴日志点名）。v1.7.10 整条分支删除。

        `action` 不在 `QZONE_CGI_BY_ID` 里时（例如标准 OneBot 的 `send_msg`）**不进**
        这条通道：那不是空间动作，照旧交给 `call_qzone_action` 打平台。
        """
        cgi_action = cgi_action or QZONE_CGI_BY_ID.get(action, '')
        if not cgi_action:
            return await call_qzone_action(call, action, params)
        request = self._qzone_cgi_request()
        if not callable(request):
            raise QzoneCgiUnavailable(
                _qzone_channel_required_text(action, '传输层没有原始 HTTP 能力（request_text）'),
                action, None, False,
            )
        try:
            data = await call_qzone_cgi(request, call, cgi_action, params)
        except QzoneCgiUnavailable as error:
            # 没 cookie / 没接上：把原因包进"下一步做什么"的文案再上抛。**不回退**——
            # 没有任何后端认识这个动作名（回退只会换 1404），而且写动作更不能重发。
            raise QzoneCgiUnavailable(
                _qzone_channel_required_text(action, error), action, error.retcode, False,
            ) from error
        self.report_standalone('debug', 'QQ 空间动作走 NapCat QZone CGI 动作=%s', cgi_action)
        return data

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
        串行队列内**原子预留配额**）→ 空间动作（网络调用留在队列外）→ 回写
        `confirmed` / `failed` / `unknown`。

        传输类异常与"成功帧但无 tid"都记 `unknown`——结果不明按已发生保守计入
        配额，且绝不自动重试非幂等动作。
        """
        # 复制一份：可见性那一档要在下面把"五档中文标签"解析成 `ugcRight` 再落审计行，
        # 改调用方传进来的 Mapping 不是本函数该做的事。
        payload = dict(_mapping(input))
        runtime = self.qzone_runtime()
        if not runtime.get('enabled'):
            return {'ok': False, 'tid': '', 'error': 'QQ 空间通道未启用（「幕间控制台 → 配置 → QQ 空间」）。'}
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
                'error': '没有可用的 OneBot 连接（账号 %s 不在线）；不会改用其他账号执行空间动作。' % account_self_id,
            }
        now = self.now()
        story_id = str(pick(story, 'id') or '')

        if kind == 'visibility':
            # 五档中文标签 → `ugc_right`（值域与 NapCat 的 `ValidUgcRights` 同表）。
            # 解析放在 reserve 之前：参数不合法就**不该**留下一条审计行。
            label = str(payload.get('visible') or '').strip()
            right = qzone_visible_value(label)
            if right is None:
                return {
                    'ok': False, 'tid': str(payload.get('tid') or ''),
                    'error': '可见范围只能是这五档之一：%s。' % ' / '.join(QZONE_VISIBILITY_VALUES),
                }
            payload['visible'] = label
            payload['ugcRight'] = right
            uins = [str(item).strip() for item in _rows(payload.get('targetUins')) if str(item).strip()]
            payload['targetUins'] = uins
            if right in (16, 128) and not uins:
                return {
                    'ok': False, 'tid': str(payload.get('tid') or ''),
                    'error': '「%s」必须带上 target_uins（这档可见性作用在哪些 QQ 上）。' % label,
                }

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
            if kind == 'visibility' and payload.get('ugcRight'):
                # v1.7.5：审计行要记下"改成了哪一档"（上游没有这个 kind）。
                data['ugcRight'] = payload.get('ugcRight')
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
            if kind == 'visibility':
                return await self._qzone_apply_visibility(
                    story, story_id, payload, pending_id, now, account_self_id,
                )
            if kind == 'comment':
                params: dict[str, Any] = {
                    'tid': payload.get('tid') or '',
                    'content': payload.get('content') or '',
                }
                target_uin = _target_uin_param(payload.get('targetUin'))
                if target_uin is not None:
                    params['targetUin'] = target_uin
                await self._qzone_run_action(call, 'comment_qzone', params, 'comment')
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
            if kind == 'forward':
                forward_params: dict[str, Any] = {'tid': payload.get('tid') or ''}
                if payload.get('content'):
                    forward_params['content'] = payload.get('content')
                forward_target = _target_uin_param(payload.get('targetUin'))
                if forward_target is not None:
                    forward_params['targetUin'] = forward_target
                await self._qzone_run_action(call, 'forward_qzone', forward_params, 'forward')
                await self._qzone_set_status(pending_id, {
                    'status': 'confirmed', 'postedAt': self.now(),
                })
                await self.append_entry(story_id, {
                    'kind': 'system', 'actor': 'character',
                    'content': '[空间动态] 她转发了一条说说%s' % (
                        '：%s' % clip(payload.get('content'), QZONE_COMMENT_SUMMARY_CHARS)
                        if payload.get('content') else '',
                    ),
                    'occurredAt': iso(now),
                    'metadata': {'qzone_kind': 'forward', 'tid': payload.get('tid') or ''},
                }, now)
                self.report_operation(
                    'standard', 'info', story, 'user-message',
                    'QQ 空间转发已发出 tid=%s', payload.get('tid') or '',
                )
                return {'ok': True, 'tid': payload.get('tid') or '', 'error': ''}
            params = {'tid': payload.get('tid') or ''}
            target_uin = _target_uin_param(payload.get('targetUin'))
            if target_uin is not None:
                params['targetUin'] = target_uin
            await self._qzone_run_action(call, 'like_qzone', params, 'like')
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
            # `failed`——`unknown` 保守计入配额（**可能已发生**），语义上禁止自动重试。
            # 这一条是**所有写路径共用**的收口（发帖 / 评论 / 点赞 / 转发 / 改可见范围
            # 都从这里出去），所以漏标的可能只出在"抛出来的异常是不是
            # `QzoneActionError`"这一层——`call_qzone_action`（平台原生动作）与
            # `call_qzone_cgi`（NapCat WS）两条通道都已经按同一口径打标。
            ambiguous = isinstance(error, QzoneActionError) and error.ambiguous
            note = '（结果未知：请求可能已生效，为避免重复不会自动重试。）' if ambiguous else ''
            await self._qzone_set_status(pending_id, {
                'status': 'unknown' if ambiguous else 'failed',
                # 审计行本身也要写明"可能已发生"：光看 status 字面看不出它的分量。
                'error': (message + note)[:QZONE_ERROR_MAX_CHARS],
            })
            self.report_standalone(
                'warn', 'QQ 空间动作%s 类型=%s 无法确认是否生效 错误=%s',
                '结果未知' if ambiguous else '失败', kind, message,
            )
            return {'ok': False, 'tid': '', 'error': message + note}

    async def _qzone_apply_visibility(
        self,
        story: Any,
        story_id: str,
        payload: Mapping[str, Any],
        pending_id: Any,
        now: Any,
        account_self_id: str,
    ) -> dict[str, Any]:
        """改一条**自己发的**说说的可见范围（v1.7.5，本移植版新增的动作）。

        **只发可见性 + 既有字段，富文本字段照参考实现传空串**（v1.7.9 的 H1 裁定）。

        参考实现的 `qzone_api/api/api_parms.py::build_edit_message_params` 就是**专门改可见
        范围**的构造器（docstring：「tid 为说说 id；ugcright_id 取自说说列表里该条的
        ``ugcright_id``」），而它的 `pic_template` / `richtype` / `richval` / `subrichtype` /
        `special_url` **全是空串**——即"**空串 = 不改动富文本**"，服务端只更新给到的那几个
        字段。v1.7.7 / v1.7.8 那套"下载原图 → 逐张重新上传 → 用新回执拼 `richval`"是建立在
        "`emotion_cgi_update` 按整条重建"这个**推断**上的，它**有副作用**（腾讯侧变成新副本、
        原图 URL 换掉、混排的视频会丢），已整条删除。证据原文与裁定过程见
        `docs/PORTING_NOTES.md` §43。

        步骤：

        1. 用 `moods`（说说列表）按 `tid` 找回这条说说——正文要原样带回去（`con` 是参考实现
           里**必带**的字段，空正文等于把正文清掉，所以读不回来就拒绝）；
        2. **转发的说说继续拒绝**（`rt_tid` 非空）：编辑的字段清单里**没有** `rt_con` /
           `rt_tid` 这一组，转发目标还原不了，宁可不做（这条与本轮的 H1 无关，是"我们手上
           根本没有重建转发所需的字段"）；
        3. 过**跳闸门**（见下）与正文非空检查，再发 update；
        4. update 成功后**立刻回读这条说说**，比对附件（图片数 / 视频数）与正文——这是
           **观测**，不是猜测：H1 成不成立，每改一次就当场验一次。

        **跳闸（`QZONE_VISIBILITY_GUARD_FILE`）**：一旦回读发现附件变少，就落一个持久标志，
        此后带附件的说说一律拒绝改可见范围（纯文字不受影响），直到用户删掉那个文件——
        "绝不允许静默毁第二条说说"。上次的 tid / 时间 / 解除办法都写在日志与拒绝理由里。

        这条动作**只有** QZone CGI 通道能做（任何后端的 API 清单里都没有它），
        拿不到 cookie 就明确失败，**不**回落平台。

        成功会：写剧本条目（她自己记得改了谁能看）+ 审计行 `confirmed` + 一条标准日志。
        """
        tid = str(payload.get('tid') or '')
        right = payload.get('ugcRight')
        label = str(payload.get('visible') or qzone_visibility_label(right))
        target_uins = _rows(payload.get('targetUins'))

        async def fail(reason: str) -> dict[str, Any]:
            """明确的失败：写审计行 + 标准 warn（别让"改了没生效"变成悬案）。"""
            await self._qzone_set_status(pending_id, {'status': 'failed', 'error': reason})
            self.report_standalone('warn', 'QQ 空间改可见范围失败 tid=%s 原因=%s', tid, reason)
            return {'ok': False, 'tid': tid, 'error': reason}

        request = self._qzone_cgi_request()
        if not callable(request):
            return await fail(
                '改一条说说的可见范围只能走 NapCat WebSocket 通道（QZone 的 '
                'emotion_cgi_update），当前传输层没有这个能力。'
            )
        # ① 找回这条说说（列表接口按 tid 精确命中；找不到就不敢改）。
        lookup = await self.qzone_read(
            story, 'moods', {'targetUin': account_self_id, 'count': QZONE_VISIBILITY_LOOKUP_COUNT},
        )
        if not (isinstance(lookup, Mapping) and lookup.get('ok')):
            # 读不到正文与"这条说说不在最近 N 条里"是两件事：前者说清真实原因。
            return await fail('读回这条说说的正文失败：%s' % (
                (lookup.get('error') if isinstance(lookup, Mapping) else '') or '未知原因',
            ))
        post: Any = None
        for row in _rows(lookup.get('posts') if isinstance(lookup, Mapping) else None):
            if str(pick(row, 'tid') or '') == tid:
                post = row
                break
        if post is None:
            return await fail(
                '找不到这条说说的当前正文（只回看最近 %d 条，且必须是她自己发的）；'
                '不带上正文直接改会把正文清掉，所以这次不做。' % QZONE_VISIBILITY_LOOKUP_COUNT
            )
        if str(pick(post, 'rt_tid') or '').strip():
            return await fail(
                '这条是转发的说说：编辑的字段清单里没有转发目标（rt_con / rt_tid）那一组，'
                '转发没法还原，所以不做。'
            )
        before_pics = len(_rows(pick(post, 'pic')))
        before_videos = len(_rows(pick(post, 'video')))
        # 跳闸门：上一次实测到"改完附件变少"之后，带附件的说说一律不碰（纯文字照常）。
        guard = self._qzone_visibility_guard_reason()
        if guard and (before_pics or before_videos):
            return await fail(
                '这条说说带 %d 个附件（图片 %d / 视频 %d），而**上一次**改可见范围时实测到附件'
                '被删——在弄清原因前，带附件的说说一律不改（纯文字说说不受影响）。%s'
                % (before_pics + before_videos, before_pics, before_videos, guard)
            )
        content = str(pick(post, 'content') or '')
        if not content.strip():
            return await fail('这条说说读回来的正文是空的，不敢拿空正文去改（会把正文清掉）。')
        # ② 改可见范围（NapCat WS 通道；只发可见性 + 既有字段，富文本字段照参考实现留空）。
        #    `QzoneCgiUnavailable` / CGI 失败由外层统一记 failed / unknown——"请求可能已
        #    到达"不自动重试。
        await call_qzone_cgi(
            request, self._qzone_call_onebot(), 'update_visibility',
            {'tid': tid, 'content': content, 'ugcRight': right, 'targetUins': target_uins},
        )
        self.report_standalone('debug', 'QQ 空间动作走 NapCat WS 通道 动作=update_visibility')
        await self._qzone_set_status(pending_id, {
            'tid': tid, 'status': 'confirmed', 'postedAt': self.now(),
        })
        # ③ 回读校验（**观测**，不是猜测）：H1 说"留空富文本字段 = 附件与正文原样保留"，
        #    那就每改一次当场验一次；不成立时把代价写进日志与剧本条目，并落下跳闸标志。
        try:
            readback, note = await self._qzone_visibility_readback(
                story, account_self_id, tid, (before_pics, before_videos, content),
            )
        except Exception as error:  # noqa: BLE001 - 校验自己崩了不该影响"改已成功"这件事
            readback, note = 'unverified', '回读校验自身出错：%s' % error
        if readback == 'lost':
            # 按坑 25 的口径走 warn：这是**丢内容**，必须让用户看得见。
            self.report_standalone(
                'warn',
                'QQ 空间改可见范围的**回读校验不通过**：%s（tid=%s）。'
                '「富文本字段留空 = 不改动图片 / 视频」这个前提在这条说说不成立，'
                '请检查这条说说的配图是否被删。%s',
                note, tid, self._qzone_visibility_trip_guard(tid, now, note),
            )
        elif readback == 'changed':
            self.report_standalone(
                'warn',
                'QQ 空间改可见范围的**回读校验不通过**：%s（tid=%s）。'
                '请检查这条说说 tid=%s。',
                note, tid, tid,
            )
        elif readback == 'unverified':
            self.report_standalone(
                'warn',
                'QQ 空间改可见范围已完成，但**回读校验没做成**（%s），无法确认附件还在不在：'
                '请自行看一眼这条说说 tid=%s。',
                note, tid,
            )
        else:
            self.report_standalone(
                'debug',
                'QQ 空间改可见范围回读校验通过 tid=%s（改前附件 %d 图 / %d 视频，H1 当场成立）',
                tid, before_pics, before_videos,
            )
        # 改的是"谁能看见"，对这段关系是有意义的事，所以进剧本（评论/点赞那种过细的才不进）。
        metadata: dict[str, Any] = {'qzone_kind': 'visibility', 'tid': tid, 'ugc_right': right}
        if readback == 'lost':
            # 事后追溯用：这条说说的附件在改可见范围时变少了（H1 不成立的那一次）。
            metadata['qzone_visibility_attachment_loss'] = True
        await self.append_entry(story_id, {
            'kind': 'system', 'actor': 'character',
            'content': '[空间动态] 她把说说的可见范围改成了「%s」：%s' % (
                label, clip(content, QZONE_POST_SUMMARY_CHARS),
            ),
            'occurredAt': iso(now),
            'metadata': metadata,
        }, now)
        self.report_operation(
            'standard', 'info', story, 'user-message',
            'QQ 空间说说可见范围已改 tid=%s 可见性=%s（%s）', tid, right, label,
        )
        return {'ok': True, 'tid': tid, 'error': ''}

    async def _qzone_visibility_readback(
        self, story: Any, account_self_id: str, tid: str, before: tuple[int, int, str],
    ) -> tuple[str, str]:
        """update 之后**回读这条说说**，比对附件与正文（v1.7.9 的观测点）。

        `before` 是改之前的 `(图片数, 视频数, 正文)`。返回 `(状态, 说明)`：

        * `ok`——附件与正文都对得上（H1 当场被证实）；
        * `lost`——**附件变少**（图片或视频少了）：这是事故，调用方据此告警 + 跳闸；
        * `changed`——附件没少但正文对不上（同样要告警，但不属于"附件被删"那条跳闸规则）；
        * `unverified`——回读本身没成（读失败 / 最近 N 条里找不到它）：**不改判** update 的成败，
          只说明这次没验成，调用方打一条 warn 让用户自己看一眼。

        **为什么要有它**：「空富文本字段 = 不改动附件」来自参考实现的参数清单，是**推断**而非
        抓包；回读把它变成"每改一次就实测一次"。附件的口径 = 图片（`pic`）+ 视频（`video`）
        ——视频不在正文里，只在 `parse_mood` 的 `video` 里，所以两个都要数。
        """
        before_pics, before_videos, before_content = before
        try:
            lookup = await self.qzone_read(
                story, 'moods',
                {'targetUin': account_self_id, 'count': QZONE_VISIBILITY_LOOKUP_COUNT},
            )
        except Exception as error:  # noqa: BLE001 - 读不到 ≠ 改失败
            return 'unverified', '回读失败：%s' % error
        if not (isinstance(lookup, Mapping) and lookup.get('ok')):
            return 'unverified', '回读失败：%s' % (
                (lookup.get('error') if isinstance(lookup, Mapping) else '') or '未知原因'
            )
        post: Any = None
        for row in _rows(lookup.get('posts') if isinstance(lookup, Mapping) else None):
            if str(pick(row, 'tid') or '') == tid:
                post = row
                break
        if post is None:
            return 'unverified', '回读时在最近 %d 条里没找到这条说说' % QZONE_VISIBILITY_LOOKUP_COUNT
        after_pics = len(_rows(pick(post, 'pic')))
        after_videos = len(_rows(pick(post, 'video')))
        after_content = str(pick(post, 'content') or '')
        problems: list[str] = []
        if after_pics < before_pics:
            problems.append('配图从 %d 张变成 %d 张' % (before_pics, after_pics))
        if after_videos < before_videos:
            problems.append('视频从 %d 个变成 %d 个' % (before_videos, after_videos))
        if after_content.strip() != before_content.strip():
            problems.append('正文对不上（改前 %d 字 → 改后 %d 字）' % (
                len(before_content.strip()), len(after_content.strip()),
            ))
        if not problems:
            return 'ok', ''
        if after_pics < before_pics or after_videos < before_videos:
            return 'lost', '；'.join(problems)
        return 'changed', '；'.join(problems)

    def _qzone_visibility_guard_path(self) -> Optional[Path]:
        """跳闸标志文件（`<数据目录>/qzone_visibility_guard.json`）的路径。

        数据目录按**生产上真的挂着的那一个**取：`ServiceBase.__init__` 存的是 `self.ctx`
        （`chunk2` 读表情库目录就是这么取的 `getattr(self.ctx, 'base_dir', '')`），
        `self.context` 在生产里**根本不存在**（那是适配层 `AstrbotInterludeContext` 自己的
        属性，指的是宿主 Context，core 拿不到）。顺序：`ctx.base_dir` → 宿主注入的
        `context.base_dir`（单测的裸宿主常这么塞）→ 自己的 `base_dir`。

        一个数据目录都拿不到就回 `None` = 跳闸机制不生效——写不下去的时候，宁可照常做
        也不能凭空把动作锁死。
        """
        for holder in (getattr(self, 'ctx', None), getattr(self, 'context', None), None):
            base = getattr(self, 'base_dir', '') if holder is None else getattr(holder, 'base_dir', '')
            if base:
                return Path(str(base)) / QZONE_VISIBILITY_GUARD_FILE
        return None

    def _qzone_visibility_guard_reason(self) -> str:
        """跳闸是否生效：生效就回"给用户看的上一回事故 + 解除办法"，否则回空串。

        判定只看一个字段：文件里的 `attachmentLoss` 为真。**文件不存在 / 空文件 / 坏 JSON
        一律当"没跳闸"**——用户的解除办法就是删掉或清空它，不能因为一个手改坏的文件把
        整条动作永久锁死（坏 JSON 会留一条 warn 说明"这次按没跳闸处理"）。
        """
        path = self._qzone_visibility_guard_path()
        if path is None:
            return ''
        try:
            raw = path.read_text(encoding='utf-8')
        except FileNotFoundError:
            return ''
        except Exception as error:  # noqa: BLE001 - 读不到就当没跳闸，但要留痕
            self.report_standalone(
                'warn', '改可见范围的跳闸标志读取失败（本次按未跳闸处理）：%s', error,
            )
            return ''
        data: Any = {}
        if raw.strip():
            try:
                data = json.loads(raw)
            except Exception as error:  # noqa: BLE001
                self.report_standalone(
                    'warn', '改可见范围的跳闸标志不是合法 JSON（本次按未跳闸处理）：%s', error,
                )
                return ''
        if not (isinstance(data, Mapping) and data.get('attachmentLoss')):
            return ''
        return (
            '上次在 tid=%s 上实测到附件被删（%s）；要恢复对带附件说说的操作，'
            '删掉文件 %s 即可' % (
                data.get('tid') or '未知', data.get('detectedAt') or '时间未知', path,
            )
        )

    def _qzone_visibility_trip_guard(self, tid: str, now: Any, note: str) -> str:
        """落下跳闸标志（只在**实测到附件变少**时调）。回一句给日志看的话（写没写成都说清）。"""
        path = self._qzone_visibility_guard_path()
        if path is None:
            return ''
        payload = {
            'attachmentLoss': True, 'tid': tid, 'detectedAt': iso(now), 'detail': note,
        }
        try:
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        except Exception as error:  # noqa: BLE001 - 写不下去也必须让用户知道，别静默
            self.report_standalone('warn', '改可见范围的跳闸标志写不下去（%s）：%s', path, error)
            return ''
        return '已跳闸：以后带附件的说说一律拒绝改可见范围（恢复：删掉 %s）' % path

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
    # 被评论 / 被点赞感知（rc29 + rc33）
    # ------------------------------------------------------------------ #

    def _qzone_reaction_line(self, delta: Any) -> str:
        """一条增量 → `[空间动态]` 正文（措辞判据只在这里一处）。"""
        excerpt = str(pick(delta, 'content_excerpt', 'contentExcerpt') or '')
        head = '[空间动态] 她的说说%s' % ('「%s」' % excerpt if excerpt else '')
        parts: list[str] = []
        current = pick(delta, 'current')
        previous = pick(delta, 'previous')
        if current is not None and previous is not None:
            parts.append('收到了 %d 条新评论（累计 %d 条）' % (int(current) - int(previous), int(current)))
        like_current = pick(delta, 'like_current', 'likeCurrent')
        like_previous = pick(delta, 'like_previous', 'likePrevious')
        if like_current is not None and like_previous is not None:
            parts.append('收到了 %d 个新赞（累计 %d 个）' % (
                int(like_current) - int(like_previous), int(like_current),
            ))
        return '%s%s' % (head, '、'.join(parts))

    def _note_qzone_like_unavailable(self, story: Any) -> None:
        """**可见降级**：这条读通道的说说回执里没有点赞数。

        上游 `qzoneReactionDeltas` 的注释逐字写着「赞数上游（SnowLuma mapMsgList /
        RawEmotion）尚未暴露字段……此处只算评论」；本移植版读的是原始 QZone CGI
        （`emotion_cgi_msglist_v6`），字段由腾讯决定，同样可能没有。拿不到就是
        **能力不可知**——绝不按 0 处理（那会凭空造出"从 0 涨到 N"的幽灵点赞），
        也不能静默（用户会以为"被赞了她就会知道"）。节流一条，写清下一步。
        """
        self.note_access_skip(
            'qzone-like-count-unavailable', QZONE_REACTION_LIKE_NOTE_INTERVAL_MS,
            'QQ 空间的说说回执里没有「点赞数」（%s）：这条通道**只能感知评论增量**，'
            '点赞增量不可知，不会写进剧本（上游同样未暴露该字段，不是这台机器的问题）。'
            '下一步：确认 QZone 读通道（emotion_cgi_msglist_v6）的回执里是否带 '
            'likecount / like_num；有就照常感知，没有就继续保持"不可知"，**不要**'
            '把缺失当 0。',
            pick(story, 'id') or '',
        )

    async def qzone_reaction_sweep(
        self, story: Any, call: Any, address: Any, now: Any,
    ) -> None:
        """被评论 / 被点赞感知（上游 `qzoneReactionSweep`，`src/service.ts:7432`）。

        **感知零动作配额**：只读 `get_qzone_msg_list`，不占 `daily_*_cap`、不进
        `interlude_qzone_post` 的动作行计数；产出的条目是 `[空间动态]`（SOCIAL SURFACE
        规则现成，零提示词改动），由下一次推进（自动或对话）自然携带——**轮询本身
        绝不调度推进**（被赞不立即开 advance）。

        三条上游语义逐条照抄（`src/service.ts:7456-7496`）：

        1. **无增量的帖子照常推进基线**（含 `commentNum` 为 `None` 的首次观测初始化）
           ——跳过会让这条说说的增量感知**永久失效**；
        2. **单轮预算 3 条**，之外的增量**不推进基线**（下轮重新发现），杜绝
           "第 4 条起永久丢失"；
        3. **先推进该帖基线、再写感知条目**；基线回写失败则**本轮中止**（不写条目），
           杜绝"旧基线重算出相同增量"的重复入账。条目写入失败时基线**不回滚**
           （该增量让位，避免重复入账），只留 warn。

        只读拉取失败 → 基线不动、下轮重试，但**必须可见**（此前零日志）。
        """
        story_id = str(pick(story, 'id') or '')
        rows = await self.db_get('interlude_qzone_post', {
            'storyId': story_id, 'kind': 'post', 'status': 'confirmed',
        })
        cutoff = dt_ms(now) - QZONE_REACTION_WINDOW_DAYS * 24 * 60 * 60 * 1000
        tracked = [
            row for row in _rows(rows)
            if str(pick(row, 'tid') or '').strip()
            and (_row_ms(row) is None or _row_ms(row) >= cutoff)
        ]
        if not tracked:
            return
        self_id = pick(address, 'selfId', 'self_id')
        target_uin, reason = _qzone_target_uin(self_id)
        if target_uin is None:
            # 多通道账号标识校验（rc33）：认不出数字 QQ 号就**不查**——省掉 target_uin
            # 会让 CGI 按"我自己"返回列表，那就是认错账号（拿她的帖子去比对别人的评论）。
            self.note_access_skip(
                'qzone-reaction-account-not-qq', QZONE_REACTION_LIKE_NOTE_INTERVAL_MS,
                'QQ 空间被评论感知跳过：当前轮询账号（%s）%s——空间读通道只能按数字 QQ 号'
                '定位，拿不到就会去查"我自己"的列表（认错账号）。下一步：给这条通道'
                '配上该故事的数字 QQ 角色账号（端点注册表里的 selfId），或关掉 QQ 空间。',
                self_id if self_id not in (None, '') else '(空)',
                '不是数字 QQ 号' if reason == 'non-qq' else '没有账号标识',
            )
            return
        try:
            raw = _mapping(await self._qzone_run_action(call, 'get_qzone_msg_list', {
                'targetUin': target_uin,
                'count': QZONE_REACTION_MSG_NUM,
            }, 'moods'))
        except Exception as error:  # noqa: BLE001 - 拉取失败：基线不动、下轮重试，但必须可见
            self.report('warn', story, 'advance', '被评论列表拉取失败，本轮感知跳过 错误=%s', error)
            return
        entries = [
            entry for entry in (
                normalize_qzone_msg_entry(item)
                for item in _rows(pick(raw, 'msglist', 'posts'))
            ) if entry
        ]
        result = qzone_reaction_deltas(tracked, entries)
        deltas = _rows(result.get('deltas'))
        baselines = _rows(result.get('baselines'))
        if baselines and all(
            pick(item, 'like_num', 'likeNum') is None for item in baselines
        ):
            # 通道没给点赞数 = 能力缺失：可见降级，绝不静默、也不按 0 比增量。
            self._note_qzone_like_unavailable(story)
        # ① 无增量的帖子照常推进基线（含首次观测初始化）；有增量的留给下面逐条提交。
        delta_tids = {str(pick(item, 'tid') or '') for item in deltas}
        for baseline in baselines:
            tid = str(pick(baseline, 'tid') or '')
            if tid in delta_tids:
                continue
            row = next((item for item in tracked if str(pick(item, 'tid') or '') == tid), None)
            row_id = pick(row, 'id') if row is not None else None
            patch: dict[str, Any] = {'commentNum': pick(baseline, 'comment_num', 'commentNum')}
            if pick(baseline, 'like_num', 'likeNum') is not None:
                patch['likeNum'] = pick(baseline, 'like_num', 'likeNum')
            if row_id is None or _same_reaction_baseline(row, patch):
                continue
            try:
                await self.db_set('interlude_qzone_post', {'id': row_id}, patch)
            except Exception as error:  # noqa: BLE001 - 基线初始化失败不影响本轮感知
                self.report(
                    'warn', story, 'advance',
                    '被评论基线初始化失败（不影响本轮感知） tid=%s 错误=%s', tid, error,
                )
        # ② 逐条提交：先推进该帖基线再写条目；预算 3；基线回写失败则本轮中止。
        accounted = 0
        accounted_new = 0
        for delta in deltas:
            if accounted >= QZONE_REACTION_BUDGET:
                break
            tid = str(pick(delta, 'tid') or '')
            baseline = next((item for item in baselines if str(pick(item, 'tid') or '') == tid), None)
            row = next((item for item in tracked if str(pick(item, 'tid') or '') == tid), None)
            row_id = pick(row, 'id') if row is not None else None
            if baseline is None or row_id is None:
                continue
            patch = {'commentNum': pick(baseline, 'comment_num', 'commentNum')}
            if pick(baseline, 'like_num', 'likeNum') is not None:
                patch['likeNum'] = pick(baseline, 'like_num', 'likeNum')
            try:
                await self.db_set('interlude_qzone_post', {'id': row_id}, patch)
            except Exception as error:  # noqa: BLE001 - 基线写不进去：本轮中止，别重复入账
                self.report(
                    'warn', story, 'advance',
                    '被评论基线回写失败，本轮感知中止（下轮重算） tid=%s 错误=%s', tid, error,
                )
                break
            metadata: dict[str, Any] = {
                'qzone_tid': tid,
                'qzone_reactions': {
                    'previous': pick(delta, 'previous'),
                    'current': pick(delta, 'current'),
                    'like_previous': pick(delta, 'like_previous'),
                    'like_current': pick(delta, 'like_current'),
                },
            }
            try:
                await self.append_entry(story_id, {
                    'kind': 'friend-feed', 'actor': 'system',
                    'content': self._qzone_reaction_line(delta),
                    # `occurredAt` = **她这轮知道的时刻**（与好友动态入账同一条判据，§55）。
                    'occurredAt': iso(now),
                    'metadata': metadata,
                }, now)
                accounted += 1
                accounted_new += max(0, int(pick(delta, 'current') or 0) - int(pick(delta, 'previous') or 0))
            except Exception as error:  # noqa: BLE001 - 基线已推进：该增量让位，只留日志
                self.report(
                    'warn', story, 'advance',
                    '被评论感知条目写入失败，该增量让位 tid=%s 错误=%s', tid, error,
                )
        if deltas:
            self.report_operation(
                'diagnostic', 'debug', story, 'advance',
                '被评论感知已入账 新评论=%d 入账帖数=%d/%d（预算 %d）',
                accounted_new, accounted, len(deltas), QZONE_REACTION_BUDGET,
            )

    # ------------------------------------------------------------------ #
    # 好友动态轮询
    # ------------------------------------------------------------------ #

    def qzone_feed_poll_minutes(self) -> int:
        """轮询间隔（上游 `applyConfig`）：`min(60, max(15, round(feedWindow/2)))` 分钟。"""
        window = int(self.qzone_runtime().get('feed_window_minutes') or 120)
        return min(60, max(15, _js_round(window / 2.0)))

    async def _qzone_feed_media_lines(self, story: Any, facts: Any, text: str) -> list[str]:
        """一条动态里图片 / 视频的识别（**失败绝不阻断入账**）。

        返回接在该条观察后面的那几行（可数线索 + 事实 + 观察）；一行都没有就回空表。
        判据全在既有处，这里一份都不重算：

        * **要不要做**：`qzone_feed_media_capability()`（本文件唯一一处能力判据）；
        * **取几张 / 几段**：`qzone.feed_image_cap` / `qzone.feed_video_cap` 与既有的
          **每回合视觉预算**（`chunk3._image_budget()`）取**小**——两道闸谁拦下的
          都点名在线索与 warn 里（"只调了一个键却还是 N 张"必须一眼看得出）。
          **本条路不读 `forward_message`**（合并转发的键，与动态无关，v1.9.6 解开）；
        * **怎么取**：图片走既有视觉通道（`load_native_images` → `describe_current_images`，
          下载 / 白名单 / 感知哈希去重 / 降采样都是它那一套）；视频走既有视频链
          （`collect_video_sources`：抽帧 + 音轨、模式与降级都由它判），抽出的帧再进
          同一条视觉通道。**不另造媒体通道**。

        `text` 是给识图模型的上下文（这条动态的正文），与回合里那句
        `describe_current_images(story, images, user_message)` 同一个参数位。
        """
        images = [str(item).strip() for item in _rows(facts.get('images')) if str(item or '').strip()]
        videos = [str(item).strip() for item in _rows(facts.get('videos')) if str(item or '').strip()]
        if not images and not videos:
            return []
        capability = qzone_feed_media_capability(self)
        runtime = self.qzone_runtime()
        lines: list[str] = []
        if images:
            lines.extend(
                await self._qzone_feed_image_lines(
                    story, images, text, capability, int(runtime.get('feed_image_cap') or 1),
                )
            )
        if videos:
            lines.extend(
                await self._qzone_feed_video_lines(
                    story, videos, text, capability, int(runtime.get('feed_video_cap') or 1),
                )
            )
        return lines

    async def _qzone_feed_image_lines(
        self, story: Any, images: list[str], text: str, capability: Mapping[str, Any], cap: int,
    ) -> list[str]:
        """动态里的图片：进既有视觉通道，或者如实标注"没识别"。"""
        if not capability.get('images'):
            # 能力关着 = 这不是故障（不刷 warn），但"有几张、没看"必须留下来
            # （与 `[视频×K，未取]` 同一族的可数事实）。
            if capability.get('image_reason') and pick(_vision_config(self), 'enabled') is True:
                # **开着却没配识图模型**：这是配置缺口，必须可见 + 可行动（坑 25）。
                _note_qzone_feed_media(
                    self, 'qzone-feed-image-no-vision',
                    '好友动态里有图片，但%s，这次没有识别：这条动态照常入账，'
                    '剧本里只有"有几张图"这条事实。',
                    capability.get('image_reason'),
                )
            return ['[图片×%d，未识别]' % len(images)]
        budget = _image_budget(self)
        taken = max(1, min(int(cap), int(budget)))
        sources = images[:taken]
        loaded = await self.load_native_images(story, sources, None, None)
        observations = await self.describe_current_images(story, loaded, text) or []
        lines = [
            '[图片观察] %s' % str(item).strip()
            for item in _rows(observations) if str(item or '').strip()
        ]
        if sources and not loaded:
            # **一张都没取回来**：能力开着、也调了模型，却没有任何画面可看。这件事
            # 必须可见（坑 25），而且要说清"下一次该看哪儿"——取图域名不在白名单 /
            # 直链过期 / 下载失败，三种现场看这句 + 宿主的取图日志分得开。
            host = urlsplit(str(sources[0])).hostname
            _note_qzone_feed_media(
                self, 'qzone-feed-image-unreadable',
                '好友动态的图片没取回来（%d 张）：这条动态照常入账，这次没有画面'
                '（图片来源 %s）。',
                len(sources), host or str(sources[0])[:80],
            )
            lines.insert(0, '[图片×%d，未识别]' % len(images))
        if len(images) > len(sources):
            note = qzone_media_limit_note('image', len(images), len(sources))
            if note:
                lines.append(note)
                _note_qzone_media_limit(
                    self, 'qzone-feed-image-limit', 'image',
                    len(images), len(sources), 'cap' if int(cap) <= int(budget) else 'budget',
                )
        return lines

    async def _qzone_feed_video_lines(
        self, story: Any, videos: list[str], text: str, capability: Mapping[str, Any], cap: int,
    ) -> list[str]:
        """动态里的视频：走既有视频链（抽帧 + 音轨），帧再进同一条视觉通道。

        段数 = `min(qzone.feed_video_cap, 每回合视觉预算)`：第二道闸是
        `chunk3._image_budget()`（帧与图片共用同一条视觉通道），**不是**合并转发那个键
        ——`forward_message.max_videos` 一个字都不读（v1.9.6 解开的耦合）。
        """
        if not capability.get('videos'):
            return ['[视频×%d，未识别]' % len(videos)]
        budget = _image_budget(self)
        taken = max(1, min(int(cap), int(budget)))
        sources = videos[:taken]
        # 合成一份"入站媒体表"：合并转发里的视频正是这么进来的（`kind='video'` 的
        # `SessionView.media` 条目）——所以这条链的判据与私聊转发**逐字同一条**。
        # 缺 `is_direct` 按私聊处理（只有总开关生效），抽帧/音轨/降级全由链自己判。
        # `limit=taken` 让链按本组的上限截断：不给它的话链会拿 `forward_message.max_videos`
        # 再砍一刀，还会留一条点名转发键的 warn。
        media_session = {'media': [{'kind': 'video', 'source': url} for url in sources]}
        video_media = await collect_video_sources(self, story, media_session, limit=taken)
        lines: list[str] = []
        try:
            if video_media.note:
                lines.append(str(video_media.note).strip())
            frames = _rows(video_media.image_sources)
            if frames and capability.get('images'):
                loaded = await self.load_native_images(story, frames, None, None)
                observations = await self.describe_current_images(story, loaded, text) or []
                lines.extend(
                    '[视频观察] %s' % str(item).strip()
                    for item in _rows(observations) if str(item or '').strip()
                )
            elif frames:
                # 抽帧白抽了：视频理解开着、图片理解关着，帧**没有通道可去**
                # （与群回合那条 `GROUP_FRAMES_NO_CHANNEL_REASON` 同一条尺子：帧丢掉要
                # 说出来，不能让模型以为她看见了画面）。帧照旧丢掉，但留事实 + 一条可行动 warn。
                _note_qzone_feed_media(
                    self, 'qzone-feed-video-frames-no-vision',
                    '好友动态的视频抽好了 %d 帧，但%s，这次没有识别：帧没有交给任何模型'
                    '（抽帧本身照常完成）。',
                    len(frames), capability.get('image_reason') or '没有可用的识图模型',
                )
                lines.append('[图片×%d，未识别]' % len(frames))
        finally:
            # 帧字节已经读进内存（音轨是 data: URI，本处没有语音通道，不交给任何模型）。
            # 放 finally：那一跳抛异常也不留临时目录垃圾。
            video_media.cleanup()
        if len(videos) > len(sources):
            note = qzone_media_limit_note('video', len(videos), len(sources))
            if note:
                lines.append(note)
                _note_qzone_media_limit(
                    self, 'qzone-feed-video-limit', 'video',
                    len(videos), len(sources),
                    'cap' if int(cap) <= int(budget) else 'budget',
                )
        return lines

    async def qzone_feed_sweep(self) -> None:
        """好友动态轮询（上游 `qzoneFeedSweep`）：感知零模型调用——新鲜说说写成
        `[好友动态]` 条目，反应留给回合内决策。

        单飞锁 + 失败只 warn，**绝不上抛**；feeds 接口间歇失败就静默跳过，下轮再试。

        `qzone.auto_feed`（本移植版新增）没打开时**不轮询好友动态**——这个开关就是
        「允许她浏览好友动态」；这时按小时节流打一条可见说明，不然用户会以为
        "QQ 空间开了却什么都不发生"。

        ⚠️ **被评论 / 被点赞感知（`qzone_reaction_sweep`）不受 `auto_feed` 约束**：
        它读的是**她自己发出去的说说**上的互动，不是"浏览别人动态"。关掉自动浏览
        不代表"别人评论她、她不该知道"，所以那一段在这道闸**之前**跑（仍在同一个
        单飞锁与"通道可用"判据内）。
        """
        runtime = self.qzone_runtime()
        if (
            getattr(self, 'desktop_runtime_phase', 'running') == 'paused'
            or getattr(self, 'database_resetting', False)
            or not runtime.get('enabled')
            or getattr(self, '_qzone_feed_sweep_running', False)
        ):
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
            # 她自己帖子上的评论 / 点赞：与 auto_feed 无关（见方法说明）。
            await self.qzone_reaction_sweep(story, call, address, now)
            if not runtime.get('auto_feed'):
                self.note_access_skip(
                    'qzone-auto-feed-off', QZONE_AUTO_FEED_NOTE_INTERVAL_MS,
                    'QQ 空间通道已启用，但「自动浏览好友动态」是关的：本轮及以后都不会'
                    '轮询好友动态（在「幕间控制台 → 配置 → QQ 空间」里打开'
                    '「自动浏览好友动态」即可；手动/意图触发的发说说、评论、点赞不受影响）',
                )
                return
            try:
                # **只读动作一律走 `_qzone_run_action`**：NapCat WS 方案（get_cookies +
                # QZone CGI）；拿不到 cookie 就明确失败，不再回落平台动作。
                # 早先这里直接 `call_qzone_action`，等于把 `get_qzone_feeds` 当成
                # NapCat 的**原生动作**打过去——NapCat 根本没有这条 API，于是每轮都
                # 以 `retcode 1404 不支持的Api get_qzone_feeds` 失败（用户贴日志点名）。
                # `page` 是 CGI 认的那个键（历史上另有一条回退通道认 `page_num`，
                # 已随 v1.7.10 删除；这里只留真正会用到的那一个）。
                raw = _mapping(await self._qzone_run_action(call, 'get_qzone_feeds', {
                    'page': QZONE_FEED_PAGE_NUM,
                    'count': QZONE_FEED_PAGE_SIZE,
                }, 'feed'))
                feeds = [
                    entry for entry in (
                        normalize_qzone_feed_entry(_qzone_feed_row(item), now)
                        for item in _rows(pick(raw, 'feeds'))
                    ) if entry
                ]
            except QzoneCgiUnavailable as error:
                # **通道没接上** ≠ 这轮网不好：静默跳过会让"开了自动浏览却永远没动静"
                # 变成无解之谜。按小时节流把原因与下一步做什么说一次（可见 warn）。
                self.note_access_skip(
                    'qzone-channel-required', QZONE_AUTO_FEED_NOTE_INTERVAL_MS, '%s', error,
                )
                return
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
                feed_key = str(pick(feed, 'key') or '')
                # 正文对位的事实（`qzone_feed_content_lookup` 是唯一判据）：
                # `found=False`（列表里没有这条 tid）与"命中了、只是没有文字"必须分开。
                facts: dict[str, Any] = {
                    'found': False, 'content': '', 'images': [], 'videos': [], 'forward': None,
                }
                entries: list[Any] = []
                content_error: Any = None
                try:
                    # 同一条口径：正文对齐也走 CGI 优先的读通道（NapCat 没有
                    # `get_qzone_msg_list` 这条原生动作）。参数名两个通道各取所需：
                    # CGI 认 `targetUin` + `count`。
                    # 账号标识校验（rc33）：**认不出数字 QQ 号就不查**——省掉
                    # `targetUin` 会让 CGI 返回"我自己"的说说列表（认错账号）。
                    target_uin, target_reason = _qzone_target_uin(pick(feed, 'uin'))
                    if target_uin is None:
                        raise _QzoneTargetAccountNotQq(pick(feed, 'uin'), target_reason)
                    list_raw = _mapping(await self._qzone_run_action(call, 'get_qzone_msg_list', {
                        'targetUin': target_uin,
                        'count': QZONE_FEED_MSG_NUM,
                    }, 'moods'))
                    entries = [
                        entry for entry in (
                            normalize_qzone_msg_entry(item)
                            # CGI 通道把说说列表放在 `posts` 里。
                            for item in _rows(pick(list_raw, 'msglist', 'posts'))
                        ) if entry
                    ]
                    facts = qzone_feed_content_lookup(entries, feed)
                except Exception as error:  # noqa: BLE001 - 正文拉取失败按元数据处理
                    # **不许静默**（§55）：正文拉不到是"她只看到有这条动态、没看到内容"，
                    # 这件事必须可见——下面按条给一条可行动的 warn，措辞里也不许声称她看到了。
                    content_error = error
                owner = pick(feed, 'nickname') or 'QQ %s' % pick(feed, 'uin')
                published_at = iso(pick(feed, 'time'))
                images = _rows(facts.get('images'))
                videos = _rows(facts.get('videos'))
                forward = facts.get('forward') if isinstance(facts.get('forward'), Mapping) else None
                has_text = bool(
                    str(facts.get('content') or '').strip()
                    or str(pick(forward, 'content') or '').strip()
                )
                # 图片 / 视频的识别（v1.9.6）：按**已配置的模型能力**自动决定做不做，
                # 走的是既有视觉通道与既有视频链（判据一处，见 `_qzone_feed_media_lines`）。
                # **失败不阻断入账**：这一步出任何岔子，这条动态照常写进剧本。
                media_lines: list[str] = []
                if images or videos:
                    try:
                        media_lines = await self._qzone_feed_media_lines(
                            story, facts, _qzone_feed_observation(owner, facts, published_at),
                        )
                    except Exception as error:  # noqa: BLE001 - 附加能力绝不许带崩动态入账
                        _note_qzone_feed_media(
                            self, 'qzone-feed-media-failed',
                            '好友动态的图片 / 视频识别失败（%s）：这条动态照常入账，'
                            '这次没有画面可看。', error,
                        )
                metadata: dict[str, Any] = {
                    'qzone_feed_key': pick(feed, 'key'),
                    'qzone_feed_uin': pick(feed, 'uin'),
                    'qzone_feed_nickname': pick(feed, 'nickname'),
                    'qzone_feed_time': published_at,
                }
                # 有图片 / 视频 / 是转发就把这几条事实留在条目上（与 `qzone_feed_time` 同一个
                # 槽；**没有**另造媒体通道——结构化媒体表是回合入站媒体的）。
                if images:
                    metadata['qzone_feed_images'] = images
                if videos:
                    metadata['qzone_feed_videos'] = videos
                if forward is not None and str(pick(forward, 'tid') or '').strip():
                    metadata['qzone_feed_forward_tid'] = pick(forward, 'tid')
                await self.append_entry(story_id, {
                    'kind': 'friend-feed', 'actor': 'system',
                    # 措辞判据只有一处（`_qzone_feed_observation`）：有正文 = 她确实看到了
                    # 什么；没正文也要分清"这条本来就没文字"与"我们没取到"；媒体那几行
                    # 接在同一段观察后面（同一处措辞判据）。
                    'content': _qzone_feed_observation(owner, facts, published_at, media_lines),
                    # ⚠️ `occurredAt` 是**她刷到这条动态的时刻**，不是说说自己的发布时间（§55）。
                    # 上游写的是 `feed.time`，而本插件按"故事时间"倒序的两处消费方都会因此
                    # 把它排到旧位置：模型侧的 `recent_entries_for_prompt`（前 50 条 + 60 分钟窗）
                    # 取不到它 → 她压根不知道刷到过；控制台 `console/script` 首页（60 条）
                    # 也看不到 → 用户以为"动态没进剧本"。说说的发布时间改放正文与 metadata。
                    'occurredAt': iso(now),
                    'metadata': metadata,
                }, now)
                if not has_text:
                    if facts.get('found'):
                        # ① 命中了，只是**这条说说本身没有文字**（只有图片 / 转发）——
                        # 这不是故障：留事实（措辞 + metadata）与一条 debug，**不刷 warn**
                        # （§70 真机：一条只有图片的转发动态被报成"说说不含这条 tid"，
                        # 用户照着去查 NapCat 登录态）。
                        self.report_standalone(
                            'debug', '好友动态条目没有文字 归属=%s tid=%s 图片=%d 转发=%s',
                            owner, feed_key, len(images), '是' if forward is not None else '否',
                        )
                    else:
                        # ② 列表里**确实没有这条 tid**（或这一条 CGI 失败）：查的是哪个 tid、
                        # 列表里有几条，必须写进 warn——否则用户无从判断该去修哪一头。
                        if content_error is not None:
                            reason = str(content_error)
                            if isinstance(content_error, _QzoneTargetAccountNotQq):
                                # 多通道账号标识校验（rc33）：不是数字 QQ 号 = 这条读通道
                                # 定位不了那个人的说说列表，下一步要说清该去配什么。
                                next_step = ('确认这条动态带的 uin 是数字 QQ 号——空间读通道'
                                             '（emotion_cgi_msglist_v6）只按数字 QQ 号定位；'
                                             'wxid_* / @chatroom 这类多通道账号本通道读不到，'
                                             '这时只留"她刷到过"，正文不猜')
                            else:
                                next_step = ('确认 NapCat 登录态与 QZone 读通道'
                                             '（emotion_cgi_msglist_v6）能返回该好友的说说列表')
                        else:
                            reason = '查的 tid=%s，该好友最近 %d 条说说里没有它' % (
                                feed_key, len(entries),
                            )
                            next_step = ('确认 QZone 读通道（emotion_cgi_msglist_v6）返回的'
                                         '说说列表里有这条 tid')
                        self.note_access_skip(
                            'qzone-feed-content-missing', QZONE_FEED_CONTENT_NOTE_INTERVAL_MS,
                            '好友动态没拿到正文（%s）：她已经知道%s有这条动态，但没看到内容，'
                            '剧本里也不会写成"她看到了"。下一步：%s。',
                            reason, owner, next_step,
                        )
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
                if media_lines:
                    self.report_standalone(
                        'debug', '好友动态媒体已处理 归属=%s 图片=%d 视频=%d 事实行=%d',
                        owner, len(images), len(videos), len(media_lines),
                    )
                self.report_operation(
                    'standard', 'info', story, 'advance',
                    '好友动态已入账 归属=%s 正文=%s', owner,
                    '有' if has_text else ('只有图片' if images else '无'),
                )
        except Exception as error:  # noqa: BLE001 - 轮询绝不上抛
            self.report_standalone('warn', 'QQ 空间动态轮询失败 错误=%s', error)
        finally:
            self._qzone_feed_sweep_running = False
