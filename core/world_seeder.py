"""世界事件播种器（World Event Seeder）——**纯函数层**。

上游 `src/world-seeder.ts`（1.0.1-rc28）的纯函数层；服务层钩子（定时器、
排水状态机、DB）在 service mixin 里。

本文件只放「可独立测试的判定与解析」：切面轮换、模型输出解析、六道校验闸、
运行配置解析、提示词、季节。模型客户端（`createWorldSeeder`）与
`service.ts` 的 sweep / drain / 注入流程**不在这里**——那些需要宿主与数据库。

依赖约定
--------
* **零第三方依赖**：只用 stdlib（`math` / `unicodedata` / `zoneinfo` /
  `datetime`），时间解析复用同包的 `core/time.py::parse_dt`。
* **不 import astrbot**（`core/` 铁律，见 `plugin/core/__init__.py`）。
* 命名法：Python 标识符与内部快照用 snake_case；**模型 payload / 事件草稿**
  沿用上游 camelCase（`occursAt` / `expiresAt` / `summary` …），因为它们在
  下游要逐字进 wire payload 与数据库列；读外部输入两种拼写都认、优先 camelCase。

事实权威、反应自由：事件是既成现实；她如何感知与应对是主作者的领地。
注册参与者严格拉黑（他们背后是真实的人）；只允许线下通道与 NPC。
"""

from __future__ import annotations

import math
import unicodedata
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .time import parse_dt

__all__ = [
    'WORLD_SEED_DOMAINS',
    'SEED_REJECTIONS',
    'SEED_IMPORTANCE',
    'DEFAULT_WORLD_SEEDER_RUNTIME',
    'STATUS_SCHEDULED',
    'STATUS_INJECTING',
    'STATUS_INJECTED',
    'STATUS_EXPIRED',
    'STATUS_DROPPED',
    'resolve_world_seeder_runtime',
    'seed_domain_for_run',
    'summary_jaccard',
    'parse_world_seed_events',
    'validate_seed_event',
    'world_seeder_system_prompt',
    'season_for_month',
]


# ── 常量 ────────────────────────────────────────────────────────────────────

#: 模型可给的三个重要度档位（上游 `SeedImportance`）。
SEED_IMPORTANCE = frozenset({'low', 'medium', 'high'})

#: 校验闸的拒绝码集合（上游 `SeedRejection`）。
#:
#: `'invalid-shape'` 是上游 `validateSeedEvent` **永不返回**的死码
#: （形状归一发生在 `parseWorldSeedEvents` 里），这里原样保留以便与上游类型
#: 逐字对账，但本实现不会真的用它。
SEED_REJECTIONS = frozenset({
    'invalid-shape',
    'empty-summary',
    'invalid-importance',
    'invalid-time',
    'blocked-name',
    'night-high',
    'duplicate',
})

#: `interlude_seeded_event` 行的状态值。上游 `types.ts` 只声明四个终态
#: （`scheduled` / `injected` / `expired` / `dropped`），`service.ts` 另用
#: `injecting` 作为「已被某轮 sweep 认领」的中间态，这里一并给出。
STATUS_SCHEDULED = 'scheduled'
STATUS_INJECTING = 'injecting'
STATUS_INJECTED = 'injected'
STATUS_EXPIRED = 'expired'
STATUS_DROPPED = 'dropped'

#: 下游快照的默认值（上游 `DEFAULT_WORLD_SEEDER_RUNTIME`）。
#: 键名是**本移植版的 snake_case 快照**（调用方写 `runtime['cadence_minutes']`）；
#: 上游 camelCase 只出现在模型 payload 与数据库列那一侧。
DEFAULT_WORLD_SEEDER_RUNTIME: Dict[str, Any] = {
    'enabled': False,
    'provider': None,
    'cadence_minutes': 45,
    'max_pending': 4,
    'daily_cap': 4,
    'max_horizon_hours': 72,
    'temperature': 0.9,
    'max_tokens': 1_000,
    'timeout': 60_000,
}

#: 世界切面（domain rotation）：切面是任意居住世界都成立的通用方面（非现代地球专属题材），
#: 具体面貌由各剧本自己的 `worldSetting` 决定。切面按 (storyId, 时间槽) 确定性轮换，
#: 无新增持久状态。**顺序即索引**（`seed_domain_for_run` 的 `% len(...)` 依赖它）。
WORLD_SEED_DOMAINS: List[Dict[str, str]] = [
    {'key': 'nature', 'label': '天象与环境', 'brief': '这个世界自己的自然节律：季节、天气、光照、声音与气味、环境的变化'},
    {'key': 'dwelling', 'label': '居所与近邻', 'brief': '她居住的地方与身边的空间：住处内外、近邻的动静、共用场所的状态变化'},
    {'key': 'livelihood', 'label': '生计与日常事务', 'brief': '她谋生、求学或营生方式带来的外部事务：安排与期限、场所状态、来自机构或雇主的告示'},
    {'key': 'close-people', 'label': '亲近之人', 'brief': '线下世界里与她有来往的人：家人、长辈、师长、旧识的近况或线下来讯'},
    {'key': 'paths', 'label': '途中与陌生人', 'brief': '她在外会遇到的：路人与人流、道路或交通、公共场合里偶发的善意或摩擦'},
    {'key': 'chance', 'label': '小意外与际遇', 'brief': '丢失与拾得、临时的小机会、小麻烦、身体的小状况'},
]

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

#: JS `\s`（ECMAScript WhiteSpace ∪ LineTerminator）里**不属于** Unicode `Zs`
#: 的那些：TAB / LF / VT / FF / CR / LS / PS / ZWNBSP。其余空白都是 `Zs`。
_JS_SPACE_EXTRA = '\t\n\x0b\x0c\r\u2028\u2029\ufeff'

#: 切面提示词插入锚点：上游把该行插在第 5 条规则之后。
_DOMAIN_ANCHOR = '- Fit her established life:'

_DOMAIN_PROMPT_LINE = (
    "- THIS RUN'S SLICE OF THE WORLD: {label} — {brief}. A slice is an aspect of the world, "
    "not a genre: render it through this world's own places, people and vocabulary — the supplied "
    "worldSetting is authoritative, and you must never import real-world institutions into a world "
    "that does not have them. Originate this run's event from this slice only; other slices belong "
    "to other runs. If nothing genuine fits the slice right now, return an empty array."
)

#: 上游 `worldSeederSystemPrompt` 的基础文本（逐字：保留 U+2019 弯引号与 em dash）。
_WORLD_SEEDER_PROMPT_LINES: List[str] = [
    'You are the world seeder for HDS Interlude. Your only job is to occasionally originate small external events in the protagonist’s world.',
    'You will receive: current local time and season, the story’s world setting, current scene and arc summaries, a bounded excerpt of her recent established life, her in-flight working details, her relationship network listed as BLOCKED NAMES, and recently seeded events to avoid repeating.',
    'Rules:',
    '- External facts only: things that happen TO her world — environment, neighborhood, offline social world, NPCs she knows, minor mishaps, small opportunities. Never her own decisions, feelings or actions.',
    '- Offline channels only: phone calls, in-person encounters, notices, deliveries, weather, public events. Never any chat message, platform notification or online conversation content.',
    '- NEVER generate events about BLOCKED NAMES or the user. Family, classmates, shopkeepers and strangers who exist only in her offline life are fine.',
    '- Fit the environment: season, weather, the canon setting’s texture (city or village, era, neighborhood), and her daily circumstances.',
    '- Fit her established life: events must be plausible next to the recent script, her working details and the current arc; never contradict what has already happened.',
    '- Place events at concrete future times within the allowed horizon, expressed in the story timezone. Ordinary gaps in her day are the best slots.',
    '- Importance: low = texture she may barely notice; medium = a small practical change; high = relationship-relevant or disruptive. Low must be most of your output; high is rare.',
    'Real life is mostly uneventful. MOST RUNS MUST RETURN an empty events array. Output at most 1 event.',
    'Output one JSON object only: {"events":[{"summary":"one concrete Chinese sentence stating what happened","importance":"low|medium|high","occursAt":"ISO-8601 with offset","expiresAt":"optional ISO-8601","subjects":["names of offline people involved, empty when none"],"rationale":"short reason this fits now"}]}',
]


# ── 内部小工具 ──────────────────────────────────────────────────────────────


def _pick(record: Dict[str, Any], *names: str) -> Tuple[bool, Any]:
    """双读外部输入：按给定顺序取第一个**存在**的键，优先 camelCase。

    返回 ``(键是否存在, 值)``。存在的判定必须与「值是不是 None」分开：
    上游 `Number(null) === 0`，而 `Number(undefined)` 是 `NaN`（见坑 11）。
    """
    for name in names:
        if name in record:
            return True, record[name]
    return False, None


def _js_number(present: bool, value: Any) -> float:
    """等价 JS `Number(value)`；缺键（`undefined`）返回 `NaN`。"""
    if not present:
        return float('nan')
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0.0
        try:
            return float(text)
        except ValueError:
            return float('nan')
    if isinstance(value, (list, tuple)):
        if len(value) == 0:
            return 0.0
        if len(value) == 1:
            return _js_number(True, value[0])
        return float('nan')
    return float('nan')  # dict 等：JS `Number({})` 也是 NaN


def _clamp(present: bool, value: Any, minimum: int, maximum: int, fallback: int) -> int:
    """上游 `clamp`：`Math.floor(Number(v))`，非有限数回落 `fallback`，再夹取。"""
    number = _js_number(present, value)
    if not math.isfinite(number):
        return fallback
    return max(minimum, min(maximum, math.floor(number)))


def _epoch_ms(value: datetime) -> int:
    """等价 JS `Date#getTime()`：自 epoch 起算的**整数**毫秒。

    用 `timedelta` 相减（整数运算）而不是 `timestamp() * 1000` 截断，避免浮点
    误差把整点毫秒算少 1 毫秒（那会让切面槽位偶尔跳格）。
    naive `datetime` 按 UTC 解释，与 `core/time.py` 的 `_ensure_aware` 同法。
    """
    instant = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    delta = instant.astimezone(timezone.utc) - _EPOCH
    return (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000


def _utf16_code_unit(ch: str) -> int:
    """`ch.charCodeAt(0)`：该字符**第一个 UTF-16 码元**（little-endian 前两字节）。"""
    return int.from_bytes(ch.encode('utf-16-le', 'surrogatepass')[:2], 'little')


def _is_js_space(ch: str) -> bool:
    """等价 JS 正则 `\\s` 的字符判定（Unicode `Zs` + 那几个非 `Zs` 的空白）。"""
    return ch in _JS_SPACE_EXTRA or unicodedata.category(ch) == 'Zs'


def _bigrams(text: str) -> Set[str]:
    """上游 `bigrams`：先删掉所有标点(P*)、符号(S*)、空白，再取连续字符二元组。"""
    normalized = ''.join(
        ch for ch in text
        if not (_is_js_space(ch) or unicodedata.category(ch)[0] in ('P', 'S'))
    )
    return {normalized[i:i + 2] for i in range(len(normalized) - 1)}


def _is_importance(value: Any) -> bool:
    """上游用的是三个 `!==` 字符串比较；Python 侧不能直接 `in frozenset`。

    （模型可能回不可哈希的畸形值，`[] in frozenset` 会抛 `TypeError`。）
    """
    return isinstance(value, str) and value in SEED_IMPORTANCE


def _local_hour(value: datetime, name: Any) -> int:
    """故事时区里的小时数；时区无效时回落 UTC（上游 `catch` 分支）。"""
    if isinstance(name, str) and name.strip():
        try:
            return value.astimezone(ZoneInfo(name.strip())).hour
        except (ZoneInfoNotFoundError, ValueError, KeyError, OSError):
            pass
    return value.astimezone(timezone.utc).hour


# ── 运行配置 ────────────────────────────────────────────────────────────────


def resolve_world_seeder_runtime(record: Any, provider: Any = None) -> Dict[str, Any]:
    """上游 `resolveWorldSeederRuntime`：解析「世界播种」运行快照。

    提供商不单独配置：模型中心的连接行勾选「用于世界播种」（`useForWorldSeeding`）
    即为选择。**总开关开了但没有勾选连接 → 关闭**。

    返回快照的键名是 snake_case（本移植版的内部结构）。读 `record` 时 camelCase
    与 snake_case 两种拼写都认、优先 camelCase。

    `temperature` 保留上游的 `||` 怪癖：**显式 `0` 会回落到 `0.9`**
    （`Number(record.temperature) || DEFAULT.temperature`）。这是逐字对等，
    不是 bug——要写 0 得改上游；真想让「零温度」生效的调用方请绕开本函数。
    """
    source = record if isinstance(record, dict) else {}
    assigned: Optional[Dict[str, Any]] = None
    if isinstance(provider, dict) and provider.get('enabled') is not False:
        model = provider.get('model')
        if str(model or '').strip():
            assigned = provider
    snapshot = dict(DEFAULT_WORLD_SEEDER_RUNTIME)
    snapshot.update({
        'enabled': source.get('enabled') is True and assigned is not None,
        'provider': assigned,
        'cadence_minutes': _clamp(
            *_pick(source, 'cadenceMinutes', 'cadence_minutes'), 5, 1_440,
            DEFAULT_WORLD_SEEDER_RUNTIME['cadence_minutes']),
        'max_pending': _clamp(
            *_pick(source, 'maxPending', 'max_pending'), 1, 20,
            DEFAULT_WORLD_SEEDER_RUNTIME['max_pending']),
        'daily_cap': _clamp(
            *_pick(source, 'dailyCap', 'daily_cap'), 1, 20,
            DEFAULT_WORLD_SEEDER_RUNTIME['daily_cap']),
        'max_horizon_hours': _clamp(
            *_pick(source, 'maxHorizonHours', 'max_horizon_hours'), 1, 336,
            DEFAULT_WORLD_SEEDER_RUNTIME['max_horizon_hours']),
        'max_tokens': _clamp(
            *_pick(source, 'maxTokens', 'max_tokens'), 256, 8_192,
            DEFAULT_WORLD_SEEDER_RUNTIME['max_tokens']),
        'timeout': _clamp(
            *_pick(source, 'timeout'), 5_000, 300_000,
            DEFAULT_WORLD_SEEDER_RUNTIME['timeout']),
    })
    temperature_number = _js_number(*_pick(source, 'temperature'))
    # 上游 `|| 0.9`：NaN 与 ±0 都回落默认；`Infinity` 是 truthy，仍会被夹到 2。
    candidate = (
        DEFAULT_WORLD_SEEDER_RUNTIME['temperature']
        if (temperature_number != temperature_number or temperature_number == 0.0)
        else temperature_number
    )
    snapshot['temperature'] = max(0.0, min(2.0, float(candidate)))
    return snapshot


# ── 切面轮换 ────────────────────────────────────────────────────────────────


def seed_domain_for_run(story_id: str, slot_start: datetime, cadence_minutes: int) -> Dict[str, str]:
    """上游 `seedDomainForRun`：同一 (storyId, 时间槽) 内切面稳定，跨槽前进一格。

    逐字等价于上游的
    ``[...storyId].reduce((acc, ch) => (Math.imul(acc, 31) + ch.charCodeAt(0)) >>> 0, 7)``：

    * **UTF-16 码元**：JS 的 `[...storyId]` 按**码点**切分，但 `ch.charCodeAt(0)`
      取的是该码点的**第一个 UTF-16 码元**。二者只在 BMP 之外（星平面字符，如
      `𠮷`、emoji）分道扬镳：那里 `charCodeAt(0)` 给的是**高代理**（0xD800–0xDBFF）。
      `ch.encode('utf-16-le')[:2]` 按小端读回这两个字节，正是同一个码元。
    * **`& 0xFFFFFFFF` 与 `>>> 0` 等价**：`Math.imul(acc, 31)` 是 32 位有符号乘法
      （结果 = 真值 mod 2^32 映到 int32），加 `code`（≤ 0xFFFF）再 `>>> 0` 即
      「mod 2^32 的无符号值」。Python 里 `acc` 恒在 [0, 2^32)，
      `(acc * 31 + code) & 0xFFFFFFFF` 与之一致——`&` 与 `>>>` 都做 mod 2^32。
    * 槽位取 `floor(epoch_ms / (max(5, cadence) * 60_000))`；`max`/`floor` 对负值
      （1970 前）也是朝 −∞ 取整，与 JS `Math.floor` 同向。

    **受控偏离**：Python 的 `%` 是欧几里得取模，`(hash + slot) % 6` 恒落在
    `0..5`；上游在 1970 年之前的槽位上会算出负数下标 → `WORLD_SEED_DOMAINS[-1]`
    → `undefined`。我们这里永远给得出一个合法切面。
    """
    accumulated = 7
    for ch in str(story_id):
        accumulated = (accumulated * 31 + _utf16_code_unit(ch)) & 0xFFFFFFFF
    cadence = max(5, cadence_minutes)
    period = cadence * 60_000
    instant = _epoch_ms(slot_start)
    if isinstance(period, float) and period.is_integer():
        period = int(period)
    if isinstance(period, int):
        slot = instant // period
    else:  # 非整数 cadence（上游 `number` 不保证整数）：退回浮点 floor
        slot = math.floor(instant / period)
    return WORLD_SEED_DOMAINS[(accumulated + slot) % len(WORLD_SEED_DOMAINS)]


# ── 提示词 ──────────────────────────────────────────────────────────────────


def world_seeder_system_prompt(domain: Optional[Dict[str, Any]] = None) -> str:
    """上游 `worldSeederSystemPrompt`：逐字保留（含 U+2019 与 em dash）。

    传了 `domain` 时，在第 5 条规则（`- Fit her established life: …`）之后插入
    「THIS RUN'S SLICE OF THE WORLD」那一行；不传则整行不出现
    （因此提示词里也不会出现子串 `SLICE OF THE WORLD`，旧调用形态逐字不变）。
    """
    lines = list(_WORLD_SEEDER_PROMPT_LINES)
    if domain:
        line = _DOMAIN_PROMPT_LINE.format(
            label=domain.get('label', ''), brief=domain.get('brief', ''))
        anchor = next(
            (index for index, text in enumerate(lines) if text.startswith(_DOMAIN_ANCHOR)),
            len(lines) - 1,
        )
        lines.insert(anchor + 1, line)
    return '\n'.join(lines)


# ── 解析与校验闸（纯函数，可测） ────────────────────────────────────────────


def summary_jaccard(left: str, right: str) -> float:
    """上游 `summaryJaccard`：二元组集合的 Jaccard；任一侧空集返回 `0`。

    Python `re` 不支持 `\\p{P}\\p{S}`，所以归一化改成**逐字符**判定：
    Unicode 类别首字母是 `P`（标点）或 `S`（符号）的删掉，空白删掉；
    空白按 JS `\\s` 的字符集判（Unicode `Zs` + `\\t\\n\\v\\f\\r` + LS/PS/ZWNBSP），
    不用 `str.isspace()`——后者会把 U+001C–U+001F、U+0085 也算进来。

    **受控偏离**：上游的 bigram 按 **UTF-16 码元**滑窗，本实现按**码点**滑窗。
    只在归一化后仍含星平面字符、且该字符不是符号/标点（例如扩展 B 区汉字
    `𠮷`）时两者才不同：`bigrams('𠮷好')` 上游是 `{'𠮷', '\\udfb7好'}`（低位代理
    单独参与滑窗），本实现是 `{'𠮷好'}`。这类字在中文散文里极罕见；阈值 0.6 上的
    判决在实测的正常句子上完全一致（用 Node 跑了 10 组含全角/emoji/空白/标点的对照）。
    """
    a, b = _bigrams(left), _bigrams(right)
    if not a or not b:
        return 0.0
    shared = len(a & b)
    return shared / (len(a) + len(b) - shared)


def parse_world_seed_events(value: Any, limit: int = 2) -> List[Dict[str, Any]]:
    """上游 `parseWorldSeedEvents`：防御解析模型输出，坏项丢弃。

    输入可能是 dict、可能带 `events`、也可能是裸字符串；非 dict / `events` 非
    数组一律 `[]`。逐条：`summary` trim + `[:200]`（空则丢）；`importance` 必须
    是三档之一；`occursAt` 解析不出就丢；`expiresAt` 只在能解析且 **> occursAt**
    时落键；`subjects` 只留字符串、trim、`[:40]`、去空、最多 4；`rationale`
    trim + `[:200]`。最多返回 `limit` 条。

    输出是**内部草稿**：键名按上游 camelCase（下游要逐字进 payload / 数据库列），
    时间值是 aware `datetime`（不是字符串）。
    """
    record = value if isinstance(value, dict) else {}
    raw_events = record.get('events')
    events = raw_events if isinstance(raw_events, list) else []
    drafts: List[Dict[str, Any]] = []
    for raw in events[:limit]:
        if not isinstance(raw, dict):
            continue
        summary_raw = raw.get('summary')
        summary = summary_raw.strip()[:200] if isinstance(summary_raw, str) else ''
        importance = raw.get('importance')
        if not summary or not _is_importance(importance):
            continue
        occurs_at = parse_dt(_pick(raw, 'occursAt', 'occurs_at')[1])
        if occurs_at is None:
            continue
        expires_at = parse_dt(_pick(raw, 'expiresAt', 'expires_at')[1])
        if expires_at is not None and expires_at <= occurs_at:
            expires_at = None
        subjects: List[str] = []
        raw_subjects = raw.get('subjects')
        if isinstance(raw_subjects, list):
            trimmed = [item.strip()[:40] for item in raw_subjects if isinstance(item, str)]
            subjects = [item for item in trimmed if item][:4]
        rationale_raw = raw.get('rationale')
        rationale = rationale_raw.strip()[:200] if isinstance(rationale_raw, str) else ''
        draft: Dict[str, Any] = {
            'summary': summary,
            'importance': importance,
            'occursAt': occurs_at,
            'subjects': subjects,
            'rationale': rationale,
        }
        if expires_at is not None:
            draft['expiresAt'] = expires_at
        drafts.append(draft)
    return drafts


def validate_seed_event(draft: Dict[str, Any], input_: Dict[str, Any]) -> Optional[str]:
    """上游 `validateSeedEvent`：六道闸按序短路，返回拒绝码或 `None`。

    `draft` 是 `parse_world_seed_events` 那样的 camelCase 草稿（双读，
    snake_case 也认）；`input_` 是 snake_case 的校验上下文：
    `now` / `timezone` / `max_horizon_hours` / `blocked_names` / `recent_summaries`
    （camelCase 也认、优先 camelCase）。

    宁可错杀：任何一项不过即弃，不重试。
    """
    summary = draft.get('summary')
    # 长度查的是**未 trim** 的串（上游 `draft.summary.length > 200`）。
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 200:
        return 'empty-summary'

    importance = draft.get('importance')
    if not _is_importance(importance):
        return 'invalid-importance'

    now = parse_dt(_pick(input_, 'now')[1])
    occurs_at = parse_dt(_pick(draft, 'occursAt', 'occurs_at')[1])
    if now is None or occurs_at is None:
        return 'invalid-time'
    occurs_ms = _epoch_ms(occurs_at)
    now_ms = _epoch_ms(now)
    # 缺 `maxHorizonHours` 时上游是 `now + undefined * …` = NaN，比较恒假
    # （即没有上界）；`_js_number` 缺键给 NaN，保持同一行为。
    horizon_ms = _js_number(*_pick(input_, 'maxHorizonHours', 'max_horizon_hours')) * 3_600_000
    if occurs_ms <= now_ms or occurs_ms > now_ms + horizon_ms:
        return 'invalid-time'

    blocked_present, blocked = _pick(input_, 'blockedNames', 'blocked_names')
    if blocked_present and isinstance(blocked, list):
        raw_subjects = draft.get('subjects')
        subjects = raw_subjects if isinstance(raw_subjects, list) else []
        for name in blocked:
            if not isinstance(name, str):
                continue
            trimmed = name.strip()
            if len(trimmed) < 2:
                continue
            if trimmed in summary:
                return 'blocked-name'
            for subject in subjects:
                if isinstance(subject, str) and (trimmed in subject or subject.strip() in trimmed):
                    return 'blocked-name'

    if importance == 'high':
        hour = _local_hour(occurs_at, _pick(input_, 'timezone')[1])
        if 0 <= hour < 6:
            return 'night-high'

    recent_present, recent = _pick(input_, 'recentSummaries', 'recent_summaries')
    if recent_present and isinstance(recent, list):
        for item in recent:
            if isinstance(item, str) and summary_jaccard(summary, item) > 0.6:
                return 'duplicate'
    return None


# ── 季节（北半球硬编码） ────────────────────────────────────────────────────


def season_for_month(month: int) -> str:
    """北半球季节名：3–5 `spring` / 6–8 `summer` / 9–11 `autumn` / 其余 `winter`。"""
    if 3 <= month <= 5:
        return 'spring'
    if 6 <= month <= 8:
        return 'summer'
    if 9 <= month <= 11:
        return 'autumn'
    return 'winter'
