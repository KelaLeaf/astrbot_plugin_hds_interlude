"""上游 `src/database.ts` 的 Python 对应物：HDS Interlude 的持久化层（13 张表）。

移植自 Koishi / TypeScript 上游快照 `upstream/src/database.ts`（159 行）。
上游用 Koishi ORM（`ctx.model.extend(...)`，底层 minato + sql.js）声明式注册表；
本移植版用标准库 `sqlite3` 建等价的表与索引。

语言映射约定（详见 `docs/PORT_PLAN.md`）：
- 字段名一律**保留上游 camelCase**（`selfId` / `occurredAt` / `sourceEntryIds` …）：
  service 层按名字读写，转换会破坏兼容性。
- `unsigned` → `INTEGER`；`double` → `REAL`；`boolean` → `INTEGER`；
  `json` → `TEXT`（`json.dumps` / `json.loads`，`ensure_ascii=False`）；
  `text` → `TEXT`；`timestamp` → `TEXT`（ISO-8601 字符串，见 `core/time.py`）；
  `unsigned autoInc` 主键 → `INTEGER PRIMARY KEY AUTOINCREMENT`。
- 读写行时按 `TABLES` 里声明的 json / timestamp 列**自动互转**。

对应关系
--------
* `registerTables(ctx)` → `Database.register_tables()`。
  上游靠"父 context 上已注册的表不重建，只补新表/新列"来避免热重载时
  minato 重建全部索引；本移植版对应"`CREATE TABLE IF NOT EXISTS` +
  `ALTER TABLE ADD COLUMN` 增量补列"。
* `registerScriptEntryEmbedding` / `registerWebObservationTable` /
  `registerOverlaySnapshotTable` / `registerStickerTable` /
  `registerSchedulePreplanTable` 五个独立注册函数 → 统一由 `TABLES` 数据驱动。
* `normalizeDatabaseRow`（上游 service.ts:8169）**属于 `service.py` 的移植任务**，
  本模块不实现它；只导出 `json_columns(table)` / `timestamp_columns(table)`
  两个辅助函数供其使用。

全局写队列
----------
上游注释明确「SQLite 写入另有全局写队列」。本模块把它显式化：
`WRITE_LOCK`（模块级 `asyncio.Lock`）用于把**异步**写操作串行化，
`Database.write(fn)` 是唯一入口；同步方法本身保持不变（sqlite3 连接配
模块级 `_SYNC_WRITE_LOCK`，跨线程安全）。
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import sqlite3
import threading
import weakref
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Optional, TypeVar

try:  # 以包结构 import（正常路径）。
    from .time import iso, parse_dt
except ImportError:  # pragma: no cover - 允许单文件直接执行/调试。
    from plugin.core.time import iso, parse_dt

__all__ = [
    'ColumnSpec', 'TableSpec', 'TABLES',
    'WRITE_LOCK', 'write_lock',
    'json_columns', 'timestamp_columns',
    'primary_key', 'auto_increment', 'table_names',
    'Database',
]

# ========== 类型映射（上游 minato 字面量 → sqlite3 类型） ==========

#: 上游 `unsigned` / `unsigned autoInc` / `string(n)` → `INTEGER`。
_INTEGER = 'INTEGER'
#: 上游 `double` → `REAL`。
_REAL = 'REAL'
#: 上游 `string(n)` / `text` / `json` / `timestamp` / `boolean` → `TEXT`
#: （json 用 `json.dumps`；timestamp 用 ISO 字符串；boolean 见下）。
_TEXT = 'TEXT'
#: 上游 `boolean` → `INTEGER`（0/1），见任务约定。
_BOOLEAN = 'INTEGER'

#: 上游类型字面量 → 本移植版的存储类型。
TYPE_MAP: dict[str, str] = {
    'unsigned': _INTEGER,
    'double': _REAL,
    'boolean': _BOOLEAN,
    'json': _TEXT,
    'text': _TEXT,
    'timestamp': _TEXT,
}


def _string_type(length: int) -> str:
    """上游 `string(255)`：sqlite3 不做长度约束，但把长度记进 DDL 注释保真。"""
    del length  # sqlite3 的 TEXT 无长度语义，长度只作为上游信息保留。
    return _TEXT


def _spec(upstream_type: str) -> 'ColumnSpec':
    """上游类型字面量 → `ColumnSpec`。"""
    if upstream_type == 'unsigned autoInc':
        return ColumnSpec(_INTEGER, upstream_type, auto_increment=True)
    if upstream_type.startswith('string('):
        length = upstream_type[len('string('):-1]
        return ColumnSpec(_string_type(int(length)), upstream_type)
    return ColumnSpec(TYPE_MAP[upstream_type], upstream_type)


@dataclass(frozen=True)
class ColumnSpec:
    """一列的存储定义（含上游类型字面量，便于对照/审计）。"""

    type: str
    upstream_type: str
    auto_increment: bool = False

    @property
    def is_json(self) -> bool:
        return self.upstream_type == 'json'

    @property
    def is_timestamp(self) -> bool:
        return self.upstream_type == 'timestamp'


@dataclass(frozen=True)
class TableSpec:
    """一张表的列定义与索引定义（上游 `ctx.model.extend(name, fields, options)`）。"""

    name: str
    fields: dict[str, ColumnSpec]
    primary: str
    auto_increment: bool = False
    indexes: tuple[str, ...] = ()
    unique: tuple[str, ...] = ()
    #: 上游按需（升级时）单独注册的表；首次注册顺序里的位置由此保留。
    added_later: bool = False

    @property
    def json_columns(self) -> frozenset[str]:
        return frozenset(name for name, spec in self.fields.items() if spec.is_json)

    @property
    def timestamp_columns(self) -> frozenset[str]:
        return frozenset(name for name, spec in self.fields.items() if spec.is_timestamp)

    def column_sql(self, name: str) -> str:
        """单列的 DDL 片段。主键由 `create_table_sql()` 统一处理。"""
        spec = self.fields[name]
        parts = [quote_ident(name)]
        if name == self.primary:
            parts.append(_INTEGER if spec.type == _INTEGER else spec.type)
        else:
            parts.append(spec.type)
        return ' '.join(parts)

    def create_table_sql(self) -> str:
        """`CREATE TABLE IF NOT EXISTS ...`（列顺序与上游 `fields` 声明顺序一致）。"""
        columns = [self.column_sql(name) for name in self.fields]
        if self.auto_increment:
            columns.append('PRIMARY KEY (%s AUTOINCREMENT)' % quote_ident(self.primary))
        else:
            columns.append('PRIMARY KEY (%s)' % quote_ident(self.primary))
        return 'CREATE TABLE IF NOT EXISTS %s (\n  %s\n)' % (
            quote_ident(self.name), ',\n  '.join(columns),
        )


# ========== 13 张表的注册表（逐字对应 database.ts） ==========

#: `interlude_story` —— 可追溯的 canon + 可变当前状态；原始剧本不内嵌。
STORY = TableSpec(
    name='interlude_story',
    fields={
        'id': _spec('string(255)'),
        'platform': _spec('string(63)'),
        'selfId': _spec('string(63)'),
        'userId': _spec('string(127)'),
        'channelId': _spec('string(127)'),
        'status': _spec('string(16)'),
        'setting': _spec('json'),
        'state': _spec('json'),
        'cursorAt': _spec('timestamp'),
        'createdAt': _spec('timestamp'),
        'updatedAt': _spec('timestamp'),
    },
    primary='id',
    indexes=('platform', 'selfId', 'userId'),
)

#: `interlude_participant` —— 单个私聊账号，以及它在该主剧本内的关系分支。
PARTICIPANT = TableSpec(
    name='interlude_participant',
    fields={
        'id': _spec('string(255)'),
        'storyId': _spec('string(255)'),
        'platform': _spec('string(63)'),
        'selfId': _spec('string(63)'),
        'userId': _spec('string(127)'),
        'channelId': _spec('string(127)'),
        'personId': _spec('string(255)'),
        'displayName': _spec('string(255)'),
        'profile': _spec('text'),
        'relationship': _spec('text'),
        'state': _spec('json'),
        'status': _spec('string(16)'),
        'createdAt': _spec('timestamp'),
        'updatedAt': _spec('timestamp'),
    },
    primary='id',
    indexes=('storyId', 'status', 'personId', 'userId'),
)

#: `interlude_script_entry` —— 事实来源；场景/事实/状态变化都能回溯到这里。
SCRIPT_ENTRY = TableSpec(
    name='interlude_script_entry',
    fields={
        'id': _spec('unsigned autoInc'),
        'storyId': _spec('string(255)'),
        'participantId': _spec('string(255)'),
        'kind': _spec('string(32)'),
        'actor': _spec('string(32)'),
        'content': _spec('text'),
        'occurredAt': _spec('timestamp'),
        'metadata': _spec('json'),
        'embedding': _spec('json'),
        'createdAt': _spec('timestamp'),
    },
    primary='id',
    auto_increment=True,
    indexes=('storyId', 'occurredAt'),
)

#: `interlude_memory` —— 叙事记忆条目。
MEMORY = TableSpec(
    name='interlude_memory',
    fields={
        'id': _spec('unsigned autoInc'),
        'storyId': _spec('string(255)'),
        'participantId': _spec('string(255)'),
        'category': _spec('string(32)'),
        'content': _spec('text'),
        'importance': _spec('double'),
        'status': _spec('string(16)'),
        'sourceEntryId': _spec('unsigned'),
        'createdAt': _spec('timestamp'),
        'updatedAt': _spec('timestamp'),
    },
    primary='id',
    auto_increment=True,
    indexes=('storyId', 'importance'),
)

#: `interlude_intent` —— 未来的可能性，不是已发生的剧情。
INTENT = TableSpec(
    name='interlude_intent',
    fields={
        'id': _spec('unsigned autoInc'),
        'storyId': _spec('string(255)'),
        'participantId': _spec('string(255)'),
        'type': _spec('string(32)'),
        'summary': _spec('text'),
        'notBefore': _spec('timestamp'),
        'status': _spec('string(16)'),
        'payload': _spec('json'),
        'createdAt': _spec('timestamp'),
        'updatedAt': _spec('timestamp'),
    },
    primary='id',
    auto_increment=True,
    indexes=('storyId', 'status', 'notBefore'),
)

#: `interlude_scene` —— 对原始条目的低 token 索引，不替代原文。
SCENE = TableSpec(
    name='interlude_scene',
    fields={
        'id': _spec('unsigned autoInc'),
        'storyId': _spec('string(255)'),
        'status': _spec('string(16)'),
        'startedAt': _spec('timestamp'),
        'endedAt': _spec('timestamp'),
        'hook': _spec('text'),
        'summary': _spec('text'),
        'entryCount': _spec('unsigned'),
        'lastEntryId': _spec('unsigned'),
        'createdAt': _spec('timestamp'),
        'updatedAt': _spec('timestamp'),
    },
    primary='id',
    auto_increment=True,
    indexes=('storyId', 'status', 'startedAt'),
)

#: `interlude_arc` —— 比场景更长，维持关系与长期事件的方向感。
ARC = TableSpec(
    name='interlude_arc',
    fields={
        'id': _spec('unsigned autoInc'),
        'storyId': _spec('string(255)'),
        'status': _spec('string(16)'),
        'title': _spec('string(255)'),
        'summary': _spec('text'),
        'sceneCount': _spec('unsigned'),
        'createdAt': _spec('timestamp'),
        'updatedAt': _spec('timestamp'),
    },
    primary='id',
    auto_increment=True,
    indexes=('storyId', 'status', 'updatedAt'),
)

#: `interlude_fact` —— 长期事实；`knowledge` 是后期新增列，需增量补齐。
FACT = TableSpec(
    name='interlude_fact',
    fields={
        'knowledge': _spec('json'),
        'id': _spec('unsigned autoInc'),
        'storyId': _spec('string(255)'),
        'participantId': _spec('string(255)'),
        'scope': _spec('string(32)'),
        'content': _spec('text'),
        'importance': _spec('double'),
        'confidence': _spec('double'),
        'unresolved': _spec('boolean'),
        'embedding': _spec('json'),
        'status': _spec('string(16)'),
        'sourceEntryIds': _spec('json'),
        'lastSeenAt': _spec('timestamp'),
        'createdAt': _spec('timestamp'),
        'updatedAt': _spec('timestamp'),
    },
    primary='id',
    auto_increment=True,
    indexes=('storyId', 'status', 'importance'),
)

#: `interlude_state_patch` —— 设定变化先以提案存在，过证据与阈值检查后才写入 overlay。
STATE_PATCH = TableSpec(
    name='interlude_state_patch',
    fields={
        'id': _spec('unsigned autoInc'),
        'storyId': _spec('string(255)'),
        'participantId': _spec('string(255)'),
        'target': _spec('string(32)'),
        'path': _spec('string(255)'),
        'proposedValue': _spec('text'),
        'evidence': _spec('text'),
        'confidence': _spec('double'),
        'impact': _spec('string(16)'),
        'status': _spec('string(16)'),
        'sourceEntryIds': _spec('json'),
        'createdAt': _spec('timestamp'),
        'appliedAt': _spec('timestamp'),
    },
    primary='id',
    auto_increment=True,
    indexes=('storyId', 'status', 'confidence'),
)

#: `interlude_web_observation` —— 独立注册的后期新增表，升级时不必重建既有索引。
WEB_OBSERVATION = TableSpec(
    name='interlude_web_observation',
    fields={
        'id': _spec('unsigned autoInc'),
        'storyId': _spec('string(255)'),
        'participantId': _spec('string(255)'),
        'intentId': _spec('unsigned'),
        'mode': _spec('string(16)'),
        'query': _spec('text'),
        'url': _spec('text'),
        'title': _spec('text'),
        'excerpt': _spec('text'),
        'summary': _spec('text'),
        'status': _spec('string(16)'),
        'accessedAt': _spec('timestamp'),
        'createdAt': _spec('timestamp'),
    },
    primary='id',
    auto_increment=True,
    indexes=('storyId', 'status', 'accessedAt'),
    added_later=True,
)

#: `interlude_overlay_snapshot` —— 压缩更早的 overlay 演化，同时保留 state_patch 审计链。
OVERLAY_SNAPSHOT = TableSpec(
    name='interlude_overlay_snapshot',
    fields={
        'id': _spec('unsigned autoInc'),
        'storyId': _spec('string(255)'),
        'participantId': _spec('string(255)'),
        'target': _spec('string(32)'),
        'tier': _spec('string(16)'),
        'periodStart': _spec('timestamp'),
        'periodEnd': _spec('timestamp'),
        'summary': _spec('text'),
        'majorEvents': _spec('json'),
        'sourcePatchIds': _spec('json'),
        'status': _spec('string(16)'),
        'createdAt': _spec('timestamp'),
        'updatedAt': _spec('timestamp'),
    },
    primary='id',
    auto_increment=True,
    indexes=('storyId', 'status', 'target', 'periodEnd'),
    added_later=True,
)

#: `interlude_sticker` —— 本地表情库资产（`assetId` 唯一）。
STICKER = TableSpec(
    name='interlude_sticker',
    fields={
        'id': _spec('unsigned autoInc'),
        'assetId': _spec('string(255)'),
        'filePath': _spec('string(1024)'),
        'group': _spec('string(128)'),
        'mimeType': _spec('string(127)'),
        'animated': _spec('boolean'),
        'size': _spec('unsigned'),
        'hash': _spec('string(64)'),
        'description': _spec('text'),
        'aliases': _spec('json'),
        'status': _spec('string(16)'),
        'embedding': _spec('json'),
        'createdAt': _spec('timestamp'),
        'updatedAt': _spec('timestamp'),
    },
    primary='id',
    auto_increment=True,
    unique=('assetId',),
    indexes=('status', 'group', 'updatedAt'),
    added_later=True,
)

#: `interlude_schedule_preplan` —— 日程预排；主键是 `storyId`（非自增，每剧本一行）。
SCHEDULE_PREPLAN = TableSpec(
    name='interlude_schedule_preplan',
    fields={
        'storyId': _spec('string(255)'),
        'revision': _spec('unsigned'),
        'timezone': _spec('string(127)'),
        'validFrom': _spec('string(10)'),
        'validThrough': _spec('string(10)'),
        'lastReviewedLocalDate': _spec('string(10)'),
        'lastEvidenceEntryId': _spec('unsigned'),
        'reviewReason': _spec('text'),
        'regimes': _spec('json'),
        'exceptions': _spec('json'),
        'materializedDays': _spec('json'),
        'createdAt': _spec('timestamp'),
        'updatedAt': _spec('timestamp'),
    },
    primary='storyId',
    indexes=('validThrough', 'lastReviewedLocalDate'),
    added_later=True,
)

#: 表名 → `TableSpec`。键顺序 = 上游 `registerTables` 的注册顺序。
TABLES: dict[str, TableSpec] = {
    spec.name: spec for spec in (
        STORY, PARTICIPANT, SCRIPT_ENTRY, MEMORY, INTENT, SCENE, ARC, FACT, STATE_PATCH,
        WEB_OBSERVATION, OVERLAY_SNAPSHOT, STICKER, SCHEDULE_PREPLAN,
    )
}


# ========== 辅助函数（供 service.normalize_database_row 使用） ==========


def _require_table(table: str) -> TableSpec:
    try:
        return TABLES[table]
    except KeyError:  # pragma: no cover - 防御性分支。
        raise KeyError(f'unknown table: {table!r}') from None


def json_columns(table: str) -> frozenset[str]:
    """该表里需要 `json.dumps` / `json.loads` 自动转换的列名集合。"""
    return _require_table(table).json_columns


def timestamp_columns(table: str) -> frozenset[str]:
    """该表里需要 datetime ↔ ISO 字符串自动转换的列名集合。"""
    return _require_table(table).timestamp_columns


def primary_key(table: str) -> str:
    """该表的主键列名。"""
    return _require_table(table).primary


def auto_increment(table: str) -> bool:
    """该表的主键是否自增。"""
    return _require_table(table).auto_increment


def table_names() -> list[str]:
    """13 张表的表名（上游 `registerTables` 的注册顺序）。"""
    return list(TABLES)


def quote_ident(name: str) -> str:
    """SQLite 标识符引用（`group` 等是保留字，必须引号包裹）。"""
    return '"%s"' % name.replace('"', '""')


def drop_table_sql(table: str) -> str:
    """SQLite 没有 `DROP INDEX IF EXISTS` 之外的便捷写法，这里只用于工具/测试。"""
    return 'DROP TABLE IF EXISTS %s' % quote_ident(table)


# ========== 全局写队列 ==========

#: 上游注释「SQLite 写入另有全局写队列」——**同步**侧的进程内串行闸门。
#: 它保证即使 `Database.write` 把工作丢进线程池，同一进程内也不会有两个写并发。
_SYNC_WRITE_LOCK = threading.RLock()
#: **异步**侧的全局写队列（`Database.write` 唯一入口）。
#: 注：`asyncio.Lock` 一旦被 `await` 就绑定到当时的事件循环，而 `asyncio.run()`
#: 每次都会新建一个循环；生产环境是常驻单循环，测试/脚本里却会多次
#: `asyncio.run()`。因此这里按事件循环各存一把锁——**同一循环内恒为同一个对象**
#: （这才是"全局写队列"的关键），换循环则换锁，避免
#: `RuntimeError: ... is bound to a different event loop`。
WRITE_LOCK = asyncio.Lock()
#: loop → lock。用 WeakKeyDictionary，循环被回收后锁也随之释放。
_WRITE_LOCKS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = \
    weakref.WeakKeyDictionary()


def write_lock() -> asyncio.Lock:
    """返回当前事件循环的全局异步写锁。

    同一个循环内恒返回**同一个**锁对象；换了事件循环（`asyncio.run()` 第二次
    调用）则换一个新锁。没有运行中的循环时返回模块级 `WRITE_LOCK`（此时谁也
    `await` 不了它，仅用于自省/测试）。
    """
    global WRITE_LOCK
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return WRITE_LOCK
    lock = _WRITE_LOCKS.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _WRITE_LOCKS[loop] = lock
    WRITE_LOCK = lock
    return lock


# ========== 行 ↔ 存储值转换 ==========

Row = dict[str, Any]
T = TypeVar('T')


def _json_dumps(value: Any) -> str:
    """上游 `JSON.stringify`：`ensure_ascii=False` 保留中文原样。"""
    return json.dumps(value, ensure_ascii=False)


def _json_loads(value: Any) -> Any:
    """容错版 `JSON.parse`：解析失败时原样返回（不丢用户数据）。"""
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value).decode('utf-8', 'replace')
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return value


def to_storage_value(spec: ColumnSpec, value: Any) -> Any:
    """Python 值 → sqlite3 可存的值（json 序列化、datetime → ISO 字符串）。

    未知类型的值原样交给 sqlite3（非法类型会抛 `InterfaceError`，不做静默吞掉）。
    """
    if value is None:
        return None
    if spec.is_json:
        return value if isinstance(value, str) else _json_dumps(value)
    if spec.is_timestamp:
        # `iso()` 同时接受 datetime 与 ISO 字符串；解析不了就原样入库（不丢数据）。
        converted = iso(value)
        return value if converted is None else converted
    return value


def from_storage_value(spec: ColumnSpec, value: Any) -> Any:
    """sqlite3 读回的值 → Python 值（json 反序列化、ISO 字符串 → aware datetime）。"""
    if value is None:
        return None
    if spec.is_json:
        return _json_loads(value)
    if spec.is_timestamp:
        parsed = parse_dt(value)
        return value if parsed is None else parsed
    return value


def encode_row(table: str, row: dict[str, Any]) -> Row:
    """把一行 Python 值编码成可写库的形状（只处理该表声明过的列）。"""
    spec = _require_table(table)
    return {
        key: to_storage_value(spec.fields[key], value)
        for key, value in row.items()
        if key in spec.fields
    }


def _reject_unknown_columns(spec: TableSpec, row: dict[str, Any]) -> None:
    """写入前校验列名：拼错列名会静默丢数据，这里直接报错。"""
    unknown = [key for key in row if key not in spec.fields]
    if unknown:
        raise KeyError(f'unknown column(s) {unknown!r} for table {spec.name!r}')


def decode_row(table: str, row: Any) -> Row:
    """把一行库里的值解码成 Python 形状（json / timestamp 列自动还原）。"""
    spec = _require_table(table)
    return {
        key: from_storage_value(spec.fields[key], row[key])
        for key in row.keys()
        if key in spec.fields
    }


# ========== 连接 ==========


class Database:
    """HDS Interlude 的 sqlite3 持久化层。

    上游 `ctx.model` 的等价物：13 张表的建表/查询/写入。
    连接以 `check_same_thread=False` 打开，因为 `Database.write` 会把工作
    丢进线程池；所有写操作都由 `_SYNC_WRITE_LOCK` / `WRITE_LOCK` 串行化。
    """

    def __init__(self, path: str):
        self.path = path
        if path != ':memory:':
            parent = os.path.dirname(os.path.abspath(path))
            if parent:
                os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA journal_mode = WAL')
        self.conn.execute('PRAGMA busy_timeout = 5000')
        #: 单条 sqlite3 连接**不是线程安全的**：`Database.write` 会把工作丢进
        #: `asyncio.to_thread` 的工作线程，而 service 层的 `asyncio.gather` 又会
        #: 从事件循环里并发触发多次读。没有互斥时会出现
        #: `sqlite3.InterfaceError: bad parameter or other API misuse`
        #: （实测 6 路并发读 × 200 轮约有 7.5% 的失败率）。
        #: 因此每个实例持有一把可重入锁，把**每一次**连接操作串行化。
        self._lock = threading.RLock()
        # 写队列与实例锁是同一把（可重入），避免出现两把锁的获取顺序问题。
        self._write_lock = self._lock

    # ---- 生命周期 ----

    def __enter__(self) -> 'Database':
        self.register_tables()
        return self

    def __exit__(self, *_exc_info: Any) -> None:
        self.close()

    def close(self) -> None:
        """关闭连接（幂等）。"""
        conn = getattr(self, 'conn', None)
        if conn is None:
            return
        try:
            conn.close()
        finally:
            self.conn = None  # type: ignore[assignment]

    def commit(self) -> None:
        """提交当前事务（写方法内部都会调，留一个公开入口给 service）。"""
        with self._write_lock:
            self.conn.commit()

    # ---- 建表（上游 `registerTables`） ----

    def list_tables(self) -> list[str]:
        """库里已存在的表名（`sqlite_%` 内部表已排除）。"""
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name",
        ).fetchall()
        return [row[0] for row in rows]

    def columns(self, table: str) -> list[str]:
        """该表的实际列名（按 `PRAGMA table_info` 顺序）。"""
        rows = self.conn.execute(
            'PRAGMA table_info(%s)' % quote_ident(table),
        ).fetchall()
        return [row[1] for row in rows]

    def table_exists(self, table: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,),
        ).fetchone()
        return row is not None

    def indexes(self, table: str) -> list[str]:
        """该表上显式创建的索引名（不含 `sqlite_autoindex_*`）。"""
        rows = self.conn.execute(
            'PRAGMA index_list(%s)' % quote_ident(table),
        ).fetchall()
        return [row[1] for row in rows if not str(row[1]).startswith('sqlite_autoindex_')]

    def index_columns(self, index: str) -> list[str]:
        """索引包含的列名。"""
        rows = self.conn.execute(
            'PRAGMA index_info(%s)' % quote_ident(index),
        ).fetchall()
        return [row[2] for row in rows]

    def register_tables(self) -> list[str]:
        """幂等建表 + 建索引（等价上游 `registerTables`）。

        语义与上游一致：
        1. 已存在的表**不重建**（`CREATE TABLE IF NOT EXISTS`），因此热重载不会
           重建索引、不会丢失数据；
        2. 后加的表（web_observation / overlay_snapshot / sticker /
           schedule_preplan）会被补上；
        3. 后加的列（`interlude_fact.knowledge`、`interlude_script_entry.embedding`）
           用 `ALTER TABLE ADD COLUMN` 增量补齐，原数据保留。

        返回本次**新建**的表名（已存在的不出现）。
        """
        created: list[str] = []
        with self._write_lock:
            for table, spec in TABLES.items():
                if not self.table_exists(table):
                    self.conn.execute(spec.create_table_sql())
                    created.append(table)
                for statement in self._add_missing_columns_sql(spec):
                    self.conn.execute(statement)
                self.conn.commit()
            for table, spec in TABLES.items():
                for column in spec.unique:
                    self.conn.execute(
                        'CREATE UNIQUE INDEX IF NOT EXISTS %s ON %s (%s)' % (
                            quote_ident(self._index_name(table, column)),
                            quote_ident(table), quote_ident(column),
                        ),
                    )
                for column in spec.indexes:
                    self.conn.execute(
                        'CREATE INDEX IF NOT EXISTS %s ON %s (%s)' % (
                            quote_ident(self._index_name(table, column)),
                            quote_ident(table), quote_ident(column),
                        ),
                    )
            self.conn.commit()
        return created

    def _add_missing_columns_sql(self, spec: TableSpec) -> list[str]:
        """构造增量补列语句列表（列已存在时不产生语句）。

        对应上游 `registerTables` 里的两处增量补齐：
        `interlude_fact.knowledge` 与 `interlude_script_entry.embedding`。
        """
        statements: list[str] = []
        existing = set(self.columns(spec.name))
        for name, column in spec.fields.items():
            if name in existing:
                continue
            if name == spec.primary:
                # 主键不可能"后补"：表已存在说明建表时就没把它当主键。
                continue
            # `ALTER TABLE ADD COLUMN` 不允许 UNIQUE / PRIMARY KEY 约束；
            # 唯一性由 `CREATE UNIQUE INDEX` 承担。
            statements.append('ALTER TABLE %s ADD COLUMN %s %s' % (
                quote_ident(spec.name), quote_ident(name), column.type,
            ))
        return statements

    @staticmethod
    def _index_name(table: str, column: str) -> str:
        """索引命名：`<table>_<column>`（与 minato 的可读命名风格一致）。"""
        return f'{table}_{column}'

    # ---- 通用读取 ----

    def get(self, table: str, where: dict[str, Any]) -> Optional[Row]:
        """按条件取第一行；无匹配返回 `None`。"""
        rows = self.all(table, where=where, limit=1)
        return rows[0] if rows else None

    def all(
        self,
        table: str,
        where: Optional[dict[str, Any]] = None,
        order: Optional[str | list[str]] = None,
        limit: Optional[int] = None,
    ) -> list[Row]:
        """按条件查询多行。

        `order` 传列名（默认升序）或 `'occurredAt DESC'` 这种显式片段；
        未指定时按主键升序，保证结果稳定。
        """
        spec = _require_table(table)
        sql, params = self._select_sql(spec, where, order, limit)
        rows = self.conn.execute(sql, params).fetchall()
        return [decode_row(table, row) for row in rows]

    def count(self, table: str, where: Optional[dict[str, Any]] = None) -> int:
        """按条件计数。"""
        spec = _require_table(table)
        clauses, params = self._where_clause(where, spec)
        sql = 'SELECT COUNT(*) FROM %s%s' % (quote_ident(spec.name), clauses)
        row = self.conn.execute(sql, params).fetchone()
        return int(row[0]) if row is not None else 0

    def _select_sql(
        self,
        spec: TableSpec,
        where: Optional[dict[str, Any]],
        order: Optional[str | list[str]],
        limit: Optional[int],
    ) -> tuple[str, list[Any]]:
        clauses, params = self._where_clause(where, spec)
        orders = self._order_clause(spec, order)
        sql = 'SELECT * FROM %s%s%s' % (quote_ident(spec.name), clauses, orders)
        if limit is not None:
            sql += ' LIMIT %d' % max(int(limit), 0)
        return sql, params

    @staticmethod
    def _where_clause(
        where: Optional[dict[str, Any]],
        spec: Optional[TableSpec] = None,
    ) -> tuple[str, list[Any]]:
        """`where` → `WHERE` 子句 + 参数。列名必须属于该表（防注入 + 防拼错列名）。"""
        if not where:
            return '', []
        if spec is not None:
            unknown = [key for key in where if key not in spec.fields]
            if unknown:
                raise KeyError(f'unknown column(s) {unknown!r} for table {spec.name!r}')
        keys = sorted(where)
        body = ' AND '.join(
            '%s IS NULL' % quote_ident(key) if where[key] is None
            else '%s = ?' % quote_ident(key)
            for key in keys
        )
        params = [where[key] for key in keys if where[key] is not None]
        return ' WHERE %s' % body, params

    @staticmethod
    def _order_clause(spec: TableSpec, order: Optional[str | list[str]]) -> str:
        if order is None:
            return ' ORDER BY %s ASC' % quote_ident(spec.primary)
        if isinstance(order, str):
            items = [order]
        else:
            items = list(order)
        parts: list[str] = []
        for item in items:
            text = str(item).strip()
            if not text:
                continue
            head, _, tail = text.partition(' ')
            direction = tail.strip().upper()
            parts.append('%s %s' % (
                quote_ident(head), direction if direction in ('ASC', 'DESC') else 'ASC',
            ))
        return (' ORDER BY %s' % ', '.join(parts)) if parts else ''

    # ---- 写入 ----

    def insert(self, table: str, row: dict[str, Any]) -> Row:
        """插入一行；自增主键留空时自动补上生成的 id，返回写入后的完整行。

        `unsigned autoInc` 表（剧本条目/记忆/意图/…）上：调用方显式给 id 时**先用
        它**（上游 minato 同语义，例如从旧库导入固定 id），留空时用
        `cursor.lastrowid` 回填。返回值一律是**库里真正落下去的那一行**，
        不会因为 id 被忽略而谎报写入结果。
        """
        spec = _require_table(table)
        _reject_unknown_columns(spec, row)
        encoded = encode_row(table, dict(row))
        inserted_id: Any = encoded.get(spec.primary)
        with self._write_lock:
            if spec.auto_increment:
                # 自增主键：`None` / 空串交给 sqlite3 生成。
                if inserted_id is None or inserted_id == '':
                    keys = [key for key in encoded if key != spec.primary]
                else:
                    keys = list(encoded)
            else:
                if inserted_id is None or inserted_id == '':
                    raise ValueError(f'insert into {table!r} requires primary key {spec.primary!r}')
                keys = list(encoded)
            values = [encoded[key] for key in keys]
            sql = 'INSERT INTO %s (%s) VALUES (%s)' % (
                quote_ident(table),
                ', '.join(quote_ident(key) for key in keys),
                ', '.join('?' for _ in keys),
            )
            cursor = self.conn.execute(sql, values)
            if spec.auto_increment and (inserted_id is None or inserted_id == ''):
                inserted_id = cursor.lastrowid
            self.conn.commit()
        stored = dict(encoded)
        stored[spec.primary] = inserted_id
        return self.get(table, {spec.primary: inserted_id}) or stored

    def update(self, table: str, where: dict[str, Any], patch: dict[str, Any]) -> int:
        """按条件更新，返回受影响行数。"""
        spec = _require_table(table)
        _reject_unknown_columns(spec, patch)
        encoded = encode_row(table, dict(patch))
        encoded.pop(spec.primary, None)
        if not encoded:
            return 0
        clauses, params = self._where_clause(where, spec)
        keys = sorted(encoded)
        sql = 'UPDATE %s SET %s%s' % (
            quote_ident(table),
            ', '.join('%s = ?' % quote_ident(key) for key in keys),
            clauses,
        )
        with self._write_lock:
            cursor = self.conn.execute(sql, [encoded[key] for key in keys] + params)
            self.conn.commit()
            return int(cursor.rowcount if cursor.rowcount is not None and cursor.rowcount >= 0 else 0)

    def remove(self, table: str, where: dict[str, Any]) -> int:
        """按条件删除，返回受影响行数。"""
        spec = _require_table(table)
        clauses, params = self._where_clause(where, spec)
        sql = 'DELETE FROM %s%s' % (quote_ident(table), clauses)
        with self._write_lock:
            cursor = self.conn.execute(sql, params)
            self.conn.commit()
            return int(cursor.rowcount if cursor.rowcount is not None and cursor.rowcount >= 0 else 0)

    def upsert(self, table: str, row: dict[str, Any], primary: Optional[str] = None) -> Row:
        """按主键插入或更新，返回写入后的完整行。

        上游 minato 的 `upsert` 语义：`primary` 已存在则整行更新，否则插入。
        `primary` 省略时用 `TABLES` 声明的主键（上游把主键写在 options 里）。
        """
        spec = _require_table(table)
        _reject_unknown_columns(spec, row)
        key = primary or spec.primary
        if key not in spec.fields:
            raise KeyError(f'unknown primary column {key!r} for table {table!r}')
        value = row.get(key)
        if value is not None and self.get(table, {key: value}) is not None:
            patch = {name: item for name, item in row.items() if name != key}
            if patch:
                self.update(table, {key: value}, patch)
            return self.get(table, {key: value}) or dict(row)
        return self.insert(table, dict(row))

    # ---- 异步写队列（上游「全局写队列」） ----

    async def write(self, fn: Callable[['Database'], T]) -> T:
        """在全局写队列里执行 `fn(self)`，把任意写操作串行化。

        所有写都在锁内跑在线程池里，因此不会阻塞事件循环；
        同一个 `Database` 的并发写、以及多个 `Database` 之间的并发写都被排队。
        """
        async with write_lock():
            return await asyncio.to_thread(fn, self)

    # ---- 小工具 ----

    def iter_rows(self, table: str, where: Optional[dict[str, Any]] = None) -> Iterator[Row]:
        """惰性遍历（大表导出/迁移用）。

        `sqlite3` 游标与连接绑定，因此这里先把结果**取完再释放锁**，
        再逐行 yield —— 否则调用方在迭代期间会与其它线程共用游标。
        """
        spec = _require_table(table)
        clauses, params = self._where_clause(where, spec)
        with self._lock:
            cursor = self.conn.execute(
                'SELECT * FROM %s%s' % (quote_ident(spec.name), clauses), params,
            )
            rows = [decode_row(table, row) for row in cursor]
        yield from rows


# --------------------------------------------------------------------------- #
# 全连接操作串行化
# --------------------------------------------------------------------------- #

def _serialized(method: Callable[..., T]) -> Callable[..., T]:
    """把一次连接操作整体放进该实例的可重入锁里（见 `Database.__init__` 的说明）。"""

    @functools.wraps(method)
    def wrapper(self: 'Database', *args: Any, **kwargs: Any) -> T:
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


for _name in (
    'list_tables', 'columns', 'table_exists', 'indexes', 'index_columns',
    'register_tables', 'get', 'all', 'count', 'insert', 'update', 'remove',
    'upsert', 'commit',
):
    setattr(Database, _name, _serialized(getattr(Database, _name)))
del _name

