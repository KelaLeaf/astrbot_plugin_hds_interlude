"""`plugin/core/service/chunk13.py`（QQ 空间通道服务层）的**端到端**用例。

为什么单独一个文件：`plugin/tests/test_qzone.py` 覆盖的是"通道 + 服务层"的分层用例
（含纯策略与 `_Host(ServiceChunk13)` 夹具）；这里只放**一条链到底**的证据——
"她被评论 / 被点赞之后，究竟是怎么**知道**的"。这条链有四跳，和 §55 的好友动态那条
同型（`test_qzone.py::ServiceFeedObservationPipelineTests`）：

1. **取回**：`chunk13.qzone_reaction_sweep` 打 QZone CGI 读她自己说说列表
   （`emotion_cgi_msglist_v6`，transport 是唯一替身）；
2. **落库**：`append_entry` 写 `interlude_script_entry`；
3. **选择**：`recent_entries_for_prompt` 按**故事时间倒序**取窗口；
4. **组装**：`narrator_prompts.to_prompt_payload` → 模型真正看得见的 `recentScript`。

夹具全部复用 `test_qzone` 的既有桩（跨测试模块 import 是本仓既有惯例，见
`test_qzone_napcat_channel.py`、`test_group_historical_images.py`）。
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from plugin.core.database import Database  # noqa: E402
from plugin.core.narrator_prompts import to_prompt_payload  # noqa: E402
from plugin.core.service import InterludeService  # noqa: E402
from plugin.core.service.base import InterludeContext  # noqa: E402
from plugin.core.time import iso  # noqa: E402
from plugin.tests.test_qzone import (  # noqa: E402 - 复用 qzone 的宿主/传输层桩
    NOW,
    STORY,
    _cgi_feed_text,
    _cgi_moods_text,
    _raw_msg,
    _StubTransport,
    _sweep_handler,
    _sweep_http,
)


class QzoneReactionReachesTheModelTests(unittest.IsolatedAsyncioTestCase):
    """被评论 / 被点赞的感知条目必须**真的进到模型 payload**，不是只落库。

    真机上要能回答的问题是"她怎么知道"。答案必须是**同一条链**：CGI 读回增量 →
    写 `[空间动态]` 条目 → 下一回合的 `recentScript` 里出现。这条用例跑真实
    `InterludeService`（真 sqlite + 真实写入方），所以第 2–4 跳一个桩都没有。
    """

    def _service(self, **qzone: Any) -> tuple[Any, Any, datetime]:
        tmp = tempfile.TemporaryDirectory(prefix='hdsi_qzone_reaction_')
        self.addCleanup(tmp.cleanup)
        database = Database(':memory:')
        self.addCleanup(database.close)
        database.register_tables()
        config = {'qzone': {
            'enabled': True, 'auto_feed': False, 'feed_window_minutes': 120,
            'daily_post_cap': 3, 'daily_comment_cap': 6, 'daily_like_cap': 12,
            'min_interval_minutes': 90, **qzone,
        }}
        service = InterludeService(
            InterludeContext(base_dir=tmp.name, database=database), config, database, None,
        )
        now = service.now()
        database.insert('interlude_story', {
            'id': STORY['id'], 'platform': 'onebot', 'selfId': '10001', 'userId': '',
            'channelId': '', 'status': 'active',
            'setting': {'timezone': 'Asia/Shanghai'}, 'state': {},
            'cursorAt': now, 'createdAt': now, 'updatedAt': now,
        })
        return service, database, now

    def _transport(self, msgs: list[dict[str, Any]], feeds: str = '') -> _StubTransport:
        return _StubTransport(_sweep_handler(), http=_sweep_http(feeds, _cgi_moods_text(msgs)))

    async def _recent_script(self, service: Any, database: Any, now: datetime) -> list[Any]:
        recent = await service.recent_entries_for_prompt(STORY['id'], now)
        story = database.get('interlude_story', {'id': STORY['id']})
        payload = to_prompt_payload({
            'story': story, 'from': now, 'now': now, 'phase': 'advance',
            'recentEntries': recent,
        })
        return payload['relevantEstablishedEpisodes']['recentScript']

    def _seed_post(self, database: Any, now: datetime, **overrides: Any) -> dict[str, Any]:
        row: dict[str, Any] = {
            'storyId': STORY['id'], 'kind': 'post', 'tid': 't1',
            'content': '今天的晚霞', 'status': 'confirmed', 'createdAt': now,
        }
        row.update(overrides)
        return database.insert('interlude_qzone_post', row)

    async def test_a_new_comment_reaches_the_next_turns_recent_script(self):
        service, database, now = self._service()
        self._seed_post(database, now, commentNum=3)
        service.transport = self._transport([_raw_msg('t1', '今天的晚霞', cmtnum=5)])
        await service.qzone_feed_sweep()
        script = await self._recent_script(service, database, now)
        lines = [item['content'] for item in script if item['kind'] == 'friend-feed']
        self.assertEqual(len(lines), 1, '被评论感知必须走到 recentScript：%s' % (script,))
        self.assertIn('[空间动态]', lines[0])
        self.assertIn('今天的晚霞', lines[0])
        self.assertIn('收到了 2 条新评论（累计 5 条）', lines[0])

    async def test_the_reaction_sweep_runs_before_the_auto_feed_gate(self):
        """**反向**：`auto_feed=off` 时**不读好友动态**，但**仍然读她自己说说上的互动**。

        判据是两条 CGI 的 URL：`feeds3_html_more`（别人）零次、`emotion_cgi_msglist_v6`
        （她自己）一次。若把反应轮询放到 `auto_feed` 闸之后，这条用例当场红。
        """
        service, database, now = self._service()
        self._seed_post(database, now, commentNum=3)
        transport = self._transport(
            [_raw_msg('t1', '晚霞', cmtnum=5)], feeds=_cgi_feed_text('k1', '10002'),
        )
        service.transport = transport
        await service.qzone_feed_sweep()
        urls = [call['url'] for call in transport.http_calls]
        self.assertTrue(any('emotion_cgi_msglist_v6' in url for url in urls),
                        '关了自动浏览也要读她自己说说上的互动：%s' % urls)
        self.assertFalse(any('feeds3_html_more' in url for url in urls),
                         '关了自动浏览就不许读别人的动态：%s' % urls)

    async def test_a_new_like_reaches_the_next_turns_recent_script(self):
        service, database, now = self._service()
        self._seed_post(database, now, commentNum=3, likeNum=2)
        service.transport = self._transport([_raw_msg('t1', '晚霞', cmtnum=3, likecount=6)])
        await service.qzone_feed_sweep()
        script = await self._recent_script(service, database, now)
        lines = [item['content'] for item in script if item['kind'] == 'friend-feed']
        self.assertEqual(len(lines), 1)
        self.assertIn('收到了 4 个新赞（累计 6 个）', lines[0])

    async def test_she_still_knows_when_the_previous_entry_was_buried(self):
        """排序判据：感知条目的时间必须是**她这轮知道的时刻**（与 §55 同一条）。

        垫一屏更近、更早的对话条目；如果 `occurredAt` 写的是说说发布时间（很久以前），
        它就会掉出 50 条窗口 / 60 分钟窗——她压根不知道有人评论了她。
        """
        service, database, now = self._service()
        self._seed_post(database, now, commentNum=3)
        for index in range(55):
            await service.append_entry(STORY['id'], {
                'kind': 'user-message', 'actor': 'user', 'content': '第 %d 条' % index,
                'occurredAt': iso(now - timedelta(seconds=5 * index)), 'metadata': {},
            }, now)
        service.transport = self._transport([_raw_msg('t1', '晚霞', cmtnum=5)])
        await service.qzone_feed_sweep()
        script = await self._recent_script(service, database, now)
        lines = [item['content'] for item in script if item['kind'] == 'friend-feed']
        self.assertEqual(len(lines), 1, '被挤掉的感知 = 她压根不知道被评论了')


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
