"""异步动作「完成即回报」的三态判据（v1.9.9，见 `docs/PORTING_NOTES.md` §87）。

上游没有这一层：Koishi 版把「发出去没有」写进投递账本（M6.1）之后，只等下一次叙事
回合顺带把 `deliveryReality` 读走——用户那边看到的就是"她说在搜/在发，然后没有然后"，
直到有人再发一条消息。本模块是那条链的**唯一判据**：

    投递账本（`delivery_ledger`）→ 投递现实（`delivery_reality`）→ 三态 + 逐段事实

三态
----
* ``delivered`` 成功（结果已确认送达）
* ``failed``    失败（出错 / 已取消——**没发出去**）
* ``unknown``   结果不可知（超时、回执缺失：片段还停在 ``pending``）

**判据只在这一处。** 状态词表 ``SEGMENT_FACTS`` 归 ``delivery_reality`` 所有：本模块
不复制它的措辞，逐段事实一律取 `delivery_reality()` 返回值里的 ``fact`` 字段；
本模块只做一件 `delivery_reality` 不做的事——把它的**逐段结果词**归并成三态，
并给模型一句可以直接读的总述（`completion_summary`）。

失败方向（刻意选择，见 §87）
---------------------------
* 认不出的结果词一律归 ``unknown``，**绝不猜成成功**（把"没发出去"说成"已送达"
  是本模块最坏的失败方向）。
* 一个动作里既有失败又有回执缺失时，总述取 ``unknown``（"这条到底发没发出去"是
  更诚实的总结），但那几行**逐段事实照样全部列出**——补发还是作罢由她看着原文决定。
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from .delivery_reality import delivery_reality

#: 三态（顺序即"从好到坏"的展示顺序，不是优先级）。
COMPLETION_STATES = ('delivered', 'failed', 'unknown')

#: 三态各自的一句话（**模型可见文案**，只在逐段事实缺失时兜底）。
#:
#: 正常路径用 `delivery_reality` 的逐段 `fact`（§58 已定稿的措辞）；只有拿不到逐段
#: 事实时（例如平台动作、浏览观察只有一句总述）才由这里给总述开头。
STATE_FACTS = {
    'delivered': '已送达',
    'failed': '没发出去',
    'unknown': '结果不确定',
}

#: 结果词 → 三态。键取自 `delivery_reality` 的 ``outcome`` 枚举（`_segment_outcome`），
#: 另外收几个执行侧自己用的词（浏览观察的 ``success``/``blocked``、平台动作的 ``ok``）。
#: **认不出的一律 ``unknown``**（见模块 docstring 的失败方向）。
_STATE_OF_OUTCOME = {
    'delivered': 'delivered',
    'success': 'delivered',
    'ok': 'delivered',
    'failed': 'failed',
    'cancelled': 'failed',
    'delivery-not-confirmed-after-error': 'failed',
    'not-confirmed': 'unknown',
    'pending': 'unknown',
    'blocked': 'unknown',
    'timeout': 'unknown',
    'unavailable': 'unknown',
    'unknown': 'unknown',
}


def _word(value: Any) -> str:
    """结果词归一（None / 非字符串 → 空串，绝不隐式转成 ``"None"``）。"""
    return value.strip().lower() if isinstance(value, str) else ''


def completion_state(outcomes: Any) -> str:
    """把一串结果词归并成三态（**唯一判据**）。

    ``outcomes`` 是一段动作里每个可确认单元的结果词；顺序无关。空序列按 ``unknown``
    处理：没有结果词就是**没有回执**，不是成功。
    """
    if isinstance(outcomes, str):
        outcomes = [outcomes]
    words = [_word(item) for item in (outcomes or [])]
    if not words:
        return 'unknown'
    states = [_STATE_OF_OUTCOME.get(word, 'unknown') for word in words]
    # 回执缺失优先于失败：一段里既有"出错"又有"没回执"时，整体仍不是可结算的结论。
    if any(state == 'unknown' for state in states):
        return 'unknown'
    if any(state == 'failed' for state in states):
        return 'failed'
    return 'delivered'


def completion_summary(state: Any, facts: Any = ()) -> str:
    """一句模型可见的总述：``状态词：逐段事实``。

    成功（``delivered``）**不加**状态词前缀——那时逐段事实本身就是结论（浏览观察的
    摘要是一整句中文），硬拼一个"已送达："只会让句子变形。失败/不可知一定要带状态词，
    她才知道这是"没发出去"而不是正文。
    """
    normalized = _word(state) or 'unknown'
    if normalized not in COMPLETION_STATES:
        normalized = 'unknown'
    lines = [item.strip() for item in (facts or []) if isinstance(item, str) and item.strip()]
    body = '；'.join(lines)
    if normalized == 'delivered':
        return body or STATE_FACTS['delivered']
    lead = STATE_FACTS[normalized]
    return '%s：%s' % (lead, body) if body else lead


def ledger_completion(
    entry: Any,
    commit_id: Any,
    event_id: Any,
    participant_id: Optional[str] = None,
    share_participant_details: bool = False,
) -> dict[str, Any]:
    """从一个剧本行的投递账本派生完成事实（三态 + 逐段事实）。

    **判据的唯一入口**：`delivery_reality()` 是"这一段到底怎么了"的唯一判据，本函数
    只把它返回的 ``outcome`` / ``fact`` 收拢成三态与事实行，不自己看片段状态。

    ``delivery_reality`` 只报告**还有未确认片段**的行动，所以"这次调用没查到该事件"
    有两种可能：全部片段都已送达（成功），或者账本里压根没有这个事件。两种情况都返回
    ``delivered`` —— 调用方（投递漏斗）永远是在**刚写完这一笔**之后调用，能区分二者；
    而"没有这一笔"时按成功返回是安全的：它不产生回报，也不会把没发出去的说成发出去
    （那需要账本里真的写着 failed/pending）。
    """
    if not isinstance(entry, dict):
        return {'state': 'delivered', 'facts': [], 'outcomes': []}
    reports = delivery_reality(
        [{'kind': 'script', 'id': entry.get('id'), 'metadata': entry.get('metadata')}],
        participant_id or None,
        share_participant_details,
        float('inf'),
    )
    match: Optional[dict[str, Any]] = None
    for report in reports:
        if isinstance(report, dict) and report.get('eventId') == event_id:
            match = report
            break
    if match is None:
        return {'state': 'delivered', 'facts': [], 'outcomes': []}
    segments = [item for item in (match.get('segments') or []) if isinstance(item, dict)]
    outcomes = [item.get('outcome') for item in segments]
    facts = [item.get('fact') for item in segments if isinstance(item.get('fact'), str)]
    return {
        'state': completion_state(outcomes),
        'facts': facts,
        'outcomes': outcomes,
        'sourceEntryId': match.get('sourceEntryId'),
        'commitId': commit_id,
        'eventId': event_id,
    }


def platform_action_state(outcomes: Iterable[Any]) -> str:
    """平台动作的完成状态（chunk12 写进剧本行的 ``metadata.platform_actions``）。

    执行侧（chunk12/chunk13）不动：它已经把自己的结果如实写进剧本条目（`[平台动作] …`），
    并通过**同一个** `completion_state()` 归并成三态。逐条事实直接用执行侧写下的
    那一句正文，所以这里不产生第二份措辞。
    """
    items = [item for item in (outcomes or []) if isinstance(item, dict)]
    if not items:
        return 'delivered'
    words: list[str] = []
    for item in items:
        error = item.get('error') if isinstance(item.get('error'), str) else ''
        if item.get('ok'):
            words.append('ok')
            continue
        # 有原因 = 明确失败；没有原因 = 我们不知道它到底有没有发生（回执缺失）。
        words.append('failed' if error else 'unknown')
    return completion_state(words)
