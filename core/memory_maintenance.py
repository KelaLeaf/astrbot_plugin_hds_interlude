"""记忆维护的纯逻辑（v1.4.0，见 `docs/MEMORY_MAINTENANCE.md`）。

这个模块只做**不依赖服务、不依赖 astrbot** 的两件事：

1. `anchor_relative_times`：把「昨天」「上周」这类相对时间按当时的剧情时间换成
   具体日期，避免长期事实随天数漂移（记忆腐烂）。
2. `MaintenanceBudget`：一次后台维护的硬预算（调用次数 / 运行时长 / 调用最小间隔）。

真正的落库与模型调用在 `service/chunk8.py` 的 `run_memory_maintenance` 里编排。
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Optional

# --------------------------------------------------------------------- #
# 相对时间锚定
# --------------------------------------------------------------------- #

#: 中文数字（一到十与常见写法），只覆盖日期里会出现的量级。
_CN_DIGITS = {
    '零': 0, '一': 1, '两': 2, '二': 2, '三': 3, '四': 4,
    '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10,
}

#: 星期字 → `date.weekday()`（周一 = 0）。
_WEEKDAYS = {'一': 0, '二': 1, '三': 2, '四': 3, '五': 4, '六': 5, '日': 6, '天': 6}


def parse_cn_number(text: str) -> Optional[int]:
    """解析中文数字（支持「十五」「二十」「二十三」这类十位写法）。"""
    raw = (text or '').strip()
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    if raw == '十':
        return 10
    if '十' in raw:
        head, _, tail = raw.partition('十')
        tens = _CN_DIGITS.get(head, 1) if head else 1
        ones = _CN_DIGITS.get(tail, 0) if tail else 0
        if head and head not in _CN_DIGITS:
            return None
        if tail and tail not in _CN_DIGITS:
            return None
        return tens * 10 + ones
    if len(raw) == 1 and raw in _CN_DIGITS:
        return _CN_DIGITS[raw]
    return None


def _minus_months(moment: datetime, months: int) -> datetime:
    """按月回退，日超出目标月天数时夹到该月最后一天。"""
    total = (moment.year * 12 + moment.month - 1) - months
    year, month = divmod(total, 12)
    month += 1
    day = moment.day
    while day > 1:
        try:
            return moment.replace(year=year, month=month, day=day)
        except ValueError:
            day -= 1
    return moment.replace(year=year, month=month, day=1)


def _date_text(moment: datetime, anchor: datetime) -> str:
    """同一年只写月日，跨年写完整年份。"""
    if moment.year == anchor.year:
        return '%d月%d日' % (moment.month, moment.day)
    return '%d年%d月%d日' % (moment.year, moment.month, moment.day)


def _week_bounds(moment: datetime) -> tuple[datetime, datetime]:
    start = moment - timedelta(days=moment.weekday())
    return start, start + timedelta(days=6)


def anchor_relative_times(text: str, anchor: datetime) -> str:
    """把一段中文里的相对时间换成具体日期。

    `anchor` 用**剧情时间**（不是墙钟时间）：一条事实是从某个回合抽取的，
    那句「昨天」指的是那个回合当天，回退一天。

    只替换能唯一落到某一天的写法；「最近」「这几天」这类无法定界的词一律保持原样。
    """
    if not text or not isinstance(text, str):
        return text
    result = text

    # 前一天 / 后一天：先处理最长的写法，避免「大前天」被「前天」截胡。
    day_offsets = (
        ('大大前天', -4), ('大前天', -3), ('大后天', 3),
        ('前天', -2), ('后天', 2),
        ('昨天', -1), ('昨日', -1), ('今天', 0), ('今日', 0),
        ('明天', 1), ('明日', 1),
    )
    for token, offset in day_offsets:
        if token in result:
            result = result.replace(token, _date_text(anchor + timedelta(days=offset), anchor))

    # N 天前 / N 天后 / N 天以后（阿拉伯数字与中文数字都认）。
    number = r'([0-9]{1,4}|[零一两二三四五六七八九十]{1,3})'
    pattern = re.compile(number + r'\s*天\s*(前|之前|后|之后|以后)')
    for match in reversed(list(pattern.finditer(result))):
        count = parse_cn_number(match.group(1))
        if count is None:
            continue
        backward = match.group(2) in ('前', '之前')
        moment = anchor + timedelta(days=-count if backward else count)
        result = result[:match.start()] + _date_text(moment, anchor) + result[match.end():]

    # 上周X / 上星期X / 本周X：落到具体那一天。
    pattern = re.compile(r'(上|这|本)?\s*(?:周|星期|礼拜)\s*([一二三四五六日天])')
    for match in reversed(list(pattern.finditer(result))):
        prefix = match.group(1) or ''
        weekday = _WEEKDAYS.get(match.group(2))
        if weekday is None:
            continue
        if prefix == '上':
            moment = anchor - timedelta(days=anchor.weekday() + 7 - weekday)
        elif prefix in ('这', '本'):
            moment = anchor - timedelta(days=anchor.weekday() - weekday)
        else:
            # 裸写的「周三」按锚点所在的那一周算；跨不跨周不影响落到哪一天。
            moment = anchor - timedelta(days=anchor.weekday() - weekday)
        result = result[:match.start()] + _date_text(moment, anchor) + result[match.end():]

    # 裸写「上周 / 上星期」：给出一整周的区间。
    pattern = re.compile(r'上\s*(?:周|星期|礼拜)(?![一二三四五六日天])')
    for match in reversed(list(pattern.finditer(result))):
        start, end = _week_bounds(anchor - timedelta(days=7))
        result = (
            result[:match.start()]
            + '%s到%s' % (_date_text(start, anchor), _date_text(end, anchor))
            + result[match.end():]
        )

    # N 个月前 / N 个月前（带「个」）。
    pattern = re.compile(number + r'\s*个?\s*月\s*(前|之前|后|之后|以后)')
    for match in reversed(list(pattern.finditer(result))):
        count = parse_cn_number(match.group(1))
        if count is None:
            continue
        backward = match.group(2) in ('前', '之前')
        moment = _minus_months(anchor, count if backward else -count)
        result = result[:match.start()] + _date_text(moment, anchor) + result[match.end():]

    # 上个月 / 上月 + 可选的具体日。
    pattern = re.compile(r'上\s*个?\s*月\s*([0-9]{1,2}|[零一二三四五六七八九十]{1,3})?\s*(?:号|日)?')
    for match in reversed(list(pattern.finditer(result))):
        day_text = match.group(1)
        previous = _minus_months(anchor, 1)
        if not day_text:
            # 没写具体日就只到月份：「上个月」指的是 8 月，不该凭空补出一个 27 号。
            stamp = ('%d年%d月' % (previous.year, previous.month)) if previous.year != anchor.year \
                else ('%d月' % previous.month)
            result = result[:match.start()] + stamp + result[match.end():]
            continue
        day = parse_cn_number(day_text)
        if day and 1 <= day <= 31:
            try:
                previous = previous.replace(day=day)
            except ValueError:
                pass
        result = result[:match.start()] + _date_text(previous, anchor) + result[match.end():]

    # 去年 / 明年 / 前年：只到年份，日不重要。
    pattern = re.compile(r'(去|明|今|前)\s*年')
    for match in reversed(list(pattern.finditer(result))):
        prefix = match.group(1)
        year = anchor.year + {'去': -1, '明': 1, '今': 0, '前': -2}.get(prefix, 0)
        result = result[:match.start()] + ('%d年' % year) + result[match.end():]

    return result


# --------------------------------------------------------------------- #
# 单轮维护预算
# --------------------------------------------------------------------- #

@dataclass
class MaintenanceBudget:
    """一次后台维护的硬预算：调用次数、运行时长、两次调用之间的最小间隔。

    对应参考实现的「单轮最多 N 次调用 / 最长 M 分钟 / 最小间隔 X 毫秒」，
    在本移植版里由 `run_memory_maintenance` 现场构造。`sleep` 与 `clock`
    可注入，测试里不需要真的等。
    """

    max_calls: int = 12
    max_runtime_seconds: float = 600.0
    min_call_interval_ms: int = 500
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    calls: int = 0
    started_at: float = field(default=0.0)
    last_call_at: Optional[float] = None
    spent_ms: float = 0.0

    def __post_init__(self) -> None:
        self.started_at = self.clock()
        self.max_calls = max(0, int(self.max_calls))
        self.max_runtime_seconds = max(0.0, float(self.max_runtime_seconds))
        self.min_call_interval_ms = max(0, int(self.min_call_interval_ms))

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, self.clock() - self.started_at)

    def exhausted(self) -> str:
        """返回不能继续的原因，空串表示还能继续。"""
        if self.calls >= self.max_calls:
            return '预算用尽（调用数 %d）' % self.max_calls
        if self.max_runtime_seconds and self.elapsed_seconds >= self.max_runtime_seconds:
            return '预算用尽（时长 %d 分钟）' % int(self.max_runtime_seconds // 60)
        return ''

    async def acquire(self) -> str:
        """占用一次调用额度；返回空串表示拿到，否则返回放弃的原因。"""
        blocked = self.exhausted()
        if blocked:
            return blocked
        if self.last_call_at is not None and self.min_call_interval_ms:
            wait_ms = self.min_call_interval_ms - (self.clock() - self.last_call_at) * 1000
            if wait_ms > 0:
                await self.sleep(wait_ms / 1000.0)
        self.calls += 1
        now = self.clock()
        self.last_call_at = now
        self.spent_ms = max(0.0, (now - self.started_at) * 1000)
        return ''

    def summary(self) -> dict[str, Any]:
        return {
            'calls': self.calls,
            'elapsed_ms': int(self.elapsed_seconds * 1000),
            'max_calls': self.max_calls,
            'max_runtime_seconds': self.max_runtime_seconds,
            'min_call_interval_ms': self.min_call_interval_ms,
        }


# --------------------------------------------------------------------- #
# 事实分组与维护决定
# --------------------------------------------------------------------- #

#: 归一化后内容相同即视为完全重复（不需要模型判断）。
#: 相似度用的双字组集合，超过下面这个阈值才算"疑似同一条"。
SIMILAR_FACT_THRESHOLD = 0.62

_STOP_CHARS = re.compile(r'[\s\u3000，。！？、；：""' + "'" + r'（）《》【】,.!?;:()\[\]{}<>"\-—…~]+')


def fact_text_key(text: Any) -> str:
    """事实文本的比较键：去空白与标点、统一小写。"""
    return _STOP_CHARS.sub('', str(text or '')).lower()


def fact_text_keys(text: Any) -> set[str]:
    """中文双字组 + 英文数字词的集合（不依赖分词器）。"""
    normalized = _STOP_CHARS.sub('', str(text or '')).lower()
    keys = set(re.findall(r'[a-z0-9]{3,}', normalized))
    for run in re.findall(r'[\u3400-\u9fff]{2,}', normalized):
        keys.update(run[index:index + 2] for index in range(len(run) - 1))
    return keys


def fact_similarity(left: Any, right: Any) -> float:
    """两条事实的相似度（双字组重叠率），[0, 1]。

    分母取**较短**那一条的键数，而不是并集：事实的重复通常是"一条比另一条多写了细节"，
    用并集会因为多出来的字而降分，把同一条事实判成两条不同的。
    代价是短句容易被并进包含它的长句里（例如「她在家」与「她在家乡的老房子住了很久」），
    所以这只是**候选**筛选：最终留哪条由维护模型裁决，宁可多花一次调用，也不误删。
    """
    left_keys = fact_text_keys(left)
    right_keys = fact_text_keys(right)
    if not left_keys or not right_keys:
        return 0.0
    shorter = min(len(left_keys), len(right_keys))
    return len(left_keys & right_keys) / shorter if shorter else 0.0


def _fact_ids(fact: Any) -> set[Any]:
    raw = fact.get('sourceEntryIds') if isinstance(fact, dict) else None
    return set(raw) if isinstance(raw, list) else set()


def _same_owner(left: Any, right: Any) -> bool:
    return str(left.get('participantId') or '') == str(right.get('participantId') or '')


def group_similar_facts(
    facts: list[Any], threshold: float = SIMILAR_FACT_THRESHOLD,
) -> list[list[Any]]:
    """把疑似重复/矛盾的事实分成组（并查集）。

    两条事实进同一组要同时满足两个条件：

    1. **同一个参与者**（`participantId` 相同）——不同人的事实永不合并，
       否则等于把一个人的隐私写进另一个人的记忆；
    2. 证据回合有交集，或内容相似度达到阈值。

    只返回成员数 ≥ 2 的组；调用方再决定要不要花模型调用去裁决。
    """
    rows = [fact for fact in (facts or []) if isinstance(fact, dict)]
    parent = list(range(len(rows)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[max(root_left, root_right)] = min(root_left, root_right)

    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            if not _same_owner(rows[i], rows[j]):
                continue
            shared_evidence = bool(_fact_ids(rows[i]) & _fact_ids(rows[j]))
            similar = fact_similarity(rows[i].get('content'), rows[j].get('content')) >= threshold
            if shared_evidence or similar:
                union(i, j)

    buckets: dict[int, list[Any]] = {}
    for index, fact in enumerate(rows):
        buckets.setdefault(find(index), []).append(fact)
    return [group for group in buckets.values() if len(group) > 1]


def exact_duplicate_groups(facts: list[Any]) -> list[list[Any]]:
    """归一化后内容完全相同的组（同一参与者），不需要模型调用。"""
    buckets: dict[tuple[str, str], list[Any]] = {}
    for fact in facts or []:
        if not isinstance(fact, dict):
            continue
        key = (str(fact.get('participantId') or ''), fact_text_key(fact.get('content')))
        if not key[1]:
            continue
        buckets.setdefault(key, []).append(fact)
    return [group for group in buckets.values() if len(group) > 1]


#: 维护模型可以给一组事实的三个决定。
MAINTENANCE_ACTIONS = ('merge', 'supersede', 'keep')


def normalize_maintenance_decision(raw: Any, allowed_ids: set[Any]) -> list[dict[str, Any]]:
    """校验并规范化维护模型的输出。

    接受 `{"groups": [{"ids": [...], "action": ..., "content": ..., "keepId": ..., "reason": ...}]}`
    或直接给数组；**只保留合法且落在候选集合里的 id**，动作不在三个之内的一律丢弃，
    宁可少做也不做错。
    """
    record = raw if isinstance(raw, dict) else {}
    groups = record.get('groups') if isinstance(record.get('groups'), list) else raw
    decisions: list[dict[str, Any]] = []
    if not isinstance(groups, list):
        return decisions
    for item in groups:
        if not isinstance(item, dict):
            continue
        action = item.get('action')
        if action not in MAINTENANCE_ACTIONS:
            continue
        raw_ids = item.get('ids')
        ids = [
            value for value in (raw_ids if isinstance(raw_ids, list) else [])
            if value in allowed_ids
        ]
        if len(ids) < 2:
            continue
        keep_id = item.get('keepId', item.get('keep_id'))
        if keep_id not in ids:
            keep_id = ids[0]
        content = item.get('content')
        reason = item.get('reason')
        decisions.append({
            'ids': ids,
            'action': action,
            'keepId': keep_id,
            'content': content.strip() if isinstance(content, str) else '',
            'reason': reason.strip() if isinstance(reason, str) else '',
        })
    return decisions
