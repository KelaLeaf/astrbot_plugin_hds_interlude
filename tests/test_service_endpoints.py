"""端点注册表的**服务侧**写路径（`chunk11` 的 6 条管理入口，rc28）。

`test_endpoints.py` 的文档字符串一直指到这里（"带 IO 的那部分由
`test_service_endpoints.py` 覆盖"），但文件一直没建——`unlink_participant_endpoint`
的留痕分支因此长期无人看管，v1.7.9 的"读了生产不存在的属性"就出在这里：

    原先写的是 `self.get_participant_by_id(...)`（core 里**根本没有**这个方法，
    真名是 chunk7 的 `get_participant`），外面还套了 `hasattr` —— 不抛、不出声、
    永远走 else → 解除链接的那条 `[通道迁移]` 剧本留痕被静默丢掉。

这里钉的就是**留痕真的写下来了**（不是"没抛异常就算过"）。
"""

from __future__ import annotations

import pathlib
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from plugin.core.service.chunk11 import ServiceChunk11  # noqa: E402

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
PARTICIPANT_ID = 'onebot:1234:5678'
STORY_ID = 'character:onebot:1234'


def _endpoint_row(**overrides):
    row = {
        'id': 'e1', 'ownerKind': 'participant-user', 'ownerId': PARTICIPANT_ID,
        'platform': 'onebot', 'selfId': '1234', 'userId': '5678',
        'channelKind': 'qq', 'enabled': True,
    }
    row.update(overrides)
    return row


class _Host(ServiceChunk11):
    """最小宿主：只实现 unlink 路径碰到的底座。

    `endpoint_registry_ready` 直接置真，跳过 `reconcile_endpoint_registry`（那是另一条
    路径的事）；`get_participant` / `append_entry` 用真实签名打桩，好断言"谁被调到了"。
    """

    def __init__(self, *, rows=None, participant=True) -> None:
        self.endpoint_rows = list(rows if rows is not None else [_endpoint_row()])
        self.endpoint_registry_ready = True
        self.rows = self.endpoint_rows
        self.entries: list[tuple] = []
        self.asks: list[str] = []
        self.reports: list[tuple] = []
        self.participant = (
            {'id': PARTICIPANT_ID, 'storyId': STORY_ID, 'status': 'active'}
            if participant else None
        )

    def now(self):
        return NOW

    async def db_get(self, table, query=None, options=None):
        if table == 'interlude_endpoint':
            return [dict(row) for row in self.endpoint_rows]
        return []

    async def db_set(self, table, query, patch):
        self.asks.append((table, dict(query)))
        for row in self.endpoint_rows:
            if row.get('id') == query.get('id'):
                row.update(patch)
        return True

    async def get_participant(self, participant_id):
        self.asks.append(participant_id)
        if self.participant is None or self.participant.get('id') != participant_id:
            return None
        return self.participant

    async def append_entry(self, story_id, entry, now, participant_id=''):
        self.entries.append((story_id, entry, participant_id))
        return {'id': 'entry-1'}

    def report_standalone_operation(self, verbosity, level, message, *args):
        self.reports.append((level, message % args if args else message))


class UnlinkParticipantEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_unlink_audit_entry_is_written_to_the_participants_real_story(self):
        """v1.7.9 回归：解除链接必须真的落下 `[通道迁移]` 剧本条目。"""
        host = _Host()
        result = await host.unlink_participant_endpoint('e1')
        self.assertTrue(result['ok'], result)
        self.assertFalse(host.endpoint_rows[0]['enabled'], '端点必须被停用')
        self.assertIn(PARTICIPANT_ID, host.asks, '要按**真实方法名**取参与者')
        self.assertEqual(len(host.entries), 1, '解除链接必须有一条留痕（原先被静默丢掉）')
        story_id, entry, _participant_id = host.entries[0]
        self.assertEqual(story_id, STORY_ID, '条目挂到参与者的故事上')
        self.assertEqual(entry['kind'], 'system')
        self.assertIn('用户端点已解除链接', entry['content'])
        # `script_entry.metadata` 的键按本移植版既定约定保持 snake_case（坑 9）。
        self.assertEqual(entry['metadata'].get('endpoint_unlinked'), True)
        self.assertEqual(entry['metadata'].get('endpoint_id'), 'e1')

    async def test_a_stub_host_without_the_participant_reader_degrades_quietly(self):
        """拿不到参与者（裸宿主 / 参与者已删）：不抛、不写条目，端点照旧停用。"""
        host = _Host(participant=False)
        result = await host.unlink_participant_endpoint('e1')
        self.assertTrue(result['ok'], result)
        self.assertEqual(host.entries, [])
        self.assertFalse(host.endpoint_rows[0]['enabled'])
        self.assertTrue(any('用户端点已解除' in message for _level, message in host.reports))

    async def test_missing_or_already_disabled_or_other_owner_kind_endpoints(self):
        """三种非命中：不存在 / 已解除 / 别人的端点（角色端点不许走这个口）。"""
        missing = _Host()
        self.assertFalse((await missing.unlink_participant_endpoint('nope'))['ok'])
        self.assertEqual(missing.entries, [])

        disabled = _Host(rows=[_endpoint_row(enabled=False)])
        result = await disabled.unlink_participant_endpoint('e1')
        self.assertTrue(result['ok'])
        self.assertIn('已处于解除状态', result.get('error', ''))
        self.assertEqual(disabled.entries, [], '已经解除过的端点不该再写一条留痕')

        role = _Host(rows=[_endpoint_row(ownerKind='story-role', ownerId=STORY_ID)])
        self.assertFalse((await role.unlink_participant_endpoint('e1'))['ok'])
        self.assertEqual(role.entries, [])


if __name__ == '__main__':
    unittest.main()
