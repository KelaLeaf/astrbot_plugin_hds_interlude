"""上游 `src/database.ts`（Koishi ORM → 标准库 sqlite3）的移植测试。

对应关系
--------
上游 `database.ts` 是声明式建表，没有自己的 `test/database.test.ts`；本文件的
契约来自 `docs/PORT_PLAN.md` §2/§3 与移植任务书，覆盖：

1. `registerTables` → 13 张表 + 全部索引都能建出来（查 `sqlite_master`）；
2. 「热重载不重复建表」→ `register_tables()` 幂等，表结构不变；
3. 「只补新表/新列」→ 手工造一个缺 `knowledge` 列的 `interlude_fact`，
   `register_tables()` 后用 `ALTER TABLE ADD COLUMN` 增量补齐且原数据不丢；
   `interlude_script_entry.embedding` 同理；
4. sqlite3 类型映射（`unsigned`→INTEGER / `double`→REAL / `timestamp`/`json`→TEXT）；
5. 自增主键、JSON 列往返、时间列往返（datetime → 库内 ISO 字符串 → 读回 aware datetime）；
6. 通用读写（`get` / `all` / `count` / `insert` / `update` / `remove` / `upsert`）；
7. 全局写队列：并发 20 个异步写任务串行化，最终 count 正确且无 `database is locked`。

数据库文件建在本测试目录下（**不放 /tmp**），每个 TestCase 用临时目录，
`tearDown` 里整体删除。

运行：
    cd /home/kela/文档/harness/hds-interlude && python3 -m unittest plugin.tests.test_database -v
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

# 把仓库根加入 sys.path，便于以插件包结构 import（与 tests/test_urge.py 同法）。
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
_PLUGIN_DIR = os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_HERE = os.path.dirname(os.path.abspath(__file__))

htime = importlib.import_module(f"{_PLUGIN_DIR}.core.time")
db_mod = importlib.import_module(f"{_PLUGIN_DIR}.core.database")

Database = db_mod.Database
TABLES = db_mod.TABLES
WRITE_LOCK = db_mod.WRITE_LOCK

UTC = timezone.utc
T0 = datetime(2026, 9, 7, 4, 0, 0, 123000, tzinfo=UTC)

#: 上游 `registerTables` 里的 13 张表（顺序即注册顺序）。
EXPECTED_TABLES = [
    'interlude_story',
    'interlude_participant',
    'interlude_script_entry',
    'interlude_memory',
    'interlude_intent',
    'interlude_scene',
    'interlude_arc',
    'interlude_fact',
    'interlude_state_patch',
    'interlude_web_observation',
    'interlude_overlay_snapshot',
    'interlude_sticker',
    'interlude_schedule_preplan',
]

#: `(表, 主键, 是否自增)` —— 直接抄自 database.ts 的 `primary` / `autoInc`。
EXPECTED_PRIMARY = {
    'interlude_story': ('id', False),
    'interlude_participant': ('id', False),
    'interlude_script_entry': ('id', True),
    'interlude_memory': ('id', True),
    'interlude_intent': ('id', True),
    'interlude_scene': ('id', True),
    'interlude_arc': ('id', True),
    'interlude_fact': ('id', True),
    'interlude_state_patch': ('id', True),
    'interlude_web_observation': ('id', True),
    'interlude_overlay_snapshot': ('id', True),
    'interlude_sticker': ('id', True),
    # 日程预排是每剧本一行，主键是 `storyId`（非自增）。
    'interlude_schedule_preplan': ('storyId', False),
}

#: `indexes` / `unique` 选项 → 期望存在的索引列（顺序照抄 database.ts）。
EXPECTED_INDEXES = {
    'interlude_story': ['platform', 'selfId', 'userId'],
    'interlude_participant': ['storyId', 'status', 'personId', 'userId'],
    'interlude_script_entry': ['storyId', 'occurredAt'],
    'interlude_memory': ['storyId', 'importance'],
    'interlude_intent': ['storyId', 'status', 'notBefore'],
    'interlude_scene': ['storyId', 'status', 'startedAt'],
    'interlude_arc': ['storyId', 'status', 'updatedAt'],
    'interlude_fact': ['storyId', 'status', 'importance'],
    'interlude_state_patch': ['storyId', 'status', 'confidence'],
    'interlude_web_observation': ['storyId', 'status', 'accessedAt'],
    'interlude_overlay_snapshot': ['storyId', 'status', 'target', 'periodEnd'],
    # `unique: ['assetId']` 与 `indexes` 合在一起。
    'interlude_sticker': ['assetId', 'status', 'group', 'updatedAt'],
    'interlude_schedule_preplan': ['validThrough', 'lastReviewedLocalDate'],
}

#: 每张表应有的列（列名与顺序都照抄 database.ts）。
EXPECTED_COLUMNS = {
    'interlude_story': [
        'id', 'platform', 'selfId', 'userId', 'channelId', 'status', 'setting', 'state',
        'cursorAt', 'createdAt', 'updatedAt',
    ],
    'interlude_participant': [
        'id', 'storyId', 'platform', 'selfId', 'userId', 'channelId', 'personId',
        'displayName', 'profile', 'relationship', 'state', 'status', 'createdAt', 'updatedAt',
    ],
    'interlude_script_entry': [
        'id', 'storyId', 'participantId', 'kind', 'actor', 'content', 'occurredAt',
        'metadata', 'embedding', 'createdAt',
    ],
    'interlude_memory': [
        'id', 'storyId', 'participantId', 'category', 'content', 'importance', 'status',
        'sourceEntryId', 'createdAt', 'updatedAt',
    ],
    'interlude_intent': [
        'id', 'storyId', 'participantId', 'type', 'summary', 'notBefore', 'status', 'payload',
        'createdAt', 'updatedAt',
    ],
    'interlude_scene': [
        'id', 'storyId', 'status', 'startedAt', 'endedAt', 'hook', 'summary', 'entryCount',
        'lastEntryId', 'createdAt', 'updatedAt',
    ],
    'interlude_arc': [
        'id', 'storyId', 'status', 'title', 'summary', 'sceneCount', 'createdAt', 'updatedAt',
    ],
    'interlude_fact': [
        'knowledge', 'id', 'storyId', 'participantId', 'scope', 'content', 'importance',
        'confidence', 'unresolved', 'embedding', 'status', 'sourceEntryIds', 'lastSeenAt',
        'createdAt', 'updatedAt',
    ],
    'interlude_state_patch': [
        'id', 'storyId', 'participantId', 'target', 'path', 'proposedValue', 'evidence',
        'confidence', 'impact', 'status', 'sourceEntryIds', 'createdAt', 'appliedAt',
    ],
    'interlude_web_observation': [
        'id', 'storyId', 'participantId', 'intentId', 'mode', 'query', 'url', 'title',
        'excerpt', 'summary', 'status', 'accessedAt', 'createdAt',
    ],
    'interlude_overlay_snapshot': [
        'id', 'storyId', 'participantId', 'target', 'tier', 'periodStart', 'periodEnd',
        'summary', 'majorEvents', 'sourcePatchIds', 'status', 'createdAt', 'updatedAt',
    ],
    'interlude_sticker': [
        'id', 'assetId', 'filePath', 'group', 'mimeType', 'animated', 'size', 'hash',
        'description', 'aliases', 'status', 'embedding', 'createdAt', 'updatedAt',
    ],
    'interlude_schedule_preplan': [
        'storyId', 'revision', 'timezone', 'validFrom', 'validThrough',
        'lastReviewedLocalDate', 'lastEvidenceEntryId', 'reviewReason', 'regimes',
        'exceptions', 'materializedDays', 'createdAt', 'updatedAt',
    ],
}

#: sqlite3 类型映射断言（`unsigned`/`double`/`boolean`/`json`/`timestamp`）。
#: `None` 表示该列是主键（`INTEGER PRIMARY KEY AUTOINCREMENT`，`type` 仍是 INTEGER）。
EXPECTED_TYPES = {
    'interlude_script_entry': {'id': 'INTEGER', 'storyId': 'TEXT', 'occurredAt': 'TEXT',
                               'metadata': 'TEXT', 'embedding': 'TEXT'},
    'interlude_memory': {'importance': 'REAL', 'sourceEntryId': 'INTEGER', 'content': 'TEXT'},
    'interlude_fact': {'knowledge': 'TEXT', 'importance': 'REAL', 'confidence': 'REAL',
                       'unresolved': 'INTEGER', 'embedding': 'TEXT', 'sourceEntryIds': 'TEXT',
                       'lastSeenAt': 'TEXT'},
    'interlude_scene': {'entryCount': 'INTEGER', 'lastEntryId': 'INTEGER', 'hook': 'TEXT'},
    'interlude_sticker': {'animated': 'INTEGER', 'size': 'INTEGER', 'aliases': 'TEXT'},
    'interlude_schedule_preplan': {'revision': 'INTEGER', 'validFrom': 'TEXT',
                                   'regimes': 'TEXT', 'exceptions': 'TEXT',
                                   'materializedDays': 'TEXT'},
}


class _DatabaseTestCase(unittest.TestCase):
    """每个用例一个全新的数据库文件（建在 `plugin/tests/` 下的临时目录里）。"""

    def setUp(self) -> None:
        self._dir = tempfile.mkdtemp(prefix='hdsi_db_', dir=_HERE)
        self.path = os.path.join(self._dir, 'interlude.sqlite3')
        self.db = Database(self.path)

    def tearDown(self) -> None:
        try:
            self.db.close()
        finally:
            shutil.rmtree(self._dir, ignore_errors=True)

    # ---- 原始 SQL 探针（绕过封装，直接看库里真正建了什么） ----

    def _raw(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def _table_names(self) -> list[str]:
        conn = self._raw()
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name",
            ).fetchall()
        finally:
            conn.close()
        return [row[0] for row in rows]

    def _master_sql(self) -> dict[str, str]:
        conn = self._raw()
        try:
            rows = conn.execute('SELECT name, sql FROM sqlite_master').fetchall()
        finally:
            conn.close()
        return {row[0]: (row[1] or '') for row in rows}

    def _table_info(self, table: str) -> dict[str, dict[str, object]]:
        conn = self._raw()
        try:
            rows = conn.execute('PRAGMA table_info("%s")' % table).fetchall()
        finally:
            conn.close()
        return {
            row[1]: {'cid': row[0], 'type': (row[2] or '').upper(), 'notnull': row[3],
                     'default': row[4], 'pk': row[5]}
            for row in rows
        }

    def _index_map(self, table: str) -> dict[str, list[str]]:
        """索引名 → 索引列（只含显式索引，排除 sqlite_autoindex_*）。"""
        conn = self._raw()
        try:
            names = [
                row[1] for row in conn.execute('PRAGMA index_list("%s")' % table).fetchall()
                if not str(row[1]).startswith('sqlite_autoindex_')
            ]
            return {
                name: [row[2] for row in conn.execute('PRAGMA index_info("%s")' % name).fetchall()]
                for name in names
            }
        finally:
            conn.close()

    def _index_columns(self, table: str) -> list[str]:
        return [col for cols in self._index_map(table).values() for col in cols]

    def _raw_cell(self, sql: str, params: tuple = ()):
        conn = self._raw()
        try:
            row = conn.execute(sql, params).fetchone()
        finally:
            conn.close()
        return None if row is None else row[0]


# ========== 1. 建表 / 索引 / 类型映射 ==========


class RegisterTablesTests(_DatabaseTestCase):
    """对应上游 `registerTables(ctx)`：13 张表一次性建成。"""

    def test_all_thirteen_tables_created(self):
        created = self.db.register_tables()
        self.assertEqual(sorted(created), sorted(EXPECTED_TABLES))
        self.assertEqual(sorted(self._table_names()), sorted(EXPECTED_TABLES))
        # `TABLES` 注册表本身也是 13 项。
        self.assertEqual(len(TABLES), 13)
        self.assertEqual(db_mod.table_names(), EXPECTED_TABLES)

    def test_columns_match_upstream_declaration(self):
        self.db.register_tables()
        for table, expected in EXPECTED_COLUMNS.items():
            with self.subTest(table=table):
                self.assertEqual(list(self._table_info(table)), expected)

    def test_primary_key_declaration(self):
        self.db.register_tables()
        for table, (primary, auto_inc) in EXPECTED_PRIMARY.items():
            with self.subTest(table=table):
                info = self._table_info(table)
                pks = [name for name, meta in info.items() if meta['pk']]
                self.assertEqual(pks, [primary])
                if auto_inc:
                    # `INTEGER PRIMARY KEY AUTOINCREMENT` 与 `INTEGER PRIMARY KEY`
                    # 在 `PRAGMA table_info` 里都是 type INTEGER + pk=1，
                    # 但后者可从建表 SQL 里区分。
                    self.assertIn('AUTOINCREMENT', self._master_sql()[table])

    def test_indexes_created_in_sqlite_master(self):
        self.db.register_tables()
        master = self._master_sql()
        for table, columns in EXPECTED_INDEXES.items():
            with self.subTest(table=table):
                index_map = self._index_map(table)
                self.assertTrue(index_map, f'{table} 没有任何索引')
                for name in index_map:
                    self.assertIn(name, master, f'{name} 没出现在 sqlite_master')
                # 每个声明的列都被某个索引用到了（且是精确匹配，不是前缀）。
                used = [cols for cols in index_map.values()]
                for column in columns:
                    self.assertIn([column], used, f'{table}.{column} 缺索引')

    def test_sticker_asset_id_is_unique(self):
        """上游 `unique: ['assetId']`。"""
        self.db.register_tables()
        conn = self._raw()
        try:
            rows = conn.execute('PRAGMA index_list("interlude_sticker")').fetchall()
        finally:
            conn.close()
        unique_indexes = [
            row[1] for row in rows
            if row[2] == 1 and not str(row[1]).startswith('sqlite_autoindex_')
        ]
        self.assertTrue(unique_indexes, 'assetId 的唯一索引没建出来')
        self.assertIn(['assetId'], [self._index_map('interlude_sticker')[name] for name in unique_indexes])

    def test_unsigned_double_boolean_json_timestamp_type_mapping(self):
        self.db.register_tables()
        for table, columns in EXPECTED_TYPES.items():
            info = self._table_info(table)
            for column, expected in columns.items():
                with self.subTest(table=table, column=column):
                    self.assertEqual(info[column]['type'], expected)

    def test_auto_increment_starts_at_one_and_increases(self):
        self.db.register_tables()
        first = self.db.insert('interlude_script_entry', {'storyId': 's1', 'content': 'a'})
        second = self.db.insert('interlude_script_entry', {'storyId': 's1', 'content': 'b'})
        self.assertEqual(first['id'], 1)
        self.assertEqual(second['id'], 2)

    def test_explicit_id_is_respected(self):
        """调用方显式给 id 时不应被自增值覆盖（上游 minato 同语义）。"""
        self.db.register_tables()
        row = self.db.insert('interlude_script_entry', {'id': 42, 'storyId': 's1', 'content': 'a'})
        self.assertEqual(row['id'], 42)
        self.assertEqual(self.db.get('interlude_script_entry', {'id': 42})['content'], 'a')

    def test_register_tables_is_idempotent(self):
        """上游「热重载不重复建表」：二次调用不报错、结构不变、数据不丢。"""
        first_created = self.db.register_tables()
        self.db.insert('interlude_story', {
            'id': 'story-1', 'platform': 'qq', 'status': 'active',
            'setting': {'character': {'name': '凌梦'}}, 'state': {},
            'cursorAt': T0, 'createdAt': T0, 'updatedAt': T0,
        })
        schema_before = {name: sql for name, sql in self._master_sql().items() if name in EXPECTED_TABLES}

        for _ in range(3):
            second_created = self.db.register_tables()
            self.assertEqual(second_created, [], '已存在的表不应被再次创建')

        self.assertEqual(first_created and sorted(first_created), sorted(EXPECTED_TABLES))
        self.assertEqual(
            {name: sql for name, sql in self._master_sql().items() if name in EXPECTED_TABLES},
            schema_before, '二次注册不得改变已存在表的结构',
        )
        self.assertEqual(self.db.count('interlude_story'), 1)
        self.assertEqual(self.db.get('interlude_story', {'id': 'story-1'})['platform'], 'qq')

    def test_register_tables_adds_only_missing_tables(self):
        """模拟旧安装：只有最早的一批表，后加的四张表应被补上。"""
        self.db.register_tables()
        for table in ('interlude_web_observation', 'interlude_overlay_snapshot',
                      'interlude_sticker', 'interlude_schedule_preplan'):
            self.db.conn.execute('DROP TABLE IF EXISTS "%s"' % table)
        self.db.conn.commit()

        created = self.db.register_tables()
        self.assertEqual(
            sorted(created),
            ['interlude_overlay_snapshot', 'interlude_schedule_preplan',
             'interlude_sticker', 'interlude_web_observation'],
        )
        self.assertEqual(sorted(self._table_names()), sorted(EXPECTED_TABLES))


class IncrementalColumnTests(_DatabaseTestCase):
    """上游 `registerTables` 的「只补新列」分支（`database.ts:33-43`）。"""

    def _make_legacy_fact_table(self) -> None:
        """手工造一个**没有** `knowledge` 列的旧 `interlude_fact`。"""
        self.db.conn.execute(
            'CREATE TABLE interlude_fact (\n'
            '  "id" INTEGER,\n'
            '  "storyId" TEXT,\n'
            '  "scope" TEXT,\n'
            '  "content" TEXT,\n'
            '  "importance" REAL,\n'
            '  "status" TEXT,\n'
            '  PRIMARY KEY ("id" AUTOINCREMENT)\n'
            ')',
        )
        self.db.conn.execute(
            'INSERT INTO interlude_fact (storyId, scope, content, importance, status) '
            "VALUES ('story-1', 'character', '旧数据', 0.5, 'active')",
        )
        self.db.conn.commit()

    def test_missing_knowledge_column_is_added_and_data_survives(self):
        self._make_legacy_fact_table()
        self.assertNotIn('knowledge', self._table_info('interlude_fact'))

        self.db.register_tables()

        info = self._table_info('interlude_fact')
        self.assertIn('knowledge', info)
        self.assertEqual(info['knowledge']['type'], 'TEXT')
        # 原有数据不丢。
        rows = self.db.all('interlude_fact')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['content'], '旧数据')
        self.assertIsNone(rows[0]['knowledge'])
        # 补齐后可以正常写 json。
        self.db.update('interlude_fact', {'id': rows[0]['id']},
                       {'knowledge': {'kind': 'testimony', 'entryIds': [3]}})
        self.assertEqual(
            self.db.get('interlude_fact', {'id': rows[0]['id']})['knowledge'],
            {'kind': 'testimony', 'entryIds': [3]},
        )

    def test_missing_script_entry_embedding_column_is_added(self):
        """上游 `registerScriptEntryEmbedding`：`interlude_script_entry.embedding`。"""
        self.db.conn.execute(
            'CREATE TABLE interlude_script_entry (\n'
            '  "id" INTEGER,\n'
            '  "storyId" TEXT,\n'
            '  "content" TEXT,\n'
            '  PRIMARY KEY ("id" AUTOINCREMENT)\n'
            ')',
        )
        self.db.conn.execute(
            "INSERT INTO interlude_script_entry (storyId, content) VALUES ('story-1', '原文')",
        )
        self.db.conn.commit()
        self.assertNotIn('embedding', self._table_info('interlude_script_entry'))

        self.db.register_tables()

        self.assertIn('embedding', self._table_info('interlude_script_entry'))
        rows = self.db.all('interlude_script_entry')
        self.assertEqual(rows[0]['content'], '原文')
        self.assertIsNone(rows[0]['embedding'])
        self.db.update('interlude_script_entry', {'id': rows[0]['id']}, {'embedding': [0.25, -1.5]})
        self.assertEqual(
            self.db.get('interlude_script_entry', {'id': rows[0]['id']})['embedding'],
            [0.25, -1.5],
        )

    def test_full_legacy_database_upgrades_in_place(self):
        """完整的旧库（缺 sticker / preplan / 两个新列）升级后 13 张表齐全。"""
        self._make_legacy_fact_table()
        self.db.conn.execute(
            'CREATE TABLE interlude_story (\n'
            '  "id" TEXT, "platform" TEXT, "setting" TEXT,\n'
            '  PRIMARY KEY ("id")\n'
            ')',
        )
        self.db.conn.execute(
            "INSERT INTO interlude_story (id, platform, setting) VALUES ('s', 'qq', '{}')",
        )
        self.db.conn.commit()

        self.db.register_tables()

        self.assertEqual(sorted(self._table_names()), sorted(EXPECTED_TABLES))
        self.assertEqual(self.db.get('interlude_story', {'id': 's'})['platform'], 'qq')
        self.assertEqual(self.db.count('interlude_fact'), 1)


# ========== 2. 通用读写 / 类型往返 ==========


class RowAccessTests(_DatabaseTestCase):

    def setUp(self) -> None:
        super().setUp()
        self.db.register_tables()

    def _story(self, story_id: str, **overrides) -> dict:
        row = {
            'id': story_id, 'platform': 'qq', 'selfId': '10000', 'userId': '2106758890',
            'channelId': 'private:2106758890', 'status': 'active',
            'setting': {'character': {'name': '凌梦'}, 'timezone': 'Asia/Shanghai'},
            'state': {'schema_version': 1},
            'cursorAt': T0, 'createdAt': T0, 'updatedAt': T0,
        }
        row.update(overrides)
        return row

    def test_insert_returns_full_row_and_get_reads_it_back(self):
        inserted = self.db.insert('interlude_story', self._story('story-1'))
        self.assertEqual(inserted['id'], 'story-1')
        self.assertEqual(inserted['platform'], 'qq')
        fetched = self.db.get('interlude_story', {'id': 'story-1'})
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched['channelId'], 'private:2106758890')
        self.assertIsNone(self.db.get('interlude_story', {'id': 'missing'}))

    def test_all_where_order_limit(self):
        for index in range(1, 6):
            self.db.insert('interlude_script_entry', {
                'storyId': 's1', 'content': f'entry-{index}',
                'occurredAt': T0 + timedelta(minutes=index), 'createdAt': T0,
            })
        self.db.insert('interlude_script_entry', {
            'storyId': 's2', 'content': 'other', 'occurredAt': T0, 'createdAt': T0,
        })

        self.assertEqual(self.db.count('interlude_script_entry'), 6)
        self.assertEqual(self.db.count('interlude_script_entry', {'storyId': 's1'}), 5)
        self.assertEqual(self.db.count('interlude_script_entry', {'storyId': 'nope'}), 0)

        # 无 order 时按主键升序，结果稳定。
        self.assertEqual(
            [row['id'] for row in self.db.all('interlude_script_entry', {'storyId': 's1'})],
            [1, 2, 3, 4, 5],
        )
        # 显式 DESC + limit。
        newest = self.db.all('interlude_script_entry', {'storyId': 's1'},
                             order='occurredAt DESC', limit=2)
        self.assertEqual([row['content'] for row in newest], ['entry-5', 'entry-4'])
        # order 也接受列表。
        self.assertEqual(
            [row['id'] for row in self.db.all('interlude_script_entry', {'storyId': 's1'},
                                              order=['occurredAt ASC'], limit=1)],
            [1],
        )

    def test_all_without_where_returns_everything(self):
        self.db.insert('interlude_arc', {'storyId': 's1', 'status': 'active', 'title': 'A'})
        self.db.insert('interlude_arc', {'storyId': 's1', 'status': 'closed', 'title': 'B'})
        self.assertEqual(len(self.db.all('interlude_arc')), 2)
        self.assertEqual(self.db.count('interlude_arc'), 2)

    def test_update_returns_affected_rows(self):
        self.db.insert('interlude_story', self._story('story-1'))
        self.db.insert('interlude_story', self._story('story-2'))

        affected = self.db.update('interlude_story', {'id': 'story-1'}, {'status': 'paused'})
        self.assertEqual(affected, 1)
        self.assertEqual(self.db.get('interlude_story', {'id': 'story-1'})['status'], 'paused')
        self.assertEqual(self.db.get('interlude_story', {'id': 'story-2'})['status'], 'active')
        # 主键不会被 patch 改掉。
        self.db.update('interlude_story', {'id': 'story-1'}, {'id': 'hacked', 'status': 'archived'})
        self.assertIsNone(self.db.get('interlude_story', {'id': 'hacked'}))
        self.assertEqual(self.db.get('interlude_story', {'id': 'story-1'})['status'], 'archived')
        # 无匹配行 → 0。
        self.assertEqual(self.db.update('interlude_story', {'id': 'nope'}, {'status': 'x'}), 0)

    def test_remove_returns_affected_rows(self):
        for index in range(3):
            self.db.insert('interlude_memory', {'storyId': 's1', 'content': f'm{index}'})
        self.assertEqual(self.db.remove('interlude_memory', {'storyId': 's1'}), 3)
        self.assertEqual(self.db.count('interlude_memory'), 0)
        self.assertEqual(self.db.remove('interlude_memory', {'storyId': 's1'}), 0)

    def test_upsert_inserts_then_updates_by_primary(self):
        row = self._story('story-1')
        created = self.db.upsert('interlude_story', row, 'id')
        self.assertEqual(created['status'], 'active')
        self.assertEqual(self.db.count('interlude_story'), 1)

        updated = self.db.upsert('interlude_story', self._story('story-1', status='paused'), 'id')
        self.assertEqual(updated['status'], 'paused')
        self.assertEqual(self.db.count('interlude_story'), 1)

        self.db.upsert('interlude_story', self._story('story-2'), 'id')
        self.assertEqual(self.db.count('interlude_story'), 2)

    def test_upsert_uses_table_primary_by_default(self):
        """`primary` 省略时用 TABLES 声明的主键（story=id / schedule_preplan=storyId）。"""
        self.db.upsert('interlude_scene', {'storyId': 's1', 'status': 'active', 'hook': '开场'})
        self.db.upsert('interlude_scene', {'id': 1, 'storyId': 's1', 'status': 'closed', 'hook': '收场'})
        self.assertEqual(self.db.count('interlude_scene'), 1)
        self.assertEqual(self.db.get('interlude_scene', {'id': 1})['hook'], '收场')

        self.db.upsert('interlude_schedule_preplan', {
            'storyId': 's1', 'revision': 1, 'timezone': 'Asia/Shanghai',
            'validFrom': '2026-09-07', 'validThrough': '2026-09-13',
            'lastReviewedLocalDate': '2026-09-07', 'lastEvidenceEntryId': 0,
            'reviewReason': '首次生成', 'regimes': [], 'exceptions': [],
            'materializedDays': [], 'createdAt': T0, 'updatedAt': T0,
        })
        self.db.upsert('interlude_schedule_preplan', {
            'storyId': 's1', 'revision': 2, 'updatedAt': T0,
        })
        self.assertEqual(self.db.count('interlude_schedule_preplan'), 1)
        self.assertEqual(self.db.get('interlude_schedule_preplan', {'storyId': 's1'})['revision'], 2)

    def test_unknown_table_and_unknown_column_are_rejected(self):
        with self.assertRaises(KeyError):
            self.db.get('interlude_nope', {'id': 1})
        with self.assertRaises(KeyError):
            self.db.all('interlude_nope')
        with self.assertRaises(KeyError):
            db_mod.json_columns('interlude_nope')
        # 拼错列名不能静默丢数据。
        with self.assertRaises(KeyError):
            self.db.insert('interlude_story', {'id': 'x', 'storyID': 'typo'})
        with self.assertRaises(KeyError):
            self.db.update('interlude_story', {'id': 'x'}, {'statsu': 'typo'})
        with self.assertRaises(KeyError):
            self.db.count('interlude_story', {'id; DROP TABLE interlude_story': 1})

    def test_null_where_matches_is_null(self):
        self.db.insert('interlude_scene', {'storyId': 's1', 'status': 'active'})
        self.db.insert('interlude_scene', {'storyId': 's1', 'status': 'closed', 'endedAt': T0})
        self.assertEqual(self.db.count('interlude_scene', {'endedAt': None}), 1)
        self.assertEqual(len(self.db.all('interlude_scene', {'endedAt': None})), 1)


class TypeRoundTripTests(_DatabaseTestCase):

    def setUp(self) -> None:
        super().setUp()
        self.db.register_tables()

    def test_timestamp_round_trip_is_aware_utc(self):
        self.db.insert('interlude_story', {
            'id': 'story-1', 'platform': 'qq', 'setting': {}, 'state': {},
            'cursorAt': T0, 'createdAt': T0, 'updatedAt': T0,
        })
        # 库里必须是 ISO 字符串（与上游 `Date#toISOString()` 同形）。
        raw = self._raw_cell('SELECT cursorAt FROM interlude_story WHERE id = ?', ('story-1',))
        self.assertIsInstance(raw, str)
        self.assertEqual(raw, '2026-09-07T04:00:00.123Z')
        self.assertEqual(raw, htime.iso(T0))

        row = self.db.get('interlude_story', {'id': 'story-1'})
        self.assertIsInstance(row['cursorAt'], datetime)
        self.assertIsNotNone(row['cursorAt'].tzinfo)
        self.assertEqual(row['cursorAt'].utcoffset(), timedelta(0))
        self.assertEqual(row['cursorAt'], T0)
        self.assertEqual(htime.dt_ms(row['cursorAt']), htime.dt_ms(T0))

    def test_timestamp_accepts_iso_string_and_naive_datetime(self):
        self.db.insert('interlude_story', {
            'id': 'story-1', 'platform': 'qq', 'setting': {}, 'state': {},
            'cursorAt': '2026-09-07T04:00:00.123Z',
            'createdAt': '2026-09-07T04:00:00.123Z',
            'updatedAt': T0.replace(tzinfo=None),  # naive 按 UTC 解释
        })
        row = self.db.get('interlude_story', {'id': 'story-1'})
        self.assertEqual(row['cursorAt'], T0)
        self.assertEqual(row['updatedAt'], T0)

    def test_null_timestamp_stays_none(self):
        self.db.insert('interlude_scene', {'storyId': 's1', 'status': 'active'})
        row = self.db.get('interlude_scene', {'storyId': 's1'})
        self.assertIsNone(row['endedAt'])
        self.assertIsNone(row['createdAt'])

    def test_json_columns_round_trip_with_non_ascii(self):
        setting = {
            'character': {'name': '凌梦', 'profile': '白发的猫娘，140 岁。'},
            'relationship': '恋人',
            'tags': ['中文', 'emoji 🐾'],
        }
        state = {'schema_version': 1, 'setting_overlay': {'character_traits': ['傲娇']}}
        self.db.insert('interlude_story', {
            'id': 'story-1', 'platform': 'qq', 'setting': setting, 'state': state,
            'cursorAt': T0, 'createdAt': T0, 'updatedAt': T0,
        })
        # 库里是 JSON 文本，且中文不被转义（ensure_ascii=False）。
        raw = self._raw_cell('SELECT setting FROM interlude_story WHERE id = ?', ('story-1',))
        self.assertEqual(json.loads(raw), setting)
        self.assertIn('凌梦', raw)
        self.assertNotIn('\\u', raw)

        row = self.db.get('interlude_story', {'id': 'story-1'})
        self.assertDictEqual(row['setting'], setting)
        self.assertDictEqual(row['state'], state)

    def test_json_list_columns_round_trip(self):
        self.db.insert('interlude_fact', {
            'storyId': 's1', 'scope': 'character', 'content': '她喜欢猫。',
            'importance': 0.8, 'confidence': 0.6, 'unresolved': False,
            'embedding': [0.1, 0.2, 0.3], 'status': 'active',
            'sourceEntryIds': [11, 12], 'lastSeenAt': T0, 'createdAt': T0, 'updatedAt': T0,
        })
        row = self.db.get('interlude_fact', {'storyId': 's1'})
        self.assertEqual(row['sourceEntryIds'], [11, 12])
        self.assertEqual(row['embedding'], [0.1, 0.2, 0.3])
        self.assertEqual(row['importance'], 0.8)
        self.assertEqual(row['confidence'], 0.6)
        # boolean → INTEGER 0/1。
        self.assertEqual(row['unresolved'], 0)
        self.assertEqual(
            self._raw_cell('SELECT unresolved FROM interlude_fact WHERE storyId = ?', ('s1',)), 0,
        )

    def test_json_invalid_text_does_not_crash(self):
        """历史脏数据（非法 JSON 文本）不能被读崩；原样返回交给上层处理。"""
        self.db.insert('interlude_story', {
            'id': 'story-1', 'platform': 'qq', 'setting': {}, 'state': {},
            'cursorAt': T0, 'createdAt': T0, 'updatedAt': T0,
        })
        self.db.conn.execute(
            "UPDATE interlude_story SET setting = ? WHERE id = ?", ('{not json', 'story-1'),
        )
        self.db.conn.commit()
        self.assertEqual(self.db.get('interlude_story', {'id': 'story-1'})['setting'], '{not json')

    def test_column_helpers_for_normalize_database_row(self):
        """`service.normalize_database_row` 只依赖这两个辅助函数。"""
        self.assertEqual(
            db_mod.json_columns('interlude_script_entry'), frozenset({'metadata', 'embedding'}),
        )
        self.assertEqual(
            db_mod.timestamp_columns('interlude_script_entry'),
            frozenset({'occurredAt', 'createdAt'}),
        )
        self.assertEqual(
            db_mod.json_columns('interlude_story'), frozenset({'setting', 'state'}),
        )
        self.assertEqual(
            db_mod.timestamp_columns('interlude_story'),
            frozenset({'cursorAt', 'createdAt', 'updatedAt'}),
        )
        self.assertEqual(
            db_mod.json_columns('interlude_overlay_snapshot'),
            frozenset({'majorEvents', 'sourcePatchIds'}),
        )
        self.assertEqual(
            db_mod.json_columns('interlude_fact'),
            frozenset({'knowledge', 'embedding', 'sourceEntryIds'}),
        )
        # 没有 json / timestamp 列的表返回空集，而不是报错。
        self.assertEqual(db_mod.json_columns('interlude_schedule_preplan') &
                         db_mod.timestamp_columns('interlude_schedule_preplan'), frozenset())
        self.assertEqual(db_mod.primary_key('interlude_schedule_preplan'), 'storyId')
        self.assertTrue(db_mod.auto_increment('interlude_script_entry'))
        self.assertFalse(db_mod.auto_increment('interlude_story'))

    def test_encode_decode_row_helpers(self):
        encoded = db_mod.encode_row('interlude_scene', {
            'id': 1, 'occurredAt': T0, 'payload': {'a': 1}, 'startedAt': T0,
        })
        # 未声明的列被丢弃（`encode_row` 只认识该表的列）。
        self.assertNotIn('payload', encoded)
        self.assertEqual(encoded['id'], 1)
        self.assertEqual(encoded['startedAt'], '2026-09-07T04:00:00.123Z')

        conn = sqlite3.connect(':memory:')
        conn.row_factory = sqlite3.Row
        try:
            conn.execute(self.db.conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'interlude_scene'",
            ).fetchone()[0])
            conn.execute(
                'INSERT INTO interlude_scene (id, startedAt, endedAt) VALUES (?, ?, ?)',
                (1, encoded['startedAt'], None),
            )
            raw = conn.execute('SELECT * FROM interlude_scene').fetchone()
            decoded = db_mod.decode_row('interlude_scene', raw)
        finally:
            conn.close()
        self.assertEqual(decoded['startedAt'], T0)
        self.assertIsNone(decoded['endedAt'])

    def test_iter_rows(self):
        for index in range(3):
            self.db.insert('interlude_memory', {'storyId': 's1', 'content': f'm{index}'})
        self.assertEqual(
            [row['content'] for row in self.db.iter_rows('interlude_memory', {'storyId': 's1'})],
            ['m0', 'm1', 'm2'],
        )


# ========== 3. 全局写队列 ==========


class WriteQueueTests(_DatabaseTestCase):
    """上游「SQLite 写入另有全局写队列」→ `WRITE_LOCK` + `Database.write`。"""

    def setUp(self) -> None:
        super().setUp()
        self.db.register_tables()

    def test_write_lock_is_a_module_level_asyncio_lock(self):
        self.assertIsInstance(WRITE_LOCK, asyncio.Lock)
        self.assertIsInstance(db_mod.write_lock(), asyncio.Lock)
        # 同一个事件循环内必须是**同一个**锁对象（这才叫全局写队列）；
        # 换循环（多次 `asyncio.run()`）才会换锁，见 database.write_lock 的说明。
        first = db_mod.write_lock()
        second = db_mod.write_lock()
        self.assertIs(first, second)
        self.assertIs(db_mod.WRITE_LOCK, first)

    def test_write_runs_function_and_returns_value(self):
        async def main():
            stored = await self.db.write(
                lambda database: database.insert('interlude_memory', {'storyId': 's1', 'content': 'x'}),
            )
            return stored, await self.db.write(lambda database: database.count('interlude_memory'))

        stored, total = asyncio.run(main())
        self.assertEqual(stored['content'], 'x')
        self.assertEqual(total, 1)

    def test_twenty_concurrent_writes_are_serialized(self):
        """并发 20 个写任务：最终 count 正确、id 连续、不出现 `database is locked`。"""
        total = 20

        async def main():
            async def writer(index: int):
                def run(database: Database):
                    return database.insert('interlude_script_entry', {
                        'storyId': 's1', 'content': f'entry-{index}',
                        'occurredAt': T0, 'createdAt': T0,
                    })

                return await self.db.write(run)

            return await asyncio.gather(*(writer(index) for index in range(total)))

        rows = asyncio.run(main())
        self.assertEqual(len(rows), total)
        self.assertEqual(self.db.count('interlude_script_entry'), total)
        self.assertEqual(sorted(row['id'] for row in rows), list(range(1, total + 1)))
        self.assertEqual(
            sorted(row['content'] for row in self.db.all('interlude_script_entry')),
            sorted(f'entry-{index}' for index in range(total)),
        )

    def test_concurrent_mixed_writes_stay_consistent(self):
        """交错 insert/update/remove 也不丢行、不报锁错误。"""

        async def main():
            async def job(index: int):
                def run(database: Database):
                    if index % 3 == 0:
                        return database.insert('interlude_story', {
                            'id': f'story-{index}', 'platform': 'qq', 'status': 'active',
                            'setting': {}, 'state': {}, 'cursorAt': T0,
                            'createdAt': T0, 'updatedAt': T0,
                        })

                    if index % 3 == 1:
                        return database.update('interlude_story', {'id': 'story-0'},
                                               {'status': 'paused'})

                    return database.count('interlude_story')

                return await self.db.write(run)

            return await asyncio.gather(*(job(index) for index in range(12)))

        asyncio.run(main())
        self.assertEqual(self.db.count('interlude_story'), 4)
        self.assertEqual(self.db.get('interlude_story', {'id': 'story-0'})['status'], 'paused')

    def test_write_failure_releases_the_lock(self):
        """写任务抛异常后锁必须释放，后续写仍能进行。"""
        async def main():
            def boom(database: Database):
                raise RuntimeError('boom')

            with self.assertRaises(RuntimeError):
                await self.db.write(boom)
            return await self.db.write(
                lambda database: database.insert('interlude_memory', {'storyId': 's1', 'content': 'ok'}),
            )

        stored = asyncio.run(main())
        self.assertEqual(stored['content'], 'ok')
        self.assertEqual(self.db.count('interlude_memory'), 1)

    def test_context_manager_registers_tables_and_closes(self):
        other_dir = tempfile.mkdtemp(prefix='hdsi_db_', dir=_HERE)
        try:
            path = os.path.join(other_dir, 'other.sqlite3')
            database = Database(path)
            with database:
                self.assertEqual(sorted(database.list_tables()), sorted(EXPECTED_TABLES))
                database.insert('interlude_memory', {'storyId': 's1', 'content': 'x'})
                connection = database.conn
            # `close()` 幂等：连接已释放，句柄置空。
            self.assertIsNone(database.conn)
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute('SELECT 1')
            database.close()
        finally:
            shutil.rmtree(other_dir, ignore_errors=True)

    def test_data_survives_reopen(self):
        """同一个文件重新打开仍能读到（持久化而非内存）。"""
        self.db.insert('interlude_story', {
            'id': 'story-1', 'platform': 'qq', 'setting': {'character': {'name': '凌梦'}},
            'state': {}, 'cursorAt': T0, 'createdAt': T0, 'updatedAt': T0,
        })
        self.db.close()
        reopened = Database(self.path)
        try:
            reopened.register_tables()
            self.assertEqual(reopened.count('interlude_story'), 1)
            self.assertEqual(
                reopened.get('interlude_story', {'id': 'story-1'})['setting']['character']['name'],
                '凌梦',
            )
            self.assertEqual(reopened.get('interlude_story', {'id': 'story-1'})['cursorAt'], T0)
        finally:
            reopened.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
