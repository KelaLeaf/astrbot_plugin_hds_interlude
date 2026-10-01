"""QQ 空间的 **NapCat WebSocket 通道** + **NapCat 专属动作标注**（v1.7.1）。

这一批的三条契约都必须靠测试钉住，因为它们在真机上出问题时**看不出来**：

| 契约 | 出错的样子 | 用例 |
| --- | --- | --- |
| 空间动作优先走 NapCat WS（`get_cookies` + QZone CGI），拿不到 cookie 才回退 SnowLuma | 静默走回退：装了 SnowLuma 的用户以为在走 NapCat，没装的人发现动作"没反应" | `NapcatChannelTests` |
| CGI **真打出去了**但失败 → **不许**再走回退（否则一次动作写两次） | 重复评论 / 重复点赞 | `test_a_failed_cgi_call_never_falls_back_to_snowluma` |
| 只读的 `qzone_read` 不落审计行、不占配额 | 她"看一眼好友动态"就把当天的评论额度花光 | `QzoneReadTests` |
| `napcat_actions()` = 那 9 条；`backend_labels` 顺序 = `backends` 顺序 | 面板把 NapCat 专属标丢 / 标签顺序与运行期优先级不一致 | `BackendCatalogTests` |
| `forward` 走**评论**配额（不是点赞） | 转发把点赞额度吃掉 | `ForwardGateTests` |

传输层按契约 stub（`call_onebot` + `request_text`），**绝不真实联网**；夹具里的
QQ 号 / tid / cookie 全是编的。

运行：`python3 -m unittest plugin.tests.test_qzone_napcat_channel -v`
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import unittest
from datetime import timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from plugin.core import platform_actions as pa  # noqa: E402
from plugin.core import qzone as q  # noqa: E402
from plugin.core import qzone_cgi as cgi  # noqa: E402
from plugin.tests.test_qzone import (  # noqa: E402 - 复用 qzone 的宿主/传输层桩
    BASE_CONFIG,
    NOW,
    STORY,
    _Host as _QzoneHost,
    _StubTransport,
)

#: 编的 cookie：`p_skey` 是算 `g_tk` 的唯一输入，`skey` 可缺。
COOKIES = 'uin=o010001; skey=@abc123; p_skey=pin1n2x3'
P_SKEY = 'pin1n2x3'
#: 编的 tid / 好友 QQ。
TID = 'TID-0001'
FRIEND_UIN = '10002'

#: 好友动态页（腾讯那页是用 `{ver:` 伪分隔的 HTML 串，字段是**单引号**）。
FEED_TEXT = (
    "{ver:1,key:'K1',appid:311,uin:10002,nickname:'\u67d0\u4eba',abstime:1700000000,"
    "html:'<div>\u4eca\u5929\u5929\u6c14\u5f88\u597d</div>',}"
)
#: 说说列表（JSONP，回调名与 `build_mood_list_request` 里的一致）。
MOODS_TEXT = (
    '_preloadCallback({"code":0,"msglist":['
    '{"tid":"TID-0001","content":"\u665a\u5b89","created_time":1700000000}]});'
)
#: 带**配图**的同一条说说：改可见范围时必须被拒（`richval` 还原不了，硬改可能丢图）。
MOODS_TEXT_WITH_PIC = (
    '_preloadCallback({"code":0,"msglist":[{"tid":"TID-0001","content":"\u665a\u5b89",'
    '"pic":[{"url1":"https://example.invalid/a.jpg","width":1,"height":1}]}]});'
)
#: **转发**的说说：`rt_con` 有值 → 转发目标同样还原不了。
MOODS_TEXT_FORWARDED = (
    '_preloadCallback({"code":0,"msglist":[{"tid":"TID-0001","content":"\u8f6c\u4e86",'
    '"rt_tid":"OTHER-1","rt_uin":"10002","rt_con":{"content":"\u539f\u6587"}}]});'
)
#: 列表里**没有**这条 tid（正文找不回来 → 不敢改）。
MOODS_TEXT_OTHER_TID = (
    '_preloadCallback({"code":0,"msglist":[{"tid":"OTHER-9","content":"\u53e6\u4e00\u6761"}]});'
)


def _visibility_http(moods: str = MOODS_TEXT, result: str = '{"code":0}'):
    """按 URL 分派：`msglist`（读正文）→ `moods`；`emotion_cgi_update` → `result`。"""
    calls: list[dict] = []

    def handler(method: str, url: str, headers: object, data: object) -> str:
        calls.append({'method': method, 'url': url, 'data': dict(data or {})})
        if 'emotion_cgi_msglist_v6' in url:
            return moods
        return result

    return handler, calls


def _napcat_handler(calls: list) -> object:
    """NapCat 侧的 OneBot 直通：只认 `get_cookies` / `get_login_info`。"""

    def handler(action: str, params: dict) -> dict:
        calls.append((action, dict(params)))
        if action == 'get_cookies':
            return {'ok': True, 'error': '', 'data': {'cookies': COOKIES}}
        if action == 'get_login_info':
            return {'ok': True, 'error': '', 'data': {'user_id': 10001}}
        return {'ok': False, 'error': 'NapCat 没有这个动作'}

    return handler


class _NapcatTransport(_StubTransport):
    """`call_onebot`（OneBot 直通 = NapCat WS）+ `request_text`（QZone CGI 的 HTTP）。

    `has_http=False` 模拟"传输层没接原始 HTTP"（纯 SnowLuma 环境）。
    """

    def __init__(self, handler: object = None, http: object = None, has_http: bool = True) -> None:
        super().__init__(handler)
        self.http_handler = http
        self.http_calls: list[dict] = []
        if not has_http:
            # `_qzone_cgi_request()` 用 getattr + callable 判能力：这里显式关掉。
            self.request_text = None  # type: ignore[assignment]

    async def request_text(self, method: str, url: str, headers: object = None,
                           data: object = None) -> object:
        self.http_calls.append({
            'method': method, 'url': url,
            'headers': dict(headers or {}), 'data': dict(data or {}),
        })
        if self.http_handler is None:
            return '{"code":0}'
        result = self.http_handler(method, url, headers, data)
        if asyncio.iscoroutine(result):
            result = await result
        if isinstance(result, BaseException):
            raise result
        return result


class _Host(_QzoneHost):
    """在 `test_qzone._Host` 之上记一份 `db_create` 流水（断言"只读不落库"）。"""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.created: list[tuple[str, dict]] = []

    async def db_create(self, table: str, data: dict) -> dict:
        self.created.append((table, dict(data)))
        return await super().db_create(table, data)

    def actions_called(self) -> list[str]:
        transport = self.transport
        return [name for name, _ in getattr(transport, 'calls', [])]

    def notes(self, level: str = '') -> list[str]:
        return [text for current, text in self.standalone if not level or current == level]


# --------------------------------------------------------------------------- #
# 1. 通道优先级：NapCat WS 优先，SnowLuma 只在拿不到 cookie 时兜底
# --------------------------------------------------------------------------- #


class NapcatChannelTests(unittest.IsolatedAsyncioTestCase):
    async def test_comment_prefers_the_napcat_websocket_channel(self):
        """CGI 优先：`get_cookies` + `get_login_info` 被调用，SnowLuma 动作**一次都没调**。"""
        host = _Host(config=dict(BASE_CONFIG, daily_comment_cap=5))
        seen: list[tuple[str, dict]] = []
        transport = _NapcatTransport(
            _napcat_handler(seen),
            http=lambda *a: '{"code":0,"tid":"%s"}' % TID,
        )
        host.transport = transport

        result = await host.qzone_execute(STORY, 'comment', {
            'tid': TID, 'content': '写得真好', 'targetUin': FRIEND_UIN,
        })

        self.assertTrue(result['ok'], result)
        names = [name for name, _ in seen]
        self.assertIn('get_cookies', names)
        self.assertIn('get_login_info', names)
        self.assertNotIn('comment_qzone', names, '走了 NapCat 就不该再打 SnowLuma 动作')
        # 取 cookie 的域是腾讯只认的那一个（写错域 = 永远拿不到 p_skey）。
        self.assertEqual(dict(seen)['get_cookies']['domain'], q.QZONE_COOKIE_DOMAIN)
        self.assertEqual(q.QZONE_COOKIE_DOMAIN, 'user.qzone.qq.com')

        self.assertEqual(len(transport.http_calls), 1)
        call = transport.http_calls[0]
        self.assertEqual(call['method'], 'POST')
        self.assertIn('user.qzone.qq.com', call['url'])
        self.assertIn('emotion_cgi_re_feeds', call['url'])
        self.assertIn('g_tk=%d' % cgi.compute_g_tk(P_SKEY), call['url'])
        self.assertEqual(call['data']['content'], '写得真好')
        self.assertEqual(call['data']['topicId'], TID)
        self.assertEqual(call['data']['hostUin'], FRIEND_UIN)
        # 审计行照旧写 confirmed（通道换了对上层那一套限流/账本零影响）。
        self.assertEqual(host.rows[-1]['status'], 'confirmed')

    async def test_like_forward_and_publish_maps_resolve_to_cgi_actions(self):
        """`QZONE_CGI_BY_ID` 的每条映射都能真的翻成一次 CGI 请求（含不传 cgi_action 的默认路径）。"""
        for action, needle in (
            ('like_qzone', 'internal_dolike_app'),
            ('forward_qzone', 'emotion_cgi_forward_v6'),
            ('send_qzone_msg', 'emotion_cgi_publish_v6'),
            ('delete_qzone_msg', 'emotion_cgi_delete_v6'),
        ):
            with self.subTest(action=action):
                host = _Host()
                seen: list[tuple[str, dict]] = []
                transport = _NapcatTransport(
                    _napcat_handler(seen),
                    http=lambda *a: '{"code":0,"tid":"%s"}' % TID,
                )
                host.transport = transport
                await host._qzone_run_action(
                    transport.call_onebot, action, {'tid': TID, 'content': 'x'},
                )
                self.assertEqual(len(transport.http_calls), 1, action)
                self.assertIn(needle, transport.http_calls[0]['url'])
                self.assertNotIn(action, [name for name, _ in seen])

    async def test_an_action_without_a_cgi_mapping_goes_straight_to_the_platform(self):
        """没有 CGI 映射的动作**不进** NapCat 通道，直接打平台（别把未知动作当 CGI 发）。"""
        host = _Host()

        def handler(action: str, params: dict) -> dict:
            self.fail('没有 CGI 映射的动作不该去问 NapCat 要 cookie：%s' % action)

        def plain(action: str, params: dict) -> dict:
            return {'ok': True, 'error': '', 'data': {'plain': action}}

        transport = _NapcatTransport(handler)
        # 平台侧直通：只认 SnowLuma 动作名（`handler` 只会在问 cookie 时被调到）。
        async def call(action: str, params: dict) -> dict:
            self.assertNotIn(action, ('get_cookies', 'get_login_info'))
            return plain(action, params)

        host.transport = transport
        result = await host._qzone_run_action(call, 'some_plain_action', {'x': 1})
        self.assertEqual(result, {'plain': 'some_plain_action'})
        self.assertEqual(transport.http_calls, [])
        self.assertEqual(transport.calls, [])

    async def test_cookies_without_p_skey_fall_back_to_snowluma(self):
        """拿不到 `p_skey` = 没有 `g_tk` = CGI 做不了：**静默回退**到 SnowLuma 动作名。"""
        host = _Host(config=dict(BASE_CONFIG, daily_like_cap=5))
        calls: list[str] = []

        def handler(action: str, params: dict) -> dict:
            calls.append(action)
            if action == 'get_cookies':
                return {'ok': True, 'error': '', 'data': {'cookies': 'uin=o1; skey=@x'}}
            if action == 'get_login_info':
                return {'ok': True, 'error': '', 'data': {'user_id': 10001}}
            return {'ok': True, 'error': '', 'data': {}}

        transport = _NapcatTransport(handler, http=lambda *a: '{"code":0}')
        host.transport = transport
        result = await host._qzone_run_action(transport.call_onebot, 'like_qzone', {'tid': TID})

        self.assertEqual(result, {})
        self.assertIn('get_cookies', calls)
        self.assertIn('like_qzone', calls)
        self.assertEqual(transport.http_calls, [], '没有 p_skey 就不该发 CGI')
        self.assertTrue(
            any('NapCat 通道不可用' in text and '回退' in text for text in host.notes()),
            host.standalone,
        )

    async def test_a_transport_without_http_capability_uses_snowluma_without_asking_for_cookies(self):
        """纯 SnowLuma 环境（没接原始 HTTP）：连 `get_cookies` 都不该问。"""
        host = _Host(config=dict(BASE_CONFIG, daily_like_cap=5))
        transport = _NapcatTransport(lambda a, p: {'ok': True, 'error': '', 'data': {}}, has_http=False)
        host.transport = transport
        await host._qzone_run_action(transport.call_onebot, 'like_qzone', {'tid': TID})
        self.assertEqual(host.actions_called(), ['like_qzone'])

    async def test_a_failed_cgi_call_never_falls_back_to_snowluma(self):
        """CGI **已经发出去了**但没回执：记 `unknown`、绝不回退（回退 = 写两次）。"""
        host = _Host(config=dict(BASE_CONFIG, daily_comment_cap=5))
        seen: list[tuple[str, dict]] = []
        transport = _NapcatTransport(_napcat_handler(seen), http=lambda *a: None)
        host.transport = transport

        result = await host.qzone_execute(STORY, 'comment', {
            'tid': TID, 'content': '重复就麻烦了', 'targetUin': FRIEND_UIN,
        })

        self.assertFalse(result['ok'])
        self.assertIn('结果未知', result['error'])
        names = [name for name, _ in seen]
        self.assertNotIn('comment_qzone', names, '结果不明时回退会写成两条评论')
        self.assertEqual(host.rows[-1]['status'], 'unknown')

    async def test_a_platform_side_failure_frame_falls_back(self):
        """NapCat 明确回失败帧（没登录 / 没这个动作）= 通道不可用 → 允许回退。"""
        host = _Host(config=dict(BASE_CONFIG, daily_like_cap=5))
        seen: list[tuple[str, dict]] = []

        def handler(action: str, params: dict) -> dict:
            seen.append((action, dict(params)))
            if action == 'get_cookies':
                return {'ok': False, 'error': 'NapCat 的 get_cookies 不可用'}
            return {'ok': True, 'error': '', 'data': {'fallback': True}}

        transport = _NapcatTransport(handler)
        host.transport = transport
        result = await host._qzone_run_action(transport.call_onebot, 'like_qzone', {'tid': TID})
        self.assertEqual(result, {'fallback': True})
        self.assertEqual([name for name, _ in seen], ['get_cookies', 'like_qzone'])
        self.assertEqual(transport.http_calls, [])


# --------------------------------------------------------------------------- #
# 2. 只读：qzone_read（feed / moods）
# --------------------------------------------------------------------------- #


class QzoneReadTests(unittest.IsolatedAsyncioTestCase):
    def _feed_host(self) -> _Host:
        host = _Host(config=dict(BASE_CONFIG, daily_comment_cap=1))
        host.transport = _NapcatTransport(
            _napcat_handler([]), http=lambda *a: FEED_TEXT,
        )
        return host

    async def test_feeds_go_through_the_napcat_cgi_and_report_the_channel(self):
        host = self._feed_host()
        result = await host.qzone_read(STORY, 'feed', {'count': 5})

        self.assertTrue(result['ok'], result)
        self.assertEqual(result['channel'], 'napcat')
        self.assertEqual(result['error'], '')
        self.assertEqual(result['count'], len(result['feeds']))
        self.assertEqual([item['key'] for item in result['feeds']], ['K1'])
        self.assertEqual(result['feeds'][0]['uin'], '10002')
        call = host.transport.http_calls[0]
        self.assertEqual(call['method'], 'GET')
        self.assertIn('feeds3_html_more', call['url'])
        self.assertEqual(call['data']['pagenum'], '1')
        self.assertEqual(call['data']['count'], '5')
        # 只读：**一行审计都不落**（不占配额、不进 48h 窗口）。
        self.assertEqual(host.created, [])
        self.assertEqual(host.rows, [])

    async def test_moods_read_reports_posts_and_the_channel(self):
        host = _Host(config=dict(BASE_CONFIG))
        host.transport = _NapcatTransport(_napcat_handler([]), http=lambda *a: MOODS_TEXT)
        result = await host.qzone_read(STORY, 'moods', {'targetUin': FRIEND_UIN, 'count': 3})

        self.assertTrue(result['ok'], result)
        self.assertEqual(result['channel'], 'napcat')
        self.assertEqual(result['count'], 1)
        self.assertEqual(result['posts'][0]['tid'], TID)
        self.assertEqual(result['posts'][0]['content'], '晚安')
        call = host.transport.http_calls[0]
        self.assertIn('emotion_cgi_msglist_v6', call['url'])
        self.assertEqual(call['data']['uin'], FRIEND_UIN)
        self.assertEqual(call['data']['num'], '3')
        self.assertEqual(host.created, [])

    async def test_moods_without_a_target_needs_include_self(self):
        """不指定 `target_uin` 又没说"就是她自己"：明确报错，别拿空 uin 去问腾讯。"""
        host = _Host(config=dict(BASE_CONFIG))
        host.transport = _NapcatTransport(_napcat_handler([]), http=lambda *a: MOODS_TEXT)
        refused = await host.qzone_read(STORY, 'moods', {})
        self.assertFalse(refused['ok'])
        self.assertIn('target_uin', refused['error'])
        self.assertEqual(host.transport.http_calls, [], '拒绝了就不该再发请求')
        self.assertEqual(host.created, [])

        allowed = await host.qzone_read(STORY, 'moods', {}, include_self=True)
        self.assertTrue(allowed['ok'], allowed)
        self.assertEqual(allowed['channel'], 'napcat')
        self.assertEqual(host.transport.http_calls[0]['data']['uin'], '')

    async def test_reads_fall_back_to_snowluma_and_say_so_in_the_channel_field(self):
        """没有原始 HTTP 能力（纯 SnowLuma 环境）：走平台动作，`channel` 如实写 `snowluma`。"""
        host = _Host(config=dict(BASE_CONFIG))
        transport = _NapcatTransport(
            lambda a, p: {'ok': True, 'error': '', 'data': {'feeds': [{'key': 'K9'}]}},
            has_http=False,
        )
        host.transport = transport
        result = await host.qzone_read(STORY, 'feed', {'count': 2})
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['channel'], 'snowluma')
        self.assertEqual(result['feeds'], [{'key': 'K9'}])
        self.assertEqual(host.actions_called(), ['get_qzone_feeds'])

    async def test_reads_do_not_consume_the_write_quota(self):
        """读三次不占配额：评论上限 1 时，读完仍然写得进去；第二条才被每日上限拦下。"""
        host = self._feed_host()
        for _ in range(3):
            self.assertTrue((await host.qzone_read(STORY, 'feed', {'count': 1}))['ok'])
        self.assertEqual(host.created, [])

        host.transport = _NapcatTransport(
            _napcat_handler([]), http=lambda *a: '{"code":0,"tid":"%s"}' % TID,
        )
        first = await host.qzone_execute(STORY, 'comment', {
            'tid': TID, 'content': '读了几眼才决定回一句',
        })
        self.assertTrue(first['ok'], first)

        host.now_value = NOW + timedelta(minutes=30)
        second = await host.qzone_execute(STORY, 'comment', {'tid': TID, 'content': '再来一条'})
        self.assertFalse(second['ok'])
        self.assertIn('上限', second['error'])
        # 三次读一条都没落；第一条评论落了 pending 行；第二条在**落行之前**就被门拦下
        # （被拦下的动作不该留下审计行）。
        self.assertEqual([data.get('kind') for _, data in host.created], ['comment'])

    async def test_reads_report_a_missing_transport_instead_of_crashing(self):
        host = _Host(config=dict(BASE_CONFIG))
        host.transport = None
        result = await host.qzone_read(STORY, 'feed', {})
        self.assertFalse(result['ok'])
        self.assertIn('OneBot', result['error'])


# --------------------------------------------------------------------------- #
# 2b. 改说说可见范围（v1.7.5）
# --------------------------------------------------------------------------- #


class SetVisibilityTests(unittest.IsolatedAsyncioTestCase):
    """`set_qzone_visibility`：**只有** NapCat WS 通道能做，且必须先读回正文。

    三条边界各有一个用例，因为它们在真机上都不出声：

    | 边界 | 出错的样子 |
    | --- | --- |
    | 带图 / 转发 → 拒绝 | 图被静默弄丢（`richval` 还原不了） |
    | 正文找不回来 → 拒绝 | 服务端按整条重建，空 `con` = 把正文清掉 |
    | 没有 CGI 通道 → 明确失败 | 回落到平台打一个不存在的动作名，报错看不懂 |
    """

    def _host(self, moods: str = MOODS_TEXT, result: str = '{"code":0}') -> tuple:
        host = _Host(config=dict(BASE_CONFIG, daily_post_cap=5, min_interval_minutes=0))
        handler, calls = _visibility_http(moods, result)
        host.transport = _NapcatTransport(_napcat_handler([]), http=handler)
        return host, calls

    def _payload(self, **overrides: object) -> dict:
        payload = {'tid': TID, 'visible': '部分人可见', 'targetUins': ['10002']}
        payload.update(overrides)
        return payload

    async def test_five_tiers_map_onto_the_ugc_right_values(self):
        """五档标签 → `ugc_right`，并且只有 16/128 带 `allow_uins`。"""
        for label, expected in (
            ('所有人可见', 1), ('仅 QQ 好友可见', 4), ('部分人可见', 16),
            ('部分人不可见', 128), ('仅自己可见', 64),
        ):
            with self.subTest(label=label):
                host, calls = self._host()
                params = self._payload(visible=label)
                if expected not in (16, 128):
                    params.pop('targetUins')
                result = await host.qzone_execute(STORY, 'visibility', params)
                self.assertTrue(result['ok'], result)
                update = [call for call in calls if 'emotion_cgi_update' in call['url']][0]
                self.assertEqual(update['data']['ugc_right'], str(expected))
                self.assertEqual(update['data']['con'], '晚安', '正文必须原样带回去')
                self.assertEqual(update['data']['tid'], TID)
                if expected in (16, 128):
                    self.assertEqual(update['data']['allow_uins'], '10002')
                else:
                    self.assertNotIn('allow_uins', update['data'])
                self.assertEqual(host.rows[-1]['status'], 'confirmed')
                self.assertEqual(host.rows[-1]['ugcRight'], expected)
                self.assertEqual(host.rows[-1]['kind'], 'visibility')
                self.assertEqual(host.entries[-1]['metadata']['qzone_kind'], 'visibility')
                self.assertIn(label, host.entries[-1]['content'])

    async def test_the_targeted_tiers_require_a_uin_list(self):
        """「部分人可见 / 部分人不可见」没带名单 = 参数错，不许落审计行、不许发出请求。"""
        for label in ('部分人可见', '部分人不可见'):
            with self.subTest(label=label):
                host, calls = self._host()
                result = await host.qzone_execute(STORY, 'visibility', self._payload(
                    visible=label, targetUins=[],
                ))
                self.assertFalse(result['ok'], result)
                self.assertIn('target_uins', result['error'])
                self.assertEqual(host.rows, [], '坏参数不该留下审计行')
                self.assertEqual(calls, [], '坏参数不该发出任何请求')

    async def test_an_unknown_label_is_rejected_before_anything_happens(self):
        host, calls = self._host()
        result = await host.qzone_execute(STORY, 'visibility', self._payload(visible='仅好友可见'))
        self.assertFalse(result['ok'], result)
        self.assertIn('五档', result['error'])
        self.assertEqual(host.rows, [])
        self.assertEqual(calls, [])

    async def test_the_cgi_call_carries_the_g_tk_and_the_update_endpoint(self):
        """真的走了 NapCat WS 的 CGI（`get_cookies` + `g_tk` 挂 URL），没打平台动作。"""
        host, calls = self._host()
        result = await host.qzone_execute(STORY, 'visibility', self._payload())
        self.assertTrue(result['ok'], result)
        update = [call for call in calls if 'emotion_cgi_update' in call['url']][0]
        self.assertEqual(update['method'], 'POST')
        self.assertIn('g_tk=%d' % cgi.compute_g_tk(P_SKEY), update['url'])
        self.assertTrue(update['url'].startswith('https://user.qzone.qq.com/proxy/domain/'))
        # 平台侧**只**被问了 cookie / 登录信息（读正文一次 + 改可见范围一次；
        # 没有"改可见范围"这条原生动作可打）。
        self.assertEqual(
            {name for name, _ in host.transport.calls}, {'get_cookies', 'get_login_info'},
        )
        self.assertTrue(
            any('update_visibility' in text for _level, text in host.standalone),
            '通道选择要有一条 debug 记录（排查"到底走没走 NapCat"靠它）：%s' % host.standalone,
        )

    async def test_a_post_with_images_is_refused_instead_of_losing_the_pictures(self):
        host, calls = self._host(moods=MOODS_TEXT_WITH_PIC)
        result = await host.qzone_execute(STORY, 'visibility', self._payload())
        self.assertFalse(result['ok'], result)
        self.assertIn('图', result['error'])
        self.assertEqual([call for call in calls if 'emotion_cgi_update' in call['url']], [],
                         '拒绝时必须**没有**发出编辑请求')
        self.assertEqual(host.rows[-1]['status'], 'failed')
        self.assertIn('reason', host.rows[-1] if 'reason' in host.rows[-1] else {'reason': ''})

    async def test_a_forwarded_post_is_refused_too(self):
        host, _calls = self._host(moods=MOODS_TEXT_FORWARDED)
        result = await host.qzone_execute(STORY, 'visibility', self._payload())
        self.assertFalse(result['ok'], result)
        self.assertIn('转发', result['error'])
        self.assertEqual(host.rows[-1]['status'], 'failed')

    async def test_a_post_we_cannot_read_back_is_refused(self):
        host, calls = self._host(moods=MOODS_TEXT_OTHER_TID)
        result = await host.qzone_execute(STORY, 'visibility', self._payload())
        self.assertFalse(result['ok'], result)
        self.assertIn('正文', result['error'])
        self.assertEqual([call for call in calls if 'emotion_cgi_update' in call['url']], [])
        self.assertEqual(host.rows[-1]['status'], 'failed')

    async def test_a_failed_lookup_surfaces_the_real_reason(self):
        """读回正文这一步失败时，报的是"读取失败 + 原因"，不是含糊的"找不到正文"。"""
        host, _calls = self._host()

        async def broken_read(story, kind, params=None, **kwargs):
            return {'ok': False, 'error': '没有可用的 OneBot 连接'}

        host.qzone_read = broken_read  # type: ignore[method-assign]
        result = await host.qzone_execute(STORY, 'visibility', self._payload())
        self.assertFalse(result['ok'], result)
        self.assertIn('读回', result['error'])
        self.assertIn('没有可用的 OneBot 连接', result['error'])
        self.assertEqual(host.rows[-1]['status'], 'failed')

    async def test_without_the_cgi_channel_it_fails_instead_of_falling_back(self):
        """没有原始 HTTP 能力（纯 SnowLuma 环境）：明确失败，**不**回落平台动作。"""
        host = _Host(config=dict(BASE_CONFIG, daily_post_cap=5, min_interval_minutes=0))
        host.transport = _NapcatTransport(_napcat_handler([]), has_http=False)
        result = await host.qzone_execute(STORY, 'visibility', self._payload())
        self.assertFalse(result['ok'], result)
        self.assertIn('NapCat', result['error'])
        self.assertEqual(host.transport.calls, [], '不该往平台打任何动作（根本没有这条动作）')
        self.assertEqual(host.rows[-1]['status'], 'failed')

    async def test_a_transport_error_is_recorded_as_failed(self):
        """传输层异常 → `failed`（与其它 CGI 动作一致），这条动作**重试是安全的**。

        为什么不判 `unknown`：`unknown` 的语义是"可能已生效，禁止自动重试"，那是给
        **非幂等**动作（发帖 / 评论）准备的。改可见范围是幂等的——再改一次结果一样，
        所以让它留在 `failed` 更诚实：模型可以重试，代价只是再来一次请求。
        （`call_qzone_cgi` 不给传输层异常打 `ambiguous`，与 `call_qzone_action` 不同；
        这是既有行为，本用例把它钉住，改口径要连带改发帖/评论那几条。）
        """
        host, _calls = self._host(result=RuntimeError('socket closed'))
        result = await host.qzone_execute(STORY, 'visibility', self._payload())
        self.assertFalse(result['ok'], result)
        self.assertEqual(host.rows[-1]['status'], 'failed')
        self.assertIn('socket closed', result['error'])


# --------------------------------------------------------------------------- #
# 3. 转发计入评论配额
# --------------------------------------------------------------------------- #


class ForwardGateTests(unittest.TestCase):
    def _record(self, kind: str, minutes_ago: int, **overrides: object) -> dict:
        row = {
            'storyId': STORY['id'], 'kind': kind, 'tid': TID,
            'status': 'confirmed', 'createdAt': NOW - timedelta(minutes=minutes_ago),
        }
        row.update(overrides)
        return row

    def test_forward_uses_the_comment_cap_not_the_like_cap(self):
        config = {'enabled': True, 'daily_comment_cap': 2, 'daily_like_cap': 12,
                  'daily_post_cap': 3, 'min_interval_minutes': 10}
        gate = q.evaluate_qzone_gate([], config, 'forward', NOW)
        self.assertTrue(gate['allowed'])
        self.assertEqual(gate['cap'], 2, '转发按评论类计数')
        self.assertNotEqual(gate['cap'], 12)

    def test_a_forward_row_consumes_the_comment_budget(self):
        config = {'enabled': True, 'daily_comment_cap': 1, 'daily_like_cap': 12,
                  'daily_post_cap': 3, 'min_interval_minutes': 10}
        records = [self._record('forward', 6 * 60)]
        blocked = q.evaluate_qzone_gate(records, config, 'forward', NOW)
        self.assertFalse(blocked['allowed'])
        self.assertEqual(blocked['reason'], 'daily-cap')
        self.assertEqual(blocked['used_today'], 1)
        # 点赞走自己那档更宽的上限，不受转发影响。
        like = q.evaluate_qzone_gate(records, config, 'like', NOW)
        self.assertTrue(like['allowed'])
        self.assertEqual(like['cap'], 12)
        self.assertEqual(like['used_today'], 0)

    def test_like_rows_do_not_eat_the_forward_budget(self):
        config = {'enabled': True, 'daily_comment_cap': 1, 'daily_like_cap': 12,
                  'daily_post_cap': 3, 'min_interval_minutes': 10}
        records = [self._record('like', 6 * 60)]
        gate = q.evaluate_qzone_gate(records, config, 'forward', NOW)
        self.assertTrue(gate['allowed'], gate)
        self.assertEqual(gate['used_today'], 0)

    def test_forward_is_a_first_class_action_kind(self):
        self.assertIn('forward', q.QZONE_ACTION_KINDS)
        # v1.7.5：多了 `visibility`（改可见范围），它按 post 那一档计额度。
        self.assertEqual(
            q.QZONE_ACTION_KINDS,
            frozenset({'post', 'comment', 'like', 'forward', 'visibility'}),
        )
        # 只读感知标记（feed-seen）挤不进任何配额。
        config = {'enabled': True, 'daily_comment_cap': 1, 'daily_like_cap': 1,
                  'daily_post_cap': 1, 'min_interval_minutes': 10}
        records = [self._record('feed-seen', 5, status='confirmed')]
        self.assertTrue(q.evaluate_qzone_gate(records, config, 'forward', NOW)['allowed'])


# --------------------------------------------------------------------------- #
# 4. 目录标注一致性（core 一侧）
# --------------------------------------------------------------------------- #


#: 那 9 条 NapCat 专属（非 NapCat 后端无法使用）：空间 8 条 + 改在线状态。
NAPCAT_ONLY_IDS = frozenset({
    'publish_qzone_post', 'comment_qzone_post', 'like_qzone_post',
    'list_qzone_posts', 'list_qzone_feeds', 'forward_qzone_post', 'delete_qzone_post',
    'set_qzone_visibility',
    'update_qq_status',
})
QZONE_IDS = frozenset(NAPCAT_ONLY_IDS - {'update_qq_status'})


class BackendCatalogTests(unittest.TestCase):
    def test_backend_labels_are_the_two_literals_the_panel_pins(self):
        """前端 `actions-view.ts` 也钉了这两条字面量（徽章语气按后端种类分）。

        v1.7.3：界面上只有**正式通道**——回退实现（SnowLuma 那套动作名）留在适配层的
        `_PLATFORM_CALLS` 里当运行期兜底，**不进这张表**，也就不可能被下发到面板上。
        """
        self.assertEqual(pa.BACKEND_LABELS, {
            'onebot': '标准 OneBot',
            'napcat': 'NapCat 专属',
        })
        self.assertNotIn('snowluma', pa.BACKEND_LABELS)
        for action in pa.ACTIONS.values():
            with self.subTest(action=action.id):
                self.assertNotIn('snowluma', action.backends,
                                 '回退通道不许写进目录（界面上不承诺它）')

    def test_every_action_declares_at_least_one_known_backend(self):
        for action in pa.ACTIONS.values():
            with self.subTest(action=action.id):
                self.assertTrue(action.backends, '每条动作都要写清谁能承载它')
                for name in action.backends:
                    self.assertIn(name, pa.BACKEND_LABELS)
                self.assertEqual(len(set(action.backends)), len(action.backends), '别重复声明')

    def test_backend_labels_follow_the_backends_order_and_dedupe(self):
        for action in pa.ACTIONS.values():
            with self.subTest(action=action.id):
                expected = [pa.BACKEND_LABELS[name] for name in action.backends]
                self.assertEqual(pa.backend_labels(action), expected, '顺序 = 优先级，不许重排')
        pending = pa.PlatformAction('x', 'qzone', 'l', 's', backends=('napcat', 'napcat', 'onebot'))
        self.assertEqual(pa.backend_labels(pending), ['NapCat 专属', '标准 OneBot'])

    def test_napcat_only_is_exactly_those_nine_actions(self):
        self.assertEqual(len(pa.ACTIONS), 62)
        napcat = pa.napcat_actions()
        self.assertEqual({action.id for action in napcat}, set(NAPCAT_ONLY_IDS))
        self.assertEqual(len(napcat), 9)
        self.assertEqual([action.id for action in napcat], sorted(NAPCAT_ONLY_IDS), '按 id 排序')
        for action in pa.ACTIONS.values():
            with self.subTest(action=action.id):
                self.assertEqual(action.napcat_only, action.id in NAPCAT_ONLY_IDS)

    def test_the_qzone_actions_are_napcat_only(self):
        """空间动作**只有** NapCat 这一条通道（界面上不再宣传别的回退）。

        执行期的回退（`chunk13` 拿不到 cookie 时改打另一个扩展的动作名）照旧存在，
        有它自己的用例；这里钉的是**目录/界面**这一侧不声明它。
        """
        for action_id in QZONE_IDS:
            with self.subTest(action=action_id):
                action = pa.ACTIONS[action_id]
                self.assertEqual(action.backends, ('napcat',))
                self.assertEqual(pa.backend_labels(action), ['NapCat 专属'])
                self.assertTrue(action.napcat_only)
        self.assertEqual(pa.ACTIONS['update_qq_status'].backends, ('napcat',))
        self.assertEqual(pa.backend_labels(pa.ACTIONS['update_qq_status']), ['NapCat 专属'])

    def test_the_two_new_actions_are_in_the_catalog_with_their_params(self):
        feeds = pa.ACTIONS['list_qzone_feeds']
        self.assertEqual(feeds.category, 'qzone')
        self.assertEqual(feeds.risk, 'safe')
        self.assertEqual([param.name for param in feeds.params], ['page', 'count'])
        forward = pa.ACTIONS['forward_qzone_post']
        self.assertEqual(forward.risk, 'sensitive')
        self.assertEqual([param.name for param in forward.params], ['tid', 'target_uin', 'content'])
        self.assertTrue(forward.param('tid').required)
        # 转发是**写**动作：必须由本机（chunk12/13）办，不能直通传输层绕过限流门。
        from plugin.core.service.chunk12 import CORE_HANDLED_ACTIONS, QZONE_ACTION_KINDS_BY_ID

        self.assertIn('forward_qzone_post', CORE_HANDLED_ACTIONS)
        self.assertIn('list_qzone_feeds', CORE_HANDLED_ACTIONS)
        self.assertIn('list_qzone_posts', CORE_HANDLED_ACTIONS)
        self.assertEqual(QZONE_ACTION_KINDS_BY_ID['forward_qzone_post'], 'forward')
        self.assertNotIn('list_qzone_feeds', QZONE_ACTION_KINDS_BY_ID, '只读动作不进写通道')

    def test_the_catalog_ids_and_the_cgi_maps_cover_each_other(self):
        """`QZONE_CGI_ACTIONS` 是**目录 id** → CGI：两边都不许漏，也不许多。"""
        self.assertEqual(set(q.QZONE_CGI_ACTIONS), set(QZONE_IDS))
        builders = {
            'publish': 'build_publish_request', 'delete': 'build_delete_request',
            'comment': 'build_comment_request', 'like': 'build_like_request',
            'forward': 'build_forward_request', 'moods': 'build_mood_list_request',
            'feed': 'build_feed_request',
            # v1.7.5：改可见范围（`emotion_cgi_update`）。
            'update_visibility': 'build_update_visibility_request',
        }
        self.assertEqual(set(q.QZONE_CGI_ACTIONS.values()), set(builders))
        for cgi_action, builder in builders.items():
            with self.subTest(cgi=cgi_action):
                self.assertTrue(callable(getattr(cgi, builder)), builder)

    def test_the_platform_action_names_are_mapped_for_the_runtime_side(self):
        """`QZONE_CGI_BY_ID` 是**平台动作名** → CGI，两条新增动作在里面。"""
        self.assertEqual(set(q.QZONE_CGI_BY_ID.values()), set(q.QZONE_CGI_ACTIONS.values()))
        self.assertEqual(q.QZONE_CGI_BY_ID['forward_qzone'], 'forward')
        self.assertEqual(q.QZONE_CGI_BY_ID['get_qzone_feeds'], 'feed')
        # v1.7.5 的例外：改可见范围两个平台都没有原生动作，键直接用**目录 id**，
        # 值就是它唯一可达的那条 CGI（`_qzone_run_action` 不传 cgi_action 时的默认查表）。
        self.assertEqual(q.QZONE_CGI_BY_ID['set_qzone_visibility'], 'update_visibility')
        self.assertEqual(q.QZONE_CGI_ACTIONS['set_qzone_visibility'], 'update_visibility')
        self.assertEqual(
            q.QZONE_NAPCAT_ONLY_ACTIONS,
            frozenset({'forward_qzone_post', 'list_qzone_feeds'}),
        )
        for action_id in q.QZONE_NAPCAT_ONLY_ACTIONS:
            self.assertIn(action_id, pa.ACTIONS)

    def test_the_console_payload_carries_the_labels_the_panel_needs(self):
        """面板读的是 payload：徽章文案 / NapCat 布尔 / 专属清单都必须在里面。"""
        # 导入顺序有意义：`test_astrbot_bridge` 导入时装 AstrBot 桩，`console_api`
        # 依赖它（`plugin.adapters.__init__` 会 import astrbot）。
        from plugin.tests.test_astrbot_bridge import _make_bridge  # noqa: F401
        from plugin.adapters.console_api import ConsoleApi

        bridge = _make_bridge({})
        bridge.db = None  # 没配数据库也要能出目录（§29 的控制台取数约定）
        payload = asyncio.run(ConsoleApi(bridge).actions_catalog())

        self.assertEqual(payload['stats']['napcat_only'], 9)
        self.assertEqual(payload['napcat_only'], [action.id for action in pa.napcat_actions()])
        self.assertEqual(payload['backend_labels'], pa.BACKEND_LABELS)
        for row in payload['actions']:
            action = pa.ACTIONS[row['id']]
            with self.subTest(action=row['id']):
                self.assertEqual(row['backends'], pa.backend_labels(action))
                self.assertEqual(row['napcat_only'], action.id in NAPCAT_ONLY_IDS)
        json.dumps(payload, ensure_ascii=False)  # 面板直接吃它，必须可序列化


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
