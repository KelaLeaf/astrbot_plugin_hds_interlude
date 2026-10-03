"""QQ 空间的 **NapCat WebSocket 通道** + **NapCat 专属动作标注**（v1.7.1）。

这一批的三条契约都必须靠测试钉住，因为它们在真机上出问题时**看不出来**：

| 契约 | 出错的样子 | 用例 |
| --- | --- | --- |
| 空间动作**只有** NapCat WS（`get_cookies` + QZone CGI）一条路：拿不到 cookie 就明确失败 | 把不存在的动作名发给平台 → `retcode 1404 不支持的Api`；或静默什么都不做 | `NapcatChannelTests` / `MissingPlatformActionTests` |
| CGI **真打出去了**但失败 → 记 `unknown`，绝不重发（否则一次动作写两次） | 重复评论 / 重复点赞 | `test_a_failed_cgi_call_never_retries_the_write` |
| CGI **拿不到响应**（回 `None` / 抛异常）→ `unknown`（"可能已发生"，不自动重试）；`code != 0` → `failed` | 发帖的网络错误记成 `failed` → 调用方重试 → **重复发帖** | `CgiOutcomeClassificationTests` / `WritePathAmbiguityTests` |
| 只读的 `qzone_read` 不落审计行、不占配额 | 她"看一眼好友动态"就把当天的评论额度花光 | `QzoneReadTests` |
| `napcat_actions()` = 那 9 条；`backend_labels` 顺序 = `backends` 顺序 | 面板把 NapCat 专属标丢 / 标签顺序与运行期优先级不一致 | `BackendCatalogTests` |
| `forward` 走**评论**配额（不是点赞） | 转发把点赞额度吃掉 | `ForwardGateTests` |
| 带附件（图片 / 视频）改可见范围：**只发可见性 + 既有字段、零上传零下载** | 又去重传一遍 = 腾讯侧多出副本、原图 URL 换掉、混排视频被丢 | `SetVisibilityTests`（`..._updates_without_uploading_or_downloading`） |
| update 之后**回读校验**附件与正文；变少 → warn + metadata + **跳闸**（此后带附件的拒绝、纯文字照常） | 服务端真删了附件却没人知道 = 静默毁第二条说说 | `SetVisibilityTests`（`..._attachment_loss...` / `..._guard_refuses...`） |

传输层按契约 stub（`call_onebot` + `request_text` + `fetch_image`），**绝不真实联网**；
夹具里的 QQ 号 / tid / cookie 全是编的（`fetch_image` 留着是为了**断言它一次都没被调**）。

运行：`python3 -m unittest plugin.tests.test_qzone_napcat_channel -v`
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import tempfile
import types
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
#: 带**配图**的同一条说说（v1.7.9：照常改，**不上传、不下载**，只发可见性）。
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


#: 一条**带视频**的说说（v1.7.9）：视频与图片一样不在正文里，只有 `video` 能看见它。
MOODS_TEXT_WITH_VIDEO = (
    '_preloadCallback({"code":0,"msglist":[{"tid":"TID-0001","content":"\u665a\u5b89",'
    '"video":[{"url3":"https://example.invalid/v.mp4","url1":"https://example.invalid/c.jpg",'
    '"video_id":"VID-1"}]}]});'
)
#: 同一条说说**改完之后**一个附件都不剩了（回读校验要抓的那种事故）。
MOODS_TEXT_NO_ATTACHMENTS = (
    '_preloadCallback({"code":0,"msglist":[{"tid":"TID-0001","content":"\u665a\u5b89"}]});'
)
#: 同一条说说**改完之后正文对不上**（附件没少，正文被改掉了）。
MOODS_TEXT_REWRITTEN = (
    '_preloadCallback({"code":0,"msglist":[{"tid":"TID-0001","content":"\u665a\u5b89\u554a",'
    '"pic":[{"url1":"https://example.invalid/a.jpg","width":1,"height":1}]}]});'
)


def _visibility_http(moods: str = MOODS_TEXT, result: str = '{"code":0}',
                     later: object = None):
    """按 URL 分派：`msglist` → `moods`（**第二次起**用 `later`，默认还是 `moods`）；
    `emotion_cgi_update` → `result`。

    这里**刻意没有上传分支**：v1.7.9 起改可见范围这条路上不该再出现任何上传，
    真出现了就是 `calls` 里多一条 `cgi_upload_image`（用例据此挂掉）。
    """
    calls: list[dict] = []
    reads = {'n': 0}

    def handler(method: str, url: str, headers: object, data: object) -> str:
        calls.append({'method': method, 'url': url, 'data': dict(data or {})})
        if 'emotion_cgi_msglist_v6' in url:
            reads['n'] += 1
            if reads['n'] > 1 and later is not None:
                return later
            return moods
        return result

    return handler, calls


def _napcat_handler(calls: list) -> object:
    """NapCat 侧的 OneBot 直通：认**两个**取凭据接口 + `get_login_info`。

    现代 NapCat 两个取凭据接口都有（`get_credentials` / `get_cookies`，
    napcat.apifox.cn/226657054e0 与 226657041e0），主路是前者（参考插件
    `astrbot_plugin_qzone_tools` v5.7.5 `main.py:866`），所以夹具两个都服务——
    夹具不许比生产少一条路（坑 39/66）。**顺序**由
    `CredentialApiOrderTests` 专门钉住，这里不掺和。
    """

    def handler(action: str, params: dict) -> dict:
        calls.append((action, dict(params)))
        if action == 'get_credentials':
            return {'ok': True, 'error': '', 'data': {'cookies': COOKIES, 'token': 1869525896}}
        if action == 'get_cookies':
            return {'ok': True, 'error': '', 'data': {'cookies': COOKIES, 'bkn': '1869525896'}}
        if action == 'get_login_info':
            return {'ok': True, 'error': '', 'data': {'user_id': 10001}}
        return {'ok': False, 'error': 'NapCat 没有这个动作'}

    return handler


class _NapcatTransport(_StubTransport):
    """`call_onebot`（OneBot 直通 = NapCat WS）+ `request_text`（QZone CGI 的 HTTP）
    + `fetch_image`（原图字节）。

    v1.7.9 起改可见范围**不许**再下载原图，`fetch_image` 留在桩上就是为了让
    `self.fetch_calls == []` 这条断言有意义（能力还在，只是这条路不该用）。
    `has_http=False` 模拟"传输层没接原始 HTTP"（打不了 QZone CGI）；
    `has_fetch=False` 模拟"传输层不能下载图片"（改可见范围应当照常成功）。
    """

    def __init__(self, handler: object = None, http: object = None, has_http: bool = True,
                 image: object = None, has_fetch: bool = True) -> None:
        super().__init__(handler)
        self.http_handler = http
        self.http_calls: list[dict] = []
        self.image_bytes = image if image is not None else b'\x89PNG-fake-bytes'
        self.fetch_calls: list[str] = []
        if not has_http:
            # `_qzone_cgi_request()` 用 getattr + callable 判能力：这里显式关掉。
            self.request_text = None  # type: ignore[assignment]
        if not has_fetch:
            self.fetch_image = None  # type: ignore[assignment]

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

    async def fetch_image(self, url: str) -> object:
        """原图字节（`Transport.fetch_image` 的契约：失败回 `None`，绝不抛）。"""
        self.fetch_calls.append(url)
        return self.image_bytes


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
# 1. 唯一通道：NapCat WS（get_cookies + QZone CGI）——没有第二条路
# --------------------------------------------------------------------------- #


class NapcatChannelTests(unittest.IsolatedAsyncioTestCase):
    async def test_comment_prefers_the_napcat_websocket_channel(self):
        """`get_cookies` + `get_login_info` 被调用，**一个空间平台动作都没发出去**。"""
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
        # 主路是 `get_credentials`（参考插件验证过的那条），域是主域。
        self.assertEqual(names, ['get_credentials', 'get_login_info'])
        self.assertNotIn(
            'comment_qzone', names,
            '`comment_qzone` 不是任何后端的动作名（发了就是 1404）：只许走 CGI',
        )
        # 取 cookie 的域是腾讯只认的那一个（写错域 = 永远拿不到 p_skey）。
        self.assertEqual(dict(seen)['get_credentials']['domain'], q.QZONE_COOKIE_DOMAIN)
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
        # 平台侧直通：`handler` 只会在问凭据时被调到（这条动作没有 CGI 映射）。
        async def call(action: str, params: dict) -> dict:
            self.assertNotIn(action, q.QZONE_COOKIE_APIS + ('get_login_info',))
            return plain(action, params)

        host.transport = transport
        result = await host._qzone_run_action(call, 'some_plain_action', {'x': 1})
        self.assertEqual(result, {'plain': 'some_plain_action'})
        self.assertEqual(transport.http_calls, [])
        self.assertEqual(transport.calls, [])

    async def test_cookies_without_p_skey_fails_actionably_without_calling_the_platform(self):
        """拿不到 `p_skey` = 没有 `g_tk` = CGI 做不了：**明确失败**，不许换个动作名再试。

        `like_qzone` 在任何后端的 API 清单里都不存在，回退只会换回
        `retcode 1404 不支持的Api`（真机日志点名）——所以这里连一个平台动作都不许发。
        """
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
        with self.assertRaises(q.QzoneCgiUnavailable) as caught:
            await host._qzone_run_action(transport.call_onebot, 'like_qzone', {'tid': TID})

        text = str(caught.exception)
        self.assertIn('点赞说说', text, '要点名是哪个动作')
        self.assertIn('需要 NapCat 通道', text, '要说清靠谁')
        self.assertIn('like_qzone', text, '要把动作名摆出来（它就是 1404 的那个名字）')
        self.assertIn('可执行的路', text, '要给出下一步动作')
        # 两个取凭据接口 × 两个域各问一次（`QZONE_COOKIE_APIS` / `QZONE_COOKIE_DOMAINS`），
        # 之后**一个动作都不发**——除取凭据外不发任何空间动作。
        self.assertEqual(
            calls,
            ['get_credentials', 'get_credentials', 'get_cookies', 'get_cookies'],
            '只许问取凭据接口，不许发空间动作',
        )
        self.assertIn('p_skey', text, '失败文案要说清缺的是 p_skey')
        self.assertEqual(transport.http_calls, [], '没有 p_skey 就不该发 CGI')
        self.assertFalse(caught.exception.ambiguous, '这条动作根本没发出去，不是"结果未知"')

    async def test_a_transport_without_http_capability_fails_without_asking_for_cookies(self):
        """传输层没有原始 HTTP 能力（打不了 QZone CGI）：明确失败，一个动作都不发。"""
        host = _Host(config=dict(BASE_CONFIG, daily_like_cap=5))
        transport = _NapcatTransport(lambda a, p: {'ok': True, 'error': '', 'data': {}}, has_http=False)
        host.transport = transport
        with self.assertRaises(q.QzoneCgiUnavailable) as caught:
            await host._qzone_run_action(transport.call_onebot, 'like_qzone', {'tid': TID})
        self.assertIn('原始 HTTP', str(caught.exception))
        self.assertEqual(transport.calls, [], '没有 CGI 出口就不许问 cookie、也不许发平台动作')

    async def test_the_cookie_domain_chain_accepts_the_first_usable_domain(self):
        """逐域试：主域给不出 `p_skey` 就换备用域（两个参考实现写法不同）。

        谁给出可用的 `p_skey` 就用谁——"域写错了"与"平台没有这条 API"在失败文案里
        也分得开（逐域写明原因）。取到之后**不再多问**（也不再去问下一个接口）。
        """
        host = _Host(config=dict(BASE_CONFIG, daily_like_cap=5))
        seen: list[tuple[str, dict]] = []

        def handler(action: str, params: dict) -> dict:
            seen.append((action, dict(params)))
            if action in q.QZONE_COOKIE_APIS:
                if params.get('domain') == q.QZONE_COOKIE_DOMAINS[0]:
                    # 主域有这个域自己的 cookie，但**没有 p_skey** → 换了域才可用。
                    return {'ok': True, 'error': '', 'data': {'cookies': 'uin=o1; skey=@x'}}
                return {'ok': True, 'error': '', 'data': {'cookies': COOKIES}}
            return {'ok': True, 'error': '', 'data': {'user_id': 10001}}

        transport = _NapcatTransport(handler, http=lambda *a: '{"code":0}')
        host.transport = transport
        result = await host._qzone_run_action(transport.call_onebot, 'like_qzone', {'tid': TID})
        self.assertIs(result.get('success'), True, result)
        self.assertEqual(
            [params['domain'] for name, params in seen if name in q.QZONE_COOKIE_APIS],
            list(q.QZONE_COOKIE_DOMAINS),
        )
        self.assertEqual([name for name, _ in seen].count('get_credentials'), 2)
        self.assertNotIn('get_cookies', [name for name, _ in seen], '主路通了就别再问下一个接口')
        self.assertEqual(len(transport.http_calls), 1, '第二个域可用就不再往下问')

    async def test_a_failed_cgi_call_never_retries_the_write(self):
        """CGI **已经发出去了**但没回执：记 `unknown`、绝不重发（重发 = 写两次）。"""
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

    async def test_a_platform_side_failure_frame_fails_actionably_without_a_retry(self):
        """NapCat 明确回失败帧（没登录 / Cookie 被清）= 通道不可用 → 明确失败，**不回退**。"""
        host = _Host(config=dict(BASE_CONFIG, daily_like_cap=5))
        seen: list[tuple[str, dict]] = []

        def handler(action: str, params: dict) -> dict:
            seen.append((action, dict(params)))
            if action == 'get_cookies':
                return {'ok': False, 'error': 'NapCat 的 get_cookies 不可用'}
            return {'ok': True, 'error': '', 'data': {'fallback': True}}

        transport = _NapcatTransport(handler)
        host.transport = transport
        with self.assertRaises(q.QzoneCgiUnavailable) as caught:
            await host._qzone_run_action(transport.call_onebot, 'like_qzone', {'tid': TID})
        self.assertIn('get_cookies 不可用', str(caught.exception), '平台的原话要带出来')
        self.assertEqual(
            [name for name, _ in seen],
            ['get_credentials', 'get_credentials', 'get_cookies', 'get_cookies'],
            '两个接口 × 两个域各问一次就停（失败后不许再发空间动作）',
        )
        self.assertEqual(transport.http_calls, [])


# --------------------------------------------------------------------------- #
# 1b. 取凭据：两个接口 + 顺序（`get_credentials` → `get_cookies`，各自逐域）
# --------------------------------------------------------------------------- #


def _credential_handler(calls: list, credentials: bool = True) -> Any:
    """取凭据的 OneBot 直通：`credentials=False` 时模拟"老 NapCat 没有 get_credentials"。"""
    def handler(action: str, params: dict) -> dict:
        calls.append((action, dict(params)))
        if action == 'get_credentials':
            if not credentials:
                return _unusable(action, params)
            return {'ok': True, 'error': '', 'data': {'cookies': COOKIES, 'token': 1869525896}}
        if action == 'get_cookies':
            return {'ok': True, 'error': '', 'data': {'cookies': COOKIES, 'bkn': '1869525896'}}
        if action == 'get_login_info':
            return {'ok': True, 'error': '', 'data': {'user_id': 10001}}
        return {'ok': False, 'error': 'NapCat 没有这个动作'}

    return handler


def _unusable(action: str, params: dict) -> dict:
    """这个取凭据接口**不存在**：宿主的真实形状是 `ActionFailed(retcode 1404)`。"""
    return {
        'ok': False, 'retcode': 1404,
        'error': '%s 失败：failed retcode=1404 不支持的Api %s' % (action, action),
        'data': None,
    }


class CredentialApiOrderTests(unittest.IsolatedAsyncioTestCase):
    """取登录凭据的两条 API 与它们的**顺序**。

    参考插件 `Wyccotccy/astrbot_plugin_qzone_tools` v5.7.5 是
    `get_credentials(domain=…)`（`main.py:866`）→ 失败再 `get_cookies(domain=…)`
    （`main.py:870`），它的 README.md:374 把 `> 4.17.55` 直接绑在"需支持
    `get_credentials` / `get_cookies`"上。顺序不是随手定的：它决定
    "两个都能用时走哪条"——所以①那条用例既钉能力也**钉顺序**。
    """

    def _host(self, handler: Any) -> tuple[Any, Any]:
        host = _Host(config=dict(BASE_CONFIG, daily_like_cap=5))
        transport = _NapcatTransport(handler, http=lambda *a: '{"code":0}')
        host.transport = transport
        return host, transport

    async def test_credentials_api_alone_is_enough(self):
        """① 只有 `get_credentials` 能用（老/新 NapCat 都可能）：通道打通，**不问** `get_cookies`。"""
        calls: list[tuple[str, dict]] = []
        host, transport = self._host(_credential_handler(calls))

        result = await host._qzone_run_action(transport.call_onebot, 'like_qzone', {'tid': TID})

        self.assertIs(result.get('success'), True, result)
        self.assertEqual(
            [name for name, _ in calls], ['get_credentials', 'get_login_info'],
            '顺序：主路先上；通了就不该再去问另一个接口',
        )
        self.assertEqual(len(transport.http_calls), 1, '登录态拿到了就该打 CGI')

    async def test_cookies_api_is_the_fallback(self):
        """② 只有 `get_cookies` 能用（`get_credentials` 回 1404）：仍打通。"""
        calls: list[tuple[str, dict]] = []
        host, transport = self._host(_credential_handler(calls, credentials=False))

        result = await host._qzone_run_action(transport.call_onebot, 'like_qzone', {'tid': TID})

        self.assertIs(result.get('success'), True, result)
        self.assertEqual(
            [name for name, _ in calls],
            ['get_credentials', 'get_credentials', 'get_cookies', 'get_login_info'],
            '主路对两个域各失败一次后，才轮到备路',
        )
        self.assertEqual(len(transport.http_calls), 1)

    async def test_both_apis_missing_fails_actionably_without_any_retry(self):
        """③ 两个接口都不存在（都回 1404）：明确失败、**零重试**、不是 ambiguous。"""
        calls: list[tuple[str, dict]] = []
        host, transport = self._host(lambda a, p: (calls.append((a, dict(p))), _unusable(a, p))[1])

        with self.assertRaises(q.QzoneCgiUnavailable) as caught:
            await host._qzone_run_action(transport.call_onebot, 'like_qzone', {'tid': TID})

        error = caught.exception
        self.assertIs(error.ambiguous, False, '请求压根没发出去，不是"结果未知"')
        text = str(error)
        self.assertIn('点赞说说', text, '要点名动作')
        self.assertIn('get_credentials', text, '要说清第一步为什么没成')
        self.assertIn('get_cookies', text, '也要说清第二步')
        self.assertIn('retcode=1404', text, '平台的原话要带出来')
        self.assertIn('可执行的路', text)
        self.assertEqual(transport.http_calls, [], '拿不到登录态就一个 CGI 都不许打')
        self.assertEqual(
            [name for name, _ in calls],
            ['get_credentials', 'get_credentials', 'get_cookies', 'get_cookies'],
            '每个接口 × 每个域**恰好一次**：不许自动重试',
        )

    async def test_the_failure_text_names_the_napcat_version_requirement(self):
        """④ 两个接口都缺：文案点名"需要支持这两个接口的 NapCat（> 4.17.55）"。"""
        host, transport = self._host(lambda a, p: _unusable(a, p))
        with self.assertRaises(q.QzoneCgiUnavailable) as caught:
            await host._qzone_run_action(transport.call_onebot, 'get_qzone_feeds', {'count': 5})
        text = str(caught.exception)
        self.assertIn('get_credentials', text)
        self.assertIn('get_cookies', text)
        self.assertIn('4.17.55', text, '上不了就是 NapCat 版本不够：把参考插件那条要求写给用户')
        self.assertIn('看好友动态', text)

    async def test_both_apis_are_parsed_with_the_same_unwrapped_shape(self):
        """两种回执**同一个判据**：都是 `{'ok': True, 'data': {'cookies': …}}`（剥壳后的 data）。

        这里断言的是 core 侧：接口名不影响解析（`_cookie_string_from_frame` 一份）；
        "剥壳"本身在适配层一处（`astrbot_bridge._onebot_payload_frame`），见
        `test_astrbot_bridge.OnebotFrameShapeTests`。
        """
        for api in q.QZONE_COOKIE_APIS:
            with self.subTest(api=api):
                def handler(a: str, p: dict) -> dict:
                    if a == api:
                        return {'ok': True, 'error': '', 'data': {'cookies': COOKIES}}
                    if a == 'get_login_info':
                        return {'ok': True, 'error': '', 'data': {'user_id': 10001}}
                    return _unusable(a, p)

                host, transport = self._host(handler)
                auth = await q.qzone_cgi_auth(transport.call_onebot)
                self.assertEqual(auth.uin, '10001')
                self.assertEqual(auth.p_skey, P_SKEY)


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
        self.assertEqual(host.transport.http_calls[0]['data']['uin'], '')

    async def test_without_the_cgi_channel_a_read_fails_actionably_and_sends_nothing(self):
        """没有原始 HTTP 能力 → 读不了好友动态：**明确失败**，一个平台动作都不发。

        （v1.7.1–v1.7.9 这里会回退去打 `get_qzone_feeds`——那个动作名在 AstrBot 的
        任何后端上都不存在，真机换回 `retcode 1404 不支持的Api`。）
        """
        host = _Host(config=dict(BASE_CONFIG))
        transport = _NapcatTransport(
            lambda a, p: {'ok': True, 'error': '', 'data': {'feeds': [{'key': 'K9'}]}},
            has_http=False,
        )
        host.transport = transport
        with self.assertRaises(q.QzoneCgiUnavailable) as caught:
            await host.qzone_read(STORY, 'feed', {'count': 2})
        self.assertIn('看好友动态', str(caught.exception))
        self.assertIn('原始 HTTP', str(caught.exception))
        self.assertEqual(host.actions_called(), [], '不许把不存在的动作名发给平台')

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
# 2c. 自动浏览好友动态也必须走 CGI（v1.7.8 修误路由）
# --------------------------------------------------------------------------- #


def _fresh_feed_text(seconds_ago: int = 1200) -> str:
    """一条"时间窗内"的好友动态（`abstime` 是 CGI 通道的时间字段，秒）。"""
    return (
        "{ver:1,key:'K1',appid:311,uin:10002,nickname:'\u67d0\u4eba',abstime:%d,"
        "html:'<div>\u4eca\u5929\u5929\u6c14\u5f88\u597d</div>',}"
        % int(NOW.timestamp() - seconds_ago)
    )


def _feed_sweep_http(method: str, url: str, headers: object, data: object) -> str:
    """CGI 两个读接口的回包：动态页 + 说说列表（tid 与动态的 `key` 对齐）。"""
    if 'emotion_cgi_msglist' in url:
        return '_preloadCallback({"code":0,"msglist":[{"tid":"K1","content":"\u4eca\u5929\u5929\u6c14\u5f88\u597d"}]});'
    return _fresh_feed_text()


class FeedSweepChannelTests(unittest.IsolatedAsyncioTestCase):
    """**新用例（用户点名）**：自动浏览好友动态走 **QZone CGI 读通道**。

    日志里的铁证是：

        OneBot 动作 get_qzone_feeds 传输异常（结果未知，请勿自动重试）：
        <ActionFailed retcode: 1404, message: '不支持的Api get_qzone_feeds'>

    NapCat **没有** `get_qzone_feeds` / `get_qzone_msg_list` 这两条原生动作（它只有
    发 / 删说说），所以任何把 QQ 空间的**读**当平台动作派出去的地方都是误路由。
    这条回归钉住：默认（有 NapCat WS 通道）路径下，这两个动作名**一个都不会出现**
    在 OneBot 直通里。
    """

    def _sweep_host(self) -> _Host:
        host = _Host(config=dict(BASE_CONFIG, auto_feed=True, daily_comment_cap=1))
        host.transport = _NapcatTransport(_napcat_handler([]), http=_feed_sweep_http)
        return host

    async def test_the_sweep_reads_through_the_qzone_cgi_not_a_platform_action(self):
        host = self._sweep_host()
        await host.qzone_feed_sweep()

        actions = host.actions_called()
        self.assertNotIn('get_qzone_feeds', actions,
                         'NapCat 没有这条动作，绝不许当平台动作发出去（1404 的来源）')
        self.assertNotIn('get_qzone_msg_list', actions)
        self.assertEqual(
            actions,
            ['get_credentials', 'get_login_info', 'get_credentials', 'get_login_info'],
            '一轮轮询 = 两个 CGI 动作各取一次登录态；**零个**空间平台动作',
        )
        urls = [call['url'] for call in host.transport.http_calls]
        self.assertEqual(len(urls), 2, '一条动态 + 一次正文对齐，两条都走 CGI')
        self.assertTrue(any('feeds3_html_more' in url for url in urls))
        self.assertTrue(any('emotion_cgi_msglist_v6' in url for url in urls))
        # 参数按 CGI 的口径发：`pagenum` / `count`（动态）与 `num`（说说正文）。
        feed_call = host.transport.http_calls[0]
        self.assertEqual(feed_call['data']['pagenum'], '1')
        self.assertEqual(feed_call['data']['count'], '20')
        self.assertEqual(host.transport.http_calls[1]['data']['num'], '5')
        self.assertEqual(len(host.entries), 1)
        # v1.9.1（§55）：正文到手就写成**她的观察**（措辞与排序判据都在 chunk13）。
        self.assertIn('[好友动态] 她刷到了 某人 的说说', host.entries[0]['content'])
        self.assertIn('今天天气很好', host.entries[0]['content'])

    async def test_the_sweep_still_records_the_dedupe_ledger_over_the_cgi(self):
        """换通道不许把去重账本弄丢：`feed-seen` 行照旧落，且 tid 就是动态的 `key`。"""
        host = self._sweep_host()
        await host.qzone_feed_sweep()
        seen = [data for table, data in host.created
                if table == 'interlude_qzone_post' and data.get('kind') == 'feed-seen']
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]['tid'], 'K1')

    async def test_the_cgi_time_field_keeps_the_entry_inside_the_freshness_window(self):
        """CGI 那页的时间字段叫 `abstime`（归一化层只认 `time`）：搬不过去就会被当成
        1970 年、被新鲜度过滤整批丢掉（悄悄什么都不进剧本）。"""
        host = self._sweep_host()
        await host.qzone_feed_sweep()
        self.assertEqual(len(host.entries), 1, '时间字段搬运没做对时这里会是 0')

    async def test_without_any_cgi_channel_the_sweep_says_why_and_sends_nothing(self):
        """没有 CGI 通道时轮询**不发任何平台动作**，只留一条可见说明（按小时节流）。

        真机上的表现曾是每轮都打 `get_qzone_feeds` → `retcode 1404`；用户看到的
        除了刷屏的 1404 之外什么都没有。现在是一条说清"缺什么、去哪修"的 warn，
        并且**零个请求**。
        """
        host = _Host(config=dict(BASE_CONFIG, auto_feed=True, daily_comment_cap=1))
        host.transport = _NapcatTransport(_napcat_handler([]), has_http=False)
        await host.qzone_feed_sweep()
        self.assertEqual(host.entries, [])
        self.assertEqual(host.actions_called(), [], '一个动作都不许发（包括取 cookie）')
        warned = host.notes('warn')
        self.assertTrue(warned, '通道缺失必须可见')
        self.assertIn('需要 NapCat 通道', warned[0])
        self.assertIn('get_qzone_feeds', warned[0])


# --------------------------------------------------------------------------- #
# 2b. 改说说可见范围（v1.7.5）
# --------------------------------------------------------------------------- #


class SetVisibilityTests(unittest.IsolatedAsyncioTestCase):
    """`set_qzone_visibility`：**只有** NapCat WS 通道能做，且必须先读回正文。

    v1.7.9 的裁定（H1）：**只发可见性 + 既有字段，富文本字段照参考实现传空串**——
    不带富文本字段是**无副作用**的那条路；"必须重传原图才能改带图说说"只是个推断，
    已连整条下载 + 重传链路一起删掉。留下的边界与**观测**（都能在真机上出声）：

    | 边界 | 出错的样子 |
    | --- | --- |
    | 带图 / 带视频：**零上传、零下载**，只发 update | 又去重传一遍 = 腾讯侧多出副本、原图 URL 换掉、混排视频被丢 |
    | update 后**回读校验**附件与正文 | 服务端真删了附件却没人知道 = 静默毁第二条说说 |
    | 回读发现附件变少 → warn + 剧本 metadata + **跳闸** | 第二次、第三次接着毁 |
    | 跳闸后带附件的说说被拒、**纯文字照常** | 一刀切把功能关死，或者继续冒险 |
    | 转发 → 拒绝 | 编辑的字段清单里没有转发目标那一组，改完等于改掉转发 |
    | 正文找不回来 → 拒绝 | 空 `con` = 把正文清掉 |
    | 没有 CGI 通道 → 明确失败 | 回落到平台打一个不存在的动作名，报错看不懂 |
    """

    def setUp(self) -> None:
        # 跳闸标志落在"数据目录"里；单测给它一个临时目录（生产是插件数据目录）。
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _guard_path(self) -> pathlib.Path:
        return pathlib.Path(self._tmp.name) / 'qzone_visibility_guard.json'

    def _write_guard(self, payload: object) -> None:
        self._guard_path().write_text(json.dumps(payload), encoding='utf-8')

    def _host(self, moods: str = MOODS_TEXT, result: object = '{"code":0}',
              later: object = None, has_fetch: bool = True) -> tuple:
        """`moods` = 改之前读回的那条说说；`later` = 之后每次**回读校验**的响应
        （默认与 `moods` 相同 = 附件与正文一点没变）。

        数据目录**按生产的样子挂**：`ServiceBase` 存的是 `self.ctx`（`chunk2` 读表情库
        目录就是这么取的），而 `self.context` 在生产里根本不存在——这里故意只用 `ctx`，
        免得"跳闸文件写到测试塞的假属性上"这种错误被测试放过。
        """
        host = _Host(config=dict(BASE_CONFIG, daily_post_cap=5, min_interval_minutes=0))
        host.ctx = types.SimpleNamespace(base_dir=self._tmp.name)
        handler, calls = _visibility_http(moods, result, later)
        host.transport = _NapcatTransport(
            _napcat_handler([]), http=handler, has_fetch=has_fetch,
        )
        return host, calls

    def _payload(self, **overrides: object) -> dict:
        payload = {'tid': TID, 'visible': '部分人可见', 'targetUins': ['10002']}
        payload.update(overrides)
        return payload

    @staticmethod
    def _updates(calls: list) -> list:
        return [call for call in calls if 'emotion_cgi_update' in call['url']]

    @staticmethod
    def _uploads(calls: list) -> list:
        return [call for call in calls if 'cgi_upload_image' in call['url']]

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
                update = self._updates(calls)[0]
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
        update = self._updates(calls)[0]
        self.assertEqual(update['method'], 'POST')
        self.assertIn('g_tk=%d' % cgi.compute_g_tk(P_SKEY), update['url'])
        self.assertTrue(update['url'].startswith('https://user.qzone.qq.com/proxy/domain/'))
        # 平台侧**只**被问了取凭据 / 登录信息（读正文一次 + 回读校验一次 + 改可见范围
        # 一次；没有"改可见范围"这条原生动作可打）。
        self.assertEqual(
            {name for name, _ in host.transport.calls},
            {'get_credentials', 'get_login_info'},
        )
        self.assertTrue(
            any('update_visibility' in text for _level, text in host.standalone),
            '通道选择要有一条 debug 记录（排查"到底走没走 NapCat"靠它）：%s' % host.standalone,
        )

    # ---- 带附件：只发可见性，零上传零下载（v1.7.9 的核心正向断言） -------

    async def test_a_post_with_images_updates_without_uploading_or_downloading(self):
        """带图：**只发可见性 + 既有字段**，富文本槽位全空、零上传、零下载。

        这条取代了 v1.7.8 的"先重传原图再 update"——重传是**有副作用**的那条路
        （腾讯侧多出副本、原图 URL 换掉、混排视频被丢），而"不带富文本字段"没有副作用。
        """
        host, calls = self._host(moods=MOODS_TEXT_WITH_PIC)
        result = await host.qzone_execute(STORY, 'visibility', self._payload())
        self.assertTrue(result['ok'], result)
        update = self._updates(calls)[0]
        self.assertEqual(update['data']['richval'], '', '富文本字段照参考实现留空')
        self.assertEqual(update['data']['richtype'], '')
        self.assertEqual(update['data']['subrichtype'], '')
        self.assertEqual(update['data']['pic_template'], '')
        self.assertNotIn('pic_bo', update['data'])
        self.assertEqual(update['data']['con'], '晚安')
        self.assertEqual(update['data']['tid'], TID)
        self.assertEqual(self._uploads(calls), [], '这条路上不许再出现任何上传')
        self.assertEqual(host.transport.fetch_calls, [], '也不许再去下载原图')
        self.assertEqual(host.rows[-1]['status'], 'confirmed')
        # 旧版本那套"重新上传"的痕迹一个都不许留。
        self.assertNotIn('qzone_images_reuploaded', host.entries[-1]['metadata'])
        self.assertNotIn('重新上传', host.entries[-1]['content'])
        self.assertNotIn('重新上传', ' '.join(text for _level, text in host.standalone))
        self.assertEqual(host.notes('warn'), [], '这一轮没有代价，不该有 warn：%s' % host.standalone)

    async def test_a_transport_without_fetch_image_still_updates(self):
        """传输层没有 `fetch_image` 也照常改（旧版本会因此拒绝带图说说）。"""
        host, calls = self._host(moods=MOODS_TEXT_WITH_PIC, has_fetch=False)
        result = await host.qzone_execute(STORY, 'visibility', self._payload())
        self.assertTrue(result['ok'], result)
        self.assertEqual(len(self._updates(calls)), 1)
        self.assertEqual(host.rows[-1]['status'], 'confirmed')

    # ---- 回读校验：把"留空字段不改动附件"这个推断变成每次实测 -----------

    async def test_the_readback_confirms_that_the_attachments_survived(self):
        """附件没变 → 按成功记账，不写 `attachment_loss`、不跳闸。"""
        host, calls = self._host(moods=MOODS_TEXT_WITH_PIC)
        result = await host.qzone_execute(STORY, 'visibility', self._payload())
        self.assertTrue(result['ok'], result)
        self.assertEqual(len(self._updates(calls)), 1)
        self.assertEqual(
            len(host.transport.http_calls), 3,
            '改前读一次 + update 一次 + 改后回读一次',
        )
        self.assertTrue(
            any('回读校验通过' in text for _level, text in host.standalone),
            '校验通过也要留一条 debug（"H1 当场成立"是这么攒出来的）：%s' % host.standalone,
        )
        self.assertNotIn('qzone_visibility_attachment_loss', host.entries[-1]['metadata'])
        self.assertFalse(self._guard_path().exists(), '没实测到丢附件就不该跳闸')

    async def test_an_attachment_loss_warns_trips_the_guard_and_is_recorded(self):
        """**最重要的一条**：回读发现配图没了 → warn + 剧本 metadata + 跳闸标志。"""
        host, _calls = self._host(moods=MOODS_TEXT_WITH_PIC, later=MOODS_TEXT_NO_ATTACHMENTS)
        result = await host.qzone_execute(STORY, 'visibility', self._payload())
        self.assertTrue(result['ok'], 'update 本身是成功的：%s' % result)
        self.assertEqual(host.rows[-1]['status'], 'confirmed')
        warns = host.notes('warn')
        self.assertTrue(
            any('回读校验不通过' in text and '配图从 1 张变成 0 张' in text for text in warns),
            '要说清是"哪一项"变了：%s' % warns,
        )
        self.assertTrue(any('请检查' in text for text in warns), warns)
        self.assertTrue(host.entries[-1]['metadata']['qzone_visibility_attachment_loss'])
        guard = json.loads(self._guard_path().read_text(encoding='utf-8'))
        self.assertTrue(guard['attachmentLoss'])
        self.assertEqual(guard['tid'], TID)
        self.assertIn('配图从 1 张变成 0 张', guard['detail'])

    async def test_a_video_loss_also_trips_the_guard(self):
        """视频与图片一样算附件（视频不在正文里，只看 `pic` 会把它漏掉）。"""
        host, _calls = self._host(moods=MOODS_TEXT_WITH_VIDEO, later=MOODS_TEXT_NO_ATTACHMENTS)
        result = await host.qzone_execute(STORY, 'visibility', self._payload())
        self.assertTrue(result['ok'], result)
        self.assertTrue(
            any('视频从 1 个变成 0 个' in text for text in host.notes('warn')),
            host.notes('warn'),
        )
        self.assertTrue(host.entries[-1]['metadata']['qzone_visibility_attachment_loss'])
        self.assertTrue(json.loads(self._guard_path().read_text(encoding='utf-8'))['attachmentLoss'])

    async def test_a_content_mismatch_warns_without_tripping_the_guard(self):
        """附件没少但正文对不上 → 同样要看见，但不算"附件被删"（不跳闸）。"""
        host, _calls = self._host(
            moods=MOODS_TEXT_WITH_PIC, later=MOODS_TEXT_REWRITTEN,
        )
        result = await host.qzone_execute(STORY, 'visibility', self._payload())
        self.assertTrue(result['ok'], result)
        self.assertTrue(
            any('正文对不上' in text for text in host.notes('warn')), host.notes('warn'),
        )
        self.assertNotIn('qzone_visibility_attachment_loss', host.entries[-1]['metadata'])
        self.assertFalse(self._guard_path().exists())

    async def test_an_unavailable_readback_warns_but_the_update_still_counts(self):
        """回读本身没成（读通道炸了）→ 不改判 update 的成败，但要一条 warn 让用户自己看。"""
        host, _calls = self._host(moods=MOODS_TEXT_WITH_PIC, later=RuntimeError('socket closed'))
        result = await host.qzone_execute(STORY, 'visibility', self._payload())
        self.assertTrue(result['ok'], result)
        self.assertEqual(host.rows[-1]['status'], 'confirmed')
        self.assertTrue(
            any('回读校验没做成' in text for text in host.notes('warn')), host.notes('warn'),
        )
        self.assertNotIn('qzone_visibility_attachment_loss', host.entries[-1]['metadata'])

    # ---- 跳闸：实测过一次丢附件之后，带附件的一律不碰 -------------------

    async def test_the_guard_refuses_attachment_posts_and_text_still_works(self):
        """跳闸 → 带附件的说说被拒（零 update），**纯文字照常**；删掉文件即恢复。"""
        self._write_guard({
            'attachmentLoss': True, 'tid': 'OLD-1', 'detectedAt': '2026-01-01T00:00:00+08:00',
            'detail': '配图从 3 张变成 0 张',
        })
        # ① 带附件：明确拒绝，且**不发** update。
        host, calls = self._host(moods=MOODS_TEXT_WITH_PIC)
        result = await host.qzone_execute(STORY, 'visibility', self._payload())
        self.assertFalse(result['ok'], result)
        self.assertIn('附件', result['error'])
        self.assertIn('纯文字', result['error'])
        self.assertIn('删掉文件', result['error'], '解除办法必须写在拒绝理由里：%s' % result['error'])
        self.assertEqual(self._updates(calls), [])
        self.assertEqual(host.rows[-1]['status'], 'failed')
        self.assertEqual(host.entries, [], '被拒的动作不进剧本')
        # ② 纯文字：不受跳闸影响。
        host2, calls2 = self._host()
        ok = await host2.qzone_execute(STORY, 'visibility', self._payload())
        self.assertTrue(ok['ok'], ok)
        self.assertEqual(len(self._updates(calls2)), 1)
        self.assertEqual(host2.rows[-1]['status'], 'confirmed')
        # ③ 删掉标志文件 = 恢复。
        self._guard_path().unlink()
        host3, calls3 = self._host(moods=MOODS_TEXT_WITH_PIC)
        self.assertTrue(
            (await host3.qzone_execute(STORY, 'visibility', self._payload()))['ok'],
        )
        self.assertEqual(len(self._updates(calls3)), 1)

    async def test_a_broken_guard_file_does_not_lock_the_action(self):
        """坏 JSON 的跳闸文件按"没跳闸"处理（用户的解除办法就是删掉 / 清空它），但要留 warn。"""
        self._guard_path().write_text('{ not json', encoding='utf-8')
        host, calls = self._host(moods=MOODS_TEXT_WITH_PIC)
        result = await host.qzone_execute(STORY, 'visibility', self._payload())
        self.assertTrue(result['ok'], result)
        self.assertEqual(len(self._updates(calls)), 1)
        self.assertTrue(
            any('不是合法 JSON' in text for text in host.notes('warn')), host.notes('warn'),
        )

    async def test_an_explicitly_released_guard_file_lets_attachments_through(self):
        """`attachmentLoss` 为假 / 缺这个键 = 用户已解除：带附件的照常改。"""
        for payload in ({'attachmentLoss': False}, {}, {'note': '看过了，没事'}):
            with self.subTest(payload=payload):
                self._write_guard(payload)
                host, calls = self._host(moods=MOODS_TEXT_WITH_PIC)
                result = await host.qzone_execute(STORY, 'visibility', self._payload())
                self.assertTrue(result['ok'], result)
                self.assertEqual(len(self._updates(calls)), 1)

    # ---- 其余边界（v1.7.5 起就有，v1.7.9 一字未改） ---------------------

    async def test_the_text_path_still_sends_no_rich_text_block(self):
        """纯文字路径**一个字节都没变**（字段集合与顺序仍是参考实现那一份）。"""
        host, calls = self._host()
        result = await host.qzone_execute(STORY, 'visibility', self._payload())
        self.assertTrue(result['ok'], result)
        self.assertEqual(host.transport.fetch_calls, [])
        self.assertEqual(self._uploads(calls), [])
        update = self._updates(calls)[0]
        self.assertNotIn('pic_bo', update['data'])
        self.assertEqual(update['data']['richval'], '')
        self.assertEqual(update['data']['richtype'], '')
        self.assertEqual(update['data']['subrichtype'], '')
        self.assertNotIn('qzone_images_reuploaded', host.entries[-1]['metadata'])

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
        self.assertEqual(self._updates(calls), [])
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
        """没有原始 HTTP 能力：明确失败，**不**往平台打任何动作（那条动作不存在）。"""
        host = _Host(config=dict(BASE_CONFIG, daily_post_cap=5, min_interval_minutes=0))
        host.ctx = types.SimpleNamespace(base_dir=self._tmp.name)
        host.transport = _NapcatTransport(_napcat_handler([]), has_http=False)
        result = await host.qzone_execute(STORY, 'visibility', self._payload())
        self.assertFalse(result['ok'], result)
        self.assertIn('NapCat', result['error'])
        self.assertEqual(host.transport.calls, [], '不该往平台打任何动作（根本没有这条动作）')
        self.assertEqual(host.rows[-1]['status'], 'failed')

    async def test_the_guard_file_lands_in_the_data_dir_the_production_host_exposes(self):
        """跳闸文件真的落在 `ctx.base_dir` 里（生产挂的就是它），并且三种持有点都认。

        这条是因为"数据目录读错属性"在本仓库有前科（读法一错就是**静默失效**）：
        `ServiceBase` 存的是 `self.ctx`；若照抄 `self.context`（生产里根本没有这个属性），
        跳闸会永远写不出去、也永远拦不住。
        """
        for holder in ('ctx', 'context', 'self'):
            with self.subTest(holder=holder):
                host, _calls = self._host(
                    moods=MOODS_TEXT_WITH_PIC, later=MOODS_TEXT_NO_ATTACHMENTS,
                )
                if holder != 'ctx':
                    del host.ctx
                    if holder == 'context':
                        host.context = types.SimpleNamespace(base_dir=self._tmp.name)
                    else:
                        host.base_dir = self._tmp.name
                self.assertTrue(
                    (await host.qzone_execute(STORY, 'visibility', self._payload()))['ok'],
                )
                self.assertTrue(
                    self._guard_path().exists(),
                    '跳闸标志必须落在数据目录里（持有点=%s）' % holder,
                )
                self._guard_path().unlink()

    async def test_a_transport_error_is_recorded_as_unknown_not_failed(self):
        """传输层异常 → `unknown`（v1.7.6 改口径）。

        原先判 `failed`（理由：改可见范围是幂等的，重试安全）。现在与**其它四条写
        路径**统一：`call_qzone_cgi` 拿不到响应就标 `ambiguous`，审计行写 `unknown`
        ——"可能已经生效"的东西一律不自动重试。幂等动作少一次重试是可接受的代价，
        而"某一条路径单独用另一套口径"正是重复发帖那类事故的温床。
        """
        host, _calls = self._host(result=RuntimeError('socket closed'))
        result = await host.qzone_execute(STORY, 'visibility', self._payload())
        self.assertFalse(result['ok'], result)
        self.assertEqual(host.rows[-1]['status'], 'unknown')
        self.assertIn('socket closed', result['error'])
        self.assertIn('结果未知', result['error'])
        self.assertIn('不会自动重试', result['error'])

    async def test_the_service_layer_never_reaches_for_an_upload_or_a_download(self):
        """源码哨兵：`ServiceChunk13` 的**代码**里不许再出现下载 / 上传 / `richval` 入口。

        v1.7.8 那条路是"下载原图 → 逐张重传 → 拼新 `richval`"，它的副作用是真的
        （腾讯侧多出副本），所以这里钉住它不会悄悄长回来——要重新引入必须先改这条断言。
        只扫**代码**（AST 的标识符 / 属性 / 字符串字面量），文档与注释里解释这段历史的
        文字不算（那是必须留下的记录）。
        """
        import ast
        import inspect

        from plugin.core.service.chunk13 import ServiceChunk13

        tree = ast.parse(inspect.getsource(ServiceChunk13))
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
                body = getattr(node, 'body', [])
                if (body and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)
                        and isinstance(body[0].value.value, str)):
                    docstrings.add(id(body[0].value))
        symbols: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                symbols.append(node.attr)
            elif isinstance(node, ast.Name):
                symbols.append(node.id)
            elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in docstrings):
                symbols.append(node.value)
        for needle in ('fetch_image', 'cgi_upload_image', 'upload_image', 'richval', 'pic_bo'):
            with self.subTest(needle=needle):
                self.assertFalse(
                    [symbol for symbol in symbols if needle in symbol],
                    'v1.7.9 起改可见范围这条路上不许再出现 %s（要接回来先改这条断言）' % needle,
                )


# --------------------------------------------------------------------------- #
# 2c. CGI 通道的失败分类（v1.7.6）：拿不到响应 = unknown，接口拒绝 = failed
# --------------------------------------------------------------------------- #


async def _auth_only(action: str, params: dict) -> dict:
    """只够 `qzone_cgi_auth` 用的 OneBot 直通（`get_cookies` + `get_login_info`）。"""
    if action == 'get_cookies':
        return {'ok': True, 'error': '', 'data': {'cookies': COOKIES}}
    return {'ok': True, 'error': '', 'data': {'user_id': 10001}}


class CgiOutcomeClassificationTests(unittest.IsolatedAsyncioTestCase):
    """`call_qzone_cgi` 的三分类（与 `call_qzone_action` **同一套口径**）。

    这几条是**重复发帖**的最后一道闸门：传输层拿不到响应时若记成 `failed`，调用方
    会认为"没发出去、可以重试"，于是同一个 tid 出现两条说说。
    """

    async def _call(self, request: object) -> dict:
        return await q.call_qzone_cgi(
            request, _auth_only, 'comment', {'tid': TID, 'content': '好看'},
        )

    async def test_a_raising_transport_is_ambiguous(self):
        """① 传输层抛异常（超时 / 断连）→ `ambiguous`，且**不是** `QzoneCgiUnavailable`。

        这一点必须钉住：`_qzone_run_action` 只把 `QzoneCgiUnavailable`（拿不到登录态）
        当成"通道没接上"；写动作的**传输异常**要是落进那个类，就会被当成"通道不可用"
        再发一次 = 重复评论。
        """
        async def request(method: str, url: str, headers: object = None, data: object = None) -> object:
            raise RuntimeError('socket closed')

        with self.assertRaises(q.QzoneActionError) as caught:
            await self._call(request)
        self.assertIs(caught.exception.ambiguous, True)
        self.assertNotIsInstance(caught.exception, q.QzoneCgiUnavailable)
        self.assertIn('结果未知，请勿自动重试', str(caught.exception))
        self.assertIn('socket closed', str(caught.exception))

    async def test_a_none_response_is_ambiguous_too(self):
        """① 传输层按约定回 `None`（失败只记 debug）→ 同样 `ambiguous`。"""

        async def request(method: str, url: str, headers: object = None, data: object = None) -> object:
            return None

        with self.assertRaises(q.QzoneActionError) as caught:
            await self._call(request)
        self.assertIs(caught.exception.ambiguous, True)
        self.assertIn('结果未知，请勿自动重试', str(caught.exception))

    async def test_an_explicit_cgi_rejection_is_failed(self):
        """② 拿到响应且 `code != 0`（没登录 / 风控 / 参数不对）→ `failed`，重试安全。"""
        async def request(method: str, url: str, headers: object = None, data: object = None) -> object:
            return '{"code":-3000,"message":"操作太频繁"}'

        with self.assertRaises(q.QzoneActionError) as caught:
            await self._call(request)
        self.assertIs(caught.exception.ambiguous, False)
        self.assertEqual(caught.exception.retcode, -3000)
        self.assertIn('操作太频繁', str(caught.exception))
        self.assertNotIn('结果未知', str(caught.exception))

    async def test_a_read_only_action_may_be_retried_and_says_so(self):
        """只读（feed / moods）拿不到响应 → `ambiguous=False` + "可重试"。

        读不产生副作用，没必要按写动作的保守口径吓自己；更要紧的是**文案里不能出现
        "请勿自动重试"**——日志层早先被我们自己写的这句否定式坑过（标签打成
        `[自动重试]`，与正文说的正好相反）。
        """
        async def request(method: str, url: str, headers: object = None, data: object = None) -> object:
            raise RuntimeError('socket closed')

        for action in ('feed', 'moods'):
            with self.subTest(action=action):
                with self.assertRaises(q.QzoneActionError) as caught:
                    await q.call_qzone_cgi(request, _auth_only, action, {'count': 5})
                self.assertIs(caught.exception.ambiguous, False, '只读动作重试安全')
                self.assertIn('读取失败', str(caught.exception))
                self.assertIn('重试不会产生副作用', str(caught.exception))
                self.assertNotIn('请勿自动重试', str(caught.exception))
                # 内部动作键（`get_qzone_feeds` / `get_qzone_msg_list`）也是同一分类。
        for action in ('get_qzone_feeds', 'get_qzone_msg_list'):
            with self.subTest(action=action):
                self.assertIs(q.qzone_action_is_read(action), True)

    async def test_a_read_only_action_without_a_receipt_is_not_ambiguous_either(self):
        async def request(method: str, url: str, headers: object = None, data: object = None) -> object:
            return None

        with self.assertRaises(q.QzoneActionError) as caught:
            await q.call_qzone_cgi(request, _auth_only, 'feed', {'count': 5})
        self.assertIs(caught.exception.ambiguous, False)
        self.assertNotIn('请勿自动重试', str(caught.exception))

    async def test_a_write_action_says_do_not_retry(self):
        """反向用例：写动作（comment / like / forward / publish / delete / visibility）
        拿不到响应一律 `ambiguous=True` + "结果未知，请勿自动重试"。"""
        async def request(method: str, url: str, headers: object = None, data: object = None) -> object:
            return None

        for action in ('comment', 'like', 'forward', 'publish', 'delete', 'update_visibility'):
            with self.subTest(action=action):
                with self.assertRaises(q.QzoneActionError) as caught:
                    await q.call_qzone_cgi(request, _auth_only, action, {'tid': TID, 'content': 'x'})
                self.assertIs(caught.exception.ambiguous, True, '%s 是写动作，不许重试' % action)
                self.assertIn('结果未知，请勿自动重试', str(caught.exception))
                self.assertIs(q.qzone_action_is_read(action), False)

    async def test_a_successful_call_returns_the_parsed_result(self):
        """③ 成功：回 `success_or_error` 解析出来的那份结果。"""
        async def request(method: str, url: str, headers: object = None, data: object = None) -> object:
            return '{"code":0,"tid":"%s"}' % TID

        result = await self._call(request)
        self.assertIs(result['success'], True)
        self.assertEqual(result['tid'], TID)


class WritePathAmbiguityTests(unittest.IsolatedAsyncioTestCase):
    """④ **五条写路径**在传输失败时都必须记 `unknown`（不能有一条漏标）。

    发帖 / 评论 / 点赞 / 转发四条走各自的通道（发帖是平台原生 `send_qzone_msg`，
    其余优先 NapCat WS 的 QZone CGI），改可见范围是本移植版补的第五条。
    参数化跑一遍，避免"某一条路径单独另一套口径"。
    """

    #: 每条路径一条最低限度能过门的 payload。
    CASES = {
        'post': {'content': '今天天气不错'},
        'comment': {'tid': TID, 'content': '好看', 'targetUin': FRIEND_UIN},
        'like': {'tid': TID, 'targetUin': FRIEND_UIN},
        'forward': {'tid': TID},
        'visibility': {'tid': TID, 'visible': '所有人可见'},
    }

    def _host_for(self, kind: str) -> _Host:
        """造一个"写请求一定拿不到响应"的宿主；读请求（找回正文）照常成功。"""
        host = _Host(config=dict(
            BASE_CONFIG, daily_post_cap=5, daily_comment_cap=5, daily_like_cap=5,
            min_interval_minutes=0,
        ))
        if kind == 'post':
            # 发帖走平台原生动作（NapCat 原生只有发/删说说）：让 OneBot 直通抛异常。
            def handler(action: str, params: dict) -> dict:
                raise RuntimeError('socket closed')

            host.transport = _NapcatTransport(handler, has_http=False)
            return host

        def http(method: str, url: str, headers: object, data: object) -> object:
            if 'emotion_cgi_msglist_v6' in url:  # 改可见范围要先读回正文
                return MOODS_TEXT
            raise RuntimeError('socket closed')

        host.transport = _NapcatTransport(_napcat_handler([]), http=http)
        return host

    async def test_every_write_path_records_a_missing_response_as_unknown(self):
        for kind, payload in self.CASES.items():
            with self.subTest(kind=kind):
                host = self._host_for(kind)
                result = await host.qzone_execute(STORY, kind, payload)

                self.assertFalse(result['ok'], result)
                self.assertIn('结果未知', result['error'], '调用方要能看出"可能已发生"')
                self.assertIn('不会自动重试', result['error'])
                row = host.rows[-1]
                self.assertEqual(row['status'], 'unknown', '记 failed 就会招来重试 = 重复动作')
                self.assertIn('可能已生效', row['error'], '审计行自己也要写明')
                self.assertTrue(
                    any('无法确认是否生效' in text for text in host.notes('warn')),
                    host.standalone,
                )

    async def test_an_unknown_outcome_blocks_the_immediate_retry(self):
        """`unknown` 的实际后果：马上重试会被限流门挡下（保守按"已发生"计入）。

        "不重试"不是一句口号——它落在 `evaluate_qzone_gate` 上：`unknown` 计间隔、
        计配额，所以调用方即使想重发也会先撞门。被拦下的那次**不落审计行**。
        """
        host = _Host(config=dict(
            BASE_CONFIG, daily_comment_cap=5, min_interval_minutes=90,
        ))
        host.transport = _NapcatTransport(
            _napcat_handler([]), http=lambda *a: RuntimeError('socket closed'),
        )
        first = await host.qzone_execute(STORY, 'comment', {
            'tid': TID, 'content': '好看', 'targetUin': FRIEND_UIN,
        })
        self.assertFalse(first['ok'], first)
        self.assertEqual(host.rows[-1]['status'], 'unknown')

        second = await host.qzone_execute(STORY, 'comment', {
            'tid': TID, 'content': '好看', 'targetUin': FRIEND_UIN,
        })
        self.assertFalse(second['ok'], second)
        self.assertIn('最小间隔', second['error'])
        self.assertEqual(len([row for row in host.rows if row.get('kind') == 'comment']), 1,
                         '被门拦下的重试不该再落一条审计行')

    async def test_every_write_path_records_an_explicit_rejection_as_failed(self):
        """反面：接口**明确拒绝**（`code != 0`）时五条路径都记 `failed`（重试安全）。"""
        for kind, payload in self.CASES.items():
            with self.subTest(kind=kind):
                host = _Host(config=dict(
                    BASE_CONFIG, daily_post_cap=5, daily_comment_cap=5, daily_like_cap=5,
                    min_interval_minutes=0,
                ))
                if kind == 'post':
                    def handler(action: str, params: dict) -> dict:
                        return {'ok': False, 'error': '操作过于频繁', 'retcode': 1200}

                    host.transport = _NapcatTransport(handler, has_http=False)
                else:
                    def http(method: str, url: str, headers: object, data: object) -> object:
                        if 'emotion_cgi_msglist_v6' in url:
                            return MOODS_TEXT
                        return '{"code":-3000,"message":"操作太频繁"}'

                    host.transport = _NapcatTransport(_napcat_handler([]), http=http)
                result = await host.qzone_execute(STORY, kind, payload)

                self.assertFalse(result['ok'], result)
                self.assertNotIn('可能已生效', result['error'])
                self.assertEqual(host.rows[-1]['status'], 'failed')


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

        v1.7.10：`OneBot` / `NapCat` 两档表达的是**现在真实存在的差别**——
        NapCat 那 9 条要么是 NapCat 原生动作、要么要靠 NapCat 的 `get_cookies` 打
        QZone CGI；换成别的 OneBot 实现（Lagrange / LLOneBot / go-cqhttp）拿不到 cookie，
        这些动作就做不了。所以面板上的"NapCat 专属"徽章与筛选**仍然有意义**，保留。
        """
        self.assertEqual(pa.BACKEND_LABELS, {
            'onebot': '标准 OneBot',
            'napcat': 'NapCat 专属',
        })
        # 删除哨兵：后端集合只能是这两档（`BACKEND_LABELS` 是唯一真源）。
        # 上游 Koishi 那套扩展动作名（v1.7.10 删除）**刻意不在这里点名**——
        # 点名等于给它留了个位置；这条断言对任何"新加一个后端名"都会红。
        for action in pa.ACTIONS.values():
            with self.subTest(action=action.id):
                self.assertTrue(
                    set(action.backends) <= set(pa.BACKEND_LABELS),
                    '%s 声明了表外的后端：%s' % (action.id, action.backends),
                )

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
        # v1.7.6：`qzone.QZONE_NAPCAT_ONLY_ACTIONS` 已删（只有测试引用过它）。NapCat
        # 专属动作的**唯一真源**是目录：`napcat_actions()` 由每条 `PlatformAction.backends`
        # 派生，面板徽章 / 筛选与运行期优先级都读它。
        self.assertFalse(
            hasattr(q, 'QZONE_NAPCAT_ONLY_ACTIONS'),
            'NapCat 专属清单只能有目录这一个真源',
        )
        self.assertEqual({action.id for action in pa.napcat_actions()}, set(NAPCAT_ONLY_IDS))

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
